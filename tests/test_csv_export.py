import csv
import datetime
import io
from collections.abc import Callable
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from hypothesis import example, given
from hypothesis import strategies as st
from hypothesis.extra.django import TestCase

from conference_drawing.raffle.csv_export import (
    CsvRow,
    export_rows,
    neutralize,
    write_csv,
)
from conference_drawing.raffle.models import (
    Conference,
    CouponCode,
    Entrant,
    Pick,
    PrizeLine,
    Question,
)

S = Pick.Status
FIXED_HEAD = ["name", "email", "entered_at"]
FIXED_TAIL = [
    "status",
    "won",
    "digital_codes",
    "digital_urls",
    "followup_ack_at",
    "bd_optin",
    "bd_optin_at",
]


def _questions(conference: Conference, *keys: str) -> list[Question]:
    return [
        Question.objects.create(conference=conference, key=k, prompt=k, position=i)
        for i, k in enumerate(keys)
    ]


def _read(conference: Conference) -> list[list[str]]:
    out = io.StringIO(newline="")
    write_csv(
        export_rows(conference),
        list(Question.objects.filter(conference=conference)),
        out,
    )
    return list(csv.reader(io.StringIO(out.getvalue(), newline="")))


def test_columns_exact_order(conference_factory: Callable[..., Conference]) -> None:
    conf = conference_factory()
    _questions(conf, "second_q", "first_q")
    Question.objects.filter(key="second_q").update(position=5)
    assert _read(conf)[0] == [*FIXED_HEAD, "first_q", "second_q", *FIXED_TAIL]


def test_winner_status_counts_claimed_and_digital_only(
    conference_factory: Callable[..., Conference],
    entrant_factory: Callable[..., Entrant],
    drawing_factory: Callable[..., Any],
    prize_line_factory: Callable[..., PrizeLine],
    pick_factory: Callable[..., Pick],
    book_factory: Callable[..., Any],
) -> None:
    conf = conference_factory()
    drawing = drawing_factory(conf)
    physical = prize_line_factory(
        drawing, book=book_factory(title="Physical Book"), quantity=5
    )
    digital = prize_line_factory(
        drawing,
        book=book_factory(title="Digital Book"),
        kind=PrizeLine.Kind.DIGITAL,
        code_mode=PrizeLine.CodeMode.POOL,
        url_template="https://x.example/r?c={code}",
    )
    shared = prize_line_factory(
        drawing,
        book=book_factory(title="Shared Book"),
        kind=PrizeLine.Kind.DIGITAL,
        code_mode=PrizeLine.CodeMode.SHARED,
        shared_code="SHARE 1",
        url_template="https://y.example/s?c={code}",
    )
    claimed = entrant_factory(conf, name="Claimed")
    dig = entrant_factory(conf, name="Dig")
    both = entrant_factory(conf, name="Both")
    pending = entrant_factory(conf, name="Pending")
    nothere = entrant_factory(conf, name="NotHere")
    void = entrant_factory(conf, name="Void")
    entrant_factory(conf, name="None")
    pick_factory(physical, claimed, status=S.CLAIMED, slot=1)
    d = pick_factory(digital, dig, status=S.DIGITAL)
    CouponCode.objects.create(prize_line=digital, code="A/B", pick=d)
    pick_factory(shared, both, status=S.DIGITAL)
    pick_factory(physical, both, status=S.CLAIMED, slot=2)
    pick_factory(physical, pending, status=S.PENDING, slot=3)
    pick_factory(physical, nothere, status=S.NOT_HERE, slot=4)
    pick_factory(physical, void, status=S.VOID, slot=5)
    table = _read(conf)
    col = {h: i for i, h in enumerate(table[0])}
    rows = {r[0]: r for r in table[1:]}
    assert {n: r[col["status"]] for n, r in rows.items()} == {
        "Claimed": "winner",
        "Dig": "winner",
        "Both": "winner",
        "Pending": "nonwinner",
        "NotHere": "nonwinner",
        "Void": "nonwinner",
        "None": "nonwinner",
    }
    assert rows["Claimed"][col["won"]] == "Physical Book"
    assert rows["Dig"][col["won"]] == "Digital Book"
    assert rows["Dig"][col["digital_codes"]] == "A/B"
    assert rows["Dig"][col["digital_urls"]] == "https://x.example/r?c=A%2FB"
    assert set(rows["Both"][col["won"]].split("; ")) == {"Physical Book", "Shared Book"}
    assert rows["Both"][col["digital_codes"]] == "SHARE 1"
    assert rows["Both"][col["digital_urls"]] == "https://y.example/s?c=SHARE%201"
    assert rows["Pending"][col["won"]] == ""


