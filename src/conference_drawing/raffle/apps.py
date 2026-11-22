from django.apps import AppConfig


class RaffleConfig(AppConfig):
    name = "conference_drawing.raffle"
    label = "raffle"

    def ready(self) -> None:
        """Import the Drip config module so its system check is registered."""
        from .drip import config  # noqa: F401
