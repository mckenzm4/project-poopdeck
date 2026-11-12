"""Requirement acceptance tests for F01 & F02: Packaging, Config, R5 Guardrails & Corporate DLP Shield.

Covers:
- F01 Tier 1 & Tier 2: `pyproject.toml`, `config.example.yaml`, `load_config` precedence chain,
  `PrivacyConfig` `AliasChoices("allow_sample_rows", "include_sample_rows")`, `python -m poopdeck`
  CLI entrypoint behavior, R5 zero-Node/DB/Bot architecture prohibitions, and strict Pydantic/YAML
  boundary rejections.
- F02 Tier 1 & Tier 2: Root `.gitignore` exclusions vs. `!examples/demo_vault/`, LF-only POSIX
  `.git/hooks/pre-commit` installation, `run_dlp_check` and `git commit` enforcement on external
  remotes (`mckenzm4`) vs. `corporate_remote_patterns`, nested `vault/.git` isolation (including
  `160000` gitlink staging), secret/PAT/JDBC/PEM scanning, and catalog/table dump blocking.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib

import pytest


def _git(
    repo: Path,
    env: dict[str, str],
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a Git command inside `repo` using an isolated `GIT_*`-stripped environment."""
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def _setup_dlp_hook_in_sandbox(
    project_root: Path,
    git_sandbox: Path,
) -> tuple[Path, Callable[[Path], tuple[bool, list[str]]]]:
    """Copy `scripts/install_dlp_hook.py` into `git_sandbox` and install `.git/hooks/pre-commit`."""
    import scripts.install_dlp_hook as dlp_mod
    from scripts.install_dlp_hook import install_hook, run_dlp_check

    workspace_script = project_root / "scripts" / "install_dlp_hook.py"
    src_script = (
        workspace_script
        if workspace_script.is_file()
        else Path(dlp_mod.__file__).resolve()
    )
    assert src_script.is_file(), f"Missing DLP hook script at {src_script}"

    dst_script = git_sandbox / "scripts" / "install_dlp_hook.py"
    dst_script.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_script, dst_script)

    hook_path = install_hook(git_sandbox)
    return hook_path, run_dlp_check


def _normalize_dep_name(dep_spec: str) -> str:
    """Extract normalized lowercase PEP 508 distribution name from a dependency specifier."""
    raw = re.split(r"[><=!~;\[\s]", dep_spec.strip(), maxsplit=1)[0]
    return raw.strip().lower().replace("_", "-")


# ============================================================================
# F01 Tier 1 — Python Packaging, Config & R5 Architecture Guardrails (6 tests)
# ============================================================================


@pytest.mark.tier1
def test_f01_t1_pyproject_metadata_deps_and_script_entrypoint(project_root: Path) -> None:
    """Verify pyproject.toml build-system, minimal corporate-safe deps, script entrypoint & version."""
    import poopdeck

    pyproject_path = project_root / "pyproject.toml"
    assert pyproject_path.is_file(), "Missing pyproject.toml at repository root"
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    assert "build-system" in data
    assert bool(data["build-system"].get("build-backend"))
    assert len(data["build-system"].get("requires", [])) >= 1

    project = data["project"]
    assert project["name"] == "poopdeck"
    assert "3.11" in project["requires-python"]

    core_dep_names = {_normalize_dep_name(spec) for spec in project.get("dependencies", [])}
    expected_core = {"pydantic", "pyyaml", "fastapi", "uvicorn", "click", "httpx"}
    assert expected_core.issubset(core_dep_names), (
        f"Missing required core dependencies: {expected_core - core_dep_names}"
    )
    assert "databricks-sdk" not in core_dep_names

    opt_deps = project.get("optional-dependencies", {})
    dbx_opt_names = {_normalize_dep_name(spec) for spec in opt_deps.get("databricks", [])}
    assert "databricks-sdk" in dbx_opt_names

    assert project["scripts"]["poopdeck"] == "poopdeck.cli:main"
    assert isinstance(poopdeck.__version__, str)
    assert poopdeck.__version__ == project["version"]


