"""Milestone M4 unit tests: Pre-commit DLP hook, Claude Code 14-file harness, docs, and synthetic demo vault."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Iterator

import pytest
import yaml

from poopdeck.brief import (
    generate_opportunity_brief,
    generate_portfolio_brief,
    generate_verify_questions_brief,
)
from poopdeck.config import DLPConfig, PoopdeckConfig
from poopdeck.models import (
    DatasetCard,
    ReportCard,
    StakeholderCard,
    compute_portfolio_schedule,
    evaluate_driver_formula,
)
from poopdeck.packs import load_external_data_catalog
from poopdeck.redaction import redact_text
from poopdeck.vault_io import (
    HUMAN_NOTES_END,
    HUMAN_NOTES_START,
    archive_opportunity,
    build_lineage_graph,
    clean_git_env,
    initialize_vault,
    load_all_opportunities,
    load_questions_and_playbook,
    parse_markdown_card,
    promote_opportunity,
    safe_rmtree,
    set_boss_mandate,
    validate_vault,
)
import scripts.install_dlp_hook as dlp_mod
from scripts.install_dlp_hook import DLP_BANNER, install_hook, main as dlp_main, run_dlp_check

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_VAULT_DIR = REPO_ROOT / "examples" / "demo_vault"

EXPECTED_SUBAGENTS: tuple[str, ...] = (
    "catalog-cartographer",
    "bi-bridge-analyst",
    "context-harvester",
    "ds-opportunity-architect",
    "skeptical-cfo-critic",
)

EXPECTED_COMMANDS: tuple[str, ...] = (
    "onboard-check",
    "sync-workspace",
    "ingest-notes",
    "discover-opportunities",
    "review-staging",
    "boss-override",
    "exec-brief",
)


def _sandbox_git_env() -> dict[str, str]:
    """Return isolated subprocess env with GIT_* and Poopdeck override vars stripped."""
    env = clean_git_env()
    for key in list(env):
        if key.upper().startswith("GIT_"):
            env.pop(key, None)
    env.pop("POOPDECK_CONFIG_PATH", None)
    env.pop("POOPDECK_VAULT_DIR", None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _git(repo_dir: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    """Run git inside repo_dir with isolated env."""
    run_env = env if env is not None else _sandbox_git_env()
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_dir),
        env=run_env,
        capture_output=True,
        check=False,
    )


def _init_git_sandbox(base_dir: Path, *, copy_hook_script: bool = True) -> Path:
    """Create a fresh Git repository in base_dir with repo-local identity configured."""
    base_dir.mkdir(parents=True, exist_ok=True)
    res = _git(base_dir, "init")
    assert res.returncode == 0, res.stderr.decode("utf-8", errors="replace")
    assert _git(base_dir, "config", "user.name", "Poopdeck Test User").returncode == 0
    assert _git(base_dir, "config", "user.email", "test@poopdeck.local").returncode == 0
    assert _git(base_dir, "config", "core.autocrlf", "false").returncode == 0

    if copy_hook_script:
        dst_script = base_dir / "scripts" / "install_dlp_hook.py"
        dst_script.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / "scripts" / "install_dlp_hook.py", dst_script)

    return base_dir


def _write_lf(path: Path, content: str) -> Path:
    """Write UTF-8 LF-only text file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    return path


def _parse_frontmatter_markdown(path: Path) -> tuple[dict[str, object], str]:
    """Extract YAML frontmatter mapping and Markdown body from a .claude/*.md file."""
    text = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n?(.*)\Z", text, flags=re.DOTALL)
    assert match is not None, f"Missing YAML frontmatter block in {path}"
    fm = yaml.safe_load(match.group(1))
    assert isinstance(fm, dict), f"Frontmatter in {path} must parse to a dict, got {type(fm)}"
    return fm, match.group(2)


def _sha256_manifest(root: Path) -> dict[str, str]:
    """Compute relative-path -> SHA-256 digest map for every file under root."""
    manifest: dict[str, str] = {}
    for file_path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = file_path.relative_to(root).as_posix()
        manifest[rel] = hashlib.sha256(file_path.read_bytes()).hexdigest()
    return manifest


@pytest.fixture()
def git_sandbox(tmp_path: Path) -> Iterator[Path]:
    """Yield an isolated Git repository sandbox and tear down read-only .git packfiles cleanly."""
    repo = _init_git_sandbox(tmp_path / "engine_sandbox")
    try:
        yield repo
    finally:
        safe_rmtree(repo)


# ---------------------------------------------------------------------------
# T01 - T08: scripts/install_dlp_hook.py Unit & Real Git Hook Execution Tests
# ---------------------------------------------------------------------------


def test_t01_install_hook_lf_python_probing_idempotence_and_errors(tmp_path: Path) -> None:
    """T01: install_hook writes LF-only sh.exe-safe pre-commit script, supports gitfiles, and rejects non-repos."""
    missing_repo = tmp_path / "no_git_here"
    missing_repo.mkdir(parents=True, exist_ok=True)
    with pytest.raises(FileNotFoundError):
        install_hook(missing_repo)

    spaced_repo = _init_git_sandbox(tmp_path / "Dir With Spaces" / "poopdeck repo")
    try:
        hook_path = install_hook(spaced_repo)
        assert hook_path == spaced_repo / ".git" / "hooks" / "pre-commit"
        assert hook_path.is_file()

        raw_first = hook_path.read_bytes()
        assert not raw_first.startswith(b"\xef\xbb\xbf")
        assert b"\r" not in raw_first
        assert raw_first.startswith(b"#!/bin/sh\n")

        hook_text = raw_first.decode("utf-8")
        assert 'import sys' in hook_text
        assert "scripts/install_dlp_hook.py" in hook_text
        assert "--hook" in hook_text
        assert "POOPDECK_ENGINE_ROOT=" in hook_text
        assert sys.executable.replace("\\", "/") in hook_text

        # Second call is idempotent
        hook_path_second = install_hook(spaced_repo)
        assert hook_path_second == hook_path
        assert hook_path.read_bytes() == raw_first

        # Git worktree style .git pointer file
        wt_dir = tmp_path / "worktree_checkout"
        actual_git_dir = tmp_path / "actual_worktree_gitdir"
        actual_git_dir.mkdir(parents=True, exist_ok=True)
        wt_dir.mkdir(parents=True, exist_ok=True)
        _write_lf(wt_dir / ".git", f"gitdir: {actual_git_dir.as_posix()}\n")
        wt_hook = install_hook(wt_dir)
        assert wt_hook == actual_git_dir / "hooks" / "pre-commit"
        assert b"\r" not in wt_hook.read_bytes()
    finally:
        safe_rmtree(spaced_repo)


def test_t02_dlp_allows_clean_engine_demo_vault_and_engine_root_fallback(tmp_path: Path) -> None:
    """T02: Clean engine files, partial poopdeck/ dir, .env.example, and examples/demo_vault/** pass on mckenzm4 remote."""
    spaced_repo = _init_git_sandbox(
        tmp_path / "Space Path" / "clean_engine",
        copy_hook_script=False,  # Also exercises POOPDECK_ENGINE_ROOT fallback!
    )
    try:
        install_hook(spaced_repo)
        assert (
            _git(
                spaced_repo,
                "remote",
                "add",
                "origin",
                "https://github.com/mckenzm4/project-poopdeck.git",
            ).returncode
            == 0
        )

        # Partial poopdeck/ directory WITHOUT config.py to verify zero sys.path shadowing
        _write_lf(spaced_repo / "poopdeck" / "feature_helper.py", "VALUE = 42\n")
        _write_lf(spaced_repo / ".env.example", "DATABRICKS_HOST=https://adb-example.azuredatabricks.net\n")
        shutil.copy2(REPO_ROOT / "config.example.yaml", spaced_repo / "config.example.yaml")
        shutil.copytree(DEMO_VAULT_DIR, spaced_repo / "examples" / "demo_vault")

        assert _git(spaced_repo, "add", ".").returncode == 0

        ok, reasons = run_dlp_check(spaced_repo)
        assert ok is True, reasons
        assert reasons == []

        commit_res = _git(spaced_repo, "commit", "-m", "Initial clean engine + demo vault commit")
        stderr_text = commit_res.stderr.decode("utf-8", errors="replace")
        assert commit_res.returncode == 0, stderr_text
        assert DLP_BANNER not in stderr_text
    finally:
        safe_rmtree(spaced_repo)


