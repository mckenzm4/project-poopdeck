"""Tier 2 negative, boundary, redaction, and guardrail E2E tests for F09, F10, and F12.

Covers:
- F09 Tier 2 (6 tests): `.claude/settings.json` DLP `deny` rules & zero external MCP/bot entries,
  `skeptical-cfo-critic.md` omitting `Write` tool while `ds-opportunity-architect.md` forbids live
  `vault/opportunities/` writes, compilation/existence of `notebooks/databricks_zero_admin_crawler.py`
  & `queries/powerbi_dax_info_extract.dax`, safe empty/unknown `$ARGUMENTS` handling in slash commands,
  `[VERIFY WITH STAKEHOLDER]` / `finance_verified` / `cfo_critique_verdict` red-team rules, and
  UTF-8 no-BOM + pure LF hygiene across all 14 Claude harness files.
- F10 Tier 2 (6 tests): `KeyError` on unknown `opp_id` in `generate_opportunity_brief` &
  `generate_verify_questions_brief`, staged opportunity (`OPP-005`, `OPP-006`) brief generation
  across `teams`/`exec`, `OPP-001` (0 unverified drivers) vs `OPP-006` (3 unverified drivers)
  verification brief boundaries, `generate_portfolio_brief` under `capacity_weeks=0.0` & `4.0`
  overflow/displacement, demo vault `sample_rows == []` + `[REDACTED:PII]` + `redact_text(raw) == raw`
  invariant, and `finance_verified` rejection on unverified staged specs.
- F12 Tier 2 (6 tests): `poopdeck init --company` idempotency preserving `HUMAN-NOTES` & `vault/.git`
  history, `poopdeck demo` refusing to clobber a populated company vault without `--force`,
  `poopdeck reset --yes` wiping read-only Windows `vault/.git` packfiles cleanly, `poopdeck validate`
  & `--strict` non-zero exits without tracebacks, `poopdeck brief OPP-UNKNOWN` clean non-zero exit,
  and `poopdeck ui --help` `127.0.0.1` loopback default + invalid CLI target/flag rejection.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path
import py_compile
import re
import shutil
import stat
import subprocess
from typing import Any

import pytest


def _parse_frontmatter_and_body(md_path: Path) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter and Markdown body from a `.md` file."""
    import yaml

    text = md_path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{md_path} must start with LF YAML frontmatter"
    end_idx = text.find("\n---\n", 4)
    assert end_idx != -1, f"{md_path} missing closing YAML frontmatter delimiter"
    fm = yaml.safe_load(text[4:end_idx])
    assert isinstance(fm, dict)
    return fm, text[end_idx + 5 :]


def _copy_demo_vault(project_root: Path, vault_sandbox: Path) -> Path:
    """Copy `examples/demo_vault` into isolated `vault_sandbox`."""
    demo_dir = project_root / "examples" / "demo_vault"
    assert demo_dir.is_dir()
    shutil.copytree(demo_dir, vault_sandbox, dirs_exist_ok=True)
    return vault_sandbox


def _inject_human_notes(card_path: Path, custom_notes: str) -> str:
    """Inject `custom_notes` inside `HUMAN-NOTES` markers and return exact extracted notes."""
    from poopdeck.vault_io import HUMAN_NOTES_END, HUMAN_NOTES_START, parse_markdown_card

    raw = card_path.read_text(encoding="utf-8")
    pattern = re.compile(
        re.escape(HUMAN_NOTES_START) + r".*?" + re.escape(HUMAN_NOTES_END),
        flags=re.DOTALL,
    )
    replacement = f"{HUMAN_NOTES_START}\n{custom_notes}\n{HUMAN_NOTES_END}"
    updated = pattern.sub(lambda _m: replacement, raw, count=1)
    card_path.write_bytes(updated.encode("utf-8"))
    parsed = parse_markdown_card(card_path)
    assert parsed.human_notes is not None
    return parsed.human_notes


# ============================================================================
# F09 Tier 2 — Claude Harness Guardrails, Tool Isolation & Encoding Boundaries
# ============================================================================


