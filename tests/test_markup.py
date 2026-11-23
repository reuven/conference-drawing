import re

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from conference_drawing.raffle.markup import _MD, render_markdown


def test_renders_headings_lists_and_links() -> None:
    out = render_markdown("## Hi\n\n- [site](https://example.com)")
    assert "<h2>Hi</h2>" in out
    assert '<a href="https://example.com" rel="noopener noreferrer">site</a>' in out


def test_raw_html_is_escaped() -> None:
    out = render_markdown("<script>x</script><b>y</b>")
    assert "&lt;script&gt;" in out
    assert "<script" not in out


def test_images_are_not_rendered() -> None:
    assert "<img" not in render_markdown("![a](https://example.com/x.png)")


@pytest.mark.parametrize(
    "url", ["https://example.com", "http://example.com", "mailto:a@example.com"]
)
def test_mailto_and_http_links_allowed(url: str) -> None:
    assert f'href="{url}"' in render_markdown(f"[t]({url})")


@pytest.mark.parametrize(
    "source",
    [
        "[link text](javascript:alert(1))",
        "[link text](JAVASCRIPT:alert(1))",
        "[link text](data:text/html,x)",
        "[link text](vbscript:x)",
        "[link text](/relative)",
        "[link text](#frag)",
        "<javascript:alert(1)>",
        "[link text][r]\n\n[r]: javascript:alert(1)",
        "[link text](jav&#x61;script:x)",
    ],
)
def test_refused_links_render_as_text(source: str) -> None:
    out = render_markdown(source)
    assert "href=" not in out
    assert "<a " not in out
    if source.startswith("[link text]("):
        assert "link text" in out


def test_unparseable_url_is_refused() -> None:
    # urlsplit raises ValueError on an unbalanced IPv6 bracket; refuse, don't crash.
    assert _MD.validateLink("http://[::1") is False


def test_empty_is_empty() -> None:
    assert render_markdown("  \n") == ""


_FRAGMENTS = st.sampled_from(
    ["<script>", "javascript:", "](data:", "onerror=", "![x](", "[x](", ")", "<", ">"]
)


@example("[x](javascript:alert(1)) <javascript:alert(1)> [y](data:text/html,x)")
@given(st.lists(st.one_of(st.text(), _FRAGMENTS), max_size=12).map("".join))
def test_never_emits_active_content(text: str) -> None:
    out = render_markdown(text)
    assert "<script" not in out
    assert "<img" not in out
    assert re.search(r'href="\s*(javascript|data):', out, re.I) is None
    assert re.search(r"<[^>]*\son\w+=", out) is None
