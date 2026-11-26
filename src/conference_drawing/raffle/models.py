"""Data model for the raffle app.

All instants are stored in UTC; `Conference.time_zone` governs how organizers
enter and read them (see `timezones.py`).
"""

import re
import secrets
import zoneinfo
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import Storage
from django.core.validators import (
    MaxLengthValidator,
    MinValueValidator,
    RegexValidator,
)
from django.db import models, transaction
from django.db.models import Q
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone

from .text import normalize_email, validate_single_line_label
from .timezones import time_zone_choices

# Re-exported (`as` makes it explicit for mypy): migration 0005 names the
# validators as `conference_drawing.raffle.models.<name>`.
from .validators import (
    MAX_REDIRECT_URL_LENGTH,
)
from .validators import (
    logo_upload_to as logo_upload_to,
)
from .validators import (
    validate_logo as validate_logo,
)
from .validators import (
    validate_redirect_url as validate_redirect_url,
)

DEFAULT_FOLLOWUP_ACK_TEXT = (
    "Entering means one follow-up email from me after the conference."
)
DEFAULT_BD_CHECKBOX_TEXT = "Also subscribe me to Better Developers"

# \A and \Z, not ^ and $: `$` also matches before a trailing newline.
_SLUG_RE = r"\A[a-z0-9]+(-[a-z0-9]+)*\Z"
_slug_validator = RegexValidator(
    _SLUG_RE, "Use lowercase letters, digits and single hyphens."
)

# Booth URL segments that sit beside /booth/<slug>/; a conference with one of
# these slugs would be shadowed by (or shadow) the sign-in and action routes.
RESERVED_SLUGS = frozenset({"login", "logout", "drawing", "pick"})


def validate_slug_not_reserved(value: str) -> None:
    """Refuse a slug that a fixed /booth/ route already owns.

    Why: ``/booth/login/`` must always be the sign-in page, never a conference.
    """
    if value in RESERVED_SLUGS:
        raise ValidationError(
            "\u201c%(word)s\u201d is reserved; choose another slug.",
            code="reserved_slug",
            params={"word": value},
        )


# Question keys: the default ones use underscores (want_to_learn).
_QUESTION_KEY_RE = re.compile(r"\A[a-z0-9]+([_-][a-z0-9]+)*\Z")

# A question key becomes a CSV column and a Drip event property, so it may not
# shadow a fixed CSV column or the event's own `conference` property.
RESERVED_QUESTION_KEYS = frozenset(
    {
        "name",
        "email",
        "entered_at",
        "status",
        "won",
        "digital_codes",
        "digital_urls",
        "followup_ack_at",
        "bd_optin",
        "bd_optin_at",
        "conference",
    }
)


def validate_question_key(value: str) -> None:
    """Reject a key that isn't a lowercase slug or that a fixed column already uses.

    Why: the key is the answer's CSV column and its Drip event property name.
    """
    if not _QUESTION_KEY_RE.match(value):
        raise ValidationError(
            "Use lowercase letters and digits, separated by single _ or -.",
            code="invalid",
        )
    if value in RESERVED_QUESTION_KEYS:
        raise ValidationError(
            f"{value!r} is reserved: the export already has a column by that name.",
            code="reserved",
        )


# Browsers always empty a file picker when a form comes back with errors, so
# every file field says so; nothing else on the form is lost.
CHOOSE_FILE_AGAIN = "If a save fails, choose the file again."


def validate_time_zone(value: str) -> None:
    """Reject anything that is not an IANA zone name known to this system."""
    if value not in zoneinfo.available_timezones():
        raise ValidationError(f"Unknown time zone: {value!r}.", code="invalid")


def validate_label(value: str) -> None:
    """Model-field adapter: run `validate_single_line_label`, discard its result."""
    validate_single_line_label(value)
    if value != value.strip():
        raise ValidationError(
            "Remove leading or trailing whitespace.", code="untrimmed"
        )


def new_display_token() -> str:
    """Return an unguessable token that authorizes the public display URL."""
    return secrets.token_urlsafe(32)


MAX_AFTER_MARKDOWN_LENGTH = 20_000


class AfterMode(models.TextChoices):
    """What a wrapped-up conference's entry page does."""

    PAGE = "page", "Show a page"
    REDIRECT = "redirect", "Redirect"
    INHERIT = "inherit", "Use the install-wide setting"


