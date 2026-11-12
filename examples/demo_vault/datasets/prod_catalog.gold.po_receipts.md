---
id: prod_catalog.gold.po_receipts
fqn: prod_catalog.gold.po_receipts
catalog: prod_catalog
schema_name: gold
table_name: po_receipts
table_type: MANAGED
medallion_layer: gold
row_count: 2680000
min_timestamp: '2024-01-01T00:00:00Z'
max_timestamp: '2026-11-11T05:50:00Z'
freshness_lag_hours: 1.1
query_count_30d: 389
grain: One row per completed purchase order line receipt (po_line_id)
join_keys:
- po_line_id
- sku_loc_key
- supplier_id
- origin_port_code
columns:
- name: po_line_id
  data_type: STRING
  nullable: false
  null_pct: 0.0
  distinct_count: 2680000
  min_val: null
  max_val: null
  sample_values: []
  is_pii: false
  comment: PO receipt line primary key
- name: sku_loc_key
  data_type: STRING
  nullable: false
  null_pct: 0.0
  distinct_count: 39600
  min_val: null
  max_val: null
  sample_values: []
  is_pii: false
  comment: Receiving SKU-DC key
- name: supplier_id
  data_type: STRING
  nullable: false
  null_pct: 0.0
  distinct_count: 640
  min_val: null
  max_val: null
  sample_values: []
  is_pii: false
  comment: Vendor master identifier
- name: origin_port_code
  data_type: STRING
  nullable: true
  null_pct: 8.2
  distinct_count: 34
  min_val: null
  max_val: null
  sample_values: []
  is_pii: false
  comment: UN/LOCODE port for import lanes
- name: actual_lead_time_days
  data_type: DOUBLE
  nullable: true
  null_pct: 0.5
  distinct_count: 140
  min_val: 1.0
  max_val: 128.0
  sample_values: []
  is_pii: false
  comment: Observed PO order-to-putaway days
- name: quoted_lead_time_days
  data_type: DOUBLE
  nullable: false
  null_pct: 0.0
  distinct_count: 45
  min_val: 2.0
  max_val: 90.0
  sample_values: []
  is_pii: false
  comment: ERP static vendor quoted lead time
- name: vendor_rep_email
  data_type: STRING
  nullable: true
  null_pct: 4.1
  distinct_count: 580
  min_val: '[REDACTED:PII]'
  max_val: '[REDACTED:PII]'
  sample_values:
  - '[REDACTED:PII]'
  is_pii: true
  comment: Supplier rep email masked by safe_aggregates
sample_rows: []
upstream_refs:
- '[[prod_catalog.bronze.wms_inventory_events]]'
downstream_refs: []
scheduled_jobs:
- wf_gold_po_leadtime_daily_0550
tags:
  domain: ops_supply_chain
  sla_tier: tier_1
updated_at: 2026-11-11
---

# Dataset: `prod_catalog.gold.po_receipts`

- **Medallion Layer**: `gold`
- **Table Type**: `MANAGED`
- **Grain**: One row per completed purchase order line receipt (po_line_id)
- **Join Keys**: `po_line_id`, `sku_loc_key`, `supplier_id`, `origin_port_code`
- **Row Count**: 2,680,000
- **Freshness Lag (Hours)**: 1.1h
- **30d Query Count**: 389

## Lineage & Workflows
- **Upstream**: [[prod_catalog.bronze.wms_inventory_events]]
- **Downstream**: _None_
- **Scheduled Jobs**: `wf_gold_po_leadtime_daily_0550`

## Column Schema & Safe Profile

| Column | Type | Null % | Distinct | PII | Comment |
|---|---|---:|---:|---|---|
| `po_line_id` | `STRING` | 0.0% | 2,680,000 | No | PO receipt line primary key |
| `sku_loc_key` | `STRING` | 0.0% | 39,600 | No | Receiving SKU-DC key |
| `supplier_id` | `STRING` | 0.0% | 640 | No | Vendor master identifier |
| `origin_port_code` | `STRING` | 8.2% | 34 | No | UN/LOCODE port for import lanes |
| `actual_lead_time_days` | `DOUBLE` | 0.5% | 140 | No | Observed PO order-to-putaway days |
| `quoted_lead_time_days` | `DOUBLE` | 0.0% | 45 | No | ERP static vendor quoted lead time |
| `vendor_rep_email` | `STRING` | 4.1% | 580 | REDACTED/PII | Supplier rep email masked by safe_aggregates |

## Sample Rows

_Zero sample rows stored (safe_aggregates privacy gate)._

<!-- HUMAN-NOTES-START -->
## Human Notes
- Captures empirical p50/p90 lead-time drift vs. ERP static vendor quotes; joins cleanly to port congestion indices on `origin_port_code`.
<!-- HUMAN-NOTES-END -->
