"""Tier 3 pairwise cross-feature integration E2E tests (`T3-01` through `T3-14`).

Covers all 14 planned pairwise contracts from `TEST_INFRA.md §5`:
- T3-01 (F01 x F02): `PoopdeckConfig.dlp` remote patterns govern `run_dlp_check` decisions.
- T3-02 (F01 x F03): `load_config` `privacy_mode` + `AliasChoices` govern `redact_dataset_payload`.
- T3-03 (F03 x F06): `sync_databricks` scrubs PATs in comments & masks PII columns/samples on disk.
- T3-04 (F06 x F04): `sync_databricks` re-sync updates frontmatter while preserving `HUMAN-NOTES`.
- T3-05 (F03 x F08): `sync_teams` scrubs inline emails/SSNs/phones/tokens across `.vtt`/`.docx`/`.md`.
- T3-06 (F06 x F07 x F04): `sync_databricks` + `sync_powerbi` cross-link lineage & pass `validate_vault`.
- T3-07 (F07 x F05): Power BI heuristic DAX gaps feed `existing_pbi_bonus_points` in priority scoring.
- T3-08 (F08 x F04 x F11): `sync_teams` updates stakeholders & questions surfaced via FastAPI UI API.
- T3-09 (F05 x F09): Claude architect & CFO prompts align with `OpportunitySpec` & `external_data_catalog.yaml`.
- T3-10 (F04 x F10 x F11): `POST /api/promote` & `/api/archive` move staged demo cards preserving `HUMAN-NOTES`.
- T3-11 (F05 x F10 x F11): `POST /api/boss-pin` cutline displacement matches `/api/portfolio` & briefs.
- T3-12 (F02 x F12): `poopdeck init` installs LF pre-commit hook blocking forced `vault` commits on `mckenzm4`.
- T3-13 (F01 x F09): Every CLI subcommand/flag & script path cited in `.claude/**` & `CLAUDE.md` exists.
- T3-14 (F02 x F10 x F12): `poopdeck demo` -> `validate` -> `reset --yes` -> `validate` isolates `demo_vault`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

import pytest


def _git(
    repo: Path,
    env: dict[str, str],
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a Git command in `repo` with `GIT_*`-stripped `env`."""
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


def _inject_human_notes(card_path: Path, custom_notes: str) -> str:
    """Inject `custom_notes` between `HUMAN-NOTES` markers and return exact extracted string."""
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


@pytest.mark.tier3
def test_t3_01_f01_f02_config_dlp_remote_patterns_govern_hook_decisions(
    project_root: Path,
    git_sandbox: Path,
    clean_git_env: dict[str, str],
) -> None:
    """Verify `PoopdeckConfig.dlp` `corporate_remote_patterns` vs `external_remote_patterns` govern DLP."""
    from poopdeck.config import load_config
    from scripts.install_dlp_hook import install_hook, run_dlp_check

    for rel in (".gitignore", "scripts/install_dlp_hook.py"):
        src = project_root / rel
        dst = git_sandbox / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    cfg_path = git_sandbox / "config.yaml"
    cfg_path.write_bytes(
        b"dlp:\n"
        b"  corporate_remote_patterns:\n"
        b"    - github.corp.acme.internal\n"
        b"  external_remote_patterns:\n"
        b"    - mckenzm4\n"
        b"    - github.com\n"
    )
    loaded_cfg = load_config(cfg_path)
    assert "github.corp.acme.internal" in loaded_cfg.dlp.corporate_remote_patterns
    install_hook(git_sandbox)

    _git(git_sandbox, clean_git_env, "remote", "add", "origin", "https://github.com/mckenzm4/project-poopdeck.git")

    vault_secret = git_sandbox / "vault" / "datasets" / "secret_tbl.md"
    vault_secret.parent.mkdir(parents=True, exist_ok=True)
    vault_secret.write_bytes(b"# Proprietary Company Table\n")
    _git(git_sandbox, clean_git_env, "add", "-f", "vault/datasets/secret_tbl.md")

    allowed_ext, reasons_ext = run_dlp_check(git_sandbox)
    assert allowed_ext is False
    assert any("vault" in r.lower() for r in reasons_ext)

    _git(
        git_sandbox,
        clean_git_env,
        "remote",
        "set-url",
        "origin",
        "https://github.corp.acme.internal/ds/vault-sync.git",
    )
    allowed_corp, reasons_corp = run_dlp_check(git_sandbox)
    assert allowed_corp is True, f"Corporate remote should allow internal sync, got {reasons_corp}"
    assert reasons_corp == []

    # Switching back to external mckenzm4 while staging clean examples/demo_vault/ must succeed
    _git(
        git_sandbox,
        clean_git_env,
        "remote",
        "set-url",
        "origin",
        "https://github.com/mckenzm4/project-poopdeck.git",
    )
    _git(git_sandbox, clean_git_env, "reset", "HEAD", "vault/datasets/secret_tbl.md", check=False)
    _git(git_sandbox, clean_git_env, "rm", "--cached", "-r", "vault", check=False)
    demo_card = git_sandbox / "examples" / "demo_vault" / "datasets" / "synthetic.md"
    demo_card.parent.mkdir(parents=True, exist_ok=True)
    demo_card.write_bytes(b"# Synthetic Demo Table\n")
    _git(git_sandbox, clean_git_env, "add", "examples/demo_vault/datasets/synthetic.md")

    allowed_demo, reasons_demo = run_dlp_check(git_sandbox)
    assert allowed_demo is True, f"Clean examples/demo_vault commit blocked: {reasons_demo}"