def _stored_logo_name(instance: models.Model) -> str:
    """The logo name the database holds for `instance`, or "" if it has none yet.

    Read before saving so the file being replaced is known even though the
    in-memory field already holds the new upload.
    """
    if instance.pk is None:
        return ""
    return str(
        type(instance)
        ._default_manager.filter(pk=instance.pk)
        .values_list("after_logo", flat=True)
        .first()
        or ""
    )


def _delete_on_commit(name: str | None, storage: Storage) -> None:
    """Delete a stored logo once the surrounding transaction commits.

    Why on commit: a rolled-back save must not lose the file the database still
    points at. Why the re-check: a stale admin tab may have written the name
    back, so a file some row still references is kept. `robust` keeps a storage
    error from turning a committed save into a 500. `delete` is a no-op for a
    file that is already gone.
    """
    if not name:
        return

    def delete() -> None:
        in_use = any(
            model.objects.filter(after_logo=name).exists()
            for model in (InstallSettings, Conference)
        )
        if not in_use:
            storage.delete(name)

    transaction.on_commit(delete, robust=True)


def _save_replacing_logo(
    instance: models.Model, save: Any, *args: Any, **kwargs: Any
) -> None:
    """Run `save`, then queue deletion of the logo file it replaced or cleared.

    A save that names `update_fields` without `after_logo` leaves the stored
    logo alone, so it must not queue anything.
    """
    fields = kwargs.get("update_fields")
    if fields is not None and "after_logo" not in fields:
        save(*args, **kwargs)
        return
    with transaction.atomic():
        old = _stored_logo_name(instance)
        save(*args, **kwargs)
        if old != instance.after_logo.name:  # type: ignore[attr-defined]
            _delete_on_commit(old, instance.after_logo.storage)  # type: ignore[attr-defined]


_NEEDS_URL = "Redirect mode needs a URL (here or in the install settings)."


class ConferenceTags(NamedTuple):
    """The four Drip tags a conference uses."""

    all: str
    winner: str
    nonwinner: str
    bd_optin: str


