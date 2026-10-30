"""One pure function per check in PRD section 5.1.

Rules: no I/O, no LLM, no clock (the date comes from ctx.today), money is Decimal, every threshold
comes from ctx.settings. Same context in -> same Check out.

A check that cannot be evaluated because something upstream is missing returns WARN
("Not evaluated: ..."), never PASS: uncertainty holds, it never approves.
Bank account numbers are masked in every message and evidence dict; the full numbers are only
touched inside check_bank_match's comparison.
"""
from collections import defaultdict
from decimal import Decimal

from app.schemas import CRITICAL_FIELDS, Check, ExtractedLine, RuleContext
from app.util import edit_ratio, inr, mask_account, norm_id

PERCENT = Decimal(100)


# ---------------------------------------------------------------- helpers
def _check(code, result, message, evidence=None, action=None, risk="low", stage="rules") -> Check:
    if result != "PASS" and action is None:
        action = "hold"
    return Check(code=code, result=result, message=message, evidence=evidence or {}, action=action,
                 risk=risk, stage=stage)


def _pass(code, message, evidence=None, stage="rules") -> Check:
    return _check(code, "PASS", message, evidence, stage=stage)


def _not_evaluated(code, why, stage="rules") -> Check:
    return _check(code, "WARN", f"Not evaluated: {why}", stage=stage)


def _num(d: Decimal) -> str:
    """800.00 -> '800' (for readable messages)."""
    return format(d.normalize(), "f")


def _compact(s: str) -> str:
    return "".join(s.split()).replace("-", "").upper()


def _unit_price(line: ExtractedLine) -> Decimal | None:
    if line.unit_price is not None:
        return line.unit_price
    if line.amount is not None and line.qty:
        return line.amount / line.qty
    return None


def _utilisation(consumed: Decimal, po_total: Decimal) -> Decimal:
    return (consumed / po_total * PERCENT).quantize(Decimal("0.1"))


# ---------------------------------------------------------------- vendor
def check_vendor_approved(ctx: RuleContext) -> Check:
    inv = ctx.invoice
    gstin = inv.vendor_gstin.value if inv else None
    name = inv.vendor_name.value if inv else None
    if ctx.vendor is None:
        return _check("VENDOR_APPROVED", "FAIL",
                      f"No vendor in the master matches GSTIN {gstin or '(not on invoice)'} or the name on the invoice",
                      {"invoice_gstin": gstin, "invoice_vendor_name": name}, action="reject", risk="high")
    if ctx.vendor.status != "active":
        return _check("VENDOR_APPROVED", "FAIL", f"{ctx.vendor.name} is not active (status: {ctx.vendor.status})",
                      {"vendor_id": ctx.vendor.vendor_id, "status": ctx.vendor.status}, action="reject", risk="high")
    return _pass("VENDOR_APPROVED", f"{ctx.vendor.name} is an active vendor",
                 {"vendor_id": ctx.vendor.vendor_id, "status": ctx.vendor.status})


def check_vendor_ambiguous(ctx: RuleContext) -> Check:
    vm = ctx.vendor_match
    if vm is None:
        return _not_evaluated("VENDOR_AMBIGUOUS", "vendor matching did not run")
    if vm.method == "name" or len(vm.candidates) > 1:
        gstin = ctx.invoice.vendor_gstin.value if ctx.invoice else None
        return _check("VENDOR_AMBIGUOUS", "FAIL",
                      f"Vendor not confirmed by GSTIN (invoice GSTIN: {gstin or 'none'}); closest name match is "
                      f"{vm.name or ', '.join(vm.candidates)}. A name match alone is never enough to approve.",
                      {"invoice_gstin": gstin, "suggested_vendor": vm.name, "candidates": vm.candidates,
                       "name_similarity": vm.score}, risk="medium")
    return _pass("VENDOR_AMBIGUOUS", "Vendor identified by exact GSTIN match" if vm.method == "gstin"
                 else "No similar vendor names to confuse")


