import type { Check, RunDetail } from "../api";
import { inr, pct } from "../format";
import { stageOf } from "../stages";
import { ErrorBox, Skeleton } from "./Common";
import { ResultPill } from "./Pills";

type Line = { label: string; value: string; red?: boolean };

const MONEY = new Set(["po_subtotal", "approved_to_date", "invoice_subtotal", "total_after", "limit", "matched_po_lines_value", "its_total", "this_total", "total", "lines_plus_charges", "subtotal", "tax"]);

/** The numbers behind a check, as label/value lines. CUMULATIVE_PO gets the full breakdown from the brief. */
function breakdown(c: Check): Line[] {
  const e = c.evidence;
  if (c.code === "CUMULATIVE_PO" && e.po_subtotal !== undefined) {
    const over = Number(e.total_after) - Number(e.limit);
    const earlier = (e.approved_invoices ?? []).map((i: { invoice_number: string }) => i.invoice_number).join(", ");
    return [
      { label: "PO subtotal", value: inr(e.po_subtotal) },
      { label: "Approved to date", value: inr(e.approved_to_date) + (earlier ? `  (${earlier})` : "") },
      { label: "This invoice (excl. GST)", value: inr(e.invoice_subtotal) },
      { label: "Cumulative total", value: `${inr(e.total_after)} = ${pct(e.utilisation_pct)}%`, red: over > 0 },
      { label: `Limit (${pct((Number(e.limit) / Number(e.po_subtotal)) * 100)}% of PO)`, value: inr(e.limit) },
      ...(over > 0 ? [{ label: "Over the limit by", value: inr(over), red: true }] : []),
    ];
  }
  if (c.code === "LINE_MATCH" && Array.isArray(e.issues)) return e.issues.map((t: string, i: number) => ({ label: `Issue ${i + 1}`, value: t }));
  return Object.entries(e)
    .filter(([, v]) => v === null || ["string", "number", "boolean"].includes(typeof v))
    .map(([k, v]) => ({ label: k.replace(/_/g, " "), value: MONEY.has(k) && v !== null ? inr(v as string) : v === null ? "—" : String(v) }));
}

function CheckBox({ check }: { check: Check }) {
  const fail = check.result === "FAIL";
  const lines = breakdown(check);
  return (
    <div className={`rounded-xl border p-4 ${fail ? "border-bad-line bg-bad-bg" : "border-[#FEC84B] bg-warn-bg"}`}>
      <div className="flex items-center gap-2">
        <span className={`font-mono text-[12px] font-semibold ${fail ? "text-[#912018]" : "text-warn"}`}>{check.code}</span>
        <ResultPill result={check.result} />
      </div>
      <p className="mt-2 text-[14px] text-ink">{check.message}</p>
      {lines.length > 0 && (
        <dl className={`mt-3 grid grid-cols-[auto_1fr] gap-x-6 gap-y-1 rounded-lg border bg-white p-3 font-mono text-[12.5px] ${fail ? "border-[#FECDCA]" : "border-[#FEDF89]"}`}>
          {lines.map((l) => (
            <div key={l.label} className="contents">
              <dt className="text-muted">{l.label}</dt>
              <dd className={`break-words ${l.red ? "font-semibold text-bad" : "text-ink"}`}>{l.value}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}

export function ChecksPanel({ run, which }: { run: RunDetail; which: "validate" | "rules" }) {
  const stage = stageOf(run, which);
  const checks = run.checks.filter((c) => (which === "validate" ? c.stage === "validate" : c.stage !== "validate"));
  // The decision can be triggered by a Validate check (e.g. REQUIRED_FIELDS). Show those on the Rules panel too,
  // so it never says "0 fail" while the decision card names a failing check.
  const fromValidate = which === "rules" ? run.checks.filter((c) => c.stage === "validate" && c.result !== "PASS") : [];
  const fails = checks.filter((c) => c.result === "FAIL");
  const warns = checks.filter((c) => c.result === "WARN");
  const passes = checks.filter((c) => c.result === "PASS");
  const title = which === "validate" ? "Validation" : "Rules";
  return (
    <section className="card" aria-label={title}>
      <h2 className="section-title">{title}</h2>
      {stage?.status === "failed" && <div className="mt-3"><ErrorBox>{title} failed: {stage.error}</ErrorBox></div>}
      {stage?.status === "skipped" && <p className="mt-2 text-[13px] text-muted">Skipped because an earlier stage failed.</p>}
      {checks.length === 0 && (stage?.status === "pending" || stage?.status === "running") && <div className="mt-4 space-y-2"><Skeleton className="h-10 w-full" /><Skeleton className="h-10 w-full" /></div>}
      {(checks.length > 0 || fromValidate.length > 0) && (
        <>
          <p className="mt-0.5 text-[13px] text-muted">
            {checks.length} check{checks.length === 1 ? "" : "s"} · {passes.length} pass{warns.length ? ` · ${warns.length} warn` : ""}{fails.length ? ` · ${fails.length} fail` : ""}
            {fromValidate.length > 0 && ` · from Validate: ${fromValidate.filter((c) => c.result === "FAIL").length} fail, ${fromValidate.filter((c) => c.result === "WARN").length} warn`} · no LLM involved
          </p>
          {fromValidate.length > 0 && (
            <div className="mt-4">
              <p className="label mb-2">From Validate</p>
              <div className="space-y-3">{[...fromValidate.filter((c) => c.result === "FAIL"), ...fromValidate.filter((c) => c.result === "WARN")].map((c) => <CheckBox key={`v-${c.code}`} check={c} />)}</div>
            </div>
          )}
          <div className="mt-4 space-y-3">{[...fails, ...warns].map((c) => <CheckBox key={c.code} check={c} />)}</div>
          {passes.length > 0 && (
            <ul className="mt-4 divide-y divide-divider">
              {passes.map((c) => (
                <li key={c.code} className="flex items-baseline gap-3 py-2">
                  <ResultPill result="PASS" />
                  <span className="w-[170px] shrink-0 font-mono text-[12px] text-ink">{c.code}</span>
                  <span className="text-[13px] text-body">{c.message}</span>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
