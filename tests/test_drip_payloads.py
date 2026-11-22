"""Drip payload models: what we send, and what we never send."""

import datetime
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from conference_drawing.raffle.drip.payloads import (
    EventPayload,
    SubscriberPayload,
    won_prize_event,
)
from conference_drawing.raffle.models import (
    Conference,
    CouponCode,
    Drawing,
    Entrant,
    Pick,
    PrizeLine,
)

WHEN = datetime.datetime(2027, 5, 11, 21, 0, tzinfo=datetime.UTC)


def _digital_pick(
    conference_factory: Callable[..., Conference],
    drawing_factory: Callable[..., Drawing],
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
    pick_factory: Callable[..., Pick],
    **line: Any,
) -> Pick:
    conference = conference_factory(name="PyCon US")
    prize = prize_line_factory(
        drawing_factory(conference), kind=PrizeLine.Kind.DIGITAL, **line
    )
    return pick_factory(
        prize,
        entrant_factory(conference, email="Winner@Example.com"),
        status=Pick.Status.DIGITAL,
        drawn_at=WHEN,
    )


def test_won_prize_event_properties(
    conference_factory: Callable[..., Conference],
    drawing_factory: Callable[..., Drawing],
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
    pick_factory: Callable[..., Pick],
) -> None:
    pick = _digital_pick(
        conference_factory,
        drawing_factory,
        prize_line_factory,
        entrant_factory,
        pick_factory,
        url_template="https://example.com/redeem?c={code}",
        code_mode=PrizeLine.CodeMode.POOL,
    )
    CouponCode.objects.create(prize_line=pick.prize_line, code="A&B", pick=pick)
    event = won_prize_event(pick)
    assert event.email == "winner@example.com"
    assert event.action == "Won raffle prize"
    assert event.occurred_at == WHEN
    assert event.properties == {
        "book_title": pick.prize_line.book.title,
        "coupon_code": "A&B",
        "prize_url": "https://example.com/redeem?c=A%26B",
        "conference": "PyCon US",
    }


def test_won_prize_event_uses_shared_code(
    conference_factory: Callable[..., Conference],
    drawing_factory: Callable[..., Drawing],
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
    pick_factory: Callable[..., Pick],
) -> None:
    pick = _digital_pick(
        conference_factory,
        drawing_factory,
        prize_line_factory,
        entrant_factory,
        pick_factory,
        url_template="https://example.com/s",
        code_mode=PrizeLine.CodeMode.SHARED,
        shared_code="SHARED7",
    )
    assert won_prize_event(pick).properties["coupon_code"] == "SHARED7"


def test_won_prize_event_without_codes_has_empty_code(
    conference_factory: Callable[..., Conference],
    drawing_factory: Callable[..., Drawing],
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
    pick_factory: Callable[..., Pick],
) -> None:
    pick = _digital_pick(
        conference_factory,
        drawing_factory,
        prize_line_factory,
        entrant_factory,
        pick_factory,
        url_template="https://example.com/s",
    )
    props = won_prize_event(pick).properties
    assert props["coupon_code"] == ""
    assert props["prize_url"] == "https://example.com/s"


def test_event_payload_serializes_iso_and_roundtrips() -> None:
    event = EventPayload(
        email="a@example.com",
        action="Entered raffle",
        properties={"conference": "X"},
        occurred_at=WHEN,
    )
    data = event.to_json()
    assert data["occurred_at"] == "2027-05-11T21:00:00Z"
    assert EventPayload.model_validate(data) == event


def test_event_rejects_unknown_action_and_bad_email() -> None:
    with pytest.raises(ValidationError):
        EventPayload.model_validate(
            {"email": "a@example.com", "action": "Other", "occurred_at": WHEN}
        )
    with pytest.raises(ValidationError):
        EventPayload(
            email="not-an-email",
            action="Entered raffle",
            properties={},
            occurred_at=WHEN,
        )


def test_subscriber_payload_never_has_status() -> None:
    sub = SubscriberPayload(email="a@example.com", first_name="Ann", tags=["t"])
    data = sub.to_json()
    assert "status" not in data
    assert data == {"email": "a@example.com", "first_name": "Ann", "tags": ["t"]}
    assert "status" not in SubscriberPayload.model_fields


def test_eu_consent_only_with_message() -> None:
    with pytest.raises(ValidationError):
        SubscriberPayload(
            email="a@example.com", first_name="A", tags=[], eu_consent="granted"
        )
    with pytest.raises(ValidationError):
        SubscriberPayload(
            email="a@example.com", first_name="A", tags=[], eu_consent_message="ok"
        )
    both = SubscriberPayload(
        email="a@example.com",
        first_name="A",
        tags=[],
        eu_consent="granted",
        eu_consent_message="I agreed",
    ).to_json()
    assert both["eu_consent"] == "granted"
    assert both["eu_consent_message"] == "I agreed"


def test_subscriber_from_entrant_uses_first_name_and_tags(
    conference_factory: Callable[..., Conference],
    entrant_factory: Callable[..., Entrant],
) -> None:
    conference = conference_factory(slug="pycon")
    entrant = entrant_factory(conference, name="Ada Lovelace", email="Ada@Example.com")
    sub = SubscriberPayload.from_entrant(entrant, tags=[conference.tags().all])
    assert sub.first_name == "Ada"
    assert sub.email == "ada@example.com"
    assert sub.tags == ["pycon-raffle"]