def check_bank_match(ctx: RuleContext) -> Check:
    if ctx.vendor is None or ctx.invoice is None:
        return _not_evaluated("BANK_MATCH", "vendor is unknown")
    account, ifsc = ctx.invoice.bank_account.value, ctx.invoice.ifsc.value
    if not account or not ifsc:
        return _check("BANK_MATCH", "WARN", "Invoice has no complete bank details, so they cannot be verified "
                      "against the vendor master",
                      {"invoice_account": mask_account(account), "invoice_ifsc": ifsc}, risk="medium")
    evidence = {"invoice_account": mask_account(account), "master_account": mask_account(ctx.vendor.bank_account),
                "invoice_ifsc": ifsc, "master_ifsc": ctx.vendor.ifsc}
    same_account = _compact(account) == _compact(ctx.vendor.bank_account)
    same_ifsc = _compact(ifsc) == _compact(ctx.vendor.ifsc)
    if same_account and same_ifsc:
        return _pass("BANK_MATCH", "Bank account and IFSC match the vendor master", evidence)
    differs = " and ".join(n for n, same in (("account", same_account), ("IFSC", same_ifsc)) if not same)
    return _check("BANK_MATCH", "FAIL",
                  f"Bank {differs} on the invoice differs from the vendor master "
                  f"({evidence['invoice_account']} vs {evidence['master_account']} on file). High fraud risk.",
                  evidence, risk="high")


# ---------------------------------------------------------------- purchase order
def check_po_found(ctx: RuleContext) -> Check:
    pm = ctx.po_match
    if pm is None:
        return _not_evaluated("PO_FOUND", "PO matching did not run")
    if ctx.po is None:
        if pm.reference:
            return _check("PO_FOUND", "FAIL", f"PO {pm.reference} on the invoice does not exist",
                          {"po_reference": pm.reference})
        return _check("PO_FOUND", "FAIL", "Invoice has no PO reference and no open PO of this vendor could be "
                      "inferred", {"po_reference": None, "ranking": [c.model_dump() for c in pm.ranking]})
    if ctx.vendor is None:
        return _not_evaluated("PO_FOUND", "vendor is unknown, so PO ownership cannot be checked")
    evidence = {"po_id": ctx.po.po_id, "po_vendor_id": ctx.po.vendor_id, "invoice_vendor_id": ctx.vendor.vendor_id,
                "po_status": ctx.po.status}
    if ctx.po.vendor_id != ctx.vendor.vendor_id:
        return _check("PO_FOUND", "FAIL", f"{ctx.po.po_id} belongs to a different vendor", evidence,
                      action="reject", risk="high")
    if ctx.po.status != "open":
        return _check("PO_FOUND", "FAIL", f"{ctx.po.po_id} is {ctx.po.status}, not open", evidence)
    return _pass("PO_FOUND", f"{ctx.po.po_id} exists, is open and belongs to {ctx.vendor.name}", evidence)


def check_po_inferred(ctx: RuleContext) -> Check:
    pm = ctx.po_match
    if pm is None:
        return _not_evaluated("PO_INFERRED", "PO matching did not run")
    if pm.method == "inferred":
        return _check("PO_INFERRED", "WARN",
                      f"No PO on the invoice; the best match is {pm.po_id} (confidence {pm.confidence:.0%}). "
                      "Inferred POs always need human confirmation.",
                      {"suggested_po": pm.po_id, "confidence": pm.confidence, "reason": pm.reason})
    return _pass("PO_INFERRED", "PO was stated on the invoice" if pm.method == "explicit" else "No PO inferred")


def _matched_po_lines(ctx: RuleContext) -> dict:
    by_id = {l.po_line_id: l for l in ctx.po.lines}
    return {m.po_line_id: by_id[m.po_line_id] for m in ctx.line_matches if m.po_line_id in by_id}


def check_amount_tolerance(ctx: RuleContext) -> Check:
    subtotal = ctx.invoice.subtotal.value if ctx.invoice else None
    if ctx.po is None or subtotal is None:
        return _not_evaluated("AMOUNT_TOLERANCE", "no PO or no invoice subtotal")
    matched_value = sum((l.value for l in _matched_po_lines(ctx).values()), Decimal(0))
    if matched_value == 0:
        return _not_evaluated("AMOUNT_TOLERANCE", "no invoice line matched a PO line")
    tolerance = ctx.settings.tolerance_pct
    limit = matched_value * (1 + tolerance / PERCENT)
    variance = ((subtotal - matched_value) / matched_value * PERCENT).quantize(Decimal("0.1"))
    evidence = {"invoice_subtotal": subtotal, "matched_po_lines_value": matched_value, "limit": limit,
                "variance_pct": variance, "tolerance_pct": tolerance}
    text = f"{inr(subtotal)} invoiced vs {inr(matched_value)} on the matched PO lines ({variance:+}%, allowed +{tolerance}%)"
    if subtotal <= limit:
        return _pass("AMOUNT_TOLERANCE", text, evidence)
    return _check("AMOUNT_TOLERANCE", "FAIL", text, evidence, risk="medium")


