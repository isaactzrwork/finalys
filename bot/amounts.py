from datetime import date

from services.openai_client import extract_field_value


def parse_amount(text: str, reference_date: date) -> float | None:
    cleaned = text.strip().lstrip("$").replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        pass
    try:
        return float(extract_field_value("amount", text, reference_date))
    except Exception:
        return None
