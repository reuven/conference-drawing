import importlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import settings

from tests import conftest


@pytest.fixture
def restore_profile() -> Iterator[None]:
    name = settings.get_current_profile_name()
    yield
    settings.load_profile(name)


@pytest.mark.usefixtures("restore_profile")
def test_ci_profile_runs_200_examples(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HYPOTHESIS_PROFILE", raising=False)
    monkeypatch.setenv("CI", "1")
    importlib.reload(conftest)
    assert settings().max_examples == 200


@pytest.mark.usefixtures("restore_profile")
def test_explicit_profile_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "1")
    monkeypatch.setenv("HYPOTHESIS_PROFILE", "dev")
    importlib.reload(conftest)
    assert settings.get_current_profile_name() == "dev"


@pytest.mark.usefixtures("restore_profile")
def test_mutants_directory_selects_mutmut_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mutants = tmp_path / "mutants"
    mutants.mkdir()
    monkeypatch.chdir(mutants)
    monkeypatch.delenv("HYPOTHESIS_PROFILE", raising=False)
    monkeypatch.delenv("CI", raising=False)
    importlib.reload(conftest)
    assert settings.get_current_profile_name() == "mutmut"
