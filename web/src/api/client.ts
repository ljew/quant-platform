/** 后端 API 客户端（对接 FastAPI，前缀 /api/v1）。 */

const BASE = import.meta.env.VITE_API_BASE || "/api/v1";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!r.ok) {
    const e = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(e.detail || r.statusText);
  }
  return r.json() as Promise<T>;
}

export const get = <T>(path: string) => request<T>(path);
export const post = <T>(path: string, body: unknown) =>
  request<T>(path, { method: "POST", body: JSON.stringify(body) });
export const put = <T>(path: string, body: unknown) =>
  request<T>(path, { method: "PUT", body: JSON.stringify(body) });
export const patch = <T>(path: string, body: unknown) =>
  request<T>(path, { method: "PATCH", body: JSON.stringify(body) });
export const del = <T>(path: string) => request<T>(path, { method: "DELETE" });

// —— 类型 ——
/** 策略参数元数据（后端 registry 提供，含分组）。 */
/** 回测列表项（/strategy/backtests）。 */
export interface BacktestListItem {
  id: number;
  strategy_key: string;
  strategy_name: string;
  start_date: string;
  end_date: string;
  total_return: number | null;
  benchmark_total_return: number | null;
  excess_return: number | null;
  sharpe: number | null;
  max_drawdown: number | null;
  win_rate: number | null;
  trade_count: number | null;
  created_at: string | null;
}

/** 回测详情（/strategy/backtests/{id} 的实际返回，含双线曲线与归因字段）。
 *  注意与 BacktestResult 的区别：后端这个接口给的是 start/end（不是 start_date），
 *  equity_curve 带 benchmark/hedged。 */
export interface BacktestFull {
  id: number;
  symbol: string;
  start: string;
  end: string;
  strategy_key: string;
  total_return: number | null;
  benchmark_total_return: number | null;
  excess_return: number | null;
  sharpe: number | null;
  max_drawdown: number | null;
  trade_count: number | null;
  final_equity: number | null;
  equity_curve: { date: string; equity: number; benchmark?: number | null; hedged?: number | null }[];
  industry_distribution: Record<string, number> | null;
  holdings: unknown[];
  round_trips: unknown;
  hedged_total_return: number | null;
  hedged_sharpe: number | null;
  hedged_max_drawdown: number | null;
  params: Record<string, unknown> | null;
}

/** 一次寻优（/strategy/optimize/runs，已落库）。 */
export interface OptimizeRunRow {
  id: number;
  job_id: string;
  strategy_key: string;
  status: string;
  total: number;
  done: number;
  best_sharpe: number | null;
  best_excess: number | null;
  best_params: Record<string, unknown>;
  error: string;
  started_at: string | null;
  finished_at: string | null;
}

/** 表级新鲜度（/monitor/table-freshness）。 */
export interface TableFreshnessRow {
  table: string;
  date_col: string | null;
  rows: number;
  latest: string | null;
  days_ago: number | null;
  status: "OK" | "WARN" | "FAIL" | "EMPTY" | "n/a";
  note?: string;
}

export interface TableFreshness {
  as_of: string;
  grace_days: number;
  total: number;
  counts: Record<string, number>;
  worst: string;
  tables: TableFreshnessRow[];
}

export interface AlertLogItem {
  id: number; level: string; channel: string; status: string;
  title: string; error: string | null; at: string | null;
}

export interface ParamField {
  key: string;
  label: string;
  type: string;              // int / float / str
  default?: unknown;
  min?: number | null;
  max?: number | null;
  step?: number | null;
  options?: string[] | null; // type=str 时的枚举值
  desc?: string | null;      // 参数口径说明
  group?: string | null;     // 折叠分组名
}

export interface StrategyInfo {
  key: string;
  name: string;
  desc?: string;
  description?: string;
  default_params: Record<string, unknown>;
  param_schema: ParamField[];
  multi_asset?: boolean;
  /** paper/strategies 返回 single|portfolio */
  kind?: "single" | "portfolio" | string;
  index_name?: string | null;
  /** 该策略的基准指数行情代码（如 sh000300）。回测页切策略时用来自动填标的。 */
  index_symbol?: string | null;
}

export interface KlineBar {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  amount: number;
}

/** /market/quote/{symbol} 实时快照（个股与指数通用）。 */
export interface SpotQuote {
  symbol: string;
  name: string;
  price: number;
  change_pct: number;
  updated_at: string;
}

/** /market/stocks 返回的标的信息 */
export interface StockItem {
  symbol: string;
  name: string;
  market?: string;
  industry?: string | null;
  list_date?: string | null;
}

export interface CostProfile {
  commission?: number;
  slippage?: number;
  stamp_tax?: number;
  min_commission?: number;
  risk_free_rate?: number;
  source?: string;
  label?: string;
}

export interface ValuationPit {
  mode?: string;
  coverage?: number;
  note?: string | null;
}

export interface PeadPit {
  mode?: string;
  scale?: string;
  aligned_with?: string;
  note?: string | null;
}

export interface EvalOos {
  enabled?: boolean;
  verdict?: string;
  oos_ratio?: number;
  [k: string]: unknown;
}

export interface BacktestResult {
  id: number;
  symbol: string;
  strategy_key?: string;
  start_date?: string;
  end_date?: string;
  total_return: number;
  annual_return: number;
  sharpe: number;
  max_drawdown: number;
  trade_count?: number;
  benchmark_total_return?: number;
  /** 基准的行情代码，单标的回测里就是标的自买入持有。 */
  benchmark_symbol?: string;
  excess_return?: number;
  equity_curve?: { date: string; equity: number }[];
  trades?: {
    trade_date: string;
    symbol: string;
    side: string;
    price: number;
    shares: number;
    signal_type?: string;
  }[];
  factor_analysis?: { factors: Record<string, { ic_mean?: number }> };
  risk_limits?: Record<string, number> | null;
  risk_clamps?: unknown[];
  cost_profile?: CostProfile | null;
  valuation_pit?: ValuationPit | null;
  pead_pit?: PeadPit | null;
  oos?: EvalOos | null;
}

// —— 业务 API ——

/** W46-1 成本敏感网格 */

/** W46-2 walk-forward OOS */
export interface WalkForwardRequest {
  symbol: string;
  start: string;
  end: string;
  strategy: string;
  params?: Record<string, unknown>;
  initial_cash?: number;
  commission?: number | null;
  slippage?: number | null;
  adj?: string;
  mode?: "walk_forward" | "single";
  oos_ratio?: number;
  n_windows?: number;
  scheme?: "expanding";
  metric?: "sharpe" | "excess_return";
  min_is_bars?: number;
  min_oos_bars?: number;
}

export interface WalkForwardSegmentMetrics {
  n_bars: number;
  sharpe: number;
  total_return: number;
  excess_return: number;
  max_drawdown: number;
  trade_count: number;
}

export interface WalkForwardWindow {
  index: number;
  is_start: string;
  is_end: string;
  oos_start: string;
  oos_end: string;
  is: WalkForwardSegmentMetrics;
  oos: WalkForwardSegmentMetrics;
  verdict: string;
  note?: string;
}

export interface WalkForwardLegacy {
  split_date: string;
  oos_start: string;
  is: WalkForwardSegmentMetrics;
  oos: WalkForwardSegmentMetrics;
  verdict: string;
  note?: string;
}

export interface WalkForwardResult {
  mode: string;
  scheme: string;
  metric: string;
  oos_ratio: number;
  n_windows: number;
  cost_profile: CostProfile;
  oos: EvalOos;
  legacy_single?: WalkForwardLegacy | null;
  windows: WalkForwardWindow[];
  summary: {
    final_verdict: string;
    agg_verdict: string;
    legacy_verdict: string;
    gate_ok: boolean;
    fragile_regime: boolean;
    note?: string;
  };
  strategy_key: string;
  symbol: string;
  start: string;
  end: string;
}

export interface CostSensitivityRequest {
  symbol: string;
  start: string;
  end: string;
  strategy: string;
  params?: Record<string, unknown>;
  initial_cash?: number;
  adj?: string;
  mode?: "grid" | "multipliers";
  commissions?: number[];
  slippages?: number[];
  multipliers?: number[];
  base_commission?: number;
  base_slippage?: number;
  metric?: "excess_return" | "sharpe" | "total_return";
}

export interface CostGridCell {
  commission: number;
  slippage: number;
  multiplier?: number | null;
  is_default?: boolean;
  total_return: number;
  annual_return: number;
  sharpe: number;
  max_drawdown: number;
  excess_return: number;
  trade_count?: number;
  final_equity?: number;
}

export interface CostSensitivityResult {
  mode: string;
  metric: string;
  base_cost_profile: CostProfile;
  axes: {
    commissions?: number[] | null;
    slippages?: number[] | null;
    multipliers?: number[] | null;
  };
  cells: CostGridCell[];
  summary: {
    default_cell_ok: boolean;
    double_cost_excess_positive: boolean;
    fragile: boolean;
    note?: string;
  };
  strategy_key: string;
  symbol: string;
  start: string;
  end: string;
}

