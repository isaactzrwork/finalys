from datetime import date, timedelta

from services.safe_to_spend import compute_safe_to_spend
from services.supabase_client import get_categories, get_client

BUDGET_ALERT_THRESHOLDS = (0.5, 0.7, 0.9)


def _highest_threshold_crossed(pct: float) -> float | None:
    crossed = [t for t in BUDGET_ALERT_THRESHOLDS if pct >= t]
    return max(crossed) if crossed else None


def get_budget_alerts(today: date) -> list[str]:
    result = compute_safe_to_spend(today)
    alerts = []

    if result["mode"] == "none":
        return alerts

    if result["mode"] == "overall":
        pct = result["spend"] / result["budget"] if result["budget"] else 0
        threshold = _highest_threshold_crossed(pct)
        if threshold:
            alerts.append(
                f"You've used {pct * 100:.0f}% of your ${result['budget']:.2f} monthly budget "
                f"(crossed the {threshold * 100:.0f}% mark) — ${result['spend']:.2f} spent."
            )
        return alerts

    for cat in result["per_category"].values():
        if cat["budget"] <= 0:
            continue
        pct = cat["spent"] / cat["budget"]
        threshold = _highest_threshold_crossed(pct)
        if threshold:
            alerts.append(
                f"{cat['name']}: {pct * 100:.0f}% of ${cat['budget']:.2f} budget used "
                f"(crossed the {threshold * 100:.0f}% mark) — ${cat['spent']:.2f} spent."
            )

    if result["has_cap"]:
        pct = result["spend"] / result["cap"] if result["cap"] else 0
        threshold = _highest_threshold_crossed(pct)
        if threshold:
            alerts.append(
                f"Overall cap: {pct * 100:.0f}% of ${result['cap']:.2f} used "
                f"(crossed the {threshold * 100:.0f}% mark) — ${result['spend']:.2f} spent."
            )

    return alerts


def get_weekly_spending_summary(today: date) -> dict:
    week_start = today - timedelta(days=6)
    res = (
        get_client()
        .table("transactions")
        .select("amount, category_id")
        .gte("transaction_date", week_start.isoformat())
        .lte("transaction_date", today.isoformat())
        .execute()
    )

    total = sum(r["amount"] for r in res.data)
    by_category: dict[int, float] = {}
    for r in res.data:
        by_category[r["category_id"]] = by_category.get(r["category_id"], 0) + r["amount"]

    cat_names = {c["id"]: c["name"] for c in get_categories()}
    biggest = None
    if by_category:
        biggest_id = max(by_category, key=by_category.get)
        biggest = {"name": cat_names.get(biggest_id, "?"), "amount": by_category[biggest_id]}

    return {"total": total, "biggest_category": biggest, "week_start": week_start, "week_end": today}
