"""Model-written text shown in the UI must never show raw rupee numbers.
Two layers are tested: (1) amounts go INTO every prompt already formatted (₹1,14,450.00) and the prompt says to
copy them exactly; (2) a deterministic guard rewrites any raw amount that still comes OUT in the text.
Ranking, confidence and decisions are never touched by either layer."""
import json
import re
from decimal import Decimal

from app.config import Settings
from app.db import repository as repo
from app.llm.client import PROMPT_DIR
from app.pipeline import match as m
from app.pipeline.orchestrator import run_pipeline
from app.schemas import Explanation, LineMatchResponse, LLMLineMatch, POCandidate, POInference
from app.util import reformat_amounts
from conftest import FakeLLM, TODAY, mirror, po_record, sample_pdf, scenario_llm, vendor_record

SETTINGS = Settings()
POS = [po_record(p) for p in ("PO-2057", "PO-2061")]
RAW_AMOUNT = re.compile(r"(?<![\w₹.,])\d{4,}(?:\.\d+)?(?!\w)")     # a bare 4+ digit number (no ₹, no grouping)


def payload_of(llm, prompt):
    return json.loads(next(c.content for c in llm.calls if c.prompt == prompt)[0]["text"])


def infer(reason, confidence=0.93):
    llm = FakeLLM({POInference: POInference(ranking=[
        POCandidate(po_id="PO-2057", confidence=confidence, reason=reason),
        POCandidate(po_id="PO-2061", confidence=0.04, reason="Different goods: uniforms and trolleys")])})
    return llm, m.match_po(llm, mirror("E3"), vendor_record("V004"), POS, SETTINGS)


# ---------------------------------------------------------------- amounts go INTO the infer_po prompt formatted
def test_infer_po_payload_has_every_rupee_amount_formatted_and_quantities_plain():
    llm, _ = infer("fine")
    p = payload_of(llm, "infer_po")
    assert p["invoice"]["subtotal"] == "₹1,14,450.00"
    assert [l["unit_price"] for l in p["invoice"]["lines"]] == ["₹420.00", "₹310.00", "₹95.00", "₹120.00"]
    assert [l["amount"] for l in p["invoice"]["lines"]] == ["₹33,600.00", "₹18,600.00", "₹14,250.00", "₹48,000.00"]
    assert {c["po_id"]: c["po_total"] for c in p["candidate_pos"]} == {"PO-2057": "₹1,14,450.00", "PO-2061": "₹1,09,800.00"}
    assert p["candidate_pos"][1]["lines"][2]["unit_price"] == "₹7,800.00"
    assert p["invoice"]["lines"][0]["qty"] == "80"                       # quantities are not money


def test_no_raw_money_number_reaches_the_infer_po_prompt():
    llm, _ = infer("fine")
    text = next(c.content for c in llm.calls if c.prompt == "infer_po")[0]["text"]
    without_ids = re.sub(r"\d{4}-\d{2}-\d{2}|PO-\d+", "", text)               # dates and PO ids are not amounts
    assert not RAW_AMOUNT.search(without_ids)


def test_infer_po_prompt_says_to_copy_amounts_exactly():
    text = (PROMPT_DIR / "infer_po.md").read_text()
    assert "EXACTLY as given" in text and "₹1,14,450.00" in text and "bare number" in text


# ---------------------------------------------------------------- amounts coming OUT are guarded
def test_raw_amounts_in_an_inference_reason_are_rewritten_as_rupees():
    _, (pm, po) = infer("All four lines match and the PO total of 114450 equals the invoice subtotal 114450.00 "
                        "(1,14,450); lines total 48000 for garbage bags at 120 each.")
    assert pm.reason == ("All four lines match and the PO total of ₹1,14,450.00 equals the invoice subtotal ₹1,14,450.00 "
                         "(₹1,14,450.00); lines total ₹48,000.00 for garbage bags at ₹120.00 each.")
    assert not RAW_AMOUNT.search(pm.reason)


def test_the_guard_changes_text_only_never_ranking_or_confidence():
    llm, (pm, po) = infer("PO total 114450 matches", confidence=0.93)
    assert (pm.method, pm.po_id, pm.confidence) == ("inferred", "PO-2057", 0.93) and po.po_id == "PO-2057"
    assert [(c.po_id, c.confidence) for c in pm.ranking] == [("PO-2057", 0.93), ("PO-2061", 0.04)]


def test_every_ranking_reason_is_guarded_not_just_the_top_one():
    llm = FakeLLM({POInference: POInference(ranking=[
        POCandidate(po_id="PO-2057", confidence=0.9, reason="matches"),
        POCandidate(po_id="PO-2061", confidence=0.1, reason="its total 109800 is lower than 114450")])})
    pm, _ = m.match_po(llm, mirror("E3"), vendor_record("V004"), POS, SETTINGS)
    assert pm.ranking[1].reason == "its total ₹1,09,800.00 is lower than ₹1,14,450.00"


