"""Root pytest configuration and isolated offline sandbox fixtures for Project Poopdeck.

Design invariants (PROJECT.md / sub_orch_e2e/SCOPE.md):
1. ZERO top-level or collection-time `poopdeck` imports (Python stdlib + `pytest` only).
2. Registers `tier1`..`tier5` markers cleanly under `--strict-markers` whether or not
   `pyproject.toml` exists yet.
3. Strips inherited `GIT_*` environment variables, enforces repo-local Git author
   identity (`Test User <test@example.com>`), and clears Windows read-only `.git`
   pack attributes (`stat.S_IWRITE`) during sandbox teardown.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from typing import Any

import pytest

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

_TIER_MARKERS: tuple[tuple[str, str], ...] = (
    ("tier1", "Tier 1 core feature functional happy-path tests (F01-F12)"),
    ("tier2", "Tier 2 negative, boundary, encoding, redaction, and DLP edge-case tests (F01-F12)"),
    ("tier3", "Tier 3 pairwise cross-feature integration tests"),
    ("tier4", "Tier 4 end-to-end real-world solo Data Scientist application scenarios"),
    ("tier5", "Tier 5 white-box adversarial hardening and regression tests"),
)


def pytest_configure(config: pytest.Config) -> None:
    """Register Tier 1-5 markers and ensure project root is on `sys.path`."""
    root_str = str(PROJECT_ROOT)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    for name, description in _TIER_MARKERS:
        config.addinivalue_line("markers", f"{name}: {description}")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Allow standalone `--collect-only tests/conftest.py` plugin smoke checks to exit 0.

    Pytest loads `conftest.py` as a plugin rather than collecting `test_*` items from
    it, returning `ExitCode.NO_TESTS_COLLECTED` (5) when `tests/conftest.py` is the
    sole collection target before test modules are added. All other invocations
    (`tests/`, `tests/e2e`, `tests/test_*.py`, `-m`, `-k`, or execution runs) preserve
    `ExitCode.NO_TESTS_COLLECTED` (5) so empty test selections always fail loudly.
    """
    if (
        exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED
        and bool(getattr(session.config.option, "collectonly", False))
        and not getattr(session.config.option, "markexpr", "")
        and not getattr(session.config.option, "keyword", "")
    ):
        raw_args = list(getattr(session.config, "args", []) or [])
        if raw_args and all(
            Path(str(arg).split("::", 1)[0]).name == "conftest.py" for arg in raw_args
        ):
            session.exitstatus = pytest.ExitCode.OK


def _clear_readonly(target: Path | str) -> None:
    """Grant owner rwx permissions (`0o700`) without following symlinks or junctions."""
    try:
        if os.chmod in os.supports_follow_symlinks:
            os.chmod(target, stat.S_IRWXU, follow_symlinks=False)
        elif not os.path.islink(target):
            os.chmod(target, stat.S_IRWXU)
    except OSError:
        pass


def _force_remove_readonly(
    func: Callable[..., Any],
    path: str,
    exc_info: BaseException | tuple[Any, ...],
) -> None:
    """Clear read-only attributes (e.g. `.git/objects/pack/*` or `0o555` dirs) and retry.

    Compatible with both Python 3.12+ `shutil.rmtree(..., onexc=...)` (where `exc_info`
    is a `BaseException` instance) and Python <3.12 `onerror` 3-tuples.
    """
    exc: BaseException | None = (
        exc_info[1] if isinstance(exc_info, tuple) else exc_info
    )
    if isinstance(exc, FileNotFoundError):
        return
    _clear_readonly(path)
    func(path)


def safe_rmtree(target: Path | str) -> None:
    """Recursively delete `target` (or unlink top-level symlinks/junctions) cleanly."""
    path_obj = Path(target)
    if not os.path.lexists(path_obj):
        return
    is_junction = hasattr(path_obj, "is_junction") and path_obj.is_junction()
    if path_obj.is_symlink() or is_junction:
        _clear_readonly(path_obj)
        try:
            if is_junction:
                os.rmdir(path_obj)
            else:
                path_obj.unlink()
        except FileNotFoundError:
            return
        except OSError:
            os.rmdir(path_obj)
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path_obj, onexc=_force_remove_readonly)
    else:
        shutil.rmtree(path_obj, onerror=_force_remove_readonly)


def _build_clean_git_env(root: Path = PROJECT_ROOT) -> dict[str, str]:
    """Build an isolated subprocess environment with all `GIT_*` variables stripped."""
    env = {
        key: val
        for key, val in os.environ.items()
        if not key.upper().startswith("GIT_")
        and key.upper() not in {"POOPDECK_VAULT_DIR", "POOPDECK_CONFIG_PATH"}
    }
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["POOPDECK_DISABLE_BROWSER"] = "1"
    root_str = str(root)
    existing_py_path = env.get("PYTHONPATH", "")
    py_parts = [part for part in existing_py_path.split(os.pathsep) if part]
    if root_str not in py_parts:
        env["PYTHONPATH"] = os.pathsep.join([root_str, *py_parts])
    return env


@pytest.fixture(scope="session")
def project_root() -> Path:
    """Return the absolute path to the Project Poopdeck repository root."""
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def fixtures_dir(project_root: Path) -> Path:
    """Return the absolute path to `tests/fixtures`."""
    return project_root / "tests" / "fixtures"


@pytest.fixture
def clean_git_env() -> dict[str, str]:
    """Return a per-test environment copy with all `GIT_*` variables stripped."""
    return _build_clean_git_env(PROJECT_ROOT)


@pytest.fixture
def git_sandbox(tmp_path: Path, clean_git_env: dict[str, str]) -> Iterator[Path]:
    """Yield a clean, unborn-`HEAD` `tmp_path / "repo"` Git repo with local config.

    The working tree is guaranteed clean (`git status --porcelain == ""`) with zero
    initial commits. Tests executing `.git/hooks/pre-commit` via `git commit` should
    copy `project_root / "scripts" / "install_dlp_hook.py"` to
    `git_sandbox / "scripts" / "install_dlp_hook.py"` when testing worktree-relative
    hook execution.
    """
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.name", "Test User"],
        ["git", "config", "user.email", "test@example.com"],
    ):
        subprocess.run(
            cmd,
            cwd=repo_dir,
            env=clean_git_env,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    try:
        yield repo_dir
    finally:
        safe_rmtree(repo_dir)


@pytest.fixture
def vault_sandbox(tmp_path: Path) -> Iterator[Path]:
    """Yield an isolated `tmp_path / "vault"` directory with safe `vault/.git` teardown."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)
    try:
        yield vault_dir
    finally:
        safe_rmtree(vault_dir)


@pytest.fixture
def run_cli(
    project_root: Path,
    clean_git_env: dict[str, str],
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Return a helper invoking `[sys.executable, "-m", "poopdeck", *args]` cleanly."""

    def _invoke(
        *args: str | os.PathLike[str],
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        timeout: float = 30.0,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        merged_env = dict(clean_git_env)
        if env:
            merged_env.update({str(k): str(v) for k, v in env.items()})
        work_dir = Path(cwd) if cwd is not None else project_root
        return subprocess.run(
            [sys.executable, "-m", "poopdeck", *(str(a) for a in args)],
            cwd=work_dir,
            env=merged_env,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=check,
        )

    return _invoke
