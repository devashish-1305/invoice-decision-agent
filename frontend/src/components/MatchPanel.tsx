import type { RunDetail } from "../api";
import { inr } from "../format";
import { stageOf } from "../stages";
import { ErrorBox, Skeleton } from "./Common";
import { Pill } from "./Pills";

export function MatchPanel({ run }: { run: RunDetail }) {
  const stage = stageOf(run, "match");
  const m = run.match;
  return (
    <section className="card" aria-label="Match">
      <h2 className="section-title">Match</h2>
      <p className="mt-0.5 text-[13px] text-muted">Vendor, purchase order and line items matched against master data</p>
      {stage?.status === "failed" && <div className="mt-3"><ErrorBox>Matching failed: {stage.error}</ErrorBox></div>}
      {stage?.status === "skipped" && <p className="mt-3 text-[13px] text-muted">Skipped because an earlier stage failed.</p>}
      {!m && (stage?.status === "pending" || stage?.status === "running") && <div className="mt-4 space-y-2"><Skeleton className="h-12 w-full" /><Skeleton className="h-12 w-full" /></div>}
      {m && (
        <>
          <div className="mt-4 grid grid-cols-2 gap-4">
            <div className="rounded-lg bg-page p-3">
              <p className="label">Vendor</p>
              <p className="mt-1 text-[14px] font-medium text-ink">{m.vendor.name ?? "No vendor matched"}</p>
              <p className="mt-0.5 text-[12px] text-muted">
                {m.vendor.method === "gstin" && <>Exact GSTIN match <span className="font-mono">{m.vendor.master_gstin}</span></>}
                {m.vendor.method === "name" && <>Name suggestion only ({m.vendor.score}). Not confirmed by GSTIN, so it cannot be approved.</>}
                {m.vendor.method === "none" && "No GSTIN or name match in the vendor master"}
              </p>
            </div>
            <div className="rounded-lg bg-page p-3">
              <p className="label">Purchase order</p>
              <p className="mt-1 flex items-center gap-2 text-[14px] font-medium text-ink">
                <span className="font-mono">{m.po.po_id ?? m.po.reference ?? "None"}</span>
                {m.po.method === "explicit" && <Pill tone="ok">Printed</Pill>}
                {m.po.method === "inferred" && <Pill tone="warn">Inferred by AI · {Math.round((m.po.confidence ?? 0) * 100)}%</Pill>}
                {m.po.method === "none" && <Pill tone="solid-bad">Not found</Pill>}
              </p>
              <p className="mt-0.5 text-[12px] text-muted">
                {m.po.method === "explicit" && "Reference printed on the invoice; looked up directly, no AI"}
                {m.po.method === "inferred" && <em>{m.po.reason}</em>}
                {m.po.method === "none" && (m.po.reference ? `PO ${m.po.reference} does not exist` : "No PO on the invoice and none could be inferred")}
              </p>
            </div>
          </div>
          <h3 className="label mb-2 mt-5">Invoice line ↔ PO line</h3>
          <table className="w-full text-[12px]">
            <thead><tr className="text-muted"><th className="pb-1 font-medium">Invoice line</th><th className="pb-1 font-medium">PO line</th><th className="pb-1 font-medium">Method</th></tr></thead>
            <tbody className="divide-y divide-divider align-top">
              {m.lines.map((l, i) => (
                <tr key={i}>
                  <td className="py-2 pr-3"><span className="block text-body">{l.invoice_line.description}</span><span className="font-mono text-muted">{l.invoice_line.qty ? Number(l.invoice_line.qty) : "—"} × {inr(l.invoice_line.unit_price)}</span></td>
                  <td className="py-2 pr-3">{l.po_line ? <><span className="block text-body">{l.po_line.description}</span><span className="font-mono text-muted">{Number(l.po_line.qty)} × {inr(l.po_line.unit_price)}</span></> : <span className="text-muted">No PO line</span>}</td>
                  <td className="py-2">
                    {l.method === "exact" && <Pill tone="ok">Exact</Pill>}
                    {l.method === "semantic" && <Pill tone="accent">AI by meaning</Pill>}
                    {l.method === "none" && <Pill tone="solid-bad">Unmatched</Pill>}
                    {l.method === "semantic" && <span className="mt-1 block max-w-[200px] text-[11px] italic text-muted">{l.reason}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}
