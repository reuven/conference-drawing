"""What we send to Drip: typed payloads that serialize without nulls or `status`.

Keeping the wire format in pydantic models means a bad email or a half-set
consent record fails when we build the payload, not as a 422 from Drip hours
later. `status` is deliberately not a field on `SubscriberPayload`: sending it
would let an upsert re-subscribe someone who unsubscribed.
"""

import datetime
import unicodedata
from typing import Any, Literal, Self

from pydantic import BaseModel, EmailStr, field_validator, model_validator

from ..codes import render_prize_url
from ..models import CouponCode, Entrant, Pick, PrizeLine

FIRST_NAME_MAX = 40
_FIRST_NAME_PUNCTUATION = " '-."


def safe_first_name(name: str) -> str | None:
    """Return `name` stripped if it is safe to hand to Drip, else None.

    Drip merges the first name into emails, so only letters (Unicode L*),
    combining marks (M*), space, apostrophe, hyphen and period are allowed, 1 to
    40 characters after stripping. Everything else, including bidi and
    zero-width controls (Cf), makes the name unusable and the field is left
    out rather than sent empty or cleaned up.
    """
    cleaned = name.strip()
    if not 1 <= len(cleaned) <= FIRST_NAME_MAX:
        return None
    if all(
        c in _FIRST_NAME_PUNCTUATION or unicodedata.category(c)[0] in "LM"
        for c in cleaned
    ):
        return cleaned
    return None


class EventPayload(BaseModel):
    """One Drip custom event for one subscriber."""

    email: EmailStr
    action: Literal["Won raffle prize", "Entered raffle"]
    properties: dict[str, str]
    occurred_at: datetime.datetime

    def to_json(self) -> dict[str, Any]:
        """Return the JSON-ready dict Drip expects, with a `Z`-suffixed UTC time."""
        return self.model_dump(mode="json", exclude_none=True)


class SubscriberPayload(BaseModel):
    """One subscriber to create or update, with tags and optional EU consent."""

    email: EmailStr
    first_name: str | None = None
    tags: list[str]
    eu_consent: Literal["granted"] | None = None
    eu_consent_message: str | None = None

    @field_validator("first_name")
    @classmethod
    def _first_name_is_safe(cls, value: str | None) -> str | None:
        """Drop an unsafe name, including from jobs queued before this check."""
        return None if value is None else safe_first_name(value)

    @model_validator(mode="after")
    def _consent_is_both_or_neither(self) -> Self:
        """Drip needs the consent wording alongside a grant, so never send one alone."""
        if (self.eu_consent is None) != (self.eu_consent_message is None):
            raise ValueError("eu_consent and eu_consent_message go together")
        return self

    def to_json(self) -> dict[str, Any]:
        """Return the JSON-ready dict Drip expects, omitting unset fields."""
        return self.model_dump(mode="json", exclude_none=True)

    @classmethod
    def from_entrant(
        cls,
        entrant: Entrant,
        *,
        tags: list[str],
        consent_message: str | None = None,
    ) -> Self:
        """Build a payload from an entrant, using the first word of their name.

        A blank or whitespace-only name (the form requires one, but admin edits
        and old rows may not) leaves `first_name` unset (the key is omitted, so an
        existing first name in Drip is not wiped) rather than raising. A first
        word that `safe_first_name` refuses is treated the same way.
        Pass `consent_message` only for an entrant who opted in: it records the
        EU consent grant with that wording. Without it no consent fields are set.
        """
        words = entrant.name.split()
        return cls(
            email=entrant.email,
            first_name=safe_first_name(words[0]) if words else None,
            tags=tags,
            eu_consent="granted" if consent_message is not None else None,
            eu_consent_message=consent_message,
        )


def won_prize_event(pick: Pick) -> EventPayload:
    """Build the `Won raffle prize` event for a digital pick.

    The code is the pick's pooled code, else the line's shared code, else empty.
    The prize URL is rendered with the same percent-encoding the booth uses.
    """
    line: PrizeLine = pick.prize_line
    assigned = CouponCode.objects.filter(pick=pick).values_list("code", flat=True)
    code = assigned.first() or (
        line.shared_code if line.code_mode == PrizeLine.CodeMode.SHARED else ""
    )
    return EventPayload(
        email=pick.entrant.email,
        action="Won raffle prize",
        properties={
            "book_title": line.book.title,
            "coupon_code": code,
            "prize_url": render_prize_url(line.url_template, code),
            "conference": line.drawing.conference.name,
        },
        occurred_at=pick.drawn_at,
    )
