"""境外 RSS/Atom 适配器（OPS-FN-1a）。

优先 stdlib xml.etree + httpx；单源超时 10s；统一浏览器 UA（Fed/SEC 常要）。
返回归一条目：key / title / summary / url / published_at / extra
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

logger = logging.getLogger("foreign_news")

SOURCE_TIMEOUT = 10.0

# Fed / SEC 等对缺 UA 的请求常 403
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36 QuantDesk/foreign-news"
)

_HTML_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    t = _HTML_TAG.sub(" ", s or "")
    return _WS.sub(" ", t).strip()


def _local(tag: str) -> str:
    """去掉 XML 命名空间前缀：{ns}item → item。"""
    if tag and "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag or ""


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    # 拼接子文本（Atom 有时把内容拆开）
    parts = [el.text or ""]
    for c in el:
        parts.append(_text(c))
        parts.append(c.tail or "")
    return "".join(parts).strip()


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    # RFC 2822（RSS pubDate）
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except Exception:  # noqa: BLE001
        pass
    # ISO / Atom
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except Exception:  # noqa: BLE001
        return None


def _child(el: ET.Element, *names: str) -> ET.Element | None:
    want = set(names)
    for c in el:
        if _local(c.tag) in want:
            return c
    return None


def _link_of(el: ET.Element) -> str:
    """RSS <link>text</link> 或 Atom <link href="..."/> / rel=alternate。"""
    # 先找带 href 的（Atom）
    best = ""
    for c in el:
        if _local(c.tag) != "link":
            continue
        href = (c.attrib.get("href") or "").strip()
        rel = (c.attrib.get("rel") or "alternate").lower()
        if href and rel in ("alternate", ""):
            return href
        if href and not best:
            best = href
        txt = _text(c).strip()
        if txt.startswith("http") and not best:
            best = txt
    if best:
        return best
    # 纯文本 link
    n = _child(el, "link")
    return _text(n) if n is not None else ""


def _guid_of(el: ET.Element) -> str:
    for name in ("guid", "id"):
        n = _child(el, name)
        if n is not None:
            t = _text(n)
            if t:
                return t
            href = (n.attrib.get("href") or "").strip()
            if href:
                return href
    return ""


def _parse_items(root: ET.Element) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    # RSS: channel/item；Atom: feed/entry；也容忍直接挂在根下
    nodes: list[ET.Element] = []
    for el in root.iter():
        loc = _local(el.tag)
        if loc in ("item", "entry"):
            nodes.append(el)

    for el in nodes:
        title = _strip_html(_text(_child(el, "title")))
        if len(title) < 4:
            continue
        url = _link_of(el)
        guid = _guid_of(el)
        summary_raw = ""
        for name in ("description", "summary", "content", "content:encoded"):
            # content:encoded 在 ET 里通常是 {ns}encoded 或带冒号的本地名
            n = _child(el, name, name.split(":")[-1] if ":" in name else name)
            if n is not None:
                summary_raw = _text(n)
                if summary_raw:
                    break
        # 再扫一遍 encoded
        if not summary_raw:
            for c in el:
                if _local(c.tag) in ("encoded", "content", "description", "summary"):
                    summary_raw = _text(c)
                    if summary_raw:
                        break
        published = None
        for name in ("pubDate", "published", "updated", "date"):
            n = _child(el, name)
            if n is not None:
                published = _parse_dt(_text(n))
                if published:
                    break
        key = url or guid or f"{title}|{published.isoformat() if published else ''}"
        items.append({
            "key": key,
            "title": title,
            "summary": _strip_html(summary_raw),
            "url": url,
            "published_at": published,
            "extra": {
                "lang": "en",
                "guid": guid or None,
            },
        })
    return items


def fetch_rss(url: str, timeout: float = SOURCE_TIMEOUT) -> list[dict[str, Any]]:
    """拉一条 RSS/Atom URL，返回归一条目列表。失败抛异常（由编排层隔离）。"""
    headers = {
        "User-Agent": _UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    }
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        r = client.get(url)
        r.raise_for_status()
        raw = r.content
    # 部分源带 BOM / 声明编码怪异，ET 从 bytes 更稳
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        # 再试一次：剥掉控制字符
        text = raw.decode(r.encoding or "utf-8", errors="replace")
        text = "".join(ch for ch in text if ord(ch) >= 32 or ch in "\n\r\t")
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            raise RuntimeError(f"RSS 解析失败：{e}") from e
    return _parse_items(root)
