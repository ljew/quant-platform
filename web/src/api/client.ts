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

/** /market/stocks 返回的标的信息 */
export interface StockItem {
  symbol: string;
  name: string;
  market?: string;
  industry?: string | null;
  list_date?: string | null;
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
}

// —— 业务 API ——
export const api = {
  strategies: () => get<StrategyInfo[]>("/strategy/strategies"),
  kline: (symbol: string, start: string, end: string) =>
    get<KlineBar[]>(`/market/kline/${symbol}?start=${start}&end=${end}`),
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
  // 参数寻优
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
  paperCreate: (body: Record<string, unknown>) => post<PaperTask>("/paper/tasks", body),
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
  monitorHealth: () => get<HealthReport>(`/monitor/health-report`),
  frameworkHealth: () => get<FrameworkHealth>(`/monitor/framework-health`),
  holidayMode: () => get<HolidayMode>(`/monitor/holiday-mode`),
  setHolidayMode: (mode: "auto" | "on" | "off") =>
    put<HolidayMode>(`/monitor/holiday-mode`, { mode }),
  monitorDataflow: () => get<DataflowReport>(`/monitor/dataflow`),
  monitorLineage: () => get<LineageReport>(`/monitor/lineage`),
  monitorAssets: (force = false) => get<AssetsReport>(`/monitor/assets?force=${force}`),
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
};

export interface RegistryRow {
  id: number; name: string; expr: string; direction: number;
  category: string; status: string; ic_mean: number | null; created_at: string;
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

export const newsApi = {
  feed: (refresh = false) => get<NewsFeed>(`/news/feed${refresh ? "?refresh=true" : ""}`),
  stock: (symbol: string) =>
    get<StockNews>(`/news/stock?symbol=${encodeURIComponent(symbol)}`),
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
  stats: Record<string, EtfFlowForwardStat>;
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
  month_total: number[];
  turnover: number[];
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
}

export interface EtfFlowDetailItem {
  symbol: string;
  name: string;
  category: string;
  amount: number;      // 当日成交额（亿元）
  share: number;       // 占 20 只合计的百分比
  pct_chg: number | null;
  rel: number;         // 相对成交额最大者的比例（0–1，用作条形长度）
}

export interface EtfFlowDashboard {
  meta: EtfFlowMeta;
  summary: EtfFlowSummary;
  etf_detail: EtfFlowDetailItem[];
  focus: EtfFlowFocus;
  valuation: EtfFlowValuation;
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
