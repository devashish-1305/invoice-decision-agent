"""Every data shape in the app.

Three groups:
  1. LLM output schemas (ExtractedInvoice, POInference, LineMatchResponse, Explanation). The LLM client
     validates every reply against one of these; a reply that does not fit is a failed call.
  2. Records and results passed between stages (VendorRecord, MatchResult, Check, Decision ...).
  3. RuleContext: everything the pure rules need, assembled once by the pipeline.

Money is Decimal everywhere. Bank account numbers appear in ExtractedInvoice and VendorRecord only
so BANK_MATCH can compare them; safe_dump() / exclude=True keep them out of anything stored or returned.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.config import Settings
from app.util import mask_account, mask_text

Confidence = Field(ge=0, le=1)


# --------------------------------------------------------------------------- 1. LLM output schemas
class Evidenced(BaseModel):
    """A critical field as the model read it: value + how sure + the exact text + the page."""
    confidence: float = Confidence
    evidence: str | None
    page: int | None


class TextField(Evidenced):
    value: str | None

    @field_validator("value")
    @classmethod
    def blank_is_null(cls, v):
        return v.strip() or None if v is not None else None

    @classmethod
    def empty(cls):
        return cls(value=None, confidence=0.0, evidence=None, page=None)


class DateField(Evidenced):
    value: date | None

    @classmethod
    def empty(cls):
        return cls(value=None, confidence=0.0, evidence=None, page=None)


class MoneyField(Evidenced):
    value: Decimal | None

    @classmethod
    def empty(cls):
        return cls(value=None, confidence=0.0, evidence=None, page=None)


class ExtractedLine(BaseModel):
    description: str
    qty: Decimal | None
    unit_price: Decimal | None
    amount: Decimal | None
    confidence: float = Confidence


class ExtractedCharge(BaseModel):
    """Freight and other non-product charges."""
    description: str
    amount: Decimal | None
    confidence: float = Confidence


class ExtractedTax(BaseModel):
    description: str          # e.g. "CGST @ 9%"
    rate_pct: Decimal | None
    amount: Decimal | None


CRITICAL_FIELDS = ("vendor_name", "vendor_gstin", "invoice_number", "invoice_date", "po_reference",
                   "bank_account", "ifsc", "subtotal", "total")


class ExtractedInvoice(BaseModel):
    vendor_name: TextField
    vendor_gstin: TextField
    invoice_number: TextField
    invoice_date: DateField
    po_reference: TextField
    bank_account: TextField
    ifsc: TextField
    line_items: list[ExtractedLine]
    other_charges: list[ExtractedCharge]
    subtotal: MoneyField
    tax_lines: list[ExtractedTax]
    total: MoneyField
    currency: TextField

    def safe_dump(self) -> dict:
        """JSON-safe dump with bank account numbers masked, for storage, events and the UI."""
        data = self.model_dump(mode="json")
        data["bank_account"]["value"] = mask_account(data["bank_account"]["value"])
        for name in CRITICAL_FIELDS:
            data[name]["evidence"] = mask_text(data[name]["evidence"])
        return data


class POCandidate(BaseModel):
    po_id: str
    confidence: float = Confidence
    reason: str


class POInference(BaseModel):
    ranking: list[POCandidate]


class LLMLineMatch(BaseModel):
    invoice_line_index: int
    po_line_id: str | None
    confidence: float = Confidence
    reason: str


class LineMatchResponse(BaseModel):
    matches: list[LLMLineMatch]


class Explanation(BaseModel):
    summary: str
    drafted_message: str | None


# --------------------------------------------------------------------------- 2. records and results
class VendorRecord(BaseModel):
    vendor_id: str
    name: str
    gstin: str
    bank_account: str = Field(exclude=True, repr=False)   # never serialised: only BANK_MATCH reads it
    ifsc: str
    status: str


class POLineRecord(BaseModel):
    po_line_id: str
    po_id: str
    line_no: int
    description: str
    hsn: str | None
    qty: Decimal
    unit: str | None
    unit_price: Decimal

    @property
    def value(self) -> Decimal:
        return self.qty * self.unit_price


class PORecord(BaseModel):
    po_id: str
    vendor_id: str
    po_date: date
    status: str
    currency: str
    lines: list[POLineRecord]

    @property
    def subtotal(self) -> Decimal:
        """Tax-exclusive value of all PO lines."""
        return sum((l.value for l in self.lines), Decimal(0))


class ApprovedInvoiceRecord(BaseModel):
    vendor_id: str
    invoice_number: str
    po_id: str | None
    invoice_date: date
    subtotal: Decimal      # tax-exclusive: PO consumption
    tax_amount: Decimal
    total: Decimal         # incl. GST: duplicate comparison
    approved_on: date


class ReferenceData(BaseModel):
    vendors: list[VendorRecord]
    pos: list[PORecord]


class StageFailure(BaseModel):
    stage: str
    error: str


class VendorMatch(BaseModel):
    vendor_id: str | None
    name: str | None
    method: Literal["gstin", "name", "none"]
    score: float | None = None          # name similarity when method == "name"
    candidates: list[str] = []          # vendor names that look similar (suggestions only)


class POMatch(BaseModel):
    po_id: str | None
    method: Literal["explicit", "inferred", "none"]
    reference: str | None = None        # what the invoice printed
    confidence: float | None = None
    reason: str | None = None
    ranking: list[POCandidate] = []


class LineMatch(BaseModel):
    invoice_line_index: int
    description: str
    po_line_id: str | None
    method: Literal["exact", "semantic", "none"]
    confidence: float | None = None
    reason: str | None = None


class MatchResult(BaseModel):
    vendor: VendorMatch
    po: POMatch
    lines: list[LineMatch]


Risk = Literal["low", "medium", "high"]


class Check(BaseModel):
    code: str
    result: Literal["PASS", "WARN", "FAIL"]
    message: str
    evidence: dict[str, Any] = {}
    action: Literal["hold", "reject"] | None = None   # what a non-PASS means for the decision
    risk: Risk = "low"
    stage: str = "rules"
    not_evaluated: bool = False   # a WARN that only says "couldn't check this" because something upstream is missing

    @property
    def triggered(self) -> bool:
        return self.result != "PASS"


class Decision(BaseModel):
    decision: Literal["Approve", "Hold", "Reject"]
    risk_level: Literal["none", "low", "medium", "high"]
    triggered_codes: list[str]
    top_reason: str | None


class Routing(BaseModel):
    team: str
    action: str


# --------------------------------------------------------------------------- 3. rule context
@dataclass
class RuleContext:
    """Plain data for the pure rule functions. No DB handles, no LLM, no clock (today is passed in)."""
    settings: Settings
    today: date
    invoice: ExtractedInvoice | None = None
    vendor: VendorRecord | None = None              # identified (GSTIN) or merely suggested (name)
    vendor_match: VendorMatch | None = None
    po: PORecord | None = None
    po_match: POMatch | None = None
    line_matches: list[LineMatch] = field(default_factory=list)
    approved_to_date: Decimal = Decimal(0)          # sum of approved subtotals on ctx.po
    vendor_approved_invoices: list[ApprovedInvoiceRecord] = field(default_factory=list)
    stage_failures: list[StageFailure] = field(default_factory=list)
