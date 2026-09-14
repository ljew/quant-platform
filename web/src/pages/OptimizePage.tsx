import { useCallback, useEffect, useMemo, useState } from "react";
import { api, OptimizeTrial, StrategyInfo } from "../api/client";
import { useTheme } from "../theme";
import type { ThemeColors } from "../theme";
import { Btn, Card, PageHeader } from "../components/ui";
import EChart from "../components/EChart";

type MetricKey = "sharpe" | "total_return" | "oos_sharpe" | "robustness";

const METRIC_LABEL: Record<MetricKey, string> = {
  sharpe: "样本内夏普",
  total_return: "样本内收益",
  oos_sharpe: "样本外夏普",
  robustness: "稳健性",
};

/** 参数寻优：网格搜索 + 样本外验证 + 参数稳健性。 */
export default function OptimizePage() {
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [key, setKey] = useState("");
  const [symbol, setSymbol] = useState("sh600519");
  const [start, setStart] = useState("2021-01-01");
  const [end, setEnd] = useState("2025-06-30");
  const [ranges, setRanges] = useState<Record<string, string>>({});
  const [rankBy, setRankBy] = useState("sharpe");
  const [oosRatio, setOosRatio] = useState(30); // 百分比
  const [trials, setTrials] = useState<OptimizeTrial[]>([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [sel, setSel] = useState(0); // 选中的行，热力图以此为切面
  const [heatMetric, setHeatMetric] = useState<MetricKey>("sharpe");
  const [xKey, setXKey] = useState("");
  const [yKey, setYKey] = useState("");
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

  const parseRanges = () => {
    const out: Record<string, number[]> = {};
    for (const [pk, v] of Object.entries(ranges)) {
      const nums = v.split(/[,，\s]+/).map(Number).filter((n) => Number.isFinite(n) && n > 0);
      if (nums.length) out[pk] = nums;
    }
    return out;
  };

  const comboCount = useMemo(
    () => Object.values(ranges).reduce((acc, v) => {
      const n = v.split(/[,，\s]+/).map(Number).filter(Number.isFinite).length;
      return acc * Math.max(n, 1);
    }, 1),
    [ranges]
  );

  const run = useCallback(async () => {
    setRunning(true);
    setError("");
    setTrials([]);
    try {
      const param_ranges = parseRanges();
      if (!Object.keys(param_ranges).length) throw new Error("请至少为一个参数填写取值列表（逗号分隔）");
      const result = await api.optimize({
        symbol, start, end, strategy: key,
        param_ranges, initial_cash: 1000000, rank_by: rankBy,
        oos_ratio: oosRatio / 100,
      });
      setTrials(result);
      setSel(0);
      const ks = Object.keys(param_ranges);
      if (ks.length >= 2) { setXKey(ks[0]); setYKey(ks[1]); }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRunning(false);
    }
  }, [key, symbol, start, end, ranges, rankBy, oosRatio]);

  const meta = strategies.find((s) => s.key === key);
  const best = trials.length ? trials[Math.min(sel, trials.length - 1)] : null;
  const paramKeys = trials.length ? Object.keys(trials[0].params) : [];
  const hasOos = !!trials.length && trials[0].oos_start != null;

  // —— 稳健性热力图：穿过选中参数组合的一个 XY 切面 ——
  const heatOption = useMemo(() => {
    if (!trials.length || !xKey || !yKey || xKey === yKey || !best) return null;
    const dims = paramKeys.filter((k) => k === xKey || k === yKey);
    const fixedKeys = paramKeys.filter((k) => !dims.includes(k));
    // 固定其余维度为所选组合的取值，只让 x/y 在网格里变化
    const slice = trials.filter((t) => fixedKeys.every((k) => t.params[k] === best.params[k]));
    if (slice.length < 2) return null;
    const xs = Array.from(new Set(slice.map((t) => t.params[xKey]))).sort((a, b) => a - b);
    const ys = Array.from(new Set(slice.map((t) => t.params[yKey]))).sort((a, b) => a - b);
    const data: [number, number, number | null][] = [];
    let min = Infinity, max = -Infinity;
    for (const t of slice) {
      const raw = t[heatMetric as keyof OptimizeTrial] as number | null;
      if (raw == null || Number.isNaN(raw)) continue;
      data.push([xs.indexOf(t.params[xKey]), ys.indexOf(t.params[yKey]), Number(raw.toFixed(3))]);
      min = Math.min(min, raw); max = Math.max(max, raw);
    }
    if (!data.length) return null;
    if (min === max) { max = min + 1e-6; }
    return {
      tooltip: {
        position: "top" as const,
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        formatter: (p: any) => `${xKey}=${xs[p.value[0]]} · ${yKey}=${ys[p.value[1]]}<br/><b>${METRIC_LABEL[heatMetric]}</b> ${p.value[2]}`,
      },
      grid: { left: 70, right: 24, top: 12, bottom: 62 },
      xAxis: { type: "category" as const, data: xs.map(String), name: xKey, nameLocation: "middle" as const, nameGap: 26 },
      yAxis: { type: "category" as const, data: ys.map(String), name: yKey },
      visualMap: {
        min: Number(min.toFixed(3)), max: Number(max.toFixed(3)),
        calculable: true, orient: "horizontal" as const, left: "center", bottom: 4,
        itemWidth: 12, itemHeight: 120,
        textStyle: { color: colors.muted, fontSize: 11 },
        // A 股习惯：低（差）= 绿，高（好）= 红
        inRange: { color: ["#1a7f37", "#8fce9b", "#f2f4f6", "#f6b39a", "#c0392b"] },
      },
      series: [{
        name: METRIC_LABEL[heatMetric], type: "heatmap" as const, data,
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        label: { show: true, fontSize: 10, color: "#fff", formatter: (p: any) => String(p.value[2]) },
        itemStyle: { borderColor: colors.card, borderWidth: 1 },
        emphasis: { itemStyle: { borderColor: colors.up, borderWidth: 2 } },
      }],
    };
  }, [trials, xKey, yKey, best, heatMetric, paramKeys, colors]);

  /** 过拟合嫌疑：样本内赚钱、样本外亏钱。 */
  const flag = (t: OptimizeTrial) => {
    if (t.oos_sharpe == null) return null;
    if (t.sharpe > 0.1 && t.oos_sharpe < 0) return { text: "样本外失效", color: colors.down };
    if (t.oos_sharpe >= Math.max(t.sharpe, 0) * 0.6) return { text: "样本外保持", color: colors.up };
    return null;
  };

  return (
    <div style={{ maxWidth: 1280, margin: "0 auto" }}>
      <PageHeader title="参数寻优" desc="网格搜索 · 样本外验证 · 参数稳健性——防止挑出来的最优参数只是运气" />
      <Card title="回测设置" colors={colors}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(140px,1fr))", gap: 10 }}>
          <label>
            策略（单标的）
            <select value={key} onChange={(e) => onStrategyChange(e.target.value)} style={inputStyle(colors)}>
              {strategies.map((s) => (<option key={s.key} value={s.key}>{s.name}</option>))}
            </select>
          </label>
          <label>标的<input value={symbol} onChange={(e) => setSymbol(e.target.value)} style={inputStyle(colors)} /></label>
          <label>开始<input type="date" value={start} onChange={(e) => setStart(e.target.value)} style={inputStyle(colors)} /></label>
          <label>结束<input type="date" value={end} onChange={(e) => setEnd(e.target.value)} style={inputStyle(colors)} /></label>
          <label>
            排序依据
            <select value={rankBy} onChange={(e) => setRankBy(e.target.value)} style={inputStyle(colors)}>
              <option value="sharpe">样本内夏普</option>
              <option value="total_return">样本内收益</option>
              <option value="max_drawdown">样本内回撤（小优先）</option>
              <option value="oos_sharpe">样本外夏普（推荐）</option>
              <option value="robustness">稳健性（推荐）</option>
            </select>
          </label>
          <label>
            样本外占比 {oosRatio}%
            <input type="range" min={0} max={50} step={5} value={oosRatio}
              onChange={(e) => setOosRatio(Number(e.target.value))} style={{ width: "100%", marginTop: 6 }} />
          </label>
        </div>
        <div style={{ color: colors.muted, fontSize: 12, marginTop: 8 }}>
          区间末尾 {oosRatio}% 的交易日留作验证段，不参与参数挑选。设为 0 则退化为纯样本内网格搜索（会高估表现）。
        </div>
      </Card>

      {/* 参数取值列表 */}
      {(meta?.param_schema || []).length > 0 && (
        <Card title={`参数取值（逗号分隔 = 网格搜索）· 预估组合数 ${comboCount}`} colors={colors}>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
            {meta!.param_schema.map((f) => (
              <label key={f.key} style={{ fontSize: 12 }}>
                {f.label}
                <input value={ranges[f.key] ?? ""} onChange={(e) => setRanges((r) => ({ ...r, [f.key]: e.target.value }))}
                  style={{ ...inputStyle(colors), width: 130, marginTop: 2 }} placeholder={`默认 ${f.default}`} />
              </label>
            ))}
          </div>
          {comboCount > 400 && (
            <div style={{ color: colors.down, fontSize: 12, marginTop: 8 }}>
              组合数超过 400 上限，请减少取值个数或维度后再跑。
            </div>
          )}
        </Card>
      )}

      <Btn onClick={run} disabled={running || comboCount > 400}>
        {running ? "寻优中…（每组参数跑样本内+样本外两段）" : "开始寻优"}
      </Btn>
      {error && <div style={{ color: colors.down, margin: "8px 0" }}>{error}</div>}

      {/* 结论 */}
      {best && (
        <Card title="结论" colors={colors}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(160px,1fr))", gap: 12, fontSize: 13 }}>
            <div>
              <div style={{ color: colors.muted }}>最优参数</div>
              <div style={{ fontWeight: 600 }}>{paramKeys.map((k) => `${k}=${best.params[k]}`).join(" · ")}</div>
            </div>
            <div>
              <div style={{ color: colors.muted }}>样本内 收益 / 夏普</div>
              <div style={{ fontWeight: 600, color: best.total_return >= 0 ? colors.up : colors.down }}>
                {(best.total_return * 100).toFixed(2)}% / {best.sharpe.toFixed(3)}
              </div>
            </div>
            <div>
              <div style={{ color: colors.muted }}>样本外 收益 / 夏普</div>
              <div style={{ fontWeight: 600, color: (best.oos_total_return ?? 0) >= 0 ? colors.up : colors.down }}>
                {best.oos_total_return == null ? "—" : `${(best.oos_total_return * 100).toFixed(2)}% / ${(best.oos_sharpe ?? 0).toFixed(3)}`}
              </div>
            </div>
            <div>
              <div style={{ color: colors.muted }}>稳健性（相邻参数一致性）</div>
              <div style={{ fontWeight: 600 }}>{best.robustness == null ? "—" : `${(best.robustness * 100).toFixed(0)} 分`}</div>
            </div>
          </div>
          <Verdict best={best} colors={colors} />
          {hasOos && <div style={{ color: colors.muted, fontSize: 12, marginTop: 6 }}>验证段起点：{best.oos_start}</div>}
        </Card>
      )}

      {/* 稳健性热力图 */}
      {heatOption && (
        <Card title="参数稳健性热力图" colors={colors}>
          <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 8, flexWrap: "wrap", fontSize: 12 }}>
            <span style={{ color: colors.muted }}>X 轴</span>
            <select value={xKey} onChange={(e) => setXKey(e.target.value)} style={{ ...inputStyle(colors), width: 120 }}>
              {paramKeys.map((k) => <option key={k} value={k}>{k}</option>)}
            </select>
            <span style={{ color: colors.muted }}>Y 轴</span>
            <select value={yKey} onChange={(e) => setYKey(e.target.value)} style={{ ...inputStyle(colors), width: 120 }}>
              {paramKeys.map((k) => <option key={k} value={k}>{k}</option>)}
            </select>
            <span style={{ color: colors.muted }}>颜色</span>
            <select value={heatMetric} onChange={(e) => setHeatMetric(e.target.value as MetricKey)} style={{ ...inputStyle(colors), width: 130 }}>
              {(Object.keys(METRIC_LABEL) as MetricKey[]).map((k) => <option key={k} value={k}>{METRIC_LABEL[k]}</option>)}
            </select>
            <span style={{ color: colors.muted }}>
              切面基准：{paramKeys.filter((k) => k !== xKey && k !== yKey).map((k) => `${k}=${best?.params[k]}`).join(" · ") || "仅两维，无其他参数"}
            </span>
          </div>
          <EChart option={heatOption} height={340} />
          <div style={{ color: colors.muted, fontSize: 12, marginTop: 4 }}>
            看颜色过渡是否平缓：连成一片的「高原」说明参数有效；只有一格很亮、周围都很暗的「孤峰」多半是运气。
          </div>
        </Card>
      )}

      {/* 结果表格 */}
      {trials.length > 0 && (
        <div style={{ marginTop: 16, background: colors.card, borderRadius: 10, padding: 14, border: `1px solid ${colors.border}` }}>
          <div style={{ fontWeight: 600, marginBottom: 10, fontSize: 13 }}>
            共 {trials.length} 组参数（点击行切换上方的结论与热力图切面）
          </div>
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5, whiteSpace: "nowrap" }}>
              <thead>
                <tr style={{ color: colors.muted, textAlign: "left" }}>
                  <th style={{ padding: "6px 8px" }}>#</th>
                  {paramKeys.map((k) => <th key={k} style={{ padding: "6px 8px" }}>{k}</th>)}
                  <th style={{ padding: "6px 8px" }}>IS收益</th>
                  <th style={{ padding: "6px 8px" }}>IS夏普</th>
                  <th style={{ padding: "6px 8px" }}>IS回撤</th>
                  {hasOos && <th style={{ padding: "6px 8px" }}>OOS收益</th>}
                  {hasOos && <th style={{ padding: "6px 8px" }}>OOS夏普</th>}
                  <th style={{ padding: "6px 8px" }}>稳健性</th>
                  <th style={{ padding: "6px 8px" }}>交易数</th>
                  <th style={{ padding: "6px 8px" }}>判定</th>
                </tr>
              </thead>
              <tbody>
                {trials.map((t, i) => {
                  const f = flag(t);
                  return (
                    <tr key={i} onClick={() => setSel(i)}
                      style={{
                        borderTop: `1px solid ${colors.border}`, cursor: "pointer",
                        background: i === sel ? colors.tableStripe : undefined,
                      }}>
                      <td style={{ padding: "6px 8px" }}>{i + 1}</td>
                      {paramKeys.map((k) => <td key={k} style={{ padding: "6px 8px" }}>{t.params[k]}</td>)}
                      <td style={{ padding: "6px 8px", color: t.total_return >= 0 ? colors.up : colors.down }}>
                        {(t.total_return * 100).toFixed(2)}%
                      </td>
                      <td style={{ padding: "6px 8px", fontWeight: i === 0 ? 600 : 400 }}>{t.sharpe.toFixed(3)}</td>
                      <td style={{ padding: "6px 8px" }}>{(t.max_drawdown * 100).toFixed(1)}%</td>
                      {hasOos && (
                        <td style={{ padding: "6px 8px", color: (t.oos_total_return ?? 0) >= 0 ? colors.up : colors.down }}>
                          {t.oos_total_return == null ? "—" : `${(t.oos_total_return * 100).toFixed(2)}%`}
                        </td>
                      )}
                      {hasOos && (
                        <td style={{ padding: "6px 8px", fontWeight: 600 }}>
                          {t.oos_sharpe == null ? "—" : t.oos_sharpe.toFixed(3)}
                        </td>
                      )}
                      <td style={{ padding: "6px 8px" }}>{t.robustness == null ? "—" : (t.robustness * 100).toFixed(0)}</td>
                      <td style={{ padding: "6px 8px" }}>{t.trade_count}</td>
                      <td style={{ padding: "6px 8px", color: f?.color, fontSize: 11.5 }}>{f?.text ?? ""}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div style={{ color: colors.muted, fontSize: 12, marginTop: 6 }}>
            IS = 样本内（用于挑选参数），OOS = 样本外（未参与挑选，检验是否真的有效）。稳健性越高，说明相邻参数表现越接近，不是孤峰。
          </div>
        </div>
      )}
    </div>
  );
}

