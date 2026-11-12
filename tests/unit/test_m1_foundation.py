"""Comprehensive offline unit tests for Milestone M1 Core Foundation."""

from __future__ import annotations

import copy
import datetime
import inspect
import json
import random
import shutil
import stat
import subprocess
import time
import tomllib
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

import poopdeck
import poopdeck.brief as brief_mod
import poopdeck.config as cfg_mod
import poopdeck.models as mod_mod
import poopdeck.packs as packs_mod
import poopdeck.vault_io as vio_mod
from poopdeck.brief import (
    generate_opportunity_brief,
    generate_portfolio_brief,
    generate_verify_questions_brief,
)
from poopdeck.config import (
    DatabricksConfig,
    PoopdeckConfig,
    PowerBIConfig,
    PrivacyConfig,
    ScoringWeightsConfig,
    load_config,
)
from poopdeck.models import (
    ColumnProfile,
    DatasetCard,
    DaxMeasure,
    DriverSpec,
    ExternalDataReq,
    GlossaryCard,
    GlossaryTerm,
    InternalDatasetUse,
    NewReportSpec,
    OpportunitySpec,
    QuestionItem,
    ReportCard,
    ReportSupplement,
    StakeholderCard,
    ValuationSpec,
    VaultBaseModel,
    compute_portfolio_schedule,
    compute_priority_score,
    evaluate_driver_formula,
)
from poopdeck.packs import (
    ArchetypeSpec,
    DomainLens,
    ExternalDataCatalog,
    ExternalDatasetEntry,
    get_domain_lens,
    get_external_datasets_for_domain,
    list_external_datasets,
    load_external_data_catalog,
)
from poopdeck.redaction import (
    is_pii_column,
    redact_dataset_payload,
    redact_text,
)
from poopdeck.templates import (
    build_default_glossary,
    build_default_playbook,
    build_default_question_bank,
)
from poopdeck.vault_io import (
    HUMAN_NOTES_END,
    HUMAN_NOTES_START,
    ValidationReport,
    archive_opportunity,
    build_lineage_graph,
    clean_git_env,
    extract_wikilinks,
    initialize_vault,
    load_all_opportunities,
    load_questions_and_playbook,
    parse_markdown_card,
    promote_opportunity,
    render_dataset_body,
    render_opportunity_body,
    render_report_body,
    render_stakeholder_body,
    safe_rmtree,
    set_boss_mandate,
    update_question_or_phase,
    upsert_markdown_card,
    validate_vault,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_opp(
    opp_id: str = "OPP-004",
    title: str = "SKU-Location Quantile Stockout Forecast",
    status: str = "backlog",
    boss_mandate: bool = False,
    effort_weeks: float = 3.5,
    readiness: int = 4,
    friction: int = 2,
    confidence: str = "directional",
    verified_all: bool = False,
    supplement_report: str | None = "[[Exec Weekly Ops Scorecard]]",
    pinned_score: float | None = None,
) -> OpportunitySpec:
    drivers = [
        DriverSpec(
            name="annual_stockout_lost_margin_usd",
            value=4_250_000.0,
            source="prod.gold.inventory_daily",
            verified=True,
        ),
        DriverSpec(
            name="addressable_share",
            value=0.65,
            source="Ops VP estimate",
            verified=verified_all,
            verify_question="Confirm 65% of stockouts have >=14d supplier lead time.",
        ),
        DriverSpec(
            name="expected_reduction_pct",
            range=(0.07, 0.12, 0.19),
            source="Pilot benchmark",
            verified=verified_all,
        ),
    ]
    supplements = (
        [
            ReportSupplement(
                ref=supplement_report,
                integration_mode="Add 14d Stockout Risk % column to weekly visual",
            )
        ]
        if supplement_report
        else []
    )
    return OpportunitySpec(
        id=opp_id,
        title=title,
        status=status,  # type: ignore[arg-type]
        boss_mandate=boss_mandate,
        domain_pack="ops_supply_chain",
        internal_datasets=[
            InternalDatasetUse(
                ref="[[prod.gold.inventory_daily]]",
                role="feature_and_target",
                transformations_needed=["Deduplicate intraday snapshots"],
            )
        ],
        external_data_required=[
            ExternalDataReq(
                source="NOAA GHCN Daily Weather",
                purpose="Regional storm transit delay signal",
                acquisition="free_public_api",
                blocker_severity="low",
            )
        ],
        existing_reports_supplemented=supplements,
        new_reports_unlocked=[
            NewReportSpec(
                title="Safety Stock Reallocation Simulator",
                audience="Supply Planners",
                why_net_new="Replaces Monday morning manual Excel exports",
            )
        ],
        valuation=ValuationSpec(
            confidence_tier=confidence,  # type: ignore[arg-type]
            driver_formula="annual_stockout_lost_margin_usd * addressable_share * expected_reduction_pct",
            drivers=drivers,
        ),
        effort_weeks=effort_weeks,
        data_readiness_score=readiness,
        adoption_friction_score=friction,
        computed_priority_score=pinned_score,
        steps_to_completion=[
            "Validate historical point-in-time inventory snapshots",
            "Train LightGBM p50/p90 quantile model",
            "Publish Gold predictions table to Power BI",
        ],
        stakeholders=["[[Sarah Jenkins]]"],
    )


# ===========================================================================
# Group 1: Packaging, R5 Guardrails & .gitignore DLP Isolation
# ===========================================================================


def test_pyproject_and_r5_guardrails() -> None:
    pyproject_path = REPO_ROOT / "pyproject.toml"
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    assert data["project"]["name"] == "poopdeck"
    assert "readme" not in data["project"]
    assert data["project"]["scripts"]["poopdeck"] == "poopdeck.cli:main"

    deps_joined = " ".join(data["project"]["dependencies"]).lower()
    for forbidden in ("postgres", "psycopg", "redis", "sqlalchemy", "botbuilder", "msgraph"):
        assert forbidden not in deps_joined

    pkg_data = data["tool"]["setuptools"]["package-data"]["poopdeck"]
    assert "packs/*.yaml" in pkg_data
    assert "ui/static/*" in pkg_data

    assert poopdeck.__version__ == "0.1.0"
    for sym in (
        "GlossaryTerm",
        "GlossaryCard",
        "evaluate_driver_formula",
        "load_external_data_catalog",
    ):
        assert hasattr(poopdeck, sym)


def test_gitignore_dlp_and_demo_vault_allowlist(tmp_path: Path) -> None:
    sandbox = tmp_path / "repo"
    sandbox.mkdir()
    shutil.copy2(REPO_ROOT / ".gitignore", sandbox / ".gitignore")
    env = clean_git_env()
    subprocess.run(["git", "init"], cwd=sandbox, env=env, check=True, capture_output=True)

    ignored_paths = [
        "vault/datasets/prod.md",
        ".env",
        ".env.local",
        "sales.pbix",
        "nested/report.pbix",
        "model.vpax",
        "exports/brief.md",
        "config.yaml",
        ".agents/state.md",
        "PROJECT.md",
        "TEST_INFRA.md",
        "TEST_READY.md",
        "examples/demo_vault/leak.pbix",
        "examples/demo_vault/reports/model.vpax",
        "examples/demo_vault/.env",
        "examples/demo_vault/__pycache__/x.pyc",
    ]
    for rel in ignored_paths:
        res = subprocess.run(
            ["git", "check-ignore", "-q", rel],
            cwd=sandbox,
            env=env,
            capture_output=True,
        )
        assert res.returncode == 0, f"Expected {rel} to be gitignored"

    allowed_paths = [
        ".env.example",
        "config.example.yaml",
        "pyproject.toml",
        "poopdeck/__init__.py",
        "examples/demo_vault/glossary.md",
        "examples/demo_vault/datasets/gold_orders.md",
        "examples/demo_vault/staging/opportunities/OPP-005.md",
    ]
    for rel in allowed_paths:
        res = subprocess.run(
            ["git", "check-ignore", "-q", rel],
            cwd=sandbox,
            env=env,
            capture_output=True,
        )
        assert res.returncode == 1, f"Expected {rel} NOT to be gitignored"

    # Nested vault/.git must remain invisible to root git status
    initialize_vault(sandbox / "vault", seed_questions=True, init_git=True)
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=sandbox,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "vault" not in status
    safe_rmtree(sandbox)


# ===========================================================================
# Group 2: Configuration Precedence, Aliases & Weight Validation
# ===========================================================================


def test_load_config_precedence_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("POOPDECK_CONFIG_PATH", raising=False)
    monkeypatch.delenv("POOPDECK_VAULT_DIR", raising=False)

    repo_cfg = load_config(REPO_ROOT / "config.example.yaml")
    assert repo_cfg.privacy.privacy_mode == "safe_aggregates"
    assert repo_cfg.privacy.allow_sample_rows is False
    assert repo_cfg.privacy.include_sample_rows is False

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.example.yaml").write_text(
        "domain: retail_cpg\nquarterly_capacity_weeks: 10.0\n", encoding="utf-8"
    )
    assert load_config().domain == "retail_cpg"

    (tmp_path / "config.yaml").write_text(
        "domain: finance_risk\nquarterly_capacity_weeks: 11.0\n", encoding="utf-8"
    )
    assert load_config().domain == "finance_risk"

    env_cfg_file = tmp_path / "env_cfg.yaml"
    env_cfg_file.write_text("domain: b2b_saas\n", encoding="utf-8")
    monkeypatch.setenv("POOPDECK_CONFIG_PATH", str(env_cfg_file))
    assert load_config().domain == "b2b_saas"

    explicit_file = tmp_path / "explicit.yaml"
    explicit_file.write_text("domain: general\n", encoding="utf-8")
    loaded_explicit = load_config(explicit_file)
    assert loaded_explicit.domain == "generic"

    monkeypatch.setenv("POOPDECK_VAULT_DIR", str(tmp_path / "custom_vault"))
    cfg_with_env_vault = load_config(explicit_file)
    assert cfg_with_env_vault.vault_path == str(tmp_path / "custom_vault")
    assert cfg_with_env_vault.resolve_vault_dir() == tmp_path / "custom_vault"
    assert cfg_with_env_vault.resolve_vault_dir(tmp_path / "cli_v") == tmp_path / "cli_v"

    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "does_not_exist.yaml")


def test_privacy_and_mode_aliases() -> None:
    assert PrivacyConfig(allow_sample_rows=True).include_sample_rows is True
    assert PrivacyConfig(include_sample_rows=True).allow_sample_rows is True

    dual = PrivacyConfig.model_validate(
        {"allow_sample_rows": False, "include_sample_rows": True}
    )
    assert dual.allow_sample_rows is True
    dual.include_sample_rows = False
    assert dual.allow_sample_rows is False

    assert DatabricksConfig(mode="tier1_sdk").mode == "tier1"  # type: ignore[arg-type]
    assert DatabricksConfig(mode="tier2_bundle").mode == "tier2"  # type: ignore[arg-type]
    assert PowerBIConfig(mode="tier1_rest").mode == "tier1"  # type: ignore[arg-type]
    assert PowerBIConfig(mode="tier2_local").mode == "tier2"  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        DatabricksConfig(mode="tier99")  # type: ignore[arg-type]


