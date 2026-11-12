---
id: priya-nair
name: Priya Nair
role: Lead BI & Semantic Model Architect
department: Enterprise Data & Analytics
influence_tier: partner
decision_appetite: high
owned_reports:
- '[[Daily Buyer Workbench]]'
- '[[Executive OTIF & Service Scorecard]]'
- '[[Freight & Working Capital Finance Tracker]]'
kpis_cared_about:
- Semantic Model Refresh SLA %
- DAX Query Duration (ms)
- Certified Gold Reuse
pain_points:
- Hand-rolled nested SWITCH(TRUE(), ...) DAX risk buckets are brittle and slow down
  model refreshes
- Shadow Excel spreadsheets proliferate whenever Power BI only shows backward-looking
  KPIs
explicit_asks:
- Publish daily Unity Catalog Gold prediction tables keyed on `sku_loc_key` and `shipment_id`
  for thin `.pbip` joins
boss_mandate_asks: []
updated_at: 2026-11-11
---

# Stakeholder: Priya Nair (Lead BI & Semantic Model Architect)

- **Department**: Enterprise Data & Analytics
- **Influence Tier**: `partner`
- **Decision Appetite**: `high`
- **Owned Reports**: [[Daily Buyer Workbench]], [[Executive OTIF & Service Scorecard]], [[Freight & Working Capital Finance Tracker]]

## KPIs Cared About
- Semantic Model Refresh SLA %
- DAX Query Duration (ms)
- Certified Gold Reuse

## Pain Points
- Hand-rolled nested SWITCH(TRUE(), ...) DAX risk buckets are brittle and slow down model refreshes
- Shadow Excel spreadsheets proliferate whenever Power BI only shows backward-looking KPIs

## Explicit Asks
- Publish daily Unity Catalog Gold prediction tables keyed on `sku_loc_key` and `shipment_id` for thin `.pbip` joins

## Boss Mandate Asks
- _None._

<!-- HUMAN-NOTES-START -->
## Human Notes
- Owns `.pbip` Git repo for all three certified semantic models; can wire a Gold table into Power BI in <0.5 days.
<!-- HUMAN-NOTES-END -->
