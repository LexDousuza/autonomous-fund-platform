import { getMarketOverview, getCorrelation } from "@/lib/api";
import { IndexChart } from "@/components/index-chart";
import { CorrelationHeatmap } from "@/components/correlation-heatmap";

export default async function MarketPage() {
  const overview = await getMarketOverview();

  const moverSymbols = [
    ...overview.top_gainers.slice(0, 6).map((m) => m.symbol),
    ...overview.top_losers.slice(0, 6).map((m) => m.symbol),
  ];
  const correlation = moverSymbols.length ? await getCorrelation(moverSymbols) : null;

  return (
    <div className="p-8">
      <main className="mx-auto max-w-5xl space-y-6">
        <h1 className="text-xl font-semibold">Market Overview</h1>

        <div className="grid gap-4 sm:grid-cols-2">
          {Object.entries(overview.indices).map(([symbol, index]) => (
            <section key={symbol} className="rounded-xl border border-neutral-200 dark:border-neutral-800 p-4">
              <h2 className="mb-2 text-sm font-medium text-neutral-500">{symbol}</h2>
              {index.available && index.series ? (
                <IndexChart data={index.series} />
              ) : (
                <div className="flex h-32 items-center justify-center text-sm text-neutral-500">
                  {index.reason ?? "unavailable"}
                </div>
              )}
            </section>
          ))}
        </div>

        <section className="rounded-xl border border-neutral-200 dark:border-neutral-800 p-4">
          <h2 className="mb-3 text-sm font-medium text-neutral-500">
            Market Breadth ({overview.universe_size} symbols)
          </h2>
          <div className="flex gap-6 text-sm">
            <span className="text-emerald-600">▲ {overview.breadth.advancing} advancing</span>
            <span className="text-red-600">▼ {overview.breadth.declining} declining</span>
            <span className="text-neutral-500">{overview.breadth.unchanged} unchanged</span>
          </div>
        </section>

        <div className="grid gap-4 sm:grid-cols-2">
          <section className="rounded-xl border border-neutral-200 dark:border-neutral-800 p-4">
            <h2 className="mb-2 text-sm font-medium text-neutral-500">Top Gainers</h2>
            <ul className="space-y-1 text-sm">
              {overview.top_gainers.map((m) => (
                <li key={m.symbol} className="flex justify-between">
                  <span>{m.symbol}</span>
                  <span className="text-emerald-600">+{m.change_pct.toFixed(2)}%</span>
                </li>
              ))}
            </ul>
          </section>
          <section className="rounded-xl border border-neutral-200 dark:border-neutral-800 p-4">
            <h2 className="mb-2 text-sm font-medium text-neutral-500">Top Losers</h2>
            <ul className="space-y-1 text-sm">
              {overview.top_losers.map((m) => (
                <li key={m.symbol} className="flex justify-between">
                  <span>{m.symbol}</span>
                  <span className="text-red-600">{m.change_pct.toFixed(2)}%</span>
                </li>
              ))}
            </ul>
          </section>
        </div>

        {correlation && (
          <section className="rounded-xl border border-neutral-200 dark:border-neutral-800 p-4">
            <h2 className="mb-3 text-sm font-medium text-neutral-500">
              Correlation - today&apos;s top movers
            </h2>
            <CorrelationHeatmap data={correlation} />
          </section>
        )}
      </main>
    </div>
  );
}
