"""Runs the pipeline end to end and records every stage.

  ingest -> extract -> validate -> match -> rules -> decide -> explain

(Stage 6 of the PRD, "Decide & explain", is two events: `decide` = the deterministic decision and ledger
insert, `explain` = the AI-written summary and drafted message.)

Each stage writes started / completed / failed (or skipped) events with output and duration to the DB and
to the optional on_event callback (the SSE stream later). A stage that fails is shown as failed, never faked.
Missing data from a failed stage is never guessed: the rules see STAGE_FAILED and the run is a Hold.

The money-critical part is ONE transaction: read the PO balance -> run the rules -> decide -> save checks and
decision -> (Approve only) insert into the approved-invoice ledger -> commit. Writer sessions begin with
BEGIN IMMEDIATE (db/session.py), so the write lock is held before the balance is read and until the commit:
two invoices cannot both consume the same PO headroom.
"""
import time
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from app.config import Settings
from app.db import repository as repo
from app.llm.client import LLMClient
from app.pipeline import decide as decide_stage
from app.pipeline.extract import extract
from app.pipeline.ingest import ingest
from app.pipeline.match import Matched, match
from app.pipeline.validate import validate
from app.rules import engine
from app.schemas import (Check, Decision, Explanation, ExtractedInvoice, Routing, StageFailure)


@dataclass
class RunResult:
    run_id: int
    status: str                      # completed | failed (any stage failed)
    decision: Decision | None
    routing: Routing | None
    explanation: Explanation | None
    checks: list[Check]
    failures: list[StageFailure]
    llm_calls: int
    duration_ms: int


class Recorder:
    """Writes stage events. Events are published to on_event only after the DB commit succeeds."""

    def __init__(self, session: Session, run_id: int, on_event=None):
        self.session, self.run_id, self.on_event, self._pending = session, run_id, on_event, []

    def event(self, stage, status, output=None, error=None, duration_ms=None):
        repo.add_stage_event(self.session, self.run_id, stage, status, output, error, duration_ms)
        self._pending.append({"run_id": self.run_id, "stage": stage, "status": status, "output": output,
                              "error": error, "duration_ms": duration_ms})

    def commit(self):
        self.session.commit()
        pending, self._pending = self._pending, []
        for e in pending:
            if self.on_event:
                self.on_event(e)

    def rollback(self):
        self.session.rollback()
        self._pending = []


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _timed_stage(rec: Recorder, failures: list[StageFailure], name: str, fn):
    """Run one stage outside the decision transaction. fn() returns (result, output_payload).
    On failure: record it as failed, remember it for STAGE_FAILED, return None."""
    rec.event(name, "started")
    rec.commit()
    started = time.perf_counter()
    try:
        result, output = fn()
    except Exception as e:  # noqa: BLE001 - any failure must surface as a failed stage
        rec.rollback()
        rec.event(name, "failed", error=f"{type(e).__name__}: {e}", duration_ms=_ms(started))
        failures.append(StageFailure(stage=name, error=f"{type(e).__name__}: {e}"))
        rec.commit()
        return None
    rec.event(name, "completed", output=output, duration_ms=_ms(started))
    rec.commit()
    return result


