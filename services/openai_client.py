import base64
import io
import json
from datetime import date

from openai import OpenAI

from bot.config import OPENAI_API_KEY

MODEL = "gpt-4.1-mini"
TRANSCRIBE_MODEL = "gpt-4o-mini-transcribe"

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client


CATEGORIES = [
    "Food & Dining",
    "Groceries",
    "Transport",
    "Shopping",
    "Bills & Subscriptions",
    "Entertainment",
    "Health & Wellness",
    "Travel",
    "ting<3",
    "Others",
]

TRANSACTION_SCHEMA = {
    "name": "transaction_extraction",
    "schema": {
        "type": "object",
        "properties": {
            "merchant": {"type": "string"},
            "amount": {"type": "number"},
            "currency": {
                "type": "string",
                "description": "ISO 4217 code, e.g. SGD, USD. Assume SGD unless clearly stated otherwise.",
            },
            "date": {"type": "string", "description": "Transaction date as YYYY-MM-DD"},
            "category": {"type": "string", "enum": CATEGORIES},
        },
        "required": ["merchant", "amount", "currency", "date", "category"],
        "additionalProperties": False,
    },
    "strict": True,
}

IMAGE_SYSTEM_PROMPT = (
    "You extract structured transaction data from receipt photos. "
    "Assume Singapore (SGD) unless the receipt clearly shows a different currency. "
    "Pick exactly one category from the allowed list, using 'Others' only if nothing else fits."
)

TEXT_SYSTEM_PROMPT_TEMPLATE = (
    "You extract structured transaction data from a short message describing a purchase or expense. "
    "Today's date is {today}; resolve relative dates like 'yesterday' or 'last night' against it. "
    "Assume Singapore (SGD) unless a different currency is explicitly mentioned. "
    "If no merchant is named, use a short description of what was bought instead. "
    "Pick exactly one category from the allowed list, using 'Others' only if nothing else fits."
)


CLASSIFY_SCHEMA = {
    "name": "photo_classification",
    "schema": {
        "type": "object",
        "properties": {
            "document_type": {
                "type": "string",
                "enum": ["receipt", "statement", "other"],
            },
            "bank": {
                "type": ["string", "null"],
                "description": (
                    "If document_type is 'statement', the bank/issuer name printed on this page "
                    "(e.g. 'DBS', 'Citibank', 'HSBC'). Null for receipts or 'other'."
                ),
            },
        },
        "required": ["document_type", "bank"],
        "additionalProperties": False,
    },
    "strict": True,
}

CLASSIFY_SYSTEM_PROMPT = (
    "Classify this photo as exactly one of: "
    "'receipt' (a purchase receipt/invoice for a single transaction), "
    "'statement' (a credit/debit card or bank statement page — shows a billing period, "
    "multiple transactions, and/or a rewards/points summary), or "
    "'other' (anything else). "
    "If it's a statement, also identify the bank/issuer name printed on it."
)


def classify_photo(image_bytes: bytes) -> tuple[str, str | None]:
    """Returns (document_type, bank). bank is None unless document_type == 'statement'."""
    client = _get_client()
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    response = client.chat.completions.create(
        model=MODEL,
        temperature=0,
        messages=[
            {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What kind of document is this photo?"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            },
        ],
        response_format={"type": "json_schema", "json_schema": CLASSIFY_SCHEMA},
    )

    result = json.loads(response.choices[0].message.content)
    return result["document_type"], result.get("bank")


def extract_receipt(image_bytes: bytes) -> dict:
    client = _get_client()
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    response = client.chat.completions.create(
        model=MODEL,
        temperature=0,
        messages=[
            {"role": "system", "content": IMAGE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Extract the transaction details from this receipt."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            },
        ],
        response_format={"type": "json_schema", "json_schema": TRANSACTION_SCHEMA},
    )

    return json.loads(response.choices[0].message.content)


def extract_from_text(text: str, reference_date: date) -> dict:
    client = _get_client()

    response = client.chat.completions.create(
        model=MODEL,
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": TEXT_SYSTEM_PROMPT_TEMPLATE.format(today=reference_date.isoformat()),
            },
            {"role": "user", "content": text},
        ],
        response_format={"type": "json_schema", "json_schema": TRANSACTION_SCHEMA},
    )

    return json.loads(response.choices[0].message.content)


