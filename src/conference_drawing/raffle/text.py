"""Text normalization and validation shared by models, forms and CSV export."""

import unicodedata

from django.core.exceptions import ValidationError
from email_validator import EmailNotValidError, validate_email

# Control, format (zero-width and bidi overrides), and line/paragraph separators.
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
# Format characters real text needs: ZWJ glues emoji sequences (woman + laptop),
# ZWNJ is required by Persian and Indic scripts. Neither can reorder text.
_ALLOWED_FORMAT_CHARS = frozenset({"\u200c", "\u200d"})


def has_forbidden_chars(value: str, allow_newline: bool = False) -> bool:
    """True if `value` has control, invisible-format or separator characters.

    Rejects Cc, Zl/Zp and every Cf (bidi overrides, zero-width space, BOM,
    soft hyphen) except ZWJ and ZWNJ. Newlines are allowed only on request,
    for free-text answers; single-line labels never allow them.
    """
    return any(
        unicodedata.category(ch) in _FORBIDDEN_CATEGORIES
        and ch not in _ALLOWED_FORMAT_CHARS
        and not (allow_newline and ch == "\n")
        for ch in value
    )


def normalize_email(raw: str) -> str:
    """Return the canonical form of an email address.

    Entry, exclusion lists and the unique constraint must all agree on who is
    "the same person"; routing every comparison through this one function is
    what makes `Alice@Example.COM ` and `alice@example.com` match.
    """
    return unicodedata.normalize("NFC", raw.strip()).lower()


def validate_deliverable_email(value: str) -> str:
    """Return `value` if Drip's payload validation will accept it, else raise.

    Django's `EmailField` is more lenient than the `email-validator` library
    behind pydantic's `EmailStr` (it allows `a@localhost`, `a@x.test`, ...).
    Entry must refuse anything the Drip payload would later reject, so both
    sides use this one rule; no DNS lookup is made.
    """
    try:
        validate_email(value, check_deliverability=False)
    except EmailNotValidError:
        raise ValidationError("Please enter a valid email address.") from None
    return value


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
    # Check the raw value: stripping first would hide a leading/trailing newline.
    if has_forbidden_chars(value):
        raise ValidationError(
            "Remove line breaks and invisible or control characters.",
            code="invalid_characters",
        )
    return stripped
