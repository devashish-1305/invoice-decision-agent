"""Generate the six scenario invoice PDFs (H, E1-E5) from the seed CSVs.

Run:  cd backend && python scripts/generate_invoices.py

Single source of truth: vendors, POs, PO lines and approved-invoice history are read
from backend/data/*.csv. Each scenario below only says "which PO lines, what quantity".
Amounts, tax and the summary table are computed, and verify_seed() checks that every
scenario sits where the PRD (section 6) says it should before any PDF is written.
"""
import csv
import io
import random
import sys
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from xml.sax.saxutils import escape

import pymupdf  # PyMuPDF: render the scanned page to an image
import pdfplumber
from PIL import Image, ImageFilter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, Table

DATA = Path(__file__).resolve().parent.parent / "data"
OUT = DATA / "invoices"

# Thresholds used only to sanity-check the seed data (the real ones live in app/config.py later).
TOLERANCE = Decimal("0.02")
NEAR_DUP_TOTAL = Decimal("0.005")
NEAR_DUP_DAYS = 30
GST_RATE = Decimal("0.18")
FREIGHT_HSN = "9965"

BUYER = {
    "name": "Northwind Retail Pvt Ltd",
    "address": ["Plot 41, Sector 18", "Gurugram, Haryana 122015"],
    "gstin": "06AAECN7721P1ZK",
}
BANK_NAMES = {"HDFC": "HDFC Bank", "ICIC": "ICICI Bank", "SBIN": "State Bank of India",
              "UTIB": "Axis Bank", "KKBK": "Kotak Mahindra Bank", "PUNB": "Punjab National Bank"}


# ----------------------------------------------------------------------------- seed data
def read_csv(name):
    with open(DATA / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


VENDORS = {r["vendor_id"]: r for r in read_csv("vendors.csv")}
POS = {r["po_id"]: r for r in read_csv("purchase_orders.csv")}
PO_LINES = {r["po_line_id"]: r for r in read_csv("po_lines.csv")}
HISTORY = read_csv("invoice_history.csv")


def po_line_ids(po_id):
    return [k for k, r in PO_LINES.items() if r["po_id"] == po_id]


def po_subtotal(po_id):
    return sum(Decimal(PO_LINES[k]["qty"]) * Decimal(PO_LINES[k]["unit_price"]) for k in po_line_ids(po_id))


def approved_to_date(po_id):
    return sum((Decimal(h["subtotal"]) for h in HISTORY if h["po_id"] == po_id), Decimal(0))


# ----------------------------------------------------------------------------- scenarios
def full_po(po_id):
    """Invoice every line of the PO at its full ordered quantity."""
    return [(k, int(PO_LINES[k]["qty"])) for k in po_line_ids(po_id)]


# lines = [(po_line_id, qty)] ; descriptions/prices come from the PO line unless overridden.
SCENARIOS = [
    dict(id="H", file="H_happy_path.pdf", vendor="V001", po="PO-2038", number="SOS/2627/0418",
         date=date(2026, 10, 19), lines=full_po("PO-2038"), show_po=True,
         decision="Approve", check="none"),
    dict(id="E1", file="E1_split_invoice_exceeds_po.pdf", vendor="V002", po="PO-2041", number="KIF-26-0233",
         date=date(2026, 10, 22), show_po=True, decision="Hold", check="CUMULATIVE_PO",
         lines=[("POL-2041-1", 800), ("POL-2041-2", 1500), ("POL-2041-3", 750), ("POL-2041-4", 300)]),
    dict(id="E2", file="E2_near_duplicate.pdf", vendor="V003", po="PO-2050", number="INV-1042A",
         date=date(2026, 10, 11), show_po=True, decision="Hold", check="DUPLICATE_NEAR",
         lines=[("POL-2050-1", 1500), ("POL-2050-2", 60), ("POL-2050-3", 40)]),
    dict(id="E3", file="E3_scanned_no_po.pdf", vendor="V004", po="PO-2057", number="GFS/1026/088",
         date=date(2026, 10, 24), show_po=False, scanned=True, decision="Hold", check="PO_INFERRED",
         lines=full_po("PO-2057"),
         reword={"POL-2057-2": "Liquid Handwash 5 Ltr Can", "POL-2057-4": "Garbage Bags 30 Litre - Pack of 30"}),
    dict(id="E4", file="E4_bank_changed.pdf", vendor="V005", po="PO-2063", number="MEC-INV-7731",
         date=date(2026, 10, 21), lines=full_po("PO-2063"), show_po=True, bank_account="9183402756",
         decision="Hold (high risk)", check="BANK_MATCH"),
    dict(id="E5", file="E5_within_tolerance.pdf", vendor="V006", po="PO-2068", number="IFS/26-27/00415",
         date=date(2026, 10, 26), lines=full_po("PO-2068"), show_po=True,
         charges=[("Freight & Handling Charges", Decimal("2600.00"))],
         decision="Approve", check="none (within tolerance)"),
]

# Quantities of the invoices already in invoice_history.csv. Used only by verify_seed() to prove
# each one is individually within the PO line quantities and matches the CSV subtotal.
SEEDED_QTYS = {
    "KIF-26-0171": [("POL-2041-1", 800), ("POL-2041-2", 1600), ("POL-2041-3", 2000), ("POL-2041-4", 400)],
    "KIF-26-0209": [("POL-2041-1", 800), ("POL-2041-2", 1500), ("POL-2041-3", 2000), ("POL-2041-4", 300)],
    "INV-1042": [("POL-2050-1", 1500), ("POL-2050-2", 60), ("POL-2050-3", 40)],
}


# ----------------------------------------------------------------------------- money
def money(x):
    return Decimal(x).quantize(Decimal("0.01"), ROUND_HALF_UP)


def inr(x):
    """Indian digit grouping: 540000 -> 5,40,000.00"""
    whole, frac = f"{money(x):.2f}".split(".")
    if len(whole) > 3:
        head, tail, groups = whole[:-3], whole[-3:], []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + groups + [tail])
    return f"{whole}.{frac}"


