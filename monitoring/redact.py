"""Mask personal data (PII) in log lines before they are saved to incidents/ or used for training.

What gets masked:
- email addresses              -> <EMAIL>
- card-like numbers (Luhn)     -> <CARD>
- Indian phone numbers         -> <PHONE>
- names of people              -> <NAME>  (known first/last names, plus values after name=/customer=)
- street addresses             -> <ADDRESS>

Known names and streets come from demo_app's fake-people.properties, the same lists the customer
table is seeded from. For a real system, load them from the customer table instead.
"""

import json
import re
from functools import lru_cache
from pathlib import Path

PEOPLE_FILE = Path(__file__).resolve().parent / "demo_app_template" / "src" / "main" / "resources" / "fake-people.properties"

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CARD = re.compile(r"(?<![\w])(?:\d[ -]?){12,18}\d(?![\w])")
# Indian mobile numbers start with 6-9; requiring that keeps counts like 1000000000 from being masked.
PHONE = re.compile(r"(?<![\w+])(?:\+91[\s-]?|\+91-\+91\s)?(?:\+91\s)?[6-9]\d{4}\s?\d{5}(?![\w])")
FIELD_NAME = re.compile(r"\b(name|customer)=([A-Z][\w'-]*(?:\s[A-Z][\w'-]*)?)")


def _luhn_ok(digits: str) -> bool:
    total, double = 0, False
    for d in reversed(digits):
        n = int(d) * (2 if double else 1)
        total += n - 9 if n > 9 else n
        double = not double
    return total % 10 == 0


def _card(match: re.Match) -> str:
    digits = re.sub(r"\D", "", match.group())
    return "<CARD>" if 13 <= len(digits) <= 19 and _luhn_ok(digits) else match.group()


@lru_cache
def _people_patterns() -> tuple[re.Pattern, re.Pattern]:
    people = {}
    for line in PEOPLE_FILE.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            people[key.strip()] = [v.strip() for v in value.split(",") if v.strip()]
    names = sorted(people["firstNames"] + people["lastNames"], key=len, reverse=True)
    name_re = re.compile(r"\b(?:" + "|".join(map(re.escape, names)) + r")\b")
    street_re = re.compile(r"\b\d{1,4}\s+(?:" + "|".join(map(re.escape, people["streets"])) + r")\b")
    return name_re, street_re


def redact_text(text: str) -> str:
    name_re, street_re = _people_patterns()
    text = EMAIL.sub("<EMAIL>", text)  # first, so names inside emails go with them
    text = CARD.sub(_card, text)
    text = PHONE.sub("<PHONE>", text)
    text = street_re.sub("<ADDRESS>", text)
    text = FIELD_NAME.sub(lambda m: f"{m.group(1)}=<NAME>", text)
    text = name_re.sub("<NAME>", text)
    return text


def redact_value(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    return value


def redact_log_line(line: str) -> str:
    """Redact one JSON log line (every string value), or the raw text if it isn't JSON."""
    try:
        return json.dumps(redact_value(json.loads(line)), ensure_ascii=False)
    except ValueError:
        return redact_text(line)
