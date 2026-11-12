"""Requirement acceptance tests for F05: Driver-Tree ROI Valuation, Domain Packs & Boss-Mandate Cutlines.

Covers Tier 1 happy-path valuation, priority scoring, external data pack loading, and portfolio
capacity scheduling, plus Tier 2 anti-hallucination guards, safe AST sandboxing, CFO confidence
gates, over-capacity mandate overflow, and strict numeric/boolean validation boundaries.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _build_opp_for_schedule(
    opp_id: str,
    status: str = "backlog",
    effort_weeks: float = 4.0,
    priority_score: float | None = 75.0,
    boss_mandate: bool = False,
    base_usd: float = 250_000.0,
    confidence_tier: str = "directional",
    verified: bool = True,
    supplement_report: bool = False,
    readiness: int = 4,
    friction: int = 2,
) -> object:
    """Build an OpportunitySpec instance at runtime for scoring and portfolio tests."""
    from poopdeck.models import (
        DriverSpec,
        OpportunitySpec,
        ReportSupplement,
        ValuationSpec,
    )

    supplements = (
        [
            ReportSupplement(
                ref="[[Supply Chain__Daily Buyer Workbench]]",
                integration_mode="Add reorder risk tier column",
            )
        ]
        if supplement_report
        else []
    )
    return OpportunitySpec(
        id=opp_id,
        title=f"Opportunity {opp_id}",
        status=status,
        boss_mandate=boss_mandate,
        domain_pack="ops_supply_chain",
        existing_reports_supplemented=supplements,
        valuation=ValuationSpec(
            confidence_tier=confidence_tier,
            driver_formula="unit_benefit * volume",
            drivers=[
                DriverSpec(
                    name="unit_benefit",
                    value=base_usd / 1_000.0,
                    source="ERP baseline",
                    verified=verified,
                ),
                DriverSpec(
                    name="volume",
                    value=1_000.0,
                    source="Order table count",
                    verified=verified,
                ),
            ],
        ),
        effort_weeks=effort_weeks,
        data_readiness_score=readiness,
        adoption_friction_score=friction,
        computed_priority_score=priority_score,
    )


@pytest.mark.tier1
def test_f05_t1_safe_ast_driver_formula_evaluates_scalar_and_ranged_drivers() -> None:
    """Verify safe AST evaluation across arithmetic operators, unary signs, and subtracted ranges."""
    from poopdeck.models import DriverSpec, ValuationSpec, evaluate_driver_formula

    drivers = [
        DriverSpec(name="vol", value=1_000.0, verified=True),
        DriverSpec(name="rate", range=(0.10, 0.20, 0.30), verified=False),
        DriverSpec(name="margin", value=50.0, verified=True),
        DriverSpec(name="fixed_cost", value=2_000.0, verified=True),
    ]
    formula = "(+vol * rate * (margin + 10) / 2) + (-fixed_cost)"
    assert evaluate_driver_formula(formula, drivers) == (1_000.0, 4_000.0, 7_000.0)

    val = ValuationSpec(
        confidence_tier="directional",
        driver_formula=formula,
        drivers=drivers,
    )
    assert (
        val.annual_benefit_low_usd,
        val.annual_benefit_base_usd,
        val.annual_benefit_high_usd,
    ) == (1_000.0, 4_000.0, 7_000.0)
    assert val.compute_from_drivers() == (1_000.0, 4_000.0, 7_000.0)

    # Subtracted ranged driver orders (low <= base <= high) via min/max
    sub_val = ValuationSpec(
        confidence_tier="directional",
        driver_formula="gross_benefit - variable_cost",
        drivers=[
            DriverSpec(name="gross_benefit", value=100_000.0, verified=True),
            DriverSpec(
                name="variable_cost",
                range=(10_000.0, 20_000.0, 35_000.0),
                verified=False,
            ),
        ],
    )
    assert (
        sub_val.annual_benefit_low_usd,
        sub_val.annual_benefit_base_usd,
        sub_val.annual_benefit_high_usd,
    ) == (65_000.0, 80_000.0, 90_000.0)


@pytest.mark.tier1
def test_f05_t1_unverified_drivers_emit_verify_prompts_and_verified_drivers_allow_finance_verified() -> None:
    """Verify [VERIFY WITH STAKEHOLDER] prompt generation without duplicate prefixing, plus finance_verified unlock."""
    from poopdeck.models import DriverSpec, ValuationSpec

    val_unverified = ValuationSpec(
        confidence_tier="directional",
        driver_formula="annual_orders * late_rate * expedite_cost",
        drivers=[
            DriverSpec(
                name="annual_orders",
                value=50_000.0,
                source="prod_catalog.gold.orders",
                verified=True,
            ),
            DriverSpec(
                name="late_rate",
                range=(0.04, 0.06, 0.08),
                source="Ops estimate",
                verified=False,
            ),
            DriverSpec(
                name="expedite_cost",
                value=150.0,
                source="Freight invoice sample",
                verified=False,
                verify_question="[VERIFY WITH STAKEHOLDER] Confirm air-freight surcharge with Logistics Finance",
            ),
        ],
    )
    assert val_unverified.unverified_drivers_count == 2
    assert len(val_unverified.verification_prompts) == 2
    for prompt in val_unverified.verification_prompts:
        assert prompt.startswith("[VERIFY WITH STAKEHOLDER] ")
        assert prompt.count("[VERIFY WITH STAKEHOLDER]") == 1
    assert any("late_rate" in p for p in val_unverified.verification_prompts)
    assert any(
        "expedite_cost: Confirm air-freight surcharge with Logistics Finance" in p
        for p in val_unverified.verification_prompts
    )

    val_verified = ValuationSpec(
        confidence_tier="finance_verified",
        driver_formula="annual_orders * late_rate * expedite_cost",
        drivers=[
            DriverSpec(name="annual_orders", value=50_000.0, verified=True),
            DriverSpec(name="late_rate", range=(0.04, 0.06, 0.08), verified=True),
            DriverSpec(name="expedite_cost", value=150.0, verified=True),
        ],
    )
    assert val_verified.unverified_drivers_count == 0
    assert val_verified.verification_prompts == []
    assert val_verified.annual_benefit_base_usd == 450_000.0


@pytest.mark.tier1
def test_f05_t1_compute_priority_score_weights_and_existing_pbi_bonus() -> None:
    """Verify exact weighted composite priority score and +10pt adoption bonus for supplementing existing PBI reports."""
    from poopdeck.config import ScoringWeightsConfig
    from poopdeck.models import compute_priority_score

    opp_standalone = _build_opp_for_schedule(
        opp_id="OPP-STANDALONE",
        effort_weeks=4.0,
        priority_score=None,
        base_usd=500_000.0,
        confidence_tier="finance_verified",
        verified=True,
        supplement_report=False,
        readiness=5,
        friction=1,
    )
    opp_supplement = _build_opp_for_schedule(
        opp_id="OPP-SUPPLEMENT",
        effort_weeks=4.0,
        priority_score=None,
        base_usd=500_000.0,
        confidence_tier="finance_verified",
        verified=True,
        supplement_report=True,
        readiness=5,
        friction=1,
    )

    weights = ScoringWeightsConfig()
    score_standalone = compute_priority_score(opp_standalone, weights)
    score_supplement = compute_priority_score(opp_supplement, weights)

    # norm_roi=50, norm_readiness=100, norm_adoption=90 vs 100, norm_speed=50, mult=1.00
    assert score_standalone == 70.5
    assert score_supplement == 72.5
    assert opp_standalone.computed_priority_score == 70.5
    assert opp_supplement.computed_priority_score == 72.5
    assert round(score_supplement - score_standalone, 2) == 2.0


@pytest.mark.tier1
def test_f05_t1_confidence_tier_multipliers_scale_priority_score_monotonically() -> None:
    """Verify hypothesis (0.60), directional (0.85), and finance_verified (1.00) multipliers scale monotonically."""
    from poopdeck.models import compute_priority_score

    opp_hyp = _build_opp_for_schedule(
        opp_id="OPP-HYP",
        effort_weeks=4.0,
        priority_score=None,
        base_usd=500_000.0,
        confidence_tier="hypothesis",
        verified=True,
        supplement_report=True,
        readiness=5,
        friction=1,
    )
    opp_dir = _build_opp_for_schedule(
        opp_id="OPP-DIR",
        effort_weeks=4.0,
        priority_score=None,
        base_usd=500_000.0,
        confidence_tier="directional",
        verified=True,
        supplement_report=True,
        readiness=5,
        friction=1,
    )
    opp_fin = _build_opp_for_schedule(
        opp_id="OPP-FIN",
        effort_weeks=4.0,
        priority_score=None,
        base_usd=500_000.0,
        confidence_tier="finance_verified",
        verified=True,
        supplement_report=True,
        readiness=5,
        friction=1,
    )

    s_hyp = compute_priority_score(opp_hyp)
    s_dir = compute_priority_score(opp_dir)
    s_fin = compute_priority_score(opp_fin)

    assert s_hyp < s_dir < s_fin
    assert s_hyp == 43.5
    assert s_dir == 61.62
    assert s_fin == 72.5


@pytest.mark.tier1
def test_f05_t1_external_data_catalog_yaml_lenses_public_packs_and_vendor_packs(
    project_root: Path,
) -> None:
    """Verify external_data_catalog.yaml defines 6 domain lenses, archetypes, free public APIs, and vendor packs."""
    import yaml

    from poopdeck.packs import (
        get_domain_lens,
        list_external_datasets,
        load_external_data_catalog,
    )

    catalog_file = project_root / "poopdeck" / "packs" / "external_data_catalog.yaml"
    assert catalog_file.is_file()
    raw_yaml = yaml.safe_load(catalog_file.read_text(encoding="utf-8-sig"))
    assert isinstance(raw_yaml, dict)

    catalog = load_external_data_catalog(catalog_file)
    expected_domains = {
        "generic",
        "ops_supply_chain",
        "retail_cpg",
        "finance_risk",
        "b2b_saas",
        "manufacturing",
    }
    assert expected_domains.issubset(set(catalog.domain_lenses.keys()))
    assert len(catalog.archetypes) >= 5
    assert len(catalog.external_datasets) >= 8

    all_sources_blob = " ".join(
        f"{d.id} {d.source} {d.provider}" for d in catalog.external_datasets
    ).lower()
    for required_public in ("fred", "census", "noaa", "holiday"):
        assert required_public in all_sources_blob, f"Missing external source: {required_public}"

    acquisitions = {d.acquisition for d in catalog.external_datasets}
    assert "free_public_api" in acquisitions
    assert bool(acquisitions & {"existing_vendor", "net_new_budget"})

    free_ops = list_external_datasets("ops_supply_chain", free_only=True, catalog=catalog)
    assert len(free_ops) >= 4
    assert all(entry.acquisition == "free_public_api" for entry in free_ops)

    fallback_lens = get_domain_lens("unrecognized_industry_vertical", catalog=catalog)
    assert fallback_lens.slug == "generic"


@pytest.mark.tier1
def test_f05_t1_compute_portfolio_schedule_boss_mandate_pins_p0_and_marks_displaced_cutline() -> None:
    """Verify boss_mandate=True pins P0 rank 1 and accurately identifies displaced baseline-funded items."""
    from poopdeck.models import compute_portfolio_schedule

    opp_1 = _build_opp_for_schedule("OPP-001", status="active", effort_weeks=4.0, priority_score=90.0)
    opp_2 = _build_opp_for_schedule("OPP-002", status="backlog", effort_weeks=4.0, priority_score=82.0)
    opp_3 = _build_opp_for_schedule("OPP-003", status="backlog", effort_weeks=4.0, priority_score=75.0)
    opp_4 = _build_opp_for_schedule(
        "OPP-004",
        status="backlog",
        effort_weeks=5.0,
        priority_score=35.0,
        boss_mandate=True,
    )

    sched = compute_portfolio_schedule(
        [opp_1, opp_2, opp_3, opp_4],
        quarterly_capacity_weeks=12.0,
    )
    assert sched.items[0].id == "OPP-004"
    assert sched.items[0].rank == 1
    assert sched.items[0].above_cutline is True
    assert sched.items[0].baseline_above_cutline is False
    assert sched.items[0].displaced_by_mandate is False

    assert sched.funded_ids == ["OPP-004", "OPP-001"]
    assert sched.displaced_ids == ["OPP-002", "OPP-003"]

    by_id = {item.id: item for item in sched.items}
    for disp_id in ("OPP-002", "OPP-003"):
        assert by_id[disp_id].baseline_above_cutline is True
        assert by_id[disp_id].above_cutline is False
        assert by_id[disp_id].displaced_by_mandate is True

    assert sched.used_capacity_weeks == 9.0
    assert sched.mandate_weeks == 5.0
    assert sched.remaining_capacity_weeks == 3.0
    assert sched.overflow_weeks == 0.0
    assert sched.over_capacity is False


@pytest.mark.tier1
def test_f05_t1_compute_portfolio_schedule_filters_to_backlog_and_active_only() -> None:
    """Verify staging, blocked, shipped, and archived items are excluded from quarterly capacity allocation."""
    from poopdeck.models import compute_portfolio_schedule

    items = [
        _build_opp_for_schedule("OPP-STG", status="staging", effort_weeks=3.0, priority_score=99.0, boss_mandate=True),
        _build_opp_for_schedule("OPP-ACT", status="active", effort_weeks=4.0, priority_score=85.0),
        _build_opp_for_schedule("OPP-BKL", status="backlog", effort_weeks=5.0, priority_score=78.0),
        _build_opp_for_schedule("OPP-BLK", status="blocked", effort_weeks=2.0, priority_score=95.0, boss_mandate=True),
        _build_opp_for_schedule("OPP-SHP", status="shipped", effort_weeks=3.0, priority_score=92.0),
        _build_opp_for_schedule("OPP-ARC", status="archived", effort_weeks=4.0, priority_score=91.0, boss_mandate=True),
    ]

    sched = compute_portfolio_schedule(items, quarterly_capacity_weeks=12.0)
    assert [i.id for i in sched.items] == ["OPP-ACT", "OPP-BKL"]
    assert sched.funded_ids == ["OPP-ACT", "OPP-BKL"]
    assert sched.used_capacity_weeks == 9.0
    assert sched.mandate_weeks == 0.0


@pytest.mark.tier2
def test_f05_t2_scalar_hallucination_guard_rejects_gt_5pct_divergence_from_driver_formula() -> None:
    """Verify scalar benefit overrides within <=5% pass while >5% divergence or inverted bounds fail validation."""
    from pydantic import ValidationError

    from poopdeck.models import DriverSpec, ValuationSpec

    drivers = [
        DriverSpec(name="vol", value=1_000.0, verified=True),
        DriverSpec(name="rate", range=(0.10, 0.20, 0.30), verified=False),
    ]

    # Exact formula outputs (100.0, 200.0, 300.0); +4% on base (208.0) is within 5% tolerance
    within_tol = ValuationSpec(
        confidence_tier="directional",
        driver_formula="vol * rate",
        drivers=drivers,
        annual_benefit_low_usd=100.0,
        annual_benefit_base_usd=208.0,
        annual_benefit_high_usd=300.0,
    )
    assert within_tol.annual_benefit_base_usd == 208.0

    # +25% base divergence -> rejected
    with pytest.raises(ValidationError):
        ValuationSpec(
            confidence_tier="directional",
            driver_formula="vol * rate",
            drivers=drivers,
            annual_benefit_base_usd=250.0,
        )

    # -20% low divergence -> rejected
    with pytest.raises(ValidationError):
        ValuationSpec(
            confidence_tier="directional",
            driver_formula="vol * rate",
            drivers=drivers,
            annual_benefit_low_usd=80.0,
        )

    # +33.3% high divergence -> rejected
    with pytest.raises(ValidationError):
        ValuationSpec(
            confidence_tier="directional",
            driver_formula="vol * rate",
            drivers=drivers,
            annual_benefit_high_usd=400.0,
        )

    # Inverted bounds (low > base when formula yields identical low=base=high=200.0)
    scalar_drivers = [
        DriverSpec(name="vol", value=1_000.0, verified=True),
        DriverSpec(name="rate", value=0.20, verified=True),
    ]
    with pytest.raises(ValidationError):
        ValuationSpec(
            confidence_tier="directional",
            driver_formula="vol * rate",
            drivers=scalar_drivers,
            annual_benefit_low_usd=205.0,
            annual_benefit_base_usd=195.0,
            annual_benefit_high_usd=205.0,
        )


@pytest.mark.tier2
def test_f05_t2_unsafe_ast_constructs_rejected_without_execution() -> None:
    """Verify function calls, attribute access, comprehensions, unsupported operators, and constant-only formulas fail."""
    from pydantic import ValidationError

    from poopdeck.models import DriverSpec, ValuationSpec, evaluate_driver_formula

    drivers = [DriverSpec(name="vol", value=1_000.0, verified=True)]
    disallowed_formulas = (
        "__import__('os').system('echo blocked')",
        "(1).__class__",
        "vol.__class__",
        "abs(vol)",
        "eval('1')",
        "[x for x in [1, 2]]",
        "{'a': vol}",
        "vol ** 2",
        "vol // 2",
        "vol ^ 2",
        "250000",
    )
    for bad_expr in disallowed_formulas:
        with pytest.raises(ValueError):
            evaluate_driver_formula(bad_expr, drivers)
        with pytest.raises(ValidationError):
            ValuationSpec(
                confidence_tier="hypothesis",
                driver_formula=bad_expr,
                drivers=drivers,
            )


@pytest.mark.tier2
def test_f05_t2_undeclared_driver_syntax_error_and_division_by_zero_rejected() -> None:
    """Verify undeclared variables, duplicate drivers, syntax errors, and division by zero raise ValidationError."""
    from pydantic import ValidationError

    from poopdeck.models import DriverSpec, ValuationSpec

    base_drv = [DriverSpec(name="vol", value=100.0, verified=True)]

    # Undeclared variable
    with pytest.raises(ValidationError):
        ValuationSpec(
            confidence_tier="hypothesis",
            driver_formula="vol * ghost_driver",
            drivers=base_drv,
        )

    # Duplicate driver names
    with pytest.raises(ValidationError):
        ValuationSpec(
            confidence_tier="hypothesis",
            driver_formula="vol * 2",
            drivers=[
                DriverSpec(name="vol", value=10.0),
                DriverSpec(name="vol", value=20.0),
            ],
        )

    # Syntax errors and blank formula
    for broken in ("vol * * 2", "   "):
        with pytest.raises(ValidationError):
            ValuationSpec(
                confidence_tier="hypothesis",
                driver_formula=broken,
                drivers=base_drv,
            )

    # Literal division by zero and ranged zero divisor
    with pytest.raises(ValidationError):
        ValuationSpec(
            confidence_tier="hypothesis",
            driver_formula="vol / 0",
            drivers=base_drv,
        )

    with pytest.raises(ValidationError):
        ValuationSpec(
            confidence_tier="hypothesis",
            driver_formula="vol / zero_drv",
            drivers=[
                DriverSpec(name="vol", value=100.0),
                DriverSpec(name="zero_drv", range=(0.0, 0.1, 0.2)),
            ],
        )


@pytest.mark.tier2
def test_f05_t2_premature_finance_verified_with_unverified_driver_rejected() -> None:
    """Verify confidence_tier='finance_verified' raises ValidationError if any driver has verified=False."""
    from pydantic import ValidationError

    from poopdeck.models import DriverSpec, ValuationSpec

    with pytest.raises(ValidationError, match="finance_verified"):
        ValuationSpec(
            confidence_tier="finance_verified",
            driver_formula="a * b",
            drivers=[
                DriverSpec(name="a", value=100.0, verified=True),
                DriverSpec(name="b", value=2.0, verified=False),
            ],
        )


@pytest.mark.tier2
def test_f05_t2_over_capacity_boss_mandates_flag_overflow_and_zero_remaining_capacity() -> None:
    """Verify boss mandates exceeding quarterly capacity set over_capacity=True, overflow_weeks, and displace non-mandates."""
    from poopdeck.models import compute_portfolio_schedule

    mandate_a = _build_opp_for_schedule(
        "OPP-MANDATE-A",
        status="active",
        effort_weeks=8.0,
        priority_score=40.0,
        boss_mandate=True,
    )
    mandate_b = _build_opp_for_schedule(
        "OPP-MANDATE-B",
        status="backlog",
        effort_weeks=6.0,
        priority_score=30.0,
        boss_mandate=True,
    )
    opp_high = _build_opp_for_schedule(
        "OPP-HIGH",
        status="backlog",
        effort_weeks=4.0,
        priority_score=95.0,
        boss_mandate=False,
    )

    sched = compute_portfolio_schedule(
        [opp_high, mandate_a, mandate_b],
        quarterly_capacity_weeks=12.0,
    )
    assert sched.over_capacity is True
    assert sched.mandate_weeks == 14.0
    assert sched.used_capacity_weeks == 14.0
    assert sched.remaining_capacity_weeks == 0.0
    assert sched.overflow_weeks == 2.0
    assert sched.funded_ids == ["OPP-MANDATE-A", "OPP-MANDATE-B"]
    assert sched.displaced_ids == ["OPP-HIGH"]


@pytest.mark.tier2
def test_f05_t2_opportunity_spec_boundary_scores_and_non_positive_effort_rejected() -> None:
    """Verify OpportunitySpec rejects non-positive effort_weeks and out-of-range readiness/friction/priority scores."""
    from pydantic import ValidationError

    for bad_effort in (0.0, -2.5):
        with pytest.raises(ValidationError):
            _build_opp_for_schedule("OPP-BAD", effort_weeks=bad_effort)

    for bad_readiness in (0, 6):
        with pytest.raises(ValidationError):
            _build_opp_for_schedule("OPP-BAD", readiness=bad_readiness)

    for bad_friction in (0, 6):
        with pytest.raises(ValidationError):
            _build_opp_for_schedule("OPP-BAD", friction=bad_friction)

    for bad_priority in (-1.0, 105.0):
        with pytest.raises(ValidationError):
            _build_opp_for_schedule("OPP-BAD", priority_score=bad_priority)


@pytest.mark.tier2
def test_f05_t2_non_finite_booleans_and_invalid_driver_specs_rejected() -> None:
    """Verify DriverSpec, ScoringWeightsConfig, and compute_portfolio_schedule reject NaN/Inf/bool and invalid bounds."""
    from pydantic import ValidationError

    from poopdeck.config import ScoringWeightsConfig
    from poopdeck.models import DriverSpec, compute_portfolio_schedule

    # Missing both value and range
    with pytest.raises(ValidationError):
        DriverSpec(name="vol")

    # Inverted range (low > base > high)
    with pytest.raises(ValidationError):
        DriverSpec(name="rate", range=(0.30, 0.20, 0.10))

    # Python reserved keyword as driver name
    with pytest.raises(ValidationError):
        DriverSpec(name="for", value=10.0)

    # Non-finite or boolean numeric values
    for bad_num in (float("nan"), float("inf"), float("-inf"), True):
        with pytest.raises(ValidationError):
            DriverSpec(name="vol", value=bad_num)  # type: ignore[arg-type]

    # ScoringWeightsConfig sum != 1.0
    with pytest.raises(ValidationError):
        ScoringWeightsConfig(roi_weight=0.90)

    # Portfolio capacity negative, NaN, or bool
    for bad_cap in (-1.0, float("nan"), True):
        with pytest.raises(ValueError):
            compute_portfolio_schedule([], quarterly_capacity_weeks=bad_cap)  # type: ignore[arg-type]
