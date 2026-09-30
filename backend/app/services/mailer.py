"""信号发布通道：邮件（HTML）。

凭据迁移（2026-09-30 老刘拍板「复用」）：
- SMTP：quantdesk notify.config.json（smtp.163.com:465，levitt.liu@163.com）
首次调用时自动从旧项目导入到 ``data/notify_channels.json``（data/ 已 gitignore，
凭据不入库不进 git）。之后以该文件为准；老刘后续应轮换 SMTP 授权码。

飞书通道按 2026-09-30 决策移除（不用飞书）；send_feishu 保留备用，
重新启用时需在配置里补 feishu.webhook_url。

所有发送写 NotifyLog（dedup_key 去重，同渠道同 key 只发一次）。
"""

from __future__ import annotations

import json
import smtplib
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from sqlalchemy.orm import Session

from app.config import DATA_DIR
from app.models import NotifyLog

CONFIG_PATH = Path(DATA_DIR) / "notify_channels.json"

_QUANTDESK_NOTIFY = Path(
    "/Users/happyljew/Workbuddy/2026-08-11-16-10-38/chanlun-site/notify.config.json"
)


def load_config() -> dict:
    """读取发布通道配置；首次调用时从旧项目一次性迁移。"""
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    cfg: dict = {"email": {"enabled": False}}
    try:
        qd = json.loads(_QUANTDESK_NOTIFY.read_text(encoding="utf-8"))
        em = qd.get("email", {})
        if em.get("enabled") and em.get("pass"):
            cfg["email"] = {
                "enabled": True,
                "smtp_host": em.get("smtpHost", "smtp.163.com"),
                "smtp_port": int(em.get("smtpPort", 465)),
                "user": em.get("user", ""),
                "password": em.get("pass", ""),
                "from_addr": em.get("from", em.get("user", "")),
                "to": em.get("to", []),
            }
    except Exception:  # noqa: BLE001
        pass

    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    return cfg


def send_email_html(db: Session, subject: str, html: str,
                    dedup_key: str = "") -> dict:
    """发送 HTML 邮件；dedup_key 非空时同渠道同 key 只发一次。"""
    cfg = load_config().get("email", {})
    if dedup_key and _already_sent(db, "email", dedup_key):
        return {"ok": True, "skipped": True, "reason": "dedup 命中"}

    if not cfg.get("enabled"):
        return _log(db, "email", subject, dedup_key, ok=False, error="邮件通道未配置")

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = cfg["from_addr"]
        msg["To"] = ", ".join(cfg["to"])
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], timeout=20) as srv:
            srv.login(cfg["user"], cfg["password"])
            srv.sendmail(cfg["from_addr"], cfg["to"], msg.as_string())
        return _log(db, "email", subject, dedup_key, ok=True)
    except Exception as exc:  # noqa: BLE001
        return _log(db, "email", subject, dedup_key, ok=False, error=str(exc)[:300])


def send_feishu(db: Session, title: str, lines: list[str],
                dedup_key: str = "") -> dict:
    """发送飞书富文本消息（grouped post 格式，与 Sequoia 雷达一致）。"""
    cfg = load_config().get("feishu", {})
    if dedup_key and _already_sent(db, "feishu", dedup_key):
        return {"ok": True, "skipped": True, "reason": "dedup 命中"}
    if not cfg.get("enabled"):
        return _log(db, "feishu", title, dedup_key, ok=False, error="飞书通道未配置")

    payload = {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": title,
                    "content": [[{"tag": "text", "text": ln}] for ln in lines],
                }
            }
        },
    }
    try:
        resp = requests.post(cfg["webhook_url"], json=payload, timeout=15,
                             proxies={"http": None, "https": None})
        ok = resp.ok and resp.json().get("code") in (0, None)
        return _log(db, "feishu", title, dedup_key, ok=bool(ok),
                    error=None if ok else resp.text[:300])
    except Exception as exc:  # noqa: BLE001
        return _log(db, "feishu", title, dedup_key, ok=False, error=str(exc)[:300])


def _already_sent(db: Session, channel: str, dedup_key: str) -> bool:
    """去重按「渠道 + key」：邮件已发不代表飞书已发（2026-09-30 实测踩坑）。"""
    from sqlalchemy import select

    row = db.scalars(
        select(NotifyLog).where(NotifyLog.channel == channel,
                                NotifyLog.dedup_key == dedup_key,
                                NotifyLog.status == "ok").limit(1)
    ).first()
    return row is not None


def _log(db: Session, channel: str, subject: str, dedup_key: str,
         ok: bool, error: str | None = None) -> dict:
    db.add(NotifyLog(channel=channel, subject=subject[:250], dedup_key=dedup_key,
                     status="ok" if ok else "fail", error=error))
    db.commit()
    return {"ok": ok, "channel": channel, "error": error,
            "at": datetime.now().isoformat(timespec="seconds")}
