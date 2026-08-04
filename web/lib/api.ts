// Thin fetch wrapper around apps/api (FastAPI). Server-side only for now
// (page.tsx is a Server Component) - no client bundle exposure needed
// until a page requires client-side polling (Phase 2+).

export type HomeSummary = {
  session_id: string;
  app_name: string;
  status: string;
  strategy_name: string | null;
  trading_date: string | null;
  started_at: string;
  ended_at: string | null;
  realized_pnl: number;
  trade_count: number;
  win_rate: number | null;
  profit_factor: number | null;
  sharpe_ratio: number | null;
  max_drawdown: number;
  equity_curve: [string, number][];
};

export type StrategySummary = {
  name: string;
  active: boolean;
  session_count: number;
  last_session_at: string | null;
  last_status: string | null;
  trade_count: number;
  total_pnl: number;
  win_rate: number | null;
  profit_factor: number | null;
  sharpe_ratio: number | null;
  max_drawdown: number;
};

export type StrategyDetail = StrategySummary & {
  recent_sessions: { session_id: string; status: string; started_at: string }[];
  signal_funnel: { generated: number; approved: number; rejected: number };
  avg_win: number | null;
  avg_loss: number | null;
  latest_trades: {
    symbol: string;
    entry_price: number;
    exit_price: number;
    quantity: number;
    pnl: number;
    exit_timestamp: string;
  }[];
};

export type OrderRecord = {
  session_id: string;
  order_id: string;
  symbol: string;
  side: string;
  quantity: number;
  status: string;
  intended_price: number | null;
  average_price: number | null;
  submitted_at: string;
  placed_at: string;
  latency_ms: number;
  slippage: number | null;
};

export type TradeRecord = {
  session_id: string;
  strategy_name: string | null;
  trading_date: string | null;
  symbol: string;
  entry_price: number;
  exit_price: number;
  quantity: number;
  pnl: number;
  exit_timestamp: string;
};

export type TradeHistory = {
  trades: TradeRecord[];
  total_count: number;
  pnl_distribution: { range_start: number; range_end: number; count: number }[];
  calendar_heatmap: { date: string; pnl: number }[];
  day_of_week: { day: string; trade_count: number; total_pnl: number; avg_pnl: number }[];
};

const API_BASE_URL = process.env.AQTP_API_BASE_URL ?? "http://127.0.0.1:8000";

async function apiGet<T>(path: string, params?: Record<string, string>): Promise<T> {
  const url = new URL(path, API_BASE_URL);
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value) url.searchParams.set(key, value);
  }

  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) {
    throw new Error(`GET ${url} failed: ${res.status} ${await res.text()}`);
  }
  return res.json();
}

export function getHomeSummary(sessionId?: string): Promise<HomeSummary> {
  return apiGet("/api/home/summary", { session_id: sessionId ?? "" });
}

export function listStrategies(): Promise<StrategySummary[]> {
  return apiGet("/api/strategies");
}

export function getStrategyDetail(name: string): Promise<StrategyDetail> {
  return apiGet(`/api/strategies/${encodeURIComponent(name)}`);
}

export function listOrders(params?: {
  session_id?: string;
  symbol?: string;
  status?: string;
}): Promise<OrderRecord[]> {
  return apiGet("/api/orders", params);
}

export function listTrades(params?: { symbol?: string; strategy_name?: string }): Promise<TradeHistory> {
  return apiGet("/api/trades", params);
}

export type AllocationBreakdown = {
  by_strategy: {
    strategy_name: string;
    as_of_session_started_at: string;
    intraday_capital: number | null;
    reserve_capital: number | null;
    max_capital_per_trade: number | null;
  }[];
  by_broker: { broker: string; session_count: number }[];
};

export type BacktestSummary = {
  generated_at: string;
  window_from: string | null;
  window_to: string | null;
  starting_capital: number;
  strategies: string[];
  result: {
    portfolio: {
      metrics: {
        total_return_pct: number;
        cagr_pct: number | null;
        sharpe_ratio: number | null;
        max_drawdown_pct: number;
      };
      equity_curve: [string, number][];
    };
    benchmark: {
      symbol: string;
      metrics: {
        total_return_pct: number;
        cagr_pct: number | null;
        sharpe_ratio: number | null;
        max_drawdown_pct: number;
      };
      series: [string, number][];
    };
  };
};

export type MarketOverview = {
  indices: Record<
    string,
    { available: boolean; series?: [string, number][]; reason?: string }
  >;
  top_gainers: { symbol: string; last_close: number; change_pct: number }[];
  top_losers: { symbol: string; last_close: number; change_pct: number }[];
  breadth: { advancing: number; declining: number; unchanged: number };
  universe_size: number;
};

export type CorrelationMatrix = {
  symbols: string[];
  matrix: (number | null)[][];
};

export type WatchlistItem = {
  id: number;
  symbol: string;
  exchange: string;
  added_at: string;
  last_close: number | null;
  sparkline: number[];
};

export function getAllocation(): Promise<AllocationBreakdown> {
  return apiGet("/api/portfolio/allocation");
}

export function getLatestBacktest(): Promise<BacktestSummary> {
  return apiGet("/api/portfolio/backtest");
}

export function getMarketOverview(): Promise<MarketOverview> {
  return apiGet("/api/market/overview");
}

export function getCorrelation(symbols?: string[]): Promise<CorrelationMatrix> {
  return apiGet("/api/market/correlation", symbols ? { symbols: symbols.join(",") } : undefined);
}

export function getWatchlist(listName = "default"): Promise<WatchlistItem[]> {
  return apiGet("/api/watchlist", { list_name: listName });
}

export type PortfolioRisk = {
  source: string;
  observation_count: number;
  var: { confidence_level: number; value_at_risk_pct: number; expected_shortfall_pct: number } | null;
  beta_vs_benchmark: number | null;
  benchmark_symbol: string;
};

export type ExposureSummary = {
  session_id: string;
  intraday_capital: number | null;
  total_notional: number;
  leverage: number | null;
  concentration_by_symbol: { symbol: string; notional: number; pct_of_notional: number | null }[];
};

export type BrokerHealth = {
  lookback_sessions: number;
  broker_errors: number;
  market_data_errors: number;
  note?: string;
};

export function getPortfolioRisk(): Promise<PortfolioRisk> {
  return apiGet("/api/risk/portfolio");
}

export function getExposure(): Promise<ExposureSummary> {
  return apiGet("/api/risk/exposure");
}

export function getBrokerHealth(): Promise<BrokerHealth> {
  return apiGet("/api/risk/broker-health");
}

export type RatiosSummary = {
  sharpe_ratio: number | null;
  cagr_pct: number | null;
  max_drawdown_pct: number;
  annualized_volatility_pct: number | null;
  sortino_ratio: number | null;
  calmar_ratio: number | null;
  omega_ratio: number | null;
  alpha_pct: number | null;
  treynor_ratio: number | null;
  information_ratio: number | null;
  benchmark_symbol: string;
  stress_test: {
    label: string;
    shock_return_pct: number;
    repetitions: number;
    resulting_value: number;
    drawdown_pct: number;
  }[];
};

export type Forecast = {
  symbol: string;
  spot: number;
  horizon_days: number;
  percentiles: number[];
  percentile_paths: Record<string, number[]>;
  historical_closes: number[];
};

export function getRatios(): Promise<RatiosSummary> {
  return apiGet("/api/analytics/ratios");
}

export function getForecast(symbol: string, horizonDays = 10): Promise<Forecast> {
  return apiGet("/api/analytics/forecast", { symbol, horizon_days: String(horizonDays) });
}
