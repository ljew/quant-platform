/**
 * 信号组合（原「信号中心」精简版）：镜像组合（跟随信号自动模拟）+ 实验组合
 * （fresh_ma 规则 + regime 门控）+ 各策略命中明细。
 * 每日买卖动作已并入「今日观点」首页，本页回答「跟着信号走，长期效果如何」。
 */
import { useEffect, useState } from "react";
import { Card, PageHeader } from "../components/ui";
import EChart from "../components/EChart";
import { get, signalApi, PortfolioSnapshot } from "../api/client";
import { useTheme } from "../theme";

export default function SignalPage() {
  const { colors } = useTheme();
  const [date, setDate] = useState("");
  const [dates, setDates] = useState<{ date: string; symbols: number }[]>([]);
  const [picks, setPicks] = useState<{ strategy: string; items: { symbol: string; name: string | null }[] }[]>([]);
  const [pf, setPf] = useState<PortfolioSnapshot | null>(null);
  const [pfExp, setPfExp] = useState<PortfolioSnapshot | null>(null);

  useEffect(() => {
    signalApi.portfolio().then((r) => { if (r.ok) setPf(r); }).catch(() => {});
    signalApi.portfolioExp().then((r) => { if (r.ok) setPfExp(r); }).catch(() => {});
  }, []);

  useEffect(() => {
    get<{ date: string; symbols: number }[]>("/signal/dates").then((d) => {
      setDates(d);
      if (d[0]) setDate(d[0].date);
    }).catch(() => {});
  }, []);

  useEffect(() => {
    if (!date) return;
    get<{ groups: { strategy: string; items: { symbol: string; name: string | null }[] }[] }>(
      `/signal/picks?date=${date}`).then((d) => setPicks(d.groups)).catch(() => {});
  }, [date]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <PageHeader
        title="信号组合"
        desc="镜像组合跟随 BUY_STRONG 信号自动模拟；实验组合（fresh_ma+门控）为 2026-10-14 重新优化的第二条 paper sleeve，与生产信号管道隔离"
        actions={
          <select value={date} onChange={(e) => setDate(e.target.value)}
            style={{ padding: "6px 10px", borderRadius: 8, border: `1px solid ${colors.border}`, background: colors.card, color: colors.text }}>
            {dates.map((d) => <option key={d.date} value={d.date}>{d.date}（{d.symbols} 只）</option>)}
          </select>
        }
      />

      {/* —— 镜像组合（真实成本模拟）—— */}
      {pf && (
        <Card title={`镜像组合 · 按每日信号自动模拟（截至 ${pf.as_of}）`}
          colors={colors}>
          <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginBottom: 10 }}>
            {[
              { k: "净值", v: `${pf.summary.total.toLocaleString()}` },
              { k: "收益率", v: `${pf.summary.return_pct > 0 ? "+" : ""}${pf.summary.return_pct}%`,
                color: pf.summary.return_pct >= 0 ? "#c0392b" : "#1e8449" },
              { k: "最大回撤", v: `${pf.summary.max_drawdown_pct}%` },
              { k: "成交笔数", v: `${pf.summary.n_trades}` },
              { k: "当前持仓", v: `${pf.summary.n_holdings} 只` },
            ].map((x) => (
              <div key={x.k} style={{ minWidth: 110 }}>
                <div style={{ fontSize: 11, color: colors.muted }}>{x.k}</div>
                <div style={{ fontSize: 17, fontWeight: 700, color: x.color || colors.text }}>{x.v}</div>
              </div>
            ))}
          </div>
          <EChart height={220} option={{
            tooltip: { trigger: "axis" },
            grid: { left: 70, right: 20, top: 20, bottom: 30 },
            xAxis: { type: "category", data: pf.equity.map((e) => e.date.slice(2)),
              axisLabel: { fontSize: 10, interval: Math.ceil(pf.equity.length / 8) } },
            yAxis: [{ type: "value", scale: true, axisLabel: { fontSize: 10,
              formatter: (v: number) => `${(v / 10000).toFixed(0)}万` } }],
            series: [{ name: "组合净值", type: "line", data: pf.equity.map((e) => e.total),
              showSymbol: false, lineStyle: { width: 1.6, color: "#c0392b" },
              areaStyle: { opacity: 0.08 } }],
          } as never} />
          {pf.holdings.length > 0 && (
            <div style={{ fontSize: 12, color: colors.text, marginTop: 6 }}>
              <b>当前持仓：</b>
              {pf.holdings.map((h) => `${h.symbol}(${h.shares}股@${h.entry_price})`).join("、")}
            </div>
          )}
        </Card>
      )}

      {/* —— 实验组合（fresh_ma + regime 门控，2026-10-14 重新优化）—— */}
      {pfExp && (
        <Card title={
          <span>实验组合 · fresh_ma 规则 + regime 门控（截至 {pfExp.as_of}）
            <span style={{ fontSize: 11, fontWeight: 400, color: colors.muted, marginLeft: 8 }}>
              候选 = enabled 策略新鲜信号（连续≤2天）且收盘处于回调区（close&lt;ma20）；
              门控 = 候选池近 90 日 T+10 超额 &gt; 0 才开新仓；T+1 开盘建仓 / 持有 10 个交易日
            </span>
          </span>
        } colors={colors}>
          <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginBottom: 10 }}>
            {[
              { k: "净值", v: `${pfExp.summary.total.toLocaleString()}` },
              { k: "收益率", v: `${pfExp.summary.return_pct > 0 ? "+" : ""}${pfExp.summary.return_pct}%`,
                color: pfExp.summary.return_pct >= 0 ? "#c0392b" : "#1e8449" },
              { k: "最大回撤", v: `${pfExp.summary.max_drawdown_pct}%` },
              { k: "成交笔数", v: `${pfExp.summary.n_trades}` },
              { k: "当前持仓", v: `${pfExp.summary.n_holdings} 只` },
              { k: "门控开启", v: `${pfExp.summary.gate_open_days ?? 0} / ${pfExp.summary.days} 天` },
              { k: "门控处超额", v: pfExp.summary.gate_exc_now == null ? "n/a"
                : `${(pfExp.summary.gate_exc_now * 100).toFixed(2)}%`,
                color: (pfExp.summary.gate_exc_now ?? 0) >= 0 ? "#c0392b" : "#1e8449" },
            ].map((x) => (
              <div key={x.k} style={{ minWidth: 100 }}>
                <div style={{ fontSize: 11, color: colors.muted }}>{x.k}</div>
                <div style={{ fontSize: 17, fontWeight: 700, color: x.color || colors.text }}>{x.v}</div>
              </div>
            ))}
          </div>
          <EChart height={220} option={{
            tooltip: { trigger: "axis" },
            grid: { left: 70, right: 20, top: 20, bottom: 30 },
            xAxis: { type: "category", data: pfExp.equity.map((e) => e.date.slice(2)),
              axisLabel: { fontSize: 10, interval: Math.ceil(pfExp.equity.length / 8) } },
            yAxis: [{ type: "value", scale: true, axisLabel: { fontSize: 10,
              formatter: (v: number) => `${(v / 10000).toFixed(0)}万` } }],
            series: [{ name: "组合净值", type: "line", data: pfExp.equity.map((e) => e.total),
              showSymbol: false, lineStyle: { width: 1.6, color: "#7d3c98" },
              areaStyle: { opacity: 0.08 },
              markArea: (() => {
                // 门控关闭期阴影：连续 gate_open=false 的区间
                const eq = pfExp.equity;
                const areas: [{ xAxis: string }, { xAxis: string }][] = [];
                let start: string | null = null;
                eq.forEach((e, i) => {
                  if (e.gate_open === false && start === null) start = e.date.slice(2);
                  const isLast = i === eq.length - 1;
                  if ((e.gate_open !== false || isLast) && start !== null) {
                    const end = e.gate_open !== false ? eq[i - 1].date.slice(2) : e.date.slice(2);
                    areas.push([{ xAxis: start }, { xAxis: end }]);
                    start = null;
                  }
                });
                return { silent: true, data: areas,
                  itemStyle: { color: "rgba(128,128,128,0.12)" } };
              })(),
            }],
          } as never} />
          <div style={{ fontSize: 11, color: colors.muted, marginTop: 4 }}>
            灰色区间 = regime 门控关闭（候选池近 90 日 T+10 超额 ≤ 0 或样本不足，停止开新仓）
          </div>
          {pfExp.holdings.length > 0 && (
            <div style={{ fontSize: 12, color: colors.text, marginTop: 6 }}>
              <b>当前持仓：</b>
              {pfExp.holdings.map((h) => `${h.symbol}(${h.shares}股@${h.entry_price})`).join("、")}
            </div>
          )}
        </Card>
      )}

      <Card title="各策略命中明细" colors={colors}>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
          {picks.map((g) => (
            <div key={g.strategy} style={{
              border: `1px solid ${colors.border}`, borderRadius: 8, padding: "8px 12px", minWidth: 220,
            }}>
              <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>
                {g.strategy} <span style={{ color: colors.muted, fontWeight: 400 }}>({g.items.length})</span>
              </div>
              <div style={{ fontSize: 12, lineHeight: 1.8, color: colors.text }}>
                {g.items.slice(0, 12).map((i) => i.name || i.symbol).join("、")}
                {g.items.length > 12 && <span style={{ color: colors.muted }}> …</span>}
              </div>
            </div>
          ))}
          {!picks.length && <div style={{ color: colors.muted, fontSize: 13 }}>当日无命中</div>}
        </div>
      </Card>
    </div>
  );
}