@pytest.mark.tier2
def test_f09_t2_settings_json_dlp_deny_rules_block_dotenv_git_push_force_add_and_zero_external_mcp(
    project_root: Path,
) -> None:
    """Verify `.claude/settings.json` denies `.env*`, `git push*`, `git add -f*`/`vault*`, and has 0 MCP bots."""
    settings_path = project_root / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))

    for forbidden_top_key in ("mcpServers", "remoteServers", "webhooks", "teamsBot"):
        assert forbidden_top_key not in settings, (
            f"R5 violation — prohibited external integration key {forbidden_top_key!r} in settings.json"
        )

    deny_rules: list[str] = settings["permissions"]["deny"]
    deny_joined = "\n".join(deny_rules)

    for required_deny_pattern in (
        "Read(.env)",
        "Read(.env.*)",
        "Edit(.env)",
        "Write(.env)",
        "git push",
        "git -C vault push",
        "git add -f",
        "git add --force",
        "git add vault",
    ):
        assert required_deny_pattern in deny_joined, (
            f"Missing mandatory DLP deny pattern {required_deny_pattern!r} in .claude/settings.json"
        )


@pytest.mark.tier2
def test_f09_t2_skeptical_cfo_omits_write_tool_and_architect_forbids_direct_live_opportunities_writes(
    project_root: Path,
) -> None:
    """Verify `skeptical-cfo-critic.md` omits `Write` tool while architect forbids `vault/opportunities/` writes."""
    cfo_fm, cfo_body = _parse_frontmatter_and_body(
        project_root / ".claude" / "agents" / "skeptical-cfo-critic.md"
    )
    cfo_tools = {tool.strip() for tool in str(cfo_fm["tools"]).split(",") if tool.strip()}
    assert "Edit" in cfo_tools
    assert "Read" in cfo_tools
    assert "Write" not in cfo_tools, (
        "skeptical-cfo-critic must omit `Write` so it can only edit staged specs in place"
    )
    assert "vault/opportunities/" in cfo_body

    arch_fm, arch_body = _parse_frontmatter_and_body(
        project_root / ".claude" / "agents" / "ds-opportunity-architect.md"
    )
    arch_tools = {tool.strip() for tool in str(arch_fm["tools"]).split(",") if tool.strip()}
    assert "Write" in arch_tools
    assert "vault/staging/opportunities/" in arch_body
    arch_lower = arch_body.lower()
    assert "never" in arch_lower and "vault/opportunities/" in arch_body


@pytest.mark.tier2
def test_f09_t2_all_referenced_scripts_dax_queries_and_packs_exist_and_compile_cleanly(
    project_root: Path,
    tmp_path: Path,
) -> None:
    """Verify zero-admin notebook compiles, DAX script has all 4 `INFO.*` functions, and hook/packs exist."""
    notebook_path = project_root / "notebooks" / "databricks_zero_admin_crawler.py"
    assert notebook_path.is_file(), f"Missing referenced notebook: {notebook_path}"
    nb_source = notebook_path.read_text(encoding="utf-8")
    ast.parse(nb_source, filename=str(notebook_path))
    compiled_pyc = tmp_path / "databricks_zero_admin_crawler.pyc"
    py_compile.compile(str(notebook_path), cfile=str(compiled_pyc), doraise=True)
    assert compiled_pyc.is_file()

    dax_path = project_root / "queries" / "powerbi_dax_info_extract.dax"
    assert dax_path.is_file(), f"Missing referenced DAX query script: {dax_path}"
    dax_text = dax_path.read_text(encoding="utf-8")
    for info_fn in (
        "INFO.TABLES()",
        "INFO.COLUMNS()",
        "INFO.MEASURES()",
        "INFO.RELATIONSHIPS()",
    ):
        assert info_fn in dax_text, f"Missing {info_fn} in {dax_path}"

    assert (project_root / "scripts" / "install_dlp_hook.py").is_file()
    assert (project_root / "poopdeck" / "packs" / "external_data_catalog.yaml").is_file()


