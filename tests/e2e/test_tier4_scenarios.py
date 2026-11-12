"""Tier 4 real-world Solo Data Scientist end-to-end user scenario tests (`S01` through `S08`).

Implements all 8 multi-step opaque-box user journeys defined in `TEST_INFRA.md`:
- S01 (F01, F02, F04, F10, F12): Day-0 demo practice to Day-1 company reset & DLP shield.
- S02 (F01, F03, F04, F06, F07, F08, F12): Week-1 zero-admin Tier 2 multi-source discovery & redaction.
- S03 (F01, F03, F04, F06, F07, F08): Iterative re-sync preserving `HUMAN-NOTES` & scrubbed samples.
- S04 (F04, F05, F09, F10, F11): Staging Skeptical CFO red-team gate, promote/archive & UI sync.
- S05 (F04, F05, F10, F11, F12): Urgent boss-mandate pinning, capacity displacement & tradeoff brief.
- S06 (F04, F05, F06, F07, F10, F11): End-to-end 6-type lineage graph with external data & BI impact.
- S07 (F04, F05, F09, F10, F11, F12): Day 1-90 onboarding questionnaire & stakeholder verification roundtrip.
- S08 (F01, F02, F09, F11, F12): Cross-platform Day-1 bootstrap scripts, offline SPA & R5 audit.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

import pytest


def _git(
    repo: Path,
    env: dict[str, str],
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a Git subprocess inside `repo` with `GIT_*`-stripped `env`."""
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


def _dir_sha256_manifest(directory: Path) -> dict[str, str]:
    """Return sorted relative POSIX path -> SHA-256 digest mapping for `directory`."""
    return {
        p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def _inject_human_notes(card_path: Path, custom_notes: str) -> str:
    """Replace content inside `HUMAN-NOTES` markers and return exact parsed `human_notes`."""
    from poopdeck.vault_io import HUMAN_NOTES_END, HUMAN_NOTES_START, parse_markdown_card

    raw = card_path.read_text(encoding="utf-8")
    pattern = re.compile(
        re.escape(HUMAN_NOTES_START) + r".*?" + re.escape(HUMAN_NOTES_END),
        flags=re.DOTALL,
    )
    replacement = f"{HUMAN_NOTES_START}\n{custom_notes}\n{HUMAN_NOTES_END}"
    updated, count = pattern.subn(lambda _m: replacement, raw, count=1)
    assert count == 1, f"Missing balanced HUMAN-NOTES markers in {card_path}"
    card_path.write_bytes(updated.encode("utf-8"))
    parsed = parse_markdown_card(card_path)
    assert parsed.human_notes is not None
    return parsed.human_notes


def _resolve_posix_sh() -> Path:
    """Locate a genuine POSIX `sh` executable (Git for Windows MSYS2 `sh.exe` on Windows)."""
    if os.name != "nt":
        which_sh = shutil.which("sh")
        assert which_sh is not None, "POSIX `sh` not found on PATH"
        return Path(which_sh)

    candidates: list[Path] = []
    git_exe = shutil.which("git")
    if git_exe:
        git_root = Path(git_exe).resolve().parent.parent
        candidates.extend(
            [
                git_root / "bin" / "sh.exe",
                git_root / "usr" / "bin" / "sh.exe",
            ]
        )
    candidates.extend(
        [
            Path(r"C:\Program Files\Git\bin\sh.exe"),
            Path(r"C:\Program Files\Git\usr\bin\sh.exe"),
        ]
    )
    for cand in candidates:
        if cand.is_file():
            return cand
    raise AssertionError(f"Git for Windows POSIX sh.exe not found; checked: {candidates}")


def _stage_day1_sandbox(
    project_root: Path,
    repo_dir: Path,
    clean_git_env: dict[str, str],
) -> dict[str, str]:
    """Stage an isolated Git sandbox containing engine package, Day-1 scripts, and config templates."""
    import sysconfig

    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(repo_dir, clean_git_env, "init", "-q")
    _git(repo_dir, clean_git_env, "config", "user.name", "Test User")
    _git(repo_dir, clean_git_env, "config", "user.email", "test@example.com")

    for rel_path in (
        ".gitignore",
        "pyproject.toml",
        "config.example.yaml",
        "scripts/install_dlp_hook.py",
        "scripts/day1_init.ps1",
        "scripts/day1_init.sh",
    ):
        src = project_root / rel_path
        assert src.is_file(), f"Missing required engine file for Day-1 bootstrap: {src}"
        dst = repo_dir / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    poopdeck_src = project_root / "poopdeck"
    assert poopdeck_src.is_dir(), f"Missing required engine package directory: {poopdeck_src}"
    shutil.copytree(
        poopdeck_src,
        repo_dir / "poopdeck",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        dirs_exist_ok=True,
    )

    _git(repo_dir, clean_git_env, "add", ".")
    _git(repo_dir, clean_git_env, "commit", "-q", "-m", "Seed clean engine files")

    env = dict(clean_git_env)
    env["POOPDECK_ENGINE_ROOT"] = str(project_root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    py_dir = Path(sys.executable).resolve().parent
    user_scripts = sysconfig.get_path("scripts", sysconfig.get_preferred_scheme("user"))
    candidate_bins = [
        py_dir / "Scripts",
        py_dir / "bin",
        Path(user_scripts) if user_scripts else None,
        py_dir,
    ]
    path_prefix = [str(p) for p in candidate_bins if p is not None and p.is_dir()]
    env["PATH"] = os.pathsep.join([*path_prefix, env.get("PATH", "")])
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo_dir), *(p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p)]
    )
    env["PYTHON"] = Path(sys.executable).resolve().as_posix()
    return env


