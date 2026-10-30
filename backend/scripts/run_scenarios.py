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
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make `app` importable

import generate_invoices as gen  # noqa: E402  (sits next to this script)

from app.config import get_settings  # noqa: E402
from app.db import repository as repo  # noqa: E402
from app.db.seed import reset  # noqa: E402
from app.db.session import make_engine, make_session_factory  # noqa: E402
from app.llm.client import LLMClient  # noqa: E402
from app.pipeline.orchestrator import run_pipeline  # noqa: E402
from app.util import mask_account  # noqa: E402


def diff_extraction(spec, dump):
    """Compare what the LLM extracted (the stored, masked dump) with the invoice the generator produced."""
    inv = gen.build_invoice(spec)
    diffs = []

    def same(a, b):
        try:
            return Decimal(str(a)) == Decimal(str(b))
        except (InvalidOperation, TypeError):
            return str(a).strip() == str(b).strip() if a is not None and b is not None else a == b

    expected = {"vendor_name": inv.vendor["name"], "vendor_gstin": inv.vendor["gstin"],
                "invoice_number": spec["number"], "invoice_date": str(spec["date"]),
                "po_reference": spec["po"] if spec["show_po"] else None,
                "bank_account": mask_account(inv.bank_account), "ifsc": inv.vendor["ifsc"],
                "subtotal": inv.subtotal, "total": inv.total, "currency": "INR"}
    for name, want in expected.items():
        got = dump[name]["value"]
        if not same(got, want):
            diffs.append(f"{name}: extracted {got!r}, invoice has {want!r}")
    if len(dump["line_items"]) != len(inv.lines):
        diffs.append(f"line count: extracted {len(dump['line_items'])}, invoice has {len(inv.lines)}")
    for i, (got, want) in enumerate(zip(dump["line_items"], inv.lines), 1):
        for key, exp in (("description", want.description), ("qty", want.qty), ("unit_price", want.rate),
                         ("amount", want.amount)):
            if not same(got[key], exp):
                diffs.append(f"line {i} {key}: extracted {got[key]!r}, invoice has {exp!r}")
    got_charges = [(c["description"], c["amount"]) for c in dump["other_charges"]]
    if len(got_charges) != len(inv.charges) or any(
            not same(g[1], w[1]) for g, w in zip(got_charges, inv.charges)):
        diffs.append(f"other charges: extracted {got_charges}, invoice has {[(l, str(a)) for l, a in inv.charges]}")
    got_tax = sum((Decimal(str(t["amount"])) for t in dump["tax_lines"] if t["amount"] is not None), Decimal(0))
    if got_tax != inv.tax:
        diffs.append(f"tax total: extracted {got_tax}, invoice has {inv.tax}")
    return diffs


def run_once(settings, engine, factory, llm, repeat_no, only=None):
    reset(engine, factory, settings)
    print(f"\n=== Pass {repeat_no}: after reset ===")
    header = ("Scenario", "Expected", "Actual", "Failing / warning checks", "LLM calls", "Tokens in/out", "Duration")
    rows, outcomes, notes = [], {}, []
    with factory() as session:
        for spec in [s for s in gen.SCENARIOS if only is None or s["id"] == only]:
            expected = spec["decision"].split(" (")[0]
            pdf = (gen.OUT / spec["file"]).read_bytes()
            calls_before = len(llm.calls)
            result = run_pipeline(session, llm, pdf, spec["file"], settings, today=date.today())
            used = llm.calls[calls_before:]
            tokens = f"{sum(c.input_tokens for c in used)}/{sum(c.output_tokens for c in used)}"
            actual = result.decision.decision if result.decision else "-"
            flag = "OK " if actual == expected and result.status == "completed" else "BAD"
            codes = ", ".join(result.decision.triggered_codes) if result.decision else "-"
            if result.failures:
                codes += "  [FAILED: " + ", ".join(f.stage for f in result.failures) + "]"
            rows.append((spec["id"], expected, f"{flag} {actual}", codes or "none",
                         str(result.llm_calls), tokens, f"{result.duration_ms / 1000:.1f}s"))
            outcomes[spec["id"]] = (actual, tuple(result.decision.triggered_codes) if result.decision else ())
            events = repo.get_events(session, result.run_id)
            done = {e.stage: e for e in events if e.status == "completed"}
            if "extract" in done:
                for d in diff_extraction(spec, done["extract"].output["invoice"]):
                    notes.append(f"{spec['id']} extraction differs: {d}")
                if not any(n.startswith(spec["id"]) for n in notes):
                    notes.append(f"{spec['id']} extraction: every checked field matches the generated invoice")
            if spec["id"] == "E3":  # exact GSTIN match is what identifies the vendor: show what the scan gave us
                read = done["extract"].output["invoice"]["vendor_gstin"]["value"] if "extract" in done else None
                master = gen.VENDORS[spec["vendor"]]["gstin"]
                notes.append(f"E3 GSTIN  extracted: {read}   vendor master: {master}   exact match: {read == master}")
                po = done["match"].output["po"] if "match" in done else {}
                notes.append(f"E3 PO     method: {po.get('method')}   inferred: {po.get('po_id')}   "
                             f"confidence: {po.get('confidence')}   reason: {po.get('reason')}")
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    for r in [header, *rows]:
        print("  " + "  ".join(v.ljust(widths[i]) for i, v in enumerate(r)))
    for note in notes:
        print("  " + note)
    return outcomes, all(r[2].startswith("OK") for r in rows)


def main():
    parser = argparse.ArgumentParser(description="Run all six scenarios with the real LLM.")
    parser.add_argument("--repeat", type=int, default=1, help="number of reset + full passes (default 1)")
    parser.add_argument("--only", choices=[s["id"] for s in gen.SCENARIOS], help="run just one scenario")
    args = parser.parse_args()

    settings = get_settings()
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    llm = LLMClient(settings)

    passes = [run_once(settings, engine, factory, llm, n, args.only) for n in range(1, args.repeat + 1)]
    all_ok = all(ok for _, ok in passes)
    stable = all(outcome == passes[0][0] for outcome, _ in passes)
    print(f"\nPasses with every decision as expected: {sum(ok for _, ok in passes)}/{len(passes)}")
    if args.repeat > 1:
        print(f"Identical decisions and triggered checks across passes: {stable}")
    print(f"LLM calls in total: {len(llm.calls)}   tokens in/out: "
          f"{sum(c.input_tokens for c in llm.calls)}/{sum(c.output_tokens for c in llm.calls)}")
    sys.exit(0 if all_ok and stable else 1)


if __name__ == "__main__":
    main()
