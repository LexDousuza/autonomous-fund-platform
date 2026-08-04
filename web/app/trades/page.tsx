import { listTrades } from "@/lib/api";
import { PnlDistributionChart, DayOfWeekChart, CalendarHeatmapStrip } from "@/components/trade-charts";

export default async function TradesPage({
  searchParams,
}: {
  searchParams: Promise<{ symbol?: string; strategy_name?: string }>;
}) {
  const params = await searchParams;
  const history = await listTrades(params);

  return (
    <div className="p-8">
      <main className="mx-auto max-w-5xl space-y-6">
        <div>
          <h1 className="text-xl font-semibold">Trade History</h1>
          <p className="text-sm text-neutral-500">{history.total_count} closed trades, all sessions</p>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <section className="rounded-xl border border-neutral-200 dark:border-neutral-800 p-4">
            <h2 className="mb-2 text-sm font-medium text-neutral-500">P&L Distribution</h2>
            <PnlDistributionChart data={history.pnl_distribution} />
          </section>
          <section className="rounded-xl border border-neutral-200 dark:border-neutral-800 p-4">
            <h2 className="mb-2 text-sm font-medium text-neutral-500">Day of Week</h2>
            <DayOfWeekChart data={history.day_of_week} />
          </section>
        </div>

        <section className="rounded-xl border border-neutral-200 dark:border-neutral-800 p-4">
          <h2 className="mb-3 text-sm font-medium text-neutral-500">Calendar</h2>
          <CalendarHeatmapStrip data={history.calendar_heatmap} />
        </section>

        <section className="overflow-x-auto rounded-xl border border-neutral-200 dark:border-neutral-800">
          <table className="w-full text-sm">
            <thead className="bg-neutral-100 dark:bg-neutral-900 text-left text-xs uppercase text-neutral-500">
              <tr>
                <th className="px-4 py-2">Symbol</th>
                <th className="px-4 py-2">Strategy</th>
                <th className="px-4 py-2">Entry</th>
                <th className="px-4 py-2">Exit</th>
                <th className="px-4 py-2">Qty</th>
                <th className="px-4 py-2">P&L</th>
                <th className="px-4 py-2">Exit Time</th>
              </tr>
            </thead>
            <tbody>
              {history.trades.map((t, i) => (
                <tr key={i} className="border-t border-neutral-200 dark:border-neutral-800">
                  <td className="px-4 py-2 font-medium">{t.symbol}</td>
                  <td className="px-4 py-2 text-neutral-500">{t.strategy_name ?? "—"}</td>
                  <td className="px-4 py-2">{t.entry_price.toFixed(2)}</td>
                  <td className="px-4 py-2">{t.exit_price.toFixed(2)}</td>
                  <td className="px-4 py-2">{t.quantity}</td>
                  <td className={`px-4 py-2 ${t.pnl >= 0 ? "text-emerald-600" : "text-red-600"}`}>
                    {t.pnl.toFixed(2)}
                  </td>
                  <td className="px-4 py-2 text-neutral-500">{new Date(t.exit_timestamp).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </main>
    </div>
  );
}
