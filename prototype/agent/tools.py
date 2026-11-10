"""工具层：把 quant-platform 的只读 API 包成智能体能调的函数。

三条设计原则（决定了智能体能不能给出可信结论）：

  ① **只返回决策要用的字段**。`/strategy/strategies` 返回的 param_schema 有几十 KB，
     对"这个策略怎么样"毫无用处，裁掉。工具返回什么，agent 就只能看到什么 ——
     这是控制幻觉的第一道闸门。

  ② **每个结果都带 source**。`GET /strategy/backtests` 这种。结论里的每个数字
     都能指回哪个接口、什么时候取的 —— 这是 provenance，不是可选项。

  ③ **失败不抛异常，返回 ok=False**。工具挂了要能被 agent 看见并如实说"没查到"，
     而不是让整个流程崩掉、或者模型自己编一个数。

全部是 GET，一个有副作用的都没有。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Callable

from .config import API_BASE, API_TIMEOUT


def _get(path: str, params: dict | None = None) -> tuple[Any, str]:
    """GET 一个 API，返回 (data, full_url)。异常统一抛给调用方包装。"""
    url = f"{API_BASE}{path}"
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=API_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8")), url


def _ok(data: Any, source: str, note: str = "") -> dict:
    out = {
        "ok": True,
        "data": data,
        "source": source,
        "as_of": datetime.now().isoformat(timespec="seconds"),
    }
    if note:
        out["note"] = note
    return out


def _err(e: Exception, source: str) -> dict:
    if isinstance(e, urllib.error.HTTPError):
        detail = f"HTTP {e.code}"
    elif isinstance(e, urllib.error.URLError):
        detail = f"连不上（{e.reason}）"
    else:
        detail = f"{type(e).__name__}: {e}"
    return {
        "ok": False,
        "error": detail,
        "source": source,
        "as_of": datetime.now().isoformat(timespec="seconds"),
        # 这句是给模型看的：它必须如实转述，不能自己补
        "hint": "调用失败，请在回答里如实说明「没查到」，不要推测数据。",
    }


# ─────────────────────────── 具体工具 ───────────────────────────

def list_strategies() -> dict:
    """所有已注册策略（key / 名称 / 是否多标的）。"""
    try:
        rows, url = _get("/strategy/strategies")
        slim = [
            {
                "key": r.get("key"),
                "name": r.get("name"),
                "multi_asset": r.get("multi_asset"),
                "description": (r.get("description") or "")[:120],
            }
            for r in rows
        ]
        return _ok(slim, f"GET {url}", f"共 {len(slim)} 个策略")
    except Exception as e:  # noqa: BLE001
        return _err(e, "GET /strategy/strategies")


def get_backtests(strategy_key: str | None = None, limit: int = 5) -> dict:
    """回测历史。按策略过滤，返回关键绩效字段（不含净值曲线）。"""
    try:
        rows, url = _get("/strategy/backtests", {"limit": max(1, min(limit, 20))})
        if strategy_key:
            rows = [r for r in rows if r.get("strategy_key") == strategy_key]
        keep = (
            "id", "strategy_key", "strategy_name", "start_date", "end_date",
            "total_return", "benchmark_total_return", "excess_return",
            "sharpe", "max_drawdown", "win_rate", "trade_count",
        )
        slim = [{k: r.get(k) for k in keep} for r in rows]
        note = f"{len(slim)} 条"
        if strategy_key and not slim:
            note = (f"过滤后 0 条（可能是该策略最近 {limit} 条里没有）。"
                    f"注意 /backtests 只返回最近 N 条，不代表该策略从没跑过。")
        return _ok(slim, f"GET {url}", note)
    except Exception as e:  # noqa: BLE001
        return _err(e, "GET /strategy/backtests")


def get_signal_stats(window_days: int = 90) -> dict:
    """信号六维统计：按策略 / 连榜 / 共振 / 分级 / 月份拆超额。"""
    try:
        d, url = _get("/signal/stats", {"window_days": window_days})
        by_st = []
        for r in (d.get("by_strategy") or []):
            by_st.append({
                "strategy": r.get("strategy"),
                "n": r.get("n_5"),
                "exc_5d": r.get("exc_5"),
                "win_5d": r.get("win_5"),
                "t_5d": r.get("t_5"),
            })
        return _ok(
            {
                "window_days": d.get("window_days"),
                "n_signals": d.get("n_signals"),
                "by_strategy": by_st,
                "by_streak": d.get("by_streak"),
                "by_tier": d.get("by_tier"),
            },
            f"GET {url}",
            "exc_5d 是 5 日持有超额（%），t_5d 是显著性；|t|<2 不要下结论",
        )
    except Exception as e:  # noqa: BLE001
        return _err(e, "GET /signal/stats")


def get_signal_actions(date: str | None = None) -> dict:
    """某个交易日的信号动作汇总（买/卖/观察/持有各多少，明细前 20 条）。"""
    try:
        d, url = _get("/signal/actions", {"date": date})
        items = d.get("items") or []
        slim = [
            {
                "symbol": it.get("symbol"),
                "name": it.get("name"),
                "action": it.get("action"),
                "strategy": it.get("strategy"),
                "score": it.get("score"),
            }
            for it in items[:20]
        ]
        return _ok(
            {"date": d.get("date"), "total": len(items), "top20": slim},
            f"GET {url}",
            f"共 {len(items)} 条，这里只给前 20 条明细",
        )
    except Exception as e:  # noqa: BLE001
        return _err(e, "GET /signal/actions")


def list_research_cards(state: str | None = None) -> dict:
    """研究卡清单（策略卡 / 因子卡），可按状态过滤。"""
    try:
        d, url = _get("/research/cards")
        cards = d.get("cards") or []
        if state:
            cards = [c for c in cards if c.get("state") == state]
        slim = [
            {
                "id": c.get("id"),
                "name": c.get("name"),
                "card_type": c.get("card_type"),
                "state": c.get("state"),
                "strategy_key": c.get("strategy_key"),
                "has_backtest": bool(c.get("backtest")),
                "excess": (c.get("backtest") or {}).get("excess_return"),
            }
            for c in cards
        ]
        return _ok(
            {"total": len(slim), "state_count": d.get("state_count"), "cards": slim},
            f"GET {url}",
        )
    except Exception as e:  # noqa: BLE001
        return _err(e, "GET /research/cards")


def get_research_card(card_id: int | None = None, key: str | None = None) -> dict:
    """单张研究卡的完整内容（假设 / 证据 / 失效条件 / 最近回测 / 决策历史）。"""
    try:
        d, url = _get("/research/cards")
        cards = d.get("cards") or []
        hit = None
        for c in cards:
            if card_id is not None and c.get("id") == card_id:
                hit = c
                break
            if key and c.get("key") == key:
                hit = c
                break
        if not hit:
            return _ok(None, f"GET {url}", f"没找到 card_id={card_id} key={key}")
        keep = (
            "id", "key", "name", "card_type", "state", "strategy_key",
            "hypothesis", "data_spec", "evidence", "cost_note",
            "crowd_note", "fail_condition", "backtest", "updated_at",
        )
        return _ok({k: hit.get(k) for k in keep}, f"GET {url}")
    except Exception as e:  # noqa: BLE001
        return _err(e, "GET /research/cards")


def list_paper_tasks() -> dict:
    """模拟盘任务：净值多少、跑到哪一天（last_bar_date）、最后一次运行时间。"""
    try:
        rows, url = _get("/paper/tasks")
        slim = [
            {
                "id": t.get("id"),
                "name": t.get("name"),
                "strategy_key": t.get("strategy_key"),
                "kind": t.get("kind"),
                "enabled": t.get("enabled"),
                "equity": t.get("equity"),
                "pnl_pct": t.get("pnl_pct"),
                "positions": t.get("positions_count"),
                "last_bar_date": t.get("last_bar_date"),
                "last_run_at": str(t.get("last_run_at") or "")[:19],
            }
            for t in rows
        ]
        return _ok(slim, f"GET {url}", f"{len(slim)} 个任务")
    except Exception as e:  # noqa: BLE001
        return _err(e, "GET /paper/tasks")


def get_framework_health() -> dict:
    """五层健康（数据 / 因子 / 信号 / 回测 / 发布），看有没有异常。"""
    try:
        d, url = _get("/monitor/framework-health")
        layers = []
        for l in (d.get("layers") or []):
            bad = [
                {"label": it.get("label"), "detail": str(it.get("detail"))[:80]}
                for it in (l.get("items") or [])
                if it.get("status") != "OK"
            ]
            layers.append({
                "name": l.get("name"),
                "status": l.get("status"),
                "problems": bad,
            })
        return _ok({"worst": d.get("worst"), "layers": layers}, f"GET {url}")
    except Exception as e:  # noqa: BLE001
        return _err(e, "GET /monitor/framework-health")


def get_latest_digest() -> dict:
    """最新一份当日全景（市场在交易什么、主线是什么）。"""
    try:
        d, url = _get("/factor/brief-digest/latest")
        t = d.get("summary") or ""
        return _ok(
            {"day": d.get("day"), "model": d.get("model"),
             "chars": len(t), "summary": t[:1200]},
            f"GET {url}",
            "超过 1200 字的部分已截断",
        )
    except Exception as e:  # noqa: BLE001
        return _err(e, "GET /factor/brief-digest/latest")


# ─────────────────────────── 注册表 ───────────────────────────
# 每个工具：实现 + 给模型看的说明 + 参数 schema。
# 新增工具只加一行，persona 的白名单按名字引用。

REGISTRY: dict[str, dict] = {
    "list_strategies": {
        "fn": list_strategies,
        "desc": "列出所有已注册策略的 key 和名称。查某个策略之前先用它确认 key。",
        "params": {"type": "object", "properties": {}, "required": []},
    },
    "get_backtests": {
        "fn": get_backtests,
        "desc": "查回测记录的关键绩效（收益/基准/超额/夏普/回撤/胜率/成交笔数）。"
                "可按 strategy_key 过滤。注意只返回最近若干条。",
        "params": {
            "type": "object",
            "properties": {
                "strategy_key": {"type": "string", "description": "策略 key，如 xq_chan_buy"},
                "limit": {"type": "integer", "description": "取最近几条，默认 5，上限 20"},
            },
            "required": [],
        },
    },
    "get_signal_stats": {
        "fn": get_signal_stats,
        "desc": "信号有效性统计：各策略的持有期超额、胜率、t 值。看信号本身有没有效。",
        "params": {
            "type": "object",
            "properties": {
                "window_days": {"type": "integer", "description": "统计窗口天数，默认 90"},
            },
            "required": [],
        },
    },
    "get_signal_actions": {
        "fn": get_signal_actions,
        "desc": "某个交易日的信号动作（买/卖/观察/持有）与明细前 20 条。",
        "params": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD，留空取最新"},
            },
            "required": [],
        },
    },
    "list_research_cards": {
        "fn": list_research_cards,
        "desc": "研究卡清单（策略卡/因子卡），含状态和最近回测超额。",
        "params": {
            "type": "object",
            "properties": {
                "state": {"type": "string",
                          "description": "enabled / candidate / downgraded / retired，留空取全部"},
            },
            "required": [],
        },
    },
    "get_research_card": {
        "fn": get_research_card,
        "desc": "单张研究卡的完整内容：假设、数据口径、证据、失效条件、最近回测、决策历史。",
        "params": {
            "type": "object",
            "properties": {
                "card_id": {"type": "integer", "description": "卡片 id"},
                "key": {"type": "string", "description": "卡片 key，如 chan_buy"},
            },
            "required": [],
        },
    },
    "list_paper_tasks": {
        "fn": list_paper_tasks,
        "desc": "模拟盘任务列表：净值、收益、持仓数、最近运行。",
        "params": {"type": "object", "properties": {}, "required": []},
    },
    "get_framework_health": {
        "fn": get_framework_health,
        "desc": "五层健康检查（数据/因子/信号/回测/发布），有异常时会列出具体问题。",
        "params": {"type": "object", "properties": {}, "required": []},
    },
    "get_latest_digest": {
        "fn": get_latest_digest,
        "desc": "最新当日全景：市场在交易什么、主线是什么。用于解释策略表现的市场背景。",
        "params": {"type": "object", "properties": {}, "required": []},
    },
}


def call_tool(name: str, params: dict | None = None) -> dict:
    """执行一个工具。名字不在注册表里 → 直接拒绝（不是「让它别调」）。"""
    if name not in REGISTRY:
        return {
            "ok": False,
            "error": f"未注册的工具：{name}",
            "hint": "只能调用白名单里的工具。",
            "as_of": datetime.now().isoformat(timespec="seconds"),
        }
    fn: Callable = REGISTRY[name]["fn"]
    try:
        return fn(**(params or {}))
    except TypeError as e:
        return {
            "ok": False,
            "error": f"参数不对：{e}",
            "source": f"tool:{name}",
            "as_of": datetime.now().isoformat(timespec="seconds"),
        }


def tool_specs(names: list[str]) -> list[dict]:
    """把白名单转成 OpenAI function-calling 的 tools 数组。"""
    out = []
    for n in names:
        if n not in REGISTRY:
            continue
        spec = REGISTRY[n]
        out.append({
            "type": "function",
            "function": {
                "name": n,
                "description": spec["desc"],
                "parameters": spec["params"],
            },
        })
    return out
