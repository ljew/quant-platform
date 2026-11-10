"""基金持续性 API：读 fund_stat_cache 预计算结果。

设计：
- 统计口径固定为 2015–2025 完整年度、年度等权 t（不用 pooled 观测，避免显著性虚高）。
- 数据由 fm_analysis/build_stats.py 从 fund_master / fund_nav_year 计算后写入缓存表，
  接口只做读取，不做实时计算（7600 只 × 11 年的分位排名不适合放在请求链路里）。
- 缓存缺失时返回 ok=false 并给出原因，不返回空壳结构（避免前端显示成「没有数据」）。
"""
from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter
from sqlalchemy import text

from app.database import SessionLocal

router = APIRouter(prefix="/fund", tags=["fund"])

# 持仓缺口查询要扫全量净值表（约 3 秒），按报告期缓存 10 分钟
_gaps_cache: dict = {"key": None, "at": 0.0, "val": None}


@router.get("/persistence")
def persistence():
    """梯队迁移 / 次年超额 / 经理层对照 / 分母体检。"""
    db = SessionLocal()
    try:
        row = db.execute(
            text("SELECT payload_json, updated_at FROM fund_stat_cache WHERE key = 'persistence'")
        ).fetchone()
    except Exception as e:  # 表还没建（首次部署）
        return {"ok": False, "error": f"统计缓存表不存在：{e}"}
    finally:
        db.close()
    if not row:
        return {"ok": False, "error": "统计缓存为空，请先在 fm_analysis 下运行 build_stats.py"}
    data = json.loads(row[0])
    data["ok"] = True
    data["cached_at"] = row[1]
    return data


@router.get("/status")
def status():
    """基金数据覆盖度：用于页面顶部提示「数据到什么时候」。"""
    db = SessionLocal()
    try:
        r = db.execute(text("""
            SELECT (SELECT COUNT(*) FROM fund_master)                AS n_master,
                   (SELECT COUNT(*) FROM fund_nav_year)              AS n_nav,
                   (SELECT COUNT(*) FROM fund_manager_tenure)        AS n_tenure,
                   (SELECT MAX(year) FROM fund_nav_year)             AS last_year,
                   (SELECT updated_at FROM fund_stat_cache WHERE key='persistence') AS cached_at
        """)).fetchone()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        db.close()
    return {"ok": True, "n_master": r[0], "n_nav": r[1], "n_tenure": r[2],
            "last_year": r[3], "cached_at": r[4]}


# ---------------------------------------------------------------------------
# 持仓补齐 / 行业穿透（只读）
#
# 数据源：backend/scripts/fetch_fund_holdings.py 拉明细，
#         backend/scripts/build_fund_penetration.py 预计算后写入
#         fund_hold_coverage / fund_industry_exposure / fund_stat_cache。
# 请求链路不做重算；单基金明细走 by_fund/{code}.csv 分片，避免扫全量 CSV。
# 双轨纪律：disclosed 为真实披露，carried 为锚点继承的影子持仓，
#           仅用于行业结构分母校正，绝不进收益/归因结论。
# ---------------------------------------------------------------------------
# 独立研究库：宿主机离线脚本独占写，容器内 API 一律只读，
# 避免 bind mount 上两边同时写 quant_dev.db 造成 WAL 视图错乱。
HOLDINGS_DIR = Path(os.getenv("QUANT_HOLDINGS_DIR", "/data/holdings"))
RESEARCH_DIR = Path(os.getenv("QUANT_RESEARCH_DIR", "/data/research"))
RESEARCH_DB = Path(os.getenv("QUANT_RESEARCH_DB", "/data/fund_research.db"))
CODE_RE = re.compile(r"^\d{6}(\.(OF|SH|SZ))?$", re.I)


def _research(sql: str, params=()):
    """独立研究库只读查询（短连接，用完即关）。"""
    con = sqlite3.connect(f"file:{RESEARCH_DB}?mode=ro", uri=True, timeout=10)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def _median(xs):
    """去掉 None 后取中位数，空集返回 None。"""
    vals = sorted(float(x) for x in xs if x is not None)
    if not vals:
        return None
    n = len(vals)
    mid = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    return round(mid, 2)


@router.get("/penetration/overview")
def penetration_overview():
    """总览：覆盖度修复前后、按报告期口径、行业配置、集中度与行业映射质量。"""
    try:
        rows = _research(
            "SELECT payload_json, updated_at FROM fund_stat_cache WHERE key='penetration'")
        row = rows[0] if rows else None
    except Exception as e:
        return {"ok": False, "error": f"穿透缓存读取失败：{e}"}
    if not row:
        return {"ok": False, "error": "尚无穿透缓存，请先运行 backend/scripts/build_fund_penetration.py"}
    data = json.loads(row[0])
    # 拉数进度实时覆盖：缓存是离线生成的，里面的 done 会滞后于后台拉取
    pf = HOLDINGS_DIR / "progress.json"
    if pf.exists():
        try:
            done_n = len(json.loads(pf.read_text(encoding="utf-8")).get("done", []))
            total = (data.get("fetch_progress") or {}).get("total", 0)
            if total:
                data["fetch_progress"] = {
                    "done": done_n, "total": total,
                    "completed": done_n >= total,
                    "pct": round(done_n / total * 100, 1),
                }
        except Exception:
            pass
    data["ok"] = True
    data["cached_at"] = row[1]
    return data


@router.get("/penetration/gaps")
def penetration_gaps(end_date: str = ""):
    """最新报告期的持仓缺口：近期仍有净值、却没有该期持仓披露的基金。

    为什么必须单独给出（实测 2026Q3 有 206 只）
    ------------------------------------------
    Tushare 的 fund_portfolio 与天天基金 F10 对这些基金都没有三季报数据，
    属**源头未披露**，不是抓取遗漏（12,092 只全部抓完、0 失败）。
    但页面上只写「覆盖 8,238 只」，会被误读成「全市场只有 8,238 只基金」。
    给出分母与缺口名单，才知道「补齐」补到了什么程度、还剩哪些真的补不了。

    这个查询要扫 1,160 万行净值表（约 3 秒），所以独立成接口并缓存，
    不塞进首屏的 overview。
    """
    key = (end_date.strip(),)
    if _gaps_cache["key"] == key and time.time() - _gaps_cache["at"] < 600:
        return _gaps_cache["val"]
    try:
        if not end_date:
            r = _research("SELECT MAX(end_date) FROM fund_hold_coverage")
            end_date = (r[0][0] if r and r[0][0] else "")
        if not end_date:
            return {"ok": False, "error": "覆盖表为空"}
        ld = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
        base = """
            FROM (SELECT DISTINCT ts_code FROM fund_nav_daily
                  WHERE nav_date >= date(?, '-45 day') AND ts_code LIKE '%.OF') n
            LEFT JOIN fund_master_lite m ON m.ts_code = n.ts_code
            WHERE NOT EXISTS (SELECT 1 FROM fund_hold_coverage c
                              WHERE c.ts_code = n.ts_code AND c.end_date = ?)
        """
        total = _research("SELECT COUNT(*)" + base, (ld, end_date))[0][0]
        have = _research(
            "SELECT COUNT(DISTINCT ts_code) FROM fund_hold_coverage WHERE end_date=?",
            (end_date,))[0][0]
        rows = _research(
            "SELECT n.ts_code, COALESCE(m.name,'') " + base +
            " ORDER BY n.ts_code LIMIT 60", (ld, end_date))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    val = {"ok": True, "end_date": end_date, "active_funds_45d": total + have,
           "with_holdings": have, "missing": total,
           "missing_pct": round(total / max(total + have, 1) * 100, 2),
           "items": [{"ts_code": r[0], "name": r[1]} for r in rows],
           "note": "缺口为源头未披露（Tushare 与天天基金均无该期数据），非抓取遗漏"}
    _gaps_cache.update({"key": key, "at": time.time(), "val": val})
    return val


