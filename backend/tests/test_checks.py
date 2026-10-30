"""Every rule in PRD 5.1, tested on its own: a pass case and a fail case (plus edge cases).
Each test starts from a clean scenario context (conftest.make_ctx) and changes one thing."""
from datetime import timedelta
from decimal import Decimal

from app.rules import checks as c
from app.schemas import LineMatch, POMatch, StageFailure, VendorMatch
from conftest import TODAY, make_ctx, tf


# ---------------------------------------------------------------- VENDOR_APPROVED
def test_vendor_approved_pass():
    assert c.check_vendor_approved(make_ctx("H")).result == "PASS"


def test_vendor_approved_fails_and_rejects_when_vendor_unknown():
    r = c.check_vendor_approved(make_ctx("H", vendor=None))
    assert (r.result, r.action) == ("FAIL", "reject")


def test_vendor_approved_fails_and_rejects_when_vendor_blocked():
    ctx = make_ctx("H")
    ctx.vendor.status = "blocked"
    r = c.check_vendor_approved(ctx)
    assert (r.result, r.action) == ("FAIL", "reject")


# ---------------------------------------------------------------- VENDOR_AMBIGUOUS
def test_vendor_ambiguous_pass_when_identified_by_gstin():
    assert c.check_vendor_ambiguous(make_ctx("H")).result == "PASS"


def test_vendor_ambiguous_fails_when_matched_by_name_only():
    ctx = make_ctx("H", vendor_match=VendorMatch(vendor_id="V001", name="Sharma Office Supplies Pvt Ltd",
                                                 method="name", score=0.97, candidates=["Sharma Office Supplies Pvt Ltd"]))
    r = c.check_vendor_ambiguous(ctx)
    assert (r.result, r.action) == ("FAIL", "hold")


# ---------------------------------------------------------------- BANK_MATCH
def test_bank_match_pass():
    assert c.check_bank_match(make_ctx("H")).result == "PASS"


def test_bank_match_fails_high_risk_and_never_shows_full_account_numbers():
    ctx = make_ctx("E4")  # E4's invoice account differs from the master
    r = c.check_bank_match(ctx)
    assert (r.result, r.action, r.risk) == ("FAIL", "hold", "high")
    text = r.message + str(r.evidence)
    assert ctx.invoice.bank_account.value not in text and ctx.vendor.bank_account not in text
    assert "XXXXXX4417" in text  # the master account, masked


def test_bank_match_fails_when_only_ifsc_differs():
    ctx = make_ctx("H")
    ctx.invoice.ifsc.value = "HDFC0009999"
    r = c.check_bank_match(ctx)
    assert r.result == "FAIL" and "IFSC" in r.message


def test_bank_match_warns_when_invoice_has_no_bank_details():
    ctx = make_ctx("H")
    ctx.invoice.bank_account = tf(None)
    assert c.check_bank_match(ctx).result == "WARN"


# ---------------------------------------------------------------- PO_FOUND
def test_po_found_pass():
    assert c.check_po_found(make_ctx("H")).result == "PASS"


def test_po_found_fails_when_printed_po_does_not_exist():
    ctx = make_ctx("H", po=None, po_match=POMatch(po_id=None, method="none", reference="PO-9999"))
    r = c.check_po_found(ctx)
    assert (r.result, r.action) == ("FAIL", "hold")


def test_po_found_fails_when_no_reference_and_nothing_inferred():
    ctx = make_ctx("H", po=None, po_match=POMatch(po_id=None, method="none"))
    assert c.check_po_found(ctx).result == "FAIL"


def test_po_found_rejects_when_po_belongs_to_another_vendor():
    ctx = make_ctx("H")
    ctx.po.vendor_id = "V002"
    r = c.check_po_found(ctx)
    assert (r.result, r.action) == ("FAIL", "reject")


def test_po_found_fails_when_po_is_closed():
    ctx = make_ctx("H")
    ctx.po.status = "closed"
    assert c.check_po_found(ctx).result == "FAIL"


# ---------------------------------------------------------------- PO_INFERRED
def test_po_inferred_pass_when_po_was_printed():
    assert c.check_po_inferred(make_ctx("H")).result == "PASS"


def test_po_inferred_warns_when_po_was_inferred_even_at_high_confidence():
    ctx = make_ctx("E3", po_match=POMatch(po_id="PO-2057", method="inferred", confidence=0.99, reason="lines match"))
    r = c.check_po_inferred(ctx)
    assert (r.result, r.action) == ("WARN", "hold")


# ---------------------------------------------------------------- AMOUNT_TOLERANCE
def test_amount_tolerance_pass():
    assert c.check_amount_tolerance(make_ctx("H")).result == "PASS"


def test_amount_tolerance_pass_for_freight_that_keeps_total_within_two_percent():
    r = c.check_amount_tolerance(make_ctx("E5"))  # +1.3%
    assert r.result == "PASS" and r.evidence["variance_pct"] == Decimal("1.3")