class InstallSettings(models.Model):
    """Singleton row of per-install copy: owner name, privacy note, consent text."""

    owner_name = models.CharField(max_length=200, blank=True)
    privacy_note = models.TextField(blank=True)
    followup_ack_text = models.TextField(default=DEFAULT_FOLLOWUP_ACK_TEXT)
    bd_checkbox_text = models.TextField(default=DEFAULT_BD_CHECKBOX_TEXT)
    after_mode = models.CharField(
        max_length=10,
        choices=[(AfterMode.PAGE, "Show a page"), (AfterMode.REDIRECT, "Redirect")],
        default=AfterMode.PAGE,
        db_default=AfterMode.PAGE,
    )
    after_logo = models.ImageField(
        upload_to=logo_upload_to,
        blank=True,
        default="",
        db_default="",
        validators=[validate_logo],
        help_text="PNG, JPEG, WebP or GIF, up to 2 MB. " + CHOOSE_FILE_AGAIN,
    )
    after_markdown = models.TextField(
        blank=True,
        default="",
        db_default="",
        validators=[MaxLengthValidator(MAX_AFTER_MARKDOWN_LENGTH)],
        help_text=(
            "Markdown, up to 20,000 characters. Links (https, http, mailto) work; "
            "raw HTML and images don't \u2014 put a logo in the logo field."
        ),
    )
    after_redirect_url = models.URLField(
        max_length=MAX_REDIRECT_URL_LENGTH,
        blank=True,
        default="",
        db_default="",
        validators=[validate_redirect_url],
        help_text="https only.",
    )

    class Meta:
        verbose_name = "install settings"
        verbose_name_plural = "install settings"

    def __str__(self) -> str:
        return "Install settings"

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.pk = 1
        _save_replacing_logo(self, super().save, *args, **kwargs)

    def clean(self) -> None:
        if self.after_mode == AfterMode.REDIRECT and not self.after_redirect_url:
            raise ValidationError({"after_redirect_url": _NEEDS_URL})

    @classmethod
    def get(cls) -> InstallSettings:
        """Return the one settings row, creating it with defaults on first use."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class Book(models.Model):
    """A book that can be a prize; reused across conferences."""

    title = models.CharField(max_length=200, validators=[validate_label])
    cover = models.ImageField(
        upload_to="covers/", blank=True, help_text=CHOOSE_FILE_AGAIN
    )
    link = models.URLField(blank=True)

    def __str__(self) -> str:
        return self.title


class Conference(models.Model):
    """One event at which drawings are run; owns its entrants and settings."""

    name = models.CharField(max_length=200, validators=[validate_label])
    slug = models.CharField(
        max_length=60,
        unique=True,
        validators=[_slug_validator, validate_slug_not_reserved],
    )
    time_zone = models.CharField(
        max_length=64,
        choices=time_zone_choices,
        validators=[validate_time_zone],
        help_text="Click the list and type a city name to jump to it.",
    )
    booth_location = models.CharField(max_length=200, blank=True)
    starts_on = models.DateField()
    ends_on = models.DateField()
    exclusion_emails = models.TextField(blank=True)
    display_token = models.CharField(max_length=64, default=new_display_token)
    wrapped_up_at = models.DateTimeField(null=True, blank=True)
    deleted_entrant_count = models.PositiveIntegerField(null=True, blank=True)
    after_mode = models.CharField(
        max_length=10,
        choices=AfterMode,
        default=AfterMode.INHERIT,
        db_default=AfterMode.INHERIT,
    )
    after_logo = models.ImageField(
        upload_to=logo_upload_to,
        blank=True,
        default="",
        db_default="",
        validators=[validate_logo],
        help_text="PNG, JPEG, WebP or GIF, up to 2 MB. " + CHOOSE_FILE_AGAIN,
    )
    after_markdown = models.TextField(
        blank=True,
        default="",
        db_default="",
        validators=[MaxLengthValidator(MAX_AFTER_MARKDOWN_LENGTH)],
        help_text=(
            "Markdown, up to 20,000 characters. Links (https, http, mailto) work; "
            "raw HTML and images don't \u2014 put a logo in the logo field."
        ),
    )
    after_redirect_url = models.URLField(
        max_length=MAX_REDIRECT_URL_LENGTH,
        blank=True,
        default="",
        db_default="",
        validators=[validate_redirect_url],
        help_text="https only.",
    )
    helpers = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="helped_conferences",
        help_text=(
            "People who can run this conference's drawings, until it is wrapped "
            "up. Add accounts under Users."
        ),
    )

    def __str__(self) -> str:
        return self.name

    def save(self, *args: Any, **kwargs: Any) -> None:
        _save_replacing_logo(self, super().save, *args, **kwargs)

    def clean(self) -> None:
        errors: dict[str, str] = {}
        if self.starts_on and self.ends_on and self.ends_on < self.starts_on:
            errors["ends_on"] = "The end date is before the start date."
        if self.after_mode == AfterMode.REDIRECT and not self.after_redirect_url:
            # Read-only lookup: clean() must not create the site row.
            site_url = (
                InstallSettings.objects.filter(pk=1)
                .values_list("after_redirect_url", flat=True)
                .first()
            )
            if not site_url:
                errors["after_redirect_url"] = _NEEDS_URL
        if errors:
            raise ValidationError(errors)

    @property
    def excluded_emails(self) -> frozenset[str]:
        """Normalized addresses barred from winning (hosts, staff), as a set."""
        parts = re.split(r"[\s,;]+", self.exclusion_emails)
        return frozenset(normalize_email(p) for p in parts if p.strip())

    def tags(self) -> ConferenceTags:
        """Return this conference's Drip tags, derived from the slug."""
        s = self.slug
        return ConferenceTags(
            all=f"{s}-raffle",
            winner=f"{s}-raffle-winner",
            nonwinner=f"{s}-raffle-nonwinner",
            bd_optin=f"{s}-bd-optin",
        )


