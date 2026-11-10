"""quant-mcp：把 quant-platform 的只读接口包成模型可调用的工具。

设计边界（写死在代码里，不靠自觉）：

  · **不查库**：只走 HTTP /api/v1/*。库是平台的，双写会让 SQLite 真损坏。
  · **不算指标**：超额、t 值、回撤全部来自平台返回值，这里只做字段整理。
    任何「顺手算一下」都会让口径分叉 —— 平台上已经有一套 PIT 和复权逻辑。
  · **不重试**：失败原样返回 ok=false。重试由 workflow 决定，避免雪崩。
  · **每个结果带 source + as_of + missing**：模型能看见「这是哪个接口、什么时候
    取的、缺什么」，才可能在回答里如实说「没查到」。

9 个工具全部只读，一个有副作用的都没有。写操作（create_paper_task /
propose_card_transition）留到第 3 步，且必须带审批令牌。

自检：
    python3 agent/quant_mcp.py --selftest    # 9 个工具逐个调通 + 耗时
    python3 agent/quant_mcp.py --check-urls  # 路径与 /openapi.json 逐一核对
    python3 agent/quant_mcp.py               # stdio 启动（给 dsh 用）
"""

from __future__ import annotations

import sys
import time

from mcp.server.mcpserver import MCPServer

import actions
from platform_api import API_BASE, dumps, err, get, ok, openapi_paths, post

mcp = MCPServer(
    "quant-mcp",
    version="0.1.0",
    instructions=(
        "量化投研平台的事实接口。所有工具只读、返回 JSON。"
        "回答里引用的每个数字都必须来自某次工具调用的 data，"
        "并写明 source 与 as_of；missing 非空时必须把它写进「没查到」。"
    ),
)


# ─────────────────────────── 工具实现 ───────────────────────────
# 纯函数，返回 dict。MCP 包装层只负责序列化，selftest 直接调这些函数。

def _list_strategies() -> dict:
    try:
        rows, url = get("/strategy/strategies")
        slim = [
            {
                "key": r.get("key"),
                "name": r.get("name"),
                "multi_asset": r.get("multi_asset"),
                "description": (r.get("description") or "")[:120],
            }
            for r in rows
        ]
        return ok(slim, f"GET {url}", note=(
            f"共 {len(slim)} 个策略。param_schema 未返回（几十 KB，对判断无用），"
            "需要具体参数请看策略页。"
        ))
    except Exception as e:  # noqa: BLE001
        return err(e, "GET /strategy/strategies")


def _get_backtests(strategy_key: str | None = None, limit: int = 5) -> dict:
    try:
        limit = max(1, min(int(limit or 5), 20))
        rows, url = get("/strategy/backtests", {"limit": limit})
        if strategy_key:
            rows = [r for r in rows if r.get("strategy_key") == strategy_key]
        keep = (
            "id", "strategy_key", "strategy_name", "start_date", "end_date",
            "total_return", "benchmark_total_return", "excess_return",
            "sharpe", "max_drawdown", "win_rate", "trade_count",
        )
        slim = [{k: r.get(k) for k in keep} for r in rows]
        missing = []
        note = f"{len(slim)} 条"
        if strategy_key and not slim:
            note = (f"按 {strategy_key} 过滤后 0 条。注意 /backtests 只返回最近 "
                    f"{limit} 条，不代表该策略从没跑过 —— 换个策略 key 或加大 limit。")
            missing.append(f"最近 {limit} 条回测里没有 {strategy_key}")
        return ok(slim, f"GET {url}", missing=missing, note=note)
    except Exception as e:  # noqa: BLE001
        return err(e, "GET /strategy/backtests")


def _get_signal_stats(window_days: int = 90) -> dict:
    try:
        window_days = max(5, min(int(window_days or 90), 365))
        d, url = get("/signal/stats", {"window_days": window_days})
        by_st = [
            {
                "strategy": r.get("strategy"),
                "n": r.get("n_5"),
                "exc_5d": r.get("exc_5"),
                "win_5d": r.get("win_5"),
                "t_5d": r.get("t_5"),
            }
            for r in (d.get("by_strategy") or [])
        ]
        missing = [] if by_st else ["窗口内没有任何策略的信号统计"]
        return ok(
            {
                "window_days": d.get("window_days"),
                "n_signals": d.get("n_signals"),
                "by_strategy": by_st,
                "by_streak": d.get("by_streak"),
                "by_tier": d.get("by_tier"),
            },
            f"GET {url}",
            missing=missing,
            note="exc_5d 是 5 日持有超额（%），t_5d 是显著性；|t|<2 不要下结论。",
        )
    except Exception as e:  # noqa: BLE001
        return err(e, "GET /signal/stats")


