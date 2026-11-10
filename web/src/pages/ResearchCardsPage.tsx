/**
 * 研究卡（策略研究 · 首个 Tab）：一个策略/因子 = 一张标准卡片。
 * 平时不用看；有建议时顶部横幅提醒你，点「同意」即执行（下一次跑批生效）。
 * 点击卡片进入整页详情：完整档案 + 信号日历 + 绩效细分 + 决策历史。
 * 数据源：GET /research/cards + /research/suggestions（证据由每周复盘自动刷新）。
 */
import { useCallback, useEffect, useState } from "react";
import { Badge, Btn, PageHeader } from "../components/ui";
import { EvidenceBars, STATE_META, STATE_ORDER, TransitionBox } from "../components/cardShared";
import ResearchCardDetailPage from "./ResearchCardDetailPage";
import { CardSuggestion, researchApi, ResearchCardItem } from "../api/client";
import { useTheme, ThemeColors } from "../theme";

/** 名词解释（回答「柱子/t值/证据是啥」），页面底部可折叠。 */
const GLOSSARY: [string, string][] = [
  ["三个柱子（240日/120日/90日）",
   "用这个策略的信号分别回看最近 240 / 120 / 90 个交易日，平均比大盘（全市场等权）多赚多少。" +
   "红柱=跑赢，绿柱=跑输。三个窗口都看是为了防止「只在某段行情里碰巧有效」——长期短期都为正才算稳。"],
  ["t 值",
   "「这不是运气」的可信度。公式：t = 平均超额 ÷ (波动 ÷ √样本数)——分子是赚了多少，分母是这个均值有多不靠谱。" +
   "例：缠论买点平均超额 +0.72%，但靠 1.7 万样本把标准误压到 0.078%，t = 0.72 ÷ 0.078 ≈ 9.3。" +
   "|t|≥2 才算大概率不是运气；t 很大但超额很薄 = 稳定地赚一点点（扣完成本可能没剩）；t 负得多 = 稳定地亏。" +
   "挖矿批量测试时，门槛会随测试数量上浮（测得多了总有几个碰巧好看）。"],
  ["证据",
   "决定一张卡能不能上岗的全部事实：三窗口超额 + 样本量 n + t 值 + 胜率，外加成本和相关性评估。" +
   "证据由每周复盘自动更新，卡片上显示的是最近一次复盘的结果。"],
  ["样本量 n",
   "这段窗口里信号出现了多少次。n 太小时超额再好也说明不了什么（状态机要求 n≥20 才采信）。"],
  ["负周数 X/4",
   "降级卡的退役倒计时：连续 4 周超额为负，系统就会建议你让它退役。恢复为正会自动清零。"],
];

