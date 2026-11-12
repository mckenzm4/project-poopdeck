---
id: prod_catalog.gold.otif_shipments
fqn: prod_catalog.gold.otif_shipments
catalog: prod_catalog
schema_name: gold
table_name: otif_shipments
table_type: MANAGED
medallion_layer: gold
row_count: 4120000
min_timestamp: '2024-01-01T00:00:00Z'
max_timestamp: '2026-11-11T06:10:00Z'
freshness_lag_hours: 0.8
query_count_30d: 640
grain: One row per outbound customer shipment leg (shipment_id)
join_keys:
- shipment_id
- carrier_scac
- origin_zip_3
- dest_zip_3
- ship_date
columns:
- name: shipment_id
  data_type: STRING
  nullable: false
  null_pct: 0.0
  distinct_count: 4120000
  min_val: null
  max_val: null
  sample_values: []
  is_pii: false
  comment: Outbound BOL shipment identifier
- name: carrier_scac
  data_type: STRING
  nullable: false
  null_pct: 0.0
  distinct_count: 48
  min_val: null
  max_val: null
  sample_values: []
  is_pii: false
  comment: Carrier SCAC code
- name: ship_date
  data_type: DATE
  nullable: false
  null_pct: 0.0
  distinct_count: 1046
  min_val: null
  max_val: null
  sample_values: []
  is_pii: false
  comment: Planned warehouse dispatch date
- name: otif_flag
  data_type: INT
  nullable: false
  null_pct: 0.0
  distinct_count: 2
  min_val: 0
  max_val: 1
  sample_values: []
  is_pii: false
  comment: 1 if delivered on-time and in-full, else 0
- name: sla_penalty_chargeback_usd
  data_type: DOUBLE
  nullable: false
  null_pct: 0.0
  distinct_count: 4900
  min_val: 0.0
  max_val: 18500.0
  sample_values: []
  is_pii: false
  comment: Customer contractual OTIF chargeback USD
- name: customer_contact_email
  data_type: STRING
  nullable: true
  null_pct: 3.4
  distinct_count: 1420
  min_val: '[REDACTED:PII]'
  max_val: '[REDACTED:PII]'
  sample_values:
  - '[REDACTED:PII]'
  is_pii: true
  comment: Customer receiving contact email masked by safe_aggregates
sample_rows: []
upstream_refs:
- '[[hive_metastore.legacy_erp.bronze_freight_invoices]]'
- '[[prod_catalog.gold.sku_demand_daily]]'
downstream_refs: []
scheduled_jobs:
- wf_gold_otif_shipments_daily_0610
tags:
  domain: ops_supply_chain
  sla_tier: tier_1
updated_at: 2026-11-11
---

# Dataset: `prod_catalog.gold.otif_shipments`

- **Medallion Layer**: `gold`
- **Table Type**: `MANAGED`
- **Grain**: One row per outbound customer shipment leg (shipment_id)
- **Join Keys**: `shipment_id`, `carrier_scac`, `origin_zip_3`, `dest_zip_3`, `ship_date`
- **Row Count**: 4,120,000
- **Freshness Lag (Hours)**: 0.8h
- **30d Query Count**: 640

## Lineage & Workflows
- **Upstream**: [[hive_metastore.legacy_erp.bronze_freight_invoices]], [[prod_catalog.gold.sku_demand_daily]]
- **Downstream**: _None_
- **Scheduled Jobs**: `wf_gold_otif_shipments_daily_0610`

## Column Schema & Safe Profile

| Column | Type | Null % | Distinct | PII | Comment |
|---|---|---:|---:|---|---|
| `shipment_id` | `STRING` | 0.0% | 4,120,000 | No | Outbound BOL shipment identifier |
| `carrier_scac` | `STRING` | 0.0% | 48 | No | Carrier SCAC code |
| `ship_date` | `DATE` | 0.0% | 1,046 | No | Planned warehouse dispatch date |
| `otif_flag` | `INT` | 0.0% | 2 | No | 1 if delivered on-time and in-full, else 0 |
| `sla_penalty_chargeback_usd` | `DOUBLE` | 0.0% | 4,900 | No | Customer contractual OTIF chargeback USD |
| `customer_contact_email` | `STRING` | 3.4% | 1,420 | REDACTED/PII | Customer receiving contact email masked by safe_aggregates |

## Sample Rows

_Zero sample rows stored (safe_aggregates privacy gate)._

<!-- HUMAN-NOTES-START -->
## Human Notes
- Powers [[Executive OTIF & Service Scorecard]]; trailing 12M `sla_penalty_chargeback_usd` verified at `$1.20M`.
<!-- HUMAN-NOTES-END -->
