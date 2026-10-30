"""Scenario isolation: each of H, E1-E5 is run through the real pipeline (real PDF, real ingest, real
validate/match/rules/decide, real DB) with the LLM mocked to return the extraction a perfect reader would
produce. We assert the expected decision AND that only the intended primary check triggers."""
import pytest

from app.db import repository as repo
from app.pipeline.orchestrator import run_pipeline
from conftest import TODAY, sample_pdf, scenario_llm

# scenario: (decision, primary triggered check or None, LLM prompts the pipeline is allowed to use)
EXPECTED = {
    "H":  ("Approve", None, ["extract", "explain"]),
    "E1": ("Hold", "CUMULATIVE_PO", ["extract", "explain"]),
    "E2": ("Hold", "DUPLICATE_NEAR", ["extract", "explain"]),
    "E3": ("Hold", "PO_INFERRED", ["extract", "infer_po", "match_lines", "explain"]),
    "E4": ("Hold", "BANK_MATCH", ["extract", "explain"]),
    "E5": ("Approve", None, ["extract", "explain"]),
}


@pytest.mark.parametrize("scenario", EXPECTED)
def test_scenario_decision_and_only_the_intended_check_triggers(db, settings, scenario):
    _, _, session = db
    decision, primary, prompts = EXPECTED[scenario]
    llm = scenario_llm(scenario)

    result = run_pipeline(session, llm, sample_pdf(scenario), f"{scenario}.pdf", settings, today=TODAY)

    assert result.status == "completed" and not result.failures
    assert result.decision.decision == decision
    triggered = set(result.decision.triggered_codes)
    if primary is None:
        assert triggered == set()
    elif scenario == "E3":  # a scan may legitimately also show low-confidence warnings
        assert primary in triggered and triggered <= {primary, "EXTRACTION_CONFIDENCE"}
    else:
        assert triggered == {primary}
    assert llm.prompts == prompts  # the LLM is called only when needed


def test_e1_reports_108_percent_utilisation(db, settings):
    _, _, session = db
    result = run_pipeline(session, scenario_llm("E1"), sample_pdf("E1"), "E1.pdf", settings, today=TODAY)
    cumulative = next(c for c in result.checks if c.code == "CUMULATIVE_PO")
    assert "= 108%" in cumulative.message and cumulative.evidence["utilisation_pct"] == 108


def test_e4_is_high_risk(db, settings):
    _, _, session = db
    result = run_pipeline(session, scenario_llm("E4"), sample_pdf("E4"), "E4.pdf", settings, today=TODAY)
    assert result.decision.risk_level == "high" and result.routing.team == "Vendor master / treasury team"


def test_e3_is_inferred_scanned_and_routed_to_the_ap_reviewer(db, settings):
    _, _, session = db
    result = run_pipeline(session, scenario_llm("E3"), sample_pdf("E3"), "E3.pdf", settings, today=TODAY)
    assert repo.get_run(session, result.run_id).doc_type == "scanned"
    assert result.routing.team == "AP reviewer"
    assert next(c for c in result.checks if c.code == "PO_FOUND").result == "PASS"


def test_e3_with_a_low_confidence_gstin_still_holds_and_flags_the_confidence(db, settings):
    _, _, session = db
    llm = scenario_llm("E3")
    from app.schemas import ExtractedInvoice
    inv = llm.responses[ExtractedInvoice]
    inv.vendor_gstin.confidence = 0.6
    result = run_pipeline(session, llm, sample_pdf("E3"), "E3.pdf", settings, today=TODAY)
    assert "EXTRACTION_CONFIDENCE" in result.decision.triggered_codes and result.decision.decision == "Hold"


def test_every_stage_is_recorded_with_started_and_completed_events_and_duration(db, settings):
    _, _, session = db
    seen = []
    result = run_pipeline(session, scenario_llm("H"), sample_pdf("H"), "H.pdf", settings, today=TODAY,
                          on_event=seen.append)
    events = repo.get_events(session, result.run_id)
    stages = ["ingest", "extract", "validate", "match", "rules", "decide", "explain"]
    assert [e.stage for e in events if e.status == "started"] == stages
    done = [e for e in events if e.status == "completed"]
    assert [e.stage for e in done] == stages and all(e.duration_ms is not None for e in done)
    assert [e["stage"] for e in seen if e["status"] == "completed"] == stages  # also streamed to the callback


def test_every_check_and_the_decision_are_persisted(db, settings):
    _, _, session = db
    result = run_pipeline(session, scenario_llm("E2"), sample_pdf("E2"), "E2.pdf", settings, today=TODAY)
    rows = repo.get_checks(session, result.run_id)
    assert {r.code for r in rows} >= {"ARITHMETIC", "REQUIRED_FIELDS", "VENDOR_APPROVED", "BANK_MATCH", "PO_FOUND",
                                      "CUMULATIVE_PO", "LINE_MATCH", "DUPLICATE_EXACT", "DUPLICATE_NEAR", "STAGE_FAILED"}
    decision = repo.get_decision(session, result.run_id)
    assert decision.decision == "Hold" and decision.routed_to == "AP reviewer" and decision.drafted_message