@dataclass
class Line:
    description: str
    hsn: str
    qty: int
    unit: str
    rate: Decimal

    @property
    def amount(self):
        return money(self.qty * self.rate)


@dataclass
class Invoice:
    spec: dict
    vendor: dict
    lines: list
    charges: list  # [(label, amount)] freight etc., exempt from line matching
    bank_account: str

    @property
    def subtotal(self):
        return sum((l.amount for l in self.lines), Decimal(0)) + sum((a for _, a in self.charges), Decimal(0))

    @property
    def tax_lines(self):
        """Same state as the buyer (06) -> CGST + SGST, otherwise IGST."""
        if self.vendor["gstin"][:2] == BUYER["gstin"][:2]:
            half = money(self.subtotal * GST_RATE / 2)
            return [("CGST @ 9%", half), ("SGST @ 9%", half)]
        return [("IGST @ 18%", money(self.subtotal * GST_RATE))]

    @property
    def tax(self):
        return sum((a for _, a in self.tax_lines), Decimal(0))

    @property
    def total(self):
        return self.subtotal + self.tax


def build_invoice(spec):
    lines = []
    for po_line_id, qty in spec["lines"]:
        r = PO_LINES[po_line_id]
        desc = spec.get("reword", {}).get(po_line_id, r["description"])
        lines.append(Line(desc, r["hsn"], qty, r["unit"], Decimal(r["unit_price"])))
    vendor = VENDORS[spec["vendor"]]
    return Invoice(spec, vendor, lines, spec.get("charges", []), spec.get("bank_account", vendor["bank_account"]))


