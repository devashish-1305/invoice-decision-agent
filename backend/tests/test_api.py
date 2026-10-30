"""The HTTP API. The LLM is mocked (conftest.scenario_llm); everything else is real: routing, background
execution, the database, the pipeline, SSE."""
import dataclasses
import io
import json

import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

import generate_invoices as gen
from app.api import views
from app.api.stream import read_new_events
from app.config import Settings
from app.db import repository as repo
from app.main import create_app
from conftest import SPECS, TODAY, sample_pdf, scenario_llm

ALL_ACCOUNTS = [v["bank_account"] for v in gen.VENDORS.values()] + [SPECS["E4"]["bank_account"]]
STAGES = ["ingest", "extract", "validate", "match", "rules", "decide", "explain"]


class Api:
    """A test client plus a handle on which scripted LLM the next run will use."""

    def __init__(self, tmp_path):
        self.dist = tmp_path / "dist"
        self.settings = settings = dataclasses.replace(Settings(), database_url=f"sqlite:///{tmp_path / 'api.db'}",
                                                       frontend_dist=self.dist)
        self.llm = None
        self.llm_factory_calls = 0          # how many times a run asked for an LLM client (0 = no LLM involved)
        self.app = create_app(settings, llm_factory=self._llm_factory, today=lambda: TODAY)
        self.client = TestClient(self.app)

    def _llm_factory(self):
        self.llm_factory_calls += 1
        return self.llm

    def run_sample(self, code):
        self.llm = scenario_llm(code)
        response = self.client.post("/api/runs", json={"sample": code})
        assert response.status_code == 202, response.text
        return response.json()["run_id"]

    def detail(self, run_id):
        return self.client.get(f"/api/runs/{run_id}").json()


@pytest.fixture
def api(tmp_path):
    return Api(tmp_path)


def sse(client, url, headers=None):
    """Read a whole SSE response into [(id, event, data)]."""
    out, current = [], {}
    with client.stream("GET", url, headers=headers or {}) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line == "":
                if current:
                    out.append((current.get("id"), current.get("event"), json.loads(current["data"])))
                current = {}
            elif ":" in line and not line.startswith(":"):
                key, _, value = line.partition(":")
                current[key] = value.strip()
    return out


# ---------------------------------------------------------------- samples
def test_samples_are_the_six_scenario_cards(api):
    cards = api.client.get("/api/samples").json()
    assert [c["code"] for c in cards] == ["H", "E1", "E2", "E3", "E4", "E5"]
    assert all({"title", "description", "vendor", "po", "subtotal", "expected"} <= set(c) for c in cards)
    by = {c["code"]: c for c in cards}
    assert by["E3"]["po"] is None and by["E1"]["po"] == "PO-2041"
    assert by["E4"]["expected"] == "Hold · high risk" and by["H"]["expected"] == "Approve"
    assert by["E1"]["subtotal"] == "165000.00" and "108%" in by["E1"]["description"]
    assert "file" not in by["H"]


# ---------------------------------------------------------------- starting runs
def test_sample_run_returns_run_id_immediately_and_completes_with_every_stage(api):
    run_id = api.run_sample("H")
    d = api.detail(run_id)
    assert d["status"] == "completed" and d["doc_type"] == "text"
    assert [s["stage"] for s in d["stages"]] == STAGES
    assert all(s["status"] == "completed" and s["duration_ms"] is not None and s["summary"] for s in d["stages"])
    assert d["header"]["vendor_name"] == "Sharma Office Supplies Pvt Ltd" and d["header"]["total"] == "131522.80"
    assert d["header"]["vendor_gstin"] == "07AAGCS4172K1Z8" and d["header"]["invoice_number"] == "SOS/2627/0418"
    assert d["decision"]["decision"] == "Approve" and d["decision"]["ledger_inserted"] is True
    assert d["extracted"]["invoice_number"]["evidence"] and d["extracted"]["vendor_gstin"]["page"] == 1
    assert d["match"]["po"]["method"] == "explicit" and all(l["method"] == "exact" for l in d["match"]["lines"])
    assert {c["code"] for c in d["checks"]} >= {"ARITHMETIC", "BANK_MATCH", "CUMULATIVE_PO", "STAGE_FAILED"}
    assert d["file_size_bytes"] > 0


