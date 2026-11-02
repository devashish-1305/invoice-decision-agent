"""The evaluation's scoring (eval/). No LLM and no database: this only proves the scoring counts things honestly."""
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import generate_invoices as gen  # noqa: E402
import generate_test_inputs as inputs  # noqa: E402
from conftest import extracted_from  # noqa: E402
from eval import ground_truth  # noqa: E402
from eval.scoring import (FAILED, FIELDS, assert_safe, score_decision, score_extraction, totals,  # noqa: E402
                          usage_text)

CASES = ground_truth.build()
INVOICES = {**{s["id"]: gen.build_invoice(s) for s in gen.SCENARIOS},
            **{i.spec["id"]: i for i in inputs.valid_invoices().values()}}
ACCOUNTS = ground_truth.full_accounts()
BY_ID = {c["id"]: c for c in CASES}


def perfect(case_id):
    return extracted_from(INVOICES[case_id])


def row(case_id="H", invoice="perfect", **overrides):
    case = BY_ID[case_id]
    scored = score_extraction(case["truth"], ACCOUNTS[case_id], perfect(case_id) if invoice == "perfect" else invoice)
    return {"status": "completed", "decision_ok": True, "check_ok": True, "expected_decision": case["expected"]["decision"],
            "actual_decision": case["expected"]["decision"], **scored, **overrides}


def test_there_are_nine_labelled_invoices_and_the_frozen_file_matches_the_generators():
    assert [c["id"] for c in CASES] == ["H", "E1", "E2", "E3", "E4", "E5", "T1", "T2", "T3"]
    assert ground_truth.load_frozen() == CASES


def test_labels_carry_the_expected_results():
    expected = {c["id"]: (c["expected"]["decision"], c["expected"]["check"], c["expected"]["risk_level"]) for c in CASES}
    assert expected == {
        "H": ("Approve", None, "none"), "E1": ("Hold", "CUMULATIVE_PO", None), "E2": ("Hold", "DUPLICATE_NEAR", None),
        "E3": ("Hold", "PO_INFERRED", None), "E4": ("Hold", "BANK_MATCH", "high"), "E5": ("Approve", None, "none"),
        "T1": ("Hold", "REQUIRED_FIELDS", None), "T2": ("Hold", "PO_FOUND", None), "T3": ("Hold", "BANK_MATCH", "high")}
    assert BY_ID["E3"]["expected"]["inferred_po"] == "PO-2057" and BY_ID["E3"]["truth"]["po_reference"] is None
    assert BY_ID["T1"]["truth"]["invoice_number"] is None


def test_the_frozen_labels_contain_no_full_bank_account():
    text = ground_truth.GROUND_TRUTH.read_text(encoding="utf-8")
    assert not any(account in text for account in ACCOUNTS.values())


@pytest.mark.parametrize("case_id", BY_ID)
def test_a_perfect_reading_scores_every_field_correct(case_id):
    scored = row(case_id)
    assert all(f["ok"] for f in scored["fields"].values())
    assert scored["cells"][0] == scored["cells"][1] > 0


def test_bank_accounts_are_compared_in_full_and_never_shown():
    full = ACCOUNTS["H"]
    same_last_four = ("9" if full[0] != "9" else "8") + full[1:]     # would look identical once masked
    invoice = perfect("H")
    invoice.bank_account.value = same_last_four
    result = row("H", invoice)["fields"]["bank_account"]
    assert result == {"ok": False, "detail": "no match"}


def test_spaces_in_a_printed_account_number_still_match():
    invoice = perfect("H")
    full = ACCOUNTS["H"]
    invoice.bank_account.value = f"{full[:4]} {full[4:]}"
    assert row("H", invoice)["fields"]["bank_account"]["ok"]


def test_an_invented_value_for_an_absent_field_is_wrong():
    invoice = perfect("T1")
    invoice.invoice_number.value = "INV-0001"
    assert not row("T1", invoice)["fields"]["invoice_number"]["ok"]


def test_tax_lines_summed_into_one_are_not_a_correct_reading():
    invoice = perfect("E1")                 # same-state vendor: CGST + SGST
    assert len(invoice.tax_lines) == 2
    invoice.tax_lines[0].amount += invoice.tax_lines[1].amount
    del invoice.tax_lines[1]
    assert not row("E1", invoice)["fields"]["tax"]["ok"]