# ----------------------------------------------------------------------------- verification
def verify_seed(invoices):
    problems = []

    def check(ok, msg):
        if not ok:
            problems.append(msg)

    gstins = [v["gstin"] for v in VENDORS.values()]
    check(len(set(gstins)) == len(gstins), "vendor GSTINs are not unique")

    # History rows are internally consistent and their quantities fit the PO lines.
    for h in HISTORY:
        sub, tax, tot = Decimal(h["subtotal"]), Decimal(h["tax_amount"]), Decimal(h["total"])
        check(money(sub * GST_RATE) == tax and sub + tax == tot, f"{h['invoice_number']}: history tax/total inconsistent")
        qtys = SEEDED_QTYS[h["invoice_number"]]
        calc = sum(Decimal(q) * Decimal(PO_LINES[k]["unit_price"]) for k, q in qtys)
        check(calc == sub, f"{h['invoice_number']}: history subtotal {sub} != quantities {calc}")
        for k, q in qtys:
            check(q <= int(PO_LINES[k]["qty"]), f"{h['invoice_number']}: {k} qty {q} exceeds PO qty")

    flagged_near_dup = set()
    for inv in invoices:
        s, sid = inv.spec, inv.spec["id"]
        po, vendor = POS[s["po"]], inv.vendor
        check(po["vendor_id"] == vendor["vendor_id"] and po["status"] == "open", f"{sid}: PO not open / not this vendor's")
        matched_value = Decimal(0)
        for k, q in s["lines"]:
            r = PO_LINES[k]
            check(r["po_id"] == s["po"], f"{sid}: {k} is not on {s['po']}")
            check(q <= int(r["qty"]), f"{sid}: {k} qty {q} exceeds PO line qty {r['qty']}")
            matched_value += Decimal(r["qty"]) * Decimal(r["unit_price"])
        check(inv.subtotal <= matched_value * (1 + TOLERANCE), f"{sid}: AMOUNT_TOLERANCE would fail")
        utilisation = (approved_to_date(s["po"]) + inv.subtotal) / po_subtotal(s["po"]) * 100
        check(utilisation <= 100 * (1 + TOLERANCE) or sid == "E1", f"{sid}: CUMULATIVE_PO would fail unexpectedly")
        check(inv.total == inv.subtotal + inv.tax, f"{sid}: arithmetic")
        check((inv.bank_account != vendor["bank_account"]) == (sid == "E4"), f"{sid}: bank account vs master")
        check(s.get("show_po", False) == (sid != "E3"), f"{sid}: PO reference printing")
        # near-duplicate: same vendor, total within 0.5%, date within 30 days
        for h in HISTORY:
            if h["vendor_id"] != vendor["vendor_id"]:
                continue
            check(h["invoice_number"] != s["number"], f"{sid}: exact duplicate of {h['invoice_number']}")
            close_total = abs(inv.total - Decimal(h["total"])) <= Decimal(h["total"]) * NEAR_DUP_TOTAL
            close_date = abs((s["date"] - date.fromisoformat(h["invoice_date"])).days) <= NEAR_DUP_DAYS
            if close_total and close_date:
                flagged_near_dup.add(sid)

    by_id = {i.spec["id"]: i for i in invoices}
    e1, e2, e5 = by_id["E1"], by_id["E2"], by_id["E5"]
    check(approved_to_date("PO-2041") + e1.subtotal == po_subtotal("PO-2041") * Decimal("1.08"), "E1 is not exactly 108%")
    check(e5.subtotal == po_subtotal("PO-2068") * Decimal("1.013"), "E5 is not exactly +1.3%")
    check(approved_to_date("PO-2068") == 0, "E5 PO has prior approved invoices")
    check(flagged_near_dup == {"E2"}, f"near-duplicate flagged for {sorted(flagged_near_dup)}, expected only E2")
    check((e2.spec["date"] - date(2026, 10, 5)).days == 6, "E2 date is not 6 days after INV-1042")
    check(e2.total == Decimal(next(h for h in HISTORY if h["invoice_number"] == "INV-1042")["total"]), "E2 total != INV-1042 total")
    check(len([p for p in POS.values() if p["vendor_id"] == "V004" and p["status"] == "open"]) >= 2, "E3 vendor needs 2+ open POs")
    check(not {k for k, _ in by_id["E3"].spec["lines"]} - set(po_line_ids("PO-2057")), "E3 lines are not all on PO-2057")

    if problems:
        print("SEED DATA CHECK FAILED:")
        for p in problems:
            print("  -", p)
        sys.exit(1)
    print("Seed data checks passed.")


