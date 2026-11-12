"""Requirement acceptance tests for F04: Vault Schemas, Non-Destructive Markdown I/O & Seed Bank.

Covers Tier 1 happy-path contracts and Tier 2 boundary, encoding, recovery, and validation
invariants across `poopdeck.models`, `poopdeck.vault_io`, and `poopdeck.templates`.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _make_sample_opportunity_dict(
    opp_id: str = "OPP-001",
    title: str = "Safety Stock Optimization",
    status: str = "backlog",
    dataset_ref: str = "[[prod_catalog.gold.orders]]",
    report_ref: str = "[[Supply Chain__Daily Buyer Workbench]]",
    stakeholder_ref: str = "[[Sarah Chen]]",
) -> dict[str, object]:
    """Construct a valid OpportunitySpec payload dictionary at runtime."""
    return {
        "id": opp_id,
        "title": title,
        "status": status,
        "boss_mandate": False,
        "domain_pack": "ops_supply_chain",
        "internal_datasets": (
            [
                {
                    "ref": dataset_ref,
                    "role": "demand_signal",
                    "transformations_needed": ["Aggregate weekly SKU demand"],
                }
            ]
            if dataset_ref
            else []
        ),
        "external_data_required": [
            {
                "source": "NOAA GHCN Daily Weather",
                "purpose": "Cold-chain transit delay covariates",
                "acquisition": "free_public_api",
                "blocker_severity": "low",
            }
        ],
        "existing_reports_supplemented": (
            [{"ref": report_ref, "integration_mode": "Add reorder risk tier column"}]
            if report_ref
            else []
        ),
        "new_reports_unlocked": [
            {
                "title": "Executive SLA Simulator",
                "audience": "VP Supply Chain",
                "why_net_new": "Interactive service-level vs working-capital frontier",
            }
        ],
        "valuation": {
            "confidence_tier": "directional",
            "driver_formula": "annual_spend * reduction_pct",
            "drivers": [
                {
                    "name": "annual_spend",
                    "value": 2_000_000.0,
                    "source": "ERP FY26 baseline",
                    "verified": True,
                },
                {
                    "name": "reduction_pct",
                    "range": [0.05, 0.10, 0.15],
                    "source": "Industry benchmark",
                    "verified": False,
                    "verify_question": "Confirm safety-stock carrying cost rate with Finance",
                },
            ],
        },
        "effort_weeks": 4.0,
        "data_readiness_score": 4,
        "adoption_friction_score": 2,
        "steps_to_completion": [
            "Join Gold orders with Silver inventory",
            "Backtest quantile lead-time model",
            "Publish risk column to Daily Buyer Workbench",
        ],
        "stakeholders": [stakeholder_ref] if stakeholder_ref else [],
    }


@pytest.mark.tier1
def test_f04_t1_pydantic_vault_entity_models_and_wikilinks() -> None:
    """Validate all 7 canonical vault Pydantic v2 models and [[wikilink]] helpers."""
    from poopdeck.models import (
        ColumnProfile,
        DatasetCard,
        DaxMeasure,
        GlossaryCard,
        GlossaryTerm,
        OpenQuestionBank,
        OpportunitySpec,
        PlaybookPhase,
        PlaybookTracker,
        QuestionItem,
        ReportCard,
        StakeholderCard,
    )
    from poopdeck.vault_io import (
        extract_wikilinks,
        format_wikilink,
        normalize_wikilink_target,
    )

    uc_card = DatasetCard.model_validate(
        {
            "fqn": "prod_catalog.gold.orders",
            "table_type": "MANAGED",
            "row_count": 125_000,
            "freshness_lag_hours": 2.5,
            "grain": "one row per order_id",
            "join_keys": ["order_id", "sku_id"],
            "columns": [
                ColumnProfile(
                    name="order_id",
                    data_type="STRING",
                    nullable=False,
                    null_pct=0.0,
                    distinct_count=125_000,
                ).model_dump()
            ],
            "upstream_refs": ["[[prod_catalog.silver.orders_clean]]"],
            "downstream_refs": ["[[Supply Chain__Daily Buyer Workbench]]"],
        }
    )
    assert uc_card.id == "prod_catalog.gold.orders"
    assert uc_card.catalog == "prod_catalog"
    assert uc_card.schema_name == "gold"
    assert uc_card.table_name == "orders"
    assert uc_card.medallion_layer == "gold"
    assert uc_card.upstream_refs == ["[[prod_catalog.silver.orders_clean]]"]

    hive_card = DatasetCard.model_validate(
        {
            "fqn": "legacy_erp.bronze_shipments",
            "table_type": "EXTERNAL",
            "grain": "shipment_line_id",
        }
    )
    assert hive_card.catalog == "hive_metastore"
    assert hive_card.fqn == "hive_metastore.legacy_erp.bronze_shipments"
    assert hive_card.medallion_layer == "bronze"

    report_card = ReportCard.model_validate(
        {
            "title": "Daily Buyer Workbench",
            "workspace": "Supply Chain",
            "semantic_model": "BuyerSemanticModel",
            "upstream_datasets": ["[[prod_catalog.gold.orders]]"],
            "owners": ["[[Sarah Chen|VP Supply Chain]]"],
            "measures": [
                DaxMeasure(
                    name="Net Revenue",
                    table="Orders",
                    expression="SUM('Orders'[net_usd])",
                ).model_dump()
            ],
        }
    )
    assert report_card.id == "Supply Chain__Daily Buyer Workbench"
    assert report_card.upstream_datasets == ["[[prod_catalog.gold.orders]]"]

    stakeholder_card = StakeholderCard.model_validate(
        {
            "name": "Sarah Chen",
            "role": "VP Supply Chain",
            "department": "Operations",
            "influence_tier": "exec",
            "decision_appetite": "high",
            "owned_reports": ["[[Supply Chain__Daily Buyer Workbench]]"],
        }
    )
    assert stakeholder_card.id == "sarah-chen"
    assert stakeholder_card.owned_reports == ["[[Supply Chain__Daily Buyer Workbench]]"]

    opp = OpportunitySpec.model_validate(_make_sample_opportunity_dict())
    assert opp.valuation.annual_benefit_low_usd == 100_000.0
    assert opp.valuation.annual_benefit_base_usd == 200_000.0
    assert opp.valuation.annual_benefit_high_usd == 300_000.0
    assert opp.computed_priority_score is not None and opp.computed_priority_score > 0.0

    q_bank = OpenQuestionBank(
        questions=[
            QuestionItem(
                id="Q-MGR-01",
                category="manager",
                who_to_ask="Hiring Manager",
                topic="Mandate",
                question="What is the top 90-day priority?",
                linked_refs=["[[OPP-001]]"],
            )
        ]
    )
    assert len(q_bank.questions) == 1 and q_bank.questions[0].status == "open"

    pb_tracker = PlaybookTracker(
        phases=[
            PlaybookPhase(
                id="PHASE-1",
                phase="Days 1-5: Wire & Map",
                goal="Bootstrap discovery vault",
                actions=["Run poopdeck init"],
                output="Seeded vault",
            )
        ]
    )
    assert len(pb_tracker.phases) == 1 and pb_tracker.phases[0].completed is False

    glossary = GlossaryCard(
        terms=[
            GlossaryTerm(
                term="OTIF",
                definition="On-Time In-Full delivery rate",
                source="Ops Sync",
                linked_refs=["[[prod_catalog.gold.orders]]"],
            )
        ]
    )
    assert glossary.id == "glossary" and glossary.terms[0].term == "OTIF"

    assert normalize_wikilink_target("[[prod_catalog.gold.orders|Orders#Schema]]") == "prod_catalog.gold.orders"
    assert format_wikilink("Sarah Chen") == "[[Sarah Chen]]"
    assert format_wikilink("[[Sarah Chen]]") == "[[Sarah Chen]]"
    prose = "Link [[prod_catalog.gold.orders|Gold Orders]] to [[Sarah Chen#KPIs]] and [[prod_catalog.gold.orders]]."
    assert extract_wikilinks(prose) == ["prod_catalog.gold.orders", "Sarah Chen"]


@pytest.mark.tier1
def test_f04_t1_upsert_and_parse_markdown_card_preserves_multiline_human_notes_byte_for_byte(
    vault_sandbox: Path,
) -> None:
    """Verify byte-for-byte preservation of HUMAN-NOTES across Dataset, Report, and Stakeholder re-syncs."""
    from poopdeck.models import DatasetCard, ReportCard, StakeholderCard
    from poopdeck.vault_io import (
        DEFAULT_HUMAN_NOTES,
        HUMAN_NOTES_END,
        HUMAN_NOTES_START,
        parse_markdown_card,
        render_dataset_body,
        render_report_body,
        render_stakeholder_body,
        upsert_markdown_card,
    )

    ds_path = vault_sandbox / "datasets" / "prod_catalog.gold.orders.md"
    ds_initial = DatasetCard(
        id="prod_catalog.gold.orders",
        fqn="prod_catalog.gold.orders",
        catalog="prod_catalog",
        schema_name="gold",
        table_name="orders",
        row_count=1_200,
        freshness_lag_hours=6.0,
    )
    upsert_markdown_card(ds_path, ds_initial, render_dataset_body(ds_initial))
    raw_initial = ds_path.read_text(encoding="utf-8")
    assert raw_initial.endswith(f"{HUMAN_NOTES_START}{DEFAULT_HUMAN_NOTES}{HUMAN_NOTES_END}\n")
    assert parse_markdown_card(ds_path).human_notes == DEFAULT_HUMAN_NOTES

    custom_notes = (
        "\n\n### 1:1 Takeaway with [[Sarah Chen|VP Ops]]\n"
        "| Check | Nuance |\n"
        "|---|---|\n"
        "| Cutover | Exclude pre-2024 ERP migration rows — Δ margin café 📈 |\n"
        "Trailing intentional spaces ->   \n\n"
    )
    rewritten_disk = (
        raw_initial[: raw_initial.index(HUMAN_NOTES_START)]
        + f"{HUMAN_NOTES_START}{custom_notes}{HUMAN_NOTES_END}\n"
    )
    with open(ds_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(rewritten_disk)

    rep_path = vault_sandbox / "reports" / "Supply Chain__Daily Buyer Workbench.md"
    rep_initial = ReportCard(
        title="Daily Buyer Workbench",
        workspace="Supply Chain",
        high_excel_export=False,
    )
    upsert_markdown_card(
        rep_path,
        rep_initial,
        render_report_body(rep_initial),
        human_notes_override=custom_notes,
    )

    sh_path = vault_sandbox / "stakeholders" / "sarah-chen.md"
    sh_initial = StakeholderCard(
        name="Sarah Chen",
        role="Director Supply Chain",
        influence_tier="manager",
    )
    upsert_markdown_card(
        sh_path,
        sh_initial,
        render_stakeholder_body(sh_initial),
        human_notes_override=custom_notes,
    )

    # Automated re-sync with updated metadata and ZERO human_notes_override
    ds_updated = ds_initial.model_copy(update={"row_count": 98_500, "freshness_lag_hours": 1.5})
    upsert_markdown_card(ds_path, ds_updated, render_dataset_body(ds_updated))

    rep_updated = rep_initial.model_copy(update={"high_excel_export": True, "refresh_failures_30d": 3})
    upsert_markdown_card(rep_path, rep_updated, render_report_body(rep_updated))

    sh_updated = sh_initial.model_copy(update={"role": "VP Supply Chain", "influence_tier": "exec"})
    upsert_markdown_card(sh_path, sh_updated, render_stakeholder_body(sh_updated))

    ds_parsed = parse_markdown_card(ds_path)
    assert ds_parsed.human_notes == custom_notes
    assert ds_parsed.frontmatter["row_count"] == 98_500
    assert "98,500" in ds_parsed.body and "1.5h" in ds_parsed.body

    rep_parsed = parse_markdown_card(rep_path)
    assert rep_parsed.human_notes == custom_notes
    assert rep_parsed.frontmatter["high_excel_export"] is True
    assert "**High Excel Export Signal**: Yes" in rep_parsed.body

    sh_parsed = parse_markdown_card(sh_path)
    assert sh_parsed.human_notes == custom_notes
    assert sh_parsed.frontmatter["influence_tier"] == "exec"
    assert "**Influence Tier**: `exec`" in sh_parsed.body


@pytest.mark.tier1
def test_f04_t1_initialize_vault_seeds_15_questions_5_phases_and_glossary(
    vault_sandbox: Path,
) -> None:
    """Verify initialize_vault creates all 12 subdirs and idempotently seeds 15 questions, 5 phases, and glossary."""
    from poopdeck.models import GlossaryCard
    from poopdeck.vault_io import (
        VAULT_SUBDIRS,
        initialize_vault,
        load_questions_and_playbook,
        parse_markdown_card,
    )

    res = initialize_vault(vault_sandbox, seed_questions=True, init_git=False)
    assert res["has_git"] is False
    assert len(res["seeded_files"]) == 3
    for rel_sub in VAULT_SUBDIRS:
        assert (vault_sandbox / rel_sub).is_dir(), f"Missing vault subdir: {rel_sub}"

    bank, tracker = load_questions_and_playbook(vault_sandbox)
    expected_q_ids = [
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
    assert [q.id for q in bank.questions] == expected_q_ids
    assert {q.category for q in bank.questions} == {
        "manager",
        "it_security",
        "data_engineering",
        "power_bi",
        "business_finance",
    }

    assert [p.id for p in tracker.phases] == [
        "PHASE-1",
        "PHASE-2",
        "PHASE-3",
        "PHASE-4",
        "PHASE-5",
    ]
    assert all(p.completed is False for p in tracker.phases)

    glossary_parsed = parse_markdown_card(vault_sandbox / "glossary.md")
    gcard = GlossaryCard.model_validate(glossary_parsed.frontmatter)
    assert gcard.id == "glossary"
    assert gcard.terms == []

    res_repeat = initialize_vault(vault_sandbox, seed_questions=True, init_git=False)
    assert res_repeat["seeded_files"] == []


@pytest.mark.tier1
def test_f04_t1_validate_vault_clean_report_and_accurate_entity_counts(
    vault_sandbox: Path,
) -> None:
    """Verify validate_vault(strict=True) passes with zero errors/warnings on a fully cross-linked vault."""
    from poopdeck.models import DatasetCard, OpportunitySpec, ReportCard, StakeholderCard
    from poopdeck.vault_io import (
        initialize_vault,
        render_dataset_body,
        render_opportunity_body,
        render_report_body,
        render_stakeholder_body,
        upsert_markdown_card,
        validate_vault,
    )

    initialize_vault(vault_sandbox, seed_questions=True, init_git=False)

    ds_silver = DatasetCard(
        id="prod_catalog.silver.orders_clean",
        fqn="prod_catalog.silver.orders_clean",
        catalog="prod_catalog",
        schema_name="silver",
        table_name="orders_clean",
        downstream_refs=["[[prod_catalog.gold.orders]]"],
    )
    ds_gold = DatasetCard(
        id="prod_catalog.gold.orders",
        fqn="prod_catalog.gold.orders",
        catalog="prod_catalog",
        schema_name="gold",
        table_name="orders",
        upstream_refs=["[[prod_catalog.silver.orders_clean]]"],
    )
    upsert_markdown_card(
        vault_sandbox / "datasets" / "prod_catalog.silver.orders_clean.md",
        ds_silver,
        render_dataset_body(ds_silver),
    )
    upsert_markdown_card(
        vault_sandbox / "datasets" / "prod_catalog.gold.orders.md",
        ds_gold,
        render_dataset_body(ds_gold),
    )

    sh_card = StakeholderCard(
        name="Sarah Chen",
        role="VP Supply Chain",
        owned_reports=["[[Supply Chain__Daily Buyer Workbench]]"],
    )
    upsert_markdown_card(
        vault_sandbox / "stakeholders" / "sarah-chen.md",
        sh_card,
        render_stakeholder_body(sh_card),
    )

    rep_card = ReportCard(
        title="Daily Buyer Workbench",
        workspace="Supply Chain",
        upstream_datasets=["[[prod_catalog.gold.orders]]"],
        owners=["[[Sarah Chen]]"],
    )
    upsert_markdown_card(
        vault_sandbox / "reports" / "Supply Chain__Daily Buyer Workbench.md",
        rep_card,
        render_report_body(rep_card),
    )

    opp_active = OpportunitySpec.model_validate(
        _make_sample_opportunity_dict(opp_id="OPP-001", status="active")
    )
    upsert_markdown_card(
        vault_sandbox / "opportunities" / "OPP-001-safety-stock.md",
        opp_active,
        render_opportunity_body(opp_active),
    )

    opp_staged = OpportunitySpec.model_validate(
        _make_sample_opportunity_dict(
            opp_id="OPP-005",
            title="Vendor Lead-Time Quantile Model",
            status="staging",
        )
    )
    upsert_markdown_card(
        vault_sandbox / "staging" / "opportunities" / "OPP-005-lead-time.md",
        opp_staged,
        render_opportunity_body(opp_staged, cfo_critique="Passed initial CFO screen."),
    )

    report = validate_vault(vault_sandbox, strict=True)
    assert report.valid is True
    assert report.errors == []
    assert report.warnings == []
    assert report.counts == {
        "datasets": 2,
        "reports": 1,
        "stakeholders": 1,
        "questions": 15,
        "playbook": 5,
        "glossary": 1,
        "staging_opportunities": 1,
        "opportunities": 1,
    }


@pytest.mark.tier1
def test_f04_t1_staging_promote_archive_and_boss_mandate_preserve_human_notes(
    vault_sandbox: Path,
) -> None:
    """Verify promote_opportunity, archive_opportunity, and set_boss_mandate preserve HUMAN-NOTES and CFO sections."""
    from poopdeck.models import OpportunitySpec
    from poopdeck.vault_io import (
        archive_opportunity,
        initialize_vault,
        load_all_opportunities,
        parse_markdown_card,
        promote_opportunity,
        render_opportunity_body,
        set_boss_mandate,
        upsert_markdown_card,
    )

    initialize_vault(vault_sandbox, seed_questions=True, init_git=False)

    notes_005 = "\n### Reviewer Notes OPP-005\n- Verified SKU coverage with [[Sarah Chen]]\n"
    notes_006 = "\n### Reviewer Notes OPP-006\n- Hold until Q3 vendor renewal\n"

    opp_005 = OpportunitySpec.model_validate(
        _make_sample_opportunity_dict(opp_id="OPP-005", status="staging", report_ref="", stakeholder_ref="")
    )
    opp_006 = OpportunitySpec.model_validate(
        _make_sample_opportunity_dict(opp_id="OPP-006", status="staging", report_ref="", stakeholder_ref="")
    )
    staged_005 = vault_sandbox / "staging" / "opportunities" / "OPP-005-promote.md"
    staged_006 = vault_sandbox / "staging" / "opportunities" / "OPP-006-archive.md"

    upsert_markdown_card(
        staged_005,
        opp_005,
        render_opportunity_body(opp_005, cfo_critique="Audit passed with [VERIFY] caveats."),
        human_notes_override=notes_005,
    )
    upsert_markdown_card(
        staged_006,
        opp_006,
        render_opportunity_body(opp_006, cfo_critique="High external vendor dependency."),
        human_notes_override=notes_006,
    )

    promoted_path = promote_opportunity(vault_sandbox, "OPP-005", target_status="backlog")
    assert not staged_005.exists()
    assert promoted_path == vault_sandbox / "opportunities" / "OPP-005-promote.md"
    parsed_005 = parse_markdown_card(promoted_path)
    assert parsed_005.frontmatter["status"] == "backlog"
    assert "## Skeptical CFO Critique" in parsed_005.body
    assert "Audit passed with [VERIFY] caveats." in parsed_005.body
    assert parsed_005.human_notes == notes_005

    _, toggled_on = set_boss_mandate(vault_sandbox, "OPP-005")
    assert toggled_on.boss_mandate is True
    assert parse_markdown_card(promoted_path).human_notes == notes_005

    _, toggled_off = set_boss_mandate(vault_sandbox, "OPP-005", boss_mandate=False)
    assert toggled_off.boss_mandate is False
    assert parse_markdown_card(promoted_path).human_notes == notes_005

    archived_path = archive_opportunity(
        vault_sandbox, "OPP-006", reason="Blocked on vendor contract"
    )
    assert not staged_006.exists()
    assert archived_path == vault_sandbox / "opportunities" / "OPP-006-archive.md"
    parsed_006 = parse_markdown_card(archived_path)
    assert parsed_006.frontmatter["status"] == "archived"
    assert "## Archive Reason" in parsed_006.body
    assert "Blocked on vendor contract" in parsed_006.body
    assert parsed_006.human_notes == notes_006

    active_only = load_all_opportunities(vault_sandbox, include_archived=False)
    assert [item[1].id for item in active_only] == ["OPP-005"]

    with_archived = load_all_opportunities(vault_sandbox, include_archived=True)
    assert [item[1].id for item in with_archived] == ["OPP-005", "OPP-006"]


@pytest.mark.tier1
def test_f04_t1_update_question_or_phase_persists_markdown_and_preserves_human_notes(
    vault_sandbox: Path,
) -> None:
    """Verify update_question_or_phase updates frontmatter + checkboxes while preserving HUMAN-NOTES."""
    from poopdeck.vault_io import (
        initialize_vault,
        load_questions_and_playbook,
        parse_markdown_card,
        render_playbook_body,
        render_questions_body,
        update_question_or_phase,
        upsert_markdown_card,
    )

    initialize_vault(vault_sandbox, seed_questions=True, init_git=False)
    q_path = vault_sandbox / "questions" / "open-questions.md"
    pb_path = vault_sandbox / "questions" / "day1-90-playbook.md"

    bank_init, tracker_init = load_questions_and_playbook(vault_sandbox)
    q_notes = "\n### Finance 1:1 Context\n- Controller confirmed COGS basis\n"
    pb_notes = "\n### Onboarding Log\n- Laptop setup completed ahead of schedule\n"

    upsert_markdown_card(q_path, bank_init, render_questions_body(bank_init), human_notes_override=q_notes)
    upsert_markdown_card(pb_path, tracker_init, render_playbook_body(tracker_init), human_notes_override=pb_notes)

    update_question_or_phase(
        vault_sandbox,
        "Q-FIN-02",
        status="answered",
        answer_summary="Standard gross margin is 42%",
    )
    update_question_or_phase(vault_sandbox, "PHASE-1", completed=True)

    bank_after, tracker_after = load_questions_and_playbook(vault_sandbox)
    q_fin_02 = next(q for q in bank_after.questions if q.id == "Q-FIN-02")
    assert q_fin_02.status == "answered"
    assert q_fin_02.answer_summary == "Standard gross margin is 42%"

    phase_1 = next(p for p in tracker_after.phases if p.id == "PHASE-1")
    assert phase_1.completed is True

    q_parsed = parse_markdown_card(q_path)
    assert "- [x] **Q-FIN-02" in q_parsed.body
    assert "Standard gross margin is 42%" in q_parsed.body
    assert q_parsed.human_notes == q_notes

    pb_parsed = parse_markdown_card(pb_path)
    assert "## [x] PHASE-1" in pb_parsed.body
    assert pb_parsed.human_notes == pb_notes


@pytest.mark.tier1
def test_f04_t1_build_lineage_graph_resolves_six_node_types_and_wikilinks(
    vault_sandbox: Path,
) -> None:
    """Verify build_lineage_graph constructs all 6 node types and 8 directed edge relations."""
    from poopdeck.models import DatasetCard, OpportunitySpec, ReportCard, StakeholderCard
    from poopdeck.vault_io import (
        build_lineage_graph,
        initialize_vault,
        render_dataset_body,
        render_opportunity_body,
        render_report_body,
        render_stakeholder_body,
        upsert_markdown_card,
    )

    initialize_vault(vault_sandbox, seed_questions=True, init_git=False)

    ds_silver = DatasetCard(
        id="prod_catalog.silver.orders",
        fqn="prod_catalog.silver.orders",
        catalog="prod_catalog",
        schema_name="silver",
        table_name="orders",
        downstream_refs=["[[prod_catalog.gold.orders]]"],
    )
    ds_gold = DatasetCard(
        id="prod_catalog.gold.orders",
        fqn="prod_catalog.gold.orders",
        catalog="prod_catalog",
        schema_name="gold",
        table_name="orders",
        upstream_refs=["[[prod_catalog.silver.orders]]"],
    )
    upsert_markdown_card(
        vault_sandbox / "datasets" / "prod_catalog.silver.orders.md",
        ds_silver,
        render_dataset_body(ds_silver),
    )
    upsert_markdown_card(
        vault_sandbox / "datasets" / "prod_catalog.gold.orders.md",
        ds_gold,
        render_dataset_body(ds_gold),
    )

    sh_card = StakeholderCard(
        name="Sarah Chen",
        role="VP Supply Chain",
        owned_reports=["[[Supply Chain__Daily Buyer Workbench]]"],
    )
    upsert_markdown_card(
        vault_sandbox / "stakeholders" / "sarah-chen.md",
        sh_card,
        render_stakeholder_body(sh_card),
    )

    rep_card = ReportCard(
        title="Daily Buyer Workbench",
        workspace="Supply Chain",
        upstream_datasets=["[[prod_catalog.gold.orders]]"],
        owners=["[[Sarah Chen]]"],
    )
    upsert_markdown_card(
        vault_sandbox / "reports" / "Supply Chain__Daily Buyer Workbench.md",
        rep_card,
        render_report_body(rep_card),
    )

    opp = OpportunitySpec.model_validate(
        _make_sample_opportunity_dict(
            opp_id="OPP-001",
            status="active",
            dataset_ref="[[prod_catalog.gold.orders]]",
            report_ref="[[Supply Chain__Daily Buyer Workbench]]",
            stakeholder_ref="[[Sarah Chen|VP Supply Chain]]",
        )
    )
    upsert_markdown_card(
        vault_sandbox / "opportunities" / "OPP-001-safety-stock.md",
        opp,
        render_opportunity_body(opp),
    )

    graph = build_lineage_graph(vault_sandbox, include_staging=True)
    node_types = {n["type"] for n in graph["nodes"]}
    assert node_types == {
        "external_data",
        "dataset",
        "opportunity",
        "existing_report",
        "new_report",
        "stakeholder",
    }
    assert all(n["exists_in_vault"] is True for n in graph["nodes"])

    edge_relations = {e["relation"] for e in graph["edges"]}
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


@pytest.mark.tier2
def test_f04_t2_windows_backslash_and_regex_backref_tokens_survive_upsert_verbatim(
    vault_sandbox: Path,
) -> None:
    """Ensure Windows file paths and regex replacement tokens inside HUMAN-NOTES never trigger re.sub escapes."""
    from poopdeck.models import DatasetCard
    from poopdeck.vault_io import (
        parse_markdown_card,
        render_dataset_body,
        upsert_markdown_card,
    )

    tricky_notes = (
        r"""
