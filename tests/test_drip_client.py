"""DripClient: request shape, auth, and error mapping, against mocked HTTP only."""

import base64
import datetime
import email.utils
import json
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import respx
from pydantic import SecretStr

from conference_drawing.raffle.drip.client import (
    DEADLINE_SECONDS,
    DripClient,
    DripError,
    build_http_client,
)
from conference_drawing.raffle.drip.config import DripSettings
from conference_drawing.raffle.drip.payloads import EventPayload, SubscriberPayload

BASE = "https://api.getdrip.com/v2/1234567"
UA = "conference-drawing (+https://github.com/reuven/conference-drawing)"
EVENT = EventPayload(
    email="a@example.com",
    action="Won raffle prize",
    properties={"book_title": "B"},
    occurred_at=datetime.datetime(2027, 5, 11, 21, 0, tzinfo=datetime.UTC),
)


@pytest.fixture
def http() -> Iterator[httpx.Client]:
    with httpx.Client() as client:
        yield client


@pytest.fixture
def drip_settings_fixture() -> DripSettings:
    return DripSettings(account_id="1234567", api_key=SecretStr("fake-key"))


@pytest.fixture
def drip(http: httpx.Client) -> DripClient:
    return DripClient(
        DripSettings(account_id="1234567", api_key=SecretStr("fake-key")), http
    )


def test_client_posts_expected_json_and_user_agent(
    respx_mock: respx.MockRouter, drip: DripClient
) -> None:
    route = respx_mock.post(f"{BASE}/events").mock(return_value=httpx.Response(204))
    drip.record_event(EVENT)
    request = route.calls.last.request
    assert json.loads(request.content) == {"events": [EVENT.to_json()]}
    assert request.headers["User-Agent"] == UA
    assert request.headers["Content-Type"] == "application/json"
    expected = base64.b64encode(b"fake-key:").decode()
    assert request.headers["Authorization"] == f"Basic {expected}"
    assert request.extensions["timeout"]["read"] == 10


def test_upsert_subscribers_posts_batches(
    respx_mock: respx.MockRouter, drip: DripClient
) -> None:
    route = respx_mock.post(f"{BASE}/subscribers/batches").mock(
        return_value=httpx.Response(201)
    )
    subs = [
        SubscriberPayload(email=f"p{i}@example.com", first_name="P", tags=["t"])
        for i in range(1001)
    ]
    drip.upsert_subscribers(subs)
    assert route.call_count == 2
    sizes = [
        len(json.loads(c.request.content)["batches"][0]["subscribers"])
        for c in route.calls
    ]
    assert sizes == [1000, 1]


def test_upsert_nothing_makes_no_call(drip: DripClient) -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.post(f"{BASE}/subscribers/batches")
        drip.upsert_subscribers([])
    assert route.call_count == 0


def test_client_raises_drip_error_on_422(
    respx_mock: respx.MockRouter, drip: DripClient
) -> None:
    respx_mock.post(f"{BASE}/events").mock(
        return_value=httpx.Response(422, json={"errors": ["a@example.com is bad"]})
    )
    with pytest.raises(DripError) as caught:
        drip.record_event(EVENT)
    assert caught.value.status_code == 422
    assert "a@example.com" not in str(caught.value)
    assert not caught.value.retryable


@pytest.mark.parametrize(
    ("status", "retryable"), [(400, False), (404, False), (429, True), (503, True)]
)
def test_drip_error_retryable_rule(status: int, retryable: bool) -> None:
    assert DripError(status).retryable is retryable


def _throttled(
    drip: DripClient, respx_mock: respx.MockRouter, **headers: str
) -> DripError:
    respx_mock.post(f"{BASE}/events").mock(
        return_value=httpx.Response(429, headers=headers)
    )
    with pytest.raises(DripError) as caught:
        drip.record_event(EVENT)
    return caught.value


