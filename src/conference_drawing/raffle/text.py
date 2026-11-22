"""Text normalization and validation shared by models, forms and CSV export."""

import unicodedata

from django.core.exceptions import ValidationError

# Control, format (zero-width and bidi overrides), and line/paragraph separators.
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def normalize_email(raw: str) -> str:
    """Return the canonical form of an email address.

    Entry, exclusion lists and the unique constraint must all agree on who is
    "the same person"; routing every comparison through this one function is
    what makes `Alice@Example.COM ` and `alice@example.com` match.
    """
    return unicodedata.normalize("NFC", raw.strip()).lower()


def validate_single_line_label(value: str, max_len: int = 200) -> str:
    """Return `value` stripped, or raise ValidationError if it is unsafe as a label.

    Names and titles end up on a projected display, in CSVs and in emails, so
    they must be one visible line: no newlines, control or invisible format
    characters (including bidi overrides that can spoof text direction), and
    no excess length. Use it on any short free-text field a visitor types.
    """
    stripped = value.strip()
    if not stripped:
        raise ValidationError("This field cannot be blank.", code="blank")
    if len(stripped) > max_len:
        raise ValidationError(
            f"Ensure this value has at most {max_len} characters.", code="max_length"
        )
    if any(unicodedata.category(ch) in _FORBIDDEN_CATEGORIES for ch in stripped):
        raise ValidationError(
            "Remove line breaks and invisible or control characters.",
            code="invalid_characters",
        )
    return stripped
