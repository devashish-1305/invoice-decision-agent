"""Run the six demo scenarios (H, E1-E5) end to end with the REAL LLM and compare with the expected decisions.

    cd backend && python scripts/run_scenarios.py              # reset, run all six once
    cd backend && python scripts/run_scenarios.py --repeat 3   # reset + all six, three times in a row

Needs ANTHROPIC_API_KEY in backend/.env. Each repeat starts with an explicit reset (the same thing
POST /api/reset will do), so every pass starts from the seed state. Expected decisions come from
generate_invoices.SCENARIOS, the same source the sample PDFs were built from.
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make `app` importable

import generate_invoices as gen  # noqa: E402  (sits next to this script)

from app.config import get_settings  # noqa: E402
from app.db import repository as repo  # noqa: E402
from app.db.seed import reset  # noqa: E402
from app.db.session import make_engine, make_session_factory  # noqa: E402
from app.llm.client import LLMClient  # noqa: E402
from app.pipeline.orchestrator import run_pipeline  # noqa: E402


def run_once(settings, engine, factory, llm, repeat_no):
    reset(engine, factory, settings)
    print(f"\n=== Pass {repeat_no}: after reset ===")
    header = ("Scenario", "Expected", "Actual", "Failing / warning checks", "LLM calls", "Duration")
    rows, outcomes, e3_note = [], {}, None
    with factory() as session:
        for spec in gen.SCENARIOS:
            expected = spec["decision"].split(" (")[0]
            pdf = (gen.OUT / spec["file"]).read_bytes()
            result = run_pipeline(session, llm, pdf, spec["file"], settings, today=date.today())
            actual = result.decision.decision if result.decision else "-"
            flag = "OK " if actual == expected and result.status == "completed" else "BAD"
            codes = ", ".join(result.decision.triggered_codes) if result.decision else "-"
            if result.failures:
                codes += "  [FAILED: " + ", ".join(f.stage for f in result.failures) + "]"
            rows.append((spec["id"], expected, f"{flag} {actual}", codes or "none",
                         str(result.llm_calls), f"{result.duration_ms / 1000:.1f}s"))
            outcomes[spec["id"]] = (actual, tuple(result.decision.triggered_codes) if result.decision else ())
            if spec["id"] == "E3":  # exact GSTIN match is what identifies the vendor: show what the scan gave us
                extract = next((e for e in repo.get_events(session, result.run_id)
                                if e.stage == "extract" and e.status == "completed"), None)
                read = extract.output["invoice"]["vendor_gstin"]["value"] if extract else None
                master = gen.VENDORS[spec["vendor"]]["gstin"]
                e3_note = f"E3 GSTIN  extracted: {read}   vendor master: {master}   exact match: {read == master}"
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    for r in [header, *rows]:
        print("  " + "  ".join(v.ljust(widths[i]) for i, v in enumerate(r)))
    if e3_note:
        print("\n  " + e3_note)
    return outcomes, all(r[2].startswith("OK") for r in rows)


def main():
    parser = argparse.ArgumentParser(description="Run all six scenarios with the real LLM.")
    parser.add_argument("--repeat", type=int, default=1, help="number of reset + full passes (default 1)")
    args = parser.parse_args()

    settings = get_settings()
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    llm = LLMClient(settings)

    passes = [run_once(settings, engine, factory, llm, n) for n in range(1, args.repeat + 1)]
    all_ok = all(ok for _, ok in passes)
    stable = all(outcome == passes[0][0] for outcome, _ in passes)
    print(f"\nPasses with every decision as expected: {sum(ok for _, ok in passes)}/{len(passes)}")
    if args.repeat > 1:
        print(f"Identical decisions and triggered checks across passes: {stable}")
    print(f"LLM calls in total: {len(llm.calls)}")
    sys.exit(0 if all_ok and stable else 1)


if __name__ == "__main__":
    main()
