import { listOrders } from "@/lib/api";

export default async function OrdersPage({
  searchParams,
}: {
  searchParams: Promise<{ session_id?: string; symbol?: string; status?: string }>;
}) {
  const params = await searchParams;
  const orders = await listOrders(params);

  return (
    <div className="p-8">
      <main className="mx-auto max-w-5xl space-y-4">
        <h1 className="text-xl font-semibold">Orders</h1>
        <p className="text-sm text-neutral-500">
          {orders.length} order{orders.length === 1 ? "" : "s"}
          {params.session_id ? ` · session ${params.session_id}` : " · last 50 real sessions"}
        </p>

        <div className="overflow-x-auto rounded-xl border border-neutral-200 dark:border-neutral-800">
          <table className="w-full text-sm">
            <thead className="bg-neutral-100 dark:bg-neutral-900 text-left text-xs uppercase text-neutral-500">
              <tr>
                <th className="px-4 py-2">Symbol</th>
                <th className="px-4 py-2">Side</th>
                <th className="px-4 py-2">Qty</th>
                <th className="px-4 py-2">Status</th>
                <th className="px-4 py-2">Intended</th>
                <th className="px-4 py-2">Filled</th>
                <th className="px-4 py-2">Slippage</th>
                <th className="px-4 py-2">Latency</th>
                <th className="px-4 py-2">Placed At</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((o, i) => (
                <tr key={i} className="border-t border-neutral-200 dark:border-neutral-800">
                  <td className="px-4 py-2 font-medium">{o.symbol}</td>
                  <td className="px-4 py-2">{o.side}</td>
                  <td className="px-4 py-2">{o.quantity}</td>
                  <td className="px-4 py-2">
                    <span
                      className={`rounded-full px-2 py-0.5 text-xs ${
                        o.status === "FILLED"
                          ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300"
                          : o.status === "REJECTED"
                          ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300"
                          : "bg-neutral-200 text-neutral-600 dark:bg-neutral-800 dark:text-neutral-400"
                      }`}
                    >
                      {o.status}
                    </span>
                  </td>
                  <td className="px-4 py-2">{o.intended_price?.toFixed(2) ?? "—"}</td>
                  <td className="px-4 py-2">{o.average_price?.toFixed(2) ?? "—"}</td>
                  <td className="px-4 py-2">{o.slippage === null ? "—" : o.slippage.toFixed(2)}</td>
                  <td className="px-4 py-2">{o.latency_ms.toFixed(0)}ms</td>
                  <td className="px-4 py-2 text-neutral-500">{new Date(o.placed_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </main>
    </div>
  );
}