def test_digital_winner_without_assigned_code_has_empty_code_cells(
    conference_factory: Callable[..., Conference],
    entrant_factory: Callable[..., Entrant],
    drawing_factory: Callable[..., Any],
    prize_line_factory: Callable[..., PrizeLine],
    pick_factory: Callable[..., Pick],
) -> None:
    conf = conference_factory()
    line = prize_line_factory(
        drawing_factory(conf),
        kind=PrizeLine.Kind.DIGITAL,
        code_mode=PrizeLine.CodeMode.POOL,
        url_template="https://x.example/r?c={code}",
    )
    pick_factory(line, entrant_factory(conf), status=S.DIGITAL)
    table = _read(conf)
    col = {h: i for i, h in enumerate(table[0])}
    assert table[1][col["status"]] == "winner"
    assert table[1][col["digital_codes"]] == ""
    assert table[1][col["digital_urls"]] == ""


def test_times_in_conference_zone(
    conference_factory: Callable[..., Conference],
    entrant_factory: Callable[..., Entrant],
) -> None:
    conf = conference_factory(time_zone="America/Los_Angeles")
    entrant_factory(
        conf,
        created_at=datetime.datetime(2027, 5, 11, 20, 30, tzinfo=datetime.UTC),
        followup_ack_at=datetime.datetime(2027, 5, 11, 20, 31, tzinfo=datetime.UTC),
        bd_optin_at=datetime.datetime(2027, 5, 11, 20, 32, tzinfo=datetime.UTC),
    )
    entrant_factory(
        conf,
        created_at=datetime.datetime(2027, 5, 11, 21, 0, tzinfo=datetime.UTC),
        bd_optin_at=None,
    )
    head, first, second = _read(conf)
    col = {h: i for i, h in enumerate(head)}
    assert first[col["entered_at"]] == "2027-05-11T13:30:00-07:00"
    assert first[col["followup_ack_at"]] == "2027-05-11T13:31:00-07:00"
    assert first[col["bd_optin_at"]] == "2027-05-11T13:32:00-07:00"
    assert first[col["bd_optin"]] == "yes"
    assert second[col["bd_optin_at"]] == ""
    assert second[col["bd_optin"]] == "no"


@pytest.mark.parametrize("bad", ["=1+1", "+x", "-x", "@x", "\tx", "\rx"])
def test_formula_cells_are_neutralized(
    bad: str,
    conference_factory: Callable[..., Conference],
    entrant_factory: Callable[..., Entrant],
) -> None:
    conf = conference_factory()
    _questions(conf, "q")
    entrant_factory(conf, name=bad, answers={"q": bad})
    row = _read(conf)[1]
    assert row[0] == "'" + bad
    assert row[3] == "'" + bad


def test_neutralize_leaves_safe_text() -> None:
    assert neutralize("Ada") == "Ada"
    assert neutralize("") == ""


def test_csv_row_rejects_bad_optin() -> None:
    with pytest.raises(ValueError, match="bd_optin"):
        CsvRow(
            name="a",
            email="a@example.com",
            entered_at="",
            followup_ack_at="",
            bd_optin="maybe",  # type: ignore[arg-type]
            bd_optin_at="",
            status="winner",
        )


def test_non_string_answer_is_stringified(
    conference_factory: Callable[..., Conference],
    entrant_factory: Callable[..., Entrant],
) -> None:
    conf = conference_factory()
    _questions(conf, "q")
    entrant_factory(conf, answers={"q": 5})
    assert _read(conf)[1][3] == "5"


