---
id: prod_catalog.bronze.wms_inventory_events
fqn: prod_catalog.bronze.wms_inventory_events
catalog: prod_catalog
schema_name: bronze
table_name: wms_inventory_events
table_type: EXTERNAL
medallion_layer: bronze
row_count: 48250000
min_timestamp: '2024-01-01T00:00:00Z'
max_timestamp: '2026-11-11T05:45:00Z'
freshness_lag_hours: 1.2
query_count_30d: 184
grain: One row per raw WMS barcode scan and bin movement event (event_id)
join_keys:
- event_id
- sku_loc_key
- warehouse_id
columns:
- name: event_id
  data_type: STRING
  nullable: false
  null_pct: 0.0
  distinct_count: 48250000
  min_val: null
  max_val: null
  sample_values: []
  is_pii: false
  comment: Immutable WMS event UUID
- name: sku_loc_key
  data_type: STRING
  nullable: false
  null_pct: 0.0
  distinct_count: 42800
  min_val: null
  max_val: null
  sample_values: []
  is_pii: false
  comment: Composite SKU + DC key
- name: event_ts
  data_type: TIMESTAMP
  nullable: false
  null_pct: 0.0
  distinct_count: 19400000
  min_val: null
  max_val: null
  sample_values: []
  is_pii: false
  comment: UTC scan timestamp
- name: qty_delta
  data_type: DOUBLE
  nullable: true
  null_pct: 0.2
  distinct_count: 1850
  min_val: -5000.0
  max_val: 12000.0
  sample_values: []
  is_pii: false
  comment: Signed unit movement quantity
- name: operator_email
  data_type: STRING
  nullable: true
  null_pct: 1.4
  distinct_count: 620
  min_val: '[REDACTED:PII]'
  max_val: '[REDACTED:PII]'
  sample_values:
  - '[REDACTED:PII]'
  is_pii: true
  comment: Scanner operator email masked by safe_aggregates
sample_rows: []
upstream_refs: []
downstream_refs:
- '[[prod_catalog.silver.inventory_snapshots]]'
- '[[prod_catalog.gold.po_receipts]]'
scheduled_jobs:
- wf_wms_cdc_autoloader_hourly
tags:
  domain: ops_supply_chain
  sla_tier: tier_2
updated_at: 2026-11-11
---

# Dataset: `prod_catalog.bronze.wms_inventory_events`

- **Medallion Layer**: `bronze`
- **Table Type**: `EXTERNAL`
- **Grain**: One row per raw WMS barcode scan and bin movement event (event_id)
- **Join Keys**: `event_id`, `sku_loc_key`, `warehouse_id`
- **Row Count**: 48,250,000
- **Freshness Lag (Hours)**: 1.2h
- **30d Query Count**: 184

## Lineage & Workflows
- **Upstream**: _None_
- **Downstream**: [[prod_catalog.silver.inventory_snapshots]], [[prod_catalog.gold.po_receipts]]
- **Scheduled Jobs**: `wf_wms_cdc_autoloader_hourly`

## Column Schema & Safe Profile

| Column | Type | Null % | Distinct | PII | Comment |
|---|---|---:|---:|---|---|
| `event_id` | `STRING` | 0.0% | 48,250,000 | No | Immutable WMS event UUID |
| `sku_loc_key` | `STRING` | 0.0% | 42,800 | No | Composite SKU + DC key |
| `event_ts` | `TIMESTAMP` | 0.0% | 19,400,000 | No | UTC scan timestamp |
| `qty_delta` | `DOUBLE` | 0.2% | 1,850 | No | Signed unit movement quantity |
| `operator_email` | `STRING` | 1.4% | 620 | REDACTED/PII | Scanner operator email masked by safe_aggregates |

## Sample Rows

_Zero sample rows stored (safe_aggregates privacy gate)._

<!-- HUMAN-NOTES-START -->
## Human Notes
- Append-only Auto Loader stream from regional WMS; deduplicate on `event_id` before aggregating.
<!-- HUMAN-NOTES-END -->
