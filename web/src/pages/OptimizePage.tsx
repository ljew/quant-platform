import { useCallback, useEffect, useState } from "react";
import { api, OptimizeTrial, StrategyInfo } from "../api/client";
import { useTheme } from "../theme";
import { Btn, Card, PageHeader, inputStyle as _is } from "../components/ui";

/** 参数寻优（网格搜索，设计 v1.0 策略研究模块）。 */
export default function OptimizePage() {
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [key, setKey] = useState("");
  const [symbol, setSymbol] = useState("sh600519");
  const [start, setStart] = useState("2022-01-01");
  const [end, setEnd] = useState("2025-06-30");
  const [ranges, setRanges] = useState<Record<string, string>>({}); // 参数 -> "5,10,20"
  const [rankBy, setRankBy] = useState("sharpe");
  const [trials, setTrials] = useState<OptimizeTrial[]>([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const { colors } = useTheme();

  useEffect(() => {
    api.strategies().then((s) => {
      const single = s.filter((x) => !x.default_params?.multi_asset);
      setStrategies(single);
      if (single.length) {
        setKey(single[0].key);
        const r: Record<string, string> = {};
        (single[0].param_schema || []).forEach((f) => (r[f.key] = String(f.default)));
        setRanges(r);
      }
    });
  }, []);

  const onStrategyChange = (k: string) => {
    setKey(k);
    const s = strategies.find((x) => x.key === k);
    if (s) {
      const r: Record<string, string> = {};
      (s.param_schema || []).forEach((f) => (r[f.key] = String(f.default)));
      setRanges(r);
    }
  };

  const run = useCallback(async () => {
    setRunning(true);
    setError("");
    setTrials([]);
    try {
      const param_ranges: Record<string, number[]> = {};
      for (const [pk, v] of Object.entries(ranges)) {
        const nums = v.split(/[,，\s]+/).map(Number).filter((n) => Number.isFinite(n));
        if (nums.length) param_ranges[pk] = nums;
      }
      if (!Object.keys(param_ranges).length) throw new Error("请至少为一个参数填写取值列表（逗号分隔）");
      const result = await api.optimize({
        symbol, start, end, strategy: key,
        param_ranges, initial_cash: 1000000, rank_by: rankBy,
      });
      setTrials(result);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRunning(false);
    }
  }, [key, symbol, start, end, ranges, rankBy]);

  const meta = strategies.find((s) => s.key === key);
  // 参数组合数预估
  const comboCount = Object.values(ranges).reduce((acc, v) => {
    const n = v.split(/[,，\s]+/).map(Number).filter(Number.isFinite).length;
    return acc * Math.max(n, 1);
  }, 1);

  return (
    <div style={{ maxWidth: 1280, margin: "0 auto" }}>
      <PageHeader title="参数寻优" desc="网格搜索 · 多维参数组合 · 夏普/收益/回撤排序" />
      <Card title="回测设置" colors={colors}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(150px,1fr))", gap: 10 }}>
        <label>
          策略（单标的）
          <select value={key} onChange={(e) => onStrategyChange(e.target.value)} style={inputStyle(colors)}>
            {strategies.map((s) => (
              <option key={s.key} value={s.key}>{s.name}</option>
            ))}
          </select>
        </label>
        <label>标的<input value={symbol} onChange={(e) => setSymbol(e.target.value)} style={inputStyle(colors)} /></label>
        <label>开始<input type="date" value={start} onChange={(e) => setStart(e.target.value)} style={inputStyle(colors)} /></label>
        <label>结束<input type="date" value={end} onChange={(e) => setEnd(e.target.value)} style={inputStyle(colors)} /></label>
        <label>
          排序依据
          <select value={rankBy} onChange={(e) => setRankBy(e.target.value)} style={inputStyle(colors)}>
            <option value="sharpe">夏普</option>
            <option value="total_return">总收益</option>
            <option value="max_drawdown">最大回撤（小优先）</option>
          </select>
        </label>
      </div>
      </Card>

      {/* 参数取值列表 */}
      {(meta?.param_schema || []).length > 0 && (
        <Card title={`参数取值（逗号分隔 = 网格搜索）· 预估组合数 ${comboCount}`} colors={colors}>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
            {meta!.param_schema.map((f) => (
              <label key={f.key} style={{ fontSize: 12 }}>
                {f.label}
                <input
                  value={ranges[f.key] ?? ""}
                  onChange={(e) => setRanges((r) => ({ ...r, [f.key]: e.target.value }))}
                  style={{ ...inputStyle, width: 130, marginTop: 2 }}
                  placeholder={`默认 ${f.default}`}
                />
              </label>
            ))}
          </div>
        </Card>
      )}

      <Btn onClick={run} disabled={running}>{running ? "寻优中…" : "开始寻优"}</Btn>
      {error && <div style={{ color: colors.up, margin: "8px 0" }}>{error}</div>}

      {/* 结果表格 */}
      {trials.length > 0 && (
        <div style={{ marginTop: 16, background: colors.card, borderRadius: 10, padding: 14, border: `1px solid ${colors.border}` }}>
          <div style={{ fontWeight: 700, marginBottom: 10 }}>
            共 {trials.length} 组参数结果（按{rankBy === "sharpe" ? "夏普" : rankBy === "total_return" ? "总收益" : "回撤"}排序）
          </div>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
            <thead>
              <tr style={{ color: colors.muted, textAlign: "left" }}>
                <th style={{ padding: "6px 8px" }}>#</th>
                {Object.keys(trials[0].params).map((k) => <th key={k}>{k}</th>)}
                <th>总收益</th><th>年化</th><th>夏普</th><th>回撤</th><th>胜率</th><th>交易数</th>
              </tr>
            </thead>
            <tbody>
              {trials.slice(0, 50).map((t, i) => (
                <tr key={i} style={{ borderTop: "1px solid #f0f0f0", background: i === 0 ? "#f0f7ff" : undefined }}>
                  <td style={{ padding: "6px 8px" }}>{i + 1}</td>
                  {Object.values(t.params).map((v, j) => <td key={j}>{v}</td>)}
                  <td style={{ color: t.total_return >= 0 ? colors.up : colors.down }}>{(t.total_return * 100).toFixed(2)}%</td>
                  <td>{(t.annual_return * 100).toFixed(2)}%</td>
                  <td style={{ fontWeight: i === 0 ? 700 : 400 }}>{t.sharpe.toFixed(3)}</td>
                  <td>{(t.max_drawdown * 100).toFixed(1)}%</td>
                  <td>{(t.win_rate * 100).toFixed(0)}%</td>
                  <td>{t.trade_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {trials.length > 50 && <div style={{ color: "#888", marginTop: 6, fontSize: 12 }}>仅显示前 50 组，共 {trials.length} 组</div>}
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
