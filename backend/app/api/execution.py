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


INTERRUPTED = "Processing was interrupted before it finished."


def record_crash(state: AppState, run_id: int, error: Exception) -> None:
    text = f"{type(error).__name__}: {error}"
    record_failure(state, run_id, text, f"The run crashed and was routed to manual processing. ({text})")


def record_failure(state: AppState, run_id: int, error_text: str, check_message: str) -> None:
    """Close a run that can't finish: a failed stage with the error, a STAGE_FAILED check and (if the run never
    reached a decision) a fail-safe Hold. A decision that was already committed is left exactly as it is: it is
    tied to the approved-invoice ledger, which must stay consistent. The run is marked failed either way, so it can
    never look like it completed successfully."""
    with state.session_factory() as session:
        last_status: dict[str, str] = {}
        for event in repo.get_events(session, run_id):
            last_status[event.stage] = event.status
        for stage, status in last_status.items():       # a stage that started but never finished did not complete
            if status == "started":
                repo.add_stage_event(session, run_id, stage, "failed", error=error_text)
        repo.add_stage_event(session, run_id, "pipeline", "failed", error=error_text)
        check = Check(code="STAGE_FAILED", result="FAIL", action="hold", risk="medium", stage="pipeline",
                      message=check_message)
        repo.save_checks(session, run_id, [check])
        row = repo.get_decision(session, run_id)
        if row is None:
            decision, routing = engine.decide([check]), engine.route([check])
            repo.save_decision(session, run_id, decision, routing)
            repo.update_run(session, run_id, decision=decision.decision, risk_level=decision.risk_level,
                            top_reason=decision.top_reason)
            repo.save_explanation(session, run_id, decide_stage.fallback_summary(decision), None)
        elif row.summary is None:
            decided = Decision(decision=row.decision, risk_level=row.risk_level, triggered_codes=[],
                               top_reason=row.top_reason)
            repo.save_explanation(session, run_id, decide_stage.fallback_summary(decided), None)
        repo.update_run(session, run_id, status="failed")
        session.commit()


def recover_interrupted_runs(state: AppState) -> int:
    """Called once at startup. A run still queued/running now belongs to a process that no longer exists (its
    background task died with it), so it is closed as failed with a Hold. Assumes one server process."""
    with state.read_session_factory() as session:
        stuck = [r.id for r in session.scalars(select(m.Run).where(m.Run.status.in_(("queued", "running"))))]
    for run_id in stuck:
        record_failure(state, run_id, INTERRUPTED, f"{INTERRUPTED} The invoice was held for manual processing.")
    return len(stuck)
