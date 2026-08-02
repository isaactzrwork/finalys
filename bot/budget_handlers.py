import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.amounts import parse_amount
from bot.config import TELEGRAM_ALLOWED_USER_ID
from services.budgets import (
    carry_over_period,
    describe_period,
    get_budget_period,
    get_latest_confirmed_period,
    is_first_of_month,
    is_three_days_before_month_end,
    month_start,
    next_month_start,
    save_budget_period,
    validate_category_sum,
)
from services.supabase_client import get_categories

logger = logging.getLogger(__name__)

SGT = ZoneInfo("Asia/Singapore")


def _is_authorized(update: Update) -> bool:
    if not TELEGRAM_ALLOWED_USER_ID:
        return True
    return str(update.effective_user.id) == str(TELEGRAM_ALLOWED_USER_ID)


async def _guard(update: Update) -> bool:
    if not _is_authorized(update):
        return False
    return True


def _carryover_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Carry over unchanged", callback_data="budget:carryover:yes")],
            [InlineKeyboardButton("Redefine", callback_data="budget:carryover:no")],
        ]
    )


def _mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Per-category budgets", callback_data="budget:mode:per_category")],
            [InlineKeyboardButton("One overall budget", callback_data="budget:mode:overall")],
            [InlineKeyboardButton("No budget", callback_data="budget:mode:none")],
        ]
    )


