"""Turns database rows into the JSON the UI shows. Read-only. Money is always a string (never a float).
Everything stored is already masked; bank accounts never appear here."""
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models as m
from app.db import repository as repo

STAGES = ["ingest", "extract", "validate", "match", "rules", "decide", "explain"]
FINAL_STATUSES = ("completed", "failed")


def iso(dt) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def money(x: Decimal | None) -> str | None:
    return None if x is None else str(x)


def primary_check(rows):
    """The check the decision is 'triggered by': a reject first, then any FAIL, then a WARN (same order as the engine)."""
    fails = [r for r in rows if r.result == "FAIL"]
    rejects = [r for r in fails if r.action == "reject"]
    warns = [r for r in rows if r.result == "WARN"]
    return (rejects or fails or warns or [None])[0]


# ------------------------------------------------------------------ run list + KPIs
def run_list(session: Session) -> dict:
    runs = list(session.scalars(select(m.Run).order_by(m.Run.id.desc())))
    checks_by_run: dict[int, list] = {}
    for row in session.scalars(select(m.CheckRow).order_by(m.CheckRow.id)):
        checks_by_run.setdefault(row.run_id, []).append(row)
    items, kpi = [], {"processed": 0, "approved": 0, "held": 0, "rejected": 0,
                      "auto_approved": Decimal(0), "stopped": Decimal(0)}
    for r in runs:
        lead = primary_check(checks_by_run.get(r.id, [])) if r.decision != "Approve" else None
        items.append({"id": r.id, "created_at": iso(r.created_at), "filename": r.filename, "status": r.status,
                      "doc_type": r.doc_type, "vendor_name": r.vendor_name, "invoice_number": r.invoice_number,
                      "subtotal": money(r.subtotal), "total": money(r.total), "decision": r.decision,
                      "risk_level": r.risk_level, "top_code": lead.code if lead else None, "top_reason": r.top_reason})
        if r.status in FINAL_STATUSES and r.decision:      # finished runs only; KPIs use SUBTOTALS (excl. GST)
            kpi["processed"] += 1
            if r.decision == "Approve":
                kpi["approved"] += 1
                kpi["auto_approved"] += r.subtotal or Decimal(0)
            else:
                kpi["held" if r.decision == "Hold" else "rejected"] += 1
                kpi["stopped"] += r.subtotal or Decimal(0)
    kpi["auto_approved"], kpi["stopped"] = str(kpi["auto_approved"]), str(kpi["stopped"])
    return {"kpis": kpi, "runs": items}


# ------------------------------------------------------------------ run detail
def _stage_summary(stage: str, status: str, output: dict | None, error: str | None, doc_type: str | None) -> str | None:
    if status == "failed":
        return (error or "Failed")[:160]
    if status == "skipped":
        return "Skipped: an earlier stage failed"
    if status != "completed" or output is None and stage != "ingest":
        return None
    out = output or {}
    if stage == "ingest":
        kind = "Text PDF" if out.get("doc_type") == "text" else "Scanned image"
        pages = out.get("page_count", 0)
        return f"{kind} · {pages} page{'s' if pages != 1 else ''}"
    if stage == "extract":
        inv = out["invoice"]
        read = sum(1 for k in ("vendor_name", "vendor_gstin", "invoice_number", "invoice_date", "po_reference",
                               "bank_account", "ifsc", "subtotal", "total") if inv[k]["value"] is not None)
        return f"{read} of 9 key fields read · {len(inv['line_items'])} line items"
    if stage in ("validate", "rules"):
        checks = out.get("checks", [])
        bad = [c for c in checks if c["result"] != "PASS"]
        noun = f"{len(checks)} check{'s' if len(checks) != 1 else ''}"
        return (f"{noun} · all pass" if not bad else
                f"{noun} · " + ", ".join(f"{sum(c['result'] == r for c in checks)} {r.lower()}"
                                                       for r in ("FAIL", "WARN") if any(c["result"] == r for c in checks)))
    if stage == "match":
        po, lines = out["po"], out["lines"]
        po_text = (f"{po['po_id']}, printed" if po["method"] == "explicit" else
                   f"{po['po_id']}, inferred ({po['confidence']:.0%})" if po["method"] == "inferred" else
                   f"PO {po['reference']} not found" if po["reference"] else "no PO found")
        exact = sum(l["method"] == "exact" for l in lines)
        semantic = sum(l["method"] == "semantic" for l in lines)
        line_text = (f"{exact}/{len(lines)} lines exact" if not semantic and exact == len(lines) else
                     f"{exact} exact, {semantic} by meaning, {len(lines) - exact - semantic} unmatched")
        vendor = "" if out["vendor"]["method"] == "gstin" else f"vendor by {out['vendor']['method']} · "
        return f"{vendor}{po_text} · {line_text}"
    if stage == "decide":
        tail = "ledger updated" if out.get("ledger_inserted") else f"{out.get('risk_level')} risk"
        return f"{out['decision']} · {tail}" if out["decision"] != "Approve" else f"Approve · {tail}"
    if stage == "explain":
        return "Summary and message written"
    return None


