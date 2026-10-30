"""All database reads and writes. Functions take a Session and never commit: the caller owns the
transaction, which is what lets the orchestrator keep "balance check + decision + ledger insert" atomic.
"""
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models as m
from app.schemas import (ApprovedInvoiceRecord, Check, Decision, POLineRecord, PORecord, ReferenceData,
                         Routing, VendorRecord)
from app.util import mask_json, mask_text


# ------------------------------------------------------------------ reference data
def load_reference(session: Session) -> ReferenceData:
    vendors = [VendorRecord(vendor_id=v.vendor_id, name=v.name, gstin=v.gstin, bank_account=v.bank_account,
                            ifsc=v.ifsc, status=v.status) for v in session.scalars(select(m.Vendor))]
    lines_by_po: dict[str, list[POLineRecord]] = {}
    for l in session.scalars(select(m.POLine).order_by(m.POLine.po_id, m.POLine.line_no)):
        lines_by_po.setdefault(l.po_id, []).append(POLineRecord(
            po_line_id=l.po_line_id, po_id=l.po_id, line_no=l.line_no, description=l.description,
            hsn=l.hsn, qty=l.qty, unit=l.unit, unit_price=l.unit_price))
    pos = [PORecord(po_id=p.po_id, vendor_id=p.vendor_id, po_date=p.po_date, status=p.status,
                    currency=p.currency, lines=lines_by_po.get(p.po_id, []))
           for p in session.scalars(select(m.PurchaseOrder))]
    return ReferenceData(vendors=vendors, pos=pos)


# ------------------------------------------------------------------ approved-invoice ledger
def approved_to_date(session: Session, po_id: str) -> Decimal:
    """PO consumption: sum of tax-exclusive subtotals of approved invoices on this PO."""
    rows = session.scalars(select(m.ApprovedInvoice.subtotal).where(m.ApprovedInvoice.po_id == po_id))
    return sum(rows, Decimal(0))


def approved_invoices_for_vendor(session: Session, vendor_id: str) -> list[ApprovedInvoiceRecord]:
    rows = session.scalars(select(m.ApprovedInvoice).where(m.ApprovedInvoice.vendor_id == vendor_id))
    return [ApprovedInvoiceRecord(vendor_id=r.vendor_id, invoice_number=r.invoice_number, po_id=r.po_id,
                                  invoice_date=r.invoice_date, subtotal=r.subtotal, tax_amount=r.tax_amount,
                                  total=r.total, approved_on=r.approved_on) for r in rows]


def insert_approved_invoice(session: Session, rec: ApprovedInvoiceRecord, run_id: int) -> None:
    session.add(m.ApprovedInvoice(**rec.model_dump(), run_id=run_id))
    session.flush()  # raises IntegrityError here (not at commit) if the unique constraint is hit


def count_ledger(session: Session) -> int:
    return len(session.scalars(select(m.ApprovedInvoice.id)).all())


# ------------------------------------------------------------------ audit trail
def create_run(session: Session, filename: str, pdf: bytes | None = None, status: str = "running") -> int:
    run = m.Run(filename=filename, pdf=pdf, status=status)
    session.add(run)
    session.flush()
    return run.id


def update_run(session: Session, run_id: int, **fields) -> None:
    run = session.get(m.Run, run_id)
    for key, value in fields.items():
        setattr(run, key, value)
    session.flush()


def add_stage_event(session: Session, run_id: int, stage: str, status: str, output: dict | None = None,
                    error: str | None = None, duration_ms: int | None = None) -> None:
    session.add(m.StageEvent(run_id=run_id, stage=stage, status=status, output=mask_json(output),
                             error=mask_text(error), duration_ms=duration_ms))
    session.flush()


def save_checks(session: Session, run_id: int, checks: list[Check]) -> None:
    for c in checks:
        session.add(m.CheckRow(run_id=run_id, stage=c.stage, code=c.code, result=c.result, action=c.action,
                               risk=c.risk, message=mask_text(c.message),
                               evidence=mask_json(c.model_dump(mode="json")["evidence"])))
    session.flush()


def save_decision(session: Session, run_id: int, decision: Decision, routing: Routing | None) -> None:
    session.add(m.DecisionRow(run_id=run_id, decision=decision.decision, risk_level=decision.risk_level,
                              top_reason=mask_text(decision.top_reason),
                              routed_to=routing.team if routing else None,
                              required_action=routing.action if routing else None))
    session.flush()


def save_explanation(session: Session, run_id: int, summary: str | None, drafted_message: str | None) -> None:
    row = session.get(m.DecisionRow, run_id)
    row.summary = mask_text(summary)
    row.drafted_message = mask_text(drafted_message)
    session.flush()


def get_run(session: Session, run_id: int) -> m.Run:
    return session.get(m.Run, run_id)


def get_events(session: Session, run_id: int) -> list[m.StageEvent]:
    return list(session.scalars(select(m.StageEvent).where(m.StageEvent.run_id == run_id).order_by(m.StageEvent.id)))


def get_checks(session: Session, run_id: int) -> list[m.CheckRow]:
    return list(session.scalars(select(m.CheckRow).where(m.CheckRow.run_id == run_id).order_by(m.CheckRow.id)))


def get_decision(session: Session, run_id: int) -> m.DecisionRow | None:
    return session.get(m.DecisionRow, run_id)