export const api = {
  strategies: () => get<StrategyInfo[]>("/strategy/strategies"),
  // limit 显式传入：后端默认只返回 500 根，长区间会被静默截断成「最近 500 根」。
  kline: (symbol: string, start: string, end: string, limit = 500) =>
    get<KlineBar[]>(`/market/kline/${symbol}?start=${start}&end=${end}&limit=${limit}`),
  quote: (symbol: string) => get<SpotQuote>(`/market/quote/${symbol}`),
  stocks: (keyword: string, limit = 10) =>
    get<StockItem[]>(`/market/stocks?keyword=${encodeURIComponent(keyword)}&limit=${limit}`),
  backtestSync: (payload: Record<string, unknown>) =>
    post<BacktestResult>("/strategy/backtest", payload),
  backtestAsync: (payload: Record<string, unknown>) =>
    post<{ task_id: string; status: string }>("/strategy/backtest/async", payload),
  taskStatus: (taskId: string) =>
    get<{ id: string; status: string; progress: number; message: string; result_id?: number; error?: string }>(
      `/strategy/backtest/tasks/${taskId}`
    ),
  backtestDetail: (id: number) => get<BacktestResult>(`/strategy/backtests/${id}`),
  backtestHistory: (limit = 30) => get<BacktestResult[]>(`/strategy/backtests?limit=${limit}`),
  /** W46-1 成本敏感网格（同步；长区间请自行缩小或用 multipliers） */
  costSensitivity: (payload: CostSensitivityRequest) =>
    post<CostSensitivityResult>("/strategy/backtest/cost-sensitivity", payload),
  /** W46-2 walk-forward OOS（同步；未做 async） */
  walkForward: (payload: WalkForwardRequest) =>
    post<WalkForwardResult>("/strategy/backtest/walk-forward", payload),
  // 参数寻优
  strategyBacktests: (limit = 20) =>
    get<BacktestListItem[]>(`/strategy/backtests?limit=${limit}`),
  paperSnapshots: (taskId: number, limit = 500) =>
    get<{ task_id: number; total: number; points: { date: string; equity: number; pnl_pct: number }[] }>(
      `/paper/tasks/${taskId}/snapshots?limit=${limit}`),
  backtestFull: (btId: number) =>
    get<BacktestFull>(`/strategy/backtests/${btId}`),
  optimizeRuns: (strategyKey?: string, limit = 10) =>
    get<{ total: number; runs: OptimizeRunRow[] }>(
      `/strategy/optimize/runs?limit=${limit}` +
      (strategyKey ? `&strategy_key=${encodeURIComponent(strategyKey)}` : "")),
  optimize: (payload: Record<string, unknown>) =>
    post<OptimizeTrial[]>("/strategy/optimize", payload),
  // 大网格：提交后台任务（子进程执行），轮询进度
  optimizeAsync: (payload: Record<string, unknown>) =>
    post<{ job_id: string; total: number; pid: number }>("/strategy/optimize/async", payload),
  optimizeAsyncStatus: (jobId: string) =>
    get<OptimizeJobStatus>(`/strategy/optimize/async/${jobId}`),
  optimizeAsyncCancel: (jobId: string) =>
    del<{ ok: boolean }>(`/strategy/optimize/async/${jobId}`),
  // 模拟盘
  paperTasks: () => get<PaperTask[]>("/paper/tasks"),
  paperCreate: (body: Record<string, unknown>) => post<PaperTask & { ok?: boolean; commission?: number; slippage?: number; cost_profile?: CostProfile; valuation_pit?: ValuationPit; pead_pit?: PeadPit }>("/paper/tasks", body),
  paperRun: (id: number) => post<{ ok: boolean }>(`/paper/tasks/${id}/run`, {}),
  paperToggle: (id: number) => post<PaperTask>(`/paper/tasks/${id}/toggle`, {}),
  paperDelete: async (id: number) => {
    const r = await fetch(`${BASE}/paper/tasks/${id}`, { method: "DELETE" });
    if (!r.ok) {
      const e = await r.json().catch(() => ({ detail: r.statusText }));
      throw new Error(e.detail || r.statusText);
    }
    return r.json();
  },
  paperDetail: (id: number) => get<PaperDetail>(`/paper/tasks/${id}`),
  // 监控
  monitor: () => get<MonitorStatus>("/monitor/status"),
  // 因子挖掘
  factorValidate: (expr: string) => post<{ ok: boolean; error?: string; sample_value?: number | null }>("/factor/validate", { expr }),
  factorFunctions: () => get<Record<string, Record<string, string>>>("/factor/functions"),
  factorMine: (payload: Record<string, unknown>) => post<FactorMineReport>("/factor/mine", payload),
  factorMtState: () => get<{ scope: string; n_tested_cum: number; updated_at?: string | null }>("/factor/mt/state"),
  factorMineHorizons: (payload: Record<string, unknown>) =>
    post<FactorHorizonsResult>("/factor/mine/horizons", payload),
  factorMineResults: (limit = 20) => get<FactorMineSummary[]>("/factor/mine/results?limit=" + limit),
  factorMineDetail: (id: number) => get<FactorMineReport>(`/factor/mine/results/${id}`),
  factorMineDelete: (id: number) => post<{ ok: boolean }>(`/factor/mine/results/${id}/delete`, {}),
  // GP 自动挖掘
  factorGpDirections: () => get<{ key: string; note: string }[]>("/factor/gp/directions"),
  factorGpMine: (payload: Record<string, unknown>) => post<GpMineResult>("/factor/gp/mine", payload),
  /** 情绪时序（非结构化管道）。source: news=东财新闻 / mp=公众号语料 */
  factorNewsDaily: (limit = 500, source: "news" | "mp" = "news") =>
    get<{ date: string; n_articles: number; n_finance: number; bull: number; bear: number; net_sentiment: number | null }[]>(`/factor/news/daily?limit=${limit}&source=${source}`),
  /** 极端情绪日 → 未来 N 日指数收益检验。source 支持 news / mp / both 合并 */
  factorNewsTest: (extreme_pct: number, horizon: number, source: "news" | "mp" | "both" = "both") =>
    post<NewsEventReport>("/factor/news/event-test", { extreme_pct, horizon, source }),
  /** 每小时热点简报（一手快讯 → LLM 筛选解读） */
  // 当日全景综述（每天 21:45 生成，v4-pro）。before 传今天时取「该日及之前
  // 最近一份」—— 早上看首页时今天还没生成，要显示昨天那份而不是空白。
  briefDigestLatest: (before?: string) =>
    get<BriefDigest>("/factor/brief-digest/latest" + (before ? `?before=${before}` : "")),
  briefDigestList: (limit = 30) => get<BriefDigest[]>("/factor/brief-digest/list?limit=" + limit),
  briefDigestRun: (day?: string) =>
    post<{ ok: boolean; day?: string; chars?: number; seconds?: number; error?: string }>(
      "/factor/brief-digest/run" + (day ? `?day=${day}` : ""),
      {},
    ),
  hotBriefLatest: () => get<HotBrief>("/factor/hot-brief/latest"),
  /** 今日累计：各轮条目合并去重 + 最新综述（首页用这个） */
  hotBriefToday: () => get<HotBrief>("/factor/hot-brief/today"),
  hotBriefList: (limit = 24) => get<HotBrief[]>("/factor/hot-brief/list?limit=" + limit),
  /** 单条输入的原文内容；full=true 时实时抓整页正文（联网、按次计费） */
  /** 舆情策略：择时 + 行业 + 个股（三层合一的可执行结论） */
  sentimentStrategy: (topIndustry = 8, topStock = 15) =>
    get<SentimentStrategy>(
      `/factor/sentiment/strategy?top_industry=${topIndustry}&top_stock=${topStock}`,
    ),
  hotBriefInput: (ref: string, opts?: { runId?: number; full?: boolean }) => {
    const q = new URLSearchParams({ ref });
    if (opts?.runId) q.set("run_id", String(opts.runId));
    if (opts?.full) q.set("full", "1");
    return get<HotBriefInputDetail>(`/factor/hot-brief/input?${q.toString()}`);
  },
  /** 社交关注度（雪球 A 股热股 / 微博热搜）；只有热度、无正文，不做多空打分 */
  factorSocialHeat: (days = 30) =>
    get<{ date: string; source: string; n_items: number; n_finance: number; heat_total: number; heat_top10: number }[]>(`/factor/social/heat?days=${days}`),
  // AI 因子生成（自然语言 ↔ 表达式）
  factorAiStatus: () => get<AiStatus>("/factor/ai/status"),
  factorAiGenerate: (text: string, max_retry = 3) =>
    post<AiGenerateResult>("/factor/ai/generate", { text, max_retry }),
  factorAiExplain: (expr: string) => post<AiExplainResult>("/factor/ai/explain", { expr }),

  // —— 大模型通道配置：页面维护，写入即生效（无需重启后端）——
  llmProviders: () => get<LlmProviderList>("/llm/providers"),
  llmPresets: () => get<{ presets: LlmVendorPreset[] }>("/llm/presets"),
  llmCreate: (p: LlmProviderInput) => post<LlmProvider>("/llm/providers", p),
  llmUpdate: (id: number, p: Partial<LlmProviderInput>) =>
    patch<LlmProvider>(`/llm/providers/${id}`, p),
  llmDelete: (id: number) => del<{ ok: boolean }>(`/llm/providers/${id}`),
  llmSetDefault: (id: number) => post<{ ok: boolean }>(`/llm/providers/${id}/default`, {}),
  /** 用库中已保存的配置测试（编辑时未改 key 的场景）。 */
  llmTestSaved: (id: number) => post<LlmTestResult>(`/llm/providers/${id}/test`, {}),
  /** 用表单当前值测试（保存前先验证）；api_key 留空且有 id 时后端复用已存 key。 */
  llmTestDraft: (p: { id?: number; base_url: string; model: string; api_key?: string }) =>
    post<LlmTestResult>("/llm/test", p),
  // 数据健康度 + 数据流全景
  tableFreshness: (force = false) =>
    get<TableFreshness>(`/monitor/table-freshness?fresh=${force}`),
  alertLog: (limit = 50, level?: string) =>
    get<{ total: number; counts: Record<string, number>; items: AlertLogItem[] }>(
      `/monitor/alert-log?limit=${limit}${level ? `&level=${level}` : ""}`),
  monitorHealth: () => get<HealthReport>(`/monitor/health-report`),
  frameworkHealth: () => get<FrameworkHealth>(`/monitor/framework-health`),
  holidayMode: () => get<HolidayMode>(`/monitor/holiday-mode`),
  setHolidayMode: (mode: "auto" | "on" | "off") =>
    put<HolidayMode>(`/monitor/holiday-mode`, { mode }),
  monitorDataflow: () => get<DataflowReport>(`/monitor/dataflow`),
  monitorLineage: () => get<LineageReport>(`/monitor/lineage`),
  monitorAssets: (force = false) => get<AssetsReport>(`/monitor/assets?force=${force}`),
  // 基金数据管道：看状态 / 应急触发 / 查最近一次输出
  fundPipeline: () => get<FundPipelineStatus>("/fund/pipeline/status"),
  fundPipelineRun: (stage: string, force = true) =>
    post<{ ok: boolean; stage: string; started?: boolean; skipped?: boolean; error?: string }>(
      "/fund/pipeline/run", { stage, force }),
  fundPipelineDetail: (stage: string) =>
    get<{ ok: boolean; stage: string; status?: string; detail?: string; error?: string;
          started_at?: string; duration_sec?: number; rows?: number }>(
      `/fund/pipeline/detail?stage=${encodeURIComponent(stage)}`),
  // 应急：今天没成功的环节按依赖顺序补跑
  fundPipelineRunAll: () =>
    post<{ ok: boolean; started?: string[]; skipped?: string[]; error?: string }>(
      "/fund/pipeline/run-all", {}),
  // 应急：清掉卡住的 running 记录，使其能立刻重跑（不必等 3 小时自动接管）
  fundPipelineReset: (stage: string) =>
    post<{ ok: boolean; stage: string; reset?: boolean; message?: string; error?: string }>(
      "/fund/pipeline/reset", { stage }),
  // 应急：暂停/恢复自动巡检（手工补数时避免调度器抢库锁）
  fundPipelinePause: (paused: boolean) =>
    post<{ ok: boolean; paused?: boolean; error?: string }>(
      "/fund/pipeline/pause", { paused }),
  healthRules: () => get<HealthRuleRow[]>(`/monitor/health/rules`),
  healthRuleToggle: (id: number, enabled: boolean) =>
    put(`/monitor/health/rules/${id}`, { enabled }),
  healthRuleSave: (id: number, payload: Record<string, unknown>) =>
    put(`/monitor/health/rules/${id}`, payload),
  healthRuleAdd: (payload: Record<string, unknown>) => post<{ ok: boolean; id: number }>(`/monitor/health/rules`, payload),
  healthRuleDelete: (id: number) => del(`/monitor/health/rules/${id}`),
  // 因子注册表
  factorRegistryRegister: (payload: Record<string, unknown>) =>
    post<{ ok: boolean; id: number; status: string; existed?: boolean }>("/factor/registry/register", payload),
  factorRegistryList: (status = "") =>
    get<RegistryRow[]>(`/factor/registry${status ? `?status=${encodeURIComponent(status)}` : ""}`),
  factorRegistryToggle: (id: number) =>
    post<{ ok: boolean; id: number; status: string }>(`/factor/registry/${id}/toggle`, {}),
};

