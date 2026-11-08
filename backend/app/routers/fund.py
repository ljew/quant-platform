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
from pathlib import Path

from fastapi import APIRouter
from sqlalchemy import text

from app.database import SessionLocal

router = APIRouter(prefix="/fund", tags=["fund"])


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


@router.get("/penetration/funds")
def penetration_funds(q: str = "", limit: int = 40):
    """有穿透结果的基金列表，支持 6 位代码 / 名称模糊搜索。"""
    keyword = q.strip()
    try:
        rows = _research("""
            SELECT c.base_code, c.ts_code, COALESCE(m.name,'') AS name,
                   COUNT(*) AS n_sections, MAX(c.end_date) AS last_end_date,
                   ROUND(AVG(c.disclosed_cover),1) AS avg_cover,
                   SUM(c.n_carried) AS n_carried
            FROM fund_hold_coverage c
            LEFT JOIN fund_master_lite m ON m.ts_code = c.ts_code
            WHERE (? = '' OR c.base_code LIKE ? OR c.ts_code LIKE ?
                   OR COALESCE(m.name,'') LIKE ?)
            GROUP BY c.ts_code
            ORDER BY c.base_code
            LIMIT ?
        """, (keyword, f"%{keyword}%", f"%{keyword}%", f"%{keyword}%",
              min(max(limit, 1), 200)))
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "q": keyword, "items": [
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
                   equity_source, reg_equity
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
            "hhi": r[12], "top3_share": r[13],
        } for r in cov],
        "current": {
            "report_type": c[2], "disclosed_cover": c[4], "carried_cover": c[10],
            "confidence": c[11], "hhi": c[12], "top3_share": c[13],
            "anchor_end_date": c[6], "anchor_gap_days": c[7], "lookahead": bool(c[8]),
            "equity_source": c[14] or "anchor", "reg_equity": c[15],
            # 补齐假设的边界：股票仓位按锚点继承，基金若已主动减仓，继承占比会虚高
            "warning": (
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
