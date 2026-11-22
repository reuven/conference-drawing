"""Drip credentials, read from Django settings (which load the project `.env`)."""

from django.conf import settings
from pydantic import BaseModel, SecretStr, field_validator


class DripSettings(BaseModel):
    """Validated Drip credentials; the API key never appears in a repr or log."""

    account_id: str
    api_key: SecretStr

    @field_validator("account_id")
    @classmethod
    def _digits_only(cls, value: str) -> str:
        """Account ids go into request URLs, so refuse anything but digits."""
        if not value.isascii() or not value.isdigit():
            raise ValueError("account_id must be digits only")
        return value


def drip_settings() -> DripSettings | None:
    """Return Drip credentials, or None when Drip isn't configured.

    "Not configured" means either value is missing; callers then fall back to
    showing prize codes at the booth. A present but malformed account id raises
    a validation error rather than being silently treated as unconfigured.
    """
    account_id = getattr(settings, "DRIP_ACCOUNT_ID", "")
    api_key = getattr(settings, "DRIP_API_KEY", "")
    if not account_id or not api_key:
        return None
    return DripSettings(account_id=account_id, api_key=SecretStr(api_key))