/** 当日全景综述（brief_digest 表）。与 HotBrief 是不同层次：
 *  HotBrief 是每轮增量，这个是全天全景。 */
export interface BriefDigest {
  day: string;
  mainline?: string;
  summary?: string;
  chars?: number;
  model?: string;
  n_items: number;
  n_news: number;
  created_at?: string;
  stats?: {
    n_items?: number;
    n_news?: number;
    n_rounds?: number;
    src_stat?: string;
    hot_stocks?: string;
    sector_stat?: string;
    seconds?: number;
  };
}

export interface RegistryRow {
  id: number; name: string; expr: string; direction: number;
  category: string; status: string; ic_mean: number | null;
  source_id?: number | null; created_at: string;
}

// —— AI 因子生成 ——
export interface AiStatus {
  configured: boolean;
  model: string;
  base_url: string;
  /** 未配置大模型时，本地关键词规则兜底是否可用（试用模式）。 */
  fallback_ready?: boolean;
  /** 需要配置的环境变量名，前端在配置引导里展示。 */
  env_key?: string;
  /** 生效来源：db=页面配置的通道，env=环境变量（向后兼容通道）。 */
  source?: "" | "db" | "env";
  /** 生效通道名（页面配置时为其名称）。 */
  provider_name?: string;
  provider_id?: number | null;
  key_masked?: string;
}

// —— 大模型通道配置（/llm/*）——
export interface LlmProvider {
  id: number;
  name: string;
  vendor: string;
  base_url: string;
  model: string;
  /** 脱敏后的 key，如 sk-abc******wxyz；明文永远不会返回。 */
  api_key_masked: string;
  has_key: boolean;
  is_default: boolean;
  last_test_at: string | null;
  last_test_ok: boolean | null;
  last_test_ms: number | null;
  last_test_msg: string | null;
  updated_at: string | null;
}

export interface LlmActive {
  configured: boolean;
  source: "" | "db" | "env";
  name: string;
  model: string;
  base_url: string;
  provider_id: number | null;
  key_masked: string;
  count: number;
  /** 页面已配置但 .env 里也有 key 时给出提示，避免「改了 .env 没反应」的困惑。 */
  env_shadowed: boolean;
}

export interface LlmProviderList {
  active: LlmActive;
  items: LlmProvider[];
}

export interface LlmVendorPreset {
  vendor: string;
  label: string;
  base_url: string;
  models: string[];
  note: string;
}

export interface LlmTestResult {
  ok: boolean;
  ms: number;
  model: string;
  reply?: string;
  error?: string;
}

/** 新建/编辑通道的提交体（编辑时 api_key 留空 = 不修改）。 */
export interface LlmProviderInput {
  name: string;
  vendor: string;
  base_url: string;
  model: string;
  api_key?: string;
  make_default?: boolean;
}

/** 一次校验失败的记录（模型第几轮写了什么、报什么错），用于展示自修复过程。 */
export interface AiFix {
  attempt: number;
  expr: string;
  error: string;
}

export interface AiGenerateResult {
  ok: boolean;
  expr?: string;
  name?: string;
  logic?: string;
  direction?: string;
  unsupported?: string;
  confidence?: number | null;
  sample_value?: number | null;
  attempts?: number;
  fixes?: AiFix[];
  model?: string;
  error?: string;
  hint?: string;
  /** 结果来源：llm=大模型，local=本地关键词规则（试用模式）。 */
  source?: "llm" | "local";
  /** 本地模式下命中的规则名（让用户一眼看出是否理解对了）。 */
  matched?: string[];
}

export interface AiExplainResult {
  ok: boolean;
  expr?: string;
  name?: string;
  logic?: string;
  direction?: string;
  caveats?: string;
  error?: string;
}

export interface HealthCheck {
  name: string;
  status: "ok" | "warn" | "error";
  value: string;
  expect: string;
  muted?: boolean;
}
export interface HealthLayer {
  label: string;
  checks: HealthCheck[];
  score: number;
  status: "healthy" | "warn" | "error";
}
export interface HealthAlert {
  level: string; layer: string; check: string; detail: string; expect: string;
}
export interface HolidayMode {
  mode: "auto" | "on" | "off";
  active: boolean;
  today: string;
  trading_day: boolean;
  next_trading_day: string | null;
}
/** 框架四层体检（调度/编排/数据/研究）—— /monitor/framework-health */
export interface FrameworkHealthItem {
  label: string;
  status: "OK" | "WARN" | "FAIL";
  detail: string;
}
export interface FrameworkHealthLayer {
  key: string;
  name: string;
  status: "OK" | "WARN" | "FAIL";
  items: FrameworkHealthItem[];
}
export interface FrameworkHealth {
  checked_at: string;
  worst: "OK" | "WARN" | "FAIL";
  layers: FrameworkHealthLayer[];
}

export interface HealthReport {
  overall_score: number;
  overall_status: "healthy" | "warn" | "error";
  layers: Record<string, HealthLayer>;
  alerts: HealthAlert[];
  holiday?: Pick<HolidayMode, "mode" | "active" | "trading_day" | "next_trading_day">;
  generated_at: string;
}

// —— GP 自动挖掘类型 ——
/** 每小时热点简报条目 */
export interface HotBriefSource {
  ref: string;          // 输入编号，如 "S1"
  kind: string;         // 快讯 / 境外 / 搜索
  label: string;        // 财联社电报 / Fed 新闻稿 / DeepSeek · 央广财经
  url: string;
}

