"""FastAPI 应用入口。"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings, BASE_DIR
from app.database import init_db
from app.routers import data, market, strategy, paper, hedge, monitor, factor, llm, signal, agent, news, auto, research, live_ops, wechat
from app.schemas import HealthResponse

import os

app = FastAPI(title=settings.app_name, version="0.2.0", debug=settings.debug)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(market.router, prefix=settings.api_prefix)
app.include_router(data.router, prefix=settings.api_prefix)
app.include_router(strategy.router, prefix=settings.api_prefix)
app.include_router(paper.router, prefix=settings.api_prefix)
app.include_router(hedge.router, prefix=settings.api_prefix)
# 实盘：2026-09-30 曾按「平台范围不含实盘」摘除（live.py，单笔下单 demo）。
# 2026-10-28 决策变更——上实盘，但形态不同：平台只做「数据与决策」，不做下单。
# 因此启用 live_ops（只读 + 成交录入），旧的 live.py 仍不启用：
# 它硬编码 SimulatedBroker 且提供下单接口，与「个人不得外接券商」的合规边界冲突。
app.include_router(live_ops.router, prefix=settings.api_prefix)
app.include_router(monitor.router, prefix=settings.api_prefix)
app.include_router(factor.router, prefix=settings.api_prefix)
app.include_router(llm.router, prefix=settings.api_prefix)
app.include_router(signal.router, prefix=settings.api_prefix)
app.include_router(agent.router, prefix=settings.api_prefix)
app.include_router(news.router, prefix=settings.api_prefix)
app.include_router(wechat.router, prefix=settings.api_prefix)
app.include_router(auto.router, prefix=settings.api_prefix)
app.include_router(research.router, prefix=settings.api_prefix)


@app.on_event("startup")
def on_startup():
    init_db()
    # 首次升级：把 .env 里已配的 LLM key 落成一条记录（仅当表为空），
    # 之后配置统一在页面上维护，改完即时生效无需重启。
    from app.services.llm_config import ensure_env_provider
    ensure_env_provider()
    from app.core.engine.paper_scheduler import start_paper_scheduler
    start_paper_scheduler()
    # 研究流水线每日定时（17:30 交易日，含补跑；与 data_scheduler 并存，均幂等）
    from app.services.orchestrator import start_daily_scheduler
    start_daily_scheduler()
    # 数据管道调度（settings.data_schedule ← .env/环境 QUANT_DATA_SCHEDULE=1：
    # 每交易日 19:00 ETL 自动日更 + 断供自愈 + 指数成分(PIT)月度快照自动刷新）
    from app.core.data_scheduler import start_data_scheduler
    start_data_scheduler(settings.data_schedule)
    # 自动研究闭环：每日挖矿（交易日 20:47）+ 每周复盘（周六 10:15），均幂等可补跑
    from app.services.auto_research import start_auto_scheduler
    start_auto_scheduler()
    # 看门狗：每 10 分钟四层体检（调度/编排/数据/研究），异常发邮件告警；
    # 交易日 17:35 后仍无当日编排 → 自动补跑一次（幂等）。进程挂掉由 launchd 兜。
    from app.services.watchdog import start_watchdog
    start_watchdog()
    # 公众号每日增量抓取（08:20 / 20:40）：只读挂载的 wewe-rss.db 抓正文进 knowledge.db，
    # 不依赖宿主机 wewe-rss 的 HTTP 接口，所以能在这里跟着后端常驻。
    try:
        from app.core.wechat_scheduler import start_wechat_scheduler
        start_wechat_scheduler()
    except Exception as _e:  # noqa: BLE001
        # 公众号是附加模块，起不来不能拖垮主服务
        import logging
        logging.getLogger(__name__).warning("公众号调度器启动失败：%s", _e)

    # 国家队介入强度指数（NSI）每日信号：19:30 刷新 nsi_signal.csv，
    # 供 national_team 策略读取。脚本缺失/更新失败只影响该策略，不影响主服务。
    try:
        from app.core.nsi_scheduler import start_nsi_scheduler
        start_nsi_scheduler()
    except Exception as _e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("NSI 调度器启动失败：%s", _e)


@app.get("/health", response_model=HealthResponse, tags=["meta"])
def health():
    from app.services import data_source
    return HealthResponse(
        status="ok",
        version="0.2.0",
        db="sqlite" if settings.is_sqlite else "postgres",
        data_source="akshare" if data_source.check_akshare() else "unavailable",
    )


# —— 前端静态页面托管 ——
# v2.0 起只托管 Vite React 完整平台（web/dist，QUANT·DESK 四入口导航）；
# Phase1 原生 HTML 原型目录（frontend/）已删除，构建产物缺失时请先在 web/ 下 npm run build。
_front_dir = settings.frontend_dir
if os.path.isdir(_front_dir) and os.path.exists(os.path.join(_front_dir, "index.html")):
    app.mount("/", StaticFiles(directory=_front_dir, html=True), name="frontend")
else:
    import logging
    logging.getLogger(__name__).warning(
        "前端构建产物缺失：%s/index.html 不存在，仅提供 API（/docs）。"
        "请执行 cd web && npm run build", _front_dir)
