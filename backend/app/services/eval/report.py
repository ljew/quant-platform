"""评估报告渲染：把因子 IC / 策略绩效结果输出为自包含 HTML。

设计取舍
-------
· **自包含**：CSS/SVG 全部内联，无 CDN 依赖 —— 报告要能直接发微信、邮件、存档，
  外部依赖一断就白屏的排版等于没做。
· **A 股配色**：涨红跌绿（#d33a3a 涨 / #0f9d58 跌），与国内习惯一致。
  注意这与欧美相反，不能照搬境外模板。
· **显著性可视化**：t 值画成带 ±2 参考线的条形。
  只给数值会诱导「看到正 IC 就以为有效」，而 |t|<2 时其实什么都说明不了。
"""
from __future__ import annotations

import html
import os

import numpy as np
import pandas as pd

UP, DOWN, MUTED = "#d33a3a", "#0f9d58", "#8a8f98"
BG, CARD, BORDER, TEXT = "#f7f8fa", "#ffffff", "#e3e6eb", "#1f2329"

_CSS = f"""
*{{box-sizing:border-box}}
body{{margin:0;background:{BG};color:{TEXT};
 font:14px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}}
.wrap{{max-width:1100px;margin:0 auto;padding:32px 20px 64px}}
h1{{font-size:24px;margin:0 0 6px}} h2{{font-size:17px;margin:34px 0 12px;
 padding-left:9px;border-left:3px solid {UP}}}
.sub{{color:{MUTED};font-size:13px;margin-bottom:22px}}
.card{{background:{CARD};border:1px solid {BORDER};border-radius:10px;padding:18px 20px;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:8px 10px;text-align:right;border-bottom:1px solid {BORDER}}}
th{{background:#fafbfc;color:{MUTED};font-weight:600;text-align:right;white-space:nowrap}}
th:first-child,td:first-child{{text-align:left}}
tr:last-child td{{border-bottom:none}}
.pos{{color:{UP};font-weight:600}} .neg{{color:{DOWN};font-weight:600}}
.mut{{color:{MUTED}}}
.verdict{{border-radius:8px;padding:14px 16px;font-size:14px;line-height:1.7;
 background:#fff8e6;border:1px solid #f0d9a0}}
.verdict.ok{{background:#fdecec;border-color:#f3c2c2}}
.verdict.bad{{background:#eaf6ee;border-color:#bfe0cb}}
.note{{font-size:12.5px;color:{MUTED};line-height:1.7}}
.pill{{display:inline-block;padding:1px 8px;border-radius:20px;font-size:12px;
 background:#eef0f4;color:{MUTED};margin-right:6px}}
"""


def _fmt(v, nd=4, pct=False, signed=True):
    if v is None or (isinstance(v, float) and v != v):
        return '<span class="mut">—</span>'
    s = f"{v*100:+.{nd-2}f}%" if pct else f"{v:+.{nd}f}" if signed else f"{v:.{nd}f}"
    cls = "pos" if v > 0 else ("neg" if v < 0 else "mut")
    return f'<span class="{cls}">{s}</span>'


def _tcell(t):
    """t 值单元格：|t|>=2 加粗着色，否则灰显。"""
    if t is None or (isinstance(t, float) and t != t):
        return '<span class="mut">—</span>'
    if abs(t) >= 2:
        return f'<span class="{"pos" if t>0 else "neg"}">{t:+.2f}</span>'
    return f'<span class="mut">{t:+.2f}</span>'


