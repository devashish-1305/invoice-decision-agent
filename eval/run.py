"""Run the nine labelled invoices through the real pipeline with the REAL LLM and write eval/results.md.

    python -m eval.ground_truth      # once, before any run: freeze the labels
    python -m eval.run               # needs ANTHROPIC_API_KEY in backend/.env

Safety: this always uses a new temporary SQLite file, set BEFORE the app is imported, whatever DATABASE_URL says.
It never opens the default database and never calls a deployed app. Every invoice starts from a reset, so one
result cannot change the next.

Measurement: the LLM client is unchanged. Its SDK object is wrapped so each API attempt records the usage metadata
the API returned, and the parsed extraction is kept in memory because the stored copy has the bank account masked.
"""
import hashlib
import os
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from statistics import median

_scratch = tempfile.TemporaryDirectory(prefix="invoice-eval-")
SCRATCH_URL = f"sqlite:///{_scratch.name}/eval.db"
if "app.config" in sys.modules:
    raise SystemExit("app.config was imported before the scratch database was set; refusing to run")
os.environ["DATABASE_URL"] = SCRATCH_URL

from eval import ground_truth  # noqa: E402  (puts backend/ on the path)
from eval.scoring import FIELDS, assert_safe, score_decision, score_extraction, totals, usage_text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.seed import reset  # noqa: E402
from app.db.session import make_engine, make_session_factory  # noqa: E402
from app.llm.client import LLMClient  # noqa: E402
from app.pipeline.orchestrator import run_pipeline  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results.md"
EVAL_TODAY = date(2026, 11, 2)      # fixed, so the date checks give the same answer whenever this is re-run
PROMPT_NAMES = {"ExtractedInvoice": "extract", "POInference": "infer_po", "LineMatchResponse": "match_lines",
                "Explanation": "explain"}


class UsageRecorder:
    """Stands where LLMClient expects the SDK client. Passes every call through unchanged and records what the API
    reported for it. An attempt that raises has no response to read, so its usage stays None (unavailable)."""

    def __init__(self, sdk):
        self._sdk = sdk
        self.messages = self
        self.calls: list[dict] = []
        self.extracted = None

    def start_invoice(self):
        self.calls, self.extracted = [], None

    def parse(self, **kwargs):
        call = {"prompt": PROMPT_NAMES.get(kwargs["output_format"].__name__, "other"), "ok": False, "error": None,
                "input_tokens": None, "output_tokens": None, "cache_read": None, "cache_write": None, "ms": None}
        self.calls.append(call)
        started = time.perf_counter()
        try:
            response = self._sdk.messages.parse(**kwargs)
        except Exception as e:
            call["error"] = type(e).__name__
            raise
        finally:
            call["ms"] = int((time.perf_counter() - started) * 1000)
        usage = getattr(response, "usage", None)
        call["input_tokens"] = getattr(usage, "input_tokens", None)
        call["output_tokens"] = getattr(usage, "output_tokens", None)
        call["cache_read"] = getattr(usage, "cache_read_input_tokens", None)
        call["cache_write"] = getattr(usage, "cache_creation_input_tokens", None)
        parsed = response.parsed_output
        call["ok"] = parsed is not None and response.stop_reason != "refusal"
        if call["ok"] and type(parsed).__name__ == "ExtractedInvoice":
            self.extracted = parsed
        return response


def check_inputs(cases: list[dict]) -> None:
    """Stop if the labels or PDFs differ from what was frozen before this run."""
    if cases != ground_truth.build():
        raise SystemExit("eval/ground_truth.json no longer matches the generators or PDFs. "
                         "Do not re-freeze to make a run pass; find out what changed.")


