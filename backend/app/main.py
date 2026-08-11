"""FastAPI 应用入口。"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db
from app.routers import data, market, strategy, paper, hedge
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


@app.on_event("startup")
def on_startup():
    init_db()
    from app.core.engine.paper_scheduler import start_paper_scheduler
    start_paper_scheduler()


@app.get("/health", response_model=HealthResponse, tags=["meta"])
def health():
    from app.services import data_source
    return HealthResponse(
        status="ok",
        version="0.2.0",
        db="sqlite" if settings.is_sqlite else "postgres",
        data_source="akshare" if data_source.check_akshare() else "unavailable",
    )


# —— 前端原型页面托管（Phase 1 用，生产替换为 Vite 构建产物）——
_frontend = settings.frontend_dir
if os.path.isdir(_frontend) and os.path.exists(os.path.join(_frontend, "index.html")):
    app.mount("/", StaticFiles(directory=_frontend, html=True), name="frontend")
