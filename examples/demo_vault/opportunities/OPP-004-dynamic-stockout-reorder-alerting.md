---
id: OPP-004
title: Quantile Lead-Time & 14-Day Stockout Risk Scoring in Buyer Workbench
status: backlog
boss_mandate: false
domain_pack: ops_supply_chain
created_at: 2026-11-05
updated_at: 2026-11-11
internal_datasets:
- ref: '[[prod_catalog.silver.inventory_snapshots]]'
  role: point-in-time daily on-hand inventory and static safety stock history
  transformations_needed:
  - Join daily snapshot_date without forward-looking leakage
- ref: '[[prod_catalog.gold.po_receipts]]'
  role: empirical supplier and port p50/p90 lead-time distribution
  transformations_needed:
  - Compute rolling hierarchical supplier + origin_port_code quantile lead times
- ref: '[[prod_catalog.gold.sku_demand_daily]]'
  role: daily unconstrained ordered units and historical stockout margin loss
  transformations_needed:
  - Build 14-day horizon demand quantile features per `sku_loc_key`
external_data_required:
- source: NOAA GHCN Daily Weather, Heating/Cooling Degree Days & Storm Extremes
  purpose: Capture regional weather-driven demand surges across distribution centers
  acquisition: free_public_api
  blocker_severity: low
- source: Global Port Congestion, Dwell Time & Container Freight Index
  purpose: Adjust import supplier p90 lead-time tails during port dwell spikes
  acquisition: free_public_api
  blocker_severity: low
existing_reports_supplemented:
- ref: '[[Daily Buyer Workbench]]'
  integration_mode: Join `prod_catalog.gold.sku_reorder_risk_daily` on `sku_loc_key`
    to replace static `Reorder Risk Tier` DAX and Monday Excel exports
new_reports_unlocked:
- title: Executive Working-Capital vs. Service-Level Tradeoff Simulator
  audience: Inventory Planning & Commercial Finance
  why_net_new: Interactive policy curve showing aggregate inventory dollars required
    per +0.5% service-level target
valuation:
  confidence_tier: directional
  driver_formula: annual_stockout_lost_margin_usd * addressable_share * expected_reduction_pct
  drivers:
  - name: annual_stockout_lost_margin_usd
    value: 4250000.0
    range: null
    source: Trailing 12M `stockout_lost_margin_usd` in [[prod_catalog.gold.sku_demand_daily]]
      powering [[Daily Buyer Workbench]]
    verified: true
    verify_question: null
  - name: addressable_share
    value: 0.65
    range: null
    source: Inventory planning lead-time window estimate
    verified: false
    verify_question: Confirm with [[Marcus Vance]] what share of stockouts occur on
      SKUs where buyers can expedite or reallocate DC stock within 14 days.
  - name: expected_reduction_pct
    value: null
    range:
    - 0.07
    - 0.12
    - 0.19
    source: Quantile safety-stock pilot benchmark
    verified: false
    verify_question: Validate 7%-19% preventable stockout reduction during a 4-week
      shadow pilot with [[Marcus Vance]] and [[Elena Rostova]].
  annual_benefit_low_usd: 193375.0
  annual_benefit_base_usd: 331500.0
  annual_benefit_high_usd: 524875.0
  verification_prompts:
  - '[VERIFY WITH STAKEHOLDER] addressable_share: Confirm with [[Marcus Vance]] what
    share of stockouts occur on SKUs where buyers can expedite or reallocate DC stock
    within 14 days.'
  - '[VERIFY WITH STAKEHOLDER] expected_reduction_pct: Validate 7%-19% preventable
    stockout reduction during a 4-week shadow pilot with [[Marcus Vance]] and [[Elena
    Rostova]].'
effort_weeks: 3.5
data_readiness_score: 4
adoption_friction_score: 2
computed_priority_score: 47.18
steps_to_completion:
- Backtest p90 supplier lead-time + 14-day demand quantile model on [[prod_catalog.silver.inventory_snapshots]]
- Schedule daily Unity Catalog Gold scoring table `prod_catalog.gold.sku_reorder_risk_daily`
- Wire risk rank and recommended reorder qty into [[Daily Buyer Workbench]] with [[Priya
  Nair]]
- Measure Monday Excel export drop and prevented stockouts across 3 pilot buyers with
  [[Marcus Vance]]
stakeholders:
- '[[Marcus Vance]]'
- '[[Priya Nair]]'
- '[[Elena Rostova]]'
cfo_critique_verdict: READY_FOR_REVIEW
cfo_critique_notes:
- Point-in-time history verified in [[prod_catalog.silver.inventory_snapshots]] (`894`
  daily snapshots).