def test_t03_dlp_blocks_vault_paths_and_160000_gitlink_on_unborn_and_external(tmp_path: Path) -> None:
    """T03: Staged vault/ files and mode-160000 vault gitlinks are blocked with [POOPDECK-DLP-BLOCKED]."""
    # Case A: No remote configured -> fails closed on vault/datasets/secret.md
    repo_no_remote = _init_git_sandbox(tmp_path / "repo_no_remote")
    try:
        install_hook(repo_no_remote)
        _write_lf(repo_no_remote / "vault" / "datasets" / "secret.md", "# Confidential Table\n")
        assert _git(repo_no_remote, "add", "-f", "vault/datasets/secret.md").returncode == 0

        ok, reasons = run_dlp_check(repo_no_remote)
        assert ok is False
        assert any("vault" in r.lower() for r in reasons), reasons

        res = _git(repo_no_remote, "commit", "-m", "Attempt vault leak without remote")
        err = res.stderr.decode("utf-8", errors="replace")
        assert res.returncode != 0
        assert DLP_BANNER in err
    finally:
        safe_rmtree(repo_no_remote)

    # Case B: Unborn outer HEAD + nested vault/.git with a commit -> mode 160000 gitlink on mckenzm4 remote
    repo_gitlink = _init_git_sandbox(tmp_path / "repo_gitlink")
    try:
        install_hook(repo_gitlink)
        assert (
            _git(
                repo_gitlink,
                "remote",
                "add",
                "origin",
                "https://github.com/mckenzm4/project-poopdeck.git",
            ).returncode
            == 0
        )
        nested_vault = repo_gitlink / "vault"
        initialize_vault(nested_vault, seed_questions=True, init_git=True)
        assert _git(nested_vault, "config", "user.name", "Vault User").returncode == 0
        assert _git(nested_vault, "config", "user.email", "vault@poopdeck.local").returncode == 0
        assert _git(nested_vault, "add", ".").returncode == 0
        assert _git(nested_vault, "commit", "-m", "Seed isolated company vault").returncode == 0

        # Force-stage nested vault repo as 160000 gitlink in outer repo
        add_res = _git(repo_gitlink, "add", "-f", "vault")
        assert add_res.returncode == 0, add_res.stderr.decode("utf-8", errors="replace")

        ok_link, link_reasons = run_dlp_check(repo_gitlink)
        assert ok_link is False
        assert any("160000" in r and "vault" in r.lower() for r in link_reasons), link_reasons

        commit_link = _git(repo_gitlink, "commit", "-m", "Attempt 160000 vault gitlink")
        link_err = commit_link.stderr.decode("utf-8", errors="replace")
        assert commit_link.returncode != 0
        assert DLP_BANNER in link_err
    finally:
        safe_rmtree(repo_gitlink)


@pytest.mark.parametrize(
    "forbidden_relpath",
    [
        ".env",
        ".env.local",
        "reports/exec_dashboard.pbix",
        "examples/demo_vault/leak.pbix",
        "models/tabular_extract.vpax",
        "exports/portfolio_brief.md",
    ],
)
def test_t04_dlp_blocks_forbidden_env_pbix_vpax_and_exports(
    git_sandbox: Path,
    forbidden_relpath: str,
) -> None:
    """T04: Every forbidden artifact path (.env*, *.pbix, *.vpax, exports/*) blocks commit on external remote."""
    install_hook(git_sandbox)
    assert (
        _git(
            git_sandbox,
            "remote",
            "add",
            "origin",
            "https://github.com/mckenzm4/project-poopdeck.git",
        ).returncode
        == 0
    )

    # First verify non-root 'exports' segment under tests/ is allowed
    _write_lf(git_sandbox / "tests" / "fixtures" / "powerbi" / "dax_info_exports" / "ok.json", "{}\n")
    assert _git(git_sandbox, "add", "-f", "tests/fixtures/powerbi/dax_info_exports/ok.json").returncode == 0
    ok_clean, clean_reasons = run_dlp_check(git_sandbox)
    assert ok_clean is True, clean_reasons

    # Now stage the forbidden artifact
    _write_lf(git_sandbox / forbidden_relpath, "placeholder_artifact_payload\n")
    assert _git(git_sandbox, "add", "-f", forbidden_relpath).returncode == 0

    ok, reasons = run_dlp_check(git_sandbox)
    assert ok is False
    assert any(forbidden_relpath in r for r in reasons), reasons

    commit_res = _git(git_sandbox, "commit", "-m", f"Attempt forbidden {forbidden_relpath}")
    stderr_text = commit_res.stderr.decode("utf-8", errors="replace")
    assert commit_res.returncode != 0
    assert DLP_BANNER in stderr_text


def _build_synthetic_secret_vectors() -> list[tuple[str, str]]:
    """Construct synthetic secret probes dynamically so this test file contains zero literal tokens."""
    return [
        ("databricks_pat", "TOKEN = '" + "dapi" + ("0123456789abcdef" * 2) + "'\n"),
        ("github_classic_pat", "GH = '" + "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6" + "'\n"),
        ("github_fine_pat", "GH2 = '" + "github_pat_" + "11ABCDEF0123456789_abcdefghijklmnop" + "'\n"),
        ("anthropic_key", "ANTH = '" + "sk-ant-" + "api03-AbCdEfGhIjKlMnOpQrStUvWxYz12" + "'\n"),
        ("aws_akia", "AWS_ID = '" + "AKIA" + "IOSFODNN7EXAMPLE" + "'\n"),
        (
            "azure_account_key",
            "CONN = 'DefaultEndpointsProtocol=https;AccountName=corp;"
            + "AccountKey="
            + "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY=="
            + "'\n",
        ),
        (
            "databricks_jdbc_pwd",
            "URL = 'jdbc:databricks://adb-99.azuredatabricks.net:443/default;UID=token;"
            + "PWD="
            + "SuperSecretPass123!"
            + "'\n",
        ),
        (
            "sql_server_password",
            "SQL = 'Server=tcp:corp.database.windows.net,1433;Database=erp;"
            + "Password="
            + "SqlProdSecret987!"
            + "'\n",
        ),
        (
            "bearer_jwt",
            "HDR = 'Authorization: Bearer "
            + "eyJhbGciOiJIUzI1NiJ9"
            + "."
            + "eyJzdWIiOiJ1c2VyIn0"
            + "."
            + "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c'\n",
        ),
        (
            "pem_private_key",
            "-----BEGIN RSA "
            + "PRIVATE KEY-----\n"
            + ("MIIEpAIBAAKCAQEA0123456789abcdef" * 2)
            + "\n-----END RSA "
            + "PRIVATE KEY-----\n",
        ),
    ]


@pytest.mark.parametrize(("secret_name", "payload"), _build_synthetic_secret_vectors())
def test_t05_dlp_blocks_secrets_on_both_external_and_corporate_remotes(
    tmp_path: Path,
    secret_name: str,
    payload: str,
) -> None:
    """T05: Raw credentials/PATs/JDBC passwords/PEM keys are blocked on both external and corporate remotes."""
    repo = _init_git_sandbox(tmp_path / f"secret_{secret_name}")
    try:
        install_hook(repo)
        _write_lf(
            repo / "config.yaml",
            "dlp:\n  corporate_remote_patterns:\n    - 'dev.azure.com/corp-org'\n",
        )
        assert (
            _git(
                repo,
                "remote",
                "add",
                "origin",
                "https://dev.azure.com/corp-org/analytics/_git/poopdeck",
            ).returncode
            == 0
        )

        # Symbolic placeholders in docs are allowed
        _write_lf(
            repo / "docs_ok.md",
            "Placeholders: `dapi...`, `ghp_...`, `jdbc:databricks://host;PWD=<REDACTED>`, `AccountKey=[REDACTED:SECRET]`.\n",
        )
        assert _git(repo, "add", "docs_ok.md").returncode == 0
        ok_doc, doc_reasons = run_dlp_check(repo)
        assert ok_doc is True, doc_reasons

        # Staging real-shaped credential in engine probe blocks even on corporate remote
        _write_lf(repo / "poopdeck" / "auth_probe.py", payload)
        assert _git(repo, "add", "poopdeck/auth_probe.py").returncode == 0

        ok_corp, corp_reasons = run_dlp_check(repo)
        assert ok_corp is False, f"{secret_name} was not blocked on corporate remote"
        assert any("secret/credential" in r.lower() for r in corp_reasons), corp_reasons

        commit_corp = _git(repo, "commit", "-m", f"Leak {secret_name}")
        assert commit_corp.returncode != 0
        assert DLP_BANNER in commit_corp.stderr.decode("utf-8", errors="replace")

        # Also blocked on external remote
        assert (
            _git(
                repo,
                "remote",
                "set-url",
                "origin",
                "https://github.com/mckenzm4/project-poopdeck.git",
            ).returncode
            == 0
        )
        ok_ext, _ = run_dlp_check(repo)
        assert ok_ext is False
    finally:
        safe_rmtree(repo)


