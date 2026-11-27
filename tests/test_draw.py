"""Drawing service: start, claim, not-here redraw, undo, close."""

import datetime
import functools
import json
import random
import threading
from collections import Counter
from collections.abc import Callable
from typing import Any

import pytest
from django.db import connection, transaction
from hypothesis import example, given, settings
from hypothesis import strategies as st
from hypothesis.extra.django import TestCase

from conference_drawing.raffle.draw import (
    EARLY_START_WINDOW,
    DrawingError,
    close_drawing,
    mark_claimed,
    mark_not_here,
    start_drawing,
    undo_last,
    undo_rule,
)
from conference_drawing.raffle.eligibility import eligible_entrant_ids
from conference_drawing.raffle.models import (
    AuditEvent,
    Conference,
    CouponCode,
    Drawing,
    Entrant,
    Pick,
    PrizeLine,
)
from conference_drawing.raffle.wrapup import wrap_up
from tests import factories

NOW = datetime.datetime(2027, 5, 11, 20, 30, tzinfo=datetime.UTC)
LATER = NOW + datetime.timedelta(minutes=1)
SCHEDULED = NOW + datetime.timedelta(minutes=10)
BEFORE = NOW - datetime.timedelta(hours=1)
S = Pick.Status
K = AuditEvent.Kind
PHYS = PrizeLine.Kind.PHYSICAL
DIG = PrizeLine.Kind.DIGITAL


def rng() -> random.Random:
    return random.Random(0)


def digital_line(drawing: Drawing, **kwargs: Any) -> PrizeLine:
    defaults: dict[str, Any] = {
        "kind": DIG,
        "url_template": "https://example.com/{code}",
        "code_mode": PrizeLine.CodeMode.SHARED,
        "shared_code": "SHARED1",
    }
    return factories.make_prize_line(drawing, **{**defaults, **kwargs})


def add_codes(line: PrizeLine, n: int) -> list[CouponCode]:
    return [
        CouponCode.objects.create(prize_line=line, code=f"CODE{line.pk}-{i}")
        for i in range(n)
    ]


def kinds(drawing: Drawing) -> list[str]:
    return list(
        AuditEvent.objects.filter(drawing=drawing)
        .order_by("id")
        .values_list("kind", flat=True)
    )


@pytest.fixture
def conf(conference_factory: Callable[..., Conference]) -> Conference:
    return conference_factory()


@pytest.fixture
def drawing(conf: Conference, drawing_factory: Callable[..., Drawing]) -> Drawing:
    return drawing_factory(conf, scheduled_at=SCHEDULED)


@pytest.fixture
def entrants(conf: Conference, entrant_factory: Callable[..., Entrant]) -> list[int]:
    return [entrant_factory(conf, created_at=BEFORE).pk for _ in range(6)]


def started_single(drawing: Drawing, quantity: int = 1) -> list[Pick]:
    factories.make_prize_line(drawing, quantity=quantity)
    return start_drawing(drawing, now=NOW, rng=rng())


def dismiss_pending(drawing: Drawing) -> None:
    """Mark Not here on every pending pick (and each redraw) until none remain."""
    pending = Pick.objects.filter(prize_line__drawing=drawing, status=S.PENDING)
    while p := pending.first():
        mark_not_here(p, now=LATER, rng=rng())


# --- start -------------------------------------------------------------------


def test_start_refused_more_than_window_early(conf: Conference) -> None:
    d = factories.make_drawing(
        conf, scheduled_at=NOW + EARLY_START_WINDOW + datetime.timedelta(minutes=1)
    )
    factories.make_prize_line(d)
    with pytest.raises(DrawingError, match=r"can start at .*\d:\d\d [AP]M"):
        start_drawing(d, now=NOW, rng=rng())
    d.refresh_from_db()
    assert d.status == Drawing.Status.SCHEDULED
    assert not AuditEvent.objects.filter(drawing=d).exists()


def test_start_message_names_local_time(conf: Conference) -> None:
    # SCHEDULED is 20:40 UTC, 1:40 PM in America/Los_Angeles (PDT); plus 15 min.
    d = factories.make_drawing(conf, scheduled_at=SCHEDULED + EARLY_START_WINDOW)
    with pytest.raises(DrawingError) as excinfo:
        start_drawing(d, now=NOW - EARLY_START_WINDOW, rng=rng())
    assert str(excinfo.value) == "This drawing can start at 1:55 PM."


def test_start_allowed_exactly_at_window_edge(conf: Conference) -> None:
    d = factories.make_drawing(conf, scheduled_at=NOW + EARLY_START_WINDOW)
    factories.make_prize_line(d)
    start_drawing(d, now=NOW, rng=rng())
    d.refresh_from_db()
    assert d.status == Drawing.Status.OPEN


def test_early_ok_overrides_the_window(conf: Conference) -> None:
    d = factories.make_drawing(conf, scheduled_at=NOW + datetime.timedelta(hours=2))
    factories.make_prize_line(d)
    start_drawing(d, now=NOW, rng=rng(), early_ok=True)
    d.refresh_from_db()
    assert d.status == Drawing.Status.OPEN
    assert d.cutoff_at == NOW


