/** Types for the API responses, and a tiny fetch wrapper. Money values are strings (never floats). */

export type Decision = "Approve" | "Hold" | "Reject";
export type Risk = "none" | "low" | "medium" | "high";
export type StageName = "ingest" | "extract" | "validate" | "match" | "rules" | "decide" | "explain" | "pipeline";
export type StageStatus = "pending" | "running" | "completed" | "failed" | "skipped";

export interface Sample {
  code: string; title: string; description: string; vendor_id: string; vendor: string;
  po: string | null; subtotal: string; total: string; expected: string; expected_check: string;
}

export interface Field { value: string | null; confidence: number; evidence: string | null; page: number | null }
export interface ExtractedLine { description: string; qty: string | null; unit_price: string | null; amount: string | null; confidence: number }
export interface Extracted {
  vendor_name: Field; vendor_gstin: Field; invoice_number: Field; invoice_date: Field; po_reference: Field;
  bank_account: Field; ifsc: Field; subtotal: Field; total: Field; currency: Field;
  line_items: ExtractedLine[];
  other_charges: { description: string; amount: string | null; confidence: number }[];
  tax_lines: { description: string; rate_pct: string | null; amount: string | null }[];
}

export interface Check {
  stage: string; code: string; result: "PASS" | "WARN" | "FAIL"; action: string | null; risk: string;
  message: string; evidence: Record<string, any>;
}

export interface StageView {
  stage: StageName; status: StageStatus; duration_ms: number | null; summary: string | null;
  error: string | null; output: any;
}

export interface MatchView {
  vendor: { vendor_id: string | null; name: string | null; method: "gstin" | "name" | "none"; score: number | null; candidates: string[]; master_gstin?: string | null };
  po: { po_id: string | null; method: "explicit" | "inferred" | "none"; reference: string | null; confidence: number | null; reason: string | null };
  lines: {
    invoice_line: { description: string; qty: string | null; unit_price: string | null; amount: string | null };
    po_line: { po_line_id: string; description: string; qty: string; unit_price: string } | null;
    method: "exact" | "semantic" | "none"; confidence: number | null; reason: string | null;
  }[];
}

export interface DecisionView {
  decision: Decision; risk_level: Risk; top_reason: string | null; top_code: string | null;
  routed_to: string | null; required_action: string | null; summary: string | null;
  drafted_message: string | null; ledger_inserted: boolean; checks_total: number;
}

export interface RunDetail {
  id: number; filename: string; status: "queued" | "running" | "completed" | "failed";
  doc_type: "text" | "scanned" | null; created_at: string; file_size_bytes: number | null; has_document: boolean;
  interruption: "before_decision" | "after_decision" | null;
  header: { vendor_name: string | null; vendor_gstin: string | null; invoice_number: string | null; invoice_date: string | null; subtotal: string | null; total: string | null; currency: string | null };
  stages: StageView[]; total_duration_ms: number; extracted: Extracted | null; match: MatchView | null;
  checks: Check[]; decision: DecisionView | null;
}

export interface RunRow {
  id: number; created_at: string; filename: string; status: string; doc_type: string | null;
  vendor_name: string | null; invoice_number: string | null; subtotal: string | null; total: string | null;
  decision: Decision | null; risk_level: Risk | null; top_code: string | null; top_reason: string | null;
}
export interface RunList {
  kpis: { processed: number; approved: number; held: number; rejected: number; auto_approved: string; stopped: string };
  runs: RunRow[];
}

export interface Reference {
  vendors: { vendor_id: string; name: string; gstin: string; state: string; bank_account: string; ifsc: string; status: string }[];
  pos: { po_id: string; vendor_id: string; vendor: string; po_date: string; status: string; lines: number; subtotal: string; approved_to_date: string; utilisation_pct: string }[];
}

export class ApiError extends Error {}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url, init);
  } catch {
    throw new ApiError("Cannot reach the server. Is it running?");
  }
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep default */ }
    throw new ApiError(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return response.json();
}

export const api = {
  samples: () => request<Sample[]>("/api/samples"),
  runs: () => request<RunList>("/api/runs"),
  run: (id: number | string) => request<RunDetail>(`/api/runs/${id}`),
  reference: () => request<Reference>("/api/reference"),
  startSample: (code: string) =>
    request<{ run_id: number }>("/api/runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sample: code }) }),
  startUpload: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<{ run_id: number }>("/api/runs", { method: "POST", body: form });
  },
  reset: () => request<{ status: string }>("/api/reset", { method: "POST" }),
};
