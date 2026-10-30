import { useState } from "react";
import { useLocation, useParams } from "react-router-dom";
import { ChecksPanel } from "../components/ChecksPanel";
import { ErrorBox, Skeleton } from "../components/Common";
import { DecisionCard } from "../components/DecisionCard";
import { ExtractPanel } from "../components/ExtractPanel";
import { MatchPanel } from "../components/MatchPanel";
import { DecidePanel, IngestPanel } from "../components/OtherPanels";
import { PipelinePanel } from "../components/PipelinePanel";
import { RunHeader } from "../components/RunHeader";
import { ROWS, autoRow, stageOf, type RowKey } from "../stages";
import { useRun } from "../useRun";

export default function LiveRun() {
  const { id } = useParams();
  const hash = useLocation().hash.slice(1);  // /runs/3#extract opens that stage (a shareable deep link)
  const { run, error } = useRun(id);
  const [chosen, setChosen] = useState<{ id?: string; row: RowKey } | null>(null);

  if (error && !run) return <ErrorBox>{error}</ErrorBox>;
  if (!run) {
    return <div className="space-y-5"><Skeleton className="h-[88px] w-full" /><div className="flex gap-5"><Skeleton className="h-[420px] w-[250px]" /><Skeleton className="h-[420px] flex-1" /></div></div>;
  }

  // The selection follows the running stage until the user clicks a row.
  const linked = ROWS.find((r) => r.key === hash)?.key ?? null;
  const userRow = chosen && chosen.id === id ? chosen.row : linked;
  const selected = userRow ?? autoRow(run);
  const crash = stageOf(run, "pipeline");

  return (
    <div className="space-y-5">
      <RunHeader run={run} />
      {error && <ErrorBox>{error}</ErrorBox>}
      {crash?.status === "failed" && <ErrorBox>The run stopped unexpectedly: {crash.error}. It was held for manual processing and nothing was approved.</ErrorBox>}
      <div className="flex items-start gap-5">
        <PipelinePanel run={run} selected={selected} onSelect={(row) => setChosen({ id, row })} />
        <div className="min-w-0 flex-1">
          {selected === "ingest" && <IngestPanel run={run} />}
          {selected === "extract" && <ExtractPanel run={run} />}
          {selected === "validate" && <ChecksPanel run={run} which="validate" />}
          {selected === "match" && <MatchPanel run={run} />}
          {selected === "rules" && <ChecksPanel run={run} which="rules" />}
          {selected === "decide" && <DecidePanel run={run} />}
        </div>
      </div>
      <DecisionCard run={run} />
    </div>
  );
}
