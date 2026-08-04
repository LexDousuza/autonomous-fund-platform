import { getStrategyDetail } from "@/lib/api";
import { KpiCard } from "@/components/kpi-card";
import { PauseResumeButton } from "@/components/pause-resume-button";

function fmtPct(v: number | null): string {
  return v === null ? "—" : `${(v * 100).toFixed(1)}%`;
}

function fmtNum(v: number | null): string {
  return v === null ? "—" : v.toFixed(2);
}

function fmtCurrency(v: number): string {
  return `₹${v.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

export default async function StrategyDetailPage({
  params,
}: {
  params: Promise<{ name: string }>;
}) {
  const { name } = await params;
  const strategy = await getStrategyDetail(name);
  const funnelTotal = strategy.signal_funnel.generated || 1;

  return (
    <div className="p-8">
      <main className="mx-auto max-w-5xl space-y-6">
        <header className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-semibold">{strategy.name}</h1>
            <p className="text-sm text-neutral-500">
              {strategy.session_count} sessions · last run {strategy.last_status ?? "—"}
            </p>
          </div>
          <PauseResumeButton name={strategy.name} active={strategy.active} />
        </header>

        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <KpiCard label="Trades" value={String(strategy.trade_count)} />
          <KpiCard
            label="Total P&L"
            value={fmtCurrency(strategy.total_pnl)}
            tone={strategy.total_pnl >= 0 ? "good" : "bad"}
          />
          <KpiCard label="Win Rate" value={fmtPct(strategy.win_rate)} />
          <KpiCard label="Profit Factor" value={fmtNum(strategy.profit_factor)} />
          <KpiCard label="Sharpe (per-trade)" value={fmtNum(strategy.sharpe_ratio)} />
          <KpiCard label="Max Drawdown" value={fmtCurrency(strategy.max_drawdown)} tone="bad" />
          <KpiCard label="Avg Win" value={strategy.avg_win === null ? "—" : fmtCurrency(strategy.avg_win)} tone="good" />
          <KpiCard label="Avg Loss" value={strategy.avg_loss === null ? "—" : fmtCurrency(strategy.avg_loss)} tone="bad" />
        </div>

        <section className="rounded-xl border border-neutral-200 dark:border-neutral-800 p-4">
          <h2 className="mb-3 text-sm font-medium text-neutral-500">Signal Funnel</h2>
          <div className="flex items-end gap-6">
            {(["generated", "approved", "rejected"] as const).map((key) => (
              <div key={key} className="flex-1">
                <div className="text-xs uppercase text-neutral-500">{key}</div>
                <div className="text-lg font-semibold">{strategy.signal_funnel[key]}</div>
                <div className="mt-1 h-2 rounded-full bg-neutral-200 dark:bg-neutral-800">
                  <div
                    className="h-2 rounded-full bg-neutral-900 dark:bg-neutral-100"
                    style={{ width: `${(strategy.signal_funnel[key] / funnelTotal) * 100}%` }}
                  />
                </div>
              </div>
            ))}
          </div>
        </section>

        <section className="rounded-xl border border-neutral-200 dark:border-neutral-800 p-4">
          <h2 className="mb-2 text-sm font-medium text-neutral-500">Latest Trades</h2>
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase text-neutral-500">
              <tr>
                <th className="py-1">Symbol</th>
                <th className="py-1">Entry</th>
                <th className="py-1">Exit</th>
                <th className="py-1">Qty</th>
                <th className="py-1">P&L</th>
                <th className="py-1">Exit Time</th>
              </tr>
            </thead>
            <tbody>
              {strategy.latest_trades.map((t, i) => (
                <tr key={i} className="border-t border-neutral-200 dark:border-neutral-800">
                  <td className="py-1">{t.symbol}</td>
                  <td className="py-1">{t.entry_price.toFixed(2)}</td>
                  <td className="py-1">{t.exit_price.toFixed(2)}</td>
                  <td className="py-1">{t.quantity}</td>
                  <td className={`py-1 ${t.pnl >= 0 ? "text-emerald-600" : "text-red-600"}`}>
                    {t.pnl.toFixed(2)}
                  </td>
                  <td className="py-1 text-neutral-500">{new Date(t.exit_timestamp).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="rounded-xl border border-neutral-200 dark:border-neutral-800 p-4">
          <h2 className="mb-2 text-sm font-medium text-neutral-500">Recent Sessions</h2>
          <ul className="space-y-1 text-sm">
            {strategy.recent_sessions.map((s) => (
              <li key={s.session_id} className="flex justify-between text-neutral-600 dark:text-neutral-400">
                <span>{s.session_id}</span>
                <span>{new Date(s.started_at).toLocaleString()}</span>
                <span className="uppercase">{s.status}</span>
              </li>
            ))}
          </ul>
        </section>
      </main>
    </div>
  );
}