@router.get("/penetration/recheck")
def penetration_recheck():
    """用修复后的完整年报持仓复核研究卡 20/21 的结论（离线脚本产物，只读）。"""
    f = RESEARCH_DIR / "holdings_recheck.json"
    if not f.exists():
        return {"ok": False, "error": "复核结果尚未生成，请运行 rebuild_holdings_research.py"}
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "error": f"复核结果读取失败：{e}"}
    d["ok"] = True
    return d


@router.get("/penetration/regression-holdings")
def penetration_regression_holdings(code: str = "", end_date: str = "", limit: int = 30):
    """第五轨：把约束 WLS 的行业 β 反推到个股级，与贝叶斯补齐逐股对比。

    独立推断轨，不覆盖 disclosed/carried 主口径；quality=low 的截面仅供参考。
    """
    code = code.strip().upper()
    if not code or not CODE_RE.match(code):
        return {"ok": False, "error": "需 6 位基金代码"}
    try:
        summary_row = _research(
            "SELECT payload_json,updated_at FROM fund_regression_holdings_summary WHERE k='main'")
        summary = json.loads(summary_row[0][0]) if summary_row else {}
    except Exception:
        summary = {}

    try:
        real = _research(
            "SELECT DISTINCT ts_code FROM fund_hold_coverage WHERE base_code=? LIMIT 1",
            (code[:6],))
        ts_code = real[0][0] if real else (code if "." in code else f"{code}.OF")
        dates = [r[0] for r in _research(
            "SELECT DISTINCT end_date FROM fund_regression_holdings_meta "
            "WHERE ts_code=? ORDER BY end_date", (ts_code,))]
        if not dates:
            return {"ok": True, "available": False, "reason": "该基金无回归隐含持仓",
                    "summary": summary}
        ed = end_date if end_date in dates else dates[-1]
        m = _research("""
            SELECT r2,spearman,n_obs,beta_total,disclosed_total,implied_total,
                   bayes_total,l1_vs_bayes,n_stocks,n_proxy,quality
            FROM fund_regression_holdings_meta WHERE ts_code=? AND end_date=?
        """, (ts_code, ed))
        meta = None
        if m:
            r = m[0]
            meta = {"r2": r[0], "spearman": r[1], "n_obs": r[2],
                    "beta_total": r[3], "disclosed_total": r[4],
                    "implied_total": r[5], "bayes_total": r[6],
                    "l1_vs_bayes": r[7], "n_stocks": r[8], "n_proxy": r[9],
                    "quality": r[10]}
        rows = _research("""
            SELECT symbol,industry,beta_w,bayes_w,diff,source,confidence,quality
            FROM fund_regression_holdings WHERE ts_code=? AND end_date=?
            ORDER BY ABS(diff) DESC LIMIT ?
        """, (ts_code, ed, min(max(limit, 1), 200)))
        by_src = _research("""
            SELECT source,COUNT(*),ROUND(SUM(beta_w),2) FROM fund_regression_holdings
            WHERE ts_code=? AND end_date=? GROUP BY source
        """, (ts_code, ed))
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True, "available": True, "ts_code": ts_code, "end_date": ed,
        "dates": dates, "meta": meta, "summary": summary,
        "by_source": [{"source": r[0], "n": r[1], "weight": r[2]} for r in by_src],
        "items": [{"symbol": r[0], "industry": r[1], "beta_w": r[2],
                   "bayes_w": r[3], "diff": r[4], "source": r[5],
                   "confidence": r[6], "quality": r[7]} for r in rows],
    }


@router.get("/penetration/fof/layer")
def penetration_fof_layer():
    """FOF 基金层穿透总览。

    数据来自定期报告 PDF 的「基金投资明细」（fetch_fof_reports.py 解析），
    再叠加各底层基金的监管 A 股仓位算两层穿透（audit_fof_holdings.py）。
    """
    try:
        rep = _research(
            "SELECT report_type, parse_status, COUNT(*) FROM fund_fof_reports GROUP BY 1,2")
        tot = _research("SELECT COUNT(*), COUNT(DISTINCT base_code) FROM fund_fof_reports")
        n_rows = _research("SELECT COUNT(*) FROM fund_fof_holdings")
        as_of = _research("SELECT MAX(end_date) FROM fund_fof_reports WHERE parse_status='ok'")
        rows = _research("""
            SELECT p.base_code, COALESCE(m.name,''), p.end_date, p.report_type,
                   p.n_funds, p.w_total, p.w_pool, p.w_equity, p.a_share_decl,
                   p.eq_total, p.n_missing_eq
            FROM fund_fof_layer_check p
            LEFT JOIN fund_fof_share_map m
                   ON m.share_code = p.base_code AND m.is_rep = 1
            WHERE p.end_date = (SELECT MAX(end_date) FROM fund_fof_layer_check p2
                                WHERE p2.base_code = p.base_code)
            ORDER BY p.w_total DESC
        """)
    except Exception as e:
        return {"ok": False, "error": f"FOF 基金层数据尚未生成：{e}"}

    n_rep = tot[0][0] if tot else 0
    ok_rep = sum(r[2] for r in rep if r[1] == "ok")
    funds = [{
        "base_code": r[0], "name": r[1], "end_date": r[2], "report_type": r[3],
        "n_funds": r[4], "w_total": r[5], "w_pool": r[6], "w_equity": r[7],
        "a_share_decl": r[8], "eq_total": r[9], "n_missing_eq": r[10],
    } for r in rows]
    return {
        "ok": True,
        "summary": {
            "n_reports": n_rep,
            "n_fof": tot[0][1] if tot else 0,
            "n_holdings": n_rows[0][0] if n_rows else 0,
            "success_rate": round(ok_rep / n_rep, 4) if n_rep else None,
            "as_of": as_of[0][0] if as_of else None,
            "n_funds_latest": len(funds),
            "w_total_med": _median([f["w_total"] for f in funds]),
            "w_pool_med": _median([f["w_pool"] for f in funds]),
        },
        "by_type": [{"report_type": r[0], "status": r[1], "n": r[2]} for r in rep],
        "funds": funds,
    }


