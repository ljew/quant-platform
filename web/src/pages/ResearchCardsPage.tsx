/**
 * 研究卡（策略研究 · 首个 Tab）：一个策略/因子 = 一张标准卡片。
 * 回答三个问题：现在哪些在干活？哪些证据在恶化？每次状态变化为什么？
 * 数据源：GET /research/cards（后端实时读 research_cards 表，每周复盘自动更新证据）。
 * 状态机：观察 → 候选 → 启用 → 降级 → 退役；升级自动执行，降级/退役需人工确认。
 */
import { useCallback, useEffect, useState } from "react";
import { Badge, Btn, PageHeader } from "../components/ui";
import { researchApi, ResearchCardItem } from "../api/client";
import { useTheme } from "../theme";

const STATE_META: Record<string, { label: string; color: string; desc: string }> = {
  enabled: { label: "启用", color: "#1e8449", desc: "进每日信号生产" },
  candidate: { label: "候选", color: "#2456c8", desc: "证据积累中" },
  watch: { label: "观察", color: "#7a8299", desc: "只记录不交易" },
  downgraded: { label: "降级", color: "#b9770e", desc: "停止建仓只观察" },
  retired: { label: "退役", color: "#c0392b", desc: "归档不再出现" },
};
const STATE_ORDER = ["enabled", "candidate", "watch", "downgraded", "retired"];

/** 证据小柱：240/120/90 日超额（正负按 A 股配色：正红负绿，与全站一致）。 */
function EvidenceBars({ ev, colors }: { ev: ResearchCardItem["evidence"]; colors: ReturnType<typeof useTheme>["colors"] }) {
  const bars: [string, number | null | undefined][] = [
    ["240日", ev.w240], ["120日", ev.w120], ["90日", ev.d90 ?? ev.exc_10],
  ];
  return (
    <div style={{ display: "flex", gap: 8, alignItems: "flex-end" }}>
      {bars.map(([label, v]) => {
        if (v === null || v === undefined)
          return (
            <div key={label} style={{ flex: 1, textAlign: "center" }}>
              <div style={{ fontSize: 12.5, fontWeight: 700, color: colors.muted }}>—</div>
              <div style={{ height: 40, display: "flex", alignItems: "flex-end", justifyContent: "center", margin: "3px 0" }}>
                <div style={{ width: 24, height: 2, background: colors.border, borderRadius: 2 }} />
              </div>
              <div style={{ fontSize: 10.5, color: colors.muted }}>{label}</div>
            </div>
          );
        const h = Math.min(38, Math.max(3, Math.abs(v) * 13));
        const c = v >= 0 ? colors.up : colors.down;
        return (
          <div key={label} style={{ flex: 1, textAlign: "center" }}>
            <div style={{ fontSize: 12.5, fontWeight: 700, color: c, fontVariantNumeric: "tabular-nums" }}>
              {v > 0 ? "+" : ""}{v.toFixed(2)}%
            </div>
            <div style={{ height: 40, display: "flex", alignItems: "flex-end", justifyContent: "center", margin: "3px 0" }}>
              <div style={{ width: 24, height: h, background: c, borderRadius: "4px 4px 0 0", opacity: 0.85 }} />
            </div>
            <div style={{ fontSize: 10.5, color: colors.muted }}>{label}</div>
          </div>
        );
      })}
    </div>
  );
}