def check_cumulative_po(ctx: RuleContext) -> Check:
    subtotal = ctx.invoice.subtotal.value if ctx.invoice else None
    if ctx.po is None or subtotal is None:
        return _not_evaluated("CUMULATIVE_PO", "no PO or no invoice subtotal")
    po_total = ctx.po.subtotal
    if po_total == 0:
        return _not_evaluated("CUMULATIVE_PO", "PO has no value")
    after = ctx.approved_to_date + subtotal
    tolerance = ctx.settings.tolerance_pct
    limit = po_total * (1 + tolerance / PERCENT)
    utilisation = _utilisation(after, po_total)
    evidence = {"approved_to_date": ctx.approved_to_date, "invoice_subtotal": subtotal, "total_after": after,
                "po_subtotal": po_total, "utilisation_pct": utilisation, "limit": limit}
    text = (f"{inr(after)} of {inr(po_total)} = {_num(utilisation)}% of PO {ctx.po.po_id} "
            f"({inr(ctx.approved_to_date)} already approved + {inr(subtotal)} this invoice; "
            f"allowed up to {_num(PERCENT + tolerance)}%)")
    if after <= limit:
        return _pass("CUMULATIVE_PO", text, evidence)
    return _check("CUMULATIVE_PO", "FAIL", text, evidence, risk="medium")


def check_line_match(ctx: RuleContext) -> Check:
    if ctx.po is None or ctx.invoice is None:
        return _not_evaluated("LINE_MATCH", "no PO to match lines against")
    if not ctx.invoice.line_items:
        return _check("LINE_MATCH", "FAIL", "Invoice has no product lines to match against the PO", risk="medium")
    po_lines = {l.po_line_id: l for l in ctx.po.lines}
    by_index = {m.invoice_line_index: m for m in ctx.line_matches}
    price_tolerance = ctx.settings.price_tolerance_pct
    issues, rows = [], []
    qty_used: dict[str, Decimal] = defaultdict(Decimal)
    for i, line in enumerate(ctx.invoice.line_items):
        match = by_index.get(i)
        po_line = po_lines.get(match.po_line_id) if match and match.po_line_id else None
        if po_line is None:
            issues.append(f"'{line.description}' does not map to any PO line")
            rows.append({"invoice_line": line.description, "po_line": None})
            continue
        price = _unit_price(line)
        if price is None:
            issues.append(f"'{line.description}': unit price not readable")
        else:
            off = abs(price - po_line.unit_price) / po_line.unit_price * PERCENT
            if off > price_tolerance:
                issues.append(f"'{line.description}': unit price {inr(price)} vs PO {inr(po_line.unit_price)} "
                              f"({off.quantize(Decimal('0.1'))}% off, allowed {price_tolerance}%)")
        if line.qty is None:
            issues.append(f"'{line.description}': quantity not readable")
        else:
            qty_used[po_line.po_line_id] += line.qty
        rows.append({"invoice_line": line.description, "po_line": po_line.po_line_id, "method": match.method,
                     "qty": line.qty, "po_qty": po_line.qty, "unit_price": price, "po_unit_price": po_line.unit_price})
    for po_line_id, qty in qty_used.items():
        if qty > po_lines[po_line_id].qty:
            issues.append(f"{po_lines[po_line_id].description}: invoiced qty {_num(qty)} exceeds PO qty "
                          f"{_num(po_lines[po_line_id].qty)}")
    evidence = {"lines": rows, "issues": issues}
    if issues:
        return _check("LINE_MATCH", "FAIL", "; ".join(issues), evidence, risk="medium")
    return _pass("LINE_MATCH", f"All {len(rows)} product lines map to PO lines within quantity and price limits", evidence)