def test_scoring_weights_validation() -> None:
    with pytest.raises(ValidationError):
        PoopdeckConfig(quarterly_capacity_weeks=0.0)
    with pytest.raises(ValidationError):
        ScoringWeightsConfig(roi_cap_usd=0.0)
    with pytest.raises(ValidationError):
        ScoringWeightsConfig(roi_weight=0.80, data_readiness_weight=0.25)


# ===========================================================================
# Group 3: Privacy Gate, PII Column Discrimination & Secret Scrubbing
# ===========================================================================


def test_is_pii_column_discrimination() -> None:
    pii_cols = [
        "email",
        "user_email",
        "ssn",
        "social_security",
        "tax_id",
        "phone",
        "phone_number",
        "mobile",
        "dob",
        "birth_date",
        "date_of_birth",
        "salary",
        "base_salary",
        "compensation",
        "wage",
        "address",
        "billing_address",
        "street",
        "zip_code",
        "postal_code",
        "first_name",
        "last_name",
        "full_name",
        "customer_name",
        "employee_name",
        "contact_name",
        "person_name",
        "name",
        "Customers[Customer Name]",
    ]
    for col in pii_cols:
        assert is_pii_column(col) is True, f"Expected PII=True for {col!r}"

    safe_cols = [
        "table_name",
        "schema_name",
        "catalog_name",
        "product_name",
        "vendor_name",
        "carrier_name",
        "warehouse_name",
        "metric_name",
        "file_name",
        "workspace_name",
        "Table Name",
        "addressable_share",
    ]
    for col in safe_cols:
        assert is_pii_column(col) is False, f"Expected PII=False for {col!r}"

    assert is_pii_column("col_x", comment="Primary customer email") is True
    assert is_pii_column("col_y", comment="PII - employee identifier") is True
    assert is_pii_column("col_z", comment="Source table name in Unity Catalog") is False
    assert is_pii_column("col_w", comment="Non-PII product name") is False


def test_redact_text_secrets_pii_and_idempotency() -> None:
    raw = (
        "Databricks dapi0123456789abcdef0123456789abcdef, "
        "GH ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456, "
        "FG github_pat_11ABCDEF0123456789_abcdefghijklmnop, "
        "Claude sk-ant-api03-abcdefghijklmnopqrstuvwxyz, "
        "OpenAI sk-proj-abcdefghijklmnopqrstuvwxyz123456, "
        "AWS AKIAIOSFODNN7EXAMPLE, "
        "Auth Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c, "
        "Conn jdbc:databricks://adb.azuredatabricks.net:443;PWD=SuperSecret!555-123-4567; "
        "SQL Server=tcp:corp.database.windows.net;Password=MyPass123!; "
        "Blob AccountKey=AbCdEfGhIjKlMnOpQrStUvWxYz0123456789+/==; "
        "URL https://svc_user:MyTopSecret@github.com/org/repo.git "
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA...\n-----END RSA PRIVATE KEY----- "
        "Contact jane.doe@corp.com, SSN 123-45-6789, call +1-800-555-0199. "
        "Keep date 2026-11-18, UUID 83e6a7aa-3574-447b-8b70-0b6eb6437ec7, IP 127.0.0.1, lift +1250.50."
    )
    scrubbed = redact_text(raw)
    assert "dapi0123456789" not in scrubbed
    assert "ghp_ABCDEF" not in scrubbed
    assert "github_pat_" not in scrubbed
    assert "sk-ant-" not in scrubbed
    assert "AKIAIOSFODNN7EXAMPLE" not in scrubbed
    assert "SuperSecret!" not in scrubbed
    assert "MyPass123!" not in scrubbed
    assert "MyTopSecret" not in scrubbed
    assert "BEGIN RSA PRIVATE KEY" not in scrubbed
    assert "[REDACTED:SECRET]" in scrubbed
    assert "[REDACTED:EMAIL]" in scrubbed
    assert "[REDACTED:SSN]" in scrubbed
    assert "[REDACTED:PHONE]." in scrubbed

    assert "2026-11-18" in scrubbed
    assert "83e6a7aa-3574-447b-8b70-0b6eb6437ec7" in scrubbed
    assert "127.0.0.1" in scrubbed
    assert "+1250.50" in scrubbed

    # Idempotency & redact_secrets=False toggle
    assert redact_text(scrubbed) == scrubbed
    no_secret_scrub = redact_text(
        "Token dapi0123456789abcdef0123456789abcdef email a@b.co",
        redact_secrets=False,
    )
    assert "dapi0123456789abcdef0123456789abcdef" in no_secret_scrub
    assert "[REDACTED:EMAIL]" in no_secret_scrub


def test_redact_dataset_payload_and_model_roundtrip() -> None:
    raw_payload = {
        "fqn": "prod.gold.orders",
        "grain": "1 row per order",
        "columns": [
            {
                "name": "order_id",
                "data_type": "STRING",
                "min_val": "ORD-001",
                "max_val": "ORD-999",
                "sample_values": None,
            },
            {
                "name": "customer_email",
                "data_type": "STRING",
                "min_val": "a@corp.com",
                "max_val": "z@corp.com",
                "sample_values": "a@corp.com",
            },
        ],
        "sample_rows": [
            {"order_id": "ORD-1", "customer_email": "alice@corp.com", "note": "Call 555-234-5678"},
            {"order_id": "ORD-2", "customer_email": "bob@corp.com", "note": "Clean row"},
            {"order_id": "ORD-3", "customer_email": "c@corp.com", "note": "Extra"},
        ],
    }
    snapshot = copy.deepcopy(raw_payload)

    # Default safe_aggregates -> 0 sample rows (even if privacy_mode="scrubbed_samples" while allow_sample_rows=False)
    redacted_default = redact_dataset_payload(
        raw_payload, PrivacyConfig(privacy_mode="scrubbed_samples", allow_sample_rows=False)
    )
    assert raw_payload == snapshot
    assert redacted_default["sample_rows"] == []
    assert redacted_default["columns"][1]["is_pii"] is True
    assert redacted_default["columns"][1]["min_val"] == "[REDACTED:PII]"
    assert redacted_default["columns"][1]["sample_values"] == "[REDACTED:PII]"

    # Round-trip into DatasetCard (Seam Rule #1: None -> [] and scalar str -> [str])
    card = DatasetCard.model_validate(redacted_default)
    assert card.columns[0].sample_values == []
    assert card.columns[1].sample_values == ["[REDACTED:PII]"]

    # Explicit opt-in allow_sample_rows=True with max_sample_rows=2
    redacted_opt_in = redact_dataset_payload(
        raw_payload, PrivacyConfig(allow_sample_rows=True, max_sample_rows=2)
    )
    assert len(redacted_opt_in["sample_rows"]) == 2
    assert redacted_opt_in["sample_rows"][0]["customer_email"] == "[REDACTED:PII]"
    assert "[REDACTED:PHONE]" in redacted_opt_in["sample_rows"][0]["note"]
    assert redacted_opt_in["sample_rows"][0]["order_id"] == "ORD-1"


# ===========================================================================
# Group 4: Schemas, Safe AST ROI, Priority Score, Cutline & Domain Packs
# ===========================================================================


def test_entity_cross_doc_aliases_and_glossary_seam() -> None:
    ds_use = InternalDatasetUse(ref="[[t]]", transformations_needed="Deduplicate rows")  # type: ignore[arg-type]
    assert ds_use.transformations_needed == ["Deduplicate rows"]

    q = QuestionItem.model_validate(
        {
            "id": "Q-1",
            "category": "IT/Security",
            "owner": "CISO",
            "prompt": "Policy?",
            "answer": "Approved",
        }
    )
    assert q.category == "it_security"
    assert q.who_to_ask == "CISO"
    assert q.question == "Policy?"
    assert q.answer_summary == "Approved"

    term = GlossaryTerm.model_validate(
        {
            "term": "OTIF",
            "definition": "On-Time In-Full delivery rate",
            "source": "Ops Kickoff Transcript",
            "linked_refs": ["[[prod.gold.shipments]]"],
        }
    )
    assert term.source == "Ops Kickoff Transcript"
    assert term.source_refs == ["[[prod.gold.shipments]]"]
    assert term.linked_refs == ["[[prod.gold.shipments]]"]

    hive_ds = DatasetCard.model_validate({"fqn": "legacy_erp.bronze_shipments"})
    assert hive_ds.catalog == "hive_metastore"
    assert hive_ds.medallion_layer == "bronze"


def test_safe_ast_valuation_and_hallucination_guard() -> None:
    opp = _make_opp()
    assert (
        opp.valuation.annual_benefit_low_usd,
        opp.valuation.annual_benefit_base_usd,
        opp.valuation.annual_benefit_high_usd,
    ) == (193375.0, 331500.0, 524875.0)

    # Within 5% tolerance (340000 vs 331500 = 2.56%) passes on both scalar and ranged drivers
    v_ok = ValuationSpec(
        confidence_tier="directional",
        driver_formula="a * b",
        drivers=[DriverSpec(name="a", value=331500.0), DriverSpec(name="b", value=1.0)],
        annual_benefit_base_usd=340000.0,
    )
    assert v_ok.annual_benefit_base_usd == 340000.0

    v_ranged_ok = ValuationSpec(
        confidence_tier="directional",
        driver_formula="annual_stockout_lost_margin_usd * addressable_share * expected_reduction_pct",
        drivers=opp.valuation.drivers,
        annual_benefit_base_usd=340000.0,
    )
    assert v_ranged_ok.annual_benefit_base_usd == 340000.0

    # Explicitly inverted bounds still fail
    with pytest.raises(ValidationError):
        ValuationSpec(
            confidence_tier="directional",
            driver_formula="a * b",
            drivers=[DriverSpec(name="a", value=331500.0), DriverSpec(name="b", value=1.0)],
            annual_benefit_low_usd=340000.0,
            annual_benefit_base_usd=331500.0,
            annual_benefit_high_usd=335000.0,
        )

    # >5% divergence (180000 vs 193375 = 6.92%) raises ValidationError
    with pytest.raises(ValidationError):
        ValuationSpec(
            confidence_tier="directional",
            driver_formula="annual_stockout_lost_margin_usd * addressable_share * expected_reduction_pct",
            drivers=opp.valuation.drivers,
            annual_benefit_low_usd=180000.0,
        )

    bad_formulas = [
        "__import__('os').system('echo pwned')",
        "True * 100",
        "'hello'",
        "a ** 2",
        "a ^ 2",
        "undeclared_var * 2",
        "a / 0",
        "340000",
    ]
    for bad in bad_formulas:
        with pytest.raises((ValueError, ValidationError)):
            evaluate_driver_formula(bad, [DriverSpec(name="a", value=10.0)])