@pytest.mark.tier2
def test_f09_t2_slash_commands_define_safe_fallback_behavior_for_empty_or_unknown_arguments(
    project_root: Path,
) -> None:
    """Verify slash commands restrict tool scope and specify safe handling for empty/unknown `$ARGUMENTS`."""
    commands_dir = project_root / ".claude" / "commands"

    onboard_fm, onboard_body = _parse_frontmatter_and_body(commands_dir / "onboard-check.md")
    assert "Write(vault/**)" not in onboard_fm["allowed-tools"]
    assert "poopdeck init" in onboard_body and "poopdeck demo" in onboard_body

    brief_fm, brief_body = _parse_frontmatter_and_body(commands_dir / "exec-brief.md")
    assert "Write(vault/**)" not in brief_fm["allowed-tools"]
    assert "Write(exports/**)" in brief_fm["allowed-tools"]
    assert "$ARGUMENTS" in brief_body

    _, boss_body = _parse_frontmatter_and_body(commands_dir / "boss-override.md")
    _, rev_body = _parse_frontmatter_and_body(commands_dir / "review-staging.md")
    for cmd_name, cmd_body in (
        ("boss-override.md", boss_body),
        ("review-staging.md", rev_body),
        ("exec-brief.md", brief_body),
    ):
        lower = cmd_body.lower()
        assert "empty" in lower or "omitted" in lower or "missing" in lower or "without" in lower, (
            f"{cmd_name} must document safe behavior when `$ARGUMENTS` is empty/omitted"
        )


@pytest.mark.tier2
def test_f09_t2_skeptical_cfo_enforces_verify_prompts_finance_verified_gate_and_verdict_enums(
    project_root: Path,
) -> None:
    """Verify `skeptical-cfo-critic.md` enforces `[VERIFY]`, `finance_verified` gate, and 3 verdict enums."""
    _, cfo_body = _parse_frontmatter_and_body(
        project_root / ".claude" / "agents" / "skeptical-cfo-critic.md"
    )
    claude_text = (project_root / "CLAUDE.md").read_text(encoding="utf-8")

    for verdict in ("READY_FOR_REVIEW", "NEEDS_VERIFICATION", "SIMPLIFY_TO_SQL_DAX"):
        assert verdict in cfo_body, f"Missing verdict enum {verdict} in skeptical-cfo-critic.md"

    assert "[VERIFY WITH STAKEHOLDER]" in cfo_body
    assert "finance_verified" in cfo_body
    assert "driver_formula" in cfo_body
    assert "HUMAN-NOTES" in cfo_body
    assert "[VERIFY WITH STAKEHOLDER]" in claude_text
    assert "finance_verified" in claude_text


@pytest.mark.tier2
def test_f09_t2_all_fourteen_claude_harness_files_enforce_utf8_no_bom_and_pure_lf_line_endings(
    project_root: Path,
) -> None:
    """Verify all 14 F09 Claude Code files are non-empty UTF-8 without BOM and have 0 CRLF bytes."""
    harness_files = [
        project_root / "CLAUDE.md",
        project_root / ".claude" / "settings.json",
        *sorted((project_root / ".claude" / "agents").glob("*.md")),
        *sorted((project_root / ".claude" / "commands").glob("*.md")),
    ]
    assert len(harness_files) == 14, f"Expected 14 Claude harness files, found {len(harness_files)}"

    for path in harness_files:
        assert path.is_file(), f"Missing harness file: {path}"
        raw = path.read_bytes()
        assert len(raw.strip()) > 0, f"Empty harness file: {path}"
        assert not raw.startswith(b"\xef\xbb\xbf"), f"UTF-8 BOM prohibited in {path}"
        assert b"\r\n" not in raw, f"CRLF line endings prohibited in {path}"
        raw.decode("utf-8")


# ============================================================================
# F10 Tier 2 — Demo Vault & Brief Generator Negative / Boundary Cases
# ============================================================================


@pytest.mark.tier2
def test_f10_t2_unknown_opportunity_id_raises_key_error_in_opportunity_and_verify_briefs(
    project_root: Path,
    vault_sandbox: Path,
) -> None:
    """Verify `generate_opportunity_brief` and `generate_verify_questions_brief` raise `KeyError` on unknown ID."""
    from poopdeck.brief import generate_opportunity_brief, generate_verify_questions_brief

    _copy_demo_vault(project_root, vault_sandbox)

    with pytest.raises(KeyError):
        generate_opportunity_brief(vault_sandbox, "OPP-999")

    with pytest.raises(KeyError):
        generate_verify_questions_brief(vault_sandbox, opp_id="OPP-999")


