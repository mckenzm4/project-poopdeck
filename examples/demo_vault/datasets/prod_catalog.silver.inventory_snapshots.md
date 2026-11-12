---
id: prod_catalog.silver.inventory_snapshots
fqn: prod_catalog.silver.inventory_snapshots
catalog: prod_catalog
schema_name: silver
table_name: inventory_snapshots
table_type: MANAGED
medallion_layer: silver
row_count: 31245000
min_timestamp: '2024-06-01T00:00:00Z'
max_timestamp: '2026-11-11T05:30:00Z'
freshness_lag_hours: 1.5
query_count_30d: 462
grain: Daily point-in-time snapshot per SKU and distribution center (sku_loc_key,
  snapshot_date)
join_keys:
- sku_loc_key
- snapshot_date
- sku_id
- warehouse_id
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
  comment: Canonical SKU-DC composite key
- name: snapshot_date
  data_type: DATE
  nullable: false
  null_pct: 0.0
  distinct_count: 894
  min_val: null
  max_val: null
  sample_values: []
  is_pii: false
  comment: As-of UTC business date (safe for point-in-time ML training)
- name: on_hand_units
  data_type: DOUBLE
  nullable: false
  null_pct: 0.0
  distinct_count: 14200
  min_val: 0.0
  max_val: 95000.0
  sample_values: []
  is_pii: false
  comment: End-of-day allocatable units
- name: safety_stock_units
  data_type: DOUBLE
  nullable: true
  null_pct: 0.4
  distinct_count: 4100
  min_val: 0.0
  max_val: 25000.0
  sample_values: []
  is_pii: false
  comment: ERP static safety stock target
- name: unit_cost_usd
  data_type: DOUBLE
  nullable: false
  null_pct: 0.0
  distinct_count: 8900
  min_val: 0.35
  max_val: 1450.0
  sample_values: []
  is_pii: false
  comment: Standard landed unit cost USD
- name: buyer_name
  data_type: STRING
  nullable: false
  null_pct: 0.0
  distinct_count: 14
  min_val: '[REDACTED:PII]'
  max_val: '[REDACTED:PII]'
  sample_values:
  - '[REDACTED:PII]'
  is_pii: true
  comment: Assigned buyer full name masked by safe_aggregates
sample_rows: []
upstream_refs:
- '[[prod_catalog.bronze.wms_inventory_events]]'
downstream_refs:
- '[[prod_catalog.gold.sku_demand_daily]]'
scheduled_jobs:
- wf_silver_inventory_daily_0530
tags:
  domain: ops_supply_chain
  point_in_time_safe: 'true'
  sla_tier: tier_1
updated_at: 2026-11-11
---

# Dataset: `prod_catalog.silver.inventory_snapshots`

- **Medallion Layer**: `silver`
- **Table Type**: `MANAGED`
- **Grain**: Daily point-in-time snapshot per SKU and distribution center (sku_loc_key, snapshot_date)
- **Join Keys**: `sku_loc_key`, `snapshot_date`, `sku_id`, `warehouse_id`
- **Row Count**: 31,245,000
- **Freshness Lag (Hours)**: 1.5h
- **30d Query Count**: 462

## Lineage & Workflows
- **Upstream**: [[prod_catalog.bronze.wms_inventory_events]]
- **Downstream**: [[prod_catalog.gold.sku_demand_daily]]
- **Scheduled Jobs**: `wf_silver_inventory_daily_0530`

## Column Schema & Safe Profile

| Column | Type | Null % | Distinct | PII | Comment |
|---|---|---:|---:|---|---|
| `sku_loc_key` | `STRING` | 0.0% | 42,800 | No | Canonical SKU-DC composite key |
| `snapshot_date` | `DATE` | 0.0% | 894 | No | As-of UTC business date (safe for point-in-time ML training) |
| `on_hand_units` | `DOUBLE` | 0.0% | 14,200 | No | End-of-day allocatable units |
| `safety_stock_units` | `DOUBLE` | 0.4% | 4,100 | No | ERP static safety stock target |
| `unit_cost_usd` | `DOUBLE` | 0.0% | 8,900 | No | Standard landed unit cost USD |
| `buyer_name` | `STRING` | 0.0% | 14 | REDACTED/PII | Assigned buyer full name masked by safe_aggregates |

## Sample Rows

_Zero sample rows stored (safe_aggregates privacy gate)._

<!-- HUMAN-NOTES-START -->
## Human Notes
- Confirmed true daily partitioned history since 2024-06-01 (no overwrite leakage) — safe feature store backbone for [[OPP-004]] and [[OPP-005]].
<!-- HUMAN-NOTES-END -->
