# PRD — Invoice Decision Agent (Zamp ASA Case Study, PS-1)

Owner: Devashish Dwivedi · Deadline: Sunday 1 Nov 2026, evening IST · Status: **frozen** — changes only to fix a bug found during the build

---

## 1. Problem

A mid-size company receives hundreds of vendor invoices a month as PDFs. An AP clerk opens each one, finds the matching purchase order (PO), checks the numbers, and decides whether to pay. It is slow and error-prone, and the errors cost real money: duplicate payments, over-billing beyond the PO, and payments sent to fraudulent bank accounts.

**Goal:** take an invoice PDF as input and produce a clear decision — **Approve**, **Hold** or **Reject** — with every step and every reason visible.

**Buyer:** Finance Controller / CFO. They care about three things:
1. Did we pay the right amount, to the right party, exactly once?
2. Can I show an auditor why each decision was made?
3. Can my team stop doing the routine 80% and focus on the 20% that needs judgment?

## 2. Design principle (repeat this everywhere)

> **AI reads the messy document. Plain code makes the money decision. Every decision is explainable and every step reports its confidence.**

- LLM is used for: reading the invoice, matching line items by meaning, inferring a missing PO, and writing explanations / exception messages.
- LLM is **never** used to make the Approve / Hold / Reject decision. That is a deterministic rules engine: same input → same decision, every time.
- When the AI is unsure, the process holds for a human instead of guessing.

## 3. Scope

### In scope
- Upload a PDF (text-based or scanned image) or pick a sample invoice in one click.
- Six-stage pipeline with live progress (section 4).
- Deterministic rules engine with tolerance, duplicate, cumulative-PO and vendor/bank checks.
- Six scenarios — H (happy path) + E1–E5 — all reproducible on demand (section 6).
- Live run view + dashboard + run detail page.
- "Reset demo data" so every demo starts from the same state.
- Deployed at a public URL.

### Out of scope (say "in production this would…" in the interview)
- Email inbox ingestion (production: hook into the AP mailbox).
- ERP integration (production: read POs/vendors from SAP/NetSuite/Tally).
- GRN / three-way match (we do two-way: invoice ↔ PO).
- Auth, multi-tenant, payment execution.

## 4. Pipeline

| # | Stage | What it does | AI? | Output |
|---|---|---|---|---|
| 1 | **Ingest** | Detect text layer. Text PDF → extract text (pdfplumber). No text → render page(s) to image. | No | `doc_type: text \| scanned`, page count |
| 2 | **Extract** | LLM returns schema-constrained JSON: vendor name, vendor GSTIN, invoice no., invoice date, PO reference, bank account + IFSC, line items (description, qty, unit price, amount), other charges (freight etc.), subtotal, tax lines, total, currency. Each critical field returns `{value, confidence, evidence, page}` — `evidence` is the exact text the model read (e.g. "Invoice No: INV-1042") so a reviewer can verify it. Fields not on the page = `null`, never guessed. Confidence is model-reported, not calibrated — it is one signal, cross-checked by arithmetic and master data. | Yes (vision for scans) | `ExtractedInvoice` |
| 3 | **Validate** | Internal consistency: Σ line amounts + other charges = subtotal (±₹1), subtotal + tax = total (±₹1), GST rate is 0/5/12/18/28% (other → WARN), date not in future, date older than 180 days → WARN (configurable), required fields present, critical-field confidence ≥ 0.80. | No | list of `Check` |
| 4 | **Match** | **Vendor:** (1) exact GSTIN match → identified; (2) otherwise the best name match is shown as a *suggestion* but always Holds via `VENDOR_AMBIGUOUS` — a name match alone, however strong, is never enough to approve; (3) no match → `VENDOR_APPROVED` fails. **PO:** explicit reference → validate it exists, is open and belongs to that vendor (no AI). Missing reference → AI ranks the vendor's open POs by lines + amount + date with a confidence; an inferred PO always Holds. **Lines:** exact/normalised description match first; AI matches by meaning only for lines that don't match deterministically. Semantic similarity never overrides vendor identity or financial limits. | Only when needed | `MatchResult` (vendor, PO, line mapping, method used, confidences) |
| 5 | **Rules** | Deterministic checks (section 5). Each returns PASS / WARN / FAIL with code, message and evidence. | No | list of `Check` |
| 6 | **Decide & explain** | Decision from rules (section 5.2). AI writes a 2–3 sentence plain-English summary and, for Hold/Reject, a routed action + drafted message to the right party. | Explanation only | `Decision` |

