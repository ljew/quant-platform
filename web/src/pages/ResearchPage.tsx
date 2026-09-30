/**
 * 研究驾驶舱（P2）：多 Agent 流水线的可视化与人工闸门。
 *
 * - 顶部：流水线步骤条（8 步，按事件流实时点亮）
 * - 闸门横幅：RiskAgent 挂起时显示倒计时 + 放行/否决
 * - 中部：Agent 事件流（SSE 实时追加，含每步耗时/行数）
 * - 底部：最近运行历史
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Badge, Btn, Card, PageHeader } from "../components/ui";
import {
  AgentEventItem, AgentState, agentApi, NotifyConfig, ResearchRunItem,
} from "../api/client";
import { useTheme, ThemeColors } from "../theme";

const STEPS = [
  { key: "data", agent: "data", title: "数据采集" },
  { key: "quality", agent: "quality", title: "数据质检" },
  { key: "research", agent: "research", title: "策略选股" },
  { key: "signal", agent: "signal", title: "信号判定" },
  { key: "backtest", agent: "backtest", title: "回测刷新" },
  { key: "attribution", agent: "attribution", title: "归因统计" },
  { key: "risk", agent: "risk", title: "发布审查" },
  { key: "publish", agent: "publish", title: "信号发布" },
];

const AGENT_CN: Record<string, string> = {
  orchestrator: "编排器", data: "DataAgent", quality: "QualityAgent",
  research: "ResearchAgent", signal: "SignalAgent", backtest: "BacktestAgent",
  attribution: "AttributionAgent", risk: "RiskAgent", publish: "PublishAgent",
};

const ACTION_CN: Record<string, string> = {
  BUY_STRONG: "重点买入", WATCH: "关注", NEW: "新出现",
  REDUCE: "减仓", SELL: "卖出", EXIT: "移出",
};

/** 步骤状态：由事件流推导（running > ok/fail > 未开始）。 */
function stepStatus(events: AgentEventItem[], key: string): "idle" | "running" | "ok" | "fail" {
  const rel = events.filter((e) => e.step === key || e.agent === key);
  if (!rel.length) return "idle";
  const last = rel[rel.length - 1];
  if (last.status === "running" || last.status === "pending") return "running";
  if (last.status === "fail") return "fail";
  return "ok";
}