def _get_signal_actions(date: str | None = None) -> dict:
    try:
        d, url = get("/signal/actions", {"date": date})
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
        missing = [] if items else [f"{d.get('date') or date or '最新交易日'} 没有信号动作"]
        return ok(
            {"date": d.get("date"), "total": len(items), "top20": slim},
            f"GET {url}",
            missing=missing,
            note=f"共 {len(items)} 条，这里只给前 20 条明细。",
        )
    except Exception as e:  # noqa: BLE001
        return err(e, "GET /signal/actions")


def _list_research_cards(state: str | None = None) -> dict:
    try:
        d, url = get("/research/cards")
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
        missing = [] if slim else [f"state={state or '全部'} 下没有研究卡"]
        return ok(
            {"total": len(slim), "state_count": d.get("state_count"), "cards": slim},
            f"GET {url}",
            missing=missing,
            note="state: enabled / candidate / downgraded / retired；has_backtest 为 false "
                 "表示这张卡还没跑过回测，别据此判断好坏。",
        )
    except Exception as e:  # noqa: BLE001
        return err(e, "GET /research/cards")


def _get_research_card(card_id: int | None = None, key: str | None = None) -> dict:
    try:
        # 取列表后本地筛：一次拿到全部卡，既能按 id 也能按 key，
        # 还避免详情接口与列表接口返回结构不一致的问题（原型已验证）。
        d, url = get("/research/cards")
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
            return ok(
                None,
                f"GET {url}（列表后本地筛选）",
                missing=[f"没有 card_id={card_id} / key={key} 的研究卡"],
                note="先调 list_research_cards 看有哪些卡和它们的 id。",
            )
        keep = (
            "id", "key", "name", "card_type", "state", "strategy_key",
            "hypothesis", "data_spec", "evidence", "cost_note",
            "crowd_note", "fail_condition", "backtest", "updated_at",
        )
        return ok({k: hit.get(k) for k in keep},
                  f"GET {url}（列表后本地筛选）")
    except Exception as e:  # noqa: BLE001
        return err(e, "GET /research/cards")


def _list_paper_tasks() -> dict:
    try:
        rows, url = get("/paper/tasks")
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
                # 这个字段曾经因为没有返回，让智能体误判「模拟盘从没跑过」。不许再裁。
                "last_bar_date": t.get("last_bar_date"),
                "last_run_at": str(t.get("last_run_at") or "")[:19],
            }
            for t in rows
        ]
        missing = [f"任务 {t['name']} 的 last_bar_date 为空（可能还没跑过）"
                   for t in slim if not t.get("last_bar_date")]
        return ok(slim, f"GET {url}", missing=missing,
                  note=f"{len(slim)} 个任务。last_bar_date 是「净值算到哪一天」，"
                       "不是「最后一次运行时间」，两者要分开说。")
    except Exception as e:  # noqa: BLE001
        return err(e, "GET /paper/tasks")


def _get_framework_health() -> dict:
    try:
        d, url = get("/monitor/framework-health")
        layers = []
        for layer in (d.get("layers") or []):
            bad = [
                {"label": it.get("label"), "detail": str(it.get("detail"))[:80]}
                for it in (layer.get("items") or [])
                if it.get("status") != "OK"
            ]
            layers.append({
                "name": layer.get("name"),
                "status": layer.get("status"),
                "problems": bad,
            })
        return ok({"worst": d.get("worst"), "layers": layers}, f"GET {url}",
                  note="worst 是五层里最差的状态；problems 为空表示该层全部正常。")
    except Exception as e:  # noqa: BLE001
        return err(e, "GET /monitor/framework-health")


def _get_latest_digest() -> dict:
    try:
        d, url = get("/factor/brief-digest/latest")
        text = d.get("summary") or ""
        missing = [] if text else ["还没有生成日报（当日全景）"]
        return ok(
            {"day": d.get("day"), "model": d.get("model"),
             "chars": len(text), "summary": text[:1200]},
            f"GET {url}",
            missing=missing,
            note="summary 超过 1200 字的部分已截断；chars 是原文总字数。",
        )
    except Exception as e:  # noqa: BLE001
        return err(e, "GET /factor/brief-digest/latest")


# ─────────────────────────── 写操作（两段式） ───────────────────────────
# 这两个工具是全局唯一的写入口，都走「提议 → 人批准 → 执行」：
# 第一次调用只写 pending，返回 action_id；用户在审批中心通过后，
# 再带同一个 action_id 调一次才真正落库。
# 令牌由 actions.consume() 内部取，**不出现在工具入参里** ——
# 写成入参就可能被资讯正文里的注入文本骗走（这不是理论风险）。

