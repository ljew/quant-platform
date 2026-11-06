"""公众号全文库读写服务。

数据来自 knowledge.db（SQLite + FTS5 trigram），由 backend/scripts/wechat/crawl_content.py
在宿主机侧抓取写入，本服务**只读**。

两个环境差异要处理：
1. 路径 —— 容器内代码在 /app、数据挂载在 /data，所以数据目录走 settings.wechat_data_dir
   （compose 里用 QUANT_WECHAT_DATA_DIR=/data/wechat 覆盖），不硬编码 BASE_DIR/data。
2. 分词器 —— FTS5 用 trigram，**查询词不足 3 字符时永远命中 0**（"城投" 查不到，
   "城投债" 才查得到）。所以 _search_clause() 按长度分流：>=3 字走 FTS，否则走 LIKE。
   FTS 在个别环境不可用（如 sqlite 未编 trigram）时也回落 LIKE，不让页面崩。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from app.config import settings

_lock = threading.Lock()
_fts_ok: bool | None = None  # 缓存 FTS 可用性探测结果，避免每次请求都试
CHAT_DB = settings.wechat_data_dir


def _db_path() -> Path | None:
    p = Path(CHAT_DB) / "knowledge.db"
    return p if p.exists() else None


def _connect() -> sqlite3.Connection | None:
    p = _db_path()
    if p is None:
        return None
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


def _fts_available(con: sqlite3.Connection) -> bool:
    """探测 FTS 索引是否可查询（表存在且有数据）。结果缓存一次。

    注意：只在能确认结果时缓存。首次调用若库还没生成，不缓存 False，
    否则服务启动后新建的库要重启才能被检索到。
    """
    global _fts_ok
    with _lock:
        if _fts_ok is not None:
            return _fts_ok
        try:
            n = con.execute("SELECT count(*) FROM articles_fts").fetchone()[0]
            _fts_ok = n > 0
        except sqlite3.Error:
            return False  # 不缓存：可能是表还没建
        return _fts_ok


def _search_clause(q: str, con: sqlite3.Connection) -> tuple[str, list[Any]]:
    """返回 (where 片段, 参数)。q 已 strip。"""
    if not q:
        return "", []
    if len(q) >= 3 and _fts_available(con):
        # trigram 分词支持子串匹配，直接当 MATCH 查询用；引号包住避免语法注入。
        # articles_fts 是 external-content 表，rowid 与 articles.rowid 一一对应。
        return (" AND a.rowid IN (SELECT rowid FROM articles_fts "
                "WHERE articles_fts MATCH ?)"), [f'"{q}"']
    like = f"%{q}%"
    return " AND (a.title LIKE ? OR a.content_text LIKE ?)", [like, like]


def _rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


def list_mps(limit: int = 500) -> list[dict]:
    """公众号列表：篇数 + 最新发文时间 + 最近 7 天篇数。"""
    con = _connect()
    if con is None:
        return []
    try:
        now = int(time.time())
        rows = con.execute("""
            SELECT a.mp_id,
                   a.mp_name,
                   COUNT(*) AS n,
                   MAX(a.publish_time) AS last_ts,
                   SUM(CASE WHEN a.publish_time >= ? THEN 1 ELSE 0 END) AS n7d
            FROM articles a
            GROUP BY a.mp_id
            ORDER BY last_ts DESC
            LIMIT ?
        """, (now - 7 * 86400, limit)).fetchall()
        out = _rows_to_dicts(rows)
        for r in out:
            r["last_date"] = time.strftime("%Y-%m-%d", time.localtime(r["last_ts"])) if r["last_ts"] else ""
        return out
    except sqlite3.Error:
        return []
    finally:
        con.close()


def list_articles(mp_id: str = "", q: str = "", since_days: int = 0,
                  limit: int = 30, offset: int = 0) -> dict:
    """文章流。不返回全文，只给 160 字摘要（避免单次响应过大）。"""
    con = _connect()
    if con is None:
        return {"items": [], "total": 0, "db_missing": True}
    try:
        where = ["1=1"]
        params: list[Any] = []
        if mp_id:
            where.append("a.mp_id = ?")
            params.append(mp_id)
        if since_days:
            where.append("a.publish_time >= ?")
            params.append(int(time.time()) - since_days * 86400)
        clause, cparams = _search_clause(q.strip(), con)
        params.extend(cparams)

        sql_where = " AND ".join(where) + clause
        total = con.execute(
            f"SELECT COUNT(*) FROM articles a WHERE {sql_where}", params).fetchone()[0]
        rows = con.execute(f"""
            SELECT a.id, a.mp_id, a.mp_name, a.title, a.author,
                   a.publish_time, a.url, a.cover,
                   SUBSTR(a.content_text, 1, 160) AS digest,
                   LENGTH(a.content_text) AS chars
            FROM articles a
            WHERE {sql_where}
            ORDER BY a.publish_time DESC
            LIMIT ? OFFSET ?
        """, [*params, limit, offset]).fetchall()
        items = _rows_to_dicts(rows)
        for it in items:
            it["publish_date"] = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(it["publish_time"]))
        return {"items": items, "total": total, "offset": offset, "limit": limit}
    except sqlite3.Error as e:
        return {"items": [], "total": 0, "error": str(e)}
    finally:
        con.close()


def get_article(art_id: str) -> dict | None:
    con = _connect()
    if con is None:
        return None
    try:
        row = con.execute("""
            SELECT id, mp_id, mp_name, title, author, publish_time, url, cover,
                   content_text, fetched_at
            FROM articles WHERE id = ?
        """, (art_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["publish_date"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(d["publish_time"]))
        d["fetched_date"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(d["fetched_at"]))
        return d
    except sqlite3.Error:
        return None
    finally:
        con.close()


def stats() -> dict:
    """总览：总篇数、覆盖号数、时间跨度、今日/近 7 天新发。"""
    con = _connect()
    if con is None:
        return {"available": False, "msg": f"未找到 knowledge.db（{CHAT_DB}）"}
    try:
        now = int(time.time())
        today0 = int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)))
        row = con.execute("""
            SELECT COUNT(*) AS n, COUNT(DISTINCT mp_id) AS mps,
                   MIN(publish_time) AS first_ts, MAX(publish_time) AS last_ts,
                   SUM(LENGTH(content_text)) AS chars,
                   SUM(CASE WHEN publish_time >= ? THEN 1 ELSE 0 END) AS today,
                   SUM(CASE WHEN publish_time >= ? THEN 1 ELSE 0 END) AS last7d
            FROM articles
        """, (today0, now - 7 * 86400)).fetchone()
        d = dict(row)
        d.update({
            "available": True,
            "first_date": time.strftime("%Y-%m-%d", time.localtime(d["first_ts"])) if d["first_ts"] else "",
            "last_date": time.strftime("%Y-%m-%d %H:%M", time.localtime(d["last_ts"])) if d["last_ts"] else "",
            "stale_days": round((now - d["last_ts"]) / 86400, 1) if d["last_ts"] else None,
            "total_chars": d.pop("chars") or 0,
        })
        return d
    except sqlite3.Error as e:
        return {"available": False, "msg": str(e)}
    finally:
        con.close()


def daily(date_str: str = "") -> dict:
    """某一天的新发文（默认今天）。date_str 形如 2026-08-03。"""
    con = _connect()
    if con is None:
        return {"items": [], "date": date_str}
    try:
        if date_str:
            t0 = int(time.mktime(time.strptime(date_str, "%Y-%m-%d")))
            label = date_str
        else:
            t0 = int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)))
            label = time.strftime("%Y-%m-%d")
        t1 = t0 + 86400
        rows = con.execute("""
            SELECT a.id, a.mp_id, a.mp_name, a.title, a.author, a.publish_time, a.url,
                   SUBSTR(a.content_text, 1, 200) AS digest, LENGTH(a.content_text) AS chars
            FROM articles a
            WHERE a.publish_time >= ? AND a.publish_time < ?
            ORDER BY a.publish_time DESC
        """, (t0, t1)).fetchall()
        items = _rows_to_dicts(rows)
        for it in items:
            it["publish_time_str"] = time.strftime("%H:%M", time.localtime(it["publish_time"]))
        return {"date": label, "items": items, "n": len(items)}
    except (sqlite3.Error, ValueError) as e:
        return {"items": [], "date": date_str, "error": str(e)}
    finally:
        con.close()


def recent_days(days: int = 14) -> list[dict]:
    """最近 N 天每天的篇数（页面顶部小趋势用）。"""
    con = _connect()
    if con is None:
        return []
    try:
        t0 = int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))) - (days - 1) * 86400
        rows = con.execute("""
            SELECT publish_time FROM articles WHERE publish_time >= ?
        """, (t0,)).fetchall()
        bucket: dict[str, int] = {}
        for (ts,) in rows:
            d = time.strftime("%Y-%m-%d", time.localtime(ts))
            bucket[d] = bucket.get(d, 0) + 1
        out = []
        for i in range(days):
            d = time.strftime("%Y-%m-%d", time.localtime(t0 + i * 86400))
            out.append({"date": d, "n": bucket.get(d, 0)})
        return out
    except sqlite3.Error:
        return []
    finally:
        con.close()


# ───────────────────────── 订阅号管理 ─────────────────────────

def parse_mp_id(raw: str) -> str:
    """从用户粘贴的内容里提取公众号 bookId。

    用户不需要理解格式 —— 以下三种写法都接受，指向同一串东西：
      ① 整条链接  https://weread.qq.com/web/mp/content?reviewId=MP_WXS_3698093049_u96u8...
      ② 完整 reviewId                      MP_WXS_3698093049_u96u8Gimgcy3rLRtOhoBbw
      ③ 纯 bookId                          MP_WXS_3698093049
    """
    import re

    s = (raw or "").strip()
    m = re.search(r"MP_WXS_(\d+)", s)
    if not m:
        raise ValueError(
            "没找到公众号编号。请粘贴微信读书里那篇文章的分享链接"
            "（形如 https://weread.qq.com/web/mp/content?reviewId=MP_WXS_...），"
            "或直接填 MP_WXS_ 开头的那串编号。"
        )
    return "MP_WXS_" + m.group(1)


def add_feed(raw_id: str, mp_name: str = "", verify: bool = True) -> dict:
    """新增订阅号：写入 feeds.json（采集脚本读这份清单）。

    只需给 `raw_id`（链接 / reviewId / bookId 均可，见 parse_mp_id）。
    `mp_name` 可留空 —— 会自动调微信读书 cover 接口取公众号真名。
    已存在则只更新名称，不重复插入。
    """
    import json

    mp_id = parse_mp_id(raw_id)
    mp_name = (mp_name or "").strip()

    feeds_file = Path(CHAT_DB) / "feeds.json"
    feeds: list[dict] = []
    if feeds_file.exists():
        try:
            feeds = json.loads(feeds_file.read_text(encoding="utf-8")) or []
        except Exception:  # noqa: BLE001
            feeds = []

    # 名称来源优先级：用户填的 → cover 接口返回的 name → 库里的历史名 → 编号兜底
    info: dict = {}
    note = ""
    if verify:
        try:
            info = _probe_mp(mp_id)
            note = info.get("note") or ""
        except Exception as e:  # noqa: BLE001
            note = f"校验异常：{type(e).__name__}（已写入，下轮采集再看）"
    auto_name = (info or {}).get("name") or ""
    name = (mp_name or auto_name or _name_from_db(mp_id) or "").strip()

    for f in feeds:
        if f.get("mp_id") == mp_id:
            # ⚠️ 只有确实拿到真名才更新。2026-11-06 实测：probe 因 UA 被拒失败，
            # 这里拿编号兜底，把已有的「ABS视界」覆盖成了 MP_WXS_3698093049。
            if name and not name.startswith("MP_WXS_"):
                f["mp_name"] = name
            feeds_file.write_text(json.dumps(feeds, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
            return {"ok": True, "created": False, "mp_id": mp_id,
                    "mp_name": f.get("mp_name", ""), "verified": note,
                    "total": len(feeds)}

    mp_name = name or mp_id

    feeds.append({"mp_id": mp_id, "mp_name": mp_name, "cover": ""})
    feeds.sort(key=lambda f: f.get("mp_name", ""))
    feeds_file.write_text(json.dumps(feeds, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "created": True, "mp_id": mp_id, "mp_name": mp_name,
            "verified": note, "auto_named": bool(auto_name and not (mp_name or "").startswith("MP_WXS_")),
            "total": len(feeds)}


def _name_from_db(mp_id: str) -> str:
    """从已采集的文章里反查公众号名（probe 失败时的兜底，避免存成编号）。"""
    con = _connect()
    if con is None:
        return ""
    try:
        row = con.execute(
            "select mp_name from articles where mp_id = ? and mp_name <> '' limit 1",
            (mp_id,),
        ).fetchone()
        return (row[0] if row else "") or ""
    except Exception:  # noqa: BLE001
        return ""
    finally:
        con.close()


def _probe_mp(mp_id: str) -> dict:
    """调微信读书 cover 接口确认这个号能取到文章。

    返回 {"name": 公众号名, "title": 最新一篇标题, "note": 异常说明}。
    """
    import json as _json
    import urllib.error
    import urllib.request

    ck = Path(CHAT_DB) / "weread_cookie.json"
    if not ck.exists():
        return {"note": "未配置 Cookie，跳过校验"}
    raw = _json.loads(ck.read_text(encoding="utf-8"))
    cookie = raw if isinstance(raw, str) else "; ".join(
        f"{c['name']}={c['value']}" for c in raw)
    req = urllib.request.Request(
        f"https://weread.qq.com/api/mp/cover?bookId={mp_id}",
        headers={
            "Cookie": cookie,
            "Referer": "https://weread.qq.com/",
            # 实测：UA 只写 "Mozilla/5.0" 会被微信读书直接拒（HTTPError 403），
            # 必须是完整 Chrome UA 才返回 JSON。
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/126.0.0.0 Safari/537.36",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = _json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        # 499 = 微信读书风控限流（不是 Cookie 失效、也不是号有问题）。
        # 2026-11-06 实测：连续请求多了之后 cover/content 一律 499，
        # 采集脚本同一时间也在报「连续 5 个号取列表失败」而被熔断。
        if e.code == 499:
            return {"note": "微信读书正在限流（HTTP 499），已写入，等它自动恢复即可"}
        raise
    ec = d.get("errCode", d.get("errcode"))
    if ec in (-2010, -2011, -2012, -2041):
        return {"note": f"Cookie 失效（{ec}），已写入但需续期后才能采"}
    if not d.get("reviewId"):
        return {"name": d.get("name") or "", "note": "该号暂无可采文章（无 reviewId）"}
    return {"name": d.get("name") or "", "title": d.get("title") or ""}
