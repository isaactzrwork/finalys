from datetime import date, timedelta

from bot.dates import format_date
from services.budgets import month_end, month_start
from services.miles import get_miles_timeline
from services.supabase_client import get_client, get_payment_methods_with_miles_programs


def get_spending_by_category(today: date) -> list[dict]:
    m_start, m_end = month_start(today), month_end(today)
    res = (
        get_client()
        .table("transactions")
        .select("amount, category_id, categories(name)")
        .gte("transaction_date", m_start.isoformat())
        .lte("transaction_date", m_end.isoformat())
        .execute()
    )
    totals: dict[str, float] = {}
    for r in res.data:
        name = r["categories"]["name"] if r["categories"] else "Unknown"
        totals[name] = totals.get(name, 0) + r["amount"]
    return sorted(({"label": k, "amount": v} for k, v in totals.items()), key=lambda x: -x["amount"])


def get_spending_by_payment_method(today: date) -> list[dict]:
    m_start, m_end = month_start(today), month_end(today)
    res = (
        get_client()
        .table("transactions")
        .select("amount, payment_method_id, payment_methods(label)")
        .gte("transaction_date", m_start.isoformat())
        .lte("transaction_date", m_end.isoformat())
        .execute()
    )
    totals: dict[str, float] = {}
    for r in res.data:
        label = r["payment_methods"]["label"] if r["payment_methods"] else "Unknown"
        totals[label] = totals.get(label, 0) + r["amount"]
    return sorted(({"label": k, "amount": v} for k, v in totals.items()), key=lambda x: -x["amount"])


def get_miles_expiry_timeline(within_days: int | None = None) -> list[dict]:
    """Ledger entries with an actual expiry date (cards that never expire are excluded — there's
    nothing to show in an expiry timeline for those). If within_days is set, only entries expiring
    in the rolling window [today, today + within_days] are included."""
    today = date.today()
    window_end = today + timedelta(days=within_days) if within_days is not None else None

    entries = []
    for pm in get_payment_methods_with_miles_programs():
        for entry in get_miles_timeline(pm["id"]):
            if not entry["expiry_date"]:
                continue

            expiry = date.fromisoformat(entry["expiry_date"])
            if window_end is not None and not (today <= expiry <= window_end):
                continue

            entries.append(
                {
                    "card": pm["label"],
                    "program": pm["miles_program"],
                    "miles": entry["miles_earned"],
                    "period_end": format_date(entry["statement_period_end"]),
                    "expiry_date": format_date(entry["expiry_date"]),
                    "expiry_sort_key": entry["expiry_date"],
                }
            )

    entries.sort(key=lambda e: e["expiry_sort_key"])
    return entries


def get_total_miles_by_card() -> list[dict]:
    """Latest known balance per card, taken from that card's most recent statement upload."""
    entries = []
    for pm in get_payment_methods_with_miles_programs():
        res = (
            get_client()
            .table("miles_ledger")
            .select("balance, statement_period_end")
            .eq("payment_method_id", pm["id"])
            .order("statement_period_end", desc=True)
            .limit(1)
            .execute()
        )
        if res.data and res.data[0]["balance"] is not None:
            entries.append(
                {
                    "card": pm["label"],
                    "program": pm["miles_program"],
                    "balance": res.data[0]["balance"],
                    "as_of": format_date(res.data[0]["statement_period_end"]),
                }
            )
    return sorted(entries, key=lambda e: -e["balance"])


def get_bank_flagged_expiries() -> list[dict]:
    """The bank's own explicit 'expiring soon' flags (e.g. DBS's 'Expiring On' column), taken
    from each card's most recent statement upload — more precise than our FIFO estimate when
    the bank actually states it, since it's authoritative rather than derived."""
    entries = []
    for pm in get_payment_methods_with_miles_programs():
        res = (
            get_client()
            .table("miles_ledger")
            .select("bank_flagged_expiring_amount, bank_flagged_expiry_date, statement_period_end")
            .eq("payment_method_id", pm["id"])
            .order("statement_period_end", desc=True)
            .limit(1)
            .execute()
        )
        if res.data and res.data[0]["bank_flagged_expiring_amount"] is not None:
            row = res.data[0]
            entries.append(
                {
                    "card": pm["label"],
                    "program": pm["miles_program"],
                    "amount": row["bank_flagged_expiring_amount"],
                    "expiry_date": format_date(row["bank_flagged_expiry_date"]),
                    "expiry_sort_key": row["bank_flagged_expiry_date"],
                    "as_of": format_date(row["statement_period_end"]),
                }
            )
    return sorted(entries, key=lambda e: e["expiry_sort_key"])