@pytest.mark.parametrize(
    ("scheduled", "expected"), [(SCHEDULED, NOW), (BEFORE, BEFORE)]
)
def test_start_sets_cutoff_to_earlier_of_scheduled_and_now(
    conf: Conference,
    drawing_factory: Callable[..., Drawing],
    scheduled: datetime.datetime,
    expected: datetime.datetime,
) -> None:
    d = drawing_factory(conf, scheduled_at=scheduled)
    factories.make_prize_line(d)
    start_drawing(d, now=NOW, rng=rng())
    d.refresh_from_db()
    assert d.status == Drawing.Status.OPEN
    assert d.started_at == NOW
    assert d.cutoff_at == expected


def test_lines_drawn_in_position_order_and_slots_numbered(
    drawing: Drawing, entrants: list[int]
) -> None:
    second = factories.make_prize_line(drawing, quantity=2, position=2)
    first = factories.make_prize_line(drawing, quantity=3, position=1)
    picks = start_drawing(drawing, now=NOW, rng=rng())
    assert [(p.prize_line_id, p.slot) for p in picks] == [
        (first.pk, 1),
        (first.pk, 2),
        (first.pk, 3),
        (second.pk, 1),
        (second.pk, 2),
    ]
    assert [p.pool_size for p in picks] == [6, 5, 4, 3, 2]
    assert len({p.entrant_id for p in picks}) == 5
    assert all(p.status == S.PENDING and p.drawn_at == NOW for p in picks)
    assert kinds(drawing) == [K.STARTED] + [K.PICKED] * 5


def test_seeded_rng_chooses_from_sorted_eligible_ids(
    drawing: Drawing, entrants: list[int]
) -> None:
    [pick] = started_single(drawing)
    assert pick.entrant_id == random.Random(0).choice(sorted(entrants))


def test_digital_picks_get_distinct_pool_codes(
    drawing: Drawing, entrants: list[int]
) -> None:
    line = digital_line(
        drawing, quantity=3, code_mode=PrizeLine.CodeMode.POOL, shared_code=""
    )
    codes = add_codes(line, 4)
    picks = start_drawing(drawing, now=NOW, rng=rng())
    assert all(p.status == S.DIGITAL for p in picks)
    assigned = {c.pk: c.pick_id for c in CouponCode.objects.all()}
    assert assigned == {
        codes[0].pk: picks[0].pk,
        codes[1].pk: picks[1].pk,
        codes[2].pk: picks[2].pk,
        codes[3].pk: None,
    }


def test_shared_code_line_assigns_no_coupon_rows(
    drawing: Drawing, entrants: list[int]
) -> None:
    digital_line(drawing, quantity=2)
    picks = start_drawing(drawing, now=NOW, rng=rng())
    assert [p.status for p in picks] == [S.DIGITAL, S.DIGITAL]
    assert not CouponCode.objects.exists()


def test_start_refuses_when_codes_short(
    drawing: Drawing, entrants: list[int], book_factory: Callable[..., Any]
) -> None:
    line = digital_line(
        drawing,
        book=book_factory(title="Pandas Workout"),
        quantity=3,
        code_mode=PrizeLine.CodeMode.POOL,
        shared_code="",
    )
    codes = add_codes(line, 3)
    codes[0].pick = factories.make_pick(line, Entrant.objects.get(pk=entrants[0]))
    codes[0].save()
    with pytest.raises(DrawingError, match=r"^Pandas Workout: needs 3 codes, has 2\.$"):
        start_drawing(drawing, now=NOW, rng=rng())
    drawing.refresh_from_db()
    assert drawing.status == Drawing.Status.SCHEDULED
    assert not AuditEvent.objects.exists()


def test_start_refuses_second_open_drawing(
    conf: Conference, drawing: Drawing, drawing_factory: Callable[..., Drawing]
) -> None:
    drawing_factory(conf, status=Drawing.Status.OPEN)
    factories.make_prize_line(drawing)
    with pytest.raises(DrawingError, match="Another drawing is open"):
        start_drawing(drawing, now=NOW, rng=rng())


def test_open_drawing_in_other_conference_does_not_block(
    drawing: Drawing, entrants: list[int], drawing_factory: Callable[..., Drawing]
) -> None:
    drawing_factory(factories.make_conference(), status=Drawing.Status.OPEN)
    assert len(started_single(drawing)) == 1


def test_start_refuses_already_started(drawing: Drawing, entrants: list[int]) -> None:
    started_single(drawing)
    with pytest.raises(DrawingError, match="already started"):
        start_drawing(drawing, now=LATER, rng=rng())


def test_pool_smaller_than_quantity_leaves_slots_unawarded(
    conf: Conference, drawing: Drawing, entrant_factory: Callable[..., Entrant]
) -> None:
    ids = {entrant_factory(conf, created_at=BEFORE).pk for _ in range(2)}
    picks = started_single(drawing, quantity=3)
    assert {p.entrant_id for p in picks} == ids
    assert [p.slot for p in picks] == [1, 2]
    exhausted = AuditEvent.objects.get(kind=K.EXHAUSTED)
    assert exhausted.detail == {"prize_line": picks[0].prize_line_id, "slot": 3}
    drawing.refresh_from_db()
    assert drawing.status == Drawing.Status.OPEN


