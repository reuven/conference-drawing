"""Django admin: the owner's setup screens and read-only views of entrants/outbox."""

import datetime
from typing import Any

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.widgets import AdminSplitDateTime
from django.db.models import Count, Q, QuerySet
from django.http import HttpRequest
from django.utils import timezone

from .codes import parse_codes
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
    create_default_questions,
)
from .timezones import to_local, to_utc


@admin.register(InstallSettings)
class InstallSettingsAdmin(admin.ModelAdmin[InstallSettings]):
    """Per-install copy; one row only: no second add, no delete."""

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
class ConferenceAdmin(admin.ModelAdmin[Conference]):
    """Conferences with their optional questions and exclusion list.

    `display_token` authorizes the public display URL, so it is read-only here
    and never shown in the list.
    """

    list_display = ("name", "slug", "starts_on", "ends_on", "time_zone")
    search_fields = ("name", "slug")
    readonly_fields = ("display_token", "wrapped_up_at", "deleted_entrant_count")
    inlines = [QuestionInline]

    def save_model(
        self, request: HttpRequest, obj: Conference, form: Any, change: bool
    ) -> None:
        super().save_model(request, obj, form, change)
        if not change:
            create_default_questions(obj)


def _has_picks(drawing: Drawing | None) -> bool:
    return (
        drawing is not None
        and Drawing.objects.filter(
            pk=drawing.pk, prizeline__pick__isnull=False
        ).exists()
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
            total=Count("pk"), unused=Count("pk", filter=Q(pick__isnull=True))
        )
        return f"{counts['unused']} unused / {counts['total']} total"

    def has_delete_permission(
        self, request: HttpRequest, obj: Drawing | None = None
    ) -> bool:
        # Deleting a line cascades to its picks and assigned codes.
        return not _has_picks(obj) and super().has_delete_permission(request, obj)


class DrawingAdminForm(forms.ModelForm[Drawing]):
    """Takes `scheduled_at` as local wall-clock time in the conference's zone."""

    scheduled_at = forms.SplitDateTimeField(
        widget=AdminSplitDateTime,
        help_text="Local time at the conference.",
    )

    class Meta:
        model = Drawing
        fields = ("conference", "scheduled_at", "status")

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
        return ("conference",) if obj else ()

    @admin.display(description="Scheduled (local)", ordering="scheduled_at")
    def local_time(self, obj: Drawing) -> str:
        local: datetime.datetime = to_local(obj.scheduled_at, obj.conference.time_zone)
        return local.strftime("%Y-%m-%d %H:%M")

    def get_queryset(self, request: HttpRequest) -> QuerySet[Drawing]:
        return super().get_queryset(request).select_related("conference")

    def has_delete_permission(
        self, request: HttpRequest, obj: Drawing | None = None
    ) -> bool:
        return not _has_picks(obj) and super().has_delete_permission(request, obj)


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
        count = queryset.filter(status=OutboxJob.Status.FAILED).update(
            status=OutboxJob.Status.PENDING,
            attempts=0,
            next_attempt_at=timezone.now(),
        )
        self.message_user(request, f"Requeued {count} failed job(s).", messages.SUCCESS)

    def has_retry_permission(self, request: HttpRequest) -> bool:
        return bool(request.user.is_superuser)
