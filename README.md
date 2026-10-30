# Invoice Decision Agent

Upload a vendor invoice PDF. Get **Approve**, **Hold** or **Reject**, with every step, number and reason visible.

Built as a case study for an accounts-payable team: the AI reads the messy document, plain code makes the money decision, and a clerk (or an auditor) can see exactly why.

## Design principle

> **AI reads the document. Deterministic rules make the payment decision. Every decision is explainable, and every step reports its confidence.**

- The LLM is used for four things only: reading the invoice (including scanned pages, with vision, no OCR library), inferring a PO when none is printed, matching line items by meaning when the wording differs, and writing the plain-English summary and drafted message.
- The LLM **never** decides. Approve / Hold / Reject comes from pure, deterministic Python in `backend/app/rules/`: same input, same decision.
- **Uncertainty holds.** Approve requires every check to pass. A missing or low-confidence field, an inferred PO, a probable duplicate or a failed stage can never produce Approve.
- Every LLM reply is schema-validated. Unparseable output is retried once, then the stage is shown as failed; nothing is guessed.
- Bank account numbers are masked (`XXXXXX4417`) in every response, event, stored payload and drafted message. The vendor master's bank details are never changed automatically.
- Uploaded documents are untrusted: prompts tell the model to ignore instructions inside them, and the rules cannot be influenced by document text.

## The pipeline

`Ingest → Extract → Validate → Match → Rules → Decide → Explain`, each recorded with status, output and duration, and streamed live to the UI.

| Stage | What it does | AI? |
|---|---|---|
| Ingest | Detect a text layer or render scanned pages; enforce limits (PDF, ≤ 10 MB, ≤ 5 pages, not password-protected, not blank) | No |
| Extract | Schema-constrained JSON: each critical field with value, confidence, the exact text it was read from, and the page. Missing means `null`. | Yes |
| Validate | Arithmetic, GST slab, date, required fields, confidence | No |
| Match | Vendor by exact GSTIN; PO as printed (or inferred); lines by normalised description, then by meaning | Only when needed |
| Rules | The checks below | No |
| Decide / Explain | Decision from the checks; AI writes the summary and a drafted message from the triggered checks only | Explanation only |

**Checks:** `VENDOR_APPROVED`, `VENDOR_AMBIGUOUS`, `BANK_MATCH`, `PO_FOUND`, `PO_INFERRED`, `AMOUNT_TOLERANCE`, `CUMULATIVE_PO`, `LINE_MATCH`, `DUPLICATE_EXACT`, `DUPLICATE_NEAR`, `ARITHMETIC`, `REQUIRED_FIELDS`, `EXTRACTION_CONFIDENCE`, `STAGE_FAILED` (plus `GST_RATE` and `INVOICE_DATE` from Validate). All thresholds (2% tolerance, 0.80 confidence, 30-day duplicate window, …) live in `backend/app/config.py`.

**Money:** `Decimal` everywhere; comparisons are tax-exclusive. Only *approved* invoices consume PO balance. The balance check, the decision and the insert into the approved-invoice ledger (unique on vendor + invoice number) happen in one database transaction.

## The six demo scenarios

Fictional buyer Northwind Retail Pvt Ltd; INR with GST. Each is a one-click sample card, and the card shows the *expected* result before you run it.

| | Scenario | Expected | Why |
|---|---|---|---|
| **H** | Happy path | Approve | Clean text invoice, PO printed, every line matches |
| **E1** | Split invoice exceeds PO | Hold, `CUMULATIVE_PO` | Third invoice on PO-2041; each bill is fine alone, together they reach 108% of the PO |
| **E2** | Near-duplicate | Hold, `DUPLICATE_NEAR` | `INV-1042A` repeats the vendor and amount of the approved `INV-1042` |
| **E3** | Scanned, no PO number | Hold, `PO_INFERRED` | Image-only, rotated, noisy scan; the AI infers the PO but a human must confirm |
| **E4** | Bank details changed | Hold, high risk, `BANK_MATCH` | Everything matches except the bank account |
| **E5** | Within tolerance | Approve | A freight line puts the subtotal 1.3% over the PO, inside the 2% tolerance |

## Prepared test inputs

