"""Shared test fixtures. The LLM is ALWAYS mocked: no API key, no network.

Test data comes from the same source as the sample PDFs: scripts/generate_invoices.py builds each scenario
invoice from the seed CSVs, and `mirror()` turns it into the ExtractedInvoice an ideal extraction would return.
"""
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import generate_invoices as gen  # scripts/ is on the path (pytest.ini)
import pytest

from app.config import Settings
from app.db import repository as repo
from app.db.seed import seed_if_missing
from app.db.session import init_db, make_engine, make_session_factory
from app.schemas import (ApprovedInvoiceRecord, DateField, Explanation, ExtractedCharge, ExtractedInvoice,
                         ExtractedLine, ExtractedTax, LineMatch, LineMatchResponse, LLMLineMatch, MoneyField,
                         POCandidate, POInference, POLineRecord, PORecord, POMatch, RuleContext, TextField,
                         VendorMatch, VendorRecord)

TODAY = date(2026, 10, 30)
SPECS = {s["id"]: s for s in gen.SCENARIOS}


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def db(tmp_path, settings):
    """A fresh, seeded SQLite file per test. Yields (engine, session_factory, session)."""
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        seed_if_missing(session, settings)
        yield engine, factory, session
    engine.dispose()


# ----------------------------------------------------------------------------- extraction mirrors
def tf(value, conf=0.95):
    return TextField(value=value, confidence=conf if value else 0.0, evidence=value, page=1 if value else None)


def mirror(scenario_id: str) -> ExtractedInvoice:
    """What a perfect extraction of the scenario's PDF looks like."""
    return extracted_from(gen.build_invoice(SPECS[scenario_id]))


def extracted_from(inv) -> ExtractedInvoice:
    """What a perfect reader returns for a generated invoice (a missing invoice number is a null field)."""
    spec = inv.spec
    return ExtractedInvoice(
        vendor_name=tf(inv.vendor["name"]), vendor_gstin=tf(inv.vendor["gstin"]),
        invoice_number=tf(spec["number"]),
        invoice_date=DateField(value=spec["date"], confidence=0.95, evidence=str(spec["date"]), page=1),
        po_reference=tf(spec["po"] if spec["show_po"] else None),
        bank_account=tf(inv.bank_account), ifsc=tf(inv.vendor["ifsc"]),
        line_items=[ExtractedLine(description=l.description, qty=Decimal(l.qty), unit_price=l.rate,
                                  amount=l.amount, confidence=0.95) for l in inv.lines],
        other_charges=[ExtractedCharge(description=label, amount=amount, confidence=0.95)
                       for label, amount in inv.charges],
        subtotal=MoneyField(value=inv.subtotal, confidence=0.95, evidence=gen.inr(inv.subtotal), page=1),
        tax_lines=[ExtractedTax(description=label, rate_pct=Decimal(9 if "9%" in label else 18), amount=amount)
                   for label, amount in inv.tax_lines],
        total=MoneyField(value=inv.total, confidence=0.95, evidence=gen.inr(inv.total), page=1),
        currency=tf("INR"))


def vendor_record(vendor_id: str) -> VendorRecord:
    v = gen.VENDORS[vendor_id]
    return VendorRecord(vendor_id=vendor_id, name=v["name"], gstin=v["gstin"], bank_account=v["bank_account"],
                        ifsc=v["ifsc"], status=v["status"])


def po_record(po_id: str) -> PORecord:
    po = gen.POS[po_id]
    lines = [POLineRecord(po_line_id=k, po_id=po_id, line_no=int(r["line_no"]), description=r["description"],
                          hsn=r["hsn"], qty=Decimal(r["qty"]), unit=r["unit"], unit_price=Decimal(r["unit_price"]))
             for k, r in gen.PO_LINES.items() if r["po_id"] == po_id]
    return PORecord(po_id=po_id, vendor_id=po["vendor_id"], po_date=date.fromisoformat(po["po_date"]),
                    status=po["status"], currency=po["currency"], lines=lines)


def make_ctx(scenario_id: str = "H", **overrides) -> RuleContext:
    """A fully matched context for a scenario: vendor by GSTIN, PO as printed, lines matched exactly in order.
    Rule tests start from this and change one thing."""
    spec = SPECS[scenario_id]
    ctx = RuleContext(
        settings=Settings(), today=TODAY, invoice=mirror(scenario_id), vendor=vendor_record(spec["vendor"]),
        vendor_match=VendorMatch(vendor_id=spec["vendor"], name=gen.VENDORS[spec["vendor"]]["name"], method="gstin"),
        po=po_record(spec["po"]), po_match=POMatch(po_id=spec["po"], method="explicit", reference=spec["po"]),
        line_matches=[LineMatch(invoice_line_index=i, description=gen.PO_LINES[k]["description"], po_line_id=k,
                                method="exact", confidence=1.0) for i, (k, _) in enumerate(spec["lines"])],
        approved_to_date=gen.approved_to_date(spec["po"]),
        vendor_approved_invoices=[
            ApprovedInvoiceRecord(vendor_id=h["vendor_id"], invoice_number=h["invoice_number"], po_id=h["po_id"],
                                  invoice_date=date.fromisoformat(h["invoice_date"]), subtotal=Decimal(h["subtotal"]),
                                  tax_amount=Decimal(h["tax_amount"]), total=Decimal(h["total"]),
                                  approved_on=date.fromisoformat(h["approved_on"]))
            for h in gen.HISTORY if h["vendor_id"] == spec["vendor"]])
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


# ----------------------------------------------------------------------------- fake LLM
@dataclass
class FakeCall:
    prompt: str
    content: list


class FakeLLM:
    """Stands in for LLMClient. responses maps schema class -> reply object, Exception, or callable(content)."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[FakeCall] = []

    def call(self, prompt, content, schema):
        self.calls.append(FakeCall(prompt, content))
        reply = self.responses.get(schema)
        if callable(reply):
            reply = reply(content)
        if reply is None:
            raise AssertionError(f"unexpected LLM call: {prompt}")
        if isinstance(reply, Exception):
            raise reply
        return reply

    @property
    def prompts(self):
        return [c.prompt for c in self.calls]


def scenario_llm(scenario_id: str) -> FakeLLM:
    """The mocked LLM for a scenario. Only the calls the scenario legitimately needs are answered; any other
    call raises, so a test fails if the pipeline calls the LLM when it should not."""
    responses = {ExtractedInvoice: mirror(scenario_id),
                 Explanation: Explanation(summary="Explained.", drafted_message=None if scenario_id in ("H", "E5") else "Please review.")}
    if scenario_id == "E3":
        responses[POInference] = POInference(ranking=[
            POCandidate(po_id="PO-2057", confidence=0.93, reason="Cleaning products, quantities and prices match"),
            POCandidate(po_id="PO-2061", confidence=0.04, reason="Uniforms and trolleys: different goods")])

        def match_lines(content):
            asked = json.loads(content[0]["text"])["invoice_lines"]
            answers = {"Liquid Handwash 5 Ltr Can": "POL-2057-2", "Garbage Bags 30 Litre - Pack of 30": "POL-2057-4"}
            return LineMatchResponse(matches=[LLMLineMatch(invoice_line_index=a["invoice_line_index"],
                                                           po_line_id=answers[a["description"]], confidence=0.95,
                                                           reason="Same product, different wording") for a in asked])
        responses[LineMatchResponse] = match_lines
    return FakeLLM(responses)


def sample_pdf(scenario_id: str) -> bytes:
    return (gen.OUT / SPECS[scenario_id]["file"]).read_bytes()
