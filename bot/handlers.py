import logging
import re
from datetime import date

import fitz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.budget_handlers import handle_budget_text
from bot.config import TELEGRAM_ALLOWED_USER_ID
from bot.correction_handlers import handle_correction_message
from bot.dates import format_date
from bot.recurring_handlers import handle_recurring_text, maybe_prompt_recurring
from services.miles import compute_expiry_date, statement_period_from_date
from services.openai_client import (
    answer_query,
    classify_message_intent,
    classify_photo,
    extract_field_value,
    extract_from_text,
    extract_receipt,
    extract_statement,
    transcribe_voice,
)
from services.supabase_client import (
    get_payment_method,
    get_payment_methods,
    get_payment_methods_with_miles_programs,
    insert_miles_ledger,
    insert_transaction,
    match_payment_method_by_last4,
)

logger = logging.getLogger(__name__)

EDIT_FIELDS = ("merchant", "amount", "date", "category")
ALBUM_DEBOUNCE_SECONDS = 5.0


def _is_authorized(update: Update) -> bool:
    if not TELEGRAM_ALLOWED_USER_ID:
        return True
    return str(update.effective_user.id) == str(TELEGRAM_ALLOWED_USER_ID)


async def _guard(update: Update) -> bool:
    user = update.effective_user
    logger.info("Message from user_id=%s username=%s", user.id, user.username)
    if not _is_authorized(update):
        logger.warning("Ignoring message from unauthorized user_id=%s", user.id)
        return False
    return True


async def _download_voice_bytes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bytes:
    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    return bytes(await file.download_as_bytearray())


def _rasterize_pdf(pdf_bytes: bytes) -> list[bytes]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return [page.get_pixmap(dpi=150).tobytes("png") for page in doc]
    finally:
        doc.close()


def _normalize_bank(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _group_statement_photos(photos: list[bytes], classifications: list[tuple[str, str | None]]) -> dict[str, list[bytes]]:
    """Group album photos by bank, fuzzily — e.g. 'Citi' and 'Citibank' on different pages of the
    same statement must land in the same group, or extraction for a multi-page statement runs on
    an incomplete subset of its pages (missing the rewards-summary page, wrong dates, etc.)."""
    groups: dict[str, list[bytes]] = {}
    for img, (_doc_type, bank) in zip(photos, classifications):
        norm = _normalize_bank(bank or "unknown")
        matched_key = next((key for key in groups if norm in key or key in norm), None)
        key = matched_key or norm
        groups.setdefault(key, []).append(img)
    return groups


# ---------------------------------------------------------------------------
# Transaction review/edit flow (receipts, text, voice)
# ---------------------------------------------------------------------------


def _review_text(pending: dict) -> str:
    return (
        "Here's what I have:\n"
        f"Merchant: {pending['merchant']}\n"
        f"Amount: {pending['amount']} {pending['currency']}\n"
        f"Date: {format_date(pending['date'])}\n"
        f"Category: {pending['category']}"
    )


def _review_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Edit transaction", callback_data="review:edit")],
            [InlineKeyboardButton("Looks good!", callback_data="review:ok")],
        ]
    )


def _edit_field_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"Edit {field}", callback_data=f"editfield:{field}")] for field in EDIT_FIELDS]
    )


def _payment_method_keyboard() -> InlineKeyboardMarkup:
    methods = get_payment_methods()
    return InlineKeyboardMarkup([[InlineKeyboardButton(m["label"], callback_data=f"pm:{m['id']}")] for m in methods])


async def _start_review(send, context: ContextTypes.DEFAULT_TYPE, data: dict, source_type: str, raw_input: str | None) -> None:
    context.user_data["pending_transaction"] = {
        **data,
        "source_type": source_type,
        "raw_input": raw_input,
    }
    context.user_data.pop("awaiting_edit_field", None)

    pending = context.user_data["pending_transaction"]
    await send(_review_text(pending), reply_markup=_review_keyboard())


async def _apply_field_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, field: str, text: str) -> None:
    pending = context.user_data.get("pending_transaction")
    if not pending:
        context.user_data.pop("awaiting_edit_field", None)
        await update.message.reply_text("Nothing pending to edit — send a new photo/text/voice note.")
        return

    try:
        value = extract_field_value(field, text, update.message.date.date())
    except Exception:
        logger.exception("Field edit extraction failed")
        await update.message.reply_text(f"Sorry, I couldn't parse that as a new {field}. Try again?")
        return

    pending[field] = value
    context.user_data.pop("awaiting_edit_field", None)

    await update.message.reply_text(_review_text(pending), reply_markup=_review_keyboard())


# ---------------------------------------------------------------------------
# Statement review flow (PDF, single photo, or photo album) — supports
# combined statements covering multiple cards, matched by last-4 digits.
# ---------------------------------------------------------------------------


