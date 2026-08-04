import Link from "next/link";
import { listStrategies } from "@/lib/api";
import { PauseResumeButton } from "@/components/pause-resume-button";

function fmtPct(v: number | null): string {
  return v === null ? "—" : `${(v * 100).toFixed(1)}%`;
}

function fmtNum(v: number | null): string {
  return v === null ? "—" : v.toFixed(2);
}

export default async function StrategiesPage() {
  const strategies = await listStrategies();

  return (
    <div className="p-8">
      <main className="mx-auto max-w-5xl space-y-4">
        <h1 className="text-xl font-semibold">Strategies</h1>

        <div className="overflow-x-auto rounded-xl border border-neutral-200 dark:border-neutral-800">
          <table className="w-full text-sm">
            <thead className="bg-neutral-100 dark:bg-neutral-900 text-left text-xs uppercase text-neutral-500">
              <tr>
                <th className="px-4 py-2">Strategy</th>
                <th className="px-4 py-2">Status</th>
                <th className="px-4 py-2">Sessions</th>
                <th className="px-4 py-2">Trades</th>
                <th className="px-4 py-2">Total P&L</th>
                <th className="px-4 py-2">Win Rate</th>
                <th className="px-4 py-2">Profit Factor</th>
                <th className="px-4 py-2">Sharpe</th>
                <th className="px-4 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {strategies.map((s) => (
                <tr key={s.name} className="border-t border-neutral-200 dark:border-neutral-800">
                  <td className="px-4 py-2">
                    <Link href={`/strategies/${s.name}`} className="font-medium hover:underline">
                      {s.name}
                    </Link>
                  </td>
                  <td className="px-4 py-2">
                    <span
                      className={`rounded-full px-2 py-0.5 text-xs ${
                        s.active
                          ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300"
                          : "bg-neutral-200 text-neutral-600 dark:bg-neutral-800 dark:text-neutral-400"
                      }`}
                    >
                      {s.active ? "active" : "paused"}
                    </span>
                  </td>
                  <td className="px-4 py-2">{s.session_count}</td>
                  <td className="px-4 py-2">{s.trade_count}</td>
                  <td className={`px-4 py-2 ${s.total_pnl >= 0 ? "text-emerald-600" : "text-red-600"}`}>
                    ₹{s.total_pnl.toLocaleString("en-IN", { maximumFractionDigits: 2 })}
                  </td>
                  <td className="px-4 py-2">{fmtPct(s.win_rate)}</td>
                  <td className="px-4 py-2">{fmtNum(s.profit_factor)}</td>
                  <td className="px-4 py-2">{fmtNum(s.sharpe_ratio)}</td>
                  <td className="px-4 py-2">
                    <PauseResumeButton name={s.name} active={s.active} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </main>
    </div>
  );
}
