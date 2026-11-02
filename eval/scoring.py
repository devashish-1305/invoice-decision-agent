"""Scoring for the evaluation: pure functions with no I/O and no LLM, so tests can exercise them directly.

Extraction and decisions are scored separately: a correct decision does not show every field was read correctly.
"""
import re
from decimal import Decimal

from app.util import mask_text

FIELDS = ["vendor_gstin", "invoice_number", "invoice_date", "po_reference", "bank_account", "subtotal", "tax",
          "total", "line_items"]
FAILED = "not extracted (stage failed)"


# ----------------------------------------------------------------------------- extraction
def _shown(value) -> str:
    """A value as it may appear in a report: anything that looks like an account number is masked."""
    return "null" if value is None else repr(mask_text(str(value))[:80])


def _field(ok: bool, read, want) -> dict:
    return {"ok": ok, "detail": "" if ok else f"read {_shown(read)}, invoice has {_shown(want)}"}


def _text_field(read, want) -> dict:
    """Exact, apart from surrounding spaces. Null is correct only when the document has no value."""
    read = read.strip() if isinstance(read, str) else read
    return _field(read == want, read, want)


def _same_money(read, want) -> bool:
    if read is None or want is None:
        return read is None and want is None
    return Decimal(str(read)) == Decimal(str(want))


def _money_field(read, want) -> dict:
    return _field(_same_money(read, want), read, want)


def _same_description(read: str, want: str) -> bool:
    """Case and whitespace are ignored (a wrapped table cell changes spacing); every word must match."""
    return " ".join(read.split()).casefold() == " ".join(want.split()).casefold()


def _bank_field(read, full_account: str) -> dict:
    """Compares the FULL numbers, because masked ones can agree on the last four digits and still differ.
    The result carries no digits at all: only match / no match."""
    compact = re.sub(r"[\s-]", "", read) if read else None
    ok = compact == full_account
    return {"ok": ok, "detail": "" if ok else "no match"}


def _tax_field(tax_lines: list, truth: dict) -> dict:
    """Each printed tax line must be read: CGST + SGST reported as one summed line is not a correct reading."""
    read = [line.amount for line in tax_lines]
    want = [Decimal(line["amount"]) for line in truth["tax_lines"]]
    ok = None not in read and sorted(Decimal(str(a)) for a in read) == sorted(want)
    return _field(ok, read, [str(a) for a in want])


def _lines_field(invoice, truth: dict) -> tuple[dict, int, int]:
    """All or nothing per invoice (every product line and every charge), plus a per-cell count.
    Returns (field result, cells correct, cells total). Extra or missing lines count against the total."""
    problems, correct = [], 0
    want_lines, got_lines = truth["line_items"], invoice.line_items
    total = max(len(want_lines), len(got_lines)) * 4
    for i, (got, want) in enumerate(zip(got_lines, want_lines), 1):
        checks = [("description", _same_description(got.description, want["description"]), got.description, want["description"]),
                  ("qty", _same_money(got.qty, want["qty"]), got.qty, want["qty"]),
                  ("unit price", _same_money(got.unit_price, want["unit_price"]), got.unit_price, want["unit_price"]),
                  ("amount", _same_money(got.amount, want["amount"]), got.amount, want["amount"])]
        for name, ok, read, expected in checks:
            correct += ok
            if not ok:
                problems.append(f"line {i} {name}: read {_shown(read)}, invoice has {_shown(expected)}")
    if len(got_lines) != len(want_lines):
        problems.append(f"read {len(got_lines)} lines, invoice has {len(want_lines)}")

    want_charges, got_charges = truth["other_charges"], invoice.other_charges
    total += max(len(want_charges), len(got_charges)) * 2
    for i, (got, want) in enumerate(zip(got_charges, want_charges), 1):
        checks = [("description", _same_description(got.description, want["description"]), got.description, want["description"]),
                  ("amount", _same_money(got.amount, want["amount"]), got.amount, want["amount"])]
        for name, ok, read, expected in checks:
            correct += ok
            if not ok:
                problems.append(f"charge {i} {name}: read {_shown(read)}, invoice has {_shown(expected)}")
    if len(got_charges) != len(want_charges):
        problems.append(f"read {len(got_charges)} charges, invoice has {len(want_charges)}")
    return {"ok": not problems, "detail": "; ".join(problems)}, correct, total


