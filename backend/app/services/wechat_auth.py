"""微信读书 Cookie 管理：状态检查 / 保存 / 立即验证。

## 为什么需要

公众号采集依赖 `data/wechat/weread_cookie.json` 里的一份登录态。它会过期
（返回 -2010/-2011/-2012/-2041），过期后采集脚本静默失败 —— 2026-11-10
早上就是这样：08:20 那轮「已最新 7 篇 / 失败 0」，实际第 8 个号就凭证失效了，
后面 103 个号一个没采。

以前续期要在终端里跑 Playwright 脚本，还得记得放哪（`/tmp` 会被清）。现在
放进管理界面：随时看状态、随时换 Cookie。

## 为什么不直接做扫码

容器里没有 Playwright/Chromium（镜像已 1.06G，再加 ~400MB）。而扫码要维持
一个有状态的浏览器会话，成本不小。这里先做**粘贴** —— 零依赖、100% 可靠，
扫码失败时它也是兜底。要扫码版可以后续加。
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("wechat_auth")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Cookie 失效的错误码（-2041 是「接口只给最新一篇」的老问题，不算失效，但一并提示）
EXPIRED_CODES = {-2010, -2011, -2012}


def _dir() -> Path:
    return Path(os.getenv("QUANT_WECHAT_DATA_DIR", "/data/wechat"))


def cookie_path() -> Path:
    return _dir() / "weread_cookie.json"


def feeds_path() -> Path:
    return _dir() / "feeds.json"


def load_raw():
    """返回原始 Cookie（list 或 str），不存在返回 None。"""
    p = cookie_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def cookie_header(raw) -> str:
    """转成请求头里的 `k=v; k=v` 形式。"""
    if isinstance(raw, str):
        return raw.strip()
    return "; ".join(f"{c['name']}={c['value']}" for c in raw if c.get("name"))


def parse_cookie(text: str) -> list[dict]:
    """把用户粘贴的内容解析成 Playwright 格式的 cookie 数组。

    支持三种粘贴来源：
      1. F12 → Console → `document.cookie` 的结果：`a=1; b=2`
      2. 请求头里复制的 `Cookie:` 后面那串：同上
      3. 已经是 JSON 数组（比如从旧的 weread_cookie.json 抄的）
    """
    s = (text or "").strip()
    if not s:
        raise ValueError("内容为空")
    if s.startswith("["):
        data = json.loads(s)
        if not isinstance(data, list) or not data:
            raise ValueError("JSON 不是非空数组")
        for c in data:
            if not isinstance(c, dict) or not c.get("name"):
                raise ValueError("JSON 里每项都要有 name/value")
        return data
    # `k=v; k=v` —— 去掉可能带的 "Cookie:" 前缀
    if s.lower().startswith("cookie:"):
        s = s[7:].strip()
    out: list[dict] = []
    for part in s.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if not k:
            continue
        out.append({
            "name": k, "value": v,
            "domain": ".weread.qq.com", "path": "/",
            "expires": -1, "httpOnly": False, "secure": True, "sameSite": "Lax",
        })
    if not out:
        raise ValueError("没能解析出任何 cookie（格式应为 `name=value; name2=value2`）")
    return out


def _first_mp_id() -> str:
    p = feeds_path()
    if not p.exists():
        return ""
    try:
        feeds = json.loads(p.read_text(encoding="utf-8"))
        return (feeds[0] or {}).get("mp_id", "") if feeds else ""
    except Exception:  # noqa: BLE001
        return ""


def probe(raw=None) -> dict:
    """用 Cookie 调一次 cover 接口，判断是否还有效。

    返回 {"ok": bool, "code": int|None, "reason": str, "mp": str}
    """
    raw = raw if raw is not None else load_raw()
    if raw is None:
        return {"ok": False, "code": None, "reason": "没有 Cookie 文件", "mp": ""}
    mp_id = _first_mp_id()
    if not mp_id:
        return {"ok": False, "code": None, "reason": "feeds.json 里没有公众号", "mp": ""}

    req = urllib.request.Request(
        f"https://weread.qq.com/api/mp/cover?bookId={mp_id}",
        headers={"Cookie": cookie_header(raw), "Referer": "https://weread.qq.com/",
                 "User-Agent": UA, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        if e.code == 499:
            return {"ok": True, "code": 499, "mp": mp_id,
                    "reason": "微信读书正在限流（HTTP 499），Cookie 本身可能是好的"}
        return {"ok": False, "code": e.code, "reason": f"HTTP {e.code}", "mp": mp_id}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": None, "reason": f"{type(e).__name__}: {str(e)[:80]}",
                "mp": mp_id}

    ec = d.get("errCode", d.get("errcode"))
    if ec in EXPIRED_CODES:
        return {"ok": False, "code": ec, "mp": mp_id,
                "reason": f"Cookie 已失效（errCode {ec}），需要重新登录"}
    review_id = d.get("reviewId") or ""
    title = str(d.get("title") or "")[:40]
    if not review_id:
        return {"ok": True, "code": ec, "mp": mp_id,
                "reason": "列表接口通了，但这个号暂无新文章（可能只是没更新）"}

    # ⚠️ 必须再测一次「正文接口」。Cookie 会**半失效**：列表接口还能拿到
    # reviewId，正文接口已经 -2012 登录超时 —— 2026-11-10 早上就是这样，
    # 采集日志显示「已最新 7 / 失败 0」，实际第 8 个号之后一个都没采到。
    # 只测列表会把这种状态误判成「有效」。
    try:
        req2 = urllib.request.Request(
            f"https://weread.qq.com/web/mp/content?reviewId={review_id}",
            headers={"Cookie": cookie_header(raw), "Referer": "https://weread.qq.com/",
                     "User-Agent": UA},
        )
        with urllib.request.urlopen(req2, timeout=20) as r2:
            body = r2.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        if e.code == 499:
            return {"ok": True, "code": 499, "mp": mp_id,
                    "reason": "微信读书正在限流（HTTP 499），Cookie 本身可能是好的"}
        return {"ok": False, "code": e.code, "mp": mp_id,
                "reason": f"正文接口 HTTP {e.code}", }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": None, "mp": mp_id,
                "reason": f"正文接口异常：{type(e).__name__}"}

    if body.lstrip().startswith("{"):
        try:
            ej = json.loads(body)
        except Exception:  # noqa: BLE001
            ej = {}
        ec2 = ej.get("errCode")
        if ec2 in EXPIRED_CODES:
            return {"ok": False, "code": ec2, "mp": mp_id,
                    "reason": f"列表可用但正文接口失效（{ec2} {ej.get('errMsg') or ''}）"
                              f"，采集会静默失败，需要重新登录"}
        return {"ok": False, "code": ec2, "mp": mp_id,
                "reason": f"正文接口返回异常：{str(body)[:80]}"}

    if "js_content" not in body:
        return {"ok": False, "code": None, "mp": mp_id,
                "reason": f"正文接口通了但拿不到正文结构（{len(body)} 字节），需要重新登录"}

    return {"ok": True, "code": ec, "mp": mp_id,
            "reason": f"有效（列表 + 正文都通），最新一篇：{title}"}


def status(check: bool = False) -> dict:
    """Cookie 当前状态。`check=True` 会真的联网探一次（约 1 秒）。"""
    raw = load_raw()
    p = cookie_path()
    info = {
        "exists": raw is not None,
        "path": str(p),
        "n_cookies": len(raw) if isinstance(raw, list) else (1 if raw else 0),
        "updated_at": (datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")
                       if p.exists() else None),
        "valid": None,
        "reason": "",
    }
    if raw is None:
        info["reason"] = "未配置 Cookie"
        info["valid"] = False
        return info
    if check:
        pr = probe(raw)
        info["valid"] = pr["ok"]
        info["reason"] = pr["reason"]
        info["code"] = pr.get("code")
    return info


def save(text: str) -> dict:
    """保存新 Cookie 并立即验证。"""
    cookies = parse_cookie(text)
    p = cookie_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # 原子写：先写临时文件再替换，避免采集中途读到半截 JSON
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cookies, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)
    logger.info("Cookie 已更新：%d 条", len(cookies))

    pr = probe(cookies)
    return {
        "ok": True,
        "saved": len(cookies),
        "valid": pr["ok"],
        "reason": pr["reason"],
        "code": pr.get("code"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
