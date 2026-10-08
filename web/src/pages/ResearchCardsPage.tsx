/**
 * 研究卡（策略研究 · 首个 Tab）：一个策略/因子 = 一张标准卡片。
 * 平时不用看；有建议时顶部横幅提醒你，点「同意」即执行（下一次跑批生效）。
 * 点击卡片看完整档案：假设 / 口径 / 证据明细 / 成本 / 拥挤 / 失效条件 / 决策历史。
 * 数据源：GET /research/cards + /research/suggestions（证据由每周复盘自动刷新）。
 */
import { useCallback, useEffect, useState } from "react";
import { Badge, Btn, PageHeader } from "../components/ui";
import { CardSuggestion, researchApi, ResearchCardItem } from "../api/client";
import { useTheme, ThemeColors } from "../theme";

const STATE_META: Record<string, { label: string; color: string; desc: string }> = {
  enabled: { label: "启用", color: "#1e8449", desc: "进每日信号生产，可以建仓" },
  candidate: { label: "候选", color: "#2456c8", desc: "试用期，证据积累中" },
  watch: { label: "观察", color: "#7a8299", desc: "只记录，不交易" },
  downgraded: { label: "降级", color: "#b9770e", desc: "停止建仓，只观察" },
  retired: { label: "退役", color: "#c0392b", desc: "归档，不再出现" },
};
const STATE_ORDER = ["enabled", "candidate", "watch", "downgraded", "retired"];

/** 名词解释（回答「柱子/t值/证据是啥」），页面底部可折叠。 */
const GLOSSARY: [string, string][] = [
  ["三个柱子（240日/120日/90日）",
   "用这个策略的信号分别回看最近 240 / 120 / 90 个交易日，平均比大盘（全市场等权）多赚多少。" +
   "红柱=跑赢，绿柱=跑输。三个窗口都看是为了防止「只在某段行情里碰巧有效」——长期短期都为正才算稳。"],
  ["t 值",
   "「这不是运气」的可信度。比如 +0.75% 的超额，可能是策略真有效，也可能是运气好撞上的。" +
   "t 值把超额和它的波动放在一起算：|t|≥2 才算「大概率不是运气」；t 越大越可信，负得越多说明「稳定地亏」。"],
  ["证据",
   "决定一张卡能不能上岗的全部事实：三窗口超额 + 样本量 n + t 值 + 胜率，外加成本和相关性评估。" +
   "证据由每周复盘自动更新，卡片上显示的是最近一次复盘的结果。"],
  ["样本量 n",
   "这段窗口里信号出现了多少次。n 太小时超额再好也说明不了什么（状态机要求 n≥20 才采信）。"],
  ["负周数 X/4",
   "降级卡的退役倒计时：连续 4 周超额为负，系统就会建议你让它退役。恢复为正会自动清零。"],
];

