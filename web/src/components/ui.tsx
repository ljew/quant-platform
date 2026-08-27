import { ReactNode } from "react";
import { ThemeColors } from "../theme";

/** 投研平台设计系统：统一卡片/指标/徽章/页头。 */

export function Card({
  title,
  extra,
  children,
  colors,
  pad = 16,
  style,
}: {
  title?: ReactNode;
  extra?: ReactNode;
  children: ReactNode;
  colors: { card: string; border: string; text: string; muted: string };
  pad?: number;
  style?: React.CSSProperties;
}) {
  return (
    <div
      style={{
        background: colors.card,
        borderRadius: 12,
        border: `1px solid ${colors.border}`,
        boxShadow: "0 1px 3px rgba(15,23,42,.04)",
        overflow: "hidden",
        ...style,
      }}
    >
      {(title || extra) && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            padding: "12px 16px",
            borderBottom: `1px solid ${colors.border}`,
          }}
        >
          <span style={{ fontWeight: 600, fontSize: 14 }}>{title}</span>
          {extra}
        </div>
      )}
      <div style={{ padding: pad }}>{children}</div>
    </div>
  );
}

export function PageHeader({
  title,
  desc,
  actions,
}: {
  title: string;
  desc?: string;
  actions?: ReactNode;
}) {
  return (
    <div style={{ display: "flex", alignItems: "flex-end", justifyContent: "space-between", marginBottom: 18 }}>
      <div>
        <h1 style={{ fontSize: 21, margin: 0, fontWeight: 700, letterSpacing: 0.5 }}>{title}</h1>
        {desc && <div style={{ fontSize: 13, color: "#7a8299", marginTop: 5 }}>{desc}</div>}
      </div>
      {actions && <div style={{ display: "flex", gap: 10, alignItems: "center" }}>{actions}</div>}
    </div>
  );
}

export function KpiCard({
  label,
  value,
  sub,
  tone = "neutral",
  colors,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "up" | "down" | "neutral" | "accent";
  colors: { card: string; border: string; muted: string; up: string; down: string; accent: string; text: string };
}) {
  const color =
    tone === "up" ? colors.up : tone === "down" ? colors.down : tone === "accent" ? colors.accent : colors.text;
  return (
    <div
      style={{
        background: colors.card,
        borderRadius: 10,
        padding: "12px 16px",
        border: `1px solid ${colors.border}`,
        borderTop: `2px solid ${color === colors.text ? colors.border : color}`,
      }}
    >
      <div style={{ fontSize: 11.5, color: colors.muted, marginBottom: 4 }}>{label}</div>
      <div
        style={{
          fontSize: 22,
          fontWeight: 700,
          color,
          fontVariantNumeric: "tabular-nums",
          fontFamily: "'SF Mono', Menlo, Consolas, monospace",
          letterSpacing: -0.5,
        }}
      >
        {value}
      </div>
      {sub && <div style={{ fontSize: 11.5, color: colors.muted, marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

export function Badge({
  text,
  color,
  soft,
}: {
  text: string;
  color: string;
  soft?: boolean;
}) {
  return (
    <span
      style={{
        display: "inline-block",
        padding: "2px 11px",
        borderRadius: 999,
        fontSize: 12,
        fontWeight: 600,
        color: soft ? color : "#fff",
        background: soft ? `${color}1f` : color,
        letterSpacing: 0.5,
      }}
    >
      {text}
    </span>
  );
}

export function Btn({
  onClick,
  children,
  kind = "primary",
  disabled,
  small,
}: {
  onClick?: () => void;
  children: ReactNode;
  kind?: "primary" | "ghost" | "warning";
  disabled?: boolean;
  small?: boolean;
}) {
  const styles: Record<string, React.CSSProperties> = {
    primary: { background: "linear-gradient(135deg,#2456c8,#3a72e6)", color: "#fff", border: 0 },
    ghost: { background: "transparent", color: "inherit", border: "1px solid currentColor", opacity: 0.75 },
    warning: { background: "linear-gradient(135deg,#c8860d,#e8a520)", color: "#fff", border: 0 },
  };
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      style={{
        ...styles[kind],
        padding: small ? "4px 12px" : "8px 22px",
        borderRadius: 7,
        cursor: disabled ? "not-allowed" : "pointer",
        fontSize: small ? 12 : 13.5,
        opacity: disabled ? 0.55 : 1,
        fontWeight: 500,
      }}
    >
      {children}
    </button>
  );
}

export const inputStyle = (c: { text: string; card: string; border: string }) => ({
  width: "100%",
  padding: "7px 10px",
  borderRadius: 7,
  border: `1px solid ${c.border}`,
  background: c.card,
  color: c.text,
  boxSizing: "border-box" as const,
  fontSize: 13.5,
});
