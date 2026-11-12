"""Comprehensive non-mocked unit tests for Milestone M3 (`poopdeck.ui`)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
import json
import math
from pathlib import Path
import re
import shutil
import subprocess

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import poopdeck.ui
from poopdeck.models import (
    ColumnProfile,
    DatasetCard,
    DaxMeasure,
    DriverSpec,
    ExternalDataReq,
    InternalDatasetUse,
    NewReportSpec,
    OpportunitySpec,
    ReportCard,
    ReportSupplement,
    StakeholderCard,
    ValuationSpec,
)
from poopdeck.ui.app import app as module_app, create_app
from poopdeck.vault_io import (
    initialize_vault,
    parse_markdown_card,
    upsert_markdown_card,
    validate_vault,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
M3_OWNED_FILES: tuple[Path, ...] = (
    REPO_ROOT / "poopdeck" / "ui" / "__init__.py",
    REPO_ROOT / "poopdeck" / "ui" / "app.py",
    REPO_ROOT / "poopdeck" / "ui" / "static" / "index.html",
    REPO_ROOT / "poopdeck" / "ui" / "static" / "styles.css",
    REPO_ROOT / "poopdeck" / "ui" / "static" / "app.js",
    REPO_ROOT / "tests" / "unit" / "test_m3_ui.py",
)


def _make_valuation(
    *,
    confidence_tier: str = "directional",
    formula: str = "annual_volume * unit_margin_usd * lift_pct",
    volume: float = 100_000.0,
    unit_margin: float = 20.0,
    lift_range: tuple[float, float, float] = (0.05, 0.10, 0.15),
    verified_lift: bool = False,
) -> ValuationSpec:
    return ValuationSpec(
        confidence_tier=confidence_tier,  # type: ignore[arg-type]
        driver_formula=formula,
        drivers=[
            DriverSpec(
                name="annual_volume",
                value=volume,
                source="Gold PO Receipts 12m",
                verified=True,
            ),
            DriverSpec(
                name="unit_margin_usd",
                value=unit_margin,
                source="Finance Controller std margin",
                verified=True,
            ),
            DriverSpec(
                name="lift_pct",
                range=lift_range,
                source="Supply Chain benchmark",
                verified=verified_lift,
                verify_question="Confirm pilot lift range with VP Supply Chain.",
            ),
        ],
    )


@pytest.fixture
def seeded_vault(tmp_path: Path) -> Path:
    """Create a schema-valid synthetic vault in `tmp_path` using real M1 primitives."""
    vdir = tmp_path / "vault"
    initialize_vault(vdir, seed_questions=True, init_git=False)

    ds_inv = DatasetCard(
        fqn="prod_catalog.silver.inventory_snapshots",
        table_type="MANAGED",
        medallion_layer="silver",
        row_count=2_450_000,
        freshness_lag_hours=3.5,
        grain="sku_id x dc_id x snapshot_date",
        join_keys=["sku_id", "dc_id"],
        columns=[
            ColumnProfile(name="sku_id", data_type="STRING", null_pct=0.0),
            ColumnProfile(name="on_hand_units", data_type="BIGINT", null_pct=0.1),
        ],
        downstream_refs=["[[prod_catalog.gold.po_receipts]]"],
        updated_at=date(2026, 11, 10),
    )
    upsert_markdown_card(
        vdir / "datasets" / "prod_catalog.silver.inventory_snapshots.md",
        ds_inv,
        "## Dataset Overview\n\nDaily DC inventory snapshot table.",
    )

    ds_po = DatasetCard(
        fqn="prod_catalog.gold.po_receipts",
        table_type="MANAGED",
        medallion_layer="gold",
        row_count=890_000,
        freshness_lag_hours=6.0,
        grain="po_line_id",
        join_keys=["po_id", "sku_id", "vendor_id"],
        columns=[
            ColumnProfile(name="po_id", data_type="STRING", null_pct=0.0),
            ColumnProfile(name="lead_time_days", data_type="DOUBLE", null_pct=1.2),
        ],
        upstream_refs=["[[prod_catalog.silver.inventory_snapshots]]"],
        updated_at=date(2026, 11, 10),
    )
    upsert_markdown_card(
        vdir / "datasets" / "prod_catalog.gold.po_receipts.md",
        ds_po,
        "## Dataset Overview\n\nVendor purchase order receipts & lead-time history.",
    )

    report = ReportCard(
        id="Supply Chain Workspace__Daily Buyer Workbench",
        title="Daily Buyer Workbench",
        workspace="Supply Chain Workspace",
        semantic_model="SupplyChainCore",
        refresh_schedule="Daily 06:00 EST",
        refresh_failures_30d=2,
        upstream_datasets=[
            "[[prod_catalog.silver.inventory_snapshots]]",
            "[[prod_catalog.gold.po_receipts]]",
        ],
        measures=[
            DaxMeasure(
                name="Static Reorder Flag",
                table="Inventory",
                expression="IF([Days_Of_Supply] < 14, 1, 0)",
                flagged_heuristics=["static_threshold_alert"],
            )
        ],
        bi_to_ds_gaps=["Static 14-day reorder cutoff ignores vendor lead-time variance"],
        high_excel_export=True,
        owners=["[[Sarah Jenkins]]"],
        updated_at=date(2026, 11, 10),
    )
    upsert_markdown_card(
        vdir / "reports" / "daily-buyer-workbench.md",
        report,
        "## Report Summary\n\nPrimary buyer replenishment Power BI report.",
    )

    sh_sarah = StakeholderCard(
        name="Sarah Jenkins",
        role="VP Supply Chain",
        department="Operations",
        influence_tier="exec",
        decision_appetite="high",
        owned_reports=["[[Supply Chain Workspace__Daily Buyer Workbench]]"],
        kpis_cared_about=["Fill Rate", "Inventory Carrying Cost"],
        updated_at=date(2026, 11, 10),
    )
    upsert_markdown_card(
        vdir / "stakeholders" / "sarah-jenkins.md",
        sh_sarah,
        "## Stakeholder Notes\n\nSponsors replenishment modernization.",
    )

    sh_marcus = StakeholderCard(
        name="Marcus Vance",
        role="Director Finance Analytics",
        department="Finance",
        influence_tier="manager",
        decision_appetite="medium",
        kpis_cared_about=["Working Capital", "Gross Margin"],
        updated_at=date(2026, 11, 10),
    )
    upsert_markdown_card(
        vdir / "stakeholders" / "marcus-vance.md",
        sh_marcus,
        "## Stakeholder Notes\n\nValidates ROI unit economics.",
    )

    # Active/Backlog opportunities carefully calibrated for 12.0w cutline math:
    # Unpinned order by score: OPP-001 (2.5w, 91.0) + OPP-003 (4.0w, 86.0) + OPP-004 (3.5w, 79.0) = 10.0w <= 12.0w;
    # OPP-002 (4.5w, 42.0) lands at 14.5w (below cutline).
    # When OPP-002 is pinned (boss_mandate=True): OPP-002 (4.5w) + OPP-001 (2.5w) + OPP-003 (4.0w) = 11.0w <= 12.0w;
    # OPP-004 hits 14.5w > 12.0w -> displaced_ids == ["OPP-004"]!
    opp_specs = [
        (
            "OPP-001-lead-time-quantile.md",
            OpportunitySpec(
                id="OPP-001",
                title="Vendor Lead-Time Quantile Forecasting",
                status="active",
                boss_mandate=False,
                domain_pack="ops_supply_chain",
                internal_datasets=[
                    InternalDatasetUse(
                        ref="[[prod_catalog.gold.po_receipts]]",
                        role="training target",
                        transformations_needed=["Compute 90th percentile vendor lag"],
                    )
                ],
                external_data_required=[
                    ExternalDataReq(
                        source="NOAA GHCN Daily Weather",
                        purpose="Capture severe weather transit delays",
                        acquisition="free_public_api",
                        blocker_severity="low",
                    )
                ],
                existing_reports_supplemented=[
                    ReportSupplement(
                        ref="[[Supply Chain Workspace__Daily Buyer Workbench]]",
                        integration_mode="Add P50/P90 dynamic safety stock columns",
                    )
                ],
                new_reports_unlocked=[
                    NewReportSpec(
                        title="Vendor Reliability Risk Scorecard",
                        audience="Procurement Leadership",
                        why_net_new="No PBI view tracks forward tail risk today",
                    )
                ],
                valuation=_make_valuation(
                    confidence_tier="finance_verified",
                    volume=120_000.0,
                    unit_margin=25.0,
                    lift_range=(0.08, 0.12, 0.16),
                    verified_lift=True,
                ),
                effort_weeks=2.5,
                data_readiness_score=5,
                adoption_friction_score=2,
                computed_priority_score=91.0,
                steps_to_completion=["Train quantile regressor", "Publish Gold table"],
                stakeholders=["[[Sarah Jenkins]]", "[[Marcus Vance]]"],
            ),
            "## Problem Framing\n\nReplaces static lead times.",
            "\nKeep aligned with [[Sarah Jenkins]] weekly staff review.\n",
        ),
        (
            "OPP-002-exec-board-deck-sim.md",
            OpportunitySpec(
                id="OPP-002",
                title="Executive Tariff Scenario Simulator",
                status="backlog",
                boss_mandate=False,
                domain_pack="finance_risk",
                internal_datasets=[
                    InternalDatasetUse(
                        ref="[[prod_catalog.gold.po_receipts]]",
                        role="landed cost baseline",
                    )
                ],
                valuation=_make_valuation(
                    confidence_tier="hypothesis",
                    volume=50_000.0,
                    unit_margin=15.0,
                    lift_range=(0.04, 0.08, 0.12),
                ),
                effort_weeks=4.5,
                data_readiness_score=3,
                adoption_friction_score=3,
                computed_priority_score=42.0,
                steps_to_completion=["Map HTS codes", "Build scenario parameter table"],
                stakeholders=["[[Marcus Vance]]"],
            ),
            "## Problem Framing\n\nBoard ask for quarterly tariff sensitivity.",
            "\n  Custom indent\twith [[Marcus Vance]] & DAX `A < B && C > 0` notes!  \nLine 2 preserved verbatim.\n",
        ),
        (
            "OPP-003-safety-stock-optimizer.md",
            OpportunitySpec(
                id="OPP-003",
                title="Multi-Echelon Safety Stock Optimizer",
                status="backlog",
                boss_mandate=False,
                domain_pack="ops_supply_chain",
                internal_datasets=[
                    InternalDatasetUse(
                        ref="[[prod_catalog.silver.inventory_snapshots]]",
                        role="historical stockout exposure",
                    )
                ],
                existing_reports_supplemented=[
                    ReportSupplement(
                        ref="[[Supply Chain Workspace__Daily Buyer Workbench]]",
                        integration_mode="Surface recommended reorder point",
                    )
                ],
                valuation=_make_valuation(
                    confidence_tier="directional",
                    volume=110_000.0,
                    unit_margin=22.0,
                    lift_range=(0.07, 0.11, 0.15),
                ),
                effort_weeks=4.0,
                data_readiness_score=4,
                adoption_friction_score=2,
                computed_priority_score=86.0,
                steps_to_completion=["Calibrate service-level targets"],
                stakeholders=["[[Sarah Jenkins]]"],
            ),
            "## Problem Framing\n\nBalances working capital vs fill rate.",
            "\nOPP-003 private human note.\n",
        ),
        (
            "OPP-004-dynamic-stockout-alerting.md",
            OpportunitySpec(
                id="OPP-004",
                title="Dynamic Stockout Risk & Reorder Alerting",
                status="backlog",
                boss_mandate=False,
                domain_pack="ops_supply_chain",
                internal_datasets=[
                    InternalDatasetUse(
                        ref="[[prod_catalog.silver.inventory_snapshots]]",
                        role="daily stock position",
                    )
                ],
                existing_reports_supplemented=[
                    ReportSupplement(
                        ref="[[Supply Chain Workspace__Daily Buyer Workbench]]",
                        integration_mode="Replace static 14d flag with 14d stockout probability",
                    )
                ],
                valuation=_make_valuation(
                    confidence_tier="directional",
                    volume=130_000.0,
                    unit_margin=25.5,
                    lift_range=(0.06, 0.10, 0.15),
                ),
                effort_weeks=3.5,
                data_readiness_score=4,
                adoption_friction_score=2,
                computed_priority_score=79.0,
                steps_to_completion=["Score daily SKU-DC risk"],
                stakeholders=["[[Sarah Jenkins]]"],
            ),
            "## Problem Framing\n\nEliminates manual Excel triage.",
            "\nOPP-004 human note.\n",
        ),
    ]

    for fname, spec, body, hnotes in opp_specs:
        upsert_markdown_card(
            vdir / "opportunities" / fname,
            spec,
            body,
            human_notes_override=hnotes,
        )

    # Staged candidate specs in vault/staging/opportunities/
    opp_005 = OpportunitySpec(
        id="OPP-005",
        title="Shrink & Spoilage Anomaly Detector",
        status="staging",
        boss_mandate=False,
        domain_pack="ops_supply_chain",
        internal_datasets=[
            InternalDatasetUse(
                ref="[[prod_catalog.silver.inventory_snapshots]]",
                role="cycle-count adjustments",
            )
        ],
        valuation=_make_valuation(
            confidence_tier="directional",
            volume=80_000.0,
            unit_margin=18.0,
            lift_range=(0.05, 0.09, 0.14),
        ),
        effort_weeks=3.0,
        data_readiness_score=4,
        adoption_friction_score=2,
        cfo_critique_verdict="READY_FOR_REVIEW",
        cfo_critique_notes=["Verify net recovery rate after disposal cost."],
        stakeholders=["[[Sarah Jenkins]]"],
    )
    upsert_markdown_card(
        vdir / "staging" / "opportunities" / "OPP-005-shrink-anomaly.md",
        opp_005,
        (
            "## Candidate Summary\n\nDetects abnormal DC write-offs.\n\n"
            "## Skeptical CFO Critique\n\n"
            "Challenge gross-to-net recovery; confirm [[Sarah Jenkins]] owns DC cycle counts."
        ),
        human_notes_override=(
            "\n  Staged note for [[Sarah Jenkins]]: check `IF([Shrink] < 0 && [Cost] > 50)`  \n"
        ),
    )

    opp_006 = OpportunitySpec(
        id="OPP-006",
        title="Simple Late-PO Threshold View",
        status="staging",
        boss_mandate=False,
        domain_pack="ops_supply_chain",
        internal_datasets=[
            InternalDatasetUse(
                ref="[[prod_catalog.gold.po_receipts]]",
                role="late flag",
            )
        ],
        valuation=_make_valuation(
            confidence_tier="hypothesis",
            volume=30_000.0,
            unit_margin=10.0,
            lift_range=(0.02, 0.04, 0.06),
        ),
        effort_weeks=2.0,
        data_readiness_score=5,
        adoption_friction_score=1,
        cfo_critique_verdict="SIMPLIFY_TO_SQL_DAX",
        cfo_critique_notes=["80% solvable via SQL view + Power BI conditional formatting."],
        stakeholders=["[[Marcus Vance]]"],
    )
    upsert_markdown_card(
        vdir / "staging" / "opportunities" / "OPP-006-late-po-view.md",
        opp_006,
        (
            "## Candidate Summary\n\nFlags overdue POs.\n\n"
            "## Skeptical CFO Critique\n\n"
            "Do not build ML; ship a 20-line DAX measure instead."
        ),
        human_notes_override="\nArchive candidate note — keep verbatim!\n",
    )

    report_check = validate_vault(vdir, strict=True)
    assert report_check.valid is True, report_check.errors
    return vdir


# ---------------------------------------------------------------------------
# 1. App Factory, Exports, Encoding & Dynamic Vault Resolution
# ---------------------------------------------------------------------------


def test_create_app_exported_from_both_package_and_module() -> None:
    assert poopdeck.ui.create_app is create_app
    assert isinstance(module_app, FastAPI)
    fresh = create_app()
    assert isinstance(fresh, FastAPI)

    for path in M3_OWNED_FILES:
        assert path.is_file(), f"Missing M3 owned file: {path}"
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"UTF-8 BOM found in {path}"
        assert b"\r\n" not in raw, f"CRLF line endings found in {path}"
        raw.decode("utf-8")


def test_explicit_missing_config_path_raises_file_not_found(tmp_path: Path) -> None:
    missing_cfg = tmp_path / "does_not_exist_config.yaml"
    with pytest.raises(FileNotFoundError):
        create_app(config_path=missing_cfg)


def test_vault_resolution_precedence_explicit_over_env_over_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_cfg = tmp_path / "vault_from_cfg"
    vault_env = tmp_path / "vault_from_env"
    vault_exp = tmp_path / "vault_from_explicit"
    for v in (vault_cfg, vault_env, vault_exp):
        initialize_vault(v, seed_questions=True, init_git=False)

    cfg_file = tmp_path / "custom_config.yaml"
    cfg_file.write_text(
        f"vault_path: {vault_cfg.as_posix()}\nquarterly_capacity_weeks: 16.5\ndomain: retail_cpg\n",
        encoding="utf-8",
        newline="\n",
    )

    monkeypatch.delenv("POOPDECK_VAULT_DIR", raising=False)
    app_from_cfg = create_app(config_path=cfg_file)
    with TestClient(app_from_cfg) as client:
        h1 = client.get("/api/health").json()
        assert h1["vault_dir"] == vault_cfg.resolve().as_posix()
        assert h1["quarterly_capacity_weeks"] == 16.5
        assert h1["domain"] == "retail_cpg"

        # Env var overrides config.yaml dynamically without recreating app
        monkeypatch.setenv("POOPDECK_VAULT_DIR", str(vault_env))
        h2 = client.get("/api/health").json()
        assert h2["vault_dir"] == vault_env.resolve().as_posix()

    # Explicit vault_dir overrides POOPDECK_VAULT_DIR and config.yaml
    app_explicit = create_app(vault_dir=vault_exp, config_path=cfg_file)
    with TestClient(app_explicit) as client_exp:
        h3 = client_exp.get("/api/health").json()
        assert h3["vault_dir"] == vault_exp.resolve().as_posix()


def test_multiple_app_instances_isolated_without_global_leakage(
    seeded_vault: Path, tmp_path: Path
) -> None:
    second_vault = tmp_path / "second_empty_vault"
    initialize_vault(second_vault, seed_questions=True, init_git=False)

    with (
        TestClient(create_app(vault_dir=seeded_vault)) as client_a,
        TestClient(create_app(vault_dir=second_vault)) as client_b,
    ):
        pa = client_a.get("/api/portfolio").json()
        pb = client_b.get("/api/portfolio").json()
        assert len(pa["opportunities"]) == 4
        assert len(pb["opportunities"]) == 0

        pin = client_a.post("/api/boss-pin", json={"id": "OPP-002"})
        assert pin.status_code == 200
        assert client_b.get("/api/portfolio").json()["mandate_weeks"] == 0.0


def test_zero_stale_cache_reflects_external_disk_edits_immediately(
    seeded_vault: Path,
) -> None:
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        before = client.get("/api/portfolio").json()
        opp1_before = next(o for o in before["opportunities"] if o["id"] == "OPP-001")
        assert opp1_before["effort_weeks"] == 2.5
        assert client.get("/api/staging").json()["count"] == 2

        # External edit directly on disk (simulating VS Code / Obsidian / Claude Code)
        card_path = seeded_vault / "opportunities" / "OPP-001-lead-time-quantile.md"
        parsed = parse_markdown_card(card_path)
        fm = dict(parsed.frontmatter)
        fm["title"] = "Updated Live From Obsidian"
        fm["effort_weeks"] = 5.0
        upsert_markdown_card(
            card_path,
            OpportunitySpec.model_validate(fm),
            parsed.body,
            human_notes_override=parsed.human_notes,
        )

        # Drop a new staged spec directly on disk
        opp_007 = OpportunitySpec(
            id="OPP-007",
            title="Live Dropped Staged Opportunity",
            status="staging",
            valuation=_make_valuation(),
            effort_weeks=2.0,
            data_readiness_score=4,
            adoption_friction_score=2,
        )
        upsert_markdown_card(
            seeded_vault / "staging" / "opportunities" / "OPP-007-live.md",
            opp_007,
            "## Live Staged Spec",
        )

        after = client.get("/api/portfolio").json()
        opp1_after = next(o for o in after["opportunities"] if o["id"] == "OPP-001")
        assert opp1_after["title"] == "Updated Live From Obsidian"
        assert opp1_after["effort_weeks"] == 5.0
        assert after["staging_count"] == 3
        assert client.get("/api/staging").json()["count"] == 3


def test_empty_and_uninitialized_vault_endpoints_return_200_without_disk_writes(
    tmp_path: Path,
) -> None:
    ghost_vault = tmp_path / "uninitialized_vault"
    with TestClient(create_app(vault_dir=ghost_vault)) as client:
        r_health = client.get("/api/health")
        assert r_health.status_code == 200
        assert r_health.json()["status"] == "ok"
        assert r_health.json()["vault_exists"] is False

        r_port = client.get("/api/portfolio")
        assert r_port.status_code == 200
        assert r_port.json()["opportunities"] == []
        assert r_port.json()["used_capacity_weeks"] == 0.0

        r_stag = client.get("/api/staging")
        assert r_stag.status_code == 200
        assert r_stag.json()["count"] == 0

        r_lin = client.get("/api/lineage")
        assert r_lin.status_code == 200
        assert r_lin.json() == {
            "nodes": [],
            "edges": [],
            "focus_id": None,
            "matched_focus_id": None,
            "summary": {"node_count": 0, "edge_count": 0, "by_type": {}},
            "warnings": [],
        }

        for q_route in ("/api/questions", "/api/readiness"):
            rq = client.get(q_route)
            assert rq.status_code == 200
            assert rq.json()["questions"] == []
            assert rq.json()["phases"] == []
            assert rq.json()["summary"]["readiness_pct"] == 0.0

        rb = client.get("/api/brief")
        assert rb.status_code == 200
        assert rb.json()["brief_type"] == "weekly_status"

        rv = client.get("/api/validate")
        assert rv.status_code == 200
        assert rv.json()["valid"] is False

    assert not ghost_vault.exists()
    assert not (tmp_path / "exports").exists()


# ---------------------------------------------------------------------------
# 2. Static SPA Serving, R5 Air-Gap & Structural XSS Immunity
# ---------------------------------------------------------------------------


def test_spa_root_and_static_assets_served_with_security_headers(
    seeded_vault: Path,
) -> None:
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        root = client.get("/")
        assert root.status_code == 200
        assert "text/html" in root.headers["content-type"]
        assert root.headers["x-content-type-options"] == "nosniff"
        assert "default-src 'self'" in root.headers["content-security-policy"]
        html = root.text
        assert html.count("<!DOCTYPE html>") == 1
        html_ids = re.findall(r'\bid="([^"]+)"', html)
        assert len(html_ids) == len(set(html_ids))
        for expected_id in (
            "tab-portfolio",
            "tab-staging",
            "tab-lineage",
            "tab-readiness",
            "tab-briefs",
            "panel-portfolio",
            "panel-staging",
            "panel-lineage",
            "panel-readiness",
            "panel-briefs",
            "roi-matrix-svg",
            "lineage-svg",
        ):
            assert expected_id in html
        assert "<svg" in html
        assert 'data-theme="dark"' in html

        css = client.get("/static/styles.css")
        assert css.status_code == 200
        assert "text/css" in css.headers["content-type"]

        js = client.get("/static/app.js")
        assert js.status_code == 200
        assert "javascript" in js.headers["content-type"]

        missing = client.get("/static/nonexistent-asset.js")
        assert missing.status_code == 404


def test_r5_airgap_zero_external_urls_and_no_node_chain() -> None:
    static_dir = REPO_ROOT / "poopdeck" / "ui" / "static"
    static_files = sorted(static_dir.glob("*"))
    assert [f.name for f in static_files] == ["app.js", "index.html", "styles.css"]

    for sf in static_files:
        content = sf.read_text(encoding="utf-8")
        assert re.findall(r"https?://", content, flags=re.IGNORECASE) == [], (
            f"External URL substring found in {sf.name}"
        )
        assert "@import" not in content.lower()
        assert "graph.microsoft.com" not in content.lower()
        assert "webhook.office.com" not in content.lower()

    assert not (REPO_ROOT / "package.json").exists()
    assert not (REPO_ROOT / "node_modules").exists()


def test_static_assets_structural_xss_sink_prohibition() -> None:
    static_dir = REPO_ROOT / "poopdeck" / "ui" / "static"
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    js = (static_dir / "app.js").read_text(encoding="utf-8")

    prohibited_tokens = (
        "inner" + "HTML",
        "outer" + "HTML",
        "insertAdjacent" + "HTML",
        "document.write",
        "eval(",
        "new Function(",
    )
    for token in prohibited_tokens:
        assert token not in html, f"Prohibited token {token!r} in index.html"
        assert token not in js, f"Prohibited token {token!r} in app.js"

    # Zero inline scripts or inline on*= event attributes in index.html
    script_tags = re.findall(r"<script\b[^>]*>(.*?)</script>", html, flags=re.DOTALL)
    assert script_tags == [""]
    assert re.findall(r"\son[a-z]+\s*=", html, flags=re.IGNORECASE) == []

    for required_safe_api in (
        "createElement",
        "createElementNS",
        "createTextNode",
        "textContent",
        "replaceChildren",
    ):
        assert required_safe_api in js


def test_xss_payloads_and_dax_operators_roundtrip_safely(seeded_vault: Path) -> None:
    xss_title = '<script>alert("xss")</script><img src=x onerror=alert(1)>'
    dax_note = 'IF([Days_Of_Supply] < 14 && [Gross_Margin] > 0, "BUY", "HOLD")'

    evil_opp = OpportunitySpec(
        id="OPP-099",
        title=xss_title,
        status="staging",
        valuation=_make_valuation(),
        effort_weeks=2.0,
        data_readiness_score=3,
        adoption_friction_score=2,
    )
    upsert_markdown_card(
        seeded_vault / "staging" / "opportunities" / "OPP-099-xss-probe.md",
        evil_opp,
        f"## DAX Probe\n\n`{dax_note}`",
        human_notes_override=f"\n{xss_title} | {dax_note}\n",
    )

    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        index_html = client.get("/").text
        assert xss_title not in index_html

        st_resp = client.get("/api/staging")
        assert st_resp.status_code == 200
        assert "application/json" in st_resp.headers["content-type"]
        opp99 = next(i for i in st_resp.json()["items"] if i["id"] == "OPP-099")
        assert opp99["title"] == xss_title
        assert dax_note in opp99["body"]

        q_resp = client.patch(
            "/api/questions/Q-MGR-01",
            json={"status": "answered", "answer": f"{xss_title} & {dax_note}"},
        )
        assert q_resp.status_code == 200
        assert q_resp.json()["answer"] == f"{xss_title} & {dax_note}"

        b_resp = client.get("/api/brief/OPP-099")
        assert b_resp.status_code == 200
        assert xss_title in b_resp.json()["markdown"]


def test_css_dark_light_theme_tokens_and_responsive_rules() -> None:
    css = (REPO_ROOT / "poopdeck" / "ui" / "static" / "styles.css").read_text(
        encoding="utf-8"
    )
    for token in (
        '[data-theme="dark"]',
        '[data-theme="light"]',
        "color-scheme",
        "--font-sans",
        "--font-mono",
        ":focus-visible",
        "::after",
        "@media",
    ):
        assert token in css
    assert "scrollbar-color" not in css


def test_js_svg_edge_guards_and_clipboard_fallback_present() -> None:
    js = (REPO_ROOT / "poopdeck" / "ui" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    for token in (
        "Number.isFinite",
        "over_capacity",
        "exists_in_vault",
        "navigator.clipboard",
        "execCommand",
    ):
        assert token in js


# ---------------------------------------------------------------------------
# 3. Portfolio Schedule, Cutline Simulator & Boss-Mandate Displacement
# ---------------------------------------------------------------------------


def test_get_portfolio_dual_nested_and_flat_schedule_contract(
    seeded_vault: Path,
) -> None:
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        resp = client.get("/api/portfolio")
        assert resp.status_code == 200
        data = resp.json()

        assert data["capacity_weeks"] == 12.0
        assert data["used_capacity_weeks"] == 10.0
        assert data["mandate_weeks"] == 0.0
        assert data["remaining_capacity_weeks"] == 2.0
        assert data["overflow_weeks"] == 0.0
        assert data["over_capacity"] is False
        assert data["over_capacity_warning"] is False
        assert data["funded_ids"] == ["OPP-001", "OPP-003", "OPP-004"]
        assert data["displaced_ids"] == []
        assert data["staging_count"] == 2
        assert "ops_supply_chain" in data["available_domains"]

        assert data["opportunities"] == data["items"]
        assert len(data["opportunities"]) == 4
        assert data["schedule"]["funded_ids"] == ["OPP-001", "OPP-003", "OPP-004"]
        assert data["cutline_summary"]["funded_count"] == 3

        opp4 = next(o for o in data["opportunities"] if o["id"] == "OPP-004")
        assert opp4["unverified_drivers_count"] == 1
        assert len(opp4["verification_prompts"]) == 1
        assert opp4["verification_prompts"] == opp4["verify_questions"]
        assert opp4["valuation"]["verify_prompts"] == opp4["verification_prompts"]


def test_get_portfolio_capacity_weeks_override_changes_cutline(
    seeded_vault: Path,
) -> None:
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        data = client.get("/api/portfolio?capacity_weeks=6.0").json()
        assert data["capacity_weeks"] == 6.0
        assert data["funded_ids"] == ["OPP-001"]
        assert data["used_capacity_weeks"] == 2.5
        assert data["remaining_capacity_weeks"] == 3.5


def test_get_portfolio_domain_and_include_archived_filters(seeded_vault: Path) -> None:
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        fin = client.get("/api/portfolio?domain=finance_risk").json()
        assert [o["id"] for o in fin["opportunities"]] == ["OPP-002"]

        ops = client.get("/api/portfolio?domain=ops_supply_chain").json()
        assert [o["id"] for o in ops["opportunities"]] == [
            "OPP-001",
            "OPP-003",
            "OPP-004",
        ]

        # Archive OPP-003 and verify include_archived=false vs true
        arch = client.post(
            "/api/archive", json={"id": "OPP-003", "reason": "Deferred to Q3"}
        )
        assert arch.status_code == 200

        without_arch = client.get("/api/portfolio?include_archived=false").json()
        assert "OPP-003" not in [o["id"] for o in without_arch["opportunities"]]

        with_arch = client.get("/api/portfolio?include_archived=true").json()
        ids = [o["id"] for o in with_arch["opportunities"]]
        assert "OPP-003" in ids
        archived_item = next(
            o for o in with_arch["opportunities"] if o["id"] == "OPP-003"
        )
        assert archived_item["status"] == "archived"
        assert archived_item["above_cutline"] is False
        assert archived_item["displaced_by_mandate"] is False
        assert archived_item["rank"] is None
        assert "OPP-003" not in with_arch["funded_ids"]


def test_post_boss_pin_toggle_none_and_explicit_bool_with_id_aliases(
    seeded_vault: Path,
) -> None:
    card_path = seeded_vault / "opportunities" / "OPP-002-exec-board-deck-sim.md"
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        # Omitted boss_mandate toggles False -> True
        r1 = client.post("/api/boss-pin", json={"id": "OPP-002"})
        assert r1.status_code == 200
        assert r1.json()["boss_mandate"] is True
        assert parse_markdown_card(card_path).frontmatter["boss_mandate"] is True

        # Explicit False via opp_id alias sets True -> False
        r2 = client.post(
            "/api/boss-pin", json={"opp_id": "OPP-002", "boss_mandate": False}
        )
        assert r2.status_code == 200
        assert r2.json()["boss_mandate"] is False
        assert parse_markdown_card(card_path).frontmatter["boss_mandate"] is False

        # Pinned alias sets False -> True
        r3 = client.post("/api/boss-pin", json={"id": "OPP-002", "pinned": True})
        assert r3.status_code == 200
        assert r3.json()["boss_mandate"] is True


def test_post_boss_pin_computes_exact_cutline_displacement(seeded_vault: Path) -> None:
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        res = client.post(
            "/api/boss-pin",
            json={"id": "OPP-002", "boss_mandate": True, "capacity_weeks": 12.0},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["mandate_weeks"] == 4.5
        assert body["used_capacity_weeks"] == 11.0
        assert body["remaining_capacity_weeks"] == 1.0
        assert body["over_capacity"] is False
        assert body["funded_ids"] == ["OPP-002", "OPP-001", "OPP-003"]
        assert body["displaced_ids"] == ["OPP-004"]
        assert body["displaced_titles"] == ["Dynamic Stockout Risk & Reorder Alerting"]

        opp4 = next(o for o in body["portfolio"]["opportunities"] if o["id"] == "OPP-004")
        assert opp4["baseline_above_cutline"] is True
        assert opp4["above_cutline"] is False
        assert opp4["displaced_by_mandate"] is True
        assert opp4["displaced"] is True


def test_post_boss_pin_over_capacity_clamps_remaining_to_zero(
    seeded_vault: Path,
) -> None:
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        res = client.post(
            "/api/boss-pin",
            json={"id": "OPP-002", "boss_mandate": True, "capacity_weeks": 3.0},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["over_capacity"] is True
        assert body["over_capacity_warning"] is True
        assert body["remaining_capacity_weeks"] == 0.0
        assert body["overflow_weeks"] == 1.5
        assert body["funded_ids"] == ["OPP-002"]
        assert "OPP-001" in body["displaced_ids"]


def test_single_opportunity_portfolio_finite_metrics(tmp_path: Path) -> None:
    vdir = tmp_path / "single_vault"
    initialize_vault(vdir, seed_questions=False, init_git=False)
    solo = OpportunitySpec(
        id="OPP-101",
        title="Solo Opportunity",
        status="active",
        valuation=_make_valuation(),
        effort_weeks=3.0,
        data_readiness_score=4,
        adoption_friction_score=2,
    )
    upsert_markdown_card(vdir / "opportunities" / "OPP-101-solo.md", solo, "## Solo")

    with TestClient(create_app(vault_dir=vdir)) as client:
        data = client.get("/api/portfolio").json()
        assert len(data["opportunities"]) == 1
        for key in (
            "capacity_weeks",
            "used_capacity_weeks",
            "mandate_weeks",
            "remaining_capacity_weeks",
            "overflow_weeks",
        ):
            assert math.isfinite(data[key])


def test_boss_pin_preserves_human_notes_byte_for_byte_and_staged_pin_stays_staged(
    seeded_vault: Path,
) -> None:
    opp2_path = seeded_vault / "opportunities" / "OPP-002-exec-board-deck-sim.md"
    original_notes = parse_markdown_card(opp2_path).human_notes

    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        assert client.post("/api/boss-pin", json={"id": "OPP-002"}).status_code == 200
        assert parse_markdown_card(opp2_path).human_notes == original_notes

        assert client.post("/api/boss-pin", json={"id": "OPP-002"}).status_code == 200
        assert parse_markdown_card(opp2_path).human_notes == original_notes

        # Pinning a staged-only spec updates in place without bypassing staging gate
        st_pin = client.post(
            "/api/boss-pin", json={"id": "OPP-005", "boss_mandate": True}
        )
        assert st_pin.status_code == 200
        assert st_pin.json()["opportunity"]["status"] == "staging"
        assert "OPP-005" not in st_pin.json()["funded_ids"]

    assert validate_vault(seeded_vault, strict=True).valid is True


# ---------------------------------------------------------------------------
# 4. Staging Gate, Skeptical CFO Review, Promote & Archive
# ---------------------------------------------------------------------------


def test_get_staging_exposes_verdict_critique_verify_prompts_and_notes(
    seeded_vault: Path,
) -> None:
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        resp = client.get("/api/staging")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        assert data["items"] == data["opportunities"]

        opp5 = next(i for i in data["items"] if i["id"] == "OPP-005")
        assert opp5["cfo_critique_verdict"] == "READY_FOR_REVIEW"
        assert "Challenge gross-to-net recovery" in opp5["cfo_critique"]
        assert len(opp5["verification_prompts"]) == 1
        assert opp5["verification_prompts"][0].startswith("[VERIFY WITH STAKEHOLDER]")
        assert opp5["verify_prompts"] == opp5["verification_prompts"]
        assert "driver_formula" in opp5["valuation"]
        assert "IF([Shrink] < 0 && [Cost] > 50)" in opp5["human_notes"]
        assert opp5["frontmatter"]["id"] == "OPP-005"


def test_post_promote_moves_card_to_opportunities_and_preserves_notes(
    seeded_vault: Path,
) -> None:
    staged_file = seeded_vault / "staging" / "opportunities" / "OPP-005-shrink-anomaly.md"
    before_parsed = parse_markdown_card(staged_file)

    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        res = client.post("/api/promote", json={"id": "OPP-005"})
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "promoted"
        assert body["new_status"] == "backlog"
        assert "staging/opportunities/" in body["source_path"]
        assert "/opportunities/" in body["target_path"]
        assert not staged_file.exists()

        dest_file = Path(body["target_path"])
        assert dest_file.is_file()
        after_parsed = parse_markdown_card(dest_file)
        assert after_parsed.human_notes == before_parsed.human_notes
        assert "## Skeptical CFO Critique" in after_parsed.body
        assert after_parsed.body == before_parsed.body

        assert client.get("/api/staging").json()["count"] == 1
        port_ids = [o["id"] for o in client.get("/api/portfolio").json()["opportunities"]]
        assert "OPP-005" in port_ids

        # Second promote on already-promoted card returns 404
        dup = client.post("/api/promote", json={"id": "OPP-005"})
        assert dup.status_code == 404


def test_post_promote_supports_opp_id_alias_and_active_target_status(
    seeded_vault: Path,
) -> None:
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        res = client.post(
            "/api/promote",
            json={"opp_id": "OPP-006", "target_status": "active"},
        )
        assert res.status_code == 200
        assert res.json()["new_status"] == "active"
        assert res.json()["opportunity"]["status"] == "active"


def test_post_promote_action_archive_unified_handler_support(
    seeded_vault: Path,
) -> None:
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        res = client.post(
            "/api/promote",
            json={"id": "OPP-006", "action": "archive", "reason": "Handled by DAX"},
        )
        assert res.status_code == 200
        assert res.json()["status"] == "archived"
        assert res.json()["new_status"] == "archived"


def test_post_promote_rejects_staging_and_archived_without_action_with_422(
    seeded_vault: Path,
) -> None:
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        for bad_status in ("staging", "archived", "nonexistent_status"):
            res = client.post(
                "/api/promote",
                json={"id": "OPP-005", "target_status": bad_status},
            )
            assert res.status_code == 422


def test_post_archive_from_staging_and_active_with_reason_and_notes(
    seeded_vault: Path,
) -> None:
    st_path = seeded_vault / "staging" / "opportunities" / "OPP-006-late-po-view.md"
    st_notes = parse_markdown_card(st_path).human_notes

    act_path = seeded_vault / "opportunities" / "OPP-003-safety-stock-optimizer.md"
    act_notes = parse_markdown_card(act_path).human_notes

    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        r_stag = client.post(
            "/api/archive",
            json={"id": "OPP-006", "reason": "DAX rule covers 80%"},
        )
        assert r_stag.status_code == 200
        dest6 = Path(r_stag.json()["target_path"])
        parsed6 = parse_markdown_card(dest6)
        assert parsed6.frontmatter["status"] == "archived"
        assert "## Archive Reason\n\nDAX rule covers 80%" in parsed6.body
        assert parsed6.human_notes == st_notes

        r_act = client.post(
            "/api/archive",
            json={"opp_id": "OPP-003", "reason": "Deprioritized"},
        )
        assert r_act.status_code == 200
        parsed3 = parse_markdown_card(act_path)
        assert parsed3.frontmatter["status"] == "archived"
        assert parsed3.human_notes == act_notes
        assert "OPP-003" not in client.get("/api/portfolio").json()["funded_ids"]


def test_promote_archive_and_boss_pin_missing_opp_returns_404(
    seeded_vault: Path,
) -> None:
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        for endpoint in ("/api/promote", "/api/archive", "/api/boss-pin"):
            res = client.post(endpoint, json={"id": "OPP-999"})
            assert res.status_code == 404


# ---------------------------------------------------------------------------
# 5. Interactive Lineage Graph
# ---------------------------------------------------------------------------


def test_get_lineage_returns_all_six_node_types_and_valid_edges(
    seeded_vault: Path,
) -> None:
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        res = client.get("/api/lineage")
        assert res.status_code == 200
        data = res.json()
        node_ids = {n["id"] for n in data["nodes"]}
        node_types = {n["type"] for n in data["nodes"]}
        assert node_types == {
            "external_data",
            "dataset",
            "opportunity",
            "existing_report",
            "new_report",
            "stakeholder",
        }
        for n in data["nodes"]:
            assert n["exists"] == n["exists_in_vault"]
        for edge in data["edges"]:
            assert edge["source"] in node_ids
            assert edge["target"] in node_ids


def test_get_lineage_include_staging_false_excludes_staged_nodes(
    seeded_vault: Path,
) -> None:
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        with_st = {n["id"] for n in client.get("/api/lineage?include_staging=true").json()["nodes"]}
        assert "opportunity:OPP-005" in with_st
        assert "opportunity:OPP-006" in with_st

        no_st = {n["id"] for n in client.get("/api/lineage?include_staging=false").json()["nodes"]}
        assert "opportunity:OPP-005" not in no_st
        assert "opportunity:OPP-006" not in no_st
        assert "opportunity:OPP-001" in no_st


def test_get_lineage_unresolved_wikilink_emits_placeholder_node(
    seeded_vault: Path,
) -> None:
    opp_dangling = OpportunitySpec(
        id="OPP-050",
        title="Legacy ERP Bridge",
        status="backlog",
        internal_datasets=[
            InternalDatasetUse(ref="[[legacy_erp.missing_table]]", role="source")
        ],
        valuation=_make_valuation(),
        effort_weeks=2.0,
        data_readiness_score=2,
        adoption_friction_score=3,
    )
    upsert_markdown_card(
        seeded_vault / "opportunities" / "OPP-050-legacy-bridge.md",
        opp_dangling,
        "## Legacy Bridge",
    )

    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        data = client.get("/api/lineage").json()
        placeholder = next(
            n for n in data["nodes"] if "legacy_erp.missing_table" in n["id"]
        )
        assert placeholder["exists_in_vault"] is False
        assert placeholder["exists"] is False


def test_get_lineage_focus_id_query_parameter_and_unmatched_200(
    seeded_vault: Path,
) -> None:
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        focused = client.get("/api/lineage?focus_id=Daily%20Buyer%20Workbench")
        assert focused.status_code == 200
        fdata = focused.json()
        assert (
            fdata["matched_focus_id"]
            == "existing_report:Supply Chain Workspace__Daily Buyer Workbench"
        )
        f_ids = {n["id"] for n in fdata["nodes"]}
        # Report cone includes upgrading opportunities, upstream datasets, and report owner
        assert "opportunity:OPP-001" in f_ids
        assert "opportunity:OPP-003" in f_ids
        assert "opportunity:OPP-004" in f_ids
        assert "dataset:prod_catalog.silver.inventory_snapshots" in f_ids
        assert "stakeholder:sarah-jenkins" in f_ids
        # Unrelated finance-only OPP-002 does not supplement Daily Buyer Workbench
        assert "opportunity:OPP-002" not in f_ids
        focused_node = next(
            n for n in fdata["nodes"] if n["id"] == fdata["matched_focus_id"]
        )
        assert focused_node["focused"] is True

        unmatched = client.get("/api/lineage?focus_id=totally_unknown_node")
        assert unmatched.status_code == 200
        assert unmatched.json()["nodes"] == []
        assert unmatched.json()["edges"] == []
        assert unmatched.json()["matched_focus_id"] is None


# ---------------------------------------------------------------------------
# 6. Day 1-90 Playbook Readiness & Open Questions Tracker
# ---------------------------------------------------------------------------


def test_get_questions_and_readiness_alias_return_15_questions_and_5_phases(
    seeded_vault: Path,
) -> None:
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        q_data = client.get("/api/questions").json()
        r_data = client.get("/api/readiness").json()
        assert q_data == r_data
        assert len(q_data["questions"]) == 15
        assert len(q_data["phases"]) == 5
        assert q_data["phases"] == q_data["playbook_phases"]
        assert q_data["summary"]["total_questions"] == 15
        assert q_data["summary"]["total_phases"] == 5

        first_q = q_data["questions"][0]
        assert first_q["owner"] == first_q["who_to_ask"]
        assert first_q["prompt"] == first_q["question"]
        assert "answer" in first_q
        assert "items" in q_data["phases"][0]


def test_get_questions_category_normalization_and_status_filters(
    seeded_vault: Path,
) -> None:
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        mgr = client.get("/api/questions?category=manager").json()
        assert len(mgr["questions"]) == 4
        assert all(q["category"] == "manager" for q in mgr["questions"])

        it_sec = client.get("/api/questions?category=IT/Security").json()
        assert len(it_sec["questions"]) == 3
        assert all(q["category"] == "it_security" for q in it_sec["questions"])

        bad_status = client.get("/api/questions?status=not_valid")
        assert bad_status.status_code == 422


def test_patch_and_post_questions_path_and_body_id_with_aliases(
    seeded_vault: Path,
) -> None:
    q_path = seeded_vault / "questions" / "open-questions.md"
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        r1 = client.patch(
            "/api/questions/Q-MGR-01",
            json={"status": "answered", "answer": "Fill rate is North Star KPI."},
        )
        assert r1.status_code == 200
        assert r1.json()["status"] == "answered"
        assert r1.json()["answer_summary"] == "Fill rate is North Star KPI."

        r2 = client.post(
            "/api/questions/Q-IT-02",
            json={"status": "blocked", "answer_summary": "Awaiting SPN ticket."},
        )
        assert r2.status_code == 200
        assert r2.json()["status"] == "blocked"

        # Body-ID route variant
        r3 = client.post(
            "/api/questions",
            json={"id": "Q-DE-01", "status": "answered", "notes": "Gold runs at 05:00."},
        )
        assert r3.status_code == 200
        assert r3.json()["id"] == "Q-DE-01"
        assert r3.json()["answer"] == "Gold runs at 05:00."

        answered = client.get("/api/questions?status=answered").json()["questions"]
        assert {q["id"] for q in answered} == {"Q-MGR-01", "Q-DE-01"}

        disk_parsed = parse_markdown_card(q_path)
        assert "Fill rate is North Star KPI." in disk_parsed.body


def test_patch_and_post_readiness_phase_completion_updates_progress(
    seeded_vault: Path,
) -> None:
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        p1 = client.patch("/api/readiness/PHASE-1", json={"completed": True})
        assert p1.status_code == 200
        assert p1.json()["completed"] is True
        assert p1.json()["summary"]["completed_phases"] == 1
        assert p1.json()["summary"]["playbook_progress_pct"] == 20.0

        p2 = client.post("/api/questions/PHASE-2", json={"status": "completed"})
        assert p2.status_code == 200
        assert p2.json()["completed"] is True
        assert p2.json()["summary"]["completed_phases"] == 2
        assert p2.json()["summary"]["playbook_progress_pct"] == 40.0

        # Body-ID readiness route
        p1_reopen = client.patch(
            "/api/readiness", json={"phase_id": "PHASE-1", "completed": False}
        )
        assert p1_reopen.status_code == 200
        assert p1_reopen.json()["completed"] is False
        assert p1_reopen.json()["summary"]["completed_phases"] == 1


def test_questions_and_playbook_mutations_preserve_human_notes_byte_for_byte(
    seeded_vault: Path,
) -> None:
    q_file = seeded_vault / "questions" / "open-questions.md"
    pb_file = seeded_vault / "questions" / "day1-90-playbook.md"

    q_parsed = parse_markdown_card(q_file)
    custom_q_notes = "\n  1:1 Notes with [[Sarah Jenkins]] — preserve\ttabs & trailing spaces!   \n"
    upsert_markdown_card(
        q_file, q_parsed.frontmatter, q_parsed.body, human_notes_override=custom_q_notes
    )

    pb_parsed = parse_markdown_card(pb_file)
    custom_pb_notes = "\n  Playbook reflection: Week 1 check-in complete.  \n"
    upsert_markdown_card(
        pb_file,
        pb_parsed.frontmatter,
        pb_parsed.body,
        human_notes_override=custom_pb_notes,
    )

    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        assert (
            client.patch(
                "/api/questions/Q-FIN-01",
                json={"status": "answered", "answer": "8% hurdle rate"},
            ).status_code
            == 200
        )
        assert (
            client.patch("/api/readiness/PHASE-3", json={"completed": True}).status_code
            == 200
        )

    assert parse_markdown_card(q_file).human_notes == custom_q_notes
    assert parse_markdown_card(pb_file).human_notes == custom_pb_notes
    assert validate_vault(seeded_vault, strict=True).valid is True


def test_questions_and_readiness_unknown_id_404_and_invalid_mutations_422(
    seeded_vault: Path,
) -> None:
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        assert (
            client.patch(
                "/api/questions/Q-DOES-NOT-EXIST", json={"status": "answered"}
            ).status_code
            == 404
        )
        # Empty update payload -> 422
        assert client.patch("/api/questions/Q-MGR-01", json={}).status_code == 422
        # Bogus status enum -> 422
        assert (
            client.patch(
                "/api/questions/Q-MGR-01", json={"status": "bogus_status"}
            ).status_code
            == 422
        )
        # Phase-only status "completed" on QuestionItem -> 422
        assert (
            client.patch(
                "/api/questions/Q-MGR-01", json={"status": "completed"}
            ).status_code
            == 422
        )
        # Question-only status "blocked" or answer-only on PlaybookPhase -> 422
        assert (
            client.patch(
                "/api/readiness/PHASE-1", json={"status": "blocked"}
            ).status_code
            == 422
        )
        assert (
            client.patch(
                "/api/readiness/PHASE-1", json={"answer": "Not applicable"}
            ).status_code
            == 422
        )
        # Path vs body ID mismatch -> 422
        assert (
            client.patch(
                "/api/questions/Q-MGR-01",
                json={"id": "Q-MGR-02", "status": "answered"},
            ).status_code
            == 422
        )
        # Missing item_id on body-only route -> 422
        assert (
            client.patch("/api/questions", json={"status": "answered"}).status_code
            == 422
        )


def test_concurrent_threadpool_question_and_boss_pin_mutations_lose_zero_updates(
    seeded_vault: Path,
) -> None:
    app_instance = create_app(vault_dir=seeded_vault)
    targets = [
        "Q-MGR-01",
        "Q-MGR-02",
        "Q-MGR-03",
        "Q-IT-01",
        "Q-IT-02",
        "Q-DE-01",
        "Q-PBI-01",
        "Q-FIN-01",
    ]

    def _worker(qid: str) -> int:
        with TestClient(app_instance) as local_client:
            r = local_client.patch(
                f"/api/questions/{qid}",
                json={"status": "answered", "answer": f"Confirmed {qid}"},
            )
            return r.status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(_worker, targets))

    assert statuses == [200] * len(targets)
    with TestClient(app_instance) as check_client:
        answered = check_client.get("/api/questions?status=answered").json()["questions"]
        assert {q["id"] for q in answered} == set(targets)


def test_corrupt_markdown_card_in_vault_emits_warnings_without_500(
    seeded_vault: Path,
) -> None:
    broken_file = seeded_vault / "opportunities" / "OPP-BROKEN-corrupt.md"
    broken_file.write_text(
        "---\nid: [unclosed yaml\n---\nBroken card body\n",
        encoding="utf-8",
        newline="\n",
    )

    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        r_port = client.get("/api/portfolio")
        assert r_port.status_code == 200
        assert len(r_port.json()["opportunities"]) == 4
        assert any("OPP-BROKEN-corrupt.md" in w for w in r_port.json()["warnings"])

        r_stag = client.get("/api/staging")
        assert r_stag.status_code == 200
        assert r_stag.json()["count"] == 2

        r_lin = client.get("/api/lineage")
        assert r_lin.status_code == 200
        assert len(r_lin.json()["nodes"]) > 0
        assert any("OPP-BROKEN-corrupt.md" in w for w in r_lin.json()["warnings"])

        r_brief = client.get("/api/brief")
        assert r_brief.status_code == 200
        assert "OPP-001" in r_brief.json()["markdown"]
        assert any(
            "OPP-BROKEN-corrupt.md" in w
            for w in r_brief.json().get("warnings", [])
        )

        r_broken_target = client.get("/api/brief/OPP-BROKEN")
        assert r_broken_target.status_code == 422


# ---------------------------------------------------------------------------
# 7. Executive, Microsoft Teams & Stakeholder [VERIFY] Brief Generation
# ---------------------------------------------------------------------------


def test_get_and_post_opportunity_brief_teams_and_exec_modes(
    seeded_vault: Path,
) -> None:
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        g_teams = client.get("/api/brief/OPP-004?format=teams")
        assert g_teams.status_code == 200
        md_teams = g_teams.json()["markdown"]
        assert "## [OPP-004]" in md_teams
        assert "annual_volume * unit_margin_usd * lift_pct" in md_teams
        assert "[VERIFY WITH STAKEHOLDER]" in md_teams
        assert "Daily Buyer Workbench" in md_teams
        assert g_teams.json()["saved_path"] is None

        g_exec = client.get("/api/brief?opp_id=OPP-004&format=exec")
        assert g_exec.status_code == 200
        assert "# Executive Opportunity Brief — OPP-004" in g_exec.json()["markdown"]

        p_alias = client.post(
            "/api/brief",
            json={"id": "OPP-005", "type": "opportunity", "fmt": "teams"},
        )
        assert p_alias.status_code == 200
        assert "STAGING (UNPROMOTED CANDIDATE)" in p_alias.json()["markdown"]


def test_weekly_status_and_verify_questions_briefs_reflect_live_vault(
    seeded_vault: Path,
) -> None:
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        client.post(
            "/api/boss-pin", json={"id": "OPP-002", "boss_mandate": True}
        )

        w_get = client.get("/api/brief/weekly")
        assert w_get.status_code == 200
        assert w_get.json()["brief_type"] == "weekly_status"
        assert "OPP-004" in w_get.json()["markdown"]

        w_post = client.post("/api/brief", json={"brief_type": "weekly_status"})
        assert w_post.status_code == 200
        assert "Displaced by Boss Mandate" in w_post.json()["markdown"]

        v_all = client.post("/api/brief", json={"brief_type": "verify_questions"})
        assert v_all.status_code == 200
        assert v_all.json()["brief_type"] == "verify_questions"
        assert "Q-MGR-01" in v_all.json()["markdown"]

        v_one = client.get("/api/brief?brief_type=verify_questions&opp_id=OPP-004")
        assert v_one.status_code == 200
        assert v_one.json()["opp_id"] == "OPP-004"
        assert "[VERIFY WITH STAKEHOLDER]" in v_one.json()["markdown"]


def test_post_brief_save_export_false_vs_true_sandboxed_output(
    seeded_vault: Path,
) -> None:
    exports_dir = seeded_vault.parent / "exports"
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        r_no = client.post(
            "/api/brief",
            json={"opp_id": "OPP-001", "brief_type": "opportunity", "save_export": False},
        )
        assert r_no.status_code == 200
        assert r_no.json()["saved_path"] is None
        assert not exports_dir.exists()

        r_yes = client.post(
            "/api/brief",
            json={"opp_id": "OPP-001", "brief_type": "opportunity", "save_export": True},
        )
        assert r_yes.status_code == 200
        saved_str = r_yes.json()["saved_path"]
        assert saved_str is not None
        saved_file = Path(saved_str)
        assert saved_file.is_file()
        assert saved_file.resolve().is_relative_to(exports_dir.resolve())
        raw_bytes = saved_file.read_bytes()
        assert b"\r\n" not in raw_bytes


def test_brief_unknown_opportunity_404_and_empty_post_422(
    seeded_vault: Path,
) -> None:
    exports_dir = seeded_vault.parent / "exports"
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        assert client.get("/api/brief/OPP-404").status_code == 404
        r_missing = client.post(
            "/api/brief",
            json={"opp_id": "OPP-404", "brief_type": "opportunity", "save_export": True},
        )
        assert r_missing.status_code == 404
        assert not exports_dir.exists()

        assert client.post("/api/brief", json={}).status_code == 422
        assert client.get("/api/brief?format=pdf").status_code == 422


# ---------------------------------------------------------------------------
# 8. Security Boundaries, Type Validation & Workspace Cleanliness
# ---------------------------------------------------------------------------


def test_path_traversal_and_malicious_identifiers_rejected(
    seeded_vault: Path,
) -> None:
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        malicious_ids = [
            "../../pyproject.toml",
            "..\\secret",
            "OPP-001/../../leak",
            "   ",
            "OPP\x00001",
        ]
        for bad_id in malicious_ids:
            for endpoint in ("/api/promote", "/api/archive", "/api/boss-pin"):
                r = client.post(endpoint, json={"id": bad_id})
                assert r.status_code in (400, 404, 422)
            rb = client.post(
                "/api/brief",
                json={"opp_id": bad_id, "brief_type": "opportunity", "save_export": True},
            )
            assert rb.status_code in (400, 404, 422)
            rq = client.patch(
                "/api/questions",
                json={"id": bad_id, "status": "answered"},
            )
            assert rq.status_code in (400, 404, 422)

        assert client.get("/api/brief/..%2F..%2Fpyproject.toml").status_code in (
            400,
            404,
            422,
        )
        assert client.get("/static/..%2Fapp.py").status_code in (400, 404)
        assert not (seeded_vault.parent / "exports").exists()


def test_packs_and_validate_helper_endpoints(seeded_vault: Path) -> None:
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        packs = client.get("/api/packs?domain=ops_supply_chain&free_only=true")
        assert packs.status_code == 200
        pdata = packs.json()
        assert "ops_supply_chain" in pdata["domain_lenses"]
        assert all(
            d["acquisition"] == "free_public_api" for d in pdata["external_datasets"]
        )

        val = client.get("/api/validate?strict=true")
        assert val.status_code == 200
        assert val.json()["valid"] is True
        assert val.json()["counts"]["opportunities"] == 4
        assert val.json()["counts"]["staging_opportunities"] == 2


def test_invalid_query_and_body_types_return_422_and_workspace_clean(
    seeded_vault: Path,
) -> None:
    with TestClient(create_app(vault_dir=seeded_vault)) as client:
        for bad_cap in ("0", "-4.5", "nan", "inf", "true", "abc"):
            assert (
                client.get(f"/api/portfolio?capacity_weeks={bad_cap}").status_code
                == 422
            )

        assert (
            client.post(
                "/api/boss-pin", json={"id": "OPP-001", "capacity_weeks": 0}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/boss-pin", json={"id": "OPP-001", "boss_mandate": "yes"}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/boss-pin", json={"id": "OPP-001", "boss_mandate": 1}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/boss-pin", json={"id": "OPP-001", "capacity_weeks": True}
            ).status_code
            == 422
        )

    for forbidden in ("vault", "exports", "package.json", "node_modules"):
        assert not (REPO_ROOT / forbidden).exists(), (
            f"Forbidden artifact {forbidden!r} found in workspace root"
        )


# ---------------------------------------------------------------------------
# 9. Iteration 2 Adversarial Regressions (Concurrency, Corrupt Isolation, JS VM)
# ---------------------------------------------------------------------------


def test_concurrent_read_write_vault_lock_no_torn_reads(tmp_path: Path) -> None:
    vdir = tmp_path / "vault"
    initialize_vault(vdir, seed_questions=False, init_git=False)

    upsert_markdown_card(
        vdir / "datasets" / "prod.silver.orders.md",
        DatasetCard(
            id="DS-ORD",
            fqn="prod.silver.orders",
            catalog="prod",
            schema_name="silver",
            table_name="orders",
            medallion_layer="silver",
            grain="order_id",
            updated_at=date(2026, 11, 10),
        ),
        "## Orders",
    )
    upsert_markdown_card(
        vdir / "reports" / "ops-board.md",
        ReportCard(
            id="RPT-OPS",
            title="Ops Board",
            workspace="Ops",
            semantic_model="OpsModel",
            upstream_datasets=["[[prod.silver.orders]]"],
            updated_at=date(2026, 11, 10),
        ),
        "## Ops Report",
    )

    opp1_notes = "\n  Concurrent boss-pin note 🚀 [[Ops Board]]  \n"
    for idx, status in ((1, "active"), (2, "backlog")):
        spec = OpportunitySpec(
            id=f"OPP-00{idx}",
            title=f"Concurrent Opportunity {idx}",
            status=status,  # type: ignore[arg-type]
            boss_mandate=(idx == 1),
            internal_datasets=[
                InternalDatasetUse(ref="[[prod.silver.orders]]", role="features")
            ],
            existing_reports_supplemented=[
                ReportSupplement(ref="[[RPT-OPS]]", integration_mode="enrich")
            ],
            valuation=_make_valuation(),
            effort_weeks=4.0 if idx == 1 else 3.0,
            data_readiness_score=4,
            adoption_friction_score=2,
        )
        upsert_markdown_card(
            vdir / "opportunities" / f"OPP-00{idx}-concurrent.md",
            spec,
            f"## Body {idx}",
            human_notes_override=opp1_notes if idx == 1 else "\nNote 2\n",
        )

    total_calls = 120

    with TestClient(create_app(vault_dir=vdir)) as client:

        def _step(k: int) -> tuple[str, int, dict]:
            slot = k % 4
            if slot == 0:
                r = client.post(
                    "/api/boss-pin",
                    json={"id": "OPP-001", "boss_mandate": (k % 8 == 0)},
                )
                return ("pin", r.status_code, r.json())
            if slot == 1:
                r = client.get("/api/portfolio")
                return ("portfolio", r.status_code, r.json())
            if slot == 2:
                r = client.get("/api/brief/OPP-001")
                return ("brief", r.status_code, r.json())
            r = client.get("/api/lineage")
            return ("lineage", r.status_code, r.json())

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(_step, range(total_calls)))

    assert len(results) == total_calls
    assert all(code == 200 for _, code, _ in results), [
        (kind, code, body) for kind, code, body in results if code != 200
    ]
    for kind, _, body in results:
        if kind == "portfolio":
            assert {o["id"] for o in body["opportunities"]} == {"OPP-001", "OPP-002"}
            assert body["warnings"] == []
        elif kind == "brief":
            assert "OPP-001" in body["markdown"]
            assert body.get("warnings", []) == []
        elif kind == "lineage":
            node_ids = {n["id"] for n in body["nodes"]}
            assert {"opportunity:OPP-001", "opportunity:OPP-002"} <= node_ids
            assert body["warnings"] == []

    assert (
        parse_markdown_card(
            vdir / "opportunities" / "OPP-001-concurrent.md"
        ).human_notes
        == opp1_notes
    )


def test_corrupt_sibling_isolation_across_mutations_and_briefs(tmp_path: Path) -> None:
    vdir = tmp_path / "vault"
    initialize_vault(vdir, seed_questions=True, init_git=False)

    notes_1 = "\n  OPP-001 active note\twith [[Finance]] & emoji 🔒  \n"
    notes_2 = "\n  OPP-002 staged note — preserve byte-for-byte!  \n"
    notes_3 = "\n  OPP-003 staged archive note — preserve verbatim!  \n"

    opp1_path = vdir / "opportunities" / "OPP-001-active.md"
    upsert_markdown_card(
        opp1_path,
        OpportunitySpec(
            id="OPP-001",
            title="Valid Active Spec",
            status="active",
            valuation=_make_valuation(verified_lift=False),
            effort_weeks=3.5,
            data_readiness_score=4,
            adoption_friction_score=2,
        ),
        "## Valid Active Body",
        human_notes_override=notes_1,
    )
    upsert_markdown_card(
        vdir / "staging" / "opportunities" / "OPP-002-staged.md",
        OpportunitySpec(
            id="OPP-002",
            title="Valid Staged Spec",
            status="staging",
            valuation=_make_valuation(verified_lift=False),
            effort_weeks=2.5,
            data_readiness_score=4,
            adoption_friction_score=2,
        ),
        "## Valid Staged Body",
        human_notes_override=notes_2,
    )
    upsert_markdown_card(
        vdir / "staging" / "opportunities" / "OPP-003-staged-arch.md",
        OpportunitySpec(
            id="OPP-003",
            title="Valid Staged Archive Candidate",
            status="staging",
            valuation=_make_valuation(verified_lift=False),
            effort_weeks=2.0,
            data_readiness_score=3,
            adoption_friction_score=3,
        ),
        "## Staged Archive Body",
        human_notes_override=notes_3,
    )

    (vdir / "opportunities" / "000-corrupt.md").write_text(
        "---\n: [broken\n---\n", encoding="utf-8", newline="\n"
    )
    (vdir / "staging" / "opportunities" / "000-corrupt.md").write_text(
        "---\n: [broken\n---\n", encoding="utf-8", newline="\n"
    )
    (vdir / "opportunities" / "001-invalid-sibling.md").write_text(
        "---\nid: OPP-900\ntitle: Invalid Sibling\nstatus: active\neffort_weeks: -5\n---\n",
        encoding="utf-8",
        newline="\n",
    )
    (vdir / "staging" / "opportunities" / "001-invalid-sibling.md").write_text(
        "---\nid: OPP-901\ntitle: Invalid Staged Sibling\nstatus: staging\neffort_weeks: -5\n---\n",
        encoding="utf-8",
        newline="\n",
    )
    (vdir / "opportunities" / "OPP-BAD.md").write_text(
        "---\nid: OPP-BAD\ntitle: Self Invalid\nstatus: active\neffort_weeks: -5\n---\n",
        encoding="utf-8",
        newline="\n",
    )
    (vdir / "staging" / "opportunities" / "OPP-BAD-STG.md").write_text(
        "---\nid: OPP-BAD-STG\ntitle: Self Invalid Staged\nstatus: staging\neffort_weeks: -5\n---\n",
        encoding="utf-8",
        newline="\n",
    )

    with TestClient(create_app(vault_dir=vdir)) as client:
        r_pin = client.post(
            "/api/boss-pin", json={"id": "OPP-001", "boss_mandate": True}
        )
        assert r_pin.status_code == 200
        assert r_pin.json()["boss_mandate"] is True
        assert parse_markdown_card(opp1_path).human_notes == notes_1

        r_prom = client.post(
            "/api/promote", json={"id": "OPP-002", "target_status": "backlog"}
        )
        assert r_prom.status_code == 200
        assert r_prom.json()["new_status"] == "backlog"
        promoted_path = vdir / "opportunities" / "OPP-002-staged.md"
        assert promoted_path.is_file()
        assert parse_markdown_card(promoted_path).human_notes == notes_2

        r_arch_stg = client.post(
            "/api/archive", json={"id": "OPP-003", "reason": "SQL view sufficient"}
        )
        assert r_arch_stg.status_code == 200
        archived_stg_path = vdir / "opportunities" / "OPP-003-staged-arch.md"
        assert parse_markdown_card(archived_stg_path).human_notes == notes_3

        r_arch_act = client.post(
            "/api/archive",
            json={"id": "OPP-002", "reason": "Deferred to next quarter"},
        )
        assert r_arch_act.status_code == 200
        assert parse_markdown_card(promoted_path).human_notes == notes_2

        r_gb = client.get("/api/brief/OPP-001")
        assert r_gb.status_code == 200
        assert "OPP-001" in r_gb.json()["markdown"]
        assert len(r_gb.json().get("warnings", [])) >= 1

        r_pb_opp = client.post(
            "/api/brief",
            json={"opp_id": "OPP-001", "brief_type": "opportunity", "format": "exec"},
        )
        assert r_pb_opp.status_code == 200
        assert (
            "# Executive Opportunity Brief — OPP-001"
            in r_pb_opp.json()["markdown"]
        )
        assert len(r_pb_opp.json().get("warnings", [])) >= 1

        r_pb_wk = client.post("/api/brief", json={"brief_type": "weekly_status"})
        assert r_pb_wk.status_code == 200
        assert "OPP-001" in r_pb_wk.json()["markdown"]
        assert len(r_pb_wk.json().get("warnings", [])) >= 1

        r_pb_vq = client.post("/api/brief", json={"brief_type": "verify_questions"})
        assert r_pb_vq.status_code == 200
        assert "[VERIFY WITH STAKEHOLDER]" in r_pb_vq.json()["markdown"]
        assert len(r_pb_vq.json().get("warnings", [])) >= 1

        assert (
            client.post(
                "/api/boss-pin", json={"id": "OPP-BAD", "boss_mandate": True}
            ).status_code
            == 422
        )
        assert client.post("/api/archive", json={"id": "OPP-BAD"}).status_code == 422
        assert (
            client.post("/api/promote", json={"id": "OPP-BAD-STG"}).status_code == 422
        )
        assert client.get("/api/brief/OPP-BAD").status_code == 422
        assert (
            client.post(
                "/api/brief", json={"opp_id": "OPP-BAD", "brief_type": "opportunity"}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/brief",
                json={"opp_id": "OPP-BAD", "brief_type": "verify_questions"},
            ).status_code
            == 422
        )

        for ep in ("/api/boss-pin", "/api/promote", "/api/archive"):
            assert client.post(ep, json={"id": "OPP-404-NONE"}).status_code == 404
        assert client.get("/api/brief/OPP-404-NONE").status_code == 404
        assert (
            client.post(
                "/api/brief",
                json={"opp_id": "OPP-404-NONE", "brief_type": "opportunity"},
            ).status_code
            == 404
        )


def test_corrupt_questions_vs_playbook_independent_mutation(tmp_path: Path) -> None:
    v_corrupt_q = tmp_path / "vault_corrupt_q"
    initialize_vault(v_corrupt_q, seed_questions=True, init_git=False)
    pb_file = v_corrupt_q / "questions" / "day1-90-playbook.md"
    pb_parsed = parse_markdown_card(pb_file)
    pb_notes = "\n  Playbook custom note preserved!  \n"
    upsert_markdown_card(
        pb_file, pb_parsed.frontmatter, pb_parsed.body, human_notes_override=pb_notes
    )
    (v_corrupt_q / "questions" / "open-questions.md").write_text(
        "---\n: [broken_questions\n---\n", encoding="utf-8", newline="\n"
    )

    with TestClient(create_app(vault_dir=v_corrupt_q)) as client_a:
        r_phase = client_a.patch("/api/readiness/PHASE-1", json={"completed": True})
        assert r_phase.status_code == 200
        assert r_phase.json()["completed"] is True
        assert r_phase.json()["summary"]["completed_phases"] == 1
        assert (
            client_a.patch(
                "/api/questions/Q-MGR-01", json={"status": "answered"}
            ).status_code
            == 422
        )
        assert (
            client_a.patch(
                "/api/readiness/PHASE-99", json={"completed": True}
            ).status_code
            == 404
        )

    pb_after = parse_markdown_card(pb_file)
    assert pb_after.human_notes == pb_notes
    assert (
        next(p for p in pb_after.frontmatter["phases"] if p["id"] == "PHASE-1")[
            "completed"
        ]
        is True
    )

    v_corrupt_pb = tmp_path / "vault_corrupt_pb"
    initialize_vault(v_corrupt_pb, seed_questions=True, init_git=False)
    q_file = v_corrupt_pb / "questions" / "open-questions.md"
    q_parsed = parse_markdown_card(q_file)
    q_notes = "\n  Questions custom note preserved!  \n"
    upsert_markdown_card(
        q_file, q_parsed.frontmatter, q_parsed.body, human_notes_override=q_notes
    )
    (v_corrupt_pb / "questions" / "day1-90-playbook.md").write_text(
        "---\n: [broken_playbook\n---\n", encoding="utf-8", newline="\n"
    )

    with TestClient(create_app(vault_dir=v_corrupt_pb)) as client_b:
        r_q = client_b.patch(
            "/api/questions/Q-MGR-01",
            json={"status": "answered", "answer": "North Star confirmed."},
        )
        assert r_q.status_code == 200
        assert r_q.json()["status"] == "answered"
        assert r_q.json()["answer_summary"] == "North Star confirmed."
        assert (
            client_b.patch(
                "/api/readiness/PHASE-1", json={"completed": True}
            ).status_code
            == 422
        )
        assert (
            client_b.patch(
                "/api/questions/Q-999", json={"status": "answered"}
            ).status_code
            == 404
        )

    q_after = parse_markdown_card(q_file)
    assert q_after.human_notes == q_notes
    assert (
        next(q for q in q_after.frontmatter["questions"] if q["id"] == "Q-MGR-01")[
            "answer_summary"
        ]
        == "North Star confirmed."
    )


def test_malformed_config_yaml_returns_422_without_startup_crash(
    tmp_path: Path,
) -> None:
    vdir = tmp_path / "vault_bad_cfg"
    initialize_vault(vdir, seed_questions=False, init_git=False)
    bad_cfg = tmp_path / "broken_config.yaml"
    bad_cfg.write_text(
        "quarterly_capacity_weeks: [unclosed\n", encoding="utf-8", newline="\n"
    )

    bad_app = create_app(vault_dir=vdir, config_path=bad_cfg)
    with TestClient(bad_app) as client:
        assert client.get("/api/health").status_code == 422
        assert client.get("/api/portfolio").status_code == 422
        assert client.get("/api/validate").status_code == 422


_JS_VM_HARNESS = r"""
const fs = require("fs"), vm = require("vm"), rl = require("readline");
const [htmlPath, jsPath] = process.argv.slice(1);
const VOID = new Set(["meta", "link", "input", "br", "hr", "img"]);
let lastFocused = null;
class Node {}
class Text extends Node { constructor(t) { super(); this.textContent = String(t); } }
const matchSel = (x, s) => {
  if (s === "svg") return x.tagName === "SVG";
  if (s === 'meta[name="color-scheme"]') return x.tagName === "META" && x.getAttribute("name") === "color-scheme";
  if (s === '[role="tab"]') return x.getAttribute("role") === "tab";
  if (s.startsWith("#")) return x.id === s.slice(1);
  if (s.startsWith(".")) return x.className.split(/\s+/).includes(s.slice(1));
  return x.tagName === s.toUpperCase();
};
const walk = (n, o = []) => { if (n instanceof Elem) o.push(n); (n.children || []).forEach(c => walk(c, o)); return o; };
class Elem extends Node {
  constructor(t, ns = null) { super(); this.tagName = t.toUpperCase(); this.namespaceURI = ns; this.attrs = new Map(); this.children = []; this.ls = new Map(); this.style = {}; this.dataset = {}; }
  set id(v) { this.setAttribute("id", v); } get id() { return this.getAttribute("id") || ""; }
  set className(v) { this.setAttribute("class", v); } get className() { return this.getAttribute("class") || ""; }
  set hidden(v) { v ? this.setAttribute("hidden", "") : this.removeAttribute("hidden"); } get hidden() { return this.hasAttribute("hidden"); }
  setAttribute(k, v) { const s = String(v); this.attrs.set(k, s); if (k === "value" && this._v === undefined) this._v = s; if (k === "checked") this.checked = true; }
  getAttribute(k) { return this.attrs.has(k) ? this.attrs.get(k) : null; } hasAttribute(k) { return this.attrs.has(k); } removeAttribute(k) { this.attrs.delete(k); }
  appendChild(c) { if (c) { c.parentNode = this; this.children.push(c); } return c; }
  replaceChildren(...cs) { this.children = []; if (this.tagName === "SELECT") this._v = undefined; cs.forEach(c => this.appendChild(c)); }
  get options() { return this.tagName === "SELECT" ? this.children.filter(c => c.tagName === "OPTION") : []; }
  get value() {
    if (this.tagName === "SELECT") { const os = this.options; if (this._v !== undefined && os.some(o => o.value === this._v)) return this._v; return (os.find(o => o.selected) || os[0])?.value || ""; }
    return this._v ?? (this.getAttribute("value") || "");
  }
  set value(v) { const s = String(v); this._v = s; if (this.tagName === "SELECT") for (const o of this.options) o.selected = (o.value === s); }
  get textContent() { return this.children.map(c => c.textContent).join(""); } set textContent(v) { this.children = v === "" ? [] : [new Text(v)]; }
  querySelector(s) { return walk(this).slice(1).find(x => matchSel(x, s)) || null; }
  querySelectorAll(s) { return walk(this).slice(1).filter(x => matchSel(x, s)); }
  addEventListener(t, f) { this.ls.set(t, [...(this.ls.get(t) || []), f]); }
  dispatchEvent(e) { e.target = e.target || this; e.preventDefault = () => {}; e.stopPropagation = () => {}; for (const f of (this.ls.get(e.type) || [])) f(e); }
  click() { this.dispatchEvent({ type: "click" }); } focus() { lastFocused = this; } select() {} remove() {}
}
const html = fs.readFileSync(htmlPath, "utf8"), root = new Elem("doc"), st = [root]; let sd = 0;
for (const m of html.matchAll(/<!--[\s\S]*?-->|<!DOCTYPE[^>]*>|<\/([a-zA-Z0-9-]+)>|<([a-zA-Z0-9-]+)([^>]*)>|([^<]+)/g)) {
  if (m[1]) { if (m[1].toLowerCase() === "svg") sd--; st.pop(); }
  else if (m[2]) {
    const t = m[2].toLowerCase(); if (t === "svg") sd++;
    const e = new Elem(t, sd > 0 ? "http://www.w3.org/2000/svg" : null);
    for (const a of m[3].matchAll(/([a-zA-Z0-9:-]+)(?:="([^"]*)")?/g)) e.setAttribute(a[1], a[2] ?? "");
    st[st.length - 1].appendChild(e);
    if (!VOID.has(t) && !m[3].trim().endsWith("/")) st.push(e); else if (t === "svg") sd--;
  } else if (m[4] && m[4].trim()) st[st.length - 1].appendChild(new Text(m[4]));
}
const doc = {
  documentElement: walk(root).find(x => x.tagName === "HTML"), body: walk(root).find(x => x.tagName === "BODY"), ls: new Map(),
  createElement: t => new Elem(t), createElementNS: (ns, t) => new Elem(t, ns), createTextNode: t => new Text(t), execCommand: () => true,
  getElementById: id => walk(root).find(x => x.id === id) || null,
  querySelector: s => walk(root).find(x => matchSel(x, s)) || null,
  querySelectorAll: s => walk(root).filter(x => matchSel(x, s)),
  addEventListener: (t, f) => { doc.ls.set(t, [...(doc.ls.get(t) || []), f]); },
};
let seq = 0, inf = 0; const pend = new Map(), rlIf = rl.createInterface({ input: process.stdin });
rlIf.on("line", l => { const m = JSON.parse(l); pend.get(m.id)?.(m); pend.delete(m.id); inf--; });
const fetch = (path, opts = {}) => new Promise(res => {
  const id = ++seq; inf++;
  pend.set(id, m => res({ ok: m.ok, status: m.status, json: async () => m.json }));
  process.stdout.write(JSON.stringify({ rpc: "fetch", id, path, method: opts.method || "GET", headers: opts.headers || {}, body: opts.body || null }) + "\n");
});
const settle = async () => {
  while (inf > 0) await new Promise(r => setTimeout(r, 2));
  for (let i = 0; i < 8; i++) { await new Promise(r => setImmediate(r)); if (inf > 0) return settle(); }
};
(async () => {
  const ctx = vm.createContext({ document: doc, navigator: { clipboard: { writeText: async () => {} } }, fetch, URLSearchParams, Node, setTimeout: () => 1, clearTimeout: () => {}, encodeURIComponent, decodeURIComponent });
  vm.runInContext(fs.readFileSync(jsPath, "utf8"), ctx);
  for (const f of (doc.ls.get("DOMContentLoaded") || [])) f();
  await settle();

  const roiSvg = doc.getElementById("roi-matrix-svg");
  const nanInfTexts = walk(roiSvg).filter(x => x.tagName === "TEXT" && /NaN|Infinity|undefined/i.test(x.textContent)).map(x => x.textContent);
  const nanInfAttrs = [];
  for (const x of walk(roiSvg)) for (const [k, v] of x.attrs.entries()) if (/NaN|Infinity|undefined/i.test(v)) nanInfAttrs.push(`${x.tagName}[${k}=${v}]`);
  const circleCount = walk(roiSvg).filter(x => x.tagName === "CIRCLE").length;

  lastFocused = null;
  walk(roiSvg).find(x => x.className.split(/\s+/).includes("bubble-group") && (x.getAttribute("aria-label") || "").startsWith("OPP-001")).dispatchEvent({ type: "keydown", key: "Enter" });
  const bubbleFocusRestored = Boolean(lastFocused && (lastFocused.getAttribute("aria-label") || "").startsWith("OPP-001"));

  lastFocused = null;
  walk(doc.getElementById("lineage-svg")).find(x => x.getAttribute("aria-label") === "dataset: cat.silver.orders").dispatchEvent({ type: "keydown", key: "Enter" });
  const lineageFocusRestored = Boolean(lastFocused && lastFocused.getAttribute("aria-label") === "dataset: cat.silver.orders");
  const jsCone = walk(doc.getElementById("lineage-svg")).filter(x => x.className.split(/\s+/).includes("lineage-node") && !x.className.split(/\s+/).includes("is-dimmed")).length;

  const saveCb = doc.getElementById("brief-save-checkbox");
  saveCb.checked = true;
  const bt = doc.getElementById("brief-type-select"); bt.value = "verify_questions"; bt.dispatchEvent({ type: "change" });
  walk(doc.getElementById("opp-drawer")).find(x => x.tagName === "BUTTON" && x.textContent.includes("Generate Exec Brief")).click();
  await settle();
  const drawerOpts = doc.getElementById("brief-opp-select").options.map(o => o.value);
  const badge = doc.getElementById("brief-type-badge").textContent;

  const stagingText = doc.getElementById("staging-inspector").textContent;

  const df = doc.getElementById("domain-filter"), ia = doc.getElementById("include-archived-checkbox");
  const rowIds = () => doc.getElementById("portfolio-tbody").children.map(r => r.children[2].children[0].textContent);
  df.value = "retail_cpg"; df.dispatchEvent({ type: "change" }); await settle();
  ia.checked = true; ia.dispatchEvent({ type: "change" }); await settle();
  const beforeIds = rowIds();
  walk(doc.getElementById("portfolio-tbody")).find(x => x.tagName === "BUTTON").click();
  await settle();
  const afterIds = rowIds();

  doc.getElementById("btn-refresh-all").click();
  await settle();
  const globalError = doc.getElementById("global-error").textContent;

  rlIf.close();
  process.stdout.write(JSON.stringify({ rpc: "done", jsCone, beforeIds, afterIds, nanInfTexts, nanInfAttrs, circleCount, badge, drawerOpts, globalError, bubbleFocusRestored, lineageFocusRestored, stagingText }) + "\n");
})();
"""


def test_frontend_js_vm_lineage_cycles_filters_floats_and_exec_shortcut(
    tmp_path: Path,
) -> None:
    node_bin = shutil.which("node")
    if node_bin is None:
        pytest.skip("node binary not available on PATH")

    vdir = tmp_path / "vault_js_vm"
    initialize_vault(vdir, seed_questions=True, init_git=False)

    upsert_markdown_card(
        vdir / "datasets" / "orders.md",
        DatasetCard(
            id="D1",
            fqn="cat.silver.orders",
            catalog="cat",
            schema_name="silver",
            table_name="orders",
            medallion_layer="silver",
            grain="order_id",
            upstream_refs=["[[cat.gold.demand]]"],
            downstream_refs=["[[cat.gold.demand]]"],
            updated_at=date(2026, 1, 1),
        ),
        "## Orders",
    )
    upsert_markdown_card(
        vdir / "datasets" / "demand.md",
        DatasetCard(
            id="D2",
            fqn="cat.gold.demand",
            catalog="cat",
            schema_name="gold",
            table_name="demand",
            medallion_layer="gold",
            grain="sku_day",
            upstream_refs=["[[cat.silver.orders]]"],
            updated_at=date(2026, 1, 1),
        ),
        "## Demand",
    )
    upsert_markdown_card(
        vdir / "reports" / "r1.md",
        ReportCard(
            id="R1",
            title="PBI Forecast",
            workspace="W",
            semantic_model="M",
            updated_at=date(2026, 1, 1),
        ),
        "## PBI",
    )

    for oid, status, dom, eff, val in (
        ("OPP-001", "active", "ops_supply_chain", 1.6e308, 1.6e308),
        ("OPP-002", "backlog", "retail_cpg", 2.0, 100_000.0),
        ("OPP-003", "archived", "retail_cpg", 1.0, 50_000.0),
    ):
        upsert_markdown_card(
            vdir / "opportunities" / f"{oid}.md",
            OpportunitySpec(
                id=oid,
                title=f"Title {oid}",
                status=status,  # type: ignore[arg-type]
                domain_pack=dom,
                created_at=date(2026, 1, 1),
                updated_at=date(2026, 1, 1),
                effort_weeks=eff,
                data_readiness_score=3,
                adoption_friction_score=3,
                internal_datasets=[
                    InternalDatasetUse(ref="[[cat.gold.demand]]", role="features")
                ],
                existing_reports_supplemented=[
                    ReportSupplement(ref="[[R1]]", integration_mode="enrich")
                ],
                steps_to_completion=["Ship"],
                valuation=ValuationSpec(
                    confidence_tier="hypothesis",
                    driver_formula="v * 1",
                    drivers=[DriverSpec(name="v", value=val, verified=False)],
                ),
            ),
            f"## {oid}",
        )

    upsert_markdown_card(
        vdir / "staging" / "opportunities" / "OPP-004.md",
        OpportunitySpec(
            id="OPP-004",
            title="Staged Candidate",
            status="staging",
            domain_pack="retail_cpg",
            created_at=date(2026, 1, 1),
            updated_at=date(2026, 1, 1),
            effort_weeks=2.0,
            data_readiness_score=3,
            adoption_friction_score=3,
            cfo_critique_notes=["DAX measure may suffice for phase 1."],
            steps_to_completion=["Validate"],
            valuation=_make_valuation(),
        ),
        "## Candidate Summary\nFull problem statement and architectural notes.",
    )

    html_path = REPO_ROOT / "poopdeck" / "ui" / "static" / "index.html"
    js_path = REPO_ROOT / "poopdeck" / "ui" / "static" / "app.js"
    done_payload: dict | None = None

    with TestClient(create_app(vault_dir=vdir)) as client:
        api_cone = len(
            client.get("/api/lineage?focus_id=dataset:cat.silver.orders").json()[
                "nodes"
            ]
        )
        with subprocess.Popen(
            [node_bin, "-e", _JS_VM_HARNESS, str(html_path), str(js_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        ) as proc:
            assert proc.stdin is not None
            assert proc.stdout is not None
            assert proc.stderr is not None
            for raw_line in proc.stdout:
                msg = json.loads(raw_line)
                if msg["rpc"] == "fetch":
                    resp = client.request(
                        msg["method"],
                        msg["path"],
                        content=msg["body"],
                        headers=msg["headers"],
                    )
                    proc.stdin.write(
                        json.dumps(
                            {
                                "id": msg["id"],
                                "ok": 200 <= resp.status_code < 300,
                                "status": resp.status_code,
                                "json": resp.json(),
                            }
                        )
                        + "\n"
                    )
                    proc.stdin.flush()
                elif msg["rpc"] == "done":
                    done_payload = msg
                    proc.stdin.close()
                    break
            stderr_text = proc.stderr.read()
            rc = proc.wait(timeout=10)

    assert rc == 0, f"Node VM harness failed (rc={rc}): {stderr_text}"
    assert done_payload is not None, f"Missing done payload; stderr={stderr_text}"
    assert done_payload["globalError"] == ""
    assert done_payload["jsCone"] == api_cone == 5
    assert done_payload["beforeIds"] == ["OPP-002", "OPP-003"]
    assert done_payload["afterIds"] == ["OPP-002", "OPP-003"]
    assert done_payload["circleCount"] >= 2
    assert done_payload["nanInfTexts"] == []
    assert done_payload["nanInfAttrs"] == []
    assert done_payload["badge"] == "opportunity | exec"
    assert "" not in done_payload["drawerOpts"]
    assert done_payload["bubbleFocusRestored"] is True
    assert done_payload["lineageFocusRestored"] is True
    assert "DAX measure may suffice for phase 1." in done_payload["stagingText"]
    assert "Full problem statement and architectural notes." in done_payload["stagingText"]
    assert not (vdir.parent / "exports").exists()
    assert (
        parse_markdown_card(vdir / "opportunities" / "OPP-002.md").frontmatter[
            "boss_mandate"
        ]
        is True
    )


def test_iter3_bare_json_nan_inf_and_control_char_query_params_return_422(
    seeded_vault: Path,
) -> None:
    """Bare JSON NaN/Infinity, invalid UTF-8 bodies, and control-char query/path params return 422."""
    app_instance = create_app(vault_dir=seeded_vault)
    opp1_path = seeded_vault / "opportunities" / "OPP-001-lead-time-quantile.md"
    before_bytes = opp1_path.read_bytes()

    json_headers = {"content-type": "application/json"}
    mutation_cases: list[tuple[str, str, str]] = [
        ("POST", "/api/boss-pin", '{"opp_id":"OPP-001","pinned":false,"capacity_weeks":NaN}'),
        ("POST", "/api/boss-pin", '{"opp_id":"OPP-001","pinned":false,"extra":{"nested":[NaN,Infinity]}}'),
        ("POST", "/api/boss-pin", "NaN"),
        ("POST", "/api/boss-pin", "[Infinity, -Infinity]"),
        ("POST", "/api/promote", '{"opp_id":Infinity}'),
        ("POST", "/api/promote", '{"opp_id":"OPP-005","target_status":"backlog","meta":{"x":-Infinity}}'),
        ("POST", "/api/archive", '{"opp_id":{"nested":[NaN]}}'),
        ("POST", "/api/archive", '{"opp_id":"OPP-001","extra":Infinity}'),
        ("POST", "/api/brief", '{"brief_type":"weekly_status","capacity_weeks":-Infinity}'),
        ("POST", "/api/brief", '{"brief_type":"weekly_status","nested":{"bad":NaN}}'),
        ("POST", "/api/brief", '{"brief_type":"weekly_status\\u001f"}'),
        ("PATCH", "/api/questions/Q-MGR-01", '{"extra":NaN}'),
        ("POST", "/api/questions/Q-MGR-01", '{"status":"answered","meta":[Infinity]}'),
        ("PATCH", "/api/questions", '{"id":"Q-MGR-01","status":NaN}'),
        ("POST", "/api/readiness/PHASE-1", '{"completed":true,"extra":-Infinity}'),
        ("PATCH", "/api/readiness", '{"id":"PHASE-1","extra":{"v":NaN}}'),
    ]

    with TestClient(app_instance, raise_server_exceptions=False) as client:
        for method, path, raw_body in mutation_cases:
            resp = client.request(method, path, content=raw_body, headers=json_headers)
            assert resp.status_code == 422, (
                f"{method} {path} with {raw_body!r} returned {resp.status_code}: {resp.text}"
            )
            assert "detail" in resp.json()
            assert "content-security-policy" in resp.headers

        for ctype in ("application/json", "text/plain", "application/octet-stream"):
            for bpath in ("/api/boss-pin", "/api/questions/Q-MGR-01"):
                resp_bin = client.post(
                    bpath, content=b"\xff\xfe\x80\x00", headers={"content-type": ctype}
                )
                assert resp_bin.status_code == 422
                assert "detail" in resp_bin.json()

        for bad_dom in ("ops\x00bad", "ops\x1fbad"):
            resp_bad_dom = client.post(
                "/api/boss-pin",
                json={"id": "OPP-001", "boss_mandate": False, "domain": bad_dom},
            )
            assert resp_bad_dom.status_code == 422

        assert (
            client.patch(
                "/api/questions/Q-MGR-01%00bad", json={"status": "answered"}
            ).status_code
            == 422
        )

        query_paths = [
            "/api/portfolio?domain=ops%00bad",
            "/api/portfolio?domain=ops%1fbad",
            "/api/packs?domain=retail%00bad",
            "/api/packs?domain=retail%7fbad",
            "/api/lineage?focus_id=OPP-001%00bad",
            "/api/questions?category=manager%00bad",
            "/api/questions?status=open%07bad",
            "/api/readiness?category=manager%00bad",
            "/api/brief?brief_type=weekly_status&format=teams%00bad",
            "/api/brief?brief_type=weekly%1fstatus",
            "/api/brief?mode=exec%00bad",
        ]
        for qpath in query_paths:
            qresp = client.get(qpath)
            assert qresp.status_code == 422, f"GET {qpath} -> {qresp.status_code}"

    assert opp1_path.read_bytes() == before_bytes


def test_iter3_corrupt_non_prefixed_utf8_body_and_flow_yaml_targets_return_422(
    tmp_path: Path,
) -> None:
    """Corrupt non-stem-prefixed cards with invalid UTF-8 body or flow YAML return 422; prefix/body decoys return 404."""
    vdir = tmp_path / "vault"
    initialize_vault(vdir, seed_questions=True, init_git=False)

    (vdir / "opportunities" / "custom_utf8_target.md").write_bytes(
        b"---\nid: OPP-UTF8-BODY\ntitle: Bad Body\n---\n\xff\xfe\x80\n"
    )
    (vdir / "opportunities" / "custom_flow_target.md").write_text(
        "---\n{id: OPP-FLOW-BAD, effort_weeks: .nan}\n---\nBody\n",
        encoding="utf-8",
    )
    (vdir / "staging" / "opportunities" / "custom_stg_flow.md").write_bytes(
        b"---\r\n{id: 'OPP-STG-FLOW', effort_weeks: -5}\r\n---\r\nBody\r\n"
    )
    (vdir / "opportunities" / "custom_prefix_100.md").write_text(
        '---\n{id: "OPP-100", effort_weeks: .inf}\n---\nBody\nid: OPP-BODY-SPOOF\n',
        encoding="utf-8",
    )

    with TestClient(create_app(vault_dir=vdir)) as client:
        for bad_id in ("OPP-UTF8-BODY", "OPP-FLOW-BAD", "OPP-100"):
            assert client.post("/api/boss-pin", json={"opp_id": bad_id, "pinned": True}).status_code == 422
            assert client.post("/api/archive", json={"opp_id": bad_id}).status_code == 422
            assert client.get(f"/api/brief/{bad_id}").status_code == 422
            assert client.post("/api/brief", json={"opp_id": bad_id, "brief_type": "opportunity"}).status_code == 422

        assert client.post("/api/promote", json={"opp_id": "OPP-STG-FLOW", "target_status": "backlog"}).status_code == 422

        for decoy_id in ("OPP-10", "OPP-BODY-SPOOF"):
            assert client.post("/api/boss-pin", json={"opp_id": decoy_id, "pinned": True}).status_code == 404
            assert client.post("/api/archive", json={"opp_id": decoy_id}).status_code == 404
            assert client.get(f"/api/brief/{decoy_id}").status_code == 404


def test_iter3_duplicate_opp_id_isolation_and_promote_archive_collision_guard(
    tmp_path: Path,
) -> None:
    """Duplicate IDs must not produce Chimera schedule items or clobber live HUMAN-NOTES; in-place live archive stays 200."""
    vdir = tmp_path / "vault"
    initialize_vault(vdir, seed_questions=True, init_git=False)

    upsert_markdown_card(vdir / "datasets" / "alpha.md", DatasetCard(fqn="cat.gold.alpha", table_name="alpha", row_count=100), "Alpha.")
    upsert_markdown_card(vdir / "datasets" / "beta.md", DatasetCard(fqn="cat.gold.beta", table_name="beta", row_count=200), "Beta.")

    kw = dict(
        domain_pack="ops_supply_chain",
        valuation=_make_valuation(),
        data_readiness_score=4,
        adoption_friction_score=2,
        created_at=date(2026, 2, 1),
        updated_at=date(2026, 2, 2),
    )
    upsert_markdown_card(
        vdir / "opportunities" / "01-dup-active.md",
        OpportunitySpec(id="OPP-DUP", title="Active Primary Dup", status="active", boss_mandate=True, effort_weeks=4.0, internal_datasets=[InternalDatasetUse(ref="[[cat.gold.alpha]]", role="feature_source")], **kw),
        "Primary body.",
    )
    upsert_markdown_card(
        vdir / "opportunities" / "02-dup-blocked.md",
        OpportunitySpec(id="OPP-DUP", title="Blocked Secondary Dup", status="blocked", boss_mandate=False, effort_weeks=99.0, internal_datasets=[InternalDatasetUse(ref="[[cat.gold.beta]]", role="feature_source")], **kw),
        "Secondary duplicate body.",
    )

    live_clobber_path = vdir / "opportunities" / "OPP-CLOBBER.md"
    upsert_markdown_card(
        live_clobber_path,
        OpportunitySpec(id="OPP-CLOBBER", title="Live Protected Card", status="active", effort_weeks=3.0, **kw),
        "Live body.",
        human_notes_override="LIVE NOTES MUST SURVIVE",
    )
    live_clobber_bytes = live_clobber_path.read_bytes()

    upsert_markdown_card(
        vdir / "staging" / "opportunities" / "OPP-CLOBBER.md",
        OpportunitySpec(id="OPP-CLOBBER", title="Staged Same-Name Collision", status="staging", effort_weeks=8.0, **kw),
        "Draft body.",
        human_notes_override="DRAFT NOTES",
    )
    upsert_markdown_card(
        vdir / "opportunities" / "OPP-LIVE-ID.md",
        OpportunitySpec(id="OPP-LIVE-ID", title="Live ID Target", status="backlog", effort_weeks=2.0, **kw),
        "Live ID body.",
        human_notes_override="LIVE ID NOTES MUST SURVIVE",
    )
    upsert_markdown_card(
        vdir / "staging" / "opportunities" / "different-slug-collision.md",
        OpportunitySpec(id="OPP-LIVE-ID", title="Staged Different-Slug Same-ID", status="staging", effort_weeks=6.0, **kw),
        "Staged alt slug.",
    )
    upsert_markdown_card(
        vdir / "opportunities" / "OPP-UNIQUE-LIVE.md",
        OpportunitySpec(id="OPP-UNIQUE-LIVE", title="Unique Live Archivable", status="active", effort_weeks=2.5, **kw),
        "Unique live body.",
        human_notes_override="UNIQUE LIVE NOTES",
    )
    upsert_markdown_card(
        vdir / "staging" / "opportunities" / "OPP-CLEAN-STAGE.md",
        OpportunitySpec(id="OPP-CLEAN-STAGE", title="Unique Staged Card", status="staging", effort_weeks=2.0, **kw),
        "Clean staged body.",
        human_notes_override="CLEAN STAGE NOTES",
    )

    with TestClient(create_app(vault_dir=vdir)) as client:
        port = client.get("/api/portfolio").json()
        dup_items = [item for item in port["items"] if item["id"] == "OPP-DUP"]
        assert len(dup_items) == 1 and dup_items[0]["status"] == "active"
        assert not (dup_items[0]["status"] == "blocked" and dup_items[0]["above_cutline"])
        assert any("OPP-DUP" in w for w in port["warnings"])
        assert any("OPP-CLOBBER" in w for w in client.get("/api/staging").json()["warnings"])

        lin = client.get("/api/lineage").json()
        assert any("OPP-DUP" in w for w in lin["warnings"])
        assert {e["source"] for e in lin["edges"] if e["target"] == "opportunity:OPP-DUP"} == {"dataset:cat.gold.alpha"}

        wb_resp = client.post("/api/brief", json={"brief_type": "weekly_status"})
        assert wb_resp.status_code == 200 and any("OPP-DUP" in w for w in wb_resp.json()["warnings"])

        assert client.post("/api/boss-pin", json={"opp_id": "OPP-DUP", "pinned": False}).status_code == 422
        assert client.get("/api/brief/OPP-DUP").status_code == 422
        assert client.post("/api/brief", json={"opp_id": "OPP-DUP", "brief_type": "opportunity"}).status_code == 422

        for collide_id in ("OPP-CLOBBER", "OPP-LIVE-ID"):
            assert client.post("/api/promote", json={"opp_id": collide_id, "target_status": "backlog"}).status_code == 422
            assert client.post("/api/promote", json={"opp_id": collide_id, "action": "archive"}).status_code == 422
            assert client.post("/api/archive", json={"opp_id": collide_id, "reason": "collision check"}).status_code == 422

        assert live_clobber_path.read_bytes() == live_clobber_bytes
        assert parse_markdown_card(live_clobber_path).human_notes == "LIVE NOTES MUST SURVIVE"
        assert parse_markdown_card(vdir / "opportunities" / "OPP-LIVE-ID.md").human_notes == "LIVE ID NOTES MUST SURVIVE"

        assert client.post("/api/archive", json={"opp_id": "OPP-UNIQUE-LIVE", "reason": "deferred"}).status_code == 200
        live_arch = parse_markdown_card(vdir / "opportunities" / "OPP-UNIQUE-LIVE.md")
        assert live_arch.frontmatter["status"] == "archived" and live_arch.human_notes == "UNIQUE LIVE NOTES"

        assert client.post("/api/promote", json={"opp_id": "OPP-CLEAN-STAGE", "target_status": "backlog"}).status_code == 200
        assert client.post("/api/promote", json={"opp_id": "OPP-CLEAN-STAGE", "target_status": "backlog"}).status_code == 404

        q_path = vdir / "questions" / "open-questions.md"
        q_raw = q_path.read_text(encoding="utf-8")
        q_path.write_text(q_raw.replace("questions:\n", "questions:\n- id: Q-MGR-01\n  category: manager\n  who_to_ask: VP\n  question: Dup Q?\n  why_it_matters: Dup\n  status: open\n", 1), encoding="utf-8", newline="\n")
        q_get = client.get("/api/questions")
        assert q_get.status_code == 200 and any("Q-MGR-01" in w for w in q_get.json()["warnings"])
        assert sum(1 for q in q_get.json()["questions"] if q["id"] == "Q-MGR-01") == 1
        assert client.patch("/api/questions/Q-MGR-01", json={"status": "answered"}).status_code == 422


def test_iter3_lineage_focus_id_hash_pipe_js_parity_and_wikilink_fallback(
    tmp_path: Path,
) -> None:
    """GET /api/lineage?focus_id=... preserves '#' and '|' on exact IDs/labels while resolving [[Target|Alias]] and [[Target#Heading]]."""
    vdir = tmp_path / "vault"
    initialize_vault(vdir, seed_questions=True, init_git=False)

    upsert_markdown_card(
        vdir / "datasets" / "sales.md",
        DatasetCard(fqn="cat.gold.sales", table_name="sales", row_count=500),
        "Sales dataset.",
    )
    upsert_markdown_card(
        vdir / "reports" / "exec-margin.md",
        ReportCard(title="Exec | Margin #1", workspace="W", upstream_datasets=["[[cat.gold.sales]]"]),
        "Margin report.",
    )
    upsert_markdown_card(
        vdir / "reports" / "rpt2.md",
        ReportCard(id="RPT#2", title="Score #2", workspace="W", upstream_datasets=["[[cat.gold.sales]]"]),
        "Score report.",
    )
    upsert_markdown_card(
        vdir / "stakeholders" / "sh1.md",
        StakeholderCard(id="SH|1", name="VP | Ops", role="VP"),
        "Ops stakeholder.",
    )
    upsert_markdown_card(
        vdir / "opportunities" / "OPP-701.md",
        OpportunitySpec(
            id="OPP-701",
            title="Margin Optimization",
            status="active",
            domain_pack="ops_supply_chain",
            internal_datasets=[InternalDatasetUse(ref="[[cat.gold.sales]]", role="feature_source")],
            existing_reports_supplemented=[
                ReportSupplement(ref="[[Exec | Margin #1]]", integration_mode="Add forecast"),
                ReportSupplement(ref="[[RPT#2]]", integration_mode="Add alerts"),
            ],
            stakeholders=["[[SH|1]]"],
            valuation=_make_valuation(),
            effort_weeks=3.0,
            data_readiness_score=4,
            adoption_friction_score=2,
            created_at=date(2026, 2, 1),
            updated_at=date(2026, 2, 2),
        ),
        "Opportunity body.",
    )

    with TestClient(create_app(vault_dir=vdir)) as client:
        for exact_id, expected_count in (
            ("existing_report:W__Exec | Margin #1", 4),
            ("existing_report:RPT#2", 4),
            ("stakeholder:SH|1", 4),
        ):
            r_focus = client.get("/api/lineage", params={"focus_id": exact_id})
            assert r_focus.status_code == 200
            data = r_focus.json()
            assert data["matched_focus_id"] == exact_id
            assert len(data["nodes"]) == expected_count

        for label_or_wl, expected_match in (
            ("Exec | Margin #1", "existing_report:W__Exec | Margin #1"),
            ("VP | Ops", "stakeholder:SH|1"),
            ("[[RPT#2]]", "existing_report:RPT#2"),
            ("[[Exec | Margin #1]]", "existing_report:W__Exec | Margin #1"),
            ("[[cat.gold.sales|Gold Sales]]", "dataset:cat.gold.sales"),
            ("[[cat.gold.sales#Schema]]", "dataset:cat.gold.sales"),
        ):
            r_alt = client.get("/api/lineage", params={"focus_id": label_or_wl})
            assert r_alt.status_code == 200
            alt_data = r_alt.json()
            assert alt_data["matched_focus_id"] == expected_match
            assert len(alt_data["nodes"]) >= 4


def test_iter3_multi_dbl_max_effort_schedule_resilience_and_boss_pin_atomic_rollback(
    tmp_path: Path,
) -> None:
    """Multiple 1.6e308 effort_weeks cards return 200 with finite schedule metrics; failed boss-pin leaves zero partial writes."""
    vdir = tmp_path / "vault"
    initialize_vault(vdir, seed_questions=True, init_git=False)

    kw = dict(
        domain_pack="ops_supply_chain",
        valuation=_make_valuation(),
        data_readiness_score=4,
        adoption_friction_score=2,
        created_at=date(2026, 2, 1),
        updated_at=date(2026, 2, 2),
    )
    opp1_path = vdir / "opportunities" / "OPP-801.md"
    opp2_path = vdir / "opportunities" / "OPP-802.md"
    upsert_markdown_card(opp1_path, OpportunitySpec(id="OPP-801", title="Extreme Effort One", status="active", boss_mandate=True, effort_weeks=1.6e308, **kw), "Extreme card 1.")
    upsert_markdown_card(opp2_path, OpportunitySpec(id="OPP-802", title="Extreme Effort Two", status="active", boss_mandate=False, effort_weeks=1.6e308, **kw), "Extreme card 2.", human_notes_override="EXTREME NOTES SURVIVE")

    with TestClient(create_app(vault_dir=vdir)) as client:
        r_port = client.get("/api/portfolio")
        assert r_port.status_code == 200
        port = r_port.json()
        assert port["over_capacity"] is True and any("overflowed" in w.lower() for w in port["warnings"])
        assert math.isfinite(port["used_capacity_weeks"]) and math.isfinite(port["mandate_weeks"]) and math.isfinite(port["overflow_weeks"])
        assert {i["id"] for i in port["items"]} == {"OPP-801", "OPP-802"}

        pre_fail_bytes = opp2_path.read_bytes()
        assert client.post("/api/boss-pin", json={"id": "OPP-802", "boss_mandate": True, "domain": "bad\x00domain"}).status_code == 422
        assert opp2_path.read_bytes() == pre_fail_bytes

        r_pin = client.post("/api/boss-pin", json={"id": "OPP-802", "boss_mandate": True})
        assert r_pin.status_code == 200
        parsed_after = parse_markdown_card(opp2_path)
        assert parsed_after.frontmatter["boss_mandate"] is True and parsed_after.human_notes == "EXTREME NOTES SURVIVE"

        r_brief = client.post("/api/brief", json={"brief_type": "weekly_status"})
        assert r_brief.status_code == 200
        assert "OPP-801" in r_brief.json()["markdown"] and "OPP-802" in r_brief.json()["markdown"]
        assert float(parse_markdown_card(opp1_path).frontmatter["effort_weeks"]) > 1e300
        assert float(parse_markdown_card(opp2_path).frontmatter["effort_weeks"]) > 1e300


def test_unpaired_utf16_surrogates_rejected_422_with_csp_and_zero_truncation(tmp_path: Path) -> None:
    """Lone UTF-16 surrogates return 422 with CSP and zero vault/orphan mutations; valid emoji pair returns 200."""
    vdir = tmp_path / "vault"
    initialize_vault(vdir, seed_questions=True, init_git=False)
    kw = dict(domain_pack="ops_supply_chain", valuation=_make_valuation(), effort_weeks=3.0, data_readiness_score=4, adoption_friction_score=2, created_at=date(2026, 2, 1), updated_at=date(2026, 2, 2))
    live_p = upsert_markdown_card(vdir / "opportunities" / "OPP-101-live.md", OpportunitySpec(id="OPP-101", title="Live Card", status="active", **kw), "Live body.", human_notes_override="LIVE NOTES")
    stg_p = upsert_markdown_card(vdir / "staging" / "opportunities" / "OPP-105-staged.md", OpportunitySpec(id="OPP-105", title="Staged Card", status="staging", **kw), "Staged body.", human_notes_override="STAGED NOTES")
    ok_p = upsert_markdown_card(vdir / "opportunities" / "OPP-102-emoji.md", OpportunitySpec(id="OPP-102", title="Emoji Control", status="backlog", **kw), "Emoji body.", human_notes_override="EMOJI NOTES")
    q_p = vdir / "questions" / "open-questions.md"
    live_b, stg_b, q_b = live_p.read_bytes(), stg_p.read_bytes(), q_p.read_bytes()
    hdrs = {"content-type": "application/json"}
    bad_cases = [
        ("POST", "/api/promote", b'{"id": "/bad\\ud800"}'),
        ("POST", "/api/promote", b'{"id": "OPP-105", "target_status": "bad\\udbff"}'),
        ("POST", "/api/promote", b'{"id": "OPP-105", "action": "archive", "reason": "bad \\ud800"}'),
        ("POST", "/api/promote", b'{"\\ud800": 1}'),
        ("POST", "/api/archive", b'{"id": "OPP-101", "reason": "bad \\ud800"}'),
        ("POST", "/api/archive", b'{"id": "OPP-101", "reason": "low \\udfff"}'),
        ("PATCH", "/api/questions/Q-MGR-01", b'{"answer_summary": "bad \\ud800"}'),
        ("POST", "/api/questions", b'{"id": "Q-MGR-01", "answer_summary": "bad \\udc00"}'),
        ("POST", "/api/boss-pin", b'{"id": "OPP-101", "domain": "bad\\ud800"}'),
        ("POST", "/api/boss-pin", b'{"id": "OPP-101", "pinned": true, "\\udfff": 1}'),
        ("POST", "/api/brief", b'{"capacity_weeks": "bad\\ud800"}'),
    ]
    with TestClient(create_app(vault_dir=vdir), raise_server_exceptions=False) as client:
        for method, path, raw in bad_cases:
            r = client.request(method, path, content=raw, headers=hdrs)
            assert r.status_code == 422 and "content-security-policy" in r.headers and "detail" in r.json()
        assert live_p.read_bytes() == live_b and stg_p.read_bytes() == stg_b and q_p.read_bytes() == q_b
        assert not (vdir / "opportunities" / stg_p.name).exists()
        r_ok = client.post("/api/archive", content=b'{"id": "OPP-102", "reason": "Ship it \\ud83d\\ude80 \\u2014 caf\\u00e9 \\u65e5\\u672c\\u8a9e"}', headers=hdrs)
        assert r_ok.status_code == 200 and "content-security-policy" in r_ok.headers
        parsed_ok = parse_markdown_card(ok_p)
        assert "\U0001f680 \u2014 caf\u00e9 \u65e5\u672c\u8a9e" in parsed_ok.body and parsed_ok.human_notes == "EMOJI NOTES"


def test_deeply_nested_json_returns_422_with_csp_across_all_write_routes(tmp_path: Path) -> None:
    """Deeply nested JSON arrays and extra dicts (depth=1200, 5000) return 422 with CSP across all 8 write routes."""
    vdir = tmp_path / "vault"
    initialize_vault(vdir, seed_questions=True, init_git=False)
    opp_p = upsert_markdown_card(vdir / "opportunities" / "OPP-201.md", OpportunitySpec(id="OPP-201", title="Deep Guard", status="active", domain_pack="ops_supply_chain", valuation=_make_valuation(), effort_weeks=3.0, data_readiness_score=4, adoption_friction_score=2, created_at=date(2026, 2, 1), updated_at=date(2026, 2, 2)), "Body.", human_notes_override="DEEP NOTES")
    before = opp_p.read_bytes()
    hdrs = {"content-type": "application/json"}
    routes = [("POST", "/api/boss-pin"), ("POST", "/api/promote"), ("POST", "/api/archive"), ("POST", "/api/brief"), ("PATCH", "/api/questions/Q-MGR-01"), ("POST", "/api/questions/Q-MGR-01"), ("PATCH", "/api/readiness/PHASE-1"), ("POST", "/api/readiness/PHASE-1")]
    payloads = [p for d in (1200, 5000) for p in ("[" * d + "]" * d, '{"id":"OPP-201","status":"answered","extra":' + '{"a":' * d + "1" + "}" * d + "}")]
    with TestClient(create_app(vault_dir=vdir), raise_server_exceptions=False) as client:
        for method, path in routes:
            for raw in payloads:
                r = client.request(method, path, content=raw, headers=hdrs)
                assert r.status_code == 422 and "content-security-policy" in r.headers and "detail" in r.json()
    assert opp_p.read_bytes() == before


def test_non_stem_truncated_yaml_cards_and_questions_return_422_not_404(tmp_path: Path) -> None:
    """Truncated non-stem YAML cards and corrupt target specs return 422 while missing IDs stay 404; truncated questions warn and 422."""
    vdir = tmp_path / "vault"
    initialize_vault(vdir, seed_questions=True, init_git=False)
    (vdir / "opportunities" / "custom_trunc_syn.md").write_text("---\nid: OPP-TRUNC-SYN\ntitle: [unclosed\n", encoding="utf-8")
    (vdir / "opportunities" / "custom_trunc_eof.md").write_text("---\nid: OPP-TRUNC-EOF\ntitle: No closing fence\neffort_weeks: 3.0\n", encoding="utf-8")
    (vdir / "staging" / "opportunities" / "stg_trunc.md").write_text("---\nid: OPP-STG-TRUNC\ntitle: [unclosed\n", encoding="utf-8")
    (vdir / "opportunities" / "OPP-900-bad.md").write_text("---\nid: OPP-900\neffort_weeks: -5\n---\n", encoding="utf-8")
    with TestClient(create_app(vault_dir=vdir), raise_server_exceptions=False) as client:
        for tid in ("OPP-TRUNC-SYN", "OPP-TRUNC-EOF", "OPP-900"):
            assert client.post("/api/boss-pin", json={"id": tid, "pinned": True}).status_code == 422
            assert client.post("/api/archive", json={"id": tid}).status_code == 422
            assert client.get(f"/api/brief/{tid}").status_code == 422
            assert client.post("/api/brief", json={"opp_id": tid, "brief_type": "opportunity"}).status_code == 422
        assert client.post("/api/promote", json={"id": "OPP-STG-TRUNC", "target_status": "backlog"}).status_code == 422
        assert client.post("/api/archive", json={"id": "OPP-STG-TRUNC"}).status_code == 422
        assert client.get("/api/brief/OPP-STG-TRUNC").status_code == 422
        assert client.post("/api/brief", json={"opp_id": "OPP-STG-TRUNC", "brief_type": "opportunity"}).status_code == 422
        for mid in ("OPP-MISSING", "OPP-999"):
            assert client.post("/api/boss-pin", json={"id": mid, "pinned": True}).status_code == 404
            assert client.post("/api/archive", json={"id": mid}).status_code == 404
            assert client.post("/api/promote", json={"id": mid}).status_code == 404
            assert client.get(f"/api/brief/{mid}").status_code == 404
            assert client.post("/api/brief", json={"opp_id": mid, "brief_type": "opportunity"}).status_code == 404
        (vdir / "questions" / "open-questions.md").write_text("---\nid: QUESTION-BANK\nquestions:\n  - id: Q-MGR-01\n    question: [unclosed\n", encoding="utf-8")
        r_q = client.get("/api/questions")
        assert r_q.status_code == 200 and any("open-questions.md" in w for w in r_q.json()["warnings"]) and len(r_q.json()["phases"]) == 5
        assert client.patch("/api/questions/Q-MGR-01", json={"status": "answered"}).status_code == 422


def test_control_char_capacity_weeks_rejected_422_without_mutation(tmp_path: Path) -> None:
    """Control-character whitespace in capacity_weeks query/body returns 422 with CSP and zero disk mutation."""
    vdir = tmp_path / "vault"
    initialize_vault(vdir, seed_questions=True, init_git=False)
    opp_p = upsert_markdown_card(vdir / "opportunities" / "OPP-301.md", OpportunitySpec(id="OPP-301", title="Cap Guard", status="active", domain_pack="ops_supply_chain", valuation=_make_valuation(), effort_weeks=3.0, data_readiness_score=4, adoption_friction_score=2, created_at=date(2026, 2, 1), updated_at=date(2026, 2, 2)), "Body.", human_notes_override="CAP NOTES")
    before = opp_p.read_bytes()
    with TestClient(create_app(vault_dir=vdir), raise_server_exceptions=False) as client:
        for qurl in ("/api/portfolio?capacity_weeks=12.0%1f", "/api/portfolio?capacity_weeks=%0b12.0", "/api/brief?brief_type=weekly_status&capacity_weeks=12.0%1f", "/api/brief/weekly_status?capacity_weeks=%0b12.0"):
            r = client.get(qurl)
            assert r.status_code == 422 and "content-security-policy" in r.headers
        assert client.post("/api/boss-pin", json={"id": "OPP-301", "boss_mandate": True, "capacity_weeks": "12.0\u001f"}).status_code == 422
        assert client.post("/api/brief", json={"brief_type": "weekly_status", "capacity_weeks": "12.0\u001c"}).status_code == 422
    assert opp_p.read_bytes() == before


def test_non_utf8_siblings_and_targets_across_mutations_and_readiness(tmp_path: Path) -> None:
    """Non-UTF-8 siblings do not block valid mutations/readiness (200 + HUMAN-NOTES intact), while non-UTF-8 targets return 422 with zero byte changes."""
    vdir = tmp_path / "vault"
    initialize_vault(vdir, seed_questions=True, init_git=False)
    kw = dict(domain_pack="ops_supply_chain", valuation=_make_valuation(), effort_weeks=3.0, data_readiness_score=4, adoption_friction_score=2, created_at=date(2026, 2, 1), updated_at=date(2026, 2, 2))
    live_p = upsert_markdown_card(vdir / "opportunities" / "OPP-001.md", OpportunitySpec(id="OPP-001", title="Live", status="active", **kw), "Live body.", human_notes_override="HN-LIVE-1")
    stg5_p = upsert_markdown_card(vdir / "staging" / "opportunities" / "OPP-005.md", OpportunitySpec(id="OPP-005", title="Staged 5", status="staging", **kw), "Staged 5.", human_notes_override="HN-STG-5")
    stg6_p = upsert_markdown_card(vdir / "staging" / "opportunities" / "OPP-006.md", OpportunitySpec(id="OPP-006", title="Staged 6", status="staging", **kw), "Staged 6.", human_notes_override="HN-STG-6")
    q_p, pb_p = vdir / "questions" / "open-questions.md", vdir / "questions" / "day1-90-playbook.md"
    q_init, pb_init = parse_markdown_card(q_p), parse_markdown_card(pb_p)
    upsert_markdown_card(q_p, q_init.frontmatter, q_init.body, human_notes_override="HN-Q-BANK")
    upsert_markdown_card(pb_p, pb_init.frontmatter, pb_init.body, human_notes_override="HN-PB-TRACKER")
    pb_valid_bytes = pb_p.read_bytes()
    bin_fm, bin_body, bin_stg = vdir / "opportunities" / "000-cp1252.md", vdir / "opportunities" / "000-bin.md", vdir / "staging" / "opportunities" / "000-cp1252.md"
    bin_fm.write_bytes(b"---\nid: OPP-BIN-FM\ntitle: \x93Note\x94\n---\n")
    bin_body.write_bytes(b"---\nid: OPP-BIN-BODY\ntitle: Good\n---\n\xff\xfe\x80\n")
    bin_stg.write_bytes(b"---\nid: OPP-STG-BIN\ntitle: \x93Draft\x94\n---\n\xff\xfe\x80\n")
    pb_bad = b"---\nid: PLAYBOOK\n---\n\x93quote\x94\xff\xfe\n"
    pb_p.write_bytes(pb_bad)
    fm_b, body_b, stg_b = bin_fm.read_bytes(), bin_body.read_bytes(), bin_stg.read_bytes()
    with TestClient(create_app(vault_dir=vdir), raise_server_exceptions=False) as client:
        assert client.post("/api/boss-pin", json={"id": "OPP-001", "boss_mandate": True}).status_code == 200
        assert client.post("/api/promote", json={"id": "OPP-005", "target_status": "backlog"}).status_code == 200
        assert client.post("/api/promote", json={"id": "OPP-006", "action": "archive", "reason": "dup"}).status_code == 200
        assert client.post("/api/archive", json={"id": "OPP-001", "reason": "done"}).status_code == 200
        assert client.patch("/api/questions/Q-MGR-01", json={"status": "answered", "answer": "ok"}).status_code == 200
        assert client.post("/api/questions/Q-MGR-02", json={"status": "answered", "answer": "ok2"}).status_code == 200
        assert client.patch("/api/readiness/PHASE-1", json={"completed": True}).status_code == 422
        assert client.patch("/api/questions/Q-MISSING-99", json={"status": "answered"}).status_code == 404
        assert parse_markdown_card(live_p).human_notes == "HN-LIVE-1" and parse_markdown_card(q_p).human_notes == "HN-Q-BANK"
        assert parse_markdown_card(vdir / "opportunities" / stg5_p.name).human_notes == "HN-STG-5"
        assert parse_markdown_card(vdir / "opportunities" / stg6_p.name).human_notes == "HN-STG-6"
        for tid in ("OPP-BIN-FM", "OPP-BIN-BODY", "OPP-STG-BIN"):
            assert client.post("/api/boss-pin", json={"id": tid, "boss_mandate": True}).status_code == 422
            assert client.post("/api/archive", json={"id": tid, "reason": "x"}).status_code == 422
            assert client.get(f"/api/brief/{tid}").status_code == 422
        assert client.post("/api/promote", json={"id": "OPP-STG-BIN", "target_status": "backlog"}).status_code == 422
        assert bin_fm.read_bytes() == fm_b and bin_body.read_bytes() == body_b and bin_stg.read_bytes() == stg_b and pb_p.read_bytes() == pb_bad
        pb_p.write_bytes(pb_valid_bytes)
        q_bad = b"---\nid: QUESTION-BANK\ntitle: \x93bad\x94\n---\n\xff\xfe\x80\n"
        q_p.write_bytes(q_bad)
        assert client.patch("/api/readiness/PHASE-1", json={"completed": True}).status_code == 200
        assert client.post("/api/readiness/PHASE-2", json={"status": "completed"}).status_code == 200
        assert client.patch("/api/questions/Q-MGR-01", json={"status": "answered"}).status_code == 422
        assert client.patch("/api/readiness/PHASE-99", json={"completed": True}).status_code == 404
        assert parse_markdown_card(pb_p).human_notes == "HN-PB-TRACKER" and q_p.read_bytes() == q_bad


def test_corrupt_card_id_decoys_and_live_collision_isolation(tmp_path: Path) -> None:
    """Corrupt-card decoys (comments, title quotes, nested/list/flow/body/HUMAN-NOTES) return 404 while real top-level IDs return 422 and guard promote collisions."""
    vdir = tmp_path / "vault"
    initialize_vault(vdir, seed_questions=True, init_git=False)
    kw = dict(domain_pack="ops_supply_chain", valuation=_make_valuation(), effort_weeks=3.0, data_readiness_score=4, adoption_friction_score=2, created_at=date(2026, 2, 1), updated_at=date(2026, 2, 2))
    stg_col = upsert_markdown_card(vdir / "staging" / "opportunities" / "stg_collide.md", OpportunitySpec(id="OPP-REAL-1", title="Collide", status="staging", **kw), "Body.", human_notes_override="HN-COL")
    stg_dec = upsert_markdown_card(vdir / "staging" / "opportunities" / "stg_decoy_ok.md", OpportunitySpec(id="OPP-COMMENT-DECOY", title="Decoy Staged", status="staging", **kw), "Body.", human_notes_override="HN-DEC-OK")
    c1_p = vdir / "opportunities" / "c1.md"
    c1_p.write_text("---\n# note, id: OPP-COMMENT-DECOY\nid: OPP-REAL-1\neffort_weeks: [unclosed\n", encoding="utf-8")
    (vdir / "opportunities" / "c2.md").write_text('---\ntitle: "x, id: OPP-TITLE-DECOY }"\nid: OPP-REAL-2\neffort_weeks: [unclosed\n---\n', encoding="utf-8")
    (vdir / "opportunities" / "c3.md").write_text("---\nmeta:\n  id: OPP-NESTED-DECOY\nid: OPP-REAL-3\neffort_weeks: [unclosed\n---\n", encoding="utf-8")
    (vdir / "opportunities" / "c4.md").write_text("---\n- name: x\n  id: OPP-LIST-DECOY\n---\n", encoding="utf-8")
    (vdir / "opportunities" / "c5_flow.md").write_text('---\n{title: "x, id: OPP-FLOW-DECOY", meta: {id: OPP-FLOW-NESTED}, id: OPP-FLOW-REAL, effort_weeks: [unclosed\n---\n', encoding="utf-8")
    (vdir / "opportunities" / "c6_body.md").write_text("---\ntitle: [unclosed\n\n## Details\nid: OPP-BODY-DECOY\n\n---\n", encoding="utf-8")
    (vdir / "opportunities" / "trunc_hn_hr.md").write_text("---\ntitle: [unclosed\n<!-- HUMAN-NOTES-START -->\nid: OPP-DECOY-HN\n---\n<!-- HUMAN-NOTES-END -->\n", encoding="utf-8")
    (vdir / "opportunities" / "cp_decoy.md").write_bytes(b"---\ntitle: \x93x, id: OPP-CP-DECOY\x94\nid: OPP-REAL-CP\n---\n")
    c1_before, col_before = c1_p.read_bytes(), stg_col.read_bytes()
    with TestClient(create_app(vault_dir=vdir), raise_server_exceptions=False) as client:
        for rid in ("OPP-REAL-1", "OPP-REAL-2", "OPP-REAL-3", "OPP-FLOW-REAL", "OPP-REAL-CP"):
            assert client.post("/api/boss-pin", json={"id": rid, "pinned": True}).status_code == 422
            assert client.get(f"/api/brief/{rid}").status_code == 422
        for did in ("OPP-TITLE-DECOY", "OPP-NESTED-DECOY", "OPP-LIST-DECOY", "OPP-FLOW-DECOY", "OPP-FLOW-NESTED", "OPP-BODY-DECOY", "OPP-DECOY-HN", "OPP-CP-DECOY"):
            assert client.post("/api/boss-pin", json={"id": did, "pinned": True}).status_code == 404
            assert client.post("/api/archive", json={"id": did}).status_code == 404
            assert client.post("/api/promote", json={"id": did}).status_code == 404
            assert client.get(f"/api/brief/{did}").status_code == 404
        assert client.post("/api/promote", json={"id": "OPP-REAL-1", "target_status": "backlog"}).status_code == 422
        assert c1_p.read_bytes() == c1_before and stg_col.read_bytes() == col_before and not (vdir / "opportunities" / stg_col.name).exists()
        assert client.post("/api/promote", json={"id": "OPP-COMMENT-DECOY", "target_status": "backlog"}).status_code == 200
        assert parse_markdown_card(vdir / "opportunities" / stg_dec.name).human_notes == "HN-DEC-OK" and c1_p.read_bytes() == c1_before
