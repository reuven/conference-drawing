"""A thin Drip v2 client: two calls, Basic auth, and errors that carry no data."""

import datetime
import email.utils
import math

import httpx

from .config import DripSettings
from .payloads import EventPayload, SubscriberPayload

API_ROOT = "https://api.getdrip.com/v2"
USER_AGENT = "conference-drawing (+https://github.com/reuven/conference-drawing)"
TIMEOUT_SECONDS = 10.0
BATCH_LIMIT = 1000
RETRY_AFTER_DEFAULT = 60.0
RETRY_AFTER_MAX = 3600.0


def parse_retry_after(value: str | None, *, now: datetime.datetime) -> float:
    """Turn a `Retry-After` header into seconds to wait, always within [0, 3600].

    The header is delta-seconds or an HTTP date. A missing or unparseable value
    waits a minute rather than hammering a service that just said "slow down".
    """
    if value is None:
        return RETRY_AFTER_DEFAULT
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except TypeError, ValueError:
            return RETRY_AFTER_DEFAULT
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.UTC)
        seconds = (when - now).total_seconds()
    if not math.isfinite(seconds):
        return RETRY_AFTER_DEFAULT
    return min(max(seconds, 0.0), RETRY_AFTER_MAX)


class DripError(Exception):
    """Drip answered with a non-2xx status.

    Only the status is kept: response bodies echo subscriber emails, and this
    exception's text ends up in logs. A 429 also carries `retry_after`, the
    seconds Drip asked us to wait (clamped); other errors leave it None.
    """

    def __init__(self, status_code: int, retry_after: float | None = None) -> None:
        super().__init__(f"Drip returned HTTP {status_code}")
        self.status_code = status_code
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        """True for throttling (429) and server errors; other 4xx will never succeed."""
        return self.status_code == 429 or self.status_code >= 500


class DripClient:
    """Calls Drip with an injected `httpx.Client` so tests can mock the transport."""

    def __init__(self, settings: DripSettings, http: httpx.Client) -> None:
        self._base = f"{API_ROOT}/{settings.account_id}"
        self._auth = httpx.BasicAuth(settings.api_key.get_secret_value(), "")
        self._http = http

    def _post(self, path: str, body: dict[str, object]) -> None:
        response = self._http.post(
            f"{self._base}/{path}",
            json=body,
            auth=self._auth,
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT_SECONDS,
        )
        if not response.is_success:
            retry_after = (
                parse_retry_after(
                    response.headers.get("Retry-After"),
                    now=datetime.datetime.now(datetime.UTC),
                )
                if response.status_code == 429
                else None
            )
            raise DripError(response.status_code, retry_after)

    def record_event(self, payload: EventPayload) -> None:
        """Record one custom event (Drip answers 204)."""
        self._post("events", {"events": [payload.to_json()]})

    def upsert_subscribers(self, payloads: list[SubscriberPayload]) -> None:
        """Create or update subscribers, at most 1000 per request."""
        for start in range(0, len(payloads), BATCH_LIMIT):
            chunk = payloads[start : start + BATCH_LIMIT]
            self._post(
                "subscribers/batches",
                {"batches": [{"subscribers": [p.to_json() for p in chunk]}]},
            )
