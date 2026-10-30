import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type RunDetail } from "./api";
import { isLive } from "./stages";

/**
 * Loads a run and keeps it fresh while it is in progress.
 * The server streams stage events over SSE (replayed from the database, so late or reconnecting clients
 * miss nothing). Each event just triggers a re-read of the run, so the UI always shows server state.
 * A slow poll is a safety net if the stream is blocked by a proxy.
 */
export function useRun(id: string | undefined) {
  const [run, setRun] = useState<RunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!id) return;
    try {
      setRun(await api.run(id));
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not load this run");
    }
  }, [id]);

  useEffect(() => { setRun(null); setError(null); load(); }, [id, load]);

  const live = isLive(run);
  useEffect(() => {
    if (!id || !live) return;
    const source = new EventSource(`/api/runs/${id}/stream`);
    source.addEventListener("stage", () => load());
    source.addEventListener("end", () => { source.close(); load(); });
    const poll = setInterval(load, 3000);
    return () => { source.close(); clearInterval(poll); };
  }, [id, live, load]);

  return { run, error };
}
