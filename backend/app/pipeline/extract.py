"""Stage 2: Extract. The LLM reads the document and returns an ExtractedInvoice (schema-validated).

Text PDFs are sent as text; scanned PDFs as page images (vision). The document is wrapped as data and the
prompt tells the model to ignore any instructions inside it.
"""
from app.llm.client import LLMClient, image_block, text_block
from app.pipeline.ingest import IngestResult
from app.schemas import ExtractedInvoice


def extract(llm: LLMClient, ingested: IngestResult) -> ExtractedInvoice:
    if ingested.doc_type == "text":
        pages = "\n".join(f"--- Page {i} ---\n{text}" for i, text in enumerate(ingested.page_texts, 1))
        content = [text_block(f"<document>\n{pages}\n</document>\n\nExtract the invoice from the document above.")]
    else:
        content = []
        for i, png in enumerate(ingested.page_images, 1):
            content += [text_block(f"Page {i}:"), image_block(png)]
        content.append(text_block("Extract the invoice from the page images above."))
    return llm.call("extract", content, ExtractedInvoice)
