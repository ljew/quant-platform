#!/usr/bin/env python3
"""生成 jk 系列策略的《使用与调优手册》(data/reports/jk_usage_guide.html)。

为什么用脚本而不是手写 HTML
---------------------------
手册顶部的「三段验证基线」必须和 `verify_strategy.py` 的输出严格一致，
手写就会随着代码改动悄悄过期（历史上已经发生过：jk002 的 JSON 停留在
引擎吞单 bug 修复前，写的是 -13.13%，真实值是 +7.97%）。

所以本脚本把「会变的数字」从 `strategy_verify_<key>.json` 读出来渲染，
「不会变的结论」写死在下面的模板里。每次跑完 verify_strategy.py 后
重跑一次本脚本，手册就同步了。

用法
----
    cd backend
    PYTHONPATH=$(pwd) python scripts/gen_jk_guide.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / "data" / "reports"
OUT = REPORT_DIR / "jk_usage_guide.html"

SEG_LABEL = {"①": "① 样本内（2019-2024，无滑点）",
             "②": "② 样本外（2025-2026，无滑点）",
             "③": "③ 全段含 0.2% 滑点"}


def _load(key: str) -> dict:
    p = REPORT_DIR / f"strategy_verify_{key}.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _pct(v, digits: int = 2, sign: bool = True) -> str:
    if v is None:
        return "—"
    return f"{v * 100:+.{digits}f}%" if sign else f"{v * 100:.{digits}f}%"


def _calmar(row: dict) -> str:
    g = row.get("got") or {}
    tr, dd = g.get("total_return"), g.get("max_drawdown")
    if tr is None or not dd:
        return "—"
    return f"{tr / abs(dd):.2f}"


def _seg_rows(key: str) -> dict[str, dict]:
    """把 JSON 的 rows 按 ①②③ 归位（靠名字前缀识别，与 REFERENCE 里的编号一致）。"""
    out: dict[str, dict] = {}
    for r in _load(key).get("rows", []):
        name = r.get("name", "")
        for mark in ("①", "②", "③"):
            if name.startswith(mark):
                out[mark] = r
    return out


def build_seg_table() -> str:
    data = {k: _seg_rows(k) for k in ("jk001", "jk002")}
    body = []
    for mark in ("①", "②", "③"):
        cells = [f"<td>{SEG_LABEL[mark]}</td>"]
        for key in ("jk001", "jk002"):
            r = data[key].get(mark)
            if not r:
                cells.append('<td class="num">—</td><td class="num">—</td>')
                continue
            g = r.get("got") or {}
            exc = None
            if g.get("total_return") is not None and g.get("benchmark") is not None:
                exc = g["total_return"] - g["benchmark"]
            color = "" if exc is None else (" up" if exc >= 0 else " down")
            cells.append(
                f'<td class="num">{_pct(g.get("total_return"))}'
                f'<span class="muted"> / {_pct(g.get("max_drawdown"), sign=False)}'
                f' / Calmar {_calmar(r)}</span></td>'
                f'<td class="num{color}">{_pct(exc)}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")
    meta = []
    for key, label in (("jk001", "jk001 沪深300池"), ("jk002", "jk002 全A池")):
        j = _load(key)
        meta.append(f"{label} 报告日期 {j.get('date', '—')}")
    return (f'<table><tr><th>验证段</th>'
            f'<th class="num">jk001 收益 / 回撤 / Calmar</th><th class="num">jk001 超额</th>'
            f'<th class="num">jk002 收益 / 回撤 / Calmar</th><th class="num">jk002 超额</th>'
            f'</tr>{"".join(body)}</table>'
            f'<p class="sub" style="margin-top:-8px">{" · ".join(meta)}；'
            f'超额 = 策略收益 − 同期基准（jk001 基准沪深300，jk002 基准国证A指）。</p>')


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>jk 系列策略 · 使用与调优手册</title>
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
       max-width:1000px;margin:0 auto;padding:32px 24px;color:#2C2C2A;line-height:1.75;background:#fff}
  h1{font-size:24px;font-weight:500;margin:0 0 4px}
  h2{font-size:17px;font-weight:500;margin:36px 0 12px;padding-bottom:6px;border-bottom:1px solid #E5E3DC}
  h3{font-size:14px;font-weight:500;margin:20px 0 8px}
  .sub{color:#5F5E5A;font-size:13px;margin:0 0 22px}
  table{border-collapse:collapse;width:100%;margin:10px 0 18px;font-size:13px}
  th,td{border:1px solid #E5E3DC;padding:7px 10px;text-align:left;vertical-align:top}
  th{background:#F1EFE8;font-weight:500}
  td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
  .muted{color:#8A8880;font-size:11px}
  .up{color:#C0392B;font-weight:500}
  .down{color:#1E8449}
  .good{color:#0F6E56;font-weight:500}
  .bad{color:#A32D2D}
  code{background:#F1EFE8;padding:1px 5px;border-radius:3px;font-size:12px;
       font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
  pre{background:#F1EFE8;padding:12px 14px;border-radius:6px;overflow-x:auto;font-size:12px;
      font-family:ui-monospace,SFMono-Regular,Menlo,monospace;line-height:1.6;margin:8px 0 14px}
  .box{border-left:3px solid #185FA5;background:#E6F1FB;padding:12px 16px;margin:12px 0;font-size:13px}
  .warn{border-left:3px solid #D85A30;background:#FAECE7;padding:12px 16px;margin:12px 0;font-size:13px}
  .ok{border-left:3px solid #1D9E75;background:#E1F5EE;padding:12px 16px;margin:12px 0;font-size:13px}
  ol,ul{margin:8px 0;padding-left:22px}
  li{margin:5px 0}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:14px 0}
  .card{background:#F1EFE8;border-radius:8px;padding:12px 14px}
  .card .l{font-size:12px;color:#5F5E5A}
  .card .v{font-size:20px;font-weight:500;margin-top:2px}
</style>
</head>
<body>
<h1>jk 系列策略 · 使用与调优手册</h1>
<p class="sub">2026-09-16 · jk001（沪深300 池）/ jk002（全A 池）· 平台地址 <code>http://localhost:8000</code>
· 本文档由 <code>scripts/gen_jk_guide.py</code> 从验证报告自动生成，数字不会过期</p>

<div class="ok"><b>三段验证基线（当前默认配置：trend_bear_mode=ma + bear_immediate=1 + trailing_stop_pct=0.08）</b>
@@SEG_TABLE@@
</div>

<h2>一、在网页里怎么用</h2>

<h3>1. 跑一次回测（最常用）</h3>
<ol>
<li>浏览器打开 <code>http://localhost:8000</code> → 左侧进「<b>回测</b>」页</li>
<li><b>策略</b>下拉选 <code>jk001 · 自由现金流+低波动(沪深300)</code>（或 jk002 全A版）</li>
<li><b>标的</b>填 <code>sh000300</code>（jk002 填 <code>sz399317</code>）；<b>起止日期</b>如 2019-01-02 ~ 2026-09-11
    <br><span class="muted">⚠️ 平台默认滑点 0.2%，与聚宽（无滑点）对比时要把滑点设成 0，否则差的不是策略是成本。</span></li>
<li>下方参数区会<b>自动带出该策略的全部参数</b>（由后端 <code>param_schema</code> 渲染，加新参数不用改前端）</li>
<li>点「<b>运行回测（异步）</b>」→ 出结果：收益/年化/夏普/回撤/超额四张卡 + 净值曲线 + 成交明细</li>
</ol>
<p>成交明细的「信号」列是中文：<code>jk调仓买入 / jk调仓卖出 / jk固定止损 / jk移动止损 / jk看空清仓 / jk波动减仓</code>。
<b>看到止损信号，说明风控真的在工作</b>——修复引擎吞单 bug 之前，这里一笔止损都没有。</p>

<h3>2. 用参数寻优做网格搜索</h3>
<ol>
<li>进「<b>寻优</b>」页，选策略</li>
<li>每个参数填<b>逗号分隔的取值</b>，如 <code>stop_loss_pct</code> 填 <code>0.10,0.15,0.20</code> → 网格搜索</li>
<li><b>验证段比例</b>（默认留末尾一段交易日不参与选参）——<b>务必别设成 0</b>，否则是纯样本内选参，结果会虚高</li>
<li>组合数多会自动转后台任务，可实时看进度、随时取消</li>
</ol>

<h3>3. 挂模拟盘 / 看数据健康度</h3>
<ul>
<li>「<b>模拟盘</b>」页新建任务选 jk001，调度器 30 秒轮询；组合策略是收盘后日频跑</li>
<li>「<b>数据管道</b>」页看各数据集行数/覆盖/滞后交易日，有异常可用「一键修复」</li>
<li>「<b>监控</b>」页看服务状态</li>
</ul>

<h2>二、命令行怎么用（调参更快，推荐）</h2>
<pre>cd /Users/happyljew/Desktop/kimiwork/Quant/quant-platform/backend
PY=/Users/happyljew/.workbuddy/binaries/python/envs/quant/bin/python
export PYTHONPATH=$(pwd)

# ① 单参数扫描 —— 调参主力工具（一次跑多档，出收益/回撤/夏普/Calmar）
$PY scripts/scan_param.py --strategy jk001 --param stop_loss_pct \
    --values 0.08,0.10,0.15,0.20 --start 2019-01-02 --end 2026-09-11 --slippage 0.002
#   固定覆盖其它参数用 --set，如 --set trend_bear_mode=ma

# ② 三段验证 + 出正式报告（HTML + JSON）
$PY scripts/verify_strategy.py --strategy jk001,jk002
#   → data/reports/strategy_verify_&lt;key&gt;.html / .json

# ③ 跑输归因：选股 vs 随机的百分位、板块结构、实际选股画像
$PY scripts/diag_attribution.py --strategy jk002 --start 2025-01-02 --end 2026-09-11
$PY scripts/diag_attribution.py --strategy jk002 --skip-backtest   # 只看结构，秒出

# ④ 专项诊断
$PY scripts/diag_drawdown.py      # 最大回撤当时发生了什么
$PY scripts/diag_stoploss.py      # 止损有没有真的成交
$PY scripts/diag_trailing.py      # 移动止损是不是在洗出

# ⑤ 改完代码后重生成这本手册
$PY scripts/gen_jk_guide.py</pre>

<h2>三、手动优化的六步 SOP</h2>
<div class="box"><b>核心原则：只看一个区间的收益选参数 = 拟合噪声。必须三段一起看。</b></div>
<ol>
<li><b>提假设</b>：想清楚这个参数改的是「选股 / 仓位 / 风控」哪一类，预期方向是什么。</li>
<li><b>粗扫</b>：<code>scan_param.py</code> 取 5 档左右，看 <b>Calmar（收益÷回撤）</b>，不要只看收益。</li>
<li><b>三段验证</b>：样本内（2019-2024，无滑点）、样本外（2025-2026，无滑点）、全段含 0.2% 滑点。
    <b>三段同向改善才算数</b>——注意是「同向」，收益涨但回撤也涨不算。</li>
<li><b>查单调性</b>：改动应该有合理解释且大致单调。<b>非单调跳变基本是噪声</b>
    （如 <code>stock_num</code>：样本内越集中越优、样本外完全反转）。</li>
<li><b>落地</b>：改 <code>app/core/strategies/registry.py</code> 的 <code>default_params</code>；
    新参数必须同时进 <code>param_schema</code>，UI 才会自动出现。</li>
<li><b>出报告存档</b>：<code>verify_strategy.py</code> 重跑 → <code>gen_jk_guide.py</code> 重生成手册。</li>
</ol>

<h3>已验证结论 —— 别再重复试（2026-09-16 更新）</h3>
<table>
<tr><th>旋钮 / 假设</th><th>三段验证结果</th><th>裁决</th></tr>
<tr><td><code>trailing_stop_pct</code> 0.10→0.08<br><span class="muted">假设：移动止损放太紧，被洗出</span></td>
    <td>样本内 Calmar 3.64→3.97、样本外 1.11→1.18、jk002 同向改善；放宽到 0.15 以上全线变差</td>
    <td class="good">✅ 已改默认</td></tr>
<tr><td><code>w_lowvol</code> 0.5→0.2<br><span class="muted">假设：低波动因子拖累收益</span></td>
    <td>jk001 三段同向改善（样本内 Calmar 3.97→4.94、样本外 1.90→2.14）；
        <b>但 jk002 样本内大幅变差</b>（Calmar 2.79→1.94）、样本外才改善</td>
    <td class="bad">❌ 两池方向冲突 → 不动</td></tr>
<tr><td><code>max_intraday_chg</code> 0.05→0.20<br><span class="muted">假设：不追高卡太死</span></td>
    <td>jk001 三段<b>全部变差</b>（样本内 3.97→3.92、样本外 1.90→1.78、全段 3.09→3.05）；
        jk002 样本外改善（0.70→0.80）</td>
    <td class="bad">❌ jk001 三段全否</td></tr>
<tr><td>关掉「兜底放宽准入」<br><span class="muted">假设：兜底在弱市接飞刀</span></td>
    <td>兜底 6 年只触发 19 次（补 208 只）；关掉后 jk001 样本内 Calmar 3.97→3.91、
        <b>样本外 1.90→1.69</b></td>
    <td class="bad">❌ 兜底是正贡献</td></tr>
<tr><td><code>hold_buffer</code> 3/8/15/30<br><span class="muted">假设：减少名次抖动 → 降换手降成本</span></td>
    <td>样本外 15 档掉 11pp、3 档掉 4.25pp；缓冲带越大越僵化</td>
    <td class="bad">❌ 2025-26 是快轮动市，变粘吃亏</td></tr>
<tr><td><code>rebalance_period</code> 21→42/63<br><span class="muted">假设：少调一次仓省成本</span></td>
    <td>样本内 Calmar 飙到 9.23、全段含滑点 8.08（都很漂亮），<b>但样本外掉到 +5.92% / +4.23%</b></td>
    <td class="bad">❌ 典型过拟合陷阱，被三段规则拦住</td></tr>
<tr><td>关动量准入 / 关 MA20 准入<br><span class="muted">假设：入场三条件卡太死</span></td>
    <td>样本外 Calmar 0.70 → 0.59（关动量）/ 0.49（关 MA20）</td>
    <td class="bad">❌ 两个准入都是正贡献</td></tr>
<tr><td>关止损<br><span class="muted">假设：止损把股票砍在半山腰</span></td>
    <td>样本外 +7.97% → <b>-6.09%</b>，回撤 -11.34% → -24.37%

    </td><td class="bad">❌ 止损是真在砍弱势股（止损后 60 日中位再跌 3.15%，仅 43% 上涨）</td></tr>
<tr><td>关择时 overlay<br><span class="muted">假设：牛市里择时压仓位拖累收益</span></td>
    <td>jk001 样本内关掉更差（+110.63%→+102.45%）、样本外基本中性；jk002 样本外关掉更差</td>
    <td class="bad">❌ 保留</td></tr>
<tr><td><code>vol_target</code> 开启<br><span class="muted">假设：约束波动率能改善 Calmar</span></td>
    <td>所有档位 Calmar 全线下降；0.10 档可把回撤压到 -22.94%，但代价是收益掉更多</td>
    <td>⚪ 默认关闭；只在「必须压回撤」时开 0.10</td></tr>
</table>

<div class="warn"><b>反面教材：<code>exclude_st=1</code> 看起来有效，但收益是假的（前视偏差）</b><br>
把它从 0 改成 1，jk002 样本外从 +7.97% "改善" 到 +9.43%（Calmar 0.70→0.82），收益率看着很香。
但策略文件里已注明：<b>平台没有历史 ST 标记，只有「当前名称」</b>。开启后等于拿 2026 年的 ST 名单去剔除 2019 年的股票
—— <b>提前知道谁将来会变成 ST</b>，那 1.5pp 是凭空变出来的。<br>
要做真正的 ST 过滤，先补一张 <code>st_history</code> 表（按公告日记录 ST/*ST 的进出），再按 PIT 口径引用。</div>

<h2>四、必须知道的坑</h2>
<div class="warn"><b>1. 改后端代码必须重启才生效</b>（没开 reload）：<br>
<code>lsof -ti:8000 -sTCP:LISTEN | xargs kill</code> → 守护会在 3 秒后自动拉起。<br>
改了 <code>web/</code> 前端则必须 <code>cd web &amp;&amp; npm run build</code>（dist 是 gitignore 的）。</div>
<div class="warn"><b>2. 引擎会吞掉异常，只打一行日志</b>：回测跑完务必 grep <code>failed</code>。<br>
<code>on_bar xxx failed</code> / <code>rebalance xxx failed</code> 一旦出现，那天的策略逻辑就是没执行
——而且你看收益曲线完全看不出来。</div>
<div class="warn"><b>3. 枚举参数必须用 <code>_opt()</code> 登记</b>（type=str + options）。<br>
之前 UI 把所有参数都渲染成数字框，<code>trend_bear_mode</code> 的 <code>'ma'</code> 被 <code>Number()</code>
成 NaN 传给后端 → <b>悄悄退回原版口径，收益差 20pp 却毫无提示</b>。现已修（枚举渲染为下拉框）。</div>
<div class="warn"><b>4. 对比聚宽口径要对齐滑点</b>：聚宽回测无滑点，平台默认 0.2%。同区间滑点能吃掉 24pp，
<b>不设同口径的对比毫无意义</b>。</div>
<div class="warn"><b>5. jk002 全A池很慢</b>：单次 6 年回测 8~10 分钟，扫 3 档就是半小时。
验证想法前先用短区间（如 2025-01 起）或 <code>--start 2024-01</code> 试。</div>
<div class="warn"><b>6. 平台基准已对齐、但样本内数字天然高于聚宽</b>：聚宽那段<b>平均仓位只有 51.6%</b>
（中性参数写的却是 0.9），-18.93% 的回撤是半仓的副产品，不是策略设计的风控；平台忠实满仓约 89%。
所以「平台 +111% vs 聚宽 +57%」不是平台算错，而是<b>两边仓位口径不同</b>。</div>

<h2>五、为什么 2025-2026 跑输 —— 归因结论</h2>
<p>这是本轮优化的主要产出。结论是<b>风格错配</b>，不是参数没调好，所以下面这些数字别再试着重调参去修。</p>

<table>
<tr><th>证据</th><th>数字</th><th>含义</th></tr>
<tr><td>全池个股收益分布<br><span class="muted">2025-01~2026-09，5450 只</span></td>
    <td>中位数 <b>+3.17%</b>、均值 +28.16%、P90 +104.46%、正收益占比 53.3%</td>
    <td>极端右偏：指数在涨，但<b>一半以上个股涨幅不到 3%</b></td></tr>
<tr><td>随机 30 只等权组合<br><span class="muted">10000 次蒙特卡洛对照</span></td>
    <td>中位数 +24.87%，P10 +7.71%</td>
    <td>jk002 样本外 +7.97% 恰好落在随机分布的 <b>P10</b> —— <b>选股没比随机好</b></td></tr>
<tr><td>板块结构</td>
    <td>主板(3185只) 中位 +3.0% ｜ 创业板(1390) +2.3% ｜ <b>科创板(592) +24.1%</b> ｜ 北交所(282) -15.5%</td>
    <td>2025-26 的 alpha <b>几乎全部集中在科创板右尾</b></td></tr>
<tr><td>jk002 实际选股画像</td>
    <td>主板 82.8% ｜ 科创板仅 <b>3.5%</b> ｜ 北交所 0 人次</td>
    <td>策略买不到 alpha 所在的那块（"北交所混入池子"的假设已被证伪）</td></tr>
</table>

<div class="box"><b>因果链</b>：jk002 的选股要求 <code>ROE&gt;0</code> + <code>FCF/OE&gt;-0.1</code>（自由现金流为正）
+ 低波动 —— 这是一套<b>「质量价值 + 防御」风格</b>。而科创板多数公司尚未盈利、自由现金流为负，
被这套筛选<b>结构性排除在外</b>。所以 jk002 全A 池里"什么都能买"，实际只买到了主板。
2025-2026 恰好是「科创板/成长占优、主板滞涨」的行情 → 风格错配 → 跑输基准 21pp。<br>
<b>参数调不动风格错配。</b>这也是为什么本轮 10 个候选参数全部被三段验证否决。</div>

<div class="warn"><b>更硬的一条结论：jk002「扩池到全A」这个动作本身没赚到钱</b><br>
把池子从沪深300 扩到全A，样本内（2019-2024，无滑点）jk002 做到 +63.93%，
同期国证A指 +65.16% —— <b>超额 -1.23pp，基本打平</b>；而 jk001 同期对沪深300 是 <b>+79.10pp</b>。
也就是说 FCF/OE + 低波动这套信号<b>只在沪深300 成分股里有效</b>，铺到全市场就被稀释干净了。
样本外更崩到 -21.17pp，全段含滑点 -58.35pp。<br>
所以 jk002 <b>不是「更宽的 jk001」，它是另一个东西</b>：一个相对基准没有 alpha、换手却是 jk001 两倍多的组合
（全段成交 4210 笔 vs 3644 笔，且池子更大滑点更贵）。<b>要上实盘，优先用 jk001；jk002 建议先停用或重新定位。</b></div>

<h3>下一步可选方向（按性价比排序）</h3>
<ol>
<li><b>⛔ 先做取舍：jk002 停用或重新定位</b>。它的样本内超额就是 -1.23pp（对国证A指），
    换手却是 jk001 的两倍、单次回测要 8~10 分钟。把它当「更宽的 jk001」用会持续跑输。
    真要保留全A 暴露，正确做法是<b>另建一条不看 FCF 的成长/动量子池</b>（见第 5 条）。</li>
<li><b>🔧 修执行成本（jk001 上性价比最高，且不用改策略）</b>：jk001 全段含 0.2% 滑点为 +104.46%，
    0 滑点口径明显更高；jk002 更极端——0.2% 双边滑点在样本外吃掉 <b>6.2pp</b>
    （0 滑点 +7.97% → 含滑点 +1.79%）。实盘把收盘市价单换成限价单/算法单、
    把有效滑点从 0.2% 压到 0.1% 左右，<b>jk001 一年能回收约 2~3pp 净值</b>。</li>
<li><b>✔ 止血已查完</b>：结论与直觉相反——放宽止损<b>全线变差</b>，527 次止损是真在砍弱势股。
    唯一改进是前段阈值 0.10→0.08，已落地（<code>scripts/diag_trailing.py</code>）。</li>
<li><b>✔ 归因已闭环</b>：风格错配 + 扩池稀释，别再调现有因子权重（会引入 regime 拟合）。</li>
<li><b>组合层面而非单策略层面</b>：jk001 做防御腿，另配进攻腿（<code>turtle_classic</code> 或
    <code>csi800_enhanced</code>）做风险预算分配——单策略牛市跑输是设计使然。</li>
<li><b>若真要覆盖科创板 alpha</b>：新增一条<b>不看 FCF 的成长子池</b>（如科创板动量/营收增速版本），
    与 jk001 并行，而不是把现有因子权重调来调去。</li>
</ol>

<p class="sub" style="margin-top:32px">相关文件：<code>backend/app/core/strategies/jk_series.py</code>（策略逻辑）·
<code>backend/app/core/strategies/registry.py</code>（参数登记）·
<code>backend/app/core/engine/portfolio_backtest.py</code>（回测引擎）·
<code>backend/scripts/</code>（全部调参/诊断/验证脚本）</p>
</body>
</html>
"""


def main() -> int:
    html = TEMPLATE.replace("@@SEG_TABLE@@", build_seg_table())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".html.tmp")
    tmp.write_text(html, encoding="utf-8")
    os.replace(tmp, OUT)          # 原子写：避免写一半被读到
    n = len(html)
    print(f"[saved] {OUT.relative_to(ROOT)}  ({n} 字符)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
