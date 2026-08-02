import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import ContextTypes

from bot.config import TELEGRAM_ALLOWED_USER_ID
from bot.dates import format_date
from services.insights import get_budget_alerts, get_weekly_spending_summary
from services.miles import get_expiring_soon, get_stale_expiry_rules

logger = logging.getLogger(__name__)

SGT = ZoneInfo("Asia/Singapore")


def _is_authorized(update: Update) -> bool:
    if not TELEGRAM_ALLOWED_USER_ID:
        return True
    return str(update.effective_user.id) == str(TELEGRAM_ALLOWED_USER_ID)


def _build_insights_message(today: date) -> str:
    sections = []

    summary = get_weekly_spending_summary(today)
    lines = [f"📊 Spending insights ({format_date(summary['week_start'])} – {format_date(summary['week_end'])})"]
    lines.append(f"Total spent this week: ${summary['total']:.2f}")
    if summary["biggest_category"]:
        lines.append(f"Biggest category: {summary['biggest_category']['name']} (${summary['biggest_category']['amount']:.2f})")
    sections.append("\n".join(lines))

    alerts = get_budget_alerts(today)
    if alerts:
        sections.append("⚠️ Budget alerts:\n" + "\n".join(f"- {a}" for a in alerts))

    expiring = get_expiring_soon(within_days=60)
    if expiring:
        lines = ["✈️ Miles expiring within 60 days:"]
        for e in expiring:
            lines.append(f"- {e['label']}: {e['miles_earned']} {e['miles_program']} expiring {format_date(e['expiry_date'])}")
        sections.append("\n".join(lines))

    stale = get_stale_expiry_rules(threshold_months=6)
    if stale:
        lines = ["🔍 Worth double-checking (expiry rule not re-verified in 6+ months):"]
        for s in stale:
            lines.append(f"- {s['label']} ({s['miles_program']})")
        sections.append("\n".join(lines))

    return "\n\n".join(sections)


async def cmd_insights(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    await update.message.reply_text(_build_insights_message(datetime.now(SGT).date()))


async def run_weekly_insights(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not TELEGRAM_ALLOWED_USER_ID:
        return
    message = _build_insights_message(datetime.now(SGT).date())
    await context.bot.send_message(chat_id=int(TELEGRAM_ALLOWED_USER_ID), text=message)
