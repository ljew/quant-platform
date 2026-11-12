import { useCallback, useEffect, useState, type CSSProperties } from "react";
import EChart from "../components/EChart";
import SentimentStrategyCard from "../components/SentimentStrategyCard";
import LlmConfigModal from "../components/LlmConfigModal";
import { Badge, Btn, Card, KpiCard, PageHeader, inputStyle } from "../components/ui";
import EvalBadges from "../components/EvalBadges";
import {
  api, AiExplainResult, AiGenerateResult, AiStatus,
  FactorHorizonRow, FactorMineReport, FactorMineSummary,
  FactorWorksetCorrPair, FactorWorksetMember, FactorWorksetPayload, FactorWorksetPreview,
  GpMineResult, NewsEventReport,
  researchApi,
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
  // W47-2：主路径默认对代表集残差后再评 IC
  const [orthogonalize, setOrthogonalize] = useState(true);
  const [fns, setFns] = useState<Record<string, Record<string, string>>>({});
  const [showFns, setShowFns] = useState(false);
  const [valid, setValid] = useState<{ ok: boolean; error?: string; sample_value?: number | null } | null>(null);
  const [running, setRunning] = useState(false);
  const [report, setReport] = useState<FactorMineReport | null>(null);
  // 两屏：列表（造因子 + 看历史）｜详情（一份完整检验报告）。
  // 原来是一页到底，历史压在最下面，看报告要滚过 5 个挖掘区块。
  const [view, setView] = useState<"list" | "detail">("list");
  const [history, setHistory] = useState<FactorMineSummary[]>([]);
  const [error, setError] = useState("");
  // 新闻情绪择时（市场级 · 非结构化数据）。个股 news_senti 仍因覆盖不足不参与截面挖掘。
  const [newsSeries, setNewsSeries] = useState<{ date: string; n_finance: number; net_sentiment: number | null }[]>([]);
  const [newsHorizon, setNewsHorizon] = useState(5);
  const [newsTesting, setNewsTesting] = useState(false);
  const [newsReport, setNewsReport] = useState<NewsEventReport | null>(null);
  // 数据源：both=合并（默认，样本最多）/ news=东财新闻 / mp=公众号语料
  const [newsSource, setNewsSource] = useState<"news" | "mp" | "both">("both");
  // 社交关注度（雪球/微博）：只有热度、没正文，所以只展示不参与打分
  const [socialHeat, setSocialHeat] = useState<
    { date: string; source: string; n_items: number; n_finance: number; heat_total: number; heat_top10: number }[]
  >([]);
  // both 没有单一时序，图表退化为公众号
  const chartSource: "news" | "mp" = newsSource === "news" ? "news" : "mp";
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
  const [workset, setWorkset] = useState<FactorWorksetPayload | null>(null);
  const [wsRemoving, setWsRemoving] = useState<number | null>(null);

  const loadHistory = useCallback(() => {
    api.factorMineResults(15).then(setHistory).catch(() => {});
  }, []);
  const loadWorkset = useCallback(() => {
    api.factorWorksetGet().then(setWorkset).catch(() => setWorkset({ n: 0, members: [] }));
  }, []);
  const removeFromWorkset = async (rid: number) => {
    setWsRemoving(rid);
    try {
      const out = await api.factorWorksetRemove(rid);
      if (out.workset) setWorkset(out.workset);
      else loadWorkset();
    } catch {
      /* 列表移出失败时保持当前卡，不打断页面 */
    } finally {
      setWsRemoving(null);
    }
  };

  // 单独抽出来：配好 key 并重启后端后，页面上点「重新检测」即可生效，无需刷新
  const loadAiStatus = useCallback(() => {
    api.factorAiStatus().then(setAiStatus).catch(() => setAiStatus(null));
  }, []);

  useEffect(() => {
    api.factorFunctions().then(setFns).catch(() => {});
    api.factorGpDirections().then(setGpDirs).catch(() => {});
    loadAiStatus();
    loadHistory();
    loadWorkset();
  }, [loadAiStatus, loadHistory, loadWorkset]);
  useEffect(() => {
    if (view === "list") loadWorkset();
  }, [view, loadWorkset]);

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
      if (r.elites.length > 0) { setReport(r.elites[0]); setView("detail"); }
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

  // force=true 跳过前端重复检查，强制重算并另存一条历史记录
  const runMine = async (force = false) => {
    setRunning(true);
    setError("");
    setReport(null);
    try {
      const r = await api.factorMine({ expr, name, start, end, groups, forward, force, orthogonalize });
      setReport(r);
      setView("detail");
      loadHistory();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRunning(false);
    }
  };

  useEffect(() => {
    api.factorNewsDaily(600, chartSource).then(setNewsSeries).catch(() => {});
  }, [chartSource]);

  useEffect(() => {
    api.factorSocialHeat(7).then(setSocialHeat).catch(() => {});
  }, []);

  const runNewsTest = async () => {
    setNewsTesting(true);
    try {
      setNewsReport(await api.factorNewsTest(0.10, newsHorizon, newsSource));
    } catch (e) {
      setError(`情绪检验失败: ${(e as Error).message}`);
    } finally {
      setNewsTesting(false);
    }
  };

  return (
    <div style={{ maxWidth: 1240, margin: "0 auto" }}>
      <PageHeader
        title="因子实验室"
        desc="在这里造新因子（表达式 / GP 挖掘 / AI 生成）· 造好了去「策略库」上岗：入册为研究卡 → 登记 → 候选 → 启用"
        actions={
          <div style={{ display: "flex", gap: 8 }}>
            <Btn kind="ghost" small
              onClick={() => {
                setView("list");
                // 等下一帧再滚：详情切回列表时节点刚挂载，立即 scroll 会落空
                setTimeout(() => document.getElementById("mine-history")
                  ?.scrollIntoView({ behavior: "smooth", block: "start" }), 60);
              }}>
              挖掘历史（{history.length}）
            </Btn>
            <Btn kind="ghost" small
              onClick={() => {
                setView("list");
                setTimeout(() => document.getElementById("factor-workset")
                  ?.scrollIntoView({ behavior: "smooth", block: "start" }), 60);
              }}>
              工作集（{workset?.n ?? 0}/5）
            </Btn>
            <Btn onClick={() => setShowFns(!showFns)} kind="ghost" small>函数参考</Btn>
          </div>
        }
      />

      {/* —— 舆情策略（情绪择时 + 重点行业 + 重点个股）——
          老刘 2026-11-08：这里不需要再显示热点简报，换成可执行的舆情策略。 */}
      <div style={{ marginBottom: 16 }}>
        <SentimentStrategyCard />
      </div>

      {view === "list" && (<>
      <WorksetCard workset={workset} colors={colors} onRemove={removeFromWorkset} removingId={wsRemoving} />
      {/* —— 新闻情绪择时（市场级 · 非结构化数据）—— */}
      <div style={{ marginBottom: 16 }}>
        <Card
          title="新闻情绪择时（市场级 · 非结构化数据）"
          colors={colors}
          extra={<span style={{ fontSize: 11.5, color: colors.muted }}>词典法打分 · 新闻 + 公众号语料</span>}
        >
          <div style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 8 }}>
            <span style={{ fontSize: 12.5 }}>净情绪 时序（正=多空词频偏多）</span>
            <span style={{ flex: 1 }} />
            <label style={{ fontSize: 12, color: colors.muted }}>数据源
              <select value={newsSource} onChange={(e) => setNewsSource(e.target.value as "news" | "mp" | "both")} style={{ ...inputStyle(colors), width: 118, marginLeft: 6 }}>
                <option value="both">合并（样本最多）</option>
                <option value="mp">公众号语料</option>
                <option value="news">东财新闻</option>
              </select>
            </label>
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
          {newsReport && newsReport.ok && (
            <>
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
              {newsReport.verdict && (
                <div style={{
                  marginTop: 10, fontSize: 12.5, lineHeight: 1.6,
                  color: (newsReport.edge_long_vs_base || 0) > 0 ? colors.down : colors.up,
                }}>
                  {newsReport.verdict}
                </div>
              )}
            </>
          )}
          {newsReport && !newsReport.ok && (
            <div style={{ marginTop: 8, fontSize: 12.5, color: colors.up }}>{newsReport.error}</div>
          )}
          {socialHeat.length > 0 && (
            <div style={{
              marginTop: 10, paddingTop: 10, borderTop: `1px solid ${colors.border}`,
              fontSize: 11.5, color: colors.muted, display: "flex", gap: 18, flexWrap: "wrap",
            }}>
              <span style={{ fontWeight: 600 }}>社交关注度</span>
              {socialHeat.slice(0, 4).map((r) => (
                <span key={r.date + r.source}>
                  {r.date.slice(5)} {r.source}：{r.n_items} 条
                  {r.source === "微博" ? `（财经 ${r.n_finance}）` : ` · 热度 ${Math.round(r.heat_total)}`}
                </span>
              ))}
              <span style={{ opacity: 0.75 }}>仅热度、无正文，不参与多空打分</span>
            </div>
          )}
          <div style={{ marginTop: 8, fontSize: 11.5, color: colors.muted }}>
            看多日 = 净情绪 ≥ 前 10% 分位；观察其后续 {newsHorizon} 日中证800 收益是否系统性跑赢基线。
            该指标为市场级择时信号，与截面选股因子互补。
            <b>个股新闻情绪（news_senti）因覆盖不足，暂不参与截面挖矿。</b>
          </div>
        </Card>
      </div>

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
                试用模式只识别常见表述的组合（估值 / 成长 / 波动 / 量能 / 动量 / 现金流 / 市值）。
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
          <label
            style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 12, fontSize: 12.5, cursor: "pointer" }}
            title="候选因子对动量/反转/低波/价值/规模做横截面回归取残差，再算 IC——度量增量预测力；相关矩阵仍保留"
          >
            <input
              type="checkbox"
              checked={orthogonalize}
              onChange={(e) => setOrthogonalize(e.target.checked)}
            />
            对代表集残差后再评 IC
          </label>
          <div style={{ display: "flex", gap: 10, marginTop: 16 }}>
            <Btn onClick={doValidate} kind="warning" small disabled={running}>校验表达式</Btn>
            <Btn onClick={() => runMine()} disabled={running}>{running ? "挖掘中…" : "开始挖掘"}</Btn>
          </div>
          <div style={{ marginTop: 14, fontSize: 11.5, color: colors.muted, lineHeight: 1.7 }}>
            流程：逐期截面计算因子值 → Spearman IC vs 未来收益 → 分组单调性 / 多空累计 /
            与现有 14 因子冗余度 → 综合评级。
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
              title="与主路径同一代表集/算法；候选因子先对动量/反转/低波/价值/规模做横截面回归取残差，再算IC——专挖现有因子之外的增量alpha">
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
                        <td><a style={{ color: colors.accent, cursor: "pointer", fontSize: 12 }}
                          onClick={() => { setReport(el); setView("detail"); }}>报告</a></td>
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

      {/* —— 历史 —— */}
      <div style={{ marginTop: 16 }} id="mine-history">
        <Card title={`挖掘历史（${history.length}）`} colors={colors} pad={0}
          extra={<span style={{ fontSize: 11.5, color: colors.muted }}>
            点因子名进详情 · 详情里可以「入册为研究卡」
          </span>}>
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
                      <a onClick={() => api.factorMineDetail(h.id).then((r) => { setReport(r); setView("detail"); })} style={{ color: colors.accent, cursor: "pointer", fontWeight: 500 }}>{h.name}</a>
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
      </>)}

      {view === "detail" && (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: 12, margin: "4px 0 12px" }}>
            <Btn small kind="ghost" onClick={() => setView("list")}>← 回列表</Btn>
            <span style={{ fontSize: 12.5, color: colors.muted }}>
              {report ? `${report.name} · 挖掘记录 #${report.duplicate_of ?? report.id ?? "—"}` : "没有选中的因子"}
            </span>
          </div>
          {report ? (
            <MineReport report={report} colors={colors} start={start} end={end}
              onForce={() => runMine(true)} />
          ) : (
            <Card colors={colors} pad={40}>
              <div style={{ textAlign: "center", color: colors.muted, fontSize: 13.5, lineHeight: 2 }}>
                {running
                  ? <>正在重算这一份报告…（全区间截面计算，通常 20~60 秒）</>
                  : <>还没有选中的因子。<br />回列表点一条挖掘记录，或新挖一个。</>}
              </div>
            </Card>
          )}
        </>
      )}

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
/** 时间戳统一成 MM-DD HH:mm（后端可能给 ISO「2026-09-18T06:41:04」或「2026-09-18 06:41:04」两种格式） */
function fmtStamp(s: string): string {
  return s.slice(5, 16).replace("T", " ");
}