export interface HotBriefItem {
  theme: string;
  direction: "bullish" | "bearish" | "neutral" | string;
  sectors: string[];
  tickers: string[];
  horizon: string;
  why: string;
  strength: number;
  /** 来源：模型回填输入编号，服务端翻译成人可读标签（见 hot_brief._resolve_src） */
  source_kind?: string;    // 快讯 / 境外 / 搜索 / 混合
  source_label?: string;   // 财联社电报 / Fed 新闻稿 / DeepSeek · …
  source_url?: string;
  source_refs?: string[];
  /** 完整来源清单（source_label 超过 2 个时会压缩，这里是全量） */
  source_all?: string[];
  sources?: HotBriefSource[];
  /** 今日累计视图里：该条首次被报出的时间（HH:MM） */
  first_seen?: string;
}

/** 本轮喂给模型的原始输入（用于回溯「这条是从哪看到的」） */
export interface HotBriefInput {
  ref: string;      // F1 / O2 / S3
  kind: string;     // 快讯 / 境外 / 搜索
  ts: number;       // unix 秒；搜索可能为 0
  title: string;
  site: string;
  url: string;
  /** 正文快照：快讯=财联社电报正文，搜索=DeepSeek 返回的 600~800 字摘要。
   *  3 天前的快照会被后端剥掉，只剩定位信息。 */
  body?: string;
}

/** 单条输入的详情（`full=1` 时多一个 content：实时抓到的整页正文） */
export interface HotBriefInputDetail extends HotBriefInput {
  run_id?: number;
  content?: string;
  content_title?: string;
  error?: string;
}

export interface HotBrief {
  id?: number;
  created_at?: string;
  window_start?: string;
  window_end?: string;
  n_input?: number;
  n_flash?: number;
  n_search?: number;
  n_foreign?: number;
  n_items?: number;
  items?: HotBriefItem[];
  /** 本轮原始输入快照（标题/站点/链接），可回溯原文 */
  inputs?: HotBriefInput[];
  mainline?: string;
  /** 今日综述（350~1000 字，分政策/产业/海外/资金四段） */
  summary?: string;
  /** 今日累计视图专有 */
  n_briefs?: number;
  /** 最近一轮（本小时）真正新报的条目 */
  latest_items?: HotBriefItem[];
  latest_at?: string;
  n_latest?: number;
  date?: string;
  /** 当天是否已有「当日全景」（brief_digest）。有则本卡收起主线与综述，
   *  只留明细清单 —— 两个「今日整体」叠着讲同一件事是重复的。 */
  digest_day?: string;
  source_split?: Record<string, number>;
  model?: string;
  error?: string;
}

/** 舆情策略：情绪择时 + 重点行业 + 重点个股（见 services/sentiment_strategy.py） */
export interface SentimentTiming {
  ok: boolean;
  date?: string;
  value?: number;     // 当前市场情绪 -1~+1
  pct?: number;       // 在近 N 日的分位
  ma5?: number;
  ma20?: number;
  n_days?: number;
  stance?: string;    // 悲观极致 / 偏冷 / 中性 / 偏热 / 亢奋
  position?: number;  // 建议仓位 %
  comment?: string;
}

export interface SentimentIndustry {
  industry: string;
  n_stock: number;
  mentions: number;
  net: number;
  leaders: { symbol: string; name: string; net: number; mentions: number }[];
}

export interface SentimentStock {
  symbol: string;
  name: string;
  industry: string;
  mentions: number;
  net: number;
  heat: number;
  trend: number | null;   // 相对上一有数据日的情绪变化
}

export interface SentimentStrategy {
  ok: boolean;
  asof?: string;
  generated_at?: string;
  note?: string;
  error?: string;
  timing?: SentimentTiming;
  industries?: SentimentIndustry[];
  stocks?: SentimentStock[];
}

/** 新闻情绪择时检验（市场级 · 非结构化数据）。2026-09-18 下线，2026-11-07 恢复。 */
export interface NewsEventReport {
  ok: boolean;
  error?: string;
  extreme_pct: number;
  horizon: number;
  hi_threshold?: number;
  lo_threshold?: number;
  baseline_ret: number;
  n_days_all: number;
  bull: { n_days: number; avg_ret: number; win_rate: number };
  bear: { n_days: number; avg_ret: number; win_rate: number };
  edge_long_vs_base?: number | null;
  edge_short_vs_base?: number | null;
  /** 按实测动态给的结论（含样本覆盖警告），不要在前端另写死判断 */
  verdict?: string;
  note?: string;
  n_effective_days?: number;
  coverage_warning?: string | null;
}

export interface GpMineResult {
  ok: boolean;
  directions: string[];
  pop_size: number;
  generations: number;
  n_candidates_evaluated: number;
  evolution_log: { gen: number; best_ic: number; avg_fitness: number }[];
  saved_ids: number[];
  elites: (FactorMineReport & { quick_ic?: number; fitness?: number })[];
}

// —— 因子挖掘类型 ——
/** 因子值本身的分布统计：IC 是秩相关、对数值缩放免疫，这里是唯一能反映「参数改了没有」的反馈 */
export interface FactorValueStats {
  n_values: number;
  n_per_period: number;
  mean: number | null;
  std: number | null;
  min: number | null;
  p25: number | null;
  p50: number | null;
  p75: number | null;
  max: number | null;
  skew: number | null;
  n_unique: number;
}

export interface FactorIcPack {
  ic_mean: number | null;
  icir: number | null;
  t_stat: number | null;
  ic_win_rate: number | null;
  n_periods: number;
  ic_series?: { date: string; ic: number }[];
}

export interface FactorOrthoBasis {
  names: string[];
  exprs: Record<string, string>;
  min_cross_section: number;
  n_snaps_resid: number;
  n_snaps_fallback_raw: number;
  note?: string;
}

export interface FactorMineReport {
  ok: boolean;
  error?: string;
  id?: number;
  name: string;
  expr: string;
  rating: string;
  ic_mean: number;
  icir: number;
  t_stat: number;
  ic_win_rate: number;
  ic_series: { date: string; ic: number }[];
  groups: number;
  group_means: Record<string, number>;
  monotonic_spread: number;
  mono_score: number;
  long_short: [string, number][];
  corr_with_existing: Record<string, number>;
  max_abs_corr: number;
  n_periods: number;
  n_stocks: number;
  forward_days: number;
  factor_stats?: FactorValueStats;
  /** 命中重复表达式时，指向已存在的那条历史记录 id（本次未重算、未落库） */
  duplicate_of?: number;
  duplicate_created_at?: string | null;
  cost_profile?: CostProfile | null;
  valuation_pit?: ValuationPit | null;
  pead_pit?: PeadPit | null;
  oos?: EvalOos | null;
  /** W47-2：是否对代表集残差后再评 IC */
  orthogonalize?: boolean;
  ic_raw?: FactorIcPack | null;
  ic_resid?: FactorIcPack | null;
  ortho_basis?: FactorOrthoBasis | null;
  /** W47-3：多重检验累计（报告层） */
  n_tested?: number;
  n_tested_cum?: number;
  luck_bar?: number;
  t_thresholds?: { 优秀?: number; 可用?: number; [k: string]: number | undefined };
  bonferroni_alpha?: number;
  mt_note?: string;
  mt_stale?: boolean;
  mt_replay_note?: string;
  multiple_testing?: {
    scope?: string;
    n_tested?: number;
    n_tested_cum?: number;
    luck_bar?: number;
    t_thresholds?: { 优秀?: number; 可用?: number; [k: string]: number | undefined };
    bonferroni_alpha?: number;
    formula?: string;
    stale?: boolean;
    replay_note?: string;
  } | null;
}

export interface FactorHorizonRow {
  horizon: number;
  ic_mean: number | null;
  icir: number | null;
  t_stat: number | null;
  rating?: string;
  oos?: { verdict?: string | null; enabled?: boolean | null };
  n_periods?: number;
  pead_pit?: PeadPit | null;
  valuation_pit?: ValuationPit | null;
  cost_profile?: CostProfile | null;
}

export interface FactorHorizonsResult {
  ok: boolean;
  expr: string;
  name: string;
  horizons: FactorHorizonRow[];
}

export interface FactorMineSummary {
  id: number;
  name: string;
  expr: string;
  rating: string;
  ic_mean: number | null;
  icir: number | null;
  created_at: string;
}

// —— 监控类型 ——
export interface MonitorStatus {
  server: { name: string; version: string; time: string; uptime_sec: number; db: string };
  data: {
    sqlite: Record<string, number>;
    sqlite_total: number;
    duckdb: Record<string, number>;
    duckdb_total: number;
    freshness: Record<string, { label: string; latest: string | null; days_ago: number | null; stale: boolean }>;
  };
  services: {
    data_source: { tushare: boolean; akshare: boolean };
    schedulers: {
      etl: { enabled: boolean; run_hour: string; last_run_at: string | null; last_success: string | null; last_error: string | null; runs_total: number };
      paper: { alive: boolean; interval_sec: number };
    };
    tasks: { running: number; recent: { id: string; name: string; status: string; progress: number; message: string }[] };
    paper: { tasks: number; enabled: number };
    pipeline?: {
      runs: {
        run_id: number; trigger: string; status: string;
        started_at: string | null; finished_at: string | null; error: string | null;
        steps: { name: string; status: string; duration_sec: number; rows: number }[];
      }[];
    };
  };
  disk: { data_dir_mb: number; data_dir: string };
}

