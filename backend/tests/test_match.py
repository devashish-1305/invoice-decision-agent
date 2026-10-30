"""Matching: deterministic first, LLM only for what is left."""
import json
from decimal import Decimal

from app.config import Settings
from app.pipeline import match as m
from app.schemas import LineMatchResponse, LLMLineMatch, POCandidate, POInference
from conftest import FakeLLM, mirror, po_record, tf, vendor_record

SETTINGS = Settings()
VENDORS = [vendor_record(v) for v in ("V001", "V002", "V003", "V004", "V005", "V006")]
POS = [po_record(p) for p in ("PO-2038", "PO-2041", "PO-2050", "PO-2057", "PO-2061", "PO-2063", "PO-2068")]


# ---------------------------------------------------------------- vendor
def test_vendor_exact_gstin_match():
    vm, vendor = m.match_vendor(mirror("H"), VENDORS, SETTINGS)
    assert (vm.method, vendor.vendor_id) == ("gstin", "V001")


def test_vendor_gstin_match_ignores_spaces_and_case():
    inv = mirror("H")
    inv.vendor_gstin.value = "07aagcs 4172k1z8"
    assert m.match_vendor(inv, VENDORS, SETTINGS)[0].method == "gstin"


def test_vendor_by_name_only_is_a_suggestion():
    inv = mirror("H")
    inv.vendor_gstin = tf(None)
    vm, vendor = m.match_vendor(inv, VENDORS, SETTINGS)
    assert vm.method == "name" and vendor.vendor_id == "V001"


def test_unknown_gstin_does_not_fall_back_to_a_wrong_vendor():
    inv = mirror("H")
    inv.vendor_gstin.value = "29ZZZZZ9999Z1Z9"
    inv.vendor_name = tf("Totally Different Traders")
    vm, vendor = m.match_vendor(inv, VENDORS, SETTINGS)
    assert (vm.method, vendor) == ("none", None)


# ---------------------------------------------------------------- PO
def test_explicit_po_is_a_plain_lookup_with_no_llm_call():
    llm = FakeLLM({})
    pm, po = m.match_po(llm, mirror("H"), VENDORS[0], POS, SETTINGS)
    assert (pm.method, po.po_id) == ("explicit", "PO-2038") and llm.calls == []


def test_explicit_po_that_does_not_exist():
    inv = mirror("H")
    inv.po_reference.value = "PO-9999"
    pm, po = m.match_po(FakeLLM({}), inv, VENDORS[0], POS, SETTINGS)
    assert (pm.method, pm.reference, po) == ("none", "PO-9999", None)


def test_missing_po_is_inferred_by_the_llm_and_marked_inferred():
    llm = FakeLLM({POInference: POInference(ranking=[POCandidate(po_id="PO-2057", confidence=0.9, reason="r"),
                                                     POCandidate(po_id="PO-2061", confidence=0.1, reason="r")])})
    pm, po = m.match_po(llm, mirror("E3"), VENDORS[3], POS, SETTINGS)
    assert (pm.method, po.po_id) == ("inferred", "PO-2057") and llm.prompts == ["infer_po"]


def test_inference_only_considers_the_vendors_open_pos():
    seen = {}
    def reply(content):
        seen.update(json.loads(content[0]["text"]))
        return POInference(ranking=[POCandidate(po_id="PO-2057", confidence=0.9, reason="r")])
    m.match_po(FakeLLM({POInference: reply}), mirror("E3"), VENDORS[3], POS, SETTINGS)
    assert {p["po_id"] for p in seen["candidate_pos"]} == {"PO-2057", "PO-2061"}


def test_inferred_po_below_minimum_confidence_is_discarded():
    llm = FakeLLM({POInference: POInference(ranking=[POCandidate(po_id="PO-2057", confidence=0.3, reason="r")])})
    pm, po = m.match_po(llm, mirror("E3"), VENDORS[3], POS, SETTINGS)
    assert (pm.method, po) == ("none", None)