def test_t06_dlp_blocks_catalog_and_sql_dumps_while_exempting_tests_and_deletions(
    git_sandbox: Path,
) -> None:
    """T06: Catalog/table/parquet/SQL dumps block outside tests/, while tests/** and deletions pass."""
    install_hook(git_sandbox)
    assert (
        _git(
            git_sandbox,
            "remote",
            "add",
            "origin",
            "https://github.com/mckenzm4/project-poopdeck.git",
        ).returncode
        == 0
    )

    multi_row_sql = (
        "INSERT INTO prod_orders (id, amount) VALUES (1, 120.50), (2, 340.00), (3, 89.25);\n"
    )
    multi_stmt_sql = (
        "INSERT INTO prod_orders VALUES (1, 10);\nINSERT INTO prod_orders VALUES (2, 20);\n"
    )

    dump_vectors = [
        ("data/unity_catalog_dump_q4.json", '{"tables": []}\n'),
        ("data/erp_table_dump_2026.csv", "id,val\n1,2\n"),
        ("data/information_schema_dump.json", '{"columns": []}\n'),
        ("data/warehouse_snapshot.parquet", "PAR1_BINARY_STUB\n"),
        ("queries/raw_orders_dump.sql", multi_row_sql),
        ("queries/multi_stmt_dump.sql", multi_stmt_sql),
    ]

    for rel_path, content in dump_vectors:
        _write_lf(git_sandbox / rel_path, content)
        assert _git(git_sandbox, "add", "-f", rel_path).returncode == 0
        ok, reasons = run_dlp_check(git_sandbox)
        assert ok is False, f"Expected dump {rel_path} to be blocked"
        assert any(rel_path in r for r in reasons), reasons
        assert _git(git_sandbox, "reset", "HEAD", "--", rel_path).returncode == 0
        (git_sandbox / rel_path).unlink()

    # Exact same SQL dump + synthetic PAT under tests/fixtures/ MUST be allowed
    synthetic_pat = "dapi" + ("0123456789abcdef" * 2)
    _write_lf(
        git_sandbox / "tests" / "fixtures" / "synthetic_seed.sql",
        f"-- {synthetic_pat}\n{multi_row_sql}",
    )
    _write_lf(git_sandbox / "README_TEMP.md", "# Clean file to delete next\n")
    assert _git(git_sandbox, "add", "tests/fixtures/synthetic_seed.sql", "README_TEMP.md").returncode == 0

    ok_test, test_reasons = run_dlp_check(git_sandbox)
    assert ok_test is True, test_reasons
    assert _git(git_sandbox, "commit", "-m", "Allow synthetic test fixture").returncode == 0

    # Staged deletion (`D`) is cleanly skipped without error
    assert _git(git_sandbox, "rm", "README_TEMP.md").returncode == 0
    ok_del, del_reasons = run_dlp_check(git_sandbox)
    assert ok_del is True, del_reasons
    assert _git(git_sandbox, "commit", "-m", "Delete temp readme").returncode == 0


def test_t07_dlp_corporate_allowlist_multi_remote_partial_commit_and_poisoned_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T07: Corporate allowlist allows vault/ until external secondary remote added; handles commit -a, partial commit & poisoned GIT_*."""
    corp_repo = _init_git_sandbox(tmp_path / "corp_vault_repo")
    decoy_repo = _init_git_sandbox(tmp_path / "decoy_poison_repo")
    try:
        install_hook(corp_repo)
        _write_lf(
            corp_repo / "config.yaml",
            "dlp:\n"
            "  corporate_remote_patterns:\n"
            "    - 'dev.azure.com/corp-org'\n"
            "    - 'github.com/acme-corp'\n",
        )
        assert (
            _git(
                corp_repo,
                "remote",
                "add",
                "origin",
                "https://dev.azure.com/corp-org/analytics/_git/poopdeck",
            ).returncode
            == 0
        )

        _write_lf(corp_repo / "vault" / "datasets" / "corp_gold_sales.md", "# Corp Gold Sales\n")
        assert _git(corp_repo, "add", "-f", "vault/datasets/corp_gold_sales.md", "config.yaml").returncode == 0

        ok_corp, corp_reasons = run_dlp_check(corp_repo)
        assert ok_corp is True, corp_reasons
        assert _git(corp_repo, "commit", "-m", "Allow corporate vault commit").returncode == 0

        # GitHub Enterprise / corporate org override ("github.com/acme-corp") also counts as corporate
        cfg_acme = PoopdeckConfig(
            dlp=DLPConfig(corporate_remote_patterns=["github.com/acme-corp"])
        )
        assert _git(corp_repo, "remote", "set-url", "origin", "https://github.com/acme-corp/vault.git").returncode == 0
        _write_lf(corp_repo / "vault" / "datasets" / "second.md", "# Second\n")
        assert _git(corp_repo, "add", "-f", "vault/datasets/second.md").returncode == 0
        assert run_dlp_check(corp_repo, config=cfg_acme) == (True, [])
        assert _git(corp_repo, "commit", "-m", "Allow acme-corp vault commit").returncode == 0

        # Restore azure remote and add secondary external mckenzm4 remote -> must block new vault edits!
        assert (
            _git(
                corp_repo,
                "remote",
                "set-url",
                "origin",
                "https://dev.azure.com/corp-org/analytics/_git/poopdeck",
            ).returncode
            == 0
        )
        assert (
            _git(
                corp_repo,
                "remote",
                "add",
                "personal",
                "https://github.com/mckenzm4/project-poopdeck.git",
            ).returncode
            == 0
        )

        # Test `git commit -a` (unstaged modification to tracked vault file staged via .git/index.lock)
        _write_lf(corp_repo / "vault" / "datasets" / "corp_gold_sales.md", "# Modified with personal remote present\n")
        commit_a = _git(corp_repo, "commit", "-a", "-m", "Attempt commit -a leak")
        assert commit_a.returncode != 0
        assert DLP_BANNER in commit_a.stderr.decode("utf-8", errors="replace")

        # Test partial `git commit <clean_file>` while a forbidden file is also staged in main .git/index
        _write_lf(corp_repo / "clean_note.md", "# Clean engine note\n")
        assert _git(corp_repo, "add", "clean_note.md", "vault/datasets/corp_gold_sales.md").returncode == 0
        partial_ok = _git(corp_repo, "commit", "clean_note.md", "-m", "Partial commit of clean file only")
        assert partial_ok.returncode == 0, partial_ok.stderr.decode("utf-8", errors="replace")

        # Remaining staged vault file is still blocked on normal commit
        remaining_blocked = _git(corp_repo, "commit", "-m", "Remaining staged vault file")
        assert remaining_blocked.returncode != 0
        assert DLP_BANNER in remaining_blocked.stderr.decode("utf-8", errors="replace")

        # Poison outer GIT_DIR and GIT_INDEX_FILE pointing to decoy_repo with a secret staged
        _write_lf(decoy_repo / "leak.py", "TOK = '" + "dapi" + ("abcdef0123456789" * 2) + "'\n")
        assert _git(decoy_repo, "add", "leak.py").returncode == 0
        assert _git(corp_repo, "reset", "HEAD", "--", "vault/datasets/corp_gold_sales.md").returncode == 0

        monkeypatch.setenv("GIT_DIR", str(decoy_repo / ".git"))
        monkeypatch.setenv("GIT_INDEX_FILE", str(decoy_repo / ".git" / "index"))
        ok_isolated, iso_reasons = run_dlp_check(corp_repo)
        assert ok_isolated is True, iso_reasons
    finally:
        safe_rmtree(corp_repo)
        safe_rmtree(decoy_repo)


def test_t08_dlp_cli_main_entrypoint(
    git_sandbox: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T08: scripts/install_dlp_hook.py main() installs hook and returns 0/1 with [POOPDECK-DLP-BLOCKED] under --hook."""
    # Missing .git returns 1 on install
    rc_missing = dlp_main(["--repo-root", str(tmp_path / "does_not_exist")])
    captured_missing = capsys.readouterr()
    assert rc_missing == 1
    assert DLP_BANNER in captured_missing.err

    # Install via main()
    rc_install = dlp_main(["--repo-root", str(git_sandbox)])
    captured_install = capsys.readouterr()
    assert rc_install == 0
    assert "pre-commit" in captured_install.out

    assert (
        _git(
            git_sandbox,
            "remote",
            "add",
            "origin",
            "https://github.com/mckenzm4/project-poopdeck.git",
        ).returncode
        == 0
    )

    _write_lf(git_sandbox / "README.md", "# Clean\n")
    assert _git(git_sandbox, "add", "README.md").returncode == 0
    assert dlp_main(["--repo-root", str(git_sandbox), "--hook"]) == 0
    assert capsys.readouterr().err == ""

    _write_lf(git_sandbox / ".env", "SECRET=1\n")
    assert _git(git_sandbox, "add", "-f", ".env").returncode == 0
    rc_blocked = dlp_main(["--repo-root", str(git_sandbox), "--hook"])
    captured_blocked = capsys.readouterr()
    assert rc_blocked == 1
    assert DLP_BANNER in captured_blocked.err
    assert ".env" in captured_blocked.err


