from conference_drawing.cli import main


def test_main_passes_arguments_to_django_management(db: None) -> None:
    # `check` now inspects JSONField support, which touches the database.
    assert main(["check"]) == 0