def _tbar(rows: list[tuple[str, float]], width=420, height=None):
    """t 值条形图，带 ±2 显著线。"""
    if not rows:
        return ""
    height = height or max(60, 26 * len(rows) + 20)
    finite = [abs(v) for _, v in rows if v == v]          # 滤掉 NaN
    mx = max(2.6, (max(finite) if finite else 2.6) * 1.15)
    mid, sc = width / 2, width / 2 / mx
    parts = [f'<svg viewBox="0 0 {width+150} {height}" width="100%" '
             f'style="max-width:{width+150}px">']
    for sgn in (-1, 1):                                    # ±2 显著性参考线
        x = mid + sgn * 2 * sc
        parts.append(f'<line x1="{x:.1f}" y1="0" x2="{x:.1f}" y2="{height-14}" '
                     f'stroke="{MUTED}" stroke-width="1" stroke-dasharray="4 3"/>')
    parts.append(f'<line x1="{mid}" y1="0" x2="{mid}" y2="{height-14}" '
                 f'stroke="{BORDER}" stroke-width="1"/>')
    for i, (label, v) in enumerate(rows):
        y = 6 + i * 26
        if v != v:
            continue
        w = abs(v) * sc
        x = mid if v >= 0 else mid - w
        color = UP if abs(v) >= 2 and v > 0 else DOWN if abs(v) >= 2 else MUTED
        op = 0.9 if abs(v) >= 2 else 0.45
        parts.append(f'<rect x="{x:.1f}" y="{y}" width="{w:.1f}" height="15" '
                     f'rx="3" fill="{color}" opacity="{op}"/>')
        tx = mid + w + 6 if v >= 0 else mid - w - 6
        anchor = "start" if v >= 0 else "end"
        parts.append(f'<text x="{tx:.1f}" y="{y+12}" font-size="11.5" fill="{TEXT}" '
                     f'text-anchor="{anchor}">{v:+.2f}</text>')
        parts.append(f'<text x="{width+12}" y="{y+12}" font-size="12" fill="{TEXT}">'
                     f'{html.escape(label)}</text>')
    parts.append(f'<text x="{mid+2*sc:.1f}" y="{height-3}" font-size="10.5" '
                 f'fill="{MUTED}">+2</text>')
    parts.append(f'<text x="{mid-2*sc:.1f}" y="{height-3}" font-size="10.5" '
                 f'fill="{MUTED}" text-anchor="end">−2</text>')
    parts.append("</svg>")
    return "".join(parts)