def _stage_views(events, doc_type) -> list[dict]:
    found: dict[str, dict] = {}
    for e in events:
        s = found.setdefault(e.stage, {"stage": e.stage, "status": "pending", "duration_ms": None, "output": None,
                                       "error": None})
        if e.status == "started":
            s["status"] = "running"
        else:
            s.update(status=e.status, duration_ms=e.duration_ms, output=e.output, error=e.error)
    names = STAGES + [n for n in found if n not in STAGES]      # an unexpected "pipeline" failure is shown too
    views = []
    for name in names:
        s = found.get(name, {"stage": name, "status": "pending", "duration_ms": None, "output": None, "error": None})
        s["summary"] = _stage_summary(name, s["status"], s["output"], s["error"], doc_type)
        views.append(s)
    return views


def _match_view(session: Session, out: dict | None, extracted: dict | None) -> dict | None:
    if not out:
        return None
    vendor = dict(out["vendor"])
    if vendor.get("vendor_id"):
        master = session.get(m.Vendor, vendor["vendor_id"])
        vendor["master_gstin"] = master.gstin if master else None
    po_lines = {}
    if out["po"].get("po_id"):
        rows = session.scalars(select(m.POLine).where(m.POLine.po_id == out["po"]["po_id"]))
        po_lines = {r.po_line_id: r for r in rows}
    lines = []
    for lm in out["lines"]:
        inv_line = extracted["line_items"][lm["invoice_line_index"]] if extracted else {}
        pl = po_lines.get(lm["po_line_id"]) if lm["po_line_id"] else None
        lines.append({"invoice_line": {"description": lm["description"], "qty": inv_line.get("qty"),
                                       "unit_price": inv_line.get("unit_price"), "amount": inv_line.get("amount")},
                      "po_line": {"po_line_id": pl.po_line_id, "description": pl.description, "qty": str(pl.qty),
                                  "unit_price": str(pl.unit_price)} if pl else None,
                      "method": lm["method"], "confidence": lm["confidence"], "reason": lm["reason"]})
    return {"vendor": vendor, "po": out["po"], "lines": lines}


def _check_views(session: Session, run: m.Run, rows) -> list[dict]:
    views = []
    for r in rows:
        evidence = dict(r.evidence or {})
        if r.code == "CUMULATIVE_PO" and "approved_to_date" in evidence:
            # name the earlier invoices behind "approved to date" (only if they add up to exactly that figure)
            po = next((c for c in rows if c.code == "PO_FOUND"), None)
            po_id = (po.evidence or {}).get("po_id") if po else None
            earlier = list(session.scalars(select(m.ApprovedInvoice).where(
                m.ApprovedInvoice.po_id == po_id, (m.ApprovedInvoice.run_id.is_(None)) | (m.ApprovedInvoice.run_id != run.id))))
            if po_id and sum((e.subtotal for e in earlier), Decimal(0)) == Decimal(evidence["approved_to_date"]):
                evidence["approved_invoices"] = [{"invoice_number": e.invoice_number, "subtotal": str(e.subtotal)}
                                                 for e in earlier]
        views.append({"stage": r.stage, "code": r.code, "result": r.result, "action": r.action, "risk": r.risk,
                      "message": r.message, "evidence": evidence})
    return views


def run_detail(session: Session, run_id: int) -> dict | None:
    run = session.get(m.Run, run_id)
    if run is None:
        return None
    events = repo.get_events(session, run_id)
    stages = _stage_views(events, run.doc_type)
    by_stage = {s["stage"]: s for s in stages}
    extracted = (by_stage["extract"]["output"] or {}).get("invoice") if by_stage["extract"]["status"] == "completed" else None
    check_rows = repo.get_checks(session, run_id)
    row = repo.get_decision(session, run_id)
    lead = primary_check(check_rows) if row and row.decision != "Approve" else None
    decide_out = by_stage["decide"]["output"] or {}
    inv = extracted or {}
    return {
        "id": run.id, "filename": run.filename, "status": run.status, "doc_type": run.doc_type,
        "created_at": iso(run.created_at), "file_size_bytes": len(run.pdf) if run.pdf else None,
        "has_document": bool(run.pdf),
        "header": {"vendor_name": run.vendor_name, "vendor_gstin": (inv.get("vendor_gstin") or {}).get("value"),
                   "invoice_number": run.invoice_number, "invoice_date": (inv.get("invoice_date") or {}).get("value"),
                   "subtotal": money(run.subtotal), "total": money(run.total),
                   "currency": (inv.get("currency") or {}).get("value")},
        "stages": stages,
        "total_duration_ms": sum(s["duration_ms"] or 0 for s in stages),
        "extracted": extracted,
        "match": _match_view(session, by_stage["match"]["output"] if by_stage["match"]["status"] == "completed" else None,
                             extracted),
        "checks": _check_views(session, run, check_rows),
        "decision": None if row is None else {
            "decision": row.decision, "risk_level": row.risk_level, "top_reason": row.top_reason,
            "top_code": lead.code if lead else None, "routed_to": row.routed_to,
            "required_action": row.required_action, "summary": row.summary, "drafted_message": row.drafted_message,
            "ledger_inserted": bool(decide_out.get("ledger_inserted")), "checks_total": len(check_rows)},
    }
