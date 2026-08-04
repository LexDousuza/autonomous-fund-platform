"use client";

import { AreaChart, Area, Line, ComposedChart, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";
import type { Forecast } from "@/lib/api";

export function ForecastFanChart({ forecast }: { forecast: Forecast }) {
  const historyLen = forecast.historical_closes.length;
  const data = [];

  for (let i = 0; i < historyLen; i++) {
    data.push({ x: i - historyLen, historical: forecast.historical_closes[i] });
  }
  const p10 = forecast.percentile_paths["10"];
  const p50 = forecast.percentile_paths["50"];
  const p90 = forecast.percentile_paths["90"];
  for (let i = 0; i <= forecast.horizon_days; i++) {
    data.push({ x: i, p10: p10?.[i], median: p50?.[i], p90: p90?.[i], band: p90 && p10 ? [p10[i], p90[i]] : undefined });
  }

  return (
    <ResponsiveContainer width="100%" height={260}>
      <ComposedChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
        <XAxis dataKey="x" tick={{ fontSize: 11 }} label={{ value: "days from today", position: "insideBottom", offset: -4, fontSize: 10 }} />
        <YAxis domain={["auto", "auto"]} tick={{ fontSize: 11 }} />
        <Tooltip />
        <Line type="monotone" dataKey="historical" stroke="#525252" dot={false} strokeWidth={1.5} name="History" />
        <Area type="monotone" dataKey="band" stroke="none" fill="#2563eb" fillOpacity={0.15} name="10-90th pct" />
        <Line type="monotone" dataKey="median" stroke="#2563eb" strokeDasharray="4 2" dot={false} strokeWidth={2} name="Median forecast" />
      </ComposedChart>
    </ResponsiveContainer>
  );
}