async def _save_statement_entry(context: ContextTypes.DEFAULT_TYPE, entry: dict, payment_method_id: int) -> None:
    insert_miles_ledger(
        payment_method_id=payment_method_id,
        statement_period_start=entry["statement_period_start"],
        statement_period_end=entry["statement_period_end"],
        miles_earned=entry["miles_earned"],
        balance=entry.get("balance"),
        bank_flagged_expiring_amount=entry.get("bank_flagged_expiring_amount"),
        bank_flagged_expiry_date=entry.get("bank_flagged_expiry_date"),
        extracted_from=entry.get("extracted_from"),
    )
    pm = get_payment_method(payment_method_id)
    period_end = date.fromisoformat(entry["statement_period_end"])
    expiry = compute_expiry_date(pm["expiry_rule_type"], pm["expiry_rule_params"], period_end)
    expiry_text = format_date(expiry) if expiry else "never expires (per current rule)"

    balance_text = f"\nBalance: {entry['balance']} {pm['miles_program']}" if entry.get("balance") is not None else ""
    bank_flag_text = ""
    if entry.get("bank_flagged_expiring_amount") is not None:
        bank_flag_text = (
            f"\n⚠️ Bank flags {entry['bank_flagged_expiring_amount']} {pm['miles_program']} expiring on "
            f"{format_date(entry['bank_flagged_expiry_date'])}"
        )
    line = (
        f"{pm['label']}: {entry['miles_earned']} {pm['miles_program']} earned "
        f"({format_date(entry['statement_period_start'])} to {format_date(entry['statement_period_end'])})"
        f"{balance_text}{bank_flag_text}\n"
        f"Estimated expiry: {expiry_text}"
    )
    context.user_data.setdefault("statement_summary_lines", []).append(line)


async def _advance_statement_queue(send, context: ContextTypes.DEFAULT_TYPE) -> None:
    queue = context.user_data.get("statement_unmatched_queue", [])

    if queue:
        entry = queue.pop(0)
        context.user_data["pending_statement"] = entry
        methods = get_payment_methods_with_miles_programs()
        buttons = [[InlineKeyboardButton(m["label"], callback_data=f"stmt_pm:{m['id']}")] for m in methods]
        await send(
            f"Statement shows a card ending {entry['card_last4']} that I don't recognize — "
            f"{entry['miles_earned']} points earned. Which card is this?",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    context.user_data.pop("pending_statement", None)
    lines = context.user_data.pop("statement_summary_lines", [])
    if lines:
        await send("Statement saved:\n\n" + "\n\n".join(lines), reply_markup=None)

    context.user_data.pop("statement_unmatched_queue", None)


async def _process_statement_results(
    send, context: ContextTypes.DEFAULT_TYPE, results: list[dict], extracted_from: str
) -> None:
    """Resolve one or more statement-extraction results (each possibly multi-card) into a single
    queue-processing pass. Must be called once with ALL results from a submission, not once per
    result — otherwise a later call's queue setup would clobber an earlier one still awaiting a
    button tap from the user.
    """
    context.user_data["statement_summary_lines"] = []
    context.user_data["statement_unmatched_queue"] = []

    for data in results:
        statement_date = date.fromisoformat(data["statement_date"])
        period_start, period_end = statement_period_from_date(statement_date)

        for card in data["cards"]:
            entry = {
                "card_last4": card["card_last4"],
                "miles_earned": card["miles_earned"],
                "balance": card.get("balance"),
                "bank_flagged_expiring_amount": card.get("bank_flagged_expiring_amount"),
                "bank_flagged_expiry_date": card.get("bank_flagged_expiry_date"),
                "statement_period_start": period_start.isoformat(),
                "statement_period_end": period_end.isoformat(),
                "extracted_from": extracted_from,
            }
            match = match_payment_method_by_last4(entry["card_last4"])
            if match:
                await _save_statement_entry(context, entry, match["id"])
            else:
                context.user_data["statement_unmatched_queue"].append(entry)

    await _advance_statement_queue(send, context)


# ---------------------------------------------------------------------------
# Photo album buffering — Telegram delivers multi-photo sends as separate
# updates sharing a media_group_id, with no explicit "album complete" signal,
# so we debounce and process the batch together once new photos stop arriving.
# ---------------------------------------------------------------------------


async def _buffer_album_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE, media_group_id: str, image_bytes: bytes
) -> None:
    albums = context.chat_data.setdefault("albums", {})
    album = albums.setdefault(media_group_id, {"photos": [], "message": None, "job": None})
    album["photos"].append(image_bytes)
    album["message"] = update.message

    if album["job"] is not None:
        album["job"].schedule_removal()

    album["job"] = context.job_queue.run_once(
        _process_album_job,
        when=ALBUM_DEBOUNCE_SECONDS,
        chat_id=update.effective_chat.id,
        user_id=update.effective_user.id,
        data=media_group_id,
        name=f"album-{media_group_id}",
    )


