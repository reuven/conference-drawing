"""The public entry form."""

import datetime
from typing import Any

from django import forms
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from .models import Conference, Entrant, InstallSettings, Question
from .text import (
    has_forbidden_chars,
    normalize_email,
    validate_deliverable_email,
    validate_single_line_label,
)

FREE_TEXT_MAX_LEN = 1000


def clean_free_text(value: str) -> str:
    """Return a free-text answer tidied, or raise ValidationError if it is unsafe.

    Answers are shown to the owner and exported to CSV, so they get the same
    hygiene as a name -- no control or invisible format characters -- except
    that line breaks are legitimate in a textarea. Browsers send CRLF; we store
    plain LF. The length cap lives on the form field.
    """
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if has_forbidden_chars(text, allow_newline=True):
        raise ValidationError(
            "Remove tabs and invisible or control characters.",
            code="invalid_characters",
        )
    return text


class EntryForm(forms.Form):
    """What an attendee fills in at the booth: contact details, consent, answers.

    One class serves every conference: the per-conference questions are added
    as `q_<key>` fields in `__init__`. Saving a repeat email is a silent no-op
    so the page can never be used to learn who has entered.
    """

    name = forms.CharField(max_length=200)
    email = forms.EmailField(
        max_length=254,
        error_messages={"invalid": "Please enter a valid email address."},
    )
    followup_ack = forms.BooleanField(required=True)
    bd_optin = forms.BooleanField(required=False, initial=False)
    # Honeypot: hidden from people by CSS, irresistible to form-filling bots.
    website = forms.CharField(required=False)

    def __init__(self, conference: Conference, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.conference = conference
        self.install = InstallSettings.get()
        self.fields["followup_ack"].label = self.install.followup_ack_text
        self.fields["bd_optin"].label = self.install.bd_checkbox_text
        self.fields["website"].label = "Leave this field empty"
        self.fields["website"].widget.attrs.update(
            {"autocomplete": "off", "tabindex": "-1"}
        )
        self.fields["email"].widget.attrs.update(
            {"autocomplete": "email", "inputmode": "email"}
        )
        self.fields["name"].widget.attrs.update({"autocomplete": "name"})
        self.questions = list(Question.objects.filter(conference=conference))
        for q in self.questions:
            self.fields[f"q_{q.key}"] = self._question_field(q)

    @staticmethod
    def _question_field(q: Question) -> forms.Field:
        if q.kind == Question.Kind.CHOICE:
            return forms.ChoiceField(
                label=q.prompt,
                required=False,
                choices=[(c, c) for c in q.choices],
                widget=forms.RadioSelect,
            )
        return forms.CharField(
            label=q.prompt,
            required=False,
            max_length=FREE_TEXT_MAX_LEN,
            widget=forms.Textarea(attrs={"rows": 3}),
        )

    @property
    def question_fields(self) -> list[forms.BoundField]:
        """The per-conference question fields, in display order, for templates."""
        return [self[f"q_{q.key}"] for q in self.questions]

    def clean_name(self) -> str:
        return validate_single_line_label(self.cleaned_data["name"])

    def clean_email(self) -> str:
        return normalize_email(validate_deliverable_email(self.cleaned_data["email"]))

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        for q in self.questions:
            field = f"q_{q.key}"
            if q.kind == Question.Kind.TEXT and field in cleaned:
                try:
                    cleaned[field] = clean_free_text(cleaned[field])
                except ValidationError as err:
                    self.add_error(field, err)
        return cleaned

    def honeypot_filled(self) -> bool:
        """True if something was typed into the hidden field (a bot, not a person)."""
        return bool(self.data.get("website", "").strip())

    def _already_entered(self, email: str) -> bool:
        return Entrant.objects.filter(conference=self.conference, email=email).exists()

    def save(self, now: datetime.datetime) -> Entrant | None:
        """Create the entrant, or return None (changing nothing) for a repeat email.

        Call only after `is_valid()`. Two simultaneous submits of one address
        can both pass the existence check; the unique constraint then rejects
        the loser, which is treated as the repeat it is.
        """
        email: str = self.cleaned_data["email"]
        if self._already_entered(email):
            return None
        answers = {
            q.key: self.cleaned_data[f"q_{q.key}"]
            for q in self.questions
            if self.cleaned_data.get(f"q_{q.key}")
        }
        opted_in: bool = self.cleaned_data["bd_optin"]
        entrant = Entrant(
            conference=self.conference,
            name=self.cleaned_data["name"],
            email=email,
            answers=answers,
            followup_ack_text=self.install.followup_ack_text,
            followup_ack_at=now,
            bd_optin_text=self.install.bd_checkbox_text if opted_in else "",
            bd_optin_at=now if opted_in else None,
            created_at=now,
        )
        try:
            with transaction.atomic():
                entrant.save()
        except IntegrityError:
            if self._already_entered(email):
                return None
            raise
        return entrant
