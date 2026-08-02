import calendar
from datetime import date, datetime, timedelta, timezone

from services.supabase_client import get_categories, get_client


def month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def next_month_start(d: date) -> date:
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def month_end(d: date) -> date:
    last_day = calendar.monthrange(d.year, d.month)[1]
    return date(d.year, d.month, last_day)


def is_three_days_before_month_end(today: date) -> bool:
    return today == month_end(today) - timedelta(days=3)


def is_first_of_month(today: date) -> bool:
    return today.day == 1


def get_budget_period(effective_month: date) -> dict | None:
    res = (
        get_client()
        .table("budget_periods")
        .select("*, budget_category_amounts(category_id, amount)")
        .eq("effective_month", effective_month.isoformat())
        .execute()
    )
    return res.data[0] if res.data else None


def get_latest_confirmed_period() -> dict | None:
    res = (
        get_client()
        .table("budget_periods")
        .select("*, budget_category_amounts(category_id, amount)")
        .eq("status", "confirmed")
        .order("effective_month", desc=True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def save_budget_period(
    *,
    effective_month: date,
    mode: str,
    overall_amount: float | None,
    category_amounts: dict[int, float] | None,
    status: str,
) -> dict:
    existing = get_budget_period(effective_month)
    client = get_client()
    decided_at = datetime.now(timezone.utc).isoformat() if status == "confirmed" else None

    payload = {
        "mode": mode,
        "overall_amount": overall_amount,
        "status": status,
        "decided_at": decided_at,
    }

    if existing:
        client.table("budget_category_amounts").delete().eq("budget_period_id", existing["id"]).execute()
        period = client.table("budget_periods").update(payload).eq("id", existing["id"]).execute().data[0]
    else:
        payload["effective_month"] = effective_month.isoformat()
        period = client.table("budget_periods").insert(payload).execute().data[0]

    if category_amounts:
        rows = [
            {"budget_period_id": period["id"], "category_id": cat_id, "amount": amt}
            for cat_id, amt in category_amounts.items()
        ]
        client.table("budget_category_amounts").insert(rows).execute()

    return period


def carry_over_period(from_period: dict, to_month: date, status: str) -> dict:
    category_amounts = None
    if from_period["mode"] == "per_category" and from_period.get("budget_category_amounts"):
        category_amounts = {row["category_id"]: row["amount"] for row in from_period["budget_category_amounts"]}

    return save_budget_period(
        effective_month=to_month,
        mode=from_period["mode"],
        overall_amount=from_period.get("overall_amount"),
        category_amounts=category_amounts,
        status=status,
    )


def validate_category_sum(cap: float | None, category_amounts: dict) -> bool:
    if cap is None:
        return True
    return sum(category_amounts.values()) <= cap


def describe_period(period: dict) -> str:
    if period["mode"] == "none":
        return "No budget"
    if period["mode"] == "overall":
        return f"Overall: ${period['overall_amount']}/month"

    cat_names = {c["id"]: c["name"] for c in get_categories()}
    lines = [f"  {cat_names.get(row['category_id'], '?')}: ${row['amount']}" for row in period.get("budget_category_amounts", [])]
    header = f"Per-category (cap: ${period['overall_amount']})" if period.get("overall_amount") else "Per-category (no overall cap)"
    return header + "\n" + "\n".join(lines)
