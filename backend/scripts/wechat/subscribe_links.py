#!/usr/bin/env python3
"""批量订阅公众号链接到 wewe-rss（旁车 :4000）。

用法:
  python3 wechat-monitor/subscribe_links.py < 链接文本文件
  或把链接作为参数: python3 wechat-monitor/subscribe_links.py "url1" "url2" ...

- 输入支持老式长链(/s?__biz=)和短链(/s/id)，每行一个或空格分隔，自动提取
- 流程: getMpInfo 解析 → 名单核对 → feed.add 订阅 → 延迟刷新 → 文章数验证
- 名单: 公众号-重点关注名单.txt（工作区根目录）
- 节奏: 每个源间隔 ~12s，防微信读书风控；全程无写 DB，只走 trpc
"""
import json, os, re, sys, time, urllib.request, urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import WATCH_LIST as WATCH  # noqa: E402
from paths import WEWE_URL as WXMON_URL  # noqa: E402
from paths import read_auth_code  # noqa: E402

AC = read_auth_code()
watch = (set(l.strip() for l in WATCH.read_text(encoding="utf-8").splitlines() if l.strip())
         if WATCH.exists() else set())


def trpc_post(proc, payload, timeout=60):
    req = urllib.request.Request(f"{WXMON_URL}/trpc/{proc}?batch=1",
        data=json.dumps({"0": payload}).encode(),
        headers={"Content-Type": "application/json", "Authorization": AC}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())[0]


def trpc_get(proc, payload):
    qs = urllib.parse.quote(json.dumps({"0": payload}))
    req = urllib.request.Request(f"{WXMON_URL}/trpc/{proc}?batch=1&input={qs}",
        headers={"Authorization": AC})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())[0]


def main():
    text = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else sys.stdin.read()
    links = re.findall(r"https://mp\.weixin\.qq\.com/s[^\s，,]*", text)
    if not links:
        print("未找到链接"); return 1
    print(f"共 {len(links)} 个链接")
    subs = []
    for i, link in enumerate(links, 1):
        try:
            d = trpc_post("platform.getMpInfo", {"wxsLink": link})
            if "error" in d:
                print(f"[{i}] 解析失败: {d['error']['message'][:80]}"); continue
            mp = d["result"]["data"][0]
            mark = "✓" if mp["name"] in watch else "⚠不在名单"
            add = trpc_post("feed.add", {"id": mp["id"], "mpName": mp["name"], "mpCover": mp["cover"],
                                         "mpIntro": mp["intro"], "updateTime": mp["updateTime"]})
            ok = "error" not in add
            print(f"[{i}] {mp['name']} {mark} | {'订阅成功' if ok else '订阅失败'}")
            if ok:
                subs.append((mp["name"], mp["id"]))
        except Exception as e:
            print(f"[{i}] 异常: {str(e)[:80]}")
        time.sleep(4)
    if not subs:
        return 0
    print("\n=== 刷新文章 ===")
    for name, mp in subs:
        try:
            trpc_post("feed.refreshArticles", {"mpId": mp})
            print("已触发:", name)
        except Exception as e:
            print("刷新异常:", name, str(e)[:60])
        time.sleep(12)
    time.sleep(30)
    print("\n=== 文章同步情况 ===")
    empty = []
    for name, mp in subs:
        try:
            d = trpc_get("article.list", {"limit": 3, "mpId": mp})
            data = d["result"]["data"]
            arts = data if isinstance(data, list) else data.get("items", [])
            if arts:
                latest = time.strftime("%Y-%m-%d", time.localtime(arts[0]["publishTime"]))
                print(f"{name}: {len(arts)} 篇（最新 {latest}）")
            else:
                print(f"{name}: 暂无（延迟，定时任务会补）"); empty.append(name)
        except Exception as e:
            print(name, "查询异常", str(e)[:60])
    return 0


if __name__ == "__main__":
    sys.exit(main())
