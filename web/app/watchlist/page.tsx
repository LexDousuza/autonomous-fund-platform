import { getWatchlist } from "@/lib/api";
import { AddSymbolForm, RemoveButton } from "@/components/watchlist-controls";
import { Sparkline } from "@/components/sparkline";

export default async function WatchlistPage() {
  const items = await getWatchlist();

  return (
    <div className="p-8">
      <main className="mx-auto max-w-3xl space-y-4">
        <div className="flex items-center justify-between">
          <h1 className="text-xl font-semibold">Watchlist</h1>
          <AddSymbolForm />
        </div>

        {items.length === 0 ? (
          <p className="text-sm text-neutral-500">No symbols yet - add one above.</p>
        ) : (
          <div className="overflow-x-auto rounded-xl border border-neutral-200 dark:border-neutral-800">
            <table className="w-full text-sm">
              <thead className="bg-neutral-100 dark:bg-neutral-900 text-left text-xs uppercase text-neutral-500">
                <tr>
                  <th className="px-4 py-2">Symbol</th>
                  <th className="px-4 py-2">Last Close</th>
                  <th className="px-4 py-2">20-day Trend</th>
                  <th className="px-4 py-2"></th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.id} className="border-t border-neutral-200 dark:border-neutral-800">
                    <td className="px-4 py-2 font-medium">
                      {item.symbol} <span className="text-neutral-500">({item.exchange})</span>
                    </td>
                    <td className="px-4 py-2">{item.last_close?.toFixed(2) ?? "no data"}</td>
                    <td className="px-4 py-2">
                      <Sparkline values={item.sparkline} />
                    </td>
                    <td className="px-4 py-2">
                      <RemoveButton id={item.id} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </main>
    </div>
  );
}