def test_digital_only_drawing_finishes_at_start(
    drawing: Drawing, entrants: list[int]
) -> None:
    digital_line(drawing, quantity=2)
    start_drawing(drawing, now=NOW, rng=rng())
    drawing.refresh_from_db()
    assert drawing.status == Drawing.Status.FINISHED
    assert drawing.closed_at == NOW
    assert drawing.awarded_count == 2


def test_on_digital_pick_called_once_per_digital_pick(
    drawing: Drawing, entrants: list[int]
) -> None:
    factories.make_prize_line(drawing, quantity=2)
    digital_line(drawing, quantity=2, position=1)
    seen: list[int] = []
    picks = start_drawing(
        drawing, now=NOW, rng=rng(), on_digital_pick=lambda p: seen.append(p.pk)
    )
    assert seen == [p.pk for p in picks if p.status == S.DIGITAL]
    assert len(seen) == 2


def test_on_digital_pick_failure_rolls_back_the_pick(
    drawing: Drawing, entrants: list[int]
) -> None:
    digital_line(drawing)

    def boom(pick: Pick) -> None:
        raise RuntimeError("outbox down")

    with pytest.raises(RuntimeError):
        start_drawing(drawing, now=NOW, rng=rng(), on_digital_pick=boom)
    assert not Pick.objects.exists()
    drawing.refresh_from_db()
    assert drawing.status == Drawing.Status.SCHEDULED


# --- claim / not here ----------------------------------------------------------


def test_claiming_last_physical_finishes_drawing(
    drawing: Drawing, entrants: list[int]
) -> None:
    digital_line(drawing, position=1)
    first, second, _ = started_single(drawing, quantity=2)
    claimed = mark_claimed(first, now=LATER)
    assert claimed.status == S.CLAIMED
    assert claimed.marked_at == LATER
    drawing.refresh_from_db()
    assert drawing.status == Drawing.Status.OPEN
    mark_claimed(second, now=LATER)
    drawing.refresh_from_db()
    assert drawing.status == Drawing.Status.FINISHED
    assert drawing.closed_at == LATER
    assert drawing.awarded_count == 3
    assert drawing.closed_by_owner is False
    assert K.CLOSED not in kinds(drawing)


def test_claim_twice_refused(drawing: Drawing, entrants: list[int]) -> None:
    pick = started_single(drawing, quantity=2)[0]
    mark_claimed(pick, now=LATER)
    with pytest.raises(DrawingError, match=r"^Already marked\.$"):
        mark_claimed(pick, now=LATER)


@pytest.mark.parametrize("status", [S.DIGITAL, S.VOID])
def test_marking_digital_or_void_pick_refused(
    drawing: Drawing, entrants: list[int], status: Pick.Status
) -> None:
    pick = started_single(drawing, quantity=2)[0]
    Pick.objects.filter(pk=pick.pk).update(status=status)
    with pytest.raises(DrawingError, match=r"^This pick can't be marked\.$"):
        mark_claimed(pick, now=LATER)
    with pytest.raises(DrawingError, match=r"^This pick can't be marked\.$"):
        mark_not_here(pick, now=LATER, rng=rng())


def test_marking_requires_open_drawing(drawing: Drawing, entrants: list[int]) -> None:
    line = factories.make_prize_line(drawing)
    pick = factories.make_pick(line, Entrant.objects.get(pk=entrants[0]))
    with pytest.raises(DrawingError, match="isn't open"):
        mark_claimed(pick, now=LATER)
    with pytest.raises(DrawingError, match="isn't open"):
        mark_not_here(pick, now=LATER, rng=rng())


def test_not_here_draws_replacement_and_keeps_person_eligible_later(
    conf: Conference, drawing: Drawing, entrants: list[int]
) -> None:
    [pick] = started_single(drawing)
    replacement = mark_not_here(pick, now=LATER, rng=rng())
    assert replacement is not None
    pick.refresh_from_db()
    assert pick.status == S.NOT_HERE
    assert pick.marked_at == LATER
    assert replacement.replaces_id == pick.pk
    assert replacement.slot == pick.slot
    assert replacement.status == S.PENDING
    assert replacement.pool_size == 5
    assert replacement.entrant_id != pick.entrant_id
    drawing.refresh_from_db()
    assert pick.entrant_id not in eligible_entrant_ids(drawing)
    nxt = factories.make_drawing(conf, cutoff_at=NOW)
    assert pick.entrant_id in eligible_entrant_ids(nxt)
    assert kinds(drawing)[-2:] == [K.NOT_HERE, K.PICKED]


