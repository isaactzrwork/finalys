import logging
from datetime import date

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.amounts import parse_amount
from bot.config import TELEGRAM_ALLOWED_USER_ID
from bot.dates import format_date, parse_date
from services.recurring import is_recurring_candidate, save_recurring_decision

logger = logging.getLogger(__name__)

FREQUENCY_LABELS = {
    "monthly": "Monthly",
    "quarterly": "Quarterly",
    "semi_annual": "Every 6 months",
    "yearly": "Yearly",
}


def _is_authorized(update: Update) -> bool:
    if not TELEGRAM_ALLOWED_USER_ID:
        return True
    return str(update.effective_user.id) == str(TELEGRAM_ALLOWED_USER_ID)


def _frequency_keyboard() -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(label, callback_data=f"recurring:freq:{key}")] for key, label in FREQUENCY_LABELS.items()]
    buttons.append([InlineKeyboardButton("Other", callback_data="recurring:freq:other")])
    return InlineKeyboardMarkup(buttons)


async def maybe_prompt_recurring(
    send, context: ContextTypes.DEFAULT_TYPE, *, merchant: str, amount: float, category_id: int | None, transaction_date: str
) -> None:
    """Call after a transaction saves. Sends a confirmation prompt if this merchant+amount
    looks like a new recurring pattern (2+ similar-amount occurrences in the last ~3 months)
    that isn't already known as confirmed or declined.
    """
    as_of = date.fromisoformat(transaction_date)
    if not is_recurring_candidate(merchant, amount, as_of):
        return

    context.user_data["recurring_flow"] = {
        "merchant": merchant,
        "amount": amount,
        "category_id": category_id,
        "last_seen_date": transaction_date,
        "step": "confirm",
    }
    await send(
        f"This looks like it might be a recurring charge — {merchant} for ~${amount}. Is this a recurring expense?",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Yes", callback_data="recurring:confirm:yes")],
                [InlineKeyboardButton("No", callback_data="recurring:confirm:no")],
            ]
        ),
    )


async def handle_recurring_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not _is_authorized(update):
        return

    flow = context.user_data.get("recurring_flow")
    if not flow:
        await query.edit_message_text("This has expired.")
        return

    parts = query.data.split(":")
    action = parts[1]

    if action == "confirm":
        choice = parts[2]
        if choice == "no":
            save_recurring_decision(
                merchant=flow["merchant"],
                amount=flow["amount"],
                status="declined",
                frequency=None,
                category_id=flow.get("category_id"),
                last_seen_date=date.fromisoformat(flow["last_seen_date"]),
            )
            context.user_data.pop("recurring_flow", None)
            await query.edit_message_text("Got it — won't ask about this one again.")
            return

        flow["step"] = "frequency"
        await query.edit_message_text("How often does this recur?", reply_markup=_frequency_keyboard())
        return

    if action == "freq":
        frequency = parts[2]

        if frequency == "other":
            flow["step"] = "custom_frequency"
            await query.edit_message_text("How often does this recur? Describe it (e.g. 'every 2 months').")
            return

        save_recurring_decision(
            merchant=flow["merchant"],
            amount=flow["amount"],
            status="confirmed",
            frequency=frequency,
            category_id=flow.get("category_id"),
            last_seen_date=date.fromisoformat(flow["last_seen_date"]),
        )
        context.user_data.pop("recurring_flow", None)
        await query.edit_message_text(
            f"Saved — {flow['merchant']} tracked as a {FREQUENCY_LABELS[frequency].lower()} recurring charge."
        )
        return


# ---------------------------------------------------------------------------
# Manual /recurringexpense command
# ---------------------------------------------------------------------------


async def cmd_recurring_expense(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    context.user_data["recurring_flow"] = {"step": "manual_merchant", "category_id": None}
    await update.message.reply_text("What's the merchant name for this recurring expense?")


async def handle_recurring_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if the message was consumed by an active recurring-expense flow."""
    flow = context.user_data.get("recurring_flow")
    if not flow:
        return False

    step = flow.get("step")
    text = update.message.text.strip()

    if step == "manual_merchant":
        flow["merchant"] = text
        flow["step"] = "manual_amount"
        await update.message.reply_text("What's the amount?")
        return True

    if step == "manual_amount":
        amount = parse_amount(text, update.message.date.date())
        if amount is None:
            await update.message.reply_text("Sorry, I couldn't read that as a number. Try again?")
            return True
        flow["amount"] = amount
        flow["step"] = "manual_date"
        await update.message.reply_text(
            "What date does/did this charge fall on? (DD/MM/YYYY, or any date — "
            "used to project when it'll next occur)"
        )
        return True

    if step == "manual_date":
        parsed = parse_date(text, update.message.date.date())
        if parsed is None:
            await update.message.reply_text("Sorry, I couldn't read that as a date. Try again? (e.g. 15/08/2026)")
            return True
        flow["last_seen_date"] = parsed.isoformat()
        flow["step"] = "manual_frequency"
        await update.message.reply_text(
            f"Got it — {format_date(parsed)}. How often does this recur?", reply_markup=_frequency_keyboard()
        )
        return True

    if step == "custom_frequency":
        save_recurring_decision(
            merchant=flow["merchant"],
            amount=flow["amount"],
            status="confirmed",
            frequency="custom",
            custom_frequency_note=text,
            category_id=flow.get("category_id"),
            last_seen_date=date.fromisoformat(flow["last_seen_date"]),
        )
        context.user_data.pop("recurring_flow", None)
        await update.message.reply_text(f"Saved — {flow['merchant']} tracked as recurring ({text}).")
        return True

    return False
