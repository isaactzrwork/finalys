from datetime import date, timedelta

from supabase import Client, create_client

from bot.config import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    return _client


def get_categories() -> list[dict]:
    res = get_client().table("categories").select("id, name").order("id").execute()
    return res.data


def get_payment_methods() -> list[dict]:
    res = get_client().table("payment_methods").select("id, label").order("id").execute()
    return res.data


def get_payment_methods_with_miles_programs() -> list[dict]:
    res = (
        get_client()
        .table("payment_methods")
        .select("id, label, miles_program, card_last4")
        .not_.is_("miles_program", "null")
        .order("id")
        .execute()
    )
    return res.data


def match_payment_method_by_last4(card_last4: str) -> dict | None:
    res = (
        get_client()
        .table("payment_methods")
        .select("id, label, miles_program")
        .eq("card_last4", card_last4)
        .execute()
    )
    return res.data[0] if res.data else None


def get_payment_method(payment_method_id: int) -> dict:
    res = get_client().table("payment_methods").select("*").eq("id", payment_method_id).single().execute()
    return res.data


def insert_transaction(
    *,
    merchant: str,
    amount: float,
    currency: str,
    transaction_date: str,
    category_name: str,
    payment_method_id: int,
    source_type: str,
    raw_input: str | None,
) -> dict:
    categories = {c["name"]: c["id"] for c in get_categories()}
    category_id = categories.get(category_name)
    if category_id is None:
        raise ValueError(f"Unknown category: {category_name!r}")

    res = (
        get_client()
        .table("transactions")
        .insert(
            {
                "merchant": merchant,
                "amount": amount,
                "currency": currency,
                "transaction_date": transaction_date,
                "category_id": category_id,
                "payment_method_id": payment_method_id,
                "source_type": source_type,
                "raw_input": raw_input,
            }
        )
        .execute()
    )
    return res.data[0]


def find_recent_transaction_by_merchant(merchant_hint: str, days_back: int = 90) -> dict | None:
    cutoff = (date.today() - timedelta(days=days_back)).isoformat()
    res = (
        get_client()
        .table("transactions")
        .select(
            "id, merchant, amount, currency, transaction_date, category_id, payment_method_id, "
            "categories(name), payment_methods(label)"
        )
        .ilike("merchant", f"%{merchant_hint}%")
        .gte("transaction_date", cutoff)
        .order("transaction_date", desc=True)
        .order("id", desc=True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def update_transaction_field(transaction_id: int, column: str, value) -> dict:
    res = get_client().table("transactions").update({column: value}).eq("id", transaction_id).execute()
    return res.data[0]


def insert_miles_ledger(
    *,
    payment_method_id: int,
    statement_period_start: str,
    statement_period_end: str,
    miles_earned: float,
    balance: float | None = None,
    bank_flagged_expiring_amount: float | None = None,
    bank_flagged_expiry_date: str | None = None,
    extracted_from: str | None,
) -> dict:
    res = (
        get_client()
        .table("miles_ledger")
        .insert(
            {
                "payment_method_id": payment_method_id,
                "statement_period_start": statement_period_start,
                "statement_period_end": statement_period_end,
                "miles_earned": miles_earned,
                "balance": balance,
                "bank_flagged_expiring_amount": bank_flagged_expiring_amount,
                "bank_flagged_expiry_date": bank_flagged_expiry_date,
                "extracted_from": extracted_from,
            }
        )
        .execute()
    )
    return res.data[0]