- Confirm net contribution margin basis of `$4.25M` baseline with [[Elena Rostova]]
  during pilot kickoff.
---

# OPP-004: Quantile Lead-Time & 14-Day Stockout Risk Scoring in Buyer Workbench

- **Status**: `backlog` (ROI-Ranked)
- **Domain Pack**: `ops_supply_chain`
- **Confidence Tier**: `directional`
- **Effort**: `3.5` person-weeks | **Priority Score**: `47.18`
- **Annual Benefit Range (Low / Base / High)**: `$193,375` / `$331,500` / `$524,875`

## Data & Engineering Footprint
- **Internal**: [[prod_catalog.silver.inventory_snapshots]] (point-in-time daily on-hand inventory and static safety stock history) — _Join daily snapshot_date without forward-looking leakage_
- **Internal**: [[prod_catalog.gold.po_receipts]] (empirical supplier and port p50/p90 lead-time distribution) — _Compute rolling hierarchical supplier + origin_port_code quantile lead times_
- **Internal**: [[prod_catalog.gold.sku_demand_daily]] (daily unconstrained ordered units and historical stockout margin loss) — _Build 14-day horizon demand quantile features per `sku_loc_key`_
- **External**: **NOAA GHCN Daily Weather, Heating/Cooling Degree Days & Storm Extremes** (`free_public_api`, blocker: `low`) — Capture regional weather-driven demand surges across distribution centers
- **External**: **Global Port Congestion, Dwell Time & Container Freight Index** (`free_public_api`, blocker: `low`) — Adjust import supplier p90 lead-time tails during port dwell spikes

## Power BI Delivery Surface
- **Supplements Existing Report**: [[Daily Buyer Workbench]] — Join `prod_catalog.gold.sku_reorder_risk_daily` on `sku_loc_key` to replace static `Reorder Risk Tier` DAX and Monday Excel exports
- **Unlocks Net-New Report**: **Executive Working-Capital vs. Service-Level Tradeoff Simulator** (Inventory Planning & Commercial Finance) — Interactive policy curve showing aggregate inventory dollars required per +0.5% service-level target

## Driver-Tree Valuation & Verification Prompts

**Formula**: `annual_stockout_lost_margin_usd * addressable_share * expected_reduction_pct`

| Driver | Value / Range | Verified | Source |
|---|---|---|---|
| `annual_stockout_lost_margin_usd` | `4.25e+06` | Yes | Trailing 12M `stockout_lost_margin_usd` in [[prod_catalog.gold.sku_demand_daily]] powering [[Daily Buyer Workbench]] |
| `addressable_share` | `0.65` | No (`[VERIFY]`) | Inventory planning lead-time window estimate |
| `expected_reduction_pct` | `[0.07, 0.12, 0.19]` | No (`[VERIFY]`) | Quantile safety-stock pilot benchmark |

### Required Stakeholder Verifications
- [VERIFY WITH STAKEHOLDER] addressable_share: Confirm with [[Marcus Vance]] what share of stockouts occur on SKUs where buyers can expedite or reallocate DC stock within 14 days.
- [VERIFY WITH STAKEHOLDER] expected_reduction_pct: Validate 7%-19% preventable stockout reduction during a 4-week shadow pilot with [[Marcus Vance]] and [[Elena Rostova]].

## Steps to Completion
1. Backtest p90 supplier lead-time + 14-day demand quantile model on [[prod_catalog.silver.inventory_snapshots]]
2. Schedule daily Unity Catalog Gold scoring table `prod_catalog.gold.sku_reorder_risk_daily`
3. Wire risk rank and recommended reorder qty into [[Daily Buyer Workbench]] with [[Priya Nair]]
4. Measure Monday Excel export drop and prevented stockouts across 3 pilot buyers with [[Marcus Vance]]

## Skeptical CFO Critique
- **Verdict**: `READY_FOR_REVIEW`
- Point-in-time history verified in [[prod_catalog.silver.inventory_snapshots]] (`894` daily snapshots).
- Confirm net contribution margin basis of `$4.25M` baseline with [[Elena Rostova]] during pilot kickoff.

<!-- HUMAN-NOTES-START -->
## Human Notes
- Flagship Adoption-First project (`47.18` score, `3.5w`); fits inside Q1 `12.0w` capacity even alongside [[OPP-002]] and [[OPP-001]].
<!-- HUMAN-NOTES-END -->