def test_not_here_twice_draws_one_replacement(
    drawing: Drawing, entrants: list[int]
) -> None:
    [pick] = started_single(drawing)
    stale_a = Pick.objects.get(pk=pick.pk)
    stale_b = Pick.objects.get(pk=pick.pk)
    assert mark_not_here(stale_a, now=LATER, rng=rng()) is not None
    with pytest.raises(DrawingError, match=r"^Already marked\.$"):
        mark_not_here(stale_b, now=LATER, rng=rng())
    assert Pick.objects.filter(replaces=pick).count() == 1
    assert Pick.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_concurrent_not_here_from_two_phones_draws_one_replacement(
    conf: Conference, drawing: Drawing, entrants: list[int]
) -> None:
    """Two threads, two PostgreSQL connections: the second waits its turn.

    The drawing's `select_for_update()` row lock makes the loser block until the
    winner commits; it then re-reads the pick, sees it marked, and refuses.
    """
    [pick] = started_single(drawing)
    barrier = threading.Barrier(2)
    outcomes: list[object] = []

    def tap() -> None:
        stale = Pick.objects.get(pk=pick.pk)
        try:
            barrier.wait()
            outcomes.append(mark_not_here(stale, now=LATER, rng=rng()))
        except Exception as exc:  # recorded for the assertion below
            outcomes.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=tap) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    errors = [o for o in outcomes if isinstance(o, Exception)]
    replacements = [o for o in outcomes if isinstance(o, Pick)]
    assert len(replacements) == 1
    assert [type(e) for e in errors] == [DrawingError]
    assert str(errors[0]) == "Already marked."
    assert Pick.objects.filter(replaces=pick).count() == 1


def test_not_here_with_empty_pool_reports_exhausted(
    conf: Conference, drawing: Drawing, entrant_factory: Callable[..., Entrant]
) -> None:
    entrant_factory(conf, created_at=BEFORE)
    [pick] = started_single(drawing)
    assert mark_not_here(pick, now=LATER, rng=rng()) is None
    pick.refresh_from_db()
    assert pick.status == S.NOT_HERE
    assert kinds(drawing)[-2:] == [K.NOT_HERE, K.EXHAUSTED]
    close_drawing(drawing, now=LATER)
    drawing.refresh_from_db()
    assert drawing.status == Drawing.Status.FINISHED
    assert drawing.awarded_count == 0


def test_replacement_on_digital_line_is_an_invariant_violation(
    drawing: Drawing, entrants: list[int]
) -> None:
    line = digital_line(drawing)
    drawing.status = Drawing.Status.OPEN
    drawing.cutoff_at = NOW
    drawing.save()
    corrupt = factories.make_pick(line, Entrant.objects.get(pk=entrants[0]))
    with pytest.raises(AssertionError, match="physical"):
        mark_not_here(corrupt, now=LATER, rng=rng())
    corrupt.refresh_from_db()
    assert corrupt.status == S.PENDING


# --- undo ------------------------------------------------------------------------


def test_undo_claim_restores_pending(drawing: Drawing, entrants: list[int]) -> None:
    pick = started_single(drawing, quantity=2)[0]
    mark_claimed(pick, now=LATER)
    restored = undo_last(pick, now=LATER)
    assert restored.status == S.PENDING
    assert restored.marked_at is None
    undo = AuditEvent.objects.get(kind=K.UNDO)
    assert undo.pick_id == pick.pk
    assert undo.detail == {"from": "claimed"}


def test_undo_not_here_voids_replacement_and_restores_pick(
    drawing: Drawing, entrants: list[int]
) -> None:
    [pick] = started_single(drawing)
    replacement = mark_not_here(pick, now=LATER, rng=rng())
    assert replacement is not None
    restored = undo_last(pick, now=LATER)
    assert restored.status == S.PENDING
    replacement.refresh_from_db()
    assert replacement.status == S.VOID
    assert kinds(drawing)[-2:] == [K.VOID, K.UNDO]
    again = mark_not_here(restored, now=LATER, rng=rng())
    assert again is not None
    assert Pick.objects.filter(replaces=pick).count() == 2


def test_reroll_never_redraws_someone_voided_for_that_slot(
    conf: Conference, drawing: Drawing, entrant_factory: Callable[..., Entrant]
) -> None:
    for _ in range(20):
        entrant_factory(conf, created_at=BEFORE)
    [pick] = started_single(drawing)
    seen: list[int] = []
    while (replacement := mark_not_here(pick, now=LATER, rng=rng())) is not None:
        seen.append(replacement.entrant_id)
        undo_last(pick, now=LATER)
    assert len(seen) == len(set(seen)) == 19
    assert pick.entrant_id not in seen
    assert kinds(drawing)[-1] == K.EXHAUSTED


def test_voided_person_can_still_win_another_slot(
    conf: Conference, drawing: Drawing, entrant_factory: Callable[..., Entrant]
) -> None:
    for _ in range(3):
        entrant_factory(conf, created_at=BEFORE)
    first, second = started_single(drawing, quantity=2)
    voided = mark_not_here(first, now=LATER, rng=rng())
    assert voided is not None
    undo_last(first, now=LATER)
    other = mark_not_here(second, now=LATER, rng=rng())
    assert other is not None
    assert other.slot == 2
    assert other.entrant_id == voided.entrant_id


