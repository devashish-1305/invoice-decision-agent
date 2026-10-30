"""Stages 5 and 6.

build_context:  gathers the facts the pure rules need (PO consumption, the vendor's approved invoices).
                MUST be called inside the decision transaction so the balance it reads cannot change
                before the decision and ledger insert are committed.
ledger_record:  the row an Approve inserts into the approved-invoice ledger.
explain:        the only AI in stage 6. It writes words about a decision that was already made by rules.
"""
import json
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.config import Settings
from app.db import repository as repo
from app.llm.client import LLMClient, text_block
from app.pipeline.match import Matched
from app.schemas import (ApprovedInvoiceRecord, Check, Decision, Explanation, ExtractedInvoice, Routing,
                         RuleContext, StageFailure)
from app.util import inr, mask_json, reformat_amounts

# Evidence keys that hold rupee amounts. The explanation model is given them already formatted (₹1,35,051.00),
# so it never has to format a number itself.
MONEY_KEYS = {"po_subtotal", "approved_to_date", "invoice_subtotal", "total_after", "limit", "matched_po_lines_value",
              "its_total", "this_total", "total", "lines_plus_charges", "subtotal", "tax", "unit_price", "po_unit_price"}


def collect_amounts(value, key=None, out=None):
    """Every rupee amount (as Decimal) found under a MONEY_KEYS key in JSON-like evidence."""
    out = [] if out is None else out
    if isinstance(value, dict):
        for k, v in value.items():
            collect_amounts(v, k, out)
    elif isinstance(value, list):
        for v in value:
            collect_amounts(v, key, out)
    elif key in MONEY_KEYS and value is not None:
        try:
            out.append(Decimal(str(value)))
        except InvalidOperation:
            pass
    return out


def format_amounts(value, key=None):
    """Walk JSON-like evidence and turn rupee-amount values into formatted strings."""
    if isinstance(value, dict):
        return {k: format_amounts(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [format_amounts(v, key) for v in value]
    if key in MONEY_KEYS and value is not None:
        try:
            return inr(Decimal(str(value)))
        except InvalidOperation:
            return value
    return value


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
                    "total": inr(invoice.total.value) if invoice.total.value is not None else None} if invoice else None,
        "triggered_checks": [{"code": c.code, "result": c.result, "message": c.message,
                              "evidence": format_amounts(c.model_dump(mode="json")["evidence"])} for c in checks if c.triggered],
        "passed_checks": [c.code for c in checks if not c.triggered],
    }
    answer = llm.call("explain", [text_block(json.dumps(mask_json(payload), default=str, indent=1, ensure_ascii=False))],
                      Explanation)
    # Presentation guard: summary and message are shown in the UI, so any raw amount the model still wrote
    # is reformatted (quantities are left alone).
    evidence = [c.model_dump(mode="json")["evidence"] for c in checks if c.triggered]
    amounts = collect_amounts(evidence) + ([invoice.total.value] if invoice and invoice.total.value is not None else [])
    quantities = [Decimal(str(row[k])) for ev in evidence for row in ev.get("lines", []) if isinstance(row, dict)
                  for k in ("qty", "po_qty") if row.get(k) is not None]
    return answer.model_copy(update={"summary": reformat_amounts(answer.summary, amounts, quantities),
                                     "drafted_message": reformat_amounts(answer.drafted_message, amounts, quantities)})


def fallback_summary(decision: Decision) -> str:
    """Used only when the explanation call fails. Plain, deterministic, clearly labelled."""
    reason = f" Main reason: {decision.top_reason}" if decision.top_reason else ""
    return f"{decision.decision}.{reason} (Automatic summary: no AI explanation was written.)"