# ---------------------------------------------------------------- duplicates
def check_duplicate_exact(ctx: RuleContext) -> Check:
    inv = ctx.invoice
    number = inv.invoice_number.value if inv else None
    if ctx.vendor is None or not number:
        return _not_evaluated("DUPLICATE_EXACT", "vendor or invoice number is unknown")
    for h in ctx.vendor_approved_invoices:
        if norm_id(h.invoice_number) == norm_id(number):
            return _check("DUPLICATE_EXACT", "FAIL",
                          f"Invoice {number} duplicates {h.invoice_number}, already approved on {h.approved_on}",
                          {"duplicate_of": h.invoice_number, "approved_on": h.approved_on, "total": h.total},
                          action="reject", risk="medium")
    return _pass("DUPLICATE_EXACT", f"Invoice {number} has not been approved before for this vendor")


def check_duplicate_near(ctx: RuleContext) -> Check:
    inv = ctx.invoice
    if (ctx.vendor is None or inv is None or not inv.invoice_number.value
            or inv.total.value is None or inv.invoice_date.value is None):
        return _not_evaluated("DUPLICATE_NEAR", "vendor, invoice number, date or total is unknown")
    s = ctx.settings
    number = norm_id(inv.invoice_number.value)
    for h in ctx.vendor_approved_invoices:
        if norm_id(h.invoice_number) == number or h.total == 0:
            continue  # identical numbers are DUPLICATE_EXACT's job
        total_diff = abs(inv.total.value - h.total) / h.total * PERCENT
        days_apart = abs((inv.invoice_date.value - h.invoice_date).days)
        similarity = edit_ratio(number, norm_id(h.invoice_number))
        if total_diff <= s.near_dup_total_pct and days_apart <= s.near_dup_days and similarity <= s.near_dup_max_edit_ratio:
            return _check("DUPLICATE_NEAR", "FAIL",
                          f"Probable duplicate of {h.invoice_number} (approved {h.approved_on}): total within "
                          f"{total_diff.quantize(Decimal('0.01'))}%, {days_apart} days apart, similar invoice number. "
                          "Could be legitimate, so a human confirms.",
                          {"similar_to": h.invoice_number, "its_total": h.total, "this_total": inv.total.value,
                           "total_diff_pct": total_diff, "days_apart": days_apart,
                           "number_difference": round(similarity, 2)}, risk="medium")
    return _pass("DUPLICATE_NEAR", "No approved invoice from this vendor is close in number, total and date")


# ---------------------------------------------------------------- validate-stage checks
def check_arithmetic(ctx: RuleContext) -> Check:
    inv = ctx.invoice
    if inv.subtotal.value is None or inv.total.value is None:
        return _not_evaluated("ARITHMETIC", "subtotal or total is missing", "validate")
    tolerance = ctx.settings.arithmetic_tolerance
    problems, line_sum = [], Decimal(0)
    for i, line in enumerate(inv.line_items, 1):
        amount = line.amount if line.amount is not None else (
            line.qty * line.unit_price if line.qty is not None and line.unit_price is not None else None)
        if amount is None:
            problems.append(f"line {i} has no readable amount")
        else:
            line_sum += amount
    for charge in inv.other_charges:
        if charge.amount is None:
            problems.append(f"charge '{charge.description}' has no readable amount")
        else:
            line_sum += charge.amount
    tax = sum((t.amount for t in inv.tax_lines if t.amount is not None), Decimal(0))
    evidence = {"lines_plus_charges": line_sum, "subtotal": inv.subtotal.value, "tax": tax, "total": inv.total.value}
    if not problems and abs(line_sum - inv.subtotal.value) > tolerance:
        problems.append(f"lines + charges = {inr(line_sum)} but subtotal = {inr(inv.subtotal.value)}")
    if not problems and abs(inv.subtotal.value + tax - inv.total.value) > tolerance:
        problems.append(f"subtotal {inr(inv.subtotal.value)} + tax {inr(tax)} = {inr(inv.subtotal.value + tax)} "
                        f"but total = {inr(inv.total.value)}")
    if problems:
        return _check("ARITHMETIC", "FAIL", "; ".join(problems), evidence, stage="validate")
    return _pass("ARITHMETIC", f"Lines + charges = subtotal and subtotal + tax = total (±{inr(tolerance)})",
                 evidence, "validate")


