/**
 * 信号中心（Sequoia 雷达迁移）：每日买卖动作 + 各策略命中明细。
 */
import { useEffect, useState } from "react";
import { Badge, Btn, Card, PageHeader } from "../components/ui";
import EChart from "../components/EChart";
import { get, signalApi, PortfolioSnapshot } from "../api/client";
import { useTheme } from "../theme";

interface ActionItem {
  symbol: string; name: string | null; action: string; streak: number;
  resonance: number; strategies: string; quality_tier: string;
  close: number | null; reason: string;
}

const ACTION_META: Record<string, { cn: string; color: string }> = {
  BUY_STRONG: { cn: "重点买入", color: "#c0392b" },
  WATCH: { cn: "关注", color: "#b9770e" },
  NEW: { cn: "新出现", color: "#2471a3" },
  REDUCE: { cn: "减仓", color: "#1e8449" },
  SELL: { cn: "卖出", color: "#1e8449" },
  EXIT: { cn: "移出", color: "#888780" },
};
const ORDER = ["BUY_STRONG", "WATCH", "NEW", "REDUCE", "SELL", "EXIT"];

export default function SignalPage() {
  const { colors } = useTheme();
  const [date, setDate] = useState("");
  const [dates, setDates] = useState<{ date: string; symbols: number }[]>([]);
  const [actions, setActions] = useState<ActionItem[]>([]);
  const [actionDate, setActionDate] = useState<string | null>(null);
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
    get<{ date: string | null; items: ActionItem[] }>(`/signal/actions?date=${date}`).then((d) => {
      setActions(d.items);
      setActionDate(d.date);
    }).catch(() => {});
    get<{ groups: { strategy: string; items: { symbol: string; name: string | null }[] }[] }>(
      `/signal/picks?date=${date}`).then((d) => setPicks(d.groups)).catch(() => {});
  }, [date]);

  const grouped = ORDER.map((a) => ({ action: a, items: actions.filter((x) => x.action === a) }))
    .filter((g) => g.items.length);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <PageHeader
        title="信号中心"
        desc="Sequoia 雷达口径 · streak 连续确认 + 质量分级 + 风控优先判定"
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
          <div style={{ fontSize: 11.5, color: colors.muted, marginBottom: 10 }}>
            完全跟随信号判定的 BUY_STRONG / WATCH 白名单自动进出：T+1 开盘、等权、整手、
            真实成本（佣金/印花税/滑点），并带止盈 +30% / 止损 −10%。无需任何配置 ——
            与「模拟盘」页的手动建任务不同（那边由你自选策略与标的）。
          </div>
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

      {grouped.map((g) => {
        const meta = ACTION_META[g.action];
        return (
          <Card key={g.action} colors={colors} title={
            <span><span style={{ color: meta.color, fontWeight: 700 }}>{meta.cn}</span>
              <span style={{ color: colors.muted, fontSize: 12.5, marginLeft: 8 }}>{g.items.length} 只</span></span>
          }>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(240px,1fr))", gap: 8 }}>
              {g.items.map((it) => (
                <div key={it.symbol} style={{
                  border: `1px solid ${colors.border}`, borderRadius: 8, padding: "8px 12px",
                  background: colors.tableStripe,
                }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
                    <b style={{ fontSize: 13.5 }}>{it.name || it.symbol}</b>
                    <span style={{ color: colors.muted, fontSize: 11.5 }}>{it.symbol}</span>
                  </div>
                  <div style={{ fontSize: 11.5, color: colors.muted, marginTop: 3 }}>
                    连续 <b style={{ color: meta.color }}>{it.streak}</b> 天 · 共振 {it.resonance} · {it.strategies}
                  </div>
                  <div style={{ fontSize: 11.5, color: colors.text, marginTop: 3, lineHeight: 1.5 }}>{it.reason}</div>
                </div>
              ))}
            </div>
          </Card>
        );
      })}

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
