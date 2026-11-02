"""扫描剩余公众号是否使用「合集」，结果合并进 /tmp/album_scan.json。

只补扫尚未扫描的号，可重复执行（幂等）。
"""
import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.40"
)
SCAN = Path("/tmp/album_scan.json")


def get(url: str, timeout: int = 30, retry: int = 2) -> str:
    for i in range(retry + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Referer": "https://mp.weixin.qq.com/"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(4_000_000).decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) or i == retry:
                raise
            time.sleep(15)
        except Exception:
            if i == retry:
                raise
            time.sleep(8)
    return ""


def main() -> int:
    done = {x["mp_id"] for x in json.load(open(SCAN))}
    con = sqlite3.connect("file:/data/wechat/knowledge.db?mode=ro", uri=True)
    rows = con.execute(
        "select mp_id, mp_name, count(*) c from articles group by mp_id order by c desc"
    ).fetchall()
    todo = [r for r in rows if r[0] not in done]
    print(f"总 {len(rows)} 个号，已扫 {len(done)}，本次待扫 {len(todo)}\n")

    out = json.load(open(SCAN))
    hit = 0
    for i, (mp_id, name, cnt) in enumerate(todo, 1):
        row = con.execute(
            "select url from articles where mp_id=? and url like '%mp.weixin%' "
            "order by publish_time desc limit 1",
            (mp_id,),
        ).fetchone()
        if not row:
            print(f"[{i}/{len(todo)}] {name:<18} 无 mp 链接，跳过")
            continue
        try:
            html = get(row[0])
        except Exception as e:  # noqa: BLE001
            print(f"[{i}/{len(todo)}] {name:<18} 抓取失败 {type(e).__name__}")
            time.sleep(4)
            continue
        biz = re.search(r'var\s+biz\s*=\s*"([^"]+)"', html) or re.search(
            r"__biz=([A-Za-z0-9=+/%]+)", html
        )
        albums = list(
            dict.fromkeys(
                re.findall(r'album_id["\']?\s*[:=]\s*["\'](\d{6,})', html)
                + re.findall(r'data-album-id="(\d{6,})"', html)
                + re.findall(r"album_id=(\d{6,})", html)
            )
        )
        out.append(
            {"mp_name": name, "mp_id": mp_id, "articles": cnt,
             "biz": biz.group(1) if biz else "", "albums": albums}
        )
        if albums:
            hit += 1
            print(f"[{i}/{len(todo)}] {name:<18} 总{cnt:>4}篇 ✓ 合集 {len(albums)} 个")
        else:
            print(f"[{i}/{len(todo)}] {name:<18} 总{cnt:>4}篇 ✗")
        json.dump(out, open(SCAN, "w"), ensure_ascii=False, indent=1)
        time.sleep(2)

    n = sum(1 for x in out if x["albums"])
    print(f"\n本次新增有合集 {hit} 个；累计扫描 {len(out)} 个，其中有合集 {n} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
