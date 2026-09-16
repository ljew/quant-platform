import { useCallback, useEffect, useMemo, useState } from "react";
import EChart from "../components/EChart";
import { api, BacktestResult, StockItem, StrategyInfo } from "../api/client";
import { useTheme } from "../theme";
import { Card, KpiCard, PageHeader, inputStyle } from "../components/ui";
import { signalLabel } from "../signalLabel";
import ParamPanel, { ParamMap, changedKeys, defaultParams } from "../components/ParamPanel";

/** 参数快照的稳定序列化（key 顺序无关），用于判断「参数改过但还没重跑」。 */
const snap = (p: ParamMap) =>
  JSON.stringify(Object.keys(p).sort().map((k) => [k, p[k]]));

/** 策略回测（React 版：异步任务 + WebSocket 进度 + 分组参数面板 + 历史列表 + 成交标记）。 */
export default function BacktestPage() {
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [key, setKey] = useState("");
  const [params, setParams] = useState<ParamMap>({});
  const [symbol, setSymbol] = useState("sh600519");
  const [start, setStart] = useState("2023-01-01");
  const [end, setEnd] = useState("2025-06-30");
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [history, setHistory] = useState<BacktestResult[]>([]);
  const [progress, setProgress] = useState("");
  const [running, setRunning] = useState(false);
  const [lastRun, setLastRun] = useState<string | null>(null);
  const [sug, setSug] = useState<StockItem[]>([]);
  const { colors } = useTheme();

  useEffect(() => {
    api.strategies().then((s) => {
      setStrategies(s);
      if (s.length) {
        setKey(s[0].key);
        setParams(defaultParams(s[0]));
      }
    }).catch((e) => setProgress(`加载策略失败: ${(e as Error).message}`));
    api.backtestHistory(20).then((h) => setHistory(h)).catch(() => {});
  }, []);

  // 标的关键词联想（防抖 300ms）
  useEffect(() => {
    const kw = symbol.trim();
    if (kw.length < 2 || /^[a-zA-Z]{1,2}\d{6}$/.test(kw)) {
      setSug([]);
      return;
    }
    const t = setTimeout(() => {
      api.stocks(kw, 8).then(setSug).catch(() => setSug([]));
    }, 300);
    return () => clearTimeout(t);
  }, [symbol]);

  const meta = useMemo(() => strategies.find((s) => s.key === key), [strategies, key]);

  const onStrategyChange = (k: string) => {
    setKey(k);
    const s = strategies.find((x) => x.key === k);
    if (s) {
      setParams(defaultParams(s));
      setResult(null);
      setLastRun(null);
      setProgress("");
    }
  };

  const nChanged = useMemo(() => (meta ? changedKeys(meta, params).size : 0), [meta, params]);
  const stale = running || lastRun === null ? false : snap(params) !== lastRun;

  const run = useCallback(async () => {
    if (!key) return;
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
      setProgress("回测完成");
      setLastRun(snap(params));
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

  // 净值曲线 + 买卖点标记（图表联动）
  const equityOption = (() => {
    const curve = result?.equity_curve || [];
    const trades = result?.trades || [];
    const dateIndex = new Map(curve.map((p, i) => [p.date, i]));
    const buys = trades.filter((t) => t.side === "BUY" && dateIndex.has(t.trade_date));
    const sells = trades.filter((t) => t.side === "SELL" && dateIndex.has(t.trade_date));
    const pts = (list: typeof trades) =>
      list.map((t) => [dateIndex.get(t.trade_date), curve[dateIndex.get(t.trade_date)!].equity, signalLabel(t.signal_type, "")]);
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

  const calmar = result && result.max_drawdown
    ? result.total_return / Math.abs(result.max_drawdown)
    : null;

  return (
    <div style={{ maxWidth: 1440, margin: "0 auto" }}>
      <PageHeader
        title="策略回测"
        desc={meta ? `${meta.name}${meta.index_name ? ` · 基准 ${meta.index_name}` : ""}` : "异步任务 · 参数分组 · 成交标记"}
      />

      {/* —— 顶部工具条：运行所需的最小信息集 —— */}
      <div
        style={{
          display: "flex", gap: 10, alignItems: "flex-end", flexWrap: "wrap",
          padding: "12px 14px", background: colors.card, border: `1px solid ${colors.border}`,
          borderRadius: 12, marginBottom: 14,
        }}
      >
        <label style={{ fontSize: 12, color: colors.muted }}>
          策略
          <select
            value={key}
            onChange={(e) => onStrategyChange(e.target.value)}
            style={{ ...inputStyle(colors), width: 250, marginTop: 3, display: "block" }}
          >
            {strategies.map((s) => (
              <option key={s.key} value={s.key}>{s.name}</option>
            ))}
          </select>
        </label>
        <label style={{ fontSize: 12, color: colors.muted, position: "relative" }}>
          标的
          <input
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            onBlur={() => setTimeout(() => setSug([]), 150)}
            style={{ ...inputStyle(colors), width: 120, marginTop: 3, display: "block" }}
          />
          {sug.length > 0 && (
            <div
              style={{
                position: "absolute", top: "100%", left: 0, zIndex: 20, width: 250, marginTop: 2,
                background: colors.card, border: `1px solid ${colors.border}`, borderRadius: 8,
                boxShadow: "0 6px 20px rgba(15,23,42,.12)", overflow: "hidden",
              }}
            >
              {sug.map((s) => (
                <div
                  key={s.symbol}
                  onMouseDown={() => { setSymbol(s.symbol); setSug([]); }}
                  style={{ padding: "6px 10px", fontSize: 12, cursor: "pointer" }}
                  onMouseEnter={(e) => (e.currentTarget.style.background = colors.tableStripe)}
                  onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
                >
                  <span className="num" style={{ color: colors.muted, marginRight: 8 }}>{s.symbol}</span>
                  {s.name}
                </div>
              ))}
            </div>
          )}
        </label>
        <label style={{ fontSize: 12, color: colors.muted }}>
          开始
          <input type="date" value={start} onChange={(e) => setStart(e.target.value)}
            style={{ ...inputStyle(colors), width: 140, marginTop: 3, display: "block" }} />
        </label>
        <label style={{ fontSize: 12, color: colors.muted }}>
          结束
          <input type="date" value={end} onChange={(e) => setEnd(e.target.value)}
            style={{ ...inputStyle(colors), width: 140, marginTop: 3, display: "block" }} />
        </label>
        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 12 }}>
          {stale && <span style={{ fontSize: 12, color: colors.up }}>参数已修改，需重新运行</span>}
          <button
            onClick={run}
            disabled={running}
            style={{
              padding: "9px 30px", borderRadius: 7, border: 0,
              background: running ? colors.muted : colors.accent, color: "#fff",
              cursor: running ? "not-allowed" : "pointer", fontSize: 13.5, fontWeight: 500,
            }}
          >
            {running ? "运行中…" : "运行回测"}
          </button>
        </div>
      </div>

      {progress && (
        <div style={{ marginBottom: 12, fontSize: 12.5, color: progress.startsWith("失败") ? colors.down : colors.muted }}>
          {progress}
        </div>
      )}

      {/* —— 主体：左参数栏 + 右结果区 —— */}
      <div className="bt-body">
        {meta && (
          <ParamPanel
            strategy={meta}
            params={params}
            onChange={(k, v) => setParams((p) => ({ ...p, [k]: v }))}
            onPatch={(next) => setParams(next)}
            colors={colors}
          />
        )}

        <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 14 }}>
          {!result && (
            <Card colors={colors}>
              <div style={{ padding: "34px 10px", textAlign: "center", color: colors.muted, fontSize: 13 }}>
                {meta
                  ? <>左侧 {meta.param_schema.length} 个参数已按 {new Set(meta.param_schema.map(f => f.group)).size} 组归好
                    {nChanged > 0 ? `（已改 ${nChanged} 项）` : ""}。配置好后点右上角「运行回测」。</>
                  : "正在加载策略列表…"}
              </div>
            </Card>
          )}

          {result && (
            <>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(150px,1fr))", gap: 10 }}>
                <KpiCard label="总收益" value={`${(result.total_return * 100).toFixed(2)}%`}
                  tone={result.total_return >= 0 ? "up" : "down"} colors={colors} />
                <KpiCard label="年化收益" value={`${(result.annual_return * 100).toFixed(2)}%`}
                  tone={result.annual_return >= 0 ? "up" : "down"} colors={colors} />
                <KpiCard label="夏普比率" value={result.sharpe.toFixed(2)}
                  tone={Math.abs(result.sharpe) >= 1 ? "accent" : "neutral"} colors={colors} />
                <KpiCard label="最大回撤" value={`${(result.max_drawdown * 100).toFixed(2)}%`}
                  tone="neutral" colors={colors} />
                <KpiCard label="Calmar" value={calmar === null ? "—" : calmar.toFixed(2)}
                  sub="总收益 / |最大回撤|"
                  tone={(calmar ?? 0) >= 1 ? "accent" : "neutral"} colors={colors} />
                <KpiCard label="超额收益" value={`${((result.excess_return || 0) * 100).toFixed(2)}%`}
                  sub="vs 基准指数"
                  tone={(result.excess_return || 0) >= 0 ? "up" : "down"} colors={colors} />
              </div>

              {(result.equity_curve || []).length > 0 && (
                <Card
                  title="净值曲线与成交点位"
                  extra={
                    <span style={{ fontSize: 12, color: colors.muted }}>
                      成交 {result.trade_count ?? 0} 笔 · 点击图例显隐买卖标记
                    </span>
                  }
                  colors={colors}
                  pad={8}
                >
                  <EChart option={equityOption as never} height={380} />
                </Card>
              )}

              {result.risk_limits && Object.keys(result.risk_limits).length > 0 && (
                <Card title="风险硬上限" colors={colors}>
                  <div style={{ color: colors.muted, fontSize: 13 }}>
                    上限配置: <code>{JSON.stringify(result.risk_limits)}</code> · 触发截断{" "}
                    <b style={{ color: (result.risk_clamps?.length || 0) > 0 ? colors.up : colors.down }}>
                      {result.risk_clamps?.length || 0} 次
                    </b>
                  </div>
                </Card>
              )}
            </>
          )}

          {history.length > 0 && (
            <Card title={`最近回测（${history.length}）`} colors={colors} pad={0}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                <thead>
                  <tr style={{ color: colors.muted, textAlign: "left" }}>
                    <th style={{ padding: "9px 14px" }}>ID</th>
                    <th>策略</th><th>标的</th><th>区间</th><th>总收益</th><th>夏普</th><th>回撤</th>
                  </tr>
                </thead>
                <tbody>
                  {history.map((h) => (
                    <tr key={h.id} onClick={() => loadHistory(h.id)}
                      style={{ cursor: "pointer", borderTop: `1px solid ${colors.border}` }}>
                      <td style={{ padding: "9px 14px" }}>#{h.id}</td>
                      <td>{h.strategy_key || "—"}</td>
                      <td className="num">{h.symbol || "—"}</td>
                      <td style={{ color: colors.muted }}>{h.start_date} ~ {h.end_date}</td>
                      <td className="num" style={{ color: (h.total_return || 0) >= 0 ? colors.up : colors.down, fontWeight: 600 }}>
                        {((h.total_return || 0) * 100).toFixed(2)}%
                      </td>
                      <td className="num">{h.sharpe?.toFixed(2) ?? "—"}</td>
                      <td className="num" style={{ color: colors.muted }}>
                        {h.max_drawdown ? `${(h.max_drawdown * 100).toFixed(2)}%` : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