def test_amount_tolerance_fails_over_two_percent():
    ctx = make_ctx("H")
    ctx.invoice.subtotal.value = Decimal("115000.00")  # PO lines are worth 111,460 -> +3.2%
    r = c.check_amount_tolerance(ctx)
    assert r.result == "FAIL"


def test_amount_tolerance_boundary_is_inclusive():
    ctx = make_ctx("H")
    ctx.invoice.subtotal.value = Decimal("111460") * Decimal("1.02")
    assert c.check_amount_tolerance(ctx).result == "PASS"


# ---------------------------------------------------------------- CUMULATIVE_PO
def test_cumulative_po_pass():
    r = c.check_cumulative_po(make_ctx("E2"))  # 66% of PO after this invoice
    assert r.result == "PASS"


def test_cumulative_po_fails_at_108_percent_with_evidence():
    r = c.check_cumulative_po(make_ctx("E1"))
    assert r.result == "FAIL"
    assert r.evidence["utilisation_pct"] == Decimal("108.0")
    assert "₹5,40,000.00 of ₹5,00,000.00 = 108%" in r.message


def test_cumulative_po_exactly_at_limit_passes():
    ctx = make_ctx("E1")
    ctx.approved_to_date = Decimal("345000.00")  # 345,000 + 165,000 = 510,000 = 102% of 500,000
    assert c.check_cumulative_po(ctx).result == "PASS"


def test_cumulative_po_uses_subtotals_not_totals():
    ctx = make_ctx("E1")
    assert ctx.approved_to_date == Decimal("375000.00")  # 200,000 + 175,000: tax excluded


# ---------------------------------------------------------------- LINE_MATCH
def test_line_match_pass():
    assert c.check_line_match(make_ctx("H")).result == "PASS"


def test_line_match_fails_when_a_line_maps_to_no_po_line():
    ctx = make_ctx("H")
    ctx.line_matches[0] = LineMatch(invoice_line_index=0, description="x", po_line_id=None, method="none")
    r = c.check_line_match(ctx)
    assert r.result == "FAIL" and "does not map" in r.message


def test_line_match_fails_when_qty_exceeds_the_po_line():
    ctx = make_ctx("H")
    ctx.invoice.line_items[0].qty = Decimal("301")  # PO line is 300
    assert c.check_line_match(ctx).result == "FAIL"


def test_line_match_does_not_count_other_invoices_against_the_po_line_qty():
    # E1's invoice alone is within every PO line quantity; consumption across invoices is CUMULATIVE_PO's job
    assert c.check_line_match(make_ctx("E1")).result == "PASS"


def test_line_match_fails_when_unit_price_is_more_than_two_percent_off():
    ctx = make_ctx("H")
    ctx.invoice.line_items[0].unit_price = Decimal("252.00")  # PO price 245.00 -> +2.9%
    assert c.check_line_match(ctx).result == "FAIL"


def test_line_match_pass_when_unit_price_is_within_tolerance():
    ctx = make_ctx("H")
    ctx.invoice.line_items[0].unit_price = Decimal("248.00")  # +1.2%
    assert c.check_line_match(ctx).result == "PASS"


def test_line_match_freight_is_exempt():
    assert c.check_line_match(make_ctx("E5")).result == "PASS"  # E5 has a freight charge that matches no PO line


# ---------------------------------------------------------------- DUPLICATE_EXACT
def test_duplicate_exact_pass():
    assert c.check_duplicate_exact(make_ctx("E2")).result == "PASS"  # INV-1042A is not INV-1042


def test_duplicate_exact_fails_and_rejects():
    ctx = make_ctx("E2")
    ctx.invoice.invoice_number.value = "INV-1042"
    r = c.check_duplicate_exact(ctx)
    assert (r.result, r.action) == ("FAIL", "reject")


def test_duplicate_exact_ignores_case_and_punctuation():
    ctx = make_ctx("E2")
    ctx.invoice.invoice_number.value = "inv 1042"
    assert c.check_duplicate_exact(ctx).result == "FAIL"


# ---------------------------------------------------------------- DUPLICATE_NEAR
def test_duplicate_near_pass_when_nothing_similar():
    assert c.check_duplicate_near(make_ctx("H")).result == "PASS"


def test_duplicate_near_fails_for_inv_1042a():
    r = c.check_duplicate_near(make_ctx("E2"))
    assert (r.result, r.action) == ("FAIL", "hold")
    assert r.evidence["similar_to"] == "INV-1042" and r.evidence["days_apart"] == 6


def test_duplicate_near_pass_when_dates_are_far_apart():
    ctx = make_ctx("E2")
    ctx.invoice.invoice_date.value = ctx.invoice.invoice_date.value + timedelta(days=40)
    assert c.check_duplicate_near(ctx).result == "PASS"


def test_duplicate_near_pass_when_total_differs_by_more_than_half_a_percent():
    ctx = make_ctx("E2")
    ctx.invoice.total.value = ctx.invoice.total.value * Decimal("1.01")
    assert c.check_duplicate_near(ctx).result == "PASS"


