---
id: prod_catalog.gold.sku_demand_daily
fqn: prod_catalog.gold.sku_demand_daily
catalog: prod_catalog
schema_name: gold
table_name: sku_demand_daily
table_type: MANAGED
medallion_layer: gold
row_count: 38260000
min_timestamp: '2024-06-01T00:00:00Z'
max_timestamp: '2026-11-11T06:00:00Z'
freshness_lag_hours: 1.0
query_count_30d: 715
grain: One row per SKU, distribution center, and calendar day (sku_loc_key, calendar_date)
join_keys:
- sku_loc_key
- calendar_date
- sku_id
- zip_code_3
columns:
- name: sku_loc_key
  data_type: STRING
  nullable: false
  null_pct: 0.0
  distinct_count: 42800
  min_val: null
  max_val: null
  sample_values: []
  is_pii: false
  comment: SKU-DC composite key
- name: calendar_date
  data_type: DATE
  nullable: false
  null_pct: 0.0
  distinct_count: 894
  min_val: null
  max_val: null
  sample_values: []
  is_pii: false
  comment: Order demand date
- name: ordered_units
  data_type: DOUBLE
  nullable: false
  null_pct: 0.0
  distinct_count: 9800
  min_val: 0.0
  max_val: 42000.0
  sample_values: []
  is_pii: false
  comment: Unconstrained customer ordered units
- name: shipped_units
  data_type: DOUBLE
  nullable: false
  null_pct: 0.0
  distinct_count: 9650
  min_val: 0.0
  max_val: 42000.0
  sample_values: []
  is_pii: false
  comment: Fulfilled units shipped
- name: stockout_lost_margin_usd
  data_type: DOUBLE
  nullable: false
  null_pct: 0.0
  distinct_count: 18400
  min_val: 0.0
  max_val: 64500.0
  sample_values: []
  is_pii: false
  comment: Unfulfilled order margin USD attributed to zero on-hand stock
- name: account_manager_name
  data_type: STRING
  nullable: true
  null_pct: 2.1
  distinct_count: 85
  min_val: '[REDACTED:PII]'
  max_val: '[REDACTED:PII]'
  sample_values:
  - '[REDACTED:PII]'
  is_pii: true
  comment: Commercial rep name masked by safe_aggregates
sample_rows: []
upstream_refs:
- '[[prod_catalog.silver.inventory_snapshots]]'
downstream_refs:
- '[[prod_catalog.gold.otif_shipments]]'
scheduled_jobs:
- wf_gold_sku_demand_daily_0600
tags:
  domain: ops_supply_chain
  sla_tier: tier_1
updated_at: 2026-11-11
---

# Dataset: `prod_catalog.gold.sku_demand_daily`

- **Medallion Layer**: `gold`
- **Table Type**: `MANAGED`
- **Grain**: One row per SKU, distribution center, and calendar day (sku_loc_key, calendar_date)
- **Join Keys**: `sku_loc_key`, `calendar_date`, `sku_id`, `zip_code_3`
- **Row Count**: 38,260,000
- **Freshness Lag (Hours)**: 1.0h
- **30d Query Count**: 715

## Lineage & Workflows
- **Upstream**: [[prod_catalog.silver.inventory_snapshots]]
- **Downstream**: [[prod_catalog.gold.otif_shipments]]
- **Scheduled Jobs**: `wf_gold_sku_demand_daily_0600`

## Column Schema & Safe Profile

| Column | Type | Null % | Distinct | PII | Comment |
|---|---|---:|---:|---|---|
| `sku_loc_key` | `STRING` | 0.0% | 42,800 | No | SKU-DC composite key |
| `calendar_date` | `DATE` | 0.0% | 894 | No | Order demand date |
| `ordered_units` | `DOUBLE` | 0.0% | 9,800 | No | Unconstrained customer ordered units |
| `shipped_units` | `DOUBLE` | 0.0% | 9,650 | No | Fulfilled units shipped |
| `stockout_lost_margin_usd` | `DOUBLE` | 0.0% | 18,400 | No | Unfulfilled order margin USD attributed to zero on-hand stock |
| `account_manager_name` | `STRING` | 2.1% | 85 | REDACTED/PII | Commercial rep name masked by safe_aggregates |

## Sample Rows

_Zero sample rows stored (safe_aggregates privacy gate)._

<!-- HUMAN-NOTES-START -->
## Human Notes
- Certified Gold table powering [[Daily Buyer Workbench]]; trailing 12M `stockout_lost_margin_usd` sums to `$4.25M`.
<!-- HUMAN-NOTES-END -->
