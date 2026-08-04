"use client";

import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";

type Point = { timestamp: string; pnl: number };

export function EquityChart({ data }: { data: Point[] }) {
  if (data.length === 0) {
    return (
      <div className="flex h-64 items-center justify-center text-sm text-neutral-500">
        No closed trades yet for this session.
      </div>
    );
  }

  return (
    <ResponsiveContainer width="100%" height={256}>
      <LineChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
        <XAxis
          dataKey="timestamp"
          tickFormatter={(v: string) => new Date(v).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
          tick={{ fontSize: 12 }}
        />
        <YAxis tick={{ fontSize: 12 }} />
        <Tooltip
          labelFormatter={(v: string) => new Date(v).toLocaleString()}
          formatter={(value: number) => [`₹${value.toFixed(2)}`, "Cumulative P&L"]}
        />
        <Line
          type="stepAfter"
          dataKey="pnl"
          stroke={data[data.length - 1].pnl >= 0 ? "#16a34a" : "#dc2626"}
          dot={{ r: 3 }}
          strokeWidth={2}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}
