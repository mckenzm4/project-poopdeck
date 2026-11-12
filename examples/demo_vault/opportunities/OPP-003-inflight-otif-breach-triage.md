---
id: OPP-003
title: 72-Hour Pre-Dispatch OTIF Breach Risk & Expedite Triage
status: backlog
boss_mandate: false
domain_pack: ops_supply_chain
created_at: 2026-11-05
updated_at: 2026-11-11
internal_datasets:
- ref: '[[prod_catalog.gold.otif_shipments]]'
  role: historical lane OTIF outcomes and customer chargeback schedules
  transformations_needed:
  - Construct pre-dispatch feature snapshot strictly >=48h prior to `ship_date`
- ref: '[[prod_catalog.gold.sku_demand_daily]]'
  role: daily DC outbound volume surge congestion indicator
  transformations_needed:
  - Compute 7-day rolling DC pick-wave load ratio
external_data_required:
- source: NOAA GHCN Daily Weather, Heating/Cooling Degree Days & Storm Extremes
  purpose: Incorporate corridor storm alerts into pre-dispatch transit risk
  acquisition: free_public_api
  blocker_severity: low
- source: Public Holidays, School Breaks & Fiscal Trading Calendars
  purpose: Capture carrier holiday embargoes and quarter-end shipping surges
  acquisition: free_public_api
  blocker_severity: low
existing_reports_supplemented:
- ref: '[[Executive OTIF & Service Scorecard]]'
  integration_mode: Replace post-hoc static $5,000 penalty flag with daily ranked
    pre-dispatch expedite queue
new_reports_unlocked: []
valuation:
  confidence_tier: directional
  driver_formula: annual_otif_chargebacks_usd * actionable_order_share * net_save_rate_pct
  drivers:
  - name: annual_otif_chargebacks_usd
    value: 1200000.0
    range: null
    source: Verified 12M `sla_penalty_chargeback_usd` in [[prod_catalog.gold.otif_shipments]]
    verified: true
    verify_question: null
  - name: actionable_order_share
    value: 0.8
    range: null
    source: Dispatch operations lead-time estimate
    verified: false
    verify_question: Confirm with [[Sarah Jenkins]] what percentage of high-penalty
      orders are booked >=48 hours before carrier tender.
  - name: net_save_rate_pct
    value: null
    range:
    - 0.2
    - 0.3
    - 0.4
    source: Net chargeback avoided minus expedite premium estimate
    verified: false
    verify_question: Verify with [[Elena Rostova]] net dollar savings per expedited
      shipment after incremental carrier expedite fees.
  annual_benefit_low_usd: 192000.0
  annual_benefit_base_usd: 288000.0
  annual_benefit_high_usd: 384000.0
  verification_prompts:
  - '[VERIFY WITH STAKEHOLDER] actionable_order_share: Confirm with [[Sarah Jenkins]]
    what percentage of high-penalty orders are booked >=48 hours before carrier tender.'
  - '[VERIFY WITH STAKEHOLDER] net_save_rate_pct: Verify with [[Elena Rostova]] net
    dollar savings per expedited shipment after incremental carrier expedite fees.'
effort_weeks: 4.5
data_readiness_score: 4
adoption_friction_score: 2
computed_priority_score: 44.9
steps_to_completion:
- Assemble point-in-time pre-dispatch training set from [[prod_catalog.gold.otif_shipments]]
- Train calibrated gradient-boosted breach probability model weighted by chargeback
  exposure
- Land daily `prod_catalog.gold.otif_pre_dispatch_triage` table for [[Executive OTIF
  & Service Scorecard]]
- Run 4-week dispatch shadow pilot on top 25 weekly risk-weighted shipments
stakeholders:
- '[[Sarah Jenkins]]'
- '[[Elena Rostova]]'
- '[[Priya Nair]]'
cfo_critique_verdict: READY_FOR_REVIEW
cfo_critique_notes:
- Strong unit economics once net expedite surcharge is verified with [[Elena Rostova]].
---

# OPP-003: 72-Hour Pre-Dispatch OTIF Breach Risk & Expedite Triage

- **Status**: `backlog` (ROI-Ranked)
- **Domain Pack**: `ops_supply_chain`
- **Confidence Tier**: `directional`
- **Effort**: `4.5` person-weeks | **Priority Score**: `44.90`
- **Annual Benefit Range (Low / Base / High)**: `$192,000` / `$288,000` / `$384,000`

## Data & Engineering Footprint
- **Internal**: [[prod_catalog.gold.otif_shipments]] (historical lane OTIF outcomes and customer chargeback schedules) — _Construct pre-dispatch feature snapshot strictly >=48h prior to `ship_date`_
- **Internal**: [[prod_catalog.gold.sku_demand_daily]] (daily DC outbound volume surge congestion indicator) — _Compute 7-day rolling DC pick-wave load ratio_
- **External**: **NOAA GHCN Daily Weather, Heating/Cooling Degree Days & Storm Extremes** (`free_public_api`, blocker: `low`) — Incorporate corridor storm alerts into pre-dispatch transit risk
- **External**: **Public Holidays, School Breaks & Fiscal Trading Calendars** (`free_public_api`, blocker: `low`) — Capture carrier holiday embargoes and quarter-end shipping surges

## Power BI Delivery Surface
- **Supplements Existing Report**: [[Executive OTIF & Service Scorecard]] — Replace post-hoc static $5,000 penalty flag with daily ranked pre-dispatch expedite queue

## Driver-Tree Valuation & Verification Prompts

**Formula**: `annual_otif_chargebacks_usd * actionable_order_share * net_save_rate_pct`

| Driver | Value / Range | Verified | Source |
|---|---|---|---|
| `annual_otif_chargebacks_usd` | `1.2e+06` | Yes | Verified 12M `sla_penalty_chargeback_usd` in [[prod_catalog.gold.otif_shipments]] |
| `actionable_order_share` | `0.8` | No (`[VERIFY]`) | Dispatch operations lead-time estimate |
| `net_save_rate_pct` | `[0.2, 0.3, 0.4]` | No (`[VERIFY]`) | Net chargeback avoided minus expedite premium estimate |

### Required Stakeholder Verifications
- [VERIFY WITH STAKEHOLDER] actionable_order_share: Confirm with [[Sarah Jenkins]] what percentage of high-penalty orders are booked >=48 hours before carrier tender.
- [VERIFY WITH STAKEHOLDER] net_save_rate_pct: Verify with [[Elena Rostova]] net dollar savings per expedited shipment after incremental carrier expedite fees.

## Steps to Completion
1. Assemble point-in-time pre-dispatch training set from [[prod_catalog.gold.otif_shipments]]
2. Train calibrated gradient-boosted breach probability model weighted by chargeback exposure
3. Land daily `prod_catalog.gold.otif_pre_dispatch_triage` table for [[Executive OTIF & Service Scorecard]]
4. Run 4-week dispatch shadow pilot on top 25 weekly risk-weighted shipments

## Skeptical CFO Critique
- **Verdict**: `READY_FOR_REVIEW`
- Strong unit economics once net expedite surcharge is verified with [[Elena Rostova]].

<!-- HUMAN-NOTES-START -->
## Human Notes
- High-ROI candidate (`44.90` score, `4.5w`) currently displaced into Q2 when [[OPP-002]] (`5.0w` Boss Mandate) is pinned at `12.0w` quarterly capacity.
<!-- HUMAN-NOTES-END -->
