import type { RunDetail, StageStatus } from "../api";
import { ms } from "../format";
import { CheckIcon } from "./Icons";
import { ROWS, rowDuration, rowStatus, stageOf, type RowKey } from "../stages";

function StatusIcon({ status }: { status: StageStatus }) {
  const box = "flex h-[22px] w-[22px] shrink-0 items-center justify-center rounded-full";
  if (status === "completed") return <span className={`${box} bg-ok-bg text-ok`}><CheckIcon width={12} height={12} strokeWidth={2.6} /></span>;
  if (status === "running") return <span className={`${box} bg-accent-bg`}><span className="pulse-dot h-2 w-2 rounded-full bg-accent" /></span>;
  if (status === "failed") return <span className={`${box} bg-[#FEE4E2] text-[12px] font-bold text-bad`}>!</span>;
  return <span className={`${box} border-[1.5px] border-[#D0D5DD]`} />;
}

export function PipelinePanel(props: { run: RunDetail; selected: RowKey; onSelect: (k: RowKey) => void }) {
  const { run } = props;
  return (
    <section className="card w-[250px] shrink-0 !p-3" aria-label="Pipeline">
      <div className="flex items-baseline justify-between px-2 pb-2 pt-1">
        <h2 className="section-title">Pipeline</h2>
        <span className="font-mono text-[12px] text-muted">{run.total_duration_ms ? ms(run.total_duration_ms) : ""}</span>
      </div>
      <ul className="space-y-1">
        {ROWS.map((row) => {
          const status = rowStatus(run, row.key);
          const selected = props.selected === row.key;
          const decide = stageOf(run, "decide");
          const explain = stageOf(run, "explain");
          const summary = row.key === "decide" ? null : stageOf(run, row.key)?.summary;
          const cls = status === "failed" ? "bg-bad-bg outline outline-1 outline-bad-line" : selected ? "bg-accent-bg" : "hover:bg-page";
          return (
            <li key={row.key}>
              <button type="button" onClick={() => props.onSelect(row.key)} aria-current={selected} className={`flex w-full items-start gap-2.5 rounded-lg p-2.5 text-left ${cls}`}>
                <StatusIcon status={status} />
                <span className="min-w-0 flex-1">
                  <span className="flex items-baseline justify-between gap-2">
                    <span className="text-[13px] font-medium text-ink">{row.label}</span>
                    <span className="font-mono text-[11px] text-muted">{ms(rowDuration(run, row.key))}</span>
                  </span>
                  {row.key === "decide" ? (
                    <>
                      {decide?.summary && <span className="mt-0.5 block text-[12px] leading-snug text-muted">Decision · {decide.summary}</span>}
                      {explain?.summary && <span className="mt-0.5 block text-[12px] leading-snug text-muted">Explanation · {explain.summary}</span>}
                    </>
                  ) : summary ? (
                    <span className="mt-0.5 block text-[12px] leading-snug text-muted">{summary}</span>
                  ) : status === "running" ? (
                    <span className="mt-0.5 block text-[12px] text-accent-text">Running…</span>
                  ) : null}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