@router.get("/penetration/fof/layer/{code}")
def penetration_fof_layer_detail(code: str):
    """单只 FOF 的基金层明细与多期穿透序列。"""
    base = code[:6]
    try:
        rep = _research("SELECT rep_code FROM fund_fof_share_map WHERE share_code=?", (base,))
        rc = rep[0][0] if rep else base
        series = _research("""
            SELECT end_date, report_type, n_funds, w_total, w_pool, w_equity,
                   a_share_decl, eq_total, n_missing_eq
            FROM fund_fof_layer_check WHERE base_code=? ORDER BY end_date DESC
        """, (rc,))
    except Exception as e:
        return {"ok": False, "error": f"FOF 基金层数据尚未生成：{e}"}

    if not series:
        return {"ok": False, "error": "该基金没有可穿透的基金层披露（可能是 QDII-FOF）"}

    name = ""
    nm = _research("SELECT name FROM fund_fof_share_map WHERE share_code=?", (rc,))
    if nm and nm[0][0]:
        name = nm[0][0]

    ed, rt = series[0][0], series[0][1]
    try:
        hold = _research("""
            SELECT seq, sub_code, sub_name, operate_mode, shares, mkv,
                   weight_pct, related_party
            FROM fund_fof_holdings
            WHERE base_code=? AND end_date=? AND report_type=?
            ORDER BY weight_pct DESC
        """, (rc, ed, rt))
    except Exception:
        hold = []

    return {
        "ok": True,
        "base_code": base, "parsed_code": rc, "name": name,
        "latest": {
            "end_date": ed, "report_type": rt, "n_funds": series[0][2],
            "w_total": series[0][3], "w_pool": series[0][4], "w_equity": series[0][5],
            "a_share_decl": series[0][6], "eq_total": series[0][7],
            "n_missing_eq": series[0][8],
        },
        "series": [{
            "end_date": r[0], "report_type": r[1], "n_funds": r[2],
            "w_total": r[3], "w_pool": r[4], "w_equity": r[5],
            "a_share_decl": r[6], "eq_total": r[7], "n_missing_eq": r[8],
        } for r in series],
        "holdings": [{
            "seq": r[0], "sub_code": r[1], "sub_name": r[2], "operate_mode": r[3],
            "shares": r[4], "mkv": r[5], "weight_pct": r[6], "related_party": r[7],
        } for r in hold],
    }


@router.get("/penetration/fof")
def penetration_fof():
    """FOF 可穿透性诊断：当前数据源拿不到被投基金，量化黑箱边界与排除建议。"""
    try:
        row = _research(
            "SELECT payload_json,updated_at FROM fund_fof_summary WHERE k='main'")
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if not row:
        return {"ok": False, "error": "FOF 诊断尚未生成，请运行 build_fof_diagnosis.py"}
    d = json.loads(row[0][0])
    d["ok"] = True
    d["cached_at"] = row[0][1]
    return d


@router.get("/penetration/industries")
def penetration_industries(end_date: str = ""):
    """可选行业清单 + 该期全池平均权重，供行业反查做下拉。

    只暴露真实存在的行业，不做前端硬编码 —— 申万一级会调整，
    行业名一旦写死在前端，口径变化时页面会静默错配。
    """
    try:
        if not end_date:
            r = _research("SELECT MAX(end_date) FROM fund_industry_exposure")
            end_date = (r[0][0] if r and r[0][0] else "")
        if not end_date:
            return {"ok": False, "error": "穿透结果为空，请先运行 build_fund_penetration.py"}
        n_funds = _research(
            "SELECT COUNT(DISTINCT ts_code) FROM fund_industry_exposure WHERE end_date=?",
            (end_date,))
        rows = _research("""
            SELECT industry, COUNT(*) AS n_funds,
                   ROUND(AVG(total_w), 2) AS avg_w,
                   ROUND(MAX(total_w), 2) AS max_w,
                   SUM(CASE WHEN total_w >= 5 THEN 1 ELSE 0 END) AS n_ge5
            FROM fund_industry_exposure
            WHERE end_date = ?
            GROUP BY industry
            ORDER BY avg_w DESC
        """, (end_date,))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {"ok": True, "end_date": end_date,
            "n_funds": n_funds[0][0] if n_funds else 0,
            "industries": [
                {"industry": r[0], "n_funds": r[1], "avg_w": r[2],
                 "max_w": r[3], "n_ge5": r[4]} for r in rows]}


@router.get("/penetration/industry/{industry}")
def penetration_industry(industry: str, end_date: str = "",
                          min_w: float = 0.0, min_n: int = 5, limit: int = 50):
    """行业反查：给定申万一级行业，列出重仓该行业的基金。

    「谁在买电子」比「这只基金买了什么」问得更频繁，穿透结果不能只能顺着查。
    返回按该行业权重降序，并给出披露/继承拆分与相对全池的超配，
    便于区分「真金白银买进去」和「靠补齐模型推出来的」。

    min_n 默认 5：只披露 1~2 只股票的迷你基金/数据残缺基金，会以「100% 单一行业」
    挤满榜首，把真正有代表性的组合压下去。要看去掉这个约束即可。
    """
    industry = industry.strip()
    if not industry:
        return {"ok": False, "error": "行业名不能为空"}
    try:
        if not end_date:
            r = _research("SELECT MAX(end_date) FROM fund_industry_exposure")
            end_date = (r[0][0] if r and r[0][0] else "")
        rows = _research("""
            SELECT e.base_code, e.ts_code, COALESCE(m.name,'') AS name,
                   e.disclosed_w, e.carried_w, e.total_w, e.benchmark_w, e.active_w,
                   c.disclosed_cover, c.exposure_basis, c.n_disclosed, c.is_complete
            FROM fund_industry_exposure e
            JOIN fund_hold_coverage c
              ON c.ts_code = e.ts_code AND c.end_date = e.end_date
            LEFT JOIN fund_master_lite m ON m.ts_code = e.ts_code
            WHERE e.industry = ? AND e.end_date = ? AND e.total_w >= ?
              AND c.n_disclosed >= ?
            ORDER BY e.total_w DESC
            LIMIT ?
        """, (industry, end_date, max(float(min_w), 0.0),
              max(int(min_n), 1), min(max(limit, 1), 300)))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {"ok": True, "industry": industry, "end_date": end_date,
            "min_w": min_w, "min_n": min_n, "items": [
                {"base_code": r[0], "ts_code": r[1], "name": r[2],
                 "disclosed_w": r[3], "carried_w": r[4], "total_w": r[5],
                 "benchmark_w": r[6], "active_w": r[7],
                 "disclosed_cover": r[8], "exposure_basis": r[9],
                 "n_disclosed": r[10], "is_complete": bool(r[11])} for r in rows]}