class Question(models.Model):
    """An optional question on the entry form."""

    class Kind(models.TextChoices):
        TEXT = "text"
        CHOICE = "choice"

    conference = models.ForeignKey(Conference, on_delete=models.CASCADE)
    key = models.CharField(max_length=60, validators=[validate_question_key])
    prompt = models.CharField(max_length=300)
    kind = models.CharField(max_length=10, choices=Kind, default=Kind.TEXT)
    choices = models.JSONField(default=list, blank=True)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["conference", "key"], name="question_key_unique_per_conference"
            )
        ]
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return self.key

    def clean(self) -> None:
        is_str_list = isinstance(self.choices, list) and all(
            isinstance(c, str) for c in self.choices
        )
        if not is_str_list:
            raise ValidationError({"choices": "Choices must be a list of strings."})
        if self.kind == self.Kind.CHOICE and not self.choices:
            raise ValidationError({"choices": "A choice question needs choices."})


_DEFAULT_QUESTIONS: tuple[tuple[str, str, str, list[str]], ...] = (
    ("want_to_learn", "What do you most want to learn?", "text", []),
    (
        "coding_style",
        "How do you code these days?",
        "choice",
        [
            "I'm just starting to code",
            "I write my own code, and use AI a little or not at all",
            "I work alongside AI: chat, autocomplete, asking it to explain",
            "AI agents write most of my code; I direct and review",
        ],
    ),
    ("anything_else", "Anything else to tell me?", "text", []),
)


def create_default_questions(conference: Conference) -> None:
    """Give a new conference the standard three optional entry questions."""
    Question.objects.bulk_create(
        Question(
            conference=conference,
            key=key,
            prompt=prompt,
            kind=kind,
            choices=choices,
            position=position,
        )
        for position, (key, prompt, kind, choices) in enumerate(_DEFAULT_QUESTIONS)
    )


class Entrant(models.Model):
    """A person who entered a conference's drawings, with their consent record."""

    conference = models.ForeignKey(Conference, on_delete=models.CASCADE)
    name = models.CharField(max_length=200, validators=[validate_label])
    email = models.EmailField()
    answers = models.JSONField(default=dict, blank=True)
    followup_ack_text = models.TextField(blank=True)
    followup_ack_at = models.DateTimeField(default=timezone.now)
    bd_optin_text = models.TextField(blank=True)
    bd_optin_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["conference", "email"],
                name="entrant_email_unique_per_conference",
            )
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.email = normalize_email(self.email)
        super().save(*args, **kwargs)


class Drawing(models.Model):
    """One scheduled drawing; at most one per conference is open at a time."""

    class Status(models.TextChoices):
        SCHEDULED = "scheduled"
        OPEN = "open"
        FINISHED = "finished"

    conference = models.ForeignKey(Conference, on_delete=models.CASCADE)
    scheduled_at = models.DateTimeField()
    status = models.CharField(max_length=10, choices=Status, default=Status.SCHEDULED)
    started_at = models.DateTimeField(null=True, blank=True)
    cutoff_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    awarded_count = models.PositiveIntegerField(default=0)
    # True only when the owner closed it; an auto-finish (last copy claimed)
    # leaves it False so a mis-tapped final Claimed can still be undone.
    closed_by_owner = models.BooleanField(default=False)

    class Meta:
        ordering = ["scheduled_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["conference"],
                condition=Q(status="open"),
                name="one_open_drawing_per_conference",
            )
        ]

    def __str__(self) -> str:
        return f"Drawing {self.pk}"


class PrizeLine(models.Model):
    """A batch of copies of one book awarded in a drawing."""

    class Kind(models.TextChoices):
        PHYSICAL = "physical"
        DIGITAL = "digital"

    class CodeMode(models.TextChoices):
        NONE = "", "None"
        POOL = "pool"
        SHARED = "shared"

    drawing = models.ForeignKey(Drawing, on_delete=models.CASCADE)
    book = models.ForeignKey(Book, on_delete=models.PROTECT)
    kind = models.CharField(max_length=10, choices=Kind)
    quantity = models.PositiveSmallIntegerField(
        default=1, validators=[MinValueValidator(1)]
    )
    position = models.PositiveSmallIntegerField(default=0)
    url_template = models.CharField(max_length=500, blank=True)
    code_mode = models.CharField(
        max_length=10, choices=CodeMode, blank=True, default=CodeMode.NONE
    )
    shared_code = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return f"{self.quantity} x {self.book.title}"

    def clean(self) -> None:
        if self.kind == self.Kind.PHYSICAL:
            if self.url_template or self.code_mode or self.shared_code:
                raise ValidationError("A physical prize cannot have code fields.")
            return
        if not self.url_template:
            raise ValidationError({"url_template": "A digital prize needs a URL."})
        # The booth screen renders this as a link, so refuse javascript: etc.
        parts = urlsplit(self.url_template)
        if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
            raise ValidationError(
                {"url_template": "Enter a full http:// or https:// URL."}
            )
        if not self.code_mode:
            raise ValidationError({"code_mode": "Choose where the codes come from."})
        if self.code_mode == self.CodeMode.SHARED and not self.shared_code:
            raise ValidationError({"shared_code": "Enter the shared code."})
        if self.code_mode == self.CodeMode.POOL and self.shared_code:
            raise ValidationError(
                {"shared_code": "A shared code only applies to shared mode."}
            )


