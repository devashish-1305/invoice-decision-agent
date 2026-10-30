"""Settings and every tunable threshold (PRD section 5).

Rule code never contains a number: it reads it from here, so a different client can
have different tolerances without a code change.
"""
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_DIR / ".env")  # the API key lives only in backend/.env (gitignored)


@dataclass(frozen=True)
class Settings:
    # --- rule thresholds ---
    tolerance_pct: Decimal = Decimal("2.0")          # AMOUNT_TOLERANCE and CUMULATIVE_PO headroom
    price_tolerance_pct: Decimal = Decimal("2.0")    # LINE_MATCH unit price vs PO price
    confidence_min: float = 0.80                     # EXTRACTION_CONFIDENCE
    near_dup_days: int = 30                          # DUPLICATE_NEAR date window
    near_dup_total_pct: Decimal = Decimal("0.5")     # DUPLICATE_NEAR total window
    near_dup_max_edit_ratio: float = 0.20            # DUPLICATE_NEAR invoice-number similarity
    arithmetic_tolerance: Decimal = Decimal("1")     # rupees, ARITHMETIC
    valid_gst_rates: tuple = (Decimal(0), Decimal(5), Decimal(12), Decimal(18), Decimal(28))
    gst_rate_tolerance_pct: Decimal = Decimal("0.1")
    stale_invoice_days: int = 180                    # older than this -> WARN

    # --- matching ---
    vendor_name_suggest_min: float = 0.60            # name similarity needed to suggest a vendor
    po_infer_min_confidence: float = 0.60            # below this an inferred PO is discarded
    line_match_min_confidence: float = 0.80          # semantic line matches below this are rejected

    # --- ingest limits ---
    max_upload_bytes: int = 10 * 1024 * 1024
    max_pages: int = 5
    scanned_text_min_chars: int = 30                 # fewer characters than this = no text layer
    render_dpi: int = 200

    # --- LLM ---
    llm_provider: str = os.environ.get("LLM_PROVIDER", "anthropic")
    llm_model: str = os.environ.get("LLM_MODEL", "claude-sonnet-5-5")
    llm_timeout_s: float = 30.0
    llm_max_tokens: int = 4096
    llm_effort: str = "low"                          # thinking depth: low keeps a run under 30 s

    # --- storage ---
    database_url: str = f"sqlite:///{BACKEND_DIR / 'invoice_agent.db'}"
    data_dir: Path = BACKEND_DIR / "data"
    frontend_dist: Path = BACKEND_DIR.parent / "frontend" / "dist"   # built React app, served by FastAPI


def get_settings() -> Settings:
    return Settings()