// —— 参数寻优类型 ——
export interface OptimizeTrial {
  params: Record<string, number>;
  // 样本内（IS）
  total_return: number;
  annual_return: number;
  max_drawdown: number;
  sharpe: number;
  win_rate: number;
  trade_count: number;
  final_equity: number;
  // 样本外（OOS），未启用切分时为 null
  oos_start: string | null;
  oos_total_return: number | null;
  oos_annual_return: number | null;
  oos_max_drawdown: number | null;
  oos_sharpe: number | null;
  oos_win_rate: number | null;
  oos_trade_count: number | null;
  // 稳健性（与相邻参数组合比较）
  neighbor_count: number;
  neighbor_sharpe: number | null;
  robustness: number | null;
  cost_profile?: CostProfile | null;
  valuation_pit?: ValuationPit | null;
  pead_pit?: PeadPit | null;
  oos?: EvalOos | null;
}

/** 大网格异步寻优任务的状态（GET /strategy/optimize/async/{job_id}） */
export interface OptimizeJobStatus {
  job_id: string;
  running: boolean;
  done: number;
  total: number;
  ok: boolean | null;
  error: string | null;
  cancelled: boolean;
  elapsed: number;
  /** 仅在 running=false 时返回实际结果 */
  results: OptimizeTrial[];
}

// —— 模拟盘类型 ——
export interface PaperTask {
  id: number;
  name: string;
  strategy_key: string;
  kind: "single" | "portfolio";
  symbols: string;
  index_code: string;
  enabled: boolean;
  initial_cash: number;
  commission?: number;
  slippage?: number;
  cost_profile?: CostProfile | null;
  valuation_pit?: ValuationPit | null;
  pead_pit?: PeadPit | null;
  equity: number;
  pnl: number;
  pnl_pct: number;
  positions_count: number;
  last_run_at?: string;
  error_msg?: string | null;
}

export interface PaperTrade {
  trade_date: string;
  symbol: string;
  side: string;
  price: number;
  shares: number;
  signal_type?: string;
}

export interface PaperDetail {
  id: number;
  kind: "single" | "portfolio";
  equity: number;
  pnl_pct?: number;
  positions?: Record<string, unknown>;
  curve?: { date: string; equity: number }[];
  trades?: PaperTrade[];
  factor_analysis?: unknown;
  risk_limits?: Record<string, number> | null;
  risk_clamps?: unknown[];
  error_msg?: string | null;
  /** W47-1：任务落库费率 / 成本徽章 */
  commission?: number;
  slippage?: number;
  cost_profile?: CostProfile | null;
  /** 仅佣金累计（不含印花税分项） */
  fees_paid_commission?: number;
}

export interface DataflowReport {
  sources: { name: string; type: string; enabled: boolean; description: string; params_brief: string }[];
  bronze: Record<string, { files: number; size_mb: number; latest: string | null }>;
  silver: { files: Record<string, { size_kb?: number; mtime: string | null }>; quality: Record<string, unknown> | null };
  gold: Record<string, number | string | null>;
  scorers: { version: string; desc: string; active: boolean }[];
}

export interface LineageReport {
  sources: {
    name: string; type: string; description: string; enabled: boolean;
    params: Record<string, unknown>; step: string; layer: string; produces: string;
    last_run: { status: string; rows: number; duration_sec: number; message: string } | null;
  }[];
  steps: { order: number; name: string; status: string; rows: number; duration_sec: number }[];
  layers: {
    bronze: Record<string, { files: number; size_mb: number; latest: string | null }>;
    silver: { files: Record<string, { size_kb?: number; mtime: string | null }>; quality: Record<string, unknown> | null };
    gold: {
      tables: Record<string, number>;
      latest: { kline: string | null; factor: string | null; news: string | null };
      registry_enabled: number;
    };
  };
  timeline: {
    run_id: number; trigger: string; status: string; started_at: string | null;
    finished_at: string | null; total_sec: number; error: string | null;
    steps: { name: string; status: string; duration_sec: number; rows: number; message: string }[];
  }[];
  last_run: { run_id: number; status: string; started_at: string | null } | null;
  raw_dir: string;
  config_path: string;
}

/** 数据资产清单中的一个数据集 */
export interface AssetItem {
  key: string; group: string; label: string; rows: number; symbols: number | null;
  start: string | null; latest: string | null;
  lag_trading_days: number | null; lag_calendar_days: number | null;
  max_lag: number; note: string;
  status: "ok" | "warn" | "stale" | "empty";
}
export interface AssetGroup {
  key: string; label: string; desc: string; items: AssetItem[]; rows: number; bad: number;
}
export interface CoveragePoint { date: string; symbols: number; partial: boolean; pending?: boolean; }
export interface CoverageBlock {
  days: CoveragePoint[]; median: number; peak: number;
  partial_count: number; pending_date: string | null;
}

export interface AssetsReport {
  generated_at: string; today: string; coverage: CoverageBlock;
  summary: {
    latest_trade_date: string | null;
    lag_trading_days: number | null; lag_calendar_days: number | null;
    total_rows: number; symbols: number | null;
    n_stale: number; n_warn: number; n_empty: number; n_total: number;
    n_partial_days: number;
    verdict: string; verdict_level: "ok" | "warn" | "stale";
    worst: { label: string; latest: string | null; lag: number | null } | null;
    // 盘中/收盘前，当日数据尚未发布，既不判过期也不算残缺
    pending_today: boolean; expected_latest: string | null;
  };
  groups: AssetGroup[];
  by_source: Record<string, AssetItem>;
}

export interface HealthRuleRow {
  id: number; name: string; layer: string; metric: string; params: string;
  comparator: string; threshold: number | null; level: string; weight: number;
  enabled: boolean; last_value: string | null; last_status: string | null;
  metric_doc: string;
}

// —— 基金数据管道（独立库 fund_research.db，按披露日历调度）——
export interface FundPipelineStage {
  key: string; label: string; kind: "collect" | "derived" | string;
  desc: string; on: string; depends: string[];
  run_at: string; timeout: number;
  today_status: string | null;      // running / ok / fail / null（今天没跑）
  today_started: string | null;
  today_finished: string | null;
  today_duration: number | null;
  today_rows: number | null;
  today_error: string | null;
  today_detail: string | null;
  today_attempts: number | null;    // 今日尝试次数（>1 说明被自动重试过）
  today_heartbeat: string | null;   // 子进程心跳（判断僵尸用）
  last_ok_at: string | null;
  last_fail_at: string | null;
  last_error: string | null;
  runs_total: number; fails_total: number; running: boolean;
}
export interface FundPipelineStatus {
  ok: boolean; error?: string;
  enabled: boolean;
  paused?: boolean;
  in_disclosure_window: boolean;
  is_trading_day: boolean;
  started_at: string | null;
  last_tick: string | null;
  stages: FundPipelineStage[];
  data: Record<string, { rows: number; latest: string | null }>;
}

// —— Agent 编排 / 研究驾驶舱（P2）——
export interface AgentEventItem {
  id: number; run_id: number; agent: string; step: string;
  level: string; status: string | null; message: string;
  rows: number | null; duration_ms: number | null; created_at: string | null;
}
export interface ResearchRunItem {
  id: number; trigger: string; status: string; data_date: string | null;
  gate_status: string; gate_note: string | null; gate_deadline: string | null;
  started_at: string | null; finished_at: string | null; error: string | null;
}
export interface AgentState {
  orchestrator: { running: boolean; run_id: number | null; step: string | null;
    started_at: string | null; finished_at: string | null };
  scheduler: { enabled: boolean; run_at: string; last_run_date: string | null;
    last_error: string | null; next_run: string; gate_countdown_sec: number };
  gate_countdown_sec: number;
  latest_run: ResearchRunItem | null;
  recent_runs: ResearchRunItem[];
}

// Agent 方法独立导出（api 对象字面量无法被 Object.assign 扩展类型）
export const agentApi = {
  state: () => get<AgentState>("/agent/state"),
  run: (trigger: string = "manual") =>
    post<{ ok: boolean; run_id: number }>("/agent/run", { trigger }),
  gate: (rid: number, action: "approve" | "reject", note: string = "") =>
    post<{ ok: boolean; gate_status: string }>(`/agent/runs/${rid}/gate`, { action, note }),
};

// 信号组合 + 发布通道配置（P3/P4）
export interface PortfolioSnapshot {
  ok: boolean; error?: string; as_of: string;
  summary: { capital: number; total: number; return_pct: number;
    max_drawdown_pct: number; n_trades: number; n_holdings: number; days: number;
    gate_open_days?: number; gate_exc_now?: number | null; rule?: string;
    enabled_strategies?: string[] };
  config: Record<string, number>;
  equity: { date: string; total: number; cash: number; market_value: number;
    n_positions: number; gate_open?: boolean }[];
  holdings: { symbol: string; shares: number; entry_price: number; entry_date: string }[];
  trades: { date: string; symbol: string; side: string; shares: number;
    price: number; reason: string; pnl_pct?: number }[];
}
export interface NotifyConfig {
  enabled: boolean; smtp_host: string; smtp_port: number; user: string;
  from_addr: string; to: string[]; has_password: boolean; password_hint: string;
}

export const signalApi = {
  portfolio: () => get<PortfolioSnapshot>("/signal/portfolio"),
  portfolioExp: () => get<PortfolioSnapshot>("/signal/portfolio-exp"),
  notifyConfig: () => get<NotifyConfig>("/agent/notify-config"),
  updateNotifyConfig: (cfg: Record<string, unknown>) =>
    put<{ ok: boolean; config: NotifyConfig }>("/agent/notify-config", cfg),
  testNotify: () => post<{ ok: boolean; error?: string }>("/agent/notify-test", {}),
};