@pytest.fixture
def owner(client: Client, db: None) -> Client:
    user = User.objects.create_user("owner", password="pw", is_superuser=True)
    client.force_login(user)
    return client


def test_export_requires_login(
    client: Client, conference_factory: Callable[..., Conference]
) -> None:
    conf = conference_factory(slug="pycon")
    response = client.get(reverse("booth-export", args=[conf.slug]))
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("admin:login"))


def test_export_response(
    owner: Client,
    conference_factory: Callable[..., Conference],
    entrant_factory: Callable[..., Entrant],
) -> None:
    conf = conference_factory(slug="pycon")
    entrant_factory(conf, name="Zoë 😀")
    response = owner.get(reverse("booth-export", args=[conf.slug]))
    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert (
        response["Content-Disposition"] == 'attachment; filename="pycon-entrants.csv"'
    )
    assert set(response["Cache-Control"].split(", ")) == {"private", "no-store"}
    body = response.content
    assert body.startswith(b"\xef\xbb\xbf")
    assert "Zoë 😀" in body.decode("utf-8-sig")


def test_export_404_and_post_405(
    owner: Client, conference_factory: Callable[..., Conference]
) -> None:
    conf = conference_factory(slug="pycon")
    assert owner.get(reverse("booth-export", args=["nope"])).status_code == 404
    assert owner.post(reverse("booth-export", args=[conf.slug])).status_code == 405


def test_export_query_count_is_flat(
    owner: Client,
    conference_factory: Callable[..., Conference],
    entrant_factory: Callable[..., Entrant],
    drawing_factory: Callable[..., Any],
    prize_line_factory: Callable[..., PrizeLine],
    pick_factory: Callable[..., Pick],
    django_assert_max_num_queries: Callable[..., Any],
) -> None:
    conf = conference_factory(slug="pycon")
    line = prize_line_factory(drawing_factory(conf), quantity=20)
    for slot in range(1, 21):
        pick_factory(line, entrant_factory(conf), slot=slot, status=S.CLAIMED)
    with django_assert_max_num_queries(8):
        assert owner.get(reverse("booth-export", args=[conf.slug])).status_code == 200


def test_conference_page_links_to_export(
    owner: Client, conference_factory: Callable[..., Conference]
) -> None:
    conf = conference_factory(slug="pycon")
    page = owner.get(reverse("booth-conference", args=[conf.slug])).content.decode()
    assert reverse("booth-export", args=[conf.slug]) in page
    assert "Download CSV" in page


_text = st.text(
    alphabet=st.characters(blacklist_categories=["Cs"], blacklist_characters="\x00"),
    min_size=1,
    max_size=30,
)
_tricky = st.sampled_from(
    ["a,b", 'say "hi"', "line1\nline2", "😀 emoji", "שלום עולם", "مرحبا", "=1+1", "-x"]
)


class RoundTrip(TestCase):
    @example(names=["a,b", 'say "hi"', "line1\nline2", "😀", "שלום", "=1+1"])
    @given(names=st.lists(st.one_of(_text, _tricky), min_size=1, max_size=5))
    def test_roundtrip_through_csv_reader(self, names: list[str]) -> None:
        conf = Conference.objects.create(
            name="C",
            slug="rt",
            time_zone="UTC",
            starts_on=datetime.date(2027, 1, 1),
            ends_on=datetime.date(2027, 1, 2),
        )
        Question.objects.create(conference=conf, key="q", prompt="q")
        for i, name in enumerate(names):
            Entrant.objects.create(
                conference=conf,
                name=name,
                email=f"e{i}@example.com",
                answers={"q": name},
            )
        rows = _read(conf)
        expected = sorted(
            (neutralize(e.name), neutralize(e.email), neutralize(e.answers["q"]))
            for e in Entrant.objects.filter(conference=conf)
        )
        got = sorted((r[0], r[1], r[3]) for r in rows[1:])
        assert got == expected
        assert all(len(r) == len(rows[0]) for r in rows)
