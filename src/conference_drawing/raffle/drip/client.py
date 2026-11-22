"""A thin Drip v2 client: two calls, Basic auth, and errors that carry no data."""

import httpx

from .config import DripSettings
from .payloads import EventPayload, SubscriberPayload

API_ROOT = "https://api.getdrip.com/v2"
USER_AGENT = "conference-drawing (+https://github.com/reuven/conference-drawing)"
TIMEOUT_SECONDS = 10.0
BATCH_LIMIT = 1000


class DripError(Exception):
    """Drip answered with a non-2xx status.

    Only the status is kept: response bodies echo subscriber emails, and this
    exception's text ends up in logs.
    """

    def __init__(self, status_code: int) -> None:
        super().__init__(f"Drip returned HTTP {status_code}")
        self.status_code = status_code

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
            raise DripError(response.status_code)

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