// —— 资讯中心（东财 / 新浪 / 腾讯 / 雪球） ——
export interface NewsItem {
  title: string; time: string; url: string; source: string;
}
export interface QuoteItem {
  code: string; name: string; price: number; change_pct: number; source: string;
}
export interface HotItem {
  symbol: string; name: string; heat: number; heat_increment: number;
  rank_change: number; price: number; change_pct: number; source: string;
}
export interface NewsFeed {
  generated_at: string; cached: boolean; age_sec: number; stale?: boolean;
  headlines: NewsItem[]; sina_roll: NewsItem[];
  global_quotes: QuoteItem[]; xueqiu_hot: HotItem[];
  sources_ok: { eastmoney: boolean; sina: boolean; tencent: boolean; xueqiu: boolean };
}
export interface StockNews {
  ok: boolean; error?: string; symbol?: string; items: NewsItem[]; cached?: boolean;
}

/** 统一资讯流条目（各来源原文归一后的形态） */
export interface NewsFeedItem {
  id: number;
  source: string;          // cls / eastmoney / sina / tushare / wechat / guba
  source_label: string;    // 财联社 / 东财 / …
  title: string;
  summary: string;
  url: string;
  published_at: string | null;
  /** 入库时刻（本地时间无时区），「新」标志按它算 */
  fetched_at?: string | null;
  /** 同一事件被几家报道（≥2 才给）。既是重复标志，也是热度信号。 */
  dup_count?: number;
  dup_sources?: string[];
  symbols: string;
}

export interface NewsSourceStat {
  key: string;
  label: string;
  count: number;
  latest: string | null;
}

/** 境外源状态条（OPS-FN-1a）；密钥只回传 configured 布尔 */
export interface ForeignSourceStatus {
  source: string;
  label: string;
  kind?: string;
  enabled: boolean;
  last_ok_at?: string | null;
  last_count?: number;
  last_error?: string;
  status: "ok" | "degraded" | "unconfigured" | "fail" | "disabled" | string;
  count?: number;
  latest?: string | null;
  configured?: boolean;  // 仅 fn_x
}

export interface ForeignSyncResult {
  added: number;
  status: string;
  error?: string;
}

export interface ForeignSyncStatus {
  enabled: boolean;
  last_run_at?: string | null;
  last_result?: string | null;
  last_error?: string | null;
  runs_total?: number;
  last_slot?: string | null;
  slots?: string[];
  timezone?: string;
  done_today?: string[];
  next_slot?: string;
  sources?: Record<string, { added?: number; status?: string; error?: string }>;
}
export interface ForeignDigestItem {
  title: string;
  source: string;
  source_label: string;
  published_at: string | null;
  url: string;
  summary?: string;
}

export interface ForeignDigest {
  hours: number;
  count: number;
  total: number;
  items: ForeignDigestItem[];
  text: string;
  llm: boolean;
}


export const newsApi = {
  feed: (refresh = false) => get<NewsFeed>(`/news/feed${refresh ? "?refresh=true" : ""}`),
  stock: (symbol: string) =>
    get<StockNews>(`/news/stock?symbol=${encodeURIComponent(symbol)}`),
  /** 统一资讯流（分页）。sources 传逗号串，空=全部 */
  feedItems: (p: { sources?: string; q?: string; hours?: number; limit?: number; offset?: number }) => {
    const qs = new URLSearchParams();
    if (p.sources) qs.set("sources", p.sources);
    if (p.q) qs.set("q", p.q);
    if (p.hours !== undefined) qs.set("hours", String(p.hours));
    qs.set("limit", String(p.limit ?? 60));
    qs.set("offset", String(p.offset ?? 0));
    return get<{ items: NewsFeedItem[]; total: number; has_more: boolean }>(`/news/items?${qs}`);
  },
  /** 各来源条数与最新时间 */
  feedSources: (hours = 0) => get<NewsSourceStat[]>(`/news/sources?hours=${hours}`),
  /** 手动触发一次同步 */
  feedSync: () => post<Record<string, number>>("/news/sync", {}),

  /** 境外资讯列表 */
  foreignItems: (p: {
    sources?: string;
    q?: string;
    hours?: number;
    limit?: number;
    offset?: number;
  }) => {
    const qs = new URLSearchParams();
    if (p.sources) qs.set("sources", p.sources);
    if (p.q) qs.set("q", p.q);
    if (p.hours !== undefined) qs.set("hours", String(p.hours));
    qs.set("limit", String(p.limit ?? 60));
    qs.set("offset", String(p.offset ?? 0));
    return get<{ items: NewsFeedItem[]; total: number; has_more: boolean }>(
      `/news/foreign/items?${qs}`,
    );
  },
  /** 境外源状态条 */
  foreignSources: (hours = 72) =>
    get<ForeignSourceStatus[]>(`/news/foreign/sources?hours=${hours}`),
  /** 手动全源同步（整批 ≤90s；前端超时 100s） */
  foreignSync: async () => {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 100_000);
    try {
      const r = await fetch(`${BASE}/news/foreign/sync`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
        signal: ctrl.signal,
      });
      if (!r.ok) {
        const e = await r.json().catch(() => ({ detail: r.statusText }));
        throw new Error(e.detail || r.statusText);
      }
      return (await r.json()) as Record<string, ForeignSyncResult>;
    } finally {
      clearTimeout(timer);
    }
  },
  /** 境外调度器状态 */
  foreignSyncStatus: () => get<ForeignSyncStatus>("/news/foreign/sync-status"),
  /** 境外日摘要（标题列表 + 短拼接；不调 LLM） */
  foreignDigest: (hours = 24, limit = 40) =>
    get<ForeignDigest>(`/news/foreign/digest?hours=${hours}&limit=${limit}`),
};

// —— 自动研究闭环（/auto/*）——
export interface AutoRunSummary {
  evaluated?: number; elites?: number; best_ic?: number;
  enabled?: { name: string; rating: string; ic: number | null; icir: number | null; status: string }[];
  candidates?: { name: string; rating: string }[];
  skipped?: { name: string; rating: string }[];
  date?: string; best?: string | null; worst?: string | null;
  weakening?: string[]; suggestions?: string[];
  factors?: { enabled: number; total: number; new_this_week: number };
  portfolio?: Record<string, number>;
  strategies?: { name: string; exc_10: number; exc_5: number | null; n: number }[];
}
export interface AutoRun {
  id: number; job: string; trigger: string; status: string;
  summary: AutoRunSummary; report_path?: string | null; error?: string | null;
  started_at: string | null; finished_at: string | null;
}
export interface AutoConfig {
  mining_enabled: boolean; review_enabled: boolean;
  auto_enable_excellent: boolean; email_review: boolean;
  mining_time: string; review_weekday: number; review_time: string;
  mining_pop: number; mining_gens: number; mining_top_k: number;
}
export interface AutoStatus {
  config: AutoConfig;
  mining_next: string; review_next: string; last_error: string | null;
  last_mining: AutoRun | null; last_review: AutoRun | null; total_runs: number;
}

export const autoApi = {
  status: () => get<AutoStatus>("/auto/status"),
  runs: (limit = 20) => get<AutoRun[]>(`/auto/runs?limit=${limit}`),
  runDetail: (id: number) => get<AutoRun & { report_html?: string | null }>(`/auto/runs/${id}`),
  runJob: (job: "mining" | "review") => post<{ ok: boolean; run_id: number }>(`/auto/run/${job}`, {}),
  saveConfig: (patch: Partial<AutoConfig>) =>
    put<{ ok: boolean; config: AutoConfig }>("/auto/config", patch),
};

// —— 研究卡（/research/cards，研究卡机制第 4 步）——
export interface CardHistoryEntry {
  at?: string; date?: string;
  from: string | null; to: string; reason: string; actor?: string;
}
export interface ResearchCardItem {
  id: number; card_type: "strategy" | "factor"; key: string; name: string;
  hypothesis: string; data_spec: string;
  evidence: {
    w240?: number | null; w120?: number | null; w60?: number | null; d90?: number | null;
    exc_10?: number | null; exc_5?: number | null; n_10?: number; n?: number;
    win_10?: number | null; t_10?: number | null; negative_weeks?: number;
    evidence_updated_at?: string;
    ic_mean?: number | null; icir?: number | null; rating?: string;
    weekly?: { date: string; exc_10: number | null; n_10: number; t_10?: number | null }[];
  };
  cost_note: string; crowd_note: string; fail_condition: string;
  state: "enabled" | "candidate" | "watch" | "downgraded" | "retired";
  history: CardHistoryEntry[];
  updated_at: string | null;
  /** 对应的 registry 策略 key（空 = 这张卡还没封装成可回测策略） */
  strategy_key?: string;
  /** 最近一次真实回测；factor 卡恒 null（不是可回测的东西） */
  backtest?: {
    id: number; start: string; end: string;
    total_return: number | null; benchmark_total_return: number | null;
    excess_return: number | null; sharpe: number | null;
    max_drawdown: number | null; trade_count: number | null; win_rate: number | null;
  } | null;
}
export interface ResearchCardsResp {
  total: number;
  state_count: Record<string, number>;
  cards: ResearchCardItem[];
}
export interface CardSuggestion {
  card_id: number; key: string; name: string; state: string;
  evidence_date?: string | null;
  kind: "downgrade" | "retire" | "upgrade" | "demote_watch";
  to_state: string; text: string;
}
// —— 卡片信号详情（/research/cards/{id}/signals）——
export interface CardSignalAction {
  symbol: string; name?: string | null; action?: string | null;
  streak?: number | null; resonance?: number | null;
  close?: number | null; reason?: string | null;
}
export interface StatRow {
  [k: string]: string | number | null | undefined;
}
export interface CardSignalsResp {
  ok: boolean; error?: string; key?: string; name?: string; window_days?: number;
  perf?: StatRow; by_month?: StatRow[]; by_streak?: StatRow[]; by_resonance?: StatRow[];
  recent_days?: { date: string; count: number }[];
  latest_date?: string | null; latest_total?: number; latest_actions?: CardSignalAction[];
}
export const researchApi = {
  cards: () => get<ResearchCardsResp>("/research/cards"),
  suggestions: () => get<{ total: number; items: CardSuggestion[] }>("/research/suggestions"),
  transition: (id: number, to_state: string, reason: string) =>
    post<{ ok: boolean; changed: boolean; state: string }>(`/research/cards/${id}/transition`, { to_state, reason }),
  card: (id: number) => get<ResearchCardItem>(`/research/cards/${id}`),
  cardSignals: (id: number, date?: string) =>
    get<CardSignalsResp>(`/research/cards/${id}/signals${date ? `?date=${date}` : ""}`),
};