def test_undo_refused_after_replacement_marked(
    drawing: Drawing, entrants: list[int]
) -> None:
    pick = started_single(drawing, quantity=2)[0]
    replacement = mark_not_here(pick, now=LATER, rng=rng())
    assert replacement is not None
    mark_claimed(replacement, now=LATER)
    with pytest.raises(DrawingError, match=r"^The replacement has already been"):
        undo_last(pick, now=LATER)
    pick.refresh_from_db()
    assert pick.status == S.NOT_HERE


def test_undo_not_here_after_exhausted_redraw(
    conf: Conference, drawing: Drawing, entrant_factory: Callable[..., Entrant]
) -> None:
    entrant_factory(conf, created_at=BEFORE)
    [pick] = started_single(drawing)
    mark_not_here(pick, now=LATER, rng=rng())
    assert undo_last(pick, now=LATER).status == S.PENDING
    assert kinds(drawing)[-1] == K.UNDO


def test_undo_refused_when_nothing_to_undo(
    drawing: Drawing, entrants: list[int]
) -> None:
    [pick] = started_single(drawing)
    with pytest.raises(DrawingError, match=r"^Nothing to undo\.$"):
        undo_last(pick, now=LATER)


def test_undo_claim_after_auto_finish_reopens_drawing(
    drawing: Drawing, entrants: list[int]
) -> None:
    [pick] = started_single(drawing)
    mark_claimed(pick, now=LATER)
    drawing.refresh_from_db()
    assert drawing.status == Drawing.Status.FINISHED
    assert undo_last(pick, now=LATER).status == S.PENDING
    drawing.refresh_from_db()
    assert drawing.status == Drawing.Status.OPEN
    assert drawing.closed_at is None
    assert drawing.awarded_count == 0


def test_undo_after_auto_finish_refused_when_another_drawing_open(
    conf: Conference, drawing: Drawing, entrants: list[int]
) -> None:
    [pick] = started_single(drawing)
    mark_claimed(pick, now=LATER)
    nxt = factories.make_drawing(conf, scheduled_at=SCHEDULED)
    started_single(nxt)
    with pytest.raises(DrawingError, match="Another drawing is open"):
        undo_last(pick, now=LATER)
    pick.refresh_from_db()
    assert pick.status == S.CLAIMED


def test_undo_after_auto_finish_refused_when_later_drawing_started(
    conf: Conference, drawing: Drawing, entrants: list[int]
) -> None:
    [pick] = started_single(drawing)
    mark_claimed(pick, now=LATER)
    later = factories.make_drawing(conf, scheduled_at=SCHEDULED)
    digital_line(later)
    start_drawing(later, now=LATER, rng=rng())  # digital-only: finishes at once
    later.refresh_from_db()
    assert later.status == Drawing.Status.FINISHED
    with pytest.raises(DrawingError, match=r"^A later drawing has already started"):
        undo_last(pick, now=LATER)
    pick.refresh_from_db()
    assert pick.status == S.CLAIMED


def test_undo_after_auto_finish_ignores_later_drawing_of_another_conference(
    drawing: Drawing, entrants: list[int]
) -> None:
    [pick] = started_single(drawing)
    mark_claimed(pick, now=LATER)
    elsewhere = factories.make_drawing(
        factories.make_conference(), scheduled_at=SCHEDULED
    )
    digital_line(elsewhere)
    start_drawing(elsewhere, now=LATER, rng=rng())
    assert undo_last(pick, now=LATER).status == S.PENDING


def test_undo_after_auto_finish_allowed_when_earlier_drawing_exists(
    conf: Conference, drawing: Drawing, entrants: list[int]
) -> None:
    earlier = factories.make_drawing(conf, scheduled_at=BEFORE)
    digital_line(earlier)
    start_drawing(earlier, now=BEFORE, rng=rng())
    [pick] = started_single(drawing)
    mark_claimed(pick, now=LATER)
    assert undo_last(pick, now=LATER).status == S.PENDING


def test_undo_refused_after_close_drawing(
    drawing: Drawing, entrants: list[int]
) -> None:
    first, second = started_single(drawing, quantity=2)
    mark_claimed(first, now=LATER)
    dismiss_pending(drawing)
    close_drawing(drawing, now=LATER)
    with pytest.raises(DrawingError, match=r"^This drawing was closed\.$"):
        undo_last(first, now=LATER)


def test_undo_not_here_refused_in_auto_finished_drawing(
    drawing: Drawing, entrants: list[int]
) -> None:
    [pick] = started_single(drawing)
    replacement = mark_not_here(pick, now=LATER, rng=rng())
    assert replacement is not None
    mark_claimed(replacement, now=LATER)
    with pytest.raises(DrawingError, match="isn't open"):
        undo_last(pick, now=LATER)


# --- close -----------------------------------------------------------------------


def test_close_refuses_with_pending_picks(
    drawing: Drawing, entrants: list[int]
) -> None:
    started_single(drawing, quantity=2)
    with pytest.raises(
        DrawingError, match=r"^Mark every pick Claimed or Not here first\.$"
    ):
        close_drawing(drawing, now=LATER)


