#!/usr/bin/env python3
"""修 wewe-rss 后台的「伪登录门」—— 让 AUTH_CODE 未配置时不再弹登录页。

背景（2026-11-02 排查）：
    模板 client/index.hbs 里写的是
        window.__WEWE_RSS_ENABLED_AUTH_CODE__ = '{{ enabledAuthCode }}';
    Handlebars 把布尔 false 渲染成字符串 'false'，而前端判断是
        SB = window.__WEWE_RSS_ENABLED_AUTH_CODE__ !== !1
    → 'false' !== false 恒为 true → 即使 AUTH_CODE 为空（后端根本不校验），
    后台也会强制跳 /dash/login 要你输入授权码。

做法：去掉模板里的引号，让它输出真正的布尔值。改完重启 wewe-rss 生效：
    launchctl kickstart -k gui/$(id -u)/com.quant.wechat-rss

为什么做成脚本：vendor/ 不入 git（403MB 且含 .env 凭据），重装或重新解包
wewe-rss 后补丁会丢，跑一次本脚本即可复原。幂等，重复执行无副作用。

用法：
    python3 backend/scripts/wechat/patch_wewe_auth_ui.py [--check]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HBS = Path(__file__).resolve().parents[3] / "vendor" / "wechat-rss" / "client" / "index.hbs"

BROKEN = "      window.__WEWE_RSS_ENABLED_AUTH_CODE__ = '{{ enabledAuthCode }}';"
FIXED = """      // 本机补丁：见 backend/scripts/wechat/patch_wewe_auth_ui.py
      // 原写法用单引号把布尔包成字符串，前端 'false' !== false 恒真 → 强制登录页。
      window.__WEWE_RSS_ENABLED_AUTH_CODE__ = {{ enabledAuthCode }};"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查是否需要打补丁")
    args = ap.parse_args()

    if not HBS.exists():
        print(f"找不到模板：{HBS}", file=sys.stderr)
        return 2

    text = HBS.read_text(encoding="utf-8")
    if "{{ enabledAuthCode }}" in text and "'{{ enabledAuthCode }}'" not in text:
        print("已打过补丁，无需处理")
        return 0
    if args.check:
        print("需要打补丁（当前仍会把 false 渲染成字符串）")
        return 1

    if text.count(BROKEN) != 1:
        print("模板结构与预期不符，请人工确认：", file=sys.stderr)
        print(repr([ln for ln in text.splitlines() if "ENABLED_AUTH_CODE" in ln]), file=sys.stderr)
        return 2

    HBS.write_text(text.replace(BROKEN, FIXED), encoding="utf-8")
    print(f"已打补丁：{HBS}")
    print("记得重启：launchctl kickstart -k gui/$(id -u)/com.quant.wechat-rss")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
