"use server";

import { revalidatePath } from "next/cache";

const API_BASE_URL = process.env.AQTP_API_BASE_URL ?? "http://127.0.0.1:8000";

async function postStrategyAction(name: string, action: "pause" | "resume"): Promise<void> {
  const url = new URL(`/api/strategies/${encodeURIComponent(name)}/${action}`, API_BASE_URL);
  const res = await fetch(url, { method: "POST" });
  if (!res.ok) {
    throw new Error(`POST ${url} failed: ${res.status} ${await res.text()}`);
  }
  revalidatePath("/strategies");
  revalidatePath(`/strategies/${name}`);
}

export async function pauseStrategyAction(name: string): Promise<void> {
  await postStrategyAction(name, "pause");
}

export async function resumeStrategyAction(name: string): Promise<void> {
  await postStrategyAction(name, "resume");
}

export async function addWatchlistItemAction(symbol: string): Promise<void> {
  const url = new URL("/api/watchlist", API_BASE_URL);
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbol }),
  });
  if (!res.ok) {
    throw new Error(`POST ${url} failed: ${res.status} ${await res.text()}`);
  }
  revalidatePath("/market");
}

export async function removeWatchlistItemAction(id: number): Promise<void> {
  const url = new URL(`/api/watchlist/${id}`, API_BASE_URL);
  const res = await fetch(url, { method: "DELETE" });
  if (!res.ok) {
    throw new Error(`DELETE ${url} failed: ${res.status} ${await res.text()}`);
  }
  revalidatePath("/market");
}
