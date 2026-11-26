"""Django admin: the owner's setup screens and read-only views of entrants/outbox."""

import datetime
from typing import Any

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.widgets import AdminSplitDateTime
from django.contrib.auth import get_user_model
from django.db.models import Count, Q, QuerySet
from django.http import HttpRequest
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from .codes import parse_codes
from .drip.outbox import requeue_failed
from .models import (
    Book,
    Conference,
    CouponCode,
    Drawing,
    Entrant,
    InstallSettings,
    OutboxJob,
    PrizeLine,
    Question,
    new_display_token,
)
from .timezones import to_local, to_utc

AFTER_FIELDS = (
    "after_mode",
    "after_logo_preview",
    "after_logo",
    "after_markdown",
    "after_redirect_url",
)


def _logo_preview(obj: InstallSettings | Conference | None) -> Any:
    """Show the current logo as a small image; sized by `raffle/admin.css`.

    Why a class and not `style=`: the CSP forbids inline styles.
    """
    if obj is None or not obj.after_logo:
        return "-"
    return format_html(
        '<img class="after-logo-thumb" src="{}" alt="Current logo">',
        obj.after_logo.url,
    )


class _AfterLogoMedia:
    class Media:
        css = {"all": ("raffle/admin.css",)}


@admin.register(InstallSettings)
class InstallSettingsAdmin(admin.ModelAdmin[InstallSettings], _AfterLogoMedia):
    """Per-install copy; one row only: no second add, no delete."""

    readonly_fields = ("after_logo_preview",)
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "owner_name",
                    "privacy_note",
                    "followup_ack_text",
                    "newsletter_checkbox_text",
                )
            },
        ),
        (
            "After a conference",
            {
                "fields": AFTER_FIELDS,
                "description": (
                    "Conferences use these unless they set their own, field by field."
                ),
            },
        ),
    )

    @admin.display(description="Current logo")
    def after_logo_preview(self, obj: InstallSettings | None) -> Any:
        return _logo_preview(obj)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return not InstallSettings.objects.exists()

    def has_delete_permission(
        self, request: HttpRequest, obj: InstallSettings | None = None
    ) -> bool:
        return False


@admin.register(Book)
class BookAdmin(admin.ModelAdmin[Book]):
    list_display = ("title", "link")
    search_fields = ("title",)


class QuestionInline(admin.TabularInline[Question, Conference]):
    model = Question
    extra = 0


@admin.register(Conference)
class ConferenceAdmin(admin.ModelAdmin[Conference], _AfterLogoMedia):
    """Conferences with their optional questions and exclusion list.

    `display_token` authorizes the public display URL, so it is read-only here
    and never shown in the list.
    """

    list_display = ("name", "slug", "starts_on", "ends_on", "time_zone")
    search_fields = ("name", "slug")
    readonly_fields = (
        "pages",
        "display_token",
        "wrapped_up_at",
        "deleted_entrant_count",
        "after_logo_preview",
    )
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "name",
                    "slug",
                    "pages",
                    "helpers",
                    "time_zone",
                    "booth_location",
                    "starts_on",
                    "ends_on",
                    "exclusion_emails",
                    "display_token",
                    "wrapped_up_at",
                    "deleted_entrant_count",
                )
            },
        ),
        (
            "After a conference",
            {
                "classes": ["collapse"],
                "description": "Leave blank to use the site-wide settings.",
                "fields": AFTER_FIELDS,
            },
        ),
    )
    filter_horizontal = ("helpers",)
    inlines = [QuestionInline]

    def formfield_for_manytomany(
        self, db_field: Any, request: HttpRequest, **kwargs: Any
    ) -> Any:
        """Offer only people who can be helpers: active, and not owners.

        Superusers are owners already; an inactive account can't sign in.
        """
        if db_field.name == "helpers":
            kwargs["queryset"] = (
                get_user_model()
                .objects.filter(is_active=True, is_superuser=False)
                .order_by("username")
            )
        return super().formfield_for_manytomany(db_field, request, **kwargs)

    @admin.display(description="Pages")
    def pages(self, obj: Conference | None) -> Any:
        """Link to the booth pages from here, where the owner sets things up.

        The flyer with the entry QR code lives under /booth/, which nothing in
        the admin linked to, so owners couldn't find it.
        """
        if obj is None or obj.pk is None:
            return "Save the conference to get its booth, flyer and entry links."
        links = (
            ("booth-conference", "Booth page"),
            ("booth-flyer", "Printable flyer (QR code)"),
            ("entry", "Entry page"),
        )
        return format_html_join(
            " · ",
            '<a href="{}">{}</a>',
            ((reverse(name, args=[obj.slug]), label) for name, label in links),
        )

    actions = ["rotate_display_token"]

    @admin.action(description="Rotate display token")
    def rotate_display_token(
        self, request: HttpRequest, queryset: QuerySet[Conference]
    ) -> None:
        """Give each conference a fresh token, so a leaked display URL 404s.

        The monitor then needs the new "Booth display link" from the booth.
        """
        count = 0
        for conference in queryset:
            conference.display_token = new_display_token()
            conference.save(update_fields=["display_token"])
            count += 1
        self.message_user(
            request,
            f"Rotated the display token of {count} conference(s). "
            "Open the new booth display link on the monitor.",
            messages.SUCCESS,
        )

    @admin.display(description="Current logo")
    def after_logo_preview(self, obj: Conference | None) -> Any:
        return _logo_preview(obj)