@router.get("/penetration/funds")
def penetration_funds(q: str = "", limit: int = 40, end_date: str = "",
                       basis: str = "", sort: str = "code"):
    """有穿透结果的基金列表，支持 6 位代码 / 名称模糊搜索。

    end_date 限定某一报告期；basis 按权重口径筛（如 structure_only 捞出
    「总量不可比」的那批，避免把它们和正常基金横向比较）；sort 支持
    code / cover / carried / last —— cover 升序用来排查补齐质量最差的基金。
    """
    keyword = q.strip()
    basis = basis.strip()
    ed = end_date.strip()
    order = {
        "code": "c.base_code ASC",
        "cover": "avg_cover ASC",
        "carried": "n_carried DESC",
        "last": "last_end_date DESC",
    }.get(sort, "c.base_code ASC")
    # end_date 与 basis 必须落在**同一期**上：分成两个独立 EXISTS 会出现
    # 「按 structure_only 筛、却返回该基金另一期的日期」这种自相矛盾的结果。
    where = ["(? = '' OR c.base_code LIKE ? OR c.ts_code LIKE ? OR COALESCE(m.name,'') LIKE ?)"]
    params: list = [keyword, f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"]
    if ed or basis:
        where.append("""EXISTS (SELECT 1 FROM fund_hold_coverage x
                        WHERE x.ts_code = c.ts_code
                          AND (? = '' OR x.end_date = ?)
                          AND (? = '' OR x.exposure_basis = ?))""")
        params += [ed, ed, basis, basis]
    params.append(min(max(limit, 1), 200))
    try:
        rows = _research(f"""
            SELECT c.base_code, c.ts_code, COALESCE(m.name,'') AS name,
                   COUNT(*) AS n_sections, MAX(c.end_date) AS last_end_date,
                   ROUND(AVG(c.disclosed_cover),1) AS avg_cover,
                   SUM(c.n_carried) AS n_carried
            FROM fund_hold_coverage c
            LEFT JOIN fund_master_lite m ON m.ts_code = c.ts_code
            WHERE {' AND '.join(where)}
            GROUP BY c.ts_code
            ORDER BY {order}
            LIMIT ?
        """, tuple(params))
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "q": keyword, "filters": {
        "end_date": ed, "basis": basis, "sort": sort}, "items": [
        {"base_code": r[0], "ts_code": r[1], "name": r[2], "n_sections": r[3],
         "last_end_date": r[4], "avg_cover": r[5], "n_carried": r[6]} for r in rows]}