class Pick(models.Model):
    """One drawn winner for a prize-line slot; replacements chain via `replaces`."""

    class Status(models.TextChoices):
        PENDING = "pending"
        CLAIMED = "claimed"
        NOT_HERE = "not_here"
        DIGITAL = "digital"
        VOID = "void"

    prize_line = models.ForeignKey(PrizeLine, on_delete=models.CASCADE)
    entrant = models.ForeignKey(Entrant, on_delete=models.CASCADE)
    slot = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    status = models.CharField(max_length=10, choices=Status, default=Status.PENDING)
    pool_size = models.PositiveIntegerField()
    drawn_at = models.DateTimeField(default=timezone.now)
    replaces = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    marked_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"Pick {self.pk} (slot {self.slot})"

    def clean(self) -> None:
        if self.prize_line_id and self.slot and self.slot > self.prize_line.quantity:
            raise ValidationError({"slot": "The slot exceeds the prize quantity."})


class CouponCode(models.Model):
    """One code in a digital prize line's pool, assigned to at most one pick."""

    prize_line = models.ForeignKey(PrizeLine, on_delete=models.CASCADE)
    code = models.CharField(max_length=200)
    pick = models.OneToOneField(Pick, null=True, blank=True, on_delete=models.SET_NULL)
    # Outlives `pick`: deleting an entrant nulls `pick`, but the code must never
    # return to the pool. `save()` sets it whenever a pick is assigned.
    used = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["prize_line", "code"], name="coupon_code_unique_per_line"
            ),
            models.CheckConstraint(
                condition=Q(pick__isnull=True) | Q(used=True),
                name="couponcode_pick_implies_used",
            ),
        ]

    def __str__(self) -> str:
        return self.code

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.pick_id is not None:
            self.used = True
            fields = kwargs.get("update_fields")
            if fields is not None:
                kwargs["update_fields"] = {*fields, "used"}
        super().save(*args, **kwargs)


class AuditEvent(models.Model):
    """Append-only record of a drawing's events; detail holds ids and counts only."""

    class Kind(models.TextChoices):
        STARTED = "started"
        PICKED = "picked"
        CLAIMED = "claimed"
        NOT_HERE = "not_here"
        UNDO = "undo"
        VOID = "void"
        EXHAUSTED = "exhausted"
        CLOSED = "closed"

    drawing = models.ForeignKey(Drawing, on_delete=models.CASCADE)
    kind = models.CharField(max_length=10, choices=Kind)
    pick = models.ForeignKey(Pick, null=True, blank=True, on_delete=models.SET_NULL)
    at = models.DateTimeField(default=timezone.now)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["at", "id"]

    def __str__(self) -> str:
        return f"{self.kind} event {self.pk}"


class OutboxJob(models.Model):
    """A queued Drip API call, retried by the worker until it succeeds or fails."""

    class Kind(models.TextChoices):
        EVENT = "event"
        SUBSCRIBERS = "subscribers"

    class Status(models.TextChoices):
        PENDING = "pending"
        SENT = "sent"
        FAILED = "failed"

    conference = models.ForeignKey(Conference, on_delete=models.CASCADE)
    kind = models.CharField(max_length=12, choices=Kind)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=10, choices=Status, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    last_error_class = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    sent_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.kind} job {self.pk} ({self.status})"


@receiver(post_delete, sender=Conference)
def delete_conference_logo(sender: Any, instance: Conference, **kwargs: Any) -> None:
    """Remove a deleted conference's logo file once the deletion commits."""
    _delete_on_commit(instance.after_logo.name, instance.after_logo.storage)