@pytest.mark.tier3
def test_t3_02_f01_f03_config_privacy_alias_choices_drive_sample_row_redaction_gate(
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """Verify `load_config` `privacy_mode` & `AliasChoices` govern `redact_dataset_payload` sample rows."""
    import copy
    from poopdeck.config import load_config
    from poopdeck.redaction import redact_dataset_payload

    bundle = json.loads((fixtures_dir / "databricks" / "discovery_bundle.json").read_text(encoding="utf-8"))
    inv_table = next(t for t in bundle["tables"] if t["fqn"] == "prod_catalog.gold.customer_invoices")
    inv_cols = [c for c in bundle["columns"] if c["fqn"] == "prod_catalog.gold.customer_invoices"]
    raw_payload: dict[str, Any] = {
        **copy.deepcopy(inv_table),
        "columns": copy.deepcopy(inv_cols),
        "sample_rows": copy.deepcopy(bundle["sample_rows"]["prod_catalog.gold.customer_invoices"]),
    }
    assert len(raw_payload["sample_rows"]) == 6

    cfg_a_file = tmp_path / "cfg_safe.yaml"
    cfg_a_file.write_bytes(b"privacy:\n  privacy_mode: safe_aggregates\n")
    cfg_a = load_config(cfg_a_file)
    assert cfg_a.privacy.allow_sample_rows is False
    out_a = redact_dataset_payload(copy.deepcopy(raw_payload), cfg_a.privacy)
    assert out_a["sample_rows"] == []
    cols_a = {c["name"]: c for c in out_a["columns"]}
    assert cols_a["email"]["is_pii"] is True
    assert cols_a["customer_name"]["is_pii"] is True
    assert cols_a["table_namespace"]["is_pii"] is False

    cfg_b_file = tmp_path / "cfg_include_alias.yaml"
    cfg_b_file.write_bytes(
        b"privacy:\n"
        b"  privacy_mode: scrubbed_samples\n"
        b"  include_sample_rows: true\n"
        b"  max_sample_rows: 2\n"
    )
    cfg_b = load_config(cfg_b_file)
    assert cfg_b.privacy.allow_sample_rows is True
    out_b = redact_dataset_payload(copy.deepcopy(raw_payload), cfg_b.privacy)
    assert len(out_b["sample_rows"]) == 2
    for row in out_b["sample_rows"]:
        assert row["email"] == "[REDACTED:PII]"
        assert row["ssn"] == "[REDACTED:PII]"
        assert row["customer_name"] == "[REDACTED:PII]"
        assert "dapi0123456789abcdef0123456789abcdef" not in str(row.get("billing_notes", ""))

    cfg_c_file = tmp_path / "cfg_allow_canonical.yaml"
    cfg_c_file.write_bytes(
        b"privacy:\n"
        b"  privacy_mode: scrubbed_samples\n"
        b"  allow_sample_rows: true\n"
        b"  max_sample_rows: 3\n"
    )
    cfg_c = load_config(cfg_c_file)
    out_c = redact_dataset_payload(copy.deepcopy(raw_payload), cfg_c.privacy)
    assert len(out_c["sample_rows"]) == 3


@pytest.mark.tier3
def test_t3_03_f03_f06_sync_databricks_scrubs_pats_and_masks_pii_columns_on_disk(
    fixtures_dir: Path,
    vault_sandbox: Path,
    tmp_path: Path,
) -> None:
    """Verify `sync_databricks` routes Tier 2 payloads through `redaction.py` before writing vault cards."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.databricks_ingest import sync_databricks
    from poopdeck.models import DatasetCard
    from poopdeck.vault_io import initialize_vault, parse_markdown_card

    initialize_vault(vault_sandbox, init_git=False)
    bundle_copy = tmp_path / "discovery_bundle.json"
    shutil.copy2(fixtures_dir / "databricks" / "discovery_bundle.json", bundle_copy)

    summary = sync_databricks(vault_sandbox, PoopdeckConfig(), input_path=bundle_copy, mode="tier2")
    assert summary.cards_written == 5

    inv_path = vault_sandbox / "datasets" / "prod_catalog.gold.customer_invoices.md"
    snap_path = vault_sandbox / "datasets" / "prod_catalog.silver.inventory_snapshots.md"
    assert inv_path.is_file() and snap_path.is_file()

    snap_raw = snap_path.read_text(encoding="utf-8")
    assert "dapi0123456789abcdef0123456789abcdef" not in snap_raw
    assert "[REDACTED:SECRET]" in snap_raw

    inv_card = DatasetCard.model_validate(parse_markdown_card(inv_path).frontmatter)
    assert inv_card.sample_rows == []
    col_by_name = {c.name: c for c in inv_card.columns}
    for pii_col in ("customer_name", "email", "ssn", "phone", "dob", "salary", "address"):
        assert col_by_name[pii_col].is_pii is True
        if col_by_name[pii_col].min_val is not None:
            assert col_by_name[pii_col].min_val == "[REDACTED:PII]"
        if col_by_name[pii_col].max_val is not None:
            assert col_by_name[pii_col].max_val == "[REDACTED:PII]"
    assert col_by_name["table_namespace"].is_pii is False

    cfg_opt_in = PoopdeckConfig.model_validate(
        {
            "privacy": {
                "privacy_mode": "scrubbed_samples",
                "allow_sample_rows": True,
                "max_sample_rows": 2,
            }
        }
    )
    sync_databricks(vault_sandbox, cfg_opt_in, input_path=bundle_copy, mode="tier2")
    inv_opt = DatasetCard.model_validate(parse_markdown_card(inv_path).frontmatter)
    assert len(inv_opt.sample_rows) == 2
    for row in inv_opt.sample_rows:
        assert row["email"] == "[REDACTED:PII]"
        assert row["ssn"] == "[REDACTED:PII]"


@pytest.mark.tier3
def test_t3_04_f06_f04_databricks_resync_updates_frontmatter_and_preserves_human_notes(
    fixtures_dir: Path,
    vault_sandbox: Path,
    tmp_path: Path,
) -> None:
    """Verify re-running `sync_databricks` updates `DatasetCard` stats while keeping `HUMAN-NOTES` verbatim."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.databricks_ingest import sync_databricks
    from poopdeck.models import DatasetCard
    from poopdeck.vault_io import initialize_vault, parse_markdown_card, validate_vault

    initialize_vault(vault_sandbox, init_git=False)
    bundle_copy = tmp_path / "discovery_bundle.json"
    shutil.copy2(fixtures_dir / "databricks" / "discovery_bundle.json", bundle_copy)

    sync_databricks(vault_sandbox, PoopdeckConfig(), input_path=bundle_copy, mode="tier2")
    snap_path = vault_sandbox / "datasets" / "prod_catalog.silver.inventory_snapshots.md"
    initial_card = DatasetCard.model_validate(parse_markdown_card(snap_path).frontmatter)
    assert initial_card.row_count == 5183000

    rich_notes = (
        "### Warehouse Snapshot Notes\n"
        "- Local script: `C:\\Data\\Lineage\\notes.md`\n"
        "- Discussed intraday dedupe key with [[Sarah Chen]]\n"
        "| Grain | Verified |\n| --- | --- |\n| sku_id + warehouse_id + snapshot_date | Yes |"
    )
    expected_notes = _inject_human_notes(snap_path, rich_notes)

    bundle_data = json.loads(bundle_copy.read_text(encoding="utf-8"))
    for tbl in bundle_data["tables"]:
        if tbl["fqn"] == "prod_catalog.silver.inventory_snapshots":
            tbl["row_count"] = 6250000
            tbl["freshness_lag_hours"] = 0.75
    for prof in bundle_data.get("profiles", []):
        if prof.get("fqn") == "prod_catalog.silver.inventory_snapshots":
            prof["row_count"] = 6250000
            prof["freshness_lag_hours"] = 0.75
    bundle_copy.write_text(json.dumps(bundle_data, indent=2), encoding="utf-8")

    sync_databricks(vault_sandbox, PoopdeckConfig(), input_path=bundle_copy, mode="tier2")
    reparsed = parse_markdown_card(snap_path)
    updated_card = DatasetCard.model_validate(reparsed.frontmatter)
    assert updated_card.row_count == 6250000
    assert updated_card.freshness_lag_hours == pytest.approx(0.75)
    assert reparsed.human_notes == expected_notes

    val_report = validate_vault(vault_sandbox)
    assert val_report.valid is True, val_report.errors


@pytest.mark.tier3
def test_t3_05_f03_f08_sync_teams_scrubs_inline_pii_and_secrets_across_vtt_docx_and_md(
    fixtures_dir: Path,
    vault_sandbox: Path,
) -> None:
    """Verify `sync_teams` scrubs emails, SSNs, phones, PATs, JWTs, and JDBC passwords across all 3 formats."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.teams_ingest import sync_teams
    from poopdeck.vault_io import initialize_vault

    initialize_vault(vault_sandbox, init_git=False)
    inbox = vault_sandbox / "inbox"
    for fname in ("ops_sync_meeting.vtt", "finance_review.docx", "quick_1on1_notes.md"):
        shutil.copy2(fixtures_dir / "teams" / fname, inbox / fname)

    summary = sync_teams(vault_sandbox, PoopdeckConfig())
    assert summary.cards_written >= 3
    for fname in ("ops_sync_meeting.vtt", "finance_review.docx", "quick_1on1_notes.md"):
        assert (inbox / "processed" / fname).is_file()

    target_files = [
        *sorted((vault_sandbox / "stakeholders").glob("*.md")),
        vault_sandbox / "glossary.md",
        vault_sandbox / "questions" / "open-questions.md",
    ]
    assert len(target_files) >= 5

    forbidden_leaks = (
        "sarah.chen@corp-example.com",
        "elena.rostova@corp-example.com",
        "david.park@corp-example.com",
        "123-45-6789",
        "987-65-4321",
        "(555) 867-5309",
        "555-234-5678",
        "(555) 301-9988",
        "dapi0123456789abcdef0123456789abcdef",
        "ghp_0123456789abcdefghijklmnopqrstuvwxyzABCD",
        "SuperSecretPass123!",
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    )
    for md_path in target_files:
        text = md_path.read_text(encoding="utf-8")
        for leak in forbidden_leaks:
            assert leak not in text, f"Raw PII/secret {leak!r} leaked into {md_path}"


@pytest.mark.tier3
def test_t3_06_f06_f07_f04_databricks_and_powerbi_sync_cross_link_lineage_and_validate(
    fixtures_dir: Path,
    vault_sandbox: Path,
    tmp_path: Path,
) -> None:
    """Verify `sync_databricks` + `sync_powerbi` cross-link `ReportCard.upstream_datasets` in lineage."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.databricks_ingest import sync_databricks
    from poopdeck.crawlers.powerbi_ingest import sync_powerbi
    from poopdeck.vault_io import build_lineage_graph, initialize_vault, validate_vault

    initialize_vault(vault_sandbox, init_git=False)
    dbx_in = tmp_path / "dbx"
    dbx_in.mkdir()
    shutil.copy2(fixtures_dir / "databricks" / "discovery_bundle.json", dbx_in / "discovery_bundle.json")
    pbi_in = tmp_path / "pbi"
    shutil.copytree(fixtures_dir / "powerbi" / "sample_pbip", pbi_in / "sample_pbip")

    cfg = PoopdeckConfig()
    sync_databricks(vault_sandbox, cfg, input_path=dbx_in, mode="tier2")
    sync_powerbi(vault_sandbox, cfg, input_path=pbi_in, mode="tier2")

    val_report = validate_vault(vault_sandbox)
    assert val_report.valid is True, val_report.errors

    graph = build_lineage_graph(vault_sandbox)
    nodes_by_id = {n["id"]: n for n in graph["nodes"]}
    for expected_ds in (
        "dataset:prod_catalog.silver.inventory_snapshots",
        "dataset:prod_catalog.gold.po_receipts",
        "dataset:prod_catalog.gold.customer_invoices",
    ):
        assert expected_ds in nodes_by_id
        assert nodes_by_id[expected_ds]["exists_in_vault"] is True

    feeds_report_edges = [e for e in graph["edges"] if e["relation"] == "feeds_report"]
    assert len(feeds_report_edges) >= 2
    upstream_sources = {e["source"] for e in feeds_report_edges}
    assert "dataset:prod_catalog.silver.inventory_snapshots" in upstream_sources
    assert "dataset:prod_catalog.gold.po_receipts" in upstream_sources


@pytest.mark.tier3
def test_t3_07_f07_f05_powerbi_heuristic_gaps_feed_opportunity_existing_pbi_priority_bonus(
    fixtures_dir: Path,
    vault_sandbox: Path,
    tmp_path: Path,
) -> None:
    """Verify Power BI heuristic DAX gaps feed `OpportunitySpec` & trigger `existing_pbi_bonus_points`."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.powerbi_ingest import sync_powerbi
    from poopdeck.models import (
        DriverSpec,
        InternalDatasetUse,
        NewReportSpec,
        OpportunitySpec,
        ReportCard,
        ReportSupplement,
        ValuationSpec,
        compute_priority_score,
    )
    from poopdeck.vault_io import initialize_vault, parse_markdown_card

    initialize_vault(vault_sandbox, init_git=False)
    pbi_in = tmp_path / "pbi"
    shutil.copytree(fixtures_dir / "powerbi" / "sample_pbip", pbi_in / "sample_pbip")
    sync_powerbi(vault_sandbox, PoopdeckConfig(), input_path=pbi_in, mode="tier2")

    rep_cards = [
        ReportCard.model_validate(parse_markdown_card(p).frontmatter)
        for p in sorted((vault_sandbox / "reports").glob("*.md"))
    ]
    assert len(rep_cards) >= 1
    all_flags = {f for r in rep_cards for m in r.measures for f in m.flagged_heuristics}
    assert {
        "nested_heuristic_tiers",
        "naive_moving_avg_forecast",
        "static_threshold_alert",
        "backward_looking_only",
    }.issubset(all_flags)
    assert any(len(r.bi_to_ds_gaps) >= 1 for r in rep_cards)

    target_report = rep_cards[0]
    common_kwargs: dict[str, Any] = {
        "status": "backlog",
        "created_at": date(2026, 11, 12),
        "updated_at": date(2026, 11, 12),
        "internal_datasets": [
            InternalDatasetUse(
                ref="[[prod_catalog.silver.inventory_snapshots]]",
                role="Daily SKU inventory grain",
                transformations_needed=["Compute 14-day stockout hazard"],
            )
        ],
        "valuation": ValuationSpec(
            confidence_tier="directional",
            driver_formula="skus * margin_saved_per_sku",
            drivers=[
                DriverSpec(name="skus", range=(200.0, 400.0, 600.0), verified=True),
                DriverSpec(name="margin_saved_per_sku", range=(500.0, 750.0, 1000.0), verified=False),
            ],
        ),
        "effort_weeks": 3.0,
        "data_readiness_score": 4,
        "adoption_friction_score": 2,
        "steps_to_completion": ["Publish Gold prediction table"],
    }

    opp_supplement = OpportunitySpec(
        id="OPP-101",
        title="Supplement Existing Buyer Workbench",
        existing_reports_supplemented=[
            ReportSupplement(
                ref=f"[[{target_report.id}]]",
                integration_mode="Replace static 14-day DAX alert with calibrated stockout probability",
            )
        ],
        new_reports_unlocked=[],
        **common_kwargs,
    )
    opp_standalone = OpportunitySpec(
        id="OPP-102",
        title="Net-New Standalone App Only",
        existing_reports_supplemented=[],
        new_reports_unlocked=[
            NewReportSpec(
                title="Net-New Inventory App",
                audience="Buyers",
                why_net_new="Greenfield simulator",
            )
        ],
        **common_kwargs,
    )

    score_supp = compute_priority_score(opp_supplement)
    score_stand = compute_priority_score(opp_standalone)
    assert score_supp > score_stand


@pytest.mark.tier3
def test_t3_08_f08_f04_f11_teams_sync_surfaces_stakeholders_and_questions_in_ui_api(
    fixtures_dir: Path,
    vault_sandbox: Path,
) -> None:
    """Verify `sync_teams` updates `StakeholderCard` & `open-questions.md` visible in `/api/questions` & `/api/lineage`."""
    from fastapi.testclient import TestClient
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.teams_ingest import sync_teams
    from poopdeck.models import StakeholderCard
    from poopdeck.ui.app import create_app
    from poopdeck.vault_io import initialize_vault, parse_markdown_card

    initialize_vault(vault_sandbox, init_git=False)
    inbox = vault_sandbox / "inbox"
    for fname in ("ops_sync_meeting.vtt", "finance_review.docx", "quick_1on1_notes.md"):
        shutil.copy2(fixtures_dir / "teams" / fname, inbox / fname)

    sync_teams(vault_sandbox, PoopdeckConfig())

    stakeholder_cards = [
        StakeholderCard.model_validate(parse_markdown_card(p).frontmatter)
        for p in sorted((vault_sandbox / "stakeholders").glob("*.md"))
    ]
    assert any(len(s.boss_mandate_asks) >= 1 for s in stakeholder_cards)

    client = TestClient(create_app(vault_sandbox))
    r_q = client.get("/api/questions")
    assert r_q.status_code == 200
    assert len(r_q.json()["questions"]) >= 16

    r_ready = client.get("/api/readiness")
    assert r_ready.status_code == 200
    assert len(r_ready.json()["phases"]) == 5

    r_lin = client.get("/api/lineage")
    assert r_lin.status_code == 200
    stk_nodes = [n for n in r_lin.json()["nodes"] if n["type"] == "stakeholder"]
    assert len(stk_nodes) >= 3


@pytest.mark.tier3
def test_t3_09_f05_f09_claude_architect_and_cfo_agents_align_with_staging_and_packs_schema(
    project_root: Path,
) -> None:
    """Verify Claude architect & CFO prompts align with `external_data_catalog.yaml` & `OpportunitySpec` gates."""
    from pydantic import ValidationError
    from poopdeck.models import DriverSpec, InternalDatasetUse, OpportunitySpec, ValuationSpec
    from poopdeck.packs import load_external_data_catalog

    arch_text = (project_root / ".claude" / "agents" / "ds-opportunity-architect.md").read_text(
        encoding="utf-8"
    )
    cfo_text = (project_root / ".claude" / "agents" / "skeptical-cfo-critic.md").read_text(
        encoding="utf-8"
    )
    disc_text = (project_root / ".claude" / "commands" / "discover-opportunities.md").read_text(
        encoding="utf-8"
    )

    catalog = load_external_data_catalog()
    expected_domains = {
        "generic",
        "ops_supply_chain",
        "retail_cpg",
        "finance_risk",
        "b2b_saas",
        "manufacturing",
    }
    assert expected_domains.issubset(set(catalog.domain_lenses.keys()))
    ext_blob = " ".join(f"{d.id} {d.source}" for d in catalog.external_datasets).lower()
    for public_source in ("fred", "census", "noaa", "holiday"):
        assert public_source in ext_blob

    assert "external_data_catalog.yaml" in arch_text
    assert "status: \"staging\"" in arch_text or "status: staging" in arch_text
    assert "skeptical-cfo-critic" in disc_text
    assert "[VERIFY WITH STAKEHOLDER]" in cfo_text

    staged_spec = OpportunitySpec(
        id="OPP-201",
        title="Staged Forecast Candidate",
        status="staging",
        created_at=date(2026, 11, 12),
        updated_at=date(2026, 11, 12),
        internal_datasets=[
            InternalDatasetUse(
                ref="[[prod_catalog.silver.inventory_snapshots]]",
                role="Feature history",
                transformations_needed=["Aggregate weekly"],
            )
        ],
        valuation=ValuationSpec(
            confidence_tier="directional",
            driver_formula="volume * unit_saving",
            drivers=[
                DriverSpec(name="volume", value=1000.0, verified=True),
                DriverSpec(
                    name="unit_saving",
                    range=(10.0, 25.0, 40.0),
                    verified=False,
                    verify_question="Confirm unit saving with Finance",
                ),
            ],
        ),
        effort_weeks=3.0,
        data_readiness_score=4,
        adoption_friction_score=2,
        steps_to_completion=["Run holdout backtest"],
    )
    assert any(
        p.startswith("[VERIFY WITH STAKEHOLDER]") for p in staged_spec.valuation.verification_prompts
    )

    with pytest.raises((ValidationError, ValueError)):
        ValuationSpec(
            confidence_tier="finance_verified",
            driver_formula="volume * unit_saving",
            drivers=[
                DriverSpec(name="volume", value=1000.0, verified=True),
                DriverSpec(name="unit_saving", value=25.0, verified=False),
            ],
        )


@pytest.mark.tier3
def test_t3_10_f04_f10_f11_ui_promote_and_archive_demo_staged_specs_preserve_human_notes(
    project_root: Path,
    vault_sandbox: Path,
) -> None:
    """Verify `POST /api/promote` & `/api/archive` on demo staged specs move cards & keep `HUMAN-NOTES`."""
    from fastapi.testclient import TestClient
    from poopdeck.ui.app import create_app
    from poopdeck.vault_io import parse_markdown_card, validate_vault

    shutil.copytree(project_root / "examples" / "demo_vault", vault_sandbox, dirs_exist_ok=True)
    opp5_stg = next((vault_sandbox / "staging" / "opportunities").glob("OPP-005-*.md"))
    opp6_stg = next((vault_sandbox / "staging" / "opportunities").glob("OPP-006-*.md"))

    notes_5 = _inject_human_notes(opp5_stg, "Approved for Q1 backlog (`C:\\Reviews\\opp5.md`).")
    notes_6 = _inject_human_notes(opp6_stg, "Archived due to high D&B vendor cost (`[[Elena Rostova]]`).")

    client = TestClient(create_app(vault_sandbox))
    r_prom = client.post("/api/promote", json={"id": "OPP-005"})
    assert r_prom.status_code == 200
    assert r_prom.json()["new_status"] == "backlog"

    r_arch = client.post("/api/archive", json={"id": "OPP-006", "reason": "High D&B vendor blocker"})
    assert r_arch.status_code == 200
    assert r_arch.json()["status"] == "archived"

    assert client.get("/api/staging").json()["count"] == 0
    opp5_live = vault_sandbox / "opportunities" / opp5_stg.name
    opp6_live = vault_sandbox / "opportunities" / opp6_stg.name
    assert opp5_live.is_file() and opp6_live.is_file()
    assert parse_markdown_card(opp5_live).frontmatter["status"] == "backlog"
    assert parse_markdown_card(opp6_live).frontmatter["status"] == "archived"
    assert parse_markdown_card(opp5_live).human_notes == notes_5
    assert parse_markdown_card(opp6_live).human_notes == notes_6

    val = validate_vault(vault_sandbox, strict=True)
    assert val.valid is True, val.errors


@pytest.mark.tier3
def test_t3_11_f05_f10_f11_ui_boss_pin_updates_portfolio_cutline_and_brief_parity(
    project_root: Path,
    vault_sandbox: Path,
) -> None:
    """Verify `POST /api/boss-pin` cutline displacement matches `/api/portfolio`, `/api/brief`, & `brief.py`."""
    from fastapi.testclient import TestClient
    from poopdeck.brief import generate_portfolio_brief
    from poopdeck.ui.app import create_app

    shutil.copytree(project_root / "examples" / "demo_vault", vault_sandbox, dirs_exist_ok=True)
    client = TestClient(create_app(vault_sandbox))

    r_unpin = client.post(
        "/api/boss-pin",
        json={"id": "OPP-002", "boss_mandate": False, "capacity_weeks": 12.0},
    )
    assert r_unpin.status_code == 200
    assert r_unpin.json()["displaced_ids"] == []
    assert r_unpin.json()["funded_ids"] == ["OPP-001", "OPP-004", "OPP-003"]

    r_repin = client.post(
        "/api/boss-pin",
        json={"id": "OPP-002", "boss_mandate": True, "capacity_weeks": 12.0},
    )
    assert r_repin.status_code == 200
    assert r_repin.json()["funded_ids"] == ["OPP-002", "OPP-001", "OPP-004"]
    assert r_repin.json()["displaced_ids"] == ["OPP-003"]

    port_json = client.get("/api/portfolio?capacity_weeks=12.0").json()
    assert port_json["displaced_ids"] == ["OPP-003"]

    api_brief_md = client.get("/api/brief?capacity_weeks=12.0").json()["markdown"]
    py_brief_md = generate_portfolio_brief(vault_sandbox, capacity_weeks=12.0).markdown
    assert api_brief_md == py_brief_md
    assert "OPP-002" in api_brief_md and "OPP-003" in api_brief_md


@pytest.mark.tier3
def test_t3_12_f02_f12_cli_init_installs_dlp_hook_and_blocks_forced_vault_commit_on_mckenzm4(
    project_root: Path,
    git_sandbox: Path,
    clean_git_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Verify `poopdeck init` installs LF pre-commit hook blocking `git add -f vault` on `mckenzm4`."""
    for rel in (".gitignore", "config.example.yaml", "scripts/install_dlp_hook.py"):
        src = project_root / rel
        dst = git_sandbox / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    _git(git_sandbox, clean_git_env, "remote", "add", "origin", "https://github.com/mckenzm4/project-poopdeck.git")
    nested_vault = git_sandbox / "vault"

    r_init = run_cli("init", "--company", "--repo-root", git_sandbox, "--vault", nested_vault)
    assert r_init.returncode == 0, f"poopdeck init failed: {r_init.stderr}"
    assert (nested_vault / ".git").is_dir()

    hook_path = git_sandbox / ".git" / "hooks" / "pre-commit"
    assert hook_path.is_file()
    assert b"\r\n" not in hook_path.read_bytes()

    if _git(nested_vault, clean_git_env, "rev-parse", "--verify", "HEAD", check=False).returncode != 0:
        _git(nested_vault, clean_git_env, "config", "user.name", "Test User")
        _git(nested_vault, clean_git_env, "config", "user.email", "test@example.com")
        _git(nested_vault, clean_git_env, "add", ".")
        _git(nested_vault, clean_git_env, "commit", "-q", "-m", "Seed local company vault")

    _git(git_sandbox, clean_git_env, "add", "-f", "vault/questions/open-questions.md", check=False)
    _git(git_sandbox, clean_git_env, "add", "-f", "vault")
    stage_lines = _git(git_sandbox, clean_git_env, "ls-files", "--stage").stdout.splitlines()
    assert any(ln.startswith("160000 ") and ln.endswith("\tvault") for ln in stage_lines), (
        f"Expected mode 160000 gitlink for 'vault', got: {stage_lines!r}"
    )

    commit_env = dict(clean_git_env)
    commit_env["POOPDECK_ENGINE_ROOT"] = str(project_root)
    commit_res = subprocess.run(
        ["git", "commit", "-m", "Attempt forced vault commit to mckenzm4"],
        cwd=git_sandbox,
        env=commit_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert commit_res.returncode != 0
    assert "[POOPDECK-DLP-BLOCKED]" in (commit_res.stderr + commit_res.stdout)


@pytest.mark.tier3
def test_t3_13_f01_f09_claude_harness_cli_flags_and_script_paths_match_live_surfaces(
    project_root: Path,
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Verify every script path & CLI subcommand/flag cited in `.claude/**` & `CLAUDE.md` exists."""
    for rel_script in (
        "notebooks/databricks_zero_admin_crawler.py",
        "queries/powerbi_dax_info_extract.dax",
        "scripts/install_dlp_hook.py",
        "scripts/day1_init.ps1",
        "scripts/day1_init.sh",
        "poopdeck/packs/external_data_catalog.yaml",
    ):
        assert (project_root / rel_script).is_file(), f"Missing cited repository artifact: {rel_script}"

    expected_cli_flags: dict[str, tuple[str, ...]] = {
        "init": ("--company", "--vault", "--config", "--repo-root", "--skip-hook"),
        "sync": ("--input", "--bundle", "--mode", "--allow-sample-rows", "--vault", "--config"),
        "validate": ("--vault", "--config", "--strict", "--json"),
        "demo": ("--vault", "--config", "--force"),
        "reset": ("--vault", "--config", "--yes", "--force"),
        "ui": ("--host", "--port", "--vault", "--config", "--no-browser"),
        "brief": ("--portfolio", "--format", "--output", "--vault", "--config"),
    }

    claude_md = (project_root / "CLAUDE.md").read_text(encoding="utf-8")
    for subcmd, flags in expected_cli_flags.items():
        assert f"poopdeck {subcmd}" in claude_md
        res = run_cli(subcmd, "--help")
        assert res.returncode == 0, f"`poopdeck {subcmd} --help` failed: {res.stderr}"
        for flag in flags:
            assert flag in res.stdout, f"Flag {flag!r} missing from `poopdeck {subcmd} --help`"


@pytest.mark.tier3
def test_t3_14_f02_f10_f12_cli_demo_validate_reset_validate_lifecycle_isolates_demo_vault(
    project_root: Path,
    vault_sandbox: Path,
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Verify `poopdeck demo` -> `validate` -> `reset --yes` -> `validate` leaves `demo_vault` untouched."""
    demo_dir = project_root / "examples" / "demo_vault"
    before_hashes = {
        p.relative_to(demo_dir).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(demo_dir.rglob("*"))
        if p.is_file()
    }

    assert run_cli("demo", "--vault", vault_sandbox, "--force").returncode == 0
    assert run_cli("validate", "--strict", "--vault", vault_sandbox).returncode == 0
    assert run_cli("reset", "--yes", "--vault", vault_sandbox).returncode == 0
    r_val_after = run_cli("validate", "--strict", "--vault", vault_sandbox)
    assert r_val_after.returncode == 0, f"Post-reset validate failed: {r_val_after.stderr}"

    assert (vault_sandbox / ".git").is_dir()
    for empty_dir in ("datasets", "reports", "opportunities", "staging/opportunities"):
        assert list((vault_sandbox / empty_dir).glob("*.md")) == []

    from poopdeck.vault_io import load_questions_and_playbook

    q_bank, playbook = load_questions_and_playbook(vault_sandbox)
    assert len(q_bank.questions) == 15
    assert len(playbook.phases) == 5

    after_hashes = {
        p.relative_to(demo_dir).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(demo_dir.rglob("*"))
        if p.is_file()
    }
    assert after_hashes == before_hashes