def _table(df: pd.DataFrame, cols: list[str], fmt: dict | None = None) -> str:
    fmt = fmt or {}
    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    body = []
    for _, r in df.iterrows():
        tds = []
        for c in cols:
            v = r.get(c)
            if c in fmt:
                tds.append(f"<td>{fmt[c](v)}</td>")
            elif isinstance(v, float):
                tds.append(f"<td>{_fmt(v)}</td>")
            else:
                tds.append(f"<td>{html.escape(str(v))}</td>")
        body.append("<tr>" + "".join(tds) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


# ============================ 因子检验报告 ============================
def render_factor_report(res: dict, out_path: str,
                         sd: str = "", ed: str = "") -> dict:
    """渲染四池 IC 对照报告，返回自动结论文案。"""
    summ = res["summary"]
    yearly = res.get("yearly", pd.DataFrame())
    lay = res.get("layered", pd.DataFrame())
    detail = res.get("detail", {})

    # —— 自动结论：基线 + 目标池是否显著 ——
    def get(pool, factor, col="t"):
        s = summ[(summ.池 == pool) & (summ.因子 == factor)]
        return float(s.iloc[0][col]) if len(s) else np.nan

    base_t = get("沪深300", "最终打分")
    alla_t = get("全A非科创", "最终打分")
    kcb_t = get("全A含科创", "最终打分")

    if base_t != base_t:
        verdict, vcls = "基线池缺数据，无法判定。", ""
    elif abs(base_t) < 2:
        verdict = (f"⚠️ <b>基线本身已不显著</b>（沪深300 合成分 t={base_t:+.2f}，|t|&lt;2）。"
                   f"此时讨论「扩池会不会变差」没有意义 —— 先确认现有池子的 α 是否还在。")
        vcls = "bad"
    elif alla_t == alla_t and abs(alla_t) >= 2 and alla_t > 0:
        verdict = (f"✅ 扩到全A 后合成分仍显著（t={alla_t:+.2f}）。"
                   f"扩池可行，可进入 jk002 的数据改造。")
        vcls = "ok"
    else:
        verdict = (f"❌ <b>扩池后 α 消失</b>：沪深300 t={base_t:+.2f}（显著）→ "
                   f"全A非科创 t={alla_t:+.2f}、全A含科创 t={kcb_t:+.2f}（均不显著）。"
                   f"与中证500 的前车之鉴一致 —— 池子越大，截面噪声越多，"
                   f"同一因子的区分度被稀释。建议先加重门槛（日均成交额、市值）收缩到大中盘重测。")
        vcls = "bad"

    # 各因子 × 池子的 t 值条形
    bars = []
    for _, r in summ[summ.因子 == "最终打分"].iterrows():
        bars.append((f"{r['池']} · 合成分", float(r["t"])))
    for _, r in summ[summ.因子 == "FCF/OE"].iterrows():
        bars.append((f"{r['池']} · FCF/OE", float(r["t"])))

    sum_show = summ.copy()
    tcols = ["池", "因子", "ic", "ir", "t", "win", "n"]
    sum_show = sum_show[[c for c in tcols if c in sum_show.columns]]

    yearly_html = ""
    if not yearly.empty:
        for fname in ("FCF/OE", "低波动", "最终打分"):
            sub = yearly[(yearly.因子 == fname) & (yearly.n >= 3)]
            if sub.empty:
                continue
            pv = sub.pivot_table(index="年", columns="池", values="t")
            order = [p for p in ["沪深300", "中证500", "全A非科创", "全A含科创"]
                     if p in pv.columns]
            rows = []
            for y in sorted(pv.index):
                rec = {"年": y}
                for p in order:
                    rec[p] = pv.loc[y, p]
                rows.append(rec)
            ydf = pd.DataFrame(rows)
            yearly_html += (f'<h2>{html.escape(fname)} · 年度 t 值</h2><div class="card">'
                            + _table(ydf, list(ydf.columns),
                                     {c: _tcell for c in ydf.columns if c != "年"})
                            + '<p class="note">t 值按年看，用于判断「因子是否只在某一段有效」。'
                              '灰显表示该年不显著。</p></div>')

    lay_html = ""
    if not lay.empty:
        lay_html = ('<h2>分层单调性 · 等权分 5 组的月均收益</h2><div class="card">'
                    + _table(lay, list(lay.columns),
                             {"月均收益": lambda v: _fmt(v, 4, pct=True)})
                    + '<p class="note">G1=因子值最低，G5=最高。「多空 G5−G1」为正说明因子方向正确；'
                      '理想情况是 G1→G5 单调递增。</p></div>')

    n_pairs = len(detail)
    doc = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>全A 扩池 · 因子有效性检验</title><style>{_CSS}</style></head><body><div class="wrap">
<h1>全A 扩池 · 因子有效性检验</h1>
<div class="sub">
 <span class="pill">区间 {html.escape(sd) or "—"} ~ {html.escape(ed) or "—"}</span>
 <span class="pill">月频截面 Spearman IC</span>
 <span class="pill">复权口径 hfq 后复权</span>
 <span class="pill">{n_pairs} 组池×因子</span>
</div>

<div class="verdict {vcls}">{verdict}</div>

<h2>核心结论 · t 值显著性</h2>
<div class="card">{_tbar(bars)}
<p class="note">虚线为 ±2 显著性参考线。<b>|t| &lt; 2 时只能写「无法判别」，不得宣称因子有效</b>
—— 灰显条形即属此列。</p></div>

<h2>四池 IC 对照</h2>
<div class="card">{_table(sum_show, list(sum_show.columns),
    {"ic": lambda v: _fmt(v), "ir": lambda v: _fmt(v, 3),
     "t": _tcell, "win": lambda v: _fmt(v, 4, pct=True, signed=False),
     "n": lambda v: f'<span class="mut">{int(v)}</span>'})}
<p class="note">IC 均值反映方向与强度；t 反映稳定性。IC 为正但 t&lt;2 的组合不能用作选股依据。</p></div>

{yearly_html}
{lay_html}

<h2>口径说明</h2>
<div class="card note">
① <b>四池共用同一份价格数据</b>，只按成分快照过滤，确保差异可归因到池子本身。<br>
② <b>主口径锁定四池共同区间</b>（2022-01 起）：沪深300/中证500 成分快照从 2021-11 才有，
而全A 能追到 2019，混入会带来市场环境干扰。<br>
③ <b>收益标签用后复权价计算</b>，不用 pct_chg —— 后者基于未复权前收盘，除权日会出现
人为跳变（实测最极端 −82%），且各板块分红率不同会引入方向不同的系统性偏误。<br>
④ <b>合成分在池内标准化</b>，与策略实际打分口径一致。<br>
⑤ 全A 成分由 list_date / delist_date 逐月重建（含已退市股），避免幸存者偏差。
</div>
</div></body></html>"""

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return dict(path=out_path, verdict=verdict, base_t=base_t, alla_t=alla_t)