@pytest.mark.tier1
def test_f01_t1_config_example_yaml_loads_expected_defaults(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify config.example.yaml loads cleanly with exact documented corporate-safe defaults."""
    from poopdeck.config import load_config

    monkeypatch.delenv("POOPDECK_CONFIG_PATH", raising=False)
    monkeypatch.delenv("POOPDECK_VAULT_DIR", raising=False)

    example_path = project_root / "config.example.yaml"
    assert example_path.is_file(), "Missing config.example.yaml"
    cfg = load_config(example_path)

    assert cfg.project_name == "project-poopdeck"
    assert cfg.vault_path == "vault"
    assert cfg.domain == "ops_supply_chain"
    assert cfg.quarterly_capacity_weeks == 12.0

    assert cfg.privacy.privacy_mode == "safe_aggregates"
    assert cfg.privacy.allow_sample_rows is False
    assert cfg.privacy.max_sample_rows == 5
    assert cfg.privacy.redact_secrets is True
    assert len(cfg.privacy.pii_column_patterns) >= 7

    assert cfg.scoring_weights.roi_weight == 0.40
    assert cfg.scoring_weights.data_readiness_weight == 0.25
    assert cfg.scoring_weights.bi_adoption_ease_weight == 0.20
    assert cfg.scoring_weights.speed_to_value_weight == 0.15
    assert cfg.scoring_weights.existing_pbi_bonus_points == 10.0
    assert cfg.scoring_weights.roi_cap_usd == 1_000_000.0
    assert cfg.scoring_weights.confidence_multipliers == {
        "hypothesis": 0.60,
        "directional": 0.85,
        "finance_verified": 1.00,
    }

    assert cfg.dlp.corporate_remote_patterns == []
    assert "mckenzm4" in cfg.dlp.external_remote_patterns


@pytest.mark.tier1
def test_f01_t1_load_config_full_precedence_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify load_config precedence: explicit -> POOPDECK_CONFIG_PATH -> ./config.yaml -> ./config.example.yaml -> defaults."""
    from poopdeck.config import load_config

    monkeypatch.delenv("POOPDECK_CONFIG_PATH", raising=False)
    monkeypatch.delenv("POOPDECK_VAULT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    # 1. Built-in / package fallback defaults when cwd has no config files
    cfg0 = load_config()
    assert cfg0.domain == "ops_supply_chain"
    assert cfg0.quarterly_capacity_weeks == 12.0

    # 2. ./config.example.yaml in cwd overrides built-in defaults
    (tmp_path / "config.example.yaml").write_text(
        "domain: retail_cpg\nquarterly_capacity_weeks: 9.5\n",
        encoding="utf-8",
        newline="\n",
    )
    cfg1 = load_config()
    assert cfg1.domain == "retail_cpg"
    assert cfg1.quarterly_capacity_weeks == 9.5

    # 3. ./config.yaml in cwd overrides ./config.example.yaml
    (tmp_path / "config.yaml").write_text(
        "domain: finance_risk\nquarterly_capacity_weeks: 10.5\n",
        encoding="utf-8",
        newline="\n",
    )
    cfg2 = load_config()
    assert cfg2.domain == "finance_risk"
    assert cfg2.quarterly_capacity_weeks == 10.5

    # 4. POOPDECK_CONFIG_PATH overrides ./config.yaml
    env_cfg = tmp_path / "env_cfg.yaml"
    env_cfg.write_text(
        "domain: b2b_saas\nquarterly_capacity_weeks: 14.0\n",
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.setenv("POOPDECK_CONFIG_PATH", str(env_cfg))
    cfg3 = load_config()
    assert cfg3.domain == "b2b_saas"
    assert cfg3.quarterly_capacity_weeks == 14.0

    # 5. Explicit config_path overrides POOPDECK_CONFIG_PATH
    exp_cfg = tmp_path / "explicit.yaml"
    exp_cfg.write_text(
        "domain: manufacturing\nquarterly_capacity_weeks: 16.0\n",
        encoding="utf-8",
        newline="\n",
    )
    cfg4 = load_config(exp_cfg)
    assert cfg4.domain == "manufacturing"
    assert cfg4.quarterly_capacity_weeks == 16.0

    # 6. POOPDECK_VAULT_DIR and CLI vault_override resolution
    env_vault = tmp_path / "env_vault"
    cli_vault = tmp_path / "cli_vault"
    monkeypatch.setenv("POOPDECK_VAULT_DIR", str(env_vault))
    cfg5 = load_config(exp_cfg)
    assert cfg5.resolve_vault_dir() == env_vault
    assert cfg5.resolve_vault_dir(cli_vault) == cli_vault


@pytest.mark.tier1
def test_f01_t1_privacy_config_sample_row_alias_choices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify PrivacyConfig accepts both allow_sample_rows and include_sample_rows seamlessly."""
    from poopdeck.config import PrivacyConfig, load_config

    monkeypatch.delenv("POOPDECK_CONFIG_PATH", raising=False)

    p_allow = PrivacyConfig(allow_sample_rows=True)
    assert p_allow.allow_sample_rows is True
    assert p_allow.include_sample_rows is True

    p_inc = PrivacyConfig(include_sample_rows=True)
    assert p_inc.allow_sample_rows is True
    assert p_inc.include_sample_rows is True

    p_dict = PrivacyConfig.model_validate(
        {
            "privacy_mode": "scrubbed_samples",
            "include_sample_rows": True,
            "max_sample_rows": 3,
        }
    )
    assert p_dict.allow_sample_rows is True
    assert p_dict.include_sample_rows is True
    assert p_dict.max_sample_rows == 3

    yaml_inc = tmp_path / "inc.yaml"
    yaml_inc.write_text(
        "privacy:\n  privacy_mode: scrubbed_samples\n  include_sample_rows: true\n  max_sample_rows: 4\n",
        encoding="utf-8",
        newline="\n",
    )
    c_inc = load_config(yaml_inc)
    assert c_inc.privacy.allow_sample_rows is True
    assert c_inc.privacy.include_sample_rows is True
    assert c_inc.privacy.max_sample_rows == 4

    yaml_allow = tmp_path / "allow.yaml"
    yaml_allow.write_text(
        "privacy:\n  privacy_mode: scrubbed_samples\n  allow_sample_rows: true\n",
        encoding="utf-8",
        newline="\n",
    )
    c_allow = load_config(yaml_allow)
    assert c_allow.privacy.allow_sample_rows is True
    assert c_allow.privacy.include_sample_rows is True


@pytest.mark.tier1
def test_f01_t1_cli_module_help_lists_all_seven_subcommands(
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Verify `python -m poopdeck --help` exits 0 and lists all 7 mandated CLI subcommands."""
    res = run_cli("--help")
    assert res.returncode == 0, f"CLI --help failed (rc={res.returncode}):\n{res.stderr}"
    for subcommand in ("init", "sync", "validate", "demo", "reset", "ui", "brief"):
        assert subcommand in res.stdout, (
            f"Missing subcommand '{subcommand}' in `python -m poopdeck --help`:\n{res.stdout}"
        )


@pytest.mark.tier1
def test_f01_t1_r5_architecture_prohibitions_positive_audit(project_root: Path) -> None:
    """Verify R5 guardrails: zero Node.js/npm files, zero DB files/deps, and zero bot/DB imports."""
    pruned_dirs = {
        ".git",
        ".agents",
        ".pytest_cache",
        "__pycache__",
        ".venv",
        "venv",
        ".ruff_cache",
        ".mypy_cache",
        "build",
        "dist",
        "vault",
    }
    forbidden_filenames = {
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
    }
    forbidden_suffixes = (".sqlite", ".sqlite3", ".db")

    for current_root, dirs, files in os.walk(project_root):
        assert "node_modules" not in dirs, f"Forbidden node_modules directory in {current_root}"
        dirs[:] = [
            d
            for d in dirs
            if d not in pruned_dirs and not d.endswith(".egg-info")
        ]
        for fname in files:
            lower_name = fname.lower()
            assert lower_name not in forbidden_filenames, (
                f"Forbidden Node/npm file found: {Path(current_root) / fname}"
            )
            assert not lower_name.endswith(forbidden_suffixes), (
                f"Forbidden binary database file found: {Path(current_root) / fname}"
            )

    pyproject_data = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    all_dep_specs = list(pyproject_data.get("project", {}).get("dependencies", []))
    for extra_specs in pyproject_data.get("project", {}).get("optional-dependencies", {}).values():
        all_dep_specs.extend(extra_specs)
    dep_blob = " ".join(spec.lower() for spec in all_dep_specs)
    for forbidden_pkg in (
        "postgres",
        "psycopg",
        "asyncpg",
        "sqlalchemy",
        "sqlmodel",
        "aiosqlite",
        "redis",
        "botbuilder",
        "msgraph",
    ):
        assert forbidden_pkg not in dep_blob, (
            f"Forbidden R5 package '{forbidden_pkg}' declared in pyproject.toml"
        )

    forbidden_import_roots = {
        "sqlite3",
        "aiosqlite",
        "psycopg",
        "psycopg2",
        "asyncpg",
        "sqlalchemy",
        "sqlmodel",
        "redis",
        "botbuilder",
        "msgraph",
    }
    imported_roots: set[str] = set()
    for py_file in (project_root / "poopdeck").rglob("*.py"):
        if "__pycache__" in py_file.parts:
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_roots.add(alias.name.split(".", 1)[0])
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported_roots.add(node.module.split(".", 1)[0])

    overlap = imported_roots & forbidden_import_roots
    assert not overlap, f"Forbidden R5 runtime imports found in poopdeck/: {sorted(overlap)}"


# ============================================================================
# F01 Tier 2 — Negative, Boundary & Isolation Validation (6 tests)
# ============================================================================


@pytest.mark.tier2
def test_f01_t2_load_config_rejects_invalid_privacy_mode_literal(tmp_path: Path) -> None:
    """Verify unsupported privacy_mode literals fail closed with Pydantic ValidationError."""
    from pydantic import ValidationError

    from poopdeck.config import PrivacyConfig, load_config

    bad_cfg = tmp_path / "bad_privacy.yaml"
    bad_cfg.write_text("privacy:\n  privacy_mode: raw_dump\n", encoding="utf-8", newline="\n")

    with pytest.raises(ValidationError):
        load_config(bad_cfg)

    with pytest.raises(ValidationError):
        PrivacyConfig(privacy_mode="raw_dump")  # type: ignore[arg-type]


@pytest.mark.tier2
def test_f01_t2_load_config_rejects_corrupted_and_non_mapping_yaml(tmp_path: Path) -> None:
    """Verify malformed YAML syntax and non-mapping YAML roots fail loudly without silent fallback."""
    import yaml

    from poopdeck.config import load_config

    corrupt = tmp_path / "corrupt.yaml"
    corrupt.write_text(
        "privacy:\n  privacy_mode: [unclosed_list\n  :\n",
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises((yaml.YAMLError, ValueError)):
        load_config(corrupt)

    list_yaml = tmp_path / "list.yaml"
    list_yaml.write_text("- item1\n- item2\n", encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match="YAML mapping"):
        load_config(list_yaml)

    scalar_yaml = tmp_path / "scalar.yaml"
    scalar_yaml.write_text("just_a_string\n", encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match="YAML mapping"):
        load_config(scalar_yaml)


@pytest.mark.tier2
def test_f01_t2_load_config_missing_explicit_or_env_path_raises_file_not_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify missing explicit config_path, directory path, and missing POOPDECK_CONFIG_PATH raise FileNotFoundError."""
    from poopdeck.config import load_config

    monkeypatch.delenv("POOPDECK_CONFIG_PATH", raising=False)

    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "does_not_exist.yaml")

    with pytest.raises(FileNotFoundError):
        load_config(tmp_path)

    monkeypatch.setenv("POOPDECK_CONFIG_PATH", str(tmp_path / "missing_env_config.yaml"))
    with pytest.raises(FileNotFoundError):
        load_config()


@pytest.mark.tier2
def test_f01_t2_scoring_weights_and_numeric_boundary_validation(tmp_path: Path) -> None:
    """Verify strict numeric, weight-sum, boolean, NaN/Inf, mode, and domain boundaries."""
    from pydantic import ValidationError

    from poopdeck.config import (
        DatabricksConfig,
        PoopdeckConfig,
        PrivacyConfig,
        ScoringWeightsConfig,
        load_config,
    )

    with pytest.raises(ValidationError):
        PoopdeckConfig(quarterly_capacity_weeks=0.0)
    with pytest.raises(ValidationError):
        PoopdeckConfig(quarterly_capacity_weeks=-2.0)
    with pytest.raises(ValidationError):
        PoopdeckConfig(quarterly_capacity_weeks=True)  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        ScoringWeightsConfig(roi_weight=0.80)
    with pytest.raises(ValidationError):
        ScoringWeightsConfig(roi_cap_usd=0.0)
    with pytest.raises(ValidationError):
        ScoringWeightsConfig(confidence_multipliers={"directional": 1.25})

    with pytest.raises(ValidationError):
        PrivacyConfig(max_sample_rows=-1)
    with pytest.raises(ValidationError):
        DatabricksConfig(mode="invalid_mode")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        PoopdeckConfig(domain="   ")

    nan_cfg = tmp_path / "nan.yaml"
    nan_cfg.write_text("quarterly_capacity_weeks: .nan\n", encoding="utf-8", newline="\n")
    with pytest.raises(ValidationError):
        load_config(nan_cfg)


@pytest.mark.tier2
def test_f01_t2_empty_cwd_and_blank_env_var_isolation_returns_defaults(
    tmp_path: Path,
    clean_git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify clean_git_env stripping, blank env vars, and BOM comment-only YAML return safe defaults."""
    from poopdeck.config import PoopdeckConfig, load_config

    assert "POOPDECK_CONFIG_PATH" not in clean_git_env
    assert "POOPDECK_VAULT_DIR" not in clean_git_env
    assert clean_git_env["PYTHONUTF8"] == "1"
    assert clean_git_env["POOPDECK_DISABLE_BROWSER"] == "1"
    assert not any(key.upper().startswith("GIT_") for key in clean_git_env)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("POOPDECK_CONFIG_PATH", "   ")
    monkeypatch.setenv("POOPDECK_VAULT_DIR", "")

    cfg = load_config()
    assert cfg == PoopdeckConfig()
    assert cfg.domain == "ops_supply_chain"
    assert cfg.quarterly_capacity_weeks == 12.0
    assert cfg.vault_path == "vault"
    assert cfg.privacy.privacy_mode == "safe_aggregates"
    assert cfg.privacy.allow_sample_rows is False

    (tmp_path / "config.yaml").write_text(
        "# Intentionally empty corporate config\n",
        encoding="utf-8-sig",
        newline="\n",
    )
    cfg_empty_file = load_config()
    assert cfg_empty_file == PoopdeckConfig()


@pytest.mark.tier2
def test_f01_t2_cli_unknown_subcommand_exits_nonzero_without_traceback(
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Verify `python -m poopdeck nonexistent-cmd` fails cleanly via Click without Python tracebacks."""
    res = run_cli("nonexistent-cmd")
    assert res.returncode != 0

    combined = f"{res.stdout}\n{res.stderr}"
    assert "No module named poopdeck.__main__" not in combined, (
        f"CLI entrypoint poopdeck/__main__.py is missing:\n{combined}"
    )
    assert "Traceback (most recent call last)" not in combined
    assert "nonexistent-cmd" in combined or "no such command" in combined.lower()


# ============================================================================
# F02 Tier 1 — Corporate DLP Boundary, .gitignore, Hook & vault/.git (6 tests)
# ============================================================================


@pytest.mark.tier1
def test_f02_t1_root_gitignore_excludes_sensitive_paths_and_allows_demo_vault(
    project_root: Path,
    git_sandbox: Path,
    clean_git_env: dict[str, str],
) -> None:
    """Verify root .gitignore blocks vault/, .env*, *.pbix, *.vpax, exports/, config.yaml & .agents/ while allowing examples/demo_vault/."""
    shutil.copy2(project_root / ".gitignore", git_sandbox / ".gitignore")

    must_ignore = [
        "vault/datasets/prod_orders.md",
        ".env",
        ".env.local",
        "report.pbix",
        "finance/q1_deck.pbix",
        "model.vpax",
        "exports/out.csv",
        "config.yaml",
        ".agents/teamwork/state.md",
    ]
    for rel_path in must_ignore:
        res = _git(git_sandbox, clean_git_env, "check-ignore", "-q", rel_path, check=False)
        assert res.returncode == 0, f"Expected .gitignore to ignore '{rel_path}', got rc={res.returncode}"

    must_track = [
        "examples/demo_vault/datasets/sample.md",
        "examples/demo_vault/opportunities/OPP-004.md",
        ".env.example",
        "config.example.yaml",
        "pyproject.toml",
    ]
    for rel_path in must_track:
        res = _git(git_sandbox, clean_git_env, "check-ignore", "-q", rel_path, check=False)
        assert res.returncode == 1, f"Expected .gitignore NOT to ignore '{rel_path}', got rc={res.returncode}"


@pytest.mark.tier1
def test_f02_t1_install_hook_writes_lf_posix_pre_commit_script(
    project_root: Path,
    git_sandbox: Path,
) -> None:
    """Verify install_hook writes an LF-only #!/bin/sh pre-commit hook probing Python and invoking --hook."""
    hook_path, _ = _setup_dlp_hook_in_sandbox(project_root, git_sandbox)

    assert hook_path == git_sandbox / ".git" / "hooks" / "pre-commit"
    assert hook_path.is_file()

    raw = hook_path.read_bytes()
    assert b"\r\n" not in raw, "Pre-commit hook must use strict LF (\\n) line endings on Windows"
    assert raw.startswith(b"#!/bin/sh\n")

    text = raw.decode("utf-8")
    assert "scripts/install_dlp_hook.py" in text
    assert "--hook" in text
    assert "import sys" in text
    posix_py = sys.executable.replace("\\", "/")
    assert posix_py in text or "\\Python" not in text


@pytest.mark.tier1
def test_f02_t1_dlp_allows_clean_engine_commits_on_mckenzm4_remote(
    project_root: Path,
    git_sandbox: Path,
    clean_git_env: dict[str, str],
) -> None:
    """Verify clean shareable engine commits succeed on personal/external mckenzm4 remote."""
    _git(
        git_sandbox,
        clean_git_env,
        "remote",
        "add",
        "origin",
        "https://github.com/mckenzm4/project-poopdeck.git",
    )
    _, run_dlp_check = _setup_dlp_hook_in_sandbox(project_root, git_sandbox)

    helper_py = git_sandbox / "poopdeck" / "feature_helper.py"
    helper_py.parent.mkdir(parents=True, exist_ok=True)
    helper_py.write_text(
        '"""Clean engine helper."""\n\ndef add_scores(a: float, b: float) -> float:\n    return a + b\n',
        encoding="utf-8",
        newline="\n",
    )
    (git_sandbox / "README.md").write_text(
        "# Project Poopdeck Engine\n\nClean open-source documentation.\n",
        encoding="utf-8",
        newline="\n",
    )

    _git(git_sandbox, clean_git_env, "add", "poopdeck/feature_helper.py", "README.md")

    ok, reasons = run_dlp_check(git_sandbox)
    assert ok is True, f"Expected clean engine files to pass DLP check, got: {reasons}"
    assert reasons == []

    commit_res = _git(git_sandbox, clean_git_env, "commit", "-m", "Add clean engine module", check=False)
    assert commit_res.returncode == 0, f"Clean engine commit blocked:\n{commit_res.stderr}"
    assert "[POOPDECK-DLP-BLOCKED]" not in commit_res.stderr


@pytest.mark.tier1
def test_f02_t1_dlp_allows_examples_demo_vault_on_mckenzm4_remote(
    project_root: Path,
    git_sandbox: Path,
    clean_git_env: dict[str, str],
) -> None:
    """Verify DLP path check uses anchored vault path matching so examples/demo_vault/ commits succeed."""
    shutil.copy2(project_root / ".gitignore", git_sandbox / ".gitignore")
    _git(
        git_sandbox,
        clean_git_env,
        "remote",
        "add",
        "origin",
        "https://github.com/mckenzm4/project-poopdeck.git",
    )
    _, run_dlp_check = _setup_dlp_hook_in_sandbox(project_root, git_sandbox)

    demo_ds = git_sandbox / "examples" / "demo_vault" / "datasets" / "synthetic_orders.md"
    demo_opp = git_sandbox / "examples" / "demo_vault" / "opportunities" / "OPP-001-synthetic.md"
    demo_ds.parent.mkdir(parents=True, exist_ok=True)
    demo_opp.parent.mkdir(parents=True, exist_ok=True)
    demo_ds.write_text(
        "---\nfqn: demo_catalog.gold.synthetic_orders\n---\n# Synthetic Demo Dataset\n",
        encoding="utf-8",
        newline="\n",
    )
    demo_opp.write_text(
        "---\nid: OPP-001\ntitle: Synthetic Reorder Demo\n---\n# Synthetic Opportunity\n",
        encoding="utf-8",
        newline="\n",
    )

    _git(git_sandbox, clean_git_env, "add", ".gitignore", "examples/demo_vault/")

    ok, reasons = run_dlp_check(git_sandbox)
    assert ok is True, f"examples/demo_vault/ was falsely blocked by DLP: {reasons}"
    assert reasons == []

    commit_res = _git(git_sandbox, clean_git_env, "commit", "-m", "Seed examples/demo_vault", check=False)
    assert commit_res.returncode == 0, f"examples/demo_vault/ commit failed:\n{commit_res.stderr}"
    assert "[POOPDECK-DLP-BLOCKED]" not in commit_res.stderr


@pytest.mark.tier1
def test_f02_t1_dlp_corporate_remote_allowlist_permits_corporate_repo(
    project_root: Path,
    git_sandbox: Path,
    clean_git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify corporate_remote_patterns allows corporate-only origin while blocking if an external remote is added."""
    monkeypatch.delenv("POOPDECK_CONFIG_PATH", raising=False)
    monkeypatch.chdir(git_sandbox)

    _git(
        git_sandbox,
        clean_git_env,
        "remote",
        "add",
        "origin",
        "https://dev.azure.com/corp-org/analytics/_git/poopdeck",
    )
    (git_sandbox / "config.yaml").write_text(
        "dlp:\n  corporate_remote_patterns:\n    - 'dev.azure.com/corp-org'\n",
        encoding="utf-8",
        newline="\n",
    )
    _, run_dlp_check = _setup_dlp_hook_in_sandbox(project_root, git_sandbox)

    corp_card = git_sandbox / "vault" / "datasets" / "corp_gold_sales.md"
    corp_card.parent.mkdir(parents=True, exist_ok=True)
    corp_card.write_text(
        "---\nfqn: corp_catalog.gold.sales\n---\n# Corporate Sales Metadata\n",
        encoding="utf-8",
        newline="\n",
    )
    _git(git_sandbox, clean_git_env, "add", "-f", "vault/datasets/corp_gold_sales.md")

    ok, reasons = run_dlp_check(git_sandbox)
    assert ok is True, f"Corporate allowlisted remote should allow clean vault note, got: {reasons}"
    assert reasons == []
    commit_ok = _git(git_sandbox, clean_git_env, "commit", "-m", "Internal corporate repo commit", check=False)
    assert commit_ok.returncode == 0, f"Corporate commit failed:\n{commit_ok.stderr}"

    # Multi-remote safety: adding personal external remote must re-enable DLP block!
    _git(
        git_sandbox,
        clean_git_env,
        "remote",
        "add",
        "personal",
        "https://github.com/mckenzm4/project-poopdeck.git",
    )
    second_card = git_sandbox / "vault" / "datasets" / "corp_second.md"
    second_card.write_text(
        "---\nfqn: corp_catalog.gold.margins\n---\n# Confidential Margins\n",
        encoding="utf-8",
        newline="\n",
    )
    _git(git_sandbox, clean_git_env, "add", "-f", "vault/datasets/corp_second.md")

    ok_mixed, mixed_reasons = run_dlp_check(git_sandbox)
    assert ok_mixed is False
    assert len(mixed_reasons) >= 1
    blocked_res = _git(git_sandbox, clean_git_env, "commit", "-m", "Blocked mixed remote", check=False)
    assert blocked_res.returncode != 0
    assert "[POOPDECK-DLP-BLOCKED]" in blocked_res.stderr


@pytest.mark.tier1
def test_f02_t1_initialize_vault_creates_isolated_nested_git_repo(
    project_root: Path,
    git_sandbox: Path,
    clean_git_env: dict[str, str],
) -> None:
    """Verify initialize_vault creates an independent vault/.git invisible to parent root Git index."""
    from poopdeck.vault_io import initialize_vault

    shutil.copy2(project_root / ".gitignore", git_sandbox / ".gitignore")
    (git_sandbox / "README.md").write_text("# Root Engine\n", encoding="utf-8", newline="\n")
    _git(git_sandbox, clean_git_env, "add", ".gitignore", "README.md")
    _git(git_sandbox, clean_git_env, "commit", "-m", "Initial engine commit")

    nested_vault = git_sandbox / "vault"
    info = initialize_vault(nested_vault, seed_questions=True, init_git=True)
    assert info["git_initialized"] is True
    assert info["has_git"] is True
    assert (nested_vault / ".git").is_dir()
    assert len(info["seeded_files"]) == 3

    vault_top = _git(nested_vault, clean_git_env, "rev-parse", "--show-toplevel").stdout.strip()
    outer_top = _git(git_sandbox, clean_git_env, "rev-parse", "--show-toplevel").stdout.strip()
    assert Path(vault_top).resolve() == nested_vault.resolve()
    assert Path(outer_top).resolve() == git_sandbox.resolve()
    assert Path(vault_top).resolve() != Path(outer_top).resolve()

    _git(nested_vault, clean_git_env, "add", ".")
    _git(nested_vault, clean_git_env, "commit", "-m", "Day 1 vault seed")

    assert _git(git_sandbox, clean_git_env, "status", "--porcelain").stdout.strip() == ""
    assert _git(git_sandbox, clean_git_env, "ls-files", "vault").stdout.strip() == ""


# ============================================================================
# F02 Tier 2 — DLP Pre-Commit Blocks: Vault, 160000 Gitlink, Secrets & Dumps (6 tests)
# ============================================================================


@pytest.mark.tier2
def test_f02_t2_pre_commit_hook_blocks_forced_vault_file_on_mckenzm4(
    project_root: Path,
    git_sandbox: Path,
    clean_git_env: dict[str, str],
) -> None:
    """Verify forced `git add -f vault/...` is blocked both without remote and on mckenzm4 external remote."""
    shutil.copy2(project_root / ".gitignore", git_sandbox / ".gitignore")
    _, run_dlp_check = _setup_dlp_hook_in_sandbox(project_root, git_sandbox)

    secret_card = git_sandbox / "vault" / "datasets" / "secret.md"
    secret_card.parent.mkdir(parents=True, exist_ok=True)
    secret_card.write_text(
        "---\nfqn: corp.gold.executive_margins\n---\nProprietary margin formula.\n",
        encoding="utf-8",
        newline="\n",
    )
    _git(git_sandbox, clean_git_env, "add", "-f", "vault/datasets/secret.md")

    # Fail-closed even before any remote is configured
    ok_no_remote, reasons_no_remote = run_dlp_check(git_sandbox)
    assert ok_no_remote is False
    assert len(reasons_no_remote) >= 1

    # Configure mckenzm4 personal remote and verify hook blocks commit with [POOPDECK-DLP-BLOCKED]
    _git(
        git_sandbox,
        clean_git_env,
        "remote",
        "add",
        "origin",
        "https://github.com/mckenzm4/project-poopdeck.git",
    )
    ok, reasons = run_dlp_check(git_sandbox)
    assert ok is False
    assert any("vault" in r.lower() for r in reasons), f"Expected vault reason, got: {reasons}"

    res = _git(git_sandbox, clean_git_env, "commit", "-m", "Attempt forced vault commit", check=False)
    assert res.returncode != 0
    assert "[POOPDECK-DLP-BLOCKED]" in res.stderr


@pytest.mark.tier2
def test_f02_t2_dlp_blocks_nested_vault_160000_gitlink_on_mckenzm4(
    project_root: Path,
    git_sandbox: Path,
    clean_git_env: dict[str, str],
) -> None:
    """Verify staging nested `vault/.git` as mode `160000 \\tvault` gitlink is blocked on mckenzm4."""
    from poopdeck.vault_io import initialize_vault

    shutil.copy2(project_root / ".gitignore", git_sandbox / ".gitignore")
    _git(
        git_sandbox,
        clean_git_env,
        "remote",
        "add",
        "origin",
        "https://github.com/mckenzm4/project-poopdeck.git",
    )
    _, run_dlp_check = _setup_dlp_hook_in_sandbox(project_root, git_sandbox)

    # Create >= 1 commit inside vault/ so `git add -f vault` stages mode 160000 gitlink `vault`
    vdir = git_sandbox / "vault"
    initialize_vault(vdir, seed_questions=True, init_git=True)
    _git(vdir, clean_git_env, "add", ".")
    _git(vdir, clean_git_env, "commit", "-m", "Initial company vault commit")

    _git(git_sandbox, clean_git_env, "add", "-f", "vault")
    stage_out = _git(git_sandbox, clean_git_env, "ls-files", "--stage").stdout.strip()
    assert stage_out.startswith("160000 "), f"Expected 160000 gitlink, got: {stage_out}"
    assert stage_out.endswith("\tvault"), f"Expected entry name 'vault', got: {stage_out}"

    ok, reasons = run_dlp_check(git_sandbox)
    assert ok is False
    assert len(reasons) >= 1

    res = _git(
        git_sandbox,
        clean_git_env,
        "commit",
        "-m",
        "Attempt 160000 vault gitlink commit",
        check=False,
    )
    assert res.returncode != 0
    assert "[POOPDECK-DLP-BLOCKED]" in res.stderr


@pytest.mark.tier2
def test_f02_t2_dlp_blocks_env_pbix_vpax_and_exports_while_allowing_env_example(
    project_root: Path,
    git_sandbox: Path,
    clean_git_env: dict[str, str],
) -> None:
    """Verify .env.example commits cleanly while .env, .env.local, *.pbix, *.vpax & exports/* each block independently."""
    shutil.copy2(project_root / ".gitignore", git_sandbox / ".gitignore")
    _git(
        git_sandbox,
        clean_git_env,
        "remote",
        "add",
        "origin",
        "https://github.com/mckenzm4/project-poopdeck.git",
    )
    _, run_dlp_check = _setup_dlp_hook_in_sandbox(project_root, git_sandbox)

    # Positive control: .env.example is permitted
    (git_sandbox / ".env.example").write_text(
        "DATABRICKS_HOST=https://adb-placeholder.azuredatabricks.net\n",
        encoding="utf-8",
        newline="\n",
    )
    _git(git_sandbox, clean_git_env, "add", ".gitignore", ".env.example")
    assert run_dlp_check(git_sandbox) == (True, [])
    pos_commit = _git(git_sandbox, clean_git_env, "commit", "-m", "Add .env.example", check=False)
    assert pos_commit.returncode == 0

    forbidden_files: list[tuple[str, bytes]] = [
        (".env", b"DATABRICKS_HOST=https://corp.cloud.databricks.com\n"),
        (".env.local", b"POWERBI_WORKSPACE_ID=12345\n"),
        ("reports/exec_dashboard.pbix", b"PK\x03\x04synthetic-pbix-binary"),
        ("models/tabular_extract.vpax", b"PK\x03\x04synthetic-vpax-binary"),
        ("exports/portfolio_dump.csv", b"id,title\nOPP-001,Confidential\n"),
    ]
    for rel_path, payload in forbidden_files:
        target = git_sandbox / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        _git(git_sandbox, clean_git_env, "add", "-f", rel_path)

        ok, reasons = run_dlp_check(git_sandbox)
        assert ok is False, f"Expected DLP to block '{rel_path}', but ok=True"
        assert len(reasons) >= 1

        res = _git(git_sandbox, clean_git_env, "commit", "-m", f"Try {rel_path}", check=False)
        assert res.returncode != 0, f"Expected git commit to block '{rel_path}'"
        assert "[POOPDECK-DLP-BLOCKED]" in res.stderr

        _git(git_sandbox, clean_git_env, "rm", "--cached", "-f", rel_path)
        target.unlink()


@pytest.mark.tier2
def test_f02_t2_dlp_blocks_pats_api_keys_and_bearer_jwt_in_staged_diff(
    project_root: Path,
    git_sandbox: Path,
    clean_git_env: dict[str, str],
) -> None:
    """Verify staged engine diffs containing Databricks/GitHub PATs, Anthropic/AWS keys, or Bearer JWTs are blocked."""
    _git(
        git_sandbox,
        clean_git_env,
        "remote",
        "add",
        "origin",
        "https://github.com/mckenzm4/project-poopdeck.git",
    )
    _, run_dlp_check = _setup_dlp_hook_in_sandbox(project_root, git_sandbox)

    (git_sandbox / "README.md").write_text("# Clean Repo\n", encoding="utf-8", newline="\n")
    _git(git_sandbox, clean_git_env, "add", "README.md")
    assert _git(git_sandbox, clean_git_env, "commit", "-m", "Initial clean commit", check=False).returncode == 0

    secret_snippets = [
        'DBX_PAT = "dapi0123456789abcdef0123456789abcdef"\n',
        'GH_PAT = "ghp_0123456789abcdefghijklmnopqrstuvwxyzABCD"\n',
        'GH_FG = "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz0123456789"\n',
        'ANTHROPIC_KEY = "sk-ant-api03-0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN"\n',
        'AWS_KEY = "AKIAIOSFODNN7EXAMPLE"\n',
        (
            'HEADER = "Authorization: Bearer '
            'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.'
            'eyJzdWIiOiJ1c2VyLTEyMyJ9.'
            'SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"\n'
        ),
    ]
    probe_file = git_sandbox / "poopdeck" / "auth_probe.py"
    probe_file.parent.mkdir(parents=True, exist_ok=True)

    for snippet in secret_snippets:
        probe_file.write_text(snippet, encoding="utf-8", newline="\n")
        _git(git_sandbox, clean_git_env, "add", "poopdeck/auth_probe.py")

        ok, reasons = run_dlp_check(git_sandbox)
        assert ok is False, f"Expected secret snippet to be blocked: {snippet.strip()}"
        assert len(reasons) >= 1

        res = _git(git_sandbox, clean_git_env, "commit", "-m", "Attempt secret commit", check=False)
        assert res.returncode != 0
        assert "[POOPDECK-DLP-BLOCKED]" in res.stderr

        _git(git_sandbox, clean_git_env, "rm", "--cached", "-f", "poopdeck/auth_probe.py")
        probe_file.unlink()


@pytest.mark.tier2
def test_f02_t2_dlp_blocks_jdbc_azure_sql_and_private_key_secrets(
    project_root: Path,
    git_sandbox: Path,
    clean_git_env: dict[str, str],
) -> None:
    """Verify staged engine diffs containing JDBC PWD, ADLS AccountKey, SQL Password, or PEM private keys are blocked."""
    _git(
        git_sandbox,
        clean_git_env,
        "remote",
        "add",
        "origin",
        "https://github.com/mckenzm4/project-poopdeck.git",
    )
    _, run_dlp_check = _setup_dlp_hook_in_sandbox(project_root, git_sandbox)

    (git_sandbox / "README.md").write_text("# Clean Repo\n", encoding="utf-8", newline="\n")
    _git(git_sandbox, clean_git_env, "add", "README.md")
    assert _git(git_sandbox, clean_git_env, "commit", "-m", "Initial clean commit", check=False).returncode == 0

    conn_snippets = [
        (
            'JDBC_URL = "jdbc:databricks://adb-1234567890123456.7.azuredatabricks.net:443/default;'
            'HttpPath=/sql/1.0/warehouses/ab12cd34;PWD=SuperSecretPass123!"\n'
        ),
        (
            'ADLS_CONN = "DefaultEndpointsProtocol=https;AccountName=corplake;'
            'AccountKey=wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY0123456789abcdef==;'
            'EndpointSuffix=core.windows.net"\n'
        ),
        (
            'SQL_CONN = "Server=tcp:erp-sql-prod.database.windows.net,1433;'
            'Database=supply_chain;Uid=svc_reader;Password=SqlProdSecret987!;Encrypt=true;"\n'
        ),
        (
            'PEM_KEY = """-----BEGIN RSA PRIVATE KEY-----\n'
            "MIIEpAIBAAKCAQEA0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKL\n"
            '-----END RSA PRIVATE KEY-----"""\n'
        ),
    ]
    probe_file = git_sandbox / "poopdeck" / "conn_probe.py"
    probe_file.parent.mkdir(parents=True, exist_ok=True)

    for snippet in conn_snippets:
        probe_file.write_text(snippet, encoding="utf-8", newline="\n")
        _git(git_sandbox, clean_git_env, "add", "poopdeck/conn_probe.py")

        ok, reasons = run_dlp_check(git_sandbox)
        assert ok is False, f"Expected connection/PEM secret to be blocked: {snippet[:48]}"
        assert len(reasons) >= 1

        res = _git(git_sandbox, clean_git_env, "commit", "-m", "Attempt conn secret commit", check=False)
        assert res.returncode != 0
        assert "[POOPDECK-DLP-BLOCKED]" in res.stderr

        _git(git_sandbox, clean_git_env, "rm", "--cached", "-f", "poopdeck/conn_probe.py")
        probe_file.unlink()


@pytest.mark.tier2
def test_f02_t2_dlp_blocks_catalog_and_table_dumps_outside_tests_and_demo(
    project_root: Path,
    git_sandbox: Path,
    clean_git_env: dict[str, str],
) -> None:
    """Verify catalog/table dumps, .parquet files, and bulk INSERT dumps are blocked outside tests/ & demo_vault/."""
    _git(
        git_sandbox,
        clean_git_env,
        "remote",
        "add",
        "origin",
        "https://github.com/mckenzm4/project-poopdeck.git",
    )
    _, run_dlp_check = _setup_dlp_hook_in_sandbox(project_root, git_sandbox)

    (git_sandbox / "README.md").write_text("# Clean Repo\n", encoding="utf-8", newline="\n")
    _git(git_sandbox, clean_git_env, "add", "README.md")
    assert _git(git_sandbox, clean_git_env, "commit", "-m", "Initial clean commit", check=False).returncode == 0

    bulk_sql_dump = "".join(
        f"INSERT INTO prod_catalog.gold.customer_orders (order_id, net_usd) "
        f"VALUES ('ORD-{idx:03d}A', 450.00), ('ORD-{idx:03d}B', 910.25), ('ORD-{idx:03d}C', 125.00);\n"
        for idx in range(1, 13)
    )

    dump_vectors: list[tuple[str, bytes]] = [
        ("data/unity_catalog_dump_q4.json", b'{"tables": [{"fqn": "prod.gold.orders"}]}'),
        ("data/erp_table_dump_2026.csv", b"order_id,amount\n1,99.5\n"),
        ("data/information_schema_dump.json", b'{"columns": []}'),
        ("data/warehouse_snapshot.parquet", b"PAR1\x00\x00\x00\x00PAR1"),
        ("queries/raw_orders_dump.sql", bulk_sql_dump.encode("utf-8")),
    ]

    for rel_path, payload in dump_vectors:
        target = git_sandbox / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        _git(git_sandbox, clean_git_env, "add", "-f", rel_path)

        ok, reasons = run_dlp_check(git_sandbox)
        assert ok is False, f"Expected dump vector '{rel_path}' to be blocked"
        assert len(reasons) >= 1

        res = _git(git_sandbox, clean_git_env, "commit", "-m", f"Attempt {rel_path}", check=False)
        assert res.returncode != 0
        assert "[POOPDECK-DLP-BLOCKED]" in res.stderr

        _git(git_sandbox, clean_git_env, "rm", "--cached", "-f", rel_path)
        target.unlink()

    # Path exemption: synthetic SQL fixtures inside tests/ are permitted
    exempt_sql = git_sandbox / "tests" / "fixtures" / "synthetic_seed.sql"
    exempt_sql.parent.mkdir(parents=True, exist_ok=True)
    exempt_sql.write_text(bulk_sql_dump, encoding="utf-8", newline="\n")
    _git(git_sandbox, clean_git_env, "add", "tests/fixtures/synthetic_seed.sql")

    assert run_dlp_check(git_sandbox) == (True, [])
    exempt_res = _git(git_sandbox, clean_git_env, "commit", "-m", "Add synthetic test fixture", check=False)
    assert exempt_res.returncode == 0, f"Expected tests/ SQL fixture to be exempted:\n{exempt_res.stderr}"
