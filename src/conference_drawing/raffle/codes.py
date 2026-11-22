"""Coupon-code parsing (admin paste box) and prize-URL rendering (Drip hand-off)."""

import unicodedata
from urllib.parse import quote

from django.core.exceptions import ValidationError

MAX_CODE_LENGTH = 100
_CONTROL_CATEGORIES = frozenset({"Cc", "Cf"})


def parse_codes(text: str) -> list[str]:
    """Turn a pasted block of coupon codes into a clean, de-duplicated list.

    Admins paste one code per line from a spreadsheet or email, so we tolerate
    blank lines, stray padding and repeats (first occurrence wins, order kept).
    A code that contains inner whitespace, a control or invisible format
    character, or is over 100 characters is almost certainly a paste mistake
    that would produce a broken prize link, so we refuse the whole batch and
    name the 1-based line so the admin can fix it.
    """
    # Split on "\n" only: str.splitlines would silently break a code at
    # characters like U+2028 instead of letting us reject it.
    seen: dict[str, None] = {}
    for number, line in enumerate(text.split("\n"), start=1):
        code = line.strip()
        if not code:
            continue
        if len(code) > MAX_CODE_LENGTH:
            raise ValidationError(
                f"Code on line {number} is longer than {MAX_CODE_LENGTH} characters."
            )
        if any(ch.isspace() for ch in code):
            raise ValidationError(f"Code on line {number} contains whitespace.")
        if any(unicodedata.category(ch) in _CONTROL_CATEGORIES for ch in code):
            raise ValidationError(
                f"Code on line {number} contains control or invisible characters."
            )
        seen.setdefault(code)
    return list(seen)


def render_prize_url(template: str, code: str) -> str:
    """Fill every `{code}` in a prize-URL template with the percent-encoded code.

    Codes are admin-pasted text and must not be able to alter the URL's
    structure (`/`, `&`, `#`), so we encode everything, with no safe characters.
    A template without `{code}` (a shared link) comes back unchanged.
    """
    return template.replace("{code}", quote(code, safe=""))