async def _ask_mode(send, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["budget_flow"]["step"] = "mode_choice"
    await send("How do you want to budget?", reply_markup=_mode_keyboard())


async def start_budget_flow(send, context: ContextTypes.DEFAULT_TYPE, target_month: date, offer_carryover: bool) -> None:
    prior = get_latest_confirmed_period()
    context.user_data["budget_flow"] = {
        "target_month": target_month.isoformat(),
        "prior_period": prior,
    }

    if offer_carryover and prior:
        context.user_data["budget_flow"]["step"] = "carryover_choice"
        summary = describe_period(prior)
        await send(
            f"Time to set your budget for {target_month.strftime('%B %Y')}.\n\n"
            f"Your last confirmed budget was:\n{summary}\n\n"
            "Carry it over unchanged, or redefine it?",
            reply_markup=_carryover_keyboard(),
        )
        return

    await _ask_mode(send, context)


# ---------------------------------------------------------------------------
# On-demand entry point
# ---------------------------------------------------------------------------


async def cmd_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return

    target_month = month_start(datetime.now(SGT).date())
    existing = get_budget_period(target_month)

    if existing and existing["status"] == "confirmed":
        await update.message.reply_text(
            f"Current budget for {target_month.strftime('%B %Y')}:\n{describe_period(existing)}\n\nLet's redefine it."
        )
        context.user_data["budget_flow"] = {"target_month": target_month.isoformat(), "prior_period": None}
        await _ask_mode(update.message.reply_text, context)
        return

    await start_budget_flow(update.message.reply_text, context, target_month, offer_carryover=True)


# ---------------------------------------------------------------------------
# Text input during an active budget flow — call from the main text router;
# returns True if the message was consumed, False if it should fall through
# to normal transaction parsing.
# ---------------------------------------------------------------------------


async def handle_budget_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    flow = context.user_data.get("budget_flow")
    if not flow:
        return False

    step = flow.get("step")
    text = update.message.text.strip()
    today = update.message.date.date()

    if step == "overall_amount":
        amount = parse_amount(text, today)
        if amount is None:
            await update.message.reply_text("Sorry, I couldn't read that as a number. Try again?")
            return True
        target_month = date.fromisoformat(flow["target_month"])
        save_budget_period(
            effective_month=target_month, mode="overall", overall_amount=amount, category_amounts=None, status="confirmed"
        )
        context.user_data.pop("budget_flow", None)
        await update.message.reply_text(f"Saved — ${amount}/month overall for {target_month.strftime('%B %Y')}.")
        return True

    if step == "category_amount":
        categories = flow["categories"]
        idx = flow["cat_index"]
        current = categories[idx]

        if text.lower() != "skip":
            amount = parse_amount(text, today)
            if amount is None:
                await update.message.reply_text("Sorry, I couldn't read that as a number. Send a number, or 'skip'.")
                return True
            flow["amounts"][current["id"]] = amount

        idx += 1
        if idx < len(categories):
            flow["cat_index"] = idx
            nxt = categories[idx]
            await update.message.reply_text(f"Budget for {nxt['name']}? Send a number, or 'skip'.")
            return True

        flow["step"] = "cap_amount"
        total = sum(flow["amounts"].values())
        await update.message.reply_text(
            f"Categories set — total ${total}. Optional: set an overall monthly cap? Send a number, or 'skip' for no cap."
        )
        return True

    if step == "cap_amount":
        target_month = date.fromisoformat(flow["target_month"])
        cap = None
        if text.lower() != "skip":
            cap = parse_amount(text, today)
            if cap is None:
                await update.message.reply_text("Sorry, I couldn't read that as a number. Send a number, or 'skip'.")
                return True

        if cap is not None and not validate_category_sum(cap, flow["amounts"]):
            total = sum(flow["amounts"].values())
            await update.message.reply_text(
                f"Your categories add up to ${total}, which is more than the ${cap} cap. "
                "Send a higher cap, or 'skip' to remove the cap."
            )
            return True

        save_budget_period(
            effective_month=target_month,
            mode="per_category",
            overall_amount=cap,
            category_amounts=flow["amounts"],
            status="confirmed",
        )
        context.user_data.pop("budget_flow", None)
        cap_text = f" (cap ${cap})" if cap else ""
        await update.message.reply_text(f"Saved per-category budget for {target_month.strftime('%B %Y')}{cap_text}.")
        return True

    return False


# ---------------------------------------------------------------------------
# Button callbacks
# ---------------------------------------------------------------------------


async def handle_budget_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not _is_authorized(update):
        return

    flow = context.user_data.get("budget_flow")
    if not flow:
        await query.edit_message_text("This budget flow has expired — send /budget to start again.")
        return

    parts = query.data.split(":")
    action = parts[1]

    if action == "carryover":
        choice = parts[2]
        target_month = date.fromisoformat(flow["target_month"])

        if choice == "yes":
            prior = flow["prior_period"]
            carry_over_period(prior, target_month, status="confirmed")
            context.user_data.pop("budget_flow", None)
            await query.edit_message_text(f"Carried over unchanged for {target_month.strftime('%B %Y')}.")
            return

        await query.edit_message_text("Let's redefine it.")
        await _ask_mode(query.message.reply_text, context)
        return

    if action == "mode":
        mode = parts[2]
        target_month = date.fromisoformat(flow["target_month"])

        if mode == "none":
            save_budget_period(
                effective_month=target_month, mode="none", overall_amount=None, category_amounts=None, status="confirmed"
            )
            context.user_data.pop("budget_flow", None)
            await query.edit_message_text(f"Got it — no budget for {target_month.strftime('%B %Y')}.")
            return

        if mode == "overall":
            flow["step"] = "overall_amount"
            await query.edit_message_text("What's your overall monthly budget? Send a number.")
            return

        if mode == "per_category":
            categories = get_categories()
            flow["categories"] = categories
            flow["cat_index"] = 0
            flow["amounts"] = {}
            flow["step"] = "category_amount"
            first = categories[0]
            await query.edit_message_text(f"Budget for {first['name']}? Send a number, or 'skip' to leave unbudgeted.")
            return


# ---------------------------------------------------------------------------
# Daily proactive check
# ---------------------------------------------------------------------------


async def run_daily_budget_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not TELEGRAM_ALLOWED_USER_ID:
        return

    chat_id = int(TELEGRAM_ALLOWED_USER_ID)
    today = datetime.now(SGT).date()

    async def send(text, reply_markup=None):
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)

    if not (is_three_days_before_month_end(today) or is_first_of_month(today)):
        return

    target_month = next_month_start(today) if is_three_days_before_month_end(today) else month_start(today)
    existing = get_budget_period(target_month)

    if existing and existing["status"] == "confirmed":
        return

    prior = get_latest_confirmed_period()

    if not existing and prior:
        carry_over_period(prior, target_month, status="pending_confirmation")

    await start_budget_flow(send, context, target_month, offer_carryover=bool(prior))
