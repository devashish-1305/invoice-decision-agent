"""Seed reference data and approved-invoice history from backend/data/*.csv.

seed_if_missing: used at startup, does nothing when the DB already has vendors (history is preserved).
reset:           explicit; drops everything (including run history) and reseeds, so every demo starts
                 from the same state.
"""
import csv
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import ApprovedInvoice, Base, POLine, PurchaseOrder, Vendor


def _rows(settings: Settings, name: str) -> list[dict]:
    with open(settings.data_dir / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _seed(session: Session, settings: Settings) -> None:
    for r in _rows(settings, "vendors.csv"):
        session.add(Vendor(**r))
    session.flush()  # each table is flushed before the next one that references it (foreign keys)
    for r in _rows(settings, "purchase_orders.csv"):
        session.add(PurchaseOrder(**{**r, "po_date": date.fromisoformat(r["po_date"])}))
    session.flush()
    for r in _rows(settings, "po_lines.csv"):
        session.add(POLine(po_line_id=r["po_line_id"], po_id=r["po_id"], line_no=int(r["line_no"]),
                           description=r["description"], hsn=r["hsn"], qty=Decimal(r["qty"]),
                           unit=r["unit"], unit_price=Decimal(r["unit_price"])))
    session.flush()
    for r in _rows(settings, "invoice_history.csv"):
        session.add(ApprovedInvoice(
            vendor_id=r["vendor_id"], invoice_number=r["invoice_number"], po_id=r["po_id"],
            invoice_date=date.fromisoformat(r["invoice_date"]), subtotal=Decimal(r["subtotal"]),
            tax_amount=Decimal(r["tax_amount"]), total=Decimal(r["total"]),
            approved_on=date.fromisoformat(r["approved_on"])))


def seed_if_missing(session: Session, settings: Settings) -> bool:
    """Returns True if it seeded."""
    if session.scalar(select(func.count()).select_from(Vendor)):
        return False
    _seed(session, settings)
    session.commit()
    return True


def reset(engine, session_factory, settings: Settings) -> None:
    """Drop every table (reference, ledger, runs, events, checks, decisions) and reseed."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with session_factory() as session:
        _seed(session, settings)
        session.commit()