function CardView({ card }: { card: ResearchCardItem }) {
  const { colors } = useTheme();
  const meta = STATE_META[card.state] ?? STATE_META.watch;
  const ev = card.evidence || {};
  const isFactor = card.card_type === "factor";
  const n = ev.n_10 ?? ev.n;
  const lastHist = card.history[card.history.length - 1];

  return (
    <div style={{
      background: colors.card, border: `1px solid ${colors.border}`, borderRadius: 12,
      padding: "14px 16px", display: "flex", flexDirection: "column", gap: 9,
    }}>
      {/* 头部：名称 + 状态 */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <span style={{ fontWeight: 700, fontSize: 15 }}>
          {card.name}
          <span style={{ color: colors.muted, fontSize: 11, fontWeight: 400, marginLeft: 7 }}>{card.key}</span>
          <span style={{ color: colors.muted, fontSize: 10.5, marginLeft: 6, border: `1px solid ${colors.border}`, borderRadius: 4, padding: "0 5px" }}>
            {isFactor ? "因子" : "策略"}
          </span>
        </span>
        <Badge text={meta.label} color={meta.color} soft />
      </div>

      {/* 假设 */}
      <div style={{ fontSize: 12.5, color: colors.text, background: colors.bg, borderRadius: 8, padding: "7px 10px" }}>
        <span style={{ color: colors.muted, fontSize: 11 }}>假设 · </span>{card.hypothesis || "—"}
      </div>

      {/* 证据 */}
      {isFactor ? (
        <div style={{ display: "flex", gap: 14, fontSize: 12.5 }}>
          <span style={{ color: colors.muted }}>IC <b style={{ color: (ev.ic_mean ?? 0) >= 0 ? colors.up : colors.down }}>{ev.ic_mean?.toFixed(4) ?? "—"}</b></span>
          <span style={{ color: colors.muted }}>ICIR <b style={{ color: colors.text }}>{ev.icir?.toFixed(2) ?? "—"}</b></span>
          <span style={{ color: colors.muted }}>评级 <b style={{ color: colors.text }}>{ev.rating ?? "—"}</b></span>
        </div>
      ) : (
        <EvidenceBars ev={ev} colors={colors} />
      )}

      {/* 元信息 */}
      {!isFactor && (
        <div style={{ display: "flex", gap: 12, fontSize: 11.5, color: colors.muted, flexWrap: "wrap" }}>
          {n !== undefined && <span>样本 <b style={{ color: colors.text }}>{Number(n).toLocaleString()}</b></span>}
          {ev.t_10 !== undefined && ev.t_10 !== null && (
            <span>t 值 <b style={{ color: Math.abs(ev.t_10) >= 2 ? colors.up : colors.text }}>{ev.t_10.toFixed(2)}</b></span>
          )}
          {ev.win_10 !== undefined && ev.win_10 !== null && <span>胜率 <b style={{ color: colors.text }}>{ev.win_10}%</b></span>}
          {card.state === "downgraded" && (
            <span style={{ color: (ev.negative_weeks ?? 0) >= 3 ? "#c0392b" : "#b9770e" }}>
              连续负超额 <b>{ev.negative_weeks ?? 0}/4</b> 周{(ev.negative_weeks ?? 0) >= 3 ? "（接近退役线）" : ""}
            </span>
          )}
        </div>
      )}
      {card.cost_note && card.cost_note !== "—" && (
        <div style={{ fontSize: 11.5, color: colors.muted }}>{card.cost_note}</div>
      )}

      {/* 失效条件 */}
      <div style={{ fontSize: 11.5, color: "#b9770e", borderTop: `1px dashed ${colors.border}`, paddingTop: 7 }}>
        <span style={{ color: colors.muted }}>失效条件 · </span>{card.fail_condition || "—"}
      </div>

      {/* 决策历史 */}
      {lastHist && (
        <details style={{ fontSize: 11, color: colors.muted }}>
          <summary style={{ cursor: "pointer", userSelect: "none" }}>
            最近：{lastHist.at ?? lastHist.date} {lastHist.from ? `${STATE_META[lastHist.from]?.label ?? lastHist.from} → ` : ""}{STATE_META[lastHist.to]?.label ?? lastHist.to}
            {lastHist.actor ? `（${lastHist.actor === "human" ? "人工" : "自动"}）` : ""}
          </summary>
          <div style={{ marginTop: 6, display: "flex", flexDirection: "column", gap: 5 }}>
            {[...card.history].reverse().map((h, i) => (
              <div key={i} style={{ borderLeft: `2px solid ${colors.border}`, paddingLeft: 8 }}>
                <div>{h.at ?? h.date} · {h.from ? `${STATE_META[h.from]?.label ?? h.from} → ` : ""}{STATE_META[h.to]?.label ?? h.to}
                  {h.actor ? <span style={{ opacity: 0.7 }}>（{h.actor === "human" ? "人工" : "自动"}）</span> : null}</div>
                <div style={{ opacity: 0.85 }}>{h.reason}</div>
              </div>
            ))}
          </div>
        </details>
      )}
      <div style={{ fontSize: 10.5, color: colors.muted, opacity: 0.75, marginTop: "auto" }}>
        证据更新于 {ev.evidence_updated_at ?? card.updated_at?.slice(0, 10) ?? "—"}
      </div>
    </div>
  );
}

export default function ResearchCardsPage() {
  const { colors } = useTheme();
  const [data, setData] = useState<{ total: number; state_count: Record<string, number>; cards: ResearchCardItem[] } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      setData(await researchApi.cards());
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const cards = [...(data?.cards ?? [])].sort((a, b) => {
    const d = STATE_ORDER.indexOf(a.state) - STATE_ORDER.indexOf(b.state);
    if (d !== 0) return d;
    return (b.evidence?.exc_10 ?? b.evidence?.ic_mean ?? -99) - (a.evidence?.exc_10 ?? a.evidence?.ic_mean ?? -99);
  });

  return (
    <div>
      <PageHeader
        title="研究卡"
        desc="一个策略/因子 = 一张标准卡片 · 状态机驱动生命周期 · 升级自动执行，降级/退役需人工确认 · 每周复盘自动更新证据"
        actions={<Btn onClick={load} kind="ghost" small>{loading ? "刷新中…" : "刷新"}</Btn>}
      />

      {err && <div style={{ color: colors.up, marginBottom: 14, fontSize: 13 }}>加载失败：{err}</div>}

      {/* —— 状态机总览 —— */}
      <div style={{ display: "flex", gap: 10, marginBottom: 8, flexWrap: "wrap" }}>
        {STATE_ORDER.map((s, i) => {
          const m = STATE_META[s];
          const cnt = data?.state_count?.[s] ?? 0;
          return (
            <div key={s} style={{ display: "flex", alignItems: "center", gap: 10, flex: 1, minWidth: 150 }}>
              <div style={{
                flex: 1, background: colors.card, border: `1px solid ${colors.border}`,
                borderRadius: 10, padding: "10px 14px", textAlign: "center",
              }}>
                <div style={{ fontSize: 12.5, fontWeight: 600, color: m.color }}>{m.label}</div>
                <div style={{ fontSize: 22, fontWeight: 800, fontVariantNumeric: "tabular-nums", margin: "2px 0" }}>{cnt}</div>
                <div style={{ fontSize: 10.5, color: colors.muted }}>{m.desc}</div>
              </div>
              {i < STATE_ORDER.length - 1 && <span style={{ color: colors.muted }}>→</span>}
            </div>
          );
        })}
      </div>
      <div style={{ fontSize: 11.5, color: colors.muted, textAlign: "center", marginBottom: 20 }}>
        升级自动执行 · 降级/退役出建议由人确认 · 每次迁移自动写入决策文档（架构文档第 10 章）
      </div>

      {/* —— 卡片墙 —— */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(330px, 1fr))", gap: 14 }}>
        {cards.map((c) => <CardView key={c.id} card={c} />)}
      </div>
      {!loading && cards.length === 0 && !err && (
        <div style={{ color: colors.muted, fontSize: 13, padding: 30, textAlign: "center" }}>暂无研究卡</div>
      )}
    </div>
  );
}
