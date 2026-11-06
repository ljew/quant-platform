#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 core + chanlun + strategy 合并成单文件，供直接贴进 QMT 编辑器。

为什么必须单文件：QMT 内置策略是「一个 .py 一个策略」，同目录 import 在部分
券商版本上不可靠（工作目录不是策略目录）。合成后零 import 依赖，只有标准库。

用法：
    python qmt/build_single.py
    → 生成 qmt/fresh_ma_qmt.py
"""
from __future__ import print_function

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "fresh_ma_qmt.py")

HEADER = '''# -*- coding: utf-8 -*-
"""fresh_ma · QMT 单文件策略（自动生成，勿手改）

由 qmt/build_single.py 合成，来源：
  qmt/chanlun_py36.py  缠论引擎（Python 3.6 兼容版，算法零改动）
  qmt/core.py          策略内核（选股/门控/风控/订单）
  qmt/strategy.py      QMT 入口（取数/报单/状态）

选股一致性：与平台 backend/app/services/xq 的实现在全市场 5209 只 × 5 个交易日
逐日比对，两个策略命中名单完全一致（qmt/verify_parity.py）。

部署：
  1. QMT 客户端 → 策略编辑器 → 新建 Python 策略 → 全选粘贴本文件
  2. 改顶部 ACCOUNT 为你的资金账号
  3. 先跑模拟盘，确认「[order] BUY ...」日志真的出现（这是最容易失效的一步）
  4. 报备通过后再切实盘

合规：指令在 QMT 终端内生成。本文件不发起网络请求、不读写本地文件。
"""

'''

IMPORT_BLOCK = '''import json
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

'''


def strip_module(text, drop_imports=True):
    """去掉文件头部的 import 与文档字符串，返回纯代码体。"""
    lines = text.splitlines()
    out = []
    in_doc = False
    doc_done = False
    for ln in lines:
        s = ln.strip()
        if not doc_done and s.startswith('"""') and not in_doc:
            in_doc = True
            if s.count('"""') >= 2:
                in_doc = False
                doc_done = True
            continue
        if in_doc:
            if '"""' in s:
                in_doc = False
                doc_done = True
            continue
        if drop_imports and (s.startswith("import ") or s.startswith("from ")):
            continue
        out.append(ln)
    return "\n".join(out).strip("\n")


def main():
    with open(os.path.join(HERE, "chanlun_py36.py"), encoding="utf-8") as f:
        chan_src = f.read()
    with open(os.path.join(HERE, "bootstrap_exc.py"), encoding="utf-8") as f:
        boot_src = f.read()
    with open(os.path.join(HERE, "core.py"), encoding="utf-8") as f:
        core_src = f.read()
    with open(os.path.join(HERE, "strategy.py"), encoding="utf-8") as f:
        strat_src = f.read()

    chan_body = strip_module(chan_src)
    boot_body = strip_module(boot_src)
    core_body = strip_module(core_src)
    strat_body = strip_module(strat_src)

    # 入口层里的模块引用改成同文件直调
    strat_body = strat_body.replace("core.", "")
    strat_body = strat_body.replace("chan.analyze_chanlun", "analyze_chanlun")
    strat_body = strat_body.replace("chan.Candle", "Candle")

    # core 里对 DEFAULT_CONFIG 的引用在本文件内直接可用，无需改写

    parts = [
        HEADER,
        IMPORT_BLOCK,
        "# " + "=" * 70,
        "# 第一部分 · 缠论引擎（qmt/chanlun_py36.py）",
        "# " + "=" * 70,
        chan_body,
        "",
        "# " + "=" * 70,
        "# 第一部分之二 · 门控引导数据（qmt/bootstrap_exc.py）",
        "# " + "=" * 70,
        boot_body,
        "",
        "# " + "=" * 70,
        "# 第二部分 · 策略内核（qmt/core.py）",
        "# " + "=" * 70,
        core_body,
        "",
        "# " + "=" * 70,
        "# 第三部分 · QMT 入口（qmt/strategy.py）",
        "# " + "=" * 70,
        strat_body,
        "",
    ]
    src = "\n".join(parts)

    # 合并后不应再有跨模块引用残留
    leftovers = [w for w in ("core.", "chan.", "import core", "import chanlun")
                 if re.search(r"\b" + re.escape(w) + r"\w*\(", src)]
    if leftovers:
        print("⚠ 残留跨模块引用: %s" % leftovers, file=sys.stderr)

    with open(OUT, "w", encoding="utf-8") as f:
        f.write(src)

    # 校验：3.6 语法 + 无内置泛型
    import ast
    try:
        ast.parse(src, feature_version=(3, 6))
        print("✓ 3.6 语法通过")
    except SyntaxError as e:
        print("✗ 3.6 语法失败 行 %s: %s" % (e.lineno, e.msg), file=sys.stderr)
        return 1
    bad = [(n.lineno, n.value.id) for n in ast.walk(ast.parse(src))
           if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)
           and n.value.id in ("list", "dict", "tuple", "set")]
    if bad:
        print("⚠ 内置泛型残留: %s" % bad[:5], file=sys.stderr)

    print("✓ 已生成 %s（%d 行，%.1f KB）"
          % (OUT, len(src.splitlines()), len(src.encode("utf-8")) / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