CARD_STATES = ("enabled", "candidate", "watch", "downgraded", "retired")


def _create_paper_task(strategy_key: str, name: str = "", kind: str = "single",
                       symbols: str = "", index_code: str = "",
                       initial_cash: float = 2_000_000.0,
                       commission: float = 0.0001, slippage: float = 0.0015,
                       adj: str = "hfq", params_json: str = "{}",
                       action_id: int | None = None) -> dict:
    payload = {
        "strategy_key": strategy_key,
        "name": name or f"{strategy_key} 模拟盘",
        "kind": kind or "single",
        "symbols": symbols or "",
        "index_code": index_code or "",
        "initial_cash": float(initial_cash),
        "commission": float(commission),
        "slippage": float(slippage),
        "adj": adj or "hfq",
        "params_json": params_json or "{}",
    }
    if action_id is None:
        r = actions.propose("create_paper_task", payload,
                            reason=f"新建模拟盘：{payload['name']}",
                            proposed_by="agent")
        return ok({**r, "proposal": payload}, "agent_actions.propose", note=(
            "已提交审批，**尚未建盘**。让用户在审批中心通过后，"
            f"再带 action_id={r['action_id']} 调一次本工具才会真正创建。"
            f"提案 {r['expires_at']} 前有效。"
        ))

    got = actions.consume(action_id)
    if not got.get("ok"):
        return {**got, "source": "agent_actions.consume", "as_of": actions.now_iso(),
                "missing": ["未获得执行许可"], "hint": "审批没通过或已过期，不要重试；让用户重新审批。"}
    if got.get("action") != "create_paper_task":
        return {"ok": False, "error": f"action_id={action_id} 是 {got.get('action')}，不是建盘",
                "source": "agent_actions", "as_of": actions.now_iso(),
                "missing": ["动作类型不匹配"]}
    try:
        data, url = post("/paper/tasks", got["payload"])
    except Exception as e:  # noqa: BLE001
        actions.mark_failed(action_id, str(e))
        return err(e, "POST /paper/tasks")
    actions.mark_done(action_id, {"task_id": (data or {}).get("id")})
    return ok({"action_id": action_id, "task": data,
               "task_id": (data or {}).get("id")}, f"POST {url}",
              note="审批已消耗，动作完成。")


def _propose_card_transition(card_id: int, to_state: str, reason: str) -> dict:
    if to_state not in CARD_STATES:
        return {"ok": False, "error": f"to_state 必须是 {' / '.join(CARD_STATES)}",
                "source": "agent_actions", "as_of": actions.now_iso(),
                "missing": ["状态未变更"], "hint": "先看卡当前状态，再挑一个合法目标态。"}
    if len((reason or "").strip()) < 2:
        return {"ok": False, "error": "reason 至少要 2 个字",
                "source": "agent_actions", "as_of": actions.now_iso(),
                "missing": ["状态未变更"]}
    r = actions.propose("card_transition",
                        {"card_id": int(card_id), "to_state": to_state,
                         "reason": reason.strip()[:500]},
                        reason=f"研究卡 #{card_id} → {to_state}", proposed_by="agent")
    return ok({**r, "card_id": card_id, "to_state": to_state},
              "agent_actions.propose", note=(
                  "**只提议，卡片状态没变**。人在审批中心通过后由平台执行；"
                  f"提案 {r['expires_at']} 前有效。"
              ))


# ─────────────────────────── MCP 包装 ───────────────────────────
# 工具名与设计文档 13 节的契约一一对应，改名会破坏 persona 白名单。

def _wrap(fn, *args, **kwargs) -> str:
    return dumps(fn(*args, **kwargs))


@mcp.tool()
def list_strategies() -> str:
    """列出所有已注册策略的 key、名称、是否多标的。

    什么时候用：查某个策略之前先用它确认 key（key 拼错会查不到回测）。
    返回：strategies[] + source + as_of。param_schema 不返回。
    """
    return _wrap(_list_strategies)


@mcp.tool()
def get_backtests(strategy_key: str | None = None, limit: int = 5) -> str:
    """查回测记录的关键绩效：区间、总收益、基准、超额、夏普、最大回撤、胜率、成交笔数。

    什么时候用：判断策略「回测层面」好不好。
    注意：只返回最近 limit 条，过滤后 0 条不代表该策略从没跑过。
    参数：strategy_key 例 xq_chan_buy；limit 默认 5，上限 20。
    """
    return _wrap(_get_backtests, strategy_key, limit)


