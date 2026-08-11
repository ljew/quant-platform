"""缠论(Chan Theory)核心分析模块。

处理流程：K线包含处理 → 分型(顶/底) → 笔 → 中枢 → 三类买卖点。
输入 bars 需为升序日K，字段含 open/high/low/close/date（与回测引擎一致）。
所有函数均为纯计算，不依赖框架，便于回测与实盘复用。

买卖点定义（实战简化版，忠于缠论核心逻辑）：
- 一买 buy1：下跌趋势末端，最后一段下降笔相对前一段下降笔背驰（跌幅收窄）且创新低；
- 二买 buy2：一买后回升、回调不破一买低点；
- 三买 buy3：向上笔突破中枢上沿后，回踩下降笔的低点不进入中枢；
- 卖点 sell1/2/3 与买点对称（上升背驰、反弹不过前高、跌破中枢反抽不回）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def _merge_inclusion(bars: List[dict]) -> List[dict]:
    """按缠论规则处理相邻 K 线的包含关系，返回合并后的 K 线序列。

    合并后的 K 线保留其『原始索引 orig_idx』，用于把分型/笔映射回原始 bars 位置。
    包含处理规则：
    - 向上方向取『高高』(max high) 与『低低』(max low)；
    - 向下方向取『低低』(min low) 与『高高』(min high)。
    """
    if not bars:
        return []
    out: List[dict] = [dict(bars[0])]
    out[0]["orig_idx"] = 0
    for i in range(1, len(bars)):
        cur = dict(bars[i])
        cur["orig_idx"] = i
        top = out[-1]
        # 是否存在包含关系：一根的高低区间完全落在另一根内
        top_in_cur = (cur["high"] >= top["high"] and cur["low"] <= top["low"])
        cur_in_top = (top["high"] >= cur["high"] and top["low"] <= cur["low"])
        if not (top_in_cur or cur_in_top):
            out.append(cur)
            continue
        # 有包含：确定当前方向（用已合并序列最后两根比较；不足则用 cur 与 top 比较）
        if len(out) >= 2:
            prev = out[-2]
            direction = "up" if top["high"] > prev["high"] else "down"
        else:
            direction = "up" if cur["high"] > top["high"] else "down"
        if direction == "up":
            top["high"] = max(top["high"], cur["high"])
            top["low"] = max(top["low"], cur["low"])
        else:
            top["high"] = min(top["high"], cur["high"])
            top["low"] = min(top["low"], cur["low"])
        # 合并后保留 top 的 orig_idx（更早的端点，更贴近分型真实位置）
    return out


def _find_fractals(merged: List[dict]) -> List[Tuple[int, str]]:
    """在合并序列上找顶/底分型，返回 [(orig_idx, 'top'/'bottom'), ...]（升序）。"""
    fractals: List[Tuple[int, str]] = []
    n = len(merged)
    for i in range(1, n - 1):
        a, b, c = merged[i - 1], merged[i], merged[i + 1]
        if b["high"] > a["high"] and b["high"] > c["high"]:
            fractals.append((b["orig_idx"], "top"))
        elif b["low"] < a["low"] and b["low"] < c["low"]:
            fractals.append((b["orig_idx"], "bottom"))
    return fractals


def _build_bi(fractals: List[Tuple[int, str]], bars: List[dict], bi_gap: int) -> List[dict]:
    """由分型交替连接成笔。返回 [{dir, start_idx, end_idx, start_price, end_price}]。

    - 笔必须顶/底交替；
    - 相邻笔端点（原始K线索引）间隔 >= bi_gap（缠论最低约 5 根 K 线，默认 4）；
    - 同方向出现更极值分型时延伸当前笔终点（保证笔端点是极值）。
    """
    bi: List[dict] = []
    if len(fractals) < 2:
        return bi
    cur_idx, cur_type = fractals[0]
    for k in range(1, len(fractals)):
        f_idx, f_type = fractals[k]
        if f_type == cur_type:
            # 同方向：更极值则延伸当前笔终点
            if cur_type == "top" and bars[f_idx]["high"] > bars[cur_idx]["high"]:
                cur_idx = f_idx
            elif cur_type == "bottom" and bars[f_idx]["low"] < bars[cur_idx]["low"]:
                cur_idx = f_idx
            continue
        # 反向分型：间隔足够才确认一笔
        if f_idx - cur_idx >= bi_gap:
            if cur_type == "bottom":
                bi.append({
                    "dir": "up",
                    "start_idx": cur_idx, "end_idx": f_idx,
                    "start_price": bars[cur_idx]["low"], "end_price": bars[f_idx]["high"],
                })
            else:
                bi.append({
                    "dir": "down",
                    "start_idx": cur_idx, "end_idx": f_idx,
                    "start_price": bars[cur_idx]["high"], "end_price": bars[f_idx]["low"],
                })
            cur_idx, cur_type = f_idx, f_type
        # 间隔不足：忽略该反向分型，保持 cur 不变
    return bi


def _build_zhongshu(bi: List[dict]) -> List[dict]:
    """由连续 >=3 笔的重叠区间构成中枢，返回 [{start_idx, end_idx, low, high}]。

    重叠区间 = [max(各笔低点), min(各笔高点)]。相邻中枢不重叠重复。
    """
    zs: List[dict] = []
    n = len(bi)
    i = 0
    while i <= n - 3:
        lows: List[float] = []
        highs: List[float] = []
        j = i
        last_valid = -1
        while j < n:
            lo = min(bi[j]["start_price"], bi[j]["end_price"])
            hi = max(bi[j]["start_price"], bi[j]["end_price"])
            lows.append(lo)
            highs.append(hi)
            if max(lows) < min(highs):
                if (j - i + 1) >= 3:
                    last_valid = j
                j += 1
            else:
                break
        if last_valid >= i + 2:
            seg_lows = [min(bi[t]["start_price"], bi[t]["end_price"]) for t in range(i, last_valid + 1)]
            seg_highs = [max(bi[t]["start_price"], bi[t]["end_price"]) for t in range(i, last_valid + 1)]
            zs.append({
                "start_idx": bi[i]["start_idx"],
                "end_idx": bi[last_valid]["end_idx"],
                "low": max(seg_lows),
                "high": min(seg_highs),
            })
            i = last_valid + 1  # 跳到段末，避免重叠重复
        else:
            i += 1
    return zs


def _find_signals(bi: List[dict], zs: List[dict], bars: List[dict], need_trend: int) -> List[dict]:
    """基于笔与中枢识别三类买卖点，返回 [{'idx','date','type','price','reason'}]。

    reason 为触发该信号的『数据支撑』说明：包含具体价位、涨跌幅、背驰比、中枢区间等，
    使买卖点不再是黑盒（例如一买会写明累计跌幅、末笔相对前笔的背驰比、是否创阶段新低）。
    """
    signals: List[dict] = []
    if len(bi) < 3:
        return signals
    date_of = lambda idx: bars[idx]["date"] if 0 <= idx < len(bars) else ""

    down = [b for b in bi if b["dir"] == "down"]
    up = [b for b in bi if b["dir"] == "up"]

    # —— 一买：下跌趋势背驰（最后下降笔跌幅 < 前一下降笔，且创新低）——
    if len(down) >= max(2, need_trend):
        last, prev = down[-1], down[-2]
        last_drop = abs(last["start_price"] - last["end_price"])
        prev_drop = abs(prev["start_price"] - prev["end_price"])
        if last_drop > 0 and prev_drop > 0 and last_drop < prev_drop and last["end_price"] < prev["end_price"]:
            # 趋势高点取最近若干下跌笔起点（高点）的极值，避免取错参考点
            trend_high = max(d["start_price"] for d in down[-(need_trend + 1):])
            cum_drop = (trend_high - last["end_price"]) / trend_high if trend_high > 0 else 0.0
            last_pct = last_drop / last["start_price"] if last["start_price"] > 0 else 0.0
            prev_pct = prev_drop / prev["start_price"] if prev["start_price"] > 0 else 0.0
            ratio = last_drop / prev_drop
            reason = (f"一买：自 ¥{trend_high:.2f} 高位回落，累计跌幅 {cum_drop*100:.1f}%；"
                      f"末笔跌幅 {last_pct*100:.1f}% 小于前一笔 {prev_pct*100:.1f}%（背驰比 {ratio:.2f}），"
                      f"并创阶段新低 ¥{last['end_price']:.2f}，下跌动能衰竭、易反弹。")
            signals.append({"idx": last["end_idx"], "date": date_of(last["end_idx"]),
                            "type": "buy1", "price": last["end_price"], "reason": reason})

    # —— 一卖：上升趋势背驰（最后上升笔升幅 < 前一上升笔，且不创新高）——
    if len(up) >= max(2, need_trend):
        last, prev = up[-1], up[-2]
        last_rise = abs(last["end_price"] - last["start_price"])
        prev_rise = abs(prev["end_price"] - prev["start_price"])
        if last_rise > 0 and prev_rise > 0 and last_rise < prev_rise and last["end_price"] < prev["end_price"]:
            # 趋势低点取最近若干上升笔起点（低点）的极值
            trend_low = min(u["start_price"] for u in up[-(need_trend + 1):])
            cum_rise = (last["end_price"] - trend_low) / trend_low if trend_low > 0 else 0.0
            last_pct = last_rise / last["start_price"] if last["start_price"] > 0 else 0.0
            prev_pct = prev_rise / prev["start_price"] if prev["start_price"] > 0 else 0.0
            ratio = last_rise / prev_rise
            reason = (f"一卖：自 ¥{trend_low:.2f} 低位回升，累计涨幅 {cum_rise*100:.1f}%；"
                      f"末笔升幅 {last_pct*100:.1f}% 小于前一笔 {prev_pct*100:.1f}%（背驰比 {ratio:.2f}），"
                      f"且未创新高 ¥{last['end_price']:.2f}，上升动能衰竭、易回落。")
            signals.append({"idx": last["end_idx"], "date": date_of(last["end_idx"]),
                            "type": "sell1", "price": last["end_price"], "reason": reason})

    # —— 二买：一买后回调不破一买低点 ——
    b1 = next((s for s in signals if s["type"] == "buy1"), None)
    if b1:
        b1_idx, b1_low = b1["idx"], b1["price"]
        for b in bi:
            if b["dir"] == "down" and b["end_idx"] > b1_idx and b["end_price"] > b1_low:
                gap = (b["end_price"] - b1_low) / b1_low if b1_low > 0 else 0.0
                reason = (f"二买：一买 ¥{b1_low:.2f} 后回调整理，回调低点 ¥{b['end_price']:.2f} "
                          f"高于一买低点（+{gap*100:.1f}%），未创新低，下跌结构破坏、二买确认。")
                signals.append({"idx": b["end_idx"], "date": date_of(b["end_idx"]),
                                "type": "buy2", "price": b["end_price"], "reason": reason})
                break

    # —— 二卖：一卖后反弹不过一卖高点 ——
    s1 = next((s for s in signals if s["type"] == "sell1"), None)
    if s1:
        s1_idx, s1_high = s1["idx"], s1["price"]
        for b in bi:
            if b["dir"] == "up" and b["end_idx"] > s1_idx and b["end_price"] < s1_high:
                gap = (s1_high - b["end_price"]) / s1_high if s1_high > 0 else 0.0
                reason = (f"二卖：一卖 ¥{s1_high:.2f} 后反弹，高点 ¥{b['end_price']:.2f} "
                          f"低于一卖高点（差 {gap*100:.1f}%），未过前高，反弹结构破坏、二卖确认。")
                signals.append({"idx": b["end_idx"], "date": date_of(b["end_idx"]),
                                "type": "sell2", "price": b["end_price"], "reason": reason})
                break

    # —— 三买：突破中枢上沿后，回踩下降笔低点不进中枢 ——
    if zs:
        z = max(zs, key=lambda x: x["end_idx"])  # 取最近中枢
        for b in bi:
            if b["dir"] == "up" and b["start_idx"] >= z["start_idx"] and b["end_price"] > z["high"]:
                for b2 in bi:
                    if b2["dir"] == "down" and b2["start_idx"] > b["start_idx"] and b2["end_price"] > z["high"]:
                        gap = (b2["end_price"] - z["high"]) / z["high"] if z["high"] > 0 else 0.0
                        reason = (f"三买：向上突破中枢区间 [¥{z['low']:.2f}, ¥{z['high']:.2f}] 上沿"
                                  f"（突破高点 ¥{b['end_price']:.2f}）后回踩，"
                                  f"低点 ¥{b2['end_price']:.2f} 未跌回中枢（高于上沿 {gap*100:.1f}%），"
                                  f"三买确认，上涨趋势延续。")
                        signals.append({"idx": b2["end_idx"], "date": date_of(b2["end_idx"]),
                                        "type": "buy3", "price": b2["end_price"], "reason": reason})
                        break
                break

    # —— 三卖：跌破中枢下沿后，反抽上升笔高点不回中枢 ——
    if zs:
        z = max(zs, key=lambda x: x["end_idx"])
        for b in bi:
            if b["dir"] == "down" and b["start_idx"] >= z["start_idx"] and b["end_price"] < z["low"]:
                for b2 in bi:
                    if b2["dir"] == "up" and b2["start_idx"] > b["start_idx"] and b2["end_price"] < z["low"]:
                        gap = (z["low"] - b2["end_price"]) / z["low"] if z["low"] > 0 else 0.0
                        reason = (f"三卖：向下跌破中枢区间 [¥{z['low']:.2f}, ¥{z['high']:.2f}] 下沿"
                                  f"（破位低点 ¥{b['end_price']:.2f}）后反抽，"
                                  f"高点 ¥{b2['end_price']:.2f} 未回中枢（低于下沿 {gap*100:.1f}%），"
                                  f"三卖确认，下跌趋势延续。")
                        signals.append({"idx": b2["end_idx"], "date": date_of(b2["end_idx"]),
                                        "type": "sell3", "price": b2["end_price"], "reason": reason})
                        break
                break

    signals.sort(key=lambda s: s["idx"])
    seen = set()
    uniq = []
    for s in signals:
        key = (s["idx"], s["type"])
        if key not in seen:
            seen.add(key)
            uniq.append(s)
    return uniq


def analyze_chan(bars: List[dict], bi_gap: int = 4, need_trend: int = 2) -> Dict[str, Any]:
    """完整缠论分析。

    参数：
      bars       升序日K列表，字段含 open/high/low/close/date
      bi_gap     笔的最小 K 线间隔（默认 4）
      need_trend 一买/一卖所需的最小反向笔数（默认 2）

    返回：
      {
        'fractals': [(idx, type), ...],
        'bis':      [{dir,start_idx,end_idx,start_price,end_price}, ...],
        'zhongshu': [{start_idx,end_idx,low,high}, ...],
        'signals':  [{idx,date,type,price,reason}, ...],  # reason=触发该信号的数据支撑说明
      }
    """
    if len(bars) < bi_gap + 6:
        return {"fractals": [], "bis": [], "zhongshu": [], "signals": []}
    merged = _merge_inclusion(bars)
    fractals = _find_fractals(merged)
    bi = _build_bi(fractals, bars, bi_gap)
    zs = _build_zhongshu(bi)
    signals = _find_signals(bi, zs, bars, need_trend)
    return {"fractals": fractals, "bis": bi, "zhongshu": zs, "signals": signals}
