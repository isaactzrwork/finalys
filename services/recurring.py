import re
from datetime import date, datetime, timedelta, timezone

from services.supabase_client import get_client

TOLERANCE = 0.10
LOOKBACK_DAYS = 90

_NOISE_TOKENS = {"sg", "singapore", "pte", "ltd", "inc", "corp", "llc", "com", "co"}


def _within_tolerance(a: float, b: float) -> bool:
    return abs(a - b) <= TOLERANCE * max(a, b)


def _normalize_merchant(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9\s]", " ", name.lower())
    tokens = [t for t in cleaned.split() if t and t not in _NOISE_TOKENS]
    return " ".join(tokens)


def _merchants_match(a: str, b: str) -> bool:
    na, nb = _normalize_merchant(a), _normalize_merchant(b)
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na


def find_known_recurring(merchant: str, amount: float) -> dict | None:
    """Fuzzy merchant match (normalized, noise-word-stripped) + amount within tolerance.
    Returns the existing row (confirmed or declined) if this pattern is already known, else None."""
    res = get_client().table("recurring_charges").select("*").execute()
    for row in res.data:
        if _merchants_match(row["merchant"], merchant) and _within_tolerance(row["reference_amount"], amount):
            return row
    return None


def count_similar_recent_transactions(merchant: str, amount: float, as_of: date) -> int:
    cutoff = as_of - timedelta(days=LOOKBACK_DAYS)
    res = (
        get_client()
        .table("transactions")
        .select("merchant, amount")
        .gte("transaction_date", cutoff.isoformat())
        .lte("transaction_date", as_of.isoformat())
        .execute()
    )
    return sum(
        1
        for row in res.data
        if _merchants_match(row["merchant"], merchant) and _within_tolerance(row["amount"], amount)
    )


def refresh_last_seen(row: dict, as_of: date) -> None:
    """Bump a known confirmed recurring charge's last_seen_date forward when a newer matching
    transaction comes in, so next-occurrence projections stay based on the most recent
    instance rather than the date it was first confirmed."""
    if row["status"] != "confirmed":
        return
    current = date.fromisoformat(row["last_seen_date"]) if row.get("last_seen_date") else None
    if current and as_of <= current:
        return
    get_client().table("recurring_charges").update(
        {"last_seen_date": as_of.isoformat(), "updated_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", row["id"]).execute()


def is_recurring_candidate(merchant: str, amount: float, as_of: date) -> bool:
    known = find_known_recurring(merchant, amount)
    if known:
        refresh_last_seen(known, as_of)
        return False
    return count_similar_recent_transactions(merchant, amount, as_of) >= 2


def save_recurring_decision(
    *,
    merchant: str,
    amount: float,
    status: str,
    frequency: str | None,
    category_id: int | None,
    last_seen_date: date,
    custom_frequency_note: str | None = None,
) -> dict:
    payload = {
        "merchant": merchant,
        "reference_amount": amount,
        "status": status,
        "frequency": frequency,
        "custom_frequency_note": custom_frequency_note,
        "category_id": category_id,
        "last_seen_date": last_seen_date.isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return get_client().table("recurring_charges").insert(payload).execute().data[0]
