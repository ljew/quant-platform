/**
 * 舆情策略卡：情绪择时 + 重点行业 + 重点个股。
 *
 * 老刘 2026-11-08：「直接做一个舆情信息的策略，包括重点行业和股票，情绪择时也可以有」。
 * 原来这个位置放的是「热点简报」（LLM 逐条解读新闻），那是**信息**；这里要的是**决策**。
 *
 * 三层逻辑：
 *   市场情绪分位 → 定仓位     （反向：极端悲观加仓、极端亢奋减仓）
 *   行业情绪排名 → 定方向     （个股情绪按提及数加权聚到行业）
 *   个股舆情强度 → 定标的     （情绪 × 讨论量的强度，附 5 日趋势）
 *
 * 配色遵循 A 股惯例：情绪偏多用红、偏空用绿。
 */
import { useCallback, useEffect, useState } from "react";
import { Badge, Card } from "./ui";
import { api, SentimentStrategy } from "../api/client";
import { useTheme } from "../theme";

/** 情绪值 → 颜色（+1 全多红、-1 全空绿、中间灰）。 */
function tone(v: number, up: string, down: string, muted: string): string {
  if (v > 0.15) return up;
  if (v < -0.15) return down;
  return muted;
}

const fmt = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined ? "—" : (v > 0 ? "+" : "") + v.toFixed(digits);