# ---------------------------------------------------------------------------
# T09 - T13: Claude Code 14-File Harness, Docs & DLP Self-Scan Verification
# ---------------------------------------------------------------------------


def test_t09_all_m4_deliverables_exist_utf8_lf_and_pass_dlp_self_scan(git_sandbox: Path) -> None:
    """T09: Every M4 file exists, is UTF-8 without BOM, LF-only, and triggers zero DLP false positives when staged."""
    explicit_files = [
        REPO_ROOT / "scripts" / "install_dlp_hook.py",
        REPO_ROOT / "CLAUDE.md",
        REPO_ROOT / "README.md",
        REPO_ROOT / ".claude" / "settings.json",
        *[REPO_ROOT / ".claude" / "agents" / f"{stem}.md" for stem in EXPECTED_SUBAGENTS],
        *[REPO_ROOT / ".claude" / "commands" / f"{stem}.md" for stem in EXPECTED_COMMANDS],
        REPO_ROOT / "tests" / "unit" / "test_m4_dlp_claude_demo.py",
    ]
    demo_files = sorted(p for p in DEMO_VAULT_DIR.rglob("*") if p.is_file())
    assert len(demo_files) == 23, [p.relative_to(DEMO_VAULT_DIR).as_posix() for p in demo_files]

    all_m4_files = [*explicit_files, *demo_files]
    for path in all_m4_files:
        assert path.is_file(), f"Missing required M4 deliverable: {path}"
        raw = path.read_bytes()
        assert len(raw) > 0, f"Empty M4 deliverable: {path}"
        assert not raw.startswith(b"\xef\xbb\xbf"), f"UTF-8 BOM forbidden in: {path}"
        assert b"\r" not in raw, f"CRLF carriage return forbidden in: {path}"
        raw.decode("utf-8")

    # Stage every single M4 deliverable into git_sandbox with external mckenzm4 remote
    install_hook(git_sandbox)
    assert (
        _git(
            git_sandbox,
            "remote",
            "add",
            "origin",
            "https://github.com/mckenzm4/project-poopdeck.git",
        ).returncode
        == 0
    )
    shutil.copy2(REPO_ROOT / "config.example.yaml", git_sandbox / "config.example.yaml")
    for path in all_m4_files:
        rel = path.relative_to(REPO_ROOT)
        dst = git_sandbox / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst)

    assert _git(git_sandbox, "add", ".").returncode == 0
    ok, reasons = run_dlp_check(git_sandbox)
    assert ok is True, f"False-positive DLP block on M4 deliverables: {reasons}"

    commit_res = _git(git_sandbox, "commit", "-m", "Verify all M4 deliverables pass pre-commit DLP hook")
    assert commit_res.returncode == 0, commit_res.stderr.decode("utf-8", errors="replace")


def test_t10_claude_settings_json_permissions_policy() -> None:
    """T10: .claude/settings.json is valid JSON enforcing vault/exports/CLI allows and .env/push/force-add denials."""
    settings_path = REPO_ROOT / ".claude" / "settings.json"
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert data.get("$schema") == "https://json.schemastore.org/claude-code-settings.json"

    perms = data.get("permissions")
    assert isinstance(perms, dict)
    allow = set(perms.get("allow", []))
    deny = set(perms.get("deny", []))

    for required_allow in (
        "Read",
        "Glob",
        "Grep",
        "Edit(vault/**)",
        "Write(vault/**)",
        "Write(exports/**)",
        "Bash(poopdeck *)",
        "Bash(python -m poopdeck *)",
        "Bash(pytest*)",
    ):
        assert required_allow in allow, f"Missing allow rule: {required_allow}"

    for required_deny in (
        "Read(.env)",
        "Read(.env.*)",
        "Edit(.env)",
        "Write(.env)",
        "Bash(git push*)",
        "Bash(git -C vault push*)",
        "Bash(git add -f*)",
        "Bash(git add --force*)",
        "Bash(git add vault*)",
    ):
        assert required_deny in deny, f"Missing deny rule: {required_deny}"

    assert "Write(vault/opportunities/**)" not in deny


def test_t11_claude_specialist_subagents_frontmatter_and_guardrails() -> None:
    """T11: All 5 .claude/agents/*.md files have valid string YAML frontmatter and enforce staging/CFO/HUMAN-NOTES rules."""
    agents_dir = REPO_ROOT / ".claude" / "agents"
    actual_stems = sorted(p.stem for p in agents_dir.glob("*.md"))
    assert actual_stems == sorted(EXPECTED_SUBAGENTS)

    parsed_agents: dict[str, tuple[dict[str, object], str]] = {}
    for stem in EXPECTED_SUBAGENTS:
        fm, body = _parse_frontmatter_markdown(agents_dir / f"{stem}.md")
        assert fm.get("name") == stem
        for key in ("name", "description", "tools"):
            val = fm.get(key)
            assert isinstance(val, str) and val.strip(), f"{stem}.md frontmatter '{key}' must be non-empty str"
        assert HUMAN_NOTES_START in body and HUMAN_NOTES_END in body
        assert "poopdeck validate" in body
        parsed_agents[stem] = (fm, body)

    # skeptical-cfo-critic omits Write so it can only audit/edit staged specs in place
    cfo_tools = [t.strip() for t in str(parsed_agents["skeptical-cfo-critic"][0]["tools"]).split(",")]
    assert "Edit" in cfo_tools
    assert "Write" not in cfo_tools

    arch_body = parsed_agents["ds-opportunity-architect"][1]
    assert "vault/staging/opportunities/" in arch_body
    assert "status: \"staging\"" in arch_body or "status: staging" in arch_body
    assert "vault/opportunities/" in arch_body
    assert "driver_formula" in arch_body
    assert "[VERIFY WITH STAKEHOLDER]" in arch_body
    assert "external_data_catalog.yaml" in arch_body

    cfo_body = parsed_agents["skeptical-cfo-critic"][1]
    for required_cfo_term in (
        "[VERIFY WITH STAKEHOLDER]",
        "## Skeptical CFO Critique",
        "SIMPLIFY_TO_SQL_DAX",
        "NEEDS_VERIFICATION",
        "READY_FOR_REVIEW",
        "leakage",
    ):
        assert required_cfo_term in cfo_body, f"Missing '{required_cfo_term}' in skeptical-cfo-critic.md"


def test_t12_claude_slash_commands_frontmatter_and_workflows() -> None:
    """T12: All 7 .claude/commands/*.md files parse to dict[str, str] frontmatter and wire $ARGUMENTS & subagents."""
    commands_dir = REPO_ROOT / ".claude" / "commands"
    actual_stems = sorted(p.stem for p in commands_dir.glob("*.md"))
    assert actual_stems == sorted(EXPECTED_COMMANDS)

    bodies: dict[str, str] = {}
    for stem in EXPECTED_COMMANDS:
        fm, body = _parse_frontmatter_markdown(commands_dir / f"{stem}.md")
        for key in ("description", "argument-hint", "allowed-tools"):
            val = fm.get(key)
            assert isinstance(val, str) and val.strip(), (
                f"{stem}.md frontmatter '{key}' must be quoted non-empty str, got {type(val)}: {val!r}"
            )
        assert "$ARGUMENTS" in body, f"{stem}.md must handle $ARGUMENTS"
        bodies[stem] = body

    assert "ds-opportunity-architect" in bodies["discover-opportunities"]
    assert "skeptical-cfo-critic" in bodies["discover-opportunities"]
    assert bodies["discover-opportunities"].index("ds-opportunity-architect") < bodies[
        "discover-opportunities"
    ].index("skeptical-cfo-critic")
    assert "poopdeck validate" in bodies["discover-opportunities"]

    assert "notebooks/databricks_zero_admin_crawler.py" in bodies["sync-workspace"]
    assert "queries/powerbi_dax_info_extract.dax" in bodies["sync-workspace"]
    assert "catalog-cartographer" in bodies["sync-workspace"]
    assert "bi-bridge-analyst" in bodies["sync-workspace"]

    assert "context-harvester" in bodies["ingest-notes"]
    assert "promote_opportunity" in bodies["review-staging"]
    assert "archive_opportunity" in bodies["review-staging"]
    assert "boss_mandate" in bodies["boss-override"]
    assert "poopdeck brief" in bodies["exec-brief"]


