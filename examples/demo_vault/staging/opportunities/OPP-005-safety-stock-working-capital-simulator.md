---
id: OPP-005
title: Multi-Echelon Safety-Stock Working-Capital Rebalancer
status: staging
boss_mandate: false
domain_pack: ops_supply_chain
created_at: &id001 2026-11-11
updated_at: *id001
internal_datasets:
- ref: '[[prod_catalog.silver.inventory_snapshots]]'
  role: daily SKU-DC on-hand balances, unit costs, and >90 DOS excess layers
  transformations_needed:
  - Stratify slow-moving >90 DOS inventory by supplier contractual MOQ
- ref: '[[prod_catalog.gold.po_receipts]]'
  role: historical PO order quantities and empirical supplier lead-time variance
  transformations_needed:
  - Infer empirical minimum order quantity floors per supplier SKU
external_data_required:
- source: FRED Macroeconomic Series (CPI, Industrial Production, Interest Rates, Freight
    TSI)
  purpose: Benchmark short-term commercial paper / treasury interest cost and industrial
    demand cycle
  acquisition: free_public_api
  blocker_severity: low
existing_reports_supplemented:
- ref: '[[Freight & Working Capital Finance Tracker]]'
  integration_mode: Add SKU-DC working-capital release recommendations to finance
    tracker
new_reports_unlocked:
- title: Inventory Carrying-Cost & Fill-Rate Frontier What-If Tool
  audience: Controller, VP Supply Chain & Inventory Planning
  why_net_new: Interactive quarterly target-setting view for Finance & Ops S&OP sign-off
valuation:
  confidence_tier: directional
  driver_formula: excess_safety_stock_inventory_usd * addressable_rebalance_pct *
    annual_carrying_cost_rate
  drivers:
  - name: excess_safety_stock_inventory_usd
    value: 14500000.0
    range: null
    source: Current on-hand value above 90 DOS in [[prod_catalog.silver.inventory_snapshots]]
    verified: true
    verify_question: null
  - name: addressable_rebalance_pct
    value: null
    range:
    - 0.12
    - 0.18
    - 0.25
    source: Unconstrained ABC/XYZ simulation
    verified: false
    verify_question: Verify with [[Marcus Vance]] what share of >90 DOS stock is unconstrained
      by supplier MOQ floors or non-cancellable import POs.
  - name: annual_carrying_cost_rate
    value: null
    range:
    - 0.11
    - 0.14
    - 0.17
    source: Corporate treasury & warehousing benchmark
    verified: false
    verify_question: Confirm with [[Elena Rostova]] the finance-approved annual inventory
      carrying cost % (WACC + storage + obsolescence).
  annual_benefit_low_usd: 191400.0
  annual_benefit_base_usd: 365400.0
  annual_benefit_high_usd: 616250.0
  verification_prompts:
  - '[VERIFY WITH STAKEHOLDER] addressable_rebalance_pct: Verify with [[Marcus Vance]]
    what share of >90 DOS stock is unconstrained by supplier MOQ floors or non-cancellable
    import POs.'
  - '[VERIFY WITH STAKEHOLDER] annual_carrying_cost_rate: Confirm with [[Elena Rostova]]
    the finance-approved annual inventory carrying cost % (WACC + storage + obsolescence).'
effort_weeks: 4.0
data_readiness_score: 4
adoption_friction_score: 3
computed_priority_score: 44.09
steps_to_completion:
- Audit contractual vendor MOQ constraints with [[Marcus Vance]]
- Confirm Finance WACC + storage carrying-cost rate with [[Elena Rostova]]
- Benchmark deterministic ABC/XYZ SQL policy before building stochastic optimizer
- Publish monthly rebalance recommendations to [[Freight & Working Capital Finance
  Tracker]]
stakeholders:
- '[[Elena Rostova]]'
- '[[Marcus Vance]]'
- '[[Priya Nair]]'
cfo_critique_verdict: NEEDS_VERIFICATION
cfo_critique_notes:
- Hold in staging until [[Marcus Vance]] confirms vendor MOQ constraints — if 70%
  of >90 DOS inventory sits under binding annual supplier MOQs, `addressable_rebalance_pct`
  drops sharply.