def test_verify_prompts_and_finance_verified_guard() -> None:
    opp = _make_opp(verified_all=False, confidence="directional")
    assert len(opp.valuation.verification_prompts) == 2
    assert all(
        p.startswith("[VERIFY WITH STAKEHOLDER]")
        for p in opp.valuation.verification_prompts
    )
    assert "[VERIFY WITH STAKEHOLDER] [VERIFY WITH STAKEHOLDER]" not in "\n".join(
        opp.valuation.verification_prompts
    )

    with pytest.raises(ValidationError, match="finance_verified"):
        _make_opp(verified_all=False, confidence="finance_verified")

    opp_verified = _make_opp(verified_all=True, confidence="finance_verified")
    assert opp_verified.valuation.verification_prompts == []


def test_priority_score_monotonicity_and_existing_pbi_bonus() -> None:
    s_with_pbi = compute_priority_score(
        _make_opp(friction=1, supplement_report="[[Exec Report]]")
    )
    s_no_pbi = compute_priority_score(_make_opp(friction=1, supplement_report=None))
    assert s_with_pbi > s_no_pbi

    efforts = [0.5, 1.0, 3.5, 12.0, 24.0]
    effort_scores = [compute_priority_score(_make_opp(effort_weeks=e)) for e in efforts]
    assert effort_scores == sorted(effort_scores, reverse=True)

    s_hyp = compute_priority_score(_make_opp(confidence="hypothesis"))
    s_dir = compute_priority_score(_make_opp(confidence="directional"))
    s_fin = compute_priority_score(
        _make_opp(confidence="finance_verified", verified_all=True)
    )
    assert s_hyp < s_dir < s_fin


def test_portfolio_schedule_boss_mandate_displacement_and_overflow() -> None:
    opps = [
        _make_opp(opp_id="OPP-001", effort_weeks=4.0, pinned_score=90.0, status="active"),
        _make_opp(opp_id="OPP-003", effort_weeks=4.0, pinned_score=80.0, status="backlog"),
        _make_opp(opp_id="OPP-004", effort_weeks=4.0, pinned_score=70.0, status="backlog"),
        _make_opp(
            opp_id="OPP-002",
            effort_weeks=5.0,
            pinned_score=35.0,
            boss_mandate=True,
            status="backlog",
        ),
        _make_opp(opp_id="OPP-005", effort_weeks=2.0, pinned_score=95.0, status="staging"),
    ]
    sched = compute_portfolio_schedule(opps, quarterly_capacity_weeks=12.0)
    assert sched.items[0].id == "OPP-002"
    assert sched.funded_ids == ["OPP-002", "OPP-001"]
    assert sched.displaced_ids == ["OPP-003", "OPP-004"]
    assert sched.used_capacity_weeks == 9.0
    assert sched.mandate_weeks == 5.0
    assert sched.remaining_capacity_weeks == 3.0
    assert sched.over_capacity is False

    # Oversubscribed mandate > 12.0w
    oversub = [
        _make_opp(opp_id="OPP-001", effort_weeks=4.0, pinned_score=92.0, status="backlog"),
        _make_opp(
            opp_id="OPP-009",
            effort_weeks=14.0,
            pinned_score=25.0,
            boss_mandate=True,
            status="active",
        ),
    ]
    sched_over = compute_portfolio_schedule(oversub, quarterly_capacity_weeks=12.0)
    assert sched_over.over_capacity is True
    assert sched_over.overflow_weeks == 2.0
    assert sched_over.remaining_capacity_weeks == 0.0
    assert sched_over.displaced_ids == ["OPP-001"]


def test_external_data_catalog_pack_loading() -> None:
    cat = load_external_data_catalog()
    assert len(cat.archetypes) == 5
    assert set(cat.domain_lenses.keys()) == {
        "generic",
        "ops_supply_chain",
        "retail_cpg",
        "finance_risk",
        "b2b_saas",
        "manufacturing",
    }
    sources_joined = " ".join(e.source for e in cat.external_datasets)
    for required in ("FRED", "Census", "NOAA", "Holidays", "Port Congestion", "Dun & Bradstreet"):
        assert required in sources_joined

    assert get_domain_lens("unknown_domain_xyz").slug == "generic"
    assert cat["version"] == "1.0"
    free_ops = list_external_datasets("ops_supply_chain", free_only=True)
    assert all(e.acquisition == "free_public_api" for e in free_ops)
    assert get_external_datasets_for_domain("retail_cpg") == list_external_datasets(
        "retail_cpg"
    )


# ===========================================================================
# Group 5: Day-1 Seed Templates
# ===========================================================================


def test_templates_15_questions_5_phases_and_glossary() -> None:
    bank = build_default_question_bank()
    assert len(bank.questions) == 15
    expected_ids = [
        "Q-MGR-01",
        "Q-MGR-02",
        "Q-MGR-03",
        "Q-MGR-04",
        "Q-IT-01",
        "Q-IT-02",
        "Q-IT-03",
        "Q-DE-01",
        "Q-DE-02",
        "Q-DE-03",
        "Q-PBI-01",
        "Q-PBI-02",
        "Q-PBI-03",
        "Q-FIN-01",
        "Q-FIN-02",
    ]
    assert [q.id for q in bank.questions] == expected_ids

    playbook = build_default_playbook()
    assert [p.id for p in playbook.phases] == [
        "PHASE-1",
        "PHASE-2",
        "PHASE-3",
        "PHASE-4",
        "PHASE-5",
    ]
    assert all(p.completed is False for p in playbook.phases)

    glossary = build_default_glossary()
    assert isinstance(glossary, GlossaryCard)
    assert glossary.terms == []


# ===========================================================================
# Group 6: Non-Destructive Vault I/O & Windows Git Isolation
# ===========================================================================


def test_upsert_preserves_human_notes_byte_for_byte_and_yaml_dates(
    tmp_path: Path,
) -> None:
    tricky_notes = (
        "\n\n### Local Stakeholder Context\n"
        "- Path with regex-unsafe backslashes: `C:\\Users\\analyst\\notes\\1` and `\\g<0>`\n"
        "- Linked entity: [[Sarah Jenkins]] | [[prod.gold.orders|Gold Orders]]\n\n"
        "---\n"
        "| Col | Note |\n|---|---|\n| A | Preserved |\n   \n"
    )
    ds_path = tmp_path / "vault" / "datasets" / "prod.gold.orders.md"
    ds1 = DatasetCard(
        fqn="prod.gold.orders",
        row_count=1000,
        updated_at=datetime.date(2026, 11, 11),
    )
    upsert_markdown_card(
        ds_path, ds1, render_dataset_body(ds1), human_notes_override=tricky_notes
    )

    # Re-sync updated DatasetCard, ReportCard, StakeholderCard without override
    ds2 = DatasetCard(
        fqn="prod.gold.orders",
        row_count=250000,
        freshness_lag_hours=2.5,
        updated_at=datetime.date(2026, 11, 12),
    )
    upsert_markdown_card(ds_path, ds2, render_dataset_body(ds2))

    raw_bytes = ds_path.read_bytes()
    assert b"\r\n" not in raw_bytes
    raw_text = raw_bytes.decode("utf-8")
    assert "updated_at: 2026-11-12\n" in raw_text

    parsed = parse_markdown_card(ds_path)
    assert parsed.human_notes == tricky_notes
    assert parsed.frontmatter["row_count"] == 250000

    # Verify ReportCard & StakeholderCard preservation too
    rep_path = tmp_path / "vault" / "reports" / "ops.md"
    rep = ReportCard(title="Ops Report", workspace="Exec")
    upsert_markdown_card(
        rep_path, rep, render_report_body(rep), human_notes_override=tricky_notes
    )
    rep_v2 = ReportCard(title="Ops Report", workspace="Exec", high_excel_export=True)
    upsert_markdown_card(rep_path, rep_v2, render_report_body(rep_v2))
    assert parse_markdown_card(rep_path).human_notes == tricky_notes

    sh_path = tmp_path / "vault" / "stakeholders" / "sarah.md"
    sh = StakeholderCard(name="Sarah Jenkins", role="VP Ops")
    upsert_markdown_card(
        sh_path, sh, render_stakeholder_body(sh), human_notes_override=tricky_notes
    )
    sh_v2 = StakeholderCard(name="Sarah Jenkins", role="SVP Ops", influence_tier="exec")
    upsert_markdown_card(sh_path, sh_v2, render_stakeholder_body(sh_v2))
    assert parse_markdown_card(sh_path).human_notes == tricky_notes


def test_upsert_rescues_hand_authored_file_and_bom_horizontal_rules(
    tmp_path: Path,
) -> None:
    hand_file = tmp_path / "hand_note.md"
    hand_file.write_bytes(
        "\ufeffHand-written meeting takeaway\n\n---\n\nSecond paragraph with [[TargetA]] and [[TargetB|AliasB#Sec]].".encode(
            "utf-8"
        )
    )
    parsed_initial = parse_markdown_card(hand_file)
    assert parsed_initial.frontmatter == {}
    assert extract_wikilinks(parsed_initial.body) == ["TargetA", "TargetB"]

    sh = StakeholderCard(name="Alex Rivera", role="Lead DE")
    upsert_markdown_card(hand_file, sh, render_stakeholder_body(sh))
    rescued = parse_markdown_card(hand_file)
    assert rescued.human_notes is not None
    assert "Hand-written meeting takeaway" in rescued.human_notes


def test_initialize_vault_idempotency_git_isolation_and_safe_rmtree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "poisoned_git_dir"))
    vdir = tmp_path / "company_vault"

    info1 = initialize_vault(vdir, seed_questions=True, init_git=True)
    assert info1["git_initialized"] is True
    assert (vdir / ".git").is_dir()
    assert len(info1["seeded_files"]) == 3

    # Simulate read-only .git object on Windows + custom user edit
    ro_file = vdir / ".git" / "objects" / "dummy.pack"
    ro_file.write_bytes(b"packdata")
    ro_file.chmod(stat.S_IREAD)

    q_path = vdir / "questions" / "open-questions.md"
    update_question_or_phase(vdir, "Q-MGR-01", status="answered", answer="Ship quick win")

    # Second initialize_vault call must NOT overwrite answered question
    info2 = initialize_vault(vdir, seed_questions=True, init_git=True)
    assert info2["git_initialized"] is False
    assert info2["seeded_files"] == []
    bank, _ = load_questions_and_playbook(vdir)
    assert bank.questions[0].status == "answered"

    safe_rmtree(vdir)
    assert not vdir.exists()


