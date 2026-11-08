"""脚本层 Tushare token：解析优先级 + 「源码不落明文」守门测试。

背景：fetch_fund_holdings / fetch_fund_nav_daily / fetch_sw_daily 曾各自
把 56 位 token 写成 ``os.getenv(..., "<明文>")`` 的默认值，导致
（a）密钥随仓库扩散，（b）容器注入的 QUANT_TUSHARE_TOKEN 被绕过。
这组测试锁住三件事：源码无明文、平台变量优先、缺失时显式失败。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

# Tushare token 的形态就是 56 位小写十六进制；出现在脚本源码里即视为硬编码密钥
_HEX56 = re.compile(r"\b[0-9a-f]{56}\b")


def _mod():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import _tushare_token

    return _tushare_token


def test_no_hardcoded_token_in_scripts():
    """backend/scripts 下任何 .py 都不允许出现 56 位十六进制串。"""
    offenders = []
    for py in sorted(SCRIPTS.rglob("*.py")):
        text = py.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if _HEX56.search(line):
                offenders.append(f"{py.relative_to(SCRIPTS)}:{lineno}")
    assert not offenders, f"疑似硬编码 token：{offenders}"


def test_platform_env_wins(monkeypatch):
    """QUANT_TUSHARE_TOKEN（平台标准名）优先于 TUSHARE_TOKEN。"""
    m = _mod()
    for key in m.ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TUSHARE_TOKEN", "s" * 56)
    monkeypatch.setenv("QUANT_TUSHARE_TOKEN", "p" * 56)
    assert m.resolve_token() == "p" * 56


def test_official_env_alone_works(monkeypatch):
    """只设官方名 TUSHARE_TOKEN 也能用（手工调试场景）。"""
    m = _mod()
    for key in m.ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(m, "DOTENV", Path("/tmp/__absent__.env"))
    monkeypatch.setenv("TUSHARE_TOKEN", "x" * 56)
    assert m.resolve_token() == "x" * 56


def test_missing_token_fails_loudly(monkeypatch, tmp_path):
    """环境变量与 .env 都没有时必须非零退出，不得静默回退内置值。"""
    m = _mod()
    for key in m.ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(m, "DOTENV", tmp_path / "no_such.env")
    with pytest.raises(SystemExit) as exc:
        m.require_token()
    assert exc.value.code == 2
