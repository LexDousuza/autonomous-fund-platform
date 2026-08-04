import { getAllocation, getLatestBacktest } from "@/lib/api";
import { KpiCard } from "@/components/kpi-card";
import { ComparisonChart } from "@/components/comparison-chart";

function fmtCurrency(v: number | null): string {
  return v === null ? "—" : `₹${v.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

function fmtPct(v: number | null): string {
  return v === null ? "—" : `${v.toFixed(2)}%`;
}

export default async function PortfolioPage() {
  const [allocation, backtest] = await Promise.all([
    getAllocation(),
    getLatestBacktest().catch(() => null),
  ]);

  return (
    <div className="p-8">
      <main className="mx-auto max-w-5xl space-y-6">
        <h1 className="text-xl font-semibold">Portfolio</h1>

        <div className="grid gap-4 sm:grid-cols-2">
          <section className="rounded-xl border border-neutral-200 dark:border-neutral-800 p-4">
            <h2 className="mb-3 text-sm font-medium text-neutral-500">Capital by Strategy</h2>
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase text-neutral-500">
                <tr>
                  <th className="py-1">Strategy</th>
                  <th className="py-1">Intraday</th>
                  <th className="py-1">Reserve</th>
                  <th className="py-1">Max/Trade</th>
                </tr>
              </thead>
              <tbody>
                {allocation.by_strategy.map((s) => (
                  <tr key={s.strategy_name} className="border-t border-neutral-200 dark:border-neutral-800">
                    <td className="py-1 font-medium">{s.strategy_name}</td>
                    <td className="py-1">{fmtCurrency(s.intraday_capital)}</td>
                    <td className="py-1">{fmtCurrency(s.reserve_capital)}</td>
                    <td className="py-1">{fmtCurrency(s.max_capital_per_trade)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          <section className="rounded-xl border border-neutral-200 dark:border-neutral-800 p-4">
            <h2 className="mb-3 text-sm font-medium text-neutral-500">Sessions by Broker</h2>
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase text-neutral-500">
                <tr>
                  <th className="py-1">Broker</th>
                  <th className="py-1">Sessions</th>
                </tr>
              </thead>
              <tbody>
                {allocation.by_broker.map((b) => (
                  <tr key={b.broker} className="border-t border-neutral-200 dark:border-neutral-800">
                    <td className="py-1 font-medium">{b.broker}</td>
                    <td className="py-1">{b.session_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </div>

        {backtest ? (
          <>
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <KpiCard label="Portfolio Return" value={fmtPct(backtest.result.portfolio.metrics.total_return_pct)} />
              <KpiCard
                label={`${backtest.result.benchmark.symbol} Return`}
                value={fmtPct(backtest.result.benchmark.metrics.total_return_pct)}
              />
              <KpiCard
                label="Portfolio Sharpe"
                value={backtest.result.portfolio.metrics.sharpe_ratio?.toFixed(2) ?? "—"}
              />
              <KpiCard
                label="Portfolio Max DD"
                value={fmtPct(backtest.result.portfolio.metrics.max_drawdown_pct)}
                tone="bad"
              />
            </div>

            <section className="rounded-xl border border-neutral-200 dark:border-neutral-800 p-4">
              <h2 className="mb-2 text-sm font-medium text-neutral-500">
                Latest Backtest: Portfolio vs {backtest.result.benchmark.symbol}
              </h2>
              <p className="mb-2 text-xs text-neutral-500">
                {backtest.window_from} to {backtest.window_to} · generated{" "}
                {new Date(backtest.generated_at).toLocaleString()}
              </p>
              <ComparisonChart
                portfolioSeries={backtest.result.portfolio.equity_curve}
                benchmarkSeries={backtest.result.benchmark.series}
                benchmarkLabel={backtest.result.benchmark.symbol}
              />
            </section>
          </>
        ) : (
          <p className="text-sm text-neutral-500">No backtest runs found yet.</p>
        )}
      </main>
    </div>
  );
}
