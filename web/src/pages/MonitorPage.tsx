import { useCallback, useEffect, useRef, useState } from "react";
import { api, DataflowReport, HealthReport, MonitorStatus } from "../api/client";
import { Badge, Card } from "../components/ui";
import { Arrow, FlowCol, LayerRow } from "../components/flow";
import type { ThemeColors } from "../theme";
import { useTheme } from "../theme";
import { PageHeader } from "../components/ui";

/** 平台监控页：一眼结论 → 告警 → 数据管道全景 → 运行记录。 */

export default function MonitorPage() {
  const [data, setData] = useState<MonitorStatus | null>(null);
  const [error, setError] = useState("");
  const [health, setHealth] = useState<HealthReport | null>(null);
  const [dataflow, setDataflow] = useState<DataflowReport | null>(null);
  const [lastRefresh, setLastRefresh] = useState("");
  const [showDetail, setShowDetail] = useState(false);
  const [running, setRunning] = useState(false);
  const alarmRef = useRef<HTMLDivElement>(null);
  const { colors } = useTheme();

  const refresh = useCallback(async () => {
    try {
      const d = await api.monitor();
      setHealth(await api.monitorHealth());
      api.monitorDataflow().then(setDataflow).catch(() => {});
      setData(d);
      setLastRefresh(new Date().toLocaleTimeString());
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 30000);
    return () => clearInterval(t);
  }, [refresh]);

  if (!data) return <div style={{ padding: 24, color: "#888" }}>加载监控数据…{error}</div>;
  const st = data.services;
  const fresh = health?.layers;

  return (
    <div style={{ maxWidth: 1280, margin: "0 auto" }}>
      <PageHeader title="系统监控" desc="健康度 · 告警 · 数据管道全景 · 任务执行"
        actions={
          <>
            <button
              onClick={async () => {
                try {
                  const r = await fetch("/api/v1/monitor/pipeline/run", { method: "POST" }).then((x) => x.json());
                  if (!r.ok) window.alert(r.error || "已在运行");
                  else setTimeout(refresh, 2500);
                } catch (e) {
                  window.alert(`触发失败: ${(e as Error).message}`);
                }
              }}
              disabled={running}
              style={{
                padding: "6px 14px", borderRadius: 7, border: `1px solid ${colors.accent}`,
                background: running ? colors.tableStripe : `${colors.accent}14`,
                color: colors.accent, cursor: running ? "wait" : "pointer", fontSize: 12.5,
              }}
            >
              ▶ 立即运行管道
            </button>
            <span style={{ fontSize: 11.5, color: colors.muted }}>刷新于 {lastRefresh}（30s 自动）</span>
          </>
        }
      />
      {error && <div style={{ color: colors.up, marginBottom: 10 }}>{error}</div>}

      {/* —— 第一屏：一句话结论 + 三个数据新鲜度大字 —— */}
      {health && (
        <Card colors={colors} style={{ marginBottom: 14 }}>
          <div style={{ display: "grid", gridTemplateColumns: "1.1fr 2fr 0.9fr", gap: 18, alignItems: "center" }}>
            {/* 结论 */}
            <div>
              <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                <span className="num" style={{
                  fontSize: 44, fontWeight: 800,
                  color: health.overall_status === "healthy" ? colors.down : health.overall_status === "warn" ? "#e8a520" : colors.up,
                }}>
                  {health.overall_score}
                </span>
                <div>
                  <div style={{ fontWeight: 700, fontSize: 16 }}>
                    {health.overall_status === "healthy" ? "系统运行正常"
                      : health.overall_status === "warn" ? "存在需要关注的告警"
                        : "系统状态异常"}
                  </div>
                  <div style={{ fontSize: 12, color: colors.muted }}>综合健康度 / 100</div>
                </div>
              </div>
              {/* 三层迷你条 */}
              <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
                {Object.entries(health.layers).map(([k, ly]) => (
                  <span key={k} style={{ fontSize: 11.5 }}>
                    <b className="num" style={{ color: ly.status === "healthy" ? colors.down : ly.status === "warn" ? "#e8a520" : colors.up }}>{ly.score}</b> {ly.label}
                  </span>
                ))}
              </div>
            </div>
            {/* 数据新鲜度 */}
            <FreshGrid dataflow={dataflow} colors={colors} />
            {/* 告警入口 */}
            <div ref={alarmRef}>
              <AlarmSummary health={health} colors={colors}
                onJump={() => setShowDetail(true)} />
            </div>
          </div>
          {/* 明细折叠 */}
          {showDetail && (
            <div style={{ borderTop: `1px solid ${colors.border}`, marginTop: 12 }}>
              {Object.entries(health.layers).map(([k, ly]) => (
                <div key={k}>
                  <div style={{ padding: "6px 16px", background: colors.tableStripe, fontWeight: 600, fontSize: 12 }}>{ly.label}</div>
                  {(ly.checks || []).map((c) => (
                    <div key={c.name} style={{ display: "flex", alignItems: "center", gap: 10, padding: "5px 16px", fontSize: 12.5, borderBottom: `1px solid ${colors.border}` }}>
                      <span style={{ color: c.status === "ok" ? colors.down : c.status === "error" ? colors.up : "#e8a520", width: 14 }}>{c.status === "ok" ? "●" : c.status === "warn" ? "◐" : "✕"}</span>
                      <span style={{ width: 190 }}>{c.name}</span>
                      <span className="num" style={{ flex: 1, color: colors.muted }}>{c.value}</span>
                      <span style={{ color: colors.muted }}>期望: {c.expect}</span>
                    </div>
                  ))}
                </div>
              ))}
              <button onClick={() => setShowDetail(false)}
                style={{ margin: 8, fontSize: 12, color: colors.accent, background: "none", border: 0, cursor: "pointer" }}>
                收起明细
              </button>
            </div>
          )}
          {!showDetail && (
            <button onClick={() => setShowDetail(true)}
              style={{ margin: "4px 0", padding: "6px 0", width: "100%", borderTop: `1px solid ${colors.border}`, fontSize: 12, color: colors.accent, background: "none", borderLeft: 0, borderRight: 0, borderBottom: 0, cursor: "pointer" }}>
              查看全部 {Object.values(health.layers).reduce((n, ly) => n + (ly.checks?.length || 0), 0)} 项检查明细
            </button>
          )}
        </Card>
      )}

      {/* —— 第二层：两栏 = 最近运行 + 系统服务 —— */}
      <div style={{ display: "grid", gridTemplateColumns: "1.3fr 1fr", gap: 14 }}>
        {/* 管道运行记录 */}
        {st.pipeline && st.pipeline.runs.length > 0 && (
          <Card title={`最近管道执行（${st.pipeline.runs.length} 次）`} colors={colors} pad={0}>
            {st.pipeline.runs.map((r) => (
              <div key={r.run_id} style={{ padding: "9px 14px", borderBottom: `1px solid ${colors.border}` }}>
                <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
                  <Badge text={r.status === "SUCCESS" ? "成功" : r.status === "FAILED" ? "失败" : r.status}
                    color={r.status === "SUCCESS" ? colors.down : r.status === "FAILED" ? colors.up : colors.accent} soft />
                  <span className="num" style={{ fontSize: 13, fontWeight: 600 }}>#{r.run_id}</span>
                  <span style={{ fontSize: 11.5, color: colors.muted }}>{r.trigger} · {r.started_at}{r.finished_at ? ` → ${r.finished_at.slice(11)}` : ""}</span>
                  {r.error && <span style={{ fontSize: 11, color: colors.up }}>{r.error.slice(0, 60)}</span>}
                </div>
                <div style={{ display: "flex", gap: 5, flexWrap: "wrap", marginTop: 5 }}>
                  {(r.steps || []).map((s) => (
                    <span key={s.name} style={{
                      fontSize: 11, padding: "2px 8px", borderRadius: 5,
                      background: s.status === "FAIL" ? `${colors.up}14` : colors.tableStripe,
                      border: `1px solid ${s.status === "FAIL" ? colors.up : colors.border}`,
                    }}>
                      {s.name} · {s.duration_sec}s{s.rows > 0 ? ` · ${s.rows.toLocaleString()}行` : ""}
                    </span>
                  ))}
                </div>
              </div>
            ))}
          </Card>
        )}
        {/* 系统服务 */}
        <Panel title="⚙ 系统服务" colors={colors}>
          <BigNum label="回测任务" v={`${st.tasks.running}`} sub="运行中" tone={st.tasks.running > 0 ? "accent" : "neutral"} colors={colors} />
          <BigNum label="模拟盘任务" v={`${st.paper.enabled}/${st.paper.tasks}`} sub="启用/总数" tone="neutral" colors={colors} />
          <div style={{ marginTop: 14, fontSize: 12, color: colors.muted }}>
            后端启动 {fmtUptime(data.server.uptime_sec)} · {data.server.db} · 30s 自动刷新
          </div>
        </Panel>
      </div>

      {/* —— 第三层：数据管道全景 —— */}
      {dataflow && (
        <Card title="数据从哪里来 · 怎么处理 · 最新到哪" colors={colors} style={{ marginTop: 14 }}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 26px 1fr 26px 1fr 26px 1.1fr", alignItems: "stretch" }}>
            <FlowCol title="① 数据源" colors={colors}>
              {dataflow.sources.map((sc) => (
                <div key={sc.name} style={{ fontSize: 11, marginBottom: 7 }}>
                  <span style={{ color: sc.enabled ? colors.down : colors.up }}>●</span>{" "}
                  <b>{sc.name}</b>
                  <div style={{ color: colors.muted }}>{sc.description}</div>
                </div>
              ))}
            </FlowCol>
            <Arrow />
            <FlowCol title="② Bronze 原始层" colors={colors}>
              <LayerRow label="行情日K快照" v={`${dataflow.bronze.market_bars.files} 文件 · ${dataflow.bronze.market_bars.size_mb}MB`} colors={colors} />
              <LayerRow label="公众号文章批次" v={`${dataflow.bronze.text_articles.files} 个 · ${dataflow.bronze.text_articles.size_mb}MB`} colors={colors} />
              <div style={{ fontSize: 10.5, color: colors.muted, marginTop: 4 }}>
                最新写入 {dataflow.bronze.text_articles.latest ?? "—"}
              </div>
            </FlowCol>
            <Arrow />
            <FlowCol title="③ Silver 清洗打分" colors={colors}>
              {Object.entries(dataflow.silver.files).map(([f, st2]) => (
                <LayerRow key={f} label={f} v={st2.mtime?.slice(5, 16) ?? "—"} colors={colors} />
              ))}
              <div style={{ fontSize: 10.5, color: colors.muted, marginTop: 4 }}>
                打分器: {dataflow.scorers.filter((x) => x.active).map((x) => x.version).join(" + ")}
                {dataflow.silver.quality ? " · 质检已出" : ""}
              </div>
            </FlowCol>
            <Arrow />
            <FlowCol title="④ Gold 因子层（最新数据）" colors={colors}>
              <LayerRow label="个股K线" v={`${(dataflow.gold.kline_daily as number).toLocaleString()} 行 @${dataflow.gold.kline_latest}`} colors={colors} />
              <LayerRow label="因子截面" v={`${(dataflow.gold.factor_daily as number).toLocaleString()} 行 @${dataflow.gold.factor_latest}`} colors={colors} />
              <LayerRow label="新闻情绪" v={`市场 ${dataflow.gold.news_market_daily} 天 / 个股 ${(dataflow.gold.news_stock_daily as number).toLocaleString()} 行`} colors={colors} />
              <LayerRow label="注册因子" v={`${dataflow.gold.registry_enabled} 启用 · ${(dataflow.gold.mined_rows as number).toLocaleString()} 行`} colors={colors} />
            </FlowCol>
          </div>
        </Card>
      )}

      {/* —— 第四层：数据表详情（原数据情况面板）—— */}
      <div style={{ marginTop: 14 }}>
        <DataTable data={data} colors={colors} />
      </div>
    </div>
  );
}