def run_pipeline(session: Session, llm: LLMClient, pdf_bytes: bytes, filename: str, settings: Settings,
                 today: date | None = None, on_event=None, run_id: int | None = None) -> RunResult:
    """run_id: pass the id of a run the API already created (status "queued"); otherwise one is created."""
    today = today or date.today()
    run_started = time.perf_counter()
    calls_before = len(llm.calls)
    if run_id is None:
        run_id = repo.create_run(session, filename, pdf_bytes)
    else:
        repo.update_run(session, run_id, status="running")
    rec = Recorder(session, run_id, on_event)
    rec.commit()
    failures: list[StageFailure] = []
    checks: list[Check] = []

    def llm_used(since):
        return len(llm.calls) - since

    # ---- 1 ingest
    def do_ingest():
        result = ingest(pdf_bytes, settings)
        return result, result.summary()
    ingested = _timed_stage(rec, failures, "ingest", do_ingest)
    if ingested:
        repo.update_run(session, run_id, doc_type=ingested.doc_type)

    # ---- 2 extract
    invoice: ExtractedInvoice | None = None
    if ingested:
        def do_extract():
            before = len(llm.calls)
            inv = extract(llm, ingested)
            return inv, {"invoice": inv.safe_dump(), "llm_calls": llm_used(before)}
        invoice = _timed_stage(rec, failures, "extract", do_extract)
    else:
        rec.event("extract", "skipped", error="ingest failed")
    if invoice:
        repo.update_run(session, run_id, vendor_name=invoice.vendor_name.value,
                        invoice_number=invoice.invoice_number.value, subtotal=invoice.subtotal.value,
                        total=invoice.total.value)
    rec.commit()

    # ---- 3 validate
    validate_checks: list[Check] = []
    if invoice:
        def do_validate():
            found = validate(invoice, settings, today)
            repo.save_checks(session, run_id, found)
            return found, {"checks": [c.model_dump(mode="json") for c in found]}
        validate_checks = _timed_stage(rec, failures, "validate", do_validate) or []
    else:
        rec.event("validate", "skipped", error="no extracted invoice")
        rec.commit()

    # ---- 4 match
    matched: Matched | None = None
    if invoice:
        ref = repo.load_reference(session)
        def do_match():
            before = len(llm.calls)
            m = match(llm, invoice, ref, settings)
            return m, {**m.result.model_dump(mode="json"), "llm_calls": llm_used(before)}
        matched = _timed_stage(rec, failures, "match", do_match)
    else:
        rec.event("match", "skipped", error="no extracted invoice")
        rec.commit()

    # ---- 5 rules + 6 decide: one transaction
    decision: Decision | None = None
    routing: Routing | None = None
    rule_checks: list[Check] = []
    stage = "rules"                                         # which stage to blame if the transaction fails
    try:
        started = time.perf_counter()
        rec.event("rules", "started")                       # first statement of the transaction (write lock is
                                                            # already held by BEGIN IMMEDIATE) ...
        ctx = decide_stage.build_context(session, settings, today, invoice, matched, failures)
        rule_checks = engine.run_rule_checks(ctx)
        repo.save_checks(session, run_id, rule_checks)
        rec.event("rules", "completed", duration_ms=_ms(started), output={
            "checks": [c.model_dump(mode="json") for c in rule_checks],
            "approved_to_date": str(ctx.approved_to_date)})

        stage = "decide"
        started = time.perf_counter()
        rec.event("decide", "started")
        checks = validate_checks + rule_checks
        decision = engine.decide(checks)
        routing = engine.route(checks)
        repo.save_decision(session, run_id, decision, routing)
        ledger_inserted = decision.decision == "Approve"
        if ledger_inserted:
            repo.insert_approved_invoice(session, decide_stage.ledger_record(ctx, today), run_id)
        repo.update_run(session, run_id, decision=decision.decision, risk_level=decision.risk_level,
                        top_reason=decision.top_reason)
        rec.event("decide", "completed", duration_ms=_ms(started), output={
            **decision.model_dump(mode="json"), "routed_to": routing.team if routing else None,
            "required_action": routing.action if routing else None, "ledger_inserted": ledger_inserted})
        rec.commit()
    except Exception as e:  # noqa: BLE001 - roll back EVERYTHING: no ledger row, no half-saved decision
        rec.rollback()
        error = f"{type(e).__name__}: {e}"
        failures.append(StageFailure(stage=stage, error=error))
        rec.event(stage, "failed", error=error)
        # Fail safe: record a Hold so the run is never left without an outcome, and never approved.
        fail_check = Check(code="STAGE_FAILED", result="FAIL", action="hold", risk="medium", stage=stage,
                           message=f"Stage failed: {stage}. Routed to manual processing. ({error})")
        checks = validate_checks + [fail_check]
        decision = engine.decide(checks)
        routing = engine.route(checks)
        repo.save_checks(session, run_id, [fail_check])
        repo.save_decision(session, run_id, decision, routing)
        repo.update_run(session, run_id, decision=decision.decision, risk_level=decision.risk_level,
                        top_reason=decision.top_reason)
        rec.commit()

    # ---- 7 explain (AI words about the decision; cannot change it)
    explanation: Explanation | None = None
    before = len(llm.calls)

    def do_explain():
        e = decide_stage.explain(llm, invoice, decision, routing, checks)
        return e, {**e.model_dump(mode="json"), "llm_calls": llm_used(before)}
    explanation = _timed_stage(rec, failures, "explain", do_explain)
    repo.save_explanation(session, run_id,
                          explanation.summary if explanation else decide_stage.fallback_summary(decision),
                          explanation.drafted_message if explanation else None)
    status = "failed" if failures else "completed"
    repo.update_run(session, run_id, status=status)
    rec.commit()

    return RunResult(run_id=run_id, status=status, decision=decision, routing=routing, explanation=explanation,
                     checks=checks, failures=failures, llm_calls=len(llm.calls) - calls_before,
                     duration_ms=_ms(run_started))