@pytest.mark.tier2
def test_f10_t2_opportunity_brief_renders_staged_specs_across_teams_and_exec_with_cfo_verdict(
    project_root: Path,
    vault_sandbox: Path,
) -> None:
    """Verify `generate_opportunity_brief` works on staged specs (`OPP-005`, `OPP-006`) across formats."""
    from poopdeck.brief import generate_opportunity_brief

    _copy_demo_vault(project_root, vault_sandbox)

    b5_teams = generate_opportunity_brief(vault_sandbox, "OPP-005", fmt="teams")
    b5_exec = generate_opportunity_brief(vault_sandbox, "OPP-005", fmt="exec")
    b6_lower = generate_opportunity_brief(vault_sandbox, "opp-006", fmt="exec")

    assert b5_teams.opp_id == "OPP-005"
    assert b5_exec.opp_id == "OPP-005"
    assert b6_lower.opp_id == "OPP-006"

    assert b5_teams.markdown.count("[VERIFY WITH STAKEHOLDER]") >= 2
    assert b5_exec.markdown.count("[VERIFY WITH STAKEHOLDER]") >= 2
    assert "NEEDS_VERIFICATION" in b5_exec.markdown or "staging" in b5_exec.markdown.lower()

    assert b6_lower.markdown.count("[VERIFY WITH STAKEHOLDER]") >= 3
    assert "SIMPLIFY_TO_SQL_DAX" in b6_lower.markdown or "staging" in b6_lower.markdown.lower()


@pytest.mark.tier2
def test_f10_t2_verify_questions_brief_handles_fully_verified_opp001_vs_unverified_opp006(
    project_root: Path,
    vault_sandbox: Path,
) -> None:
    """Verify `generate_verify_questions_brief` renders clean notice for `OPP-001` vs 3 prompts for `OPP-006`."""
    from poopdeck.brief import generate_verify_questions_brief

    _copy_demo_vault(project_root, vault_sandbox)

    b_verified = generate_verify_questions_brief(vault_sandbox, opp_id="OPP-001")
    assert b_verified.opp_id == "OPP-001"
    assert b_verified.brief_type == "verify_questions"
    assert "[VERIFY WITH STAKEHOLDER]" not in b_verified.markdown
    assert "verified" in b_verified.markdown.lower()

    b_unverified = generate_verify_questions_brief(vault_sandbox, opp_id="OPP-006")
    assert b_unverified.opp_id == "OPP-006"
    assert b_unverified.brief_type == "verify_questions"
    assert b_unverified.markdown.count("[VERIFY WITH STAKEHOLDER]") == 3


@pytest.mark.tier2
def test_f10_t2_portfolio_brief_handles_zero_capacity_mandate_overflow_and_tight_4w_cutline(
    project_root: Path,
    vault_sandbox: Path,
) -> None:
    """Verify `generate_portfolio_brief` handles `capacity_weeks=0.0` and tight `4.0w` displacement."""
    from poopdeck.brief import generate_portfolio_brief

    _copy_demo_vault(project_root, vault_sandbox)

    zero_brief = generate_portfolio_brief(vault_sandbox, capacity_weeks=0.0)
    assert zero_brief.brief_type == "weekly_status"
    assert "OPP-002" in zero_brief.markdown
    assert "OPP-001" in zero_brief.markdown

    tight_brief = generate_portfolio_brief(vault_sandbox, capacity_weeks=4.0)
    assert tight_brief.brief_type == "weekly_status"
    assert "OPP-002" in tight_brief.markdown
    assert "OPP-001" in tight_brief.markdown


