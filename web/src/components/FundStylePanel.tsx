/**
 * 风格穿透面板（嵌入基金穿透页）
 *
 * 行业穿透回答「拿什么行业」，这里回答「拿什么风格」。
 * 五个因子全部由持仓加权到个股因子得出 —— 不经净值反推，所以每一分暴露
 * 都能追溯到具体股票，这是它与「净值风格回归」类产品的根本区别。
 *
 * 三层门槛都写在后端（权益类白名单 / 规模≥5000万 / 覆盖≥20%），
 * 页面只负责显示后端给出的可靠性判定，不自行放宽。
 */
import { useEffect, useState } from "react";
import { Card } from "./ui";
import { get } from "../api/client";
import { useTheme } from "../theme";

interface StyleFactor {
  factor: string; label: string; n: number;
  p10: number | null; p25: number | null; median: number | null;
  p75: number | null; p90: number | null;
}
interface StyleSummary {
  ok: boolean; error?: string;
  end_date: string; available_periods: string[]; n_funds: number;
  tags: { tag: string; n: number }[];
  factors: StyleFactor[];
  coverage: { median: number | null; p10: number | null; disclosed_median: number | null };
}
interface DriftItem {
  code: string; name: string; tag_prev: string; tag_now: string; changed: boolean;
  main: { factor: string; label: string; value: number };
  delta: Record<string, number>; magnitude: number; w_cover: number;
}
interface DriftResp {
  ok: boolean; error?: string;
  end_date: string; prev_date: string; n_scanned: number; items: DriftItem[];
}

const TAG_COLOR: Record<string, string> = {
  大盘成长: "#2456c8", 大盘价值: "#0f766e",
  小盘成长: "#7c3aed", 小盘价值: "#b45309",
  规模过小: "#9ca3af", 未定: "#d1d5db",
};

/** 因子条：把 p10~p90 映射到固定宽度，中位点标出来。 */
function FactorBar({ f, colors }: {
  f: StyleFactor; colors: { text: string; muted: string; border: string };
}) {
  const lo = f.p10 ?? -2, hi = f.p90 ?? 2;
  const span = hi - lo || 1;
  const pos = (v: number | null) =>
    v == null ? 50 : Math.max(0, Math.min(100, ((v - lo) / span) * 100));
  const zero = ((0 - lo) / span) * 100;
  return (
    <div style={{ display: "grid", gridTemplateColumns: "52px 1fr 66px",
                  alignItems: "center", gap: 10, marginBottom: 7 }}>
      <span style={{ fontSize: 12, color: colors.text }}>{f.label}</span>
      <div style={{ position: "relative", height: 16,
                    background: "#f1f5f9", borderRadius: 3 }}>
        {/* p25~p75 箱体 */}
        <div style={{ position: "absolute", top: 4, bottom: 4,
                      left: `${pos(f.p25)}%`, width: `${Math.max(1, pos(f.p75) - pos(f.p25))}%`,
                      background: "#cbd5e1", borderRadius: 2 }} />
        {/* 零点参考线 */}
        <div style={{ position: "absolute", top: 1, bottom: 1, left: `${zero}%`,
                      width: 1, background: "#94a3b8" }} />
        {/* 中位点 */}
        <div style={{ position: "absolute", top: 2, width: 3, height: 12,
                      left: `calc(${pos(f.median)}% - 1.5px)`,
                      background: "#2456c8", borderRadius: 2 }} />
      </div>
      <span style={{ fontSize: 11, color: colors.muted, textAlign: "right",
                     fontVariantNumeric: "tabular-nums" }}>
        {f.median == null ? "—" : (f.median >= 0 ? "+" : "") + f.median.toFixed(2)}
      </span>
    </div>
  );
}