def check_required_fields(ctx: RuleContext) -> Check:
    inv = ctx.invoice
    missing = []
    if not (inv.vendor_name.value or inv.vendor_gstin.value):
        missing.append("vendor (name or GSTIN)")
    for label, field in (("invoice number", inv.invoice_number), ("invoice date", inv.invoice_date),
                         ("subtotal", inv.subtotal), ("total", inv.total)):
        if field.value is None:
            missing.append(label)
    if missing:
        return _check("REQUIRED_FIELDS", "FAIL", f"Missing on the invoice: {', '.join(missing)}",
                      {"missing": missing}, stage="validate")
    return _pass("REQUIRED_FIELDS", "Vendor, invoice number, date, subtotal and total are all present",
                 stage="validate")


def check_extraction_confidence(ctx: RuleContext) -> Check:
    inv, minimum = ctx.invoice, ctx.settings.confidence_min
    low = {name: getattr(inv, name).confidence for name in CRITICAL_FIELDS
           if getattr(inv, name).value is not None and getattr(inv, name).confidence < minimum}
    if low:
        listing = ", ".join(f"{n} ({c:.2f})" for n, c in low.items())
        return _check("EXTRACTION_CONFIDENCE", "WARN",
                      f"Low extraction confidence (minimum {minimum:.2f}): {listing}",
                      {"low_confidence_fields": low, "minimum": minimum}, stage="validate")
    return _pass("EXTRACTION_CONFIDENCE", f"Every critical field was read with confidence >= {minimum:.2f}",
                 stage="validate")


def check_gst_rate(ctx: RuleContext) -> Check:
    inv, s = ctx.invoice, ctx.settings
    subtotal, total = inv.subtotal.value, inv.total.value
    if not subtotal or total is None:
        return _not_evaluated("GST_RATE", "subtotal or total is missing", "validate")
    stated = [t.amount for t in inv.tax_lines if t.amount is not None]
    tax = sum(stated, Decimal(0)) if stated else total - subtotal
    rate = (tax / subtotal * PERCENT).quantize(Decimal("0.01"))
    evidence = {"effective_rate_pct": rate, "allowed_rates_pct": [str(r) for r in s.valid_gst_rates]}
    if any(abs(rate - allowed) <= s.gst_rate_tolerance_pct for allowed in s.valid_gst_rates):
        return _pass("GST_RATE", f"Effective GST rate {_num(rate)}% is a valid slab", evidence, "validate")
    return _check("GST_RATE", "WARN", f"Effective GST rate {_num(rate)}% is not one of "
                  f"{', '.join(str(r) for r in s.valid_gst_rates)}%", evidence, stage="validate")


def check_invoice_date(ctx: RuleContext) -> Check:
    invoice_date, s = ctx.invoice.invoice_date.value, ctx.settings
    if invoice_date is None:
        return _not_evaluated("INVOICE_DATE", "invoice date is missing", "validate")
    age = (ctx.today - invoice_date).days
    evidence = {"invoice_date": invoice_date, "age_days": age}
    if age < 0:
        return _check("INVOICE_DATE", "FAIL", f"Invoice is dated {invoice_date}, in the future", evidence, stage="validate")
    if age > s.stale_invoice_days:
        return _check("INVOICE_DATE", "WARN", f"Invoice is {age} days old (limit {s.stale_invoice_days})",
                      evidence, stage="validate")
    return _pass("INVOICE_DATE", f"Invoice dated {invoice_date} ({age} days old)", evidence, "validate")


# ---------------------------------------------------------------- failed stages
def check_stage_failed(ctx: RuleContext) -> Check:
    failures = ctx.stage_failures
    if not failures:
        return _pass("STAGE_FAILED", "Every pipeline stage completed")
    names = ", ".join(f.stage for f in failures)
    return _check("STAGE_FAILED", "FAIL", f"Stage failed: {names}. Routed to manual processing; "
                  "this invoice can never be approved automatically.",
                  {"failures": [f.model_dump() for f in failures]}, risk="medium")
