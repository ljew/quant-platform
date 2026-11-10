import { useCallback, useEffect, useRef, useState } from "react";
import { api, AlertLogItem, CredentialsReport, DataflowReport, FrameworkHealth, HealthReport, MonitorStatus } from "../api/client";
import { Badge, Card } from "../components/ui";
import { Arrow, FlowCol, LayerRow } from "../components/flow";
import type { ThemeColors } from "../theme";
import { useTheme } from "../theme";
import { PageHeader } from "../components/ui";

/** 平台监控页：一眼结论 → 告警 → 数据管道全景 → 运行记录。 */

/** 框架体检（五层）的状态色。首页原来那条状态带 2026-11-08 挪到这儿。 */
const FW_COLOR: Record<string, string> = {
  OK: "#1e8449",
  WARN: "#b9770e",
  FAIL: "#c0392b",
};

/** 框架体检卡：五层状态 + 非 OK 项直列。
 *
 * 单独抽出来是为了能在 `/monitor/status` 还没返回时也显示 —— 那个接口要统计
 * 71 张表的行数，实测 10 秒；原来 MonitorPage 在 data 为空时直接 return
 * 「加载监控数据…」，把这块也挡掉了（2026-11-08 挪过来时踩到）。
 */
function FrameworkCard({ fw, colors, style }: {
  fw: FrameworkHealth; colors: ThemeColors; style?: React.CSSProperties;
}) {
  const bad = fw.layers.flatMap((l) =>
    l.items.filter((i) => i.status !== "OK").map((i) => ({ l, i })));
  return (
    <Card title="框架体检" colors={colors} style={style}>
      <div style={{ display: "flex", alignItems: "center", gap: 18, flexWrap: "wrap", fontSize: 12.5 }}>
        {fw.layers.map((l) => {
          const c = FW_COLOR[l.status] ?? colors.muted;
          const tip = l.items.map((i) => `${i.label}：${i.detail}`).join("\n");
          return (
            <span key={l.key} title={tip}
              style={{ display: "inline-flex", alignItems: "center", gap: 6, cursor: "help" }}>
              <span style={{ width: 7, height: 7, borderRadius: "50%", background: c, display: "inline-block" }} />
              <span>{l.name}</span>
              <span style={{ color: c, fontWeight: 600 }}>{l.status}</span>
            </span>
          );
        })}
        <span style={{ marginLeft: "auto", color: colors.muted, fontSize: 11.5 }}>
          {fw.checked_at.slice(5).replace("T", " ")} · 每分钟刷新 · 悬停看明细
        </span>
      </div>
      {bad.length > 0 && (
        <div style={{ marginTop: 10, borderTop: `1px solid ${colors.border}`, paddingTop: 8 }}>
          {bad.map(({ l, i }) => (
            <div key={`${l.key}-${i.label}`}
              style={{ fontSize: 12, padding: "3px 0", color: colors.muted, lineHeight: 1.7 }}>
              <span style={{ color: FW_COLOR[i.status] ?? colors.muted, fontWeight: 600 }}>
                [{i.status}]
              </span>{" "}
              {l.name} · {i.label}：{i.detail}
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

export default function MonitorPage() {
  const [data, setData] = useState<MonitorStatus | null>(null);
  const [error, setError] = useState("");
  const [health, setHealth] = useState<HealthReport | null>(null);
  const [dataflow, setDataflow] = useState<DataflowReport | null>(null);
  const [lastRefresh, setLastRefresh] = useState("");
  const [showDetail, setShowDetail] = useState(false);
  const [fw, setFw] = useState<FrameworkHealth | null>(null);
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

  // 框架体检：调度 / 编排 / 数据 / 研究 / 运行时 五层，每分钟刷新
  useEffect(() => {
    const load = () => api.frameworkHealth().then(setFw).catch(() => {});
    load();
    const t = setInterval(load, 60000);
    return () => clearInterval(t);
  }, []);

  if (!data) {
    // 框架体检的数据源（/monitor/framework-health）比 /monitor/status 快得多，
    // 别让它在「加载监控数据…」后面白等 10 秒。
    return (
      <div style={{ maxWidth: 1280, margin: "0 auto" }}>
        {fw && <FrameworkCard fw={fw} colors={colors} style={{ marginBottom: 14 }} />}
        <div style={{ padding: 24, color: "#888" }}>加载监控数据…{error}</div>
      </div>
    );
  }
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

      {/* —— 节假日静默横幅 —— */}
      {health?.holiday && <HolidayBanner holiday={health.holiday} colors={colors} onChange={refresh} />}

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
                      <span style={{ color: c.status === "ok" ? colors.down : c.status === "error" ? colors.up : "#e8a520", width: 14 }}>{c.muted ? "😴" : c.status === "ok" ? "●" : c.status === "warn" ? "◐" : "✕"}</span>
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

      {fw && <FrameworkCard fw={fw} colors={colors} style={{ marginBottom: 14 }} />}

      {/* 凭证集中：五把钥匙一屏看齐。放这里而不是最后 —— 简报没发出去时，
          第一个该怀疑的就是 SMTP / Cookie / 大模型 Key，不是先看数据表。 */}
      <CredentialsCard colors={colors} />

      {/* —— 第二层：系统服务 ——
          「最近管道执行」已删：数据管道页有完整的「运行历史」（含每步耗时条形图），
          两处各存一份只会让「上一次跑成功没有」要查两个地方。 */}
      <Panel title="⚙ 系统服务" colors={colors}>
        <BigNum label="回测任务" v={`${st.tasks.running}`} sub="运行中"
          tone={st.tasks.running > 0 ? "accent" : "neutral"} colors={colors} />
        <BigNum label="模拟盘任务" v={`${st.paper.enabled}/${st.paper.tasks}`} sub="启用/总数"
          tone="neutral" colors={colors} />
        <div style={{ marginTop: 14, fontSize: 12, color: colors.muted }}>
          后端启动 {fmtUptime(data.server.uptime_sec)} · {data.server.db} · 30s 自动刷新
        </div>
      </Panel>

      {/* 「数据从哪里来 · 怎么处理」已删：数据管道页的「数据源头」就是血缘表，
          同一份东西画两遍，改一边忘一边是迟早的事。 */}

      <div style={{ marginTop: 14 }}>
        <AlertLogCard colors={colors} />
      </div>

      {/* —— 第四层：数据表详情（原数据情况面板）—— */}
      <div style={{ marginTop: 14 }}>
        <DataTable data={data} colors={colors} />
      </div>
    </div>
  );
}

/* ================= 子组件 ================= */

/** 凭证集中：数据源 / 大模型 / 邮件 / 公众号 Cookie / 数据库备份。
 *
 * 以前这五把钥匙散在五个地方：Tushare 在 .env、大模型在配置弹窗、
 * 公众号在「资讯库」、邮件根本没界面、备份只能 ls 目录。
 * 简报没发出去时，要判断是哪一把断了，得翻五遍 —— 所以收一屏。
 *
 * 全绿时收成一行（这页平时不该占屏幕），有异常才自动展开。
 */
function CredentialsCard({ colors }: { colors: ThemeColors }) {
  const [rep, setRep] = useState<CredentialsReport | null>(null);
  const [open, setOpen] = useState(false);

  const load = useCallback(() => {
    api.monitorCredentials().then((r) => {
      setRep(r);
      setOpen(r.counts.warn + r.counts.missing > 0);
    }).catch(() => {});
  }, []);
  useEffect(() => {
    load();
    const t = setInterval(load, 60000);
    return () => clearInterval(t);
  }, [load]);

  if (!rep) return null;
  const bad = rep.counts.warn + rep.counts.missing;
  const tone = bad === 0 ? colors.down : rep.counts.missing > 0 ? colors.up : "#e8a520";
  const dot = (st: string) => (st === "ok" ? colors.down : st === "missing" ? colors.up : "#e8a520");

  return (
    <Card
      colors={colors}
      style={{ marginBottom: 14 }}
      title="🔑 凭证"
      extra={
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ fontSize: 12, color: tone, fontWeight: 600 }}>
            {bad === 0 ? `全部 ${rep.counts.ok} 项正常` : `${bad} 项要处理`}
          </span>
          <button
            onClick={() => setOpen((v) => !v)}
            style={{ fontSize: 12, color: colors.accent, background: "none", border: 0, cursor: "pointer" }}
          >
            {open ? "收起" : "展开"}
          </button>
        </div>
      }
      pad={open ? 0 : 12}
    >
      {!open ? (
        <div style={{ display: "flex", gap: 14, flexWrap: "wrap", fontSize: 12, color: colors.muted }}>
          {rep.items.map((it) => (
            <span key={it.key} style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
              <span style={{ width: 6, height: 6, borderRadius: 99, background: dot(it.status) }} />
              {it.label}
            </span>
          ))}
        </div>
      ) : (
        <div>
          {rep.items.map((it) => (
            <div
              key={it.key}
              style={{
                display: "flex", alignItems: "center", gap: 10, padding: "9px 14px",
                borderBottom: `1px solid ${colors.border}`, fontSize: 12.5,
              }}
            >
              <span style={{ width: 7, height: 7, borderRadius: 99, background: dot(it.status), flexShrink: 0 }} />
              <span style={{ width: 96, fontWeight: 600, flexShrink: 0 }}>{it.label}</span>
              <span style={{
                fontSize: 10.5, color: colors.muted, border: `1px solid ${colors.border}`,
                borderRadius: 4, padding: "1px 5px", flexShrink: 0,
              }}>{it.kind}</span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ color: it.status === "ok" ? colors.text : dot(it.status) }}>{it.detail}</div>
                {it.hint && <div style={{ fontSize: 11.5, color: colors.muted, marginTop: 2 }}>{it.hint}</div>}
              </div>
              <span
                style={{
                  fontSize: 11, color: colors.muted, flexShrink: 0, maxWidth: 220,
                  overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                }}
                title={it.where}
              >
                {it.where}
              </span>
              {it.action && (
                <a
                  href={it.action.href}
                  style={{
                    fontSize: 11.5, color: colors.accent, textDecoration: "none",
                    border: `1px solid ${colors.accent}30`, borderRadius: 5, padding: "2px 8px", flexShrink: 0,
                  }}
                >{it.action.label} →</a>
              )}
            </div>
          ))}
          <div style={{ padding: "7px 14px", fontSize: 11, color: colors.muted }}>
            核对于 {rep.checked_at} · 60s 自动刷新 · 只显示状态，不回明文
          </div>
        </div>
      )}
    </Card>
  );
}

/** 告警历史：读 notify_log（不新建表）—— 「昨天半夜出过什么事」不用再翻 docker logs。 */
function AlertLogCard({ colors }: { colors: ThemeColors }) {
  const [rows, setRows] = useState<AlertLogItem[] | null>(null);
  const [level, setLevel] = useState<string>("");
  useEffect(() => {
    let alive = true;
    api.alertLog(60, level || undefined)
      .then((r) => { if (alive) setRows(r.items); })
      .catch(() => { if (alive) setRows([]); });
    return () => { alive = false; };
  }, [level]);

  const tone = (lv: string) => (lv === "FAIL" ? colors.up : lv === "WARN" ? "#b9770e" : colors.muted);
  return (
    <Card title={`告警历史（${rows?.length ?? "…"}）`} colors={colors} pad={0}
      extra={
        <span style={{ display: "inline-flex", gap: 6 }}>
          {[["", "全部"], ["FAIL", "FAIL"], ["WARN", "WARN"]].map(([k, label]) => (
            <button key={label} onClick={() => setLevel(k)} style={{
              border: `1px solid ${colors.border}`,
              background: level === k ? colors.accent : "transparent",
              color: level === k ? "#fff" : colors.muted, borderRadius: 999,
              padding: "2px 10px", fontSize: 11, cursor: "pointer",
            }}>{label}</button>
          ))}
        </span>
      }>
      {rows === null ? (
        <div style={{ padding: 18, fontSize: 12, color: colors.muted }}>加载中…</div>
      ) : rows.length === 0 ? (
        <div style={{ padding: 18, fontSize: 12.5, color: colors.muted, lineHeight: 1.8 }}>
          这个筛选下没有记录。告警走 notify_log（邮件 / 飞书），成功与失败都记。
        </div>
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
          <thead>
            <tr style={{ color: colors.muted, textAlign: "left" }}>
              {["级别", "标题", "渠道", "结果", "时间"].map((h) => (
                <th key={h} style={{ padding: "8px 14px", fontWeight: 500 }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} style={{ borderTop: `1px solid ${colors.border}` }}>
                <td style={{ padding: "7px 14px", fontWeight: 600, color: tone(r.level) }}>{r.level}</td>
                <td style={{ padding: "7px 14px" }}>{r.title}</td>
                <td style={{ padding: "7px 14px", color: colors.muted }}>{r.channel}</td>
                <td style={{ padding: "7px 14px", color: r.status === "ok" ? colors.down : colors.up }}>
                  {r.status}{r.error ? ` · ${r.error.slice(0, 40)}` : ""}
                </td>
                <td style={{ padding: "7px 14px", color: colors.muted, whiteSpace: "nowrap" }}>
                  {(r.at || "").replace("T", " ").slice(5, 16)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

function FreshGrid({ dataflow, colors }: { dataflow: DataflowReport | null; colors: { text: string; muted: string; up: string; down: string; card: string; border: string } }) {
  if (!dataflow) return null;
  const daysAgo = (d: string | null) => {
    if (!d) return -1;
    return Math.floor((Date.now() - new Date(d + "T00:00:00+08:00").getTime()) / 86400000);
  };
  const items = [
    { k: "行情K线", latest: dataflow.gold.kline_latest as string },
    { k: "因子截面", latest: dataflow.gold.factor_latest as string },
  ];
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(2,1fr)", gap: 10 }}>
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

/** 节假日静默横幅：非交易日自动静音「数据停更」类告警；可手动强制开/关。 */
function HolidayBanner({ holiday, colors, onChange }: {
  holiday: { mode: "auto" | "on" | "off"; active: boolean; trading_day: boolean; next_trading_day: string | null };
  colors: ThemeColors;
  onChange: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const setMode = async (mode: "auto" | "on" | "off") => {
    setBusy(true);
    try { await api.setHolidayMode(mode); onChange(); } finally { setBusy(false); }
  };
  const MODES: { k: "auto" | "on" | "off"; label: string }[] = [
    { k: "auto", label: "自动" }, { k: "on", label: "强制静默" }, { k: "off", label: "关闭" },
  ];
  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap",
      padding: "9px 14px", marginBottom: 14, borderRadius: 9, fontSize: 12.5,
      background: holiday.active ? "#b9770e14" : colors.card,
      border: `1px solid ${holiday.active ? "#b9770e55" : colors.border}`,
    }}>
      <span style={{ fontSize: 15 }}>{holiday.active ? "😴" : "🔔"}</span>
      {holiday.active ? (
        <span>
          <b>节假日静默中</b>
          <span style={{ color: colors.muted }}>
            （{holiday.mode === "on" ? "手动开启" : "非交易日自动"}）· 数据停更属预期，相关告警已静音
            {holiday.next_trading_day ? `，${holiday.next_trading_day.slice(5)} 开市恢复` : ""}
          </span>
        </span>
      ) : (
        <span style={{ color: colors.muted }}>
          告警正常开启{holiday.trading_day ? "（今日交易日）" : ""} · 节假日自动静音「数据停更」类误报
        </span>
      )}
      <span style={{ marginLeft: "auto", display: "inline-flex", borderRadius: 7, overflow: "hidden", border: `1px solid ${colors.border}` }}>
        {MODES.map((m) => (
          <button key={m.k} disabled={busy} onClick={() => setMode(m.k)} style={{
            padding: "4px 11px", fontSize: 11.5, border: 0, cursor: busy ? "wait" : "pointer",
            background: holiday.mode === m.k ? colors.accent : "transparent",
            color: holiday.mode === m.k ? "#fff" : colors.muted,
          }}>{m.label}</button>
        ))}
      </span>
    </div>
  );
}