/** 一句话结论：判断这组参数能不能信。 */
function Verdict({ best, colors }: { best: OptimizeTrial; colors: ThemeColors }) {
  let text = "", color = colors.muted;
  const rb = best.robustness;
  const oos = best.oos_sharpe;
  if (oos == null) {
    text = "未启用样本外验证（占比 0%），结果只能作为参考，不可直接用于实盘。";
    color = colors.muted;
  } else if (best.sharpe > 0 && oos < 0) {
    text = "⚠ 样本内在赚钱、样本外在亏钱 —— 典型的过拟合，不要按这组参数上实盘。建议改按「样本外夏普」或「稳健性」排序重挑。";
    color = colors.down;
  } else if ((rb ?? 0) >= 0.7 && oos >= 0) {
    text = "✓ 样本外为正且相邻参数表现一致，这组参数比较可靠。";
    color = colors.up;
  } else if ((rb ?? 0) < 0.5) {
    text = "⚠ 稳健性偏低：最优参数周围的组合表现差异很大，可能是孤峰（运气），建议换更平滑的参数区。";
    color = colors.down;
  } else {
    text = "样本外保持一般，建议扩大区间或增加数据后再验证。";
    color = colors.muted;
  }
  return (
    <div style={{
      marginTop: 10, padding: "10px 12px", borderRadius: 8, fontSize: 12.5,
      background: "rgba(128,128,128,0.08)", borderLeft: `3px solid ${color}`, color,
    }}>
      {text}
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
