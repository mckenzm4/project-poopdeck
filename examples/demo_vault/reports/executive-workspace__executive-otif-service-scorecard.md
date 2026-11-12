---
id: Executive Workspace__Executive OTIF & Service Scorecard
title: Executive OTIF & Service Scorecard
workspace: Executive Workspace
semantic_model: Executive Service SLA Semantic Model
refresh_schedule: Daily 06:30 UTC
refresh_failures_30d: 0
upstream_datasets:
- '[[prod_catalog.gold.otif_shipments]]'
- '[[prod_catalog.gold.sku_demand_daily]]'
measures:
- name: OTIF Attainment %
  table: OtifShipments
  expression: DIVIDE(CALCULATE(COUNTROWS(OtifShipments), OtifShipments[otif_flag]
    = 1), COUNTROWS(OtifShipments), 0)
  description: Realized historical on-time in-full shipment ratio after delivery
  flagged_heuristics:
  - backward_looking_only
- name: Carrier Penalty Alert Flag
  table: OtifShipments
  expression: IF(SUM(OtifShipments[sla_penalty_chargeback_usd]) > 5000, "ESCALATE",
    "NORMAL")
  description: Post-hoc static $5,000 weekly penalty alert after SLA breach already
    occurred
  flagged_heuristics:
  - static_threshold_alert
  - backward_looking_only
relationships:
- from: OtifShipments[ship_date]
  to: Calendar[Date]
  cardinality: many-to-one
bi_to_ds_gaps:
- Add pre-dispatch 72-hour OTIF breach probability scoring so logistics coordinators
  can expedite high-penalty orders before miss ([[OPP-003]])
- Separate weather/force-majeure delays from carrier-attributable SLA misses for Board
  Q1 contract renewals ([[OPP-002]])
high_excel_export: false
owners:
- '[[Sarah Jenkins]]'
- '[[Priya Nair]]'
updated_at: 2026-11-11
---

# Power BI Report: Executive OTIF & Service Scorecard

- **Workspace**: Executive Workspace
- **Semantic Model**: Executive Service SLA Semantic Model
- **Refresh Schedule**: Daily 06:30 UTC (30d Failures: 0)
- **High Excel Export Signal**: No

## Upstream Datasets & Owners
- **Upstream Datasets**: [[prod_catalog.gold.otif_shipments]], [[prod_catalog.gold.sku_demand_daily]]
- **Owners**: [[Sarah Jenkins]], [[Priya Nair]]

## BI-to-DS Replacement Gaps
- Add pre-dispatch 72-hour OTIF breach probability scoring so logistics coordinators can expedite high-penalty orders before miss ([[OPP-003]])
- Separate weather/force-majeure delays from carrier-attributable SLA misses for Board Q1 contract renewals ([[OPP-002]])

## Semantic Model Measures

| Measure | Table | Heuristic Flags | Expression |
|---|---|---|---|
| `OTIF Attainment %` | `OtifShipments` | `backward_looking_only` | `DIVIDE(CALCULATE(COUNTROWS(OtifShipments), OtifShipments[otif_flag] = 1), COUNTROWS(OtifShipments), 0)` |
| `Carrier Penalty Alert Flag` | `OtifShipments` | `static_threshold_alert`, `backward_looking_only` | `IF(SUM(OtifShipments[sla_penalty_chargeback_usd]) > 5000, "ESCALATE", "NORMAL")` |

<!-- HUMAN-NOTES-START -->
## Human Notes
- Reviewed every Tuesday in [[Sarah Jenkins]] executive staff sync; target delivery surface for [[OPP-002]] and [[OPP-003]].
<!-- HUMAN-NOTES-END -->