@router.get("/penetration/fund/{code}")
def penetration_fund(code: str, end_date: str = "", detail: int = 0):
    """单基金：覆盖度时间序列 + 当期行业穿透（双轨）+ 行业换手。"""
    code = code.strip().upper()
    if not CODE_RE.match(code):
        return {"ok": False, "error": "基金代码格式不合法（需 6 位代码，可带 .OF/.SH/.SZ）"}

    try:
        real = _research(
            "SELECT DISTINCT ts_code FROM fund_hold_coverage WHERE base_code=? LIMIT 1",
            (code[:6],))
        ts_code = real[0][0] if real else (code if "." in code else f"{code}.OF")
        meta = _research("SELECT name FROM fund_master_lite WHERE ts_code=?", (ts_code,))
        cov = _research("""
            SELECT end_date, ann_date, report_type, n_disclosed, disclosed_cover,
                   is_complete, anchor_end_date, anchor_gap_days, lookahead,
                   n_carried, carried_cover, confidence, hhi, top3_share,
                   equity_source, reg_equity, exposure_basis
            FROM fund_hold_coverage WHERE ts_code=? ORDER BY end_date
        """, (ts_code,))
        if not cov:
            return {"ok": False, "error": f"{ts_code} 暂无穿透数据"}
        dates = [r[0] for r in cov]
        ed = end_date if end_date in dates else dates[-1]
        exp = _research("""
            SELECT industry, disclosed_w, carried_w, total_w, benchmark_w, active_w
            FROM fund_industry_exposure WHERE ts_code=? AND end_date=?
            ORDER BY total_w DESC
        """, (ts_code, ed))
        hist = _research("""
            SELECT end_date, industry, total_w FROM fund_industry_exposure
            WHERE ts_code=? ORDER BY end_date
        """, (ts_code,))
    except Exception as e:
        return {"ok": False, "error": str(e)}

    by_date: dict[str, dict[str, float]] = {}
    for d, ind, w in hist:
        by_date.setdefault(d, {})[ind] = float(w or 0)
    turnover = []
    prev: dict[str, float] | None = None
    for d in dates:
        cur = by_date.get(d, {})
        if prev is not None:
            keys = set(prev) | set(cur)
            turnover.append({
                "end_date": d,
                "turnover": round(sum(abs(cur.get(k, 0) - prev.get(k, 0)) for k in keys) / 2, 2),
            })
        prev = cur

    c = next(r for r in cov if r[0] == ed)
    payload = {
        "ok": True, "code": code, "ts_code": ts_code,
        "name": meta[0][0] if meta else "", "end_date": ed, "dates": dates,
        "coverage_series": [{
            "end_date": r[0], "ann_date": r[1], "report_type": r[2],
            "n_disclosed": r[3], "disclosed_cover": r[4], "is_complete": bool(r[5]),
            "anchor_end_date": r[6], "anchor_gap_days": r[7], "lookahead": bool(r[8]),
            "n_carried": r[9], "carried_cover": r[10], "confidence": r[11],
            "hhi": r[12], "top3_share": r[13], "exposure_basis": r[16],
        } for r in cov],
        "current": {
            "report_type": c[2], "disclosed_cover": c[4], "carried_cover": c[10],
            "confidence": c[11], "hhi": c[12], "top3_share": c[13],
            "anchor_end_date": c[6], "anchor_gap_days": c[7], "lookahead": bool(c[8]),
            "equity_source": c[14] or "anchor", "reg_equity": c[15],
            # 权重口径：structure_only 表示无总量锚、只做过结构归一，
            # 行业结构可用，但绝对仓位不可与其它基金横向比较
            "exposure_basis": c[16] or "net_value",
            # 补齐假设的边界：股票仓位按锚点继承，基金若已主动减仓，继承占比会虚高
            "warning": (
                "本期原始权重超出合理量级（该基金的 stk_mkv_ratio 不是占净值比），"
                "已做结构归一：行业结构可信，绝对仓位不可与其它基金横向比较"
            ) if c[16] == "structure_only" else (
                f"当期真实披露仅 {c[4]:.1f}%，显著低于典型季报（约 55%）；"
                f"算法按锚点股票仓位补齐，继承占 {c[10]:.1f}%，"
                "本期行业结构主要依赖补齐假设，不宜当作事实解读"
            ) if (c[9] > 0 and c[4] < 40) else None,
        },
        "industries": [{
            "industry": r[0], "disclosed_w": r[1], "carried_w": r[2],
            "total_w": r[3], "benchmark_w": r[4], "active_w": r[5],
        } for r in exp],
        "turnover": turnover,
    }

    # 第四轨：监管披露（证监会门类行业配置，按全部持仓计算的真实 A 股仓位）
    try:
        rq = _research("SELECT a_share_pct FROM fund_csrc_equity WHERE base_code=? AND end_date=?",
                       (code[:6], ed))
        reg_rows = _research("""
            SELECT industry_code,industry_name,weight_pct FROM fund_csrc_industry
            WHERE base_code=? AND end_date=? ORDER BY weight_pct DESC
        """, (code[:6], ed)) if rq else []
        a_sw = _research("""
            SELECT COALESCE(SUM(total_w),0) FROM fund_industry_exposure
            WHERE base_code=? AND end_date=? AND industry NOT IN ('港股','其他A股')
        """, (code[:6], ed))
    except Exception:      # 监管表尚未导入时不影响主口径
        rq, reg_rows, a_sw = [], [], []
    payload["regulatory"] = {
        "available": bool(rq),
        "a_share_pct": rq[0][0] if rq else None,
        "our_a_share_pct": round(float(a_sw[0][0] or 0), 2) if a_sw else None,
        "industries": [{"code": r[0], "name": r[1], "weight": r[2]} for r in reg_rows],
    }

    # 第三轨：净值回归校验（独立读表，不覆盖上面的双轨主口径）
    reg = _research("""
        SELECT industry, beta, bayes_w, diff FROM fund_industry_regression
        WHERE ts_code=? AND end_date=? ORDER BY beta DESC
    """, (ts_code, ed))
    rmeta = _research("""
        SELECT r2,n_obs,mse,lambda,bayes_l1,spearman FROM fund_regression_meta
        WHERE ts_code=? AND end_date=?
    """, (ts_code, ed))
    payload["regression"] = {
        "available": bool(reg),
        "meta": ({"r2": rmeta[0][0], "n_obs": rmeta[0][1], "mse": rmeta[0][2],
                  "lambda": rmeta[0][3], "bayes_l1": rmeta[0][4],
                  "spearman": rmeta[0][5]} if rmeta else None),
        "industries": [{"industry": r[0], "beta": r[1], "bayes_w": r[2],
                        "diff": r[3]} for r in reg],
    }

    # FOF 标记：样本里的 FOF 拿不到被投基金持仓，行业穿透对其不适用
    try:
        fof_row = _research("""
            SELECT name,n_sections,reg_equity_med,unpenetrated_upper,verdict
            FROM fund_fof_diagnosis WHERE ts_code=?
        """, (ts_code,))
    except Exception:
        fof_row = []
    payload["fof"] = ({
        "is_fof": True, "name": fof_row[0][0], "n_sections": fof_row[0][1],
        "reg_equity_med": fof_row[0][2], "unpenetrated_upper": fof_row[0][3],
        "verdict": fof_row[0][4],
    } if fof_row else {"is_fof": False})

    # FOF 基金层：定期报告「基金投资明细」两段穿透结果（没有就是没有，不估算）
    try:
        sm = _research("SELECT rep_code FROM fund_fof_share_map WHERE share_code=?", (code[:6],))
        rc = sm[0][0] if sm else code[:6]
        lay = _research("""
            SELECT end_date, report_type, n_funds, w_total, w_pool, w_equity,
                   a_share_decl, eq_total, n_missing_eq
            FROM fund_fof_penetration WHERE base_code=?
            ORDER BY end_date DESC LIMIT 1
        """, (rc,))
    except Exception:
        lay = []
    payload["fof"]["layer"] = ({
        "available": True,
        "end_date": lay[0][0], "report_type": lay[0][1], "n_funds": lay[0][2],
        "w_total": lay[0][3], "w_pool": lay[0][4], "w_equity": lay[0][5],
        "a_share_direct": lay[0][6], "eq_total": lay[0][7], "n_missing_eq": lay[0][8],
    } if lay else {"available": False})

    if detail:
        f = HOLDINGS_DIR / "by_fund" / f"{ts_code}.csv"
        if f.exists():
            with open(f, newline="", encoding="utf-8") as fh:
                payload["detail"] = [
                    {"symbol": row["symbol"], "industry": row["industry"],
                     "weight": float(row["weight"]), "source": row["source"],
                     "anchor_end_date": row["anchor_end_date"],
                     "anchor_gap_days": int(row["anchor_gap_days"] or -1),
                     "confidence": float(row["confidence"])}
                    for row in csv.DictReader(fh) if row["end_date"] == ed
                ]
        else:
            payload["detail"] = []
    return payload


# ---------------------------------------------------------------------------
# 风格穿透
#
# 与行业穿透互补：行业穿透回答「拿什么行业」，风格穿透回答「拿什么风格」。
# 全部由持仓加权到个股因子得出，不经净值反推，因此可追溯到具体股票。
# 数据由 backend/scripts/build_fund_style.py 每日生成。
# ---------------------------------------------------------------------------
STYLE_FACTORS = ("size", "value", "momentum", "volatility", "quality")
STYLE_CN = {"size": "规模", "value": "价值", "momentum": "动量",
            "volatility": "波动", "quality": "质量"}
# 风格结论的最低样本门槛。
# 港股基金（如 006923）A 股仓位只剩 5%，4 只股票就能把 z 拉到 ±2.5 —— 那不是风格，
# 是个股权重的噪声。门槛以下不给标签、不进漂移榜，避免把偶然当信号。
MIN_COVER = 20.0
MIN_HOLDINGS = 5
MIN_NAV = 5000.0        # 万元。迷你基金打新获配会让风格失真（107 万规模的 ETF 联接
                        # 能出现 37% 单一次新股），这类基金不给标签、不进漂移榜。


def _pct(xs, p):
    """分位数（已排序列表中取位）。"""
    if not xs:
        return None
    i = min(len(xs) - 1, max(0, int(round(p / 100 * (len(xs) - 1)))))
    return round(xs[i], 3)


_NAME_CACHE: dict = {"ts": 0.0, "map": {}}


