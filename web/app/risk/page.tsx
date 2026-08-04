import { getPortfolioRisk, getExposure, getBrokerHealth } from "@/lib/api";
import { KpiCard } from "@/components/kpi-card";

function fmtPct(v: number | null, digits = 2): string {
  return v === null ? "—" : `${v.toFixed(digits)}%`;
}

function fmtCurrency(v: number | null): string {
  return v === null ? "—" : `₹${v.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

export default async function RiskPage() {
  const [risk, exposure, brokerHealth] = await Promise.all([
    getPortfolioRisk().catch(() => null),
    getExposure().catch(() => null),
    getBrokerHealth(),
  ]);

  return (
    <div className="p-8">
      <main className="mx-auto max-w-5xl space-y-6">
        <h1 className="text-xl font-semibold">Risk</h1>

        {risk?.var ? (
          <>
            <p className="text-xs text-neutral-500">
              {risk.source} · {risk.observation_count} daily observations
            </p>
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <KpiCard
                label={`VaR (${(risk.var.confidence_level * 100).toFixed(0)}%, 1-day)`}
                value={fmtPct(risk.var.value_at_risk_pct)}
                tone="bad"
              />
              <KpiCard
                label="Expected Shortfall"
                value={fmtPct(risk.var.expected_shortfall_pct)}
                tone="bad"
              />
              <KpiCard
                label={`Beta vs ${risk.benchmark_symbol}`}
                value={risk.beta_vs_benchmark === null ? "—" : risk.beta_vs_benchmark.toFixed(3)}
              />
              <KpiCard label="Leverage" value={exposure?.leverage === null || exposure?.leverage === undefined ? "—" : `${exposure.leverage.toFixed(2)}x`} />
            </div>
          </>
        ) : (
          <p className="text-sm text-neutral-500">
            Not enough backtest history yet to compute VaR/ES/beta (needs 20+ daily observations).
          </p>
        )}

        {exposure && (
          <section className="rounded-xl border border-neutral-200 dark:border-neutral-800 p-4">
            <h2 className="mb-2 text-sm font-medium text-neutral-500">
              Exposure - session {exposure.session_id}
            </h2>
            <div className="mb-3 flex gap-6 text-sm">
              <span>Intraday capital: {fmtCurrency(exposure.intraday_capital)}</span>
              <span>Total notional: {fmtCurrency(exposure.total_notional)}</span>
            </div>
            {exposure.concentration_by_symbol.length === 0 ? (
              <p className="text-sm text-neutral-500">No open positions in this session.</p>
            ) : (
              <table className="w-full text-sm">
                <thead className="text-left text-xs uppercase text-neutral-500">
                  <tr>
                    <th className="py-1">Symbol</th>
                    <th className="py-1">Notional</th>
                    <th className="py-1">% of Book</th>
                  </tr>
                </thead>
                <tbody>
                  {exposure.concentration_by_symbol.map((c) => (
                    <tr key={c.symbol} className="border-t border-neutral-200 dark:border-neutral-800">
                      <td className="py-1 font-medium">{c.symbol}</td>
                      <td className="py-1">{fmtCurrency(c.notional)}</td>
                      <td className="py-1">{fmtPct(c.pct_of_notional, 1)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        )}

        <section className="rounded-xl border border-neutral-200 dark:border-neutral-800 p-4">
          <h2 className="mb-2 text-sm font-medium text-neutral-500">
            Broker/Market-Data Error Rate (last {brokerHealth.lookback_sessions} sessions)
          </h2>
          <div className="flex gap-6 text-sm">
            <span className={brokerHealth.broker_errors > 0 ? "text-red-600" : "text-emerald-600"}>
              {brokerHealth.broker_errors} broker errors
            </span>
            <span className={brokerHealth.market_data_errors > 0 ? "text-amber-600" : "text-emerald-600"}>
              {brokerHealth.market_data_errors} market-data errors
            </span>
          </div>
          {brokerHealth.note && <p className="mt-2 text-xs text-neutral-500">{brokerHealth.note}</p>}
        </section>
      </main>
    </div>
  );
}
