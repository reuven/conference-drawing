"""The app's log filter: no display tokens, no exception text with personal data."""

import io
import logging
from collections.abc import Callable, Iterator

import pytest
from django.db import IntegrityError
from django.test import Client
from hypothesis import given
from hypothesis import strategies as st

from conference_drawing.logredact import RedactFilter, redact_paths
from conference_drawing.raffle.draw import DrawingError
from conference_drawing.raffle.models import Conference
from conference_drawing.raffle.views import display

TOKEN = "tok3n-SECRET_abc123"  # noqa: S105
EMAIL = "ada.lovelace@secret-mail.example"


@pytest.fixture
def app_log() -> Iterator[io.StringIO]:
    """What the configured stderr handler (journald in production) writes."""
    [handler] = [h for h in logging.getLogger().handlers if h.name == "stderr"]
    assert isinstance(handler, logging.StreamHandler)
    buffer = io.StringIO()
    previous = handler.setStream(buffer)
    try:
        yield buffer
    finally:
        handler.setStream(previous)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (f"/display/{TOKEN}/panel/", "/display/REDACTED/panel/"),
        (f"/display/{TOKEN}/", "/display/REDACTED/"),
        (f"GET /DISPLAY/{TOKEN} x", "GET /DISPLAY/REDACTED x"),
        (f"//display//{TOKEN}?a=1", "//display//REDACTED?a=1"),
        (f"/%64isp%6Cay/{TOKEN}/", "/%64isp%6Cay/REDACTED/"),
        (f"%2Fdisplay%2F{TOKEN}", "%2Fdisplay%2FREDACTED"),
        ("/display/", "/display/"),
        ("/displays/abc", "/displays/abc"),
        ("/c/pycon/", "/c/pycon/"),
    ],
)
def test_redact_paths_known_answers(raw: str, expected: str) -> None:
    assert redact_paths(raw) == expected


@given(
    token=st.text(
        alphabet=st.characters(categories=["L", "N"], include_characters="-_"),
        min_size=8,
        max_size=40,
    ),
    prefix=st.sampled_from(["/display/", "/Display/", "//display/", "/%64isplay/"]),
)
def test_redact_paths_never_leaves_the_token(token: str, prefix: str) -> None:
    assert token not in redact_paths(f"Not Found: {prefix}{token}/panel/")


def _record(msg: str, *args: object, exc: BaseException | None = None) -> str:
    """Run one record through the filter and the handler's format."""
    exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
    record = logging.LogRecord("t", logging.ERROR, __file__, 1, msg, args, exc_info)
    assert RedactFilter().filter(record) is True
    return logging.Formatter("%(message)s").format(record)


def _raised(exc: BaseException) -> BaseException:
    try:
        raise exc
    except BaseException as caught:
        return caught


def test_args_are_redacted_too() -> None:
    out = _record("Internal Server Error: %s", f"/display/{TOKEN}/panel/")
    assert out == "Internal Server Error: /display/REDACTED/panel/"


def test_exception_text_becomes_its_class_name() -> None:
    """A pydantic-style `input_value=` or a DETAIL line must not reach the log."""
    out = _record("boom", exc=_raised(ValueError(f"input_value='{EMAIL}'")))
    assert EMAIL not in out
    assert "ValueError" in out
    assert "Traceback (most recent call last):" in out
    assert "_raised" in out  # the frames stay: they are code, not data


def test_chained_exception_text_is_dropped_too() -> None:
    try:
        try:
            raise IntegrityError(f"DETAIL:  Key (email)=({EMAIL}) already exists.")
        except IntegrityError as inner:
            raise RuntimeError(f"saving {EMAIL} failed") from inner
    except RuntimeError as outer:
        out = _record("boom", exc=outer)
    assert EMAIL not in out
    assert "IntegrityError" in out and "RuntimeError" in out
    assert "direct cause" in out


def test_context_exception_is_kept_unless_suppressed() -> None:
    try:
        try:
            raise KeyError(EMAIL)
        except KeyError:
            raise ValueError(EMAIL)  # noqa: B904 -- implicit context on purpose
    except ValueError as exc:
        out = _record("boom", exc=exc)
    assert "During handling" in out and "KeyError" in out
    assert EMAIL not in out
    try:
        try:
            raise KeyError(EMAIL)
        except KeyError:
            raise ValueError(EMAIL) from None
    except ValueError as exc:
        out = _record("boom", exc=exc)
    assert "KeyError" not in out


def test_a_cyclic_chain_is_printed_once() -> None:
    first, second = ValueError(EMAIL), KeyError(EMAIL)
    first.__context__, second.__context__ = second, first
    out = _record("boom", exc=first)
    assert out.count("ValueError") == 1 and out.count("KeyError") == 1
    assert EMAIL not in out


def test_exc_info_without_an_exception_adds_nothing() -> None:
    record = logging.LogRecord(
        "t", logging.ERROR, __file__, 1, "boom %s", ("x",), (None, None, None)
    )
    assert RedactFilter().filter(record) is True
    assert logging.Formatter("%(message)s").format(record) == "boom x"


def test_allowlisted_exception_keeps_its_message() -> None:
    """DrawingError messages are fixed organizer copy, never personal data."""
    out = _record("boom", exc=_raised(DrawingError("This drawing isn't open.")))
    assert "DrawingError: This drawing isn't open." in out


def test_record_without_exception_is_only_redacted() -> None:
    assert _record("entry created conference=%s entrant=%s", 1, 2) == (
        "entry created conference=1 entrant=2"
    )


def test_a_logged_500_on_the_display_panel_has_no_token_or_email(
    conference_factory: Callable[..., Conference],
    app_log: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conference = conference_factory()

    def explode() -> object:
        raise ValueError(f"input_value='{EMAIL}'")

    monkeypatch.setattr(display, "_now", explode)
    client = Client(raise_request_exception=False)
    url = f"/display/{conference.display_token}/panel/"
    assert client.get(url).status_code == 500
    logged = app_log.getvalue()
    assert "Internal Server Error: /display/REDACTED/panel/" in logged
    assert conference.display_token not in logged
    assert EMAIL not in logged
    assert "ValueError" in logged


def test_a_logged_404_on_a_guessed_display_token_is_redacted(
    db: None, app_log: io.StringIO
) -> None:
    assert Client().get(f"/display/{TOKEN}/").status_code == 404
    logged = app_log.getvalue()
    assert TOKEN not in logged
    assert "/display/REDACTED/" in logged