export default function ResearchPage() {
  const { colors } = useTheme();
  const [state, setState] = useState<AgentState | null>(null);
  const [events, setEvents] = useState<AgentEventItem[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const esRef = useRef<EventSource | null>(null);
  const logRef = useRef<HTMLDivElement | null>(null);

  const loadState = useCallback(() => {
    agentApi.state().then(setState).catch((e) => setError((e as Error).message));
  }, []);

  // SSE：有运行中的 run 时订阅事件流
  const openStream = useCallback((runId: number) => {
    esRef.current?.close();
    const es = new EventSource(`/api/v1/agent/stream?run_id=${runId}`);
    es.onmessage = (m) => {
      const e = JSON.parse(m.data) as AgentEventItem;
      setEvents((prev) => (prev.some((x) => x.id === e.id) ? prev : [...prev, e]));
    };
    es.addEventListener("done", () => {
      es.close();
      esRef.current = null;
      loadState();
    });
    es.onerror = () => { /* 断线后轮询兜底由 loadState 定时器补 */ };
    esRef.current = es;
  }, [loadState]);

  useEffect(() => {
    loadState();
    const timer = setInterval(loadState, 4000); // SSE 之外的兜底轮询
    return () => { clearInterval(timer); esRef.current?.close(); };
  }, [loadState]);

  const running = state?.orchestrator.running ?? false;
  const latestRun = state?.latest_run ?? null;

  // run 开始时打开流；终态后由下方 effect 拉全量事件（回放）
  useEffect(() => {
    if (running && latestRun) {
      setEvents([]);
      openStream(latestRun.id);
    }
  }, [running, latestRun, openStream]);

  // 终态后拉全量事件（回放）
  useEffect(() => {
    if (!running && latestRun?.id) {
      fetch(`/api/v1/agent/runs/${latestRun.id}`)
        .then((r) => r.json())
        .then((d) => setEvents(d.events ?? []))
        .catch(() => {});
    }
  }, [running, latestRun?.id]);

  const start = async () => {
    setBusy(true);
    setError("");
    try {
      await agentApi.run("manual");
      loadState();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const gate = async (action: "approve" | "reject") => {
    if (!latestRun) return;
    setBusy(true);
    try {
      await agentApi.gate(latestRun.id, action,
        action === "approve" ? "驾驶舱人工放行" : "驾驶舱人工否决");
      loadState();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const gatePending = latestRun?.gate_status === "pending" && running;
  const remainSec = gatePending && latestRun?.gate_deadline
    ? Math.max(0, Math.round((new Date(latestRun.gate_deadline).getTime() - Date.now()) / 1000))
    : 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <PageHeader
        title="研究驾驶舱"
        desc="多 Agent 流水线 · 数据 → 质检 → 选股 → 信号 → 审查 → 发布"
        actions={
          <Btn onClick={start} disabled={busy || running}>
            {running ? "流水线运行中…" : "▶ 启动研究流水线"}
          </Btn>
        }
      />
      {error && <div style={{ color: colors.down, fontSize: 13 }}>{error}</div>}

      {/* —— 步骤条 —— */}
      <Card title="流水线步骤" colors={colors}>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {STEPS.map((s, i) => {
            const st = stepStatus(events, s.key);
            const bg = st === "ok" ? colors.down : st === "running" ? "#e8a520"
              : st === "fail" ? colors.up : colors.tableStripe;
            return (
              <div key={s.key} style={{
                flex: "1 1 110px", padding: "10px 12px", borderRadius: 8,
                background: bg, opacity: st === "idle" ? 0.45 : 1,
                border: `1px solid ${colors.border}`, minWidth: 110,
              }}>
                <div style={{ fontSize: 11, color: "#fff", opacity: 0.85 }}>{i + 1}</div>
                <div style={{ fontSize: 13, fontWeight: 600, color: "#fff" }}>{s.title}</div>
                <div style={{ fontSize: 11, color: "#fff", opacity: 0.85, marginTop: 2 }}>
                  {st === "running" ? "执行中…" : st === "ok" ? "完成" : st === "fail" ? "异常" : "待执行"}
                </div>
              </div>
            );
          })}
        </div>
      </Card>

      {/* —— 人工闸门横幅 —— */}
      {gatePending && (
        <div style={{
          border: `1px solid ${colors.up}`, background: `${colors.up}14`, borderRadius: 10,
          padding: "14px 16px", display: "flex", alignItems: "center", gap: 14,
        }}>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 14, fontWeight: 600, color: colors.up }}>
              ⏸ RiskAgent 发布审查挂起 —— 倒计时 {Math.floor(remainSec / 60)}:
              {String(remainSec % 60).padStart(2, "0")} 后自动放行
            </div>
            <div style={{ fontSize: 12, color: colors.muted, marginTop: 4 }}>
              {latestRun?.gate_note}
            </div>
          </div>
          <Btn onClick={() => gate("approve")} disabled={busy}>放行发布</Btn>
          <Btn onClick={() => gate("reject")} kind="warning" disabled={busy}>否决</Btn>
        </div>
      )}

      {/* —— Agent 事件流 —— */}
      <Card title={`Agent 事件流${events.length ? `（${events.length} 条）` : ""} `} colors={colors}>
        <div ref={logRef} style={{
          maxHeight: 380, overflowY: "auto", fontFamily: "ui-monospace,Menlo,monospace",
          fontSize: 12, lineHeight: 1.75,
        }}>
          {events.length === 0 && (
            <div style={{ color: colors.muted }}>暂无事件 —— 点「启动研究流水线」开始一次全自动研究</div>
          )}
          {events.map((e) => (
            <div key={e.id} style={{ display: "flex", gap: 8 }}>
              <span style={{ color: colors.muted, flexShrink: 0 }}>
                {(e.created_at || "").slice(11, 19)}
              </span>
              <span style={{
                color: e.level === "error" ? colors.up : e.agent === "risk" ? "#e8a520" : colors.accent,
                flexShrink: 0, width: 118,
              }}>
                {AGENT_CN[e.agent] || e.agent}
              </span>
              <span style={{ color: e.status === "fail" ? colors.up : colors.text, flex: 1 }}>
                {e.message}
                {e.duration_ms ? <span style={{ color: colors.muted }}> · {(e.duration_ms / 1000).toFixed(1)}s</span> : ""}
                {e.rows != null ? <span style={{ color: colors.muted }}> · {e.rows} 行</span> : ""}
              </span>
            </div>
          ))}
        </div>
      </Card>

      {/* —— 发布通道设置 —— */}
      <NotifyConfigCard colors={colors} />

      {/* —— 运行历史 —— */}
      <Card title="最近运行" colors={colors}>
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {(state?.recent_runs ?? []).map((r) => (
            <RunRow key={r.id} run={r} colors={colors} />
          ))}
          {!state?.recent_runs?.length && (
            <div style={{ color: colors.muted, fontSize: 13 }}>暂无运行记录</div>
          )}
        </div>
      </Card>
    </div>
  );
}

function RunRow({ run, colors }: { run: ResearchRunItem; colors: ThemeColors }) {  const tone = run.status === "SUCCESS" ? colors.down
    : run.status === "FAILED" ? colors.up : "#e8a520";
  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 10, fontSize: 12.5,
      padding: "7px 10px", borderRadius: 8, background: colors.tableStripe,
    }}>
      <span style={{
        fontSize: 11, padding: "2px 8px", borderRadius: 5,
        background: `${tone}14`, border: `1px solid ${tone}`, color: tone,
      }}>{run.status}</span>
      <span style={{ color: colors.muted }}>#{run.id}</span>
      <span>{run.trigger}</span>
      <span style={{ color: colors.muted }}>{(run.started_at || "").slice(5, 16)}</span>
      {run.gate_status === "rejected" && <span style={{ color: colors.up }}>已否决</span>}
      {run.error && <span style={{ color: colors.up, flex: 1, overflow: "hidden", textOverflow: "ellipsis" }}>{run.error}</span>}
    </div>
  );
}

