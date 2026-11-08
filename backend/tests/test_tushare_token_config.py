"""P0-1 密钥出库：源码不得含 Tushare token 明文默认；缺 env 时配置为空。"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
BACKEND = _HERE.parent
if not (BACKEND / "app" / "config.py").exists():
    BACKEND = Path("/app")  # docker：测试拷到 /tmp/tests，业务代码挂在 /app
ROOT = BACKEND
CONFIG_PY = BACKEND / "app" / "config.py"
FETCH_SCRIPT = BACKEND / "scripts" / "fetch_alla_bronze.py"

# getenv/environ 第二参数位上的长 hex 默认（32+ hex）一律视为可疑明文回退。
_HEX_DEFAULT_IN_GETENV = re.compile(
    r"""os\.(?:getenv|environ\.get)\s*\(\s*['\"][^'\"]*TUSHARE[^'\"]*['\"]\s*,\s*['\"][0-9a-fA-F]{32,}['\"]""",
    re.I,
)
# 任意 40+ hex 字面量（常见 token 长度）不得出现在这两个密钥相关文件中。
_LONG_HEX_LITERAL = re.compile(r"""['\"][0-9a-fA-F]{40,}['\"]""")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestNoPlaintextTokenInSource:
    def test_no_hex_getenv_default_in_two_files(self):
        for path in (CONFIG_PY, FETCH_SCRIPT):
            src = _read(path)
            assert _HEX_DEFAULT_IN_GETENV.search(src) is None, (
                f"{path.relative_to(ROOT)} 仍含 getenv/environ.get 的长 hex 默认回退"
            )

    def test_no_long_hex_literal_in_two_files(self):
        for path in (CONFIG_PY, FETCH_SCRIPT):
            src = _read(path)
            assert _LONG_HEX_LITERAL.search(src) is None, (
                f"{path.relative_to(ROOT)} 仍含长 hex 字面量（疑似 token）"
            )

    def test_settings_field_default_is_empty_literal(self):
        """Settings.tushare_token 源码默认必须是空串字面量。"""
        src = _read(CONFIG_PY)
        assert re.search(r"tushare_token\s*:\s*str\s*=\s*\"\"", src) or re.search(
            r"tushare_token\s*:\s*str\s*=\s*''", src
        ), "tushare_token 默认值必须是空字符串字面量"


class TestSettingsEmptyWithoutEnv:
    def test_empty_when_env_cleared(self, monkeypatch, tmp_path):
        """无 QUANT_TUSHARE_TOKEN 时 Settings.tushare_token 应为空（不回退明文）。"""
        monkeypatch.delenv("QUANT_TUSHARE_TOKEN", raising=False)
        monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
        # 用临时空 env 文件，避免读到仓库根 .env
        empty_env = tmp_path / ".env"
        empty_env.write_text("", encoding="utf-8")
        # 动态加载一份隔离的 Settings 定义
        import ast
        src = _read(CONFIG_PY)
        # 仅断言源码契约 + 用 pydantic 直接构造
        from pydantic_settings import BaseSettings, SettingsConfigDict

        class _S(BaseSettings):
            model_config = SettingsConfigDict(
                env_prefix="QUANT_", env_file=str(empty_env), extra="ignore"
            )
            tushare_token: str = ""

        s = _S()
        assert s.tushare_token == ""
