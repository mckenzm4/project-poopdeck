---
id: OPP-002
title: Board Carrier SLA Weather vs. Warehouse Root-Cause Attribution
status: backlog
boss_mandate: true
domain_pack: ops_supply_chain
created_at: 2026-11-05
updated_at: 2026-11-11
internal_datasets:
- ref: '[[prod_catalog.gold.otif_shipments]]'
  role: historical shipment transit times, OTIF flags, and SLA chargebacks
  transformations_needed:
  - Join origin/destination 3-digit ZIP codes to daily weather extremes
- ref: '[[prod_catalog.bronze.wms_inventory_events]]'
  role: warehouse pick-pack-stage timestamps to isolate internal dock delays
  transformations_needed:
  - Aggregate order release-to-dock-ready hours per shipment_id
external_data_required:
- source: NOAA GHCN Daily Weather, Heating/Cooling Degree Days & Storm Extremes
  purpose: Control for severe storm force-majeure transit corridors along 3-digit
    ZIP lanes
  acquisition: free_public_api
  blocker_severity: low
existing_reports_supplemented:
- ref: '[[Executive OTIF & Service Scorecard]]'
  integration_mode: Add counterfactual weather-adjusted carrier attribution slice
    to executive scorecard
new_reports_unlocked:
- title: Q1 Carrier Contract Renegotiation Rebate Dossier
  audience: VP Supply Chain, Procurement & Board Operations Committee
  why_net_new: Dedicated quarterly negotiation packet isolating carrier-caused SLA
    misses by SCAC
valuation:
  confidence_tier: hypothesis
  driver_formula: annual_sla_penalties_usd * carrier_attributable_share * contract_rebate_recovery_pct
  drivers:
  - name: annual_sla_penalties_usd
    value: 1200000.0
    range: null
    source: Trailing 12M `sla_penalty_chargeback_usd` in [[prod_catalog.gold.otif_shipments]]
    verified: true
    verify_question: null
  - name: carrier_attributable_share
    value: 0.5
    range: null
    source: Preliminary ops hypothesis prior to NOAA weather control
    verified: false
    verify_question: Confirm with [[Sarah Jenkins]] what fraction of late deliveries
      remain carrier-attributable after excluding warehouse late-stage releases.
  - name: contract_rebate_recovery_pct
    value: null
    range:
    - 0.08
    - 0.15
    - 0.25
    source: Procurement Q1 renewal negotiation target
    verified: false
    verify_question: Confirm with [[Sarah Jenkins]] and Legal whether current carrier
      contracts allow retroactive SLA rebate clawbacks.
  annual_benefit_low_usd: 48000.0
  annual_benefit_base_usd: 90000.0
  annual_benefit_high_usd: 150000.0
  verification_prompts:
  - '[VERIFY WITH STAKEHOLDER] carrier_attributable_share: Confirm with [[Sarah Jenkins]]
    what fraction of late deliveries remain carrier-attributable after excluding warehouse
    late-stage releases.'
  - '[VERIFY WITH STAKEHOLDER] contract_rebate_recovery_pct: Confirm with [[Sarah
    Jenkins]] and Legal whether current carrier contracts allow retroactive SLA rebate
    clawbacks.'
effort_weeks: 5.0
data_readiness_score: 3
adoption_friction_score: 3
computed_priority_score: 20.26
steps_to_completion:
- Build warehouse pick-to-dock duration table from [[prod_catalog.bronze.wms_inventory_events]]
- Join NOAA GHCN storm extremes across origin/dest 3-digit ZIP transit windows
- Fit counterfactual transit delay attribution model and publish Gold SCAC scorecard
- Deliver Board Q1 negotiation brief with [[Sarah Jenkins]]
stakeholders:
- '[[Sarah Jenkins]]'
- '[[Priya Nair]]'
cfo_critique_verdict: NEEDS_VERIFICATION
cfo_critique_notes:
- 'Pinned `boss_mandate: true` per [[Sarah Jenkins]] Q1 Board commitment (`5.0` person-weeks).'
- 'Note capacity trade-off: pinning `OPP-002` displaces higher-ROI [[OPP-003]] (`$288k`
  base) past the `12.0`-week quarterly cutline.'