# ----------------------------------------------------------------------------- layout helpers
def tint(color, f):
    """Blend a colour towards white (f=0 -> colour, f=1 -> white)."""
    return colors.Color(*(c + (1 - c) * f for c in (color.red, color.green, color.blue)))


def hexc(h):
    return colors.HexColor(h)


COLUMNS = {  # key: (header, relative width, alignment)
    "sl": ("S.No", 0.06, "CENTER"), "desc": ("Description of Goods", 0.42, "LEFT"),
    "hsn": ("HSN/SAC", 0.10, "CENTER"), "qty": ("Qty", 0.08, "RIGHT"), "unit": ("Unit", 0.08, "CENTER"),
    "rate": ("Rate (Rs.)", 0.12, "RIGHT"), "amount": ("Amount (Rs.)", 0.15, "RIGHT"),
}

STYLES = {
    "V001": dict(layout="classic", font="Helvetica", bold="Helvetica-Bold", accent=hexc("#1F4E79"),
                 address=["B-27, Okhla Industrial Area, Phase II", "New Delhi 110020"],
                 contact="Tel: 011-4155 2290  |  accounts@sharmaoffice.example",
                 columns=["sl", "desc", "hsn", "qty", "unit", "rate", "amount"], headers={},
                 date_fmt="%d-%b-%Y", po_label="PO No.", subtotal="Sub Total", total="Grand Total",
                 terms="Payment due within 30 days of invoice date."),
    "V002": dict(layout="tally", font="Helvetica", bold="Helvetica-Bold", accent=hexc("#111111"),
                 address=["Plot 112, Sector 6, Industrial Estate", "Faridabad, Haryana 121006"],
                 contact="Ph: 0129-4011 778",
                 columns=["sl", "desc", "hsn", "qty", "rate", "unit", "amount"], headers={"unit": "per", "rate": "Rate"},
                 date_fmt="%d-%m-%Y", po_label="Buyer's Order No.", subtotal="Taxable Value", total="Total",
                 terms="Goods once sold will not be taken back. Subject to Faridabad jurisdiction."),
    "V003": dict(layout="band", font="Helvetica", bold="Helvetica-Bold", accent=hexc("#0F766E"),
                 address=["Unit 4, MIDC Bhosari", "Pune, Maharashtra 411026"],
                 contact="billing@bharatpack.example  |  +91 20 6741 0955",
                 columns=["desc", "hsn", "qty", "rate", "amount"],
                 headers={"desc": "Item", "rate": "Unit Price", "amount": "Line Total"},
                 date_fmt="%d %b %Y", po_label="Customer PO", subtotal="Subtotal", total="Total Amount Payable",
                 terms="Net 30 days. Interest @ 1.5% per month on overdue amounts."),
    "V004": dict(layout="minimal", font="Times-Roman", bold="Times-Bold", accent=hexc("#000000"),
                 address=["Khasra 88, Manesar Industrial Model Township", "Gurugram, Haryana 122051"],
                 contact="Phone: 0124-2290 614",
                 columns=["sl", "desc", "qty", "rate", "amount"], headers={"desc": "Particulars"},
                 date_fmt="%d/%m/%Y", po_label="", subtotal="Subtotal (excl. GST)", total="Invoice Total",
                 terms="E. & O.E.  Payment within 30 days."),
    "V005": dict(layout="classic", font="Helvetica", bold="Helvetica-Bold", accent=hexc("#8A1C2B"),
                 address=["No. 14, 2nd Cross, Peenya Industrial Area", "Bengaluru, Karnataka 560058"],
                 contact="Tel: 080-2839 5510  |  sales@mehtaelectricals.example",
                 columns=["desc", "hsn", "qty", "unit", "rate", "amount"], headers={"desc": "Particulars"},
                 date_fmt="%d/%m/%Y", po_label="PO Ref.", subtotal="Sub Total", total="Net Payable",
                 terms="Payment within 15 days of receipt. Warranty as per manufacturer."),
    "V006": dict(layout="minimal", font="Helvetica", bold="Helvetica-Bold", accent=hexc("#374151"),
                 address=["88 Ambattur Industrial Estate", "Chennai, Tamil Nadu 600098"],
                 contact="accounts@iyerfurnishings.example",
                 columns=["sl", "desc", "hsn", "qty", "rate", "amount"], headers={},
                 date_fmt="%d-%b-%Y", po_label="Order Ref. (PO)", subtotal="Subtotal (excl. GST)", total="Invoice Total",
                 terms="Payment within 30 days. Freight charged at actuals."),
}


