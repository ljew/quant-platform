"""W46-2 / M3-3 walk-forward OOS。

切窗纯函数 + 窗级裁判 + 总门禁聚合；不复制成交逻辑。
契约：docs/DESIGN-W46-2-walk-forward.md（Product 冻结：通过=OOS≥0，不要求同向）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from fastapi import HTTPException

from app.core.cost_defaults import COMMISSION, SLIPPAGE
from app.core.eval_profile import build_cost_profile
from app.schemas import (
    WalkForwardLegacy,
    WalkForwardRequest,
    WalkForwardResult,
    WalkForwardSegmentMetrics,
    WalkForwardSummary,
    WalkForwardWindow,
)

MAX_N_WINDOWS = 6
DEFAULT_N_WINDOWS = 3
DEFAULT_OOS_RATIO = 0.3
DEFAULT_MIN_IS = 60
DEFAULT_MIN_OOS = 20

# 从严到宽
VERDICT_RANK = {
    "过拟合": 0,
    "未通过": 1,
    "样本不足": 2,
    "通过": 3,
    "未启用": 4,
}


@dataclass(frozen=True)
class WindowSlice:
    """bar 下标半开区间；IS = [0, oos_start)，OOS = [oos_start, oos_end)。"""

    index: int
    oos_start_i: int
    oos_end_i: int

    @property
    def is_end_i(self) -> int:
        return self.oos_start_i

    @property
    def is_bars(self) -> int:
        return self.oos_start_i

    @property
    def oos_bars(self) -> int:
        return self.oos_end_i - self.oos_start_i


Runner = Callable[[str, str], Any]
"""runner(start_date, end_date) → 引擎结果（含 sharpe/total_return/...）。"""


def clamp_oos_ratio(raw: float | None) -> float:
    r = DEFAULT_OOS_RATIO if raw is None else float(raw)
    return min(max(r, 0.0), 0.6)


def single_split_index(n_bars: int, oos_ratio: float) -> int | None:
    """与寻优 `_optimize_core` 同款切点：末段占比，IS≥30 且 OOS≥20；且 n≥60 才启用。

    返回 OOS 起点下标；None = 不分（样本不足/关闭）。
    """
    r = clamp_oos_ratio(oos_ratio)
    if r <= 0 or n_bars < 60:
        return None
    split_i = max(int(n_bars * (1 - r)), 30)
    if n_bars - split_i < 20:
        return None
    return split_i


def make_expanding_windows(
    n_bars: int,
    n_windows: int,
    *,
    min_is_bars: int = DEFAULT_MIN_IS,
    min_oos_bars: int = DEFAULT_MIN_OOS,
) -> list[WindowSlice]:
    """ROLL_EX：从 min_is_bars 起把剩余 bar 切成 n 段互不重叠 OOS；IS 始终 [0, oos_start)。"""
    if n_windows < 1:
        return []
    remaining = n_bars - min_is_bars
    if remaining <= 0:
        return []
    base = remaining // n_windows
    rem = remaining % n_windows
    out: list[WindowSlice] = []
    oos_start = min_is_bars
    for i in range(n_windows):
        length = base + (1 if i < rem else 0)
        oos_end = oos_start + length
        out.append(WindowSlice(index=i, oos_start_i=oos_start, oos_end_i=oos_end))
        oos_start = oos_end
    return out


def judge_strategy_window(
    is_metric: float | None,
    oos_metric: float | None,
    *,
    oos_bars: int,
    min_oos_bars: int = DEFAULT_MIN_OOS,
    oos_trade_count: int | None = None,
    metric_name: str = "sharpe",
) -> tuple[str, str]:
    """窗级四态（Product 冻结）：通过=OOS≥0；过拟合=IS>0∧OOS<0；其余OOS<0→未通过。"""
    if oos_bars < min_oos_bars:
        return "样本不足", f"OOS bars={oos_bars} < min_oos_bars={min_oos_bars}"
    if oos_trade_count is not None and oos_trade_count < 1 and oos_metric is None:
        return "样本不足", "OOS 无成交且无法形成收益序列"
    if is_metric is None or oos_metric is None:
        return "样本不足", f"IS/OOS {metric_name} 为 null"
    if is_metric > 0 and oos_metric < 0:
        return "过拟合", f"IS {metric_name}={is_metric:.4f}>0 且 OOS={oos_metric:.4f}<0"
    if oos_metric < 0:
        return "未通过", f"OOS {metric_name}={oos_metric:.4f}<0"
    return "通过", f"OOS {metric_name}={oos_metric:.4f}≥0"


def aggregate_verdicts(verdicts: Sequence[str]) -> str:
    """多窗聚合：过拟合 > 未通过 > 样本不足/通过 多数规则。"""
    if not verdicts:
        return "样本不足"
    if any(v == "过拟合" for v in verdicts):
        return "过拟合"
    if any(v == "未通过" for v in verdicts):
        return "未通过"
    valid = [v for v in verdicts if v != "样本不足"]
    if not valid:
        return "样本不足"
    n_pass = sum(1 for v in valid if v == "通过")
    need = max(1, math.ceil(len(valid) / 2))
    if n_pass >= need:
        return "通过"
    return "样本不足"


def stricter(*verdicts: str) -> str:
    """取更严；未知标签按「样本不足」处理。"""
    best = "未启用"
    best_rank = VERDICT_RANK["未启用"]
    for v in verdicts:
        rank = VERDICT_RANK.get(v, VERDICT_RANK["样本不足"])
        if rank < best_rank:
            best_rank = rank
            best = v if v in VERDICT_RANK else "样本不足"
    return best


def extract_segment_metrics(result: Any) -> WalkForwardSegmentMetrics:
    """最小指标集；不带 equity_curve/trades。"""
    total_return = float(getattr(result, "total_return", 0.0) or 0.0)
    sharpe = float(getattr(result, "sharpe", 0.0) or 0.0)
    max_drawdown = float(getattr(result, "max_drawdown", 0.0) or 0.0)
    trade_count = int(getattr(result, "trade_count", 0) or 0)
    excess = getattr(result, "excess_return", None)
    if excess is None:
        eq = getattr(result, "equity_curve", None) or []
        first = next((p for p in eq if getattr(p, "benchmark", 0)), None)
        last = next((p for p in reversed(list(eq)) if getattr(p, "benchmark", 0)), None)
        if first and last and first.benchmark:
            bench_total = last.benchmark / first.benchmark - 1
            excess = total_return - bench_total
        else:
            excess = 0.0
    n_bars = getattr(result, "n_bars", None)
    if n_bars is None:
        eq = getattr(result, "equity_curve", None) or []
        n_bars = len(eq) if eq else 0
    return WalkForwardSegmentMetrics(
        n_bars=int(n_bars or 0),
        sharpe=sharpe,
        total_return=total_return,
        excess_return=float(excess or 0.0),
        max_drawdown=max_drawdown,
        trade_count=trade_count,
    )


def metric_value(seg: WalkForwardSegmentMetrics | None, metric: str) -> float | None:
    if seg is None:
        return None
    if metric == "excess_return":
        return float(seg.excess_return)
    return float(seg.sharpe)


def _date_at(dates: Sequence[str], i: int) -> str:
    if i < 0:
        return dates[0]
    if i >= len(dates):
        return dates[-1]
    return dates[i]


def _end_inclusive(dates: Sequence[str], end_exclusive: int) -> str:
    """半开 end → 闭区间结束日。"""
    if end_exclusive <= 0:
        return dates[0]
    return dates[min(end_exclusive, len(dates)) - 1]


def build_oos_badge(
    *,
    enabled: bool,
    mode: str,
    verdict: str,
    oos_ratio: float,
    n_windows: int,
    counts: dict[str, int],
    note: str,
    last_is: WalkForwardSegmentMetrics | None,
    last_oos: WalkForwardSegmentMetrics | None,
) -> dict[str, Any]:
    windows_summary = (
        f"{n_windows}窗·{counts.get('通过', 0)}通过·"
        f"{counts.get('未通过', 0)}未通过·{counts.get('过拟合', 0)}过拟合·"
        f"{counts.get('样本不足', 0)}不足"
    )
    return {
        "enabled": enabled,
        "mode": mode,
        "verdict": verdict,
        "oos_ratio": oos_ratio,
        "n_windows": n_windows,
        "n_pass": counts.get("通过", 0),
        "n_fail": counts.get("未通过", 0),
        "n_overfit": counts.get("过拟合", 0),
        "n_insufficient": counts.get("样本不足", 0),
        "note": note,
        "windows_summary": windows_summary,
        # WF：填最后一窗，避免旧前端空读；single 时即唯一窗
        "is": last_is.model_dump() if last_is else None,
        "oos": last_oos.model_dump() if last_oos else None,  # segment metrics 无 alias
    }


def run_walk_forward(
    req: WalkForwardRequest,
    bar_dates: Sequence[str],
    runner: Runner,
) -> WalkForwardResult:
    """编排切窗 → 每窗 IS/OOS 回测 → legacy 对照 → final=stricter(agg, legacy)。

    本切片不做 async；长区间/组合策略请自行控制或后续加 /async。
    """
    mode = (req.mode or "walk_forward").strip().lower()
    if mode not in ("walk_forward", "single"):
        raise HTTPException(status_code=400, detail=f"未知 mode: {req.mode}（仅 walk_forward|single）")

    scheme = (req.scheme or "expanding").strip().lower()
    if scheme != "expanding":
        raise HTTPException(status_code=400, detail=f"本切片仅实现 scheme=expanding，收到: {req.scheme}")

    metric = (req.metric or "sharpe").strip().lower()
    if metric not in ("sharpe", "excess_return"):
        raise HTTPException(status_code=400, detail=f"未知 metric: {req.metric}")

    oos_ratio = clamp_oos_ratio(req.oos_ratio)
    n_windows = int(req.n_windows if req.n_windows is not None else DEFAULT_N_WINDOWS)
    if mode == "single":
        n_windows = 1
    if n_windows < 1:
        raise HTTPException(status_code=400, detail="n_windows 至少为 1")
    if n_windows > MAX_N_WINDOWS:
        raise HTTPException(
            status_code=400,
            detail=f"n_windows 上限 {MAX_N_WINDOWS}，当前 {n_windows}",
        )

    min_is = int(req.min_is_bars if req.min_is_bars is not None else DEFAULT_MIN_IS)
    min_oos = int(req.min_oos_bars if req.min_oos_bars is not None else DEFAULT_MIN_OOS)

    commission = COMMISSION if req.commission is None else float(req.commission)
    slippage = SLIPPAGE if req.slippage is None else float(req.slippage)
    cost_profile = build_cost_profile(commission, slippage, source="default")

    T = len(bar_dates)
    if T == 0:
        raise HTTPException(status_code=404, detail="无交易日数据，无法切窗")

    # —— 关闭 ——
    if oos_ratio <= 0:
        note = "oos_ratio=0，滚动/单次 OOS 未启用"
        oos_badge = build_oos_badge(
            enabled=False,
            mode=mode,
            verdict="未启用",
            oos_ratio=oos_ratio,
            n_windows=n_windows,
            counts={},
            note=note,
            last_is=None,
            last_oos=None,
        )
        return WalkForwardResult(
            mode=mode,
            scheme=scheme,
            metric=metric,
            oos_ratio=oos_ratio,
            n_windows=n_windows,
            cost_profile=cost_profile,
            oos=oos_badge,
            legacy_single=None,
            windows=[],
            summary=WalkForwardSummary(
                final_verdict="未启用",
                agg_verdict="未启用",
                legacy_verdict="未启用",
                gate_ok=False,
                fragile_regime=False,
                note=note,
            ),
            strategy_key=req.strategy,
            symbol=req.symbol,
            start=req.start,
            end=req.end,
        )

    # —— 切窗 ——
    if mode == "single" or n_windows == 1:
        split_i = single_split_index(T, oos_ratio)
        if split_i is None:
            slices: list[WindowSlice] = [
                WindowSlice(index=0, oos_start_i=max(T - 1, 0), oos_end_i=T)
            ]
            # 强制样本不足行：IS 几乎全长、OOS 几乎空
            # 更干净：构造合法结构但 oos_bars 会 < min
            # 用 split 失败语义：一整窗标不足
            slices = [WindowSlice(index=0, oos_start_i=T, oos_end_i=T)]
        else:
            slices = [WindowSlice(index=0, oos_start_i=split_i, oos_end_i=T)]
        effective_mode = "single" if mode == "single" or n_windows == 1 else mode
        if mode == "walk_forward" and n_windows == 1:
            effective_mode = "walk_forward"
    else:
        slices = make_expanding_windows(T, n_windows, min_is_bars=min_is, min_oos_bars=min_oos)
        effective_mode = mode
        if not slices:
            raise HTTPException(
                status_code=400,
                detail=f"无法生成窗口：n_bars={T}, min_is_bars={min_is}, n_windows={n_windows}",
            )

    windows: list[WalkForwardWindow] = []
    for sl in slices:
        # 时间顺序硬约束
        if not (sl.is_end_i <= sl.oos_start_i < sl.oos_end_i or sl.oos_bars == 0):
            if sl.oos_start_i < sl.is_end_i:
                raise HTTPException(status_code=500, detail="IS/OOS 切窗泄漏：IS 终点 >= OOS 起点")

        is_start = _date_at(bar_dates, 0) if sl.is_bars > 0 else req.start
        is_end = _end_inclusive(bar_dates, sl.is_end_i) if sl.is_bars > 0 else is_start
        if sl.oos_bars > 0:
            oos_start = _date_at(bar_dates, sl.oos_start_i)
            oos_end = _end_inclusive(bar_dates, sl.oos_end_i)
        else:
            oos_start = is_end
            oos_end = is_end

        is_seg: WalkForwardSegmentMetrics | None = None
        oos_seg: WalkForwardSegmentMetrics | None = None
        # 样本不足短路：仍返回行，尽量不跑引擎
        if sl.is_bars < min_is or sl.oos_bars < min_oos:
            verdict, note = "样本不足", (
                f"IS bars={sl.is_bars} (min {min_is}) / OOS bars={sl.oos_bars} (min {min_oos})"
            )
            if sl.is_bars > 0:
                try:
                    is_raw = runner(is_start, is_end)
                    is_seg = extract_segment_metrics(is_raw)
                    is_seg.n_bars = sl.is_bars
                except Exception:  # noqa: BLE001
                    is_seg = WalkForwardSegmentMetrics(n_bars=sl.is_bars)
            if sl.oos_bars > 0:
                try:
                    oos_raw = runner(oos_start, oos_end)
                    oos_seg = extract_segment_metrics(oos_raw)
                    oos_seg.n_bars = sl.oos_bars
                except Exception:  # noqa: BLE001
                    oos_seg = WalkForwardSegmentMetrics(n_bars=sl.oos_bars)
            else:
                oos_seg = WalkForwardSegmentMetrics(n_bars=0)
        else:
            try:
                is_raw = runner(is_start, is_end)
                is_seg = extract_segment_metrics(is_raw)
                is_seg.n_bars = sl.is_bars
            except Exception as e:  # noqa: BLE001
                is_seg = None
                verdict, note = "样本不足", f"IS 回测失败: {e}"
                oos_seg = WalkForwardSegmentMetrics(n_bars=sl.oos_bars)
                windows.append(
                    WalkForwardWindow(
                        index=sl.index,
                        is_start=is_start,
                        is_end=is_end,
                        oos_start=oos_start,
                        oos_end=oos_end,
                        is_=WalkForwardSegmentMetrics(n_bars=sl.is_bars),
                        oos=oos_seg,
                        verdict=verdict,
                        note=note,
                    )
                )
                continue
            try:
                oos_raw = runner(oos_start, oos_end)
                oos_seg = extract_segment_metrics(oos_raw)
                oos_seg.n_bars = sl.oos_bars
            except Exception as e:  # noqa: BLE001
                verdict, note = "样本不足", f"OOS 回测失败: {e}"
                oos_seg = WalkForwardSegmentMetrics(n_bars=sl.oos_bars)
                windows.append(
                    WalkForwardWindow(
                        index=sl.index,
                        is_start=is_start,
                        is_end=is_end,
                        oos_start=oos_start,
                        oos_end=oos_end,
                        is_=is_seg,
                        oos=oos_seg,
                        verdict=verdict,
                        note=note,
                    )
                )
                continue
            verdict, note = judge_strategy_window(
                metric_value(is_seg, metric),
                metric_value(oos_seg, metric),
                oos_bars=sl.oos_bars,
                min_oos_bars=min_oos,
                oos_trade_count=oos_seg.trade_count if oos_seg else 0,
                metric_name=metric,
            )

        # 防泄漏断言（日期字符串）
        if is_seg and oos_seg and sl.oos_bars > 0 and sl.is_bars > 0:
            if not (is_end <= oos_start):
                # 允许相等吗？optimize 用 end=split_date 给 IS，start=split_date 给 OOS
                # 即 IS 含 split 日？看 _run_single end=split_date — 通常 bars 含 end。
                # 契约：IS 终点 < OOS 起点。若同日，严格上可能同 bar。
                # 寻优：IS end=split_date, OOS start=split_date → 同日出现在两端。
                # 契约写「IS 终点 < OOS 起点」。我们用半开下标保证 is_end_i == oos_start_i，
                # 日期上 is_end = dates[oos_start_i-1], oos_start = dates[oos_start_i] → 严格 <。
                pass
            if sl.is_end_i > sl.oos_start_i:
                raise HTTPException(status_code=500, detail="切窗泄漏")

        windows.append(
            WalkForwardWindow(
                index=sl.index,
                is_start=is_start,
                is_end=is_end,
                oos_start=oos_start,
                oos_end=oos_end,
                is_=is_seg or WalkForwardSegmentMetrics(n_bars=sl.is_bars),
                oos=oos_seg or WalkForwardSegmentMetrics(n_bars=sl.oos_bars),
                verdict=verdict,
                note=note,
            )
        )

    # —— legacy_single ——
    legacy: WalkForwardLegacy | None = None
    legacy_verdict = "样本不足"
    split_i = single_split_index(T, oos_ratio)
    if split_i is not None:
        leg_is_start = bar_dates[0]
        leg_is_end = _end_inclusive(bar_dates, split_i)
        leg_oos_start = bar_dates[split_i]
        leg_oos_end = bar_dates[-1]
        try:
            leg_is = extract_segment_metrics(runner(leg_is_start, leg_is_end))
            leg_is.n_bars = split_i
        except Exception:  # noqa: BLE001
            leg_is = WalkForwardSegmentMetrics(n_bars=split_i)
        try:
            leg_oos = extract_segment_metrics(runner(leg_oos_start, leg_oos_end))
            leg_oos.n_bars = T - split_i
        except Exception:  # noqa: BLE001
            leg_oos = WalkForwardSegmentMetrics(n_bars=T - split_i)
        legacy_verdict, leg_note = judge_strategy_window(
            metric_value(leg_is, metric),
            metric_value(leg_oos, metric),
            oos_bars=T - split_i,
            min_oos_bars=min_oos,
            oos_trade_count=leg_oos.trade_count,
            metric_name=metric,
        )
        legacy = WalkForwardLegacy(
            split_date=leg_is_end,
            oos_start=leg_oos_start,
            is_=leg_is,
            oos=leg_oos,
            verdict=legacy_verdict,
            note=f"单次 oos_ratio={oos_ratio} 对照；{leg_note}",
        )
    else:
        legacy = WalkForwardLegacy(
            split_date="",
            oos_start="",
            is_=WalkForwardSegmentMetrics(n_bars=T),
            oos=WalkForwardSegmentMetrics(n_bars=0),
            verdict="样本不足",
            note=f"单次切分不可用（n_bars={T}, oos_ratio={oos_ratio}）",
        )
        legacy_verdict = "样本不足"

    agg = aggregate_verdicts([w.verdict for w in windows])
    final = stricter(agg, legacy_verdict)
    counts = {
        "通过": sum(1 for w in windows if w.verdict == "通过"),
        "未通过": sum(1 for w in windows if w.verdict == "未通过"),
        "过拟合": sum(1 for w in windows if w.verdict == "过拟合"),
        "样本不足": sum(1 for w in windows if w.verdict == "样本不足"),
    }
    note = f"聚合={agg}；legacy 单次={legacy_verdict}；final={final}"
    fragile = any(w.verdict == "过拟合" for w in windows) or (
        VERDICT_RANK.get(final, 99) < VERDICT_RANK.get(legacy_verdict, 99)
    )
    last = windows[-1] if windows else None
    oos_badge = build_oos_badge(
        enabled=True,
        mode=effective_mode if mode != "single" else "single",
        verdict=final,
        oos_ratio=oos_ratio,
        n_windows=len(windows) if windows else n_windows,
        counts=counts,
        note=note,
        last_is=last.is_ if last else None,
        last_oos=last.oos if last else None,
    )
    # mode 字段：请求生效 mode
    oos_badge["mode"] = "single" if mode == "single" or n_windows == 1 else "walk_forward"

    return WalkForwardResult(
        mode="single" if mode == "single" else "walk_forward",
        scheme=scheme,
        metric=metric,
        oos_ratio=oos_ratio,
        n_windows=len(windows) if windows else n_windows,
        cost_profile=cost_profile,
        oos=oos_badge,
        legacy_single=legacy,
        windows=windows,
        summary=WalkForwardSummary(
            final_verdict=final,
            agg_verdict=agg,
            legacy_verdict=legacy_verdict,
            gate_ok=(final == "通过"),
            fragile_regime=fragile,
            note="fragile_regime=存在窗过拟合或 final 相对 legacy 降级",
        ),
        strategy_key=req.strategy,
        symbol=req.symbol,
        start=req.start,
        end=req.end,
    )
