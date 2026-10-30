"""Stage 4: Match the invoice to master data. The LLM is called only when deterministic matching cannot answer.

Vendor: exact GSTIN match = identified. Otherwise the closest name is only a SUGGESTION (the rules then Hold).
PO:     printed reference -> plain lookup (no AI). No reference -> the LLM ranks the vendor's open POs; the
        result is marked "inferred" and the rules always Hold it.
Lines:  normalised-description match first; the LLM matches by meaning only the lines left over.
Semantic similarity never overrides vendor identity or financial limits: price, quantity and totals are
still checked by the rules.
"""
import json
from dataclasses import dataclass
from difflib import SequenceMatcher

from app.config import Settings
from app.llm.client import LLMClient, text_block
from app.schemas import (ExtractedInvoice, LineMatch, LineMatchResponse, MatchResult, POInference, POMatch,
                         PORecord, ReferenceData, VendorMatch, VendorRecord)
from app.util import inr, norm_id, norm_text, reformat_amounts


@dataclass
class Matched:
    result: MatchResult
    vendor: VendorRecord | None
    po: PORecord | None


def match_vendor(invoice: ExtractedInvoice, vendors: list[VendorRecord], settings: Settings):
    gstin = norm_id(invoice.vendor_gstin.value or "")
    if gstin:
        for v in vendors:
            if norm_id(v.gstin) == gstin:
                return VendorMatch(vendor_id=v.vendor_id, name=v.name, method="gstin"), v
    name = norm_text(invoice.vendor_name.value or "")
    if name:
        scored = sorted(((SequenceMatcher(None, name, norm_text(v.name)).ratio(), v) for v in vendors),
                        key=lambda pair: pair[0], reverse=True)
        close = [(s, v) for s, v in scored if s >= settings.vendor_name_suggest_min]
        if close:
            best_score, best = close[0]
            return VendorMatch(vendor_id=best.vendor_id, name=best.name, method="name", score=round(best_score, 2),
                               candidates=[v.name for _, v in close]), best
    return VendorMatch(vendor_id=None, name=None, method="none"), None


def match_po(llm: LLMClient, invoice: ExtractedInvoice, vendor: VendorRecord | None, pos: list[PORecord],
             settings: Settings):
    reference = invoice.po_reference.value
    if reference:  # explicit: plain lookup, no AI
        for po in pos:
            if norm_id(po.po_id) == norm_id(reference):
                return POMatch(po_id=po.po_id, method="explicit", reference=reference), po
        return POMatch(po_id=None, method="none", reference=reference), None

    candidates = [p for p in pos if vendor and p.vendor_id == vendor.vendor_id and p.status == "open"]
    if not candidates:
        return POMatch(po_id=None, method="none"), None
    # Every rupee amount goes to the model already formatted (₹1,14,450.00); quantities stay plain numbers.
    money = lambda x: inr(x) if x is not None else None
    payload = {
        "invoice": {"date": invoice.invoice_date.value, "subtotal": money(invoice.subtotal.value),
                    "lines": [{"description": l.description, "qty": l.qty, "unit_price": money(l.unit_price),
                               "amount": money(l.amount)} for l in invoice.line_items],
                    "other_charges": [{"description": c.description, "amount": money(c.amount)}
                                      for c in invoice.other_charges]},
        "candidate_pos": [{"po_id": p.po_id, "po_date": p.po_date, "po_total": money(p.subtotal),
                           "lines": [{"description": l.description, "qty": l.qty, "unit": l.unit,
                                      "unit_price": money(l.unit_price)} for l in p.lines]} for p in candidates],
    }
    answer = llm.call("infer_po", [text_block(json.dumps(payload, default=str, indent=1, ensure_ascii=False))], POInference)

    # Presentation guard: the reasons are shown in the UI, so any raw amount the model still wrote is reformatted.
    amounts = [invoice.subtotal.value] + [l.unit_price for l in invoice.line_items] + [l.amount for l in invoice.line_items] \
        + [c.amount for c in invoice.other_charges] + [p.subtotal for p in candidates] \
        + [l.unit_price for p in candidates for l in p.lines]
    quantities = [l.qty for l in invoice.line_items] + [l.qty for p in candidates for l in p.lines]
    answer_ranking = [c.model_copy(update={"reason": reformat_amounts(c.reason, amounts, quantities)})
                      for c in answer.ranking]

    known = {p.po_id: p for p in candidates}
    ranking = sorted((c for c in answer_ranking if c.po_id in known), key=lambda c: c.confidence, reverse=True)
    if ranking and ranking[0].confidence >= settings.po_infer_min_confidence:
        top = ranking[0]
        return POMatch(po_id=top.po_id, method="inferred", confidence=top.confidence, reason=top.reason,
                       ranking=ranking), known[top.po_id]
    return POMatch(po_id=None, method="none", ranking=ranking), None


def match_lines(llm: LLMClient, invoice: ExtractedInvoice, po: PORecord | None, settings: Settings) -> list[LineMatch]:
    lines = invoice.line_items
    results: dict[int, LineMatch] = {
        i: LineMatch(invoice_line_index=i, description=l.description, po_line_id=None, method="none",
                     reason="No PO to match against" if po is None else "No matching PO line")
        for i, l in enumerate(lines)}
    if po is None:
        return list(results.values())

    free = {l.po_line_id: l for l in po.lines}
    for i, line in enumerate(lines):  # 1. deterministic: same description after normalising
        for po_line in list(free.values()):
            if norm_text(po_line.description) == norm_text(line.description):
                results[i] = LineMatch(invoice_line_index=i, description=line.description,
                                       po_line_id=po_line.po_line_id, method="exact", confidence=1.0,
                                       reason="Same description")
                del free[po_line.po_line_id]
                break

    left = [i for i, r in results.items() if r.po_line_id is None]
    if left and free:  # 2. only the leftovers go to the LLM
        payload = {"invoice_lines": [{"invoice_line_index": i, "description": lines[i].description} for i in left],
                   "candidate_po_lines": [{"po_line_id": l.po_line_id, "description": l.description, "unit": l.unit}
                                          for l in free.values()]}
        answer = llm.call("match_lines", [text_block(json.dumps(payload, indent=1))], LineMatchResponse)
        for m in answer.matches:
            ok = (m.invoice_line_index in left and m.po_line_id in free
                  and m.confidence >= settings.line_match_min_confidence)
            if ok:
                results[m.invoice_line_index] = LineMatch(
                    invoice_line_index=m.invoice_line_index, description=lines[m.invoice_line_index].description,
                    po_line_id=m.po_line_id, method="semantic", confidence=m.confidence, reason=m.reason)
                del free[m.po_line_id]
    return [results[i] for i in sorted(results)]


def match(llm: LLMClient, invoice: ExtractedInvoice, ref: ReferenceData, settings: Settings) -> Matched:
    vendor_match, vendor = match_vendor(invoice, ref.vendors, settings)
    po_match, po = match_po(llm, invoice, vendor, ref.pos, settings)
    # Lines are only matched against a PO that could legitimately belong to this vendor.
    usable_po = po if po and vendor and po.vendor_id == vendor.vendor_id else None
    lines = match_lines(llm, invoice, usable_po, settings)
    return Matched(MatchResult(vendor=vendor_match, po=po_match, lines=lines), vendor, po)
