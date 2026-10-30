"""Stage 1: Ingest. No AI.

Text PDF -> extract the text layer (pdfplumber).
No text layer -> render each page to a PNG (PyMuPDF) so the LLM can read it with vision. No OCR library.

check_pdf() is the gate for every upload. POST /api/runs calls it BEFORE a run exists, so a file that can't be
processed is refused with a plain message (what is wrong + what to do) and no LLM call is ever made. ingest() runs
the very same gate first, so the pipeline and the upload endpoint can never disagree about what is readable.
"""
import io
from dataclasses import dataclass, field

import pdfplumber
import pymupdf

from app.config import Settings

BLANK_DPI = 30            # tiny render, only used to ask "is anything drawn on this page?"
BLANK_PIXEL_MIN = 245     # a grayscale pixel this light or lighter counts as paper


class IngestError(Exception):
    """The message is written for the person who uploaded the file."""


def check_pdf(pdf_bytes: bytes, settings: Settings) -> list[str]:
    """Raise IngestError with a clear message if the file can't be processed. Returns the text of each page."""
    if not pdf_bytes:
        raise IngestError("This file is empty (0 bytes). Choose the invoice PDF and try again.")
    if len(pdf_bytes) > settings.max_upload_bytes:
        raise IngestError(f"This file is larger than {settings.max_upload_bytes // (1024 * 1024)} MB. "
                          f"Upload a PDF under {settings.max_upload_bytes // (1024 * 1024)} MB.")
    if not pdf_bytes.startswith(b"%PDF-"):
        raise IngestError("This isn't a PDF file. Upload the invoice as a PDF.")
    damaged = IngestError("This PDF is damaged and can't be read. Upload another copy of the invoice.")
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:  # noqa: BLE001
        raise damaged from e
    with doc:
        if doc.needs_pass:
            raise IngestError("This file is password-protected. Upload an unlocked PDF.")
        pages = doc.page_count
        if pages == 0:
            raise IngestError("This PDF has no pages. Upload the invoice itself.")
        if pages > settings.max_pages:
            raise IngestError(f"This PDF has {pages} pages; the limit is {settings.max_pages}. "
                              f"Upload a PDF of {settings.max_pages} pages or fewer (one invoice per file).")
        # The pipeline reads text with pdfplumber, so the gate must be able to as well (PyMuPDF alone repairs
        # truncated files that pdfplumber then cannot open).
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                texts = [page.extract_text() or "" for page in pdf.pages]
        except Exception as e:  # noqa: BLE001
            raise damaged from e
        if sum(len(t.strip()) for t in texts) < settings.scanned_text_min_chars and all(_is_blank(p) for p in doc):
            raise IngestError("This PDF looks blank: it has no text and nothing visible on any page. "
                              "Upload the invoice itself.")
    return texts


def _is_blank(page) -> bool:
    pixmap = page.get_pixmap(dpi=BLANK_DPI, colorspace=pymupdf.csGRAY)
    return min(pixmap.samples) >= BLANK_PIXEL_MIN


@dataclass
class IngestResult:
    doc_type: str                                   # "text" | "scanned"
    page_count: int
    page_texts: list[str] = field(default_factory=list)
    page_images: list[bytes] = field(default_factory=list)   # PNG bytes, scanned documents only

    def summary(self) -> dict:
        return {"doc_type": self.doc_type, "page_count": self.page_count,
                "text_chars": sum(len(t.strip()) for t in self.page_texts)}


def ingest(pdf_bytes: bytes, settings: Settings) -> IngestResult:
    texts = check_pdf(pdf_bytes, settings)
    if sum(len(t.strip()) for t in texts) >= settings.scanned_text_min_chars:
        return IngestResult("text", len(texts), page_texts=texts)
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        images = [page.get_pixmap(dpi=settings.render_dpi).tobytes("png") for page in doc]
    return IngestResult("scanned", len(images), page_texts=texts, page_images=images)