@pytest.mark.tier4
def test_t4_s01_day0_demo_practice_to_day1_company_reset_and_dlp_shield(
    project_root: Path,
    git_sandbox: Path,
    vault_sandbox: Path,
    clean_git_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """S01: Day-0 demo practice -> Day-1 company reset -> three-layer external-remote DLP shield."""
    from poopdeck.vault_io import load_questions_and_playbook
    from scripts.install_dlp_hook import install_hook

    demo_dir = project_root / "examples" / "demo_vault"
    demo_before = _dir_sha256_manifest(demo_dir)

    # Day 0: seed practice demo vault, validate strictly, and export an executive opportunity pitch
    r_demo = run_cli("demo", "--vault", vault_sandbox, "--force")
    assert r_demo.returncode == 0, f"poopdeck demo failed: {r_demo.stderr}\n{r_demo.stdout}"

    r_val0 = run_cli("validate", "--strict", "--json", "--vault", vault_sandbox)
    assert r_val0.returncode == 0, f"poopdeck validate failed: {r_val0.stderr}\n{r_val0.stdout}"
    val0_payload = json.loads(r_val0.stdout)
    assert val0_payload["valid"] is True and val0_payload["errors"] == []
    assert val0_payload["counts"]["opportunities"] == 4
    assert val0_payload["counts"]["staging_opportunities"] == 2

    r_brief = run_cli("brief", "OPP-004", "--format", "exec", "--vault", vault_sandbox)
    assert r_brief.returncode == 0, f"poopdeck brief OPP-004 failed: {r_brief.stderr}"
    assert "OPP-004" in r_brief.stdout
    assert "$331,500" in r_brief.stdout
    assert "[VERIFY WITH STAKEHOLDER]" in r_brief.stdout

    # Day 1: corporate laptop with external personal origin mckenzm4/project-poopdeck
    for rel in (".gitignore", "config.example.yaml", "scripts/install_dlp_hook.py"):
        src = project_root / rel
        dst = git_sandbox / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    _git(git_sandbox, clean_git_env, "remote", "add", "origin", "https://github.com/mckenzm4/project-poopdeck.git")
    company_vault = git_sandbox / "vault"
    shutil.copytree(vault_sandbox, company_vault, dirs_exist_ok=True)

    r_reset = run_cli("reset", "--yes", "--vault", company_vault)
    assert r_reset.returncode == 0, f"poopdeck reset failed: {r_reset.stderr}"
    r_init = run_cli("init", "--company", "--repo-root", git_sandbox, "--vault", company_vault)
    assert r_init.returncode == 0, f"poopdeck init failed: {r_init.stderr}"
    install_hook(git_sandbox)

    hook_file = git_sandbox / ".git" / "hooks" / "pre-commit"
    assert hook_file.is_file()
    assert b"\r\n" not in hook_file.read_bytes()

    for wiped_sub in ("datasets", "reports", "opportunities", "staging/opportunities"):
        assert list((company_vault / wiped_sub).glob("*.md")) == []
    assert (company_vault / ".git").is_dir()
    assert (company_vault / "glossary.md").is_file()
    q_bank, playbook = load_questions_and_playbook(company_vault)
    assert len(q_bank.questions) == 15
    assert len({q.category for q in q_bank.questions}) == 5
    assert len(playbook.phases) == 5
    assert run_cli("validate", "--vault", company_vault).returncode == 0

    commit_env = {**clean_git_env, "POOPDECK_ENGINE_ROOT": str(project_root)}

    # Block 1: staged vault/ path (ensure >= 1 commit inside company_vault so git stages mode 160000 gitlink)
    if _git(company_vault, clean_git_env, "rev-parse", "--verify", "HEAD", check=False).returncode != 0:
        _git(company_vault, clean_git_env, "config", "user.name", "Test User")
        _git(company_vault, clean_git_env, "config", "user.email", "test@example.com")
        _git(company_vault, clean_git_env, "add", ".")
        _git(company_vault, clean_git_env, "commit", "-q", "-m", "Seed local company vault")
    _git(git_sandbox, clean_git_env, "add", "-f", "vault/questions/open-questions.md", check=False)
    _git(git_sandbox, clean_git_env, "add", "-f", "vault")
    s01_stage_lines = _git(git_sandbox, clean_git_env, "ls-files", "--stage").stdout.splitlines()
    assert any(ln.startswith("160000 ") and ln.endswith("\tvault") for ln in s01_stage_lines), (
        f"Expected mode 160000 gitlink for 'vault', got: {s01_stage_lines!r}"
    )
    c_vault = _git(git_sandbox, commit_env, "commit", "-m", "Leak vault", check=False)
    assert c_vault.returncode != 0
    assert "[POOPDECK-DLP-BLOCKED]" in (c_vault.stderr + c_vault.stdout)
    _git(git_sandbox, clean_git_env, "rm", "--cached", "-r", "-f", "vault", check=False)

    # Block 2: staged .env file
    dotenv_file = git_sandbox / ".env"
    dotenv_file.write_bytes(b"DATABRICKS_HOST=https://adb-9999.azuredatabricks.net\n")
    _git(git_sandbox, clean_git_env, "add", "-f", ".env")
    c_env = _git(git_sandbox, commit_env, "commit", "-m", "Leak dotenv", check=False)
    assert c_env.returncode != 0
    assert "[POOPDECK-DLP-BLOCKED]" in (c_env.stderr + c_env.stdout)
    _git(git_sandbox, clean_git_env, "rm", "--cached", "-f", ".env", check=False)
    dotenv_file.unlink()

    # Block 3: staged engine file containing Databricks PAT
    pat_file = git_sandbox / "scripts" / "leaked_note.py"
    pat_file.write_bytes(b'TOKEN = "dapi0123456789abcdef0123456789abcdef"\n')
    _git(git_sandbox, clean_git_env, "add", "scripts/leaked_note.py")
    c_pat = _git(git_sandbox, commit_env, "commit", "-m", "Leak PAT", check=False)
    assert c_pat.returncode != 0
    assert "[POOPDECK-DLP-BLOCKED]" in (c_pat.stderr + c_pat.stdout)
    _git(git_sandbox, clean_git_env, "rm", "--cached", "-f", "scripts/leaked_note.py", check=False)
    pat_file.unlink()

    # Allow: clean engine files including examples/demo_vault/ synthetic path
    synth_card = git_sandbox / "examples" / "demo_vault" / "datasets" / "synthetic_demo.md"
    synth_card.parent.mkdir(parents=True, exist_ok=True)
    synth_card.write_bytes(b"# Synthetic Demo Card\n")
    _git(
        git_sandbox,
        clean_git_env,
        "add",
        ".gitignore",
        "config.example.yaml",
        "scripts/install_dlp_hook.py",
        "examples/demo_vault/datasets/synthetic_demo.md",
    )
    c_clean = _git(git_sandbox, commit_env, "commit", "-m", "Clean engine + synthetic demo commit", check=False)
    assert c_clean.returncode == 0, f"Clean engine commit blocked:\n{c_clean.stdout}\n{c_clean.stderr}"
    assert _dir_sha256_manifest(demo_dir) == demo_before


@pytest.mark.tier4
def test_t4_s02_week1_zero_admin_tier2_multi_source_discovery_and_redaction(
    fixtures_dir: Path,
    vault_sandbox: Path,
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """S02: Week-1 zero-admin Tier 2 multi-source discovery (JSON/BOM CSV, PBIP/INFO.*, VTT/DOCX/MD) & redaction."""
    from poopdeck.models import DatasetCard, ReportCard, StakeholderCard
    from poopdeck.vault_io import parse_markdown_card

    r_init = run_cli("init", "--company", "--skip-hook", "--vault", vault_sandbox)
    assert r_init.returncode == 0, r_init.stderr

    shutil.copy2(
        fixtures_dir / "databricks" / "discovery_bundle.json",
        vault_sandbox / "inbox" / "databricks" / "discovery_bundle.json",
    )
    shutil.copytree(
        fixtures_dir / "databricks" / "csv_bundle",
        vault_sandbox / "inbox" / "databricks" / "csv_bundle",
    )
    shutil.copytree(
        fixtures_dir / "powerbi" / "sample_pbip",
        vault_sandbox / "inbox" / "powerbi" / "sample_pbip",
    )
    shutil.copytree(
        fixtures_dir / "powerbi" / "dax_info_exports",
        vault_sandbox / "inbox" / "powerbi" / "dax_info_exports",
    )
    shutil.copy2(
        fixtures_dir / "teams" / "ops_sync_meeting.vtt",
        vault_sandbox / "inbox" / "teams" / "ops_sync_meeting.vtt",
    )
    shutil.copy2(
        fixtures_dir / "teams" / "finance_review.docx",
        vault_sandbox / "inbox" / "teams" / "finance_review.docx",
    )
    shutil.copy2(
        fixtures_dir / "teams" / "quick_1on1_notes.md",
        vault_sandbox / "inbox" / "notes" / "quick_1on1_notes.md",
    )

    r_sync = run_cli("sync", "all", "--mode", "tier2", "--vault", vault_sandbox)
    assert r_sync.returncode == 0, f"poopdeck sync all failed: {r_sync.stderr}\n{r_sync.stdout}"
    r_val = run_cli("validate", "--vault", vault_sandbox)
    assert r_val.returncode == 0, f"poopdeck validate failed: {r_val.stderr}\n{r_val.stdout}"

    ds_paths = sorted((vault_sandbox / "datasets").glob("*.md"))
    rep_paths = sorted((vault_sandbox / "reports").glob("*.md"))
    stk_paths = sorted((vault_sandbox / "stakeholders").glob("*.md"))
    assert len(ds_paths) >= 5
    assert len(rep_paths) >= 2
    assert len(stk_paths) >= 3

    for dp in ds_paths:
        ds = DatasetCard.model_validate(parse_markdown_card(dp).frontmatter)
        assert ds.sample_rows == []
        assert "\ufeff" not in ds.fqn
        for col in ds.columns:
            assert "\ufeff" not in col.name
            if col.is_pii and col.min_val is not None:
                assert col.min_val == "[REDACTED:PII]"
            if col.is_pii and col.max_val is not None:
                assert col.max_val == "[REDACTED:PII]"

    rep_cards = [ReportCard.model_validate(parse_markdown_card(rp).frontmatter) for rp in rep_paths]
    flagged_classes = {h for rc in rep_cards for m in rc.measures for h in m.flagged_heuristics}
    assert {
        "nested_heuristic_tiers",
        "naive_moving_avg_forecast",
        "static_threshold_alert",
        "backward_looking_only",
    }.issubset(flagged_classes)

    stk_cards = [StakeholderCard.model_validate(parse_markdown_card(sp).frontmatter) for sp in stk_paths]
    assert any(len(sc.boss_mandate_asks) >= 1 for sc in stk_cards)

    processed_names = {
        p.name for p in (vault_sandbox / "inbox" / "processed").rglob("*") if p.is_file()
    }
    assert {"ops_sync_meeting.vtt", "finance_review.docx", "quick_1on1_notes.md"}.issubset(processed_names)

    forbidden_tokens = (
        "dapi0123456789abcdef0123456789abcdef",
        "SuperSecretPass123!",
        "ghp_0123456789abcdefghijklmnopqrstuvwxyzABCD",
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
        "sarah.chen@corp-example.com",
        "elena.rostova@corp-example.com",
        "david.park@corp-example.com",
        "billing-ops@corp-example.com",
        "ar@corp-example.com",
        "123-45-6789",
        "987-65-4321",
        "(555) 867-5309",
        "555-234-5678",
        "(555) 301-9988",
        "Zoe Patel",
    )
    vault_cards = [
        *ds_paths,
        *rep_paths,
        *stk_paths,
        vault_sandbox / "glossary.md",
        vault_sandbox / "questions" / "open-questions.md",
    ]
    for card_file in vault_cards:
        text = card_file.read_text(encoding="utf-8")
        for secret_or_pii in forbidden_tokens:
            assert secret_or_pii not in text, f"Raw PII/secret {secret_or_pii!r} leaked in {card_file}"


@pytest.mark.tier4
def test_t4_s03_iterative_resync_preserves_human_notes_and_opted_in_scrubbed_samples(
    fixtures_dir: Path,
    vault_sandbox: Path,
    tmp_path: Path,
) -> None:
    """S03: Week-2 iterative re-sync preserves `HUMAN-NOTES` verbatim & caps scrubbed `sample_rows <= 3`."""
    from poopdeck.config import load_config
    from poopdeck.crawlers import sync_all
    from poopdeck.models import DatasetCard, ReportCard
    from poopdeck.vault_io import initialize_vault, parse_markdown_card, validate_vault

    initialize_vault(vault_sandbox, init_git=False)

    bundle_path = vault_sandbox / "inbox" / "databricks" / "discovery_bundle.json"
    shutil.copy2(fixtures_dir / "databricks" / "discovery_bundle.json", bundle_path)
    pbip_dir = vault_sandbox / "inbox" / "powerbi" / "sample_pbip"
    shutil.copytree(fixtures_dir / "powerbi" / "sample_pbip", pbip_dir)
    shutil.copy2(
        fixtures_dir / "teams" / "ops_sync_meeting.vtt",
        vault_sandbox / "inbox" / "teams" / "ops_sync_meeting.vtt",
    )

    cfg_w1 = tmp_path / "config_week1.yaml"
    cfg_w1.write_bytes(
        b"privacy:\n"
        b"  privacy_mode: safe_aggregates\n"
        b"  allow_sample_rows: false\n"
    )
    sync_all(vault_sandbox, load_config(cfg_w1), target="all", mode="tier2")

    snap_card_path = vault_sandbox / "datasets" / "prod_catalog.silver.inventory_snapshots.md"
    inv_card_path = vault_sandbox / "datasets" / "prod_catalog.gold.customer_invoices.md"
    assert DatasetCard.model_validate(parse_markdown_card(inv_card_path).frontmatter).sample_rows == []

    # Inject custom Week-2 1:1 HUMAN-NOTES across dataset, report, and stakeholder cards
    recorded_notes: dict[Path, str] = {}
    recorded_notes[snap_card_path] = _inject_human_notes(
        snap_card_path,
        "### Week-2 Snapshot Audit\n- Script path: `C:\\Users\\ds\\notes\\dedupe_check.sql`\n"
        "- Verified grain with [[Sarah Chen]]\n| Check | Result |\n| --- | --- |\n| Intraday dupes | 0.4% |",
    )
    recorded_notes[inv_card_path] = _inject_human_notes(
        inv_card_path,
        "### Finance AR Notes\n- Local workbook: `C:\\Users\\ds\\notes\\ar_recon.xlsx`\n"
        "- Linked to [[Elena Rostova]] margin baseline.",
    )
    for rep_file in sorted((vault_sandbox / "reports").glob("*.md")):
        recorded_notes[rep_file] = _inject_human_notes(
            rep_file,
            f"### BI Review ({rep_file.stem})\n- Export audit at `C:\\Users\\ds\\pbi\\{rep_file.stem}`",
        )
    sarah_cards = [
        p for p in sorted((vault_sandbox / "stakeholders").glob("*.md")) if "sarah" in p.stem.lower()
    ]
    assert len(sarah_cards) >= 1
    for stk_file in sarah_cards:
        recorded_notes[stk_file] = _inject_human_notes(
            stk_file,
            "### 1:1 Stakeholder Notes\n- Prefers Monday 08:00 Teams digest (`C:\\Users\\ds\\notes`).",
        )

    # Mutate upstream Databricks stats, add a new DAX measure in model.bim, and drop a Week-2 Teams note
    bundle_obj = json.loads(bundle_path.read_text(encoding="utf-8"))
    for tbl in bundle_obj["tables"]:
        if tbl["fqn"] == "prod_catalog.silver.inventory_snapshots":
            tbl["row_count"] = 6420000
            tbl["freshness_lag_hours"] = 0.5
        elif tbl["fqn"] == "prod_catalog.gold.customer_invoices":
            tbl["row_count"] = 141800
            tbl["freshness_lag_hours"] = 1.25
    for prof in bundle_obj.get("profiles", []):
        if prof.get("fqn") == "prod_catalog.silver.inventory_snapshots":
            prof["row_count"] = 6420000
            prof["freshness_lag_hours"] = 0.5
        elif prof.get("fqn") == "prod_catalog.gold.customer_invoices":
            prof["row_count"] = 141800
            prof["freshness_lag_hours"] = 1.25
    bundle_path.write_text(json.dumps(bundle_obj, indent=2), encoding="utf-8")

    bim_path = pbip_dir / "model.bim"
    bim_obj = json.loads(bim_path.read_text(encoding="utf-8"))
    for tbl in bim_obj["model"]["tables"]:
        if tbl["name"] == "Sales":
            tbl.setdefault("measures", []).append(
                {
                    "name": "Week2 Rolling 6M Demand Avg",
                    "expression": (
                        "AVERAGEX(DATESINPERIOD('Sales'[invoice_date], "
                        "MAX('Sales'[invoice_date]), -6, MONTH), [Total Order Qty])"
                    ),
                }
            )
    bim_path.write_text(json.dumps(bim_obj, indent=2), encoding="utf-8")

    followup_md = vault_sandbox / "inbox" / "notes" / "week2_sarah_followup.md"
    followup_md.write_text(
        "# Follow-Up 1:1 — Sarah Chen\n"
        "Role: Inventory Planning Lead\n"
        "Department: Supply Chain\n"
        "- Explicit Ask: Add P90 lead-time buffer column to [[Supply Chain Workspace__Daily Buyer Workbench]]\n"
        "- Contact: sarah.chen@corp-example.com | Token: dapi0123456789abcdef0123456789abcdef\n",
        encoding="utf-8",
    )

    cfg_w2 = tmp_path / "config_week2.yaml"
    cfg_w2.write_bytes(
        b"privacy:\n"
        b"  privacy_mode: scrubbed_samples\n"
        b"  allow_sample_rows: true\n"
        b"  max_sample_rows: 3\n"
    )
    sync_all(vault_sandbox, load_config(cfg_w2), target="all", mode="tier2")

    snap_after = DatasetCard.model_validate(parse_markdown_card(snap_card_path).frontmatter)
    inv_after = DatasetCard.model_validate(parse_markdown_card(inv_card_path).frontmatter)
    assert snap_after.row_count == 6420000
    assert snap_after.freshness_lag_hours == pytest.approx(0.5)
    assert len(snap_after.sample_rows) == 2
    assert inv_after.row_count == 141800
    assert inv_after.freshness_lag_hours == pytest.approx(1.25)
    assert len(inv_after.sample_rows) == 3

    for row in inv_after.sample_rows:
        for pii_field in ("customer_name", "email", "ssn", "phone", "dob", "salary", "address"):
            assert row[pii_field] == "[REDACTED:PII]"
        notes_val = str(row.get("billing_notes", ""))
        assert "dapi0123456789abcdef0123456789abcdef" not in notes_val
        assert "ar@corp-example.com" not in notes_val

    all_measure_names = {
        m.name
        for rp in (vault_sandbox / "reports").glob("*.md")
        for m in ReportCard.model_validate(parse_markdown_card(rp).frontmatter).measures
    }
    assert "Week2 Rolling 6M Demand Avg" in all_measure_names

    for path_key, expected_human_notes in recorded_notes.items():
        actual_notes = parse_markdown_card(path_key).human_notes
        assert actual_notes == expected_human_notes, f"HUMAN-NOTES mutated during re-sync in {path_key}"

    val = validate_vault(vault_sandbox)
    assert val.valid is True, val.errors


@pytest.mark.tier4
def test_t4_s04_staging_cfo_red_team_gate_promote_archive_and_ui_sync(
    project_root: Path,
    vault_sandbox: Path,
) -> None:
    """S04: Skeptical CFO `vault/staging/` gate -> UI inspect, promote `OPP-005`, archive `OPP-006` & validate."""
    from fastapi.testclient import TestClient
    import yaml
    from poopdeck.ui.app import create_app
    from poopdeck.vault_io import parse_markdown_card, validate_vault

    # Verify F09 Claude harness enforces read/edit-only staging gate on skeptical-cfo-critic
    cfo_md = (project_root / ".claude" / "agents" / "skeptical-cfo-critic.md").read_text(encoding="utf-8")
    fm_end = cfo_md.find("\n---\n", 4)
    cfo_fm = yaml.safe_load(cfo_md[4:fm_end])
    cfo_tools = [t.strip() for t in cfo_fm["tools"].split(",")]
    assert "Write" not in cfo_tools and "Edit" in cfo_tools

    shutil.copytree(project_root / "examples" / "demo_vault", vault_sandbox, dirs_exist_ok=True)
    opp5_stg = next((vault_sandbox / "staging" / "opportunities").glob("OPP-005-*.md"))
    opp6_stg = next((vault_sandbox / "staging" / "opportunities").glob("OPP-006-*.md"))
    assert "## Skeptical CFO Critique" in parse_markdown_card(opp5_stg).body
    assert "## Skeptical CFO Critique" in parse_markdown_card(opp6_stg).body

    notes_5 = _inject_human_notes(
        opp5_stg,
        "Reviewed with Supply Chain Finance (`C:\\Reviews\\OPP-005.md`) — promote to Q1 backlog.",
    )
    notes_6 = _inject_human_notes(
        opp6_stg,
        "Agreed with CFO critique (`[[Elena Rostova]]`) — archive in favor of SQL AR aging view.",
    )

    client = TestClient(create_app(vault_sandbox))

    stg_before = client.get("/api/staging")
    assert stg_before.status_code == 200
    stg_items = {item["id"]: item for item in stg_before.json()["items"]}
    assert set(stg_items.keys()) == {"OPP-005", "OPP-006"}
    assert stg_items["OPP-005"]["cfo_critique_verdict"] == "NEEDS_VERIFICATION"
    assert stg_items["OPP-006"]["cfo_critique_verdict"] == "SIMPLIFY_TO_SQL_DAX"
    assert len(stg_items["OPP-005"]["verification_prompts"]) >= 2

    port_before_ids = {item["id"] for item in client.get("/api/portfolio").json()["opportunities"]}
    assert port_before_ids == {"OPP-001", "OPP-002", "OPP-003", "OPP-004"}

    r_prom = client.post("/api/promote", json={"id": "OPP-005"})
    assert r_prom.status_code == 200
    assert r_prom.json()["status"] == "promoted"
    assert r_prom.json()["new_status"] == "backlog"

    r_arch = client.post(
        "/api/archive",
        json={"id": "OPP-006", "reason": "SQL aging report covers 90% of value without D&B fee"},
    )
    assert r_arch.status_code == 200
    assert r_arch.json()["status"] == "archived"
    assert r_arch.json()["new_status"] == "archived"

    assert client.get("/api/staging").json()["count"] == 0
    assert list((vault_sandbox / "staging" / "opportunities").glob("*.md")) == []

    port_active = client.get("/api/portfolio").json()
    active_ids = {item["id"] for item in port_active["opportunities"]}
    assert "OPP-005" in active_ids
    assert "OPP-006" not in active_ids

    port_all = client.get("/api/portfolio?include_archived=true").json()
    by_id_all = {item["id"]: item for item in port_all["opportunities"]}
    assert "OPP-006" in by_id_all
    assert by_id_all["OPP-006"]["status"] == "archived"
    assert by_id_all["OPP-006"]["above_cutline"] is False

    opp5_live = vault_sandbox / "opportunities" / opp5_stg.name
    opp6_live = vault_sandbox / "opportunities" / opp6_stg.name
    p5_after = parse_markdown_card(opp5_live)
    p6_after = parse_markdown_card(opp6_live)
    assert p5_after.frontmatter["status"] == "backlog"
    assert p6_after.frontmatter["status"] == "archived"
    assert p5_after.human_notes == notes_5
    assert p6_after.human_notes == notes_6
    assert "## Skeptical CFO Critique" in p5_after.body
    assert "## Skeptical CFO Critique" in p6_after.body

    val = validate_vault(vault_sandbox, strict=True)
    assert val.valid is True, val.errors


@pytest.mark.tier4
def test_t4_s05_urgent_boss_mandate_pinning_capacity_displacement_and_tradeoff_brief(
    vault_sandbox: Path,
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """S05: Urgent boss-mandate P0 pinning under 12.0w budget -> displacement math & CLI/UI brief parity."""
    from fastapi.testclient import TestClient
    from poopdeck.ui.app import create_app
    from poopdeck.vault_io import parse_markdown_card, set_boss_mandate

    r_demo = run_cli("demo", "--vault", vault_sandbox, "--force")
    assert r_demo.returncode == 0, r_demo.stderr

    # Establish pure-ROI baseline (unpinning OPP-002 first via set_boss_mandate)
    opp2_path, opp2_unpinned = set_boss_mandate(vault_sandbox, "OPP-002", False)
    assert opp2_unpinned.boss_mandate is False
    expected_opp2_notes = _inject_human_notes(
        opp2_path,
        "COO promised Carrier SLA board pack before Q1 renewals (`C:\\Exec\\board_asks.md`).",
    )

    client = TestClient(create_app(vault_sandbox))
    base_port = client.get("/api/portfolio?capacity_weeks=12.0").json()
    assert base_port["funded_ids"] == ["OPP-001", "OPP-004", "OPP-003"]
    assert base_port["displaced_ids"] == []

    # Pin lower-ROI OPP-002 (5.0w, score 20.26) as boss_mandate=True via UI REST API
    r_pin = client.post(
        "/api/boss-pin",
        json={"id": "OPP-002", "boss_mandate": True, "capacity_weeks": 12.0},
    )
    assert r_pin.status_code == 200
    pin_data = r_pin.json()
    assert pin_data["boss_mandate"] is True
    assert pin_data["mandate_weeks"] == pytest.approx(5.0)
    assert pin_data["used_capacity_weeks"] == pytest.approx(11.5)
    assert pin_data["funded_ids"] == ["OPP-002", "OPP-001", "OPP-004"]
    assert pin_data["displaced_ids"] == ["OPP-003"]

    port_after = client.get("/api/portfolio?capacity_weeks=12.0").json()
    items_by_id = {it["id"]: it for it in port_after["opportunities"]}
    assert items_by_id["OPP-002"]["rank"] == 1
    assert items_by_id["OPP-002"]["above_cutline"] is True
    assert items_by_id["OPP-003"]["above_cutline"] is False
    assert items_by_id["OPP-003"]["baseline_above_cutline"] is True
    assert items_by_id["OPP-003"]["displaced_by_mandate"] is True

    reparsed_opp2 = parse_markdown_card(opp2_path)
    assert reparsed_opp2.frontmatter["boss_mandate"] is True
    assert reparsed_opp2.human_notes == expected_opp2_notes

    # Export Opportunity Pitch Brief & Weekly Portfolio Tradeoff Brief (isolated under tmp_path/exports)
    r_opp_brief = client.post(
        "/api/brief",
        json={"opp_id": "OPP-002", "brief_type": "opportunity", "format": "exec", "save_export": True},
    )
    assert r_opp_brief.status_code == 200
    opp_brief_json = r_opp_brief.json()
    assert opp_brief_json["saved_path"] is not None
    saved_opp_file = Path(opp_brief_json["saved_path"])
    assert saved_opp_file.is_file()
    assert saved_opp_file.resolve().parent == (vault_sandbox.resolve().parent / "exports")
    for expected_snippet in ("$48,000", "$90,000", "$150,000", "[VERIFY WITH STAKEHOLDER]"):
        assert expected_snippet in opp_brief_json["markdown"]

    r_wk_brief = client.post(
        "/api/brief",
        json={"brief_type": "weekly_status", "capacity_weeks": 12.0, "save_export": True},
    )
    assert r_wk_brief.status_code == 200
    wk_md = r_wk_brief.json()["markdown"]
    assert "OPP-002" in wk_md and "OPP-003" in wk_md

    r_cli_brief = run_cli("brief", "--portfolio", "--vault", vault_sandbox)
    assert r_cli_brief.returncode == 0, f"poopdeck brief --portfolio failed: {r_cli_brief.stderr}"
    assert "OPP-002" in r_cli_brief.stdout
    assert "OPP-003" in r_cli_brief.stdout


@pytest.mark.tier4
def test_t4_s06_end_to_end_lineage_graph_with_external_data_and_bi_impact_tracing(
    fixtures_dir: Path,
    vault_sandbox: Path,
    tmp_path: Path,
) -> None:
    """S06: 6-type lineage graph (`NOAA`/`FRED` + UC tables + uncrawled ref + PBI bonus & brief)."""
    from fastapi.testclient import TestClient
    from poopdeck.brief import generate_opportunity_brief
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.databricks_ingest import sync_databricks
    from poopdeck.crawlers.powerbi_ingest import sync_powerbi
    from poopdeck.models import (
        DriverSpec,
        ExternalDataReq,
        InternalDatasetUse,
        NewReportSpec,
        OpportunitySpec,
        ReportCard,
        ReportSupplement,
        StakeholderCard,
        ValuationSpec,
        compute_priority_score,
    )
    from poopdeck.packs import load_external_data_catalog
    from poopdeck.ui.app import create_app
    from poopdeck.vault_io import initialize_vault, parse_markdown_card, upsert_markdown_card, validate_vault

    initialize_vault(vault_sandbox, init_git=False)

    dbx_in = tmp_path / "dbx_in"
    dbx_in.mkdir()
    shutil.copy2(fixtures_dir / "databricks" / "discovery_bundle.json", dbx_in / "discovery_bundle.json")
    pbi_in = tmp_path / "pbi_in"
    shutil.copytree(fixtures_dir / "powerbi" / "sample_pbip", pbi_in / "sample_pbip")

    cfg = PoopdeckConfig()
    sync_databricks(vault_sandbox, cfg, input_path=dbx_in, mode="tier2")
    sync_powerbi(vault_sandbox, cfg, input_path=pbi_in, mode="tier2")

    catalog = load_external_data_catalog()
    noaa_entry = next(
        d for d in catalog.external_datasets if "noaa" in f"{d.id} {d.source}".lower()
    )
    fred_entry = next(
        d for d in catalog.external_datasets if "fred" in f"{d.id} {d.source}".lower()
    )

    rep_cards = [
        ReportCard.model_validate(parse_markdown_card(rp).frontmatter)
        for rp in sorted((vault_sandbox / "reports").glob("*.md"))
    ]
    wb_report = next((r for r in rep_cards if "Buyer Workbench" in r.title), rep_cards[0])

    sarah = StakeholderCard(
        id="sarah-chen",
        name="Sarah Chen",
        role="Inventory Planning Lead",
        department="Supply Chain",
        influence_tier="manager",
        decision_appetite="high",
        owned_reports=[f"[[{wb_report.id}]]"],
        updated_at=date(2026, 11, 12),
    )
    upsert_markdown_card(
        vault_sandbox / "stakeholders" / "sarah-chen.md",
        sarah,
        "## Stakeholder Profile\n\nOwns daily buyer reorder execution.",
    )

    shared_fields: dict[str, Any] = {
        "status": "backlog",
        "created_at": date(2026, 11, 12),
        "updated_at": date(2026, 11, 12),
        "internal_datasets": [
            InternalDatasetUse(
                ref="[[prod_catalog.silver.inventory_snapshots]]",
                role="Daily SKU warehouse stock",
                transformations_needed=["Deduplicate intraday snapshots"],
            ),
            InternalDatasetUse(
                ref="[[prod_catalog.gold.po_receipts]]",
                role="Vendor lead-time actuals",
                transformations_needed=["Compute P90 lead-time lag"],
            ),
            InternalDatasetUse(
                ref="[[prod_catalog.gold.planned_feature_store]]",
                role="Planned Gold ML feature table (not yet crawled)",
                transformations_needed=["Materialize lag features"],
            ),
        ],
        "external_data_required": [
            ExternalDataReq(
                source=noaa_entry.source,
                purpose="Regional severe weather transit delay signal",
                acquisition="free_public_api",
                blocker_severity="low",
            ),
            ExternalDataReq(
                source=fred_entry.source,
                purpose="Macro industrial demand index",
                acquisition="free_public_api",
                blocker_severity="low",
            ),
        ],
        "valuation": ValuationSpec(
            confidence_tier="directional",
            driver_formula="skus * annual_margin_protected",
            drivers=[
                DriverSpec(name="skus", range=(300.0, 500.0, 700.0), verified=True),
                DriverSpec(name="annual_margin_protected", range=(400.0, 650.0, 900.0), verified=False),
            ],
        ),
        "effort_weeks": 3.5,
        "data_readiness_score": 4,
        "adoption_friction_score": 2,
        "steps_to_completion": ["Train quantile lead-time model", "Publish Gold predictions"],
        "stakeholders": ["[[Sarah Chen]]"],
    }

    opp_supplement = OpportunitySpec(
        id="OPP-301",
        title="Weather-Adjusted Buyer Workbench Reorder Signal",
        existing_reports_supplemented=[
            ReportSupplement(
                ref=f"[[{wb_report.id}]]",
                integration_mode="Replace static 14-day alert with calibrated stockout hazard",
            )
        ],
        new_reports_unlocked=[
            NewReportSpec(
                title="Executive Weather Buffer Simulator",
                audience="VP Operations",
                why_net_new="Interactive scenario stress-testing",
            )
        ],
        **shared_fields,
    )
    opp_standalone = OpportunitySpec(
        id="OPP-302",
        title="Standalone Weather Simulator Only",
        existing_reports_supplemented=[],
        new_reports_unlocked=[
            NewReportSpec(
                title="Standalone Weather Dashboard",
                audience="Supply Planners",
                why_net_new="Greenfield report without existing PBI embed",
            )
        ],
        **shared_fields,
    )

    score_supp = compute_priority_score(opp_supplement)
    score_stand = compute_priority_score(opp_standalone)
    assert score_supp == pytest.approx(score_stand + 1.70, abs=0.05)

    upsert_markdown_card(
        vault_sandbox / "opportunities" / "OPP-301-weather-workbench.md",
        opp_supplement,
        "## Opportunity Spec\n\nSupplements existing Buyer Workbench.",
    )
    upsert_markdown_card(
        vault_sandbox / "opportunities" / "OPP-302-standalone-weather.md",
        opp_standalone,
        "## Opportunity Spec\n\nStandalone net-new report.",
    )

    client = TestClient(create_app(vault_sandbox))
    r_lin = client.get("/api/lineage")
    assert r_lin.status_code == 200
    lin = r_lin.json()
    nodes_by_id = {n["id"]: n for n in lin["nodes"]}
    node_types = {n["type"] for n in lin["nodes"]}
    assert {
        "external_data",
        "dataset",
        "opportunity",
        "existing_report",
        "new_report",
        "stakeholder",
    }.issubset(node_types)

    assert nodes_by_id["dataset:prod_catalog.silver.inventory_snapshots"]["exists_in_vault"] is True
    assert nodes_by_id["dataset:prod_catalog.gold.planned_feature_store"]["exists_in_vault"] is False
    assert nodes_by_id["dataset:prod_catalog.gold.planned_feature_store"]["exists"] is False

    edge_relations = {e["relation"] for e in lin["edges"]}
    assert {
        "feeds_dataset",
        "feeds_report",
        "owns_report",
        "inputs_to_opportunity",
        "enriches_opportunity",
        "supplements_report",
        "unlocks_new_report",
        "stakeholder_for",
    }.issubset(edge_relations)

    non_strict = validate_vault(vault_sandbox, strict=False)
    assert non_strict.valid is True
    assert any("planned_feature_store" in w for w in non_strict.warnings)

    brief_301 = generate_opportunity_brief(vault_sandbox, "OPP-301", fmt="teams")
    assert wb_report.title in brief_301.markdown
    assert noaa_entry.source in brief_301.markdown


@pytest.mark.tier4
def test_t4_s07_day1_to_90_onboarding_questionnaire_stakeholder_verification_roundtrip(
    vault_sandbox: Path,
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """S07: Day 1-90 readiness API -> Finance verification -> `directional` (0.85x) to `finance_verified` (1.00x)."""
    from fastapi.testclient import TestClient
    from pydantic import ValidationError
    from poopdeck.models import OpportunitySpec
    from poopdeck.ui.app import create_app
    from poopdeck.vault_io import parse_markdown_card, upsert_markdown_card

    r_demo = run_cli("demo", "--vault", vault_sandbox, "--force")
    assert r_demo.returncode == 0, r_demo.stderr

    q_path = vault_sandbox / "questions" / "open-questions.md"
    pb_path = vault_sandbox / "questions" / "day1-90-playbook.md"
    opp4_path = next((vault_sandbox / "opportunities").glob("OPP-004-*.md"))

    q_notes = _inject_human_notes(q_path, "Finance 1:1 scheduled with [[Elena Rostova]] (`C:\\Notes\\fin.md`).")
    pb_notes = _inject_human_notes(pb_path, "Day-30 milestone check with [[Marcus Vance]].")
    opp4_notes = _inject_human_notes(opp4_path, "Pending Finance sign-off on carrying cost & stockout margin.")

    client = TestClient(create_app(vault_sandbox))
    assert client.get("/api/questions").status_code == 200
    assert client.get("/api/readiness").status_code == 200

    r_vbrief = client.post(
        "/api/brief",
        json={"brief_type": "verify_questions", "save_export": True},
    )
    assert r_vbrief.status_code == 200
    vb_json = r_vbrief.json()
    assert "[VERIFY WITH STAKEHOLDER]" in vb_json["markdown"]
    assert "Q-FIN-02" in vb_json["markdown"]
    assert Path(vb_json["saved_path"]).resolve().parent == (vault_sandbox.resolve().parent / "exports")

    r_ans = client.patch(
        "/api/questions/Q-FIN-02",
        json={"status": "answered", "answer": "Finance verified 38% gross margin and 9.5% holding cost."},
    )
    assert r_ans.status_code == 200
    assert r_ans.json()["status"] == "answered"

    r_ph1 = client.post("/api/questions/PHASE-1", json={"completed": True})
    r_ph2 = client.post("/api/questions/PHASE-2", json={"completed": True})
    assert r_ph1.status_code == 200 and r_ph1.json()["completed"] is True
    assert r_ph2.status_code == 200 and r_ph2.json()["completed"] is True

    q_parsed = parse_markdown_card(q_path)
    pb_parsed = parse_markdown_card(pb_path)
    q_fin_02 = next(q for q in q_parsed.frontmatter["questions"] if q["id"] == "Q-FIN-02")
    assert q_fin_02["status"] == "answered"
    assert "38% gross margin" in (q_fin_02["answer_summary"] or "")
    assert "- [x]" in q_parsed.body
    assert q_parsed.human_notes == q_notes

    ph2_fm = next(p for p in pb_parsed.frontmatter["phases"] if p["id"] == "PHASE-2")
    assert ph2_fm["completed"] is True
    assert "- [x]" in pb_parsed.body
    assert pb_parsed.human_notes == pb_notes

    # Upgrade OPP-004 from directional (0.85x) to finance_verified (1.00x) after driver verification
    opp4_parsed = parse_markdown_card(opp4_path)
    opp4_before = OpportunitySpec.model_validate(opp4_parsed.frontmatter)
    assert opp4_before.valuation.confidence_tier == "directional"
    assert len(opp4_before.valuation.verification_prompts) >= 1
    score_before = opp4_before.computed_priority_score
    assert score_before is not None

    # Premature upgrade while unverified drivers remain must fail Pydantic validation
    premature_dict = opp4_before.model_dump(mode="json")
    premature_dict["valuation"]["confidence_tier"] = "finance_verified"
    with pytest.raises((ValidationError, ValueError)):
        OpportunitySpec.model_validate(premature_dict)

    verified_dict = opp4_before.model_dump(mode="json")
    for drv in verified_dict["valuation"]["drivers"]:
        drv["verified"] = True
    verified_dict["valuation"]["confidence_tier"] = "finance_verified"
    verified_dict["computed_priority_score"] = None
    opp4_upgraded = OpportunitySpec.model_validate(verified_dict)
    assert opp4_upgraded.valuation.verification_prompts == []
    assert opp4_upgraded.computed_priority_score == pytest.approx(
        score_before * (1.00 / 0.85), abs=0.05
    )

    upsert_markdown_card(opp4_path, opp4_upgraded, opp4_parsed.body)
    assert parse_markdown_card(opp4_path).human_notes == opp4_notes
    assert run_cli("validate", "--strict", "--vault", vault_sandbox).returncode == 0


@pytest.mark.tier4
def test_t4_s08_cross_platform_day1_bootstrap_scripts_offline_spa_and_r5_audit(
    project_root: Path,
    tmp_path: Path,
    clean_git_env: dict[str, str],
) -> None:
    """S08: `day1_init.ps1` + Git POSIX `day1_init.sh` + zero-CDN offline SPA + full R4/R5 repository audit."""
    from conftest import safe_rmtree
    from fastapi.testclient import TestClient
    from poopdeck.ui.app import create_app
    from poopdeck.vault_io import load_questions_and_playbook, validate_vault

    repo_ps1 = tmp_path / "repo_ps1"
    repo_sh = tmp_path / "repo_sh"
    try:
        env_ps1 = _stage_day1_sandbox(project_root, repo_ps1, clean_git_env)
        ps_exe = shutil.which("powershell") or shutil.which("pwsh")
        assert ps_exe is not None, "PowerShell executable not found"
        res_ps1 = subprocess.run(
            [
                ps_exe,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(repo_ps1 / "scripts" / "day1_init.ps1"),
            ],
            cwd=repo_ps1,
            env=env_ps1,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60.0,
        )
        assert res_ps1.returncode == 0, f"day1_init.ps1 failed:\n{res_ps1.stdout}\n{res_ps1.stderr}"

        env_sh = _stage_day1_sandbox(project_root, repo_sh, clean_git_env)
        sh_exe = _resolve_posix_sh()
        res_sh = subprocess.run(
            [str(sh_exe), "scripts/day1_init.sh"],
            cwd=repo_sh,
            env=env_sh,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60.0,
        )
        assert res_sh.returncode == 0, f"day1_init.sh failed:\n{res_sh.stdout}\n{res_sh.stderr}"

        for sandbox_repo in (repo_ps1, repo_sh):
            assert (sandbox_repo / "config.yaml").is_file()
            vdir = sandbox_repo / "vault"
            assert (vdir / ".git").is_dir()
            assert (vdir / "glossary.md").is_file()
            q_bank, playbook = load_questions_and_playbook(vdir)
            assert len(q_bank.questions) == 15
            assert len(playbook.phases) == 5
            assert validate_vault(vdir).valid is True

            hook_bytes = (sandbox_repo / ".git" / "hooks" / "pre-commit").read_bytes()
            assert hook_bytes.startswith(b"#!/bin/sh\n")
            assert b"\r\n" not in hook_bytes

            status_out = _git(sandbox_repo, clean_git_env, "status", "--porcelain").stdout.strip()
            assert status_out == "", f"Day-1 bootstrap dirtied tracked engine files: {status_out}"

        # Verify offline zero-CDN Local Command Center SPA over bootstrapped vault
        client = TestClient(create_app(repo_ps1 / "vault"))
        r_spa = client.get("/")
        r_css = client.get("/static/styles.css")
        r_js = client.get("/static/app.js")
        assert r_spa.status_code == 200 and r_css.status_code == 200 and r_js.status_code == 200
        assert "default-src 'self'" in r_spa.headers.get("content-security-policy", "")
        for asset_body in (r_spa.text, r_css.text, r_js.text):
            assert "http://" not in asset_body and "https://" not in asset_body
    finally:
        safe_rmtree(repo_ps1)
        safe_rmtree(repo_sh)

    # Full R4 Claude Code 14-file harness & R5 architecture prohibitions audit
    assert (project_root / "CLAUDE.md").is_file()
    settings = json.loads((project_root / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert len(settings["permissions"]["allow"]) >= 5
    assert len(settings["permissions"]["deny"]) >= 10
    assert len(list((project_root / ".claude" / "agents").glob("*.md"))) == 5
    assert len(list((project_root / ".claude" / "commands").glob("*.md"))) == 7

    excluded_roots = {".agents", ".git", ".pytest_cache", "__pycache__", ".venv", "venv"}
    for entry in project_root.rglob("*"):
        if any(part in excluded_roots for part in entry.relative_to(project_root).parts):
            continue
        assert entry.name not in {
            "package.json",
            "package-lock.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "node_modules",
        }, f"R5 Node.js prohibition violated by {entry}"

    pyproject_txt = (project_root / "pyproject.toml").read_text(encoding="utf-8").lower()
    for forbidden_pkg in ("psycopg", "asyncpg", "redis", "sqlalchemy", "botbuilder", "msgraph"):
        assert forbidden_pkg not in pyproject_txt

    forbidden_imports = {"sqlite3", "psycopg", "psycopg2", "asyncpg", "redis", "sqlalchemy", "botbuilder"}
    for py_file in sorted((project_root / "poopdeck").rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden_imports
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden_imports
