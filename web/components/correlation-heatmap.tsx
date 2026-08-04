import type { CorrelationMatrix } from "@/lib/api";

function cellColor(value: number | null): string {
  if (value === null) return "rgba(115,115,115,0.15)";
  const intensity = Math.min(Math.abs(value), 1);
  return value >= 0 ? `rgba(22,163,74,${0.15 + intensity * 0.7})` : `rgba(220,38,38,${0.15 + intensity * 0.7})`;
}

export function CorrelationHeatmap({ data }: { data: CorrelationMatrix }) {
  return (
    <div className="overflow-x-auto">
      <table className="border-collapse text-xs">
        <thead>
          <tr>
            <th className="p-1"></th>
            {data.symbols.map((s) => (
              <th key={s} className="p-1 font-medium text-neutral-500" style={{ writingMode: "vertical-rl" }}>
                {s}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.symbols.map((rowSymbol, i) => (
            <tr key={rowSymbol}>
              <td className="p-1 pr-2 text-right font-medium text-neutral-500">{rowSymbol}</td>
              {data.symbols.map((colSymbol, j) => {
                const value = data.matrix[i][j];
                return (
                  <td
                    key={colSymbol}
                    title={`${rowSymbol} × ${colSymbol}: ${value === null ? "n/a" : value.toFixed(2)}`}
                    className="h-8 w-8 text-center"
                    style={{ backgroundColor: cellColor(value) }}
                  >
                    {value === null ? "" : value.toFixed(1)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