def test_close_counts_claimed_and_digital_and_audits(
    drawing: Drawing, entrants: list[int]
) -> None:
    digital_line(drawing, position=1)
    first, _, _ = started_single(drawing, quantity=2)
    mark_claimed(first, now=LATER)
    dismiss_pending(drawing)
    close_drawing(drawing, now=LATER)
    drawing.refresh_from_db()
    assert drawing.status == Drawing.Status.FINISHED
    assert drawing.closed_at == LATER
    assert drawing.awarded_count == 2
    assert drawing.closed_by_owner is True
    closed = AuditEvent.objects.get(kind=K.CLOSED)
    assert closed.detail == {"awarded_count": 2}


def test_close_refuses_drawing_not_open(drawing: Drawing) -> None:
    with pytest.raises(DrawingError, match="isn't open"):
        close_drawing(drawing, now=LATER)


# --- wrap-up ---------------------------------------------------------------------


def test_start_refused_after_wrap_up(drawing: Drawing, entrants: list[int]) -> None:
    factories.make_prize_line(drawing)
    Conference.objects.filter(pk=drawing.conference_id).update(wrapped_up_at=NOW)
    with pytest.raises(DrawingError, match=r"^This conference has been wrapped up\.$"):
        start_drawing(drawing, now=LATER, rng=rng())
    drawing.refresh_from_db()
    assert drawing.status == Drawing.Status.SCHEDULED


def test_undo_claim_after_auto_finish_refused_after_wrap_up(
    conf: Conference, drawing: Drawing, entrants: list[int]
) -> None:
    """Reopening after wrap-up would let a winner miss the Drip tag already queued."""
    [pick] = started_single(drawing)
    mark_claimed(pick, now=LATER)
    wrap_up(conf, now=LATER)
    drawing.refresh_from_db()  # a stale copy would be refused as "isn't open"
    assert undo_rule(drawing).reopen_refusal == "This conference has been wrapped up."
    assert undo_rule(drawing).can_undo(pick, None) is False
    with pytest.raises(DrawingError, match=r"^This conference has been wrapped up\.$"):
        undo_last(pick, now=LATER)
    drawing.refresh_from_db()
    pick.refresh_from_db()
    assert (drawing.status, pick.status) == (Drawing.Status.FINISHED, S.CLAIMED)


def test_another_conference_wrapped_up_does_not_block_undo(
    drawing: Drawing, entrants: list[int]
) -> None:
    """Wrapping up last month's conference must not freeze today's booth."""
    [pick] = started_single(drawing)
    pick = mark_claimed(pick, now=LATER)
    other = factories.make_conference(wrapped_up_at=NOW)
    assert other.pk != drawing.conference_id
    drawing.refresh_from_db()
    assert drawing.status == Drawing.Status.FINISHED
    assert undo_rule(drawing).can_undo(pick, None) is True
    undo_last(pick, now=LATER)
    drawing.refresh_from_db()
    assert drawing.status == Drawing.Status.OPEN


def test_undo_rule_matches_undo_last(drawing: Drawing, entrants: list[int]) -> None:
    """The booth's Undo button and the service agree, pick by pick."""
    first, second, third = started_single(drawing, quantity=3)
    first = mark_claimed(first, now=LATER)
    replacement = mark_not_here(second, now=LATER, rng=rng())
    assert replacement is not None
    for stale in (drawing, second):
        stale.refresh_from_db()
    rule = undo_rule(drawing)
    assert rule.can_undo(first, None) is True
    assert rule.can_undo(second, replacement) is True
    assert rule.can_undo(third, None) is False  # pending: nothing to undo
    replacement = mark_claimed(replacement, now=LATER)
    assert undo_rule(drawing).can_undo(second, replacement) is False
    assert undo_rule(drawing).refusal(second, replacement) == (
        "The replacement has already been marked."
    )


def _hold_conference_lock_and_wrap_up(
    conference_id: int, locked: threading.Event, release: threading.Event
) -> None:
    """Act as a wrap-up in progress: FOR UPDATE on the conference, stamp, wait."""
    try:
        with transaction.atomic():
            Conference.objects.select_for_update().get(pk=conference_id)
            Conference.objects.filter(pk=conference_id).update(wrapped_up_at=NOW)
            locked.set()
            release.wait(timeout=5)
    finally:
        connection.close()


def _run_while_wrap_up_holds_lock(
    conference_id: int, action: Callable[[], object]
) -> tuple[bool, list[object]]:
    """Run `action` while a wrap-up holds the conference; report if it waited."""
    locked, release = threading.Event(), threading.Event()
    outcomes: list[object] = []

    def act() -> None:
        try:
            outcomes.append(action())
        except Exception as exc:  # recorded for the caller's assertion
            outcomes.append(exc)
        finally:
            connection.close()

    holder = threading.Thread(
        target=_hold_conference_lock_and_wrap_up,
        args=(conference_id, locked, release),
    )
    holder.start()
    worker = threading.Thread(target=act)
    try:
        assert locked.wait(timeout=5)
        worker.start()
        worker.join(timeout=0.5)
        waited = worker.is_alive()
    finally:
        release.set()
        holder.join()
        worker.join()
    return waited, outcomes


