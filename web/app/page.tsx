import { getHomeSummary } from "@/lib/api";
import { KpiCard } from "@/components/kpi-card";
import { EquityChart } from "@/components/equity-chart";

function formatCurrency(value: number): string {
  return `₹${value.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

function formatPct(value: number | null): string {
  return value === null ? "—" : `${(value * 100).toFixed(1)}%`;
}

export default async function Home({
  searchParams,
}: {
  searchParams: Promise<{ session?: string }>;
}) {
  const { session } = await searchParams;
  const summary = await getHomeSummary(session);

  return (
    <div className="p-8">
      <main className="mx-auto max-w-5xl space-y-6">
        <header className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-semibold">AQTP Dashboard</h1>
            <p className="text-sm text-neutral-500">
              {summary.session_id} · {summary.strategy_name ?? summary.app_name} ·{" "}
              <span className="uppercase">{summary.status}</span>
            </p>
          </div>
        </header>

        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <KpiCard
            label="Realized P&L"
            value={formatCurrency(summary.realized_pnl)}
            tone={summary.realized_pnl >= 0 ? "good" : "bad"}
          />
          <KpiCard label="Trades" value={String(summary.trade_count)} />
          <KpiCard label="Win Rate" value={formatPct(summary.win_rate)} />
          <KpiCard
            label="Profit Factor"
            value={summary.profit_factor === null ? "—" : summary.profit_factor.toFixed(2)}
          />
          <KpiCard
            label="Sharpe (per-trade)"
            value={summary.sharpe_ratio === null ? "—" : summary.sharpe_ratio.toFixed(2)}
          />
          <KpiCard label="Max Drawdown" value={formatCurrency(summary.max_drawdown)} tone="bad" />
          <KpiCard label="Trading Date" value={summary.trading_date ?? "—"} />
          <KpiCard label="Broker/Status" value={summary.status} />
        </div>

        <section className="rounded-xl border border-neutral-200 dark:border-neutral-800 p-4">
          <h2 className="mb-2 text-sm font-medium text-neutral-500">Cumulative Realized P&L</h2>
          <EquityChart
            data={summary.equity_curve.map(([timestamp, pnl]) => ({ timestamp, pnl }))}
          />
        </section>
      </main>
    </div>
  );
}
