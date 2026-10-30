"""Stages 5 and 6.

build_context:  gathers the facts the pure rules need (PO consumption, the vendor's approved invoices).
                MUST be called inside the decision transaction so the balance it reads cannot change
                before the decision and ledger insert are committed.
ledger_record:  the row an Approve inserts into the approved-invoice ledger.
explain:        the only AI in stage 6. It writes words about a decision that was already made by rules.
"""
import json
from datetime import date

from sqlalchemy.orm import Session

from app.config import Settings
from app.db import repository as repo
from app.llm.client import LLMClient, text_block
from app.pipeline.match import Matched
from app.schemas import (ApprovedInvoiceRecord, Check, Decision, Explanation, ExtractedInvoice, Routing,
                         RuleContext, StageFailure)
from app.util import mask_json


def build_context(session: Session, settings: Settings, today: date, invoice: ExtractedInvoice | None,
                  matched: Matched | None, failures: list[StageFailure]) -> RuleContext:
    ctx = RuleContext(settings=settings, today=today, invoice=invoice, stage_failures=failures)
    if matched is None:
        return ctx
    ctx.vendor, ctx.po = matched.vendor, matched.po
    ctx.vendor_match, ctx.po_match, ctx.line_matches = matched.result.vendor, matched.result.po, matched.result.lines
    if ctx.vendor:
        ctx.vendor_approved_invoices = repo.approved_invoices_for_vendor(session, ctx.vendor.vendor_id)
    if ctx.po:
        ctx.approved_to_date = repo.approved_to_date(session, ctx.po.po_id)
    return ctx


def ledger_record(ctx: RuleContext, today: date) -> ApprovedInvoiceRecord:
    """Built only for an Approve, when every check passed (so these fields exist)."""
    inv = ctx.invoice
    return ApprovedInvoiceRecord(
        vendor_id=ctx.vendor.vendor_id, invoice_number=inv.invoice_number.value, po_id=ctx.po.po_id,
        invoice_date=inv.invoice_date.value, subtotal=inv.subtotal.value,
        tax_amount=inv.total.value - inv.subtotal.value, total=inv.total.value, approved_on=today)


def explain(llm: LLMClient, invoice: ExtractedInvoice | None, decision: Decision, routing: Routing | None,
            checks: list[Check]) -> Explanation:
    payload = {
        "decision": decision.decision, "risk_level": decision.risk_level,
        "routed_to": routing.team if routing else None, "required_action": routing.action if routing else None,
        "invoice": {"vendor": invoice.vendor_name.value, "invoice_number": invoice.invoice_number.value,
                    "total": invoice.total.value} if invoice else None,
        "triggered_checks": [{"code": c.code, "result": c.result, "message": c.message,
                              "evidence": c.model_dump(mode="json")["evidence"]} for c in checks if c.triggered],
        "passed_checks": [c.code for c in checks if not c.triggered],
    }
    return llm.call("explain", [text_block(json.dumps(mask_json(payload), default=str, indent=1))], Explanation)


def fallback_summary(decision: Decision) -> str:
    """Used only when the explanation call fails. Plain, deterministic, clearly labelled."""
    reason = f" Main reason: {decision.top_reason}" if decision.top_reason else ""
    return f"{decision.decision}.{reason} (Automatic summary: the AI explanation was unavailable.)"
