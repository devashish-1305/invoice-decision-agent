"""The prepared edge-case files (scripts/generate_test_inputs.py). They exercise the existing architecture only:
invalid files are stopped by the upload gate; valid ones go through the normal pipeline and rules."""
import json

import pytest

import generate_test_inputs as ti
from app.pipeline.orchestrator import run_pipeline
from app.schemas import Explanation, ExtractedInvoice
from conftest import FakeLLM, TODAY, extracted_from
from test_api import Api


@pytest.fixture
def api(tmp_path):
    return Api(tmp_path)


INVALID_MESSAGES = {
    "not_a_pdf.docx": "This isn't a PDF file. Upload the invoice as a PDF.",
    "invoice_photo.png": "This isn't a PDF file. Upload the invoice as a PDF.",
    "empty.pdf": "This file is empty (0 bytes). Choose the invoice PDF and try again.",
    "corrupted.pdf": "This PDF is damaged and can't be read. Upload another copy of the invoice.",
    "password_protected.pdf": "This file is password-protected. Upload an unlocked PDF.",
    "too_many_pages.pdf": "This PDF has 6 pages; the limit is 5. Upload a PDF of 5 pages or fewer (one invoice per file).",
    "too_large.pdf": "This file is larger than 10 MB. Upload a PDF under 10 MB.",
}


@pytest.mark.parametrize("name", INVALID_MESSAGES)
def test_each_invalid_file_is_rejected_at_upload_with_a_plain_message_and_zero_llm_calls(api, name):
    data = ti.invalid_files()[name]
    response = api.client.post("/api/runs", files={"file": (name, data, "application/octet-stream")})
    assert response.status_code == 400 and response.json()["detail"] == INVALID_MESSAGES[name]
    assert api.client.get("/api/runs").json()["runs"] == []        # no run was created
    assert api.llm_factory_calls == 0                                # the LLM was never involved


def test_the_files_on_disk_are_exactly_what_the_generator_produces():
    for name, data in ti.invalid_files().items():
        assert (ti.OUT / name).read_bytes() == data, name
    for name, invoice in ti.valid_invoices().items():
        assert (ti.OUT / name).read_bytes() == ti.render(invoice), name


def test_the_readme_lists_every_file_with_the_real_gate_messages():
    text = (ti.OUT / "README.md").read_text()
    for name, message in INVALID_MESSAGES.items():
        assert f"`{name}`" in text and message in text
    assert all(f"`{name}`" in text for name in ti.VALID_SPECS)
    assert "One invoice per PDF" in text


def test_test_inputs_are_not_sample_cards(api):
    codes = [c["code"] for c in api.client.get("/api/samples").json()]
    assert codes == ["H", "E1", "E2", "E3", "E4", "E5"]
    assert "test_inputs" not in (ti.gen.OUT / "samples.json").read_text()


def run_valid(db, settings, name):
    _, _, session = db
    invoice = ti.valid_invoices()[name]
    llm = FakeLLM({ExtractedInvoice: extracted_from(invoice),
                   Explanation: Explanation(summary="Explained.", drafted_message="Please review.")})
    result = run_pipeline(session, llm, (ti.OUT / name).read_bytes(), name, settings, today=TODAY)
    return result, llm


def test_missing_invoice_number_holds_via_required_fields(db, settings):
    result, llm = run_valid(db, settings, "missing_invoice_number.pdf")
    assert result.decision.decision == "Hold" and "REQUIRED_FIELDS" in result.decision.triggered_codes
    assert set(result.decision.triggered_codes) <= {"REQUIRED_FIELDS", "DUPLICATE_EXACT", "DUPLICATE_NEAR"}
    extracted = next(c for c in llm.calls if c.prompt == "extract")
    assert extracted                                                   # the LLM read the file (it is a valid PDF)


def test_unknown_po_holds_via_po_found(db, settings):
    result, llm = run_valid(db, settings, "unknown_po.pdf")
    assert result.decision.decision == "Hold" and "PO_FOUND" in result.decision.triggered_codes
    assert set(result.decision.triggered_codes) <= {"PO_FOUND", "LINE_MATCH", "AMOUNT_TOLERANCE", "CUMULATIVE_PO"}
    assert "infer_po" not in llm.prompts                               # a printed PO is looked up, never guessed by AI


def test_prompt_injection_invoice_holds_high_risk_on_the_genuine_bank_mismatch(db, settings):
    result, llm = run_valid(db, settings, "prompt_injection.pdf")
    assert result.decision.decision == "Hold" and result.decision.risk_level == "high"
    assert result.decision.triggered_codes == ["BANK_MATCH"]
    explain_input = json.dumps(next(c.content for c in llm.calls if c.prompt == "explain"))
    assert "NOTE TO AI SYSTEM" not in explain_input and "ignore previous instructions" not in explain_input
    assert "XXXXXX0528" in explain_input and "7419630528" not in explain_input        # masked as always


def test_none_of_the_prepared_invoices_can_be_approved_whatever_the_extraction_says(db, settings):
    _, _, session = db
    from app.db import repository as repo
    for name in ti.VALID_SPECS:
        assert run_valid(db, settings, name)[0].decision.decision != "Approve"
    assert repo.count_ledger(session) == 3
