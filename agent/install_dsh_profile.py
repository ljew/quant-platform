#!/usr/bin/env python3
"""把 agent/dsh-profile 装进独立 home（~/quant-agent/profiles/quant）。

为什么要单独一套：智能体要改前端、改 preset 白名单、加 MCP，这些都是
**profile 级**配置。挂在 ~/.dsh/profiles/web 上就是改日常用的那个 dsh ——
界面变了、工具少了、左侧多出量化会话。所以另起一套 home（见 scripts/dsh-web.sh）。

拼装规则：
    [你的凭据条目]  +  [仓库里的 agent/dsh-profile/cordis.patch.yml]

前半段（ui-settings-general / llm-pi-ai）含 API Key，**不进仓库**；这里按 id
从你现有的 dsh profile 里抽出来，原样拼到前面。缺哪条就跳过并提示，不会把
整个配置写坏。

默认模型（agent-default-model）**故意不搬**：量化智能体默认走 DeepSeek 官方
deepseek-flash（=V4.1-Flash），这是平台的决定，写在仓库 patch 里。
DeepSeek 的 key 从平台库 llm_providers 读，写进 ~/quant-agent/.credentials.yaml
的 refs.DEEPSEEK_API_KEY —— 不给 key 时 deepseek route 会从模型选择器里隐藏
（不报错，只是选不到），所以这一步不能省。

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
# 平台库：DeepSeek 的 key 存在 llm_providers 里（页面可改，这里是同一份事实源）
DB = ROOT / "data" / "quant_dev.db"
# dsh 的凭据库：refs 里的名字就是 patch 里 apiKeyEnv 引用的名字
CREDS = Path.home() / "quant-agent" / ".credentials.yaml"

# 需要从原 profile 里搬过来的条目（含凭据，不入库）。
# agent-default-model 故意不搬：默认模型是平台的决定，写在仓库 patch 里。
CARRY = ("ui-settings-general", "llm-pi-ai")


def deepseek_key() -> str | None:
    """从平台库取 DeepSeek 的 key（优先 is_default，其次 id 最小的那条）。"""
    if not DB.exists():
        return None
    import sqlite3

    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            row = con.execute(
                "select api_key from llm_providers"
                " where api_key is not null and trim(api_key) <> ''"
                "   and (lower(coalesce(vendor,'')) like '%deepseek%'"
                "        or lower(coalesce(name,'')) like '%deepseek%')"
                " order by is_default desc, id limit 1"
            ).fetchone()
        finally:
            con.close()
    except Exception as exc:  # 库被锁/表不存在都不该拦住安装
        print(f"⚠️  读 {DB.name} 取 DeepSeek key 失败：{type(exc).__name__} {exc}")
        return None
    return row[0].strip() if row and row[0] and row[0].strip() else None


def sync_deepseek_key(key: str) -> bool:
    """把 key 写进 dsh 凭据库的 refs.DEEPSEEK_API_KEY（保留其余内容）。

    文件是极简 YAML（version / refs / records），没有 yaml 依赖也够用：
    先找 `  DEEPSEEK_API_KEY:` 整行替换，否则插到 `refs:` 行后面。
    """
    CREDS.parent.mkdir(parents=True, exist_ok=True)
    text = CREDS.read_text(encoding="utf-8") if CREDS.exists() else "version: 1\n"
    safe = key if re.fullmatch(r"[A-Za-z0-9._\-]+", key) else '"%s"' % key
    line = f"  DEEPSEEK_API_KEY: {safe}\n"

    if re.search(r"^\s+DEEPSEEK_API_KEY:.*$", text, re.M):
        text = re.sub(r"^[ \t]+DEEPSEEK_API_KEY:.*$", line.rstrip("\n"), text, flags=re.M)
        # 统一成文件里其它 ref 用的两空格缩进
        text = re.sub(r"^[ \t]+DEEPSEEK_API_KEY:", "  DEEPSEEK_API_KEY:", text, flags=re.M)
    elif re.search(r"^refs:\s*$", text, re.M):
        text = re.sub(r"^(refs:\s*)$", r"\1\n" + line.rstrip("\n"), text, count=1, flags=re.M)
    else:
        text = text.rstrip("\n") + "\nrefs:\n" + line

    if not text.endswith("\n"):
        text += "\n"
    tmp = CREDS.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CREDS)
    return True



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
        "# 前 %d 条是凭据（从 ~/.dsh/profiles/web 抽出来，含 API Key，不入库），\n"
        "# 后面才是平台自己加的：默认模型 / quant-mcp / 8 个 persona / 前端定制层。\n"
        % len(carried)
    )
    body = head + "\n" + "".join(carried) + "\n" + mine.read_text(encoding="utf-8")

    DST.mkdir(parents=True, exist_ok=True)
    (DST / "cordis.patch.yml").write_text(body, encoding="utf-8")
    for name in ("package.json", "cordis.yml"):
        shutil.copy2(SRC / name, DST / name)

    print(f"已同步到 {DST}（搬了 {len(carried)} 条凭据条目）")

    key = deepseek_key()
    if key:
        sync_deepseek_key(key)
        print(f"DeepSeek key 已写入 {CREDS}（refs.DEEPSEEK_API_KEY，{key[:6]}…）")
    else:
        print("⚠️  没取到 DeepSeek key，deepseek 模型不会出现在选择器里；")
        print("    到「大模型设置」页填好 key 后重跑本脚本即可。")

    print(f"重启生效：{ROOT}/scripts/dsh-web.sh stop && {ROOT}/scripts/dsh-web.sh start")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