def _frozen(drawing: Drawing | None) -> bool:
    """Whether the drawing has started (or somehow has picks) and so is history.

    Its prize lines are then read-only, with no adds or deletes, and the drawing
    itself can't be deleted: changing a line after picks could crash the booth
    (a physical line turned digital) or make the CSV disagree with the emails
    already sent (a new shared code or URL template).
    """
    return drawing is not None and (
        drawing.status != Drawing.Status.SCHEDULED
        or Drawing.objects.filter(pk=drawing.pk, prizeline__pick__isnull=False).exists()
    )


class PrizeLineForm(forms.ModelForm[PrizeLine]):
    """Prize line plus a paste box that appends coupon codes to the pool."""

    codes_text = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        label="Add codes",
        help_text="One per line. Codes already in the pool are skipped.",
    )

    class Meta:
        model = PrizeLine
        fields = (
            "book",
            "kind",
            "quantity",
            "position",
            "url_template",
            "code_mode",
            "shared_code",
        )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.parsed_codes: list[str] = []

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        text = cleaned.get("codes_text", "")
        if text.strip():
            try:
                self.parsed_codes = parse_codes(text)
            except forms.ValidationError as exc:
                self.add_error("codes_text", exc)
            else:
                if cleaned.get("code_mode") != PrizeLine.CodeMode.POOL:
                    self.add_error(
                        "codes_text", "Codes can only be pasted for a pool line."
                    )
        return cleaned

    def _append_codes(self, line: PrizeLine) -> None:
        existing = set(
            CouponCode.objects.filter(prize_line=line).values_list("code", flat=True)
        )
        CouponCode.objects.bulk_create(
            CouponCode(prize_line=line, code=c)
            for c in self.parsed_codes
            if c not in existing
        )

    def save(self, commit: bool = True) -> PrizeLine:
        line = super().save(commit=commit)
        if not self.parsed_codes:
            return line
        if commit:
            self._append_codes(line)
        else:
            original = self.save_m2m

            def save_m2m() -> None:
                original()
                self._append_codes(line)

            self.save_m2m = save_m2m  # type: ignore[method-assign]
        return line


