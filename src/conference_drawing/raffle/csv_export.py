"""Entrant export: build one `CsvRow` per entrant and write them as CSV.

The owner downloads this to work the list by hand (instead of, or alongside,
the Drip hand-off). Two concerns shape the module:

* Spreadsheet formula injection. A name or answer like `=HYPERLINK(...)` would
  run as a formula when opened in Excel or Sheets, and entrants type these
  values, so every string cell that starts with `=`, `+`, `-`, `@`, tab or CR
  gets a leading apostrophe (`neutralize`).
* Winner semantics. An entrant is a winner when they hold a `claimed` or
  `digital` pick in this conference; pending, not-here and void picks do not
  count.

Row data is never logged.
"""

import csv
import datetime
from typing import Literal, TextIO

from django.db.models import Prefetch
from pydantic import BaseModel, Field

from .codes import render_prize_url
from .models import Conference, Entrant, Pick, PrizeLine, Question
from .timezones import to_local

_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
_WINNING = (Pick.Status.CLAIMED, Pick.Status.DIGITAL)
_JOIN = "; "


def neutralize(value: str) -> str:
    """Prefix a cell that a spreadsheet would read as a formula with `'`.

    Use on every string written to the CSV: names, emails, answers, titles,
    codes and URLs are all influenced by entrants or by pasted admin input.
    """
    return "'" + value if value.startswith(_FORMULA_STARTS) else value


class CsvRow(BaseModel):
    """One entrant's export row, before formula neutralization."""

    name: str
    email: str
    entered_at: str
    answers: dict[str, str] = Field(default_factory=dict)
    status: Literal["winner", "nonwinner"]
    won: list[str] = Field(default_factory=list)
    digital_codes: list[str] = Field(default_factory=list)
    digital_urls: list[str] = Field(default_factory=list)
    followup_ack_at: str
    bd_optin: Literal["yes", "no"]
    bd_optin_at: str


def _iso(value: object, tz: str) -> str:
    """Local ISO 8601 with offset in the conference zone; empty when absent."""
    if not isinstance(value, datetime.datetime):
        return ""
    return to_local(value, tz).isoformat(timespec="seconds")


def _prize_cells(pick: Pick) -> tuple[str, str, str]:
    """Return (title, code, url) for one winning pick; code/url may be empty."""
    line: PrizeLine = pick.prize_line
    code = ""
    if line.kind == PrizeLine.Kind.DIGITAL:
        coupon = getattr(pick, "couponcode", None)
        if coupon is not None:
            code = coupon.code
        elif line.code_mode == PrizeLine.CodeMode.SHARED:
            code = line.shared_code
    url = (
        render_prize_url(line.url_template, code) if code and line.url_template else ""
    )
    return line.book.title, code, url


def export_rows(conference: Conference) -> list[CsvRow]:
    """Build one row per entrant, in entry order, with a fixed number of queries."""
    tz = conference.time_zone
    winning = Pick.objects.filter(status__in=_WINNING).select_related(
        "prize_line__book", "couponcode"
    )
    entrants = (
        Entrant.objects.filter(conference=conference)
        .order_by("created_at", "id")
        .prefetch_related(
            Prefetch(
                "pick_set",
                queryset=winning.order_by("drawn_at", "id"),
                to_attr="winning_picks",
            )
        )
    )
    rows: list[CsvRow] = []
    for entrant in entrants:
        picks: list[Pick] = entrant.winning_picks
        cells = [_prize_cells(p) for p in picks]
        raw = entrant.answers if isinstance(entrant.answers, dict) else {}
        rows.append(
            CsvRow(
                name=entrant.name,
                email=entrant.email,
                entered_at=_iso(entrant.created_at, tz),
                answers={str(k): str(v) for k, v in raw.items()},
                status="winner" if picks else "nonwinner",
                won=[title for title, _, _ in cells],
                digital_codes=[c for _, c, _ in cells if c],
                digital_urls=[u for _, _, u in cells if u],
                followup_ack_at=_iso(entrant.followup_ack_at, tz),
                bd_optin="yes" if entrant.bd_optin_at else "no",
                bd_optin_at=_iso(entrant.bd_optin_at, tz),
            )
        )
    return rows


def write_csv(rows: list[CsvRow], questions: list[Question], out: TextIO) -> None:
    """Write `rows` to `out` as CSV, one answer column per question in order.

    Columns: name, email, entered_at, <question keys by position>, status, won,
    digital_codes, digital_urls, followup_ack_at, bd_optin, bd_optin_at.
    `out` should be opened with `newline=""` so embedded line breaks survive.
    """
    keys = [q.key for q in sorted(questions, key=lambda q: (q.position, q.id))]
    writer = csv.writer(out)
    writer.writerow(
        [
            "name",
            "email",
            "entered_at",
            *keys,
            "status",
            "won",
            "digital_codes",
            "digital_urls",
            "followup_ack_at",
            "bd_optin",
            "bd_optin_at",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                neutralize(cell)
                for cell in (
                    row.name,
                    row.email,
                    row.entered_at,
                    *(row.answers.get(k, "") for k in keys),
                    row.status,
                    _JOIN.join(row.won),
                    _JOIN.join(row.digital_codes),
                    _JOIN.join(row.digital_urls),
                    row.followup_ack_at,
                    row.bd_optin,
                    row.bd_optin_at,
                )
            ]
        )
