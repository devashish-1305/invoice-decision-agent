import type { RunDetail } from "../api";
import { bytes, sentence } from "../format";
import { stageOf } from "../stages";
import { ErrorBox } from "./Common";
import { DecisionPill, Pill } from "./Pills";

export function IngestPanel({ run }: { run: RunDetail }) {
  const stage = stageOf(run, "ingest");
  const out = stage?.output;
  return (
    <section className="card" aria-label="Ingest">
      <h2 className="section-title">Ingest</h2>
      <p className="mt-0.5 text-[13px] text-muted">Detect whether the PDF has a text layer. No AI.</p>
      {stage?.status === "failed" && <div className="mt-3"><ErrorBox>Ingest failed: {stage.error}</ErrorBox></div>}
      {out && (
        <dl className="mt-4 grid grid-cols-[150px_1fr] gap-y-2 text-[13px]">
          <dt className="text-muted">File</dt><dd className="font-mono text-ink">{run.filename}</dd>
          <dt className="text-muted">Document type</dt><dd className="text-ink">{out.doc_type === "text" ? "Text PDF (text layer found)" : "Scanned image (no text layer; pages rendered for AI vision)"}</dd>
          <dt className="text-muted">Pages</dt><dd className="font-mono text-ink">{out.page_count}</dd>
          <dt className="text-muted">File size</dt><dd className="font-mono text-ink">{bytes(run.file_size_bytes)}</dd>
          <dt className="text-muted">Text characters</dt><dd className="font-mono text-ink">{out.text_chars}</dd>
        </dl>
      )}
    </section>
  );
}

export function DecidePanel({ run }: { run: RunDetail }) {
  const decide = stageOf(run, "decide");
  const explain = stageOf(run, "explain");
  const d = run.decision;
  return (
    <section className="card" aria-label="Decide and explain">
      <h2 className="section-title">Decide &amp; explain</h2>
      <p className="mt-0.5 text-[13px] text-muted">The decision comes from the checks. The AI only writes the explanation afterwards.</p>
      {decide?.status === "failed" && <div className="mt-3"><ErrorBox>The decision step failed: {sentence(decide.error)} The invoice was held for manual processing; nothing was approved.</ErrorBox></div>}
      {explain?.status === "failed" && <div className="mt-3"><ErrorBox>The explanation could not be written: {sentence(explain.error)} The decision above is unaffected.</ErrorBox></div>}
      {d && (
        <dl className="mt-4 grid grid-cols-[150px_1fr] items-center gap-y-2 text-[13px]">
          <dt className="text-muted">Decision</dt><dd><DecisionPill decision={d.decision} risk={d.risk_level} long /></dd>
          <dt className="text-muted">Risk level</dt><dd className="text-ink">{d.risk_level}</dd>
          <dt className="text-muted">Checks run</dt><dd className="font-mono text-ink">{d.checks_total}</dd>
          <dt className="text-muted">Approved ledger</dt>
          <dd>{d.ledger_inserted ? <Pill tone="ok">Recorded as approved: PO balance consumed</Pill> : <Pill tone="neutral">Not recorded: no PO balance consumed</Pill>}</dd>
        </dl>
      )}
    </section>
  );
}
