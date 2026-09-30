/**
 * 信号组合（原「信号中心」精简版）：镜像组合（跟随信号自动模拟）+ 各策略命中明细。
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

  useEffect(() => {
    signalApi.portfolio().then((r) => { if (r.ok) setPf(r); }).catch(() => {});
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
        desc="镜像组合完全跟随每日信号自动模拟（T+1 开盘 · 等权 · 真实成本 · 止盈 +30% / 止损 −10%）"
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