/** 因子值量纲跨度极大（fcf_yield ~1e-3 到 市值 ~1e2），按量级切换显示精度 */
function fmtNum(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const a = Math.abs(v);
  if (a === 0) return "0";
  if (a >= 1e6 || a < 1e-3) return v.toExponential(2);
  if (a >= 100) return v.toFixed(2);
  if (a >= 1) return v.toFixed(3);
  return v.toFixed(4);
}

function MineReport({ report, colors, start, end, onForce }: { report: FactorMineReport; colors: ThemeColors; start: string; end: string; onForce?: () => void }) {
  const fs = report.factor_stats;
  const [horizons, setHorizons] = useState<FactorHorizonRow[] | null>(null);
  const [horizonsBusy, setHorizonsBusy] = useState(false);
  const [horizonsErr, setHorizonsErr] = useState("");
  const [lifeMsg, setLifeMsg] = useState("");
  const [regId, setRegId] = useState<number | null>(null);
  const [regStatus, setRegStatus] = useState<string | null>(null);
  // 入册为研究卡：把「跑过一次」变成「进决策流程的一张卡」
  const [cardName, setCardName] = useState(report.name);
  const [hypothesis, setHypothesis] = useState("");
  const [failCond, setFailCond] = useState("");
  const [cardState, setCardState] = useState("watch");
  const [drafting, setDrafting] = useState(false);
  const [savingCard, setSavingCard] = useState(false);
  const [cardMsg, setCardMsg] = useState("");
  const [createdId, setCreatedId] = useState<number | null>(null);

  const loadHorizons = useCallback(async () => {
    setHorizonsBusy(true);
    setHorizonsErr("");
    try {
      const r = await api.factorMineHorizons({
        expr: report.expr,
        name: report.name,
        start,
        end,
        forwards: [5, 10, 20, 40],
        // 与主报告同区间/分组，避免热力与 IC 时序口径漂移
        groups: report.groups,
        orthogonalize: report.orthogonalize ?? true,
      });
      setHorizons(r.horizons || []);
    } catch (e) {
      setHorizonsErr((e as Error).message);
      setHorizons(null);
    } finally {
      setHorizonsBusy(false);
    }
  }, [report.expr, report.name, report.groups, start, end]);

  useEffect(() => {
    loadHorizons();
  }, [loadHorizons]);

  useEffect(() => {
    // 查注册表是否已有同名/同源条目，驱动生命周期按钮
    api.factorRegistryList()
      .then((rows) => {
        const hit = rows.find((r) => r.source_id === report.id)
          || rows.find((r) => r.expr === report.expr)
          || rows.find((r) => r.name === report.name);
        if (hit) {
          setRegId(hit.id);
          setRegStatus(hit.status);
        } else {
          setRegId(null);
          setRegStatus(null);
        }
      })
      .catch(() => {});
  }, [report.id, report.expr, report.name]);

  const lifeLabel = (() => {
    if (!regStatus) return "待评估（未登记）";
    if (regStatus === "candidate") return "待评估（candidate）";
    if (regStatus === "enabled") return "已采纳（enabled）";
    if (regStatus === "disabled") return "已拒绝（disabled）";
    return regStatus;
  })();

  const registerFactor = async () => {
    setLifeMsg("");
    try {
      const r = await api.factorRegistryRegister({
        name: report.name, expr: report.expr, category: "mined",
        source_id: report.id, ic_mean: report.ic_mean,
      });
      setRegId(r.id);
      setRegStatus(r.status);
      setLifeMsg(r.existed ? `已在注册表 (#${r.id}, ${r.status})` : `已登记 (#${r.id}, ${r.status})`);
    } catch (e) {
      setLifeMsg(`登记失败: ${(e as Error).message}`);
    }
  };

  /** AI 生成假设草稿。只是「怎么读这个表达式」的解读，不是证据 —— 文案里要说清。 */
  const draftHypothesis = async () => {
    setDrafting(true);
    setCardMsg("");
    try {
      const r = await api.factorAiExplain(report.expr);
      if (!r.ok) throw new Error(r.error || "AI 未返回内容");
      const parts = [
        r.name ? `${r.name}` : "",
        r.logic || "",
        r.direction ? `方向：${r.direction}` : "",
        r.caveats ? `注意：${r.caveats}` : "",
      ].filter(Boolean);
      setHypothesis(parts.join("\n\n"));
      setCardMsg("已生成草稿 —— 这是模型对表达式的解读，不是证据。请按你自己的理解改写后再入册。");
    } catch (e) {
      setCardMsg(`生成失败：${(e as Error).message}。可以直接手写假设。`);
    } finally {
      setDrafting(false);
    }
  };

  /** 入册：建一张 factor 卡，evidence 里带 source_mine_id —— 卡片能回答「来自哪次挖掘」。 */
  const registerAsCard = async () => {
    if (!cardName.trim()) { setCardMsg("名称不能为空"); return; }
    setSavingCard(true);
    setCardMsg("");
    try {
      const mineId = report.duplicate_of ?? report.id ?? null;
      const card = await researchApi.create({
        card_type: "factor",
        // key 用挖掘 id：中文名没法做 slug，而「可追溯到哪次挖掘」比可读更重要
        key: `f_mine_${mineId ?? Date.now()}`,
        name: cardName.trim(),
        hypothesis: hypothesis.trim(),
        data_spec: `${start} ~ ${end} · forward ${report.forward_days}d · ${report.groups} 组 · 核心池`,
        evidence: {
          source_mine_id: mineId,
          source_expr: report.expr,
          rating: report.rating,
          ic_mean: report.ic_mean,
          icir: report.icir,
          t_stat: report.t_stat,
          ic_win_rate: report.ic_win_rate,
          n_periods: report.n_periods,
          n_stocks: report.n_stocks,
          mono_score: report.mono_score,
          monotonic_spread: report.monotonic_spread,
          max_abs_corr: report.max_abs_corr,
          // 多重检验一起带上：离开这份上下文，t=3 会被当成 t=3 那么简单
          n_tested_cum: report.n_tested_cum ?? report.multiple_testing?.n_tested_cum ?? null,
          luck_bar: report.luck_bar ?? report.multiple_testing?.luck_bar ?? null,
          bonferroni_alpha: report.bonferroni_alpha ?? report.multiple_testing?.bonferroni_alpha ?? null,
        },
        fail_condition: failCond.trim(),
        state: cardState,
        history_note: `由因子实验室入册 · 挖掘记录 #${mineId ?? "?"}`,
        actor: "human",
      });
      setCreatedId(card.id);
      setCardMsg(`已建卡 #${card.id} —— 在「策略研究 → 策略库」里能看到。`);
    } catch (e) {
      setCardMsg(`建卡失败：${(e as Error).message}`);
    } finally {
      setSavingCard(false);
    }
  };

  const adoptFactor = async () => {
    setLifeMsg("");
    try {
      let id = regId;
      if (id == null) {
        const r = await api.factorRegistryRegister({
          name: report.name, expr: report.expr, category: "mined",
          source_id: report.id, ic_mean: report.ic_mean,
        });
        id = r.id;
        setRegId(r.id);
        setRegStatus(r.status);
      }
      // 若已是 enabled 则无需再 toggle；candidate/disabled → 需要走到 enabled
      if (regStatus !== "enabled") {
        // disabled 先回 candidate，再 toggle → enabled（两次）
        if (regStatus === "disabled") {
          const mid = await api.factorRegistryToggle(id!);
          setRegStatus(mid.status);
        }
        const cur = await api.factorRegistryList();
        const row = cur.find((x) => x.id === id);
        if (row && row.status === "candidate") {
          const out = await api.factorRegistryToggle(id!);
          setRegStatus(out.status);
          setLifeMsg(`已采纳 → ${out.status}`);
        } else if (row && row.status === "enabled") {
          setRegStatus("enabled");
          setLifeMsg("已是 enabled");
        } else {
          setLifeMsg(`当前状态 ${row?.status ?? "未知"}，请刷新后再试`);
        }
      } else {
        setLifeMsg("已是 enabled");
      }
    } catch (e) {
      setLifeMsg(`采纳失败: ${(e as Error).message}`);
    }
  };

  const rejectFactor = async () => {
    setLifeMsg("");
    try {
      let id = regId;
      if (id == null) {
        // 无门禁要求的「拒绝」：先登记再禁用会撞门禁。改为仅提示——
        // 拒绝路径：若已登记且 enabled → toggle 到 disabled；若 candidate → 直接记 disabled 需两步。
        setLifeMsg("尚未登记。过拟合/不合格因子可不必入库；若需留痕请先在 OOS 通过后登记再拒绝。");
        return;
      }
      if (regStatus === "enabled") {
        const out = await api.factorRegistryToggle(id);
        setRegStatus(out.status);
        setLifeMsg(`已拒绝 → ${out.status}`);
      } else if (regStatus === "candidate") {
        // candidate → enabled 会被门禁拦住；我们需要 candidate → disabled。
        // 当前 toggle：candidate→enabled, enabled→disabled, else→candidate。
        // 无直接 candidate→disabled；用 notes 场景下先尝试启用失败则只能提示人工。
        // 简化：若 OOS 未过，启用会 400；此时前端标记本地「拒绝」文案，并建议保留 candidate。
        try {
          const out = await api.factorRegistryToggle(id);
          if (out.status === "enabled") {
            const out2 = await api.factorRegistryToggle(id);
            setRegStatus(out2.status);
            setLifeMsg(`已拒绝 → ${out2.status}`);
          } else {
            setRegStatus(out.status);
            setLifeMsg(`状态 → ${out.status}`);
          }
        } catch (e) {
          setLifeMsg(`无法经启用再拒绝（门禁）：${(e as Error).message}。可保留 candidate，禁止 enabled。`);
        }
      } else {
        setLifeMsg(`当前已是 ${regStatus}`);
      }
    } catch (e) {
      setLifeMsg(`拒绝失败: ${(e as Error).message}`);
    }
  };

  const statCells: [string, number | null][] = fs ? [
    ["均值", fs.mean], ["标准差", fs.std], ["最小值", fs.min], ["P25", fs.p25],
    ["中位数", fs.p50], ["P75", fs.p75], ["最大值", fs.max], ["偏度", fs.skew],
  ] : [];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {report.duplicate_of != null && (
        <div style={{
          display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap",
          padding: "11px 15px", borderRadius: 10,
          background: colors.tableStripe, border: `1px solid ${colors.border}`,
          borderLeft: `3px solid ${colors.accent}`,
        }}>
          <span style={{ fontSize: 13, fontWeight: 500 }}>表达式未变化</span>
          <span style={{ fontSize: 12.5, color: colors.muted }}>
            与历史 #{report.duplicate_of}
            {report.duplicate_created_at ? `（${fmtStamp(report.duplicate_created_at)}）` : ""}
            的「表达式 + 区间 + 分组数 + forward」完全一致，本次直接回放该结果，未重算、未新增记录。
          </span>
          <span style={{ flex: 1 }} />
          {onForce && (
            <button onClick={onForce} style={{
              padding: "4px 12px", borderRadius: 6, border: `1px solid ${colors.accent}`,
              background: "transparent", color: colors.accent, cursor: "pointer", fontSize: 12,
            }}>仍然重新挖掘</button>
          )}
        </div>
      )}

      <Card colors={colors}>
        <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
          <Badge text={report.rating} color={ratingColor(report.rating)} />
          <Badge
            text={report.orthogonalize === false ? "残差IC·关" : "残差IC·开"}
            color={report.orthogonalize === false ? "#888" : "#3ba272"}
            soft
          />
          <EvalBadges
            cost_profile={report.cost_profile}
            valuation_pit={report.valuation_pit}
            pead_pit={report.pead_pit}
            oos={report.oos}
            colors={colors}
          />
          <span style={{ fontSize: 17, fontWeight: 700 }}>{report.name}</span>
          <code style={{ fontSize: 12.5, color: colors.muted, background: colors.tableStripe, padding: "4px 10px", borderRadius: 6 }}>{report.expr}</code>
          <span style={{ flex: 1 }} />
          <span style={{ fontSize: 12, color: colors.muted }}>{report.n_periods} 期截面 · {report.n_stocks} 只 · 未来 {report.forward_days} 日</span>
        </div>
      </Card>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(150px,1fr))", gap: 10 }}>
        <KpiCard label="IC 均值" value={report.ic_mean.toFixed(4)} sub={report.orthogonalize === false ? ">0 正向选股能力（raw）" : ">0 正向选股能力（主路=残差）"}
          tone={Math.abs(report.ic_mean) >= 0.02 ? "accent" : "neutral"} colors={colors} />
        <KpiCard
          label="IC raw"
          value={report.ic_raw?.ic_mean != null ? report.ic_raw.ic_mean.toFixed(4) : report.ic_mean.toFixed(4)}
          sub="原始截面 IC"
          tone="neutral"
          colors={colors}
        />
        <KpiCard
          label="IC resid"
          value={report.ic_resid?.ic_mean != null ? report.ic_resid.ic_mean.toFixed(4) : "—"}
          sub={report.orthogonalize === false ? "残差化未开启" : "残差后再评"}
          tone={report.ic_resid?.ic_mean != null && Math.abs(report.ic_resid.ic_mean) >= 0.02 ? "accent" : "neutral"}
          colors={colors}
        />
        <KpiCard label="ICIR" value={report.icir.toFixed(3)} sub="IC 稳定性"
          tone={Math.abs(report.icir) >= 0.3 ? "accent" : "neutral"} colors={colors} />
        <KpiCard label="t 统计量" value={report.t_stat.toFixed(2)}
          sub={(() => {
            const gate = report.t_thresholds?.["优秀"] ?? report.multiple_testing?.t_thresholds?.["优秀"] ?? 2;
            return Math.abs(report.t_stat) >= gate ? `|t|≥${gate} 达优秀门` : `优秀门 |t|≥${gate}`;
          })()}
          tone={(() => {
            const gate = report.t_thresholds?.["优秀"] ?? report.multiple_testing?.t_thresholds?.["优秀"] ?? 2;
            return Math.abs(report.t_stat) >= gate ? "up" : "neutral";
          })()} colors={colors} />
        <KpiCard label="IC 胜率" value={`${(report.ic_win_rate * 100).toFixed(0)}%`} sub="IC>0 占比" tone="neutral" colors={colors} />
        <KpiCard label="多空单调差" value={report.monotonic_spread.toFixed(4)} sub="Top−Bottom 组均值差"
          tone={Math.abs(report.monotonic_spread) > 0.01 ? "down" : "neutral"} colors={colors} />
        <KpiCard label="单调评分" value={report.mono_score.toFixed(2)} sub="相邻组同向占比"
          tone={report.mono_score >= 0.6 ? "down" : "neutral"} colors={colors} />
      </div>

      {(report.n_tested != null || report.n_tested_cum != null || report.multiple_testing) && (
        <Card title="多重检验" colors={colors}>
          <div style={{ display: "flex", flexDirection: "column", gap: 8, fontSize: 13 }}>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 14, color: colors.text }}>
              <span>累计检验 N=<b>{report.n_tested_cum ?? report.multiple_testing?.n_tested_cum ?? "—"}</b></span>
              <span>本次评级 N=<b>{report.n_tested ?? report.multiple_testing?.n_tested ?? "—"}</b></span>
              <span>luck_bar=<b>{(() => { const v = report.luck_bar ?? report.multiple_testing?.luck_bar; return v != null ? Number(v).toFixed(3) : "—"; })()}</b></span>
              <span>优秀门 t≥<b>{report.t_thresholds?.["优秀"] ?? report.multiple_testing?.t_thresholds?.["优秀"] ?? "—"}</b></span>
              <span>可用门 t≥<b>{report.t_thresholds?.["可用"] ?? report.multiple_testing?.t_thresholds?.["可用"] ?? "—"}</b></span>
            </div>
            <div style={{ color: colors.muted, fontSize: 12.5 }}>
              Bonferroni α≈{(report.bonferroni_alpha ?? report.multiple_testing?.bonferroni_alpha) != null
                ? Number(report.bonferroni_alpha ?? report.multiple_testing?.bonferroni_alpha).toExponential(3)
                : "—"}（仅提示，不二次抬门）
            </div>
            <div style={{ color: colors.muted, fontSize: 12.5 }}>
              {report.mt_note
                ?? report.multiple_testing?.formula
                ?? "评级门槛随累计挖掘次数抬升（luck_bar），抑制挑运气；非完整 PBO/CSCV。"}
            </div>
            {(report.mt_stale || report.mt_replay_note) && (
              <div style={{
                marginTop: 2, padding: "8px 10px", borderRadius: 8,
                background: "rgba(217, 119, 6, 0.12)", border: "1px solid rgba(217, 119, 6, 0.35)",
                color: "#b45309", fontSize: 12.5,
              }}>
                {report.mt_replay_note
                  ?? `历史回放：评级按当时 N；当前累计已增至 ${report.n_tested_cum ?? "—"}`}
              </div>
            )}
          </div>
        </Card>
      )}

      {report.factor_stats && (
        <Card title="因子值统计（全区间全部样本）" colors={colors}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(100px,1fr))", gap: 7 }}>
            {statCells.map(([label, v]) => (
              <div key={label} style={{
                textAlign: "center", padding: "8px 4px", borderRadius: 8,
                background: colors.tableStripe, border: `1px solid ${colors.border}`,
              }}>
                <div style={{ fontSize: 11, color: colors.muted }}>{label}</div>
                <div style={{ fontSize: 14.5, fontWeight: 500, fontVariantNumeric: "tabular-nums" }}>{fmtNum(v)}</div>
              </div>
            ))}
          </div>
          <div style={{ marginTop: 9, fontSize: 12, color: colors.muted, lineHeight: 1.7 }}>
            样本 <b>{report.factor_stats.n_values}</b> 个（{report.n_periods} 期 × 平均 {report.factor_stats.n_per_period} 只）
            {" · "}取值 <b>{report.factor_stats.n_unique}</b> 种
          </div>
        </Card>
      )}

      {/* —— 五块报告层（W45-lab）—— */}
      <div style={{ fontSize: 13, fontWeight: 600, color: colors.muted, letterSpacing: 0.3 }}>实验室报告层</div>

      {/* 1. IC 时序 */}
      <Card title="① IC 时序（虚线=均值）" colors={colors} pad={8}>
        <IcChart report={report} colors={colors} />
      </Card>

      {/* 2. 多 horizon 热力 */}
      <Card title="② 多 Horizon IC 热力（5 / 10 / 20 / 40）" colors={colors}>
        {horizonsBusy && <div style={{ fontSize: 12.5, color: colors.muted }}>计算中…</div>}
        {horizonsErr && (
          <div style={{ fontSize: 12.5, color: colors.up }}>
            热力失败：{horizonsErr}{" "}
            <button onClick={loadHorizons} style={{ marginLeft: 8, fontSize: 12, cursor: "pointer" }}>重试</button>
          </div>
        )}
        {horizons && horizons.length > 0 && <HorizonHeat rows={horizons} colors={colors} />}
        {!horizonsBusy && !horizonsErr && (!horizons || horizons.length === 0) && (
          <div style={{ fontSize: 12.5, color: colors.muted }}>暂无 horizon 数据</div>
        )}
      </Card>

      {/* 3. 分层收益 */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
        <Card title={`③ 分层收益（${report.groups} 组，低→高）`} colors={colors} pad={8}>
          <GroupChart report={report} colors={colors} />
        </Card>
        <Card title="③ 多空累计（Top − Bottom）" colors={colors} pad={8}>
          <LsChart report={report} colors={colors} />
        </Card>
      </div>

      {/* 4. 相关矩阵热力 */}
      <Card title="④ 与现有因子相关矩阵（热力）" colors={colors}>
        {Object.keys(report.corr_with_existing || {}).length > 0 ? (
          <>
            <CorrHeat corr={report.corr_with_existing} colors={colors} />
            <div style={{ marginTop: 8, fontSize: 12, color: colors.muted }}>
              最大 |ρ| = <b>{report.max_abs_corr}</b> —{" "}
              {report.max_abs_corr > 0.7 ? "⚠ 与现有因子高度重复，建议放弃或改造" :
               report.max_abs_corr > 0.5 ? "⚠ 中度重复，可开启上方残差化后再评" : "✓ 与现有因子独立性良好"}
            </div>
          </>
        ) : (
          <div style={{ fontSize: 12.5, color: colors.muted }}>暂无相关矩阵（可能缺少 factor_daily）</div>
        )}
      </Card>

      {/* 5. 生命周期 */}
      <Card title="⑤ 生命周期（pending_eval → adopted | rejected）" colors={colors}>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center" }}>
          <Badge text={lifeLabel} color={regStatus === "enabled" ? "#3ba272" : regStatus === "disabled" ? "#d92c2c" : colors.accent} />
          <span style={{ fontSize: 12.5, color: colors.muted }}>
            映射：candidate=待评估 · enabled=采纳 · disabled=拒绝。启用须 OOS 通过且评级优秀/可用。
          </span>
          <span style={{ flex: 1 }} />
          <button onClick={registerFactor} style={lifeBtn(colors)}>登记（candidate）</button>
          <button onClick={adoptFactor} style={lifeBtn(colors, true)}>采纳（enabled）</button>
          <button onClick={rejectFactor} style={lifeBtn(colors)}>拒绝（disabled）</button>
        </div>
        {lifeMsg && (
          <div style={{ marginTop: 10, fontSize: 12.5, color: lifeMsg.includes("失败") || lifeMsg.includes("无法") ? colors.up : colors.muted }}>
            {lifeMsg}
          </div>
        )}
      </Card>

      {/* ⑥ 入册为研究卡。没有这一步，实验室里的因子永远是「跑过一次」，
          进不了决策流程 —— 而设计文档 09 节要求「能回答这张卡来自哪次挖掘」。 */}
      <Card title="⑥ 入册为研究卡（进策略库、参与决策）" colors={colors}>
        <div style={{ fontSize: 12.5, color: colors.muted, lineHeight: 1.9, marginBottom: 12 }}>
          来源：挖掘记录 <b style={{ color: colors.text }}>#{report.duplicate_of ?? report.id ?? "—"}</b>
          {" · "}评级 <b style={{ color: colors.text }}>{report.rating}</b>
          {" · "}t=<b style={{ color: colors.text }}>{report.t_stat?.toFixed(2)}</b>
          {" · "}IC 均值 <b style={{ color: colors.text }}>{(report.ic_mean * 100).toFixed(2)}%</b>
          {" · "}累计已检验 <b style={{ color: colors.text }}>{report.n_tested_cum ?? report.multiple_testing?.n_tested_cum ?? "—"}</b> 次
          <br />
          表达式 <code style={{ fontSize: 11.5 }}>{report.expr}</code>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) 160px", gap: 12 }}>
          <label style={{ fontSize: 11.5, color: colors.muted }}>
            卡片名称
            <input value={cardName} onChange={(e) => setCardName(e.target.value)}
              style={{ ...inputStyle(colors), marginTop: 4 }} />
          </label>
          <label style={{ fontSize: 11.5, color: colors.muted }}>
            初始状态
            <select value={cardState} onChange={(e) => setCardState(e.target.value)}
              style={{ ...inputStyle(colors), marginTop: 4 }}>
              <option value="watch">观察（watch）</option>
              <option value="candidate">候选（candidate）</option>
              <option value="enabled">启用（enabled）</option>
            </select>
          </label>
        </div>

        <label style={{ display: "block", fontSize: 11.5, color: colors.muted, marginTop: 10 }}>
          假设（为什么它能预测）
          <textarea value={hypothesis} onChange={(e) => setHypothesis(e.target.value)}
            rows={4} placeholder="例如：放量突破前的缩量代表筹码锁定，突破后资金接力的概率更高……"
            style={{ ...inputStyle(colors), marginTop: 4, resize: "vertical", lineHeight: 1.7 }} />
        </label>

        <label style={{ display: "block", fontSize: 11.5, color: colors.muted, marginTop: 10 }}>
          失效条件（什么情况下这张卡作废）
          <textarea value={failCond} onChange={(e) => setFailCond(e.target.value)}
            rows={2} placeholder="例如：连续 2 个季度 IC 为负，或与动量因子相关性升到 0.7 以上"
            style={{ ...inputStyle(colors), marginTop: 4, resize: "vertical", lineHeight: 1.7 }} />
        </label>

        <div style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 12, flexWrap: "wrap" }}>
          <Btn small kind="ghost" onClick={draftHypothesis} disabled={drafting}>
            {drafting ? "生成中…" : "AI 生成假设草稿"}
          </Btn>
          <Btn small kind="primary" onClick={registerAsCard} disabled={savingCard || createdId != null}>
            {createdId != null ? `已建卡 #${createdId}` : savingCard ? "保存中…" : "入册为研究卡"}
          </Btn>
          {createdId != null && (
            <a href={`#cards/${createdId}`}
              style={{ fontSize: 12, color: colors.accent }}>
              去看这张卡 →
            </a>
          )}
        </div>

        {cardMsg && (
          <div style={{ marginTop: 10, fontSize: 12.5, lineHeight: 1.8,
            color: cardMsg.includes("失败") || cardMsg.includes("不能为空") ? colors.up : colors.muted }}>
            {cardMsg}
          </div>
        )}

        <div style={{ marginTop: 10, fontSize: 11.5, color: colors.muted, lineHeight: 1.8 }}>
          入册会把这次的 IC / t / 单调性 / 相关性 / 多重检验门槛一起写进卡片的证据里 ——
          卡片离开这份上下文，t=3 会被当成 t=3 那么简单。
        </div>
      </Card>

      <WorksetApplySection
        report={report}
        colors={colors}
        regId={regId}
        regStatus={regStatus}
        lifeMsg={lifeMsg}
        onRegistered={(id, status, m) => { setRegId(id); setRegStatus(status); setLifeMsg(m); }}
      />
    </div>
  );
}

