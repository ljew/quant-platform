"""Bronze/Silver 数据集存储管理（Parquet 分区）。"""
from __future__ import annotations

import os
from datetime import date, datetime

import pandas as pd

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
RAW_DIR = os.path.join(_PROJECT_ROOT, "data", "raw")
SILVER_DIR = os.path.join(_PROJECT_ROOT, "data", "silver")


def bronze_path(dataset: str, batch: str | None = None) -> str:
    """data/raw/{dataset}/{batch}.parquet（batch 缺省=当天日期）。"""
    b = batch or datetime.now().strftime("%Y%m%d")
    d = os.path.join(RAW_DIR, dataset)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{b}.parquet")


def silver_path(dataset: str) -> str:
    """data/silver/{dataset}.parquet（Silver 为最新全量快照，覆盖式）。"""
    os.makedirs(SILVER_DIR, exist_ok=True)
    return os.path.join(SILVER_DIR, f"{dataset}.parquet")


def write_bronze(dataset: str, df: pd.DataFrame, batch: str | None = None) -> str:
    path = bronze_path(dataset, batch)
    df.to_parquet(path, index=False)
    return path


def read_silver(dataset: str) -> pd.DataFrame | None:
    path = silver_path(dataset)
    if not os.path.exists(path):
        return None
    return pd.read_parquet(path)


def list_batches(dataset: str) -> list[str]:
    d = os.path.join(RAW_DIR, dataset)
    if not os.path.isdir(d):
        return []
    return sorted(f[:-8] for f in os.listdir(d) if f.endswith(".parquet"))