def run_one(case: dict, account: str, settings, engine, factory, llm, recorder) -> dict:
    reset(engine, factory, settings)
    recorder.start_invoice()
    expected = case["expected"]
    row = {"id": case["id"], "title": case["title"], "expected": expected, "expected_decision": expected["decision"],
           "actual_decision": None, "risk": None, "triggered": [], "failed_stages": [], "harness_error": None,
           "status": "failed", "pipeline_ms": None, "inferred_po": None, "failure_text": []}
    not_evaluated = []
    pdf = (ground_truth.ROOT / case["file"]).read_bytes()
    try:
        with factory() as session:
            result = run_pipeline(session, llm, pdf, Path(case["file"]).name, settings, today=EVAL_TODAY)
        row.update(status=result.status, pipeline_ms=result.duration_ms,
                   failed_stages=[f.stage for f in result.failures],
                   failure_text=[f"{f.stage}: {f.error}" for f in result.failures])
        if result.decision:
            row.update(actual_decision=result.decision.decision, risk=result.decision.risk_level,
                       triggered=list(dict.fromkeys(result.decision.triggered_codes)))
        not_evaluated = [c.code for c in result.checks if c.not_evaluated]
        po = next((c for c in result.checks if c.code == "PO_INFERRED"), None)
        if po is not None and expected["inferred_po"]:
            row["inferred_po"] = po.evidence.get("suggested_po")
    except Exception as e:  # noqa: BLE001 - a crash is a failed run and is counted, never dropped
        row["harness_error"] = type(e).__name__
        row["failed_stages"] = ["pipeline crashed"]
        row["failure_text"] = [f"pipeline crashed: {type(e).__name__}"]

    invoice = None if "extract" in row["failed_stages"] or row["harness_error"] else recorder.extracted
    row.update(score_extraction(case["truth"], account, invoice))
    row.update(score_decision(expected, row["actual_decision"], row["risk"], row["triggered"], not_evaluated,
                              row["failed_stages"]))
    row["calls"] = recorder.calls
    return row


# ----------------------------------------------------------------------------- report
def pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.1f}%" if whole else "n/a"


