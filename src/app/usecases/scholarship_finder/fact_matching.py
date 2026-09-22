"""Exact comparison of raw fact strings, ported from Scholarship Finder.

Deliberately no fuzzy tolerance on money or dates. The verification
standard's own worst near-miss - a claimed GBP 13,000 against a real GBP
16,750 - is exactly the sort of "close enough" figure a tolerant comparison
would call agreement. Two facts either state the same value or they do not
corroborate each other.

This is also where the automation boundary sits in the compare step: the
model supplies excerpts and nuance, and *this* decides whether two values
agree. Asking a model "do these agree?" is asking it to verify.
"""

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

_AMOUNT_PATTERN = re.compile(r"^([£$€])\s?([\d,]+(?:\.\d+)?)$")
_ORDINAL_SUFFIX = re.compile(r"(\d+)(?:st|nd|rd|th)", re.IGNORECASE)
_DEADLINE_FORMAT = "%B %d %Y"


def parse_amount(raw: str) -> tuple[str, Decimal] | None:
    """`"£13,000"` -> `("£", Decimal("13000"))`, or None if unparseable."""
    match = _AMOUNT_PATTERN.match(raw.strip())
    if not match:
        return None
    currency, digits = match.groups()
    try:
        return currency, Decimal(digits.replace(",", ""))
    except InvalidOperation:
        return None


def amounts_match(a: str, b: str) -> bool:
    parsed_a = parse_amount(a)
    parsed_b = parse_amount(b)
    return parsed_a is not None and parsed_a == parsed_b


def parse_deadline(raw: str) -> date | None:
    """`"March 15, 2026"` / `"March 15th 2026"` -> `date(2026, 3, 15)`."""
    cleaned = _ORDINAL_SUFFIX.sub(r"\1", raw.strip()).replace(",", "")
    try:
        return datetime.strptime(cleaned, _DEADLINE_FORMAT).date()
    except ValueError:
        return None


def deadlines_match(a: str, b: str) -> bool:
    parsed_a = parse_deadline(a)
    parsed_b = parse_deadline(b)
    return parsed_a is not None and parsed_a == parsed_b
