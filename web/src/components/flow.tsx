import React from "react";
import { ThemeColors } from "../theme";

export function FlowCol({ title, children, colors }: {
  title: string; children: React.ReactNode;
  colors: { text: string; card: string; border: string; tableStripe: string };
}) {
  return (
    <div style={{ background: colors.tableStripe, borderRadius: 9, padding: "10px 12px", border: `1px solid ${colors.border}` }}>
      <div style={{ fontWeight: 600, fontSize: 12.5, marginBottom: 8 }}>{title}</div>
      {children}
    </div>
  );
}

export function LayerRow({ label, v, colors }: { label: string; v: string; colors: { muted: string } }) {
  return (
    <div style={{ fontSize: 11.5, marginBottom: 5, lineHeight: 1.4 }}>
      {label}
      <span className="num" style={{ color: colors.muted, float: "right" }}>{v}</span>
    </div>
  );
}

export function Arrow() {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "center" }}>
      <svg width="20" height="20" viewBox="0 0 24 24">
        <path d="M4 12h13m-4 -4l4 4l-4 4" fill="none" stroke="#8b93a7" strokeWidth="2"
          strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </div>
  );
}

export type _CT = ThemeColors;