def _fund_names() -> dict[str, str]:
    """base_code → 名称，5 分钟缓存。

    研究库 fund_master_lite 只有 1.7 万只，主库 fund_basic 有 3.3 万只，
    合并取全，否则榜单里一半基金只有代码没有名字。
    """
    now = time.time()
    if _NAME_CACHE["map"] and now - _NAME_CACHE["ts"] < 300:
        return _NAME_CACHE["map"]
    m: dict[str, str] = {}
    try:
        with SessionLocal() as db:
            for tc, nm in db.execute(text("SELECT ts_code, name FROM fund_basic")):
                if nm:
                    m[str(tc)[:6]] = nm
    except Exception:  # noqa: BLE001
        pass
    try:
        for c, nm in _research("SELECT base_code, name FROM fund_master_lite"):
            if nm:
                m.setdefault(c, nm)
    except Exception:  # noqa: BLE001
        pass
    _NAME_CACHE.update({"ts": now, "map": m})
    return m


def _style_periods() -> list[str]:
    """可用报告期（仅法定报告期，降序）。

    表里可能混入零星入库日期（如 20260929 只有 1 只基金），
    若不剔除，跨期对比会拿它当「上一期」，导致 JOIN 全空。
    """
    return [r[0] for r in _research(
        "SELECT DISTINCT end_date FROM fund_style_summary "
        "WHERE substr(end_date,5,4) IN ('0331','0630','0930','1231') "
        "ORDER BY end_date DESC")]


@router.get("/style/summary")
def style_summary(end_date: str | None = None):
    """风格总览：可用期次、标签分布、五因子分位、覆盖质量。"""
    try:
        eds = _style_periods()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"风格表尚未生成：{e}"}
    if not eds:
        return {"ok": False, "error": "尚无风格数据，请先运行 build_fund_style.py"}
    ed = end_date if end_date in eds else eds[0]

    rows = _research(
        "SELECT z_size,z_value,z_momentum,z_volatility,z_quality,style_tag,w_cover,w_disc "
        "FROM fund_style_summary WHERE end_date=?", (ed,))
    if not rows:
        return {"ok": False, "error": f"{ed} 无风格记录"}

    tags: dict[str, int] = {}
    covers, discs = [], []
    cols = {f: [] for f in STYLE_FACTORS}
    for r in rows:
        tags[r[5] or "未定"] = tags.get(r[5] or "未定", 0) + 1
        if r[6] is not None:
            covers.append(float(r[6]))
        if r[7] is not None:
            discs.append(float(r[7]))
        for i, f in enumerate(STYLE_FACTORS):
            if r[i] is not None:
                cols[f].append(float(r[i]))

    factors = []
    for f in STYLE_FACTORS:
        v = sorted(cols[f])
        factors.append({
            "factor": f, "label": STYLE_CN[f], "n": len(v),
            "p10": _pct(v, 10), "p25": _pct(v, 25), "median": _pct(v, 50),
            "p75": _pct(v, 75), "p90": _pct(v, 90),
        })

    return {
        "ok": True, "end_date": ed, "available_periods": eds[:12],
        "n_funds": len(rows),
        "tags": sorted(({"tag": k, "n": v} for k, v in tags.items()),
                       key=lambda x: -x["n"]),
        "factors": factors,
        "coverage": {"median": _median(covers), "p10": _pct(sorted(covers), 10),
                     "disclosed_median": _median(discs)},
    }


@router.get("/style/drift")
def style_drift(min_abs: float = 0.8, limit: int = 30, end_date: str | None = None):
    """风格漂移榜：与上一期相比，风格坐标变化最大的基金。

    用途：找出「悄悄换风格」的基金 —— 半年报还是小盘价值，三季报变成大盘成长。
    只用相邻两期的 z 差值，不做任何外推。
    """
    try:
        eds = _style_periods()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"风格表尚未生成：{e}"}
    if len(eds) < 2:
        return {"ok": False, "error": "期次不足两期，无法计算漂移"}
    cur = end_date if end_date in eds else eds[0]
    prev = next((e for e in eds if e < cur), None)
    if not prev:
        return {"ok": False, "error": f"{cur} 之前没有更早的期次"}

    rows = _research(
        """SELECT a.base_code, a.style_tag, b.style_tag,
                  a.z_size-b.z_size, a.z_value-b.z_value, a.z_momentum-b.z_momentum,
                  a.z_volatility-b.z_volatility, a.z_quality-b.z_quality,
                  a.w_cover, a.n_holdings, a.nav_wan
           FROM fund_style_summary a
           JOIN fund_style_summary b
             ON b.base_code=a.base_code AND b.end_date=?
           WHERE a.end_date=?
             AND a.z_size IS NOT NULL AND b.z_size IS NOT NULL
             AND a.w_cover >= ? AND b.w_cover >= ?
             AND a.n_holdings >= ? AND b.n_holdings >= ?
             AND (a.nav_wan IS NULL OR a.nav_wan >= ?)""",
        (prev, cur, MIN_COVER, MIN_COVER, MIN_HOLDINGS, MIN_HOLDINGS, MIN_NAV))
    if not rows:
        return {"ok": False, "error": "两期无法对齐（或样本量不足）"}

    names = _fund_names()

    items = []
    for r in rows:
        d = {f: (float(r[3 + i]) if r[3 + i] is not None else 0.0)
             for i, f in enumerate(STYLE_FACTORS)}
        mag = max(abs(v) for v in d.values())
        if mag < min_abs:
            continue
        top = max(d, key=lambda k: abs(d[k]))
        items.append({
            "code": r[0], "name": names.get(r[0] or "", ""),
            "tag_prev": r[2], "tag_now": r[1],
            "changed": r[1] != r[2],
            "delta": {f: round(d[f], 3) for f in STYLE_FACTORS},
            "main": {"factor": top, "label": STYLE_CN[top], "value": round(d[top], 3)},
            "magnitude": round(mag, 3), "w_cover": r[8],
        })
    items.sort(key=lambda x: -x["magnitude"])
    return {"ok": True, "end_date": cur, "prev_date": prev,
            "n_scanned": len(rows), "items": items[:limit]}