@pytest.mark.django_db(transaction=True)
def test_start_waits_for_a_concurrent_wrap_up_and_then_refuses(
    conf: Conference, drawing: Drawing, entrants: list[int]
) -> None:
    factories.make_prize_line(drawing)
    waited, [outcome] = _run_while_wrap_up_holds_lock(
        conf.pk, lambda: start_drawing(drawing, now=LATER, rng=rng())
    )
    assert waited
    assert isinstance(outcome, DrawingError)
    assert str(outcome) == "This conference has been wrapped up."
    drawing.refresh_from_db()
    assert drawing.status == Drawing.Status.SCHEDULED


@pytest.mark.django_db(transaction=True)
def test_undo_waits_for_a_concurrent_wrap_up_and_then_refuses(
    conf: Conference, drawing: Drawing, entrants: list[int]
) -> None:
    [pick] = started_single(drawing)
    mark_claimed(pick, now=LATER)
    waited, [outcome] = _run_while_wrap_up_holds_lock(
        conf.pk, lambda: undo_last(pick, now=LATER)
    )
    assert waited
    assert isinstance(outcome, DrawingError)
    assert str(outcome) == "This conference has been wrapped up."
    drawing.refresh_from_db()
    assert drawing.status == Drawing.Status.FINISHED


def _race(*actions: Callable[[], object]) -> list[object]:
    """Run each action on its own connection, released together by a barrier."""
    barrier = threading.Barrier(len(actions))
    outcomes: list[object] = []

    def run(action: Callable[[], object]) -> None:
        try:
            barrier.wait()
            outcomes.append(action())
        except Exception as exc:  # recorded for the caller's assertion
            outcomes.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(a,)) for a in actions]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return outcomes


@pytest.mark.django_db(transaction=True)
def test_racing_wrap_up_and_start_never_leaves_an_open_drawing_wrapped_up(
    conf: Conference, drawing: Drawing, entrants: list[int]
) -> None:
    factories.make_prize_line(drawing, quantity=2)
    outcomes = _race(
        lambda: wrap_up(conf, now=LATER),
        lambda: start_drawing(drawing, now=LATER, rng=rng()),
    )
    assert [type(o) for o in outcomes].count(DrawingError) == 1
    conf.refresh_from_db()
    drawing.refresh_from_db()
    assert not (
        conf.wrapped_up_at is not None and drawing.status == Drawing.Status.OPEN
    )


@pytest.mark.django_db(transaction=True)
def test_racing_wrap_up_and_undo_never_leaves_an_open_drawing_wrapped_up(
    conf: Conference, drawing: Drawing, entrants: list[int]
) -> None:
    [pick] = started_single(drawing)
    mark_claimed(pick, now=LATER)
    outcomes = _race(
        lambda: wrap_up(conf, now=LATER),
        lambda: undo_last(pick, now=LATER),
    )
    assert [type(o) for o in outcomes].count(DrawingError) == 1
    conf.refresh_from_db()
    drawing.refresh_from_db()
    assert not (
        conf.wrapped_up_at is not None and drawing.status == Drawing.Status.OPEN
    )


@pytest.mark.django_db(transaction=True)
def test_two_phones_starting_two_drawings_open_exactly_one(
    conf: Conference, entrants: list[int]
) -> None:
    """The loser gets a DrawingError, not the unique constraint's IntegrityError."""
    drawings = [factories.make_drawing(conf, scheduled_at=SCHEDULED) for _ in "ab"]
    for d in drawings:
        factories.make_prize_line(d)
    outcomes = _race(
        *[functools.partial(start_drawing, d, now=NOW, rng=rng()) for d in drawings]
    )
    errors = [o for o in outcomes if isinstance(o, Exception)]
    assert [str(e) for e in errors] == ["Another drawing is open. Close it first."]
    assert [type(e) for e in errors] == [DrawingError]
    assert Drawing.objects.filter(status=Drawing.Status.OPEN).count() == 1


# --- privacy ---------------------------------------------------------------------


def test_audit_detail_contains_no_names_or_emails(
    conf: Conference, drawing: Drawing, entrant_factory: Callable[..., Entrant]
) -> None:
    people = [
        entrant_factory(
            conf,
            name=f"Zelda Quixote{i}",
            email=f"zq{i}@secret.example",
            created_at=BEFORE,
        )
        for i in range(5)
    ]
    line = digital_line(
        drawing, position=1, code_mode=PrizeLine.CodeMode.POOL, shared_code=""
    )
    add_codes(line, 1)
    first, second, _ = started_single(drawing, quantity=2)
    mark_not_here(first, now=LATER, rng=rng())
    undo_last(first, now=LATER)
    mark_not_here(first, now=LATER, rng=rng())
    mark_claimed(second, now=LATER)
    dismiss_pending(drawing)
    close_drawing(drawing, now=LATER)
    blob = json.dumps(list(AuditEvent.objects.values_list("detail", flat=True)))
    assert {
        K.STARTED,
        K.PICKED,
        K.NOT_HERE,
        K.UNDO,
        K.VOID,
        K.CLAIMED,
        K.CLOSED,
    } <= set(kinds(drawing))
    for person in people:
        assert person.name not in blob
        assert person.email not in blob
        assert "Zelda" not in blob and "secret.example" not in blob
    assert "CODE" not in blob


