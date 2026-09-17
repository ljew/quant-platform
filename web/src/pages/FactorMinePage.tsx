import { useCallback, useEffect, useState } from "react";
import EChart from "../components/EChart";
import LlmConfigModal from "../components/LlmConfigModal";
import { Badge, Btn, Card, KpiCard, PageHeader, inputStyle } from "../components/ui";
import {
  api, AiExplainResult, AiGenerateResult, AiStatus,
  FactorMineReport, FactorMineSummary, GpMineResult, NewsEventReport,
} from "../api/client";
import { useTheme, ThemeColors } from "../theme";

const DIRECTION_CN: Record<string, string> = {
  momentum: "动量趋势", volatility: "波动率结构", value: "估值变换",
  quality: "质量成长", reversal: "均值回归",
};

/** 因子挖掘：自定义表达式 → IC/ICIR/分组单调/多空/相关性检验报告。 */
export default function FactorMinePage() {
  const { colors } = useTheme();
  const [expr, setExpr] = useState("safe_inv(pe_ttm, 0, 1000) - roc(c_m, 60)");
  const [name, setName] = useState("估值-动量复合");
  const [start, setStart] = useState("2024-01-01");
  const [end, setEnd] = useState("2026-08-24");
  const [groups, setGroups] = useState(5);
  const [forward, setForward] = useState(20);
  const [fns, setFns] = useState<Record<string, Record<string, string>>>({});
  const [showFns, setShowFns] = useState(false);
  const [valid, setValid] = useState<{ ok: boolean; error?: string; sample_value?: number | null } | null>(null);
  const [running, setRunning] = useState(false);
  const [report, setReport] = useState<FactorMineReport | null>(null);
  const [history, setHistory] = useState<FactorMineSummary[]>([]);
  const [error, setError] = useState("");
  // AI 自然语言生成（生成后需人工确认，不自动挖掘）
  const [aiStatus, setAiStatus] = useState<AiStatus | null>(null);
  const [aiText, setAiText] = useState("");
  const [aiBusy, setAiBusy] = useState(false);
  const [aiResult, setAiResult] = useState<AiGenerateResult | null>(null);
  const [aiError, setAiError] = useState("");
  const [explainBusy, setExplainBusy] = useState(false);
  const [explainResult, setExplainResult] = useState<AiExplainResult | null>(null);
  // 大模型通道配置弹窗
  const [cfgOpen, setCfgOpen] = useState(false);
  // GP 自动挖掘
  const [gpDirs, setGpDirs] = useState<{ key: string; note: string }[]>([]);
  const [gpSel, setGpSel] = useState<string[]>(["momentum", "volatility", "value"]);
  const [gpPop, setGpPop] = useState(14);
  const [gpGens, setGpGens] = useState(6);
  const [gpRunning, setGpRunning] = useState(false);
  const [gpOrtho, setGpOrtho] = useState(false);
  const [gpCrisis, setGpCrisis] = useState(false);
  const [gpResult, setGpResult] = useState<GpMineResult | null>(null);
  // 新闻情绪择时
  const [newsSeries, setNewsSeries] = useState<{ date: string; n_finance: number; net_sentiment: number | null }[]>([]);
  const [newsHorizon, setNewsHorizon] = useState(5);
  const [newsTesting, setNewsTesting] = useState(false);
  const [newsReport, setNewsReport] = useState<NewsEventReport | null>(null);

  const loadHistory = useCallback(() => {
    api.factorMineResults(15).then(setHistory).catch(() => {});
  }, []);

  // 单独抽出来：配好 key 并重启后端后，页面上点「重新检测」即可生效，无需刷新
  const loadAiStatus = useCallback(() => {
    api.factorAiStatus().then(setAiStatus).catch(() => setAiStatus(null));
  }, []);

  useEffect(() => {
    api.factorFunctions().then(setFns).catch(() => {});
    api.factorGpDirections().then(setGpDirs).catch(() => {});
    api.factorNewsDaily(600).then(setNewsSeries).catch(() => {});
    loadAiStatus();
    loadHistory();
  }, [loadAiStatus, loadHistory]);

  const runAiGenerate = async () => {
    const text = aiText.trim();
    if (!text) return;
    setAiBusy(true);
    setAiError("");
    setAiResult(null);
    setExplainResult(null);
    try {
      const r = await api.factorAiGenerate(text);
      setAiResult(r);
      if (!r.ok) setAiError(r.error || "生成失败");
    } catch (e) {
      setAiError((e as Error).message);
    } finally {
      setAiBusy(false);
    }
  };

  // 只回填、不自动跑：把「要不要挖」的决定权留给用户
  const applyAiExpr = () => {
    if (!aiResult?.expr) return;
    setExpr(aiResult.expr);
    if (aiResult.name) setName(aiResult.name);
    setValid(null);
    setError("");
  };

  const runExplain = async () => {
    if (!expr.trim()) return;
    setExplainBusy(true);
    setExplainResult(null);
    try {
      setExplainResult(await api.factorAiExplain(expr));
    } catch (e) {
      setExplainResult({ ok: false, error: (e as Error).message });
    } finally {
      setExplainBusy(false);
    }
  };

  const runNewsTest = async () => {
    setNewsTesting(true);
    try {
      setNewsReport(await api.factorNewsTest(0.10, newsHorizon));
    } catch (e) {
      setError(`情绪检验失败: ${(e as Error).message}`);
    } finally {
      setNewsTesting(false);
    }
  };

  const runGp = async () => {
    setGpRunning(true);
    setError("");
    setGpResult(null);
    try {
      const r = await api.factorGpMine({
        directions: gpSel, name_prefix: "GP", pop_size: gpPop, generations: gpGens,
        start, end, forward, step: 30, pool_size: 220, top_k: 3,
        orthogonal: gpOrtho, crisis_only: gpCrisis,
      });
      setGpResult(r);
      loadHistory();
      if (r.elites.length > 0) setReport(r.elites[0]);
    } catch (e) {
      setError(`GP 挖掘失败: ${(e as Error).message}`);
    } finally {
      setGpRunning(false);
    }
  };

  const doValidate = async () => {
    setValid(null);
    try {
      setValid(await api.factorValidate(expr));
    } catch (e) {
      setValid({ ok: false, error: (e as Error).message });
    }
  };

  const runMine = async () => {
    setRunning(true);
    setError("");
    setReport(null);
    try {
      const r = await api.factorMine({ expr, name, start, end, groups, forward });
      setReport(r);
      loadHistory();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRunning(false);
    }
  };

  return (
    <div style={{ maxWidth: 1240, margin: "0 auto" }}>
      <PageHeader
        title="因子挖掘"
        desc="自定义表达式 · 截面 IC 检验 / 分组单调性 / 多空收益 / 冗余度分析"
        actions={<Btn onClick={() => setShowFns(!showFns)} kind="ghost" small>函数参考</Btn>}
      />

      {/* —— AI 自然语言生成（常驻；未配置大模型时给配置引导 + 本地规则试用）—— */}
      {aiStatus && (
        <Card
          title="AI 生成因子"
          colors={colors}
          extra={
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              {aiStatus.configured
                ? <Badge
                    text={aiStatus.provider_name
                      ? `${aiStatus.provider_name} · ${aiStatus.model}`
                      : aiStatus.model}
                    color={colors.accent} soft />
                : <Badge text="试用模式 · 本地规则" color="#c8860d" soft />}
              <Btn kind="ghost" small onClick={() => setCfgOpen(true)}>
                {aiStatus.configured ? "模型配置" : "配置模型"}
              </Btn>
            </div>
          }
          style={{ marginBottom: 14 }}
        >
          {!aiStatus.configured && (
            <div style={{
              marginBottom: 12, padding: "10px 12px", borderRadius: 8,
              border: "1px solid #c8860d55", background: "#c8860d12",
            }}>
              <div style={{ fontWeight: 600, fontSize: 13 }}>
                尚未配置大模型 —— 当前用本地关键词规则匹配，可直接试
              </div>
              <div style={{ fontSize: 12.5, color: colors.muted, lineHeight: 1.75, marginTop: 4 }}>
                试用模式只识别常见表述的组合（估值 / 成长 / 波动 / 量能 / 动量 / 现金流 / 情绪 / 市值）。
                配置大模型后即可理解任意自然语言，并带「校验失败自动重修」闭环。
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 10 }}>
                <Btn small onClick={() => setCfgOpen(true)}>配置模型</Btn>
                <Btn kind="ghost" small onClick={loadAiStatus}>重新检测</Btn>
              </div>
              <details style={{ marginTop: 8 }}>
                <summary style={{ fontSize: 12, color: colors.muted, cursor: "pointer" }}>
                  也可用环境变量配置（需重启后端）
                </summary>
                <pre style={{
                  margin: "8px 0 0", padding: "8px 10px", borderRadius: 6,
                  background: colors.tableStripe, border: `1px solid ${colors.border}`,
                  fontSize: 11.5, lineHeight: 1.85, overflowX: "auto",
                  fontFamily: "'SF Mono', Menlo, Consolas, monospace", color: colors.text,
                }}>{`# 在 quant-platform/.env 追加
QUANT_LLM_API_KEY=sk-你的key
# 换厂商亦可（OpenAI 兼容协议通用）：
#   QUANT_LLM_BASE_URL / QUANT_LLM_MODEL

# 改完需重启后端（环境变量在启动时读取）
lsof -ti:8000 -sTCP:LISTEN | xargs kill

# 注意：页面上配置的通道优先级更高，会覆盖这里的 key`}</pre>
              </details>
            </div>
          )}

          <div style={{ display: "flex", gap: 8, alignItems: "stretch" }}>
            <input
              value={aiText}
              onChange={(e) => setAiText(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !aiBusy) runAiGenerate(); }}
              placeholder="用一句话描述你要找什么样的股票，例如：估值不高、近期缩量整理、但现金流在改善的公司"
              style={{ ...inputStyle(colors), flex: 1 }}
            />
            <Btn onClick={runAiGenerate} disabled={aiBusy || !aiText.trim()}>
              {aiBusy ? "生成中…" : "生成表达式"}
            </Btn>
          </div>

          {aiError && !aiResult?.ok && (
            <div style={{ color: colors.up, fontSize: 13, marginTop: 8 }}>
              {aiError}
              {aiResult?.hint ? <span style={{ color: colors.muted }}> · {aiResult.hint}</span> : null}
            </div>
          )}

          {aiResult?.ok && aiResult.expr && (
            <div style={{
              marginTop: 12, padding: 12, borderRadius: 9,
              border: `1px solid ${colors.border}`, background: colors.tableStripe,
            }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10, flexWrap: "wrap" }}>
                <div style={{ fontWeight: 600, fontSize: 13.5 }}>
                  {aiResult.name}
                  {aiResult.direction && (
                    <span style={{ color: colors.muted, fontWeight: 400, fontSize: 12 }}> · {aiResult.direction}</span>
                  )}
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  {aiResult.source === "local" && (
                    <Badge text="本地规则匹配" color="#c8860d" soft />
                  )}
                  {!!aiResult.attempts && aiResult.attempts > 1 && aiResult.source !== "local" && (
                    <Badge text={`自修复 ${aiResult.attempts - 1} 轮`} color="#c8860d" soft />
                  )}
                  <Btn kind="ghost" small onClick={applyAiExpr}>填入表达式框</Btn>
                </div>
              </div>
              <code style={{
                display: "block", marginTop: 8, wordBreak: "break-all",
                fontFamily: "'SF Mono', Menlo, Consolas, monospace",
                fontSize: 13, color: colors.accent,
              }}>
                {aiResult.expr}
              </code>
              {aiResult.logic && (
                <div style={{ fontSize: 12.5, color: colors.muted, marginTop: 8 }}>{aiResult.logic}</div>
              )}
              {aiResult.source === "local" && (
                <div style={{ fontSize: 12, color: "#c8860d", marginTop: 8, lineHeight: 1.7 }}>
                  试用模式：以上是关键词匹配结果，未调用大模型
                  {!!aiResult.matched?.length && <>，命中「{aiResult.matched.join(" + ")}」</>}
                  。请确认是否贴合你的本意。
                </div>
              )}
              {aiResult.unsupported && (
                <div style={{ fontSize: 12.5, color: "#c8860d", marginTop: 8 }}>
                  平台缺少所需数据：{aiResult.unsupported} —— 上面的表达式是退而求其次的近似，用前请确认
                </div>
              )}
              {!!aiResult.fixes?.length && (
                <details style={{ marginTop: 8 }}>
                  <summary style={{ fontSize: 12, color: colors.muted, cursor: "pointer" }}>
                    查看 {aiResult.fixes.length} 次未通过的尝试
                  </summary>
                  <div style={{ marginTop: 6, fontSize: 11.5, color: colors.muted, lineHeight: 1.7 }}>
                    {aiResult.fixes.map((f) => (
                      <div key={f.attempt}>
                        第 {f.attempt} 轮 <code>{f.expr || "(空)"}</code> — {f.error}
                      </div>
                    ))}
                  </div>
                </details>
              )}
            </div>
          )}
        </Card>
      )}

      {/* —— 配置面板 —— */}
      <div style={{ display: "grid", gridTemplateColumns: "1.5fr 1fr", gap: 14, marginBottom: 16 }}>
        <Card
          title="因子表达式"
          colors={colors}
          extra={
            <Btn
              kind="ghost" small onClick={runExplain}
              disabled={explainBusy || !expr.trim() || !aiStatus?.configured}
              title={aiStatus?.configured ? "" : "需要配置大模型后才能解读表达式"}
            >
              {explainBusy ? "解读中…" : "AI 解读"}
            </Btn>
          }
        >
          <textarea
            value={expr}
            onChange={(e) => { setExpr(e.target.value); setValid(null); }}
            rows={3}
            spellCheck={false}
            style={{
              width: "100%",
              padding: "10px 12px",
              borderRadius: 8,
              border: `1px solid ${valid ? (valid.ok ? colors.down : colors.up) : colors.border}`,
              background: colors.tableStripe,
              color: colors.text,
              fontFamily: "'SF Mono', Menlo, Consolas, monospace",
              fontSize: 14,
              lineHeight: 1.7,
              boxSizing: "border-box",
              resize: "vertical",
              outline: "none",
            }}
            placeholder='safe_inv(pe_ttm, 0, 1000) - roc(c_m, 60)'
          />
          {valid && (
            <div style={{ fontSize: 12.5, marginTop: 8, fontWeight: 500 }}>
              {valid.ok ? (
                <span style={{ color: colors.down }}>✓ 表达式有效 · 试算值 <code>{valid.sample_value ?? "—"}</code></span>
              ) : (
                <span style={{ color: colors.up }}>✗ {valid.error}</span>
              )}
            </div>
          )}
          {explainResult && (
            <div style={{
              marginTop: 8, padding: "9px 11px", borderRadius: 8, fontSize: 12.5,
              background: colors.tableStripe, border: `1px solid ${colors.border}`,
            }}>
              {explainResult.ok ? (
                <>
                  <div style={{ fontWeight: 600 }}>
                    {explainResult.name}
                    {explainResult.direction && (
                      <span style={{ color: colors.muted, fontWeight: 400 }}> · {explainResult.direction}</span>
                    )}
                  </div>
                  {explainResult.logic && (
                    <div style={{ color: colors.muted, marginTop: 4 }}>{explainResult.logic}</div>
                  )}
                  {explainResult.caveats && (
                    <div style={{ color: "#c8860d", marginTop: 4 }}>失效场景：{explainResult.caveats}</div>
                  )}
                </>
              ) : (
                <div style={{ color: colors.up }}>{explainResult.error}</div>
              )}
            </div>
          )}
          {error && <div style={{ color: colors.up, marginTop: 8, fontSize: 13 }}>{error}</div>}

          {/* 函数参考 */}
          {showFns && (
            <div style={{ marginTop: 10, display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(210px,1fr))", gap: 8, fontSize: 11.5 }}>
              {Object.entries(fns).map(([cat, items]) => (
                <div key={cat} style={{ background: colors.tableStripe, borderRadius: 8, padding: 9 }}>
                  <div style={{ fontWeight: 600, marginBottom: 4, fontSize: 12 }}>{cat}</div>
                  {Object.entries(items).map(([k, v]) => (
                    <div key={k} style={{ marginBottom: 2 }}>
                      <code style={{ color: colors.accent }}>{k}</code>
                      <span style={{ color: colors.muted }}> — {v}</span>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card title="检验参数" colors={colors}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
            <Labeled label="因子名称"><input value={name} onChange={(e) => setName(e.target.value)} style={inputStyle(colors)} /></Labeled>
            <Labeled label="股票池">核心指数成分并集<div /></Labeled>
            <Labeled label="开始日期"><input type="date" value={start} onChange={(e) => setStart(e.target.value)} style={inputStyle(colors)} /></Labeled>
            <Labeled label="结束日期"><input type="date" value={end} onChange={(e) => setEnd(e.target.value)} style={inputStyle(colors)} /></Labeled>
            <Labeled label="分组数">
              <select value={groups} onChange={(e) => setGroups(Number(e.target.value))} style={inputStyle(colors)}>
                {[3, 5, 8, 10].map((g) => <option key={g} value={g}>{g} 组</option>)}
              </select>
            </Labeled>
            <Labeled label="未来收益窗口">
              <select value={forward} onChange={(e) => setForward(Number(e.target.value))} style={inputStyle(colors)}>
                {[5, 10, 20, 30, 60].map((f) => <option key={f} value={f}>{f} 日</option>)}
              </select>
            </Labeled>
          </div>
          <div style={{ display: "flex", gap: 10, marginTop: 16 }}>
            <Btn onClick={doValidate} kind="warning" small disabled={running}>校验表达式</Btn>
            <Btn onClick={runMine} disabled={running}>{running ? "挖掘中…" : "开始挖掘"}</Btn>
          </div>
          <div style={{ marginTop: 14, fontSize: 11.5, color: colors.muted, lineHeight: 1.7 }}>
            流程：逐期截面计算因子值 → Spearman IC vs 未来收益 → 分组单调性 / 多空累计 /
            与现有 14 因子冗余度 → 综合评级。
          </div>
        </Card>
      </div>

      {/* —— 新闻情绪择时因子 —— */}
      <div style={{ marginBottom: 16 }}>
        <Card
          title="新闻情绪择时因子（文本管道 · 94 公众号语料）"
          colors={colors}
          extra={<span style={{ fontSize: 11.5, color: colors.muted }}>词典法打分 · 差异化数据源</span>}
        >
          <div style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 8 }}>
            <span style={{ fontSize: 12.5 }}>净情绪 时序（正=多空词频偏多）</span>
            <span style={{ flex: 1 }} />
            <label style={{ fontSize: 12, color: colors.muted }}>未来收益窗口
              <select value={newsHorizon} onChange={(e) => setNewsHorizon(Number(e.target.value))} style={{ ...inputStyle(colors), width: 76, marginLeft: 6 }}>
                {[1, 5, 10, 20].map((h) => <option key={h} value={h}>{h} 日</option>)}
              </select>
            </label>
            <Btn small onClick={runNewsTest} disabled={newsTesting}>{newsTesting ? "检验中…" : "事件检验"}</Btn>
          </div>
          {newsSeries.length > 0 && (
            <EChart height={200} option={{
              tooltip: { trigger: "axis" },
              grid: { left: 50, right: 50, top: 24, bottom: 34 },
              xAxis: { type: "category", data: [...newsSeries].reverse().map((p) => p.date.slice(2)), axisLabel: { fontSize: 9.5, interval: Math.ceil(newsSeries.length / 8) } },
              yAxis: [
                { type: "value", name: "净情绪", scale: true, splitLine: { lineStyle: { color: colors.border, opacity: 0.4 } } },
                { type: "value", name: "文章数" },
              ],
              series: [
                { name: "净情绪", type: "bar", data: [...newsSeries].reverse().map((p) => ({ value: p.net_sentiment, itemStyle: { color: (p.net_sentiment ?? 0) >= 0 ? colors.up : colors.down, opacity: 0.7 } })), barWidth: "60%" },
                { name: "财经文章数", type: "line", yAxisIndex: 1, data: [...newsSeries].reverse().map((p) => p.n_finance), showSymbol: false, lineStyle: { color: colors.muted, width: 1 } },
              ],
            } as never} />
          )}
          {newsReport && (
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(150px,1fr))", gap: 10, marginTop: 10 }}>
              <KpiCard label="基线收益（全部交易日均值）" value={`${(newsReport.baseline_ret * 100).toFixed(2)}%`} sub={`${newsReport.n_days_all} 天样本`} tone="neutral" colors={colors} />
              <KpiCard label="极端看多日做多" value={`${(newsReport.bull.avg_ret * 100).toFixed(2)}%`}
                sub={`${newsReport.bull.n_days} 天 · 胜率 ${(newsReport.bull.win_rate * 100).toFixed(0)}%`}
                tone={(newsReport.edge_long_vs_base || 0) > 0 ? "up" : "down"} colors={colors} />
              <KpiCard label="极端看空日之后指数" value={`${(newsReport.bear.avg_ret * 100).toFixed(2)}%`}
                sub={`${newsReport.bear.n_days} 天 · 胜率 ${(newsReport.bear.win_rate * 100).toFixed(0)}%`}
                tone="neutral" colors={colors} />
              <KpiCard label="多头边际优势" value={`${((newsReport.edge_long_vs_base || 0) * 100).toFixed(2)}%`}
                sub="vs 基线" tone={(newsReport.edge_long_vs_base || 0) > 0 ? "up" : "down"} colors={colors} />
            </div>
          )}
          <div style={{ marginTop: 8, fontSize: 11.5, color: colors.muted }}>
            看多日 = 净情绪 ≥ 前 10% 分位；观察其后续 {newsHorizon} 日中证800 收益是否系统性跑赢基线。
            该指标为市场级择时信号，与截面选股因子互补。
          </div>
        </Card>
      </div>

      {/* —— GP 自动挖掘 —— */}
      <div style={{ marginBottom: 16 }}>
        <Card
          title="GP 自动挖掘（遗传规划 · QuantaAlpha 式进化搜索）"
          colors={colors}
          extra={<span style={{ fontSize: 11.5, color: colors.muted }}>研究方向互补播种 → 变异进化 → 精英全池精评</span>}
        >
          <div style={{ display: "flex", gap: 14, flexWrap: "wrap", alignItems: "center", marginBottom: 12 }}>
            <span style={{ fontSize: 12.5, color: colors.muted }}>研究方向：</span>
            {gpDirs.map((d) => {
              const on = gpSel.includes(d.key);
              return (
                <button key={d.key}
                  onClick={() => setGpSel((s) => (on ? s.filter((x) => x !== d.key) : [...s, d.key]))}
                  style={{
                    padding: "5px 13px", borderRadius: 999, cursor: "pointer", fontSize: 12.5,
                    border: `1px solid ${on ? colors.accent : colors.border}`,
                    background: on ? `${colors.accent}1a` : "transparent",
                    color: on ? colors.accent : colors.muted, fontWeight: on ? 600 : 400,
                  }}
                  title={d.note}
                >
                  {DIRECTION_CN[d.key] || d.key}
                </button>
              );
            })}
            <span style={{ flex: 1 }} />
            <label style={{ fontSize: 12, color: colors.muted }}>种群
              <input type="number" min={6} max={40} value={gpPop} onChange={(e) => setGpPop(Number(e.target.value) || 14)} style={{ ...inputStyle(colors), width: 64, marginLeft: 6 }} /></label>
            <label style={{ fontSize: 12, color: colors.muted }}>代数
              <input type="number" min={2} max={20} value={gpGens} onChange={(e) => setGpGens(Number(e.target.value) || 6)} style={{ ...inputStyle(colors), width: 64, marginLeft: 6 }} /></label>
            <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 5, cursor: "pointer" }}
              title="候选因子先对动量/反转/低波/价值/规模做横截面回归取残差，再算IC——专挖现有因子之外的增量alpha">
              <input type="checkbox" checked={gpOrtho} onChange={(e) => setGpOrtho(e.target.checked)} />
              正交增量模式
            </label>
            <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 5, cursor: "pointer" }}
              title="仅在基准下跌窗口(<-3%)评IC——专挖危机Alpha">
              <input type="checkbox" checked={gpCrisis} onChange={(e) => setGpCrisis(e.target.checked)} />
              危机Alpha
            </label>
            <Btn onClick={runGp} disabled={gpRunning || gpSel.length === 0}>{gpRunning ? "进化中（约1~3分钟）…" : "启动自动挖掘"}</Btn>
          </div>

          {gpRunning && (
            <div style={{ fontSize: 12.5, color: colors.muted }}>
              遗传规划正在进化：种群 {gpPop} × {gpGens} 代 · 方向[{gpSel.map((d) => DIRECTION_CN[d] || d).join("/")}] · {gpOrtho ? "正交增量" : "原始IC"}{gpCrisis ? " · 仅危机窗口" : ""}
            </div>
          )}

          {gpResult && (
            <div>
              {/* 进化曲线 */}
              <EChart height={150} option={{
                tooltip: { trigger: "axis" },
                grid: { left: 46, right: 14, top: 22, bottom: 26 },
                xAxis: { type: "category", data: gpResult.evolution_log.map((e) => `G${e.gen}`), axisLabel: { fontSize: 10 } },
                yAxis: { type: "value", scale: true },
                series: [{ name: "最优IC", type: "line", data: gpResult.evolution_log.map((e) => e.best_ic), showSymbol: true, lineStyle: { color: colors.accent, width: 2 } }],
              } as never} />
              <div style={{ fontSize: 11.5, color: colors.muted, margin: "4px 0 10px" }}>
                共评估 {gpResult.n_candidates_evaluated} 个候选表达式 · 入库 {gpResult.saved_ids.length} 条
              </div>
              {/* 精英表 */}
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
                <thead><tr style={{ color: colors.muted, textAlign: "left" }}>
                  <th style={{ padding: "7px 10px" }}>排名</th><th>表达式</th><th>IC</th><th>ICIR</th><th>t 值</th><th>评级</th><th>复杂度</th><th>危机</th><th></th>
                </tr></thead>
                <tbody>
                  {gpResult.elites.map((el, i) => {
                    const cx = (el as { complexity?: { score?: number } }).complexity?.score ?? "—";
                    const ci = (el as { crisis_info?: { crisis_ic?: number; crisis_used?: number } }).crisis_info;
                    return (
                      <tr key={i} style={{ borderTop: `1px solid ${colors.border}` }}>
                        <td style={{ padding: "7px 10px" }}>{i + 1}</td>
                        <td style={{ fontFamily: "'SF Mono', Menlo, monospace", fontSize: 11.5 }}>{el.expr}</td>
                        <td className="num">{el.ic_mean.toFixed(4)}</td>
                        <td className="num">{el.icir.toFixed(3)}</td>
                        <td className="num">{el.t_stat.toFixed(2)}</td>
                        <td><RatingBadge rating={el.rating} colors={colors} /></td>
                        <td className="num">{cx}</td>
                        <td className="num">{ci ? `${ci.crisis_ic}`.slice(0, 6) + ` (${ci.crisis_used})` : "—"}</td>
                        <td><a style={{ color: colors.accent, cursor: "pointer", fontSize: 12 }} onClick={() => setReport(el)}>报告</a></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              <div style={{ marginTop: 8, fontSize: 11.5, color: colors.muted }}>
                IC 为负的因子并非无效——排序取反即得正向因子，可在表达式中加负号使用。
              </div>
            </div>
          )}
        </Card>
      </div>

      {/* —— 报告 —— */}
      {report ? <MineReport report={report} colors={colors} /> : (
        <Card colors={colors} pad={40}>
          <div style={{ textAlign: "center", color: colors.muted, fontSize: 13.5, lineHeight: 2 }}>
            输入表达式并点击「开始挖掘」<br />
            将对核心池（约 1800 只）逐月截面计算，输出完整有效性检验报告
          </div>
        </Card>
      )}

      {/* —— 历史 —— */}
      <div style={{ marginTop: 16 }}>
        <Card title={`挖掘历史（${history.length}）`} colors={colors} pad={0}>
          {history.length > 0 ? (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
              <thead>
                <tr style={{ color: colors.muted, textAlign: "left" }}>
                  <th style={{ padding: "9px 16px" }}>#</th><th>名称</th><th>表达式</th>
                  <th>IC 均值</th><th>ICIR</th><th>评级</th><th>时间</th><th></th>
                </tr>
              </thead>
              <tbody>
                {history.map((h) => (
                  <tr key={h.id} style={{ borderTop: `1px solid ${colors.border}` }}>
                    <td style={{ padding: "9px 16px" }}>{h.id}</td>
                    <td>
                      <a onClick={() => api.factorMineDetail(h.id).then(setReport)} style={{ color: colors.accent, cursor: "pointer", fontWeight: 500 }}>{h.name}</a>
                    </td>
                    <td style={{ fontFamily: "monospace", fontSize: 11.5, maxWidth: 300, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{h.expr}</td>
                    <td className="num">{h.ic_mean?.toFixed(4) ?? "—"}</td>
                    <td className="num">{h.icir?.toFixed(3) ?? "—"}</td>
                    <td><RatingBadge rating={h.rating} colors={colors} /></td>
                    <td style={{ color: colors.muted }}>{(h.created_at || "").slice(5, 16)}</td>
                    <td>
                      <button onClick={() => { if (window.confirm(`删除 #${h.id}？`)) api.factorMineDelete(h.id).then(loadHistory); }}
                        style={{ fontSize: 11.5, color: colors.up, background: "none", border: 0, cursor: "pointer" }}>删除</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div style={{ padding: 24, textAlign: "center", color: colors.muted, fontSize: 13 }}>暂无历史记录</div>
          )}
        </Card>
      </div>

      <style>{`.num{fontVariantNumeric:tabular-nums}`}</style>

      {/* 大模型通道配置：改完立即生效，无需重启后端 */}
      <LlmConfigModal
        open={cfgOpen}
        colors={colors}
        onClose={() => setCfgOpen(false)}
        onChanged={loadAiStatus}
      />
    </div>
  );
}

/* ============ 报告组件 ============ */
function MineReport({ report, colors }: { report: FactorMineReport; colors: ThemeColors }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {/* 评级横幅 */}
      <Card colors={colors}>
        <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
          <Badge text={report.rating} color={ratingColor(report.rating)} />
          <span style={{ fontSize: 17, fontWeight: 700 }}>{report.name}</span>
          <code style={{ fontSize: 12.5, color: colors.muted, background: colors.tableStripe, padding: "4px 10px", borderRadius: 6 }}>{report.expr}</code>
          <span style={{ flex: 1 }} />
          <span style={{ fontSize: 12, color: colors.muted }}>{report.n_periods} 期截面 · {report.n_stocks} 只 · 未来 {report.forward_days} 日</span>
          {report.id && (
            <span style={{ flex: 1 }} />
          )}
          {report.id && (
            <button
              onClick={async () => {
                try {
                  const r = await api.factorRegistryRegister({
                    name: report.name, expr: report.expr, category: "mined",
                    source_id: report.id, ic_mean: report.ic_mean,
                  });
                  window.alert(r.existed ? "该因子已在注册表中" : `已登记入因子库 (#${r.id}, ${r.status})`);
                } catch (e) {
                  window.alert(`登记失败: ${(e as Error).message}`);
                }
              }}
              style={{ padding: "4px 12px", borderRadius: 6, border: `1px solid ${colors.accent}`,
                       background: "transparent", color: colors.accent, cursor: "pointer", fontSize: 12 }}
            >
              登记入因子库
            </button>
          )}
        </div>
      </Card>

      {/* KPI */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(150px,1fr))", gap: 10 }}>
        <KpiCard label="IC 均值" value={report.ic_mean.toFixed(4)} sub=">0 正向选股能力"
          tone={Math.abs(report.ic_mean) >= 0.02 ? "accent" : "neutral"} colors={colors} />
        <KpiCard label="ICIR" value={report.icir.toFixed(3)} sub="IC 稳定性"
          tone={Math.abs(report.icir) >= 0.3 ? "accent" : "neutral"} colors={colors} />
        <KpiCard label="t 统计量" value={report.t_stat.toFixed(2)} sub={Math.abs(report.t_stat) >= 2 ? "|t|≥2 显著" : "不显著"}
          tone={Math.abs(report.t_stat) >= 2 ? "up" : "neutral"} colors={colors} />
        <KpiCard label="IC 胜率" value={`${(report.ic_win_rate * 100).toFixed(0)}%`} sub="IC>0 占比" tone="neutral" colors={colors} />
        <KpiCard label="多空单调差" value={report.monotonic_spread.toFixed(4)} sub="Top−Bottom 组均值差"
          tone={Math.abs(report.monotonic_spread) > 0.01 ? "down" : "neutral"} colors={colors} />
        <KpiCard label="单调评分" value={report.mono_score.toFixed(2)} sub="相邻组同向占比"
          tone={report.mono_score >= 0.6 ? "down" : "neutral"} colors={colors} />
      </div>

      {/* 图表三宫格 */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
        <Card title="IC 时序（虚线=均值）" colors={colors} pad={8}><IcChart report={report} colors={colors} /></Card>
        <Card title={`分组未来收益（${report.groups} 组，低→高）`} colors={colors} pad={8}><GroupChart report={report} colors={colors} /></Card>
      </div>
      <Card title="多空组合累计收益（Top − Bottom）" colors={colors} pad={8}><LsChart report={report} colors={colors} /></Card>

      {/* 相关性 */}
      {Object.keys(report.corr_with_existing || {}).length > 0 && (
        <Card title="与现有 14 因子的截面相关性（冗余度分析）" colors={colors}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(96px,1fr))", gap: 7 }}>
            {Object.entries(report.corr_with_existing)
              .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))
              .map(([k, v]) => {
                const strong = Math.abs(v) > 0.5;
                return (
                  <div key={k} style={{
                    textAlign: "center", padding: "8px 4px", borderRadius: 8,
                    background: strong ? `${colors.up}14` : colors.tableStripe,
                    border: `1px solid ${strong ? colors.up : colors.border}`,
                  }}>
                    <div style={{ fontSize: 11, color: colors.muted }}>{factorCn(k)}</div>
                    <div style={{ fontSize: 16, fontWeight: 700, fontVariantNumeric: "tabular-nums", color: strong ? colors.up : colors.text }}>{v.toFixed(2)}</div>
                  </div>
                );
              })}
          </div>
          <div style={{ marginTop: 8, fontSize: 12, color: colors.muted }}>
            最大 |ρ| = <b>{report.max_abs_corr}</b> —{" "}
            {report.max_abs_corr > 0.7 ? "⚠ 与现有因子高度重复，建议放弃或改造" :
             report.max_abs_corr > 0.5 ? "⚠ 中度重复，可考虑正交化处理" : "✓ 与现有因子独立性良好"}
          </div>
        </Card>
      )}
    </div>
  );
}

function IcChart({ report, colors }: { report: FactorMineReport; colors: ThemeColors }) {
  return (
    <EChart height={215} option={{
      tooltip: { trigger: "axis" },
      grid: { left: 44, right: 14, top: 18, bottom: 40 },
      xAxis: { type: "category", data: report.ic_series.map((p) => p.date.slice(2)), axisLabel: { fontSize: 9.5, interval: Math.ceil(report.ic_series.length / 6) } },
      yAxis: { type: "value", splitLine: { lineStyle: { color: colors.border, opacity: 0.5 } } },
      series: [
        { name: "IC", type: "bar", data: report.ic_series.map((p) => ({ value: p.ic, itemStyle: { color: p.ic >= 0 ? colors.up : colors.down, opacity: 0.75 } })), barWidth: "55%" },
        { name: "均值", type: "line", data: report.ic_series.map(() => report.ic_mean), showSymbol: false, lineStyle: { color: colors.accent, type: "dashed", width: 1.5 }, symbolSize: 0 },
      ],
      legend: { show: false },
    } as never} />
  );
}

function GroupChart({ report, colors }: { report: FactorMineReport; colors: ThemeColors }) {
  const gKeys = Object.keys(report.group_means).map(Number).sort((a, b) => a - b);
  return (
    <EChart height={215} option={{
      tooltip: {},
      grid: { left: 50, right: 14, top: 18, bottom: 30 },
      xAxis: { type: "category", data: gKeys.map((g) => `第${g}组`), axisLabel: { fontSize: 10.5 } },
      yAxis: { type: "value", splitLine: { lineStyle: { color: colors.border, opacity: 0.5 } } },
      series: [{
        type: "bar",
        data: gKeys.map((g, i) => ({ value: report.group_means[g], itemStyle: { opacity: 0.85, color: `rgba(${report.group_means[g] >= 0 ? "217,44,44" : "26,138,72"},${0.45 + 0.45 * (i / gKeys.length)})` } })),
        barWidth: "48%",
      }],
    } as never} />
  );
}

function LsChart({ report, colors }: { report: FactorMineReport; colors: ThemeColors }) {
  return (
    <EChart height={190} option={{
      tooltip: { trigger: "axis" },
      grid: { left: 50, right: 14, top: 22, bottom: 34 },
      xAxis: { type: "category", data: report.long_short.map((p) => p[0].slice(2)), axisLabel: { fontSize: 9.5, interval: Math.ceil(report.long_short.length / 8) } },
      yAxis: { type: "value", scale: true, splitLine: { lineStyle: { color: colors.border, opacity: 0.5 } } },
      series: [{ name: "多空累计", type: "line", data: report.long_short.map((p) => p[1]), showSymbol: false, lineStyle: { color: "#8e5cf0", width: 2 }, areaStyle: { opacity: 0.07 } }],
    } as never} />
  );
}

/* ============ 小工具 ============ */
function Labeled({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label style={{ fontSize: 12, display: "block" }}>
      <span style={{ color: "#7a8299" }}>{label}</span>
      {children}
    </label>
  );
}

function RatingBadge({ rating, colors }: { rating: string; colors: ThemeColors }) {
  return <Badge text={rating} color={ratingColor(rating)} soft />;
}

function factorCn(key: string): string {
  const m: Record<string, string> = {
    ep: "EP 价值", bp: "BP 账面市值", momentum: "动量", reversal: "反转", low_vol: "低波动",
    size: "规模", beta: "Beta", idio_vol: "特异波动", skewness: "偏度", tail_risk: "尾部风险",
    roe: "ROE", revenue_yoy: "营收增速", profit_yoy: "利润增速", earnings_surprise: "盈余惊喜",
  };
  return m[key] || key;
}

const ratingColor = (r: string) => (r === "优秀" ? "#3ba272" : r === "可用" ? "#2456c8" : r === "弱" ? "#c8860d" : "#d92c2c");