@router.get("/style/fund/{code}")
def style_fund(code: str, periods: int = 8):
    """单基金风格轨迹：近 N 期的风格坐标 + 当期行业 TOP。"""
    if not CODE_RE.match(code):
        return {"ok": False, "error": "代码格式不合法"}
    base = code.split(".")[0]
    try:
        rows = _research(
            """SELECT end_date,z_size,z_value,z_momentum,z_volatility,z_quality,
                      style_tag,w_cover,w_disc,n_holdings,nav_wan
               FROM fund_style_summary WHERE base_code=?
               ORDER BY end_date DESC LIMIT ?""", (base, max(1, min(periods, 24))))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"风格表尚未生成：{e}"}
    if not rows:
        return {"ok": False, "error": f"{base} 无风格记录（可能不在股票型/混合型池内）"}

    ed = rows[0][0]
    inds = []
    try:
        inds = [{"industry": r[0], "total_w": r[1]} for r in _research(
            """SELECT industry,total_w FROM fund_industry_exposure
               WHERE base_code=? AND end_date=? ORDER BY total_w DESC LIMIT 8""",
            (base, ed))]
    except Exception:  # noqa: BLE001
        pass

    trend = [{
        "end_date": r[0],
        **{f: r[1 + i] for i, f in enumerate(STYLE_FACTORS)},
        "style_tag": r[6], "w_cover": r[7], "w_disc": r[8], "n_holdings": r[9],
        "nav_wan": r[10],
        "reliable": bool((r[7] or 0) >= MIN_COVER and (r[9] or 0) >= MIN_HOLDINGS
                         and (r[10] is None or r[10] >= MIN_NAV)),
    } for r in rows]
    cur = trend[0]
    warn = None
    if not cur["reliable"]:
        if cur["nav_wan"] is not None and cur["nav_wan"] < MIN_NAV:
            warn = f"规模仅 {cur['nav_wan']:,.0f} 万，迷你基金的新股获配会主导风格，结论不可用"
        elif cur["w_cover"] is not None:
            warn = (f"A 股覆盖仅 {cur['w_cover']:.1f}%、持仓 {cur['n_holdings']} 只，"
                    f"样本不足以判定风格")
        else:
            warn = "覆盖数据缺失"
    return {"ok": True, "code": base, "name": _fund_names().get(base, ""),
            "end_date": ed,
            "current": cur, "trend": trend, "top_industries": inds,
            "reliable": cur["reliable"], "warning": warn,
            "threshold": {"min_cover": MIN_COVER, "min_holdings": MIN_HOLDINGS,
                          "min_nav": MIN_NAV}}


# ---------------------------------------------------------------------------
# 基金数据管道（监控 + 应急）
#
# 调度逻辑在 app/core/fund_scheduler.py；这里只做「看状态」和「手动触发」。
# 设计要点：
#   * 状态接口不触发任何计算，纯读 fund_pipeline_runs / fund_pipeline_state；
#   * 手动触发一律异步（丢线程），因为单环节动辄几十分钟，同步会拖死请求；
#   * 触发前由调度器自己做幂等/并发校验，接口不做二次判断，避免两边规则漂移。
# ---------------------------------------------------------------------------
from pydantic import BaseModel  # noqa: E402


class PipelineRunReq(BaseModel):
    stage: str
    force: bool = True


class PipelinePauseReq(BaseModel):
    paused: bool = True


@router.get("/pipeline/status")
def pipeline_status():
    """基金管道各环节：定义 + 今日状态 + 历史累计 + 库内数据新鲜度。"""
    try:
        from app.core import fund_scheduler
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"调度器模块不可用：{e}"}
    try:
        return {"ok": True, **fund_scheduler.get_status()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@router.post("/pipeline/run")
def pipeline_run(req: PipelineRunReq):
    """应急：立即跑一个环节（异步）。幂等由调度器判定，force=True 强跑。"""
    try:
        from app.core import fund_scheduler
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"调度器模块不可用：{e}"}
    r = fund_scheduler.run_stage(req.stage, force=req.force, trigger="manual")
    return {**r, "stage": req.stage}


@router.post("/pipeline/pause")
def pipeline_pause(req: PipelinePauseReq):
    """应急：暂停/恢复自动巡检。

    手工补数或排障时，调度器在旁边自动触发派生环节会抢同一个库的写锁。
    暂停只停自动触发，手动「运行」照常 —— 应急时人优先于钟。
    """
    try:
        from app.core import fund_scheduler
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"调度器模块不可用：{e}"}
    return fund_scheduler.set_paused(req.paused)


@router.post("/pipeline/reset")
def pipeline_reset(req: PipelineRunReq):
    """应急：清掉今天卡住的 running 记录，让该环节能立刻重跑。

    场景：容器重启 / 进程被杀后，库里 running 记录残留，页面「运行」按钮
    会一直被拒（提示正在运行），而自动接管要等 3 小时。这里给一个显式出口。
    """
    try:
        from app.core import fund_scheduler
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"调度器模块不可用：{e}"}
    return fund_scheduler.reset_stage(req.stage)


@router.post("/pipeline/run-all")
def pipeline_run_all():
    """应急：把今天还没成功的环节按依赖顺序补跑（不含已在跑的）。"""
    try:
        from app.core import fund_scheduler
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"调度器模块不可用：{e}"}
    return fund_scheduler.run_all_missing(trigger="manual")


@router.get("/pipeline/runs")
def pipeline_runs(stage: str = "", limit: int = 60):
    """历史运行记录（倒序），用于排障：什么时候跑、跑了多久、报了什么错。"""
    limit = max(1, min(int(limit or 60), 300))
    if stage:
        rows = _research(
            "SELECT stage,run_date,status,trigger,started_at,finished_at,duration_sec,"
            "rows,error FROM fund_pipeline_runs WHERE stage=? "
            "ORDER BY started_at DESC LIMIT ?", (stage, limit))
    else:
        rows = _research(
            "SELECT stage,run_date,status,trigger,started_at,finished_at,duration_sec,"
            "rows,error FROM fund_pipeline_runs ORDER BY started_at DESC LIMIT ?", (limit,))
    keys = ("stage", "run_date", "status", "trigger", "started_at", "finished_at",
            "duration_sec", "rows", "error")
    return {"ok": True, "runs": [dict(zip(keys, r)) for r in rows]}


@router.get("/pipeline/detail")
def pipeline_detail(stage: str):
    """某环节最近一次的完整输出（脚本最后 N 行）—— 不用进容器翻日志。"""
    rows = _research(
        "SELECT run_date,status,started_at,finished_at,duration_sec,rows,detail,error "
        "FROM fund_pipeline_runs WHERE stage=? ORDER BY started_at DESC LIMIT 1", (stage,))
    if not rows:
        return {"ok": False, "error": "还没有运行记录"}
    r = rows[0]
    return {"ok": True, "stage": stage, "run_date": r[0], "status": r[1],
            "started_at": r[2], "finished_at": r[3], "duration_sec": r[4],
            "rows": r[5], "detail": r[6], "error": r[7]}