---

# OPP-002: Board Carrier SLA Weather vs. Warehouse Root-Cause Attribution

- **Status**: `backlog` (P0 BOSS MANDATE)
- **Domain Pack**: `ops_supply_chain`
- **Confidence Tier**: `hypothesis`
- **Effort**: `5.0` person-weeks | **Priority Score**: `20.26`
- **Annual Benefit Range (Low / Base / High)**: `$48,000` / `$90,000` / `$150,000`

## Data & Engineering Footprint
- **Internal**: [[prod_catalog.gold.otif_shipments]] (historical shipment transit times, OTIF flags, and SLA chargebacks) — _Join origin/destination 3-digit ZIP codes to daily weather extremes_
- **Internal**: [[prod_catalog.bronze.wms_inventory_events]] (warehouse pick-pack-stage timestamps to isolate internal dock delays) — _Aggregate order release-to-dock-ready hours per shipment_id_
- **External**: **NOAA GHCN Daily Weather, Heating/Cooling Degree Days & Storm Extremes** (`free_public_api`, blocker: `low`) — Control for severe storm force-majeure transit corridors along 3-digit ZIP lanes

## Power BI Delivery Surface
- **Supplements Existing Report**: [[Executive OTIF & Service Scorecard]] — Add counterfactual weather-adjusted carrier attribution slice to executive scorecard
- **Unlocks Net-New Report**: **Q1 Carrier Contract Renegotiation Rebate Dossier** (VP Supply Chain, Procurement & Board Operations Committee) — Dedicated quarterly negotiation packet isolating carrier-caused SLA misses by SCAC

## Driver-Tree Valuation & Verification Prompts

**Formula**: `annual_sla_penalties_usd * carrier_attributable_share * contract_rebate_recovery_pct`

| Driver | Value / Range | Verified | Source |
|---|---|---|---|
| `annual_sla_penalties_usd` | `1.2e+06` | Yes | Trailing 12M `sla_penalty_chargeback_usd` in [[prod_catalog.gold.otif_shipments]] |
| `carrier_attributable_share` | `0.5` | No (`[VERIFY]`) | Preliminary ops hypothesis prior to NOAA weather control |
| `contract_rebate_recovery_pct` | `[0.08, 0.15, 0.25]` | No (`[VERIFY]`) | Procurement Q1 renewal negotiation target |

### Required Stakeholder Verifications
- [VERIFY WITH STAKEHOLDER] carrier_attributable_share: Confirm with [[Sarah Jenkins]] what fraction of late deliveries remain carrier-attributable after excluding warehouse late-stage releases.
- [VERIFY WITH STAKEHOLDER] contract_rebate_recovery_pct: Confirm with [[Sarah Jenkins]] and Legal whether current carrier contracts allow retroactive SLA rebate clawbacks.

## Steps to Completion
1. Build warehouse pick-to-dock duration table from [[prod_catalog.bronze.wms_inventory_events]]
2. Join NOAA GHCN storm extremes across origin/dest 3-digit ZIP transit windows
3. Fit counterfactual transit delay attribution model and publish Gold SCAC scorecard
4. Deliver Board Q1 negotiation brief with [[Sarah Jenkins]]

## Skeptical CFO Critique
- **Verdict**: `NEEDS_VERIFICATION`
- Pinned `boss_mandate: true` per [[Sarah Jenkins]] Q1 Board commitment (`5.0` person-weeks).
- Note capacity trade-off: pinning `OPP-002` displaces higher-ROI [[OPP-003]] (`$288k` base) past the `12.0`-week quarterly cutline.

<!-- HUMAN-NOTES-START -->
## Human Notes
- Non-negotiable Q1 Board commitment from [[Sarah Jenkins]]; use Boss-Pin capacity view in 1:1 to review [[OPP-003]] timeline impact.
<!-- HUMAN-NOTES-END -->
