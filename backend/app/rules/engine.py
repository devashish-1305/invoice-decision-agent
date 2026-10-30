"""The decision engine (PRD section 5.2) and exception routing (section 5.3).

Pure: checks in, decision out. The LLM never touches this file.

  1. any FAIL marked reject            -> Reject
  2. else any FAIL or WARN             -> Hold   (risk = highest among triggered checks)
  3. else (every check is PASS)        -> Approve
"""
from app.rules import checks as c
from app.schemas import Check, Decision, Routing, RuleContext

RISK_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}

VALIDATE_CHECKS = [c.check_arithmetic, c.check_required_fields, c.check_extraction_confidence,
                   c.check_gst_rate, c.check_invoice_date]

RULE_CHECKS = [c.check_vendor_approved, c.check_vendor_ambiguous, c.check_bank_match, c.check_po_found,
               c.check_po_inferred, c.check_line_match, c.check_amount_tolerance, c.check_cumulative_po,
               c.check_duplicate_exact, c.check_duplicate_near]


def run_validate_checks(ctx: RuleContext) -> list[Check]:
    return [check(ctx) for check in VALIDATE_CHECKS]


def run_rule_checks(ctx: RuleContext) -> list[Check]:
    """If any earlier stage failed, STAGE_FAILED is the only honest result: the data the other
    checks need is missing, so we do not pretend to evaluate them."""
    if ctx.stage_failures:
        return [c.check_stage_failed(ctx)]
    return [check(ctx) for check in RULE_CHECKS] + [c.check_stage_failed(ctx)]


def decide(checks: list[Check]) -> Decision:
    triggered = [k for k in checks if k.triggered]
    rejects = [k for k in triggered if k.result == "FAIL" and k.action == "reject"]
    if rejects:
        outcome = "Reject"
    elif triggered:
        outcome = "Hold"
    else:
        outcome = "Approve"
    risk = max((k.risk for k in triggered), key=RISK_ORDER.get, default="none")
    lead = (rejects or [k for k in triggered if k.result == "FAIL"] or triggered or [None])[0]
    return Decision(decision=outcome, risk_level=risk, triggered_codes=[k.code for k in triggered],
                    top_reason=lead.message if lead else None)


# Routing table: first triggered code in this order decides who gets the exception.
ROUTES = [
    ("BANK_MATCH", "Vendor master / treasury team", "Verify by phone using the number on file, not the invoice"),
    ("DUPLICATE_EXACT", "Vendor", "Tell the vendor this invoice duplicates one already paid"),
    ("VENDOR_APPROVED", "Vendor master / treasury team", "Confirm the vendor's status before any payment"),
    ("STAGE_FAILED", "Manual AP processing", "Process this invoice by hand; the automated run could not finish"),
    ("PO_FOUND", "Procurement (PO owner)", "Confirm the correct PO number with the vendor, or raise the missing PO"),
    ("CUMULATIVE_PO", "Procurement (PO owner)", "Approve the variance or raise a PO amendment"),
    ("AMOUNT_TOLERANCE", "Procurement (PO owner)", "Approve the variance or raise a PO amendment"),
    ("LINE_MATCH", "Procurement (PO owner)", "Approve the variance or raise a PO amendment"),
    ("DUPLICATE_NEAR", "AP reviewer", "Compare against the earlier invoice before releasing"),
    ("PO_INFERRED", "AP reviewer", "Confirm the suggested PO"),
]
DEFAULT_ROUTE = ("AP reviewer", "Review the failed checks and confirm or correct the invoice")


def route(checks: list[Check]) -> Routing | None:
    triggered_checks = [k for k in checks if k.triggered]
    if not triggered_checks:
        return None
    # "Not evaluated" warnings are consequences of a real failure (no PO, no invoice number ...), so they must not
    # decide who gets the exception: route on the real cause when there is one.
    causes = [k for k in triggered_checks if not k.not_evaluated] or triggered_checks
    triggered = {k.code for k in causes}
    for code, team, action in ROUTES:
        if code in triggered:
            return Routing(team=team, action=action)
    return Routing(team=DEFAULT_ROUTE[0], action=DEFAULT_ROUTE[1])
