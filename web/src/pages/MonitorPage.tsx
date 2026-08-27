import { useCallback, useEffect, useState } from "react";
import { api, MonitorStatus } from "../api/client";
import { Badge, Card } from "../components/ui";
import { useTheme } from "../theme";
import { PageHeader } from "../components/ui";

/** 平台监控页：数据情况 + 系统服务状态（30s 自动刷新）。 */
export default function MonitorPage() {
  const [data, setData] = useState<MonitorStatus | null>(null);
  const [error, setError] = useState("");
  const [lastRefresh, setLastRefresh] = useState("");
  const { colors } = useTheme();

  const refresh = useCallback(async () => {
    try {
      const d = await api.monitor();
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