def score_extraction(truth: dict, full_account: str, invoice) -> dict:
    """invoice is the parsed ExtractedInvoice, or None when extraction failed. A failed extraction is never left
    out: every field counts as wrong and every cell as missed."""
    if invoice is None:
        cells = len(truth["line_items"]) * 4 + len(truth["other_charges"]) * 2
        return {"fields": {name: {"ok": False, "detail": FAILED} for name in FIELDS}, "cells": (0, cells)}
    lines, correct, total = _lines_field(invoice, truth)
    date_read = invoice.invoice_date.value.isoformat() if invoice.invoice_date.value else None
    return {"cells": (correct, total), "fields": {
        "vendor_gstin": _text_field(invoice.vendor_gstin.value, truth["vendor_gstin"]),
        "invoice_number": _text_field(invoice.invoice_number.value, truth["invoice_number"]),
        "invoice_date": _text_field(date_read, truth["invoice_date"]),
        "po_reference": _text_field(invoice.po_reference.value, truth["po_reference"]),
        "bank_account": _bank_field(invoice.bank_account.value, full_account),
        "subtotal": _money_field(invoice.subtotal.value, truth["subtotal"]),
        "tax": _tax_field(invoice.tax_lines, truth),
        "total": _money_field(invoice.total.value, truth["total"]),
        "line_items": lines}}


# ----------------------------------------------------------------------------- decision
def score_decision(expected: dict, actual: str | None, risk: str | None, triggered: list[str],
                   not_evaluated: list[str], failed_stages: list[str]) -> dict:
    """decision_ok: the outcome (and risk, where the generator states one) is right AND it did not come from a
    failed stage. A Hold caused by an outage is not a correct Hold. A failed `explain` does not count here: it
    runs after the decision is committed and cannot change it.

    check_ok: the intended check fired and nothing unexplained fired beside it. "Not evaluated" warnings are
    consequences of the intended failure (no PO, no invoice number), so they are allowed."""
    upstream_failed = [s for s in failed_stages if s != "explain"]
    decision_ok = not upstream_failed and actual == expected["decision"] and (
        expected["risk_level"] is None or risk == expected["risk_level"])
    allowed = set(not_evaluated) | set(expected["also_allowed"])
    if expected["check"] is None:
        check_ok = not triggered and not upstream_failed
    else:
        check_ok = (not upstream_failed and expected["check"] in triggered
                    and not set(triggered) - allowed - {expected["check"]})
    return {"decision_ok": decision_ok, "check_ok": check_ok}


# ----------------------------------------------------------------------------- totals
def totals(rows: list[dict]) -> dict:
    """Every row counts, including failed runs. rows: dicts with keys status, fields, cells, decision_ok, check_ok,
    expected_decision, actual_decision."""
    per_field = {name: sum(r["fields"][name]["ok"] for r in rows) for name in FIELDS}
    fields_correct = sum(per_field.values())
    return {
        "invoices": len(rows),
        "runs_ok": sum(r["status"] == "completed" for r in rows),
        "runs_failed": sum(r["status"] != "completed" for r in rows),
        "per_field": per_field,
        "fields_correct": fields_correct,
        "fields_total": len(FIELDS) * len(rows),
        "all_fields_right": sum(all(f["ok"] for f in r["fields"].values()) for r in rows),
        "cells_correct": sum(r["cells"][0] for r in rows),
        "cells_total": sum(r["cells"][1] for r in rows),
        "decisions_ok": sum(r["decision_ok"] for r in rows),
        "decisions_and_checks_ok": sum(r["decision_ok"] and r["check_ok"] for r in rows),
        "false_approvals": sum(r["actual_decision"] == "Approve" and r["expected_decision"] != "Approve" for r in rows),
        "false_holds": sum(r["actual_decision"] == "Hold" and r["expected_decision"] == "Approve" for r in rows),
    }


def usage_text(calls: list[dict], key: str) -> str:
    """Token total from API usage metadata only. Never estimated, and a missing value is never read as zero."""
    known = [c[key] for c in calls if c[key] is not None]
    if not calls or not known:
        return "unavailable"
    if len(known) < len(calls):
        return f"{sum(known):,} (partial: {len(known)} of {len(calls)} calls reported usage)"
    return f"{sum(known):,}"


# ----------------------------------------------------------------------------- safety
def assert_safe(report: str, secrets: list[str]) -> None:
    """Refuse to write a report containing a full account number, an API key or any account-length digit run."""
    if any(s and s in report for s in secrets):
        raise ValueError("report contains a secret value; not written")
    if re.search(r"sk-ant-|(?<![\w])\d{9,}(?![\w])", report):
        raise ValueError("report contains something that looks like an API key or account number; not written")
