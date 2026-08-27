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
};

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
