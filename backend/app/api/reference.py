"""GET /api/reference (vendors with masked bank accounts, POs with consumption) and POST /api/reset."""
from decimal import Decimal

from fastapi import APIRouter, Depends

from app.api.deps import AppState, get_session, get_state
from app.db import repository as repo
from app.db.seed import reset
from app.util import mask_account

router = APIRouter()

# First two digits of a GSTIN are the state code.
GST_STATES = {
    "01": "Jammu & Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh", "05": "Uttarakhand",
    "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura",
    "17": "Meghalaya", "18": "Assam", "19": "West Bengal", "20": "Jharkhand", "21": "Odisha",
    "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat", "26": "Dadra & Nagar Haveli and Daman & Diu",
    "27": "Maharashtra", "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu",
    "34": "Puducherry", "35": "Andaman & Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
}


@router.get("/reference")
def reference(session=Depends(get_session)):
    data = repo.load_reference(session)
    names = {v.vendor_id: v.name for v in data.vendors}
    vendors = [{"vendor_id": v.vendor_id, "name": v.name, "gstin": v.gstin,
                "state": GST_STATES.get(v.gstin[:2], f"State code {v.gstin[:2]}"),
                "bank_account": mask_account(v.bank_account), "ifsc": v.ifsc, "status": v.status}
               for v in data.vendors]
    pos = []
    for po in data.pos:
        consumed, total = repo.approved_to_date(session, po.po_id), po.subtotal
        pos.append({"po_id": po.po_id, "vendor_id": po.vendor_id, "vendor": names[po.vendor_id],
                    "po_date": po.po_date.isoformat(), "status": po.status, "lines": len(po.lines),
                    "subtotal": str(total), "approved_to_date": str(consumed),
                    "utilisation_pct": str((consumed / total * 100).quantize(Decimal("0.1")))})
    return {"vendors": vendors, "pos": pos}


@router.post("/reset")
def reset_demo(state: AppState = Depends(get_state)):
    """Restore the seed data and clear run history."""
    reset(state.engine, state.session_factory, state.settings)
    return {"status": "reset"}
