import { getRatios, getForecast } from "@/lib/api";
import { KpiCard } from "@/components/kpi-card";
import { ForecastFanChart } from "@/components/forecast-fan-chart";
import { ForecastSymbolForm } from "@/components/forecast-symbol-form";

function fmtNum(v: number | null): string {
  return v === null ? "—" : v.toFixed(2);
}

function fmtPct(v: number | null): string {
  return v === null ? "—" : `${v.toFixed(2)}%`;
}

export default async function AnalyticsPage({
  searchParams,
}: {
  searchParams: Promise<{ symbol?: string }>;
}) {
  const { symbol } = await searchParams;
  const forecastSymbol = symbol || "RELIANCE";

  const [ratios, forecast] = await Promise.all([
    getRatios().catch(() => null),
    getForecast(forecastSymbol).catch(() => null),
  ]);

  return (
    <div className="p-8">
      <main className="mx-auto max-w-5xl space-y-6">
        <h1 className="text-xl font-semibold">Analytics</h1>

        {ratios ? (
          <>
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <KpiCard label="Sharpe" value={fmtNum(ratios.sharpe_ratio)} />
              <KpiCard label="Sortino" value={fmtNum(ratios.sortino_ratio)} />
              <KpiCard label="Calmar" value={fmtNum(ratios.calmar_ratio)} />
              <KpiCard label="Omega" value={fmtNum(ratios.omega_ratio)} />
              <KpiCard label={`Alpha vs ${ratios.benchmark_symbol}`} value={fmtPct(ratios.alpha_pct)} />
              <KpiCard label="Treynor" value={fmtNum(ratios.treynor_ratio)} />
              <KpiCard label="Information Ratio" value={fmtNum(ratios.information_ratio)} />
              <KpiCard label="CAGR" value={fmtPct(ratios.cagr_pct)} tone={ (ratios.cagr_pct ?? 0) >= 0 ? "good" : "bad"} />
            </div>

            <section className="rounded-xl border border-neutral-200 dark:border-neutral-800 p-4">
              <h2 className="mb-3 text-sm font-medium text-neutral-500">
                Stress Test — worst historical day, repeated
              </h2>
              <table className="w-full text-sm">
                <thead className="text-left text-xs uppercase text-neutral-500">
                  <tr>
                    <th className="py-1">Scenario</th>
                    <th className="py-1">Shock/day</th>
                    <th className="py-1">Resulting Value</th>
                    <th className="py-1">Drawdown</th>
                  </tr>
                </thead>
                <tbody>
                  {ratios.stress_test.map((s) => (
                    <tr key={s.label} className="border-t border-neutral-200 dark:border-neutral-800">
                      <td className="py-1 font-medium">{s.label}</td>
                      <td className="py-1 text-red-600">{s.shock_return_pct.toFixed(2)}%</td>
                      <td className="py-1">₹{s.resulting_value.toLocaleString("en-IN", { maximumFractionDigits: 0 })}</td>
                      <td className="py-1 text-red-600">-{s.drawdown_pct.toFixed(2)}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          </>
        ) : (
          <p className="text-sm text-neutral-500">No backtest runs found yet.</p>
        )}

        <section className="rounded-xl border border-neutral-200 dark:border-neutral-800 p-4">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-medium text-neutral-500">
              Price Forecast (Monte Carlo, {forecast?.horizon_days ?? 10}-day)
            </h2>
            <ForecastSymbolForm defaultSymbol={forecastSymbol} />
          </div>
          {forecast ? (
            <ForecastFanChart forecast={forecast} />
          ) : (
            <p className="text-sm text-neutral-500">
              No daily-bar history for &quot;{forecastSymbol}&quot; to forecast from.
            </p>
          )}
        </section>
      </main>
    </div>
  );
}
