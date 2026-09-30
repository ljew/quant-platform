"""设计决策留痕（活文档机制）。

数据源：docs/decisions.json（仓库根，唯一事实来源，按时间升序追加）。
渲染目标：web/public/docs/architecture.html 第 10 章表格，行内容由
<!-- DECISIONS:BEGIN --> / <!-- DECISIONS:END --> 标记包裹，本模块负责
重新生成（public 与 web/dist 两份同步，dist 存在才写）。

追加入口：
- 策略档位变化：每周复盘（auto_research._review_body）自动调用
  record_tier_changes()，与上周快照 data/quality_snapshot.json 比对留痕。
- 其他重大变更（架构/流程/口径）：CLI 或代码调用 append()。
  CLI: python scripts/render_decisions.py --add "决策" --reason "理由"
"""
from __future__ import annotations

import html
import json
from datetime import date
from pathlib import Path

_SERVICES_DIR = Path(__file__).resolve().parent          # backend/app/services
_REPO_ROOT = _SERVICES_DIR.parent.parent.parent          # quant-platform/
DECISIONS_JSON = _REPO_ROOT / "docs" / "decisions.json"
HTML_PUBLIC = _REPO_ROOT / "web" / "public" / "docs" / "architecture.html"
HTML_DIST = _REPO_ROOT / "web" / "dist" / "docs" / "architecture.html"
SNAPSHOT = _REPO_ROOT / "data" / "quality_snapshot.json"

_BEGIN = "<!-- DECISIONS:BEGIN -->"
_END = "<!-- DECISIONS:END -->"


def _load() -> list[dict]:
    try:
        return json.loads(DECISIONS_JSON.read_text(encoding="utf-8"))["decisions"]
    except Exception:  # noqa: BLE001
        return []


def _save(rows: list[dict]) -> None:
    DECISIONS_JSON.write_text(
        json.dumps({"_readme": "由 decisions_log 维护；追加用 scripts/render_decisions.py --add",
                    "decisions": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8")


def render() -> bool:
    """按 decisions.json 重新生成架构文档的决策表。返回是否有文件被更新。"""
    rows = _load()
    body = "\n".join(
        f'<tr><td>{html.escape(str(r.get("date", "")))}</td>'
        f'<td>{html.escape(str(r.get("decision", "")))}</td>'
        f'<td>{html.escape(str(r.get("reason", "")))}</td></tr>'
        for r in rows)
    block = f"{_BEGIN}\n{body}\n{_END}"
    changed = False
    for path in (HTML_PUBLIC, HTML_DIST):
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if _BEGIN not in text or _END not in text:
            continue
        new = text[:text.index(_BEGIN)] + block + text[text.index(_END) + len(_END):]
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed = True
    return changed


def append(decision: str, reason: str, day: str | None = None) -> dict:
    """追加一条决策并重渲染。返回新条目。"""
    rows = _load()
    entry = {"date": day or date.today().isoformat(),
             "decision": decision, "reason": reason}
    rows.append(entry)
    _save(rows)
    render()
    return entry


def record_tier_changes(current: dict[str, str], context: dict[str, dict] | None = None) -> list[str]:
    """比对策略档位快照，变化的自动留痕。返回变化描述列表。

    current: STRATEGY_QUALITY；context: {strategy: {"exc_10":…, "n":…}} 复盘数据（可选）。
    首次运行只建快照不留痕（避免把历史定档重复记一遍）。
    """
    changes: list[str] = []
    old: dict[str, str] = {}
    try:
        if SNAPSHOT.exists():
            old = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        old = {}

    if old:
        for name, tier in sorted(current.items()):
            prev = old.get(name)
            if prev and prev != tier:
                ctx = (context or {}).get(name) or {}
                exc, n = ctx.get("exc_10"), ctx.get("n")
                detail = f"；当期 90 日 T+10 超额 {exc:+.2f}%（{n} 样本）" if exc is not None else ""
                append(f"信号「{name}」档位 {prev} → {tier}",
                       f"周复盘自动留痕{detail}")
                changes.append(f"{name}: {prev} → {tier}")
        for name in sorted(set(old) - set(current)):
            append(f"信号「{name}」下架（原档位 {old[name]}）", "周复盘自动留痕")
            changes.append(f"{name}: removed")

    SNAPSHOT.write_text(json.dumps(current, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return changes