def test_t13_claude_md_readme_and_harness_contract_consistency_zero_typos() -> None:
    """T13: CLAUDE.md, README.md, and .claude/** cite canonical CLI/paths and contain zero stale survey typos."""
    claude_md = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    readme_md = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    for doc_name, text in (("CLAUDE.md", claude_md), ("README.md", readme_md)):
        for subcmd in ("init", "sync", "validate", "demo", "reset", "ui", "brief"):
            assert f"poopdeck {subcmd}" in text, f"{doc_name} missing 'poopdeck {subcmd}'"
        for required in (
            "notebooks/databricks_zero_admin_crawler.py",
            "queries/powerbi_dax_info_extract.dax",
            "scripts/install_dlp_hook.py",
            "scripts/day1_init.ps1",
            "scripts/day1_init.sh",
            "poopdeck init --company",
            "vault/questions/open-questions.md",
            "vault/questions/day1-90-playbook.md",
            "vault/staging/opportunities/",
            "poopdeck/packs/external_data_catalog.yaml",
            HUMAN_NOTES_START,
            HUMAN_NOTES_END,
            "[VERIFY WITH STAKEHOLDER]",
            "safe_aggregates",
            "package.json",
        ):
            assert required in text, f"{doc_name} missing required reference '{required}'"

    banned_typos = (
        "databricks_discovery_bundle.py",
        "day-1-90-playbook.md",
        "poopdeck sync --source",
        "poopdeck demo --overwrite",
        "poopdeck reset --company",
    )
    harness_files = [
        REPO_ROOT / "CLAUDE.md",
        REPO_ROOT / "README.md",
        REPO_ROOT / ".claude" / "settings.json",
        *sorted((REPO_ROOT / ".claude" / "agents").glob("*.md")),
        *sorted((REPO_ROOT / ".claude" / "commands").glob("*.md")),
    ]
    assert len(harness_files) == 15
    for file_path in harness_files:
        content = file_path.read_text(encoding="utf-8")
        for typo in banned_typos:
            assert typo not in content, f"Prohibited stale reference '{typo}' found in {file_path}"
        assert redact_text(content) == content, (
            f"Unredacted token or non-canonical placeholder in {file_path}"
        )

    hook_src = (REPO_ROOT / "scripts" / "install_dlp_hook.py").read_text(encoding="utf-8")
    assert redact_text(hook_src) == hook_src
    for demo_file in sorted(p for p in DEMO_VAULT_DIR.rglob("*") if p.is_file()):
        demo_txt = demo_file.read_text(encoding="utf-8")
        assert redact_text(demo_txt) == demo_txt, f"redact_text mutated {demo_file}"


# ---------------------------------------------------------------------------
# T14 - T16: Synthetic Demo Vault Strict Validation, ROI Math & Round-Trips
# ---------------------------------------------------------------------------


def test_t14_demo_vault_strict_validation_and_entity_inventory() -> None:
    """T14: examples/demo_vault passes validate_vault(..., strict=True) with 0 errors/warnings and full entity coverage."""
    assert not (DEMO_VAULT_DIR / ".git").exists()
    assert not (DEMO_VAULT_DIR / "exports").exists()

    report = validate_vault(DEMO_VAULT_DIR, strict=True)
    assert report.valid is True, report.errors
    assert report.errors == []
    assert report.warnings == []

    assert report.counts["datasets"] == 6
    assert report.counts["reports"] == 3
    assert report.counts["stakeholders"] == 4
    assert report.counts["questions"] == 15
    assert report.counts["playbook"] == 5
    assert report.counts["glossary"] == 1
    assert report.counts["staging_opportunities"] == 2
    assert report.counts["opportunities"] == 4

    # Verify Bronze/Silver/Gold + legacy hive_metastore + safe_aggregates PII redaction
    layers: set[str] = set()
    catalogs: set[str] = set()
    for ds_path in sorted((DEMO_VAULT_DIR / "datasets").glob("*.md")):
        parsed = parse_markdown_card(ds_path)
        card = DatasetCard.model_validate(parsed.frontmatter)
        assert card.sample_rows == []
        layers.add(card.medallion_layer)
        catalogs.add(card.catalog)
        pii_cols = [c for c in card.columns if c.is_pii]
        assert len(pii_cols) >= 1
        for pcol in pii_cols:
            assert pcol.min_val == "[REDACTED:PII]"
            assert pcol.max_val == "[REDACTED:PII]"
    assert {"bronze", "silver", "gold"}.issubset(layers)
    assert "hive_metastore" in catalogs

    # Verify all 4 canonical DAX heuristic flags and high_excel_export across reports
    seen_flags: set[str] = set()
    excel_flags: list[bool] = []
    for rep_path in sorted((DEMO_VAULT_DIR / "reports").glob("*.md")):
        rcard = ReportCard.model_validate(parse_markdown_card(rep_path).frontmatter)
        excel_flags.append(rcard.high_excel_export)
        for m in rcard.measures:
            seen_flags.update(m.flagged_heuristics)
    assert {
        "nested_heuristic_tiers",
        "naive_moving_avg_forecast",
        "static_threshold_alert",
        "backward_looking_only",
    }.issubset(seen_flags)
    assert any(excel_flags)

    # Verify all 4 influence tiers across stakeholders
    tiers = {
        StakeholderCard.model_validate(parse_markdown_card(p).frontmatter).influence_tier
        for p in (DEMO_VAULT_DIR / "stakeholders").glob("*.md")
    }
    assert tiers == {"exec", "manager", "partner", "sme"}

    # Verify all 5 question categories and 5 playbook phases
    qbank, playbook = load_questions_and_playbook(DEMO_VAULT_DIR)
    assert {q.category for q in qbank.questions} == {
        "manager",
        "it_security",
        "data_engineering",
        "power_bi",
        "business_finance",
    }
    assert [p.id for p in playbook.phases] == [f"PHASE-{i}" for i in range(1, 6)]

    # Verify staged opportunities carry canonical CFO verdicts & ## Skeptical CFO Critique
    staged = load_all_opportunities(DEMO_VAULT_DIR, include_staging=True)
    staged_only = {opp.id: (opp, parsed) for _, opp, parsed in staged if opp.status == "staging"}
    assert set(staged_only) == {"OPP-005", "OPP-006"}
    assert staged_only["OPP-005"][0].cfo_critique_verdict == "NEEDS_VERIFICATION"
    assert staged_only["OPP-006"][0].cfo_critique_verdict == "SIMPLIFY_TO_SQL_DAX"
    for opp_id, (_, parsed_card) in staged_only.items():
        assert "## Skeptical CFO Critique" in parsed_card.body, opp_id

    # Verify sample inbox dropzone note and 100% clean redaction idempotence across all 20 files
    inbox_files = list((DEMO_VAULT_DIR / "inbox").glob("*.md"))
    assert len(inbox_files) == 1
    for md_file in DEMO_VAULT_DIR.rglob("*.md"):
        content = md_file.read_text(encoding="utf-8")
        assert redact_text(content) == content, f"Unredacted PII/secret in {md_file}"