/** 发布通道设置卡：SMTP 授权码轮换在此页面完成（password 留空 = 不修改）。 */
function NotifyConfigCard({ colors }: { colors: ThemeColors }) {
  const [cfg, setCfg] = useState<NotifyConfig | null>(null);
  const [pwd, setPwd] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    agentApi.state; // noop 保持依赖一致
    import("../api/client").then(({ signalApi }) =>
      signalApi.notifyConfig().then(setCfg).catch(() => {}));
  }, []);
  useEffect(() => { load(); }, [load]);

  const save = async () => {
    if (!cfg) return;
    setBusy(true); setMsg("");
    try {
      const { signalApi } = await import("../api/client");
      const r = await signalApi.updateNotifyConfig({
        enabled: cfg.enabled, smtp_host: cfg.smtp_host, smtp_port: cfg.smtp_port,
        user: cfg.user, password: pwd, from_addr: cfg.from_addr, to: cfg.to,
      });
      setCfg(r.config); setPwd(""); setMsg("已保存");
    } catch (e) { setMsg((e as Error).message); } finally { setBusy(false); }
  };

  const test = async () => {
    setBusy(true); setMsg("");
    try {
      const { signalApi } = await import("../api/client");
      const r = await signalApi.testNotify();
      setMsg(r.ok ? "测试邮件已发送，请查收" : `发送失败：${r.error}`);
    } catch (e) { setMsg((e as Error).message); } finally { setBusy(false); }
  };

  const inputStyle: React.CSSProperties = {
    padding: "6px 8px", borderRadius: 8, border: `1px solid ${colors.border}`,
    background: colors.card, color: colors.text, fontSize: 12.5,
  };

  return (
    <Card title="发布通道设置（每日信号邮件）" colors={colors}>
      {!cfg ? <div style={{ color: colors.muted, fontSize: 13 }}>加载中…</div> : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8, fontSize: 12.5 }}>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            <label style={{ color: colors.muted }}>SMTP
              <input value={cfg.smtp_host} style={{ ...inputStyle, width: 160, marginLeft: 6 }}
                onChange={(e) => setCfg({ ...cfg, smtp_host: e.target.value })} />
            </label>
            <label style={{ color: colors.muted }}>端口
              <input value={cfg.smtp_port} style={{ ...inputStyle, width: 64, marginLeft: 6 }}
                onChange={(e) => setCfg({ ...cfg, smtp_port: Number(e.target.value) })} />
            </label>
            <label style={{ color: colors.muted }}>账号
              <input value={cfg.user} style={{ ...inputStyle, width: 200, marginLeft: 6 }}
                onChange={(e) => setCfg({ ...cfg, user: e.target.value })} />
            </label>
            <label style={{ color: colors.muted }}>授权码
              <input type="password" value={pwd} placeholder={cfg.password_hint || "未设置"}
                style={{ ...inputStyle, width: 160, marginLeft: 6 }}
                onChange={(e) => setPwd(e.target.value)} />
            </label>
            <label style={{ color: colors.muted }}>收件人
              <input value={cfg.to.join(",")} style={{ ...inputStyle, width: 220, marginLeft: 6 }}
                onChange={(e) => setCfg({ ...cfg, to: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) })} />
            </label>
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <Btn small onClick={save} disabled={busy}>保存</Btn>
            <Btn small kind="warning" onClick={test} disabled={busy}>发送测试邮件</Btn>
            <span style={{ color: colors.muted, fontSize: 11.5 }}>
              授权码留空 = 不修改（当前 {cfg.password_hint || "未设置"}）；163 邮箱在「设置 → POP3/IMAP/SMTP」里生成新授权码后填入保存即可完成轮换
            </span>
          </div>
          {msg && <div style={{ color: colors.accent }}>{msg}</div>}
        </div>
      )}
    </Card>
  );
}