@mcp.tool()
def get_signal_stats(window_days: int = 90) -> str:
    """信号有效性统计：各策略 5 日持有超额、胜率、t 值，以及连榜/分级切分。

    什么时候用：判断「信号本身有没有效」，与回测表现分开看。
    注意：|t|<2 时只能说「无法判别」，不能下结论。
    参数：window_days 默认 90，上限 365。
    """
    return _wrap(_get_signal_stats, window_days)


@mcp.tool()
def get_signal_actions(date: str | None = None) -> str:
    """某个交易日的信号动作：买卖/观察/持有各多少条，明细前 20 条。

    什么时候用：回答「今天买什么 / 出了哪些信号」。
    参数：date 形如 2026-11-10，留空取最新交易日。
    """
    return _wrap(_get_signal_actions, date)


@mcp.tool()
def list_research_cards(state: str | None = None) -> str:
    """研究卡清单：id、名称、类型、状态、最近回测超额。

    什么时候用：找卡、看某张卡现在是什么状态、比较哪些卡没跑过回测。
    参数：state 取 enabled / candidate / downgraded / retired，留空取全部。
    """
    return _wrap(_list_research_cards, state)


@mcp.tool()
def get_research_card(card_id: int | None = None, key: str | None = None) -> str:
    """单张研究卡的完整内容：假设、数据口径、证据、失效条件、最近回测。

    什么时候用：判断一张卡「还成不成立」——重点看 hypothesis 与 fail_condition。
    参数：card_id 或 key 二选一（key 例 chan_buy）。
    """
    return _wrap(_get_research_card, card_id, key)


@mcp.tool()
def list_paper_tasks() -> str:
    """模拟盘任务：净值、收益率、持仓数、净值算到哪一天（last_bar_date）、最后运行时间。

    什么时候用：回答「模拟盘跑得怎么样」「有没有在正常跑」。
    注意：last_bar_date 是数据日期，last_run_at 是运行时间，不要把两者混说。
    """
    return _wrap(_list_paper_tasks)


@mcp.tool()
def get_framework_health() -> str:
    """五层健康检查（数据 / 因子 / 信号 / 回测 / 发布），异常时列出具体问题项。

    什么时候用：回答「数据有没有问题」「今天能不能信」。
    """
    return _wrap(_get_framework_health)


@mcp.tool()
def get_latest_digest() -> str:
    """最新一份当日全景（日报）：市场在交易什么、主线是什么。

    什么时候用：解释策略表现的市场背景；不用于判断个股。
    注意：summary 超过 1200 字已截断，chars 是原文总字数。
    """
    return _wrap(_get_latest_digest)


@mcp.tool()
def create_paper_task(strategy_key: str, name: str = "", kind: str = "single",
                      symbols: str = "", index_code: str = "",
                      initial_cash: float = 2000000.0, commission: float = 0.0001,
                      slippage: float = 0.0015, adj: str = "hfq",
                      params_json: str = "{}", action_id: int | None = None) -> str:
    """新建模拟盘。这是**两段式**操作，不要一次就以为建好了。

    第一段：不带 action_id 调用 → 只写一条待审批提案，返回 action_id；
    第二段：用户批准后，带同一个 action_id 再调一次 → 校验令牌并真正建盘。

    规则：
    · 不要在用户没批准时反复重试，也不要自己编 action_id。
    · 用户说「不用审批直接建」时，仍然只能走提议 —— 你没有绕过审批的能力。
    · 默认参数与现有 jk001r 口径一致：200 万、佣金万1、滑点 0.15%、hfq。
    """
    return _wrap(_create_paper_task, strategy_key, name, kind, symbols, index_code,
                 initial_cash, commission, slippage, adj, params_json, action_id)


@mcp.tool()
def propose_card_transition(card_id: int, to_state: str, reason: str) -> str:
    """提议修改研究卡状态（enabled / candidate / watch / downgraded / retired）。

    ⚠️ 只提议，不执行：卡片状态不会变。人要在审批中心点通过，平台才动作。
    reason 写清依据（哪一个数字、哪一段区间），它会进决策历史。
    """
    return _wrap(_propose_card_transition, card_id, to_state, reason)


# ─────────────────────────── 自检 ───────────────────────────
# 第 1 步验收：9 个工具逐个调通 + 路径与 /openapi.json 一致。
# 不用人眼看，跑这两条命令即可。

_SELFTEST: list[tuple[str, tuple, dict]] = [
    ("list_strategies", (), {}),
    ("get_backtests", (), {"limit": 3}),
    ("get_signal_stats", (), {"window_days": 90}),
    ("get_signal_actions", (), {}),
    ("list_research_cards", (), {}),
    ("get_research_card", (), {"card_id": 1}),
    ("list_paper_tasks", (), {}),
    ("get_framework_health", (), {}),
    ("get_latest_digest", (), {}),
]

