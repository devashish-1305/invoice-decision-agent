import { Link } from "react-router-dom";
import type { RunDetail } from "../api";
import { inr } from "../format";
import { isLive } from "../stages";
import { DecisionPill, Pill } from "./Pills";
import { Skeleton } from "./Common";

export function RunHeader({ run }: { run: RunDetail }) {
  const h = run.header;
  const decision = run.decision;
  return (
    <header className="card flex items-start justify-between gap-6">
      <div className="min-w-0">
        <p className="text-[12px] text-muted">Run #{run.id} · <Link to="/" className="text-accent-text hover:underline">New run</Link></p>
        <h1 className="mt-1 text-[20px] font-semibold leading-tight text-ink">
          {h.vendor_name ?? (isLive(run) ? <Skeleton className="inline-block h-5 w-64 align-middle" /> : "Vendor not read")}
        </h1>
        <p className="mt-1 font-mono text-[12px] text-muted">{h.vendor_gstin ? `GSTIN ${h.vendor_gstin}` : isLive(run) ? "" : "GSTIN not read"}</p>
      </div>
      <div className="flex shrink-0 items-start gap-8">
        <div>
          <p className="label">Invoice</p>
          <p className="mt-1 font-mono text-[15px] text-ink">{h.invoice_number ?? "—"}</p>
        </div>
        <div>
          <p className="label">Total (incl. GST)</p>
          <p className="mt-1 font-mono text-[15px] text-ink">{inr(h.total)}</p>
        </div>
        <div className="pt-0.5">
          {decision && !isLive(run) ? <DecisionPill decision={decision.decision} risk={decision.risk_level} long /> : <Pill tone="accent">Running…</Pill>}
        </div>
      </div>
    </header>
  );
}