def test_t15_demo_vault_roi_formulas_external_packs_and_boss_mandate_displacement() -> None:
    """T15: Demo vault OpportunitySpecs match safe-AST math, catalog packs, and prove Boss-Mandate displacement."""
    catalog = load_external_data_catalog()
    valid_domains = set(catalog["domain_lenses"])
    valid_ext_sources = {item["source"] for item in catalog["external_datasets"]}

    all_opps = [opp for _, opp, _ in load_all_opportunities(DEMO_VAULT_DIR, include_staging=True)]
    assert len(all_opps) == 6

    for opp in all_opps:
        assert opp.domain_pack in valid_domains
        for ext in opp.external_data_required:
            assert ext.source in valid_ext_sources

        low, base, high = evaluate_driver_formula(
            opp.valuation.driver_formula,
            opp.valuation.drivers,
        )
        assert opp.valuation.annual_benefit_low_usd == pytest.approx(low)
        assert opp.valuation.annual_benefit_base_usd == pytest.approx(base)
        assert opp.valuation.annual_benefit_high_usd == pytest.approx(high)

        unverified = [d for d in opp.valuation.drivers if not d.verified]
        if opp.valuation.confidence_tier == "finance_verified":
            assert unverified == []
            assert opp.valuation.verification_prompts == []
        else:
            assert len(unverified) >= 1
            assert len(opp.valuation.verification_prompts) == len(unverified)
            assert all(
                p.startswith("[VERIFY WITH STAKEHOLDER]")
                for p in opp.valuation.verification_prompts
            )

    live_opps = [opp for _, opp, _ in load_all_opportunities(DEMO_VAULT_DIR, include_staging=False)]
    schedule = compute_portfolio_schedule(live_opps, quarterly_capacity_weeks=12.0)

    assert schedule.mandate_weeks == pytest.approx(5.0)
    assert schedule.used_capacity_weeks == pytest.approx(11.5)
    assert schedule.funded_ids == ["OPP-002", "OPP-001", "OPP-004"]
    assert schedule.displaced_ids == ["OPP-003"]

    by_id = {item.id: item for item in schedule.items}
    assert by_id["OPP-002"].boss_mandate is True
    assert by_id["OPP-002"].above_cutline is True
    assert by_id["OPP-003"].baseline_above_cutline is True
    assert by_id["OPP-003"].above_cutline is False
    assert by_id["OPP-003"].displaced_by_mandate is True


def test_t16_demo_vault_tmp_copy_lineage_briefs_and_mutations_preserve_human_notes(
    tmp_path: Path,
) -> None:
    """T16: Lineage graph, briefs, promote/archive/boss-pin round-trips work on tmp_path copy without touching repo."""
    before_manifest = _sha256_manifest(DEMO_VAULT_DIR)
    before_has_vault = (REPO_ROOT / "vault").exists()
    before_has_git = (REPO_ROOT / ".git").exists()

    sandbox_vault = tmp_path / "vault"
    shutil.copytree(DEMO_VAULT_DIR, sandbox_vault)

    # 1. Lineage graph covers all 6 node types with zero unresolved placeholder nodes
    graph = build_lineage_graph(sandbox_vault, include_staging=True)
    node_types = {n["type"] for n in graph["nodes"]}
    assert node_types == {
        "dataset",
        "existing_report",
        "stakeholder",
        "opportunity",
        "external_data",
        "new_report",
    }
    assert [n for n in graph["nodes"] if not n["exists_in_vault"]] == []
    assert len(graph["edges"]) >= 50

    # 2. All 3 brief generators produce rich Markdown
    opp_teams = generate_opportunity_brief(sandbox_vault, "OPP-004", fmt="teams")
    opp_exec = generate_opportunity_brief(sandbox_vault, "OPP-004", fmt="exec")
    port_brief = generate_portfolio_brief(sandbox_vault, capacity_weeks=12.0)
    verify_brief = generate_verify_questions_brief(sandbox_vault)

    assert "$331,500" in opp_teams.markdown or "331,500" in opp_teams.markdown
    assert "[VERIFY WITH STAKEHOLDER]" in opp_exec.markdown
    assert "Daily Buyer Workbench" in opp_exec.markdown
    assert "OPP-003" in port_brief.markdown
    assert "[VERIFY WITH STAKEHOLDER]" in verify_brief.markdown

    # 3. Inject custom multi-line HUMAN-NOTES and verify byte-for-byte preservation across mutations
    opp5_staging = sandbox_vault / "staging" / "opportunities" / "OPP-005-safety-stock-working-capital-simulator.md"
    opp6_staging = sandbox_vault / "staging" / "opportunities" / "OPP-006-b2b-credit-delinquency-early-warning.md"
    opp4_live = sandbox_vault / "opportunities" / "OPP-004-dynamic-stockout-reorder-alerting.md"

    custom_notes_5 = "\nCustom CFO meeting note for OPP-005 — keep verbatim!\n  - Bullet A\n"
    custom_notes_6 = "\nCustom archive note for OPP-006 — keep verbatim!\n"
    custom_notes_4 = "\nCustom VP note on OPP-004 — preserve byte-for-byte!\n"

    for card_path, custom_note in (
        (opp5_staging, custom_notes_5),
        (opp6_staging, custom_notes_6),
        (opp4_live, custom_notes_4),
    ):
        raw = card_path.read_text(encoding="utf-8")
        replaced = re.sub(
            rf"{re.escape(HUMAN_NOTES_START)}.*?{re.escape(HUMAN_NOTES_END)}",
            f"{HUMAN_NOTES_START}{custom_note}{HUMAN_NOTES_END}",
            raw,
            count=1,
            flags=re.DOTALL,
        )
        _write_lf(card_path, replaced)

    promoted_path = promote_opportunity(sandbox_vault, "OPP-005", target_status="backlog")
    assert not opp5_staging.exists()
    assert promoted_path == sandbox_vault / "opportunities" / opp5_staging.name
    parsed_5 = parse_markdown_card(promoted_path)
    assert parsed_5.frontmatter["status"] == "backlog"
    assert parsed_5.human_notes == custom_notes_5
    assert "## Skeptical CFO Critique" in parsed_5.body

    archived_path = archive_opportunity(
        sandbox_vault,
        "OPP-006",
        reason="Deferred in favor of 20-line SQL/DAX payment velocity baseline.",
    )
    assert not opp6_staging.exists()
    assert archived_path == sandbox_vault / "opportunities" / opp6_staging.name
    parsed_6 = parse_markdown_card(archived_path)
    assert parsed_6.frontmatter["status"] == "archived"
    assert parsed_6.human_notes == custom_notes_6

    pinned_path, pinned_opp = set_boss_mandate(sandbox_vault, "OPP-004", boss_mandate=True)
    assert pinned_opp.boss_mandate is True
    assert parse_markdown_card(pinned_path).human_notes == custom_notes_4

    post_report = validate_vault(sandbox_vault, strict=True)
    assert post_report.valid is True, post_report.errors
    assert post_report.errors == []
    assert post_report.warnings == []

    # 4. Original examples/demo_vault/** and workspace state on disk are 100% untouched
    assert _sha256_manifest(DEMO_VAULT_DIR) == before_manifest
    assert (REPO_ROOT / "vault").exists() == before_has_vault
    assert (REPO_ROOT / ".git").exists() == before_has_git
    assert dlp_mod.DLP_BANNER == "[POOPDECK-DLP-BLOCKED]"


# ---------------------------------------------------------------------------
# T17 - T20: Iteration-2 Hardening Regressions (Worktrees, ReDoS, Encodings, URLs)
# ---------------------------------------------------------------------------


