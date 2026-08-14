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
};

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
