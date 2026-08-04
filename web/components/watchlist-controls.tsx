"use client";

import { useState, useTransition } from "react";
import { addWatchlistItemAction, removeWatchlistItemAction } from "@/lib/actions";

export function AddSymbolForm() {
  const [symbol, setSymbol] = useState("");
  const [isPending, startTransition] = useTransition();

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (!symbol.trim()) return;
        startTransition(async () => {
          await addWatchlistItemAction(symbol.trim().toUpperCase());
          setSymbol("");
        });
      }}
      className="flex gap-2"
    >
      <input
        value={symbol}
        onChange={(e) => setSymbol(e.target.value)}
        placeholder="Symbol, e.g. RELIANCE"
        className="rounded-lg border border-neutral-300 dark:border-neutral-700 bg-transparent px-3 py-1.5 text-sm"
      />
      <button
        type="submit"
        disabled={isPending}
        className="rounded-lg bg-neutral-900 px-3 py-1.5 text-sm text-white disabled:opacity-50 dark:bg-neutral-100 dark:text-neutral-900"
      >
        {isPending ? "Adding…" : "Add"}
      </button>
    </form>
  );
}

export function RemoveButton({ id }: { id: number }) {
  const [isPending, startTransition] = useTransition();

  return (
    <button
      disabled={isPending}
      onClick={() => startTransition(() => removeWatchlistItemAction(id))}
      className="text-xs text-red-600 hover:underline disabled:opacity-50"
    >
      {isPending ? "…" : "Remove"}
    </button>
  );
}