# 工具 → 平台路径（不含 /api/v1 前缀），用来和 openapi 核对
_TOOL_PATHS: dict[str, str] = {
    "list_strategies": "/strategy/strategies",
    "get_backtests": "/strategy/backtests",
    "get_signal_stats": "/signal/stats",
    "get_signal_actions": "/signal/actions",
    "list_research_cards": "/research/cards",
    "get_research_card": "/research/cards",
    "list_paper_tasks": "/paper/tasks",
    "get_framework_health": "/monitor/framework-health",
    "get_latest_digest": "/factor/brief-digest/latest",
}


def selftest() -> int:
    print(f"API_BASE = {API_BASE}")
    failed = 0
    for name, args, kwargs in _SELFTEST:
        fn = globals()[f"_{name}"]
        t0 = time.perf_counter()
        try:
            res = fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {name:<22} 抛异常 {type(e).__name__}: {e}")
            failed += 1
            continue
        ms = (time.perf_counter() - t0) * 1000
        if not res.get("ok"):
            print(f"  ✗ {name:<22} {ms:6.0f}ms  {res.get('error')}")
            failed += 1
            continue
        data = res.get("data")
        size = len(data) if isinstance(data, (list, dict)) else 1
        miss = res.get("missing") or []
        flag = f"missing={len(miss)}" if miss else "ok"
        print(f"  ✓ {name:<22} {ms:6.0f}ms  {size} 项  {flag}")
    print(f"\n{9 - failed}/9 通过")
    return 1 if failed else 0


def check_urls() -> int:
    try:
        paths = openapi_paths()
    except Exception as e:  # noqa: BLE001
        print(f"拉 /openapi.json 失败：{type(e).__name__}: {e}")
        return 1
    print(f"openapi 共 {len(paths)} 个路径\n")
    bad = 0
    for tool, path in _TOOL_PATHS.items():
        full = f"/api/v1{path}"
        hit = full in paths
        print(f"  {'✓' if hit else '✗'} {tool:<22} {full}")
        bad += 0 if hit else 1
    print(f"\n{len(_TOOL_PATHS) - bad}/{len(_TOOL_PATHS)} 与 openapi 一致")
    return 1 if bad else 0


def selftest_actions() -> int:
    """审批状态机自检：不碰平台、不建盘，只验 propose/approve/consume/reject/过期。"""
    import tempfile
    from pathlib import Path

    actions.DB_PATH = Path(tempfile.mkdtemp()) / "actions.db"
    print(f"临时库 {actions.DB_PATH}")
    checks: list[tuple[str, bool]] = []

    r = actions.propose("create_paper_task", {"strategy_key": "t", "name": "测试"},
                        reason="自检", proposed_by="selftest")
    aid = r["action_id"]
    checks.append(("提议落 pending", r["status"] == "pending"))

    r2 = actions.propose("create_paper_task", {"strategy_key": "t", "name": "测试"})
    checks.append(("同提案幂等复用", r2["action_id"] == aid and r2["reused"]))

    got = actions.consume(aid)
    checks.append(("未批准不能执行", got["ok"] is False))

    a = actions.approve(aid)
    checks.append(("通过签令牌", a["ok"] and a["token_issued"]))

    got = actions.consume(aid)
    checks.append(("批准后可执行", got["ok"] and got["payload"]["name"] == "测试"))

    got2 = actions.consume(aid)
    checks.append(("令牌一次性", got2["ok"] is False))

    r3 = actions.propose("card_transition", {"card_id": 1, "to_state": "watch"})
    rj = actions.reject(r3["action_id"], "太短")
    checks.append(("否决要理由", rj["ok"] is False))
    rj = actions.reject(r3["action_id"], "证据不足，等下一轮回测再说")
    checks.append(("否决成功", rj["ok"] and actions.get(r3["action_id"])["status"] == "rejected"))

    r4 = actions.propose("card_transition", {"card_id": 2, "to_state": "retired"}, ttl_min=-1)
    actions.expire_stale()
    checks.append(("超时自动过期", actions.get(r4["action_id"])["status"] == "expired"))

    for name, passed in checks:
        print(f"  {'✓' if passed else '✗'} {name}")
    bad = [n for n, p in checks if not p]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} 通过")
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    if "--selftest-actions" in argv:
        return selftest_actions()
    if "--check-urls" in argv:
        return check_urls()
    mcp.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
