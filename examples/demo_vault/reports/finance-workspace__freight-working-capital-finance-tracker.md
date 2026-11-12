---
id: Finance Workspace__Freight & Working Capital Finance Tracker
title: Freight & Working Capital Finance Tracker
workspace: Finance Workspace
semantic_model: Commercial Finance & Freight Semantic Model
refresh_schedule: Daily 07:00 UTC
refresh_failures_30d: 2
upstream_datasets:
- '[[hive_metastore.legacy_erp.bronze_freight_invoices]]'
- '[[prod_catalog.gold.otif_shipments]]'
- '[[prod_catalog.silver.inventory_snapshots]]'
measures:
- name: Freight Overbill Variance Alert
  table: FreightInvoices
  expression: IF(SUM(FreightInvoices[billed_freight_usd]) - SUM(FreightInvoices[contract_rate_usd])
    > 250, "AUDIT", "AUTO_PAY")
  description: Static $250 invoice variance rule missing systematic sub-threshold
    accessorial and fuel overcharges
  flagged_heuristics:
  - static_threshold_alert
- name: Customer Credit Risk Bucket
  table: OtifShipments
  expression: SWITCH(TRUE(), [Past Due Chargeback Days] > 60, "HIGH_RISK", [Past Due
    Chargeback Days] > 30, "MEDIUM_RISK", "LOW_RISK")
  description: Backward-looking aging bucket heuristic used in manual AR credit reviews
  flagged_heuristics:
  - nested_heuristic_tiers
  - backward_looking_only
relationships:
- from: FreightInvoices[shipment_id]
  to: OtifShipments[shipment_id]
  cardinality: many-to-one
bi_to_ds_gaps:
- Score every EDI 210 freight line against diesel spot indices and lane tariffs to
  recover sub-$250 accessorial leakage ([[OPP-001]])
- Evaluate inventory working-capital carrying cost vs. fill-rate frontier ([[OPP-005]])
  and commercial credit delinquency risk ([[OPP-006]])
high_excel_export: true
owners:
- '[[Elena Rostova]]'
- '[[Priya Nair]]'
updated_at: 2026-11-11
---

# Power BI Report: Freight & Working Capital Finance Tracker

- **Workspace**: Finance Workspace
- **Semantic Model**: Commercial Finance & Freight Semantic Model
- **Refresh Schedule**: Daily 07:00 UTC (30d Failures: 2)
- **High Excel Export Signal**: Yes

## Upstream Datasets & Owners
- **Upstream Datasets**: [[hive_metastore.legacy_erp.bronze_freight_invoices]], [[prod_catalog.gold.otif_shipments]], [[prod_catalog.silver.inventory_snapshots]]
- **Owners**: [[Elena Rostova]], [[Priya Nair]]

## BI-to-DS Replacement Gaps
- Score every EDI 210 freight line against diesel spot indices and lane tariffs to recover sub-$250 accessorial leakage ([[OPP-001]])
- Evaluate inventory working-capital carrying cost vs. fill-rate frontier ([[OPP-005]]) and commercial credit delinquency risk ([[OPP-006]])

## Semantic Model Measures

| Measure | Table | Heuristic Flags | Expression |
|---|---|---|---|
| `Freight Overbill Variance Alert` | `FreightInvoices` | `static_threshold_alert` | `IF(SUM(FreightInvoices[billed_freight_usd]) - SUM(FreightInvoices[contract_rate_usd]) > 250, "AUDIT", "AUTO_PAY")` |
| `Customer Credit Risk Bucket` | `OtifShipments` | `nested_heuristic_tiers`, `backward_looking_only` | `SWITCH(TRUE(), [Past Due Chargeback Days] > 60, "HIGH_RISK", [Past Due Chargeback Days] > 30, "MEDIUM_RISK", "LOW_RISK")` |

<!-- HUMAN-NOTES-START -->
## Human Notes
- Accounts Payable exports `Freight Overbill Variance Alert` lines weekly; [[OPP-001]] plugs directly into AP dispute workflow.
<!-- HUMAN-NOTES-END -->
