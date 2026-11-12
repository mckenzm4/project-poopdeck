"""Requirement acceptance tests for F11: Zero-Node FastAPI Local Command Center UI & REST API.

Covers:
- F11 Tier 1 (6 tests): Embedded zero-CDN offline SPA (`GET /`, `/static/*`) & `/api/health`,
  `/api/portfolio` 12.0-week cutline displacement & domain/archived filtering, `/api/staging` +
  `POST /api/promote` + `POST /api/archive` disk writebacks preserving `HUMAN-NOTES` & CFO critique,
  `POST /api/boss-pin` unpin/pin/toggle cutline recalculation preserving `HUMAN-NOTES`,
  `GET /api/lineage` 6-entity graph + `/api/questions` & `/api/readiness` writeback preserving
  `HUMAN-NOTES`, and `GET`/`POST /api/brief` formats/types + isolated `exports/` file generation.
- F11 Tier 2 (6 tests): HTTP `404` on unknown/already-live IDs with zero disk mutation, boss-mandate
  capacity overflow (`over_capacity=True`), unresolved `[[wikilink]]` placeholder nodes
  (`exists_in_vault=False`), HTTP `400`/`422` validation on malformed query/JSON payloads, path
  traversal rejection (`../secret`), and graceful uninitialized `vault_dir` reads + `FileNotFoundError`
  on missing explicit `config_path`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
import shutil
from typing import Any

import pytest


def _seed_demo_sandbox(project_root: Path, vault_sandbox: Path) -> Path:
    """Copy `examples/demo_vault` into isolated `vault_sandbox` (`tmp_path / "vault"`)."""
    demo_src = project_root / "examples" / "demo_vault"
    assert demo_src.is_dir(), f"Missing demo vault at {demo_src}"
    shutil.copytree(demo_src, vault_sandbox, dirs_exist_ok=True)
    return vault_sandbox


def _inject_human_notes(card_path: Path, custom_notes: str) -> str:
    """Replace the `HUMAN-NOTES` inner block of `card_path` and return exact inner text."""
    from poopdeck.vault_io import HUMAN_NOTES_END, HUMAN_NOTES_START, parse_markdown_card

    raw = card_path.read_text(encoding="utf-8")
    assert HUMAN_NOTES_START in raw and HUMAN_NOTES_END in raw
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


def _vault_digest_manifest(vault_dir: Path) -> dict[str, str]:
    """Compute a relative POSIX path -> SHA-256 map across all files in `vault_dir`."""
    return {
        p.relative_to(vault_dir).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(vault_dir.rglob("*"))
        if p.is_file()
    }


# ============================================================================
# F11 Tier 1 — Zero-Node FastAPI Local Command Center UI & REST API (6 tests)
# ============================================================================


@pytest.mark.tier1
def test_f11_t1_spa_index_static_assets_zero_cdn_and_health_endpoint(
    project_root: Path,
    vault_sandbox: Path,
) -> None:
    """Verify offline zero-CDN SPA HTML/CSS/JS assets, 5 views, SVG matrix, and `/api/health`."""
    from fastapi.testclient import TestClient
    from poopdeck.ui.app import create_app

    _seed_demo_sandbox(project_root, vault_sandbox)
    client = TestClient(create_app(vault_sandbox))

    resp_root = client.get("/")
    assert resp_root.status_code == 200
    assert "text/html" in resp_root.headers.get("content-type", "").lower()
    assert resp_root.headers.get("x-content-type-options") == "nosniff"
    html = resp_root.text

    for expected_view in (
        "panel-portfolio",
        "panel-staging",
        "panel-lineage",
        "panel-readiness",
        "panel-briefs",
    ):
        assert expected_view in html, f"Missing Command Center view section: {expected_view}"
    assert "<svg" in html.lower() and "roi-matrix-svg" in html

    resp_css = client.get("/static/styles.css")
    assert resp_css.status_code == 200
    assert "text/css" in resp_css.headers.get("content-type", "").lower()
    assert len(resp_css.text.strip()) > 100

    resp_js = client.get("/static/app.js")
    assert resp_js.status_code == 200
    assert "javascript" in resp_js.headers.get("content-type", "").lower()
    assert len(resp_js.text.strip()) > 100

    combined_assets = "\n".join([html, resp_css.text, resp_js.text]).lower()
    for forbidden_token in (
        "http://",
        "https://",
        "unpkg",
        "jsdelivr",
        "tailwindcss",
        "googleapis",
    ):
        assert forbidden_token not in combined_assets, (
            f"R5 offline SPA violation — external reference {forbidden_token!r} found in static assets"
        )

    resp_health = client.get("/api/health")
    assert resp_health.status_code == 200
    health = resp_health.json()
    assert health["status"] == "ok"
    assert health["vault_exists"] is True
    assert Path(health["vault_dir"]).resolve() == vault_sandbox.resolve()
    assert health["quarterly_capacity_weeks"] == pytest.approx(12.0)


@pytest.mark.tier1
def test_f11_t1_portfolio_cutline_math_domain_filter_and_include_archived(
    project_root: Path,
    vault_sandbox: Path,
) -> None:
    """Verify `/api/portfolio` 12.0w cutline displacement, domain filters, and `include_archived`."""
    from fastapi.testclient import TestClient
    from poopdeck.ui.app import create_app

    _seed_demo_sandbox(project_root, vault_sandbox)
    client = TestClient(create_app(vault_sandbox))

    resp = client.get("/api/portfolio?capacity_weeks=12.0&domain=all&include_archived=false")
    assert resp.status_code == 200
    data = resp.json()

    assert data["capacity_weeks"] == pytest.approx(12.0)
    assert data["used_capacity_weeks"] == pytest.approx(11.5)
    assert data["mandate_weeks"] == pytest.approx(5.0)
    assert data["remaining_capacity_weeks"] == pytest.approx(0.5)
    assert data["over_capacity"] is False
    assert data["funded_ids"] == ["OPP-002", "OPP-001", "OPP-004"]
    assert data["displaced_ids"] == ["OPP-003"]
    assert data["staging_count"] == 2

    by_id: dict[str, dict[str, Any]] = {item["id"]: item for item in data["opportunities"]}
    assert set(by_id.keys()) == {"OPP-001", "OPP-002", "OPP-003", "OPP-004"}
    assert by_id["OPP-002"]["boss_mandate"] is True
    assert by_id["OPP-002"]["above_cutline"] is True
    assert by_id["OPP-002"]["baseline_above_cutline"] is False
    assert by_id["OPP-003"]["above_cutline"] is False
    assert by_id["OPP-003"]["baseline_above_cutline"] is True
    assert by_id["OPP-003"]["displaced_by_mandate"] is True

    resp_ops = client.get("/api/portfolio?domain=ops_supply_chain")
    assert resp_ops.status_code == 200
    assert {it["id"] for it in resp_ops.json()["opportunities"]} == {
        "OPP-001",
        "OPP-002",
        "OPP-003",
        "OPP-004",
    }

    resp_fin_before = client.get("/api/portfolio?domain=finance_risk")
    assert resp_fin_before.status_code == 200
    assert resp_fin_before.json()["opportunities"] == []

    # Promote OPP-006 (finance_risk), verify domain filter, then archive and test include_archived
    prom = client.post("/api/promote", json={"id": "OPP-006"})
    assert prom.status_code == 200
    resp_fin_after = client.get("/api/portfolio?domain=finance_risk")
    assert resp_fin_after.status_code == 200
    assert [it["id"] for it in resp_fin_after.json()["opportunities"]] == ["OPP-006"]

    arch = client.post("/api/archive", json={"id": "OPP-006", "reason": "Defer external credit feed"})
    assert arch.status_code == 200

    resp_no_arch = client.get("/api/portfolio?include_archived=false")
    assert "OPP-006" not in {it["id"] for it in resp_no_arch.json()["opportunities"]}

    resp_with_arch = client.get("/api/portfolio?include_archived=true")
    assert resp_with_arch.status_code == 200
    arch_map = {it["id"]: it for it in resp_with_arch.json()["opportunities"]}
    assert "OPP-006" in arch_map
    assert arch_map["OPP-006"]["status"] == "archived"
    assert arch_map["OPP-006"]["above_cutline"] is False


@pytest.mark.tier1
def test_f11_t1_staging_list_promote_and_archive_disk_roundtrip_preserves_human_notes(
    project_root: Path,
    vault_sandbox: Path,
) -> None:
    """Verify `/api/staging`, `/api/promote`, and `/api/archive` persist cleanly & keep HUMAN-NOTES."""
    from fastapi.testclient import TestClient
    from poopdeck.ui.app import create_app
    from poopdeck.vault_io import parse_markdown_card, validate_vault

    _seed_demo_sandbox(project_root, vault_sandbox)

    opp5_staged = next((vault_sandbox / "staging" / "opportunities").glob("OPP-005-*.md"))
    opp6_staged = next((vault_sandbox / "staging" / "opportunities").glob("OPP-006-*.md"))

    notes_opp5 = (
        "### Reviewer Notes (OPP-005)\n"
        "- Local notebook check: `C:\\Users\\ds\\reviews\\opp_005_check.py`\n"
        "- Follow up with [[Marcus Vance]] and [[Elena Rostova]] on carrying cost %\n"
        "| Check | Verdict |\n| --- | --- |\n| Holdout | Passed |"
    )
    notes_opp6 = (
        "### Reviewer Notes (OPP-006)\n"
        "- Path ref: `C:\\Temp\\dnb_quote_2026.xlsx`\n"
        "- Wait until Q2 procurement window before requesting D&B API budget."
    )
    expected_notes_5 = _inject_human_notes(opp5_staged, notes_opp5)
    expected_notes_6 = _inject_human_notes(opp6_staged, notes_opp6)
    opp5_before = parse_markdown_card(opp5_staged)
    assert "## Skeptical CFO Critique" in opp5_before.body

    client = TestClient(create_app(vault_sandbox))

    stg_initial = client.get("/api/staging")
    assert stg_initial.status_code == 200
    stg_data = stg_initial.json()
    assert stg_data["count"] == 2
    assert {item["id"] for item in stg_data["opportunities"]} == {"OPP-005", "OPP-006"}

    resp_promote = client.post("/api/promote", json={"id": "OPP-005"})
    assert resp_promote.status_code == 200
    prom_json = resp_promote.json()
    assert prom_json["new_status"] == "backlog"
    assert not opp5_staged.exists()

    opp5_live = vault_sandbox / "opportunities" / opp5_staged.name
    assert opp5_live.is_file()
    opp5_after = parse_markdown_card(opp5_live)
    assert opp5_after.frontmatter["status"] == "backlog"
    assert opp5_after.human_notes == expected_notes_5
    assert "## Skeptical CFO Critique" in opp5_after.body

    stg_mid = client.get("/api/staging").json()
    assert stg_mid["count"] == 1
    assert [item["id"] for item in stg_mid["opportunities"]] == ["OPP-006"]
    assert "OPP-005" in {
        item["id"] for item in client.get("/api/portfolio").json()["opportunities"]
    }

    archive_reason = "SQL aging bucket view in Power BI covers 85% of value without D&B fee"
    resp_archive = client.post(
        "/api/archive",
        json={"id": "OPP-006", "reason": archive_reason},
    )
    assert resp_archive.status_code == 200
    arch_json = resp_archive.json()
    assert arch_json["status"] == "archived"
    assert not opp6_staged.exists()

    opp6_archived = vault_sandbox / "opportunities" / opp6_staged.name
    assert opp6_archived.is_file()
    opp6_after = parse_markdown_card(opp6_archived)
    assert opp6_after.frontmatter["status"] == "archived"
    assert archive_reason in opp6_after.raw_text
    assert opp6_after.human_notes == expected_notes_6

    assert client.get("/api/staging").json()["count"] == 0
    val_report = validate_vault(vault_sandbox, strict=True)
    assert val_report.valid is True, f"Vault validation errors: {val_report.errors}"


@pytest.mark.tier1
def test_f11_t1_boss_pin_unpin_pin_and_null_toggle_persists_and_preserves_human_notes(
    project_root: Path,
    vault_sandbox: Path,
) -> None:
    """Verify `POST /api/boss-pin` unpins, pins, and null-toggles P0 mandates preserving HUMAN-NOTES."""
    from fastapi.testclient import TestClient
    from poopdeck.ui.app import create_app
    from poopdeck.vault_io import parse_markdown_card

    _seed_demo_sandbox(project_root, vault_sandbox)
    opp2_path = next((vault_sandbox / "opportunities").glob("OPP-002-*.md"))
    opp3_path = next((vault_sandbox / "opportunities").glob("OPP-003-*.md"))

    expected_notes_2 = _inject_human_notes(
        opp2_path,
        "VP mandate check `C:\\Ops\\board_deck.pptx` — confirm carrier penalty clause.",
    )
    expected_notes_3 = _inject_human_notes(
        opp3_path,
        "OTIF triage notes with [[Sarah Jenkins]] — requires hourly EDI feed.",
    )

    client = TestClient(create_app(vault_sandbox))

    # 1. Unpin OPP-002 -> pure ROI baseline funds OPP-001 (3.0w) + OPP-004 (3.5w) + OPP-003 (4.5w) = 11.0w
    r_unpin = client.post(
        "/api/boss-pin",
        json={"id": "OPP-002", "boss_mandate": False, "capacity_weeks": 12.0},
    )
    assert r_unpin.status_code == 200
    unpin_data = r_unpin.json()
    assert unpin_data["boss_mandate"] is False
    assert unpin_data["funded_ids"] == ["OPP-001", "OPP-004", "OPP-003"]
    assert unpin_data["displaced_ids"] == []
    assert unpin_data["mandate_weeks"] == pytest.approx(0.0)
    assert parse_markdown_card(opp2_path).frontmatter["boss_mandate"] is False
    assert parse_markdown_card(opp2_path).human_notes == expected_notes_2

    # 2. Pin OPP-003 explicitly (`boss_mandate: true`) -> moves OPP-003 to rank 1 P0
    r_pin3 = client.post(
        "/api/boss-pin",
        json={"id": "OPP-003", "boss_mandate": True, "capacity_weeks": 12.0},
    )
    assert r_pin3.status_code == 200
    pin3_data = r_pin3.json()
    assert pin3_data["boss_mandate"] is True
    assert pin3_data["funded_ids"][0] == "OPP-003"
    assert set(pin3_data["funded_ids"]) == {"OPP-003", "OPP-001", "OPP-004"}
    assert parse_markdown_card(opp3_path).frontmatter["boss_mandate"] is True
    assert parse_markdown_card(opp3_path).human_notes == expected_notes_3

    # 3. Toggle OPP-002 via `boss_mandate: null` -> True (mandates: 4.5 + 5.0 = 9.5w; 2.5w left displaces OPP-001 & OPP-004)
    r_toggle2 = client.post(
        "/api/boss-pin",
        json={"id": "OPP-002", "boss_mandate": None, "capacity_weeks": 12.0},
    )
    assert r_toggle2.status_code == 200
    tog_data = r_toggle2.json()
    assert tog_data["boss_mandate"] is True
    assert tog_data["mandate_weeks"] == pytest.approx(9.5)
    assert tog_data["funded_ids"] == ["OPP-003", "OPP-002"]
    assert set(tog_data["displaced_ids"]) == {"OPP-001", "OPP-004"}
    assert parse_markdown_card(opp2_path).frontmatter["boss_mandate"] is True
    assert parse_markdown_card(opp2_path).human_notes == expected_notes_2


@pytest.mark.tier1
def test_f11_t1_lineage_6_node_types_and_questions_readiness_writeback_preserves_human_notes(
    project_root: Path,
    vault_sandbox: Path,
) -> None:
    """Verify `/api/lineage` 6 node types and `/api/questions` + `/api/readiness` disk writebacks."""
    from fastapi.testclient import TestClient
    from poopdeck.ui.app import create_app
    from poopdeck.vault_io import parse_markdown_card

    _seed_demo_sandbox(project_root, vault_sandbox)
    q_path = vault_sandbox / "questions" / "open-questions.md"
    pb_path = vault_sandbox / "questions" / "day1-90-playbook.md"
    expected_q_notes = _inject_human_notes(
        q_path,
        "Finance 1:1 notes stored at `C:\\Users\\ds\\notes\\finance_q1.md` with [[Elena Rostova]].",
    )
    expected_pb_notes = _inject_human_notes(
        pb_path,
        "Day 15 checkpoint reviewed with manager [[Marcus Vance]].",
    )

    client = TestClient(create_app(vault_sandbox))

    lin_with_stg = client.get("/api/lineage?include_staging=true")
    assert lin_with_stg.status_code == 200
    lin_data = lin_with_stg.json()
    node_types = {n["type"] for n in lin_data["nodes"]}
    assert node_types == {
        "external_data",
        "dataset",
        "opportunity",
        "existing_report",
        "new_report",
        "stakeholder",
    }
    assert len(lin_data["edges"]) >= 15
    assert all(n["exists_in_vault"] is True for n in lin_data["nodes"])
    node_ids_with_stg = {n["id"] for n in lin_data["nodes"]}
    assert "opportunity:OPP-005" in node_ids_with_stg
    assert "opportunity:OPP-006" in node_ids_with_stg

    lin_no_stg = client.get("/api/lineage?include_staging=false")
    assert lin_no_stg.status_code == 200
    node_ids_no_stg = {n["id"] for n in lin_no_stg.json()["nodes"]}
    assert "opportunity:OPP-005" not in node_ids_no_stg
    assert "opportunity:OPP-006" not in node_ids_no_stg
    assert "opportunity:OPP-001" in node_ids_no_stg

    r_all_q = client.get("/api/questions")
    r_readiness = client.get("/api/readiness")
    assert r_all_q.status_code == 200
    assert r_readiness.status_code == 200
    assert len(r_all_q.json()["questions"]) == 15
    assert len(r_readiness.json()["phases"]) == 5

    r_filtered = client.get("/api/questions?category=manager&status=open")
    assert r_filtered.status_code == 200
    filtered_qs = r_filtered.json()["questions"]
    assert len(filtered_qs) == 3
    assert all(q["category"] == "manager" and q["status"] == "open" for q in filtered_qs)

    r_patch_q = client.patch(
        "/api/questions/Q-FIN-01",
        json={"status": "answered", "answer": "Confirmed 38% unit margin"},
    )
    assert r_patch_q.status_code == 200
    assert r_patch_q.json()["status"] == "answered"
    assert r_patch_q.json()["answer_summary"] == "Confirmed 38% unit margin"

    r_post_phase = client.post("/api/questions/PHASE-2", json={"completed": True})
    assert r_post_phase.status_code == 200
    assert r_post_phase.json()["completed"] is True

    parsed_q = parse_markdown_card(q_path)
    q_fin_01 = next(q for q in parsed_q.frontmatter["questions"] if q["id"] == "Q-FIN-01")
    assert q_fin_01["status"] == "answered"
    assert q_fin_01["answer_summary"] == "Confirmed 38% unit margin"
    assert "- [x] **Q-FIN-01" in parsed_q.body
    assert parsed_q.human_notes == expected_q_notes

    parsed_pb = parse_markdown_card(pb_path)
    phase_2 = next(p for p in parsed_pb.frontmatter["phases"] if p["id"] == "PHASE-2")
    assert phase_2["completed"] is True
    assert "[x]" in parsed_pb.body
    assert parsed_pb.human_notes == expected_pb_notes


@pytest.mark.tier1
def test_f11_t1_brief_endpoints_formats_types_and_isolated_export_file(
    project_root: Path,
    vault_sandbox: Path,
) -> None:
    """Verify `GET`/`POST /api/brief` across opportunity/weekly_status/verify_questions & exports."""
    from fastapi.testclient import TestClient
    from poopdeck.ui.app import create_app

    _seed_demo_sandbox(project_root, vault_sandbox)
    client = TestClient(create_app(vault_sandbox))

    r_default = client.get("/api/brief")
    assert r_default.status_code == 200
    d_default = r_default.json()
    assert d_default["brief_type"] == "weekly_status"
    assert d_default["saved_path"] is None
    assert "OPP-002" in d_default["markdown"]
    assert "OPP-003" in d_default["markdown"]

    r_opp_exec = client.get("/api/brief/OPP-004?format=exec")
    assert r_opp_exec.status_code == 200
    d_opp_exec = r_opp_exec.json()
    assert d_opp_exec["opp_id"] == "OPP-004"
    assert d_opp_exec["brief_type"] == "opportunity"
    assert d_opp_exec["format"] == "exec"
    assert d_opp_exec["saved_path"] is None
    assert "Executive Opportunity Brief" in d_opp_exec["markdown"]
    assert "$331,500" in d_opp_exec["markdown"]
    assert "[VERIFY WITH STAKEHOLDER]" in d_opp_exec["markdown"]

    r_post_teams = client.post(
        "/api/brief",
        json={
            "opp_id": "OPP-004",
            "brief_type": "opportunity",
            "format": "teams",
            "save_export": False,
        },
    )
    assert r_post_teams.status_code == 200
    assert r_post_teams.json()["saved_path"] is None
    assert "[OPP-004]" in r_post_teams.json()["markdown"]
    assert r_post_teams.json()["markdown"] != d_opp_exec["markdown"]

    r_post_export = client.post(
        "/api/brief",
        json={
            "opp_id": "OPP-004",
            "brief_type": "opportunity",
            "format": "exec",
            "save_export": True,
        },
    )
    assert r_post_export.status_code == 200
    saved_path_str = r_post_export.json()["saved_path"]
    assert saved_path_str is not None
    saved_file = Path(saved_path_str)
    assert saved_file.is_file()
    assert saved_file.resolve().parent == (vault_sandbox.resolve().parent / "exports")
    raw_export = saved_file.read_bytes()
    assert not raw_export.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw_export

    r_post_weekly = client.post(
        "/api/brief",
        json={"brief_type": "weekly_status", "capacity_weeks": 12.0, "save_export": True},
    )
    assert r_post_weekly.status_code == 200
    assert r_post_weekly.json()["saved_path"] is not None
    assert Path(r_post_weekly.json()["saved_path"]).is_file()

    r_post_verify = client.post(
        "/api/brief",
        json={"opp_id": "OPP-006", "brief_type": "verify_questions", "save_export": False},
    )
    assert r_post_verify.status_code == 200
    assert r_post_verify.json()["saved_path"] is None
    assert r_post_verify.json()["markdown"].count("[VERIFY WITH STAKEHOLDER]") >= 3


# ============================================================================
# F11 Tier 2 — Negative, Boundary, Validation & Isolation Edge Cases (6 tests)
# ============================================================================


@pytest.mark.tier2
def test_f11_t2_unknown_resource_ids_and_already_live_promote_return_404_without_mutating_vault(
    project_root: Path,
    vault_sandbox: Path,
) -> None:
    """Verify unknown IDs & promoting an already-live opportunity return 404 with 0 disk mutations."""
    from fastapi.testclient import TestClient
    from poopdeck.ui.app import create_app

    _seed_demo_sandbox(project_root, vault_sandbox)
    before_digests = _vault_digest_manifest(vault_sandbox)
    client = TestClient(create_app(vault_sandbox))

    checks = [
        client.post("/api/promote", json={"id": "OPP-999"}),
        client.post("/api/archive", json={"id": "OPP-999", "reason": "unknown"}),
        client.post("/api/boss-pin", json={"id": "OPP-999", "boss_mandate": True}),
        client.patch(
            "/api/questions/Q-UNKNOWN-99",
            json={"status": "answered", "answer": "N/A"},
        ),
        client.post("/api/questions/PHASE-99", json={"completed": True}),
        client.get("/api/brief/OPP-999"),
        client.post("/api/brief", json={"opp_id": "OPP-999", "brief_type": "opportunity"}),
        client.post("/api/promote", json={"id": "OPP-001"}),
    ]
    for resp in checks:
        assert resp.status_code == 404, (
            f"Expected 404 for {resp.request.method} {resp.request.url}, got {resp.status_code}: {resp.text}"
        )

    after_digests = _vault_digest_manifest(vault_sandbox)
    assert after_digests == before_digests


@pytest.mark.tier2
def test_f11_t2_boss_mandate_capacity_overflow_returns_200_with_over_capacity_and_displacement(
    project_root: Path,
    vault_sandbox: Path,
) -> None:
    """Verify mandate weeks exceeding `capacity_weeks` flags `over_capacity` & keeps mandates funded."""
    from fastapi.testclient import TestClient
    from poopdeck.ui.app import create_app

    _seed_demo_sandbox(project_root, vault_sandbox)
    client = TestClient(create_app(vault_sandbox))

    # OPP-002 (5.0w) is already boss_mandate=True; pin OPP-003 (4.5w) -> 9.5w mandates vs 6.0w budget
    resp_pin = client.post(
        "/api/boss-pin",
        json={"id": "OPP-003", "boss_mandate": True, "capacity_weeks": 6.0},
    )
    assert resp_pin.status_code == 200
    pin_json = resp_pin.json()
    assert pin_json["over_capacity"] is True
    assert pin_json["mandate_weeks"] == pytest.approx(9.5)
    assert pin_json["overflow_weeks"] == pytest.approx(3.5)
    assert pin_json["remaining_capacity_weeks"] == pytest.approx(0.0)
    assert set(pin_json["funded_ids"]) == {"OPP-003", "OPP-002"}
    assert "OPP-001" in pin_json["displaced_ids"]

    resp_port = client.get("/api/portfolio?capacity_weeks=6.0")
    assert resp_port.status_code == 200
    port_json = resp_port.json()
    assert port_json["over_capacity"] is True
    assert port_json["overflow_weeks"] == pytest.approx(3.5)
    assert port_json["remaining_capacity_weeks"] == pytest.approx(0.0)
    by_id = {it["id"]: it for it in port_json["opportunities"]}
    assert by_id["OPP-002"]["above_cutline"] is True
    assert by_id["OPP-003"]["above_cutline"] is True
    assert by_id["OPP-001"]["above_cutline"] is False
    assert by_id["OPP-001"]["baseline_above_cutline"] is True
    assert by_id["OPP-001"]["displaced_by_mandate"] is True


@pytest.mark.tier2
def test_f11_t2_unresolved_wikilinks_render_placeholder_nodes_with_exists_in_vault_false(
    project_root: Path,
    vault_sandbox: Path,
) -> None:
    """Verify unresolved dataset/report `[[wikilinks]]` return 200 with `exists_in_vault=False`."""
    from fastapi.testclient import TestClient
    from poopdeck.models import InternalDatasetUse, OpportunitySpec, ReportSupplement
    from poopdeck.ui.app import create_app
    from poopdeck.vault_io import parse_markdown_card, upsert_markdown_card

    _seed_demo_sandbox(project_root, vault_sandbox)
    opp4_path = next((vault_sandbox / "opportunities").glob("OPP-004-*.md"))
    parsed = parse_markdown_card(opp4_path)
    opp4 = OpportunitySpec.model_validate(parsed.frontmatter)

    opp4.internal_datasets.append(
        InternalDatasetUse(
            ref="[[missing_ghost_dataset]]",
            role="Uncrawled future feature table",
            transformations_needed=["Join on sku_id"],
        )
    )
    opp4.existing_reports_supplemented.append(
        ReportSupplement(
            ref="[[Missing Ghost Report]]",
            integration_mode="Planned Q2 report tab",
        )
    )
    upsert_markdown_card(opp4_path, opp4, parsed.body)

    client = TestClient(create_app(vault_sandbox))
    resp = client.get("/api/lineage")
    assert resp.status_code == 200
    lineage = resp.json()

    nodes_by_label = {n["label"]: n for n in lineage["nodes"]}
    assert "missing_ghost_dataset" in nodes_by_label
    assert nodes_by_label["missing_ghost_dataset"]["exists_in_vault"] is False
    assert nodes_by_label["missing_ghost_dataset"]["type"] == "dataset"

    assert "Missing Ghost Report" in nodes_by_label
    assert nodes_by_label["Missing Ghost Report"]["exists_in_vault"] is False
    assert nodes_by_label["Missing Ghost Report"]["type"] == "existing_report"

    edge_pairs = {(e["source"], e["target"]) for e in lineage["edges"]}
    assert (nodes_by_label["missing_ghost_dataset"]["id"], "opportunity:OPP-004") in edge_pairs
    assert ("opportunity:OPP-004", nodes_by_label["Missing Ghost Report"]["id"]) in edge_pairs


@pytest.mark.tier2
def test_f11_t2_invalid_payloads_and_query_parameters_return_400_or_422(
    project_root: Path,
    vault_sandbox: Path,
) -> None:
    """Verify malformed query params and invalid JSON request bodies return HTTP 400/422."""
    from fastapi.testclient import TestClient
    from poopdeck.ui.app import create_app

    _seed_demo_sandbox(project_root, vault_sandbox)
    client = TestClient(create_app(vault_sandbox))

    invalid_requests = [
        client.get("/api/portfolio?capacity_weeks=0"),
        client.get("/api/portfolio?capacity_weeks=-5"),
        client.post("/api/boss-pin", json={"id": "OPP-001", "boss_mandate": "true"}),
        client.post(
            "/api/boss-pin",
            json={"id": "OPP-001", "boss_mandate": True, "capacity_weeks": -1},
        ),
        client.post("/api/promote", json={"id": "OPP-005", "target_status": "staging"}),
        client.get("/api/questions?status=invalid_state"),
        client.patch("/api/questions/Q-FIN-01", json={}),
        client.patch("/api/questions/Q-FIN-01", json={"status": "completed"}),
        client.post("/api/brief", json={"brief_type": "opportunity"}),
        client.post(
            "/api/brief",
            json={"opp_id": "OPP-001", "brief_type": "opportunity", "format": "pdf"},
        ),
    ]
    for resp in invalid_requests:
        assert resp.status_code in (400, 422), (
            f"Expected 400/422 for {resp.request.method} {resp.request.url}, "
            f"got {resp.status_code}: {resp.text}"
        )


@pytest.mark.tier2
def test_f11_t2_path_traversal_and_injection_ids_rejected_without_escaping_sandbox(
    project_root: Path,
    vault_sandbox: Path,
    tmp_path: Path,
) -> None:
    """Verify `../secret` and `OPP-001/../../evil` IDs are rejected without escaping `vault_sandbox`."""
    from fastapi.testclient import TestClient
    from poopdeck.ui.app import create_app

    _seed_demo_sandbox(project_root, vault_sandbox)
    canary_outside = tmp_path / "secret.md"
    canary_bytes = b"TOP_SECRET_OUTSIDE_VAULT\n"
    canary_outside.write_bytes(canary_bytes)

    before_digests = _vault_digest_manifest(vault_sandbox)
    client = TestClient(create_app(vault_sandbox))

    for malicious_id in ("../secret", "OPP-001/../../evil", "..\\secret"):
        for resp in (
            client.post("/api/promote", json={"id": malicious_id}),
            client.post("/api/archive", json={"id": malicious_id, "reason": "probe"}),
            client.post("/api/boss-pin", json={"id": malicious_id, "boss_mandate": True}),
            client.post(
                "/api/brief",
                json={
                    "opp_id": malicious_id,
                    "brief_type": "opportunity",
                    "save_export": True,
                },
            ),
        ):
            assert resp.status_code in (400, 404, 422), (
                f"Malicious ID {malicious_id!r} was not rejected ({resp.status_code})"
            )

    assert canary_outside.read_bytes() == canary_bytes
    assert not (tmp_path / "exports").exists()
    assert _vault_digest_manifest(vault_sandbox) == before_digests


@pytest.mark.tier2
def test_f11_t2_uninitialized_vault_returns_200_on_reads_and_missing_config_raises_file_not_found(
    vault_sandbox: Path,
    tmp_path: Path,
) -> None:
    """Verify missing `vault_dir` serves clean 200 read responses while missing `config_path` raises."""
    from fastapi.testclient import TestClient
    from poopdeck.ui.app import create_app

    missing_vault = tmp_path / "nonexistent_vault"
    assert not missing_vault.exists()

    client = TestClient(create_app(missing_vault))
    r_health = client.get("/api/health")
    assert r_health.status_code == 200
    assert r_health.json()["vault_exists"] is False

    for path in (
        "/api/portfolio",
        "/api/staging",
        "/api/lineage",
        "/api/questions",
        "/api/brief",
    ):
        resp = client.get(path)
        assert resp.status_code == 200, f"Read endpoint {path} failed on missing vault: {resp.text}"

    assert not missing_vault.exists()

    with pytest.raises(FileNotFoundError):
        create_app(vault_sandbox, config_path=tmp_path / "missing_cfg.yaml")
