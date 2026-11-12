---
id: OPP-001
title: Line-Level Carrier EDI 210 Freight & Fuel Leakage Recovery
status: active
boss_mandate: false
domain_pack: ops_supply_chain
created_at: 2026-11-05
updated_at: 2026-11-11
internal_datasets:
- ref: '[[hive_metastore.legacy_erp.bronze_freight_invoices]]'
  role: line-item EDI 210 billed charges vs. contracted lane rates
  transformations_needed:
  - Normalize accessorial charge codes across 48 carrier SCACs
  - Join shipment weight/zone miles from [[prod_catalog.gold.otif_shipments]]
- ref: '[[prod_catalog.gold.otif_shipments]]'
  role: outbound shipment lane, weight, and delivery confirmation
  transformations_needed:
  - Match shipment_id to EDI 210 invoice lines
external_data_required:
- source: Industrial Metals, Diesel/Energy & Agricultural Commodity Spot Indices
  purpose: Validate carrier weekly DOE diesel fuel-surcharge escalators against contract
    tables
  acquisition: free_public_api
  blocker_severity: low
existing_reports_supplemented:
- ref: '[[Freight & Working Capital Finance Tracker]]'
  integration_mode: Publish daily `prod_catalog.gold.freight_audit_exceptions` table
    replacing static $250 DAX variance cutoff
new_reports_unlocked: []
valuation:
  confidence_tier: finance_verified
  driver_formula: annual_freight_spend_usd * sub_threshold_leakage_rate * ap_recovery_capture_pct
  drivers:
  - name: annual_freight_spend_usd
    value: 28000000.0
    range: null
    source: Trailing 12M billed freight in [[hive_metastore.legacy_erp.bronze_freight_invoices]]
      verified by [[Elena Rostova]]
    verified: true
    verify_question: null
  - name: sub_threshold_leakage_rate
    value: null
    range:
    - 0.018
    - 0.024
    - 0.031
    source: 90-day stratified AP invoice audit on [[Freight & Working Capital Finance
      Tracker]]
    verified: true
    verify_question: null
  - name: ap_recovery_capture_pct
    value: null
    range:
    - 0.55
    - 0.65
    - 0.75
    source: Historical carrier EDI 210 credit-memo recovery rate co-signed by [[Elena
      Rostova]]
    verified: true
    verify_question: null
  annual_benefit_low_usd: 277200.0
  annual_benefit_base_usd: 436800.0
  annual_benefit_high_usd: 651000.0
  verification_prompts: []
effort_weeks: 3.0
data_readiness_score: 4
adoption_friction_score: 1
computed_priority_score: 64.79
steps_to_completion:
- Codify contracted lane tariff + DOE diesel surcharge index join in Databricks Workflow
- Score sub-$250 accessorial anomalies into `prod_catalog.gold.freight_audit_exceptions`
- Wire exception queue into [[Freight & Working Capital Finance Tracker]] with [[Priya
  Nair]]
- Track weekly recovered carrier credit memos with [[Elena Rostova]]
stakeholders:
- '[[Elena Rostova]]'
- '[[Priya Nair]]'
cfo_critique_verdict: READY_FOR_REVIEW
cfo_critique_notes:
- All three unit-economics drivers verified against AP ledger history and signed off
  by [[Elena Rostova]].
- Zero external vendor spend required (public EIA diesel series).
---

# OPP-001: Line-Level Carrier EDI 210 Freight & Fuel Leakage Recovery

- **Status**: `active` (ROI-Ranked)
- **Domain Pack**: `ops_supply_chain`
- **Confidence Tier**: `finance_verified`
- **Effort**: `3.0` person-weeks | **Priority Score**: `64.79`
- **Annual Benefit Range (Low / Base / High)**: `$277,200` / `$436,800` / `$651,000`

## Data & Engineering Footprint
- **Internal**: [[hive_metastore.legacy_erp.bronze_freight_invoices]] (line-item EDI 210 billed charges vs. contracted lane rates) — _Normalize accessorial charge codes across 48 carrier SCACs; Join shipment weight/zone miles from [[prod_catalog.gold.otif_shipments]]_
- **Internal**: [[prod_catalog.gold.otif_shipments]] (outbound shipment lane, weight, and delivery confirmation) — _Match shipment_id to EDI 210 invoice lines_
- **External**: **Industrial Metals, Diesel/Energy & Agricultural Commodity Spot Indices** (`free_public_api`, blocker: `low`) — Validate carrier weekly DOE diesel fuel-surcharge escalators against contract tables

## Power BI Delivery Surface
- **Supplements Existing Report**: [[Freight & Working Capital Finance Tracker]] — Publish daily `prod_catalog.gold.freight_audit_exceptions` table replacing static $250 DAX variance cutoff

## Driver-Tree Valuation & Verification Prompts

**Formula**: `annual_freight_spend_usd * sub_threshold_leakage_rate * ap_recovery_capture_pct`

| Driver | Value / Range | Verified | Source |
|---|---|---|---|
| `annual_freight_spend_usd` | `2.8e+07` | Yes | Trailing 12M billed freight in [[hive_metastore.legacy_erp.bronze_freight_invoices]] verified by [[Elena Rostova]] |
| `sub_threshold_leakage_rate` | `[0.018, 0.024, 0.031]` | Yes | 90-day stratified AP invoice audit on [[Freight & Working Capital Finance Tracker]] |
| `ap_recovery_capture_pct` | `[0.55, 0.65, 0.75]` | Yes | Historical carrier EDI 210 credit-memo recovery rate co-signed by [[Elena Rostova]] |

## Steps to Completion
1. Codify contracted lane tariff + DOE diesel surcharge index join in Databricks Workflow
2. Score sub-$250 accessorial anomalies into `prod_catalog.gold.freight_audit_exceptions`
3. Wire exception queue into [[Freight & Working Capital Finance Tracker]] with [[Priya Nair]]
4. Track weekly recovered carrier credit memos with [[Elena Rostova]]

## Skeptical CFO Critique
- **Verdict**: `READY_FOR_REVIEW`
- All three unit-economics drivers verified against AP ledger history and signed off by [[Elena Rostova]].
- Zero external vendor spend required (public EIA diesel series).

<!-- HUMAN-NOTES-START -->
## Human Notes
- Quick Win #1 in active build (`PHASE-3` / `PHASE-4`); AP analysts already act on this exact table weekly.
<!-- HUMAN-NOTES-END -->
