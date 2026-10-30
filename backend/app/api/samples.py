"""GET /api/samples: the six scenario cards. Served from data/invoices/samples.json, which
scripts/generate_invoices.py computes from the seed data (so amounts and outcomes never drift)."""
import json

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import AppState, get_state

router = APIRouter()


def load_samples(state: AppState) -> list[dict]:
    path = state.settings.data_dir / "invoices" / "samples.json"
    if not path.exists():
        raise HTTPException(500, "samples.json is missing. Run: python scripts/generate_invoices.py")
    return json.loads(path.read_text(encoding="utf-8"))


@router.get("/samples")
def list_samples(state: AppState = Depends(get_state)):
    return [{k: v for k, v in card.items() if k != "file"} for card in load_samples(state)]