export default function FundStylePanel() {
  const { colors } = useTheme();
  const [sum, setSum] = useState<StyleSummary | null>(null);
  const [drift, setDrift] = useState<DriftResp | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    get<StyleSummary>("/fund/style/summary")
      .then((d) => { if (d.ok) setSum(d); else setErr(d.error || "风格数据尚未生成"); })
      .catch((e) => setErr((e as Error).message));
    get<DriftResp>("/fund/style/drift?min_abs=1.0&limit=12")
      .then((d) => { if (d.ok) setDrift(d); })
      .catch(() => { /* 漂移榜缺失不阻塞总览 */ });
  }, []);

  const total = sum?.tags.reduce((a, t) => a + t.n, 0) || 0;

  return (
    <Card
      title="风格穿透：持仓 → 个股 → 五因子"
      colors={colors}
      extra={sum && (
        <span style={{ fontSize: 12, color: colors.muted }}>
          {sum.end_date} · {sum.n_funds.toLocaleString()} 只权益基金 ·
          覆盖中位 {sum.coverage.median ?? "—"}%
        </span>
      )}
    >
      {err && <div style={{ fontSize: 13, color: "#b91c1c" }}>{err}</div>}

      {sum && (
        <>
          {/* 标签分布 */}
          <div style={{ display: "flex", height: 26, borderRadius: 4,
                        overflow: "hidden", marginBottom: 6 }}>
            {sum.tags.map((t) => (
              <div key={t.tag}
                title={`${t.tag} ${t.n.toLocaleString()} 只`}
                style={{ width: `${(t.n / total) * 100}%`,
                         background: TAG_COLOR[t.tag] || "#cbd5e1",
                         display: "flex", alignItems: "center", justifyContent: "center",
                         fontSize: 10, color: "#fff", whiteSpace: "nowrap",
                         overflow: "hidden" }}>
                {(t.n / total) > 0.07 ? t.tag : ""}
              </div>
            ))}
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 12,
                        fontSize: 11, color: colors.muted, marginBottom: 14 }}>
            {sum.tags.map((t) => (
              <span key={t.tag}>
                <i style={{ display: "inline-block", width: 8, height: 8,
                            borderRadius: 2, marginRight: 4,
                            background: TAG_COLOR[t.tag] || "#cbd5e1" }} />
                {t.tag} {t.n.toLocaleString()}
              </span>
            ))}
            <span style={{ marginLeft: "auto" }}>
              规模 &lt; 5000 万或覆盖不足的基金不给结论
            </span>
          </div>

          {/* 五因子分位：按列填充，左列 size/value/momentum，右列 volatility/quality */}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr",
                        gridTemplateRows: "repeat(3, auto)", gridAutoFlow: "column",
                        gap: "0 28px", marginBottom: 16 }}>
            {sum.factors.map((f) => (
              <div key={f.factor}>
                <FactorBar f={f} colors={colors} />
              </div>
            ))}
          </div>
        </>
      )}

      {/* 漂移榜 */}
      {drift?.items?.length ? (
        <div>
          <div style={{ fontSize: 12, color: colors.muted, marginBottom: 8,
                        borderTop: `1px solid ${colors.border}`, paddingTop: 12 }}>
            风格漂移榜 · {drift.prev_date} → {drift.end_date} ·
            合规样本 {drift.n_scanned.toLocaleString()} 只（幅度 ≥1.0σ）
          </div>
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
              <thead>
                <tr style={{ color: colors.muted, textAlign: "left" }}>
                  <th style={{ padding: "5px 8px", fontWeight: 500 }}>代码</th>
                  <th style={{ padding: "5px 8px", fontWeight: 500 }}>名称</th>
                  <th style={{ padding: "5px 8px", fontWeight: 500 }}>上期</th>
                  <th style={{ padding: "5px 8px", fontWeight: 500 }}>本期</th>
                  <th style={{ padding: "5px 8px", fontWeight: 500 }}>主驱动</th>
                  <th style={{ padding: "5px 8px", fontWeight: 500, textAlign: "right" }}>幅度</th>
                </tr>
              </thead>
              <tbody>
                {drift.items.map((x) => (
                  <tr key={x.code} style={{ borderTop: `1px solid ${colors.border}` }}>
                    <td style={{ padding: "5px 8px", color: colors.muted,
                                 fontVariantNumeric: "tabular-nums" }}>{x.code}</td>
                    <td style={{ padding: "5px 8px", color: colors.text }}>{x.name || "—"}</td>
                    <td style={{ padding: "5px 8px", color: colors.muted }}>{x.tag_prev}</td>
                    <td style={{ padding: "5px 8px",
                                 color: x.changed ? "#b91c1c" : colors.text,
                                 fontWeight: x.changed ? 600 : 400 }}>{x.tag_now}</td>
                    <td style={{ padding: "5px 8px", color: colors.muted }}>
                      {x.main.label} {x.main.value > 0 ? "+" : ""}{x.main.value.toFixed(2)}
                    </td>
                    <td style={{ padding: "5px 8px", textAlign: "right",
                                 color: colors.text, fontVariantNumeric: "tabular-nums" }}>
                      {x.magnitude.toFixed(2)}σ
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div style={{ fontSize: 11, color: colors.muted, marginTop: 8, lineHeight: 1.6 }}>
            幅度 = 五个因子坐标变化的最大绝对值（σ）。坐标以最新期基金池为基准，
            跨期可比；上期与本期均只用真实披露部分计算，不含补齐推测。
          </div>
        </div>
      ) : null}
    </Card>
  );
}
