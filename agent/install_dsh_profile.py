#!/usr/bin/env python3
"""把 agent/dsh-profile 装进独立 home（~/quant-agent/profiles/quant）。

为什么要单独一套：智能体要改前端、改 preset 白名单、加 MCP，这些都是
**profile 级**配置。挂在 ~/.dsh/profiles/web 上就是改日常用的那个 dsh ——
界面变了、工具少了、左侧多出量化会话。所以另起一套 home（见 scripts/dsh-web.sh）。

拼装规则：
    [你的模型与凭据条目]  +  [仓库里的 agent/dsh-profile/cordis.patch.yml]

前半段（ui-settings-general / agent-default-model / llm-pi-ai）含 API Key，
**不进仓库**；这里按 id 从你现有的 dsh profile 里抽出来，原样拼到前面。
缺哪条就跳过并提示，不会把整个配置写坏。

方向是**仓库 → 运行目录**：要改配置请改仓库这份，再跑本脚本；
直接在 ~/quant-agent 里改的话，下次跑这里会被覆盖。
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "agent" / "dsh-profile"
DST = Path.home() / "quant-agent" / "profiles" / "quant"
# 模型/凭据的来源：他自己在用的 dsh profile
CRED_SRC = Path.home() / ".dsh" / "profiles" / "web" / "cordis.patch.yml"

# 需要从原 profile 里搬过来的条目（含凭据，不入库）
CARRY = ("ui-settings-general", "agent-default-model", "llm-pi-ai")


def split_entries(text: str) -> list[tuple[str, str]]:
    """按顶层 `- id:` 切开 YAML 列表，返回 [(id, 原文块)]。

    只认**行首无缩进**的 `- id:` —— 嵌套在 insert/config 里的不算。
    """
    lines = text.splitlines(keepends=True)
    starts: list[int] = []
    for i, ln in enumerate(lines):
        if re.match(r"^- id:\s*\S+", ln):
            starts.append(i)
    out: list[tuple[str, str]] = []
    for idx, st in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        block = "".join(lines[st:end])
        m = re.match(r"^- id:\s*(\S+)", lines[st])
        out.append((m.group(1), block))
    return out


def main() -> int:
    mine = SRC / "cordis.patch.yml"
    if not mine.exists():
        print(f"缺 {mine}", file=sys.stderr)
        return 1

    carried: list[str] = []
    if CRED_SRC.exists():
        entries = dict(split_entries(CRED_SRC.read_text(encoding="utf-8")))
        for key in CARRY:
            if key in entries:
                carried.append(entries[key])
            else:
                print(f"⚠️  {CRED_SRC} 里没有 {key}，跳过（模型/凭据可能要手工配）")
    else:
        print(f"⚠️  找不到 {CRED_SRC}，模型与凭据不会带过去")

    head = (
        "# ⚠️ 本文件由 agent/install_dsh_profile.sh 生成，**别直接改这里**。\n"
        "# 改仓库里的 agent/dsh-profile/cordis.patch.yml 再跑脚本。\n"
        "#\n"
        "# 前 %d 条是模型与凭据（从 ~/.dsh/profiles/web 抽出来，含 API Key，不入库），\n"
        "# 后面才是平台自己加的：quant-mcp / 8 个 persona / preset 收紧 / 前端定制层。\n"
        % len(carried)
    )
    body = head + "\n" + "".join(carried) + "\n" + mine.read_text(encoding="utf-8")

    DST.mkdir(parents=True, exist_ok=True)
    (DST / "cordis.patch.yml").write_text(body, encoding="utf-8")
    for name in ("package.json", "cordis.yml"):
        shutil.copy2(SRC / name, DST / name)

    print(f"已同步到 {DST}（搬了 {len(carried)} 条模型/凭据条目）")
    print(f"重启生效：{ROOT}/scripts/dsh-web.sh stop && {ROOT}/scripts/dsh-web.sh start")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
