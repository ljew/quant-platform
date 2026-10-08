/** 研究卡共享部件：状态元数据、证据柱、着色数字、小表格、状态迁移操作框。 */
import { ReactNode, useState } from "react";
import { Badge, Btn } from "./ui";
import { researchApi, ResearchCardItem } from "../api/client";
import { useTheme, ThemeColors } from "../theme";

export const STATE_META: Record<string, { label: string; color: string; desc: string }> = {
  enabled: { label: "启用", color: "#1e8449", desc: "进每日信号生产，可以建仓" },
  candidate: { label: "候选", color: "#2456c8", desc: "试用期，证据积累中" },
  watch: { label: "观察", color: "#7a8299", desc: "只记录，不交易" },
  downgraded: { label: "降级", color: "#b9770e", desc: "停止建仓，只观察" },
  retired: { label: "退役", color: "#c0392b", desc: "归档，不再出现" },
};
export const STATE_ORDER = ["enabled", "candidate", "watch", "downgraded", "retired"];

export const ACTION_LABEL: Record<string, string> = {
  BUY_STRONG: "买入", WATCH: "观察", SELL: "卖出", REDUCE: "减仓", EXIT: "离场", NEW: "新进",
};

/** 证据小柱：240/120/90 日超额（正红负绿，与全站一致）。 */
export function EvidenceBars({ ev, colors }: { ev: ResearchCardItem["evidence"]; colors: ThemeColors }) {
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

/** 数字着色：正红负绿（A 股配色）。 */
export function Signed({ v, suffix = "%" }: { v?: number | null; suffix?: string }) {
  const { colors } = useTheme();
  if (v === null || v === undefined || typeof v !== "number") return <span style={{ color: colors.muted }}>—</span>;
  return (
    <span style={{ color: v >= 0 ? colors.up : colors.down, fontVariantNumeric: "tabular-nums" }}>
      {v > 0 ? "+" : ""}{v}{suffix}
    </span>
  );
}

export function MiniTable({ head, rows, colors }: {
  head: string[]; rows: ReactNode[][]; colors: ThemeColors;
}) {
  return (
    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
      <thead>
        <tr style={{ color: colors.muted }}>
          {head.map((h, i) => (
            <th key={i} style={{ textAlign: i === 0 ? "left" : "right", padding: "3px 8px", fontWeight: 500 }}>{h}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i} style={{ borderTop: `1px solid ${colors.border}` }}>
            {r.map((c, j) => (
              <td key={j} style={{ padding: "3px 8px", textAlign: j === 0 ? "left" : "right" }}>{c}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** 状态迁移操作区：选目标状态 + 理由 → 确认执行（下一次跑批生效，留痕）。 */
export function TransitionBox({
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

export function actionBadge(action: string | null | undefined, colors: ThemeColors) {
  if (!action) return <span style={{ color: colors.muted }}>—</span>;
  return (
    <Badge
      text={ACTION_LABEL[action] ?? action}
      color={action === "BUY_STRONG" ? colors.up : action === "SELL" || action === "REDUCE" ? colors.down : "#b9770e"}
      soft
    />
  );
}