def test_429_carries_retry_after_seconds(
    respx_mock: respx.MockRouter, drip: DripClient
) -> None:
    assert _throttled(drip, respx_mock, **{"Retry-After": "120"}).retry_after == 120


def test_429_parses_an_http_date_retry_after(
    respx_mock: respx.MockRouter, drip: DripClient
) -> None:
    when = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=300)
    header = email.utils.format_datetime(when, usegmt=True)
    error = _throttled(drip, respx_mock, **{"Retry-After": header})
    assert error.retry_after is not None
    assert 295 <= error.retry_after <= 300


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("999999", 3600),
        ("-5", 0),
        ("Mon, 01 Jan 2001 00:00:00 GMT", 0),
        ("Fri, 01 Jan 2100 00:00:00 -0000", 3600),  # no zone: read as UTC
        ("soon", 60),
        ("", 60),
        ("nan", 60),
        ("inf", 60),
    ],
)
def test_429_retry_after_is_clamped_and_garbage_defaults(
    respx_mock: respx.MockRouter, drip: DripClient, header: str, expected: float
) -> None:
    error = _throttled(drip, respx_mock, **{"Retry-After": header})
    assert error.retry_after == expected


def test_429_without_retry_after_defaults_to_60(
    respx_mock: respx.MockRouter, drip: DripClient
) -> None:
    assert _throttled(drip, respx_mock).retry_after == 60


def test_other_errors_have_no_retry_after(
    respx_mock: respx.MockRouter, drip: DripClient
) -> None:
    respx_mock.post(f"{BASE}/events").mock(return_value=httpx.Response(503))
    with pytest.raises(DripError) as caught:
        drip.record_event(EVENT)
    assert caught.value.retry_after is None


def test_http_client_ignores_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The API key must never be routed through a proxy named in the environment."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    with httpx.Client() as default, build_http_client() as ours:
        assert default._mounts  # the default client would use the proxy
        assert not ours._mounts


def test_http_client_has_per_phase_timeouts() -> None:
    with build_http_client() as client:
        assert client.timeout == httpx.Timeout(10.0)


def test_total_deadline_is_thirty_seconds() -> None:
    assert DEADLINE_SECONDS == 30


def test_a_slow_response_is_cut_off_at_the_overall_deadline(
    drip_settings_fixture: DripSettings,
) -> None:
    release = threading.Event()

    def slow(request: httpx.Request) -> httpx.Response:
        release.wait(5)
        return httpx.Response(204)

    http = httpx.Client(transport=httpx.MockTransport(slow))
    client = DripClient(drip_settings_fixture, http, deadline=0.1)
    started = time.monotonic()
    try:
        with pytest.raises(httpx.TimeoutException) as caught:
            client.record_event(EVENT)
        assert time.monotonic() - started < 1
    finally:
        release.set()  # let the abandoned thread finish while output is captured
        time.sleep(0.2)
    assert "a@example.com" not in str(caught.value)


def test_a_fast_response_is_unaffected_by_the_deadline(
    drip_settings_fixture: DripSettings,
) -> None:
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(204)))
    DripClient(drip_settings_fixture, http, deadline=5).record_event(EVENT)


def test_an_error_inside_the_deadline_still_propagates(
    drip_settings_fixture: DripSettings,
) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    http = httpx.Client(transport=httpx.MockTransport(broken))
    with pytest.raises(httpx.ConnectError):
        DripClient(drip_settings_fixture, http, deadline=5).record_event(EVENT)


def test_no_deadline_runs_the_request_on_the_calling_thread(
    drip_settings_fixture: DripSettings,
) -> None:
    threads: list[int] = []

    def record(request: httpx.Request) -> httpx.Response:
        threads.append(threading.get_ident())
        return httpx.Response(204)

    http = httpx.Client(transport=httpx.MockTransport(record))
    DripClient(drip_settings_fixture, http, deadline=None).record_event(EVENT)
    assert threads == [threading.get_ident()]
