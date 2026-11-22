"""Operational endpoints: liveness, build identity and robots.txt."""

import json
import os
from pathlib import Path

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.http import require_safe

from conference_drawing import __version__

_KEYS = ("commit", "built_at", "branch")


def version_info() -> dict[str, str]:
    """Return build identity: package version plus fields from the deploy file.

    The deploy script writes a JSON file named by ``CD_VERSION_FILE``. Anything
    missing or malformed degrades to empty strings so ``/version`` never 500s.
    """
    info = {"version": __version__, **dict.fromkeys(_KEYS, "")}
    path = os.environ.get("CD_VERSION_FILE")
    if path:
        try:
            data = json.loads(Path(path).read_text())
        except OSError, ValueError:
            data = {}
        if isinstance(data, dict):
            info.update({k: data[k] for k in _KEYS if isinstance(data.get(k), str)})
    return {**info, "commit_short": info["commit"][:7]}


def version_context(request: HttpRequest) -> dict[str, dict[str, str]]:
    """Template context processor exposing build identity to the footer."""
    return {"cd_version": version_info()}


@require_safe
def robots_txt(request: HttpRequest) -> HttpResponse:
    """Ask every crawler to stay out; pages also send X-Robots-Tag noindex."""
    return HttpResponse(
        "User-agent: *\nDisallow: /\n", content_type="text/plain; charset=utf-8"
    )


@require_safe
def health(request: HttpRequest) -> JsonResponse:
    """Liveness probe for the reverse proxy and deploy script."""
    return JsonResponse({"status": "ok"})


@require_safe
def version(request: HttpRequest) -> JsonResponse:
    """Report which build is running, so a deploy can be verified."""
    return JsonResponse(version_info())
