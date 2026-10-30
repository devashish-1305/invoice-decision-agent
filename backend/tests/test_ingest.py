import io

import pytest
from reportlab.pdfgen import canvas

from app.config import Settings
from app.pipeline.ingest import IngestError, check_pdf, ingest
from conftest import sample_pdf


def test_text_pdf_is_detected_and_text_extracted():
    r = ingest(sample_pdf("H"), Settings())
    assert r.doc_type == "text" and r.page_count == 1 and "SOS/2627/0418" in r.page_texts[0] and r.page_images == []


def test_scanned_pdf_has_no_text_layer_and_is_rendered_to_png_pages():
    r = ingest(sample_pdf("E3"), Settings())
    assert r.doc_type == "scanned" and r.summary()["text_chars"] == 0
    assert len(r.page_images) == 1 and r.page_images[0].startswith(b"\x89PNG")


def test_rejects_non_pdf():
    with pytest.raises(IngestError, match="isn't a PDF"):
        ingest(b"hello", Settings())


def test_rejects_oversized_file():
    with pytest.raises(IngestError, match="larger than"):
        ingest(sample_pdf("H"), Settings(max_upload_bytes=100))


def test_rejects_too_many_pages():
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for i in range(6):
        c.drawString(72, 700, f"page {i}")
        c.showPage()
    c.save()
    with pytest.raises(IngestError, match="6 pages"):
        ingest(buf.getvalue(), Settings())


def test_rejects_corrupt_pdf():
    with pytest.raises(IngestError, match="damaged"):
        ingest(b"%PDF-1.4 this is not really a pdf", Settings())


def test_rejects_a_truncated_pdf_that_pymupdf_could_repair_but_pdfplumber_cannot_read():
    data = sample_pdf("H")
    with pytest.raises(IngestError, match="damaged"):
        check_pdf(data[: int(len(data) * 0.6)], Settings())


def test_rejects_a_password_protected_pdf_with_a_clear_instruction():
    from reportlab.lib.pdfencrypt import StandardEncryption
    buf = io.BytesIO()
    c = canvas.Canvas(buf, encrypt=StandardEncryption("userpw", "ownerpw"))
    c.drawString(72, 700, "Invoice 1234 total 100.00 (secret)")
    c.showPage()
    c.save()
    with pytest.raises(IngestError) as e:
        check_pdf(buf.getvalue(), Settings())
    assert str(e.value) == "This file is password-protected. Upload an unlocked PDF."


def test_rejects_a_blank_pdf_but_accepts_a_scan_with_no_text_layer():
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.showPage()
    c.save()
    with pytest.raises(IngestError, match="looks blank"):
        check_pdf(buf.getvalue(), Settings())
    assert check_pdf(sample_pdf("E3"), Settings()) == [""]          # image-only scan: no text, but content on the page


def test_rejects_an_empty_file_with_what_to_do():
    with pytest.raises(IngestError, match="empty .* Choose the invoice PDF"):
        check_pdf(b"", Settings())
