from datetime import date, datetime

from services.openai_client import extract_field_value


def format_date(value: str | date) -> str:
    """Format a date for display in bot messages as DD/MM/YYYY.

    Internal storage/API calls stay ISO (YYYY-MM-DD) throughout the codebase — this is only
    for what the user sees in chat.
    """
    d = date.fromisoformat(value) if isinstance(value, str) else value
    return d.strftime("%d/%m/%Y")


def parse_date(text: str, reference_date: date) -> date | None:
    """Parse a user-entered date. Tries DD/MM/YYYY directly first (fast, no API call), then
    falls back to LLM extraction for natural language ('next Monday', '15 Aug', etc.)."""
    text = text.strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(extract_field_value("date", text, reference_date))
    except Exception:
        return None
