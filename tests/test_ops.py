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


def test_robots_txt_disallows_everything(client: Client) -> None:
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert r["Content-Type"] == "text/plain; charset=utf-8"
    assert r.content.decode() == "User-agent: *\nDisallow: /\n"
    assert client.head("/robots.txt").status_code == 200
    assert client.post("/robots.txt").status_code == 405


def test_non_html_responses_keep_their_own_headers(client: Client) -> None:
    assert "X-Robots-Tag" not in client.get("/health")


def test_version_context_exposes_version(rf: RequestFactory) -> None:
    ctx = version_context(rf.get("/"))
    assert ctx["cd_version"]["version"] == __version__


def _footer(html: str) -> str:
    return html.split("<footer>", 1)[1].split("</footer>", 1)[0]


def test_footer_shows_the_running_version_and_commit(
    client: Client, db: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    f = tmp_path / "version.json"
    f.write_text(json.dumps({"commit": "abcdef1234567"}))
    monkeypatch.setenv("CD_VERSION_FILE", str(f))
    footer = _footer(client.get("/privacy/").content.decode())
    assert 'href="/privacy/"' in footer
    assert f"v{__version__} (abcdef1)" in footer


def test_footer_without_a_deploy_file_shows_the_package_version(
    client: Client, db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CD_VERSION_FILE", raising=False)
    footer = _footer(client.get("/privacy/").content.decode())
    assert f"v{__version__}</span>" in footer
    assert "()" not in footer


def test_every_page_template_gets_the_footer() -> None:
    """Every full page extends base.html; partials (leading `_`) are fragments.

    The display and the printed flyer hide the footer with CSS (a clean
    monitor; one printed page), but it is still in their markup.
    """
    root = Path(version_context.__code__.co_filename).parent / "raffle" / "templates"
    pages = [p for p in root.rglob("*.html") if not p.name.startswith("_")]
    assert len(pages) > 10
    bases = (
        '{% extends "raffle/base.html" %}',
        '{% extends "raffle/booth/base.html" %}',
    )
    for page in pages:
        if page.relative_to(root).as_posix() == "raffle/base.html":
            continue
        assert page.read_text().startswith(bases), page