/* ================= 子组件 ================= */

function FreshGrid({ dataflow, colors }: { dataflow: DataflowReport | null; colors: { text: string; muted: string; up: string; down: string; card: string; border: string } }) {
  if (!dataflow) return null;
  const daysAgo = (d: string | null) => {
    if (!d) return -1;
    return Math.floor((Date.now() - new Date(d + "T00:00:00+08:00").getTime()) / 86400000);
  };
  const items = [
    { k: "行情K线", latest: dataflow.gold.kline_latest as string },
    { k: "因子截面", latest: dataflow.gold.factor_latest as string },
    { k: "新闻情绪", latest: dataflow.gold.news_latest as string },
  ];
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 10 }}>
      {items.map((it) => {
        const ago = daysAgo(it.latest);
        const toneColor = ago <= 1 ? colors.down : ago <= 3 ? "#e8a520" : colors.up;
        return (
          <div key={it.k} style={{ background: colors.card, borderRadius: 9, padding: "8px 12px", border: `1px solid ${colors.border}` }}>
            <div style={{ fontSize: 11, color: colors.muted }}>{it.k}</div>
            <div style={{ fontSize: 17, fontWeight: 700, color: toneColor, fontVariantNumeric: "tabular-nums" }}>
              {ago === 0 ? "今日" : ago > 0 ? `${ago}天前` : it.latest}
            </div>
            <div style={{ fontSize: 10.5, color: colors.muted }}>{it.latest ?? "无数据"}</div>
          </div>
        );
      })}
    </div>
  );
}

