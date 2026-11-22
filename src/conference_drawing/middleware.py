"""Response headers applied to every HTML page."""

from collections.abc import Callable

from django.http import HttpRequest, HttpResponseBase


class DefaultHeadersMiddleware:
    """Keep HTML out of caches and search indexes.

    Pages can show entrants' data, so they are never stored by shared caches
    and never indexed. Non-HTML responses (JSON, static files) are untouched.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        response = self.get_response(request)
        if response.headers.get("Content-Type", "").startswith("text/html"):
            response["Cache-Control"] = "private, no-store"
            response["X-Robots-Tag"] = "noindex, nofollow"
        return response
