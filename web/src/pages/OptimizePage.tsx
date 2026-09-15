import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, BacktestResult, OptimizeTrial, StrategyInfo } from "../api/client";
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

/** 超过这么多组就走后台任务（子进程 + 进度条）；再往上到 5000 组也支持。 */
const ASYNC_THRESHOLD = 100;
const ASYNC_MAX = 5000;

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
  // 大网格后台任务
  const [jobId, setJobId] = useState("");
  const [prog, setProg] = useState<{ done: number; total: number }>({ done: 0, total: 0 });
  const [asyncMode, setAsyncMode] = useState(false);
  const polling = useRef(false);
  // 一键落地：全区间回测 / 建模拟盘
  const [verify, setVerify] = useState<BacktestResult | null>(null);
  const [verifying, setVerifying] = useState(false);
  const [buyHold, setBuyHold] = useState<number | null>(null);
  const [paperOpen, setPaperOpen] = useState(false);
  const [paperName, setPaperName] = useState("");
  const [paperCash, setPaperCash] = useState(1000000);
  const [paperAuto, setPaperAuto] = useState(true);
  const [paperBusy, setPaperBusy] = useState(false);
  const [toast, setToast] = useState("");
  const { colors } = useTheme();

  const showToast = (msg: string) => {
    setToast(msg);
    setTimeout(() => setToast(""), 6000);
  };
  /** 跨页跳转（App.tsx 监听 quant-nav 事件切 tab）。 */
  const go = (t: string) => window.dispatchEvent(new CustomEvent("quant-nav", { detail: { tab: t } }));

  useEffect(() => () => { polling.current = false; }, []);  // 离开页面停止轮询

  // 换了选中的参数组 / 重跑寻优后，之前的全区间回测结果就不再对应了，清掉避免误导
  useEffect(() => { setVerify(null); setPaperOpen(false); }, [sel, trials]);

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

  /** 轮询后台任务直到结束。polling.current 置 false 即退出（取消 / 离开页面）。 */
  const pollJob = useCallback(async (id: string) => {
    polling.current = true;
    try {
      for (;;) {
        await new Promise((r) => setTimeout(r, 1000));
        if (!polling.current) return;
        const s = await api.optimizeAsyncStatus(id);
        setProg({ done: s.done, total: s.total });
        if (!s.running) {
          if (!s.ok) throw new Error(s.error || "寻优失败");
          setTrials(s.results);
          return;
        }
      }
    } finally {
      polling.current = false;
    }
  }, []);

  const run = useCallback(async () => {
    setRunning(true);
    setError("");
    setTrials([]);
    setProg({ done: 0, total: 0 });
    setJobId("");
    try {
      const param_ranges = parseRanges();
      if (!Object.keys(param_ranges).length) throw new Error("请至少为一个参数填写取值列表（逗号分隔）");
      const body = {
        symbol, start, end, strategy: key,
        param_ranges, initial_cash: 1000000, rank_by: rankBy,
        oos_ratio: oosRatio / 100,
      };
      if (comboCount > ASYNC_THRESHOLD) {
        // 大网格：提交后台任务（后端子进程执行），前端轮询进度
        setAsyncMode(true);
        const { job_id, total } = await api.optimizeAsync(body);
        setJobId(job_id);
        setProg({ done: 0, total });
        await pollJob(job_id);
      } else {
        setAsyncMode(false);
        const result = await api.optimize(body);
        setTrials(result);
      }
      setSel(0);
      const ks = Object.keys(param_ranges);
      if (ks.length >= 2) { setXKey(ks[0]); setYKey(ks[1]); }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRunning(false);
      polling.current = false;
    }
  }, [key, symbol, start, end, ranges, rankBy, oosRatio, comboCount, pollJob]);

  const cancel = useCallback(async () => {
    if (!jobId) return;
    polling.current = false;
    try {
      await api.optimizeAsyncCancel(jobId);
      setError("已取消本次寻优");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRunning(false);
    }
  }, [jobId]);

  const meta = strategies.find((s) => s.key === key);
  const best = trials.length ? trials[Math.min(sel, trials.length - 1)] : null;
  const paramKeys = trials.length ? Object.keys(trials[0].params) : [];
  const hasOos = !!trials.length && trials[0].oos_start != null;

  /** 用当前选中的参数，在完整区间（IS+OOS 合并）再跑一次回测，作为最终确认。 */
  const verifyBest = useCallback(async () => {
    if (!best) return;
    setVerifying(true);
    setError("");
    try {
      const r = await api.backtestSync({
        symbol, start, end, strategy: key,
        params: best.params, initial_cash: 1000000,
      });
      setVerify(r);
      // 单标的回测后端不给基准，这里拿标的自身的「买入持有」做对照
      try {
        const ks = await api.kline(symbol, start, end);
        setBuyHold(ks.length > 1 ? ks[ks.length - 1].close / ks[0].close - 1 : null);
      } catch {
        setBuyHold(null);
      }
      showToast("已跑完全区间回测，并写入回测历史");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setVerifying(false);
    }
  }, [best, symbol, start, end, key]);

  /** 用当前选中的参数建一个模拟盘任务（默认立即启用并跑一次）。 */
  const createPaper = useCallback(async () => {
    if (!best) return;
    setPaperBusy(true);
    setError("");
    try {
      const created = await api.paperCreate({
        name: paperName,
        strategy_key: key,
        kind: "single",
        symbols: symbol,
        params_json: JSON.stringify(best.params),
        initial_cash: paperCash,
        start_date: start,
        enabled: paperAuto,
      });
      setPaperOpen(false);
      showToast(`模拟盘「${paperName}」已创建${paperAuto ? "，已触发首次运行" : ""}`);
      // 首次运行可能要几十秒，不 await，避免按钮一直转圈
      if (paperAuto && created?.id) {
        api.paperRun(created.id).catch(() => { /* 跑失败不影响任务已建，调度器也会再拉起 */ });
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setPaperBusy(false);
    }
  }, [best, key, symbol, start, paperName, paperCash, paperAuto]);

  /** 全区间回测的净值曲线；竖线标注样本外起点，方便对照 IS/OOS 两段表现。 */
  const verifyOption = useMemo(() => {
    if (!verify?.equity_curve?.length) return null;
    const d = verify.equity_curve;
    return {
      tooltip: {
        trigger: "axis" as const,
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        formatter: (ps: any) => {
          const p = Array.isArray(ps) ? ps[0] : ps;
          return `${p.axisValue}<br/>权益 <b>${Number(p.data).toFixed(0)}</b>`;
        },
      },
      grid: { left: 62, right: 16, top: 14, bottom: 34 },
      xAxis: {
        type: "category" as const, data: d.map((p) => p.date),
        axisLabel: { color: colors.muted, fontSize: 10 },
      },
      yAxis: {
        type: "value" as const, scale: true,
        axisLabel: { color: colors.muted, fontSize: 10 },
        splitLine: { lineStyle: { color: colors.border } },
      },
      series: [{
        name: "权益", type: "line" as const, showSymbol: false,
        data: d.map((p) => p.equity),
        lineStyle: { color: colors.accent, width: 1.6 },
        areaStyle: { color: "rgba(128,128,128,0.12)" },
        markLine: best?.oos_start
          ? {
            silent: true, symbol: "none",
            data: [{ xAxis: best.oos_start }],
            lineStyle: { color: colors.muted, type: "dashed" as const },
            label: { formatter: "OOS 起点", color: colors.muted, fontSize: 10 },
          }
          : undefined,
      }],
    };
  }, [verify, best, colors]);

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
          <div style={{ fontSize: 12, marginTop: 8, color: comboCount > ASYNC_MAX ? colors.down : colors.muted }}>
            {comboCount > ASYNC_MAX
              ? `组合数超过 ${ASYNC_MAX} 上限，请减少取值个数或维度。`
              : comboCount > ASYNC_THRESHOLD
                ? "组合数较多，将提交为后台任务（后端子进程执行）：可实时看进度、可随时取消，不占用页面。"
                : "小网格直接同步跑，通常几秒内出结果。"}
          </div>
        </Card>
      )}

      <Btn onClick={run} disabled={running || comboCount > ASYNC_MAX}>
        {running
          ? asyncMode ? `寻优中… ${prog.done}/${prog.total} 组` : "寻优中…（每组参数跑样本内+样本外两段）"
          : comboCount > ASYNC_THRESHOLD ? `开始寻优（后台运行 · ${comboCount} 组）` : "开始寻优"}
      </Btn>

      {running && asyncMode && (
        <div style={{ display: "flex", alignItems: "center", gap: 10, margin: "10px 0" }}>
          <div style={{ flex: 1, height: 8, background: colors.border, borderRadius: 4, overflow: "hidden" }}>
            <div style={{
              width: `${prog.total ? (prog.done / prog.total) * 100 : 0}%`,
              height: "100%", background: colors.accent, transition: "width .3s",
            }} />
          </div>
          <span style={{ fontSize: 12, color: colors.muted, whiteSpace: "nowrap" }}>{prog.done}/{prog.total}</span>
          <Btn onClick={cancel}>取消</Btn>
        </div>
      )}
      {error && <div style={{ color: colors.down, margin: "8px 0" }}>{error}</div>}
      {toast && (
        <div style={{ display: "flex", gap: 10, alignItems: "center", color: colors.up, margin: "8px 0", fontSize: 13 }}>
          <span>✓ {toast}</span>
          {toast.includes("模拟盘") && <Btn onClick={() => go("paper")}>去模拟盘</Btn>}
        </div>
      )}

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

          {/* —— 一键落地：拿这组参数去回测 / 建模拟盘 —— */}
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", marginTop: 12 }}>
            <Btn onClick={verifyBest} disabled={verifying}>
              {verifying ? "回测中…" : "用此参数跑全区间回测"}
            </Btn>
            <Btn onClick={() => {
              const tag = paramKeys.map((k) => best.params[k]).join("_");
              const ts = new Date().toTimeString().slice(0, 8).replace(/:/g, "");
              setPaperName(`寻优-${key}-${symbol}-${tag}-${ts}`);
              setPaperOpen((o) => !o);
            }}>
              {paperOpen ? "收起建模拟盘" : "用此参数建模拟盘"}
            </Btn>
            <span style={{ color: colors.muted, fontSize: 12 }}>
              全区间 = 样本内 + 样本外合并重跑一遍，看这组参数在完整区间到底行不行。
            </span>
          </div>

          {paperOpen && (
            <div style={{
              marginTop: 10, padding: 12, borderRadius: 8,
              background: "rgba(128,128,128,0.06)", border: `1px solid ${colors.border}`,
            }}>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(180px,1fr))", gap: 10 }}>
                <label style={{ fontSize: 12 }}>
                  任务名称
                  <input value={paperName} onChange={(e) => setPaperName(e.target.value)} style={inputStyle(colors)} />
                </label>
                <label style={{ fontSize: 12 }}>
                  初始资金（元）
                  <input type="number" value={paperCash} onChange={(e) => setPaperCash(Number(e.target.value))}
                    style={inputStyle(colors)} />
                </label>
              </div>
              <div style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 10, flexWrap: "wrap" }}>
                <label style={{ fontSize: 12.5, display: "flex", gap: 6, alignItems: "center" }}>
                  <input type="checkbox" checked={paperAuto} onChange={(e) => setPaperAuto(e.target.checked)} />
                  创建后立即启用并跑一次
                </label>
                <Btn onClick={createPaper} disabled={paperBusy || !paperName.trim()}>
                  {paperBusy ? "创建中…" : "确认创建"}
                </Btn>
                <span style={{ color: colors.muted, fontSize: 12 }}>
                  参数将写入任务：{paramKeys.map((k) => `${k}=${best.params[k]}`).join(" · ")}
                </span>
              </div>
            </div>
          )}
        </Card>
      )}

      {/* 全区间回测结果 */}
      {verify && (
        <Card title="全区间回测（用上面这组参数）" colors={colors}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(130px,1fr))", gap: 12, fontSize: 13 }}>
            <div>
              <div style={{ color: colors.muted }}>总收益</div>
              <div style={{ fontWeight: 600, color: verify.total_return >= 0 ? colors.up : colors.down }}>
                {(verify.total_return * 100).toFixed(2)}%
              </div>
            </div>
            <div>
              <div style={{ color: colors.muted }}>夏普</div>
              <div style={{ fontWeight: 600 }}>{verify.sharpe.toFixed(3)}</div>
            </div>
            <div>
              <div style={{ color: colors.muted }}>最大回撤</div>
              <div style={{ fontWeight: 600, color: colors.down }}>{(verify.max_drawdown * 100).toFixed(1)}%</div>
            </div>
            <div>
              <div style={{ color: colors.muted }}>交易数</div>
              <div style={{ fontWeight: 600 }}>{verify.trade_count ?? "—"}</div>
            </div>
            {buyHold != null && (
              <div>
                <div style={{ color: colors.muted }}>标的买入持有</div>
                <div style={{ fontWeight: 600, color: buyHold >= 0 ? colors.up : colors.down }}>
                  {(buyHold * 100).toFixed(2)}%
                </div>
              </div>
            )}
            {buyHold != null && (
              <div>
                <div style={{ color: colors.muted }}>相对买入持有</div>
                <div style={{ fontWeight: 600, color: verify.total_return - buyHold >= 0 ? colors.up : colors.down }}>
                  {verify.total_return - buyHold >= 0 ? "+" : ""}{((verify.total_return - buyHold) * 100).toFixed(2)}%
                </div>
              </div>
            )}
          </div>
          {verifyOption && <div style={{ marginTop: 10 }}><EChart option={verifyOption} height={280} /></div>}
          <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
            <Btn onClick={() => go("backtest")}>去回测页看明细</Btn>
          </div>
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