Local export path: C:\Users\ds\notes\table_1\01_raw\extract.csv
Regex tokens that break naive re.sub: \1 \2 \g<0> \g<1> $1 ${1} \u0041 \x41 \n_literal
"""
    )
    card_path = vault_sandbox / "datasets" / "prod_catalog.silver.shipments.md"
    card = DatasetCard(
        id="prod_catalog.silver.shipments",
        fqn="prod_catalog.silver.shipments",
        catalog="prod_catalog",
        schema_name="silver",
        table_name="shipments",
        row_count=500,
    )
    upsert_markdown_card(
        card_path,
        card,
        render_dataset_body(card),
        human_notes_override=tricky_notes,
    )

    # Re-upsert twice without override to exercise extraction + writeback path
    for next_count in (1_500, 2_750):
        updated = card.model_copy(update={"row_count": next_count})
        upsert_markdown_card(card_path, updated, render_dataset_body(updated))

    parsed = parse_markdown_card(card_path)
    assert parsed.frontmatter["row_count"] == 2_750
    assert parsed.human_notes == tricky_notes


@pytest.mark.tier2
def test_f04_t2_utf8_bom_and_crlf_input_normalized_to_strict_utf8_lf_preserving_notes(
    vault_sandbox: Path,
) -> None:
    """Verify cards saved with UTF-8 BOM and CRLF normalize cleanly to BOM-free LF while keeping notes."""
    from poopdeck.models import StakeholderCard
    from poopdeck.vault_io import (
        parse_markdown_card,
        render_stakeholder_body,
        upsert_markdown_card,
    )

    card_path = vault_sandbox / "stakeholders" / "marcus-vance.md"
    card_path.parent.mkdir(parents=True, exist_ok=True)
    raw_bom_crlf = (
        b"\xef\xbb\xbf---\r\n"
        b"id: marcus-vance\r\n"
        b"name: Marcus Vance\r\n"
        b"role: VP Operations\r\n"
        b"---\r\n\r\n"
        b"# Stakeholder: Marcus Vance\r\n\r\n"
        b"<!-- HUMAN-NOTES-START -->\r\n"
        b"Note line 1\r\n"
        b"Note line 2\r\n"
        b"<!-- HUMAN-NOTES-END -->\r\n"
    )
    card_path.write_bytes(raw_bom_crlf)

    pre_parsed = parse_markdown_card(card_path)
    assert pre_parsed.frontmatter["name"] == "Marcus Vance"
    assert pre_parsed.human_notes == "\nNote line 1\nNote line 2\n"

    updated = StakeholderCard(
        id="marcus-vance",
        name="Marcus Vance",
        role="SVP Operations",
        influence_tier="exec",
    )
    upsert_markdown_card(card_path, updated, render_stakeholder_body(updated))

    out_bytes = card_path.read_bytes()
    assert not out_bytes.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in out_bytes

    post_parsed = parse_markdown_card(card_path)
    assert post_parsed.frontmatter["role"] == "SVP Operations"
    assert post_parsed.human_notes == "\nNote line 1\nNote line 2\n"


@pytest.mark.tier2
def test_f04_t2_missing_human_notes_markers_recovery_on_hand_authored_markdown(
    vault_sandbox: Path,
) -> None:
    """Verify hand-authored Markdown lacking HUMAN-NOTES markers rescues custom prose or seeds default markers."""
    from poopdeck.models import DatasetCard
    from poopdeck.vault_io import (
        DEFAULT_HUMAN_NOTES,
        HUMAN_NOTES_END,
        HUMAN_NOTES_START,
        parse_markdown_card,
        upsert_markdown_card,
    )

    # Subcase A: Hand-authored file with custom prose and zero HUMAN-NOTES markers
    path_a = vault_sandbox / "datasets" / "prod_catalog.gold.hand_authored.md"
    path_a.parent.mkdir(parents=True, exist_ok=True)
    path_a.write_text(
        "---\nfqn: prod_catalog.gold.hand_authored\n---\n\nHand-typed 1:1 notes before markers existed.\n",
        encoding="utf-8",
    )
    card_a = DatasetCard(
        id="prod_catalog.gold.hand_authored",
        fqn="prod_catalog.gold.hand_authored",
        catalog="prod_catalog",
        schema_name="gold",
        table_name="hand_authored",
        row_count=42,
    )
    upsert_markdown_card(path_a, card_a, "# Synced Body")
    raw_a = path_a.read_text(encoding="utf-8")
    assert raw_a.count(HUMAN_NOTES_START) == 1 and raw_a.count(HUMAN_NOTES_END) == 1
    parsed_a = parse_markdown_card(path_a)
    assert parsed_a.body == "# Synced Body"
    assert parsed_a.human_notes is not None
    assert "Hand-typed 1:1 notes before markers existed." in parsed_a.human_notes

    # Subcase B: Hand-authored file with frontmatter only and empty body
    path_b = vault_sandbox / "datasets" / "prod_catalog.gold.empty_prose.md"
    path_b.write_text("---\nfqn: prod_catalog.gold.empty_prose\n---\n", encoding="utf-8")
    card_b = DatasetCard(
        id="prod_catalog.gold.empty_prose",
        fqn="prod_catalog.gold.empty_prose",
        catalog="prod_catalog",
        schema_name="gold",
        table_name="empty_prose",
        row_count=99,
    )
    upsert_markdown_card(path_b, card_b, "# Synced Body")
    parsed_b = parse_markdown_card(path_b)
    assert parsed_b.body == "# Synced Body"
    assert parsed_b.human_notes == DEFAULT_HUMAN_NOTES


@pytest.mark.tier2
def test_f04_t2_validate_vault_flags_malformed_yaml_invalid_enums_and_unbalanced_markers(
    vault_sandbox: Path,
) -> None:
    """Verify validate_vault reports syntax-broken YAML, invalid Pydantic enums, and unbalanced markers."""
    from poopdeck.vault_io import HUMAN_NOTES_START, initialize_vault, validate_vault

    initialize_vault(vault_sandbox, seed_questions=True, init_git=False)

    (vault_sandbox / "datasets" / "bad_yaml.md").write_text(
        "---\nfqn: [unclosed_yaml_list\n---\n\n# Body\n",
        encoding="utf-8",
    )
    (vault_sandbox / "stakeholders" / "bad_enum.md").write_text(
        "---\nname: Bad Actor\ninfluence_tier: galactic_emperor\n---\n\n"
        "<!-- HUMAN-NOTES-START -->\nok\n<!-- HUMAN-NOTES-END -->\n",
        encoding="utf-8",
    )
    (vault_sandbox / "datasets" / "unbalanced.md").write_text(
        "---\n"
        "id: prod_catalog.gold.unbalanced\n"
        "fqn: prod_catalog.gold.unbalanced\n"
        "catalog: prod_catalog\n"
        "schema_name: gold\n"
        "table_name: unbalanced\n"
        "---\n\n"
        f"# Body\n\n{HUMAN_NOTES_START}\nMissing closing marker!\n",
        encoding="utf-8",
    )

    report = validate_vault(vault_sandbox, strict=False)
    assert report.valid is False
    assert len(report.errors) >= 3
    joined_errors = "\n".join(report.errors)
    assert "datasets/bad_yaml.md" in joined_errors
    assert "stakeholders/bad_enum.md" in joined_errors
    assert "datasets/unbalanced.md" in joined_errors


@pytest.mark.tier2
def test_f04_t2_validate_vault_enforces_staging_status_invariants_duplicate_ids_and_strict_wikilinks(
    vault_sandbox: Path,
) -> None:
    """Verify dangling wikilink warnings vs strict errors, duplicate opportunity IDs, and staging folder status rules."""
    from poopdeck.models import OpportunitySpec
    from poopdeck.vault_io import (
        initialize_vault,
        render_opportunity_body,
        upsert_markdown_card,
        validate_vault,
    )

    initialize_vault(vault_sandbox, seed_questions=True, init_git=False)

    opp_dangling = OpportunitySpec.model_validate(
        _make_sample_opportunity_dict(
            opp_id="OPP-001",
            status="backlog",
            dataset_ref="[[missing_catalog.gold.nonexistent_table]]",
            report_ref="",
            stakeholder_ref="",
        )
    )
    upsert_markdown_card(
        vault_sandbox / "opportunities" / "OPP-001-dangling.md",
        opp_dangling,
        render_opportunity_body(opp_dangling),
    )

    non_strict = validate_vault(vault_sandbox, strict=False)
    assert non_strict.valid is True
    assert non_strict.errors == []
    assert any("missing_catalog.gold.nonexistent_table" in w for w in non_strict.warnings)

    strict_rep = validate_vault(vault_sandbox, strict=True)
    assert strict_rep.valid is False
    assert any("missing_catalog.gold.nonexistent_table" in e for e in strict_rep.errors)

    # Duplicate ID OPP-001 + wrong status ('backlog' inside staging/opportunities/)
    opp_bad_staged = OpportunitySpec.model_validate(
        _make_sample_opportunity_dict(
            opp_id="OPP-001",
            status="backlog",
            dataset_ref="",
            report_ref="",
            stakeholder_ref="",
        )
    )
    upsert_markdown_card(
        vault_sandbox / "staging" / "opportunities" / "OPP-001-dup.md",
        opp_bad_staged,
        render_opportunity_body(opp_bad_staged),
    )

    dup_rep = validate_vault(vault_sandbox, strict=False)
    assert dup_rep.valid is False
    joined = "\n".join(dup_rep.errors)
    assert "Duplicate opportunity id" in joined
    assert "status='staging'" in joined


@pytest.mark.tier2
def test_f04_t2_build_lineage_graph_dangling_wikilink_creates_placeholder_without_crashing(
    vault_sandbox: Path,
) -> None:
    """Verify unresolved [[wikilink]] references create exists_in_vault=False placeholder nodes cleanly."""
    from poopdeck.models import OpportunitySpec
    from poopdeck.vault_io import (
        build_lineage_graph,
        initialize_vault,
        render_opportunity_body,
        upsert_markdown_card,
    )

    initialize_vault(vault_sandbox, seed_questions=True, init_git=False)

    opp = OpportunitySpec.model_validate(
        _make_sample_opportunity_dict(
            opp_id="OPP-009",
            status="backlog",
            dataset_ref="[[prod_catalog.gold.future_table]]",
            report_ref="[[Future Workspace__Planned Dashboard]]",
            stakeholder_ref="",
        )
    )
    upsert_markdown_card(
        vault_sandbox / "opportunities" / "OPP-009-future.md",
        opp,
        render_opportunity_body(opp),
    )

    graph = build_lineage_graph(vault_sandbox)
    nodes_by_id = {n["id"]: n for n in graph["nodes"]}

    ds_placeholder = nodes_by_id["dataset:prod_catalog.gold.future_table"]
    assert ds_placeholder["type"] == "dataset"
    assert ds_placeholder["exists_in_vault"] is False
    assert ds_placeholder["metadata"]["unresolved_ref"] == "prod_catalog.gold.future_table"

    rep_placeholder = nodes_by_id["existing_report:Future Workspace__Planned Dashboard"]
    assert rep_placeholder["type"] == "existing_report"
    assert rep_placeholder["exists_in_vault"] is False
    assert rep_placeholder["metadata"]["unresolved_ref"] == "Future Workspace__Planned Dashboard"

    edge_tuples = {(e["source"], e["target"], e["relation"]) for e in graph["edges"]}
    assert (
        "dataset:prod_catalog.gold.future_table",
        "opportunity:OPP-009",
        "inputs_to_opportunity",
    ) in edge_tuples
    assert (
        "opportunity:OPP-009",
        "existing_report:Future Workspace__Planned Dashboard",
        "supplements_report",
    ) in edge_tuples


@pytest.mark.tier2
def test_f04_t2_unknown_id_and_invalid_target_status_raise_exact_exceptions(
    vault_sandbox: Path,
) -> None:
    """Verify vault mutation helpers raise KeyError on unknown IDs and ValueError on invalid promote status."""
    from poopdeck.models import OpportunitySpec
    from poopdeck.vault_io import (
        archive_opportunity,
        initialize_vault,
        promote_opportunity,
        render_opportunity_body,
        set_boss_mandate,
        update_question_or_phase,
        upsert_markdown_card,
    )

    initialize_vault(vault_sandbox, seed_questions=True, init_git=False)

    opp_005 = OpportunitySpec.model_validate(
        _make_sample_opportunity_dict(
            opp_id="OPP-005",
            status="staging",
            dataset_ref="",
            report_ref="",
            stakeholder_ref="",
        )
    )
    upsert_markdown_card(
        vault_sandbox / "staging" / "opportunities" / "OPP-005-staged.md",
        opp_005,
        render_opportunity_body(opp_005),
    )

    with pytest.raises(KeyError):
        promote_opportunity(vault_sandbox, "OPP-DOES-NOT-EXIST")

    with pytest.raises(KeyError):
        archive_opportunity(vault_sandbox, "OPP-DOES-NOT-EXIST")

    with pytest.raises(KeyError):
        set_boss_mandate(vault_sandbox, "OPP-DOES-NOT-EXIST", True)

    with pytest.raises(KeyError):
        update_question_or_phase(vault_sandbox, "Q-NOPE-99", status="answered")

    with pytest.raises(ValueError, match="staging"):
        promote_opportunity(vault_sandbox, "OPP-005", target_status="staging")
