/** The six rows of the pipeline rail. "Decide & explain" is two backend stages shown as one row. */
import type { RunDetail, StageStatus, StageView } from "./api";

export type RowKey = "ingest" | "extract" | "validate" | "match" | "rules" | "decide";

export const ROWS: { key: RowKey; label: string }[] = [
  { key: "ingest", label: "Ingest" },
  { key: "extract", label: "Extract" },
  { key: "validate", label: "Validate" },
  { key: "match", label: "Match" },
  { key: "rules", label: "Rules" },
  { key: "decide", label: "Decide & explain" },
];

export function stageOf(run: RunDetail, name: string): StageView | undefined {
  return run.stages.find((s) => s.stage === name);
}

/** The status shown for a row. For the last row, decide + explain are combined. */
export function rowStatus(run: RunDetail, key: RowKey): StageStatus {
  const names = key === "decide" ? ["decide", "explain"] : [key];
  const statuses = names.map((n) => stageOf(run, n)?.status ?? "pending");
  if (statuses.includes("failed")) return "failed";
  if (statuses.includes("running")) return "running";
  if (statuses.every((s) => s === "completed")) return "completed";
  if (statuses.every((s) => s === "skipped")) return "skipped";
  if (key === "decide" && statuses[0] === "completed") return "running"; // decision made, explanation being written
  return "pending";
}

export function rowDuration(run: RunDetail, key: RowKey): number | null {
  const names = key === "decide" ? ["decide", "explain"] : [key];
  const parts = names.map((n) => stageOf(run, n)?.duration_ms).filter((d): d is number => d !== null && d !== undefined);
  return parts.length ? parts.reduce((a, b) => a + b, 0) : null;
}

export const isLive = (run: RunDetail | null) => !!run && (run.status === "queued" || run.status === "running");

/** Which row to show when the user has not clicked one: the running stage, else the failed one, else Rules. */
export function autoRow(run: RunDetail): RowKey {
  const failed = ROWS.find((r) => rowStatus(run, r.key) === "failed");
  const running = ROWS.find((r) => rowStatus(run, r.key) === "running");
  if (isLive(run)) {
    const next = ROWS.find((r) => rowStatus(run, r.key) !== "completed");
    return (running ?? next ?? ROWS[0]).key;
  }
  return (failed ?? ROWS.find((r) => r.key === "rules")!).key;
}
