"""Runs the pipeline in the background and guarantees nothing is lost.

The run row already exists (status "queued") before this starts. If ANYTHING escapes the pipeline, the
failure is written to the database: a failed stage + the error, a STAGE_FAILED check and a fail-safe Hold.
A crash can never leave a run silently "running" forever, and can never approve.
"""
import logging

from sqlalchemy import select

from app.api.deps import AppState
from app.db import models as m
from app.db import repository as repo
from app.pipeline import decide as decide_stage
from app.pipeline.orchestrator import run_pipeline
from app.rules import engine
from app.schemas import Check, Decision

log = logging.getLogger("invoice-agent")


def execute_run(state: AppState, run_id: int, pdf: bytes, filename: str) -> None:
    try:
        with state.session_factory() as session:
            run_pipeline(session, state.llm_factory(), pdf, filename, state.settings,
                         today=state.today(), run_id=run_id)
    except Exception as e:  # noqa: BLE001 - recorded in the DB, never lost
        log.exception("run %s crashed", run_id)
        record_crash(state, run_id, e)


def record_crash(state: AppState, run_id: int, error: Exception) -> None:
    record_failure(state, run_id, "stopped unexpectedly", technical=f"{type(error).__name__}: {error}")


def record_failure(state: AppState, run_id: int, what: str, technical: str | None = None) -> None:
    """Close a run that can't finish. There are exactly two cases, and the wording says which:

      (a) BEFORE a decision was recorded: the failed stage, a STAGE_FAILED check and a fail-safe Hold.
          "Processing <what> before a decision — the invoice was held."
      (b) AFTER the decision was recorded: the decision, its checks and any ledger row are left exactly as they
          are (they were committed together; the ledger must stay consistent). Only the unfinished post-decision
          stage (the explanation) is marked failed. "...after the decision was recorded — the decision below stands."

    The run is marked failed either way, so it can never look like it completed successfully. `technical` is the
    exception text: kept in the event payload, never shown."""
    with state.session_factory() as session:
        row = repo.get_decision(session, run_id)
        decided = row is not None
        message = (f"Processing {what} after the decision was recorded — the decision below stands." if decided
                   else f"Processing {what} before a decision — the invoice was held.")
        last_status: dict[str, str] = {}
        for event in repo.get_events(session, run_id):
            last_status[event.stage] = event.status
        if decided:
            if last_status.get("explain") != "completed":   # the only stage that comes after the decision
                repo.add_stage_event(session, run_id, "explain", "failed", error=message)
        else:
            for stage, status in last_status.items():       # started but never finished: did not complete
                if status == "started":
                    repo.add_stage_event(session, run_id, stage, "failed", error=message)
        payload = {"phase": "after_decision" if decided else "before_decision"}
        if technical:
            payload["technical_error"] = technical
        repo.add_stage_event(session, run_id, "pipeline", "failed", output=payload, error=message)
        if not decided:
            check = Check(code="STAGE_FAILED", result="FAIL", action="hold", risk="medium", stage="pipeline",
                          message=message)
            repo.save_checks(session, run_id, [check])
            decision, routing = engine.decide([check]), engine.route([check])
            repo.save_decision(session, run_id, decision, routing)
            repo.update_run(session, run_id, decision=decision.decision, risk_level=decision.risk_level,
                            top_reason=decision.top_reason)
            repo.save_explanation(session, run_id, decide_stage.fallback_summary(decision), None)
        elif row.summary is None:
            decided_as = Decision(decision=row.decision, risk_level=row.risk_level, triggered_codes=[],
                                  top_reason=row.top_reason)
            repo.save_explanation(session, run_id, decide_stage.fallback_summary(decided_as), None)
        repo.update_run(session, run_id, status="failed")
        session.commit()


def recover_interrupted_runs(state: AppState) -> int:
    """Called once at startup. A run still queued/running now belongs to a process that no longer exists (its
    background task died with it). Assumes one server process."""
    with state.read_session_factory() as session:
        stuck = [r.id for r in session.scalars(select(m.Run).where(m.Run.status.in_(("queued", "running"))))]
    for run_id in stuck:
        record_failure(state, run_id, "was interrupted")
    return len(stuck)
