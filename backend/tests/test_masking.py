"""Bank account numbers are masked (XXXXXX4417) everywhere they leave BANK_MATCH: stored payloads, checks,
decisions, events, messages sent to the explanation LLM."""
import json

from sqlalchemy import text

from app.db import repository as repo
from app.pipeline.orchestrator import run_pipeline
from app.util import mask_account, mask_json, mask_text
from conftest import SPECS, TODAY, mirror, sample_pdf, scenario_llm
import generate_invoices as gen

ALL_ACCOUNTS = [v["bank_account"] for v in gen.VENDORS.values()] + [SPECS["E4"]["bank_account"]]


def test_mask_account():
    assert mask_account("2804514417") == "XXXXXX4417"
    assert mask_account("2804 5144-17") == "XXXXXX4417"
    assert mask_account(None) is None


def test_mask_text_masks_long_digit_runs_only():
    assert mask_text("A/c No.: 2804514417 IFSC KKBK0000298 Invoice 7731") == "A/c No.: XXXXXX4417 IFSC KKBK0000298 Invoice 7731"
    assert mask_json({"a": ["x 6021145570"], "n": 5}) == {"a": ["x XXXXXX5570"], "n": 5}


def test_safe_dump_masks_the_account_value_and_its_evidence():
    inv = mirror("E4")
    inv.bank_account.evidence = "A/c No.: 9183402756"
    dump = json.dumps(inv.safe_dump())
    assert "9183402756" not in dump and "XXXXXX2756" in dump


def test_nothing_unmasked_is_stored_after_the_bank_mismatch_scenario(db, settings):
    engine, _, session = db
    seen_events = []
    llm = scenario_llm("E4")
    result = run_pipeline(session, llm, sample_pdf("E4"), "E4.pdf", settings, today=TODAY, on_event=seen_events.append)

    # every text/JSON column of every audit table, plus the events handed to the SSE callback
    dump = json.dumps(seen_events, default=str)
    with engine.connect() as conn:
        for table in ("runs", "stage_events", "check_results", "decisions"):
            dump += json.dumps([list(map(str, row)) for row in conn.execute(text(f"SELECT * FROM {table}"))])
    for account in ALL_ACCOUNTS:
        assert account not in dump, f"unmasked account {account} leaked"
    assert "XXXXXX4417" in dump and "XXXXXX2756" in dump       # master and invoice accounts, masked


def test_the_explanation_prompt_never_receives_a_full_account_number(db, settings):
    _, _, session = db
    llm = scenario_llm("E4")
    run_pipeline(session, llm, sample_pdf("E4"), "E4.pdf", settings, today=TODAY)
    sent = json.dumps(next(c.content for c in llm.calls if c.prompt == "explain"))
    assert all(a not in sent for a in ALL_ACCOUNTS) and "XXXXXX" in sent


def test_vendor_record_never_serialises_its_bank_account():
    from conftest import vendor_record
    v = vendor_record("V005")
    assert "2804514417" not in v.model_dump_json() and "2804514417" not in repr(v)


def test_an_llm_explanation_that_echoes_a_full_number_is_masked_before_storage(db, settings):
    from app.schemas import Explanation
    _, _, session = db
    llm = scenario_llm("E4")
    llm.responses[Explanation] = Explanation(summary="Pay to 9183402756 instead.", drafted_message="A/c 9183402756")
    result = run_pipeline(session, llm, sample_pdf("E4"), "E4.pdf", settings, today=TODAY)
    row = repo.get_decision(session, result.run_id)
    assert "9183402756" not in row.summary and "9183402756" not in row.drafted_message
