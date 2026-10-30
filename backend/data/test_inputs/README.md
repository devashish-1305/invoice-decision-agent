# Prepared test inputs

Edge-case files for live demos. They are **not** sample cards and do not affect the six scenarios.
Regenerate with `cd backend && python scripts/generate_test_inputs.py` (this file is regenerated too).
Use them from **New run → drop an invoice PDF here**, or with `curl -F file=@<file> http://localhost:8000/api/runs`.

## Invalid uploads: rejected at upload, before any run exists and before any LLM call

Expected for every file below: HTTP 400, a plain message (what is wrong and what to do), no run created, zero LLM calls.

| File | What it is | Message shown |
|---|---|---|
| `not_a_pdf.docx` | A real Word document | This isn't a PDF file. Upload the invoice as a PDF. |
| `invoice_photo.png` | A PNG image of an invoice | This isn't a PDF file. Upload the invoice as a PDF. |
| `empty.pdf` | A 0-byte file | This file is empty (0 bytes). Choose the invoice PDF and try again. |
| `corrupted.pdf` | A PDF cut off half way (a broken download) | This PDF is damaged and can't be read. Upload another copy of the invoice. |
| `password_protected.pdf` | An invoice PDF locked with a password (`demo`) | This file is password-protected. Upload an unlocked PDF. |
| `too_many_pages.pdf` | A 6-page PDF (limit is 5) | This PDF has 6 pages; the limit is 5. Upload a PDF of 5 pages or fewer (one invoice per file). |
| `too_large.pdf` | A PDF over 10 MB (about 10.0 MB of padding after a valid invoice) | This file is larger than 10 MB. Upload a PDF under 10 MB. |

## Valid but problematic invoices: the full pipeline runs (these use the real LLM)

| File | Expected decision | Triggered check | Notes |
|---|---|---|---|
| `missing_invoice_number.pdf` | Hold | `REQUIRED_FIELDS` | Vendor, PO and bank details are all correct, but the invoice number is missing. The extraction table shows "Not on the document" for it, and the duplicate checks report "not evaluated" (they need a number). |
| `unknown_po.pdf` | Hold | `PO_FOUND` | The invoice prints PO-9999, which is not in the system. Line, amount and cumulative checks report "not evaluated" because there is no PO to compare with. |
| `prompt_injection.pdf` | Hold, high risk | `BANK_MATCH` | The bank account differs from the vendor master. A visible line on the invoice tells the AI to approve it; the instruction is ignored: no extracted value or check changes, and the summary must not present the instruction as a fact. |

In every case the decision comes from the deterministic rules, and none of these can ever be approved.

## Known limitations (deliberate, out of scope)

- **One invoice per PDF.** A PDF containing several invoices is not split; the extractor reads it as one document.
- **INR / GST only.** There is no currency conversion and no check that the currency is INR.
- Prompt injection is defended by design (the AI never decides) and by the prompts; extracted values are not cross-checked against the PDF's text layer.
