import datetime
import hashlib
import random
import re
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from pydantic import ValidationError

from conference_drawing.raffle.draw import start_drawing
from conference_drawing.raffle.drip.config import DripSettings, drip_settings
from conference_drawing.raffle.models import (
    Conference,
    CouponCode,
    Drawing,
    Entrant,
    Pick,
    PrizeLine,
)
from conference_drawing.raffle.views import booth

HTMX_SHA256 = "d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717"
HX = {"HX-Request": "true"}
EXHAUSTED = "No one left in the pool for this copy."
S = Pick.Status
APP = Path(booth.__file__).resolve().parents[1]
STARTED = datetime.datetime(2027, 5, 11, 20, 0, tzinfo=datetime.UTC)


@pytest.fixture(autouse=True)
def _seeded_rng(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(booth, "_rng", lambda: random.Random(0))


@pytest.fixture(autouse=True)
def _no_drip(settings: Any) -> None:
    settings.DRIP_ACCOUNT_ID = ""
    settings.DRIP_API_KEY = ""


@pytest.fixture
def owner(client: Client, db: None) -> Client:
    user = User.objects.create_user("owner", password="pw", is_staff=True)  # noqa: S106
    client.force_login(user)
    return client


@pytest.fixture
def conference(conference_factory: Callable[..., Conference]) -> Conference:
    return conference_factory(slug="pycon")


@pytest.fixture
def drawing(drawing_factory: Callable[..., Drawing], conference: Conference) -> Drawing:
    return drawing_factory(conference)


def _entrants(
    entrant_factory: Callable[..., Entrant], conference: Conference, n: int
) -> None:
    for _ in range(n):
        entrant_factory(conference)


def _urls(drawing: Drawing, pick: Pick) -> list[str]:
    return [
        reverse("booth-home"),
        reverse("booth-conference", args=[drawing.conference.slug]),
        reverse("booth-drawing", args=[drawing.pk]),
        reverse("booth-start", args=[drawing.pk]),
        reverse("booth-close", args=[drawing.pk]),
        *(reverse(f"booth-{a}", args=[pick.pk]) for a in ("claim", "not-here", "undo")),
    ]


def _open_drawing(
    drawing: Drawing,
    conference: Conference,
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
    *,
    entrants: int = 4,
    quantity: int = 1,
) -> PrizeLine:
    _entrants(entrant_factory, conference, entrants)
    line = prize_line_factory(drawing, quantity=quantity)
    start_drawing(drawing, now=STARTED, rng=random.Random(1))
    return line


def _page(owner: Client, drawing: Drawing) -> str:
    return owner.get(reverse("booth-drawing", args=[drawing.pk])).content.decode()


def test_booth_requires_login(
    client: Client,
    drawing: Drawing,
    prize_line_factory: Callable[..., PrizeLine],
    pick_factory: Callable[..., Pick],
    entrant_factory: Callable[..., Entrant],
) -> None:
    pick = pick_factory(
        prize_line_factory(drawing), entrant_factory(drawing.conference)
    )
    for url in _urls(drawing, pick):
        for method in (client.get, client.post):
            response = method(url)
            assert response.status_code == 302, url
            assert response["Location"].startswith(reverse("admin:login")), url


def test_pages_list_conferences_and_drawings(
    owner: Client, drawing: Drawing, conference: Conference
) -> None:
    home = owner.get(reverse("booth-home")).content.decode()
    assert conference.name in home
    assert reverse("booth-conference", args=["pycon"]) in home
    page = owner.get(reverse("booth-conference", args=["pycon"])).content.decode()
    assert reverse("booth-drawing", args=[drawing.pk]) in page
    assert owner.get(reverse("booth-conference", args=["nope"])).status_code == 404
    assert (
        owner.get(reverse("booth-drawing", args=[drawing.pk + 99])).status_code == 404
    )


def test_scheduled_drawing_offers_start(owner: Client, drawing: Drawing) -> None:
    html = _page(owner, drawing)
    assert f'action="{reverse("booth-start", args=[drawing.pk])}"' in html
    assert "csrfmiddlewaretoken" in html


def test_start_renders_picks_grouped_by_book(
    owner: Client,
    drawing: Drawing,
    conference: Conference,
    prize_line_factory: Callable[..., PrizeLine],
    book_factory: Callable[..., Any],
    entrant_factory: Callable[..., Entrant],
) -> None:
    _entrants(entrant_factory, conference, 5)
    first = prize_line_factory(
        drawing, book=book_factory(title="Alpha Book"), quantity=2
    )
    prize_line_factory(drawing, book=book_factory(title="Beta Book"), position=1)
    response = owner.post(reverse("booth-start", args=[drawing.pk]), follow=True)
    html = response.content.decode()
    assert response.redirect_chain[-1][0] == reverse("booth-drawing", args=[drawing.pk])
    assert html.index("Alpha Book") < html.index("Beta Book")
    assert html.count('class="pick ') == 3
    assert html.count("Claimed</button>") == 3
    assert html.count("Not here</button>") == 3
    assert Pick.objects.filter(prize_line=first).count() == 2
    assert 'data-state="open"' in html


def test_drawing_error_shown_as_message_not_500(
    owner: Client, drawing: Drawing, drawing_factory: Callable[..., Drawing]
) -> None:
    drawing_factory(drawing.conference, status=Drawing.Status.OPEN)
    response = owner.post(reverse("booth-start", args=[drawing.pk]), follow=True)
    assert response.status_code == 200
    assert "Another drawing is open. Close it first." in response.content.decode()
    drawing.refresh_from_db()
    assert drawing.status == Drawing.Status.SCHEDULED


def test_claim_returns_partial_for_htmx(
    owner: Client,
    drawing: Drawing,
    conference: Conference,
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
) -> None:
    line = _open_drawing(drawing, conference, prize_line_factory, entrant_factory)
    pick = Pick.objects.get(prize_line=line)
    response = owner.post(reverse("booth-claim", args=[pick.pk]), headers=HX)
    html = response.content.decode()
    assert response.status_code == 200
    assert "<html" not in html
    assert f'id="pick-{pick.pk}"' in html
    assert 'id="drawing-status"' in html
    assert 'hx-swap-oob="true"' in html
    assert "Finished" in html
    assert response["Cache-Control"] == "private, no-store"
    pick.refresh_from_db()
    assert pick.status == S.CLAIMED


def test_claim_without_htmx_redirects(
    owner: Client,
    drawing: Drawing,
    conference: Conference,
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
) -> None:
    line = _open_drawing(drawing, conference, prize_line_factory, entrant_factory)
    pick = Pick.objects.get(prize_line=line)
    response = owner.post(reverse("booth-claim", args=[pick.pk]))
    assert response.status_code == 302
    assert response["Location"] == reverse("booth-drawing", args=[drawing.pk])


def test_not_here_partial_includes_replacement_row(
    owner: Client,
    drawing: Drawing,
    conference: Conference,
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
) -> None:
    line = _open_drawing(drawing, conference, prize_line_factory, entrant_factory)
    pick = Pick.objects.get(prize_line=line)
    html = owner.post(
        reverse("booth-not-here", args=[pick.pk]), headers=HX
    ).content.decode()
    replacement = Pick.objects.get(replaces=pick)
    assert f'id="pick-{pick.pk}"' in html
    assert f'id="pick-{replacement.pk}"' in html
    assert replacement.entrant.name in html
    assert reverse("booth-claim", args=[replacement.pk]) in html
    # The passed-over pick offers Undo; the pending replacement does not.
    assert reverse("booth-undo", args=[pick.pk]) in html
    assert reverse("booth-undo", args=[replacement.pk]) not in html


def test_exhausted_slot_shows_message_and_offers_close(
    owner: Client,
    drawing: Drawing,
    conference: Conference,
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
) -> None:
    line = _open_drawing(
        drawing, conference, prize_line_factory, entrant_factory, entrants=1
    )
    pick = Pick.objects.get(prize_line=line)
    html = owner.post(
        reverse("booth-not-here", args=[pick.pk]), headers=HX
    ).content.decode()
    assert EXHAUSTED in html
    assert "Close drawing" in html
    page = _page(owner, drawing)
    assert EXHAUSTED in page
    assert reverse("booth-close", args=[drawing.pk]) in page


def test_slot_with_no_pick_at_start_shows_exhausted(
    owner: Client,
    drawing: Drawing,
    conference: Conference,
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
) -> None:
    _open_drawing(
        drawing, conference, prize_line_factory, entrant_factory, entrants=1, quantity=2
    )
    assert _page(owner, drawing).count(EXHAUSTED) == 1


def test_undo_voids_replacement_row(
    owner: Client,
    drawing: Drawing,
    conference: Conference,
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
) -> None:
    line = _open_drawing(drawing, conference, prize_line_factory, entrant_factory)
    pick = Pick.objects.get(prize_line=line)
    owner.post(reverse("booth-not-here", args=[pick.pk]), headers=HX)
    replacement = Pick.objects.get(replaces=pick)
    html = owner.post(
        reverse("booth-undo", args=[pick.pk]), headers=HX
    ).content.decode()
    assert f'id="pick-{pick.pk}"' in html
    assert f'id="pick-{replacement.pk}"' not in html
    assert reverse("booth-claim", args=[pick.pk]) in html
    replacement.refresh_from_db()
    assert replacement.status == S.VOID


def test_undo_hidden_after_replacement_marked(
    owner: Client,
    drawing: Drawing,
    conference: Conference,
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
) -> None:
    line = _open_drawing(
        drawing, conference, prize_line_factory, entrant_factory, quantity=2
    )
    pick = Pick.objects.filter(prize_line=line).order_by("slot").first()
    assert pick is not None
    owner.post(reverse("booth-not-here", args=[pick.pk]))
    replacement = Pick.objects.get(replaces=pick)
    owner.post(reverse("booth-claim", args=[replacement.pk]))
    page = _page(owner, drawing)
    assert reverse("booth-undo", args=[pick.pk]) not in page
    assert reverse("booth-undo", args=[replacement.pk]) in page


def test_service_error_on_pick_action_is_a_message(
    owner: Client,
    drawing: Drawing,
    conference: Conference,
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
) -> None:
    line = _open_drawing(drawing, conference, prize_line_factory, entrant_factory)
    pick = Pick.objects.get(prize_line=line)
    owner.post(reverse("booth-not-here", args=[pick.pk]))
    htmx = owner.post(reverse("booth-claim", args=[pick.pk]), headers=HX)
    assert htmx.status_code == 200
    assert "Already marked." in htmx.content.decode()
    assert f'id="pick-{pick.pk}"' in htmx.content.decode()
    plain = owner.post(reverse("booth-claim", args=[pick.pk]), follow=True)
    assert "Already marked." in plain.content.decode()


def test_close_offered_only_when_nothing_pending_and_closes(
    owner: Client,
    drawing: Drawing,
    conference: Conference,
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
) -> None:
    line = _open_drawing(
        drawing, conference, prize_line_factory, entrant_factory, entrants=1
    )
    pick = Pick.objects.get(prize_line=line)
    close = reverse("booth-close", args=[drawing.pk])
    assert close not in _page(owner, drawing)
    refused = owner.post(close, follow=True)
    assert "Mark every pick Claimed or Not here first." in refused.content.decode()
    owner.post(reverse("booth-not-here", args=[pick.pk]))
    response = owner.post(close, headers=HX)
    assert response.status_code == 200
    assert response["HX-Redirect"] == reverse("booth-drawing", args=[drawing.pk])
    drawing.refresh_from_db()
    assert drawing.status == Drawing.Status.FINISHED
    page = _page(owner, drawing)
    assert 'data-state="finished"' in page
    assert close not in page


def test_finished_drawing_shows_winners(
    owner: Client,
    drawing: Drawing,
    conference: Conference,
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
) -> None:
    line = _open_drawing(drawing, conference, prize_line_factory, entrant_factory)
    pick = Pick.objects.get(prize_line=line)
    owner.post(reverse("booth-claim", args=[pick.pk]))
    page = _page(owner, drawing)
    assert pick.entrant.name in page
    assert "1 awarded" in page
    # Auto-finished (not closed by the owner): a mis-tapped claim can be undone.
    assert reverse("booth-undo", args=[pick.pk]) in page
    owner.post(reverse("booth-undo", args=[pick.pk]))
    drawing.refresh_from_db()
    assert drawing.status == Drawing.Status.OPEN


def test_undo_hidden_when_owner_closed(
    owner: Client,
    drawing: Drawing,
    conference: Conference,
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
) -> None:
    line = _open_drawing(drawing, conference, prize_line_factory, entrant_factory)
    pick = Pick.objects.get(prize_line=line)
    owner.post(reverse("booth-claim", args=[pick.pk]))
    drawing.refresh_from_db()
    drawing.closed_by_owner = True
    drawing.save()
    assert "/undo/" not in _page(owner, drawing)


def test_undo_hidden_when_later_drawing_started(
    owner: Client,
    drawing: Drawing,
    conference: Conference,
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
    drawing_factory: Callable[..., Drawing],
) -> None:
    line = _open_drawing(drawing, conference, prize_line_factory, entrant_factory)
    pick = Pick.objects.get(prize_line=line)
    owner.post(reverse("booth-claim", args=[pick.pk]))
    drawing.refresh_from_db()
    assert drawing.closed_at is not None
    later = drawing_factory(
        conference,
        status=Drawing.Status.OPEN,
        started_at=drawing.closed_at + datetime.timedelta(minutes=5),
    )
    assert "/undo/" not in _page(owner, drawing)
    later.delete()


def test_get_on_post_endpoints_405(
    owner: Client,
    drawing: Drawing,
    prize_line_factory: Callable[..., PrizeLine],
    pick_factory: Callable[..., Pick],
    entrant_factory: Callable[..., Entrant],
) -> None:
    pick = pick_factory(
        prize_line_factory(drawing), entrant_factory(drawing.conference)
    )
    for url in _urls(drawing, pick)[3:]:
        assert owner.get(url).status_code == 405, url


def test_unknown_ids_404(owner: Client) -> None:
    for action in ("claim", "not-here", "undo"):
        assert owner.post(reverse(f"booth-{action}", args=[999])).status_code == 404
    assert owner.post(reverse("booth-start", args=[999])).status_code == 404
    assert owner.post(reverse("booth-close", args=[999])).status_code == 404


def _digital_line(
    drawing: Drawing,
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
    **kwargs: Any,
) -> PrizeLine:
    _entrants(entrant_factory, drawing.conference, 2)
    return prize_line_factory(drawing, kind=PrizeLine.Kind.DIGITAL, **kwargs)


def test_digital_code_shown_when_drip_not_configured(
    owner: Client,
    drawing: Drawing,
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
) -> None:
    line = _digital_line(
        drawing,
        prize_line_factory,
        entrant_factory,
        url_template="https://example.com/redeem?c={code}",
        code_mode=PrizeLine.CodeMode.POOL,
    )
    CouponCode.objects.create(prize_line=line, code="SECRET&1")
    owner.post(reverse("booth-start", args=[drawing.pk]))
    page = _page(owner, drawing)
    assert "SECRET&amp;1" in page
    assert "https://example.com/redeem?c=SECRET%261" in page


def test_digital_code_hidden_when_drip_configured(
    owner: Client,
    drawing: Drawing,
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
    settings: Any,
) -> None:
    settings.DRIP_ACCOUNT_ID = "1234567"
    settings.DRIP_API_KEY = "key"
    line = _digital_line(
        drawing,
        prize_line_factory,
        entrant_factory,
        url_template="https://example.com/redeem?c={code}",
        code_mode=PrizeLine.CodeMode.POOL,
    )
    CouponCode.objects.create(prize_line=line, code="SECRET1")
    owner.post(reverse("booth-start", args=[drawing.pk]))
    page = _page(owner, drawing)
    assert "SECRET1" not in page
    assert "example.com/redeem" not in page
    assert "Digital" in page


def test_shared_code_digital_pick_shows_shared_code(
    owner: Client,
    drawing: Drawing,
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
) -> None:
    _digital_line(
        drawing,
        prize_line_factory,
        entrant_factory,
        code_mode=PrizeLine.CodeMode.SHARED,
        shared_code="SHARED7",
        url_template="https://example.com/s",
    )
    owner.post(reverse("booth-start", args=[drawing.pk]))
    page = _page(owner, drawing)
    assert "SHARED7" in page
    assert "https://example.com/s" in page


def test_digital_pick_without_code_shows_no_code(
    owner: Client,
    drawing: Drawing,
    prize_line_factory: Callable[..., PrizeLine],
    entrant_factory: Callable[..., Entrant],
) -> None:
    _digital_line(drawing, prize_line_factory, entrant_factory)
    owner.post(reverse("booth-start", args=[drawing.pk]))
    page = _page(owner, drawing)
    assert "Digital" in page
    assert 'class="code"' not in page


def test_vendored_htmx_matches_recorded_sha256() -> None:
    js = APP / "static/raffle/vendor/htmx-2.0.11.min.js"
    assert hashlib.sha256(js.read_bytes()).hexdigest() == HTMX_SHA256
    assert HTMX_SHA256 in (APP / "templates/raffle/base.html").read_text()


def test_page_loads_htmx_and_csrf_header_without_inline_script(owner: Client) -> None:
    response = owner.get(reverse("booth-home"))
    html = response.content.decode()
    assert re.search(r'<script src="[^"]*htmx-2\.0\.11\.min\.js"', html)
    assert re.search(r"""hx-headers='\{"X-CSRFToken": "[A-Za-z0-9]{32,64}"\}'""", html)
    assert "<script>" not in html
    assert "style=" not in html
    assert response["Cache-Control"] == "private, no-store"


def test_booth_css_has_big_touch_targets() -> None:
    css = (APP / "static/raffle/site.css").read_text()
    assert ".pick button" in css
    assert "min-height: 3rem" in css


def test_drip_settings_none_unless_both_present(settings: Any) -> None:
    assert drip_settings() is None
    settings.DRIP_ACCOUNT_ID = "42"
    assert drip_settings() is None
    settings.DRIP_ACCOUNT_ID = ""
    settings.DRIP_API_KEY = "sekrit-key"
    assert drip_settings() is None
    settings.DRIP_ACCOUNT_ID = "42"
    config = drip_settings()
    assert config is not None
    assert config.account_id == "42"
    assert config.api_key.get_secret_value() == "sekrit-key"
    assert "sekrit" not in repr(config)


@pytest.mark.parametrize("bad", ["", "12a", "１２", "1 2", "-1"])
def test_drip_account_id_must_be_digits(bad: str) -> None:
    with pytest.raises(ValidationError):
        DripSettings.model_validate({"account_id": bad, "api_key": "k"})


def test_rng_factory_is_system_random(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    assert isinstance(booth._rng(), secrets.SystemRandom)