def test_freight_read_as_a_product_line_fails_line_items():
    invoice = perfect("E5")
    charge = invoice.other_charges.pop()
    invoice.line_items.append(invoice.line_items[0].model_copy(update={"description": charge.description, "qty": None,
                                                                       "unit_price": None, "amount": charge.amount}))
    scored = row("E5", invoice)
    assert not scored["fields"]["line_items"]["ok"]
    assert scored["cells"][0] < scored["cells"][1]


def test_one_wrong_cell_fails_the_line_items_field_but_is_counted_by_cell():
    invoice = perfect("H")
    invoice.line_items[0].qty += Decimal(1)
    scored = row("H", invoice)
    assert not scored["fields"]["line_items"]["ok"]
    assert scored["cells"][0] == scored["cells"][1] - 1


def test_a_failed_extraction_counts_every_field_wrong_and_stays_in_the_totals():
    scored = row("H", None, status="failed", decision_ok=False, check_ok=False, actual_decision="Hold")
    assert all(f == {"ok": False, "detail": FAILED} for f in scored["fields"].values())
    result = totals([row("E1"), scored])
    assert result["invoices"] == 2 and result["runs_failed"] == 1 and result["runs_ok"] == 1
    assert result["fields_total"] == 2 * len(FIELDS) and result["fields_correct"] == len(FIELDS)
    assert result["decisions_ok"] == 1 and result["false_holds"] == 1


HOLD_E1 = BY_ID["E1"]["expected"]


def test_a_hold_caused_by_a_failed_stage_is_not_a_correct_hold():
    assert score_decision(HOLD_E1, "Hold", "medium", ["STAGE_FAILED"], [], ["extract"]) == {
        "decision_ok": False, "check_ok": False}


def test_a_failed_explanation_does_not_change_a_correct_decision():
    assert score_decision(HOLD_E1, "Hold", "medium", ["CUMULATIVE_PO"], [], ["explain"]) == {
        "decision_ok": True, "check_ok": True}


def test_the_right_hold_for_the_wrong_reason_fails_the_check_only():
    assert score_decision(HOLD_E1, "Hold", "medium", ["LINE_MATCH"], [], []) == {"decision_ok": True, "check_ok": False}


def test_an_unexplained_extra_check_fails_the_check_score():
    assert not score_decision(HOLD_E1, "Hold", "medium", ["CUMULATIVE_PO", "ARITHMETIC"], [], [])["check_ok"]


def test_not_evaluated_warnings_beside_the_intended_check_are_allowed():
    expected = BY_ID["T2"]["expected"]
    triggered = ["PO_FOUND", "LINE_MATCH", "AMOUNT_TOLERANCE", "CUMULATIVE_PO"]
    assert score_decision(expected, "Hold", "low", triggered, triggered[1:], [])["check_ok"]


def test_a_high_risk_hold_at_the_wrong_risk_level_is_wrong():
    assert not score_decision(BY_ID["E4"]["expected"], "Hold", "medium", ["BANK_MATCH"], [], [])["decision_ok"]


def test_an_approve_that_should_have_held_is_a_false_approval():
    result = totals([row("E4", actual_decision="Approve", decision_ok=False, check_ok=False)])
    assert result["false_approvals"] == 1 and result["decisions_ok"] == 0


def test_missing_usage_is_reported_as_unavailable_and_never_as_zero():
    known = {"input_tokens": 100}
    missing = {"input_tokens": None}
    assert usage_text([known, known], "input_tokens") == "200"
    assert usage_text([missing], "input_tokens") == "unavailable"
    assert usage_text([], "input_tokens") == "unavailable"
    assert usage_text([known, missing], "input_tokens") == "100 (partial: 1 of 2 calls reported usage)"


@pytest.mark.parametrize("text", ["account 6021145570", "key sk-ant-abc", "SECRETVALUE"])
def test_a_report_containing_a_secret_is_refused(text):
    with pytest.raises(ValueError):
        assert_safe(text, ["SECRETVALUE"])


def test_a_masked_report_is_allowed():
    assert_safe("XXXXXX4417 | 114,450 tokens | PO-2057 | sha256 `0a1b2c3d4e5f60718293`", ["SECRETVALUE"])
