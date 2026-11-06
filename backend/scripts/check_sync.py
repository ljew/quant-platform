#!/usr/bin/env python3
"""校验「容器内代码」与「宿主机工作区」是否一致 —— 防止「改了没生效」。

背景（2026-11-06 事故）
----------------------
`docker-compose.yml` 只挂载了 `backend/app`，`backend/scripts` **从未挂载**。
结果是 11-02~11-06 期间所有脚本修复（公众号采集的锁身份校验、网络/限流分开、
errCode 驼峰识别、回补读取限 1.6MB）在宿主机改了、容器里跑的一直是镜像旧版。
表现是「改完没反应」「日志里没有新分支」，排查半天才想到比 md5。

用法（在宿主机仓库根执行）
------------------------
    python3 backend/scripts/check_sync.py
    python3 backend/scripts/check_sync.py --verbose

退出码：0 = 全部一致；1 = 有差异（此时容器跑的不是你正在改的代码）。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys

# (宿主机相对仓库根的目录, 容器内绝对路径)
PAIRS = [
    ("backend/app", "/app/app"),
    ("backend/scripts", "/app/scripts"),
    ("config", "/app/config"),
    ("web/dist", "/frontend"),
]

SKIP_SUFFIX = (".pyc", ".pyo", ".DS_Store")


def local_hashes(root: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if fn.endswith(SKIP_SUFFIX):
                continue
            p = os.path.join(dirpath, fn)
            try:
                with open(p, "rb") as f:
                    out[os.path.relpath(p, root)] = hashlib.md5(f.read()).hexdigest()
            except OSError:
                continue
    return out


def remote_hashes(container: str, cpath: str) -> dict[str, str]:
    cmd = (
        "find %s -type f ! -path '*__pycache__*' ! -name '*.pyc' "
        "! -name '.DS_Store' -exec md5sum {} +" % cpath
    )
    try:
        r = subprocess.run(
            ["docker", "exec", container, "sh", "-c", cmd],
            capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError:
        print("找不到 docker 命令", file=sys.stderr)
        return {}
    out: dict[str, str] = {}
    for line in r.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        h, p = parts
        out[os.path.relpath(p.strip(), cpath)] = h
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--container", default="quant-backend")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.chdir(root)

    bad = 0
    for lroot, croot in PAIRS:
        if not os.path.isdir(lroot):
            print(f"[skip] 宿主机没有 {lroot}")
            continue
        L = local_hashes(lroot)
        R = remote_hashes(args.container, croot)
        only_l = sorted(set(L) - set(R))
        only_r = sorted(set(R) - set(L))
        diff = sorted(k for k in set(L) & set(R) if L[k] != R[k])

        tag = "✅" if not (only_l or only_r or diff) else "❌"
        print(f"{tag} {lroot}: 宿主机 {len(L)} 个 / 容器 {len(R)} 个"
              f"{'' if not (only_l or only_r or diff) else f' | 差异 {len(only_l)+len(only_r)+len(diff)} 个'}")
        if not (only_l or only_r or diff):
            continue
        bad += 1
        for k in only_l:
            print("    仅在宿主机（需 restart 才生效）:", k)
        for k in only_r:
            print("    仅在容器（镜像里有、工作区已删）:", k)
        for k in diff:
            print("    内容不同（容器跑的是旧版）:", k)
        if not args.verbose:
            for k in (only_l + only_r + diff)[8:]:
                print("      ...", k)

    if bad:
        print("\n修复：docker compose restart backend（挂载目录）"
              "；若目录未挂载，检查 docker-compose.yml 的 volumes")
        return 1
    print("\n容器与宿主机完全一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
