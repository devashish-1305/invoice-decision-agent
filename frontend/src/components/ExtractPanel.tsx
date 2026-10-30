import type { Extracted, Field, RunDetail } from "../api";
import { bytes, inr } from "../format";
import { stageOf } from "../stages";
import { ErrorBox, Skeleton } from "./Common";
import { ConfidencePill } from "./Pills";

type Row = { label: string; field: Field; kind?: "money" | "text" };

function rows(x: Extracted): Row[] {
  return [
    { label: "Vendor", field: x.vendor_name, kind: "text" }, { label: "GSTIN", field: x.vendor_gstin },
    { label: "Invoice no.", field: x.invoice_number }, { label: "Invoice date", field: x.invoice_date },
    { label: "PO reference", field: x.po_reference }, { label: "Bank account", field: x.bank_account },
    { label: "IFSC", field: x.ifsc }, { label: "Subtotal (excl. GST)", field: x.subtotal, kind: "money" },
    { label: "Total (incl. GST)", field: x.total, kind: "money" }, { label: "Currency", field: x.currency },
  ];
}

export function ExtractPanel({ run }: { run: RunDetail }) {
  const stage = stageOf(run, "extract");
  const x = run.extracted;
  const scanned = run.doc_type === "scanned";
  return (
    <div className="grid grid-cols-[236px_1fr] items-start gap-4">
      <section className="card !p-3" aria-label="Source document">
        <p className="mb-2 flex justify-between gap-2 text-[12px] text-muted"><span className="truncate font-mono">{run.filename}</span><span className="font-mono">p.1</span></p>
        {run.has_document ? (
          <img src={`/api/runs/${run.id}/page.png`} alt={`Page 1 of ${run.filename}`} className="aspect-[1/1.414] w-full rounded-md border border-line object-cover object-top" />
        ) : <Skeleton className="aspect-[1/1.414] w-full" />}
        <p className="mt-2 text-[12px] text-muted">
          {run.doc_type ? (scanned ? "Scanned image · read by AI vision · no OCR library" : "Text PDF · read by AI · no OCR library") : "Reading the file…"}
        </p>
      </section>

      <section className="card" aria-label="AI extraction">
        <h2 className="section-title">AI extraction</h2>
        <p className="mt-0.5 text-[12px] text-muted">Each value shows the exact text it was read from</p>
        {stage?.status === "failed" && <div className="mt-3"><ErrorBox>Extraction failed: {stage.error}</ErrorBox></div>}
        {!x && stage?.status !== "failed" && <div className="mt-4 space-y-3">{[0, 1, 2, 3, 4, 5].map((i) => <Skeleton key={i} className="h-8 w-full" />)}</div>}
        {x && (
          <>
            <dl className="mt-3 divide-y divide-divider">
              {rows(x).map(({ label, field, kind }) => (
                <div key={label} className="grid grid-cols-[130px_1fr_auto] items-start gap-3 py-2">
                  <dt className="text-[13px] text-muted">{label}</dt>
                  <dd className="min-w-0">
                    {field.value === null ? <span className="text-[13px] text-muted">Not on the document</span> : (
                      <span className={`break-words text-[13px] font-medium text-ink ${kind === "text" ? "" : "font-mono"}`}>{kind === "money" ? inr(field.value) : field.value}</span>
                    )}
                    {field.evidence && <p className="mt-0.5 break-words text-[12px] italic text-muted">“{field.evidence}”</p>}
                  </dd>
                  <span>{field.value !== null && <ConfidencePill value={field.confidence} />}</span>
                </div>
              ))}
            </dl>
            <h3 className="label mb-2 mt-4">Line items</h3>
            <table className="w-full text-[12px]">
              <thead><tr className="text-muted"><th className="pb-1 font-medium">Description</th><th className="pb-1 text-right font-medium">Qty</th><th className="pb-1 text-right font-medium">Rate</th><th className="pb-1 text-right font-medium">Amount</th></tr></thead>
              <tbody className="divide-y divide-divider">
                {x.line_items.map((l, i) => (
                  <tr key={i}><td className="py-1.5 pr-2 text-body">{l.description}</td><td className="py-1.5 text-right font-mono">{l.qty ? Number(l.qty) : "—"}</td><td className="py-1.5 text-right font-mono">{inr(l.unit_price)}</td><td className="py-1.5 text-right font-mono">{inr(l.amount)}</td></tr>
                ))}
                {x.other_charges.map((c, i) => (
                  <tr key={`c${i}`}><td className="py-1.5 pr-2 italic text-body">{c.description}</td><td /><td /><td className="py-1.5 text-right font-mono">{inr(c.amount)}</td></tr>
                ))}
              </tbody>
            </table>
            <p className="mt-2 text-[12px] text-muted">File size {bytes(run.file_size_bytes)}</p>
          </>
        )}
      </section>
    </div>
  );
}
