"""Decide what a wrapped-up conference's entry page shows.

`resolve_after` is a pure function of the site settings and (optionally) one
conference, so views and templates never repeat the fallback rules.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from .markup import render_markdown
from .models import AfterMode, Conference, InstallSettings


class AfterContent(BaseModel):
    """What to show: a logo and rendered Markdown, or a redirect target."""

    model_config = ConfigDict(frozen=True)

    mode: Literal["page", "redirect"]
    logo_url: str | None
    html: str
    redirect_url: str | None

    @property
    def is_empty(self) -> bool:
        """True when a page would have neither a logo nor any text."""
        return self.logo_url is None and not self.html


def resolve_after(site: InstallSettings, conference: Conference | None) -> AfterContent:
    """Merge conference and site settings into the content to show.

    Why: each field (logo, Markdown, URL) falls back to the site's value on its
    own when the conference leaves it blank, so an owner can override only the
    text and keep the site logo. A conference mode of INHERIT uses the site's
    mode, and a redirect with no URL anywhere degrades to an (empty) page
    rather than a broken redirect.
    """
    logo = site.after_logo
    markdown = site.after_markdown
    url = site.after_redirect_url
    mode = site.after_mode
    if conference is not None:
        logo = conference.after_logo or logo
        markdown = conference.after_markdown or markdown
        url = conference.after_redirect_url or url
        if conference.after_mode != AfterMode.INHERIT:
            mode = conference.after_mode
    redirect = mode == AfterMode.REDIRECT and bool(url)
    return AfterContent(
        mode="redirect" if redirect else "page",
        logo_url=logo.url if logo else None,
        html=render_markdown(markdown),
        redirect_url=url if redirect else None,
    )