- Verify Finance-approved `annual_carrying_cost_rate` (`Q-FIN-02`) with [[Elena Rostova]]
  before promoting to backlog.
---

# OPP-005: Multi-Echelon Safety-Stock Working-Capital Rebalancer

- **Status**: `staging` (ROI-Ranked)
- **Domain Pack**: `ops_supply_chain`
- **Confidence Tier**: `directional`
- **Effort**: `4.0` person-weeks | **Priority Score**: `44.09`
- **Annual Benefit Range (Low / Base / High)**: `$191,400` / `$365,400` / `$616,250`

## Data & Engineering Footprint
- **Internal**: [[prod_catalog.silver.inventory_snapshots]] (daily SKU-DC on-hand balances, unit costs, and >90 DOS excess layers) — _Stratify slow-moving >90 DOS inventory by supplier contractual MOQ_
- **Internal**: [[prod_catalog.gold.po_receipts]] (historical PO order quantities and empirical supplier lead-time variance) — _Infer empirical minimum order quantity floors per supplier SKU_
- **External**: **FRED Macroeconomic Series (CPI, Industrial Production, Interest Rates, Freight TSI)** (`free_public_api`, blocker: `low`) — Benchmark short-term commercial paper / treasury interest cost and industrial demand cycle

## Power BI Delivery Surface
- **Supplements Existing Report**: [[Freight & Working Capital Finance Tracker]] — Add SKU-DC working-capital release recommendations to finance tracker
- **Unlocks Net-New Report**: **Inventory Carrying-Cost & Fill-Rate Frontier What-If Tool** (Controller, VP Supply Chain & Inventory Planning) — Interactive quarterly target-setting view for Finance & Ops S&OP sign-off

## Driver-Tree Valuation & Verification Prompts

**Formula**: `excess_safety_stock_inventory_usd * addressable_rebalance_pct * annual_carrying_cost_rate`

| Driver | Value / Range | Verified | Source |
|---|---|---|---|
| `excess_safety_stock_inventory_usd` | `1.45e+07` | Yes | Current on-hand value above 90 DOS in [[prod_catalog.silver.inventory_snapshots]] |
| `addressable_rebalance_pct` | `[0.12, 0.18, 0.25]` | No (`[VERIFY]`) | Unconstrained ABC/XYZ simulation |
| `annual_carrying_cost_rate` | `[0.11, 0.14, 0.17]` | No (`[VERIFY]`) | Corporate treasury & warehousing benchmark |

### Required Stakeholder Verifications
- [VERIFY WITH STAKEHOLDER] addressable_rebalance_pct: Verify with [[Marcus Vance]] what share of >90 DOS stock is unconstrained by supplier MOQ floors or non-cancellable import POs.
- [VERIFY WITH STAKEHOLDER] annual_carrying_cost_rate: Confirm with [[Elena Rostova]] the finance-approved annual inventory carrying cost % (WACC + storage + obsolescence).

## Steps to Completion
1. Audit contractual vendor MOQ constraints with [[Marcus Vance]]
2. Confirm Finance WACC + storage carrying-cost rate with [[Elena Rostova]]
3. Benchmark deterministic ABC/XYZ SQL policy before building stochastic optimizer
4. Publish monthly rebalance recommendations to [[Freight & Working Capital Finance Tracker]]

## Skeptical CFO Critique
- **Verdict**: `NEEDS_VERIFICATION`
- Hold in staging until [[Marcus Vance]] confirms vendor MOQ constraints — if 70% of >90 DOS inventory sits under binding annual supplier MOQs, `addressable_rebalance_pct` drops sharply.
- Verify Finance-approved `annual_carrying_cost_rate` (`Q-FIN-02`) with [[Elena Rostova]] before promoting to backlog.

Red-Team Gate Summary: Promising working-capital upside built on clean [[prod_catalog.silver.inventory_snapshots]] history, but keep in `vault/staging/opportunities/` until MOQ floors and carrying-cost WACC are confirmed.

<!-- HUMAN-NOTES-START -->
## Human Notes
- Review with [[Elena Rostova]] and [[Marcus Vance]] during Week 3 S&OP prep before promoting via `/review-staging`.
<!-- HUMAN-NOTES-END -->
