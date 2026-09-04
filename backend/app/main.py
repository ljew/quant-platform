"""FastAPI 应用入口。"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings, BASE_DIR
from app.database import init_db
from app.routers import data, market, strategy, paper, hedge, live, monitor, factor
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
app.include_router(live.router, prefix=settings.api_prefix)
app.include_router(monitor.router, prefix=settings.api_prefix)
app.include_router(factor.router, prefix=settings.api_prefix)


@app.on_event("startup")
def on_startup():
    init_db()
    from app.core.engine.paper_scheduler import start_paper_scheduler
    start_paper_scheduler()
    # 数据管道调度（settings.data_schedule ← .env/环境 QUANT_DATA_SCHEDULE=1：
    # 每交易日 19:00 ETL 自动日更 + 断供自愈 + 指数成分(PIT)月度快照自动刷新）
    from app.core.data_scheduler import start_data_scheduler
    start_data_scheduler(settings.data_schedule)


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
# 默认 Vite React 完整平台（web/dist，QUANT·DESK 左侧导航）；
# 构建产物缺失时退回 Phase1 原生 HTML 原型目录（frontend/）。
_front_dir = settings.frontend_dir
if not (os.path.isdir(_front_dir) and os.path.exists(os.path.join(_front_dir, "index.html"))):
    _front_dir = str(BASE_DIR / "frontend")
if os.path.isdir(_front_dir) and os.path.exists(os.path.join(_front_dir, "index.html")):
    app.mount("/", StaticFiles(directory=_front_dir, html=True), name="frontend")