# ===========================================================================
# Group 7: Vault Validation, Staging Mutations & Lineage Graph
# ===========================================================================


def test_validate_vault_valid_invalid_and_strict_wikilinks(tmp_path: Path) -> None:
    vdir = tmp_path / "vault"
    initialize_vault(vdir, seed_questions=True, init_git=False)

    ds = DatasetCard(fqn="prod.gold.inventory_daily", row_count=50000)
    upsert_markdown_card(
        vdir / "datasets" / "prod.gold.inventory_daily.md",
        ds,
        render_dataset_body(ds),
    )
    rep = ReportCard(
        title="Exec Weekly Ops Scorecard",
        workspace="Ops",
        upstream_datasets=["[[prod.gold.inventory_daily]]"],
        owners=["[[Sarah Jenkins]]"],
    )
    upsert_markdown_card(
        vdir / "reports" / "exec-weekly-ops-scorecard.md",
        rep,
        render_report_body(rep),
    )
    sh = StakeholderCard(name="Sarah Jenkins", role="VP Supply Chain")
    upsert_markdown_card(
        vdir / "stakeholders" / "sarah-jenkins.md",
        sh,
        render_stakeholder_body(sh),
    )
    opp = _make_opp(opp_id="OPP-004", status="backlog")
    upsert_markdown_card(
        vdir / "opportunities" / "OPP-004-stockout.md",
        opp,
        render_opportunity_body(opp),
    )

    rep_clean = validate_vault(vdir, strict=True)
    assert rep_clean.valid is True
    assert rep_clean.errors == []
    assert rep_clean.counts["questions"] == 15
    assert rep_clean.counts["playbook"] == 5

    # Add dangling wikilink -> warning in non-strict, error in strict
    opp_dangling = _make_opp(
        opp_id="OPP-005", status="staging", supplement_report="[[Missing PBI Report]]"
    )
    upsert_markdown_card(
        vdir / "staging" / "opportunities" / "OPP-005-dangling.md",
        opp_dangling,
        render_opportunity_body(opp_dangling),
    )
    non_strict = validate_vault(vdir, strict=False)
    assert non_strict.valid is True
    assert any("Missing PBI Report" in w for w in non_strict.warnings)

    strict_res = validate_vault(vdir, strict=True)
    assert strict_res.valid is False
    assert any("Missing PBI Report" in e for e in strict_res.errors)

    # Corrupt markers + bad status in staging -> collects multiple errors without halting
    (vdir / "datasets" / "broken.md").write_text(
        f"---\nfqn: prod.gold.broken\n---\n{HUMAN_NOTES_START}\nunclosed",
        encoding="utf-8",
    )
    (vdir / "staging" / "opportunities" / "wrong_status.md").write_text(
        (vdir / "opportunities" / "OPP-004-stockout.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    broken_res = validate_vault(vdir, strict=False)
    assert broken_res.valid is False
    assert len(broken_res.errors) >= 2


def test_promote_archive_and_boss_mandate_roundtrips(tmp_path: Path) -> None:
    vdir = tmp_path / "vault"
    initialize_vault(vdir, seed_questions=False, init_git=False)

    custom_notes = "\nCFO note: verify warehouse labor cap with Sarah.\n"
    staged = _make_opp(opp_id="OPP-005", status="staging")
    staged_path = vdir / "staging" / "opportunities" / "OPP-005-forecast.md"
    upsert_markdown_card(
        staged_path,
        staged,
        render_opportunity_body(staged, cfo_critique="Red-teamed baseline margin."),
        human_notes_override=custom_notes,
    )

    promoted_path = promote_opportunity(vdir, "OPP-005", target_status="backlog")
    assert not staged_path.exists()
    assert promoted_path == vdir / "opportunities" / "OPP-005-forecast.md"
    promoted_card = parse_markdown_card(promoted_path)
    assert promoted_card.frontmatter["status"] == "backlog"
    assert "## Skeptical CFO Critique" in promoted_card.body
    assert promoted_card.human_notes == custom_notes

    _, toggled_opp = set_boss_mandate(vdir, "OPP-005")
    assert toggled_opp.boss_mandate is True
    assert parse_markdown_card(promoted_path).human_notes == custom_notes

    archived_path = archive_opportunity(vdir, "OPP-005", reason="Deferred to Q3")
    archived_card = parse_markdown_card(archived_path)
    assert archived_card.frontmatter["status"] == "archived"
    assert "Deferred to Q3" in archived_card.body
    assert archived_card.human_notes == custom_notes
    assert load_all_opportunities(vdir, include_archived=False) == []
    assert len(load_all_opportunities(vdir, include_archived=True)) == 1

    with pytest.raises(KeyError):
        promote_opportunity(vdir, "OPP-999")
    with pytest.raises(KeyError):
        set_boss_mandate(vdir, "OPP-999")


def test_questions_and_playbook_mutations_preserve_notes(tmp_path: Path) -> None:
    vdir = tmp_path / "vault"
    initialize_vault(vdir, seed_questions=True, init_git=False)

    q_path = vdir / "questions" / "open-questions.md"
    pb_path = vdir / "questions" / "day1-90-playbook.md"
    q_notes = "\nFollow up with Platform Admin on Thursday.\n"
    pb_notes = "\nManager approved Phase 1 scope.\n"

    bank0, pb0 = load_questions_and_playbook(vdir)
    upsert_markdown_card(q_path, bank0, parse_markdown_card(q_path).body, human_notes_override=q_notes)
    upsert_markdown_card(pb_path, pb0, parse_markdown_card(pb_path).body, human_notes_override=pb_notes)

    # Test both `answer=` keyword alias (Seam Rule #3) and `answer_summary=`
    update_question_or_phase(
        vdir, "Q-IT-02", status="answered", answer="Unity Catalog enabled"
    )
    update_question_or_phase(vdir, "PHASE-1", completed=True)

    bank1, pb1 = load_questions_and_playbook(vdir)
    q_it02 = next(q for q in bank1.questions if q.id == "Q-IT-02")
    assert q_it02.status == "answered"
    assert q_it02.answer_summary == "Unity Catalog enabled"
    assert pb1.phases[0].completed is True
    assert parse_markdown_card(q_path).human_notes == q_notes
    assert parse_markdown_card(pb_path).human_notes == pb_notes
    assert "- [x] **Q-IT-02" in parse_markdown_card(q_path).body

    with pytest.raises(KeyError):
        update_question_or_phase(vdir, "Q-NONEXISTENT-99", status="answered")


def test_build_lineage_graph_resolves_and_handles_missing_refs(tmp_path: Path) -> None:
    vdir = tmp_path / "vault"
    initialize_vault(vdir, seed_questions=False, init_git=False)

    ds = DatasetCard(
        fqn="prod.gold.inventory_daily",
        upstream_refs=["[[missing.silver.inventory_snapshots]]"],
    )
    upsert_markdown_card(
        vdir / "datasets" / "prod.gold.inventory_daily.md",
        ds,
        render_dataset_body(ds),
    )
    rep = ReportCard(
        title="Exec Weekly Ops Scorecard",
        workspace="Ops",
        upstream_datasets=["[[prod.gold.inventory_daily]]"],
    )
    upsert_markdown_card(
        vdir / "reports" / "exec-weekly-ops-scorecard.md",
        rep,
        render_report_body(rep),
    )
    sh = StakeholderCard(name="Sarah Jenkins", role="VP Ops")
    upsert_markdown_card(
        vdir / "stakeholders" / "sarah-jenkins.md",
        sh,
        render_stakeholder_body(sh),
    )
    opp = _make_opp(opp_id="OPP-004", status="backlog")
    upsert_markdown_card(
        vdir / "opportunities" / "OPP-004-stockout.md",
        opp,
        render_opportunity_body(opp),
    )

    graph = build_lineage_graph(vdir, include_staging=True)
    node_types = {n["type"] for n in graph["nodes"]}
    assert node_types == {
        "external_data",
        "dataset",
        "opportunity",
        "existing_report",
        "new_report",
        "stakeholder",
    }
    placeholders = [n for n in graph["nodes"] if not n["exists_in_vault"]]
    assert any(n["label"] == "missing.silver.inventory_snapshots" for n in placeholders)
    assert len(graph["edges"]) >= 5


# ===========================================================================
# Group 8: Exec & Microsoft Teams Brief Generator
# ===========================================================================


def test_opportunity_portfolio_and_verify_briefs(tmp_path: Path) -> None:
    vdir = tmp_path / "vault"
    initialize_vault(vdir, seed_questions=True, init_git=False)

    rep = ReportCard(
        title="Exec Weekly Ops Scorecard",
        workspace="Ops",
        bi_to_ds_gaps=["Uses static 14-day reorder heuristic"],
        measures=[
            DaxMeasure(
                name="Risk Tier",
                table="Inventory",
                expression="SWITCH(TRUE(), ...)",
                flagged_heuristics=["nested_heuristic_tiers"],
            )
        ],
    )
    upsert_markdown_card(
        vdir / "reports" / "exec-weekly-ops-scorecard.md",
        rep,
        render_report_body(rep),
    )

    opp1 = _make_opp(opp_id="OPP-001", effort_weeks=8.0, pinned_score=88.0, status="active")
    opp2 = _make_opp(
        opp_id="OPP-002",
        effort_weeks=6.0,
        pinned_score=40.0,
        boss_mandate=True,
        status="backlog",
    )
    opp4 = _make_opp(opp_id="OPP-004", effort_weeks=3.5, pinned_score=75.0, status="backlog")
    for o in (opp1, opp2, opp4):
        upsert_markdown_card(
            vdir / "opportunities" / f"{o.id}.md", o, render_opportunity_body(o)
        )

    b_teams = generate_opportunity_brief(vdir, "OPP-004", fmt="teams")
    assert "$193,375 / $331,500 / $524,875" in b_teams.markdown
    assert "[VERIFY WITH STAKEHOLDER]" in b_teams.markdown
    assert "nested_heuristic_tiers" in b_teams.markdown

    b_exec = generate_opportunity_brief(vdir, "OPP-002", fmt="exec")
    assert "Executive Opportunity Brief" in b_exec.markdown
    assert "P0: BOSS MANDATE" in b_exec.markdown

    with pytest.raises(KeyError):
        generate_opportunity_brief(vdir, "OPP-UNKNOWN")

    b_port = generate_portfolio_brief(vdir, capacity_weeks=12.0)
    assert b_port.brief_type == "weekly_status"
    assert "Displaced by Boss Mandate" in b_port.markdown
    assert "OPP-001" in b_port.markdown

    b_ver_single = generate_verify_questions_brief(vdir, opp_id="OPP-004")
    assert "[VERIFY WITH STAKEHOLDER]" in b_ver_single.markdown

    b_ver_all = generate_verify_questions_brief(vdir)
    assert "Q-MGR-01" in b_ver_all.markdown
    assert "OPP-004" in b_ver_all.markdown


# ===========================================================================
# Group 9: Iteration 2 Adversarial Regression & Hardening Suite
# ===========================================================================


def test_jdbc_at_password_nested_sample_rows_and_demo_vault_gitignore(
    tmp_path: Path,
) -> None:
    # 1. JDBC @-passwords, UID=email, ODBC {...} braces, and @/:-in-URI-passwords + idempotency
    exact_cases = [
        (
            "jdbc:databricks://host:443;PWD=Pass@word123",
            "jdbc:databricks://host:443;PWD=[REDACTED:SECRET]",
        ),
        (
            "jdbc:databricks://host:443;UID=alice@corp.com;PWD=Pass@word123;HttpPath=/sql",
            "jdbc:databricks://host:443;UID=[REDACTED:EMAIL];PWD=[REDACTED:SECRET];HttpPath=/sql",
        ),
        (
            "Driver={Simba Spark ODBC Driver};UID=token;PWD={Pass;word@123!};",
            "Driver={Simba Spark ODBC Driver};UID=token;PWD=[REDACTED:SECRET];",
        ),
        (
            "postgres://admin:P@ss:word123@db.internal:5432/prod",
            "postgres://admin:[REDACTED:SECRET]@db.internal:5432/prod",
        ),
        (
            "https://user:s3cr3t!@api.corp.com/v1",
            "https://user:[REDACTED:SECRET]@api.corp.com/v1",
        ),
    ]
    for raw_in, expected_out in exact_cases:
        actual = redact_text(raw_in)
        assert actual == expected_out, f"{raw_in!r} -> {actual!r}"
        assert redact_text(actual) == actual

    # 2. Nested list/dict/tuple sample_rows, tags & column stats scrubbing when allow_sample_rows=True
    nested_payload = {
        "fqn": "prod.silver.connector_logs",
        "tags": {"meta": {"owner_email": "lead@corp.com", "secret": "PWD=Pass@word123"}},
        "columns": [
            {
                "name": "context_struct",
                "data_type": "STRUCT",
                "min_val": {"conn": "jdbc:databricks://host:443;PWD=Pass@word123"},
                "max_val": ("ops@corp.com", "https://user:s3cr3t!@api.corp.com/v1"),
                "sample_values": [
                    {
                        "dsn": "jdbc:databricks://host:443;PWD=Pass@word123",
                        "contact": "ops@corp.com",
                    }
                ],
            }
        ],
        "sample_rows": [
            {
                "tags_array": [
                    "owner:alice@corp.com",
                    {
                        "nested_pat": "dapi0123456789abcdef0123456789abcdef",
                        "ok_count": 7,
                    },
                ],
                "context_struct": {
                    "dsn": "jdbc:databricks://host:443;PWD=Pass@word123",
                    "phone_note": "Call 555-234-5678",
                },
            }
        ],
    }
    snapshot = copy.deepcopy(nested_payload)
    redacted = redact_dataset_payload(
        nested_payload, PrivacyConfig(allow_sample_rows=True, max_sample_rows=5)
    )
    assert nested_payload == snapshot
    flat_repr = str(redacted)
    for secret_sub in (
        "Pass",
        "word123",
        "s3cr3t!",
        "alice@corp.com",
        "ops@corp.com",
        "lead@corp.com",
        "dapi0123456789",
        "555-234-5678",
    ):
        assert secret_sub not in flat_repr
    assert redacted["tags"] == {
        "meta": {"owner_email": "[REDACTED:EMAIL]", "secret": "PWD=[REDACTED:SECRET]"}
    }
    assert redacted["columns"][0]["min_val"] == {
        "conn": "jdbc:databricks://host:443;PWD=[REDACTED:SECRET]"
    }
    assert redacted["columns"][0]["max_val"] == (
        "[REDACTED:EMAIL]",
        "https://user:[REDACTED:SECRET]@api.corp.com/v1",
    )
    assert redacted["sample_rows"][0]["tags_array"][0] == "owner:[REDACTED:EMAIL]"
    assert redacted["sample_rows"][0]["tags_array"][1]["nested_pat"] == "[REDACTED:SECRET]"
    assert redacted["sample_rows"][0]["tags_array"][1]["ok_count"] == 7

    # 3. .gitignore blocks *.pbix / *.vpax / .env / __pycache__ inside examples/demo_vault/ while allowing .md
    sandbox = tmp_path / "gitignore_sandbox"
    sandbox.mkdir()
    shutil.copy2(REPO_ROOT / ".gitignore", sandbox / ".gitignore")
    env = clean_git_env()
    subprocess.run(
        ["git", "init"], cwd=sandbox, env=env, check=True, capture_output=True
    )
    for leaked in (
        "examples/demo_vault/leak.pbix",
        "examples/demo_vault/leak.vpax",
        "examples/demo_vault/.env",
        "examples/demo_vault/__pycache__/x.pyc",
    ):
        res_ig = subprocess.run(
            ["git", "check-ignore", "-q", leaked],
            cwd=sandbox,
            env=env,
            capture_output=True,
        )
        assert res_ig.returncode == 0, f"Expected {leaked} to be gitignored"

    res_ok = subprocess.run(
        ["git", "check-ignore", "-q", "examples/demo_vault/datasets/a.md"],
        cwd=sandbox,
        env=env,
        capture_output=True,
    )
    assert res_ok.returncode == 1, "Expected examples/demo_vault/datasets/a.md NOT ignored"
    safe_rmtree(sandbox)


def test_ast_overflow_complexity_guards_and_validate_vault_resilience(
    tmp_path: Path,
) -> None:
    huge_int_formula = "a + " + ("9" * 400)
    over_256_nodes = "a" + (" + a" * 80)
    deep_chain = "a" + (" + a" * 1500)
    deep_parens = ("(" * 300) + "a" + (")" * 300)
    parser_stack_overflow = ("+" * 10000) + "a"

    for bad_expr in (
        huge_int_formula,
        over_256_nodes,
        deep_chain,
        deep_parens,
        parser_stack_overflow,
    ):
        with pytest.raises(ValueError):
            evaluate_driver_formula(bad_expr, [DriverSpec(name="a", value=10.0)])
        with pytest.raises(ValidationError):
            ValuationSpec(
                confidence_tier="directional",
                driver_formula=bad_expr,
                drivers=[DriverSpec(name="a", value=10.0)],
            )

    vdir = tmp_path / "vault"
    initialize_vault(vdir, seed_questions=False, init_git=False)
    (vdir / "staging" / "opportunities" / "OPP-OVERFLOW.md").write_text(
        "---\n"
        "id: OPP-OVERFLOW\n"
        "title: Huge Int Overflow\n"
        "status: staging\n"
        "effort_weeks: 3.0\n"
        "data_readiness_score: 4\n"
        "adoption_friction_score: 2\n"
        "valuation:\n"
        "  confidence_tier: directional\n"
        f"  driver_formula: {huge_int_formula}\n"
        "  drivers:\n"
        "    - name: a\n"
        "      value: 10.0\n"
        f"---\nBody\n\n{HUMAN_NOTES_START}\nnote\n{HUMAN_NOTES_END}\n",
        encoding="utf-8",
    )
    (vdir / "opportunities" / "OPP-NAN.md").write_text(
        "---\n"
        "id: OPP-NAN\n"
        "title: YAML NaN Effort\n"
        "status: backlog\n"
        "effort_weeks: .nan\n"
        "data_readiness_score: 4\n"
        "adoption_friction_score: 2\n"
        "valuation:\n"
        "  confidence_tier: directional\n"
        "  driver_formula: a * 1\n"
        "  drivers:\n"
        "    - name: a\n"
        "      value: 10.0\n"
        f"---\nBody\n\n{HUMAN_NOTES_START}\nnote\n{HUMAN_NOTES_END}\n",
        encoding="utf-8",
    )
    (vdir / "datasets" / "inf_ds.md").write_text(
        "---\n"
        "fqn: prod.gold.inf_ds\n"
        "freshness_lag_hours: .inf\n"
        f"---\nBody\n\n{HUMAN_NOTES_START}\nnote\n{HUMAN_NOTES_END}\n",
        encoding="utf-8",
    )

    report = validate_vault(vdir, strict=False)
    assert report.valid is False
    joined_errors = "\n".join(report.errors)
    assert "staging/opportunities/OPP-OVERFLOW.md" in joined_errors
    assert "opportunities/OPP-NAN.md" in joined_errors
    assert "datasets/inf_ds.md" in joined_errors


def test_non_finite_and_bool_rejection_preserving_int_coercion(
    tmp_path: Path,
) -> None:
    class _ProbeVaultModel(VaultBaseModel):
        metric: float

    for bad in (float("nan"), float("inf"), float("-inf"), True, False):
        with pytest.raises(ValidationError):
            PoopdeckConfig(quarterly_capacity_weeks=bad)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            ScoringWeightsConfig(roi_cap_usd=bad)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            ScoringWeightsConfig(existing_pbi_bonus_points=bad)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            ScoringWeightsConfig(confidence_multipliers={"directional": bad})  # type: ignore[dict-item]
        with pytest.raises(ValidationError):
            DatasetCard(fqn="prod.gold.orders", freshness_lag_hours=bad)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            ColumnProfile(name="amount", null_pct=bad)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            DriverSpec(name="a", value=bad)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            _make_opp(effort_weeks=bad)  # type: ignore[arg-type]

    for bad_bool in (True, False):
        with pytest.raises(ValidationError):
            PrivacyConfig(max_sample_rows=bad_bool)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            DatabricksConfig(lineage_lookback_days=bad_bool)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            ReportCard(title="Ops", refresh_failures_30d=bad_bool)  # type: ignore[arg-type]

    for non_finite in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            _ProbeVaultModel(metric=non_finite)

    # Dual-key PrivacyConfig string "false" / "true" reconciliation
    assert (
        PrivacyConfig.model_validate(
            {"allow_sample_rows": False, "include_sample_rows": "false"}
        ).allow_sample_rows
        is False
    )
    p_true = PrivacyConfig.model_validate(
        {"allow_sample_rows": False, "include_sample_rows": "true"}
    )
    assert p_true.allow_sample_rows is True
    p_true.include_sample_rows = "false"  # type: ignore[assignment]
    assert p_true.allow_sample_rows is False

    opp = _make_opp(opp_id="OPP-001", status="active")
    for bad_cap in (float("nan"), float("inf"), float("-inf"), True, False, -0.5):
        with pytest.raises(ValueError):
            compute_portfolio_schedule([opp], quarterly_capacity_weeks=bad_cap)  # type: ignore[arg-type]

    bad_cfg = tmp_path / "bad_nan_config.yaml"
    bad_cfg.write_text(
        "quarterly_capacity_weeks: .inf\n"
        "scoring_weights:\n"
        "  confidence_multipliers:\n"
        "    directional: .nan\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_config(bad_cfg)

    # Plain ints must continue to coerce cleanly to float
    cfg_int = PoopdeckConfig(
        quarterly_capacity_weeks=12,
        scoring_weights=ScoringWeightsConfig(
            roi_weight=1,
            data_readiness_weight=0,
            bi_adoption_ease_weight=0,
            speed_to_value_weight=0,
            roi_cap_usd=1000000,
            existing_pbi_bonus_points=10,
            confidence_multipliers={"finance_verified": 1},
        ),
    )
    assert cfg_int.quarterly_capacity_weeks == 12.0
    assert cfg_int.scoring_weights.roi_cap_usd == 1_000_000.0
    assert cfg_int.scoring_weights.confidence_multipliers["finance_verified"] == 1.0

    opp_int = _make_opp(opp_id="OPP-INT", status="active", effort_weeks=4)
    sched_int = compute_portfolio_schedule([opp_int], quarterly_capacity_weeks=12)
    assert sched_int.capacity_weeks == 12.0
    assert sched_int.funded_ids == ["OPP-INT"]


def test_portfolio_schedule_sub_cent_mandate_overflow_and_ieee754_cutline() -> None:
    # 1. >2-decimal mandate overflow: 6.004 + 6.004 = 12.008 > 12.0 -> over_capacity=True
    m1 = _make_opp(
        opp_id="OPP-001",
        status="active",
        boss_mandate=True,
        effort_weeks=6.004,
        pinned_score=40.0,
    )
    m2 = _make_opp(
        opp_id="OPP-002",
        status="active",
        boss_mandate=True,
        effort_weeks=6.004,
        pinned_score=35.0,
    )
    b1 = _make_opp(
        opp_id="OPP-003",
        status="backlog",
        boss_mandate=False,
        effort_weeks=4.0,
        pinned_score=95.0,
    )
    sched_over = compute_portfolio_schedule([m1, m2, b1], quarterly_capacity_weeks=12.0)
    assert sched_over.over_capacity is True
    assert sched_over.used_capacity_weeks == 12.01
    assert sched_over.mandate_weeks == 12.01
    assert sched_over.overflow_weeks == 0.01
    assert sched_over.remaining_capacity_weeks == 0.0
    assert sched_over.funded_ids == ["OPP-001", "OPP-002"]
    assert sched_over.displaced_ids == ["OPP-003"]

    # Sub-half-cent mandate overflow (6.002 + 6.002 = 12.004 > 12.0) preserves strict > invariant
    m_sub1 = m1.model_copy(update={"effort_weeks": 6.002})
    m_sub2 = m2.model_copy(update={"effort_weeks": 6.002})
    sched_sub = compute_portfolio_schedule([m_sub1, m_sub2], quarterly_capacity_weeks=12.0)
    assert sched_sub.over_capacity is True
    assert sched_sub.used_capacity_weeks > sched_sub.capacity_weeks
    assert sched_sub.overflow_weeks > 0.0

    # 2. IEEE-754 exact boundary: 0.1 + 0.2 + 11.7 == 12.0, 1.1 + 2.2 + 8.7 == 12.0, 0.1 + 0.2 + 0.3 == 0.6
    for cap_val, triplet in (
        (12.0, (0.1, 0.2, 11.7)),
        (12.0, (1.1, 2.2, 8.7)),
        (0.6, (0.1, 0.2, 0.3)),
    ):
        boundary_opps = [
            _make_opp(
                opp_id=f"OPP-B{idx}",
                status="active",
                effort_weeks=ew,
                pinned_score=90.0 - idx,
            )
            for idx, ew in enumerate(triplet, start=1)
        ]
        sched_exact = compute_portfolio_schedule(
            boundary_opps, quarterly_capacity_weeks=cap_val
        )
        assert sched_exact.over_capacity is False
        assert sched_exact.used_capacity_weeks == cap_val
        assert sched_exact.remaining_capacity_weeks == 0.0
        assert sched_exact.overflow_weeks == 0.0
        assert sched_exact.funded_ids == ["OPP-B1", "OPP-B2", "OPP-B3"]

    # 3. m1_challenger_1 exact verification probe
    s = redact_text("jdbc:databricks://host:443;PWD=Pass@word123")
    assert "word123" not in s and "Pass" not in s, s
    with pytest.raises(ValidationError):
        ValuationSpec(
            confidence_tier="directional",
            driver_formula="a + " + "9" * 400,
            drivers=[DriverSpec(name="a", value=10.0)],
        )
    with pytest.raises(ValidationError):
        ScoringWeightsConfig(confidence_multipliers={"directional": float("nan")})
    with pytest.raises(ValidationError):
        PoopdeckConfig(quarterly_capacity_weeks=float("inf"))
    o1 = OpportunitySpec(
        id="OPP-1",
        title="M1",
        status="active",
        boss_mandate=True,
        valuation=ValuationSpec(
            confidence_tier="directional",
            driver_formula="a*1",
            drivers=[DriverSpec(name="a", value=100.0)],
        ),
        effort_weeks=6.004,
        data_readiness_score=4,
        adoption_friction_score=2,
    )
    o2 = o1.model_copy(update={"id": "OPP-2"})
    with pytest.raises(ValueError):
        compute_portfolio_schedule([o1], float("nan"))
    sched_probe = compute_portfolio_schedule([o1, o2], 12.0)
    assert sched_probe.over_capacity is True, sched_probe


def test_lineage_typed_alias_collision_and_duplicate_opp_id_validation(
    tmp_path: Path,
) -> None:
    vdir = tmp_path / "vault"
    initialize_vault(vdir, seed_questions=False, init_git=False)

    ds_sales = DatasetCard(fqn="prod.gold.sales_pipeline")
    ds_crm = DatasetCard(fqn="prod.gold.crm_deals")
    upsert_markdown_card(
        vdir / "datasets" / "prod.gold.sales_pipeline.md",
        ds_sales,
        render_dataset_body(ds_sales),
    )
    upsert_markdown_card(
        vdir / "datasets" / "prod.gold.crm_deals.md",
        ds_crm,
        render_dataset_body(ds_crm),
    )

    rep = ReportCard(
        title="Sales Pipeline",
        workspace="Exec",
        upstream_datasets=["[[sales_pipeline]]"],
        bi_to_ds_gaps=["Uses static stage-probability weights"],
    )
    upsert_markdown_card(
        vdir / "reports" / "sales-pipeline.md",
        rep,
        render_report_body(rep),
    )

    sh = StakeholderCard(
        name="Sarah Jenkins",
        role="VP Sales",
        owned_reports=["[[sales-pipeline]]"],
    )
    upsert_markdown_card(
        vdir / "stakeholders" / "sarah-jenkins.md",
        sh,
        render_stakeholder_body(sh),
    )

    opp = OpportunitySpec(
        id="OPP-010",
        title="Deal Win Probability Scoring",
        status="backlog",
        internal_datasets=[InternalDatasetUse(ref="[[prod.gold.crm_deals]]")],
        existing_reports_supplemented=[
            ReportSupplement(
                ref="[[reports/sales-pipeline]]", integration_mode="Add win %"
            ),
            ReportSupplement(ref="[[sales-pipeline]]", integration_mode="Add win %"),
        ],
        valuation=ValuationSpec(
            confidence_tier="directional",
            driver_formula="deals * lift",
            drivers=[
                DriverSpec(name="deals", value=1000.0, verified=True),
                DriverSpec(name="lift", value=250.0, verified=False),
            ],
        ),
        effort_weeks=3.0,
        data_readiness_score=4,
        adoption_friction_score=2,
        stakeholders=["[[Sarah Jenkins]]"],
    )
    upsert_markdown_card(
        vdir / "opportunities" / "OPP-010-win-prob.md",
        opp,
        render_opportunity_body(opp),
    )

    # 1. Typed alias lookup resolves bare [[sales-pipeline]] & [[reports/sales-pipeline]] without dataset collision
    graph = build_lineage_graph(vdir, include_staging=True)
    assert {
        "source": "opportunity:OPP-010",
        "target": "existing_report:Exec__Sales Pipeline",
        "relation": "supplements_report",
    } in graph["edges"]
    assert {
        "source": "dataset:prod.gold.sales_pipeline",
        "target": "opportunity:OPP-010",
        "relation": "inputs_to_opportunity",
    } not in graph["edges"]
    assert {
        "source": "dataset:prod.gold.sales_pipeline",
        "target": "existing_report:Exec__Sales Pipeline",
        "relation": "feeds_report",
    } in graph["edges"]
    assert {
        "source": "stakeholder:sarah-jenkins",
        "target": "existing_report:Exec__Sales Pipeline",
        "relation": "owns_report",
    } in graph["edges"]

    brief_res = generate_opportunity_brief(vdir, "OPP-010", fmt="teams")
    assert "Uses static stage-probability weights" in brief_res.markdown

    # 2. Clean vault validates, then duplicate opp.id across staging/ and opportunities/ fails validation
    assert validate_vault(vdir, strict=True).valid is True

    dup_staged = opp.model_copy(update={"status": "staging"})
    staged_dup_path = vdir / "staging" / "opportunities" / "OPP-010-dup.md"
    upsert_markdown_card(
        staged_dup_path, dup_staged, render_opportunity_body(dup_staged)
    )
    dup_report = validate_vault(vdir, strict=False)
    assert dup_report.valid is False
    assert any(
        "Duplicate opportunity id" in err and "OPP-010" in err
        for err in dup_report.errors
    )


# ===========================================================================
# Group 10: Iteration 3 ReDoS, Multi-Pass Idempotency, Set/Frozenset & Packs Suite
# ===========================================================================


def test_redact_text_linear_time_on_unclosed_credentials_and_identifiers() -> None:
    pathological_payloads = (
        'PWD="' + ("\\a" * 2500),
        'PWD="C:' + ("\\dir" * 1000),
        "password='" + ("\\" * 2500),
        "api_key='" + ("\\'" * 2500),
        "PWD={" + ("}}" * 2500),
        "PWD={" + ("\\a" * 2500),
        "pwd={ " * 850,
        ("a." * 2500) + "@b." + ("c." * 2500),
        "-----BEGIN " * 600,
        "-eyJ" * 1500,
    )
    for payload in pathological_payloads:
        t0 = time.perf_counter()
        _ = redact_text(payload)
        dt_ms = (time.perf_counter() - t0) * 1000.0
        assert dt_ms < 50.0, f"ReDoS regression ({dt_ms:.2f} ms) on {payload[:30]!r}"

    t0_col = time.perf_counter()
    assert is_pii_column("A" * 6000, comment="A" * 6000) is False
    dt_col_ms = (time.perf_counter() - t0_col) * 1000.0
    assert dt_col_ms < 50.0, f"Column normalization ReDoS ({dt_col_ms:.2f} ms)"

    for closed_raw, expected_prefix in (
        ('PWD="' + ("\\a" * 2500) + '"', "PWD=[REDACTED:SECRET]"),
        ("password='" + ("\\'" * 2500) + "'", "password=[REDACTED:SECRET]"),
        ("PWD={" + ("}}" * 2500) + "}", "PWD=[REDACTED:SECRET]"),
    ):
        t0_c = time.perf_counter()
        scrubbed = redact_text(closed_raw)
        dt_c_ms = (time.perf_counter() - t0_c) * 1000.0
        assert dt_c_ms < 50.0
        assert scrubbed == expected_prefix


def test_redact_text_multipass_idempotency_and_seeded_corpus() -> None:
    deterministic_cases = (
        ('PWD="Pass\\"word\\\\123"', "PWD=[REDACTED:SECRET]"),
        ('Password="Pass""word123";UID=svc', "Password=[REDACTED:SECRET];UID=svc"),
        ("password='Pass\\'word123'", "password=[REDACTED:SECRET]"),
        ("password='Pass''word123'", "password=[REDACTED:SECRET]"),
        ('PWD="Pass@word123"trailing', "PWD=[REDACTED:SECRET]trailing"),
        ("PWD='Pass@word123'trailing", "PWD=[REDACTED:SECRET]trailing"),
        ('PWD="Pass@word123"_suffix', "PWD=[REDACTED:SECRET]_suffix"),
        ("PWD={P@ss;word:123!}trailing", "PWD=[REDACTED:SECRET]trailing"),
        (
            "PWD={P@ss;word:123!}UID=alice@corp.com",
            "PWD=[REDACTED:SECRET]UID=[REDACTED:EMAIL]",
        ),
        ("PWD={My}}Brace@123}NextToken99", "PWD=[REDACTED:SECRET]NextToken99"),
        (
            "jdbc:databricks://host:443;PWD=Pass@word123",
            "jdbc:databricks://host:443;PWD=[REDACTED:SECRET]",
        ),
        (
            "postgres://admin:P@ss:word123@db.internal:5432/prod",
            "postgres://admin:[REDACTED:SECRET]@db.internal:5432/prod",
        ),
        (
            "https://alice@corp.com:S3cr3t!@api.corp.com:8443/v1?sig=SecSig99",
            "https://[REDACTED:EMAIL]:[REDACTED:SECRET]@api.corp.com:8443/v1?sig=[REDACTED:SECRET]",
        ),
        ('PWD="UnclosedSecret123', "PWD=[REDACTED:SECRET]"),
        ("custom_pwd=Sec1", "custom_pwd=[REDACTED:SECRET]"),
        ('{"k": "PWD="}', '{"k": "PWD="}'),
        ('Avoid "PWD=".', 'Avoid "PWD=".'),
        ("notapwd=keep", "notapwd=keep"),
        (
            "+44 20 7946 0958+44 20 7946 0958",
            "[REDACTED:PHONE][REDACTED:PHONE]",
        ),
    )
    for raw, expected in deterministic_cases:
        p1 = redact_text(raw)
        p2 = redact_text(p1)
        assert p1 == expected, f"{raw!r} -> {p1!r} != {expected!r}"
        assert p1 == p2, f"Idempotency failed: {raw!r} -> {p1!r} -> {p2!r}"

    tokens = (
        'PWD="Sec\\"ret\\\\1"',
        "password='Sec@ret2'",
        'PWD="Unclosed9',
        "custom_pwd=S1",
        "PWD={P@ss;word:123!}",
        "PWD={My}}Brace@1}",
        "api_key=PlainSecret99",
        "UID=alice@corp.com",
        "jdbc:databricks://host:443;PWD=Pass@word123",
        "postgres://u:p@ss:1@db.corp:5432/x",
        "https://api.corp.com:8443)",
        "?sig=AbCd123%2B%3D",
        "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.SflKxwRJ",
        "dapi0123456789abcdef012345",
        "123-45-6789",
        "+1-800-555-0199",
        "(555) 234-5678",
        "+44 20 7946 0958",
        "trailing",
        "_suffix",
        ";",
        ")",
        " ",
    )
    rng = random.Random(42)
    for idx in range(200):
        sample = "".join(rng.choice(tokens) for _ in range(rng.randint(2, 8)))
        p1 = redact_text(sample)
        p2 = redact_text(p1)
        assert p1 == p2, f"Seed-42 trial {idx} failed idempotency: {sample!r} -> {p1!r} -> {p2!r}"


def test_redact_dataset_payload_set_frozenset_and_vault_yaml_roundtrip(
    tmp_path: Path,
) -> None:
    raw_payload = {
        "fqn": "prod.gold.events",
        "grain": "1 row per event (contact owner@corp.com)",
        "tags": {
            "owners": {"alice@corp.com", "PWD=TagSec123"},
            "frozen_meta": frozenset({"bob@corp.com", "api_key=FrozenTag456"}),
        },
        "columns": (
            {
                "name": "event_notes",
                "data_type": "STRING",
                "min_val": {"dsn": "PWD=StructSec222"},
                "sample_values": {
                    "carol@corp.com",
                    "PWD=ColSec789",
                    frozenset({"nested@corp.com"}),
                },
            },
            {
                "name": "audit_context",
                "data_type": "STRING",
                "sample_values": frozenset(
                    {"dave@corp.com", "jdbc:databricks://h:443;PWD=FrozenCol000"}
                ),
            },
            {
                "name": "customer_email",
                "data_type": "STRING",
                "sample_values": frozenset({"eve@corp.com", "frank@corp.com"}),
            },
        ),
        "sample_rows": (
            {
                "event_notes": {"grace@corp.com", "PWD={RowBraceSec111}"},
                "audit_context": frozenset(
                    {"heidi@corp.com", "sk-ant-1234567890abcdef"}
                ),
                "customer_email": "eve@corp.com",
            },
        ),
    }
    snapshot = copy.deepcopy(raw_payload)

    redacted = redact_dataset_payload(
        raw_payload, PrivacyConfig(allow_sample_rows=True, max_sample_rows=5)
    )
    assert raw_payload == snapshot
    flat_repr = repr(redacted)
    for leaked in (
        "owner@corp.com",
        "alice@corp.com",
        "bob@corp.com",
        "carol@corp.com",
        "nested@corp.com",
        "dave@corp.com",
        "eve@corp.com",
        "frank@corp.com",
        "grace@corp.com",
        "heidi@corp.com",
        "TagSec123",
        "FrozenTag456",
        "StructSec222",
        "ColSec789",
        "FrozenCol000",
        "RowBraceSec111",
        "sk-ant-",
    ):
        assert leaked not in flat_repr, f"Leaked {leaked!r} in {flat_repr}"

    assert isinstance(redacted["columns"], list)
    assert isinstance(redacted["tags"]["owners"], list)
    assert isinstance(redacted["tags"]["frozen_meta"], list)
    assert isinstance(redacted["columns"][0]["sample_values"], list)
    assert isinstance(redacted["columns"][1]["sample_values"], list)
    assert redacted["columns"][1]["sample_values"] == [
        "[REDACTED:EMAIL]",
        "jdbc:databricks://h:443;PWD=[REDACTED:SECRET]",
    ]
    assert redacted["columns"][2]["is_pii"] is True
    assert redacted["columns"][2]["sample_values"] == [
        "[REDACTED:PII]",
        "[REDACTED:PII]",
    ]
    assert isinstance(redacted["sample_rows"], list)
    assert isinstance(redacted["sample_rows"][0]["event_notes"], list)
    assert isinstance(redacted["sample_rows"][0]["audit_context"], list)
    assert redacted["sample_rows"][0]["customer_email"] == "[REDACTED:PII]"
    assert json.dumps(redacted, allow_nan=False)

    card = DatasetCard.model_validate(redacted)
    assert all(isinstance(v, str) for v in card.columns[1].sample_values)
    assert json.dumps(card.model_dump(mode="json"), allow_nan=False)

    vdir = tmp_path / "vault"
    initialize_vault(vdir, seed_questions=True, init_git=False)
    notes = "\nPreserved analyst note on set/frozenset dataset.\n"
    md_path = vdir / "datasets" / "prod.gold.events.md"
    upsert_markdown_card(
        md_path, card, render_dataset_body(card), human_notes_override=notes
    )

    parsed = parse_markdown_card(md_path)
    assert parsed.human_notes == notes
    roundtripped = DatasetCard.model_validate(parsed.frontmatter)
    assert roundtripped.fqn == "prod.gold.events"
    assert roundtripped.sample_rows[0]["customer_email"] == "[REDACTED:PII]"
    assert validate_vault(vdir, strict=True).valid is True


def test_packs_models_reject_non_finite_and_bool_while_coercing_int(
    tmp_path: Path,
) -> None:
    models = [
        cls
        for mod in (cfg_mod, mod_mod, packs_mod, vio_mod, brief_mod)
        for _, cls in inspect.getmembers(mod, inspect.isclass)
        if issubclass(cls, BaseModel) and cls.__module__ == mod.__name__
    ]
    assert len(models) == 35
    for cls in models:
        assert cls.model_config.get("allow_inf_nan") is False, cls
        assert cls.model_config.get("validate_assignment") is True, cls

    coerced = ArchetypeSpec(id="arch_int", title="Int Effort", typical_effort_weeks=3)
    assert coerced.typical_effort_weeks == 3.0
    assert isinstance(coerced.typical_effort_weeks, float)

    rep_ok = ValidationReport(valid=True, counts={"datasets": 3})
    assert rep_ok.counts == {"datasets": 3}
    for bad_bool in (True, False):
        with pytest.raises(ValidationError):
            ValidationReport(valid=True, counts={"datasets": bad_bool})  # type: ignore[dict-item]
        with pytest.raises(ValidationError):
            rep_ok.counts = {"datasets": bad_bool}  # type: ignore[dict-item]

    invalid_efforts = (
        float("nan"),
        float("inf"),
        float("-inf"),
        "nan",
        "inf",
        "-inf",
        True,
        False,
        0,
        -1.5,
    )
    for bad in invalid_efforts:
        with pytest.raises(ValidationError):
            ArchetypeSpec(id="a", title="A", typical_effort_weeks=bad)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            coerced.typical_effort_weeks = bad  # type: ignore[assignment]
        with pytest.raises(ValidationError):
            ExternalDataCatalog.model_validate(
                {
                    "archetypes": [
                        {"id": "a", "title": "A", "typical_effort_weeks": bad}
                    ],
                    "domain_lenses": {
                        "generic": {"slug": "generic", "title": "Generic"}
                    },
                }
            )

    lens = DomainLens(slug="generic", title="Generic")
    entry = ExternalDatasetEntry(id="fred", source="FRED")
    for bad_scalar in (float("nan"), float("inf"), float("-inf"), True, False):
        with pytest.raises(ValidationError):
            DomainLens(slug=bad_scalar, title="Generic")  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            lens.title = bad_scalar  # type: ignore[assignment]
        with pytest.raises(ValidationError):
            ExternalDatasetEntry(id="fred", source=bad_scalar)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            entry.provider = bad_scalar  # type: ignore[assignment]
        with pytest.raises(ValidationError):
            ExternalDataCatalog(
                version=bad_scalar,  # type: ignore[arg-type]
                domain_lenses={"generic": lens},
            )

    for yaml_lit in (".nan", ".inf", "-.inf", "true", "false"):
        bad_yaml = tmp_path / f"bad_catalog_{yaml_lit.strip('.-')}.yaml"
        bad_yaml.write_text(
            "version: '1.0'\n"
            "archetypes:\n"
            "  - id: a\n"
            "    title: A\n"
            f"    typical_effort_weeks: {yaml_lit}\n"
            "domain_lenses:\n"
            "  generic:\n"
            "    slug: generic\n"
            "    title: Generic\n",
            encoding="utf-8",
        )
        with pytest.raises(ValidationError):
            load_external_data_catalog(bad_yaml)


# ===========================================================================
# Group 11: Iteration 4 URL Idempotency, Quote-State Guard, Composite Keys & NaN Vault Audit
# ===========================================================================
def test_url_basic_auth_phone_email_multipass_idempotency() -> None:
    targeted_urls = (
        "https://(555) 234-5678:pass@localhost",
        "https://admin:(555) 234-5678!@corp.com",
        "http://user:+44 20 7946 0958:secret@10.0.0.1:8080",
        "postgres://+1 800 555 0199:P@ss123@db-internal:5432/prod",
        "(https://:8080+44 20 7946 0958+44 20 7946 0958@localhost",
        "{jdbc:databricks://&+44 20 7946 0958:8080=dapi0123456789abcdef0123@localhost",
        "https://alice@corp.com:S3cr3t!@api.corp.com:8443/v1?sig=SecSig99",
        "https://admin:S3cr3t!@[2001:db8::1]:8443/v1",
    )
    for raw in targeted_urls:
        p1 = redact_text(raw)
        assert p1 == redact_text(p1) == redact_text(redact_text(p1)), (raw, p1)
    assert (
        redact_text("https://alice@corp.com:S3cr3t!@api.corp.com:8443/v1?sig=SecSig99")
        == "https://[REDACTED:EMAIL]:[REDACTED:SECRET]@api.corp.com:8443/v1?sig=[REDACTED:SECRET]"
    )

    frags = (
        "https://",
        "jdbc:databricks://",
        "(555) 234-5678",
        "+44 20 7946 0958",
        "alice@corp.com",
        ":8080",
        ":S3c!",
        "@localhost",
        "PWD=Sec1",
    )
    rng = random.Random(20261112)
    for _ in range(250):
        s = "".join(rng.choice(frags) for _ in range(rng.randint(3, 7)))
        p1 = redact_text(s)
        assert p1 == redact_text(p1)


def test_rule3_closing_quote_guard_preserves_code_json_and_scrubs_secrets() -> None:
    for safe_line in (
        'conn = "jdbc:databricks://host:443;PWD=" + dbutils.secrets.get("scope", "key")',
        '{"field": "PWD=", "type": "string"}',
        '{"a": "password=", "b": "keep_me"}',
        'Avoid "PWD=" and "API_KEY=" in configs.',
        "SQL check: WHERE col = 'PWD=' OR col = 'UID='",
        "DB_PWD=\nDB_HOST=prod.corp",
        "It's important to avoid 'PWD=' and Sarah's 'API_KEY=' in docs.",
    ):
        assert redact_text(safe_line) == safe_line

    for raw, expected in (
        (
            '{"conn": "jdbc:databricks://host:443;PWD=RealSecret99", "k": "v"}',
            '{"conn": "jdbc:databricks://host:443;PWD=[REDACTED:SECRET]", "k": "v"}',
        ),
        (
            '{"conn": "jdbc:databricks://h:443;PWD=\\"MySec\\"", "k": "v"}',
            '{"conn": "jdbc:databricks://h:443;PWD=[REDACTED:SECRET]", "k": "v"}',
        ),
        (
            '{"hint": "PWD=", "dsn": "PWD=RealSecret99"}',
            '{"hint": "PWD=", "dsn": "PWD=[REDACTED:SECRET]"}',
        ),
        (
            'EXAMPLE="PWD=" PASSWORD="SuperSecret99"',
            'EXAMPLE="PWD=" PASSWORD=[REDACTED:SECRET]',
        ),
        ('sql = \'SET PWD="MySecret99"\'', "sql = 'SET PWD=[REDACTED:SECRET]'"),
        ('cmd = "SET PWD=\'MySecret99\'"', 'cmd = "SET PWD=[REDACTED:SECRET]"'),
    ):
        p1 = redact_text(raw)
        assert p1 == expected and redact_text(p1) == p1
        if expected.startswith("{"):
            assert isinstance(json.loads(p1), dict)


def test_composite_dict_keys_json_yaml_roundtrip_and_hashseed_determinism(
    tmp_path: Path,
) -> None:
    import concurrent.futures
    import os
    import sys

    red = redact_dataset_payload(
        {
            "fqn": "prod.gold.composite_keys",
            "tags": {
                ("t", frozenset({"k1", "k2"})): "PWD=TagSec1",
                frozenset({"alice@corp.com", "id"}): "v_alice",
                frozenset({"bob@corp.com", "id"}): "v_bob",
            },
            "sample_rows": [
                {
                    ("tuple_col", "ivan@corp.com"): "PWD=RowSec2",
                    ("tuple_col", "customer_email"): "Ivan Petrov",
                    ("user_email", 1): "a@b.com",
                }
            ],
        },
        PrivacyConfig(allow_sample_rows=True),
    )
    assert all(
        tok not in repr(red)
        for tok in (
            "TagSec1",
            "ivan@corp.com",
            "RowSec2",
            "Ivan Petrov",
            "a@b.com",
            "alice@corp.com",
            "bob@corp.com",
        )
    )
    assert json.dumps(red, allow_nan=False)
    card = DatasetCard.model_validate(red)
    assert json.dumps(card.model_dump(mode="json"), allow_nan=False)

    vdir = tmp_path / "vault"
    initialize_vault(vdir, seed_questions=False, init_git=False)
    upsert_markdown_card(
        vdir / "datasets" / "prod.gold.composite_keys.md",
        card,
        render_dataset_body(card),
    )
    assert validate_vault(vdir, strict=True).valid is True

    probe = (
        "from poopdeck.config import PrivacyConfig; from poopdeck.redaction import redact_dataset_payload; "
        "from poopdeck.models import DatasetCard; "
        "r = redact_dataset_payload({'fqn':'c.s.t','tags':{('t',frozenset({'k1','k2'})):'v',"
        "frozenset({'a@corp.com','id'}):'1',frozenset({'b@corp.com','id'}):'2'}}, PrivacyConfig()); "
        "print(sorted(DatasetCard.model_validate(r).tags.items()))"
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        outs = set(
            pool.map(
                lambda s: subprocess.run(
                    [sys.executable, "-B", "-c", probe],
                    cwd=REPO_ROOT,
                    env={
                        **os.environ,
                        "PYTHONHASHSEED": s,
                        "PYTHONDONTWRITEBYTECODE": "1",
                    },
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip(),
                ("1", "2", "42"),
            )
        )
    assert len(outs) == 1


def test_any_containers_parsed_card_and_validate_vault_reject_nan_inf_and_bad_yaml_keys(
    tmp_path: Path,
) -> None:
    from decimal import Decimal

    bad_numbers = (
        float("nan"),
        float("inf"),
        float("-inf"),
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        Decimal("1e500"),
        1 + 2j,
    )
    for bad in bad_numbers:
        with pytest.raises(ValidationError):
            ColumnProfile(name="c", sample_values=[{"nested": [bad]}])
        with pytest.raises(ValidationError):
            ColumnProfile(name="c", min_val=bad)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            DatasetCard(fqn="c.s.t", sample_rows=[{"x": bad}])
        with pytest.raises(ValidationError):
            DatasetCard(fqn="c.s.t", tags={"k": bad})  # type: ignore[dict-item]
        with pytest.raises(ValidationError):
            DatasetCard(fqn="c.s.t", join_keys=[bad])  # type: ignore[list-item]
        with pytest.raises(ValidationError):
            DatasetCard(fqn="c.s.t", grain=bad)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            DatasetCard(fqn="c.s.t", min_timestamp=bad)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            DatasetCard(fqn="c.s.t", catalog=bad)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            DatasetCard(fqn="c.s.t", medallion_layer=bad)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            ReportCard(title="R", relationships=[{"x": bad}])
        with pytest.raises(ValidationError):
            vio_mod.ParsedCard(frontmatter={"extra": [bad]})

    for bad_fm in ({123: "bad"}, {True: "bad"}, {"sample_rows": [{456: "bad"}]}):
        with pytest.raises(ValidationError):
            vio_mod.ParsedCard(frontmatter=bad_fm)  # type: ignore[arg-type]

    vdir = tmp_path / "vault"
    initialize_vault(vdir, seed_questions=False, init_git=False)
    corrupt_cards = {
        "datasets/nan_sv.md": "fqn: c.s.t1\ncolumns:\n  - name: c\n    sample_values: [.nan]\n",
        "datasets/inf_sr.md": "fqn: c.s.t2\nsample_rows:\n  - x: .inf\n",
        "reports/neginf_rel.md": "title: R\nrelationships:\n  - a: -.inf\n",
        "datasets/int_key.md": "123: bad\nfqn: c.s.t3\n",
        "datasets/bool_key.md": "true: bad\nfqn: c.s.t4\n",
        "datasets/nested_int_key.md": "fqn: c.s.t5\nsample_rows:\n  - 456: bad\n",
        "datasets/seq_key.md": "? [a, b]\n: bad\nfqn: c.s.t6\n",
    }
    for rel, fm in corrupt_cards.items():
        (vdir / rel).write_text(
            f"---\n{fm}---\nBody\n\n{HUMAN_NOTES_START}\nn\n{HUMAN_NOTES_END}\n",
            encoding="utf-8",
        )

    rep = validate_vault(vdir, strict=False)
    assert rep.valid is False
    assert all(any(rel in err for err in rep.errors) for rel in corrupt_cards)
