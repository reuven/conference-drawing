import pytest
from django.core.exceptions import ValidationError
from hypothesis import given
from hypothesis import strategies as st

from conference_drawing.raffle.text import normalize_email, validate_single_line_label


def test_normalize_email_examples() -> None:
    assert normalize_email("  Alice@Example.COM ") == "alice@example.com"


def test_normalize_email_uses_nfc() -> None:
    assert normalize_email("café@example.com") == "café@example.com"


@given(st.emails())
def test_normalize_email_idempotent(e: str) -> None:
    assert normalize_email(normalize_email(e)) == normalize_email(e)


@given(st.text())
def test_normalize_email_idempotent_on_any_text(raw: str) -> None:
    once = normalize_email(raw)
    assert normalize_email(once) == once


@pytest.mark.parametrize(
    "bad", ["a\nb", "a\rb", "x‮y", "z\x00", "n" * 201, "a​b", "", "   "]
)
def test_label_rejects(bad: str) -> None:
    with pytest.raises(ValidationError):
        validate_single_line_label(bad)


def test_label_strips_and_honors_max_len() -> None:
    assert validate_single_line_label("  hi  ") == "hi"
    assert validate_single_line_label("abc", max_len=3) == "abc"
    with pytest.raises(ValidationError):
        validate_single_line_label("abcd", max_len=3)


@given(
    st.text(
        alphabet=st.characters(categories=["L", "M", "N", "P", "Zs"]),
        min_size=1,
        max_size=200,
    ).filter(str.strip)
)
def test_label_accepts_printable_single_line(s: str) -> None:
    assert validate_single_line_label(s) == s.strip()