def para(text, st, size=8.5, bold=False, color=colors.black, align=0):
    return Paragraph(text, ParagraphStyle("p", fontName=st["bold"] if bold else st["font"], fontSize=size,
                                          leading=size + 2.5, textColor=color, alignment=align))


def text_lines(c, x, y_top, lines, font, size, leading):
    c.setFont(font, size)
    y = y_top
    for line in lines:
        y -= leading
        c.drawString(x, y, line)
    return y


def place(c, flowable, x, y_top, width):
    _, h = flowable.wrapOn(c, width, 10_000)
    flowable.drawOn(c, x, y_top - h)
    return y_top - h


def meta_pairs(inv, st):
    s = inv.spec
    pairs = [("Invoice No.", s["number"]), ("Invoice Date", s["date"].strftime(st["date_fmt"]))]
    if s["show_po"]:
        pairs.append((st["po_label"], s["po"]))
    return pairs


def bank_lines(inv):
    v = inv.vendor
    return [f"Bank: {BANK_NAMES[v['ifsc'][:4]]}", f"A/c Name: {v['name']}",
            f"A/c No.: {inv.bank_account}", f"IFSC: {v['ifsc']}"]


def items_table(inv, st, width, extra_cmds, header_color=colors.black):
    cols = st["columns"]
    weight = sum(COLUMNS[k][1] for k in cols)
    widths = [width * COLUMNS[k][1] / weight for k in cols]
    cell = ParagraphStyle("cell", fontName=st["font"], fontSize=8.5, leading=10.5)
    rows = [[st["headers"].get(k, COLUMNS[k][0]) for k in cols]]
    for i, ln in enumerate(inv.lines, 1):
        values = {"sl": str(i), "desc": Paragraph(escape(ln.description), cell), "hsn": ln.hsn, "qty": f"{ln.qty:,}",
                  "unit": ln.unit, "rate": inr(ln.rate), "amount": inr(ln.amount)}
        rows.append([values[k] for k in cols])
    for label, amount in inv.charges:
        values = {"sl": str(len(rows)), "desc": Paragraph(escape(label), cell), "hsn": FREIGHT_HSN, "qty": "",
                  "unit": "", "rate": "", "amount": inr(amount)}
        rows.append([values[k] for k in cols])
    cmds = [("FONTNAME", (0, 0), (-1, -1), st["font"]), ("FONTNAME", (0, 0), (-1, 0), st["bold"]),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TEXTCOLOR", (0, 0), (-1, 0), header_color),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]
    cmds += [("ALIGN", (i, 0), (i, -1), COLUMNS[k][2]) for i, k in enumerate(cols)]
    return Table(rows, colWidths=widths, style=cmds + extra_cmds)


def totals_table(inv, st, width, extra_cmds=()):
    rows = [[st["subtotal"], inr(inv.subtotal)]] + [[l, inr(a)] for l, a in inv.tax_lines]
    rows.append([st["total"], "Rs. " + inr(inv.total)])
    cmds = [("FONTNAME", (0, 0), (-1, -1), st["font"]), ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ALIGN", (1, 0), (1, -1), "RIGHT"), ("FONTNAME", (0, -1), (-1, -1), st["bold"]),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    return Table(rows, colWidths=[width * 0.55, width * 0.45], style=cmds + list(extra_cmds))


def footer(c, st, x, y, right_x):
    c.setFont(st["font"], 8)
    c.setFillColor(colors.HexColor("#444444"))
    c.drawString(x, y, st["terms"])
    c.drawRightString(right_x, y + 40, "Authorised Signatory")
    c.setFillColor(colors.black)


# ----------------------------------------------------------------------------- four layouts
def layout_classic(c, inv, v, st):
    W, H = A4
    m = 18 * mm
    w = W - 2 * m
    c.setFillColor(st["accent"])
    c.setFont(st["bold"], 17)
    c.drawString(m, H - m - 14, v["name"])
    c.setFillColor(colors.black)
    y = text_lines(c, m, H - m - 22, st["address"] + [f"GSTIN: {v['gstin']}", st["contact"]], st["font"], 9, 12.5)
    c.setFont(st["bold"], 15)
    c.setFillColor(st["accent"])
    c.drawRightString(W - m, H - m - 14, "TAX INVOICE")
    c.setFillColor(colors.black)
    meta = Table([[k, v_] for k, v_ in meta_pairs(inv, st)], colWidths=[75, 125],
                 style=[("FONTNAME", (0, 0), (0, -1), st["bold"]), ("FONTNAME", (1, 0), (1, -1), st["font"]),
                        ("FONTSIZE", (0, 0), (-1, -1), 9), ("BOX", (0, 0), (-1, -1), 0.6, st["accent"]),
                        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey)])
    y = min(y, place(c, meta, W - m - 200, H - m - 26, 200)) - 14
    c.setStrokeColor(st["accent"])
    c.setLineWidth(1.2)
    c.line(m, y, W - m, y)
    y = text_lines(c, m, y - 4, ["BILL TO", BUYER["name"], *BUYER["address"], f"GSTIN: {BUYER['gstin']}"], st["font"], 9, 12.5)
    y = place(c, items_table(inv, st, w, [("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                                          ("BACKGROUND", (0, 0), (-1, 0), tint(st["accent"], 0.8))]), m, y - 14, w)
    y = place(c, totals_table(inv, st, 200, [("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.black)]), W - m - 200, y - 8, 200)
    text_lines(c, m, y + 50, ["Bank details for payment", *bank_lines(inv)], st["font"], 9, 12.5)
    footer(c, st, m, 40 * mm, W - m)


def layout_band(c, inv, v, st):
    W, H = A4
    m = 18 * mm
    w = W - 2 * m
    c.setFillColor(st["accent"])
    c.rect(0, H - 34 * mm, W, 34 * mm, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont(st["bold"], 19)
    c.drawString(m, H - 15 * mm, v["name"])
    c.setFont(st["font"], 9)
    c.drawString(m, H - 21 * mm, f"GSTIN {v['gstin']}")
    c.drawString(m, H - 26 * mm, st["contact"])
    c.setFont(st["bold"], 22)
    c.drawRightString(W - m, H - 15 * mm, "INVOICE")
    c.setFillColor(colors.black)
    y = H - 34 * mm - 14
    c.setFont(st["bold"], 8.5)
    c.setFillColor(st["accent"])
    c.drawString(m, y, "BILLED TO")
    c.drawString(W / 2 + 20, y, "INVOICE DETAILS")
    c.setFillColor(colors.black)
    left = text_lines(c, m, y - 2, [BUYER["name"], *BUYER["address"], f"GSTIN: {BUYER['gstin']}"], st["font"], 9, 12.5)
    right = text_lines(c, W / 2 + 20, y - 2, [f"{k}:  {val}" for k, val in meta_pairs(inv, st)], st["font"], 9, 12.5)
    y = min(left, right) - 6
    y = place(c, items_table(inv, st, w, [("BACKGROUND", (0, 0), (-1, 0), st["accent"]),
                                          ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, tint(st["accent"], 0.9)]),
                                          ("LINEBELOW", (0, -1), (-1, -1), 0.8, st["accent"])],
                          header_color=colors.white), m, y - 16, w)
    y = place(c, totals_table(inv, st, 210, [("BACKGROUND", (0, -1), (-1, -1), tint(st["accent"], 0.8))]), W - m - 210, y - 10, 210)
    c.setStrokeColor(st["accent"])
    c.rect(m, y - 14, 220, 66, stroke=1, fill=0)
    text_lines(c, m + 8, y + 48, ["PAYMENT DETAILS", *bank_lines(inv)], st["font"], 8.5, 11)
    footer(c, st, m, 40 * mm, W - m)


def layout_minimal(c, inv, v, st):
    W, H = A4
    m = 22 * mm
    w = W - 2 * m
    c.setFont(st["bold"], 19)
    c.drawCentredString(W / 2, H - m - 6, v["name"])
    c.setFont(st["font"], 9.5)
    for i, line in enumerate([*st["address"], f"GSTIN: {v['gstin']}", st["contact"]]):
        c.drawCentredString(W / 2, H - m - 22 - i * 12.5, line)
    y = H - m - 22 - 4 * 12.5 - 6
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.8)
    c.line(m, y, W - m, y)
    c.setFont(st["bold"], 13)
    c.drawString(m, y - 20, "INVOICE")
    pairs = meta_pairs(inv, st)
    for i, (k, val) in enumerate(pairs):
        c.setFont(st["bold"], 9.5)
        c.drawRightString(W - m - 110, y - 16 - i * 13, k)
        c.setFont(st["font"], 9.5)
        c.drawString(W - m - 100, y - 16 - i * 13, val)
    y = y - 16 - len(pairs) * 13
    y = text_lines(c, m, y - 4, ["Billed to:", BUYER["name"], *BUYER["address"], f"GSTIN: {BUYER['gstin']}"], st["font"], 9.5, 12.5)
    y = place(c, items_table(inv, st, w, [("LINEABOVE", (0, 0), (-1, 0), 0.8, colors.black),
                                          ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.black),
                                          ("LINEBELOW", (0, -1), (-1, -1), 0.8, colors.black)]), m, y - 14, w)
    y = place(c, totals_table(inv, st, 210, [("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.black)]), W - m - 210, y - 8, 210)
    text_lines(c, m, y + 50, ["Please remit to:", *bank_lines(inv)], st["font"], 9.5, 12.5)
    footer(c, st, m, 40 * mm, W - m)


def layout_tally(c, inv, v, st):
    W, H = A4
    m = 14 * mm
    w = W - 2 * m
    c.setFont(st["bold"], 13)
    c.drawCentredString(W / 2, H - m - 8, "TAX INVOICE")
    c.setFont(st["font"], 7.5)
    c.drawRightString(W - m, H - m - 8, "(ORIGINAL FOR RECIPIENT)")
    seller = para(f"<b>{escape(v['name'])}</b><br/>{'<br/>'.join(st['address'])}<br/>GSTIN/UIN: {v['gstin']}<br/>{st['contact']}", st, 9)
    buyer = para(f"<b>Buyer (Bill to)</b><br/>{escape(BUYER['name'])}<br/>{'<br/>'.join(BUYER['address'])}<br/>GSTIN/UIN: {BUYER['gstin']}", st, 9)
    meta = Table([[k, val] for k, val in meta_pairs(inv, st)], colWidths=[w * 0.18, w * 0.29],
                 style=[("FONTNAME", (0, 0), (0, -1), st["bold"]), ("FONTNAME", (1, 0), (1, -1), st["font"]),
                        ("FONTSIZE", (0, 0), (-1, -1), 8.5), ("GRID", (0, 0), (-1, -1), 0.4, colors.grey)])
    head = Table([[seller, meta], [buyer, para(st["terms"], st, 8)]], colWidths=[w * 0.5, w * 0.5],
                 style=[("GRID", (0, 0), (-1, -1), 0.7, colors.black), ("VALIGN", (0, 0), (-1, -1), "TOP")])
    y = place(c, head, m, H - m - 16, w)
    y = place(c, items_table(inv, st, w, [("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                                          ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEEEEE"))]), m, y - 6, w)
    y = place(c, totals_table(inv, st, 230, [("BOX", (0, 0), (-1, -1), 0.7, colors.black),
                                             ("LINEABOVE", (0, -1), (-1, -1), 0.7, colors.black)]), W - m - 230, y - 6, 230)
    bank = para("<b>Company's Bank Details</b><br/>" + "<br/>".join(escape(l) for l in bank_lines(inv)), st, 8.5)
    sign = para(f"for <b>{escape(v['name'])}</b><br/><br/><br/>Authorised Signatory", st, 8.5, align=2)
    place(c, Table([[bank, sign]], colWidths=[w * 0.5, w * 0.5], style=[("BOX", (0, 0), (-1, -1), 0.7, colors.black),
                                                                        ("LINEAFTER", (0, 0), (0, 0), 0.7, colors.black)]), m, y - 16, w)
    c.setLineWidth(0.9)
    c.rect(m - 3, 12 * mm, w + 6, H - 12 * mm - m + 3 - 0, stroke=1, fill=0)


LAYOUTS = {"classic": layout_classic, "band": layout_band, "minimal": layout_minimal, "tally": layout_tally}


# ----------------------------------------------------------------------------- PDF output
def render_pdf_bytes(inv):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, invariant=1)  # invariant -> identical bytes on every run
    c.setTitle(f"Invoice {inv.spec['number']}")
    st = STYLES[inv.vendor["vendor_id"]]
    LAYOUTS[st["layout"]](c, inv, inv.vendor, st)
    c.showPage()
    c.save()
    return buf.getvalue()


def scan_effect(pdf_bytes, out_path, seed=7):
    """Render to an image, rotate slightly, add noise, save as an image-only PDF (no text layer)."""
    pix = pymupdf.open(stream=pdf_bytes, filetype="pdf")[0].get_pixmap(dpi=150)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
    img = img.rotate(1.6, resample=Image.BICUBIC, fillcolor=226)  # slightly crooked on the scanner bed
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    noise = Image.frombytes("L", img.size, random.Random(seed).randbytes(img.width * img.height))
    fixed = time.struct_time((2026, 10, 1, 0, 0, 0, 3, 274, 0))  # fixed timestamps keep the file byte-identical on every run
    Image.blend(img, noise, 0.12).save(out_path, "PDF", resolution=150, creationDate=fixed, modDate=fixed)


def write_all(invoices):
    OUT.mkdir(parents=True, exist_ok=True)
    for inv in invoices:
        data = render_pdf_bytes(inv)
        path = OUT / inv.spec["file"]
        if inv.spec.get("scanned"):
            scan_effect(data, path)
        else:
            path.write_bytes(data)


def verify_pdfs(invoices):
    print("\nPDF readability check")
    for inv in invoices:
        path, s = OUT / inv.spec["file"], inv.spec
        with pdfplumber.open(path) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages).strip()
            pages, images = len(pdf.pages), sum(len(p.images) for p in pdf.pages)
        if s.get("scanned"):
            ok = len(text) == 0 and images >= 1
            kind = f"image-only ({images} image, {len(text)} text chars)"
        else:
            ok = inv.vendor["gstin"] in text and s["number"] in text and (not s["show_po"] or s["po"] in text)
            kind = f"text ({len(text)} chars, GSTIN + invoice no. found)"
        print(f"  {'OK ' if ok else 'BAD'} {s['id']:<3} {path.name:<34} {pages} page, {kind}")
        if not ok:
            sys.exit(1)


def print_summary(invoices):
    print("\nScenario summary (computed from the seed CSVs)")
    head = ("Scenario", "PO subtotal", "Approved-to-date", "Invoice subtotal", "Utilisation", "Expected decision", "Expected primary check")
    rows = []
    for inv in invoices:
        s = inv.spec
        po_total, done = po_subtotal(s["po"]), approved_to_date(s["po"])
        util = ((done + inv.subtotal) / po_total * 100).quantize(Decimal("0.1"))
        rows.append((f"{s['id']} ({s['po']})", inr(po_total), inr(done), inr(inv.subtotal),
                     f"{util.normalize():f}%", s["decision"], s["check"]))
    widths = [max(len(r[i]) for r in [head, *rows]) for i in range(len(head))]
    for r in [head, *rows]:
        print("  " + "  ".join(val.ljust(widths[i]) if i in (0, 5, 6) else val.rjust(widths[i]) for i, val in enumerate(r)))


def main():
    invoices = [build_invoice(s) for s in SCENARIOS]
    verify_seed(invoices)
    write_all(invoices)
    verify_pdfs(invoices)
    print_summary(invoices)


if __name__ == "__main__":
    main()
