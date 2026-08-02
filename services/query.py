from datetime import date, timedelta

from services.safe_to_spend import compute_safe_to_spend
from services.supabase_client import get_categories, get_client


def query_transactions(
    category: str | None = None,
    merchant: str | None = None,
    payment_method: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    group_by: str | None = None,
) -> dict:
    """Safe, parameterized query over the user's own transactions — never raw SQL from user input."""
    q = (
        get_client()
        .table("transactions")
        .select("amount, merchant, transaction_date, category_id, payment_method_id, categories(name), payment_methods(label)")
    )

    if date_from:
        q = q.gte("transaction_date", date_from)
    if date_to:
        q = q.lte("transaction_date", date_to)
    if merchant:
        q = q.ilike("merchant", f"%{merchant}%")

    if category:
        cats = {c["name"]: c["id"] for c in get_categories()}
        cat_id = cats.get(category)
        if cat_id is None:
            return {"error": f"Unknown category: {category!r}. Known categories: {list(cats)}"}
        q = q.eq("category_id", cat_id)

    if payment_method:
        pms = get_client().table("payment_methods").select("id, label").execute().data
        matches = [p["id"] for p in pms if payment_method.lower() in p["label"].lower()]
        if not matches:
            return {"error": f"No payment method matching {payment_method!r} found."}
        q = q.in_("payment_method_id", matches)

    rows = q.execute().data

    if not rows:
        return {"total": 0, "count": 0, "transactions": []}

    total = sum(r["amount"] for r in rows)

    if group_by in ("category", "merchant", "payment_method", "day", "week", "month"):
        groups: dict[str, float] = {}
        for r in rows:
            if group_by == "category":
                key = r["categories"]["name"] if r["categories"] else "Unknown"
            elif group_by == "merchant":
                key = r["merchant"]
            elif group_by == "payment_method":
                key = r["payment_methods"]["label"] if r["payment_methods"] else "Unknown"
            elif group_by == "day":
                key = r["transaction_date"]
            elif group_by == "week":
                d = date.fromisoformat(r["transaction_date"])
                key = f"Week of {(d - timedelta(days=d.weekday())).isoformat()}"
            else:  # month
                key = r["transaction_date"][:7]
            groups[key] = groups.get(key, 0) + r["amount"]
        return {"total": total, "count": len(rows), "groups": groups}

    return {
        "total": total,
        "count": len(rows),
        "transactions": [{"merchant": r["merchant"], "amount": r["amount"], "date": r["transaction_date"]} for r in rows[:20]],
    }


def get_budget_status() -> dict:
    return compute_safe_to_spend(date.today())
