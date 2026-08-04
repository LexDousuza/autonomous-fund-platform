"use client";

import { LineChart, Line, ResponsiveContainer } from "recharts";

export function Sparkline({ values }: { values: number[] }) {
  if (values.length < 2) {
    return <span className="text-xs text-neutral-500">no data</span>;
  }
  const data = values.map((v, i) => ({ i, v }));
  const trendingUp = values[values.length - 1] >= values[0];

  return (
    <div className="h-8 w-24">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data}>
          <Line
            type="monotone"
            dataKey="v"
            stroke={trendingUp ? "#16a34a" : "#dc2626"}
            dot={false}
            strokeWidth={1.5}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