def test_scanned_run_shows_inferred_po_and_semantic_line_matches(api):
    d = api.detail(api.run_sample("E3"))
    assert d["doc_type"] == "scanned" and d["decision"]["decision"] == "Hold"
    assert d["match"]["po"]["method"] == "inferred" and d["match"]["po"]["confidence"] == 0.93
    assert [l["method"] for l in d["match"]["lines"]] == ["exact", "semantic", "exact", "semantic"]
    assert d["match"]["lines"][1]["po_line"]["description"] == "Liquid Hand Wash 5 L Can"
    assert d["decision"]["routed_to"] == "AP reviewer" and d["decision"]["top_code"] == "PO_INFERRED"
    by_stage = {s["stage"]: s for s in d["stages"]}
    assert "inferred (93%)" in by_stage["match"]["summary"] and by_stage["ingest"]["summary"].startswith("Scanned image")


def test_upload_a_pdf(api):
    api.llm = scenario_llm("E1")
    response = api.client.post("/api/runs", files={"file": ("my-invoice.pdf", sample_pdf("E1"), "application/pdf")})
    assert response.status_code == 202
    d = api.detail(response.json()["run_id"])
    assert d["filename"] == "my-invoice.pdf" and d["decision"]["decision"] == "Hold"


def test_cumulative_breakdown_names_the_earlier_invoices(api):
    d = api.detail(api.run_sample("E1"))
    cumulative = next(c for c in d["checks"] if c["code"] == "CUMULATIVE_PO")
    assert cumulative["result"] == "FAIL" and cumulative["evidence"]["utilisation_pct"] == "108.0"
    assert {i["invoice_number"] for i in cumulative["evidence"]["approved_invoices"]} == {"KIF-26-0171", "KIF-26-0209"}
    assert d["decision"]["top_code"] == "CUMULATIVE_PO"


def pdf_with_pages(n):
    import io
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for i in range(n):
        c.drawString(72, 700, f"page {i}")
        c.showPage()
    c.save()
    return buf.getvalue()


def encrypted_pdf():
    from reportlab.lib.pdfencrypt import StandardEncryption
    buf = io.BytesIO()
    c = canvas.Canvas(buf, encrypt=StandardEncryption("userpw", "ownerpw"))
    c.drawString(72, 700, "Invoice 1234")
    c.showPage()
    c.save()
    return buf.getvalue()


def blank_pdf():
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.showPage()
    c.save()
    return buf.getvalue()


@pytest.mark.parametrize("files, message", [
    ({"file": ("x.txt", b"hello", "text/plain")}, "This isn't a PDF file. Upload the invoice as a PDF."),
    ({"file": ("invoice.docx", b"PK\x03\x04" + b"0" * 100, "application/vnd.openxmlformats")}, "This isn't a PDF file."),
    ({"file": ("photo.png", b"\x89PNG\r\n\x1a\n" + b"0" * 100, "image/png")}, "This isn't a PDF file."),
    ({"file": ("photo.jpg", b"\xff\xd8\xff\xe0" + b"0" * 100, "image/jpeg")}, "This isn't a PDF file."),
    ({"file": ("x.pdf", b"", "application/pdf")}, "This file is empty (0 bytes). Choose the invoice PDF and try again."),
    ({"file": ("x.pdf", b"%PDF-1.4 not really", "application/pdf")}, "This PDF is damaged and can't be read."),
    ({"file": ("cut.pdf", sample_pdf("H")[:3000], "application/pdf")}, "This PDF is damaged and can't be read."),
    ({"file": ("big.pdf", b"%PDF-" + b"0" * (10 * 1024 * 1024), "application/pdf")}, "This file is larger than 10 MB. Upload a PDF under 10 MB."),
    ({"file": ("six.pdf", pdf_with_pages(6), "application/pdf")}, "This PDF has 6 pages; the limit is 5. Upload a PDF of 5 pages or fewer"),
    ({"file": ("locked.pdf", encrypted_pdf(), "application/pdf")}, "This file is password-protected. Upload an unlocked PDF."),
    ({"file": ("blank.pdf", blank_pdf(), "application/pdf")}, "This PDF looks blank"),
    ({"other": ("x.pdf", b"%PDF-", "application/pdf")}, "No file received"),
])
def test_invalid_uploads_get_a_clear_400_create_no_run_and_make_zero_llm_calls(api, files, message):
    response = api.client.post("/api/runs", files=files)
    assert response.status_code == 400 and message in response.json()["detail"]
    assert api.client.get("/api/runs").json()["runs"] == []
    assert api.llm_factory_calls == 0                       # no run was started, so no LLM client was ever requested


