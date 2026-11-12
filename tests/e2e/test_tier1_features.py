"""Tier 1 core feature happy-path E2E tests for F09, F10, and F12.

Covers:
- F09 Tier 1 (6 tests): `CLAUDE.md` 5 core operating sections & CLI reference, `.claude/settings.json`
  valid permissions schema (`allow` & `deny`), 5 specialist `.claude/agents/*.md` subagents,
  7 `.claude/commands/*.md` workflow slash commands with `$ARGUMENTS`, mandatory
  `vault/staging/opportunities/` Skeptical CFO red-team gate, and `HUMAN-NOTES` + Tier 2 script refs.
- F10 Tier 1 (6 tests): `examples/demo_vault/**` complete entity inventory & strict zero-error
  `validate_vault(..., strict=True)`, 6-node-type `build_lineage_graph` with zero dangling refs,
  `compute_portfolio_schedule` 12.0-week cutline (`OPP-002` displacing `OPP-003`),
  `generate_opportunity_brief` (`teams` & `exec`), `generate_portfolio_brief`, and
  `generate_verify_questions_brief` (global bundle & per-opportunity).
- F12 Tier 1 (6 tests): `poopdeck --help` + `init --company`, `demo` + `validate --strict --json`,
  `sync {databricks,powerbi,teams}` Tier 2 CLI ingestion with redaction, `reset --yes`,
  `brief` CLI stdout & `--output` file export, and `scripts/day1_init.ps1` + POSIX `sh`
  `scripts/day1_init.sh` + `ui --help` + workspace root `.git` & LF pre-commit DLP state.
"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

import pytest


def _parse_frontmatter_and_body(md_path: Path) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter (`--- ... ---`) and Markdown body from a Claude `.md` file."""
    import yaml

    text = md_path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{md_path} must begin with LF YAML frontmatter delimiter"
    end_idx = text.find("\n---\n", 4)
    assert end_idx != -1, f"{md_path} missing closing YAML frontmatter delimiter"
    fm_raw = text[4:end_idx]
    body = text[end_idx + 5 :]
    parsed = yaml.safe_load(fm_raw)
    assert isinstance(parsed, dict), f"{md_path} frontmatter must parse to a mapping"
    return parsed, body


def _copy_demo_vault(project_root: Path, vault_sandbox: Path) -> Path:
    """Copy `examples/demo_vault` into isolated `vault_sandbox` without touching engine files."""
    demo_dir = project_root / "examples" / "demo_vault"
    assert demo_dir.is_dir(), f"Missing examples/demo_vault at {demo_dir}"
    shutil.copytree(demo_dir, vault_sandbox, dirs_exist_ok=True)
    return vault_sandbox