def test_t17_dlp_real_git_worktree_add_blocks_dirty_and_supports_partial_and_commit_a(
    tmp_path: Path,
) -> None:
    """T17: install_hook(linked_wt) on a real `git worktree add` checkout enforces pre-commit DLP on all commit modes."""
    main_repo = _init_git_sandbox(tmp_path / "wt_main")
    linked_wt = tmp_path / "wt_linked"
    try:
        assert (
            _git(
                main_repo,
                "remote",
                "add",
                "origin",
                "https://github.com/mckenzm4/project-poopdeck.git",
            ).returncode
            == 0
        )
        _write_lf(main_repo / "README.md", "# Main repo seed\n")
        assert _git(main_repo, "add", "README.md").returncode == 0
        assert _git(main_repo, "commit", "-m", "Initial seed commit").returncode == 0

        wt_res = _git(main_repo, "worktree", "add", "-b", "feat/linked-wt", str(linked_wt))
        assert wt_res.returncode == 0, wt_res.stderr.decode("utf-8", errors="replace")
        assert (linked_wt / ".git").is_file()

        hook_path = install_hook(linked_wt)
        assert hook_path.is_file()
        hooks_dir_res = _git(linked_wt, "rev-parse", "--git-path", "hooks")
        assert hooks_dir_res.returncode == 0
        expected_hooks_dir = Path(hooks_dir_res.stdout.decode("utf-8").strip())
        if not expected_hooks_dir.is_absolute():
            expected_hooks_dir = (linked_wt / expected_hooks_dir).resolve()
        assert hook_path.resolve() == (expected_hooks_dir / "pre-commit").resolve()

        # 1. Dirty .env staged inside linked worktree MUST be blocked on git commit
        _write_lf(linked_wt / ".env", "DATABRICKS_TOKEN=leak\n")
        assert _git(linked_wt, "add", "-f", ".env").returncode == 0
        ok_dirty, dirty_reasons = run_dlp_check(linked_wt)
        assert ok_dirty is False
        assert any(".env" in r for r in dirty_reasons)

        blocked_commit = _git(linked_wt, "commit", "-m", "Should block .env in linked worktree")
        assert blocked_commit.returncode != 0
        assert DLP_BANNER in blocked_commit.stderr.decode("utf-8", errors="replace")

        # 2. Clean commit inside linked worktree succeeds after unstaging .env
        assert _git(linked_wt, "reset", "HEAD", "--", ".env").returncode == 0
        (linked_wt / ".env").unlink()
        _write_lf(linked_wt / "clean_wt.md", "# Clean linked worktree doc\n")
        assert _git(linked_wt, "add", "clean_wt.md").returncode == 0
        clean_commit = _git(linked_wt, "commit", "-m", "Allow clean commit in linked worktree")
        assert clean_commit.returncode == 0, clean_commit.stderr.decode("utf-8", errors="replace")

        # 3. Partial `git commit <clean_file>` succeeds while .env is staged in linked worktree index,
        #    and normal `git commit` of the remaining staged .env is still blocked
        _write_lf(linked_wt / "partial_clean.md", "# Clean partial file\n")
        _write_lf(linked_wt / ".env", "STILL_FORBIDDEN=1\n")
        assert _git(linked_wt, "add", "partial_clean.md").returncode == 0
        assert _git(linked_wt, "add", "-f", ".env").returncode == 0
        partial_ok = _git(linked_wt, "commit", "partial_clean.md", "-m", "Partial clean commit in worktree")
        assert partial_ok.returncode == 0, partial_ok.stderr.decode("utf-8", errors="replace")

        rem_blocked = _git(linked_wt, "commit", "-m", "Remaining .env must still block")
        assert rem_blocked.returncode != 0
        assert DLP_BANNER in rem_blocked.stderr.decode("utf-8", errors="replace")
        assert _git(linked_wt, "reset", "HEAD", "--", ".env").returncode == 0
        (linked_wt / ".env").unlink()

        # 4. `git commit -a` inside linked worktree blocks unstaged secret added to tracked file
        pat_tok = "dapi" + ("0123456789abcdef" * 2)
        _write_lf(linked_wt / "clean_wt.md", f"LEAK = '{pat_tok}'\n")
        commit_a_blocked = _git(linked_wt, "commit", "-a", "-m", "Should block commit -a in worktree")
        assert commit_a_blocked.returncode != 0
        assert DLP_BANNER in commit_a_blocked.stderr.decode("utf-8", errors="replace")
    finally:
        safe_rmtree(linked_wt)
        safe_rmtree(main_repo)


def test_t18_dlp_redos_sub_200ms_scaling_across_adversarial_near_miss_blobs(
    tmp_path: Path,
) -> None:
    """T18: All 85k-200k adversarial SQL/JDBC/TCP near-miss and multi-statement blobs scan in < 200 ms."""
    repo = _init_git_sandbox(tmp_path / "redos_sandbox", copy_hook_script=False)
    try:
        assert (
            _git(
                repo,
                "remote",
                "add",
                "origin",
                "https://github.com/mckenzm4/project-poopdeck.git",
            ).returncode
            == 0
        )

        ins_kw = "INSERT " + "INTO "
        jdbc_seg = "jdbc:databricks://adb-12345.6.azuredatabricks.net:443/default;AuthMech=3;SSL=1; "
        tcp_seg = "Server=tcp:synapse-prod.sql.azuresynapse.net,1433;Database=dw;Encrypt=true; "

        probes: tuple[tuple[str, str, bool], ...] = (
            ("insert_90k_ident_no_values", ins_kw + ("a" * 90_000), True),
            ("insert_5k_ident_80k_parens", ins_kw + ("t" * 5_000) + " VALUES (" + ("1)" * 40_000), True),
            ("insert_200_single_row_stmts_105k", (ins_kw + "tbl VALUES (" + ("x" * 500) + ") ") * 200, False),
            ("jdbc_repeated_singleline_160k", jdbc_seg * 2_000, True),
            ("server_tcp_repeated_singleline_190k", tcp_seg * 2_500, True),
        )

        probe_file = repo / "docs" / "stress_probe.sql"
        for label, payload, expected_ok in probes:
            _write_lf(probe_file, payload)
            assert _git(repo, "add", "docs/stress_probe.sql").returncode == 0

            t0 = time.perf_counter()
            ok, reasons = run_dlp_check(repo)
            elapsed = time.perf_counter() - t0
            if elapsed >= 0.200:
                # Re-measure once to filter transient Windows CreateProcessW jitter vs deterministic ReDoS
                t1 = time.perf_counter()
                ok, reasons = run_dlp_check(repo)
                elapsed = min(elapsed, time.perf_counter() - t1)

            assert ok is expected_ok, f"{label}: expected ok={expected_ok}, got ok={ok} ({reasons})"
            assert elapsed < 0.200, f"ReDoS budget exceeded on {label} (len={len(payload)}): {elapsed * 1000:.1f} ms"
    finally:
        safe_rmtree(repo)