def transcribe_voice(audio_bytes: bytes) -> str:
    client = _get_client()
    buffer = io.BytesIO(audio_bytes)
    buffer.name = "voice.ogg"
    transcript = client.audio.transcriptions.create(model=TRANSCRIBE_MODEL, file=buffer)
    return transcript.text


FIELD_SCHEMAS = {
    "merchant": {
        "name": "field_edit",
        "schema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    "amount": {
        "name": "field_edit",
        "schema": {
            "type": "object",
            "properties": {"value": {"type": "number"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    "date": {
        "name": "field_edit",
        "schema": {
            "type": "object",
            "properties": {"value": {"type": "string", "description": "YYYY-MM-DD"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    "category": {
        "name": "field_edit",
        "schema": {
            "type": "object",
            "properties": {"value": {"type": "string", "enum": CATEGORIES}},
            "required": ["value"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}

FIELD_PROMPTS = {
    "merchant": "The user is correcting the merchant/description of a transaction. Extract the new merchant name from their message.",
    "amount": "The user is correcting the amount of a transaction. Extract the new amount as a plain number from their message.",
    "date": (
        "The user is correcting the date of a transaction. Today's date is {today}; "
        "resolve relative dates like 'yesterday' or '2 days ago' against it. Extract the new date as YYYY-MM-DD."
    ),
    "category": "The user is correcting the category of a transaction. Map their message to exactly one of the allowed categories.",
}


def extract_field_value(field: str, text: str, reference_date: date):
    client = _get_client()
    prompt = FIELD_PROMPTS[field].format(today=reference_date.isoformat())

    response = client.chat.completions.create(
        model=MODEL,
        temperature=0,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ],
        response_format={"type": "json_schema", "json_schema": FIELD_SCHEMAS[field]},
    )

    return json.loads(response.choices[0].message.content)["value"]


STATEMENT_SCHEMA = {
    "name": "statement_extraction",
    "schema": {
        "type": "object",
        "properties": {
            "statement_date": {
                "type": "string",
                "description": "The statement date printed on the document (often labelled 'Statement Date'), YYYY-MM-DD",
            },
            "cards": {
                "type": "array",
                "description": "One entry per card shown on the statement — some statements cover multiple cards.",
                "items": {
                    "type": "object",
                    "properties": {
                        "card_last4": {
                            "type": "string",
                            "description": "The last 4 digits of this card's number, as printed next to the card name.",
                        },
                        "miles_earned": {
                            "type": "number",
                            "description": (
                                "Points/miles EARNED or ADJUSTED this statement cycle for this specific card, "
                                "read from a rewards/points summary table (e.g. a column labelled "
                                "'Earned/Adjusted'). This is NOT the amount spent, NOT the cumulative/lifetime "
                                "points balance, and NOT a total combined across cards — it must be this one "
                                "card's own earned-this-cycle figure."
                            ),
                        },
                        "balance": {
                            "type": "number",
                            "description": (
                                "The current TOTAL/cumulative points or miles balance available for this card, "
                                "as of this statement — e.g. a 'Balance' column in a points summary table, or "
                                "a 'Total Points Available' / 'Total Miles' figure. This is the running total, "
                                "not this cycle's earned amount."
                            ),
                        },
                        "bank_flagged_expiring_amount": {
                            "type": ["number", "null"],
                            "description": (
                                "If the statement explicitly flags an upcoming expiry for THIS card (e.g. an "
                                "'Expiring On' column in the points summary table showing a specific amount), "
                                "that amount. Null if the statement shows 'No Expiry' or has no such column at all."
                            ),
                        },
                        "bank_flagged_expiry_date": {
                            "type": ["string", "null"],
                            "description": (
                                "The date that bank_flagged_expiring_amount expires on, as printed on the "
                                "statement (e.g. the header of an 'Expiring On 30 SEP 2026' column), YYYY-MM-DD. "
                                "Null if bank_flagged_expiring_amount is null."
                            ),
                        },
                    },
                    "required": [
                        "card_last4",
                        "miles_earned",
                        "balance",
                        "bank_flagged_expiring_amount",
                        "bank_flagged_expiry_date",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["statement_date", "cards"],
        "additionalProperties": False,
    },
    "strict": True,
}

STATEMENT_SYSTEM_PROMPT = (
    "You extract data from a credit card statement, which may cover multiple cards and multiple pages "
    "(the rewards/points summary for a card is often on a LATER page than that card's transaction "
    "listing — check all pages provided, don't stop at the first page mentioning a card). Find the "
    "statement date. Then find each card's rewards/points summary — the layout varies by bank:\n"
    "- Some show one column per card in a shared table (e.g. 'Card Number', 'Balance as of Last "
    "Statement', 'Earned/Adjusted', 'Redeemed/Expired', 'Balance').\n"
    "- Some show a per-card breakdown block with SEPARATE 'earned this month' and 'bonus earned this "
    "month' figures plus a 'Total Available' — for these, this cycle's earned amount is the SUM of "
    "the 'earned this month' and 'bonus earned this month' figures. This sum can be NEGATIVE if there "
    "was a bonus clawback/adjustment — report it as negative in that case, don't clamp to zero.\n"
    "For EACH card, extract its last-4 card digits, its total earned-this-cycle figure (summing "
    "multiple 'earned' columns if the layout has more than one), and its current total/cumulative "
    "balance ('Balance' / 'Total Available' / 'Total Points Available'). Do not use the per-card "
    "transaction TOTAL/SUB-TOTAL amounts (those are spend, not points) and do not use a combined grand "
    "total across cards — report each card's own figures separately.\n"
    "Some statements (e.g. DBS) also include an 'Expiring On <date>' column in the points summary "
    "table, showing a specific amount expiring on a specific date — this is the bank's own explicit "
    "expiry flag for the next batch of points/miles about to expire. If present, extract that amount "
    "and date for the card it applies to. If the column says 'No Expiry' or doesn't exist for a card, "
    "leave both fields null — don't guess or estimate this yourself."
)


def extract_statement(page_images: list[bytes]) -> dict:
    client = _get_client()

    content = [
        {
            "type": "text",
            "text": "Extract the statement date and, per card, the points/miles earned this cycle from this statement.",
        }
    ]
    for img in page_images:
        b64 = base64.b64encode(img).decode("utf-8")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})

    response = client.chat.completions.create(
        model=MODEL,
        temperature=0,
        messages=[
            {"role": "system", "content": STATEMENT_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        response_format={"type": "json_schema", "json_schema": STATEMENT_SCHEMA},
    )

    return json.loads(response.choices[0].message.content)


INTENT_SCHEMA = {
    "name": "message_intent",
    "schema": {
        "type": "object",
        "properties": {"intent": {"type": "string", "enum": ["transaction", "query", "correction"]}},
        "required": ["intent"],
        "additionalProperties": False,
    },
    "strict": True,
}

INTENT_SYSTEM_PROMPT = (
    "Classify this message as exactly one of: "
    "'transaction' (the user is reporting a NEW purchase/expense they made, to be logged, "
    "e.g. 'spent $20 at Grab'), "
    "'query' (the user is asking a question about their own spending/budget/data, "
    "e.g. 'how much did I spend on Grab last month', 'am I overspending on dating this month'), or "
    "'correction' (the user wants to change/fix a detail of an ALREADY-LOGGED transaction, "
    "e.g. 'change capcut's category to bills and subscriptions', 'actually that starbucks was $6 not $5')."
)


def classify_message_intent(text: str) -> str:
    client = _get_client()
    response = client.chat.completions.create(
        model=MODEL,
        temperature=0,
        messages=[
            {"role": "system", "content": INTENT_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        response_format={"type": "json_schema", "json_schema": INTENT_SCHEMA},
    )
    return json.loads(response.choices[0].message.content)["intent"]


QUERY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_transactions",
            "description": (
                "Query the user's own logged transactions with optional filters. Returns either "
                "matching transactions (if group_by is omitted) or amounts summed per group (if set)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": CATEGORIES, "description": "Filter to one category"},
                    "merchant": {
                        "type": "string",
                        "description": "Filter to transactions whose merchant name contains this text (case-insensitive)",
                    },
                    "payment_method": {
                        "type": "string",
                        "description": "Filter to transactions on a card/payment method whose label contains this text",
                    },
                    "date_from": {"type": "string", "description": "Start date YYYY-MM-DD, inclusive"},
                    "date_to": {"type": "string", "description": "End date YYYY-MM-DD, inclusive"},
                    "group_by": {
                        "type": "string",
                        "enum": ["category", "merchant", "payment_method", "day", "week", "month"],
                        "description": "If set, sum results grouped by this dimension instead of returning a flat total",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_budget_status",
            "description": "Get the user's current-month budget, spending so far, and safe-to-spend figures.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]

QUERY_SYSTEM_PROMPT = (
    "You answer the user's questions about their own personal finance data. Today's date is {today}. "
    "Use the available tools to fetch real data before answering — never guess or estimate a number "
    "yourself. If a tool returns no matching data (or an 'error' key), say so clearly and honestly "
    "rather than making something up. Resolve relative time references (e.g. 'last month', 'this "
    "week') into explicit date_from/date_to before calling query_transactions. Keep answers "
    "conversational and concise."
)


def answer_query(question: str, today: date, history: list[dict] | None = None) -> str:
    from services.query import get_budget_status, query_transactions

    client = _get_client()
    messages = [{"role": "system", "content": QUERY_SYSTEM_PROMPT.format(today=today.isoformat())}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": question})

    for _ in range(4):  # safety cap on tool-call rounds
        response = client.chat.completions.create(model=MODEL, temperature=0, messages=messages, tools=QUERY_TOOLS)
        message = response.choices[0].message

        if not message.tool_calls:
            return message.content

        messages.append(
            {
                "role": "assistant",
                "content": message.content,
                "tool_calls": [
                    {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in message.tool_calls
                ],
            }
        )

        for tool_call in message.tool_calls:
            name = tool_call.function.name
            args = json.loads(tool_call.function.arguments)

            if name == "query_transactions":
                result = query_transactions(**args)
            elif name == "get_budget_status":
                result = get_budget_status()
            else:
                result = {"error": f"Unknown tool: {name}"}

            messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": json.dumps(result, default=str)})

    return "Sorry, I couldn't work out an answer to that."


CORRECTION_SCHEMA = {
    "name": "transaction_correction",
    "schema": {
        "type": "object",
        "properties": {
            "merchant_hint": {
                "type": "string",
                "description": "The merchant/description of the already-logged transaction the user wants to correct",
            },
            "field": {"type": "string", "enum": ["merchant", "amount", "date", "category", "payment_method"]},
            "new_value": {"type": "string", "description": "The new value for that field, exactly as the user stated it"},
        },
        "required": ["merchant_hint", "field", "new_value"],
        "additionalProperties": False,
    },
    "strict": True,
}

CORRECTION_SYSTEM_PROMPT = (
    "The user wants to correct a previously logged transaction. Identify which merchant/transaction "
    "they mean, which single field they want to change (merchant, amount, date, category, or "
    "payment_method), and the new value they stated for it."
)


def extract_correction(text: str) -> dict:
    client = _get_client()
    response = client.chat.completions.create(
        model=MODEL,
        temperature=0,
        messages=[
            {"role": "system", "content": CORRECTION_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        response_format={"type": "json_schema", "json_schema": CORRECTION_SCHEMA},
    )
    return json.loads(response.choices[0].message.content)
