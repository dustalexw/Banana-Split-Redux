"""Small, explicit conversions for the cut-review interface."""
import re


def parse_time(text: str) -> float:
    """Accept seconds, MM:SS or HH:MM:SS, with optional milliseconds."""
    text = text.strip()
    if not re.fullmatch(r"\d+(?::\d{1,2}){0,2}(?:\.\d{1,3})?", text):
        raise ValueError("Enter seconds, MM:SS or HH:MM:SS.mmm.")
    fields = text.split(":")
    if len(fields) > 1 and any(float(f) >= 60 for f in fields[1:]):
        raise ValueError("Minutes and seconds after a colon must be below 60.")
    value = 0.0
    for field in fields: value = value*60 + float(field)
    return value
