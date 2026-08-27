import { useCallback, useEffect, useState } from "react";
import { api, HealthReport, MonitorStatus } from "../api/client";
import { Badge, Card } from "../components/ui";
import { useTheme } from "../theme";
import { PageHeader } from "../components/ui";

/** 平台监控页：数据情况 + 系统服务状态（30s 自动刷新）。 */
export default function MonitorPage() {
  const [data, setData] = useState<MonitorStatus | null>(null);
  const [error, setError] = useState("");
  const [lastRefresh, setLastRefresh] = useState("");
  const [health, setHealth] = useState<HealthReport | null>(null);
  const { colors } = useTheme();

  const refresh = useCallback(async () => {
    try {
      const d = await api.monitor();
      setHealth(await api.monitorHealth());
      setData(d);
      setLastRefresh(new Date().toLocaleTimeString());
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 30000);
    return () => clearInterval(timer);
  }, [refresh]);

  if (!data) {
    return (
      <div style={{ padding: 16, color: colors.muted }}>
        {error ? `加载失败: ${error}` : "加载中…"}
        <button onClick={refresh} style={btnStyle(colors)}>重试</button>
      </div>
    );
  }

  const { freshness } = data.data;

  return (
    <div style={{ maxWidth: 1280, margin: "0 auto" }}>
      <PageHeader title="系统监控" desc="数据情况 · 服务状态 · 30 秒自动刷新" />
      {/* —— 数据健康度 —— */}
      {health && (
        <Card colors={colors} pad={0} style={{ marginBottom: 16 }}>
          <div style={{ display: "grid", gridTemplateColumns: "160px 1fr 1.4fr", minHeight: 150 }}>
            <div style={{ padding: 18, borderRight: `1px solid ${colors.border}`, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center" }}>
              <div style={{ fontSize: 12, color: colors.muted }}>整体健康度</div>
              <ScoreRing score={health.overall_score} status={health.overall_status} colors={colors} />
            </div>
            <div style={{ padding: 18, borderRight: `1px solid ${colors.border}` }}>
              <div style={{ fontSize: 12, color: colors.muted, marginBottom: 10 }}>分层评分（采集 / 处理 / 应用）</div>
              {Object.entries(health.layers).map(([k, ly]) => (
                <div key={k} style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 9 }}>
                  <span style={{ width: 62, fontSize: 12.5 }}>{ly.label}</span>
                  <div style={{ flex: 1, height: 8, borderRadius: 4, background: colors.tableStripe }}>
                    <div style={{
                      width: `${ly.score}%`, height: "100%", borderRadius: 4,
                      background: ly.status === "healthy" ? colors.down : ly.status === "warn" ? "#e8a520" : colors.up,
                      transition: "width .4s",
                    }} />
                  </div>
                  <b className="num" style={{ width: 34, textAlign: "right", fontSize: 14 }}>{ly.score}</b>
                </div>
              ))}
            </div>
            <div style={{ padding: 14 }}>
              <div style={{ fontSize: 12, color: colors.muted, marginBottom: 8 }}>⚠ 告警中心 ({health.alerts.length})</div>
              <div style={{ maxHeight: 116, overflowY: "auto" }}>
                {health.alerts.length === 0 ? (
                  <div style={{ color: colors.down, fontSize: 13 }}>✓ 无告警</div>
                ) : health.alerts.map((a, i) => (
                  <div key={i} style={{ fontSize: 12, marginBottom: 5, display: "flex", gap: 7 }}>
                    <Badge text={a.level === "error" ? "严重" : "警告"} color={a.level === "error" ? colors.up : "#e8a520"} soft />
                    <span><b>{a.layer}</b> · {a.check}: {a.detail}（期望 {a.expect}）</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
          <div style={{ borderTop: `1px solid ${colors.border}`, maxHeight: 220, overflowY: "auto" }}>
            {Object.entries(health.layers).map(([k, ly]) => (
              <div key={k}>
                <div style={{ padding: "6px 16px", background: colors.tableStripe, fontWeight: 600, fontSize: 12 }}>{ly.label}</div>
                {ly.checks.map((c) => (
                  <div key={c.name} style={{ display: "flex", alignItems: "center", gap: 10, padding: "5px 16px", fontSize: 12.5, borderBottom: `1px solid ${colors.border}` }}>
                    <span style={{ color: c.status === "ok" ? colors.down : c.status === "error" ? colors.up : "#e8a520", width: 14 }}>{c.status === "ok" ? "●" : "◐"}</span>
                    <span style={{ width: 190 }}>{c.name}</span>
                    <span className="num" style={{ flex: 1, color: colors.muted }}>{c.value}</span>
                    <span style={{ color: colors.muted }}>期望: {c.expect}</span>
                  </div>
                ))}
              </div>
            ))}
          </div>
        </Card>
      )}

      {/* 顶部信息条 */}
      <div style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 16, flexWrap: "wrap" }}>
        <Chip colors={colors} ok label={`后端 ${data.server.version}`} />
        <Chip colors={colors} ok={data.services.data_source.tushare} label={`tushare ${data.services.data_source.tushare ? "在线" : "离线"}`} />
        <Chip colors={colors} ok={data.services.data_source.akshare} label={`akshare ${data.services.data_source.akshare ? "在线" : "离线"}`} />
        <Chip colors={colors} ok label={`数据目录 ${data.disk.data_dir_mb} MB`} />
        <span style={{ flex: 1 }} />
        <button onClick={refresh} style={btnStyle(colors)}>刷新</button>
        <span style={{ fontSize: 12, color: colors.muted }}>30s 自动刷新 · {lastRefresh}</span>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(420px,1fr))", gap: 14 }}>
        {/* 数据情况 */}
        <Panel title="📊 数据情况" colors={colors}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8, marginBottom: 12 }}>
            <BigNum label="SQLite 总记录" value={data.data.sqlite_total.toLocaleString()} colors={colors} />
            <BigNum label="DuckDB 总记录" value={data.data.duckdb_total.toLocaleString()} colors={colors} />
            <BigNum label="表数量" value={`${Object.keys(data.data.sqlite).length}`} colors={colors} />
          </div>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
            <thead>
              <tr style={{ color: colors.muted, textAlign: "left" }}>
                <th style={{ padding: "5px 8px" }}>表</th>
                <th>SQLite</th>
                <th>DuckDB</th>
                <th>同步</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(data.data.sqlite).map(([t, n]) => {
                const dk = data.data.duckdb[t];
                const sync = dk === undefined ? "仅业务" : dk === n ? "✓" : "✗";
                return (
                  <tr key={t} style={{ borderTop: `1px solid ${colors.border}` }}>
                    <td style={{ padding: "5px 8px" }}>{t}</td>
                    <td>{n.toLocaleString()}</td>
                    <td>{dk !== undefined ? dk.toLocaleString() : "—"}</td>
                    <td style={{ color: sync === "✓" ? colors.down : colors.up }}>{sync}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          {/* 数据新鲜度 */}
          <div style={{ marginTop: 14, fontWeight: 600, fontSize: 13 }}>数据新鲜度（最新截面日期）</div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(150px,1fr))", gap: 8, marginTop: 8 }}>
            {Object.entries(freshness).map(([k, v]) => (
              <div key={k} style={{ background: colors.card, borderRadius: 8, padding: 10, border: `1px solid ${colors.border}` }}>
                <div style={{ fontSize: 12, color: colors.muted }}>{v.label}</div>
                <div style={{ fontSize: 14, fontWeight: 600, marginTop: 2 }}>{v.latest || "—"}</div>
                <div style={{ fontSize: 12, color: v.stale ? colors.up : colors.down }}>
                  {v.days_ago === null ? "未知" : v.stale ? `已 ${v.days_ago} 天（异常）` : `${v.days_ago} 天前`}
                </div>
              </div>
            ))}
          </div>
        </Panel>

        {/* 系统服务 */}
        <Panel title="⚙️ 系统服务" colors={colors}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(180px,1fr))", gap: 10 }}>
            <ServiceCard colors={colors} title="ETL 数据调度" ok={data.services.schedulers.etl.enabled}
              desc={`每日 ${data.services.schedulers.etl.run_hour} 后运行`}
              sub={[
                `已运行 ${data.services.schedulers.etl.runs_total} 次`,
                data.services.schedulers.etl.last_success ? `最近成功 ${data.services.schedulers.etl.last_success}` : "尚未运行",
                data.services.schedulers.etl.last_error ? `⚠ ${data.services.schedulers.etl.last_error.slice(0, 40)}` : "",
              ]}
            />
            <ServiceCard colors={colors} title="模拟盘调度" ok={data.services.schedulers.paper.alive}
              desc={`每 ${data.services.schedulers.paper.interval_sec}s 检查`}
              sub={[`任务 ${data.services.paper.enabled}/${data.services.paper.tasks} 启用`]}
            />
            <ServiceCard colors={colors} title="任务队列" ok={data.services.tasks.running === 0}
              desc={data.services.tasks.running > 0 ? `${data.services.tasks.running} 个运行中` : "空闲"}
              sub={data.services.tasks.recent.slice(0, 3).map((t) => `${t.name} · ${t.status}`)}
            />
            <ServiceCard colors={colors} title="数据源" ok={data.services.data_source.tushare && data.services.data_source.akshare}
              desc={`tushare ${data.services.data_source.tushare ? "✓" : "✗"} / akshare ${data.services.data_source.akshare ? "✓" : "✗"}`}
              sub={[]}
            />
          </div>

          {data.services.tasks.recent.length > 0 && (
            <div style={{ marginTop: 14 }}>
              <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 6 }}>最近任务</div>
              {data.services.tasks.recent.slice(0, 5).map((t) => (
                <div key={t.id} style={{ fontSize: 12, color: colors.muted, marginBottom: 3, display: "flex", gap: 8 }}>
                  <span style={{ color: t.status === "done" ? colors.down : t.status === "error" ? colors.up : colors.accent }}>{t.status}</span>
                  <span>{t.name}</span>
                  <span>{Math.round((t.progress || 0) * 100)}%</span>
                </div>
              ))}
            </div>
          )}

          <div style={{ marginTop: 14, fontSize: 12, color: colors.muted }}>
            后端启动 {fmtUptime(data.server.uptime_sec)} · 服务器时间 {data.server.time} · {data.server.db}
          </div>
        </Panel>
      </div>

      {/* —— 数据管道运行记录 —— */}
      {data.services.pipeline && data.services.pipeline.runs.length > 0 && (
        <Card title="⏱ 数据管道运行记录（extract → score → factor）" colors={colors} pad={0} style={{ marginTop: 14 }}>
          {data.services.pipeline.runs.map((r) => (
            <div key={r.run_id} style={{ padding: "10px 16px", borderBottom: `1px solid ${colors.border}` }}>
              <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                <Badge text={r.status === "SUCCESS" ? "成功" : r.status === "FAILED" ? "失败" : r.status}
                  color={r.status === "SUCCESS" ? colors.down : r.status === "FAILED" ? colors.up : colors.accent} soft />
                <span className="num" style={{ fontSize: 13, fontWeight: 600 }}>#{r.run_id}</span>
                <span style={{ fontSize: 12, color: colors.muted }}>{r.trigger} · {r.started_at}{r.finished_at ? ` → ${r.finished_at.slice(11)}` : ""}</span>
                {r.error && <span style={{ fontSize: 11.5, color: colors.up }}>{r.error.slice(0, 80)}</span>}
              </div>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 6 }}>
                {(r.steps || []).map((st) => (
                  <span key={st.name} style={{
                    fontSize: 11.5, padding: "2px 9px", borderRadius: 5,
                    background: st.status === "FAIL" ? `${colors.up}14` : colors.tableStripe,
                    border: `1px solid ${st.status === "FAIL" ? colors.up : colors.border}`,
                  }}>
                    {st.name} · {st.duration_sec}s{st.rows > 0 ? ` · ${st.rows.toLocaleString()}行` : ""}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </Card>
      )}
      {error && <div style={{ color: colors.up, marginTop: 10 }}>{error}</div>}
    </div>
  );
}

function fmtUptime(sec: number): string {
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return h > 0 ? `${h}h${m}m` : m > 0 ? `${m}m${s}s` : `${s}s`;
}

function Chip({ ok, label, colors }: { ok: boolean; label: string; colors: { card: string; border: string; up: string; down: string; text: string } }) {
  return (
    <span style={{
      padding: "4px 12px", borderRadius: 999, fontSize: 12,
      background: colors.card, border: `1px solid ${colors.border}`,
      color: ok ? colors.down : colors.up,
    }}>
      {ok ? "● " : "○ "}{label}
    </span>
  );
}

function BigNum({ label, value, colors }: { label: string; value: string; colors: { card: string; muted: string; text: string; border: string } }) {
  return (
    <div style={{ background: colors.card, borderRadius: 8, padding: "10px 12px", border: `1px solid ${colors.border}` }}>
      <div style={{ fontSize: 12, color: colors.muted }}>{label}</div>
      <div style={{ fontSize: 20, fontWeight: 700 }}>{value}</div>
    </div>
  );
}

function Panel({ title, children, colors }: { title: string; children: React.ReactNode; colors: { card: string; border: string } }) {
  return (
    <div style={{ background: colors.card, borderRadius: 12, padding: 16, border: `1px solid ${colors.border}` }}>
      <div style={{ fontWeight: 700, marginBottom: 12, fontSize: 15 }}>{title}</div>
      {children}
    </div>
  );
}

function ServiceCard({ title, ok, desc, sub, colors }: {
  title: string; ok: boolean; desc: string; sub: string[];
  colors: { card: string; border: string; up: string; down: string; muted: string };
}) {
  return (
    <div style={{ background: colors.card, borderRadius: 8, padding: 12, border: `1px solid ${colors.border}` }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{ fontWeight: 600, fontSize: 13 }}>{title}</span>
        <span style={{ color: ok ? colors.down : colors.up, fontSize: 13 }}>{ok ? "✓" : "✗"}</span>
      </div>
      <div style={{ fontSize: 12, color: colors.muted, margin: "4px 0" }}>{desc}</div>
      {sub.filter(Boolean).map((s, i) => (
        <div key={i} style={{ fontSize: 11, color: colors.muted, marginTop: 2 }}>{s}</div>
      ))}
    </div>
  );
}

const btnStyle = (c: { card: string; border: string; text: string }) => ({
  padding: "6px 16px",
  borderRadius: 6,
  border: `1px solid ${c.border}`,
  background: c.card,
  color: c.text,
  cursor: "pointer",
  fontSize: 13,
});

function ScoreRing({ score, status, colors }: { score: number; status: string; colors: { down: string; up: string; muted: string } }) {
  const color = status === "healthy" ? colors.down : status === "warn" ? "#e8a520" : colors.up;
  return (
    <div style={{ position: "relative", width: 86, height: 86, marginTop: 8 }}>
      <svg viewBox="0 0 36 36" style={{ width: "100%", height: "100%", transform: "rotate(-90deg)" }}>
        <circle cx="18" cy="18" r="15.9" fill="none" stroke="rgba(128,128,128,.18)" strokeWidth="3.4" />
        <circle cx="18" cy="18" r="15.9" fill="none" stroke={color} strokeWidth="3.4"
          strokeDasharray={`${score},100`} strokeLinecap="round" />
      </svg>
      <div style={{ position: "absolute", inset: 0, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center" }}>
        <span className="num" style={{ fontSize: 22, fontWeight: 700, color }}>{score}</span>
        <span style={{ fontSize: 9.5, color: colors.muted }}>{status === "healthy" ? "健康" : status === "warn" ? "警告" : "异常"}</span>
      </div>
    </div>
  );
}