def table(header: list[str], rows: list[list]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    return lines + ["| " + " | ".join(str(v) for v in row) + " |" for row in rows]


def seconds(ms) -> str:
    return "n/a" if ms is None else f"{ms / 1000:.1f}"


def render(rows: list[dict], settings, frozen_sha: str, commit: str) -> str:
    t = totals(rows)
    calls = [c for r in rows for c in r["calls"]]
    failed_calls = [c for c in calls if not c["ok"]]
    timed = [r["pipeline_ms"] for r in rows if r["pipeline_ms"] is not None]
    out = [
        "# Evaluation results [LIVE]", "",
        f"Real LLM (`{settings.llm_model}`, effort `{settings.llm_effort}`), run on {EVAL_TODAY.isoformat()} against "
        f"commit `{commit}`.", "",
        "**Read this first.**", "",
        "- **These 9 synthetic invoices are not representative of production performance.** They were generated by "
        "this project's own scripts. Eight have a clean text layer, which is close to the easiest case for the "
        "model; only one is a scan. Most were built to trigger a specific check, so the approve/hold mix says "
        "nothing about a real invoice mix.",
        "- **This is a single-run snapshot.** LLM extraction can vary between runs, so another run may give different "
        "numbers.",
        "- **A correct decision does not imply every field was extracted correctly.** The two are reported separately "
        "below.", "",
        "## Summary", "",
    ]
    out += table(["Measure", "Result"], [
        ["Invoices run", t["invoices"]],
        ["Runs succeeded / failed", f"{t['runs_ok']} / {t['runs_failed']}"],
        ["LLM API attempts: succeeded / failed", f"{len(calls) - len(failed_calls)} / {len(failed_calls)}"],
        ["Extraction: fields correct", f"{t['fields_correct']} of {t['fields_total']} ({pct(t['fields_correct'], t['fields_total'])})"],
        ["Extraction: invoices with all 9 fields correct", f"{t['all_fields_right']} of {t['invoices']}"],
        ["Decision correct", f"{t['decisions_ok']} of {t['invoices']}"],
        ["Decision and triggering check both correct", f"{t['decisions_and_checks_ok']} of {t['invoices']}"],
        ["False approvals (approved, should not have been)", t["false_approvals"]],
        ["False holds (held, should have been approved)", t["false_holds"]],
    ])
    if t["runs_failed"]:
        out += ["", "**Failed runs are included in every total above.** Their unread fields count as wrong, and a Hold "
                    "caused by a failed stage before the decision does not count as a correct decision."]
        for r in rows:
            for text in r["failure_text"]:
                out.append(f"- {r['id']}: {text}")

    out += ["", "## Extraction accuracy", "", "Did the model read the invoice correctly? Each field is compared with "
            "the values the invoice was generated from.", ""]
    out += table(["Field", "Correct", "Accuracy"],
                 [[name, f"{t['per_field'][name]} of {t['invoices']}", pct(t["per_field"][name], t["invoices"])]
                  for name in FIELDS]
                 + [["**Overall**", f"**{t['fields_correct']} of {t['fields_total']}**",
                     f"**{pct(t['fields_correct'], t['fields_total'])}**"]])
    out += ["", f"Line-item cells (description, quantity, unit price, amount; charge description, amount): "
                f"{t['cells_correct']} of {t['cells_total']} ({pct(t['cells_correct'], t['cells_total'])}).", "",
            "By invoice:", ""]
    out += table(["Invoice"] + FIELDS,
                 [[r["id"]] + ["✓" if r["fields"][name]["ok"] else "✗" for name in FIELDS] for r in rows])
    misses = [f"- {r['id']} `{name}`: {r['fields'][name]['detail']}" for r in rows for name in FIELDS
              if not r["fields"][name]["ok"]]
    out += ["", "Mismatches:", ""] + (misses or ["None."])

    out += ["", "## Decision accuracy", "", "Expected values come from the generators' own scenario definitions. Each "
            "invoice starts from a database reset, so no invoice affects another.", ""]
    decision_rows = []
    for r in rows:
        e = r["expected"]
        want = e["decision"] + (f", {e['risk_level']} risk" if e["risk_level"] == "high" else "")
        got = (r["actual_decision"] or "none") + (", high risk" if r["risk"] == "high" else "")
        result = "✓" if r["decision_ok"] and r["check_ok"] else ("decision ✓, check ✗" if r["decision_ok"] else "✗")
        decision_rows.append([r["id"], r["title"], want, got, f"`{e['check']}`" if e["check"] else "none",
                              ", ".join(f"`{c}`" for c in r["triggered"]) or "none", result])
    out += table(["ID", "Invoice", "Expected", "Actual", "Expected check", "Checks triggered", "Result"], decision_rows)
    inferred = [r for r in rows if r["expected"]["inferred_po"]]
    if inferred:
        out.append("")
    for r in inferred:
        want, got = r["expected"]["inferred_po"], r["inferred_po"]
        out.append(f"{r['id']} has no printed PO. Inferred PO: {got or 'none'}; correct PO: {want} "
                   f"({'match' if got == want else 'NO MATCH'}). The invoice holds either way.")

    out += ["", "## LLM calls, time and tokens", "", "Tokens are the `usage` values returned by the API for each call. "
            "Nothing is estimated.", ""]
    usage_rows = []
    for r in rows:
        steps = ", ".join(f"{p} ×{n}" for p in PROMPT_NAMES.values()
                          if (n := sum(c["prompt"] == p for c in r["calls"]))) or "none"
        usage_rows.append([r["id"], len(r["calls"]), steps, seconds(r["pipeline_ms"]),
                           seconds(sum(c["ms"] or 0 for c in r["calls"])),
                           usage_text(r["calls"], "input_tokens"), usage_text(r["calls"], "output_tokens")])
    usage_rows.append(["**Total**", f"**{len(calls)}**", "", f"**{seconds(sum(timed))}**",
                       f"**{seconds(sum(c['ms'] or 0 for c in calls))}**", f"**{usage_text(calls, 'input_tokens')}**",
                       f"**{usage_text(calls, 'output_tokens')}**"])
    out += table(["Invoice", "LLM calls", "Steps", "Pipeline (s)", "Waiting on LLM (s)", "Input tokens", "Output tokens"],
                 usage_rows)
    if timed:
        out += ["", f"Pipeline time per invoice: median {seconds(median(timed))} s, slowest {seconds(max(timed))} s, "
                    f"from {len(timed)} timed run(s). Runs were sequential."]
    complete = [c for c in calls if c["input_tokens"] is not None and c["output_tokens"] is not None]
    if rows and len(complete) == len(calls) and calls:
        out.append(f"Tokens per invoice (mean across {len(rows)}): "
                   f"{sum(c['input_tokens'] for c in calls) / len(rows):,.0f} input, "
                   f"{sum(c['output_tokens'] for c in calls) / len(rows):,.0f} output.")
    cache = sum((c["cache_read"] or 0) + (c["cache_write"] or 0) for c in calls)
    if cache:
        out.append(f"Prompt-cache tokens reported separately by the API: {cache:,}.")
    out += ["", "\"LLM calls\" counts each attempt made by the project's LLM client, including a retry after unusable "
                "output. Retries the Anthropic SDK makes by itself on network errors, rate limits and server errors "
                "happen below this level and are not counted."]
    if len(complete) < len(calls):
        out += ["", f"**Usage was unavailable for {len(calls) - len(complete)} of {len(calls)} calls**, so their tokens "
                    "are not in the totals. An attempt that raises an exception returns no response to read usage from. "
                    "Measuring those would need the raw HTTP response (the SDK's `with_raw_response`) to be read "
                    "before parsing."]

    out += ["", "## How this was measured", "",
            f"- **Invoices:** the 6 scenario PDFs and the 3 valid test inputs (missing_invoice_number, unknown_po, "
            f"prompt_injection), run once each through `run_pipeline`. The upload rejections need no LLM and are "
            f"covered by the automated tests.",
            f"- **Labels:** taken from the generators' data and frozen in `eval/ground_truth.json` before this run "
            f"(sha256 `{frozen_sha}`). No rule, threshold or prompt was changed for this evaluation.",
            "- **Text fields** (GSTIN, invoice number, PO reference): exact match. Where the document prints no value, "
            "only null is correct.",
            "- **Amounts and dates:** exact value. Tax is correct only if every printed tax line is read correctly.",
            "- **Bank account:** the full numbers are compared in memory; only match / no match is reported. Masked "
            "values would not be enough, because two accounts can share their last four digits. IFSC, vendor name and "
            "currency are not scored here.",
            "- **Line items:** correct only if every product line and charge is right in all its cells. Descriptions "
            "ignore case and spacing.",
            "- **Decision correct:** the outcome matches (and the risk level, where the scenario states one), and no "
            "stage before the decision failed.",
            "- **Triggering check correct:** the expected check fired and nothing unexplained fired beside it. "
            "\"Not evaluated\" warnings are allowed because they follow from the expected failure, and the scan may "
            "also show low-confidence warnings.",
            "- **Not scored:** the AI-written summary and drafted message, including on prompt_injection, where the "
            "scores show only that the extracted fields and the decision were unaffected.",
            "- **Isolation:** a temporary database, reset before each invoice and deleted afterwards. The default "
            "database and the deployed application were not touched.", ""]
    return "\n".join(out)


def main():
    settings = get_settings()
    if settings.database_url != SCRATCH_URL:
        raise SystemExit("Not running on the scratch database; stopping before anything is written.")

    cases = ground_truth.load_frozen()
    check_inputs(cases)
    accounts = ground_truth.full_accounts()
    frozen_sha = hashlib.sha256(ground_truth.GROUND_TRUTH.read_bytes()).hexdigest()
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ground_truth.ROOT, capture_output=True,
                            text=True).stdout.strip() or "unknown"

    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    llm = LLMClient(settings)
    recorder = UsageRecorder(llm._sdk())    # same SDK client, timeout and retries as the app
    llm._sdk_client = recorder

    rows = []
    for case in cases:
        print(f"Running {case['id']} ({case['title']}) ...", flush=True)
        row = run_one(case, accounts[case["id"]], settings, engine, factory, llm, recorder)
        rows.append(row)
        print(f"  status={row['status']} decision={row['actual_decision']} expected={case['expected']['decision']} "
              f"checks={row['triggered']} fields={sum(f['ok'] for f in row['fields'].values())}/{len(FIELDS)} "
              f"calls={len(row['calls'])}", flush=True)
    engine.dispose()
    check_inputs(cases)                     # the PDFs and labels are still what they were

    report = render(rows, settings, frozen_sha, commit)
    assert_safe(report, [*accounts.values(), os.environ.get("ANTHROPIC_API_KEY", "")])
    RESULTS.write_text(report, encoding="utf-8")
    t = totals(rows)
    print(f"\nRuns ok/failed: {t['runs_ok']}/{t['runs_failed']}   fields: {t['fields_correct']}/{t['fields_total']}   "
          f"decisions: {t['decisions_ok']}/{t['invoices']}   with check: {t['decisions_and_checks_ok']}/{t['invoices']}")
    print(f"Wrote {RESULTS}")


if __name__ == "__main__":
    main()
