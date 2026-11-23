from collections.abc import Sequence
from html.parser import HTMLParser
from urllib.parse import urlsplit

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from markdown_it.token import Token
from markdown_it.utils import EnvType, OptionsDict

from conference_drawing.raffle.markup import _MD, _build, _link_open, render_markdown


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
    ("source", "survives"),
    [
        ("[link text](javascript:alert(1))", "javascript:alert(1)"),
        ("[link text](JAVASCRIPT:alert(1))", "JAVASCRIPT:alert(1)"),
        ("[link text](data:text/html,x)", "data:text/html,x"),
        ("[link text](vbscript:x)", "vbscript:x"),
        ("[link text](/relative)", "/relative"),
        ("[link text](#frag)", "#frag"),
        ("[link text](https:/admin)", "https:/admin"),
        ("[link text](https:admin)", "https:admin"),
        ("[link text](http:///x)", "http:///x"),
        ("<javascript:alert(1)>", "&lt;javascript:alert(1)&gt;"),
        ("[link text][r]\n\n[r]: javascript:alert(1)", "javascript:alert(1)"),
        ("[link text](jav&#x61;script:x)", "script:x"),
    ],
)
def test_refused_links_render_as_text(source: str, survives: str) -> None:
    out = render_markdown(source)
    assert "href=" not in out
    assert "<a " not in out
    assert survives in out
    if source.startswith("[link text]"):
        assert "link text" in out


def test_empty_destination_has_no_href() -> None:
    assert "href" not in render_markdown("[a]()")


def test_unparseable_url_is_refused() -> None:
    # urlsplit raises ValueError on an unbalanced IPv6 bracket; refuse, don't crash.
    assert _MD.validateLink("http://[::1") is False


def test_empty_is_empty() -> None:
    assert render_markdown("  \n") == ""


_FRAGMENTS = st.sampled_from(
    ["<script>", "javascript:", "](data:", "onerror=", "![x](", "[x](", ")", "<", ">"]
)
_SCHEMES = st.sampled_from(
    [
        "javascript",
        "JaVaScRiPt",
        "jav&#x61;script",
        "jav&#97;script",
        "data",
        "vbscript",
        "https",
        "http",
        "mailto",
        "",
    ]
)
_LINKS = st.builds(
    "[t]({}:{})".format,
    _SCHEMES,
    st.sampled_from(["x", "//example.com", "/a", "alert(1)"]),
)


class _Audit(HTMLParser):
    """Collect real tags and attributes from rendered HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.bad: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "img"}:
            self.bad.append(tag)
        for name, value in attrs:
            if name.lower().startswith("on"):
                self.bad.append(name)
            if name.lower() == "href":
                parts = urlsplit((value or "").strip())
                scheme = parts.scheme.lower()
                if scheme not in {"https", "http", "mailto"}:
                    self.bad.append(f"href={value}")
                if scheme in {"https", "http"} and not parts.netloc:
                    self.bad.append(f"href={value}")


@example("[x](javascript:alert(1)) <javascript:alert(1)> [y](data:text/html,x)")
@example('[x](https://example.com "\\" onerror=x")')
@given(st.lists(st.one_of(st.text(), _FRAGMENTS, _LINKS), max_size=12).map("".join))
def test_never_emits_active_content(text: str) -> None:
    out = render_markdown(text)
    audit = _Audit()
    audit.feed(out)
    assert audit.bad == []
    assert "<script" not in out
    assert "<img" not in out


# --- configuration pins -------------------------------------------------------
# `_MD` is built once at import, so these tests call `_build()` afresh: the
# parser's options are what keep owner HTML out of a `|safe` template.


def test_build_escapes_raw_html_and_drops_images() -> None:
    md = _build()
    out = md.render("<script>x</script> ![a](https://example.com/x.png)")
    assert "<script" not in out
    assert "&lt;script&gt;" in out
    assert "<img" not in out


def test_build_is_commonmark_without_linkify_tables_or_strikethrough() -> None:
    out = _build().render("see example.com\n\n| a |\n|---|\n| b |\n\n~~gone~~")
    assert "<a " not in out
    assert "<table" not in out
    assert "<s>" not in out


def test_build_rewrites_links_with_rel_and_refuses_bad_ones() -> None:
    md = _build()
    assert md.render("[t](https://example.com)") == (
        '<p><a href="https://example.com" rel="noopener noreferrer">t</a></p>\n'
    )
    assert "<a " not in md.render("[t](javascript:alert(1))")


def test_link_open_passes_options_and_env_to_the_renderer() -> None:
    seen: list[tuple[object, object]] = []

    class Spy(RendererHTML):
        def renderToken(  # noqa: N802
            self, tokens: Sequence[Token], idx: int, options: OptionsDict, env: EnvType
        ) -> str:
            seen.append((options, env))
            return ""

    token = Token("link_open", "a", 1)
    token.attrs = {"href": "https://example.com"}
    marker_env: EnvType = {"k": 1}
    options = MarkdownIt().options
    _link_open(Spy(MarkdownIt()), [token], 0, options, marker_env)
    assert seen == [(options, marker_env)]
