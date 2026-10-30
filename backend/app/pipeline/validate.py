"""Stage 3: Validate. No AI. Internal consistency of the extracted invoice (arithmetic, GST slab, date,
required fields, confidence). The checks themselves live in rules/checks.py."""
from datetime import date

from app.config import Settings
from app.rules.engine import run_validate_checks
from app.schemas import Check, ExtractedInvoice, RuleContext


def validate(invoice: ExtractedInvoice, settings: Settings, today: date) -> list[Check]:
    return run_validate_checks(RuleContext(settings=settings, today=today, invoice=invoice))