// —— 公众号阅读（/wechat/*，数据来自 knowledge.db 全文库）——
export interface WechatStats {
  available: boolean; msg?: string;
  n?: number; mps?: number;
  first_date?: string; last_date?: string; stale_days?: number | null;
  today?: number; last7d?: number; total_chars?: number;
}
export interface WechatMp {
  mp_id: string; mp_name: string; n: number; last_ts: number;
  last_date: string; n7d: number;
}
export interface WechatArticle {
  id: string; mp_id: string; mp_name: string; title: string;
  author?: string; publish_time: number; publish_date?: string;
  url: string; cover?: string;
  digest: string; chars: number;
}
export interface WechatArticleDetail extends Omit<WechatArticle, "digest"> {
  content_text: string; fetched_date?: string;
}
export interface WechatListResp {
  items: WechatArticle[]; total: number; offset?: number; limit?: number;
  db_missing?: boolean; error?: string;
}
export interface WechatDailyResp {
  date: string; items: WechatArticle[]; n?: number; error?: string;
}
export interface WechatTrendResp { items: { date: string; n: number }[]; days: number }

export interface WechatUpstream {
  level: "ok" | "warn" | "fail";
  verdict: string;
  source: string;                       // weread = 直连微信读书
  cookie: { ok: boolean; vid: string; error: string };
  feeds: number;
  last_run: {
    ok?: boolean; new?: number; skip?: number; fail?: number;
    seconds?: number; at?: string; error?: string | null;
  };
}

export const wechatApi = {
  stats: () => get<WechatStats>("/wechat/stats"),
  upstream: () => get<WechatUpstream>("/wechat/upstream"),
  collect: (limit = 0) => post<{ ok: boolean; note: string }>(`/wechat/collect?limit=${limit}`, {}),
  mps: (limit = 300) => get<{ items: WechatMp[]; n: number }>(`/wechat/mps?limit=${limit}`),
  articles: (p: { mp_id?: string; q?: string; since_days?: number; limit?: number; offset?: number }) => {
    const qs = new URLSearchParams();
    if (p.mp_id) qs.set("mp_id", p.mp_id);
    if (p.q) qs.set("q", p.q);
    if (p.since_days) qs.set("since_days", String(p.since_days));
    qs.set("limit", String(p.limit ?? 30));
    qs.set("offset", String(p.offset ?? 0));
    return get<WechatListResp>(`/wechat/articles?${qs.toString()}`);
  },
  article: (id: string) => get<WechatArticleDetail>(`/wechat/article/${encodeURIComponent(id)}`),
  daily: (date = "") => get<WechatDailyResp>(`/wechat/daily${date ? `?date=${date}` : ""}`),
  trend: (days = 14) => get<WechatTrendResp>(`/wechat/trend?days=${days}`),
  /** 新增/更新订阅号：写 feeds.json，下一轮采集生效。 */
  addFeed: (mp_id: string, mp_name: string) =>
    post<{ ok: boolean; created: boolean; mp_id: string; mp_name: string; verified?: string; total: number }>(
      "/wechat/feeds", { mp_id, mp_name }),
};

// ══════════════════════════════════════════════════════════════════
// ETF 资金流（国家队 ETF 资金看板 · 观察口径）
//
// 与 nsiApi（信号口径）的区别见后端 services/etf_flow.py 开头：
// 这里是 20 只 / 12 类宽基 / 分钟量能，只用于观察，不参与任何仓位决策。
// ══════════════════════════════════════════════════════════════════

export interface EtfUniverseItem {
  symbol: string;
  name: string;
  category: string;
  scale: number;
}

export interface EtfFlowMeta {
  as_of: string;
  prev_trade_date: string;
  last_minute: string;
  count: number;
  live: boolean;
  is_today: boolean;
  /** 这一屏分钟线是官方 1min 线、实时采样拼的，还是今天压根没有 */
  bars_source: "official" | "sampled" | "none";
  /** 采样模式下首个有效点的时刻（如 "14:51"），官方模式为 null */
  sampled_from: string | null;
  /** 采样模式下累计到多少个有效分钟点，官方模式为 null */
  sampled_count: number | null;
  partial_from: number | null;   // 盘中启动时首个有效点的下标
  generated_at: string;
  universe: EtfUniverseItem[];
}

export interface NsiHistoryPoint {
  date: string;
  score: number;
  nsi: number | null;
  position: number | null;
}

export interface EtfFlowNsi {
  available: boolean;
  as_of?: string;
  score?: number;
  nsi_raw?: number;
  pos_penalty?: number;
  p750?: number;
  score_percentile?: number;
  hi_threshold?: number;
  flow?: number;
  behav?: number;
  press?: number;
  position?: number;
  base_position?: number;
  last_change?: string;
  holding_days?: number;
  hhi?: number;
  buyer_type?: string;
  regime?: string;
  etf_share_as_of?: string;
  data_lag_days?: number | null;
  data_warning?: string | null;
  note?: string;
  history?: NsiHistoryPoint[];
  /** 加仓窗口倒计时（NSI 是单向状态机，到期自动回落） */
  window?: {
    active: boolean;
    hold_days: number;
    used: number;
    remaining: number;
    expire: string | null;
    weakened: boolean;
    last_trigger?: string;
    note: string;
  };

  /** 信号口径与观察视图当天是否配合（服务端判定） */
  consistency?: {
    status: "aligned" | "divergent" | "unconfirmed";
    label: string;
    signal: string;
    observation: string;
    /** strong=份额口径（有方向）· weak=量能口径（只有活跃度）· none=无数据 */
    evidence: "strong" | "weak" | "none";
    note: string;
  };
}

/** 尾盘集中度：最后 30 分钟成交额占全天的比例，与 20 日均值对比 */
export interface EtfFlowTailDay { date: string; ratio: number }

export interface EtfFlowTail {
  window: string;
  today: number | null;
  avg20: number | null;
  mult: number | null;
  series: EtfFlowTailDay[];
}

export interface EtfFlowMinute {
  labels: string[];
  count: number;
  today_amount: (number | null)[];
  prev_amount: number[];
  avg20_amount: number[];
  today_index: (number | null)[];
  today_cum: (number | null)[];
  prev_cum: (number | null)[];
  avg20_cum: (number | null)[];
  last: number | null;
  prev_close: number | null;
  tail: EtfFlowTail;
}

export interface EtfFlowSlots {
  labels: string[];
  surge_amount: number[];
  surge_chg: number[];
  normal_amount: number[];
  normal_chg: number[];
  surge_samples: number;
  normal_samples: number;
}

export interface EtfSupportDay {
  date: string;
  net_inflow: number;
  turnover: number;
  index: number | null;
  index_chg: number;
}

/** 托底日之后：T+n 前瞻收益的事后统计（同指数口径，非可交易回测） */
export interface EtfFlowForwardStat {
  n: number;
  up: number;
  win: number;      // 上涨概率 %
  mean: number;
  median: number;
  worst: number;
  best: number;
}

export interface EtfFlowForwardPoint {
  date: string;
  net_inflow: number;
  index_chg: number;
  t5: number | null;
  t20: number | null;
  t60: number | null;
  /** 对照基准：上证综指 / 510300 的同窗口收益 */
  ix5?: number | null; ix20?: number | null; ix60?: number | null;
  hs5?: number | null; hs20?: number | null; hs60?: number | null;
}

export interface EtfFlowForwardGroup {
  label: string;
  n: number;
  t5: Omit<EtfFlowForwardStat, "worst" | "best" | "mean"> | null;
  t20: Omit<EtfFlowForwardStat, "worst" | "best" | "mean"> | null;
  t60: Omit<EtfFlowForwardStat, "worst" | "best" | "mean"> | null;
}

