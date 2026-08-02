import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.budget_handlers import start_budget_flow
from bot.config import TELEGRAM_ALLOWED_USER_ID
from services.budgets import month_start
from services.safe_to_spend import compute_safe_to_spend

logger = logging.getLogger(__name__)

SGT = ZoneInfo("Asia/Singapore")


def _is_authorized(update: Update) -> bool:
    if not TELEGRAM_ALLOWED_USER_ID:
        return True
    return str(update.effective_user.id) == str(TELEGRAM_ALLOWED_USER_ID)


def _format_overall(result: dict) -> str:
    upcoming_names = ", ".join(u["merchant"] for u in result["upcoming"]) or "none"
    return (
        f"Safe to spend today: ${result['safe_today']:.2f}\n\n"
        "Based on:\n"
        f"- ${result['remaining']:.2f} remaining this month (of ${result['budget']:.2f} budget)\n"
        f"- ${result['upcoming_total']:.2f} in upcoming recurring charges ({upcoming_names})\n"
        f"- {result['days_remaining']} days left in the month"
    )


def _format_per_category(result: dict) -> str:
    upcoming_total = sum(u["reference_amount"] for u in result["upcoming"])
    upcoming_names = ", ".join(u["merchant"] for u in result["upcoming"]) or "none"
    cap_note = f" (capped at ${result['cap']:.2f})" if result["has_cap"] else ""
    lines = [
        f"Safe to spend today (overall): ${result['overall_safe_today']:.2f}\n",
        "Based on:",
        f"- ${result['overall_remaining']:.2f} remaining across your budgeted categories{cap_note}",
        f"- ${upcoming_total:.2f} in upcoming recurring charges ({upcoming_names})",
        f"- {result['days_remaining']} days left in the month",
        "",
        "Per-category budget:",
    ]
    for cat in result["per_category"].values():
        lines.append(f"- {cat['name']}: ${cat['remaining']:.2f} / ${cat['budget']:.2f} left")

    if result["unbudgeted_spend"] > 0:
        lines.append(
            f"\n⚠️ You've also spent ${result['unbudgeted_spend']:.2f} this month in categories with "
            "no budget set — not included above."
        )
    return "\n".join(lines)


async def _send_safe_to_spend(send, context: ContextTypes.DEFAULT_TYPE) -> None:
    today = datetime.now(SGT).date()
    result = compute_safe_to_spend(today)

    if result["mode"] == "none":
        await send(
            "You haven't set a budget for this month, so I can't calculate a safe-to-spend number.\n\nWant to set one now?",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Set budget", callback_data="sts:setbudget")]]),
        )
        return

    if result["mode"] == "overall":
        await send(_format_overall(result))
        return

    await send(_format_per_category(result))


async def cmd_safe_to_spend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    await _send_safe_to_spend(update.message.reply_text, context)


async def handle_safe_to_spend_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not _is_authorized(update):
        return

    action = query.data.split(":", 1)[1]

    if action == "setbudget":
        target_month = month_start(datetime.now(SGT).date())
        await start_budget_flow(query.message.reply_text, context, target_month, offer_carryover=True)
        return


async def send_daily_safe_to_spend(send) -> None:
    """Sends the safe-to-spend nudge if a budget is set. Called as part of the unified daily
    check in bot/main.py, not registered as its own job."""
    result = compute_safe_to_spend(datetime.now(SGT).date())
    if result["mode"] == "none":
        return  # respect an explicit "no budget" choice rather than nagging daily

    if result["mode"] == "overall":
        await send(_format_overall(result))
    else:
        await send(_format_per_category(result))
