"""Stage 1: Ingest. No AI.

Text PDF -> extract the text layer (pdfplumber).
No text layer -> render each page to a PNG (PyMuPDF) so the LLM can read it with vision. No OCR library.
Also enforces the upload limits (size, page count, really a PDF).
"""
import io
from dataclasses import dataclass, field

import pdfplumber
import pymupdf

from app.config import Settings


class IngestError(Exception):
    pass


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
    if len(pdf_bytes) > settings.max_upload_bytes:
        raise IngestError(f"File is larger than {settings.max_upload_bytes // (1024 * 1024)} MB")
    if not pdf_bytes.startswith(b"%PDF-"):
        raise IngestError("Not a PDF file")
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            if len(pdf.pages) > settings.max_pages:
                raise IngestError(f"PDF has {len(pdf.pages)} pages; the limit is {settings.max_pages}")
            texts = [page.extract_text() or "" for page in pdf.pages]
    except IngestError:
        raise
    except Exception as e:
        raise IngestError(f"PDF could not be read: {e}") from e

    if sum(len(t.strip()) for t in texts) >= settings.scanned_text_min_chars:
        return IngestResult("text", len(texts), page_texts=texts)

    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        images = [page.get_pixmap(dpi=settings.render_dpi).tobytes("png") for page in doc]
    return IngestResult("scanned", len(images), page_texts=texts, page_images=images)