async def _process_album_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    media_group_id = context.job.data
    albums = context.chat_data.get("albums", {})
    album = albums.pop(media_group_id, None)
    if not album:
        return

    photos = album["photos"]
    send = album["message"].reply_text

    logger.info("Album %s: received %d photos", media_group_id, len(photos))

    classifications = []
    for img in photos:
        try:
            classifications.append(classify_photo(img))
        except Exception:
            logger.exception("Album photo classification failed")
            classifications.append(("other", None))

    logger.info("Album %s: classifications=%s", media_group_id, classifications)

    doc_types = {doc_type for doc_type, _bank in classifications}

    if doc_types == {"receipt"}:
        await send(f"Got {len(photos)} receipts — extracting each...")
        extracted = []
        for img in photos:
            try:
                extracted.append(extract_receipt(img))
            except Exception:
                logger.exception("Receipt extraction failed in album")
        if not extracted:
            await send("Sorry, I couldn't parse any of those receipts.")
            return
        context.user_data["receipt_queue"] = extracted[1:]
        await _start_review(send, context, extracted[0], source_type="photo", raw_input=None)
        return

    if doc_types == {"statement"}:
        groups = _group_statement_photos(photos, classifications)
        logger.info("Album %s: grouped into %s", media_group_id, {k: len(v) for k, v in groups.items()})

        await send(f"Got {len(photos)} statement pages across {len(groups)} statement(s) — extracting each...")

        results = []
        for bank_key, group_photos in groups.items():
            try:
                result = extract_statement(group_photos)
                logger.info("Album %s: extracted for %s: %s", media_group_id, bank_key, result)
                results.append(result)
            except Exception:
                logger.exception("Statement extraction failed in album for group %s", bank_key)
                await send(f"Sorry, I couldn't parse the {bank_key} statement.")

        if not results:
            return

        await _process_statement_results(send, context, results, extracted_from="photo album")
        return

    await send(
        "That batch looks like a mix of receipts, statements, or something unclear — please send "
        "statement pages together in one batch, and receipts separately."
    )


# ---------------------------------------------------------------------------
# Message handlers
# ---------------------------------------------------------------------------


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return

    if await handle_budget_text(update, context):
        return

    if await handle_recurring_text(update, context):
        return

    text = update.message.text

    awaiting = context.user_data.get("awaiting_edit_field")
    if awaiting:
        await _apply_field_edit(update, context, awaiting, text)
        return

    try:
        intent = classify_message_intent(text)
    except Exception:
        logger.exception("Intent classification failed — defaulting to transaction")
        intent = "transaction"

    if intent == "correction":
        await handle_correction_message(update, context, text)
        return

    if intent == "query":
        history = context.user_data.get("query_history", [])
        try:
            answer = answer_query(text, update.message.date.date(), history=history)
        except Exception:
            logger.exception("Query answering failed")
            answer = "Sorry, I couldn't answer that."
        else:
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": answer})
            context.user_data["query_history"] = history[-10:]
        await update.message.reply_text(answer)
        return

    try:
        data = extract_from_text(text, update.message.date.date())
    except Exception:
        logger.exception("Text extraction failed")
        await update.message.reply_text("Sorry, I couldn't parse that as a transaction.")
        return

    await _start_review(update.message.reply_text, context, data, source_type="text", raw_input=text)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = bytes(await file.download_as_bytearray())

    if update.message.media_group_id:
        await _buffer_album_photo(update, context, update.message.media_group_id, image_bytes)
        return

    try:
        doc_type, _bank = classify_photo(image_bytes)
    except Exception:
        logger.exception("Photo classification failed")
        await update.message.reply_text("Sorry, I couldn't process that photo. Try again?")
        return

    if doc_type == "other":
        await update.message.reply_text(
            "I couldn't tell what this photo is — I only handle receipts and card statements right now."
        )
        return

    if doc_type == "statement":
        await update.message.reply_text("Looks like a card statement — extracting details...")
        try:
            data = extract_statement([image_bytes])
        except Exception:
            logger.exception("Statement extraction failed")
            await update.message.reply_text("Sorry, I couldn't parse that statement.")
            return
        await _process_statement_results(update.message.reply_text, context, [data], extracted_from="photo")
        return

    await update.message.reply_text("Got a PHOTO — extracting details...")

    try:
        data = extract_receipt(image_bytes)
    except Exception:
        logger.exception("Receipt extraction failed")
        await update.message.reply_text("Sorry, I couldn't parse that receipt. Try another photo?")
        return

    await _start_review(update.message.reply_text, context, data, source_type="photo", raw_input=update.message.caption)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return

    awaiting = context.user_data.get("awaiting_edit_field")
    if awaiting:
        try:
            audio_bytes = await _download_voice_bytes(update, context)
            transcript = transcribe_voice(audio_bytes)
        except Exception:
            logger.exception("Voice transcription failed")
            await update.message.reply_text("Sorry, I couldn't transcribe that.")
            return
        await _apply_field_edit(update, context, awaiting, transcript)
        return

    await update.message.reply_text("Got a VOICE note — transcribing and extracting...")

    try:
        audio_bytes = await _download_voice_bytes(update, context)
        transcript = transcribe_voice(audio_bytes)
        data = extract_from_text(transcript, update.message.date.date())
    except Exception:
        logger.exception("Voice extraction failed")
        await update.message.reply_text("Sorry, I couldn't parse that voice note.")
        return

    await _start_review(update.message.reply_text, context, data, source_type="voice", raw_input=transcript)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return

    document = update.message.document
    filename = document.file_name or "unnamed file"

    if not filename.lower().endswith(".pdf"):
        await update.message.reply_text(f"Got a DOCUMENT: {filename}. Only PDF statements are supported.")
        return

    await update.message.reply_text("Got a statement PDF — extracting details...")

    file = await context.bot.get_file(document.file_id)
    pdf_bytes = bytes(await file.download_as_bytearray())

    try:
        page_images = _rasterize_pdf(pdf_bytes)
        data = extract_statement(page_images)
    except Exception:
        logger.exception("Statement extraction failed")
        await update.message.reply_text("Sorry, I couldn't parse that statement.")
        return

    await _process_statement_results(update.message.reply_text, context, [data], extracted_from=filename)