def test_t19_dlp_multiline_sql_quoted_creds_pem_and_utf16_bom_regressions(
    git_sandbox: Path,
) -> None:
    """T19: Multiline SQL dumps, quoted credentials, encrypted/JSON PEMs, and UTF-16 BOM blobs block with 0 false positives."""
    install_hook(git_sandbox)
    assert (
        _git(
            git_sandbox,
            "remote",
            "add",
            "origin",
            "https://github.com/mckenzm4/project-poopdeck.git",
        ).returncode
        == 0
    )

    ins_kw = "INSERT " + "INTO "

    # Phase 1: Benign controls (semicolon-less inline INSERT + later Python tuples, quoted placeholders,
    #          and a 1-row UTF-16LE SQL statement) MUST pass run_dlp_check and git commit cleanly.
    benign_doc = (
        "# Audit Log Notes\n\n"
        f"Single-row example: `{ins_kw}audit_log VALUES (1, 'init')`\n\n"
        + ("Prose paragraph explaining medallion lineage and safe_aggregates.\n" * 10)
        + "\n```python\npairs = [(10, 20), (30, 40)]\n```\n"
    )
    quoted_placeholders = "\n".join(
        [
            'AccountKey="[REDACTED:SECRET]"',
            'AccountKey="<REDACTED>"',
            'jdbc:databricks://adb.azuredatabricks.net:443/default;PWD="[REDACTED:SECRET]"',
            'jdbc:databricks://adb.azuredatabricks.net:443/default;PWD="<REDACTED>"',
            'jdbc:databricks://adb.azuredatabricks.net:443/default;PWD="..."',
            "Server=tcp:sql.database.windows.net,1433;Password='[REDACTED:SECRET]'",
            "Server=tcp:sql.database.windows.net,1433;Password='<REDACTED>'",
            "Server=tcp:sql.database.windows.net,1433;Password='...'",
            "",
        ]
    )
    _write_lf(git_sandbox / "docs" / "crawler_notes.md", benign_doc)
    _write_lf(git_sandbox / "docs" / "placeholders.md", quoted_placeholders)

    single_utf16_path = git_sandbox / "queries" / "single_row_utf16le.sql"
    single_utf16_path.parent.mkdir(parents=True, exist_ok=True)
    single_utf16_path.write_bytes(
        b"\xff\xfe" + f"{ins_kw}schema_version (ver) VALUES ('001');\n".encode("utf-16-le")
    )

    assert _git(
        git_sandbox,
        "add",
        "docs/crawler_notes.md",
        "docs/placeholders.md",
        "queries/single_row_utf16le.sql",
    ).returncode == 0
    ok_clean, clean_reasons = run_dlp_check(git_sandbox)
    assert ok_clean is True, f"False positive on benign controls: {clean_reasons}"
    clean_commit = _git(git_sandbox, "commit", "-m", "Allow benign SQL docs and quoted placeholders")
    assert clean_commit.returncode == 0, clean_commit.stderr.decode("utf-8", errors="replace")

    # Phase 2: True-positive detections across multiline SQL, quoted creds, encrypted/JSON PEM, UTF-16LE/BE & binary+ASCII
    pem_begin_rsa = "-----BEGIN " + "RSA PRIVATE KEY-----"
    pem_end_rsa = "-----END " + "RSA PRIVATE KEY-----"
    pem_begin_pk8 = "-----BEGIN " + "PRIVATE KEY-----"
    pem_end_pk8 = "-----END " + "PRIVATE KEY-----"
    pat_tok = "dapi" + ("0123456789abcdef" * 2)
    ghp_tok = "ghp_" + ("A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5"[:28])
    acct_val = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef0123456789+/=="

    _write_lf(
        git_sandbox / "dumps" / "multiline_multirow.sql",
        f"{ins_kw}tbl (a, b)\nVALUES\n  (1, 'x'),\n  (2, 'y');\n",
    )
    _write_lf(
        git_sandbox / "dumps" / "multiline_multistmt.sql",
        f"{ins_kw}tbl (a, b)\nVALUES\n  (1, 'x');\n{ins_kw}tbl (a, b)\nVALUES\n  (2, 'y');\n",
    )
    _write_lf(git_sandbox / "cfg" / "acct_dq.conf", f'AccountKey="{acct_val}"\n')
    _write_lf(git_sandbox / "cfg" / "acct_sq.conf", f"AccountKey='{acct_val}'\n")
    _write_lf(
        git_sandbox / "cfg" / "jdbc_dq.conf",
        'jdbc:databricks://adb-99.2.azuredatabricks.net:443/default;PWD="RealCorpPass!99"\n',
    )
    _write_lf(
        git_sandbox / "cfg" / "sql_sq.conf",
        "Server=tcp:prod-sql.database.windows.net,1433;Password='RealSqlPass!99'\n",
    )
    _write_lf(
        git_sandbox / "keys" / "rfc1421_enc.pem",
        f"{pem_begin_rsa}\nProc-Type: 4,ENCRYPTED\n"
        "DEK-Info: AES-256-CBC,0123456789ABCDEF0123456789ABCDEF\n\n"
        f"MIIE6TAbBgkqhkiG9w0BBQMwDgQAAABBBBCCCCDDDD==\n{pem_end_rsa}\n",
    )
    _write_lf(
        git_sandbox / "keys" / "gcp_sa.json",
        '{"type": "service_account", "private_key": "'
        + pem_begin_pk8
        + "\\nMIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQ\\n"
        + pem_end_pk8
        + '\\n"}\n',
    )

    utf16le_path = git_sandbox / "dumps" / "ssms_utf16le.sql"
    utf16le_path.write_bytes(
        b"\xff\xfe"
        + f"-- token={pat_tok}\r\n{ins_kw}orders VALUES (1, 'a'), (2, 'b');\r\n".encode("utf-16-le")
    )
    utf16be_path = git_sandbox / "dumps" / "export_utf16be.sql"
    utf16be_path.write_bytes(
        b"\xfe\xff"
        + f"-- token={ghp_tok}\n{ins_kw}orders VALUES (1, 'a'), (2, 'b');\n".encode("utf-16-be")
    )
    hybrid_bin = git_sandbox / "bin" / "hybrid_bom_ascii.bin"
    hybrid_bin.parent.mkdir(parents=True, exist_ok=True)
    hybrid_bin.write_bytes(b"\xff\xfe\x80\x00TOK=" + pat_tok.encode("ascii") + b"\n")

    assert _git(git_sandbox, "add", "dumps", "cfg", "keys", "bin").returncode == 0
    ok_dirty, reasons = run_dlp_check(git_sandbox)
    assert ok_dirty is False

    expected_hits: tuple[tuple[str, str], ...] = (
        ("dumps/multiline_multirow.sql", "INSERT INTO ... VALUES"),
        ("dumps/multiline_multistmt.sql", "INSERT INTO ... VALUES"),
        ("cfg/acct_dq.conf", "Azure Storage AccountKey"),
        ("cfg/acct_sq.conf", "Azure Storage AccountKey"),
        ("cfg/jdbc_dq.conf", "Databricks JDBC password"),
        ("cfg/sql_sq.conf", "SQL Server TCP connection password"),
        ("keys/rfc1421_enc.pem", "PEM private key block"),
        ("keys/gcp_sa.json", "PEM private key block"),
        ("dumps/ssms_utf16le.sql", "Databricks PAT token"),
        ("dumps/ssms_utf16le.sql", "INSERT INTO ... VALUES"),
        ("dumps/export_utf16be.sql", "GitHub personal access token"),
        ("dumps/export_utf16be.sql", "INSERT INTO ... VALUES"),
        ("bin/hybrid_bom_ascii.bin", "Databricks PAT token"),
    )
    for rel_path, expected_label in expected_hits:
        assert any(rel_path in r and expected_label in r for r in reasons), (
            f"Missing ({expected_label!r} in {rel_path!r}) among reasons: {reasons}"
        )

    blocked_res = _git(git_sandbox, "commit", "-m", "Must reject all Iteration-2 leak vectors")
    assert blocked_res.returncode != 0
    assert DLP_BANNER in blocked_res.stderr.decode("utf-8", errors="replace")


def test_t20_dlp_remote_url_spoofing_scp_ssh_and_escaped_regex_override(
    tmp_path: Path,
) -> None:
    """T20: Remote classifier rejects query/path/host spoofing while allowing SCP SSH and escaped regex org overrides."""
    repo = _init_git_sandbox(tmp_path / "url_sandbox", copy_hook_script=False)
    try:
        _write_lf(repo / "vault" / "datasets" / "corp_gold_sales.md", "# Corp vault card\n")
        assert _git(repo, "add", "vault/datasets/corp_gold_sales.md").returncode == 0
        assert _git(repo, "remote", "add", "origin", "https://github.com/mckenzm4/project-poopdeck.git").returncode == 0

        cfg_corp = PoopdeckConfig(
            dlp=DLPConfig(corporate_remote_patterns=["dev.azure.com/corp", "github.com/acme-corp/"])
        )
        url_matrix: tuple[tuple[str, bool], ...] = (
            ("https://github.com/attacker/exfil.git?ref=dev.azure.com/corp", False),
            ("https://github.com/evil/github.com/acme-corp.git", False),
            ("https://evil.io/github.com/acme-corp.git", False),
            ("https://dev.azure.com@evil.io/corp/vault.git", False),
            ("https://github.com/acme-corp-evil/vault.git", False),
            ("file:///C:/Users/alice/dev.azure.com/corp/vault.git", False),
            ("https://dev.azure.com/corp/analytics/_git/vault", True),
            ("git@ssh.dev.azure.com:v3/corp/analytics/vault", True),
            ("git@github.com:acme-corp/vault.git", True),
        )
        for remote_url, expected_corp in url_matrix:
            assert dlp_mod._is_corporate_remote(remote_url, cfg_corp) is expected_corp, remote_url
            assert _git(repo, "remote", "set-url", "origin", remote_url).returncode == 0
            ok, reasons = run_dlp_check(repo, config=cfg_corp)
            assert ok is expected_corp, f"{remote_url}: expected ok={expected_corp}, got {ok} ({reasons})"

        cfg_escaped_re = PoopdeckConfig(
            dlp=DLPConfig(corporate_remote_patterns=[r"^https://github\.com/acme-corp/"])
        )
        assert dlp_mod._is_corporate_remote("https://github.com/acme-corp/vault.git", cfg_escaped_re) is True
        assert dlp_mod._is_corporate_remote("https://github.com/mckenzm4/project-poopdeck.git", cfg_escaped_re) is False

        assert _git(repo, "remote", "set-url", "origin", "https://github.com/acme-corp/vault.git").returncode == 0
        ok_re_corp, re_reasons = run_dlp_check(repo, config=cfg_escaped_re)
        assert ok_re_corp is True, re_reasons

        assert (
            _git(repo, "remote", "set-url", "origin", "https://github.com/mckenzm4/project-poopdeck.git").returncode
            == 0
        )
        ok_re_ext, ext_reasons = run_dlp_check(repo, config=cfg_escaped_re)
        assert ok_re_ext is False
        assert any("vault/datasets/corp_gold_sales.md" in r for r in ext_reasons)
    finally:
        safe_rmtree(repo)
