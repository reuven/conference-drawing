import json
from pathlib import Path

import pytest
from django.test import Client, RequestFactory

from conference_drawing import __version__
from conference_drawing.ops import version_context


def test_health_get_and_head(client: Client) -> None:
    assert client.get("/health").json() == {"status": "ok"}
    assert client.head("/health").status_code == 200


def test_version_has_all_keys_and_never_500s(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CD_COMMIT", raising=False)
    monkeypatch.delenv("CD_VERSION_FILE", raising=False)
    body = client.get("/version").json()
    assert set(body) == {"version", "commit", "commit_short", "built_at", "branch"}
    assert body["commit"] == ""
    assert body["version"] == __version__


def test_version_reads_deploy_file(
    client: Client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    f = tmp_path / "version.json"
    f.write_text(
        json.dumps({"commit": "abcdef1234567", "branch": "main", "extra": "ignored"})
    )
    monkeypatch.setenv("CD_VERSION_FILE", str(f))
    body = client.get("/version").json()
    assert body["commit"] == "abcdef1234567"
    assert body["commit_short"] == "abcdef1"
    assert body["branch"] == "main"
    assert body["built_at"] == ""
    assert "extra" not in body


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '{"commit": 5}'])
def test_version_survives_bad_deploy_file(
    client: Client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str
) -> None:
    f = tmp_path / "version.json"
    f.write_text(content)
    monkeypatch.setenv("CD_VERSION_FILE", str(f))
    r = client.get("/version")
    assert r.status_code == 200
    assert r.json()["commit"] == ""


def test_version_survives_missing_deploy_file(
    client: Client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CD_VERSION_FILE", str(tmp_path / "nope.json"))
    assert client.get("/version").json()["commit"] == ""


def test_html_responses_carry_default_headers(client: Client) -> None:
    r = client.get("/admin/login/")
    assert r["Cache-Control"] == "private, no-store"
    assert r["X-Robots-Tag"] == "noindex, nofollow"
    csp = r["Content-Security-Policy"]
    assert "script-src 'self'" in csp
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_non_html_responses_keep_their_own_headers(client: Client) -> None:
    assert "X-Robots-Tag" not in client.get("/health")


def test_version_context_exposes_version(rf: RequestFactory) -> None:
    ctx = version_context(rf.get("/"))
    assert ctx["cd_version"]["version"] == __version__