@pytest.mark.tier2
def test_f10_t2_demo_vault_enforces_zero_sample_rows_pii_masking_and_clean_redaction_invariant(
    project_root: Path,
    vault_sandbox: Path,
) -> None:
    """Verify `examples/demo_vault/**` has `sample_rows == []`, `[REDACTED:PII]`, and 0 raw PII/secrets."""
    from poopdeck.models import DatasetCard
    from poopdeck.redaction import redact_text
    from poopdeck.vault_io import parse_markdown_card

    _copy_demo_vault(project_root, vault_sandbox)

    pii_cols_seen = 0
    for ds_path in sorted((vault_sandbox / "datasets").glob("*.md")):
        ds = DatasetCard.model_validate(parse_markdown_card(ds_path).frontmatter)
        assert ds.sample_rows == [], f"Demo dataset {ds.fqn} must have sample_rows == []"
        for col in ds.columns:
            if col.is_pii:
                pii_cols_seen += 1
                if col.min_val is not None:
                    assert col.min_val == "[REDACTED:PII]"
                if col.max_val is not None:
                    assert col.max_val == "[REDACTED:PII]"
    assert pii_cols_seen >= 6

    md_files = sorted(vault_sandbox.rglob("*.md"))
    assert len(md_files) == 23
    for md_path in md_files:
        raw_text = md_path.read_text(encoding="utf-8")
        assert redact_text(raw_text) == raw_text, (
            f"Unredacted PII or secret pattern found in demo vault file {md_path}"
        )


@pytest.mark.tier2
def test_f10_t2_staged_demo_specs_forbid_unverified_finance_verified_and_reject_mutated_tier(
    project_root: Path,
    vault_sandbox: Path,
) -> None:
    """Verify staged demo cards do not claim `finance_verified`, and mutating `OPP-005` fails validation."""
    import yaml
    from poopdeck.vault_io import load_all_opportunities, parse_markdown_card, validate_vault

    _copy_demo_vault(project_root, vault_sandbox)

    for _, opp, _ in load_all_opportunities(vault_sandbox, include_staging=True):
        if any(not d.verified for d in opp.valuation.drivers):
            assert opp.valuation.confidence_tier != "finance_verified"

    opp5_path = next((vault_sandbox / "staging" / "opportunities").glob("OPP-005-*.md"))
    parsed = parse_markdown_card(opp5_path)
    bad_fm = dict(parsed.frontmatter)
    bad_fm["valuation"] = dict(bad_fm["valuation"])
    bad_fm["valuation"]["confidence_tier"] = "finance_verified"
    fm_yaml = yaml.safe_dump(bad_fm, sort_keys=False, allow_unicode=True).strip()
    opp5_path.write_bytes(f"---\n{fm_yaml}\n---\n\n{parsed.body}\n".encode("utf-8"))

    report = validate_vault(vault_sandbox)
    assert report.valid is False
    assert any("OPP-005" in err or "finance_verified" in err for err in report.errors)


# ============================================================================
# F12 Tier 2 — CLI Idempotency, Clobber Refusal, Read-Only Pack Wipe & Errors
# ============================================================================