def test_duplicate_near_pass_when_invoice_numbers_are_different():
    ctx = make_ctx("E2")
    ctx.invoice.invoice_number.value = "ZX-9981-Q"
    assert c.check_duplicate_near(ctx).result == "PASS"


# ---------------------------------------------------------------- ARITHMETIC
def test_arithmetic_pass_including_freight():
    assert c.check_arithmetic(make_ctx("E5")).result == "PASS"


def test_arithmetic_fails_when_lines_do_not_add_to_subtotal():
    ctx = make_ctx("H")
    ctx.invoice.subtotal.value += Decimal("50")
    r = c.check_arithmetic(ctx)
    assert r.result == "FAIL" and "subtotal" in r.message


def test_arithmetic_tolerates_one_rupee_rounding():
    ctx = make_ctx("H")
    ctx.invoice.total.value += Decimal("0.80")
    assert c.check_arithmetic(ctx).result == "PASS"


def test_arithmetic_fails_when_total_is_not_subtotal_plus_tax():
    ctx = make_ctx("H")
    ctx.invoice.total.value += Decimal("5")
    assert c.check_arithmetic(ctx).result == "FAIL"


# ---------------------------------------------------------------- REQUIRED_FIELDS
def test_required_fields_pass():
    assert c.check_required_fields(make_ctx("H")).result == "PASS"


def test_required_fields_fails_when_invoice_number_missing():
    ctx = make_ctx("H")
    ctx.invoice.invoice_number = tf(None)
    r = c.check_required_fields(ctx)
    assert r.result == "FAIL" and "invoice number" in r.message


def test_required_fields_accepts_vendor_name_without_gstin():
    ctx = make_ctx("H")
    ctx.invoice.vendor_gstin = tf(None)
    assert c.check_required_fields(ctx).result == "PASS"


def test_required_fields_fails_when_total_missing():
    ctx = make_ctx("H")
    ctx.invoice.total.value = None
    assert c.check_required_fields(ctx).result == "FAIL"


# ---------------------------------------------------------------- EXTRACTION_CONFIDENCE
def test_extraction_confidence_pass():
    assert c.check_extraction_confidence(make_ctx("H")).result == "PASS"


def test_extraction_confidence_warns_below_minimum():
    ctx = make_ctx("H")
    ctx.invoice.vendor_gstin.confidence = 0.70
    r = c.check_extraction_confidence(ctx)
    assert (r.result, r.action) == ("WARN", "hold") and "vendor_gstin" in r.message


def test_extraction_confidence_ignores_null_optional_fields():
    ctx = make_ctx("E3")  # no PO printed: po_reference is null with confidence 0
    assert c.check_extraction_confidence(ctx).result == "PASS"


def test_extraction_confidence_boundary_is_inclusive():
    ctx = make_ctx("H")
    ctx.invoice.total.confidence = 0.80
    assert c.check_extraction_confidence(ctx).result == "PASS"


# ---------------------------------------------------------------- GST_RATE and INVOICE_DATE (Validate stage)
def test_gst_rate_pass_for_18_percent_igst_and_cgst_sgst():
    assert c.check_gst_rate(make_ctx("H")).result == "PASS"      # IGST 18%
    assert c.check_gst_rate(make_ctx("E1")).result == "PASS"     # CGST 9% + SGST 9%


def test_gst_rate_warns_for_non_slab_rate():
    ctx = make_ctx("H")
    ctx.invoice.tax_lines[0].amount = Decimal("19000.00")  # 17.05% of 111,460
    assert c.check_gst_rate(ctx).result == "WARN"


def test_invoice_date_pass():
    assert c.check_invoice_date(make_ctx("H")).result == "PASS"


def test_invoice_date_warns_when_older_than_180_days():
    ctx = make_ctx("H")
    ctx.invoice.invoice_date.value = TODAY - timedelta(days=181)
    assert c.check_invoice_date(ctx).result == "WARN"


def test_invoice_date_fails_when_in_the_future():
    ctx = make_ctx("H")
    ctx.invoice.invoice_date.value = TODAY + timedelta(days=1)
    assert c.check_invoice_date(ctx).result == "FAIL"


# ---------------------------------------------------------------- STAGE_FAILED
def test_stage_failed_pass_when_nothing_failed():
    assert c.check_stage_failed(make_ctx("H")).result == "PASS"


def test_stage_failed_fails_and_names_the_stage():
    r = c.check_stage_failed(make_ctx("H", stage_failures=[StageFailure(stage="extract", error="timeout")]))
    assert (r.result, r.action) == ("FAIL", "hold") and "extract" in r.message


# ---------------------------------------------------------------- uncertainty never passes
def test_checks_that_cannot_be_evaluated_warn_instead_of_passing():
    ctx = make_ctx("H", po=None, vendor=None)
    for check in (c.check_bank_match, c.check_amount_tolerance, c.check_cumulative_po, c.check_line_match):
        assert check(ctx).result == "WARN", check.__name__
