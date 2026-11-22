from urllib.parse import unquote

import pytest
from django.core.exceptions import ValidationError
from hypothesis import example, given
from hypothesis import strategies as st

from conference_drawing.raffle.codes import parse_codes, render_prize_url

_valid_code = st.text(
    st.characters(blacklist_categories=("Cc", "Cf", "Cs", "Z")),
    min_size=1,
    max_size=100,
)


def test_parse_codes_strips_drops_blanks_and_dedupes_in_order() -> None:
    assert parse_codes("A1\n\n A2 \nA1") == ["A1", "A2"]


def test_parse_codes_handles_crlf_and_empty() -> None:
    assert parse_codes("A\r\nB\r\n") == ["A", "B"]
    assert parse_codes("") == []
    assert parse_codes(" \n\t\n") == []


def test_parse_codes_accepts_exactly_100_chars() -> None:
    assert parse_codes("x" * 100) == ["x" * 100]


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("bad code", 1),
        ("ok\n\nbad\tcode", 3),
        ("ok\nx\x00", 2),
        ("ok\nz​w", 2),  # zero-width space (Cf)
        ("ok\nok2\nr‮l", 3),  # bidi override (Cf)
        ("ok\nin\x85ner", 2),  # NEL
        ("ok\nin ner", 2),  # no-break space
        ("ok\nin ner", 2),  # line separator
        ("ok\n" + "x" * 101, 2),
    ],
)
def test_parse_codes_rejects_naming_first_bad_line(text: str, line: int) -> None:
    with pytest.raises(ValidationError) as exc:
        parse_codes(text)
    assert f"line {line}" in exc.value.messages[0]


def test_parse_codes_reports_first_of_several_bad_lines() -> None:
    with pytest.raises(ValidationError) as exc:
        parse_codes("a\nb c\nd e")
    assert "line 2" in exc.value.messages[0]
    assert "line 3" not in exc.value.messages[0]


def test_render_prize_url_example() -> None:
    assert (
        render_prize_url("https://s.example/c?coupon={code}", "X/1")
        == "https://s.example/c?coupon=X%2F1"
    )


def test_render_prize_url_replaces_every_placeholder() -> None:
    assert render_prize_url("{code}-{code}", "a%b") == "a%25b-a%25b"


def test_render_prize_url_without_placeholder_unchanged() -> None:
    assert render_prize_url("https://s.example/c", "X") == "https://s.example/c"


@given(st.lists(_valid_code, max_size=8), st.text(" \t\r", max_size=3))
def test_parse_codes_idempotent_on_valid_input(codes: list[str], pad: str) -> None:
    once = parse_codes("\n".join(f"{pad}{c}{pad}" for c in codes))
    assert parse_codes("\n".join(once)) == once
    assert len(once) == len(set(once))


@given(_valid_code)
@example("X/1")
@example("a&b=c#d?é%41")
def test_substituted_code_round_trips_through_unquote(code: str) -> None:
    url = render_prize_url("https://s.example/c?coupon={code}&x=1", code)
    segment = url.removeprefix("https://s.example/c?coupon=").removesuffix("&x=1")
    assert unquote(segment) == code
    assert all(ch not in segment for ch in "/&#?= ")


@given(st.text())
def test_parse_codes_never_crashes(raw: str) -> None:
    try:
        result = parse_codes(raw)
    except ValidationError:
        return
    assert isinstance(result, list)
    assert all(c == c.strip() and 0 < len(c) <= 100 for c in result)