async def handle_unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await update.message.reply_text("Got a message type I don't recognize yet.")


# ---------------------------------------------------------------------------
# Callback (button) handlers
# ---------------------------------------------------------------------------


async def handle_review_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not _is_authorized(update):
        return

    pending = context.user_data.get("pending_transaction")
    if not pending:
        await query.edit_message_text("This request has expired — send a new photo/text/voice note.")
        return

    choice = query.data.split(":", 1)[1]

    if choice == "edit":
        await query.edit_message_text(_review_text(pending), reply_markup=_edit_field_keyboard())
        return

    await query.edit_message_text(
        _review_text(pending) + "\n\nWhich payment method?", reply_markup=_payment_method_keyboard()
    )


async def handle_edit_field_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not _is_authorized(update):
        return

    pending = context.user_data.get("pending_transaction")
    if not pending:
        await query.edit_message_text("This request has expired — send a new photo/text/voice note.")
        return

    field = query.data.split(":", 1)[1]
    context.user_data["awaiting_edit_field"] = field
    await query.edit_message_text(f"Send the new {field} as text or voice.")


async def handle_payment_method_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not _is_authorized(update):
        return

    pending = context.user_data.get("pending_transaction")
    if not pending:
        await query.edit_message_text("This request has expired — send a new photo/text/voice note.")
        return

    payment_method_id = int(query.data.split(":", 1)[1])

    try:
        saved = insert_transaction(
            merchant=pending["merchant"],
            amount=pending["amount"],
            currency=pending["currency"],
            transaction_date=pending["date"],
            category_name=pending["category"],
            payment_method_id=payment_method_id,
            source_type=pending["source_type"],
            raw_input=pending.get("raw_input"),
        )
    except Exception:
        logger.exception("Failed to save transaction")
        await query.edit_message_text("Sorry, saving that failed. Try again?")
        return

    context.user_data.pop("pending_transaction", None)
    context.user_data.pop("awaiting_edit_field", None)
    await query.edit_message_text(
        f"Saved: {pending['merchant']} — {pending['amount']} {pending['currency']} ({pending['category']})"
    )

    await maybe_prompt_recurring(
        query.message.reply_text,
        context,
        merchant=pending["merchant"],
        amount=pending["amount"],
        category_id=saved.get("category_id"),
        transaction_date=pending["date"],
    )

    queue = context.user_data.get("receipt_queue")
    if queue:
        next_data = queue.pop(0)
        await _start_review(query.message.reply_text, context, next_data, source_type="photo", raw_input=None)


async def handle_statement_payment_method_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not _is_authorized(update):
        return

    pending = context.user_data.get("pending_statement")
    if not pending:
        await query.edit_message_text("This request has expired — send a new statement.")
        return

    payment_method_id = int(query.data.split(":", 1)[1])

    try:
        await _save_statement_entry(context, pending, payment_method_id)
    except Exception:
        logger.exception("Failed to save statement entry")
        await query.edit_message_text("Sorry, saving that failed. Try again?")
        return

    context.user_data.pop("pending_statement", None)
    await query.edit_message_text("Saved that card. Checking for more...")
    await _advance_statement_queue(query.message.reply_text, context)