/** 顶部待确认横幅：每周复盘出的降级/退役/升级建议，一键同意或忽略。 */
function SuggestionBanner({ items, onChanged, colors }: {
  items: CardSuggestion[]; onChanged: () => void; colors: ThemeColors;
}) {
  const [dismissed, setDismissed] = useState<string[]>(() => {
    try { return JSON.parse(localStorage.getItem("rc-dismissed") || "[]"); } catch { return []; }
  });
  const [confirming, setConfirming] = useState<CardSuggestion | null>(null);

  const visible = items.filter((s) => !dismissed.includes(`${s.card_id}:${s.to_state}:${s.evidence_date}`));
  if (visible.length === 0) return null;

  const dismiss = (s: CardSuggestion) => {
    const key = `${s.card_id}:${s.to_state}:${s.evidence_date}`;
    const next = [...dismissed, key];
    setDismissed(next);
    localStorage.setItem("rc-dismissed", JSON.stringify(next));
  };

  const KIND_COLOR: Record<string, string> = {
    downgrade: "#b9770e", retire: "#c0392b", upgrade: "#1e8449", demote_watch: "#b9770e",
  };
  const KIND_LABEL: Record<string, string> = {
    downgrade: "建议降级", retire: "建议退役", upgrade: "可升启用", demote_watch: "建议降为观察",
  };

  return (
    <div style={{
      background: "#c8860d14", border: "1px solid #c8860d55", borderRadius: 12,
      padding: "12px 16px", marginBottom: 18,
    }}>
      <div style={{ fontSize: 13, fontWeight: 700, color: "#c8860d", marginBottom: 8 }}>
        ⏳ 待你确认（{visible.length}）——来自每周复盘，确认后下一次跑批生效
      </div>
      {visible.map((s) => (
        <div key={`${s.card_id}:${s.to_state}`} style={{ marginBottom: 10 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <Badge text={KIND_LABEL[s.kind] ?? s.kind} color={KIND_COLOR[s.kind] ?? "#b9770e"} soft />
            <span style={{ fontSize: 13 }}><b>{s.name}</b>：{s.text}</span>
            <Btn small onClick={() => setConfirming(confirming?.card_id === s.card_id ? null : s)}>
              {confirming?.card_id === s.card_id ? "收起" : "同意…"}
            </Btn>
            <Btn small kind="ghost" onClick={() => dismiss(s)}>再观察</Btn>
          </div>
          {confirming?.card_id === s.card_id && (
            <div style={{ marginTop: 8, paddingLeft: 4 }}>
              <TransitionBox
                cardId={s.card_id} current={s.state} colors={colors}
                defaultTo={s.to_state} defaultReason={s.text}
                onDone={() => { setConfirming(null); onChanged(); }}
              />
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

/** 卡片墙中的摘要卡（点击进整页详情）。 */
function CardSummary({ card, onOpen }: { card: ResearchCardItem; onOpen: () => void }) {
  const { colors } = useTheme();
  const meta = STATE_META[card.state] ?? STATE_META.watch;
  const ev = card.evidence || {};
  const isFactor = card.card_type === "factor";
  const n = ev.n_10 ?? ev.n;
  const lastHist = card.history[card.history.length - 1];

  return (
    <div
      onClick={onOpen}
      style={{
        background: colors.card, border: `1px solid ${colors.border}`, borderRadius: 12,
        padding: "14px 16px", display: "flex", flexDirection: "column", gap: 9, height: "100%",
        boxSizing: "border-box", transition: "border-color .15s", cursor: "pointer",
      }}
      onMouseEnter={(e) => (e.currentTarget.style.borderColor = colors.accent)}
      onMouseLeave={(e) => (e.currentTarget.style.borderColor = colors.border)}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <span style={{ fontWeight: 700, fontSize: 15 }}>
          {card.name}
          <span style={{ color: colors.muted, fontSize: 10.5, marginLeft: 6, border: `1px solid ${colors.border}`, borderRadius: 4, padding: "0 5px" }}>
            {isFactor ? "因子" : "策略"}
          </span>
        </span>
        <Badge text={meta.label} color={meta.color} soft />
      </div>

      <div style={{ fontSize: 12.5, background: colors.bg, borderRadius: 8, padding: "7px 10px" }}>
        <span style={{ color: colors.muted, fontSize: 11 }}>假设 · </span>{card.hypothesis || "—"}
      </div>

      {isFactor ? (
        <div style={{ display: "flex", gap: 14, fontSize: 12.5 }}>
          <span style={{ color: colors.muted }}>IC <b style={{ color: (ev.ic_mean ?? 0) >= 0 ? colors.up : colors.down }}>{ev.ic_mean?.toFixed(4) ?? "—"}</b></span>
          <span style={{ color: colors.muted }}>ICIR <b style={{ color: colors.text }}>{ev.icir?.toFixed(2) ?? "—"}</b></span>
          <span style={{ color: colors.muted }}>评级 <b style={{ color: colors.text }}>{ev.rating ?? "—"}</b></span>
        </div>
      ) : (
        <EvidenceBars ev={ev} colors={colors} />
      )}

      {!isFactor && (
        <div style={{ display: "flex", gap: 12, fontSize: 11.5, color: colors.muted, flexWrap: "wrap" }}>
          {n !== undefined && <span>样本 <b style={{ color: colors.text }}>{Number(n).toLocaleString()}</b></span>}
          {ev.t_10 !== undefined && ev.t_10 !== null && (
            <span title="超额÷波动换算的可信度，|t|≥2 才算不是运气">
              t 值 <b style={{ color: Math.abs(ev.t_10) >= 2 ? colors.up : colors.text }}>{ev.t_10.toFixed(2)}</b>
            </span>
          )}
          {card.state === "downgraded" && (
            <span style={{ color: (ev.negative_weeks ?? 0) >= 3 ? "#c0392b" : "#b9770e" }}>
              负周数 <b>{ev.negative_weeks ?? 0}/4</b>
            </span>
          )}
        </div>
      )}

      <div style={{ fontSize: 11.5, color: "#b9770e", borderTop: `1px dashed ${colors.border}`, paddingTop: 7 }}>
        <span style={{ color: colors.muted }}>失效条件 · </span>{card.fail_condition || "—"}
      </div>

      {lastHist && (
        <div style={{ fontSize: 11, color: colors.muted }}>
          最近：{lastHist.at ?? lastHist.date} {lastHist.from ? `${STATE_META[lastHist.from]?.label ?? lastHist.from} → ` : ""}{STATE_META[lastHist.to]?.label ?? lastHist.to}
        </div>
      )}
      <div style={{ fontSize: 10.5, color: colors.muted, opacity: 0.75, marginTop: "auto" }}>
        证据更新于 {ev.evidence_updated_at ?? card.updated_at?.slice(0, 10) ?? "—"} · 点击查看完整分析
      </div>
    </div>
  );
}

export default function ResearchCardsPage({ initialDetailId }: { initialDetailId?: number }) {
  const { colors } = useTheme();
  const [data, setData] = useState<{ total: number; state_count: Record<string, number>; cards: ResearchCardItem[] } | null>(null);
  const [suggestions, setSuggestions] = useState<CardSuggestion[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [showRetired, setShowRetired] = useState(false);
  const [loading, setLoading] = useState(false);
  // 深链：#cards/384 直接进详情（智能体产物「跳回核对」用）。
  const [detailId, setDetailId] = useState<number | null>(initialDetailId ?? null);

  useEffect(() => {
    setDetailId(initialDetailId ?? null);
  }, [initialDetailId]);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const [c, s] = await Promise.all([researchApi.cards(), researchApi.suggestions()]);
      setData(c);
      setSuggestions(s.items);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  // 整页详情（非弹层）
  if (detailId !== null) {
    return (
      <ResearchCardDetailPage
        cardId={detailId}
        onBack={() => {
          setDetailId(null);
          // 同步把地址栏的 /384 去掉，否则刷新又跳回详情
          window.history.replaceState(null, "", "#cards");
          load();
        }}
      />
    );
  }

  // 默认隐藏已退役的（2026-11-10 老刘：「策略研究这里面垃圾还比较多」——
  // 26 张里有 20 张 retired，全铺在页面上，能用的 6 张被淹了）。
  // 数据不删，退役记录留着可追溯；要看时点漏斗里的「退役」格切换。
  const retiredCount = data?.state_count?.retired ?? 0;
  const cards = [...(data?.cards ?? [])]
    .filter((c) => showRetired || c.state !== "retired")
    .sort((a, b) => {
      const d = STATE_ORDER.indexOf(a.state) - STATE_ORDER.indexOf(b.state);
      if (d !== 0) return d;
      return (b.evidence?.exc_10 ?? b.evidence?.ic_mean ?? -99) - (a.evidence?.exc_10 ?? a.evidence?.ic_mean ?? -99);
    });

  return (
    <div>
      <PageHeader
        title="研究卡"
        desc="一个策略/因子 = 一张标准卡片 · 平时不用看，有建议时顶部会提醒你 · 点卡片进详情页看完整分析"
        actions={<Btn onClick={load} kind="ghost" small>{loading ? "刷新中…" : "刷新"}</Btn>}
      />

      {err && <div style={{ color: colors.up, marginBottom: 14, fontSize: 13 }}>加载失败：{err}</div>}

      <SuggestionBanner items={suggestions} onChanged={load} colors={colors} />

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
      <div style={{ fontSize: 11.5, color: colors.muted, textAlign: "center", marginBottom: 8 }}>
        这行数字是系统的「资产负债表」：启用几个 = 有几个策略在动钱 · 升级自动执行 · 降级/退役需你确认
      </div>
      {retiredCount > 0 && (
        <div style={{ textAlign: "center", marginBottom: 18 }}>
          <button
            onClick={() => setShowRetired((v) => !v)}
            style={{
              border: `1px solid ${colors.border}`, background: showRetired ? colors.card : "transparent",
              color: colors.muted, borderRadius: 999, padding: "3px 14px",
              fontSize: 11.5, cursor: "pointer",
            }}
          >
            {showRetired ? `▾ 已显示 ${retiredCount} 张退役卡（点击收起）` : `▸ 展开 ${retiredCount} 张已退役（默认隐藏）`}
          </button>
        </div>
      )}

      {/* —— 卡片墙 —— */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(330px, 1fr))", gap: 14 }}>
        {cards.map((c) => (
          <CardSummary key={c.id} card={c} onOpen={() => setDetailId(c.id)} />
        ))}
      </div>
      {!loading && cards.length === 0 && !err && (
        <div style={{ color: colors.muted, fontSize: 13, padding: 30, textAlign: "center" }}>暂无研究卡</div>
      )}

      {/* —— 名词解释 —— */}
      <details style={{ marginTop: 26, background: colors.card, border: `1px solid ${colors.border}`, borderRadius: 12, padding: "12px 18px" }}>
        <summary style={{ cursor: "pointer", fontWeight: 600, fontSize: 14, userSelect: "none" }}>
          📖 名词解释：柱子 / t 值 / 证据都是什么意思？
        </summary>
        <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 12 }}>
          {GLOSSARY.map(([term, text]) => (
            <div key={term} style={{ fontSize: 13, lineHeight: 1.8 }}>
              <b style={{ color: colors.accent }}>{term}</b>
              <div style={{ color: colors.muted, marginTop: 2 }}>{text}</div>
            </div>
          ))}
        </div>
      </details>
    </div>
  );
}
