"""数据源配置加载（声明式 sources.yaml）。"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DEFAULT_CONFIG = os.path.join(_PROJECT_ROOT, "config", "sources.yaml")


@lru_cache(maxsize=1)
def load_sources(config_path: str | None = None) -> dict:
    """加载数据源配置 -> {source_name: {type, enabled, description, params}}。"""
    path = config_path or os.getenv("QUANT_SOURCES_YAML", DEFAULT_CONFIG)
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("sources", {}) or {}


def enabled_sources() -> dict:
    """仅返回启用的数据源。"""
    return {k: v for k, v in load_sources().items() if v.get("enabled", True)}


def get_source(name: str) -> dict | None:
    src = load_sources().get(name)
    if src and not src.get("enabled", True):
        return None
    return src


def project_root() -> str:
    return _PROJECT_ROOT
