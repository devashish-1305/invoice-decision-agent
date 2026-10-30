"""Runs the pipeline in the background and guarantees nothing is lost.

The run row already exists (status "queued") before this starts. If ANYTHING escapes the pipeline, the
failure is written to the database: a failed stage + the error, a STAGE_FAILED check and a fail-safe Hold.
A crash can never leave a run silently "running" forever, and can never approve.
"""
import logging

from app.api.deps import AppState
from app.db import repository as repo
from app.pipeline import decide as decide_stage
from app.pipeline.orchestrator import run_pipeline
from app.rules import engine
from app.schemas import Check

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
    text = f"{type(error).__name__}: {error}"
    with state.session_factory() as session:
        repo.add_stage_event(session, run_id, "pipeline", "failed", error=text)
        check = Check(code="STAGE_FAILED", result="FAIL", action="hold", risk="medium", stage="pipeline",
                      message=f"The run crashed and was routed to manual processing. ({text})")
        repo.save_checks(session, run_id, [check])
        if repo.get_decision(session, run_id) is None:
            decision, routing = engine.decide([check]), engine.route([check])
            repo.save_decision(session, run_id, decision, routing)
            repo.update_run(session, run_id, decision=decision.decision, risk_level=decision.risk_level,
                            top_reason=decision.top_reason)
            repo.save_explanation(session, run_id, decide_stage.fallback_summary(decision), None)
        repo.update_run(session, run_id, status="failed")
        session.commit()
