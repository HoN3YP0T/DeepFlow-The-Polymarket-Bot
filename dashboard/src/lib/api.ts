// API client. All state comes from the DeepFlow backend; the dashboard holds
// no trading logic and never talks to Polymarket directly.

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

export async function fetcher<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}/api${path}`, {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) {
    throw new ApiError(`GET ${path} failed`, res.status);
  }
  return (await res.json()) as T;
}

/**
 * Destructive controls (cancel all, close all, emergency stop) require a typed
 * confirmation string that the backend validates. The UI must not synthesize
 * it -- a mis-click should never be able to close the book.
 */
export async function riskAction(
  action: string,
  confirm: string,
  reason: string,
): Promise<unknown> {
  const res = await fetch(`${BASE}/api/risk/${action}`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm, reason }),
  });
  if (!res.ok) {
    throw new ApiError(`POST risk/${action} failed`, res.status);
  }
  return res.json();
}

/** Live push feed backing the trade, position and whale panels. */
export function openStream(onMessage: (data: unknown) => void): WebSocket {
  const url = BASE.replace(/^http/, "ws") + "/ws";
  const socket = new WebSocket(url);
  socket.onmessage = (event) => onMessage(JSON.parse(event.data));
  return socket;
}
