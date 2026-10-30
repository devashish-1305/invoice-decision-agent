"""PRD section 10 ledger tests: only Approve consumes PO balance, atomically, and re-runs are safe."""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from app.db import models as m
from app.db import repository as repo
from app.pipeline.orchestrator import run_pipeline
from app.schemas import ApprovedInvoiceRecord
from conftest import TODAY, sample_pdf, scenario_llm

SEEDED = 3  # rows in invoice_history.csv


def run(session, settings, scenario, **kw):
    return run_pipeline(session, scenario_llm(scenario), sample_pdf(scenario), f"{scenario}.pdf", settings,
                        today=TODAY, **kw)


def test_approve_inserts_exactly_one_ledger_row_with_subtotal_and_total(db, settings):
    _, _, session = db
    result = run(session, settings, "H")
    assert result.decision.decision == "Approve" and repo.count_ledger(session) == SEEDED + 1
    row = session.query(m.ApprovedInvoice).filter_by(invoice_number="SOS/2627/0418").one()
    assert (row.subtotal, row.tax_amount, row.total) == (Decimal("111460.00"), Decimal("20062.80"), Decimal("131522.80"))
    assert row.run_id == result.run_id and row.po_id == "PO-2038"


@pytest.mark.parametrize("scenario", ["E1", "E2", "E3", "E4"])
def test_hold_never_inserts_into_the_ledger(db, settings, scenario):
    _, _, session = db
    assert run(session, settings, scenario).decision.decision == "Hold"
    assert repo.count_ledger(session) == SEEDED


def test_reject_never_inserts_into_the_ledger(db, settings):
    _, _, session = db
    run(session, settings, "H")                         # approve it once
    assert run(session, settings, "H").decision.decision == "Reject"
    assert repo.count_ledger(session) == SEEDED + 1     # still just the one


def test_held_invoices_do_not_consume_po_balance(db, settings):
    _, _, session = db
    before = repo.approved_to_date(session, "PO-2041")
    run(session, settings, "E1")                        # Hold: over the PO
    assert repo.approved_to_date(session, "PO-2041") == before == Decimal("375000.00")


def test_approved_invoice_consumes_po_balance_by_subtotal(db, settings):
    _, _, session = db
    run(session, settings, "E5")
    assert repo.approved_to_date(session, "PO-2068") == Decimal("202600.00")  # tax excluded


def test_rerunning_an_approved_invoice_creates_a_new_run_and_rejects_via_duplicate_exact(db, settings):
    _, _, session = db
    first = run(session, settings, "H")
    second = run(session, settings, "H")                # no uniqueness error: the runs table has no constraint
    assert first.decision.decision == "Approve" and second.decision.decision == "Reject"
    assert second.run_id != first.run_id and second.status == "completed"
    assert "DUPLICATE_EXACT" in second.decision.triggered_codes
    assert session.query(m.Run).count() == 2


def test_the_database_itself_enforces_one_approval_per_vendor_and_invoice_number(db):
    _, _, session = db
    dupe = ApprovedInvoiceRecord(vendor_id="V003", invoice_number="INV-1042", po_id="PO-2050",
                                 invoice_date=date(2026, 10, 5), subtotal=Decimal("1"), tax_amount=Decimal("0"),
                                 total=Decimal("1"), approved_on=date(2026, 10, 6))
    with pytest.raises(IntegrityError):
        repo.insert_approved_invoice(session, dupe, 1)
    session.rollback()
    # same number for a DIFFERENT vendor is fine
    repo.insert_approved_invoice(session, dupe.model_copy(update={"vendor_id": "V001", "po_id": "PO-2038"}), 1)


def test_a_transaction_that_fails_midway_leaves_po_consumption_unchanged(db, settings, monkeypatch):
    _, _, session = db
    real_insert = repo.insert_approved_invoice

    def insert_then_crash(*args, **kwargs):
        real_insert(*args, **kwargs)                     # the ledger row IS flushed ...
        raise RuntimeError("disk full")                  # ... then the transaction dies before commit

    monkeypatch.setattr(repo, "insert_approved_invoice", insert_then_crash)
    consumed_before = repo.approved_to_date(session, "PO-2038")
    session.rollback()

    result = run(session, settings, "H")

    assert repo.count_ledger(session) == SEEDED
    assert repo.approved_to_date(session, "PO-2038") == consumed_before == Decimal(0)
    assert result.status == "failed" and [f.stage for f in result.failures] == ["decide"]
    assert result.decision.decision == "Hold"            # fail safe: recorded as Hold, never Approve
    events = {(e.stage, e.status) for e in repo.get_events(session, result.run_id)}
    assert ("decide", "failed") in events and ("decide", "completed") not in events


def test_after_a_failed_transaction_the_same_invoice_can_still_be_approved(db, settings, monkeypatch):
    _, _, session = db
    real_insert = repo.insert_approved_invoice
    monkeypatch.setattr(repo, "insert_approved_invoice",
                        lambda *a, **k: (real_insert(*a, **k), (_ for _ in ()).throw(RuntimeError("boom"))))
    run(session, settings, "H")
    monkeypatch.setattr(repo, "insert_approved_invoice", real_insert)
    assert run(session, settings, "H").decision.decision == "Approve"


def test_the_decision_transaction_holds_the_write_lock_so_two_approvals_cannot_race(db, tmp_path):
    """The decision transaction starts with a write. While it is open, another writer must wait."""
    from app.db.session import make_engine, make_session_factory
    engine, _, _ = db
    other = make_session_factory(make_engine(str(engine.url), busy_timeout=0.2))
    with make_session_factory(make_engine(str(engine.url)))() as first, other() as second:
        repo.create_run(first, "first.pdf")             # first statement of a decision transaction: a write
        with pytest.raises(OperationalError, match="locked"):
            repo.create_run(second, "second.pdf")       # cannot start until `first` commits or rolls back
        second.rollback()
        first.commit()
        repo.create_run(second, "second.pdf")
        second.commit()


def test_overlapping_runs_on_one_database_all_complete_without_lock_errors(tmp_path, settings):
    """Regression: five runs in parallel threads used to hit 'database is locked' (lock-upgrade deadlock)."""
    import threading
    from app.db.seed import seed_if_missing
    from app.db.session import init_db, make_engine, make_session_factory

    engine = make_engine(f"sqlite:///{tmp_path / 'concurrent.db'}")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        seed_if_missing(session, settings)

    results = {}

    def go(scenario):
        with factory() as session:
            results[scenario] = run_pipeline(session, scenario_llm(scenario), sample_pdf(scenario), f"{scenario}.pdf",
                                             settings, today=TODAY)

    threads = [threading.Thread(target=go, args=(s,)) for s in ("H", "E1", "E3", "E4", "E5")]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert {s: (r.status, r.decision.decision) for s, r in results.items()} == {
        "H": ("completed", "Approve"), "E1": ("completed", "Hold"), "E3": ("completed", "Hold"),
        "E4": ("completed", "Hold"), "E5": ("completed", "Approve")}
    with factory() as session:
        assert repo.count_ledger(session) == SEEDED + 2      # exactly the two approvals
