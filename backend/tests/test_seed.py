"""Seed-if-missing and explicit reset."""
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select

from app.db import models as m
from app.db import repository as repo
from app.db.seed import reset, seed_if_missing
from app.schemas import ApprovedInvoiceRecord


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def test_seed_loads_reference_data_and_ledger_history(db):
    _, _, session = db
    assert (count(session, m.Vendor), count(session, m.PurchaseOrder), count(session, m.POLine)) == (6, 7, 26)
    assert count(session, m.ApprovedInvoice) == 3
    assert repo.approved_to_date(session, "PO-2041") == Decimal("375000.00")


def test_seed_if_missing_does_not_touch_existing_data(db, settings):
    _, _, session = db
    session.add(m.Run(filename="keep-me.pdf"))
    session.commit()
    assert seed_if_missing(session, settings) is False
    assert count(session, m.Run) == 1 and count(session, m.Vendor) == 6


def test_reset_clears_runs_and_restores_the_seed_ledger(db, settings):
    engine, factory, session = db
    run_id = repo.create_run(session, "x.pdf")
    repo.insert_approved_invoice(session, ApprovedInvoiceRecord(
        vendor_id="V001", invoice_number="NEW-1", po_id="PO-2038", invoice_date=date(2026, 10, 1),
        subtotal=Decimal("1"), tax_amount=Decimal("0.18"), total=Decimal("1.18"), approved_on=date(2026, 10, 2)), run_id)
    session.commit()
    assert count(session, m.ApprovedInvoice) == 4
    session.close()  # an open read transaction would hold a lock
    reset(engine, factory, settings)
    with factory() as fresh:
        assert count(fresh, m.Run) == 0 and count(fresh, m.ApprovedInvoice) == 3 and count(fresh, m.Vendor) == 6
