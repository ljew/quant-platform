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
export const del = <T>(path: string) => request<T>(path, { method: "DELETE" });

// —— 类型 ——
export interface StrategyInfo {
  key: string;
  name: string;
  desc?: string;
  default_params: Record<string, unknown>;
  param_schema: { key: string; label: string; type: string; default: unknown }[];
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
  // 新闻情绪因子（文本管道第一层）
  factorNewsDaily: (limit = 500) =>
    get<{ date: string; n_articles: number; n_finance: number; bull: number; bear: number; net_sentiment: number | null }[]>(`/factor/news/daily?limit=${limit}`),
  factorNewsTest: (extreme_pct: number, horizon: number) =>
    post<NewsEventReport>("/factor/news/event-test", { extreme_pct, horizon }),
  // 数据健康度 + 数据流全景
  monitorHealth: () => get<HealthReport>(`/monitor/health-report`),
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

export interface HealthCheck {
  name: string;
  status: "ok" | "warn" | "error";
  value: string;
  expect: string;
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
export interface HealthReport {
  overall_score: number;
  overall_status: "healthy" | "warn" | "error";
  layers: Record<string, HealthLayer>;
  alerts: HealthAlert[];
  generated_at: string;
}

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
}

// —— GP 自动挖掘类型 ——
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
  total_return: number;
  annual_return: number;
  max_drawdown: number;
  sharpe: number;
  win_rate: number;
  trade_count: number;
  final_equity: number;
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
    latest_trade_date: string | null; latest_news_date: string | null;
    lag_trading_days: number | null; lag_calendar_days: number | null;
    news_lag_trading_days: number | null;
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