# --- property --------------------------------------------------------------------

LINE = st.tuples(st.sampled_from([PHYS, DIG]), st.integers(1, 3))
ACTION = st.sampled_from(["claim", "not_here", "undo"])


class DrawProperty(TestCase):
    @settings(deadline=None, max_examples=20)
    @given(
        n_entrants=st.integers(0, 7),
        drawings=st.lists(
            st.lists(LINE, min_size=1, max_size=3), min_size=1, max_size=2
        ),
        actions=st.lists(ACTION, max_size=12),
        seed=st.integers(0, 2**16),
    )
    # Not here, then undo: the replacement must be voided, not left live.
    @example(n_entrants=3, drawings=[[(PHYS, 1)]], actions=["not_here", "undo"], seed=0)
    def test_property_nobody_wins_twice_and_slots_never_exceed_quantity(
        self,
        n_entrants: int,
        drawings: list[list[tuple[str, int]]],
        actions: list[str],
        seed: int,
    ) -> None:
        r = random.Random(seed)
        conf = factories.make_conference()
        for _ in range(n_entrants):
            factories.make_entrant(conf, created_at=BEFORE)
        for spec in drawings:
            d = factories.make_drawing(conf, scheduled_at=SCHEDULED)
            for position, (kind, quantity) in enumerate(spec):
                if kind == DIG:
                    digital_line(d, quantity=quantity, position=position)
                else:
                    factories.make_prize_line(d, quantity=quantity, position=position)
            start_drawing(d, now=NOW, rng=r)
            marked: list[Pick] = []
            for action in actions:
                pending = Pick.objects.filter(
                    prize_line__drawing=d, status=S.PENDING
                ).order_by("id")
                try:
                    if action == "undo" and marked:
                        undo_last(marked.pop(), now=NOW)
                    elif action == "claim" and pending:
                        marked.append(mark_claimed(pending[0], now=NOW))
                    elif action == "not_here" and pending:
                        marked.append(pending[0])
                        mark_not_here(pending[0], now=NOW, rng=r)
                except DrawingError:
                    pass
            while p := Pick.objects.filter(
                prize_line__drawing=d, status=S.PENDING
            ).first():
                mark_claimed(p, now=NOW)
            d.refresh_from_db()
            if d.status == Drawing.Status.OPEN:
                close_drawing(d, now=NOW)
            d.refresh_from_db()
            assert d.status == Drawing.Status.FINISHED

        all_picks = Pick.objects.filter(prize_line__drawing__conference=conf)
        winners = Counter(
            all_picks.filter(status__in=[S.CLAIMED, S.DIGITAL]).values_list(
                "entrant_id", flat=True
            )
        )
        assert all(n == 1 for n in winners.values())
        live_per_drawing = Counter(
            all_picks.exclude(status=S.VOID).values_list(
                "prize_line__drawing_id", "entrant_id"
            )
        )
        assert all(n == 1 for n in live_per_drawing.values())
        live_slots = Counter(
            all_picks.exclude(status__in=[S.VOID, S.NOT_HERE]).values_list(
                "prize_line_id", "slot"
            )
        )
        assert all(n == 1 for n in live_slots.values())
        for p in all_picks.select_related("prize_line"):
            assert 1 <= p.slot <= p.prize_line.quantity
        for d in Drawing.objects.filter(conference=conf):
            assert (
                d.awarded_count
                == all_picks.filter(
                    prize_line__drawing=d, status__in=[S.CLAIMED, S.DIGITAL]
                ).count()
            )


class RerollProperty(TestCase):
    @settings(deadline=None, max_examples=15)
    @given(
        n_entrants=st.integers(2, 8),
        ops=st.lists(st.booleans(), max_size=30),
        seed=st.integers(0, 2**16),
    )
    @example(n_entrants=3, ops=[True, False] * 6, seed=0)
    def test_reroll_sequences_never_repeat_a_voided_entrant_for_the_slot(
        self, n_entrants: int, ops: list[bool], seed: int
    ) -> None:
        """True is Not here, False is Undo; a voided entrant never returns."""
        r = random.Random(seed)
        conf = factories.make_conference()
        for _ in range(n_entrants):
            factories.make_entrant(conf, created_at=BEFORE)
        drawing = factories.make_drawing(conf, scheduled_at=SCHEDULED)
        factories.make_prize_line(drawing, quantity=1)
        [original] = start_drawing(drawing, now=NOW, rng=r)
        voided: set[int] = set()
        live: Pick | None = None
        for not_here in ops:
            original.refresh_from_db()
            if not_here and original.status == S.PENDING:
                live = mark_not_here(original, now=LATER, rng=r)
                if live is not None:
                    assert live.entrant_id not in voided
            elif not not_here and original.status == S.NOT_HERE:
                undo_last(original, now=LATER)
                if live is not None:
                    voided.add(live.entrant_id)
                live = None
