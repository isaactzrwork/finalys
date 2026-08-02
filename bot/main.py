import logging
from datetime import time
from zoneinfo import ZoneInfo

from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from bot.budget_handlers import cmd_budget, handle_budget_callback, run_daily_budget_check
from bot.config import TELEGRAM_ALLOWED_USER_ID, TELEGRAM_BOT_TOKEN
from bot.correction_handlers import handle_correction_callback
from bot.handlers import (
    handle_document,
    handle_edit_field_choice,
    handle_payment_method_choice,
    handle_photo,
    handle_review_choice,
    handle_statement_payment_method_choice,
    handle_text,
    handle_unsupported,
    handle_voice,
)
from bot.insights_handlers import cmd_insights, run_weekly_insights
from bot.recurring_handlers import cmd_recurring_expense, handle_recurring_callback
from bot.safe_to_spend_handlers import cmd_safe_to_spend, handle_safe_to_spend_callback, send_daily_safe_to_spend

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)

SGT = ZoneInfo("Asia/Singapore")


async def run_daily_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unified daily job: the budget-period proactive check (fires only near month
    boundaries) followed by the safe-to-spend nudge (fires every day a budget is set)."""
    if not TELEGRAM_ALLOWED_USER_ID:
        return

    chat_id = int(TELEGRAM_ALLOWED_USER_ID)

    async def send(text, reply_markup=None):
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)

    await run_daily_budget_check(context)
    await send_daily_safe_to_spend(send)


def build_app() -> Application:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set — fill it in .env first.")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("budget", cmd_budget))
    app.add_handler(CommandHandler("recurringexpense", cmd_recurring_expense))
    app.add_handler(CommandHandler("safetospend", cmd_safe_to_spend))
    app.add_handler(CommandHandler("insights", cmd_insights))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(~filters.COMMAND, handle_unsupported))
    app.add_handler(CallbackQueryHandler(handle_review_choice, pattern=r"^review:(edit|ok)$"))
    app.add_handler(CallbackQueryHandler(handle_edit_field_choice, pattern=r"^editfield:(merchant|amount|date|category)$"))
    app.add_handler(CallbackQueryHandler(handle_payment_method_choice, pattern=r"^pm:\d+$"))
    app.add_handler(CallbackQueryHandler(handle_statement_payment_method_choice, pattern=r"^stmt_pm:\d+$"))
    app.add_handler(CallbackQueryHandler(handle_budget_callback, pattern=r"^budget:"))
    app.add_handler(CallbackQueryHandler(handle_recurring_callback, pattern=r"^recurring:"))
    app.add_handler(CallbackQueryHandler(handle_safe_to_spend_callback, pattern=r"^sts:"))
    app.add_handler(CallbackQueryHandler(handle_correction_callback, pattern=r"^correction:"))

    if TELEGRAM_ALLOWED_USER_ID:
        app.job_queue.run_daily(
            run_daily_check,
            time=time(hour=8, tzinfo=SGT),
            chat_id=int(TELEGRAM_ALLOWED_USER_ID),
            user_id=int(TELEGRAM_ALLOWED_USER_ID),
            name="daily_check",
        )
        app.job_queue.run_daily(
            run_weekly_insights,
            time=time(hour=8, tzinfo=SGT),
            days=(0,),  # Monday
            chat_id=int(TELEGRAM_ALLOWED_USER_ID),
            user_id=int(TELEGRAM_ALLOWED_USER_ID),
            name="weekly_insights",
        )

    return app


def main() -> None:
    app = build_app()
    print("Bot is starting... send it a photo, voice note, or text on Telegram (Ctrl+C to stop).")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
