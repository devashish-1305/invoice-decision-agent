"""Ground truth for the evaluation, taken from the data that generated the PDFs, never from a model's output.

    python -m eval.ground_truth      # freeze: writes eval/ground_truth.json (run this BEFORE any evaluation)

Nine invoices: the six scenarios (generate_invoices.SCENARIOS) and the three valid test inputs
(generate_test_inputs.VALID_SPECS / VALID_EXPECTATIONS). Freezing first proves the PDFs on disk are exactly what
the generator produces, so the labels describe the files being read. eval.run refuses to start if the labels or
the PDFs have changed since.

Bank accounts are masked in the JSON. The full numbers come from full_accounts() at run time and stay in memory.
"""
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend" / "scripts")]

import generate_invoices as gen  # noqa: E402
import generate_test_inputs as test_inputs  # noqa: E402
from app.util import mask_account  # noqa: E402

GROUND_TRUTH = Path(__file__).resolve().parent / "ground_truth.json"


def _sources() -> list:
    """(case id, title, invoice, path, expected decision text, expected check, rendered bytes function)"""
    out = []
    for spec in gen.SCENARIOS:
        out.append((spec["id"], spec["title"], gen.build_invoice(spec), gen.OUT / spec["file"], spec["decision"],
                    spec["check"]))
    for name, invoice in test_inputs.valid_invoices().items():
        decision, check, _ = test_inputs.VALID_EXPECTATIONS[name]
        out.append((invoice.spec["id"], name.removesuffix(".pdf"), invoice, test_inputs.OUT / name, decision, check))
    return out


def _rendered(invoice) -> bytes:
    """What the generator produces for this invoice right now."""
    if invoice.spec.get("scanned"):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / invoice.spec["file"]    # Pillow writes the file name into the PDF title
            gen.scan_effect(gen.render_pdf_bytes(invoice), path)
            return path.read_bytes()
    if invoice.spec["id"].startswith("T"):
        return test_inputs.render(invoice)
    return gen.render_pdf_bytes(invoice)


def _truth(invoice) -> dict:
    spec = invoice.spec
    return {
        "vendor_gstin": invoice.vendor["gstin"],
        "invoice_number": spec["number"],                                     # None: not printed on the document
        "invoice_date": spec["date"].isoformat(),
        "po_reference": spec["po"] if spec["show_po"] else None,
        "bank_account": mask_account(invoice.bank_account),
        "subtotal": str(invoice.subtotal),
        "tax": str(invoice.tax),
        "tax_lines": [{"description": label, "amount": str(amount)} for label, amount in invoice.tax_lines],
        "total": str(invoice.total),
        "line_items": [{"description": l.description, "qty": str(l.qty), "unit_price": str(l.rate),
                        "amount": str(l.amount)} for l in invoice.lines],
        "other_charges": [{"description": label, "amount": str(amount)} for label, amount in invoice.charges],
    }


def _expected(invoice, decision_text: str, check: str) -> dict:
    spec = invoice.spec
    decision = re.split(r"[ ,(]", decision_text)[0]                           # "Hold (high risk)" -> "Hold"
    if "high risk" in decision_text:
        risk = "high"
    else:
        risk = "none" if decision == "Approve" else None                      # None: the generator states no risk
    return {
        "decision": decision,
        "risk_level": risk,
        "check": None if check.startswith("none") else check,
        "also_allowed": ["EXTRACTION_CONFIDENCE"] if spec.get("scanned") else [],  # PRD section 6: a scan may warn
        "inferred_po": None if spec["show_po"] else spec["po"],
    }


def build() -> list[dict]:
    """The labelled cases, in run order. Contains no full bank account."""
    return [{"id": case_id, "title": title, "file": str(path.relative_to(ROOT)),
             "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "truth": _truth(invoice),
             "expected": _expected(invoice, decision, check)}
            for case_id, title, invoice, path, decision, check in _sources()]


def full_accounts() -> dict[str, str]:
    """case id -> full bank account on the invoice. In memory only: never write this anywhere."""
    return {case_id: invoice.bank_account for case_id, _, invoice, *_ in _sources()}


def load_frozen() -> list[dict]:
    return json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))


def main():
    for case_id, _, invoice, path, *_ in _sources():
        if path.read_bytes() != _rendered(invoice):
            raise SystemExit(f"{case_id}: {path} is not what the generator produces. Labels would be wrong; stopping.")
    GROUND_TRUTH.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    digest = hashlib.sha256(GROUND_TRUTH.read_bytes()).hexdigest()
    print(f"Wrote {GROUND_TRUTH} ({len(build())} invoices), sha256 {digest}")


if __name__ == "__main__":
    main()