Every stage writes its input, output, duration and status to the DB and streams an event to the UI.

## 5. Rules engine

### 5.0 Financial basis (applies to every check)
- All money is `Decimal`, never float. Round to 2 dp only for display.
- All amount comparisons against the PO are **tax-exclusive**. GST is validated separately (Validate stage) and is never included in PO consumption.
- Freight and other charges are treated as additional invoice lines: they are part of the invoice subtotal and count toward `AMOUNT_TOLERANCE` and `CUMULATIVE_PO`, but they are **not** matched to PO lines (they are exempt from `LINE_MATCH`).
- **PO consumption = sum of the subtotals of previously *approved* invoices on that PO.** Held and rejected invoices never consume balance. Each approved invoice is counted once.
- **Approved-invoice ledger:** approved invoices are recorded in a ledger table with a unique constraint on `(vendor_id, invoice_number)`. The balance check, the decision and (for Approve only) the ledger insert happen in one DB transaction. The runs table has no such constraint, so the same file can be run again in a demo — a second run of an already-approved invoice simply hits `DUPLICATE_EXACT`.

### 5.1 Checks

| Code | Check | Result |
|---|---|---|
| `VENDOR_APPROVED` | Vendor exists in master and status = active | missing/blocked → FAIL (reject) |
| `BANK_MATCH` | Invoice bank account + IFSC equal vendor master | differs → FAIL (hold, **high risk**) |
| `PO_FOUND` | PO exists, is open, belongs to this vendor | not found → FAIL (hold); wrong vendor → FAIL (reject) |
| `PO_INFERRED` | PO was inferred, not stated | WARN (hold) — always confirm inferred POs |
| `AMOUNT_TOLERANCE` | Invoice subtotal (tax-exclusive) ≤ value of matched PO lines × (1 + 2%) | over → FAIL (hold) |
| `CUMULATIVE_PO` | (approved-to-date on this PO + this invoice subtotal) ≤ PO subtotal × (1 + 2%) | exceeds → FAIL (hold), evidence shows utilisation % |
| `LINE_MATCH` | Every product line maps to a PO line; qty ≤ that PO line's ordered qty (this invoice alone — consumption across invoices is `CUMULATIVE_PO`'s job); unit price within 2% (freight/other charges exempt) | mismatch → FAIL (hold) |
| `VENDOR_AMBIGUOUS` | Vendor matched by name only, or more than one candidate | FAIL (hold) |
| `DUPLICATE_EXACT` | Same vendor + same invoice number already approved | FAIL (reject) |
| `DUPLICATE_NEAR` | Same vendor, total within 0.5%, date within 30 days, invoice number similar (normalised edit distance, e.g. `INV-1042` vs `INV-1042A`) | FAIL (hold, "probable duplicate of …" — could be legitimate, so a human confirms) |
| `ARITHMETIC` | from Validate stage | FAIL (hold) |
| `REQUIRED_FIELDS` | Critical fields present: vendor (name or GSTIN), invoice number, invoice date, subtotal, total | any null → FAIL (hold) |
| `EXTRACTION_CONFIDENCE` | any critical field < 0.80 | WARN (hold) |
| `STAGE_FAILED` | Extract, Validate or Match stage errored (e.g. LLM timeout, unparseable output) | FAIL (hold) — the run is shown as failed and routed to manual processing; it can never be approved |

### 5.2 Decision
1. Any FAIL marked *reject* → **Reject**
2. Else any FAIL or WARN → **Hold** (risk level = highest among triggered checks)
3. Else → **Approve**

**Approve requires every check to be PASS.** Therefore a missing or low-confidence financial field, an inferred PO, a probable duplicate, or any failed stage can never produce Approve.