export interface EtfFlowForward {
  horizons: number[];
  /** 主基准 = 一档等权净值（实际被托底的宽基组合） */
  stats: Record<string, EtfFlowForwardStat>;
  /** 对照：上证综指（旧口径）与 510300，用来暴露结论对基准的敏感度 */
  benchmark?: {
    main: string; index: string; core: string; tier1: string[]; note: string;
  };
  index_stats?: Record<string, EtfFlowForwardStat>;
  core_stats?: Record<string, EtfFlowForwardStat>;
  corr: Record<string, number | null>;
  points: EtfFlowForwardPoint[];
  /** 按托底时的估值分位分组后的后市表现 */
  by_valuation: EtfFlowForwardGroup[];
  /** 稳健性：按「托底段」去重（连续多日只取首日）后的 T+60 */
  dedup?: {
    segments: number;
    days: number;
    low: { n: number; up: number; win: number; median: number; mean: number } | null;
    mid: { n: number; up: number; win: number; median: number; mean: number } | null;
    high: { n: number; up: number; win: number; median: number; mean: number } | null;
  };
}

/** 全市场估值（滚动 5 年分位），供「估值 × 资金流」条件判断 */
export interface EtfFlowValuation {
  available: boolean;
  as_of?: string;
  pe?: number | null;
  pct?: number | null;
  pb?: number | null;
  pct_pb?: number | null;
  start?: string;
  count?: number;
  dates?: string[];
  pe_series?: (number | null)[];
  pct_series?: (number | null)[];
  /** 托底日当天的估值分位，用于在 PE 曲线上打点 */
  support_marks?: { date: string; pct: number }[];
  support_pct?: {
    n: number; median: number; p25: number; p75: number;
    low_share: number; high_share: number;
  } | null;
  /** 按年看托底时的估值分位中位数 —— 最能说明行为模式变化 */
  support_yearly?: { year: string; n: number; median: number }[];
}

/** 一档弹性判据：成交额能否反推申购（分层依据是逐只回归的弹性 b 与 R²） */
export interface EtfElasticityMember {
  symbol: string;
  name: string;
  category: string;
  scale: number;
  b21: number | null;
  b60: number | null;
  b120: number | null;
  r2_60: number | null;
  hits: number;          // 三个窗口里达标的个数
  tier: "一档" | "二档";
  marginal: boolean;     // 贴着阈值，会随一天数据翻档
}

export interface EtfElasticityBand {
  label: string;
  verdict: string;
  n: number;
  median: number | null;
  positive: number | null;
  big: number | null;    // 「国家队级净申购」概率 %
  lift: number | null;   // 相对全体基准的倍数
  hits: number;          // 该档实际命中次数
  /** Wilson 95% 区间（百分数）。k=0 时 ci_hi 是「尚未观测到」的上界，不能写成 0% */
  ci_lo: number | null;
  ci_hi: number | null;
}

export interface EtfElasticityDay {
  date: string;
  amount: number;
  base: number | null;
  mult: number | null;
  net: number | null;
}

export interface EtfElasticity {
  available: boolean;
  as_of?: string;
  reason?: string;
  params?: {
    windows: number[]; votes: number; b: number; r2: number;
    base_win: number; big_net: number;
  };
  members?: EtfElasticityMember[];
  tier1?: string[];
  tier1_count?: number;
  tier1_share?: number | null;
  judge?: {
    amount: number; base: number | null; mult: number | null;
    net: number | null; band: string | null; verdict: string | null;
    prob: number | null; lift: number | null; base_rate: number | null;
  };
  /** 主表口径：point_in_time = 逐日重选名单，无前视偏差 */
  basis?: "point_in_time" | "sample";
  bands?: EtfElasticityBand[];
  /** 对照：用今天名单回溯全历史的样本内口径，用来暴露选择偏差 */
  bands_sample?: EtfElasticityBand[];
  pit?: {
    valid_days: number; total_days: number; coverage: number;
    tier1_median: number | null; overlap_mean: number | null;
    same_list_share: number | null; base_rate: number | null;
  };
  recent?: EtfElasticityDay[];
  /** 近 250 日「一档合计」的滚动 60 日弹性轨迹 */
  trend?: { date: string; b: number }[];
  /** 判据可用性护栏：名单被挑走 / 弹性翻负时，概率表要降级或停用 */
  availability?: {
    state: "ok" | "degraded" | "disabled";
    reasons: string[];
    note: string;
    checks: {
      tier1_count: number; b_now: number | null;
      regime: string; marginal: number;
    };
  };
  sign_flag?: {
    year: number;
    b_ytd: number | null; r2_ytd: number | null;
    b_now: number | null; r2_now: number | null;
    net_sum: number;
    regime: "regime_shift" | "aligned_sell" | "aligned_buy" | "unclear";
    tone: "up" | "down" | "warn" | "plain";
    note: string;
  };
}

/** 焦点切换：当日成交额 top3 与前一日的对比 */
export interface EtfFlowFocusItem {
  symbol: string;
  name: string;
  amount: number;
  share: number;
}

export interface EtfFlowFocus {
  today: EtfFlowFocusItem[];
  prev: EtfFlowFocusItem[];
  prev_date: string;
  overlap: number;
  top1_changed: boolean;
  hhi: number | null;
  hhi_prev: number | null;
}

export interface EtfFlowSupport {
  excluded: { start: string; end: string; reason: string }[];
  days: EtfSupportDay[];
  today_turnover: number;
  today_estimate: number;
  threshold: number;
  sample_count: number;
  raw_count: number;
  forward: EtfFlowForward;
}

export interface EtfFlowDaily {
  dates: string[];
  index: (number | null)[];
  chg: number[];
  turnover: (number | null)[];
  net_inflow: (number | null)[];
}

export interface EtfFlowCategory {
  labels: string[];
  scale: number[];
  dates: string[];
  matrix: number[][];
  /** 字段名沿用历史叫法，实际是「最近 range_days 个交易日合计」，通常跨自然月 */
  month_total: number[];
  turnover: number[];
  range_days?: number;
  /** 按日汇总：前端在矩阵最前面用它们做「每天一句话」 */
  day_total?: number[];
  day_in?: number[];
  day_out?: number[];
  day_leader?: { name: string; value: number }[];
  /** 份额 T+1：矩阵最新日常比看板 as_of 落后一个交易日 */
  lag?: { matrix_last: string; as_of: string } | null;
  scope?: string;
}

export interface EtfFlowSummaryMetric {
  label: string;
  value: string;
  unit: string;
  sub: string;
  tone: "up" | "down" | "plain";
}

export interface EtfFlowSummaryPoint {
  tag: string;   // 量能 / 方向 / 信号
  text: string;
}

/** 顶部结论卡：后端规则驱动生成，判断口径见 services/etf_flow.py::_build_summary。 */
export interface EtfFlowSummary {
  as_of: string;
  live: boolean;
  headline: string;
  tone: "up" | "down" | "warn" | "calm";
  metrics: EtfFlowSummaryMetric[];
  points: EtfFlowSummaryPoint[];
  caveat: string;
  /** 与 elasticity.availability 同步，结论卡据此显示护栏徽标 */
  availability?: "ok" | "degraded" | "disabled";
}

export interface EtfFlowDetailItem {
  symbol: string;
  name: string;
  category: string;
  amount: number;      // 当日成交额（亿元）
  share: number;       // 占 20 只合计的百分比
  pct_chg: number | null;
  rel: number;         // 相对成交额最大者的比例（0–1，用作条形长度）
  /** 今日成交额 ÷ 前 20 日中位数 */
  mult20: number | null;
  /** 今日 ÷ 前一交易日 */
  mult_prev: number | null;
  /** 绝对量是否够国家队量级（≥50 亿）——倍数达标但量级不足就是噪声 */
  enough: boolean;
}

/** 托底门槛口径对照：固定 100 亿 vs 随成交额缩放的相对门槛 */
export interface EtfFlowThresholdSummary {
  count: number;
  by_year: { year: string; n: number; share: number }[];
  pct_median: number | null;
  pct_n: number;
  low_share: number | null;
  high_share: number | null;
  fwd: Record<string, number | null>;
}

export interface EtfFlowThresholdCompare {
  absolute: EtfFlowThresholdSummary;
  relative: EtfFlowThresholdSummary;
  ratio: number;
  note: string;
  verdict: string;
  pct_median_gap: number | null;
}

export interface EtfFlowDashboard {
  meta: EtfFlowMeta;
  summary: EtfFlowSummary;
  etf_detail: EtfFlowDetailItem[];
  focus: EtfFlowFocus;
  valuation: EtfFlowValuation;
  threshold_compare?: EtfFlowThresholdCompare;
  elasticity: EtfElasticity;
  nsi: EtfFlowNsi;
  minute: EtfFlowMinute;
  intraday_15m: EtfFlowSlots;
  support: EtfFlowSupport;
  daily: EtfFlowDaily;
  category: EtfFlowCategory;
}

export interface EtfFlowStatus {
  universe: number;
  share_rows: number;
  minute_rows: number;
  flow_days: number;
  last_share_date: string | null;
  last_flow_date: string | null;
  last_sample: string | null;
  task: { running: boolean; kind: string | null; message: string };
  task_elapsed: number | null;
}

export const etfFlowApi = {
  dashboard: () => get<EtfFlowDashboard>("/etf-flow/dashboard"),
  status: () => get<EtfFlowStatus>("/etf-flow/status"),
  bootstrap: (force = false) =>
    post<{ ok: boolean; message: string }>(`/etf-flow/bootstrap?force=${force}`, {}),
  update: () => post<{ ok: boolean; message: string }>("/etf-flow/update", {}),
  sample: () => post<{ ok: boolean; points: number }>("/etf-flow/sample", {}),
  /** 强制拉当日官方 1min 线；ok=false 表示源站还没发布，不是出错 */
  minutes: () => post<{ ok: boolean; symbols: number; message: string }>("/etf-flow/minutes", {}),
};
