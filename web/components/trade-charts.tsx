"use client";

import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from "recharts";
import type { TradeHistory } from "@/lib/api";

const GOOD = "#16a34a";
const BAD = "#dc2626";

export function PnlDistributionChart({ data }: { data: TradeHistory["pnl_distribution"] }) {
  const chartData = data.map((bucket) => ({
    label: `${bucket.range_start.toFixed(0)}`,
    count: bucket.count,
    midpoint: (bucket.range_start + bucket.range_end) / 2,
  }));

  return (
    <ResponsiveContainer width="100%" height={200}>
      <BarChart data={chartData} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
        <XAxis dataKey="label" tick={{ fontSize: 11 }} />
        <YAxis allowDecimals={false} tick={{ fontSize: 11 }} />
        <Tooltip formatter={(value: number) => [value, "Trades"]} labelFormatter={(l) => `P&L ~ ₹${l}`} />
        <Bar dataKey="count" radius={[4, 4, 0, 0]}>
          {chartData.map((entry, i) => (
            <Cell key={i} fill={entry.midpoint >= 0 ? GOOD : BAD} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

export function DayOfWeekChart({ data }: { data: TradeHistory["day_of_week"] }) {
  return (
    <ResponsiveContainer width="100%" height={200}>
      <BarChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
        <XAxis dataKey="day" tick={{ fontSize: 11 }} />
        <YAxis tick={{ fontSize: 11 }} />
        <Tooltip
          formatter={(value: number, key: string) => [
            key === "total_pnl" ? `₹${value.toFixed(2)}` : value,
            key === "total_pnl" ? "Total P&L" : key,
          ]}
        />
        <Bar dataKey="total_pnl" radius={[4, 4, 0, 0]}>
          {data.map((entry, i) => (
            <Cell key={i} fill={entry.total_pnl >= 0 ? GOOD : BAD} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

export function CalendarHeatmapStrip({ data }: { data: TradeHistory["calendar_heatmap"] }) {
  if (data.length === 0) {
    return <div className="text-sm text-neutral-500">No trades yet.</div>;
  }
  const maxAbs = Math.max(...data.map((d) => Math.abs(d.pnl)), 1);

  return (
    <div className="flex flex-wrap gap-2">
      {data.map((day) => {
        const intensity = Math.min(Math.abs(day.pnl) / maxAbs, 1);
        const color = day.pnl >= 0 ? GOOD : BAD;
        return (
          <div
            key={day.date}
            title={`${day.date}: ₹${day.pnl.toFixed(2)}`}
            className="flex h-14 w-20 flex-col items-center justify-center rounded-lg text-xs font-medium text-white"
            style={{ backgroundColor: color, opacity: 0.3 + intensity * 0.7 }}
          >
            <span>{day.date.slice(5)}</span>
            <span>₹{day.pnl.toFixed(0)}</span>
          </div>
        );
      })}
    </div>
  );
}