function EvidenceBars({ ev, colors }: { ev: ResearchCardItem["evidence"]; colors: ThemeColors }) {
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

/** 状态迁移操作区：卡片详情和确认横幅共用。 */
function TransitionBox({
  cardId, current, onDone, colors, defaultTo, defaultReason,
}: {
  cardId: number; current: string; colors: ThemeColors;
  defaultTo?: string; defaultReason?: string;
  onDone: () => void;
}) {
  const [to, setTo] = useState(defaultTo ?? "");
  const [reason, setReason] = useState(defaultReason ?? "");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const targets = STATE_ORDER.filter((s) => s !== current);

  const submit = async () => {
    if (!to || reason.trim().length < 2) {
      setMsg("请选择目标状态并填写理由（至少 2 个字）");
      return;
    }
    setBusy(true);
    setMsg(null);
    try {
      const r = await researchApi.transition(cardId, to, reason.trim());
      setMsg(r.changed ? "✅ 已执行，下一次跑批生效" : "状态未变化");
      setTimeout(onDone, 600);
    } catch (e) {
      setMsg(`失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
      <select
        value={to}
        onChange={(e) => setTo(e.target.value)}
        style={{
          padding: "6px 10px", borderRadius: 7, border: `1px solid ${colors.border}`,
          background: colors.card, color: colors.text, fontSize: 12.5,
        }}
      >
        <option value="">选择目标状态…</option>
        {targets.map((s) => (
          <option key={s} value={s}>{STATE_META[s].label}（{STATE_META[s].desc}）</option>
        ))}
      </select>
      <input
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        placeholder="理由（会写进决策历史）"
        style={{
          flex: 1, minWidth: 200, padding: "6px 10px", borderRadius: 7,
          border: `1px solid ${colors.border}`, background: colors.card,
          color: colors.text, fontSize: 12.5,
        }}
      />
      <Btn small onClick={submit} disabled={busy}>{busy ? "执行中…" : "确认执行"}</Btn>
      {msg && <span style={{ fontSize: 12, color: msg.startsWith("✅") ? colors.down : colors.up }}>{msg}</span>}
    </div>
  );
}

/** 卡片详情弹层：8 个字段全量 + 周快照 + 完整决策历史 + 人工迁移操作。 */
function CardDetail({ card, onClose, onChanged }: {
  card: ResearchCardItem; onClose: () => void; onChanged: () => void;
}) {
  const { colors } = useTheme();
  const meta = STATE_META[card.state] ?? STATE_META.watch;
  const ev = card.evidence || {};
  const isFactor = card.card_type === "factor";
  const weekly = [...(ev.weekly ?? [])].reverse().slice(0, 12);

  const row = (label: string, content: React.ReactNode) => (
    <div style={{ marginBottom: 12 }}>
      <div style={{ fontSize: 11.5, color: colors.muted, marginBottom: 3 }}>{label}</div>
      <div style={{ fontSize: 13, lineHeight: 1.7 }}>{content}</div>
    </div>
  );

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, background: "rgba(10,14,22,.55)",
        display: "flex", alignItems: "flex-start", justifyContent: "center",
        padding: "40px 20px", zIndex: 100, overflowY: "auto",
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: colors.card, border: `1px solid ${colors.border}`, borderRadius: 14,
          maxWidth: 720, width: "100%", padding: "22px 26px", boxShadow: "0 12px 40px rgba(0,0,0,.3)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 14 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span style={{ fontSize: 18, fontWeight: 700 }}>{card.name}</span>
            <span style={{ color: colors.muted, fontSize: 12 }}>{card.key}</span>
            <Badge text={isFactor ? "因子卡" : "策略卡"} color="#6c5ce7" soft />
            <Badge text={meta.label} color={meta.color} soft />
          </div>
          <button onClick={onClose} style={{
            border: 0, background: "transparent", color: colors.muted,
            fontSize: 18, cursor: "pointer", padding: "0 4px",
          }}>✕</button>
        </div>

        {row("① 假设（它凭什么赚钱）", card.hypothesis || "—")}
        {row("② 数据与口径", card.data_spec || "—")}
        {row("③ 证据（每周复盘自动更新）", isFactor ? (
          <span>IC <b>{ev.ic_mean?.toFixed(4) ?? "—"}</b> · ICIR <b>{ev.icir?.toFixed(2) ?? "—"}</b> · 评级 <b>{ev.rating ?? "—"}</b></span>
        ) : (
          <div>
            <div style={{ maxWidth: 320 }}><EvidenceBars ev={ev} colors={colors} /></div>
            <div style={{ marginTop: 8, color: colors.muted, fontSize: 12 }}>
              样本 n=<b style={{ color: colors.text }}>{(ev.n_10 ?? ev.n)?.toLocaleString() ?? "—"}</b>
              {" · "}t 值 <b style={{ color: colors.text }}>{ev.t_10?.toFixed(2) ?? "—"}</b>（|t|≥2 才不是运气）
              {" · "}胜率 <b style={{ color: colors.text }}>{ev.win_10 ?? "—"}%</b>
              {" · "}T+5 超额 <b style={{ color: colors.text }}>{ev.exc_5 != null ? `${ev.exc_5 > 0 ? "+" : ""}${ev.exc_5}%` : "—"}</b>
            </div>
            {weekly.length > 0 && (
              <table style={{ marginTop: 10, width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                <thead>
                  <tr style={{ color: colors.muted }}>
                    <th style={{ textAlign: "left", padding: "3px 8px", fontWeight: 500 }}>周</th>
                    <th style={{ textAlign: "right", padding: "3px 8px", fontWeight: 500 }}>90日超额</th>
                    <th style={{ textAlign: "right", padding: "3px 8px", fontWeight: 500 }}>t 值</th>
                    <th style={{ textAlign: "right", padding: "3px 8px", fontWeight: 500 }}>样本</th>
                  </tr>
                </thead>
                <tbody>
                  {weekly.map((w, i) => (
                    <tr key={i} style={{ borderTop: `1px solid ${colors.border}` }}>
                      <td style={{ padding: "3px 8px" }}>{w.date}</td>
                      <td style={{ padding: "3px 8px", textAlign: "right", color: (w.exc_10 ?? 0) >= 0 ? colors.up : colors.down }}>
                        {w.exc_10 != null ? `${w.exc_10 > 0 ? "+" : ""}${w.exc_10}%` : "—"}
                      </td>
                      <td style={{ padding: "3px 8px", textAlign: "right" }}>{w.t_10?.toFixed(2) ?? "—"}</td>
                      <td style={{ padding: "3px 8px", textAlign: "right" }}>{w.n_10?.toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        ))}
        {row("④ 成本", card.cost_note || "—")}
        {row("⑤ 容量与拥挤", card.crowd_note || "—")}
        {row("⑥ 失效条件（事先写好的「什么情况算它死了」）",
          <span style={{ color: "#b9770e" }}>{card.fail_condition || "—"}</span>)}
        {row(`⑦ 状态 · 当前「${meta.label}」——${meta.desc}`,
          card.state === "downgraded"
            ? <span>连续负超额 <b>{ev.negative_weeks ?? 0}/4</b> 周（满 4 周系统会建议退役；恢复为正自动清零并升回候选）</span>
            : <span style={{ color: colors.muted }}>状态迁移条件见页面顶部状态机；升级自动、降级需你确认。</span>)}
        {row("⑧ 决策历史（每次状态变化的日期与理由）", (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {[...card.history].reverse().map((h, i) => (
              <div key={i} style={{ borderLeft: `2px solid ${colors.border}`, paddingLeft: 10 }}>
                <div style={{ fontSize: 12 }}>
                  {h.at ?? h.date} · {h.from ? `${STATE_META[h.from]?.label ?? h.from} → ` : ""}{STATE_META[h.to]?.label ?? h.to}
                  <span style={{ color: colors.muted }}>（{h.actor === "human" ? "人工" : h.actor === "auto-mining" ? "自动挖矿" : h.actor === "auto-review" ? "每周复盘" : "系统"}）</span>
                </div>
                <div style={{ fontSize: 12, color: colors.muted }}>{h.reason}</div>
              </div>
            ))}
          </div>
        ))}

        <div style={{ borderTop: `1px solid ${colors.border}`, paddingTop: 14, marginTop: 6 }}>
          <div style={{ fontSize: 11.5, color: colors.muted, marginBottom: 8 }}>
            人工状态迁移（降级/退役建议在这里确认；执行后下一次跑批生效，理由会留痕）
          </div>
          <TransitionBox cardId={card.id} current={card.state} colors={colors} onDone={onChanged} />
        </div>
      </div>
    </div>
  );
}

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

export default function ResearchCardsPage() {
  const { colors } = useTheme();
  const [data, setData] = useState<{ total: number; state_count: Record<string, number>; cards: ResearchCardItem[] } | null>(null);
  const [suggestions, setSuggestions] = useState<CardSuggestion[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [detail, setDetail] = useState<ResearchCardItem | null>(null);

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

  const cards = [...(data?.cards ?? [])].sort((a, b) => {
    const d = STATE_ORDER.indexOf(a.state) - STATE_ORDER.indexOf(b.state);
    if (d !== 0) return d;
    return (b.evidence?.exc_10 ?? b.evidence?.ic_mean ?? -99) - (a.evidence?.exc_10 ?? a.evidence?.ic_mean ?? -99);
  });

  return (
    <div>
      <PageHeader
        title="研究卡"
        desc="一个策略/因子 = 一张标准卡片 · 平时不用看，有建议时顶部会提醒你 · 点卡片看完整档案"
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
      <div style={{ fontSize: 11.5, color: colors.muted, textAlign: "center", marginBottom: 20 }}>
        这行数字是系统的「资产负债表」：启用几个 = 有几个策略在动钱 · 升级自动执行 · 降级/退役需你确认
      </div>

      {/* —— 卡片墙 —— */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(330px, 1fr))", gap: 14 }}>
        {cards.map((c) => (
          <div key={c.id} onClick={() => setDetail(c)} style={{ cursor: "pointer" }}>
            <CardSummary card={c} />
          </div>
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

      {detail && (
        <CardDetail
          card={detail}
          onClose={() => setDetail(null)}
          onChanged={() => { setDetail(null); load(); }}
        />
      )}
    </div>
  );
}

/** 卡片墙中的摘要卡（点击进详情）。 */
function CardSummary({ card }: { card: ResearchCardItem }) {
  const { colors } = useTheme();
  const meta = STATE_META[card.state] ?? STATE_META.watch;
  const ev = card.evidence || {};
  const isFactor = card.card_type === "factor";
  const n = ev.n_10 ?? ev.n;
  const lastHist = card.history[card.history.length - 1];

  return (
    <div style={{
      background: colors.card, border: `1px solid ${colors.border}`, borderRadius: 12,
      padding: "14px 16px", display: "flex", flexDirection: "column", gap: 9, height: "100%",
      boxSizing: "border-box", transition: "border-color .15s",
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
        证据更新于 {ev.evidence_updated_at ?? card.updated_at?.slice(0, 10) ?? "—"} · 点击查看完整档案
      </div>
    </div>
  );
}
