import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.amounts import parse_amount
from bot.config import TELEGRAM_ALLOWED_USER_ID
from bot.dates import format_date, parse_date
from services.openai_client import extract_correction, extract_field_value
from services.supabase_client import (
    find_recent_transaction_by_merchant,
    get_categories,
    get_payment_methods,
    update_transaction_field,
)

logger = logging.getLogger(__name__)

FIELD_COLUMN = {
    "merchant": "merchant",
    "amount": "amount",
    "date": "transaction_date",
    "category": "category_id",
    "payment_method": "payment_method_id",
}


def _is_authorized(update: Update) -> bool:
    if not TELEGRAM_ALLOWED_USER_ID:
        return True
    return str(update.effective_user.id) == str(TELEGRAM_ALLOWED_USER_ID)


def _current_value_text(txn: dict, field: str) -> str:
    if field == "merchant":
        return txn["merchant"]
    if field == "amount":
        return f"{txn['amount']} {txn['currency']}"
    if field == "date":
        return format_date(txn["transaction_date"])
    if field == "category":
        return txn["categories"]["name"] if txn["categories"] else "?"
    if field == "payment_method":
        return txn["payment_methods"]["label"] if txn["payment_methods"] else "?"
    return "?"


async def handle_correction_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    try:
        parsed = extract_correction(text)
    except Exception:
        logger.exception("Correction extraction failed")
        await update.message.reply_text("Sorry, I couldn't work out what to correct.")
        return

    txn = find_recent_transaction_by_merchant(parsed["merchant_hint"])
    if not txn:
        await update.message.reply_text(f"I couldn't find a recent transaction matching {parsed['merchant_hint']!r}.")
        return

    field = parsed["field"]
    today = update.message.date.date()

    if field == "category":
        try:
            new_name = extract_field_value("category", parsed["new_value"], today)
        except Exception:
            await update.message.reply_text("Sorry, I couldn't work out the new category.")
            return
        categories = {c["name"]: c["id"] for c in get_categories()}
        cat_id = categories.get(new_name)
        if cat_id is None:
            await update.message.reply_text(f"Sorry, {new_name!r} isn't a known category.")
            return
        display_value, db_value = new_name, cat_id

    elif field == "amount":
        amount = parse_amount(parsed["new_value"], today)
        if amount is None:
            await update.message.reply_text("Sorry, I couldn't read that as a new amount.")
            return
        display_value, db_value = str(amount), amount

    elif field == "date":
        parsed_date = parse_date(parsed["new_value"], today)
        if parsed_date is None:
            await update.message.reply_text("Sorry, I couldn't read that as a new date.")
            return
        display_value, db_value = format_date(parsed_date), parsed_date.isoformat()

    elif field == "merchant":
        display_value = db_value = parsed["new_value"]

    elif field == "payment_method":
        methods = get_payment_methods()
        match = next((m for m in methods if parsed["new_value"].lower() in m["label"].lower()), None)
        if not match:
            await update.message.reply_text(f"I don't recognize a payment method matching {parsed['new_value']!r}.")
            return
        display_value, db_value = match["label"], match["id"]

    else:
        await update.message.reply_text("Sorry, I couldn't work out what to correct.")
        return

    context.user_data["correction_flow"] = {
        "transaction_id": txn["id"],
        "field": field,
        "db_value": db_value,
        "display_value": display_value,
    }

    current = _current_value_text(txn, field)
    await update.message.reply_text(
        f"Found: {txn['merchant']} — {txn['amount']} {txn['currency']} on {format_date(txn['transaction_date'])}\n\n"
        f"Change {field} from {current!r} to {display_value!r}?",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Confirm", callback_data="correction:confirm")],
                [InlineKeyboardButton("Cancel", callback_data="correction:cancel")],
            ]
        ),
    )


async def handle_correction_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not _is_authorized(update):
        return

    flow = context.user_data.get("correction_flow")
    if not flow:
        await query.edit_message_text("This has expired.")
        return

    action = query.data.split(":", 1)[1]

    if action == "cancel":
        context.user_data.pop("correction_flow", None)
        await query.edit_message_text("Cancelled — nothing changed.")
        return

    try:
        update_transaction_field(flow["transaction_id"], FIELD_COLUMN[flow["field"]], flow["db_value"])
    except Exception:
        logger.exception("Failed to apply correction")
        await query.edit_message_text("Sorry, I couldn't apply that change. Try again?")
        return

    context.user_data.pop("correction_flow", None)
    await query.edit_message_text(f"Updated — {flow['field']} is now {flow['display_value']!r}.")
