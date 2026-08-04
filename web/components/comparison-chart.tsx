"use client";

import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, Legend } from "recharts";

type Point = { date: string; portfolio?: number; benchmark?: number };

export function ComparisonChart({
  portfolioSeries,
  benchmarkSeries,
  benchmarkLabel,
}: {
  portfolioSeries: [string, number][];
  benchmarkSeries: [string, number][];
  benchmarkLabel: string;
}) {
  const byDate = new Map<string, Point>();
  for (const [date, value] of portfolioSeries) {
    byDate.set(date, { date, portfolio: value });
  }
  for (const [date, value] of benchmarkSeries) {
    byDate.set(date, { ...(byDate.get(date) ?? { date }), benchmark: value });
  }
  const data = [...byDate.values()].sort((a, b) => a.date.localeCompare(b.date));

  return (
    <ResponsiveContainer width="100%" height={280}>
      <LineChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
        <XAxis dataKey="date" tick={{ fontSize: 11 }} />
        <YAxis tick={{ fontSize: 11 }} />
        <Tooltip />
        <Legend />
        <Line type="monotone" dataKey="portfolio" name="Portfolio" stroke="#2563eb" dot={false} strokeWidth={2} />
        <Line type="monotone" dataKey="benchmark" name={benchmarkLabel} stroke="#a3a3a3" dot={false} strokeWidth={2} />
      </LineChart>
    </ResponsiveContainer>
  );
}
