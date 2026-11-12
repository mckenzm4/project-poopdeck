---
id: Supply Chain Workspace__Daily Buyer Workbench
title: Daily Buyer Workbench
workspace: Supply Chain Workspace
semantic_model: Supply Chain Core Semantic Model
refresh_schedule: Daily 06:15 UTC
refresh_failures_30d: 1
upstream_datasets:
- '[[prod_catalog.silver.inventory_snapshots]]'
- '[[prod_catalog.gold.po_receipts]]'
- '[[prod_catalog.gold.sku_demand_daily]]'
measures:
- name: Reorder Risk Tier
  table: InventorySnapshots
  expression: SWITCH(TRUE(), [Days of Supply] < 7, "CRITICAL", [Days of Supply] <
    14, "HIGH", [Days of Supply] < 28, "WATCH", "OK")
  description: Manual static DOS cutoff tiering without supplier lead-time variance
  flagged_heuristics:
  - nested_heuristic_tiers
  - static_threshold_alert
- name: Trailing 28d Demand Forecast
  table: SkuDemandDaily
  expression: CALCULATE(AVERAGEX(VALUES(Calendar[Date]), [Ordered Units]), DATESINPERIOD(Calendar[Date],
    MAX(Calendar[Date]), -28, DAY)) * 14
  description: Naive 28-day moving average extrapolated over 14 days
  flagged_heuristics:
  - naive_moving_avg_forecast
  - backward_looking_only
- name: Trailing 12M Stockout Lost Margin $
  table: SkuDemandDaily
  expression: CALCULATE(SUM(SkuDemandDaily[stockout_lost_margin_usd]), DATESINPERIOD(Calendar[Date],
    MAX(Calendar[Date]), -12, MONTH))
  description: Historical 12-month realized stockout margin loss
  flagged_heuristics:
  - backward_looking_only
relationships:
- from: SkuDemandDaily[sku_loc_key]
  to: InventorySnapshots[sku_loc_key]
  cardinality: many-to-one
- from: PoReceipts[sku_loc_key]
  to: InventorySnapshots[sku_loc_key]
  cardinality: many-to-one
bi_to_ds_gaps:
- Replace static 7/14/28-day `Reorder Risk Tier` cutoffs with calibrated 14-day stockout
  probability and p90 lead-time quantile buffers ([[OPP-004]])
- Eliminate Monday morning manual Excel exports across 14 regional inventory buyers
high_excel_export: true
owners:
- '[[Marcus Vance]]'
- '[[Priya Nair]]'
updated_at: 2026-11-11
---

# Power BI Report: Daily Buyer Workbench

- **Workspace**: Supply Chain Workspace
- **Semantic Model**: Supply Chain Core Semantic Model
- **Refresh Schedule**: Daily 06:15 UTC (30d Failures: 1)
- **High Excel Export Signal**: Yes

## Upstream Datasets & Owners
- **Upstream Datasets**: [[prod_catalog.silver.inventory_snapshots]], [[prod_catalog.gold.po_receipts]], [[prod_catalog.gold.sku_demand_daily]]
- **Owners**: [[Marcus Vance]], [[Priya Nair]]

## BI-to-DS Replacement Gaps
- Replace static 7/14/28-day `Reorder Risk Tier` cutoffs with calibrated 14-day stockout probability and p90 lead-time quantile buffers ([[OPP-004]])
- Eliminate Monday morning manual Excel exports across 14 regional inventory buyers

## Semantic Model Measures

| Measure | Table | Heuristic Flags | Expression |
|---|---|---|---|
| `Reorder Risk Tier` | `InventorySnapshots` | `nested_heuristic_tiers`, `static_threshold_alert` | `SWITCH(TRUE(), [Days of Supply] < 7, "CRITICAL", [Days of Supply] < 14, "HIGH", [Days of Supply] < 28, "WATCH", "OK")` |
| `Trailing 28d Demand Forecast` | `SkuDemandDaily` | `naive_moving_avg_forecast`, `backward_looking_only` | `CALCULATE(AVERAGEX(VALUES(Calendar[Date]), [Ordered Units]), DATESINPERIOD(Calendar[Date], MAX(Calendar[Date]), -28, DAY)) * 14` |
| `Trailing 12M Stockout Lost Margin $` | `SkuDemandDaily` | `backward_looking_only` | `CALCULATE(SUM(SkuDemandDaily[stockout_lost_margin_usd]), DATESINPERIOD(Calendar[Date], MAX(Calendar[Date]), -12, MONTH))` |

<!-- HUMAN-NOTES-START -->
## Human Notes
- Highest daily operational traffic in Supply Chain (14 buyers daily at 07:00); prime Adoption-First target for [[OPP-004]].
<!-- HUMAN-NOTES-END -->
