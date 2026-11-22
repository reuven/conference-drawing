from conference_drawing.cli import main


def test_main_passes_arguments_to_django_management() -> None:
    assert main(["check"]) == 0
