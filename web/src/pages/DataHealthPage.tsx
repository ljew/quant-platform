/**
 * 数据健康（数据中心 · Tab）：87 张表逐张看新鲜度 + 五层体检。
 *
 * 只回答一个问题：**今天的数据能不能用来做决策**。
 * 所以第一屏只有「几个 FAIL、几个 WARN」，不是一堆漂亮图表。
 *
 * 两个口径写在页面上，避免误读：
 *   · 时刻感知：交易日 19:00 前允许 1 天滞后（当天 ETL 还没跑），页面会显示 grace
 *   · 分档阈值：财报类 400 天、事件表 30 天、配置表不参与 —— 一刀切会误报一半
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type FrameworkHealth, type TableFreshness } from "../api/client";
import { useTheme } from "../theme";

const BAD = ["FAIL", "WARN"];

function statusColor(status: string, colors: ReturnType<typeof useTheme>["colors"]) {
  switch (status) {
    case "FAIL":
      return colors.up;
    case "WARN":
      return "#d97706";
    case "OK":
      return colors.down;
    default:
      return colors.muted;
  }
}

export default function DataHealthPage() {
  const { colors } = useTheme();
  const [fresh, setFresh] = useState<TableFreshness | null>(null);
  const [health, setHealth] = useState<FrameworkHealth | null>(null);
  const [onlyBad, setOnlyBad] = useState(true);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async (force = false) => {
    setLoading(true);
    setErr(null);
    try {
      const [f, h] = await Promise.all([api.tableFreshness(force), api.frameworkHealth()]);
      setFresh(f);
      setHealth(h);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const rows = useMemo(() => {
    const all = fresh?.tables ?? [];
    return onlyBad ? all.filter((r) => BAD.includes(r.status) || r.status === "EMPTY") : all;
  }, [fresh, onlyBad]);

  const counts = fresh?.counts ?? {};
  const worst = fresh?.worst ?? "OK";

  return (
    <div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 12, marginBottom: 14 }}>
        <h2 style={{ margin: 0, fontSize: 19 }}>数据健康</h2>
        <span style={{ color: colors.muted, fontSize: 12 }}>
          {fresh ? `${fresh.total} 张表 · 截至 ${fresh.as_of.replace("T", " ")}` : "加载中…"}
          {fresh && fresh.grace_days > 0 ? ` · 时刻感知宽限 ${fresh.grace_days} 天` : ""}
        </span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          <button onClick={() => setOnlyBad((v) => !v)}
            style={{
              padding: "5px 12px", fontSize: 12, cursor: "pointer",
              borderRadius: 6, border: `1px solid ${colors.border}`,
              background: onlyBad ? colors.accent : colors.card,
              color: onlyBad ? "#fff" : colors.text,
            }}>
            {onlyBad ? "只看异常" : "全部表"}
          </button>
          <button onClick={() => load(true)} disabled={loading}
            style={{
              padding: "5px 12px", fontSize: 12, cursor: "pointer",
              borderRadius: 6, border: `1px solid ${colors.border}`,
              background: colors.card, color: colors.text,
            }}>
            {loading ? "刷新中…" : "刷新"}
          </button>
        </div>
      </div>

      {err && (
        <div style={{
          background: colors.card, border: `1px solid ${colors.up}`, color: colors.up,
          borderRadius: 8, padding: "10px 12px", fontSize: 12.5, marginBottom: 14,
        }}>
          {err}
        </div>
      )}

      {/* KPI：只有四个数字，回答「今天能不能用数据」 */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12, marginBottom: 16 }}>
        {[
          { k: "FAIL", label: "断档", hint: "超过阈值，数据不能信" },
          { k: "WARN", label: "滞后", hint: "接近阈值，留意" },
          { k: "EMPTY", label: "空表", hint: "建了表没数据" },
          { k: "OK", label: "正常", hint: "在阈值内" },
        ].map((it) => (
          <div key={it.k} style={{
            background: colors.card, border: `1px solid ${colors.border}`,
            borderRadius: 10, padding: "12px 14px",
          }}>
            <div style={{ fontSize: 11, color: colors.muted }}>{it.label}</div>
            <div style={{
              fontSize: 24, fontWeight: 700, lineHeight: 1.25,
              color: statusColor(it.k, colors),
            }}>
              {counts[it.k] ?? 0}
            </div>
            <div style={{ fontSize: 10.5, color: colors.muted }}>{it.hint}</div>
          </div>
        ))}
      </div>

      {/* 五层体检 */}
      {health && (
        <div style={{
          background: colors.card, border: `1px solid ${colors.border}`,
          borderRadius: 10, padding: "12px 14px", marginBottom: 16,
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
            <b style={{ fontSize: 13 }}>五层体检</b>
            <span style={{ fontSize: 11, color: colors.muted }}>
              worst = <b style={{ color: statusColor(health.worst, colors) }}>{health.worst}</b>
              {" · "}{health.checked_at?.replace("T", " ").slice(0, 16)}
            </span>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 10 }}>
            {health.layers.map((l) => {
              const bad = (l.items ?? []).filter((i) => i.status !== "OK");
              return (
                <div key={l.key} style={{
                  border: `1px solid ${colors.border}`, borderRadius: 8, padding: "9px 10px",
                }}>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12 }}>
                    <span>{l.name}</span>
                    <b style={{ color: statusColor(l.status, colors) }}>{l.status}</b>
                  </div>
                  <div style={{ fontSize: 10.5, color: colors.muted, marginTop: 4 }}>
                    {bad.length === 0
                      ? "全部正常"
                      : bad.slice(0, 2).map((i) => `${i.label}：${i.detail}`).join("；")}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* 表级明细 */}
      <div style={{
        background: colors.card, border: `1px solid ${colors.border}`,
        borderRadius: 10, overflow: "hidden",
      }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
          <thead>
            <tr style={{ background: colors.tableStripe, color: colors.muted }}>
              {["状态", "表", "日期列", "行数", "最新", "滞后"].map((h) => (
                <th key={h} style={{ textAlign: "left", padding: "8px 12px", fontWeight: 500 }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.table} style={{ borderTop: `1px solid ${colors.border}` }}>
                <td style={{ padding: "7px 12px", color: statusColor(r.status, colors), fontWeight: 600 }}>
                  {r.status}
                </td>
                <td style={{ padding: "7px 12px", fontFamily: "ui-monospace,Menlo,monospace", fontSize: 11.5 }}>
                  {r.table}
                  {r.note ? <span style={{ color: colors.muted, marginLeft: 8, fontSize: 10.5 }}>{r.note}</span> : null}
                </td>
                <td style={{ padding: "7px 12px", color: colors.muted }}>{r.date_col ?? "—"}</td>
                <td style={{ padding: "7px 12px", textAlign: "right" }}>
                  {r.rows < 0 ? "—" : r.rows.toLocaleString("zh-CN")}
                </td>
                <td style={{ padding: "7px 12px", color: colors.muted }}>{r.latest ?? "—"}</td>
                <td style={{ padding: "7px 12px", textAlign: "right", color: colors.muted }}>
                  {r.days_ago == null ? "—" : `${r.days_ago} 天`}
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={6} style={{ padding: "20px 12px", textAlign: "center", color: colors.muted }}>
                  {onlyBad ? "没有异常表 —— 换「全部表」看完整清单" : "没有数据"}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div style={{ marginTop: 12, fontSize: 11.5, color: colors.muted, lineHeight: 1.9 }}>
        阈值按列语义分档：财报类（<code>ann_date</code>）400/800 天 · 事件/运行记录
        （<code>created_at</code>）30/90 天且 50 行以下算配置表不参与 · 基金/财务类 200/400 天 ·
        其余日频 1/4 天。<b style={{ color: colors.text }}>误报比不报更糟</b> ——
        一刀切时 10 个 FAIL 里 6 个是配置表和半年报。
      </div>
    </div>
  );
}
