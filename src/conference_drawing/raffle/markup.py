"""Safe Markdown rendering for owner-written pages.

The HTML returned here is marked ``|safe`` in a template, so this module is the
security boundary for owner content: raw HTML is escaped, images are off, and
links are limited to ``https``, ``http`` and ``mailto``.
"""

from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from markdown_it.token import Token
from markdown_it.utils import EnvType, OptionsDict

_WEB_SCHEMES = frozenset({"https", "http"})


class _SafeMarkdownIt(MarkdownIt):
    """MarkdownIt that refuses every link whose scheme is not allow-listed."""

    def validateLink(self, url: str) -> bool:
        """Allow only absolute https/http URLs with a host, and mailto.

        Called with the already-normalized URL by the link, autolink and
        reference rules. Relative links and fragments have no scheme and are
        refused, as is anything that does not parse.
        """
        try:
            parts = urlsplit(url.strip())
        except ValueError:
            return False
        scheme = parts.scheme.lower()
        if scheme == "mailto":
            return True
        return scheme in _WEB_SCHEMES and bool(parts.netloc)


def _link_open(
    self: RendererHTML,
    tokens: list[Token],
    idx: int,
    options: OptionsDict,
    env: EnvType,
) -> str:
    """Render ``<a>`` with ``rel="noopener noreferrer"`` (no window.opener leak)."""
    token = tokens[idx]
    if token.attrGet("href") == "":
        # markdown-it skips validateLink for an empty destination; drop the
        # empty href so no link survives that the validator never saw.
        del token.attrs["href"]
    token.attrSet("rel", "noopener noreferrer")
    return self.renderToken(tokens, idx, options, env)


def _build() -> MarkdownIt:
    md = _SafeMarkdownIt("commonmark", {"html": False})
    md.disable("image")
    md.add_render_rule("link_open", _link_open)
    return md


_MD = _build()


def render_markdown(text: str) -> str:
    """Render owner-written Markdown to HTML that is safe to mark ``|safe``.

    Use this for any owner-authored text shown to the public. Returns ``""``
    for empty or whitespace-only input.
    """
    if not text.strip():
        return ""
    html: str = _MD.render(text)
    return html
