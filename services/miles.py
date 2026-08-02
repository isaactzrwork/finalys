import calendar
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta

from services.supabase_client import get_client


def _quarter_end(d: date) -> date:
    quarter = (d.month - 1) // 3
    end_month = quarter * 3 + 3
    last_day = calendar.monthrange(d.year, end_month)[1]
    return date(d.year, end_month, last_day)


def statement_period_from_date(statement_date: date) -> tuple[date, date]:
    """Approximate the billing cycle as the month leading up to (but not including) the
    statement date — e.g. a statement dated 13 Jul covers 13 Jun to 12 Jul, since the next
    cycle starts fresh on the statement date itself, not the day after.
    """
    return statement_date - relativedelta(months=1), statement_date - timedelta(days=1)


def compute_expiry_date(rule_type: str | None, rule_params: dict | None, period_end: date) -> date | None:
    """Estimate when miles earned in a statement cycle expire, given the card's rule.

    period_end (the statement cycle's end date) stands in for "when the miles were earned" —
    precise enough for an expiry estimate, which this always is: redemptions made outside this
    system aren't visible here, so this can't reflect a true remaining balance.
    """
    if not rule_type or rule_type == "no_expiry":
        return None

    rule_params = rule_params or {}

    if rule_type == "rolling_quarterly":
        years = rule_params.get("years_after_quarter_end", 1)
        return _quarter_end(period_end) + relativedelta(years=years)

    if rule_type == "rolling_monthly":
        start_offset = rule_params.get("start_offset_months", 1)
        duration = rule_params.get("duration_months", 37)
        earn_month_first = date(period_end.year, period_end.month, 1)
        cycle_start = earn_month_first + relativedelta(months=start_offset)
        return cycle_start + relativedelta(months=duration)

    if rule_type == "account_cycle":
        cycle_months = rule_params["cycle_months"]
        grace_months = rule_params.get("grace_months", 0)
        anchor = date.fromisoformat(rule_params["anchor_date"])

        cycle_start = anchor
        while cycle_start + relativedelta(months=cycle_months) <= period_end:
            cycle_start += relativedelta(months=cycle_months)

        cycle_end = cycle_start + relativedelta(months=cycle_months)
        return cycle_end + relativedelta(months=grace_months)

    raise ValueError(f"Unknown expiry_rule_type: {rule_type!r}")


def get_miles_timeline(payment_method_id: int) -> list[dict]:
    """All ledger entries for a card, each tagged with its estimated expiry date, oldest first."""
    pm_res = (
        get_client()
        .table("payment_methods")
        .select("id, label, miles_program, expiry_rule_type, expiry_rule_params")
        .eq("id", payment_method_id)
        .single()
        .execute()
    )
    pm = pm_res.data

    ledger_res = (
        get_client()
        .table("miles_ledger")
        .select("id, statement_period_start, statement_period_end, miles_earned")
        .eq("payment_method_id", payment_method_id)
        .order("statement_period_end")
        .execute()
    )

    timeline = []
    for row in ledger_res.data:
        period_end = date.fromisoformat(row["statement_period_end"])
        expiry = compute_expiry_date(pm["expiry_rule_type"], pm["expiry_rule_params"], period_end)
        timeline.append({**row, "expiry_date": expiry.isoformat() if expiry else None})

    return timeline


def get_expiring_soon(within_days: int = 60) -> list[dict]:
    """Ledger entries across all cards whose estimated expiry falls within the next N days."""
    today = date.today()
    cutoff = today + timedelta(days=within_days)

    res = (
        get_client()
        .table("payment_methods")
        .select("id, label, miles_program, expiry_rule_type, expiry_rule_params")
        .not_.is_("miles_program", "null")
        .execute()
    )

    expiring = []
    for pm in res.data:
        for entry in get_miles_timeline(pm["id"]):
            if not entry["expiry_date"]:
                continue
            expiry = date.fromisoformat(entry["expiry_date"])
            if today <= expiry <= cutoff:
                expiring.append({**entry, "label": pm["label"], "miles_program": pm["miles_program"]})
    return expiring


def get_stale_expiry_rules(threshold_months: int = 6) -> list[dict]:
    """Cards whose expiry rule hasn't been re-verified against the bank's T&Cs recently."""
    cutoff = date.today() - relativedelta(months=threshold_months)

    res = (
        get_client()
        .table("payment_methods")
        .select("id, label, miles_program, expiry_rule_verified_at")
        .not_.is_("miles_program", "null")
        .execute()
    )

    stale = []
    for pm in res.data:
        verified_at = pm.get("expiry_rule_verified_at")
        if not verified_at or date.fromisoformat(verified_at) < cutoff:
            stale.append(pm)
    return stale