Thresholds live in one config file (`tolerance_pct = 2.0`, `confidence_min = 0.80`, `near_dup_days = 30`, …) so they can be tuned per client — mention this in the interview: "every client has different tolerances; that's configuration, not code."

### 5.3 Exception routing (for Hold / Reject)
| Trigger | Routed to | Drafted message |
|---|---|---|
| Bank mismatch | Vendor master / treasury team | Verify by phone using the number on file, not the invoice |
| Over PO / cumulative | Procurement (PO owner) | Approve variance or raise a PO amendment |
| Inferred PO / low confidence | AP reviewer | Confirm the suggested PO |
| Exact duplicate | Vendor | "This invoice duplicates INV-1042 paid on …" |
| Probable duplicate | AP reviewer | Compare against INV-1042 before releasing |

Drafted messages are generated **only from the checks that actually triggered** and their evidence; the prompt receives the check list, not free rein. Bank-account numbers are masked (`XXXXXX4417`) everywhere they leave the comparison logic: API responses, SSE events, stored audit payloads, UI, logs and drafted messages. The full number is used only inside `BANK_MATCH`. The process never updates vendor bank details itself.

## 6. Demo scenarios (test data)

Fictional buyer: **Northwind Retail Pvt Ltd**, Gurugram. Currency INR, GST.

| # | Scenario | Invoice | Expected | The one-line story |
|---|---|---|---|---|
| H | **Happy path** | Clean text PDF, explicit PO, all lines match | **Approve** | "Everything checks out — paid in seconds instead of minutes." |
| E1 | **Split invoice exceeds PO** | 3rd invoice on PO-2041; the first two are seeded as **approved** in the ledger. Fine on its own, but cumulative = 108% of the PO subtotal | **Hold** | "Each bill looked fine. Together they over-bill the PO by 8%." |
| E2 | **Near-duplicate** | `INV-1042A`, same vendor and amount as `INV-1042` approved 6 days earlier | **Hold** (probable duplicate) | "Someone resubmitted the same bill with an A on the end — payment stopped until a human confirms." |
| E3 | **Scanned, no PO number** | Image-only PDF (slightly rotated, noisy), no PO reference; AI infers PO from lines | **Hold** | "The AI found the likely PO but won't pay on a guess — a human confirms in one click." |
| E4 | **Bank details changed** | Everything matches perfectly, but the bank account differs from vendor master | **Hold — high risk** | "A perfect invoice can still be fraud. Matching numbers isn't enough." |
| E5 | **Within tolerance** | Product lines match the PO exactly; a freight line makes the subtotal +1.3% over the PO (PO has no prior approved invoices) | **Approve** | "Small, allowed variance — the system doesn't waste a human's time on it." |

