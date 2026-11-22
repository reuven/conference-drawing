"""A logging filter that keeps secrets and personal data out of the app's logs.

The app logs to stderr, which systemd sends to journald, so anything logged
outlives the deletion of entrants. Two things could reach it unreviewed:

- **Display tokens.** `django.request` logs the request path of every 404
  ("Not Found: ...") and 500 ("Internal Server Error: ..."), and a path under
  `/display/` carries the token that authorizes the booth display. The token
  is replaced by `REDACTED`, with the same rule as Caddy's access log: any
  case, repeated slashes, and %-escaped letters or slashes.
- **Exception text.** Messages such as a pydantic `ValidationError`'s
  `input_value=...` or an `IntegrityError`'s DETAIL line can quote a name or
  an email. The exception is still logged as a traceback, because the frames
  (file, line, function, source line) are code, not data, and are what make
  a 500 debuggable. But each exception in the chain is shown by class name
  only, unless its class is on `ALLOWED_MESSAGES`.

The filter formats the record itself (message plus sanitized traceback) and
clears `args` and `exc_info`, so no later formatter can add the raw text back.
It is attached to the handlers in `settings.LOGGING`, so it sees records from
every logger, Django's own included.
"""

import logging
import re
import traceback

# %-escapes too, so /%64isplay/ can't slip past the rule.
_DISPLAY = re.compile(
    r"((?:/|%2f)+(?:d|%64)(?:i|%69)(?:s|%73)(?:p|%70)(?:l|%6c)(?:a|%61)(?:y|%79)"
    r"(?:/|%2f)+)[^/?#\s'\"]+",
    re.IGNORECASE,
)

# Exceptions whose messages are fixed copy written in this codebase.
ALLOWED_MESSAGES = frozenset({"conference_drawing.raffle.draw.DrawingError"})


def redact_paths(text: str) -> str:
    """Replace the token in every display path in `text` with REDACTED."""
    return _DISPLAY.sub(r"\1REDACTED", text)


_CAUSE = "The above exception was the direct cause of the following exception:"
_CONTEXT = "During handling of the above exception, another exception occurred:"


def _describe(exc: BaseException) -> str:
    cls = type(exc)
    name = f"{cls.__module__}.{cls.__qualname__}"
    if name in ALLOWED_MESSAGES:
        return f"{cls.__qualname__}: {exc}"
    return cls.__qualname__


def sanitized_traceback(exc: BaseException) -> str:
    """Format `exc` and its chain like `traceback`, minus every message.

    Walks `__cause__` and `__context__` (honoring `__suppress_context__`) the
    way the interpreter prints them, oldest first. Notes are dropped too.
    """
    chain: list[tuple[BaseException, str]] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    link = ""
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append((current, link))
        if current.__cause__ is not None:
            current, link = current.__cause__, _CAUSE
        elif current.__context__ is not None and not current.__suppress_context__:
            current, link = current.__context__, _CONTEXT
        else:
            current = None
    parts: list[str] = []
    for item, following in reversed(chain):
        parts.append("Traceback (most recent call last):\n")
        parts.extend(traceback.format_tb(item.__traceback__))
        parts.append(_describe(item) + "\n")
        if following:
            parts.append(f"\n{following}\n\n")
    return "".join(parts).rstrip("\n")


class RedactFilter(logging.Filter):
    """Redact display tokens and exception text from every record it passes."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            message = f"{message}\n{sanitized_traceback(record.exc_info[1])}"
        record.msg = redact_paths(message)
        record.args = None
        record.exc_info = None
        record.exc_text = None
        return True