function AlarmSummary({ health, colors, onJump }: {
  health: HealthReport; colors: ThemeColors; onJump: () => void;
}) {
  const alerts = health.alerts || [];
  return (
    <div onClick={onJump} style={{ background: alerts.length ? `${colors.up}08` : `${colors.down}0d`, borderRadius: 9, padding: "10px 12px", border: `1px solid ${alerts.length ? colors.up : colors.down}55`, cursor: "pointer" }}>
      <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 5, color: alerts.length ? colors.up : colors.down }}>
        {alerts.length ? `⚠ ${alerts.length} 条告警` : "✓ 无告警"}
      </div>
      {alerts.slice(0, 3).map((a, i) => (
        <div key={i} style={{ fontSize: 11.5, marginBottom: 3 }}>
          <b>{a.layer}</b> · {a.check}
        </div>
      ))}
      {alerts.length > 3 && <div style={{ fontSize: 11, color: colors.muted }}>…点击查看全部</div>}
    </div>
  );
}

function DataTable({ data, colors }: { data: MonitorStatus; colors: ThemeColors }) {
  const rows = Object.entries(data.data.sqlite || {});
  return (
    <details open={false} style={{ background: colors.card, borderRadius: 10, border: `1px solid ${colors.border}`, padding: 0 }}>
      <summary style={{ padding: "10px 16px", cursor: "pointer", fontWeight: 600, fontSize: 14 }}>SQLite 表级数据量（展开查看）</summary>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
        <thead><tr style={{ color: colors.muted, textAlign: "left" }}><th style={{ padding: "6px 14px" }}>表</th><th>行数</th></tr></thead>
        <tbody>
          {rows.slice(0, 20).map(([t, n]) => (
            <tr key={t}><td style={{ padding: "4px 14px" }}>{t}</td><td>{(n as number).toLocaleString()}</td></tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

function Panel({ title, children, colors }: { title: string; children: React.ReactNode; colors: ThemeColors }) {
  return (
    <div style={{ background: colors.card, borderRadius: 10, border: `1px solid ${colors.border}`, overflow: "hidden" }}>
      <div style={{ padding: "12px 16px", fontWeight: 600, fontSize: 14, borderBottom: `1px solid ${colors.border}` }}>{title}</div>
      <div style={{ padding: 14 }}>{children}</div>
    </div>
  );
}

function BigNum({ label, v, sub, tone, colors }: {
  label: string; v: string; sub?: string; tone?: "up" | "down" | "neutral" | "accent";
  colors: { card: string; border: string; muted: string; up: string; down: string; accent: string; text: string };
}) {
  const color = tone === "up" ? colors.up : tone === "down" ? colors.down : tone === "accent" ? colors.accent : colors.text;
  return (
    <div style={{ display: "inline-block", marginRight: 22 }}>
      <div style={{ fontSize: 11.5, color: colors.muted }}>{label}</div>
      <div style={{ fontSize: 21, fontWeight: 700, color, fontVariantNumeric: "tabular-nums" }}>{v}</div>
      {sub && <div style={{ fontSize: 10.5, color: colors.muted }}>{sub}</div>}
    </div>
  );
}

function fmtUptime(sec: number) {
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return h > 0 ? `${h}时${m}分` : `${m}分`;
}
