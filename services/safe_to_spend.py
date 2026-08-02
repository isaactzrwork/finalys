from datetime import date

from dateutil.relativedelta import relativedelta

from services.budgets import get_budget_period, month_end, month_start
from services.supabase_client import get_categories, get_client

FREQUENCY_MONTHS = {"monthly": 1, "quarterly": 3, "semi_annual": 6, "yearly": 12}


def _next_occurrence(last_seen: date, frequency: str | None) -> date | None:
    months = FREQUENCY_MONTHS.get(frequency)
    if months is None:
        return None  # custom frequency — can't be projected automatically
    return last_seen + relativedelta(months=months)


def get_upcoming_recurring(today: date, period_end: date) -> list[dict]:
    """Confirmed recurring charges whose next projected occurrence falls within
    [today, period_end] — i.e. due before the period ends but hasn't happened yet this cycle."""
    res = get_client().table("recurring_charges").select("*").eq("status", "confirmed").execute()
    upcoming = []
    for row in res.data:
        if not row.get("last_seen_date"):
            continue
        next_date = _next_occurrence(date.fromisoformat(row["last_seen_date"]), row["frequency"])
        if next_date and today <= next_date <= period_end:
            upcoming.append({**row, "next_date": next_date})
    return upcoming


def get_month_spend(month_start_date: date, month_end_date: date, category_id: int | None = None) -> float:
    query = (
        get_client()
        .table("transactions")
        .select("amount, category_id")
        .gte("transaction_date", month_start_date.isoformat())
        .lte("transaction_date", month_end_date.isoformat())
    )
    if category_id is not None:
        query = query.eq("category_id", category_id)
    res = query.execute()
    return sum(row["amount"] for row in res.data)


def compute_safe_to_spend(today: date) -> dict:
    """Returns a dict describing safe-to-spend status for `today`'s budget period.
    Always has a 'mode' key; mode == 'none' means no budget is set for this month."""
    m_start = month_start(today)
    m_end = month_end(today)
    days_remaining = (m_end - today).days + 1

    period = get_budget_period(m_start)
    if not period or period["mode"] == "none":
        return {"mode": "none"}

    all_spend = get_month_spend(m_start, m_end)
    upcoming = get_upcoming_recurring(today, m_end)
    upcoming_total = sum(u["reference_amount"] for u in upcoming)

    if period["mode"] == "overall":
        remaining = period["overall_amount"] - all_spend
        safe_today = (remaining - upcoming_total) / days_remaining
        return {
            "mode": "overall",
            "budget": period["overall_amount"],
            "spend": all_spend,
            "remaining": remaining,
            "upcoming": upcoming,
            "upcoming_total": upcoming_total,
            "days_remaining": days_remaining,
            "safe_today": safe_today,
        }

    # per_category
    cat_names = {c["id"]: c["name"] for c in get_categories()}
    cat_amounts = {row["category_id"]: row["amount"] for row in period.get("budget_category_amounts", [])}

    per_category = {}
    budgeted_spend_total = 0.0
    for cat_id, budget_amt in cat_amounts.items():
        spent = get_month_spend(m_start, m_end, category_id=cat_id)
        cat_upcoming = [u for u in upcoming if u.get("category_id") == cat_id]
        cat_upcoming_total = sum(u["reference_amount"] for u in cat_upcoming)
        remaining_cat = budget_amt - spent
        per_category[cat_id] = {
            "name": cat_names.get(cat_id, "?"),
            "budget": budget_amt,
            "spent": spent,
            "remaining": remaining_cat,
            "upcoming_total": cat_upcoming_total,
            "safe_today": (remaining_cat - cat_upcoming_total) / days_remaining,
        }
        budgeted_spend_total += spent

    unbudgeted_spend = all_spend - budgeted_spend_total
    cap = period.get("overall_amount")

    if cap is not None:
        overall_remaining = cap - all_spend
        overall_safe_today = (overall_remaining - upcoming_total) / days_remaining
    else:
        overall_remaining = sum(c["remaining"] for c in per_category.values())
        budgeted_upcoming_total = sum(c["upcoming_total"] for c in per_category.values())
        overall_safe_today = (overall_remaining - budgeted_upcoming_total) / days_remaining

    return {
        "mode": "per_category",
        "has_cap": cap is not None,
        "cap": cap,
        "spend": all_spend,
        "overall_remaining": overall_remaining,
        "overall_safe_today": overall_safe_today,
        "per_category": per_category,
        "unbudgeted_spend": unbudgeted_spend,
        "upcoming": upcoming,
        "days_remaining": days_remaining,
    }
