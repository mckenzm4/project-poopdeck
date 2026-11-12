# Buyers & BI Sync — Daily Buyer Workbench & Monday Excel Exports

- **Date**: 2026-11-10
- **Participants**: Marcus Vance (Director of Inventory Planning & Purchasing), Priya Nair (Lead BI & Semantic Model Architect)
- **Channel**: Supply Chain Planning Weekly 1:1

## Discussion Highlights

1. **Monday Export-to-Excel Pain Point (`Daily Buyer Workbench`)**:
   - **Marcus Vance**: Every Monday morning between 07:00 and 09:30, 14 regional inventory buyers export the SKU-location table out of `Daily Buyer Workbench` into offline Excel spreadsheets because the `Reorder Risk Tier` DAX rule flags static 14-day Days of Supply (`DOS`) cutoffs without accounting for port congestion or supplier lead-time tail variance.
   - Buyers want a calibrated 14-day stockout probability and p90 supplier lead-time buffer surfaced directly inside `Daily Buyer Workbench` via a Unity Catalog Gold table keyed on `sku_loc_key` — not another standalone dashboard to check.

2. **Semantic Model & Gold Table Wiring**:
   - **Priya Nair**: `Supply Chain Core Semantic Model` is governed via `.pbip` in Git and refreshes daily at `06:00 UTC` from `prod_catalog.silver.inventory_snapshots`, `prod_catalog.gold.po_receipts`, and `prod_catalog.gold.sku_demand_daily`.
   - Adding a daily Gold predictions table (`sku_loc_key`, `snapshot_date`) into `Daily Buyer Workbench` takes less than half a day once Data Science lands a scheduled Databricks Workflow.

3. **Executive Mandate Context (`Sarah Jenkins`)**:
   - **Marcus Vance**: Note that VP of Supply Chain & Distribution **Sarah Jenkins** already promised the Q1 Board Operations Committee a carrier weather vs. warehouse attribution dossier (`OPP-002`) ahead of Q1 carrier contract renewals, so portfolio capacity planning needs to show what gets displaced if `OPP-002` takes 5 person-weeks.

## Open Follow-Ups
- Confirm with Corporate Controller **Elena Rostova** whether the `$4.25M` trailing 12-month `Stockout Lost Margin $` figure reflects net contribution margin after freight and promotional allowances, and verify standard annual inventory carrying cost (`11%`–`17%`).
