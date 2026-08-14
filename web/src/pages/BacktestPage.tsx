import { useCallback, useEffect, useState } from "react";
import EChart from "../components/EChart";
import { api, BacktestResult, StrategyInfo } from "../api/client";
import { useTheme } from "../theme";

/** 策略回测（React 版：异步任务 + WebSocket 进度 + 动态参数表单 + 历史列表 + 成交标记）。 */
export default function BacktestPage() {
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [key, setKey] = useState("dual_ma");
  const [params, setParams] = useState<Record<string, number>>({});
  const [symbol, setSymbol] = useState("sh600519");
  const [start, setStart] = useState("2023-01-01");
  const [end, setEnd] = useState("2025-06-30");
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [history, setHistory] = useState<BacktestResult[]>([]);
  const [progress, setProgress] = useState("");
  const [running, setRunning] = useState(false);
  const { colors } = useTheme();

  useEffect(() => {
    api.strategies().then((s) => {
      setStrategies(s);
      if (s.length) {
        setKey(s[0].key);
        initParams(s[0]);
      }
    });
    api.backtestHistory(20).then((h) => setHistory(h)).catch(() => {});
  }, []);

  const initParams = (s: StrategyInfo) => {
    const p: Record<string, number> = {};
    (s.param_schema || []).forEach((f) => (p[f.key] = Number(f.default ?? 0)));
    setParams(p);
  };

  const onStrategyChange = (k: string) => {
    setKey(k);
    const s = strategies.find((x) => x.key === k);
    if (s) initParams(s);
  };

  const run = useCallback(async () => {
    setRunning(true);
    setProgress("提交任务…");
    setResult(null);
    try {
      const { task_id } = await api.backtestAsync({
        symbol, start, end, strategy: key, params, initial_cash: 1000000,
      });
      const wsUrl = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/v1/strategy/backtest/ws/${task_id}`;
      const done = await new Promise<{ status: string; data: { error?: string; result_id?: number } }>((resolve) => {
        let settled = false;
        let ws: WebSocket | null = null;
        const finish = (status: string, data: unknown) => {
          if (!settled) {
            settled = true;
            if (ws) try { ws.close(); } catch { /* noop */ }
            resolve({ status, data: data as never });
          }
        };
        const poll = setInterval(async () => {
          try {
            const t = await api.taskStatus(task_id);
            if (t.status === "done" || t.status === "error") { clearInterval(poll); finish(t.status, t); }
          } catch { /* noop */ }
        }, 3000);
        try {
          ws = new WebSocket(wsUrl);
          ws.onmessage = (ev) => {
            const d = JSON.parse(ev.data as string);
            if (d.type === "error") { clearInterval(poll); finish("error", d); return; }
            setProgress(`进度 ${Math.round((d.progress || 0) * 100)}% · ${d.message || (d.status === "running" ? "引擎计算中…" : "")}`);
            if (d.status === "done" || d.status === "error") { clearInterval(poll); finish(d.status, d); }
          };
        } catch { /* 兜底靠轮询 */ }
      });
      const t = done.data;
      if (done.status !== "done" || !t?.result_id) throw new Error(t?.error || "任务失败");
      const res = await api.backtestDetail(t.result_id);
      setResult(res);
      setProgress("回测完成 ✓");
      api.backtestHistory(20).then((h) => setHistory(h)).catch(() => {});
    } catch (e) {
      setProgress(`失败: ${(e as Error).message}`);
    } finally {
      setRunning(false);
    }
  }, [key, symbol, start, end, params]);

  const loadHistory = async (id: number) => {
    try {
      const r = await api.backtestDetail(id);
      setResult(r);
      setProgress(`已加载回测 #${id}`);
    } catch { /* noop */ }
  };

  const meta = strategies.find((s) => s.key === key);

  // 净值曲线 + 买卖点标记（图表联动）
  const equityOption = (() => {
    const curve = result?.equity_curve || [];
    const trades = result?.trades || [];
    const dateIndex = new Map(curve.map((p, i) => [p.date, i]));
    const buys = trades.filter((t) => t.side === "BUY" && dateIndex.has(t.trade_date));
    const sells = trades.filter((t) => t.side === "SELL" && dateIndex.has(t.trade_date));
    const pts = (list: typeof trades) =>
      list.map((t) => [dateIndex.get(t.trade_date), curve[dateIndex.get(t.trade_date)!].equity, t.signal_type || ""]);
    return {
      tooltip: { trigger: "axis" },
      legend: { data: ["组合净值", "买入", "卖出"], top: 0 },
      grid: { left: 60, right: 20, top: 40, bottom: 40 },
      xAxis: { type: "category", data: curve.map((p) => p.date) },
      yAxis: { type: "value", scale: true },
      series: [
        {
          name: "组合净值", type: "line",
          data: curve.map((p) => p.equity),
          showSymbol: false, lineStyle: { color: colors.accent, width: 1.6 },
          areaStyle: { opacity: 0.06 },
        },
        {
          name: "买入", type: "scatter",
          data: pts(buys),
          symbolSize: 9, itemStyle: { color: colors.up, borderColor: "#fff", borderWidth: 1 },
        },
        {
          name: "卖出", type: "scatter",
          data: pts(sells),
          symbolSize: 9, itemStyle: { color: colors.down, borderColor: "#fff", borderWidth: 1 },
        },
      ],
    };
  })();

  return (
    <div style={{ padding: 16 }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(170px,1fr))", gap: 10, marginBottom: 12 }}>
        <label>
          策略
          <select value={key} onChange={(e) => onStrategyChange(e.target.value)} style={inputStyle(colors)}>
            {strategies.map((s) => (
              <option key={s.key} value={s.key}>{s.name}</option>
            ))}
          </select>
        </label>
        <label>
          标的
          <input value={symbol} onChange={(e) => setSymbol(e.target.value)} style={inputStyle(colors)} />
        </label>
        <label>
          开始
          <input type="date" value={start} onChange={(e) => setStart(e.target.value)} style={inputStyle(colors)} />
        </label>
        <label>
          结束
          <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} style={inputStyle(colors)} />
        </label>
      </div>

      {(meta?.param_schema || []).length > 0 && (
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 12 }}>
          {meta!.param_schema.map((f) => (
            <label key={f.key} style={{ fontSize: 12 }}>
              {f.label}
              <input
                type="number"
                step={f.type === "int" ? 1 : 0.01}
                value={params[f.key] ?? Number(f.default ?? 0)}
                onChange={(e) => setParams((p) => ({ ...p, [f.key]: Number(e.target.value) }))}
                style={{ ...inputStyle(colors), width: 110, marginTop: 2 }}
              />
            </label>
          ))}
        </div>
      )}

      <button onClick={run} disabled={running} style={{ padding: "8px 28px", borderRadius: 6, border: 0, background: colors.accent, color: "#fff", cursor: "pointer" }}>
        {running ? "运行中…" : "运行回测（异步）"}
      </button>
      <div style={{ margin: "10px 0", color: colors.muted }}>{progress}</div>

      {result && (
        <div>
          <div style={{ display: "flex", gap: 24, flexWrap: "wrap", marginBottom: 16 }}>
            <Metric label="总收益" value={`${(result.total_return * 100).toFixed(2)}%`} colors={colors} />
            <Metric label="年化" value={`${(result.annual_return * 100).toFixed(2)}%`} colors={colors} />
            <Metric label="夏普" value={result.sharpe.toFixed(2)} colors={colors} />
            <Metric label="最大回撤" value={`${(result.max_drawdown * 100).toFixed(2)}%`} colors={colors} />
            <Metric label="超额收益" value={`${((result.excess_return || 0) * 100).toFixed(2)}%`} colors={colors} />
          </div>
          {(result.equity_curve || []).length > 0 && <EChart option={equityOption as never} height={380} />}
          {result.risk_limits && Object.keys(result.risk_limits).length > 0 && (
            <div style={{ marginTop: 10, color: colors.muted }}>
              风险硬上限: {JSON.stringify(result.risk_limits)} · 截断 {result.risk_clamps?.length || 0} 次
            </div>
          )}
        </div>
      )}

      {history.length > 0 && (
        <div style={{ marginTop: 24, background: colors.card, borderRadius: 10, padding: 14, border: `1px solid ${colors.border}` }}>
          <h3 style={{ fontSize: 15, margin: "0 0 8px" }}>最近回测</h3>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ color: colors.muted, textAlign: "left" }}>
                <th style={{ padding: "6px 8px" }}>ID</th>
                <th>策略</th><th>标的</th><th>区间</th><th>总收益</th><th>夏普</th>
              </tr>
            </thead>
            <tbody>
              {history.map((h) => (
                <tr key={h.id} onClick={() => loadHistory(h.id)} style={{ cursor: "pointer", borderTop: `1px solid ${colors.border}` }}>
                  <td style={{ padding: "6px 8px" }}>#{h.id}</td>
                  <td>{h.strategy_key || "—"}</td>
                  <td>{h.symbol}</td>
                  <td>{h.start_date} ~ {h.end_date}</td>
                  <td style={{ color: (h.total_return || 0) >= 0 ? colors.up : colors.down }}>
                    {((h.total_return || 0) * 100).toFixed(2)}%
                  </td>
                  <td>{h.sharpe?.toFixed(2) ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

const inputStyle = (c: { text: string; card: string; border: string }) => ({
  width: "100%",
  padding: "6px 10px",
  borderRadius: 6,
  border: `1px solid ${c.border}`,
  background: c.card,
  color: c.text,
  marginTop: 4,
  boxSizing: "border-box" as const,
});

function Metric({ label, value, colors }: { label: string; value: string; colors: { card: string; text: string; muted: string; border: string } }) {
  return (
    <div style={{ background: colors.card, borderRadius: 8, padding: "10px 18px", minWidth: 110, border: `1px solid ${colors.border}` }}>
      <div style={{ fontSize: 12, color: colors.muted }}>{label}</div>
      <div style={{ fontSize: 18, fontWeight: 600, color: colors.text }}>{value}</div>
    </div>
  );
}
