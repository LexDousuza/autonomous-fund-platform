export function ForecastSymbolForm({ defaultSymbol }: { defaultSymbol: string }) {
  return (
    <form className="flex gap-2">
      <input
        name="symbol"
        defaultValue={defaultSymbol}
        placeholder="Symbol"
        className="w-32 rounded-lg border border-neutral-300 dark:border-neutral-700 bg-transparent px-2 py-1 text-sm"
      />
      <button
        type="submit"
        className="rounded-lg bg-neutral-900 px-3 py-1 text-sm text-white dark:bg-neutral-100 dark:text-neutral-900"
      >
        Forecast
      </button>
    </form>
  );
}