export default function SentimentStrategyCard({ dense = false }: { dense?: boolean }) {
  const { colors } = useTheme();
  const [data, setData] = useState<SentimentStrategy | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    setLoading(true);
    api
      .sentimentStrategy()
      .then((d) => setData(d && d.ok ? d : null))
      .catch(() => setData(null))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const t = data?.timing;
  const industries = data?.industries || [];
  const stocks = data?.stocks || [];
  const stanceTone =
    t?.stance === "悲观极致" || t?.stance === "偏冷"
      ? colors.up
      : t?.stance === "亢奋" || t?.stance === "偏热"
        ? colors.down
        : colors.muted;

  const meta = data?.asof ? (
    <span style={{ fontSize: 11.5, color: colors.muted, fontWeight: 400 }}>
      数据日 {data.asof} · 行业 {industries.length} 个 · 个股 {stocks.length} 只
    </span>
  ) : null;

  return (
    <Card
      title="舆情策略"
      colors={colors}
      extra={
        <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
          {meta}
          <button
            onClick={load}
            disabled={loading}
            style={{
              border: `1px solid ${colors.border}`, background: colors.card,
              color: colors.muted, borderRadius: 4, padding: "1px 7px",
              fontSize: 11, cursor: loading ? "default" : "pointer",
            }}
          >
            {loading ? "…" : "刷新"}
          </button>
        </span>
      }
    >
      {!data && (
        <div style={{ color: colors.muted, fontSize: 12.5, padding: "10px 2px" }}>
          {loading ? "加载中…" : "暂无足够的舆情数据（个股情绪需单日 ≥30 只）"}
        </div>
      )}

      {/* —— ① 情绪择时 —— */}
      {t?.ok && (
        <div
          style={{
            display: "flex", gap: 18, alignItems: "center", flexWrap: "wrap",
            padding: "10px 14px", borderRadius: 8, marginBottom: 12,
            background: colors.bg, border: `1px solid ${colors.border}`,
          }}
        >
          <div style={{ textAlign: "center", minWidth: 74 }}>
            <div
              className="num"
              style={{ fontSize: 30, fontWeight: 800, color: stanceTone, lineHeight: 1.1 }}
            >
              {t.position}%
            </div>
            <div style={{ fontSize: 10.5, color: colors.muted }}>建议仓位</div>
          </div>
          <div style={{ flex: 1, minWidth: 220 }}>
            <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 3 }}>
              情绪择时 ·{" "}
              <span style={{ color: stanceTone }}>{t.stance}</span>
              <span style={{ marginLeft: 8, fontSize: 11.5, color: colors.muted, fontWeight: 400 }}>
                当前 {fmt(t.value)} · 处于近 {t.n_days} 日 {t.pct}% 分位
              </span>
            </div>
            <div style={{ fontSize: 11.5, color: colors.muted, lineHeight: 1.7 }}>
              {t.comment}
            </div>
          </div>
          <div style={{ textAlign: "right", fontSize: 11, color: colors.muted, lineHeight: 1.8 }}>
            <div>5 日均 {fmt(t.ma5)}</div>
            <div>20 日均 {fmt(t.ma20)}</div>
          </div>
        </div>
      )}

      {/* —— ② 重点行业 + ③ 重点个股 —— */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: dense ? "1fr" : "1fr 1fr",
          gap: 14,
          alignItems: "start",
        }}
      >
        <div>
          <div style={{ fontSize: 12, color: colors.muted, marginBottom: 6 }}>
            重点行业（按情绪排序，提及数加权）
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {industries.map((x) => (
              <div
                key={x.industry}
                style={{
                  display: "flex", alignItems: "baseline", gap: 8,
                  fontSize: 12, padding: "3px 0",
                  borderBottom: `1px solid ${colors.border}`,
                }}
                title={
                  x.leaders.length
                    ? `代表股：${x.leaders.map((l) => `${l.name}(${fmt(l.net)})`).join("  ")}`
                    : ""
                }
              >
                <span style={{ fontWeight: 600, minWidth: 66 }}>{x.industry}</span>
                <span
                  className="num"
                  style={{
                    color: tone(x.net, colors.up, colors.down, colors.muted),
                    fontWeight: 600, minWidth: 48,
                  }}
                >
                  {fmt(x.net)}
                </span>
                <span style={{ color: colors.muted, minWidth: 52 }}>{x.mentions} 提及</span>
                <span
                  style={{
                    flex: 1, minWidth: 0, color: colors.muted, fontSize: 11,
                    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                  }}
                >
                  {x.leaders.map((l) => l.name).join(" / ")}
                </span>
              </div>
            ))}
            {!industries.length && (
              <div style={{ fontSize: 12, color: colors.muted }}>暂无行业数据</div>
            )}
          </div>
        </div>

        <div>
          <div style={{ fontSize: 12, color: colors.muted, marginBottom: 6 }}>
            重点个股（情绪 × 讨论量，带 5 日变化）
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
            {stocks.map((s) => (
              <div
                key={s.symbol}
                style={{
                  display: "flex", alignItems: "baseline", gap: 7, fontSize: 12,
                  padding: "3px 0", borderBottom: `1px solid ${colors.border}`,
                }}
              >
                <span style={{ fontWeight: 600, minWidth: 62 }}>{s.name}</span>
                <span style={{ color: colors.muted, fontSize: 10.5, minWidth: 50 }}>
                  {s.industry}
                </span>
                <span
                  className="num"
                  style={{
                    color: tone(s.net, colors.up, colors.down, colors.muted),
                    fontWeight: 600, minWidth: 44,
                  }}
                >
                  {fmt(s.net)}
                </span>
                <span style={{ color: colors.muted, minWidth: 40, fontSize: 11 }}>
                  {s.mentions} 提
                </span>
                {s.trend !== null && (
                  <Badge
                    text={`${s.trend > 0 ? "↑" : s.trend < 0 ? "↓" : "—"} ${fmt(Math.abs(s.trend))}`}
                    color={s.trend > 0 ? colors.up : s.trend < 0 ? colors.down : colors.muted}
                    soft
                  />
                )}
              </div>
            ))}
            {!stocks.length && (
              <div style={{ fontSize: 12, color: colors.muted }}>暂无个股数据</div>
            )}
          </div>
        </div>
      </div>

      <div style={{ marginTop: 10, fontSize: 11, color: colors.muted, lineHeight: 1.6 }}>
        {data?.note || ""}
        。「重点行业」的情绪由该行业个股情绪按提及数加权而来，「重点个股」按 情绪 × log(1+提及数)
        排序 —— 既要有讨论度，也要方向明确。择时是**反向**逻辑：舆情极端悲观时加仓、极端亢奋时减仓。
      </div>
    </Card>
  );
}
