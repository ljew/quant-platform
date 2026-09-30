/**
 * 信号中心（Sequoia 雷达迁移）：每日买卖动作 + 各策略命中明细。
 */
import { useEffect, useState } from "react";
import { Badge, Btn, Card, PageHeader } from "../components/ui";
import { get } from "../api/client";
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
