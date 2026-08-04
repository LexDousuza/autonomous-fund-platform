"use client";

import { useTransition } from "react";
import { pauseStrategyAction, resumeStrategyAction } from "@/lib/actions";

export function PauseResumeButton({ name, active }: { name: string; active: boolean }) {
  const [isPending, startTransition] = useTransition();

  return (
    <button
      disabled={isPending}
      onClick={() =>
        startTransition(async () => {
          if (active) await pauseStrategyAction(name);
          else await resumeStrategyAction(name);
        })
      }
      className={`rounded-lg px-3 py-1.5 text-xs font-medium disabled:opacity-50 ${
        active
          ? "bg-red-100 text-red-700 hover:bg-red-200 dark:bg-red-950 dark:text-red-300"
          : "bg-emerald-100 text-emerald-700 hover:bg-emerald-200 dark:bg-emerald-950 dark:text-emerald-300"
      }`}
    >
      {isPending ? "…" : active ? "Pause" : "Resume"}
    </button>
  );
}