@pytest.mark.tier2
def test_f12_t2_cli_init_company_is_idempotent_preserving_human_notes_and_vault_git_history(
    project_root: Path,
    git_sandbox: Path,
    vault_sandbox: Path,
    clean_git_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Verify repeated `poopdeck init --company` preserves existing `HUMAN-NOTES` and `vault/.git` commits."""
    from poopdeck.vault_io import parse_markdown_card

    for rel in (".gitignore", "config.example.yaml", "scripts/install_dlp_hook.py"):
        src = project_root / rel
        dst = git_sandbox / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    r1 = run_cli("init", "--company", "--repo-root", git_sandbox, "--vault", vault_sandbox)
    assert r1.returncode == 0, r1.stderr

    q_path = vault_sandbox / "questions" / "open-questions.md"
    g_path = vault_sandbox / "glossary.md"
    expected_q_notes = _inject_human_notes(
        q_path,
        "Day-1 manager sync notes at `C:\\corp\\notes\\day1.md` with [[Marcus Vance]].",
    )
    expected_g_notes = _inject_human_notes(
        g_path,
        "Custom company acronym: **OTIF-Net** excludes customer-requested holds.",
    )

    for cmd in (
        ["git", "config", "user.name", "Test User"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "add", "."],
        ["git", "commit", "-q", "-m", "Record Day-1 custom human notes"],
    ):
        subprocess.run(
            cmd,
            cwd=vault_sandbox,
            env=clean_git_env,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    sha_proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=vault_sandbox,
        env=clean_git_env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    recorded_sha = sha_proc.stdout.strip()
    assert len(recorded_sha) >= 7

    r2 = run_cli("init", "--company", "--repo-root", git_sandbox, "--vault", vault_sandbox)
    assert r2.returncode == 0, r2.stderr

    assert parse_markdown_card(q_path).human_notes == expected_q_notes
    assert parse_markdown_card(g_path).human_notes == expected_g_notes

    log_proc = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=vault_sandbox,
        env=clean_git_env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert recorded_sha[:7] in log_proc.stdout


@pytest.mark.tier2
def test_f12_t2_cli_demo_without_force_refuses_to_clobber_populated_company_vault(
    vault_sandbox: Path,
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Verify `poopdeck demo` without `--force` refuses to overwrite a populated company vault."""
    from datetime import date
    from poopdeck.models import StakeholderCard
    from poopdeck.vault_io import parse_markdown_card, upsert_markdown_card

    r_init = run_cli("init", "--company", "--skip-hook", "--vault", vault_sandbox)
    assert r_init.returncode == 0, r_init.stderr

    vp_path = vault_sandbox / "stakeholders" / "real-vp.md"
    vp_card = StakeholderCard(
        id="real-vp",
        name="Real Company VP",
        role="VP Supply Chain",
        department="Operations",
        influence_tier="exec",
        decision_appetite="high",
        updated_at=date(2026, 11, 12),
    )
    upsert_markdown_card(vp_path, vp_card, "## Summary\n\nProprietary stakeholder profile.")
    expected_vp_notes = _inject_human_notes(vp_path, "Confidential 1:1 notes — NEVER clobber.")

    r_refuse = run_cli("demo", "--vault", vault_sandbox)
    assert r_refuse.returncode != 0, "Expected `poopdeck demo` without `--force` to refuse clobbering"
    assert vp_path.is_file()
    assert parse_markdown_card(vp_path).human_notes == expected_vp_notes
    assert list((vault_sandbox / "opportunities").glob("OPP-001-*.md")) == []

    r_force = run_cli("demo", "--vault", vault_sandbox, "--force")
    assert r_force.returncode == 0, r_force.stderr
    assert len(list((vault_sandbox / "opportunities").glob("OPP-001-*.md"))) == 1


@pytest.mark.tier2
def test_f12_t2_cli_reset_yes_cleanly_wipes_readonly_windows_git_pack_files_and_synthetic_cards(
    project_root: Path,
    vault_sandbox: Path,
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Verify `poopdeck reset --yes` removes read-only Windows `vault/.git/objects/pack/*.pack` files."""
    demo_dir = project_root / "examples" / "demo_vault"
    demo_hash_before = {
        p.relative_to(demo_dir).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(demo_dir.rglob("*"))
        if p.is_file()
    }

    r_demo = run_cli("demo", "--vault", vault_sandbox, "--force")
    assert r_demo.returncode == 0, r_demo.stderr

    pack_dir = vault_sandbox / ".git" / "objects" / "pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    ro_pack = pack_dir / "pack-0123456789abcdef.pack"
    ro_pack.write_bytes(b"PACK\x00\x00\x00\x02\x00\x00\x00\x00")
    os.chmod(ro_pack, stat.S_IREAD)

    r_reset = run_cli("reset", "--vault", vault_sandbox, "--yes")
    assert r_reset.returncode == 0, f"poopdeck reset failed on read-only packfile: {r_reset.stderr}"
    assert not ro_pack.exists()
    assert list((vault_sandbox / "opportunities").glob("*.md")) == []
    assert list((vault_sandbox / "datasets").glob("*.md")) == []
    assert (vault_sandbox / ".git").is_dir()
    assert (vault_sandbox / "questions" / "open-questions.md").is_file()

    demo_hash_after = {
        p.relative_to(demo_dir).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(demo_dir.rglob("*"))
        if p.is_file()
    }
    assert demo_hash_after == demo_hash_before


@pytest.mark.tier2
def test_f12_t2_cli_validate_exits_nonzero_without_traceback_on_corrupted_card_and_strict_dangling_wikilink(
    vault_sandbox: Path,
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Verify `poopdeck validate` & `--strict` exit non-zero cleanly on invalid schema or dangling wikilinks."""
    from datetime import date
    from poopdeck.models import DriverSpec, InternalDatasetUse, OpportunitySpec, ValuationSpec
    from poopdeck.vault_io import upsert_markdown_card

    r_init = run_cli("init", "--company", "--skip-hook", "--vault", vault_sandbox)
    assert r_init.returncode == 0, r_init.stderr

    bad_card = vault_sandbox / "datasets" / "corrupted.md"
    bad_card.parent.mkdir(parents=True, exist_ok=True)
    bad_card.write_bytes(
        b"---\nid: broken\nmedallion_layer: invalid_platinum_layer\n---\n\nBroken dataset.\n"
    )

    r_bad = run_cli("validate", "--vault", vault_sandbox)
    assert r_bad.returncode != 0
    assert "Traceback (most recent call last)" not in (r_bad.stdout + r_bad.stderr)
    bad_card.unlink()

    dangling_opp = OpportunitySpec(
        id="OPP-099",
        title="Dangling Wikilink Test Spec",
        status="backlog",
        created_at=date(2026, 11, 12),
        updated_at=date(2026, 11, 12),
        internal_datasets=[
            InternalDatasetUse(
                ref="[[nonexistent.dataset]]",
                role="Missing upstream table",
                transformations_needed=["Filter active SKUs"],
            )
        ],
        valuation=ValuationSpec(
            confidence_tier="hypothesis",
            driver_formula="units * savings",
            drivers=[
                DriverSpec(name="units", range=(10.0, 20.0, 30.0), verified=False),
                DriverSpec(name="savings", range=(100.0, 200.0, 300.0), verified=False),
            ],
        ),
        effort_weeks=2.0,
        data_readiness_score=3,
        adoption_friction_score=2,
        steps_to_completion=["Verify dataset FQN"],
    )
    upsert_markdown_card(
        vault_sandbox / "opportunities" / "OPP-099-dangling.md",
        dangling_opp,
        "## Opportunity Summary\n\nTest spec with unresolved dataset wikilink.",
    )

    r_strict = run_cli("validate", "--strict", "--vault", vault_sandbox)
    assert r_strict.returncode != 0
    assert "Traceback (most recent call last)" not in (r_strict.stdout + r_strict.stderr)


@pytest.mark.tier2
def test_f12_t2_cli_brief_unknown_opportunity_exits_nonzero_without_traceback(
    vault_sandbox: Path,
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Verify `poopdeck brief OPP-UNKNOWN` exits non-zero cleanly without an unhandled traceback."""
    r_demo = run_cli("demo", "--vault", vault_sandbox, "--force")
    assert r_demo.returncode == 0, r_demo.stderr

    r_unknown = run_cli("brief", "OPP-UNKNOWN", "--vault", vault_sandbox)
    assert r_unknown.returncode != 0
    combined = r_unknown.stdout + r_unknown.stderr
    assert "Traceback (most recent call last)" not in combined
    assert "OPP-UNKNOWN" in combined or "not found" in combined.lower()


@pytest.mark.tier2
def test_f12_t2_cli_ui_defaults_to_loopback_host_and_invalid_sync_target_exits_nonzero(
    vault_sandbox: Path,
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Verify `poopdeck ui --help` defaults to `127.0.0.1` loopback and invalid sync targets exit non-zero."""
    r_ui = run_cli("ui", "--help")
    assert r_ui.returncode == 0, r_ui.stderr
    assert "127.0.0.1" in r_ui.stdout
    assert "0.0.0.0" not in r_ui.stdout
    assert "--no-browser" in r_ui.stdout

    r_bad_target = run_cli("sync", "invalid_target", "--vault", vault_sandbox)
    assert r_bad_target.returncode != 0
    assert "Traceback (most recent call last)" not in (r_bad_target.stdout + r_bad_target.stderr)

    r_bad_mode = run_cli("sync", "databricks", "--mode", "tier99", "--vault", vault_sandbox)
    assert r_bad_mode.returncode != 0
    assert "Traceback (most recent call last)" not in (r_bad_mode.stdout + r_bad_mode.stderr)
