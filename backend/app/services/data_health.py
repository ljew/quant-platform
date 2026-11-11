"""数据健康度引擎：采集/处理/应用三层检查 + 评分 + 告警生成。

每层若干检查项（check），通过比例 ×100 = 层得分；
全平台总分 = 三层平均。
生成 alerts 列表供监控页与后续推送使用。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select, func

from app.models import (
    FactorDaily,
    IndexKlineDaily,
    IndexMembership,
    KlineDaily,
    PipelineRun,
    ResearchRun,
)

_WEIGHTS = {"collect": 0.35, "process": 0.35, "apply": 0.30}


def _mk(name: str, ok: bool | None, value: str, expect: str) -> dict:
    return {"name": name,
            "status": "ok" if ok else ("warn" if ok is None else "error"),
            "value": value, "expect": expect}


def _shown(latest: str | None, days: int) -> str:
    """把「最近日期 + 距今天数」拼成一句人话。

    days < 0 有两种含义：无数据（latest 为 None）或列里有脏值（latest 已是
    一句说明）。两种都不该再拼「(-1天前)」，那会让人以为是未来日期。
    """
    if latest is None:
        return "无数据"
    if days is None or days < 0:
        return latest
    return f"{latest} ({days}天前)"


def _latest_date(db, col_model_col) -> tuple[str | None, int]:
    """某张表日期列的最近一天 + 距今天数。

    ⚠️ 2026-11-09 编排器连崩 3 次（run 311/312/313）的根因就在这里：
    某张表的日期列里混进了一条非 ISO 值（`'杨逸君'`，基金经理姓名），
    SQLAlchemy 从 SQLite 读 Date/DateTime 列时走 `fromisoformat` 解析，
    直接抛 `ValueError: Invalid isoformat string: '杨逸君'`。异常穿透到
    `orchestrator._run` 的最外层 except → 整条流水线 FAILED，而 error 字段
    只有那一行天书，**连是哪张表都看不出来**，复盘只能靠猜。

    现在的行为：脏值降级成「该列不可判定」（days=-1，不算新鲜也不算陈旧），
    value 里直接写出 `表.列=样例值`，一眼能定位。真正的修复动作是清掉脏行，
    但至少不再让一行脏数据炸掉整个日更。
    """
    tbl, cname = col_model_col.table.name, col_model_col.name
    try:
        d = db.execute(select(func.max(col_model_col))).scalar()
    except ValueError:
        # 列里存在非日期值：单独扫一遍，把样例带出来（不依赖 SQLAlchemy 的类型解析）
        sample: list[str] = []
        try:
            from sqlalchemy import text as _text

            rows = db.execute(_text(
                f'select "{cname}" from "{tbl}" '
                f'where "{cname}" is not null '
                f"and \"{cname}\" not glob '[0-9][0-9][0-9][0-9]-[0-9][0-9]-*' "
                f"limit 3"
            )).fetchall()
            sample = [str(r[0])[:24] for r in rows]
        except Exception:  # noqa: BLE001
            pass
        shown = "、".join(sample) if sample else "?"
        return f"脏值 {tbl}.{cname}={shown}", -1
    if not d:
        return None, -1
    return d.isoformat(), (date.today() - d).days


def scan_dirty_dates(db) -> list[dict]:
    """扫一遍关键表日期列里的非法值（编排器 quality 步骤调用，只报不拦）。

    与 `_latest_date` 的容错互补：那边保证不炸，这边负责**说清楚**——
    哪张表、哪一列、几条、样例值、影响哪一段。
    """
    targets = [
        (KlineDaily, KlineDaily.trade_date),
        (IndexKlineDaily, IndexKlineDaily.trade_date),
        (FactorDaily, FactorDaily.trade_date),
        (IndexMembership, IndexMembership.trade_date),
        (PipelineRun, PipelineRun.started_at),
        (ResearchRun, ResearchRun.started_at),
    ]
    from sqlalchemy import text as _text

    out: list[dict] = []
    for _model, col in targets:
        tbl, cname = col.table.name, col.name
        try:
            row = db.execute(_text(
                f'select count(*), min("{cname}") from "{tbl}" '
                f'where "{cname}" is not null '
                f"and \"{cname}\" not glob '[0-9][0-9][0-9][0-9]-[0-9][0-9]-*'"
            )).fetchone()
        except Exception:  # noqa: BLE001
            continue
        if row and row[0]:
            out.append({"table": tbl, "column": cname,
                        "bad_rows": int(row[0]), "sample": str(row[1])[:24]})
    return out


def health_report(db=None) -> dict:
    own = db is None
    from app.database import SessionLocal as _SL

    close_after = own
    db = db or _SL()
    try:
        # ========== 采集层 ==========
        collect_checks: list[dict] = []
        try:
            from app.services import data_source
            ts_ok = bool(data_source.check_tushare())
            ak_ok = bool(data_source.check_akshare())
        except Exception:  # noqa: BLE001
            ts_ok = ak_ok = False
        collect_checks.append(_mk("tushare 连通", ts_ok, "在线" if ts_ok else "离线", "在线"))
        collect_checks.append(_mk("akshare 连通", ak_ok, "在线" if ak_ok else "离线", "在线"))

        kl_latest, kl_days = _latest_date(db, KlineDaily.trade_date)
        collect_checks.append(_mk("个股K线新鲜度",
                                  kl_days is not None and 0 <= kl_days <= 5,
                                  _shown(kl_latest, kl_days), "≤5 天"))
        idx_latest, idx_days = _latest_date(db, IndexKlineDaily.trade_date)
        collect_checks.append(_mk("指数K线新鲜度",
                                  idx_days is not None and 0 <= idx_days <= 5,
                                  _shown(idx_latest, idx_days), "≤5 天"))

        # 管道最近一次运行
        try:
            run = db.execute(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(1)).scalar()
        except Exception:  # noqa: BLE001
            run = None
        if run:
            ago = ""
            if run.started_at:
                # 注意：PipelineRun.started_at 由模型默认 datetime.utcnow 写入（UTC），
                # 只能用 utcnow 相减。别被「时间看着像本地」误导改成 datetime.now()，
                # 那会凭空多算 8 小时（东八区）。
                mins = int((datetime.utcnow() - run.started_at).total_seconds() / 60)
                ago = f"({mins} 分钟前)"
            collect_checks.append(_mk(
                "数据管道最近运行",
                run.status in ("SUCCESS", "RUNNING"),
                f"#{run.id} {run.status} {ago}", "SUCCESS/RUNNING"))
        else:
            collect_checks.append(_mk("数据管道最近运行", None, "从未运行", "有记录"))

        # 研究流水线（Orchestrator）最近一次运行（P4 监控整合）
        try:
            # 排除 CANCELLED：容器重复启动会让同一时点被触发多次，多余的那些
            # 是人工取消的，不代表流水线故障。framework_health 早就这么做了，
            # 这里漏了 → 两个体检模块口径不一致，一个说健康、一个报 error，
            # 半夜还会按这个发告警（2026-11-07 实际踩到）。
            res_run = db.execute(
                select(ResearchRun)
                .where(ResearchRun.status != "CANCELLED")
                .order_by(ResearchRun.id.desc()).limit(1)
            ).scalars().first()
        except Exception:  # noqa: BLE001
            res_run = None
        if res_run:
            ok = res_run.status in ("SUCCESS", "RUNNING")
            # 今日已跑过（无论成败与否都说明调度活着）；检查调度是否停摆超 3 天
            sched_alive = bool(res_run.started_at) and (
                # ResearchRun.started_at 为 UTC（模型默认 utcnow），必须用 utcnow 相减
                datetime.utcnow() - res_run.started_at).days < 3
            mins = (int((datetime.utcnow() - res_run.started_at).total_seconds() // 60)
                    if res_run.started_at else -1)
            collect_checks.append(_mk(
                "研究流水线最近运行",
                ok and sched_alive,
                f"#{res_run.id} {res_run.status} ({mins} 分钟前)",
                "SUCCESS 且 3 天内"))
        else:
            collect_checks.append(_mk("研究流水线最近运行", None, "从未运行", "有记录"))

        from app.datahub.registry import RAW_DIR as bronze_dir
        n_bronze = 0
        for root, _d, files in __import__("os").walk(bronze_dir):
            n_bronze += len([f for f in files if f.endswith(".parquet")])
        collect_checks.append(_mk("Bronze 分区文件", n_bronze > 0, f"{n_bronze} 个 parquet", ">0"))

        # 日期列里混进非日期值（2026-11-09 让编排器连崩 3 次的那类问题）。
        # 单独成项：不炸流水线，但要能在健康分和「未过项」里看见。
        dirty = scan_dirty_dates(db)
        collect_checks.append(_mk(
            "日期列合法性",
            not dirty,
            "全部合法" if not dirty else
            "；".join(f"{d['table']}.{d['column']} {d['bad_rows']} 条（{d['sample']}）"
                      for d in dirty[:3]),
            "无非法日期值"))

        # ========== 处理层 ==========
        process_checks: list[dict] = []
        fa_latest, fa_days = _latest_date(db, FactorDaily.trade_date)
        process_checks.append(_mk("因子截面新鲜度",
                                  fa_days is not None and 0 <= fa_days <= 6,
                                  _shown(fa_latest, fa_days), "≤6 天"))
        fa_rows = db.execute(select(func.count()).select_from(FactorDaily)).scalar() or 0
        process_checks.append(_mk("因子覆盖率", fa_rows >= 1000, f"{fa_rows:,} 行", "≥1,000"))

        mem_latest, mem_days = _latest_date(db, IndexMembership.trade_date)
        this_month = date.today().replace(day=1).isoformat()
        # mem_latest 在脏值情况下是一句说明而不是日期，不能直接比大小
        # （中文码点比数字大，'脏值…' >= '2026-11-01' 会意外判成 True）
        process_checks.append(_mk("成分快照月更",
                                  mem_days >= 0 and mem_latest is not None
                                  and mem_latest >= this_month,
                                  _shown(mem_latest, mem_days), f"≥{this_month}"))

        # 最近质检报告（Silver 层）
        try:
            from app.datahub.cleaners.core import read_latest_report
            rep = read_latest_report()
            gen_at = (rep or {}).get("generated_at", "")
            days_ago_rep = ((datetime.now() - datetime.fromisoformat(gen_at)).days
                            if gen_at else -1)
            process_checks.append(_mk("Silver 质检报告",
                                      bool(rep) and days_ago_rep <= 7,
                                      f"{gen_at}" if gen_at else "无", "≤7 天"))
        except Exception:  # noqa: BLE001
            process_checks.append(_mk("Silver 质检报告", None, "不可用", "存在"))

        # 最近管道失败率
        # 允许偶发失败：数据源抖动（tushare 限速 / 盘中接口空返回）是常态，
        # 调度器本身有「失败即降级 etl_daily 重试」的兜底，下一次基本就成功了
        # （例：#2396 FAILED → 41 秒后 #2397 SUCCESS）。原来的「必须 0 失败」
        # 会让一次抖动在最近 10 次窗口里持续报 error、半夜发邮件。
        runs = db.execute(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(10)).scalars().all()
        fails = sum(1 for r in runs if r.status == "FAILED")
        process_checks.append(_mk("管道近期失败率", fails <= 2,
                                  f"最近{len(runs)}次 {fails} 次失败", "≤2 次"))

        # ========== 应用层 ==========
        apply_checks: list[dict] = []
        mismatch = []
        table_cls = {"kline_daily": KlineDaily, "factor_daily": FactorDaily}
        for t, cls in table_cls.items():
            a = db.execute(select(func.count()).select_from(cls)).scalar() or 0
            try:
                from app.services import duckdb_store
                b = duckdb_store.count(t)
            except Exception:  # noqa: BLE001
                b = -1
            if a != b:
                mismatch.append(t)
        apply_checks.append(_mk("DuckDB 同步一致性",
                                not mismatch or all(m not in mismatch for m in ["kline_daily", "factor_daily"]),
                                "一致" if not mismatch else f"差异:{','.join(mismatch)}", "一致"))

        paper_alive = False
        try:
            from app.core.engine import paper_scheduler
            paper_alive = bool(paper_scheduler._thread and paper_scheduler._thread.is_alive())
        except Exception:  # noqa: BLE001
            pass
        apply_checks.append(_mk("模拟盘调度器", paper_alive, "存活" if paper_alive else "未运行", "存活"))

        etl_enabled = False
        try:
            from app.core.data_scheduler import get_status
            st = get_status()
            etl_enabled = bool(st["enabled"])
        except Exception:  # noqa: BLE001
            pass
        apply_checks.append(_mk("ETL 定时调度", etl_enabled,
                                "已启用(17:00)" if etl_enabled else "未启用", "建议启用"))

        # ========== 打分 ==========
        def score_of(checks) -> int:
            known = [c for c in checks if c["status"] != "warn"]
            if not known:
                return 50
            good = sum(1 for c in checks if c["status"] == "ok")
            return round(good / len(checks) * 100)

        layers = {
            "collect": {"label": "数据采集", "checks": collect_checks, "score": score_of(collect_checks)},
            "process": {"label": "数据处理", "checks": process_checks, "score": score_of(process_checks)},
            "apply": {"label": "数据应用", "checks": apply_checks, "score": score_of(apply_checks)},
        }
        for ly in layers.values():
            ly["status"] = ("healthy" if ly["score"] >= 85 else
                            "warn" if ly["score"] >= 60 else "error")
        overall = round(sum(l["score"] * w for l, w in zip(layers.values(), _WEIGHTS.values())))

        # 告警列表
        alerts = []
        for lname, ly in layers.items():
            for c in ly["checks"]:
                if c["status"] != "ok":
                    level = "warn" if ly["score"] >= 60 else "error"
                    alerts.append({"level": level, "layer": ly["label"],
                                   "check": c["name"], "detail": c["value"],
                                   "expect": c["expect"]})
        alerts.sort(key=lambda a: a["level"])

        return {
            "overall_score": overall,
            "overall_status": "healthy" if overall >= 85 else "warn" if overall >= 60 else "error",
            "layers": layers,
            "alerts": alerts,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }
    finally:
        if close_after:
            db.close()
