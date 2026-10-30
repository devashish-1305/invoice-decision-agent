"""Small helpers shared across the app: masking, rupee formatting, text normalisation."""
import re
from decimal import Decimal

ACCOUNT_RUN = re.compile(r"\d{9,}")  # bank account numbers are 9+ digits; nothing else on an invoice is


def mask_account(account: str | None) -> str | None:
    """'6021145570' -> 'XXXXXX5570'. Used everywhere a bank account leaves BANK_MATCH."""
    if not account:
        return account
    digits = re.sub(r"[\s-]", "", account)
    return "X" * max(len(digits) - 4, 0) + digits[-4:]


def mask_text(text: str | None) -> str | None:
    """Mask any account-number-looking digit run inside free text (evidence, messages)."""
    if not text:
        return text
    return ACCOUNT_RUN.sub(lambda m: mask_account(m.group()), text)


def inr(amount: Decimal) -> str:
    """Indian digit grouping with the rupee sign: 540000 -> '₹5,40,000.00'."""
    whole, frac = f"{Decimal(amount):.2f}".split(".")
    sign = ""
    if whole.startswith("-"):
        sign, whole = "-", whole[1:]
    if len(whole) > 3:
        head, tail, groups = whole[:-3], whole[-3:], []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + groups + [tail])
    return f"{sign}₹{whole}.{frac}"


def norm_text(text: str) -> str:
    """Lower-case, drop punctuation, collapse spaces: for deterministic description matching."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def norm_id(text: str) -> str:
    """Upper-case alphanumerics only: 'INV-1042' and 'inv 1042' are the same identifier."""
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def edit_ratio(a: str, b: str) -> float:
    """Levenshtein distance divided by the longer length (0 = identical, 1 = nothing shared)."""
    if not a and not b:
        return 0.0
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1] / max(len(a), len(b))


def mask_json(value):
    """Defence in depth: walk any JSON-like structure and mask account-number-looking digit runs."""
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        return {k: mask_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_json(v) for v in value]
    return value
