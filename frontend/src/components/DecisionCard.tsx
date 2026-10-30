import { useState } from "react";
import type { RunDetail } from "../api";
import { isLive } from "../stages";
import { CopyIcon } from "./Icons";
import { Skeleton } from "./Common";
import { DecisionBadge } from "./Pills";

export function DecisionCard({ run }: { run: RunDetail }) {
  const d = run.decision;
  const [copied, setCopied] = useState(false);
  if (!d) return null;
  const approved = d.decision === "Approve";
  const waiting = d.summary === null && isLive(run);

  async function copy() {
    if (!d?.drafted_message) return;
    try {
      await navigator.clipboard.writeText(d.drafted_message);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch { /* clipboard unavailable: the text stays selectable */ }
  }

  return (
    <section className="card !p-6" aria-label="Decision">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <DecisionBadge decision={d.decision} />
        {d.top_code ? <p className="text-[14px] text-body">Triggered by <span className="font-mono text-[13px] font-semibold text-ink">{d.top_code}</span></p>
          : approved && <p className="text-[14px] text-body">All {d.checks_total} checks passed</p>}
        {d.routed_to && <p className="text-[14px] text-body">Routed to <strong className="font-semibold text-ink">{d.routed_to}</strong></p>}
      </div>
      <div className="mt-5 grid grid-cols-2 gap-8">
        <div>
          <h3 className="label">Summary</h3>
          {waiting ? <Skeleton className="mt-3 h-20 w-full" /> : <p className="mt-2 text-[15px] leading-[1.6] text-ink">{d.summary}</p>}
        </div>
        <div>
          {approved ? (
            <><h3 className="label">Next step</h3><p className="mt-2 text-[15px] leading-[1.6] text-ink">No action needed</p></>
          ) : (
            <>
              <div className="flex items-center justify-between">
                <h3 className="label">Drafted message{d.routed_to ? ` to ${d.routed_to}` : ""}</h3>
                {d.drafted_message && <button type="button" onClick={copy} className="btn-secondary !px-2.5 !py-1 text-[12px]"><CopyIcon width={14} height={14} className="mr-1.5" />{copied ? "Copied" : "Copy"}</button>}
              </div>
              {waiting ? <Skeleton className="mt-3 h-20 w-full" /> : (
                <div className="mt-2 whitespace-pre-wrap rounded-lg bg-page p-3 text-[13px] leading-relaxed text-body">{d.drafted_message ?? "No message was drafted."}</div>
              )}
              {d.required_action && <p className="mt-2 text-[12px] text-muted">Expected action: {d.required_action}</p>}
            </>
          )}
        </div>
      </div>
      <p className="mt-6 border-t border-divider pt-3 text-[12px] text-muted">
        The decision comes from deterministic rules. AI was used only to read the invoice and write this explanation.
      </p>
    </section>
  );
}