**Each edge case is designed around one primary check** (E1 `CUMULATIVE_PO`, E2 `DUPLICATE_NEAR`, E3 `PO_INFERRED`, E4 `BANK_MATCH`), so the demo story is unambiguous; no other check should FAIL (E3 may also show low-confidence WARNs, which is fine — it's a scan). E.g. E2's PO has enough remaining balance that only the duplicate check fires.

Seed data:
- `vendors.csv` — id, name, GSTIN, bank account, IFSC, status (5–6 vendors).
- `purchase_orders.csv` + `po_lines.csv` — 6–8 POs with line items.
- `invoice_history.csv` — previously **approved** invoices, loaded into the approved-invoice ledger (needed for E1 cumulative and E2 duplicate).
- Invoice PDFs generated by a script (reportlab) with **different layouts per vendor**; every scenario invoice shows the vendor GSTIN (so vendor identity is exact and only the intended check fires); the scanned one is rendered to an image with rotation + noise, saved as image-only PDF.

**Demo state must be reproducible:** "Reset demo data" restores the seed DB. Processing an invoice in the demo must not break the next run of the same scenario.

## 7. UI

Must look clean and professional — the brief says the UI is graded. Light theme, one accent colour, decision colours green / amber / red.

1. **New run** — drag-and-drop upload + "Sample invoices" cards (one per scenario, with a one-line label). One click starts a run.
2. **Live run view** — vertical stage rail (6 stages) lighting up as events stream (SSE). Each stage expands to show its output:
   - Extract: the PDF on the left, extracted fields on the right with confidence badges.
   - Validate / Rules: checklist with PASS / WARN / FAIL, message and evidence (e.g. "₹5,40,000 invoiced of ₹5,00,000 PO = 108%").
   - Match: invoice line ↔ PO line table with match reason.
   - Decision card: big decision badge, AI summary, routed-to, drafted message (copy button).
3. **Dashboard** — KPI tiles (invoices processed, approved / held / rejected, ₹ value auto-approved, ₹ value stopped), table of runs (time, vendor, invoice no., amount, decision, top reason), filter by decision, click → run detail.
4. **Run detail** — same as live view, from history.
5. **Reference data** — read-only tables of vendors and POs (with invoiced-to-date), so the interviewer can see what the process matched against.

## 8. Architecture

- **Backend:** Python 3.11, FastAPI, Pydantic v2, SQLite (SQLAlchemy), SSE for live events.
- **LLM:** one provider implemented first (vision + structured output), behind a thin wrapper so another can be added later; model via env. Structured (schema-constrained) output for every call. 30 s timeout; one retry on parse failure, then the stage fails visibly. Invoice text is untrusted data — prompts instruct the model to ignore any instructions inside the document.
- **Uploads:** PDF only, ≤ 10 MB, ≤ 5 pages.
- **PDF:** pdfplumber (text), pypdfium2 or PyMuPDF (render to image), reportlab (generate test invoices).
- **Frontend:** React + Vite + TypeScript + Tailwind, built and served by FastAPI as static files → **one service, one URL, no CORS**.
- **Deploy:** Render or Railway, single web service.
- **Data lifecycle:** on startup, create tables and seed reference data **only if missing**. `POST /api/reset` explicitly restores seed data and clears run history. Normal runs preserve history. (Free hosting may wipe the disk on redeploy — acceptable for a demo; production uses Postgres.)

## 9. Evaluation (differentiator — do if time allows)
- `eval/` — a labelled ground-truth JSON for each test invoice.
- `python -m eval.run` prints field-level extraction accuracy and decision accuracy across all scenarios.
- Show the table in the video/README: "Extraction: 98% field accuracy on 8 invoices; decisions: 6/6 correct."

## 10. Acceptance criteria
- [ ] All 6 scenarios (H, E1–E5) produce the expected decision, 3 runs in a row, after "Reset demo data".
- [ ] All money uses Decimal; amounts compared tax-exclusive.
- [ ] Every extracted critical field shows its evidence text.
- [ ] A scanned invoice is extracted correctly without any OCR library.
- [ ] Every Hold/Reject shows: failing checks with evidence, plain-English summary, routed-to, drafted message.
- [ ] Live view shows each stage completing in real time; a full run takes < 30 s.
- [ ] Dashboard shows history and totals; clicking a run opens its detail.
- [ ] Deployed URL works from a fresh browser.
- [ ] README: what it does, how to run, design principle, edge cases, limitations.
- [ ] Unit tests cover every rule and the decision function.
- [ ] Ledger tests: Hold and Reject never insert into the approved-invoice ledger; a transaction that fails mid-way leaves PO consumption unchanged; re-running an approved invoice creates a new run and returns Reject via `DUPLICATE_EXACT` (no uniqueness error).

## 11. Build plan
| When | Milestone |
|---|---|
| Fri | Seed data + invoice generator; extraction + validate + match + rules + decide working from CLI on all scenarios |
| Sat AM | FastAPI + SSE + DB; frontend live run view |
| Sat PM | Dashboard, run detail, reference data, reset; **deploy** |
| Sun AM | Polish, eval script, README, rehearse all scenarios on the live URL |
| Sun PM | Record 5-min video, submit |

## 12. Assumptions (note for the interview)
- Two-way match (invoice ↔ PO); GRN not available.
- One invoice may cover part of one PO; an invoice spanning multiple POs is out of scope.
- Tolerance 2% and 180-day date window are client-configurable defaults.
- INR / GST only; multi-currency would add an FX step.
- Inferred POs are never auto-approved, regardless of confidence.
- Only approved invoices consume PO balance.
- Probable duplicates are held, not rejected; only exact duplicates are rejected.
