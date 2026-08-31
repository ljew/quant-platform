import { useCallback, useEffect, useState } from "react";
import { api, HealthReport, LineageReport } from "../api/client";
import { Badge, Card, KpiCard, PageHeader } from "../components/ui";
import { useTheme, ThemeColors } from "../theme";

/** 数据管道页：数据源头 → 处理内容 → 任务执行 → 数据健康度。 */

const STATUS_COLOR = (s: string, c: { down: string; up: string; muted: string; accent: string; border: string }) =>
  s === "OK" || s === "SUCCESS" ? c.down : s === "FAIL" || s === "FAILED" ? c.up : s === "RUNNING" ? c.accent : c.muted;

export default function DataPipelinePage() {
  const { colors } = useTheme();
  const [lin, setLin] = useState<LineageReport | null>(null);
  const [health, setHealth] = useState<HealthReport | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setLin(await api.monitorLineage());
      setHealth(await api.monitorHealth());
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 30000);
    return () => clearInterval(t);
  }, [refresh]);

  if (!lin) return <div style={{ padding: 24, color: "#888" }}>加载数据管道视图…{error}</div>;

  const gold = lin.layers.gold;

  return (
    <div style={{ maxWidth: 1320, margin: "0 auto" }}>
      <PageHeader title="数据管道" desc="数据源 · 处理内容 · 任务执行 · 健康度（30s 自动刷新）"
        actions={
          <>
            <button
              onClick={async () => {
                setBusy(true);
                try {
                  const r = await fetch("/api/v1/monitor/pipeline/run", { method: "POST" }).then((x) => x.json());
                  if (!r.ok) window.alert(r.error || "已有任务在运行");
                  else setTimeout(refresh, 2500);
                } finally {
                  setBusy(false);
                }
              }}
              disabled={busy}
              style={{
                padding: "6px 14px", borderRadius: 7, border: `1px solid ${colors.accent}`,
                background: `${colors.accent}14`, color: colors.accent,
                cursor: busy ? "wait" : "pointer", fontSize: 12.5,
              }}
            >
              ▶ 立即运行
            </button>
            <span style={{ fontSize: 11.5, color: colors.muted }}>
              最近运行 #{lin.last_run?.run_id} · {lin.last_run?.status} · {lin.last_run?.started_at?.slice(0, 16).replace("T", " ")}
            </span>
          </>
        }
      />

      {/* —— ① 数据源头卡片 —— */}
      <Card title="① 数据源头" colors={colors} extra={<span style={{ fontSize: 11.5, color: colors.muted }}>配置文件 {lin.config_path}</span>}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(250px,1fr))", gap: 10 }}>
          {lin.sources.map((s) => {
            const lr = s.last_run;
            return (
              <div key={s.name} style={{
                background: colors.tableStripe, borderRadius: 9, padding: "10px 12px",
                border: `1px solid ${s.enabled ? colors.border : colors.up}66`,
              }}>
                <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
                  <span style={{ color: s.enabled ? colors.down : colors.up, fontSize: 13 }}>●</span>
                  <b style={{ fontSize: 13 }}>{s.name}</b>
                  <span style={{ flex: 1 }} />
                  <Badge text={s.enabled ? "启用" : "停用"} color={s.enabled ? colors.down : colors.up} soft />
                </div>
                <div style={{ fontSize: 11.5, color: colors.muted, marginBottom: 5 }}>{s.description}</div>
                <div style={{ fontSize: 11, color: colors.muted }}>
                  类型 <code>{s.type}</code> · 产出 {s.produces || s.layer}
                </div>
                {Object.keys(s.params).length > 0 && (
                  <div style={{ fontSize: 10.5, color: colors.muted, marginTop: 3, wordBreak: "break-all" }}>
                    参数 {JSON.stringify(s.params).slice(0, 90)}
                  </div>
                )}
                <div style={{ fontSize: 11, marginTop: 6, display: "flex", gap: 8, alignItems: "center" }}>
                  <span style={{ color: STATUS_COLOR(lr?.status || "未执行", colors), fontWeight: 600 }}>
                    {lr ? lr.status : "未执行"}
                  </span>
                  {lr && (
                    <span className="num" style={{ color: colors.muted }}>
                      {lr.rows.toLocaleString()} 行 · {lr.duration_sec}s
                    </span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </Card>

      {/* —— ② 血缘流向图 —— */}
      <Card title="② 数据血缘流向" colors={colors} style={{ marginTop: 14 }}>
        <LineageGraph lin={lin} colors={colors} />
      </Card>

      {/* —— ③ 处理步骤流水线 —— */}
      <Card title="③ 当前处理的内容（步骤流水线）" colors={colors} style={{ marginTop: 14 }} pad={0}>
        {lin.steps.map((s) => (
          <div key={s.name} style={{
            display: "flex", alignItems: "center", gap: 12, padding: "7px 16px",
            borderBottom: `1px solid ${colors.border}`,
          }}>
            <span className="num" style={{ width: 24, color: colors.muted, fontSize: 12 }}>{s.order}</span>
            <span style={{ width: 15, color: STATUS_COLOR(s.status, colors) }}>
              {s.status === "OK" ? "●" : s.status === "FAIL" ? "✕" : "○"}
            </span>
            <span style={{ width: 210, fontSize: 12.5 }}>{s.name}</span>
            <span className="num" style={{ fontSize: 12, color: colors.muted }}>
              {s.rows > 0 ? `${s.rows.toLocaleString()} 行` : "—"}
            </span>
            <span style={{ flex: 1 }} />
            <span className="num" style={{ fontSize: 12, color: colors.muted }}>{s.duration_sec}s</span>
          </div>
        ))}
      </Card>

      {/* —— ④ 任务执行时间线 —— */}
      <Card title="④ 任务执行（最近 6 次运行）" colors={colors} style={{ marginTop: 14 }}>
        {lin.timeline.map((r) => {
          const maxSec = Math.max(...r.steps.map((s) => s.duration_sec), 0.1);
          return (
            <div key={r.run_id} style={{ marginBottom: 12 }}>
              <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 5 }}>
                <Badge text={r.status} color={STATUS_COLOR(r.status, colors)} soft />
                <span className="num" style={{ fontSize: 12.5, fontWeight: 600 }}>#{r.run_id}</span>
                <span style={{ fontSize: 11.5, color: colors.muted }}>
                  {r.trigger} · {r.started_at?.slice(5, 16).replace("T", " ")} · 耗时 {r.total_sec}s
                </span>
                {r.error && <span style={{ fontSize: 11, color: colors.up }}>{r.error.slice(0, 70)}</span>}
              </div>
              <div style={{ display: "flex", gap: 3, alignItems: "flex-end", height: 26 }}>
                {r.steps.map((s) => (
                  <div key={s.name}
                    title={`${s.name} · ${s.duration_sec}s · ${s.rows}行`}
                    style={{
                      flex: 1, minWidth: 26, maxWidth: 110,
                      height: Math.max(6, (s.duration_sec / maxSec) * 26),
                      background: STATUS_COLOR(s.status, colors),
                      opacity: 0.75, borderRadius: 3,
                    }}
                  />
                ))}
              </div>
            </div>
          );
        })}
      </Card>

      {/* —— ⑤ 数据健康度 —— */}
      {health && (
        <Card title="⑤ 数据健康度" colors={colors} style={{ marginTop: 14 }}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(140px,1fr))", gap: 10, marginBottom: 12 }}>
            <KpiCard label="综合健康分" value={String(health.overall_score)} sub={health.overall_status}
              tone={health.overall_status === "healthy" ? "down" : health.overall_status === "warn" ? "accent" : "up"} colors={colors} />
            {Object.entries(health.layers).map(([k, ly]) => (
              <KpiCard key={k} label={ly.label} value={String(ly.score)}
                tone={ly.status === "healthy" ? "down" : ly.status === "warn" ? "accent" : "up"} colors={colors} />
            ))}
            <KpiCard label="告警数" value={String(health.alerts.length)} sub="未通过检查项"
              tone={health.alerts.length ? "up" : "down"} colors={colors} />
          </div>
          {health.alerts.length > 0 && (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {health.alerts.map((a, i) => (
                <span key={i} style={{
                  fontSize: 11.5, padding: "3px 10px", borderRadius: 6,
                  background: a.level === "error" ? `${colors.up}12` : `${colors.accent}12`,
                  border: `1px solid ${a.level === "error" ? colors.up : colors.accent}`,
                }}>
                  {a.layer} · {a.check}：{a.detail}
                </span>
              ))}
            </div>
          )}
        </Card>
      )}
    </div>
  );
}

/* ============ 血缘图（SVG） ============ */
function LineageGraph({ lin, colors }: { lin: LineageReport; colors: ThemeColors }) {
  const c = colors;
  const box = (x: number, y: number, w: number, h: number, title: string, sub: string | number | null, tone: string) => (
    <g key={title}>
      <rect x={x} y={y} width={w} height={h} rx={8}
        fill={c.card} stroke={tone} strokeWidth={1.2} />
      <text x={x + 10} y={y + 20} fill={c.text} fontSize={12} fontWeight={600}>{title}</text>
      <text x={x + 10} y={y + 38} fill={c.muted} fontSize={10.5}>{sub ?? ""}</text>
    </g>
  );

  const bronzeTotal = Object.values(lin.layers.bronze).reduce((n, v) => n + v.files, 0);
  const silverTotal = Object.keys(lin.layers.silver.files).length;
  const W = 1100;
  const colW = 200;
  const arrow = (x1: number, y: number, x2: number) => (
    <g key={`a${x1}-${x2}`} stroke={c.muted} strokeWidth={1.4} fill="none">
      <line x1={x1} y1={y} x2={x2 - 8} y2={y} />
      <path d={`M${x2 - 10} ${y - 4} L${x2} ${y} L${x2 - 10} ${y + 4}`} fill={c.muted} stroke="none" />
    </g>
  );

  return (
    <div style={{ overflowX: "auto" }}>
      <svg viewBox={`0 0 ${W} 300`} width="100%" height={300}>
        {/* 列标题 */}
        {["数据源", "Bronze 原始层", "Silver 清洗/打分", "Gold 因子层", "应用"].map((t, i) => (
          <text key={t} x={20 + i * 220} y={14} fill={c.muted} fontSize={11} fontWeight={600}>{t}</text>
        ))}

        {/* 数据源节点 */}
        {lin.sources.map((s, i) => {
          const y = 30 + i * 62;
          return (
            <g key={s.name}>
              {box(20, y, colW, 48, s.name,
                `${s.enabled ? "启用" : "停用"} · ${s.last_run ? s.last_run.status : "未执行"}`,
                s.enabled ? c.down : c.up)}
              {arrow(220, y + 24, 262)}
            </g>
          );
        })}

        {/* Bronze */}
        {box(264, 60, colW, 66, "原始快照 Parquet",
          `${bronzeTotal} 文件 · ${Object.entries(lin.layers.bronze).map(([k, v]) => `${k}:${v.size_mb}MB`).join(" / ")}`, c.accent)}
        {arrow(464, 93, 506)}

        {/* Silver */}
        {box(508, 60, colW, 66, "清洗 + 情绪打分",
          `${silverTotal} 文件 · 质检${lin.layers.silver.quality ? "已出" : "无"}`, c.accent)}
        {arrow(708, 93, 750)}

        {/* Gold */}
        {box(752, 40, colW, 106, "因子与情绪表",
          `K线 ${(lin.layers.gold.tables.kline_daily || 0).toLocaleString()} 行`, c.accent)}
        <text x={762} y={82} fill={c.muted} fontSize={10.5}>因子 {(lin.layers.gold.tables.factor_daily || 0).toLocaleString()} 行</text>
        <text x={762} y={98} fill={c.muted} fontSize={10.5}>情绪 市场 {lin.layers.gold.tables.news_market_daily} 天</text>
        <text x={762} y={114} fill={c.muted} fontSize={10.5}>注册因子 {lin.layers.gold.registry_enabled} 个启用</text>
        <text x={762} y={132} fill={c.up} fontSize={10.5}>最新 K线 {lin.layers.gold.latest.kline} / 新闻 {lin.layers.gold.latest.news ?? "—"}</text>
        {arrow(952, 93, 994)}

        {/* 应用 */}
        {box(996, 60, 84, 66, "回测", "组合/选股", c.muted)}
      </svg>
    </div>
  );
}