`backend/data/test_inputs/` holds edge-case files for live demos (not sample cards; they don't affect the six scenarios). Upload them from **New run**. The folder's own README lists each file with its expected result.

| File | Expected |
|---|---|
| `not_a_pdf.docx`, `invoice_photo.png`, `empty.pdf`, `corrupted.pdf`, `password_protected.pdf`, `too_many_pages.pdf`, `too_large.pdf` | Rejected at upload (HTTP 400) with a plain message, before any run exists and before any LLM call |
| `missing_invoice_number.pdf` | Hold via `REQUIRED_FIELDS` |
| `unknown_po.pdf` | Hold via `PO_FOUND` |
| `prompt_injection.pdf` | Hold, high risk, via `BANK_MATCH`: a visible line tells the AI to approve the invoice; it is ignored |

Regenerate them with `cd backend && python scripts/generate_test_inputs.py`.

## Run it locally

Requires Python 3.11+ and Node 20+.

```bash
# 1. Backend
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # then put your Anthropic API key in backend/.env

# 2. Frontend (built once; FastAPI serves it)
cd ../frontend && npm install && npm run build

# 3. Run: ONE command serves the API and the UI
cd ../backend && .venv/bin/uvicorn app.main:app --port 8000
# open http://localhost:8000
```

`backend/.env` holds `LLM_PROVIDER=anthropic`, `LLM_MODEL=claude-sonnet-5-5` and `ANTHROPIC_API_KEY`. It is git-ignored; never commit it.

On first start the database is created and seeded (vendors, purchase orders, approved-invoice history). **Reset demo data** on the Dashboard (or `POST /api/reset`) restores that state and clears run history.

**Frontend development:** `cd frontend && npm run dev` serves on :5173 and proxies `/api` to :8000 (development only; production never uses it).

**Tests** (the LLM is always mocked; no API key needed): `cd backend && .venv/bin/python -m pytest -q`

**Real-LLM scenario runner:** `cd backend && .venv/bin/python scripts/run_scenarios.py --repeat 3` resets a database and runs all six scenarios end to end, printing expected vs actual, failing checks, LLM calls and timings. It uses `DATABASE_URL` if set, so point it at a scratch file to leave your demo database alone.

**Regenerate the sample invoices:** `cd backend && python scripts/generate_invoices.py` (deterministic; also verifies the seed data).

## Deploy to Render (single web service)

The repo includes a multi-stage `Dockerfile` (Node builds the frontend; a Python 3.11-slim image runs uvicorn on `$PORT`) and a `render.yaml` Blueprint.

1. Push the repo to GitHub.
2. In Render: **New → Blueprint**, pick the repo. Render reads `render.yaml`.
3. When prompted, enter `ANTHROPIC_API_KEY`. (It is declared `sync: false`, so it is never stored in the repo.) `LLM_PROVIDER` and `LLM_MODEL` are preset.
4. Deploy. Render health-checks `GET /api/health`.

Run the same image locally first:

```bash
docker build -t invoice-decision-agent .
docker run --rm -p 8080:8080 -e PORT=8080 --env-file backend/.env invoice-decision-agent
# open http://localhost:8080
```

The SQLite file lives at `DATABASE_URL` (the image sets `sqlite:////app/data/invoice_agent.db`). Startup seeds only if the database is missing.

## API

| | |
|---|---|
| `GET /api/health` | `{"status":"ok"}` (no database, no LLM) |
| `GET /api/samples` | The six scenario cards |
| `POST /api/runs` | Start a run from an uploaded PDF (multipart `file`) or `{"sample":"E1"}`. Returns `run_id` immediately; invalid files get a 400 before anything runs |
| `GET /api/runs` | History (newest first) and KPIs: processed, approved, held, rejected, ₹ auto-approved and ₹ stopped for review (both excluding GST) |
| `GET /api/runs/{id}` | Everything for one run: stages, extracted fields with evidence, match result, every check, decision, summary, drafted message |
| `GET /api/runs/{id}/stream` | Server-Sent Events, replayed from the database; supports `Last-Event-ID` / `?after=` |
| `GET /api/runs/{id}/page.png` | Page 1 of the invoice |
| `GET /api/reference` | Vendors (bank accounts masked) and POs with approved-to-date and utilisation |
| `POST /api/reset` | Restore seed data and clear run history |

## Known limitations

- **Single server process.** Background runs, the live stream and restart recovery all assume one uvicorn process (the Dockerfile starts exactly one). Do not scale to multiple instances or workers.
- **SQLite resets on redeploy.** On Render's free tier the disk is ephemeral: a redeploy or restart wipes run history and the approved-invoice ledger, and startup reseeds the demo data. (Production would use Postgres, with row locking on the PO.)
- **One invoice per PDF.** A PDF containing several invoices is not split; it is read as one document.
- **INR and GST only.** There is no currency conversion and no check that the currency is INR.
- **Two-way match.** Invoice against PO; no goods-received note, no email ingestion, no ERP integration, no authentication, no payment execution.
- **Anthropic only.** One LLM provider is implemented behind the thin wrapper in `backend/app/llm/client.py`.
- **Prompt injection** is contained by design (the AI never decides; prompts say to ignore embedded instructions), but extracted values are not cross-checked against the PDF's own text layer.
- If the server restarts mid-run, that run is closed as failed: *before a decision* it is held; *after a decision was recorded* the decision stands and only the unfinished explanation is marked failed.

## Project layout

```
backend/app/      main.py · config.py · schemas.py · api/ · pipeline/ · rules/ · llm/ (client + prompts/) · db/
backend/data/     seed CSVs · sample invoices (invoices/) · prepared test inputs (test_inputs/)
backend/scripts/  generate_invoices.py · generate_test_inputs.py · run_scenarios.py
backend/tests/    pytest (LLM mocked)
frontend/src/     React + Vite + TypeScript + Tailwind: New run · Live run / Run detail · Dashboard · Reference data
Dockerfile · render.yaml
```