def _dir_sha256_manifest(directory: Path) -> dict[str, str]:
    """Return sorted relative POSIX path -> SHA-256 digest mapping for `directory`."""
    return {
        p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


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

    subprocess.run(
        ["git", "add", "."],
        cwd=repo_dir,
        env=clean_git_env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "Seed clean engine files"],
        cwd=repo_dir,
        env=clean_git_env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

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


# ============================================================================
# F09 Tier 1 — Claude Code Specialist Subagents, Slash Commands & CLAUDE.md
# ============================================================================


@pytest.mark.tier1
def test_f09_t1_claude_md_exists_and_documents_five_core_operating_sections_and_cli_reference(
    project_root: Path,
) -> None:
    """Verify `CLAUDE.md` contains all 5 operating sections, 7 CLI subcommands, and R5 rules."""
    claude_md = project_root / "CLAUDE.md"
    assert claude_md.is_file(), "Missing CLAUDE.md at repository root"
    text = claude_md.read_text(encoding="utf-8")
    assert len(text.strip()) >= 1500

    headings = [line.strip() for line in text.splitlines() if line.startswith("## ")]
    assert len(headings) >= 5, f"Expected >= 5 core `##` sections in CLAUDE.md, got {headings}"

    for subcmd in (
        "poopdeck init",
        "poopdeck sync",
        "poopdeck validate",
        "poopdeck demo",
        "poopdeck reset",
        "poopdeck ui",
        "poopdeck brief",
    ):
        assert subcmd in text, f"CLAUDE.md missing CLI subcommand reference: {subcmd}"

    for agent_slug in (
        "catalog-cartographer",
        "bi-bridge-analyst",
        "context-harvester",
        "ds-opportunity-architect",
        "skeptical-cfo-critic",
    ):
        assert agent_slug in text, f"CLAUDE.md missing specialist subagent reference: {agent_slug}"

    for slash_cmd in (
        "/onboard-check",
        "/sync-workspace",
        "/ingest-notes",
        "/discover-opportunities",
        "/review-staging",
        "/boss-override",
        "/exec-brief",
    ):
        assert slash_cmd in text, f"CLAUDE.md missing slash command reference: {slash_cmd}"


@pytest.mark.tier1
def test_f09_t1_claude_settings_json_valid_schema_with_scoped_allow_and_deny_lists(
    project_root: Path,
) -> None:
    """Verify `.claude/settings.json` parses cleanly and defines `permissions.allow` & `deny`."""
    settings_path = project_root / ".claude" / "settings.json"
    assert settings_path.is_file(), "Missing .claude/settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert isinstance(settings, dict)
    assert "permissions" in settings and isinstance(settings["permissions"], dict)

    allow = settings["permissions"].get("allow")
    deny = settings["permissions"].get("deny")
    assert isinstance(allow, list) and len(allow) >= 6
    assert isinstance(deny, list) and len(deny) >= 6

    allow_joined = " | ".join(allow)
    assert "Read" in allow
    assert "Glob" in allow
    assert "Grep" in allow
    assert "vault/**" in allow_joined
    assert "exports/**" in allow_joined
    assert "poopdeck" in allow_joined


@pytest.mark.tier1
def test_f09_t1_five_specialist_subagents_exist_with_valid_yaml_frontmatter_and_roles(
    project_root: Path,
) -> None:
    """Verify all 5 `.claude/agents/*.md` files have valid frontmatter (`name`, `description`, `tools`)."""
    agents_dir = project_root / ".claude" / "agents"
    assert agents_dir.is_dir(), "Missing .claude/agents directory"

    expected_agents = {
        "catalog-cartographer": "vault/datasets",
        "bi-bridge-analyst": "vault/reports",
        "context-harvester": "vault/stakeholders",
        "ds-opportunity-architect": "vault/staging/opportunities",
        "skeptical-cfo-critic": "vault/staging/opportunities",
    }
    actual_files = {p.stem: p for p in agents_dir.glob("*.md")}
    assert set(actual_files.keys()) == set(expected_agents.keys())

    for slug, required_target in expected_agents.items():
        fm, body = _parse_frontmatter_and_body(actual_files[slug])
        assert fm.get("name") == slug
        assert isinstance(fm.get("description"), str) and len(fm["description"].strip()) >= 20
        assert isinstance(fm.get("tools"), str) and "Read" in fm["tools"]
        assert required_target in body, f"{slug}.md body must reference {required_target}"
        assert "poopdeck validate" in body, f"{slug}.md body must mandate `poopdeck validate`"


@pytest.mark.tier1
def test_f09_t1_seven_slash_commands_exist_with_valid_frontmatter_and_arguments_placeholder(
    project_root: Path,
) -> None:
    """Verify all 7 `.claude/commands/*.md` files define `description`, `argument-hint`, `allowed-tools`, and `$ARGUMENTS`."""
    commands_dir = project_root / ".claude" / "commands"
    assert commands_dir.is_dir(), "Missing .claude/commands directory"

    expected_commands = {
        "onboard-check",
        "sync-workspace",
        "ingest-notes",
        "discover-opportunities",
        "review-staging",
        "boss-override",
        "exec-brief",
    }
    actual_files = {p.stem: p for p in commands_dir.glob("*.md")}
    assert set(actual_files.keys()) == expected_commands

    for cmd_name, cmd_path in actual_files.items():
        fm, body = _parse_frontmatter_and_body(cmd_path)
        assert isinstance(fm.get("description"), str) and len(fm["description"].strip()) >= 15
        assert isinstance(fm.get("argument-hint"), str) and len(fm["argument-hint"].strip()) >= 3
        assert isinstance(fm.get("allowed-tools"), str) and "poopdeck" in fm["allowed-tools"]
        assert "$ARGUMENTS" in body, f"{cmd_name}.md must reference `$ARGUMENTS`"
        assert "poopdeck " in body


@pytest.mark.tier1
def test_f09_t1_mandatory_staging_cfo_red_team_workflow_across_claude_md_agents_and_commands(
    project_root: Path,
) -> None:
    """Verify candidate opportunity specs must stage in `vault/staging/opportunities/` for CFO critique."""
    claude_text = (project_root / "CLAUDE.md").read_text(encoding="utf-8")
    _, arch_body = _parse_frontmatter_and_body(
        project_root / ".claude" / "agents" / "ds-opportunity-architect.md"
    )
    _, cfo_body = _parse_frontmatter_and_body(
        project_root / ".claude" / "agents" / "skeptical-cfo-critic.md"
    )
    _, disc_body = _parse_frontmatter_and_body(
        project_root / ".claude" / "commands" / "discover-opportunities.md"
    )
    _, rev_body = _parse_frontmatter_and_body(
        project_root / ".claude" / "commands" / "review-staging.md"
    )

    for doc_name, doc_text in (
        ("CLAUDE.md", claude_text),
        ("ds-opportunity-architect.md", arch_body),
        ("skeptical-cfo-critic.md", cfo_body),
        ("discover-opportunities.md", disc_body),
        ("review-staging.md", rev_body),
    ):
        assert "vault/staging/opportunities" in doc_text, (
            f"{doc_name} missing mandatory `vault/staging/opportunities` staging path"
        )

    assert "skeptical-cfo-critic" in arch_body
    assert "skeptical-cfo-critic" in disc_body
    assert "ds-opportunity-architect" in disc_body
    assert "## Skeptical CFO Critique" in cfo_body
    assert "cfo_critique_verdict" in cfo_body
    assert "vault/opportunities" in rev_body


@pytest.mark.tier1
def test_f09_t1_human_notes_preservation_and_tier2_crawler_scripts_referenced_in_harness(
    project_root: Path,
) -> None:
    """Verify `HUMAN-NOTES` preservation instructions and exact Tier 2 script & catalog paths in harness."""
    claude_text = (project_root / "CLAUDE.md").read_text(encoding="utf-8")
    assert "<!-- HUMAN-NOTES-START -->" in claude_text
    assert "<!-- HUMAN-NOTES-END -->" in claude_text

    for agent_name in (
        "catalog-cartographer",
        "bi-bridge-analyst",
        "context-harvester",
        "ds-opportunity-architect",
        "skeptical-cfo-critic",
    ):
        _, body = _parse_frontmatter_and_body(
            project_root / ".claude" / "agents" / f"{agent_name}.md"
        )
        assert "HUMAN-NOTES" in body, f"{agent_name}.md must enforce HUMAN-NOTES preservation"

    _, cart_body = _parse_frontmatter_and_body(
        project_root / ".claude" / "agents" / "catalog-cartographer.md"
    )
    _, bi_body = _parse_frontmatter_and_body(
        project_root / ".claude" / "agents" / "bi-bridge-analyst.md"
    )
    _, arch_body = _parse_frontmatter_and_body(
        project_root / ".claude" / "agents" / "ds-opportunity-architect.md"
    )
    _, sync_cmd_body = _parse_frontmatter_and_body(
        project_root / ".claude" / "commands" / "sync-workspace.md"
    )

    assert "notebooks/databricks_zero_admin_crawler.py" in cart_body
    assert "notebooks/databricks_zero_admin_crawler.py" in sync_cmd_body
    assert "queries/powerbi_dax_info_extract.dax" in bi_body
    assert "queries/powerbi_dax_info_extract.dax" in sync_cmd_body
    assert "poopdeck/packs/external_data_catalog.yaml" in arch_body


# ============================================================================
# F10 Tier 1 — Synthetic Demo Vault (`examples/demo_vault/`) & Brief Generator
# ============================================================================


@pytest.mark.tier1
def test_f10_t1_demo_vault_inventory_entities_and_strict_zero_error_validation(
    project_root: Path,
    vault_sandbox: Path,
) -> None:
    """Verify `examples/demo_vault/**` entity coverage, DAX heuristic flags, and strict validation."""
    from poopdeck.models import DatasetCard, ReportCard
    from poopdeck.vault_io import (
        load_all_opportunities,
        load_questions_and_playbook,
        parse_markdown_card,
        validate_vault,
    )

    _copy_demo_vault(project_root, vault_sandbox)

    report = validate_vault(vault_sandbox, strict=True)
    assert report.valid is True, f"Demo vault failed strict validation: {report.errors}"
    assert report.errors == []
    assert report.warnings == []
    assert report.counts == {
        "datasets": 6,
        "reports": 3,
        "stakeholders": 4,
        "questions": 15,
        "playbook": 5,
        "glossary": 1,
        "staging_opportunities": 2,
        "opportunities": 4,
    }

    live_opps = {
        opp.id: opp
        for _, opp, _ in load_all_opportunities(vault_sandbox, include_staging=False)
    }
    assert set(live_opps.keys()) == {"OPP-001", "OPP-002", "OPP-003", "OPP-004"}

    all_opps = {
        opp.id: opp
        for _, opp, _ in load_all_opportunities(vault_sandbox, include_staging=True)
    }
    assert set(all_opps.keys()) == {
        "OPP-001",
        "OPP-002",
        "OPP-003",
        "OPP-004",
        "OPP-005",
        "OPP-006",
    }

    ds_layers: set[str] = set()
    has_hive = False
    for ds_file in (vault_sandbox / "datasets").glob("*.md"):
        ds = DatasetCard.model_validate(parse_markdown_card(ds_file).frontmatter)
        ds_layers.add(ds.medallion_layer)
        if ds.catalog == "hive_metastore":
            has_hive = True
    assert {"bronze", "silver", "gold"}.issubset(ds_layers)
    assert has_hive is True

    observed_heuristics: set[str] = set()
    for rep_file in (vault_sandbox / "reports").glob("*.md"):
        rep = ReportCard.model_validate(parse_markdown_card(rep_file).frontmatter)
        for m in rep.measures:
            observed_heuristics.update(m.flagged_heuristics)
    assert observed_heuristics == {
        "nested_heuristic_tiers",
        "naive_moving_avg_forecast",
        "static_threshold_alert",
        "backward_looking_only",
    }

    q_bank, playbook = load_questions_and_playbook(vault_sandbox)
    assert len(q_bank.questions) == 15
    assert {q.category for q in q_bank.questions} == {
        "manager",
        "it_security",
        "data_engineering",
        "power_bi",
        "business_finance",
    }
    assert [p.id for p in playbook.phases] == [
        "PHASE-1",
        "PHASE-2",
        "PHASE-3",
        "PHASE-4",
        "PHASE-5",
    ]
    assert (vault_sandbox / "glossary.md").is_file()


@pytest.mark.tier1
def test_f10_t1_demo_vault_lineage_graph_resolves_all_6_node_types_with_zero_dangling_links(
    project_root: Path,
    vault_sandbox: Path,
) -> None:
    """Verify `build_lineage_graph` on demo vault produces all 6 node types and 0 unresolved nodes."""
    from poopdeck.vault_io import build_lineage_graph

    _copy_demo_vault(project_root, vault_sandbox)
    graph = build_lineage_graph(vault_sandbox, include_staging=True)

    nodes = graph["nodes"]
    edges = graph["edges"]
    assert len(nodes) >= 20
    assert len(edges) >= 20

    assert {n["type"] for n in nodes} == {
        "external_data",
        "dataset",
        "opportunity",
        "existing_report",
        "new_report",
        "stakeholder",
    }
    unresolved = [n for n in nodes if not n.get("exists_in_vault", False)]
    assert unresolved == [], f"Unexpected unresolved lineage nodes in demo_vault: {unresolved}"


@pytest.mark.tier1
def test_f10_t1_demo_vault_portfolio_schedule_12w_cutline_pins_opp002_and_displaces_opp003(
    project_root: Path,
    vault_sandbox: Path,
) -> None:
    """Verify `compute_portfolio_schedule` on demo vault pins P0 `OPP-002` and displaces `OPP-003`."""
    from poopdeck.models import compute_portfolio_schedule
    from poopdeck.vault_io import load_all_opportunities

    _copy_demo_vault(project_root, vault_sandbox)
    live_opps = [
        opp
        for _, opp, _ in load_all_opportunities(
            vault_sandbox,
            include_staging=False,
            include_archived=False,
        )
    ]
    schedule = compute_portfolio_schedule(live_opps, quarterly_capacity_weeks=12.0)

    assert schedule.capacity_weeks == pytest.approx(12.0)
    assert schedule.used_capacity_weeks == pytest.approx(11.5)
    assert schedule.mandate_weeks == pytest.approx(5.0)
    assert schedule.remaining_capacity_weeks == pytest.approx(0.5)
    assert schedule.over_capacity is False
    assert schedule.funded_ids == ["OPP-002", "OPP-001", "OPP-004"]
    assert schedule.displaced_ids == ["OPP-003"]

    sched_by_id = {it.opportunity.id: it for it in schedule.items}
    assert sched_by_id["OPP-001"].opportunity.valuation.annual_benefit_base_usd == pytest.approx(
        436800.0
    )
    assert sched_by_id["OPP-002"].opportunity.valuation.annual_benefit_base_usd == pytest.approx(
        90000.0
    )
    assert sched_by_id["OPP-003"].opportunity.valuation.annual_benefit_base_usd == pytest.approx(
        288000.0
    )
    assert sched_by_id["OPP-004"].opportunity.valuation.annual_benefit_base_usd == pytest.approx(
        331500.0
    )
    assert sched_by_id["OPP-003"].displaced_by_mandate is True


@pytest.mark.tier1
def test_f10_t1_generate_opportunity_brief_teams_and_exec_formats_on_demo_vault(
    project_root: Path,
    vault_sandbox: Path,
) -> None:
    """Verify `generate_opportunity_brief` renders distinct `teams` and `exec` briefs with ROI & gaps."""
    from poopdeck.brief import generate_opportunity_brief

    _copy_demo_vault(project_root, vault_sandbox)

    teams_brief = generate_opportunity_brief(vault_sandbox, "OPP-004", fmt="teams")
    exec_brief = generate_opportunity_brief(vault_sandbox, "OPP-004", fmt="exec")

    assert teams_brief.opp_id == "OPP-004"
    assert teams_brief.brief_type == "opportunity"
    assert exec_brief.opp_id == "OPP-004"
    assert exec_brief.brief_type == "opportunity"
    assert teams_brief.markdown != exec_brief.markdown

    assert "## [OPP-004]" in teams_brief.markdown
    assert "# Executive Opportunity Brief" in exec_brief.markdown
    for md in (teams_brief.markdown, exec_brief.markdown):
        assert "$193,375" in md
        assert "$331,500" in md
        assert "$524,875" in md
        assert "[VERIFY WITH STAKEHOLDER]" in md
        assert "Daily Buyer Workbench" in md


@pytest.mark.tier1
def test_f10_t1_generate_portfolio_brief_surfaces_cutline_mandate_displacement_and_staging_queue(
    project_root: Path,
    vault_sandbox: Path,
) -> None:
    """Verify `generate_portfolio_brief` summarizes capacity, funded P0s, displaced items, and staging."""
    from poopdeck.brief import generate_portfolio_brief

    _copy_demo_vault(project_root, vault_sandbox)
    brief = generate_portfolio_brief(vault_sandbox, capacity_weeks=12.0)

    assert brief.opp_id is None
    assert brief.brief_type == "weekly_status"
    assert "11.5" in brief.markdown and "12.0" in brief.markdown
    for expected_id in ("OPP-001", "OPP-002", "OPP-003", "OPP-004", "OPP-005", "OPP-006"):
        assert expected_id in brief.markdown, f"Portfolio brief missing {expected_id}"


@pytest.mark.tier1
def test_f10_t1_generate_verify_questions_brief_global_bundle_and_single_opportunity(
    project_root: Path,
    vault_sandbox: Path,
) -> None:
    """Verify `generate_verify_questions_brief` bundles global `[VERIFY]` + open Qs or scopes to 1 opp."""
    from poopdeck.brief import generate_verify_questions_brief

    _copy_demo_vault(project_root, vault_sandbox)

    global_brief = generate_verify_questions_brief(vault_sandbox)
    assert global_brief.opp_id is None
    assert global_brief.brief_type == "verify_questions"
    assert "[VERIFY WITH STAKEHOLDER]" in global_brief.markdown
    assert "OPP-004" in global_brief.markdown
    assert "OPP-006" in global_brief.markdown
    assert "Q-FIN-01" in global_brief.markdown

    opp4_brief = generate_verify_questions_brief(vault_sandbox, opp_id="OPP-004")
    assert opp4_brief.opp_id == "OPP-004"
    assert opp4_brief.brief_type == "verify_questions"
    assert opp4_brief.markdown.count("[VERIFY WITH STAKEHOLDER]") == 2
    assert "OPP-006" not in opp4_brief.markdown


# ============================================================================
# F12 Tier 1 — Poopdeck CLI (`poopdeck/cli.py`), Day-1 Scripts & Local Git Setup
# ============================================================================


@pytest.mark.tier1
def test_f12_t1_cli_help_and_init_company_bootstraps_vault_git_seed_bank_and_dlp_hook(
    project_root: Path,
    git_sandbox: Path,
    vault_sandbox: Path,
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Verify `poopdeck --help` lists all 7 subcommands and `init --company` bootstraps vault + LF hook."""
    help_res = run_cli("--help")
    assert help_res.returncode == 0, f"poopdeck --help failed: {help_res.stderr}"
    for subcmd in ("init", "sync", "validate", "demo", "reset", "ui", "brief"):
        assert subcmd in help_res.stdout, f"Subcommand {subcmd!r} missing from poopdeck --help"

    for rel in (".gitignore", "config.example.yaml", "scripts/install_dlp_hook.py"):
        src = project_root / rel
        dst = git_sandbox / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    init_res = run_cli(
        "init",
        "--company",
        "--repo-root",
        git_sandbox,
        "--vault",
        vault_sandbox,
    )
    assert init_res.returncode == 0, f"poopdeck init --company failed: {init_res.stderr}\n{init_res.stdout}"
    assert (vault_sandbox / ".git").is_dir()
    assert (vault_sandbox / "questions" / "open-questions.md").is_file()
    assert (vault_sandbox / "questions" / "day1-90-playbook.md").is_file()
    assert (vault_sandbox / "glossary.md").is_file()

    hook_path = git_sandbox / ".git" / "hooks" / "pre-commit"
    assert hook_path.is_file()
    hook_bytes = hook_path.read_bytes()
    assert hook_bytes.startswith(b"#!/bin/sh\n")
    assert b"\r\n" not in hook_bytes


@pytest.mark.tier1
def test_f12_t1_cli_demo_and_validate_strict_json_lifecycle(
    vault_sandbox: Path,
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Verify `poopdeck demo` seeds `vault_sandbox` and `poopdeck validate --strict --json` exits 0."""
    demo_res = run_cli("demo", "--vault", vault_sandbox, "--force")
    assert demo_res.returncode == 0, f"poopdeck demo failed: {demo_res.stderr}\n{demo_res.stdout}"

    val_res = run_cli("validate", "--vault", vault_sandbox, "--strict", "--json")
    assert val_res.returncode == 0, f"poopdeck validate --strict --json failed: {val_res.stderr}\n{val_res.stdout}"

    payload = json.loads(val_res.stdout)
    assert payload["valid"] is True
    assert payload["errors"] == []
    assert payload["counts"]["datasets"] == 6
    assert payload["counts"]["reports"] == 3
    assert payload["counts"]["stakeholders"] == 4
    assert payload["counts"]["staging_opportunities"] == 2
    assert payload["counts"]["opportunities"] == 4


@pytest.mark.tier1
def test_f12_t1_cli_sync_subcommands_ingest_tier2_bundles_with_redaction(
    fixtures_dir: Path,
    vault_sandbox: Path,
    tmp_path: Path,
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Verify `poopdeck sync {databricks,powerbi,teams}` ingests Tier 2 fixtures with full redaction."""
    init_res = run_cli("init", "--company", "--skip-hook", "--vault", vault_sandbox)
    assert init_res.returncode == 0, init_res.stderr

    dbx_in = tmp_path / "dbx_in"
    dbx_in.mkdir()
    shutil.copy2(fixtures_dir / "databricks" / "discovery_bundle.json", dbx_in / "discovery_bundle.json")

    pbi_in = tmp_path / "pbi_in"
    shutil.copytree(fixtures_dir / "powerbi" / "sample_pbip", pbi_in / "sample_pbip")

    teams_in = tmp_path / "teams_in"
    teams_in.mkdir()
    shutil.copy2(fixtures_dir / "teams" / "ops_sync_meeting.vtt", teams_in / "ops_sync_meeting.vtt")

    r_dbx = run_cli("sync", "databricks", "--input", dbx_in, "--mode", "tier2", "--vault", vault_sandbox)
    assert r_dbx.returncode == 0, f"sync databricks failed: {r_dbx.stderr}\n{r_dbx.stdout}"

    r_pbi = run_cli("sync", "powerbi", "--input", pbi_in, "--mode", "tier2", "--vault", vault_sandbox)
    assert r_pbi.returncode == 0, f"sync powerbi failed: {r_pbi.stderr}\n{r_pbi.stdout}"

    r_tms = run_cli("sync", "teams", "--input", teams_in, "--vault", vault_sandbox)
    assert r_tms.returncode == 0, f"sync teams failed: {r_tms.stderr}\n{r_tms.stdout}"

    r_val = run_cli("validate", "--vault", vault_sandbox)
    assert r_val.returncode == 0, f"validate after sync failed: {r_val.stderr}\n{r_val.stdout}"

    ds_cards = list((vault_sandbox / "datasets").glob("*.md"))
    rep_cards = list((vault_sandbox / "reports").glob("*.md"))
    stk_cards = list((vault_sandbox / "stakeholders").glob("*.md"))
    assert len(ds_cards) >= 5
    assert len(rep_cards) >= 1
    assert len(stk_cards) >= 1

    from poopdeck.models import DatasetCard
    from poopdeck.vault_io import parse_markdown_card

    for ds_path in ds_cards:
        ds = DatasetCard.model_validate(parse_markdown_card(ds_path).frontmatter)
        assert ds.sample_rows == []

    for md_file in [
        *ds_cards,
        *rep_cards,
        *stk_cards,
        vault_sandbox / "glossary.md",
        vault_sandbox / "questions" / "open-questions.md",
    ]:
        content = md_file.read_text(encoding="utf-8")
        assert "dapi0123456789abcdef0123456789abcdef" not in content
        assert "sarah.chen@corp-example.com" not in content
        assert "123-45-6789" not in content


@pytest.mark.tier1
def test_f12_t1_cli_reset_wipes_demo_cards_and_reseeds_clean_vault_preserving_engine(
    project_root: Path,
    vault_sandbox: Path,
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Verify `poopdeck reset --yes` wipes synthetic demo cards & reseeds questions without touching engine."""
    demo_dir = project_root / "examples" / "demo_vault"
    demo_before = _dir_sha256_manifest(demo_dir)

    r_demo = run_cli("demo", "--vault", vault_sandbox, "--force")
    assert r_demo.returncode == 0, r_demo.stderr
    assert len(list((vault_sandbox / "opportunities").glob("*.md"))) == 4

    r_reset = run_cli("reset", "--vault", vault_sandbox, "--yes")
    assert r_reset.returncode == 0, f"poopdeck reset failed: {r_reset.stderr}\n{r_reset.stdout}"

    for cleared_sub in ("datasets", "reports", "opportunities", "staging/opportunities"):
        remaining = list((vault_sandbox / cleared_sub).glob("*.md"))
        assert remaining == [], f"Expected {cleared_sub} cleared after reset, found {remaining}"

    assert (vault_sandbox / ".git").is_dir()
    assert (vault_sandbox / "questions" / "open-questions.md").is_file()
    assert (vault_sandbox / "questions" / "day1-90-playbook.md").is_file()
    assert (vault_sandbox / "glossary.md").is_file()
    assert _dir_sha256_manifest(demo_dir) == demo_before


@pytest.mark.tier1
def test_f12_t1_cli_brief_opportunity_and_portfolio_outputs_and_file_export(
    vault_sandbox: Path,
    tmp_path: Path,
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Verify `poopdeck brief OPP-004 --format exec` and `poopdeck brief --portfolio --output`."""
    r_demo = run_cli("demo", "--vault", vault_sandbox, "--force")
    assert r_demo.returncode == 0, r_demo.stderr

    r_opp = run_cli("brief", "OPP-004", "--format", "exec", "--vault", vault_sandbox)
    assert r_opp.returncode == 0, f"poopdeck brief OPP-004 failed: {r_opp.stderr}"
    assert "OPP-004" in r_opp.stdout
    assert "$331,500" in r_opp.stdout
    assert "[VERIFY WITH STAKEHOLDER]" in r_opp.stdout

    out_path = tmp_path / "weekly_portfolio_brief.md"
    r_port = run_cli("brief", "--portfolio", "--output", out_path, "--vault", vault_sandbox)
    assert r_port.returncode == 0, f"poopdeck brief --portfolio failed: {r_port.stderr}"
    assert out_path.is_file()
    exported_md = out_path.read_text(encoding="utf-8")
    assert "OPP-002" in exported_md
    assert "OPP-003" in exported_md


@pytest.mark.tier1
def test_f12_t1_day1_bootstrap_scripts_ps1_and_posix_sh_ui_help_and_root_git_dlp_state(
    project_root: Path,
    tmp_path: Path,
    clean_git_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Verify `day1_init.ps1`, POSIX `day1_init.sh`, `poopdeck ui --help`, and root `.git` + DLP hook."""
    from conftest import safe_rmtree

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

        for repo in (repo_ps1, repo_sh):
            assert (repo / "config.yaml").is_file()
            assert (repo / "vault" / ".git").is_dir()
            assert (repo / "vault" / "questions" / "open-questions.md").is_file()
            assert (repo / "vault" / "questions" / "day1-90-playbook.md").is_file()
            hook_raw = (repo / ".git" / "hooks" / "pre-commit").read_bytes()
            assert hook_raw.startswith(b"#!/bin/sh\n")
            assert b"\r\n" not in hook_raw
    finally:
        safe_rmtree(repo_ps1)
        safe_rmtree(repo_sh)

    r_ui = run_cli("ui", "--help")
    assert r_ui.returncode == 0, r_ui.stderr
    assert "--host" in r_ui.stdout and "--port" in r_ui.stdout and "--no-browser" in r_ui.stdout

    assert (project_root / ".git").is_dir(), "Workspace root `.git` repository not initialized"
    root_hook = project_root / ".git" / "hooks" / "pre-commit"
    assert root_hook.is_file(), "Workspace root `.git/hooks/pre-commit` DLP hook not installed"
    root_hook_bytes = root_hook.read_bytes()
    assert root_hook_bytes.startswith(b"#!/bin/sh\n")
    assert b"\r\n" not in root_hook_bytes

    ls_vault = subprocess.run(
        ["git", "ls-files", "vault/"],
        cwd=project_root,
        env=clean_git_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    assert ls_vault.stdout.strip() == "", f"DLP violation — tracked vault/ files in root Git: {ls_vault.stdout}"
