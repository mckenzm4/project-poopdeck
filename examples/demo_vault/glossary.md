---
id: glossary
title: Business & KPI Glossary
terms:
- term: OTIF
  definition: 'On-Time In-Full customer delivery attainment: shipment delivered on
    or before promised date with 100% of ordered line quantities.'
  source: Supply Chain SLA Policy
  formula_or_logic: COUNT(otif_flag = 1) / COUNT(shipment_id)
  source_refs:
  - '[[prod_catalog.gold.otif_shipments]]'
  - '[[Executive OTIF & Service Scorecard]]'
  owner: Sarah Jenkins
- term: Days of Supply (DOS)
  definition: Allocatable on-hand units divided by trailing or forecasted daily SKU-DC
    unit demand.
  source: Inventory Planning SOP
  formula_or_logic: on_hand_units / avg_daily_ordered_units
  source_refs:
  - '[[prod_catalog.silver.inventory_snapshots]]'
  - '[[Daily Buyer Workbench]]'
  owner: Marcus Vance
- term: Stockout Lost Margin $
  definition: Unfulfilled customer order contribution margin caused by zero allocatable
    on-hand inventory at order allocation time.
  source: Commercial Finance Controllership
  formula_or_logic: (ordered_units - shipped_units) * net_unit_contribution_margin_usd
  source_refs:
  - '[[prod_catalog.gold.sku_demand_daily]]'
  - '[[Elena Rostova]]'
  owner: Elena Rostova
- term: EDI 210 Accessorial Leakage
  definition: Carrier freight invoice line overcharges (fuel surcharges, detention,
    liftgate) billed above contracted lane tariff schedules.
  source: Accounts Payable Freight Audit
  formula_or_logic: MAX(0, billed_freight_usd - contract_rate_usd)
  source_refs:
  - '[[hive_metastore.legacy_erp.bronze_freight_invoices]]'
  - '[[Freight & Working Capital Finance Tracker]]'
  owner: Elena Rostova
- term: SKU-Loc Key
  definition: Canonical composite join key (`<sku_id>__<warehouse_id>`) linking inventory
    snapshots, PO receipts, and daily demand.
  source: Enterprise Semantic Model Architecture
  formula_or_logic: CONCAT(sku_id, '__', warehouse_id)
  source_refs:
  - '[[prod_catalog.silver.inventory_snapshots]]'
  - '[[Priya Nair]]'
  owner: Priya Nair
updated_at: 2026-11-11
---

# Business & KPI Glossary

| Term | Definition | Source | Linked Entities |
|---|---|---|---|
| **OTIF** | On-Time In-Full customer delivery attainment: shipment delivered on or before promised date with 100% of ordered line quantities. | Supply Chain SLA Policy | [[prod_catalog.gold.otif_shipments]], [[Executive OTIF & Service Scorecard]] |
| **Days of Supply (DOS)** | Allocatable on-hand units divided by trailing or forecasted daily SKU-DC unit demand. | Inventory Planning SOP | [[prod_catalog.silver.inventory_snapshots]], [[Daily Buyer Workbench]] |
| **Stockout Lost Margin $** | Unfulfilled customer order contribution margin caused by zero allocatable on-hand inventory at order allocation time. | Commercial Finance Controllership | [[prod_catalog.gold.sku_demand_daily]], [[Elena Rostova]] |
| **EDI 210 Accessorial Leakage** | Carrier freight invoice line overcharges (fuel surcharges, detention, liftgate) billed above contracted lane tariff schedules. | Accounts Payable Freight Audit | [[hive_metastore.legacy_erp.bronze_freight_invoices]], [[Freight & Working Capital Finance Tracker]] |
| **SKU-Loc Key** | Canonical composite join key (`<sku_id>__<warehouse_id>`) linking inventory snapshots, PO receipts, and daily demand. | Enterprise Semantic Model Architecture | [[prod_catalog.silver.inventory_snapshots]], [[Priya Nair]] |

<!-- HUMAN-NOTES-START -->
## Human Notes
- Standardized definitions across Supply Chain Planning and Controllership.
<!-- HUMAN-NOTES-END -->
