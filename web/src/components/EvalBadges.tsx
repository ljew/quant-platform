/**
 * 评估口径徽章（W45-badge）：成本 / 估值PIT / PEAD / OOS。
 * 设计契约：DESIGN-P0-P2-eval-factor-cost.md §5.4
 */
import { Badge } from "./ui";
import { ThemeColors } from "../theme";

export interface CostProfile {
  commission?: number;
  slippage?: number;
  stamp_tax?: number;
  min_commission?: number;
  risk_free_rate?: number;
  source?: "default" | "user" | "grid_pick" | string;
  label?: string;
}

export interface ValuationPit {
  mode?: "asof" | "fallback" | "unavailable" | "n/a" | string;
  coverage?: number;
  note?: string | null;
}

export interface PeadPit {
  mode?: "asof" | "legacy_static" | "unavailable" | "n/a" | string;
  scale?: string;
  aligned_with?: string;
  note?: string | null;
}

export interface OosBadge {
  enabled?: boolean;
  verdict?: string;
  oos_ratio?: number;
  [k: string]: unknown;
}

export interface EvalBadgesProps {
  cost_profile?: CostProfile | null;
  valuation_pit?: ValuationPit | null;
  pead_pit?: PeadPit | null;
  oos?: OosBadge | null;
  colors: ThemeColors;
}

const WARN = "#c8860d";
const OK = "#2f9e44";
const MUTED = "#868e96";
const ACCENT = "#4c6ef5";

function costText(c?: CostProfile | null): string | null {
  if (!c) return null;
  // W46-1：grid_pick 主文案仍「成本·自定义」；悬停靠 label「网格选中·」区分（无独立短标「成本·网格」）
  const src = c.source === "user" || c.source === "grid_pick" ? "自定义" : "默认";
  const shortLabel =
    c.commission === 0.00025 && c.slippage === 0.0001
      ? "万2.5+万1"
      : `佣${((c.commission ?? 0) * 10000).toFixed(1)}+滑${((c.slippage ?? 0) * 10000).toFixed(1)}`;
  return `成本·${src}${shortLabel}`;
}

function valText(v?: ValuationPit | null): string | null {
  if (!v || !v.mode || v.mode === "n/a") return null;
  if (v.mode === "asof") {
    const cov =
      typeof v.coverage === "number" && v.coverage > 0
        ? ` ${Math.round(v.coverage * 100)}%`
        : "";
    return `估值PIT·as-of${cov}`;
  }
  if (v.mode === "fallback") return "估值PIT·fallback";
  if (v.mode === "unavailable") return "估值PIT·不可用";
  return `估值PIT·${v.mode}`;
}

function peadText(p?: PeadPit | null): string | null {
  if (!p || !p.mode || p.mode === "n/a") return null;
  if (p.mode === "asof") return "PEAD·as-of";
  if (p.mode === "legacy_static") return "PEAD·legacy";
  if (p.mode === "unavailable") return "PEAD·不可用";
  return `PEAD·${p.mode}`;
}

function oosText(o?: OosBadge | null): string | null {
  if (!o) return null;
  if (o.enabled === false && !o.verdict) return null;
  const v = o.verdict || (o.enabled ? "启用" : "未启用");
  return `OOS·${v}`;
}

function tone(
  kind: "cost" | "val" | "pead" | "oos",
  data: CostProfile | ValuationPit | PeadPit | OosBadge | null | undefined,
  colors: ThemeColors,
): string {
  if (kind === "val") {
    const m = (data as ValuationPit | undefined)?.mode;
    if (m === "fallback" || m === "unavailable") return WARN;
    if (m === "asof") return OK;
    return MUTED;
  }
  if (kind === "pead") {
    const m = (data as PeadPit | undefined)?.mode;
    if (m === "legacy_static" || m === "unavailable") return WARN;
    if (m === "asof") return OK;
    return MUTED;
  }
  if (kind === "oos") {
    const v = (data as OosBadge | undefined)?.verdict;
    if (v === "通过") return OK;
    if (v === "过拟合" || v === "未通过" || v === "样本不足") return WARN;
    return colors.accent || ACCENT;
  }
  // cost
  return colors.accent || ACCENT;
}

function oosHover(o?: OosBadge | null): string | null {
  if (!o) return null;
  const parts: string[] = [];
  if (o.windows_summary) parts.push(String(o.windows_summary));
  else if (o.note) parts.push(String(o.note));
  if (typeof o.oos_ratio === "number") {
    parts.push(`oos_ratio=${Math.round(o.oos_ratio * 100)}%`);
  }
  if (o.mode === "walk_forward") {
    parts.unshift("滚动");
  }
  return parts.length ? parts.join(" · ") : null;
}

export default function EvalBadges({
  cost_profile,
  valuation_pit,
  pead_pit,
  oos,
  colors,
}: EvalBadgesProps) {
  const items: { text: string; color: string }[] = [];
  const ct = costText(cost_profile);
  if (ct) items.push({ text: ct, color: tone("cost", cost_profile, colors) });
  const vt = valText(valuation_pit);
  if (vt) items.push({ text: vt, color: tone("val", valuation_pit, colors) });
  const pt = peadText(pead_pit);
  if (pt) items.push({ text: pt, color: tone("pead", pead_pit, colors) });
  const ot = oosText(oos);
  if (ot) items.push({ text: ot, color: tone("oos", oos, colors) });

  if (!items.length) return null;

  return (
    <div
      style={{
        display: "flex",
        flexWrap: "wrap",
        gap: 8,
        alignItems: "center",
      }}
      title={[
        cost_profile?.label,
        valuation_pit?.note,
        pead_pit?.note,
        oosHover(oos),
      ]
        .filter(Boolean)
        .join(" · ")}
    >
      {items.map((it) => (
        <Badge key={it.text} text={it.text} color={it.color} soft />
      ))}
    </div>
  );
}
