import importlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings

from tests import conftest


@pytest.fixture
def restore_profile() -> Iterator[None]:
    name = settings.get_current_profile_name()
    yield
    settings.load_profile(name)


@pytest.mark.usefixtures("restore_profile")
def test_ci_profile_runs_200_examples(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Not under mutants/, where the mutmut profile would win (as in a mutmut run).
    monkeypatch.chdir(tmp_path)
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


def test_mutmut_profile_suppresses_differing_executors() -> None:
    # mutmut runs pytest twice in one process; see the profile's comment.
    profile = settings.get_profile("mutmut")
    assert HealthCheck.differing_executors in profile.suppress_health_check
    ci = settings.get_profile("ci")
    assert HealthCheck.differing_executors not in ci.suppress_health_check


@pytest.mark.usefixtures("restore_profile")
def test_ci_profile_keeps_hypothesis_builtin_ci_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Registering "ci" replaces Hypothesis's built-in profile, so ours must
    # build on it: derandomized, no example database, reproduction blobs.
    # Reloading conftest while another profile is loaded (as mutmut's second
    # in-process pytest run does) must not leak that profile into "ci".
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HYPOTHESIS_PROFILE", raising=False)
    monkeypatch.delenv("CI", raising=False)
    settings.load_profile("mutmut")
    importlib.reload(conftest)
    ci = settings.get_profile("ci")
    assert ci.derandomize is True
    assert ci.database is None
    assert ci.print_blob is True
    assert ci.max_examples == 200
    assert ci.deadline is None
    assert ci.suppress_health_check == ()