# ---------------------------------------------------------------- failures are visible and never approve
def test_failed_extraction_is_shown_as_failed_and_holds(db, settings):
    from app.llm.client import LLMError
    from app.schemas import Explanation, ExtractedInvoice
    from conftest import FakeLLM
    _, _, session = db
    llm = FakeLLM({ExtractedInvoice: LLMError("extract: reply did not match the schema after one retry"),
                   Explanation: Explanation(summary="Held for manual processing.", drafted_message="Please process by hand.")})
    result = run_pipeline(session, llm, sample_pdf("H"), "H.pdf", settings, today=TODAY)

    assert result.status == "failed" and [f.stage for f in result.failures] == ["extract"]
    assert result.decision.decision == "Hold" and result.decision.triggered_codes == ["STAGE_FAILED"]
    statuses = {e.stage: e.status for e in repo.get_events(session, result.run_id) if e.status != "started"}
    assert statuses["extract"] == "failed" and statuses["validate"] == "skipped" and statuses["match"] == "skipped"
    assert repo.count_ledger(session) == 3  # nothing approved


def test_failed_inference_stage_never_approves(db, settings):
    from app.llm.client import LLMError
    from app.schemas import POInference
    _, _, session = db
    llm = scenario_llm("E3")
    llm.responses[POInference] = LLMError("timeout")
    result = run_pipeline(session, llm, sample_pdf("E3"), "E3.pdf", settings, today=TODAY)
    assert result.status == "failed" and result.decision.decision == "Hold" and repo.count_ledger(session) == 3


def test_failed_explanation_is_shown_as_failed_but_the_decision_stands(db, settings):
    from app.llm.client import LLMError
    from app.schemas import Explanation
    _, _, session = db
    llm = scenario_llm("E4")
    llm.responses[Explanation] = LLMError("timeout")
    result = run_pipeline(session, llm, sample_pdf("E4"), "E4.pdf", settings, today=TODAY)
    assert result.status == "failed" and result.decision.decision == "Hold" and result.explanation is None
    row = repo.get_decision(session, result.run_id)
    assert "no AI explanation was written" in row.summary


def test_unknown_vendor_is_rejected_and_never_reaches_the_ledger(db, settings):
    from app.schemas import ExtractedInvoice
    from conftest import tf
    _, _, session = db
    llm = scenario_llm("H")
    inv = llm.responses[ExtractedInvoice]
    inv.vendor_gstin, inv.vendor_name = tf("29ZZZZZ9999Z1Z9"), tf("Nobody Traders")
    result = run_pipeline(session, llm, sample_pdf("H"), "H.pdf", settings, today=TODAY)
    assert result.decision.decision == "Reject" and "VENDOR_APPROVED" in result.decision.triggered_codes
    assert repo.count_ledger(session) == 3


# ---------------------------------------------------------------- missing fields never lead to Approve
from app.schemas import DateField, ExtractedInvoice, MoneyField, TextField  # noqa: E402


@pytest.mark.parametrize("field, empty, expected_code", [
    ("vendor_gstin", TextField.empty, "VENDOR_AMBIGUOUS"),     # only the name is left: a name match never approves
    ("invoice_number", TextField.empty, "REQUIRED_FIELDS"),
    ("invoice_date", DateField.empty, "REQUIRED_FIELDS"),
    ("subtotal", MoneyField.empty, "REQUIRED_FIELDS"),
    ("total", MoneyField.empty, "REQUIRED_FIELDS"),
    ("bank_account", TextField.empty, "BANK_MATCH"),
    ("ifsc", TextField.empty, "BANK_MATCH"),
])
def test_a_missing_field_on_an_otherwise_perfect_invoice_holds_and_never_approves(db, settings, field, empty, expected_code):
    _, _, session = db
    llm = scenario_llm("H")                                    # H is the invoice that normally Approves
    setattr(llm.responses[ExtractedInvoice], field, empty())
    result = run_pipeline(session, llm, sample_pdf("H"), "H.pdf", settings, today=TODAY)
    assert result.decision.decision == "Hold" and expected_code in result.decision.triggered_codes
    assert repo.count_ledger(session) == 3                      # nothing consumed the PO
    assert not any(c.result == "PASS" for c in result.checks if c.code == expected_code)


def test_missing_fields_are_reported_as_null_so_the_ui_can_say_not_on_the_document(db, settings):
    _, _, session = db
    llm = scenario_llm("H")
    llm.responses[ExtractedInvoice].invoice_number = TextField.empty()
    result = run_pipeline(session, llm, sample_pdf("H"), "H.pdf", settings, today=TODAY)
    extract = next(e for e in repo.get_events(session, result.run_id) if e.stage == "extract" and e.status == "completed")
    assert extract.output["invoice"]["invoice_number"] == {"value": None, "confidence": 0.0, "evidence": None, "page": None}
