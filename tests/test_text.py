import pytest
from django.core.exceptions import ValidationError
from hypothesis import given
from hypothesis import strategies as st

from conference_drawing.raffle.text import (
    email_key,
    normalize_email,
    validate_single_line_label,
)


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
    "bad", ["a\n", "\nb", "a\nb", "a\rb", "x‮y", "z\x00", "n" * 201, "a​b", "", "   "]
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


@pytest.mark.parametrize(
    "ok", ["Ali\u200cرضا", "Dev 👩\u200d💻", "🏳️\u200d🌈", "❤️\u200d🔥"]
)
def test_label_accepts_zwj_and_zwnj_text(ok: str) -> None:
    assert validate_single_line_label(ok) == ok


@pytest.mark.parametrize(
    "bad",
    [
        "a\u061cb",
        "a\u200eb",
        "a\u200fb",
        "a\u2066b",
        "a\ufeffb",
        "a\u00adb",
        "a\u2028b",
    ],
)
def test_label_still_rejects_bidi_bom_and_separators(bad: str) -> None:
    with pytest.raises(ValidationError):
        validate_single_line_label(bad)


@given(
    st.lists(
        st.sampled_from(["a", "é", "👩", "\u200d", "💻", "\u200c", "ر", " "]),
        min_size=1,
        max_size=50,
    ).filter(lambda parts: "".join(parts).strip() != "")
)
def test_label_accepts_text_with_zwj_and_zwnj(parts: list[str]) -> None:
    s = "".join(parts)
    assert validate_single_line_label(s) == s.strip()


@pytest.mark.parametrize(
    ("bad", "code", "message"),
    [
        ("   ", "blank", "This field cannot be blank."),
        (
            "n" * 201,
            "max_length",
            "Ensure this value has at most 200 characters.",
        ),
        (
            "a\nb",
            "invalid_characters",
            "Remove line breaks and invisible or control characters.",
        ),
    ],
)
def test_label_error_code_and_message(bad: str, code: str, message: str) -> None:
    with pytest.raises(ValidationError) as caught:
        validate_single_line_label(bad)
    [error] = caught.value.error_list
    assert (error.code, error.message) == (code, message)


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("Ada+x@Example.com", "ada@example.com"),
        ("ada+a+b@example.com", "ada@example.com"),
        ("ada+@example.com", "ada@example.com"),
        ("ada-x@example.com", "ada-x@example.com"),
        ("a.da@example.com", "a.da@example.com"),
        ("ada@Bücher.example", "ada@xn--bcher-kva.example"),
        ("ada@xn--bcher-kva.example", "ada@xn--bcher-kva.example"),
        ("+x@example.com", "+x@example.com"),
        ("no-at-sign", "no-at-sign"),
        ("ada@" + "a" * 70 + ".example", "ada@" + "a" * 70 + ".example"),
    ],
)
def test_email_key_known_answers(raw: str, key: str) -> None:
    assert email_key(raw) == key


def test_email_key_idna_spellings_agree() -> None:
    assert email_key("ada@bücher.example") == email_key("ADA@xn--bcher-kva.example")


@given(st.emails())
def test_email_key_idempotent(e: str) -> None:
    assert email_key(email_key(e)) == email_key(e)


@given(st.text())
def test_email_key_idempotent_on_any_text(raw: str) -> None:
    once = email_key(raw)
    assert email_key(once) == once


_PART = st.text("abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=10)


@given(_PART, _PART, _PART)
def test_email_key_ignores_plus_tag(user: str, tag: str, domain: str) -> None:
    assert email_key(f"{user}+{tag}@{domain}.example") == email_key(
        f"{user}@{domain}.example"
    )
