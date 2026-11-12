---
id: hive_metastore.legacy_erp.bronze_freight_invoices
fqn: hive_metastore.legacy_erp.bronze_freight_invoices
catalog: hive_metastore
schema_name: legacy_erp
table_name: bronze_freight_invoices
table_type: EXTERNAL
medallion_layer: bronze
row_count: 1940000
min_timestamp: '2023-01-01T00:00:00Z'
max_timestamp: '2026-11-10T22:00:00Z'
freshness_lag_hours: 14.5
query_count_30d: 96
grain: One row per carrier EDI 210 freight invoice charge line (invoice_line_id)
join_keys:
- invoice_line_id
- shipment_id
- carrier_scac
columns:
- name: invoice_line_id
  data_type: STRING
  nullable: false
  null_pct: 0.0
  distinct_count: 1940000
  min_val: null
  max_val: null
  sample_values: []
  is_pii: false
  comment: EDI 210 line identifier
- name: shipment_id
  data_type: STRING
  nullable: true
  null_pct: 0.8
  distinct_count: 810000
  min_val: null
  max_val: null
  sample_values: []
  is_pii: false
  comment: Links to [[prod_catalog.gold.otif_shipments]]
- name: carrier_scac
  data_type: STRING
  nullable: false
  null_pct: 0.0
  distinct_count: 48
  min_val: null
  max_val: null
  sample_values: []
  is_pii: false
  comment: Standard Carrier Alpha Code
- name: billed_freight_usd
  data_type: DOUBLE
  nullable: false
  null_pct: 0.0
  distinct_count: 94200
  min_val: 15.0
  max_val: 48500.0
  sample_values: []
  is_pii: false
  comment: Invoiced line amount in USD
- name: contract_rate_usd
  data_type: DOUBLE
  nullable: true
  null_pct: 3.1
  distinct_count: 61000
  min_val: 15.0
  max_val: 42000.0
  sample_values: []
  is_pii: false
  comment: Expected tariff baseline USD
- name: ap_contact_phone
  data_type: STRING
  nullable: true
  null_pct: 6.5
  distinct_count: 310
  min_val: '[REDACTED:PII]'
  max_val: '[REDACTED:PII]'
  sample_values:
  - '[REDACTED:PII]'
  is_pii: true
  comment: Carrier AP phone contact masked by safe_aggregates
sample_rows: []
upstream_refs: []
downstream_refs:
- '[[prod_catalog.gold.otif_shipments]]'
scheduled_jobs:
- wf_legacy_erp_edi210_nightly
tags:
  domain: ops_supply_chain
  migration_status: legacy_hive
updated_at: 2026-11-11
---

# Dataset: `hive_metastore.legacy_erp.bronze_freight_invoices`

- **Medallion Layer**: `bronze`
- **Table Type**: `EXTERNAL`
- **Grain**: One row per carrier EDI 210 freight invoice charge line (invoice_line_id)
- **Join Keys**: `invoice_line_id`, `shipment_id`, `carrier_scac`
- **Row Count**: 1,940,000
- **Freshness Lag (Hours)**: 14.5h
- **30d Query Count**: 96

## Lineage & Workflows
- **Upstream**: _None_
- **Downstream**: [[prod_catalog.gold.otif_shipments]]
- **Scheduled Jobs**: `wf_legacy_erp_edi210_nightly`

## Column Schema & Safe Profile

| Column | Type | Null % | Distinct | PII | Comment |
|---|---|---:|---:|---|---|
| `invoice_line_id` | `STRING` | 0.0% | 1,940,000 | No | EDI 210 line identifier |
| `shipment_id` | `STRING` | 0.8% | 810,000 | No | Links to [[prod_catalog.gold.otif_shipments]] |
| `carrier_scac` | `STRING` | 0.0% | 48 | No | Standard Carrier Alpha Code |
| `billed_freight_usd` | `DOUBLE` | 0.0% | 94,200 | No | Invoiced line amount in USD |
| `contract_rate_usd` | `DOUBLE` | 3.1% | 61,000 | No | Expected tariff baseline USD |
| `ap_contact_phone` | `STRING` | 6.5% | 310 | REDACTED/PII | Carrier AP phone contact masked by safe_aggregates |

## Sample Rows

_Zero sample rows stored (safe_aggregates privacy gate)._

<!-- HUMAN-NOTES-START -->
## Human Notes
- Legacy `hive_metastore` table scheduled for Unity Catalog migration in Q2; verified line-item history for [[OPP-001]].
<!-- HUMAN-NOTES-END -->