def test_inference_ignores_po_ids_the_llm_invented_or_that_belong_to_other_vendors():
    llm = FakeLLM({POInference: POInference(ranking=[POCandidate(po_id="PO-2038", confidence=0.99, reason="r"),
                                                     POCandidate(po_id="PO-0000", confidence=0.99, reason="r")])})
    pm, po = m.match_po(llm, mirror("E3"), VENDORS[3], POS, SETTINGS)
    assert (pm.method, po) == ("none", None)


def test_no_inference_without_an_identified_vendor():
    llm = FakeLLM({})
    pm, po = m.match_po(llm, mirror("E3"), None, POS, SETTINGS)
    assert (pm.method, po) == ("none", None) and llm.calls == []


# ---------------------------------------------------------------- lines
def test_lines_that_match_exactly_never_call_the_llm():
    llm = FakeLLM({})
    lines = m.match_lines(llm, mirror("H"), po_record("PO-2038"), SETTINGS)
    assert [l.method for l in lines] == ["exact"] * 4 and llm.calls == []


def test_exact_matching_ignores_case_and_punctuation():
    inv = mirror("H")
    inv.line_items[0].description = "a4 copier paper 75 gsm - (500 sheets)"
    lines = m.match_lines(FakeLLM({}), inv, po_record("PO-2038"), SETTINGS)
    assert lines[0].method == "exact"


def test_only_leftover_lines_are_sent_to_the_llm():
    sent = {}
    def reply(content):
        sent.update(json.loads(content[0]["text"]))
        return LineMatchResponse(matches=[
            LLMLineMatch(invoice_line_index=1, po_line_id="POL-2057-2", confidence=0.95, reason="same"),
            LLMLineMatch(invoice_line_index=3, po_line_id="POL-2057-4", confidence=0.95, reason="same")])
    lines = m.match_lines(FakeLLM({LineMatchResponse: reply}), mirror("E3"), po_record("PO-2057"), SETTINGS)
    assert [l.method for l in lines] == ["exact", "semantic", "exact", "semantic"]
    assert [x["invoice_line_index"] for x in sent["invoice_lines"]] == [1, 3]
    assert {x["po_line_id"] for x in sent["candidate_po_lines"]} == {"POL-2057-2", "POL-2057-4"}


def test_low_confidence_semantic_match_is_rejected():
    reply = LineMatchResponse(matches=[LLMLineMatch(invoice_line_index=1, po_line_id="POL-2057-2", confidence=0.5, reason="?")])
    lines = m.match_lines(FakeLLM({LineMatchResponse: reply}), mirror("E3"), po_record("PO-2057"), SETTINGS)
    assert lines[1].po_line_id is None and lines[1].method == "none"


def test_semantic_match_cannot_claim_a_po_line_already_matched_or_not_offered():
    reply = LineMatchResponse(matches=[
        LLMLineMatch(invoice_line_index=1, po_line_id="POL-2057-1", confidence=0.99, reason="already used"),
        LLMLineMatch(invoice_line_index=3, po_line_id="POL-2038-1", confidence=0.99, reason="other PO")])
    lines = m.match_lines(FakeLLM({LineMatchResponse: reply}), mirror("E3"), po_record("PO-2057"), SETTINGS)
    assert lines[1].po_line_id is None and lines[3].po_line_id is None


def test_no_po_means_no_line_matching_and_no_llm_call():
    llm = FakeLLM({})
    lines = m.match_lines(llm, mirror("H"), None, SETTINGS)
    assert all(l.po_line_id is None for l in lines) and llm.calls == []


def test_match_ignores_a_po_that_belongs_to_another_vendor_for_line_matching():
    inv = mirror("H")
    inv.po_reference.value = "PO-2041"  # exists, but it is V002's PO
    matched = m.match(FakeLLM({}), inv, __import__("app.schemas", fromlist=["ReferenceData"]).ReferenceData(vendors=VENDORS, pos=POS), SETTINGS)
    assert matched.po.po_id == "PO-2041" and all(l.po_line_id is None for l in matched.result.lines)