class PrizeLineInline(admin.StackedInline[PrizeLine, Drawing]):
    model = PrizeLine
    form = PrizeLineForm
    extra = 1
    readonly_fields = ("code_summary",)

    @admin.display(description="Codes")
    def code_summary(self, obj: PrizeLine | None) -> str:
        if obj is None or obj.pk is None:
            return "-"
        counts = CouponCode.objects.filter(prize_line=obj).aggregate(
            total=Count("pk"), unused=Count("pk", filter=Q(used=False))
        )
        return f"{counts['unused']} unused / {counts['total']} total"

    # Inlines take the parent object; django-stubs ignores this override too.
    def has_add_permission(  # type: ignore[override]
        self, request: HttpRequest, obj: Drawing | None
    ) -> bool:
        return not _frozen(obj) and super().has_add_permission(request, obj)

    def has_change_permission(
        self, request: HttpRequest, obj: Drawing | None = None
    ) -> bool:
        return not _frozen(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(
        self, request: HttpRequest, obj: Drawing | None = None
    ) -> bool:
        # Deleting a line cascades to its picks and assigned codes.
        return not _frozen(obj) and super().has_delete_permission(request, obj)


class DrawingAdminForm(forms.ModelForm[Drawing]):
    """Takes `scheduled_at` as local wall-clock time in the conference's zone.

    `status` is not a field: only the booth's drawing service moves a drawing
    through scheduled, open and finished, setting the cutoff and picks as it goes.
    """

    scheduled_at = forms.SplitDateTimeField(
        widget=AdminSplitDateTime,
        help_text="Local time at the conference.",
    )

    class Meta:
        model = Drawing
        fields = ("conference", "scheduled_at")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        instance = self.instance
        if instance.pk and instance.scheduled_at:
            local = to_local(instance.scheduled_at, instance.conference.time_zone)
            self.initial["scheduled_at"] = local.replace(tzinfo=None)

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        # On change the conference field is read-only, so it comes from the row.
        conference = self.instance.conference if self.instance.pk else None
        conference = conference or cleaned.get("conference")
        value = cleaned.get("scheduled_at")
        if conference is None or value is None:
            return cleaned
        if self.instance.pk and "scheduled_at" not in self.changed_data:
            # A plain re-save must not re-resolve an ambiguous wall time.
            cleaned["scheduled_at"] = self.instance.scheduled_at
            return cleaned
        # The field attaches the server zone; what the owner typed is the wall time.
        wall = value.replace(tzinfo=None)
        utc = to_utc(wall, conference.time_zone)
        if to_local(utc, conference.time_zone).replace(tzinfo=None) != wall:
            self.add_error(
                "scheduled_at",
                "That time does not exist in the conference's time zone "
                "(clocks skip it). Pick another time.",
            )
            return cleaned
        cleaned["scheduled_at"] = utc
        return cleaned


@admin.register(Drawing)
class DrawingAdmin(admin.ModelAdmin[Drawing]):
    form = DrawingAdminForm
    list_display = ("__str__", "conference", "local_time", "status")
    list_filter = ("conference", "status")
    inlines = [PrizeLineInline]

    def get_readonly_fields(
        self, request: HttpRequest, obj: Drawing | None = None
    ) -> tuple[str, ...]:
        # Moving a drawing to another conference would silently change its
        # time zone, and with it the entry cutoff.
        return ("conference", "status") if obj else ("status",)

    @admin.display(description="Scheduled (local)", ordering="scheduled_at")
    def local_time(self, obj: Drawing) -> str:
        local: datetime.datetime = to_local(obj.scheduled_at, obj.conference.time_zone)
        return local.strftime("%Y-%m-%d %H:%M")

    def get_queryset(self, request: HttpRequest) -> QuerySet[Drawing]:
        return super().get_queryset(request).select_related("conference")

    def has_delete_permission(
        self, request: HttpRequest, obj: Drawing | None = None
    ) -> bool:
        return not _frozen(obj) and super().has_delete_permission(request, obj)


class _ReadOnlyAdmin[M: Any](admin.ModelAdmin[M]):
    """No add, change or delete: the screen only lists and searches."""

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(Entrant)
class EntrantAdmin(_ReadOnlyAdmin[Entrant]):
    list_display = ("name", "email", "conference", "created_at")
    list_filter = ("conference",)
    search_fields = ("name", "email")


@admin.register(OutboxJob)
class OutboxJobAdmin(_ReadOnlyAdmin[OutboxJob]):
    """Drip outbox. The payload can hold emails, so it is never rendered."""

    list_display = (
        "id",
        "conference",
        "kind",
        "status",
        "attempts",
        "next_attempt_at",
        "last_error_class",
    )
    list_filter = ("status", "kind", "conference")
    exclude = ("payload",)
    actions = ["retry_failed"]

    @admin.action(description="Retry failed jobs", permissions=["retry"])
    def retry_failed(self, request: HttpRequest, queryset: QuerySet[OutboxJob]) -> None:
        count = requeue_failed(queryset, now=timezone.now())
        self.message_user(request, f"Requeued {count} failed job(s).", messages.SUCCESS)

    def has_retry_permission(self, request: HttpRequest) -> bool:
        return bool(request.user.is_superuser)