@router.get("/penetration/fof/industry/{code}")
def fof_industry(code: str):
    """单只 FOF 的行业穿透明细：直接持股 + 穿透底层基金，附覆盖缺口。"""
    if not CODE_RE.match(code):
        return {"ok": False, "error": "代码格式不合法"}
    base = code[:6]
    metas = _research(
        "SELECT end_date,report_type,name,n_funds,w_fund_layer,w_penetrated,"
        "w_unresolved,w_unres_debt,w_unres_equity,w_direct,w_total,residual_w,"
        "n_ind,hhi,top_json,n_funds_partial,updated_at "
        "FROM fund_fof_penetration_meta WHERE base_code=? "
        "ORDER BY end_date DESC LIMIT 12", (base,))
    if not metas:
        return {"ok": False, "error": "该基金没有 FOF 穿透结果（不在 FOF 名单或报告未解析）"}
    mkeys = ("end_date", "report_type", "name", "n_funds", "w_fund_layer", "w_penetrated",
             "w_unresolved", "w_unres_debt", "w_unres_equity", "w_direct", "w_total",
             "residual_w", "n_ind", "hhi", "top_json", "n_funds_partial", "updated_at")
    sections = []
    for m in metas:
        d = dict(zip(mkeys, m))
        try:
            d["top"] = json.loads(d.pop("top_json") or "[]")
        except Exception:  # noqa: BLE001
            d["top"] = []
        ind = _research(
            "SELECT industry,direct_w,fund_w,total_w FROM fund_fof_penetration "
            "WHERE base_code=? AND end_date=? AND report_type=? "
            "ORDER BY total_w DESC", (base, d["end_date"], d["report_type"]))
        d["industries"] = [
            {"industry": r[0], "direct_w": r[1], "fund_w": r[2], "total_w": r[3]}
            for r in ind
        ]
        sections.append(d)
    return {"ok": True, "base_code": base, "sections": sections}


# ---------------------------------------------------------------- 当日估算截面
# 与 /penetration/* 的分工：那组接口讲「按报告期的历史事实」（2026-09-30 披露了
# 什么），这组讲「今天大概长什么样」—— 同样以最新披露为锚点，但按锚点年龄衰减，
# 并把「数据距今多少天」明明白白摆出来，不伪装成当日实测。
@router.get("/nowcast/summary")
def nowcast_summary(as_of: str | None = None):
    """当日估算截面：截至哪天 / 锚点在哪期 / 已补齐多久 / 可信度多少。"""
    try:
        metas = _research(
            "SELECT as_of_date,n_funds,n_rows,conf_median,stale_median,"
            "data_end_median,generated_at FROM fund_nowcast_meta "
            "ORDER BY as_of_date DESC LIMIT 60")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"当日截面尚未生成：{e}"}
    if not metas:
        return {"ok": False, "error": "尚无当日截面，请先跑 build_fund_penetration.py --nowcast"}

    mkeys = ("as_of_date", "n_funds", "n_rows", "conf_median", "stale_median",
             "data_end_median", "generated_at")
    versions = [dict(zip(mkeys, m)) for m in metas]
    cur = next((v for v in versions if v["as_of_date"] == as_of), versions[0])
    d = cur["as_of_date"]
    n = max(int(cur["n_funds"] or 1), 1)

    inds = _research(
        "SELECT industry,SUM(total_w),SUM(disclosed_w),SUM(carried_w),SUM(benchmark_w),"
        "SUM(active_w),COUNT(*) FROM fund_industry_nowcast WHERE as_of_date=? "
        "GROUP BY industry ORDER BY SUM(total_w) DESC", (d,))
    industries = [{
        "industry": r[0],
        "avg_w": round(float(r[1] or 0) / n, 2),
        "avg_disclosed": round(float(r[2] or 0) / n, 2),
        "avg_carried": round(float(r[3] or 0) / n, 2),
        "benchmark_w": round(float(r[4] or 0) / n, 2),
        "active_w": round(float(r[5] or 0) / n, 2),
        "n_funds": int(r[6] or 0),
    } for r in inds]

    # 昨日对比：只看权重变化最大的几个行业，用来看「这几天往哪挪」
    prev = next((v for v in versions[1:] if v["as_of_date"] < d), None)
    drift = []
    if prev:
        pv = _research(
            "SELECT industry,SUM(total_w) FROM fund_industry_nowcast "
            "WHERE as_of_date=? GROUP BY industry", (prev["as_of_date"],))
        pm = {r[0]: float(r[1] or 0) / max(int(prev["n_funds"] or 1), 1) for r in pv}
        for it in industries:
            before = pm.get(it["industry"], 0.0)
            it["chg"] = round(it["avg_w"] - before, 2)
        drift = sorted(industries, key=lambda x: -abs(x.get("chg", 0)))[:6]

    return {
        "ok": True,
        "as_of": d,
        "anchor": cur["data_end_median"],
        "n_funds": int(cur["n_funds"] or 0),
        "n_rows": int(cur["n_rows"] or 0),
        "confidence": cur["conf_median"],
        "stale_days": int(cur["stale_median"] or 0),
        "generated_at": cur["generated_at"],
        "prev_as_of": prev["as_of_date"] if prev else None,
        "versions": [{"as_of": v["as_of_date"], "n_funds": v["n_funds"],
                      "confidence": v["conf_median"], "stale_days": v["stale_median"]}
                     for v in versions[:14]],
        "industries": industries,
        "drift": drift,
    }


@router.get("/nowcast/style")
def nowcast_style(as_of: str | None = None):
    """当日风格截面：五因子分位、标签分布、数据新鲜度。"""
    try:
        eds = [r[0] for r in _research(
            "SELECT DISTINCT as_of_date FROM fund_style_nowcast ORDER BY as_of_date DESC LIMIT 60")]
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"当日风格尚未生成：{e}"}
    if not eds:
        return {"ok": False, "error": "尚无当日风格截面，请先跑 build_fund_style.py --nowcast"}
    d = as_of if as_of in eds else eds[0]

    rows = _research(
        "SELECT z_size,z_value,z_momentum,z_volatility,z_quality,style_tag,"
        "w_cover,n_holdings,stale_days,confidence,nav_wan,data_end_date "
        "FROM fund_style_nowcast WHERE as_of_date=?", (d,))
    if not rows:
        return {"ok": False, "error": f"{d} 无风格记录"}

    tags: dict[str, int] = {}
    cols = {f: [] for f in STYLE_FACTORS}
    covers, stales, confs = [], [], []
    for r in rows:
        tags[r[5] or "未定"] = tags.get(r[5] or "未定", 0) + 1
        if r[6] is not None:
            covers.append(float(r[6]))
        if r[8] is not None:
            stales.append(int(r[8]))
        if r[9] is not None:
            confs.append(float(r[9]))
        for i, f in enumerate(STYLE_FACTORS):
            if r[i] is not None:
                cols[f].append(float(r[i]))

    factors = []
    for f in STYLE_FACTORS:
        v = sorted(cols[f])
        factors.append({
            "factor": f, "label": STYLE_CN[f], "n": len(v),
            "p10": _pct(v, 10), "median": _pct(v, 50), "p90": _pct(v, 90),
        })

    return {
        "ok": True, "as_of": d,
        "data_end": rows[0][11],
        "available": eds[:14],
        "n_funds": len(rows),
        "tags": sorted(({"tag": k, "n": v} for k, v in tags.items()),
                       key=lambda x: -x["n"]),
        "factors": factors,
        "coverage": {"median": _median(covers)},
        "stale_days": _median([float(x) for x in stales]),
        "confidence": _median(confs),
    }
