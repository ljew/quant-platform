"""FastAPI 应用入口。"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings, BASE_DIR
from app.database import init_db
from app.routers import data, market, strategy, paper, hedge, monitor, factor, llm, signal, agent, news, auto, research, live_ops, wechat, etf_flow, reports, fund, agent_portal
from app.schemas import HealthResponse

import logging
import os

app = FastAPI(title=settings.app_name, version="0.2.0", debug=settings.debug)


@app.middleware("http")
async def _cache_headers(request, call_next):
    """前端缓存策略：index.html 绝不缓存，带 hash 的静态资源长缓存。

    为什么必须显式设置：StaticFiles 默认只给 ETag/Last-Modified，浏览器可以
    启发式复用 index.html —— 于是「代码已更新、页面还是旧的」。2026-11-03
    实测过：后端接口和产物都已经是新版，用户浏览器仍显示旧界面（连新增的
    「基准收益」卡片都看不到），排查了半天。
    """
    resp = await call_next(request)
    path = request.url.path
    if path.startswith("/assets/"):
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp

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
app.include_router(etf_flow.router, prefix=settings.api_prefix)
# 市场资讯报告（只读）：由 ingest_market_report.py 写入 market_reports 表
app.include_router(reports.router, prefix=settings.api_prefix)
# 基金持续性（只读）：统计由 build_stats.py 预计算进 fund_stat_cache
app.include_router(fund.router, prefix=settings.api_prefix)
# 智能体门户：/api/v1/portal/*（状态/引导） + 根级 /agent/*（dsh 反代）
# 反代必须挂在根级，且要在最后那个 "/" 的 StaticFiles mount 之前注册。
app.include_router(agent_portal.api_router, prefix=settings.api_prefix)
app.include_router(agent_portal.proxy_router)


@app.on_event("startup")
def on_startup():
    # 统一日志可见性：uvicorn 只给 uvicorn.* 配 handler，其余 logger 继承 root
    # （默认 WARNING 且无 handler）→ 各调度模块的 logger.info 被静默丢弃，
    # 只有 warning+ 靠 lastResort 打得出来。2026-11-04/05 排查「策略验证连续
    # 两晚没跑」时，日志里既没有「跳过」也没有「完成」，正是这个原因。
    # 这里借用 uvicorn 已配好的 handler 把 root 提到 INFO，并压掉嘈杂的第三方。
    # 直接给调度器 logger 挂独立 handler：它们不属于 uvicorn.*，root 默认 WARNING
    # 且无 handler，logger.info 会被静默丢弃（只有 warning+ 走 lastResort 可见）。
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    for _name in ("data_scheduler", "wechat_scheduler",
                  "app.services.auto_research", "app.core.nsi_scheduler",
                  "etf_flow_scheduler", "hot_brief_scheduler",
                  "news_feed_scheduler", "news_feed",
                  "foreign_news_scheduler", "foreign_news",
                  "quote_scheduler", "quote_snapshot",
                  "fund_scheduler"):
        _lg = logging.getLogger(_name)
        if not _lg.handlers:
            _lg.addHandler(_h)
            _lg.setLevel(logging.INFO)
            _lg.propagate = False
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
        logging.getLogger(__name__).warning("公众号调度器启动失败：%s", _e)

    # 统一资讯流：各来源原文归一入库（news_feed），供「全部资讯」页浏览。
    # 快源 10 分钟同步，慢源每小时顺带（公众号/tushare/股吧）。
    try:
        from app.core.news_feed_scheduler import start_news_feed_scheduler
        start_news_feed_scheduler()
    except Exception as _e:  # noqa: BLE001
        logging.getLogger(__name__).warning("资讯同步调度器启动失败：%s", _e)

    # 境外资讯日爬：三槽 07:00 / 12:30 / 21:00 Asia/Shanghai（独立于国内 10min 循环）
    try:
        from app.core.foreign_news_scheduler import start_foreign_news_scheduler
        start_foreign_news_scheduler()
    except Exception as _e:  # noqa: BLE001
        logging.getLogger(__name__).warning("境外资讯调度器启动失败：%s", _e)

    # 每小时热点简报：一手快讯（财联社）→ LLM 筛选解读 → hot_brief 表。
    # DeepSeek API 无联网能力（tools 只支持 function，2026-11-07 实测 422），
    # 所以输入用平台自采资讯，模型只负责筛选与判断。
    try:
        from app.core.hot_brief_scheduler import start_hot_brief_scheduler
        start_hot_brief_scheduler()
    except Exception as _e:  # noqa: BLE001
        logging.getLogger(__name__).warning("热点简报调度器启动失败：%s", _e)

    # ETF 资金流看板（观察口径，非信号）：盘中每分钟采样 + 15:05–20:00 反复补官方分钟线
    # + 19:30 日更 + 次日 08:30 补份额。
    # 与下面 NSI 的分工见 services/etf_flow.py 开头；本模块不进任何策略。
    try:
        from app.core.etf_flow_scheduler import start_etf_flow_scheduler
        start_etf_flow_scheduler()
    except Exception as _e:  # noqa: BLE001
        logging.getLogger(__name__).warning("ETF 资金流调度器启动失败：%s", _e)

    # 国家队介入强度指数（NSI）每日信号：19:30 刷新 nsi_signal.csv，
    # 供 national_team 策略读取。脚本缺失/更新失败只影响该策略，不影响主服务。
    try:
        from app.core.nsi_scheduler import start_nsi_scheduler
        start_nsi_scheduler()
    except Exception as _e:  # noqa: BLE001
        logging.getLogger(__name__).warning("NSI 调度器启动失败：%s", _e)

    # 收盘行情快照：每交易日 18:30 后从 tushare 拉一次全市场截面落库，
    # /market/quote 只读本地（毫秒级），请求链路不再访问外部数据源。
    try:
        from app.core.quote_scheduler import start_quote_scheduler
        start_quote_scheduler()
    except Exception as _e:  # noqa: BLE001
        logging.getLogger(__name__).warning("行情快照调度器启动失败：%s", _e)

    # 基金数据管道：净值日更 + 披露窗口内抓持仓/监管仓位/FOF 报告 + 派生计算重算。
    # 独立库 data/fund_research.db，和主行情链路解耦（主库 2026-11-08 因双写损坏过）。
    # QUANT_FUND_SCHEDULE=0 可关闭；默认开，否则页面上的基金数据永远停在手工跑的时点。
    try:
        from app.core.fund_scheduler import start_fund_scheduler
        start_fund_scheduler(os.getenv("QUANT_FUND_SCHEDULE", "1") == "1")
    except Exception as _e:  # noqa: BLE001
        logging.getLogger(__name__).warning("基金管道调度器启动失败：%s", _e)


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
    logging.getLogger(__name__).warning(
        "前端构建产物缺失：%s/index.html 不存在，仅提供 API（/docs）。"
        "请执行 cd web && npm run build", _front_dir)
