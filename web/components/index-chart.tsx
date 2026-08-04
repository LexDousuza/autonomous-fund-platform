"use client";

import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";

export function IndexChart({ data }: { data: [string, number][] }) {
  const chartData = data.map(([date, close]) => ({ date, close }));

  return (
    <ResponsiveContainer width="100%" height={140}>
      <LineChart data={chartData} margin={{ top: 4, right: 8, bottom: 4, left: 0 }}>
        <XAxis dataKey="date" tick={{ fontSize: 10 }} minTickGap={40} />
        <YAxis domain={["auto", "auto"]} tick={{ fontSize: 10 }} width={50} />
        <Tooltip formatter={(v: number) => v.toFixed(2)} />
        <Line type="monotone" dataKey="close" stroke="#2563eb" dot={false} strokeWidth={2} />
      </LineChart>
    </ResponsiveContainer>
  );
}