def test_guard_leaves_quantities_identifiers_dates_and_formatted_amounts_alone():
    amounts = [Decimal("120"), Decimal("114450.00")]
    text = ("400 packs at 120; PO-2057 dated 2026-10-24, invoice GFS/1026/088, account XXXXXX4417, "
            "already ₹1,14,450.00 and 114450 total.")
    out = reformat_amounts(text, amounts, exclude=[Decimal("400")])
    assert out == ("400 packs at ₹120.00; PO-2057 dated 2026-10-24, invoice GFS/1026/088, account XXXXXX4417, "
                   "already ₹1,14,450.00 and ₹1,14,450.00 total.")


def test_guard_never_rewrites_a_quantity_that_happens_to_equal_an_amount():
    assert reformat_amounts("300 reams at 300", [Decimal("300")], exclude=[Decimal("300")]) == "300 reams at 300"
    assert reformat_amounts("Total 114450.", [Decimal("114450")]) == "Total ₹1,14,450.00."      # sentence-final period
    assert reformat_amounts("of 500000, above 510000; then 540000.", [Decimal("500000"), Decimal("510000"), Decimal("540000")]) \
        == "of ₹5,00,000.00, above ₹5,10,000.00; then ₹5,40,000.00."                         # commas and periods around numbers survive
    assert reformat_amounts(None, [Decimal("1")]) is None and reformat_amounts("", [Decimal("1")]) == ""


def test_pipeline_shows_formatted_amounts_for_the_inference_reason_in_the_match_output_and_the_check_evidence(db, settings):
    _, _, session = db
    llm = scenario_llm("E3")
    llm.responses[POInference] = POInference(ranking=[
        POCandidate(po_id="PO-2057", confidence=0.98, reason="The PO total of 114450 equals the invoice subtotal."),
        POCandidate(po_id="PO-2061", confidence=0.02, reason="Different goods")])
    result = run_pipeline(session, llm, sample_pdf("E3"), "E3.pdf", settings, today=TODAY)

    match_event = next(e for e in repo.get_events(session, result.run_id) if e.stage == "match" and e.status == "completed")
    assert match_event.output["po"]["reason"] == "The PO total of ₹1,14,450.00 equals the invoice subtotal."
    inferred = next(c for c in result.checks if c.code == "PO_INFERRED")
    assert inferred.evidence["reason"] == "The PO total of ₹1,14,450.00 equals the invoice subtotal."
    assert result.decision.decision == "Hold" and result.decision.triggered_codes == ["PO_INFERRED"]


# ---------------------------------------------------------------- match_lines (reasons shown in the Match panel)
def test_match_lines_prompt_gets_no_amounts_and_is_told_not_to_mention_them():
    llm = FakeLLM({LineMatchResponse: LineMatchResponse(matches=[
        LLMLineMatch(invoice_line_index=1, po_line_id="POL-2057-2", confidence=0.95, reason="Same product"),
        LLMLineMatch(invoice_line_index=3, po_line_id="POL-2057-4", confidence=0.95, reason="Same product")])})
    m.match_lines(llm, mirror("E3"), po_record("PO-2057"), SETTINGS)
    text = next(c.content for c in llm.calls if c.prompt == "match_lines")[0]["text"]
    assert "₹" not in text and "unit_price" not in text and "amount" not in text
    assert "Do not mention prices, quantities or amounts" in (PROMPT_DIR / "match_lines.md").read_text()


# ---------------------------------------------------------------- explain (summary and drafted message)
def test_raw_amounts_in_the_explanation_are_rewritten_before_they_are_stored(db, settings):
    _, _, session = db
    llm = scenario_llm("E1")
    llm.responses[Explanation] = Explanation(
        summary="Billing reaches 540000 of 500000, above the 510000 limit; this invoice is 194700 incl. GST.",
        drafted_message="Please approve: 375000 already approved plus 165000 now = 540000.")
    result = run_pipeline(session, llm, sample_pdf("E1"), "E1.pdf", settings, today=TODAY)
    row = repo.get_decision(session, result.run_id)
    assert row.summary == ("Billing reaches ₹5,40,000.00 of ₹5,00,000.00, above the ₹5,10,000.00 limit; "
                           "this invoice is ₹1,94,700.00 incl. GST.")
    assert row.drafted_message == "Please approve: ₹3,75,000.00 already approved plus ₹1,65,000.00 now = ₹5,40,000.00."
    assert not RAW_AMOUNT.search(row.summary + row.drafted_message)
