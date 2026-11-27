"""Eligibility rules: who may be drawn in a drawing."""

import datetime
from collections.abc import Callable
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.extra.django import TestCase

from conference_drawing.raffle.eligibility import eligible_entrant_ids
from conference_drawing.raffle.models import Conference, Drawing, Entrant, Pick
from tests import factories

CUTOFF = datetime.datetime(2027, 5, 11, 20, 0, tzinfo=datetime.UTC)
BEFORE = CUTOFF - datetime.timedelta(minutes=5)
S = Pick.Status


@pytest.fixture
def conf(conference_factory: Callable[..., Conference]) -> Conference:
    return conference_factory(exclusion_emails="Staff@X.com, host@x.com")


@pytest.fixture
def drawing(conf: Conference, drawing_factory: Callable[..., Drawing]) -> Drawing:
    return drawing_factory(conf, cutoff_at=CUTOFF)


def test_excludes_staff_past_winners_and_late_entries(
    conf: Conference,
    drawing: Drawing,
    drawing_factory: Callable[..., Drawing],
    prize_line_factory: Callable[..., Any],
    entrant_factory: Callable[..., Entrant],
    pick_factory: Callable[..., Pick],
) -> None:
    ok = entrant_factory(conf, created_at=BEFORE)
    entrant_factory(conf, email="host@x.com", created_at=BEFORE)
    entrant_factory(conf, created_at=CUTOFF + datetime.timedelta(seconds=1))
    past = entrant_factory(conf, created_at=BEFORE)
    earlier = drawing_factory(conf, cutoff_at=BEFORE)
    pick_factory(prize_line_factory(earlier), past, status=S.CLAIMED)
    assert eligible_entrant_ids(drawing) == [ok.pk]


def test_exclusion_matches_case_insensitively(
    conf: Conference, drawing: Drawing, entrant_factory: Callable[..., Entrant]
) -> None:
    entrant_factory(conf, email="STAFF@x.com", created_at=BEFORE)
    assert eligible_entrant_ids(drawing) == []


def test_exclusion_also_matches_plus_aliases_and_idna(
    conf: Conference, drawing: Drawing, entrant_factory: Callable[..., Entrant]
) -> None:
    entrant_factory(conf, email="staff+raffle@X.com", created_at=BEFORE)
    assert eligible_entrant_ids(drawing) == []


def test_entry_at_cutoff_is_excluded(
    conf: Conference, drawing: Drawing, entrant_factory: Callable[..., Entrant]
) -> None:
    entrant_factory(conf, created_at=CUTOFF)
    assert eligible_entrant_ids(drawing) == []


def test_not_here_in_this_drawing_excluded_but_eligible_next(
    conf: Conference,
    drawing: Drawing,
    drawing_factory: Callable[..., Drawing],
    prize_line_factory: Callable[..., Any],
    entrant_factory: Callable[..., Entrant],
    pick_factory: Callable[..., Pick],
) -> None:
    e = entrant_factory(conf, created_at=BEFORE)
    pick_factory(prize_line_factory(drawing), e, status=S.NOT_HERE)
    assert eligible_entrant_ids(drawing) == []
    nxt = drawing_factory(conf, cutoff_at=CUTOFF)
    assert eligible_entrant_ids(nxt) == [e.pk]


def test_void_pick_does_not_exclude(
    conf: Conference,
    drawing: Drawing,
    prize_line_factory: Callable[..., Any],
    entrant_factory: Callable[..., Entrant],
    pick_factory: Callable[..., Pick],
) -> None:
    e = entrant_factory(conf, created_at=BEFORE)
    pick_factory(prize_line_factory(drawing), e, status=S.VOID)
    assert eligible_entrant_ids(drawing) == [e.pk]


def test_other_conference_entrants_never_included(
    conf: Conference,
    drawing: Drawing,
    conference_factory: Callable[..., Conference],
    entrant_factory: Callable[..., Entrant],
) -> None:
    entrant_factory(conference_factory(), created_at=BEFORE)
    assert eligible_entrant_ids(drawing) == []


def test_requires_cutoff(
    conf: Conference, drawing_factory: Callable[..., Drawing]
) -> None:
    with pytest.raises(ValueError, match="cutoff"):
        eligible_entrant_ids(drawing_factory(conf))


def test_results_sorted_ascending(
    conf: Conference, drawing: Drawing, entrant_factory: Callable[..., Entrant]
) -> None:
    ids = [entrant_factory(conf, created_at=BEFORE).pk for _ in range(4)]
    assert eligible_entrant_ids(drawing) == sorted(ids)


FLAGS = [
    "plain",
    "excluded",
    "won_earlier_claimed",
    "won_earlier_digital",
    "late",
    "pending_here",
    "claimed_here",
    "not_here_here",
    "digital_here",
    "voided_here",
    "other_conference",
]
ELIGIBLE = {"plain", "voided_here"}
HERE = {
    "pending_here": S.PENDING,
    "claimed_here": S.CLAIMED,
    "not_here_here": S.NOT_HERE,
    "digital_here": S.DIGITAL,
    "voided_here": S.VOID,
}


def entrant_population_strategy() -> st.SearchStrategy[list[str]]:
    """A list of flags, one per entrant to create."""
    return st.lists(st.sampled_from(FLAGS), max_size=15)


class EligibilityProperty(TestCase):
    @settings(deadline=None, max_examples=25)
    @given(population=entrant_population_strategy())
    def test_property_returns_exactly_plain_and_voided(
        self, population: list[str]
    ) -> None:
        conf = factories.make_conference(
            exclusion_emails=" ".join(f"staff{i}@x.com" for i in range(15))
        )
        other = factories.make_conference()
        earlier = factories.make_drawing(conf, cutoff_at=BEFORE)
        drawing = factories.make_drawing(conf, cutoff_at=CUTOFF)
        line_earlier = factories.make_prize_line(earlier, quantity=20)
        line_here = factories.make_prize_line(drawing, quantity=20)
        expected: list[int] = []
        for i, flag in enumerate(population):
            kwargs: dict[str, Any] = {"created_at": BEFORE}
            if flag == "excluded":
                kwargs["email"] = f"Staff{i}@X.com"
            if flag == "late":
                kwargs["created_at"] = CUTOFF + datetime.timedelta(seconds=i % 2 * i)
            entrant = factories.make_entrant(
                other if flag == "other_conference" else conf, **kwargs
            )
            if flag in ELIGIBLE:
                expected.append(entrant.pk)
            if flag.startswith("won_earlier"):
                status = S.CLAIMED if flag.endswith("claimed") else S.DIGITAL
                factories.make_pick(line_earlier, entrant, status=status, slot=i + 1)
            if flag in HERE:
                factories.make_pick(line_here, entrant, status=HERE[flag], slot=i + 1)
        assert eligible_entrant_ids(drawing) == sorted(expected)