def test_bad_json_requests_get_400(api):
    assert "Unknown sample" in api.client.post("/api/runs", json={"sample": "ZZ"}).json()["detail"]
    assert api.client.post("/api/runs", json={}).status_code == 400
    assert api.client.post("/api/runs", content=b"hi", headers={"content-type": "text/plain"}).status_code == 400


# ---------------------------------------------------------------- queued run exists before the response; crashes are recorded
def test_run_is_persisted_as_queued_before_the_background_work_starts(api, monkeypatch):
    monkeypatch.setattr("app.api.runs.execute_run", lambda *a, **k: None)     # background work never runs
    response = api.client.post("/api/runs", json={"sample": "H"})
    run_id = response.json()["run_id"]
    assert response.json()["status"] == "queued"
    d = api.detail(run_id)
    assert d["status"] == "queued" and d["decision"] is None
    assert [s["status"] for s in d["stages"]] == ["pending"] * 7
    kpis = api.client.get("/api/runs").json()["kpis"]
    assert kpis["processed"] == 0                                              # unfinished runs are not "processed"


def test_a_crash_in_background_execution_is_recorded_as_a_failed_run_with_a_fail_safe_hold(api, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr("app.api.execution.run_pipeline", boom)
    api.llm = scenario_llm("H")
    run_id = api.client.post("/api/runs", json={"sample": "H"}).json()["run_id"]
    d = api.detail(run_id)
    assert d["status"] == "failed" and d["decision"]["decision"] == "Hold"
    failed = next(s for s in d["stages"] if s["stage"] == "pipeline")
    assert failed["status"] == "failed" and "kaboom" in failed["error"]
    assert any(c["code"] == "STAGE_FAILED" and c["result"] == "FAIL" for c in d["checks"])
    assert api.client.get("/api/reference").json()["pos"][0]["approved_to_date"] == "0"   # nothing approved
    assert [e[1] for e in sse(api.client, f"/api/runs/{run_id}/stream")][-1] == "end"      # stream still terminates


# ---------------------------------------------------------------- history + KPIs
def test_history_is_newest_first_and_kpis_use_subtotals_of_finished_runs(api):
    api.run_sample("H")        # Approve   subtotal 111,460  (total incl. GST 131,522.80)
    api.run_sample("E1")       # Hold      subtotal 165,000
    api.run_sample("E4")       # Hold      subtotal 255,050
    api.run_sample("H")        # Reject (duplicate of the approved H)
    data = api.client.get("/api/runs").json()
    assert [r["id"] for r in data["runs"]] == [4, 3, 2, 1]
    assert [r["decision"] for r in data["runs"]] == ["Reject", "Hold", "Hold", "Approve"]
    assert data["kpis"] == {"processed": 4, "approved": 1, "held": 2, "rejected": 1,
                            "auto_approved": "111460.00", "stopped": "531510.00"}   # 165000 + 255050 + 111460
    newest = data["runs"][0]
    assert newest["top_code"] == "DUPLICATE_EXACT" and newest["subtotal"] == "111460.00" and newest["total"] == "131522.80"
    assert data["runs"][1]["top_code"] == "BANK_MATCH" and data["runs"][1]["risk_level"] == "high"
    assert data["runs"][3]["top_code"] is None


def test_live_ledger_behaviour_approve_then_exact_duplicate_reject_without_error(api):
    first, second = api.run_sample("H"), api.run_sample("H")
    a, b = api.detail(first), api.detail(second)
    assert a["decision"]["decision"] == "Approve" and a["decision"]["ledger_inserted"]
    assert b["decision"]["decision"] == "Reject" and b["decision"]["top_code"] == "DUPLICATE_EXACT"
    assert b["status"] == "completed" and not any(s["status"] == "failed" for s in b["stages"])
    po = next(p for p in api.client.get("/api/reference").json()["pos"] if p["po_id"] == "PO-2038")
    assert po["approved_to_date"] == "111460.00"                      # consumed once, not twice


# ---------------------------------------------------------------- SSE
def test_stream_replays_all_events_to_a_late_client_with_increasing_ids_then_ends(api):
    run_id = api.run_sample("H")
    events = sse(api.client, f"/api/runs/{run_id}/stream")
    stage_events = [e for e in events if e[1] == "stage"]
    ids = [int(e[0]) for e in stage_events]
    assert ids == sorted(ids) and len(set(ids)) == len(ids) == 14                    # 7 stages x started/completed
    assert ids == list(range(ids[0], ids[0] + 14))                                    # no gaps
    assert [(e[2]["stage"], e[2]["status"]) for e in stage_events][:4] == [
        ("ingest", "started"), ("ingest", "completed"), ("extract", "started"), ("extract", "completed")]
    assert events[-1][1] == "end" and events[-1][2]["decision"] == "Approve"          # closes only after the last event


def test_stream_resumes_after_last_event_id_header_or_after_param_with_no_duplicates(api):
    run_id = api.run_sample("H")
    everything = [int(e[0]) for e in sse(api.client, f"/api/runs/{run_id}/stream") if e[1] == "stage"]
    cut = everything[5]
    by_header = [int(e[0]) for e in sse(api.client, f"/api/runs/{run_id}/stream", {"Last-Event-ID": str(cut)}) if e[1] == "stage"]
    by_param = [int(e[0]) for e in sse(api.client, f"/api/runs/{run_id}/stream?after={cut}") if e[1] == "stage"]
    assert by_header == by_param == everything[6:]
    assert sse(api.client, f"/api/runs/{run_id}/stream?after={everything[-1]}")[-1][1] == "end"   # nothing left: just the end


def test_stream_of_an_unknown_run_is_404(api):
    assert api.client.get("/api/runs/999/stream").status_code == 404


def test_new_events_become_visible_incrementally_and_status_is_read_before_events(api, monkeypatch):
    monkeypatch.setattr("app.api.runs.execute_run", lambda *a, **k: None)
    run_id = api.client.post("/api/runs", json={"sample": "H"}).json()["run_id"]
    state = api.app.state.ctx
    assert read_new_events(state, run_id, 0) == ("queued", None, [])
    with state.session_factory() as s:
        repo.add_stage_event(s, run_id, "ingest", "started")
        repo.add_stage_event(s, run_id, "ingest", "completed", output={"doc_type": "text", "page_count": 1}, duration_ms=5)
        s.commit()
    status, _, events = read_new_events(state, run_id, 0)
    assert status == "queued" and [e["status"] for e in events] == ["started", "completed"]   # not final: stream stays open
    assert read_new_events(state, run_id, events[-1]["id"])[2] == []
    assert views.FINAL_STATUSES == ("completed", "failed")


# ---------------------------------------------------------------- document, reference, reset
def test_page_png_renders_page_one_for_text_and_scanned_documents(api):
    for code in ("H", "E3"):
        run_id = api.run_sample(code)
        response = api.client.get(f"/api/runs/{run_id}/page.png")
        assert response.status_code == 200 and response.headers["content-type"] == "image/png"
        assert response.content.startswith(b"\x89PNG")
    assert api.client.get("/api/runs/999/page.png").status_code == 404


def test_reference_data_masks_bank_accounts_and_shows_po_consumption(api):
    ref = api.client.get("/api/reference").json()
    v5 = next(v for v in ref["vendors"] if v["vendor_id"] == "V005")
    assert v5["bank_account"] == "XXXXXX4417" and v5["state"] == "Karnataka" and v5["status"] == "active"
    po = next(p for p in ref["pos"] if p["po_id"] == "PO-2041")
    assert (po["subtotal"], po["approved_to_date"], po["utilisation_pct"]) == ("500000.00", "375000.00", "75.0")
    assert po["vendor"] == "Kapoor Fasteners & Fittings Pvt Ltd" and len(ref["pos"]) == 7


def test_reset_clears_history_and_restores_seed_state(api):
    api.run_sample("H")
    api.run_sample("E5")
    assert api.client.post("/api/reset").json() == {"status": "reset"}
    assert api.client.get("/api/runs").json()["runs"] == []
    pos = {p["po_id"]: p for p in api.client.get("/api/reference").json()["pos"]}
    assert pos["PO-2038"]["approved_to_date"] == "0" and pos["PO-2041"]["approved_to_date"] == "375000.00"
    assert api.detail(api.run_sample("H"))["decision"]["decision"] == "Approve"       # same scenario works again


def test_no_response_ever_contains_a_full_bank_account_number(api):
    run_id = api.run_sample("E4")
    api.run_sample("H")
    bodies = [api.client.get(u).text for u in ("/api/runs", f"/api/runs/{run_id}", "/api/reference", "/api/samples")]
    bodies.append(json.dumps(sse(api.client, f"/api/runs/{run_id}/stream")))
    for body in bodies:
        for account in ALL_ACCOUNTS:
            assert account not in body
    assert "XXXXXX4417" in bodies[1]                           # the master account, masked, in the BANK_MATCH evidence


# ---------------------------------------------------------------- serving the built frontend
def test_spa_fallback_serves_index_and_assets_but_never_html_for_api_routes(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>app</html>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    client = Api(tmp_path).client
    assert client.get("/").text == "<html>app</html>"
    assert client.get("/dashboard").text == "<html>app</html>"
    assert client.get("/runs/12").text == "<html>app</html>"
    assert client.get("/assets/app.js").text == "console.log(1)"
    assert client.get("/api/nope").status_code == 404 and "<html" not in client.get("/api/nope").text
    assert client.get("/%2e%2e/%2e%2e/etc/passwd").text == "<html>app</html>"          # no path traversal


# ---------------------------------------------------------------- LLM / API failure: plain message, red stage, fail-safe Hold
def llm_with_sdk_error(error):
    from types import SimpleNamespace
    from app.llm.client import LLMClient

    def boom(**kwargs):
        raise error
    return LLMClient(Settings(), sdk_client=SimpleNamespace(messages=SimpleNamespace(parse=boom)))


def test_an_ai_service_outage_shows_a_plain_message_a_failed_stage_and_a_fail_safe_hold(api):
    import anthropic
    import httpx2
    api.llm = llm_with_sdk_error(anthropic.APITimeoutError(request=httpx2.Request("POST", "https://example.invalid")))
    run_id = api.client.post("/api/runs", json={"sample": "H"}).json()["run_id"]
    d = api.detail(run_id)
    extract = next(s for s in d["stages"] if s["stage"] == "extract")
    plain = "The AI service didn't respond. The invoice was held; try again."
    assert d["status"] == "failed" and extract["status"] == "failed"
    assert extract["error"] == plain and extract["summary"] == plain             # what the UI shows, no stack-trace text
    assert "APITimeoutError" in extract["output"]["technical_error"]              # kept for the audit trail, not displayed
    assert d["decision"]["decision"] == "Hold" and d["decision"]["ledger_inserted"] is False
    assert any(c["code"] == "STAGE_FAILED" and c["result"] == "FAIL" for c in d["checks"])
    assert [s["status"] for s in d["stages"] if s["stage"] in ("validate", "match")] == ["skipped", "skipped"]


def test_a_rate_limit_is_explained_as_such(api):
    import anthropic
    import httpx2
    request = httpx2.Request("POST", "https://example.invalid")
    api.llm = llm_with_sdk_error(anthropic.RateLimitError("slow down", response=httpx2.Response(429, request=request), body=None))
    d = api.detail(api.client.post("/api/runs", json={"sample": "H"}).json()["run_id"])
    assert next(s for s in d["stages"] if s["stage"] == "extract")["error"] == \
        "The AI service is busy right now (rate limit reached). The invoice was held; try again."


def test_a_failed_explanation_says_the_decision_is_unaffected_not_that_the_invoice_was_held(api):
    from app.llm.client import LLMError
    from app.schemas import Explanation
    api.llm = scenario_llm("H")
    api.llm.responses[Explanation] = LLMError("explain: timeout")
    d = api.detail(api.client.post("/api/runs", json={"sample": "H"}).json()["run_id"])
    explain = next(s for s in d["stages"] if s["stage"] == "explain")
    assert explain["status"] == "failed" and explain["error"] == "The AI service didn't respond."
    assert d["decision"]["decision"] == "Approve"                                  # the rules' decision stands


# ---------------------------------------------------------------- server restart while a run is in progress
INTERRUPTED = "Processing was interrupted before it finished."


def _second_process(api):
    """A new server process on the same database."""
    app = create_app(api.settings, llm_factory=lambda: None, today=lambda: TODAY)
    return app, TestClient(app)


def test_runs_left_queued_or_running_by_a_dead_process_become_failed_with_a_hold_on_restart(api, monkeypatch):
    monkeypatch.setattr("app.api.runs.execute_run", lambda *a, **k: None)           # the process "dies" before working
    queued = api.client.post("/api/runs", json={"sample": "H"}).json()["run_id"]
    running = api.client.post("/api/runs", json={"sample": "E1"}).json()["run_id"]
    with api.app.state.ctx.session_factory() as s:                                    # E1 had got as far as ingest
        repo.update_run(s, running, status="running")
        repo.add_stage_event(s, running, "ingest", "started")
        repo.add_stage_event(s, running, "ingest", "completed", output={"doc_type": "text", "page_count": 1}, duration_ms=5)
        repo.add_stage_event(s, running, "extract", "started")                        # ... and was waiting on the AI
        s.commit()
    assert api.detail(queued)["status"] == "queued"

    app2, client2 = _second_process(api)
    ingest_of_running = next(s for s in client2.get(f"/api/runs/{running}").json()["stages"] if s["stage"] == "ingest")
    assert ingest_of_running["status"] == "completed"                                 # finished work is not rewritten

    for run_id in (queued, running):
        d = client2.get(f"/api/runs/{run_id}").json()
        pipeline = next(s for s in d["stages"] if s["stage"] == "pipeline")
        assert d["status"] == "failed"                                                # never "completed", never "running"
        assert pipeline["status"] == "failed" and pipeline["error"] == INTERRUPTED
        assert "running" not in {s["status"] for s in d["stages"]}                    # no stage is left "running" forever
        assert d["decision"]["decision"] == "Hold" and d["decision"]["ledger_inserted"] is False
        assert any(c["code"] == "STAGE_FAILED" and c["result"] == "FAIL" and INTERRUPTED in c["message"] for c in d["checks"])
        assert d["decision"]["summary"]                                               # something readable is shown
        assert read_new_events(app2.state.ctx, run_id, 0)[0] == "failed"              # so an open SSE stream can end
    extract = next(s for s in client2.get(f"/api/runs/{running}").json()["stages"] if s["stage"] == "extract")
    assert extract["status"] == "failed" and extract["error"] == INTERRUPTED           # the stage it was in did not complete
    ids = {r["id"]: r for r in client2.get("/api/runs").json()["runs"]}
    assert ids[running]["decision"] == "Hold" and ids[running]["top_code"] == "STAGE_FAILED"
    assert client2.get("/api/runs").json()["kpis"]["held"] == 2
    assert sse(client2, f"/api/runs/{queued}/stream")[-1][1] == "end"                 # the stream closes instead of hanging


def test_restart_leaves_finished_runs_alone_and_keeps_a_decision_that_was_already_committed(api):
    done = api.run_sample("E1")                                                       # a finished Hold
    approved = api.run_sample("H")                                                    # an Approve already in the ledger
    with api.app.state.ctx.session_factory() as s:                                    # ... that died during "explain"
        repo.update_run(s, approved, status="running")
        s.commit()
    before = api.detail(done)

    _, client2 = _second_process(api)

    assert client2.get(f"/api/runs/{done}").json() == before                          # untouched
    d = client2.get(f"/api/runs/{approved}").json()
    assert d["status"] == "failed" and d["decision"]["decision"] == "Approve"          # the committed decision is not rewritten
    assert d["decision"]["ledger_inserted"] is True
    po = next(p for p in client2.get("/api/reference").json()["pos"] if p["po_id"] == "PO-2038")
    assert po["approved_to_date"] == "111460.00"                                      # ledger and run still agree
    assert any(c["code"] == "STAGE_FAILED" for c in d["checks"])