function lifeBtn(colors: ThemeColors, primary = false): CSSProperties {
  return {
    padding: "5px 12px", borderRadius: 6,
    border: `1px solid ${colors.accent}`,
    background: primary ? colors.accent : "transparent",
    color: primary ? "#fff" : colors.accent,
    cursor: "pointer", fontSize: 12,
  };
}

function HorizonHeat({ rows, colors }: { rows: FactorHorizonRow[]; colors: ThemeColors }) {
  const vals = rows.map((r) => r.ic_mean ?? 0);
  const maxAbs = Math.max(0.01, ...vals.map((v) => Math.abs(v)));
  return (
    <div>
      <div style={{ display: "grid", gridTemplateColumns: `repeat(${rows.length}, minmax(72px, 1fr))`, gap: 8 }}>
        {rows.map((r) => {
          const v = r.ic_mean ?? 0;
          const intensity = Math.min(1, Math.abs(v) / maxAbs);
          const bg = v >= 0
            ? `rgba(217,44,44,${0.12 + 0.55 * intensity})`
            : `rgba(26,138,72,${0.12 + 0.55 * intensity})`;
          return (
            <div key={r.horizon} style={{
              textAlign: "center", padding: "12px 6px", borderRadius: 8,
              background: bg, border: `1px solid ${colors.border}`,
            }}>
              <div style={{ fontSize: 11, color: colors.muted }}>{r.horizon} 日</div>
              <div style={{ fontSize: 16, fontWeight: 700, fontVariantNumeric: "tabular-nums" }}>
                {r.ic_mean == null ? "—" : r.ic_mean.toFixed(4)}
              </div>
              <div style={{ fontSize: 11, color: colors.muted, marginTop: 4 }}>
                ICIR {r.icir == null ? "—" : r.icir.toFixed(2)} · t {r.t_stat == null ? "—" : r.t_stat.toFixed(2)}
              </div>
              <div style={{ fontSize: 11, marginTop: 4 }}>
                {r.oos?.verdict || "—"}{r.rating ? ` · ${r.rating}` : ""}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}


/* ===== W48-1 workset widgets (inlined into FactorMinePage.tsx) ===== */

const WORKSET_NOTE =
  "少而正交工作集：最多保留 3–5 个低相关因子。质量看残差评级（正交开启时），相关看 raw；不是风险平价，也不改变多重检验计数。";

function goWorksetAccept() {
  window.dispatchEvent(new CustomEvent("quant-nav", { detail: { tab: "btopt", inner: "workset-accept" } }));
}

function WorksetAcceptLink({ colors }: { colors: ThemeColors }) {
  return (
    <a
      href="#btopt/0/workset-accept"
      onClick={(e) => { e.preventDefault(); goWorksetAccept(); }}
      style={{ fontSize: 12.5, color: colors.accent, textDecoration: "none" }}
    >
      去回测验收
    </a>
  );
}

function WorksetCard({
  workset, colors, onRemove, removingId,
}: {
  workset: FactorWorksetPayload | null;
  colors: ThemeColors;
  onRemove: (registryId: number) => void;
  removingId: number | null;
}) {
  const n = workset?.n ?? 0;
  const cap = workset?.cap ?? 5;
  const members = workset?.members ?? [];
  const matrix = workset?.corr_matrix ?? [];
  return (
    <div style={{ marginBottom: 16 }} id="factor-workset">
      <Card
        title={`少而正交工作集（${n}/${cap}）`}
        colors={colors}
        extra={<span style={{ fontSize: 11.5, color: colors.muted }}>最多 5 个 · 相关看 raw</span>}
      >
        {n === 0 ? (
          <div style={{ fontSize: 12.5, color: colors.muted, lineHeight: 1.8 }}>
            工作集为空。从一份优秀/可用且 OOS 通过的报告申请加入，目标 3–5 个低相关因子。
          </div>
        ) : (
          <>
            {n < 3 && (
              <div style={{ fontSize: 12, color: colors.muted, marginBottom: 8 }}>尚未达到目标 3–5</div>
            )}
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
                <thead>
                  <tr style={{ color: colors.muted, textAlign: "left" }}>
                    <th style={{ padding: "6px 8px", fontWeight: 500 }}>名称</th>
                    <th style={{ padding: "6px 8px", fontWeight: 500 }}>表达式</th>
                    <th style={{ padding: "6px 8px", fontWeight: 500 }}>主路评级</th>
                    <th style={{ padding: "6px 8px", fontWeight: 500 }}>主路 t</th>
                    <th style={{ padding: "6px 8px", fontWeight: 500 }}>入集时间</th>
                    <th style={{ padding: "6px 8px", fontWeight: 500 }}>14 列 |ρ|（非入集门）</th>
                    <th style={{ padding: "6px 8px", fontWeight: 500 }}></th>
                  </tr>
                </thead>
                <tbody>
                  {members.map((m) => (
                    <tr key={m.registry_id} style={{ borderTop: `1px solid ${colors.border}` }}>
                      <td style={{ padding: "7px 8px" }}>{m.name || `#${m.registry_id}`}</td>
                      <td style={{ padding: "7px 8px" }}>
                        <code style={{ fontSize: 11 }}>{m.expr_excerpt || m.expr || "—"}</code>
                      </td>
                      <td style={{ padding: "7px 8px" }}>{m.rating ?? "—"}</td>
                      <td style={{ padding: "7px 8px", fontVariantNumeric: "tabular-nums" }}>
                        {m.t_stat == null ? "—" : m.t_stat.toFixed(2)}
                      </td>
                      <td style={{ padding: "7px 8px", color: colors.muted, fontSize: 12 }}>
                        {(m.added_at || "").replace("T", " ") || "—"}
                      </td>
                      <td style={{ padding: "7px 8px", fontVariantNumeric: "tabular-nums" }}>
                        {m.max_abs_corr == null ? "—" : m.max_abs_corr.toFixed(2)}
                      </td>
                      <td style={{ padding: "7px 8px", textAlign: "right" }}>
                        <Btn small kind="ghost"
                          disabled={removingId === m.registry_id}
                          onClick={() => onRemove(m.registry_id)}>
                          {removingId === m.registry_id ? "移出中…" : "移出"}
                        </Btn>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <WorksetHeat members={members} matrix={matrix} colors={colors} />
            {workset?.corr_undetermined && (
              <div style={{ marginTop: 8, fontSize: 12, color: colors.up }}>
                部分成员对相关无法判定（配对不足或缺少最新截面）。
              </div>
            )}
          </>
        )}
        <div style={{ marginTop: 10, fontSize: 11.5, color: colors.muted, lineHeight: 1.8 }}>
          {workset?.note || WORKSET_NOTE}
        </div>
        <div style={{ marginTop: 10 }}>
          <Btn small kind="ghost" onClick={goWorksetAccept}>去回测验收</Btn>
        </div>
      </Card>
    </div>
  );
}

function WorksetHeat({
  members, matrix, colors,
}: {
  members: FactorWorksetMember[];
  matrix: FactorWorksetCorrPair[];
  colors: ThemeColors;
}) {
  if (members.length < 2) return null;
  const rhoOf = (a: number, b: number): number | null | undefined => {
    if (a === b) return 1;
    const hit = matrix.find((p) => (p.a === a && p.b === b) || (p.a === b && p.b === a));
    return hit ? (hit.rho ?? null) : null;
  };
  const cell = (a: number, b: number) => {
    const v = rhoOf(a, b);
    const intensity = v == null ? 0 : Math.min(1, Math.abs(v));
    const bg = v == null
      ? colors.tableStripe
      : v >= 0
        ? `rgba(217,44,44,${0.08 + 0.5 * intensity})`
        : `rgba(26,138,72,${0.08 + 0.5 * intensity})`;
    const strong = v != null && Math.abs(v) >= 0.5;
    return (
      <div key={`${a}-${b}`} style={{
        textAlign: "center", padding: "6px 4px", borderRadius: 6,
        background: bg,
        border: `1px solid ${strong ? colors.up : colors.border}`,
        fontSize: 11.5, fontVariantNumeric: "tabular-nums",
        color: strong ? colors.up : colors.text,
      }}>
        {v == null ? "—" : v.toFixed(2)}
      </div>
    );
  };
  return (
    <div style={{ marginTop: 12 }}>
      <div style={{ fontSize: 12, color: colors.muted, marginBottom: 6 }}>成员间 raw 相关</div>
      <div style={{
        display: "grid",
        gridTemplateColumns: `72px repeat(${members.length}, minmax(56px, 1fr))`,
        gap: 6, alignItems: "center",
      }}>
        <div />
        {members.map((m) => (
          <div key={`h-${m.registry_id}`} style={{ fontSize: 11, color: colors.muted, textAlign: "center" }}>
            {m.name || `#${m.registry_id}`}
          </div>
        ))}
        {members.map((row) => (
          <div key={`r-${row.registry_id}`} style={{ display: "contents" }}>
            <div style={{ fontSize: 11, color: colors.muted }}>{row.name || `#${row.registry_id}`}</div>
            {members.map((col) => cell(row.registry_id, col.registry_id))}
          </div>
        ))}
      </div>
    </div>
  );
}

function WorksetApplySection({
  report, colors, regId, regStatus, lifeMsg, onRegistered,
}: {
  report: FactorMineReport;
  colors: ThemeColors;
  regId: number | null;
  regStatus: string | null;
  lifeMsg: string;
  onRegistered: (id: number, status: string, msg: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<FactorWorksetPreview | null>(null);
  const [msg, setMsg] = useState("");
  const [doneNote, setDoneNote] = useState("");

  const runPreview = async (id: number) => {
    const p = await api.factorWorksetPreview(id);
    setPreview(p);
    return p;
  };

  const apply = async () => {
    setBusy(true);
    setMsg("");
    setDoneNote("");
    try {
      let id = regId;
      if (id == null) {
        try {
          const r = await api.factorRegistryRegister({
            name: report.name, expr: report.expr, category: "mined",
            source_id: report.id, ic_mean: report.ic_mean,
          });
          id = r.id;
          onRegistered(r.id, r.status, r.existed
            ? `已在注册表 (#${r.id}, ${r.status})`
            : `已登记 (#${r.id}, ${r.status})`);
        } catch (e) {
          setMsg(`登记失败: ${(e as Error).message}`);
          return;
        }
      }
      if (id == null) {
        setMsg("登记失败：未获得注册表 id");
        return;
      }
      const p = await runPreview(id);
      if (p.decision === "in_set") return;
      if (p.decision === "reject" || p.ok === false) return;
    } catch (e) {
      setMsg(`预览失败: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const confirmAdmit = async () => {
    if (regId == null) return;
    setBusy(true);
    setMsg("");
    try {
      const replaceId = preview?.decision === "replace"
        ? preview.suggestion?.replace_registry_id
        : undefined;
      const out = await api.factorWorksetAdmit({
        registry_id: regId,
        confirm: true,
        replace_registry_id: replaceId ?? null,
      });
      if (out.ok) {
        setPreview({ ...out, decision: "in_set", in_set: true });
        setDoneNote("已写入工作集。入集不会自动启用。");
      } else {
        setPreview(out);
      }
    } catch (e) {
      setMsg(`写入失败: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const removeMe = async () => {
    if (regId == null) return;
    setBusy(true);
    setMsg("");
    try {
      const out = await api.factorWorksetRemove(regId);
      if (out.ok) {
        setPreview({ decision: "admit", ok: true, in_set: false });
        setDoneNote("已从工作集移出。");
      }
    } catch (e) {
      setMsg(`移出失败: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const decision = preview?.decision;
  const reasons = preview?.reasons ?? preview?.quality?.reasons ?? [];
  const sug = preview?.suggestion;

  return (
    <Card title="⑦ 申请加入工作集" colors={colors}>
      <div style={{ fontSize: 12.5, color: colors.muted, lineHeight: 1.8, marginBottom: 10 }}>
        须先登记。质量门与启用门禁相同；相关看与工作集成员的最新截面 raw Spearman，
        |ρ|&lt;0.50 才可直接入集。入集不会自动启用，也不改变多重检验计数。
      </div>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
        {decision === "in_set" ? (
          <>
            <Badge text="已在工作集" color="#3ba272" />
            <Btn small kind="ghost" onClick={removeMe} disabled={busy}>
              {busy ? "处理中…" : "移出工作集"}
            </Btn>
          </>
        ) : (
          <Btn small kind="primary" onClick={apply} disabled={busy}>
            {busy ? "处理中…" : "申请加入工作集"}
          </Btn>
        )}
        {decision === "admit" && preview?.ok !== false && preview?.in_set !== true && (
          <Btn small onClick={confirmAdmit} disabled={busy}>确认入集</Btn>
        )}
        {decision === "replace" && sug && (
          <Btn small onClick={confirmAdmit} disabled={busy}>
            确认替换「{sug.replace_name || `#${sug.replace_registry_id}`}」
          </Btn>
        )}
      </div>

      {decision === "admit" && preview?.in_set !== true && (
        <div style={{ marginTop: 10, fontSize: 12.5, color: colors.muted }}>
          可以加入工作集。主路 t={preview?.t_stat != null ? preview.t_stat.toFixed(2) : "—"}
          {preview?.corr?.max_abs_corr_ws != null
            ? ` · 与成员最大 |ρ|=${preview.corr.max_abs_corr_ws.toFixed(2)}`
            : ""}
          。请确认后写入。
        </div>
      )}
      {decision === "replace" && sug && (
        <div style={{ marginTop: 10, fontSize: 12.5, lineHeight: 1.8 }}>
          建议替换 <b>{sug.replace_name || `#${sug.replace_registry_id}`}</b>
          {sug.reason ? `：${sug.reason}` : ""}
          <br />
          raw |ρ|={sug.rho == null ? "—" : Math.abs(sug.rho).toFixed(2)}
          {" · "}候选 t={sug.t_candidate == null ? "—" : sug.t_candidate.toFixed(2)}
          {" · "}成员 t={sug.t_member == null ? "—" : sug.t_member.toFixed(2)}
        </div>
      )}
      {(decision === "reject" || preview?.ok === false) && decision !== "in_set" && (
        <div style={{ marginTop: 10, fontSize: 12.5, color: colors.up, lineHeight: 1.8 }}>
          不能加入{preview?.error_code ? `（${preview.error_code}）` : ""}
          {reasons.length > 0 ? `：${reasons.join("；")}` : ""}
          {sug?.conflict_members && sug.conflict_members.length > 0 && (
            <div style={{ marginTop: 4, color: colors.muted }}>
              替换后仍冲突：{sug.conflict_members.map((c) => c.name || `#${c.registry_id}`).join("、")}
            </div>
          )}
        </div>
      )}
      {(msg || lifeMsg.includes("失败")) && decision !== "in_set" && (
        <div style={{ marginTop: 10, fontSize: 12.5, color: colors.up, lineHeight: 1.8 }}>
          {msg}
        </div>
      )}
      {doneNote && (
        <div style={{ marginTop: 10, fontSize: 12.5, color: colors.muted, display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
          <span>{doneNote}</span>
          {doneNote.startsWith("已写入工作集") && <WorksetAcceptLink colors={colors} />}
        </div>
      )}
    </Card>
  );
}


function CorrHeat({ corr, colors }: { corr: Record<string, number>; colors: ThemeColors }) {
  const entries = Object.entries(corr).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]));
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(96px,1fr))", gap: 7 }}>
      {entries.map(([k, v]) => {
        const intensity = Math.min(1, Math.abs(v));
        const bg = v >= 0
          ? `rgba(217,44,44,${0.08 + 0.5 * intensity})`
          : `rgba(26,138,72,${0.08 + 0.5 * intensity})`;
        const strong = Math.abs(v) > 0.5;
        return (
          <div key={k} style={{
            textAlign: "center", padding: "8px 4px", borderRadius: 8,
            background: bg,
            border: `1px solid ${strong ? colors.up : colors.border}`,
          }}>
            <div style={{ fontSize: 11, color: colors.muted }}>{factorCn(k)}</div>
            <div style={{
              fontSize: 16, fontWeight: 700, fontVariantNumeric: "tabular-nums",
              color: strong ? colors.up : colors.text,
            }}>{v.toFixed(2)}</div>
          </div>
        );
      })}
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
