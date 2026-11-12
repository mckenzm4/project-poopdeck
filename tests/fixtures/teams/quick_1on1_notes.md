# 1:1 Discovery Notes — David Park (Power BI & Semantic Models)

- **Stakeholder**: David Park
- **Role**: Lead Power BI & Semantic Model Architect
- **Department**: Enterprise Analytics & BI
- **Influence Tier**: sme
- **Decision Appetite**: high
- **Contact**: david.park@corp-example.com | Mobile: (555) 301-9988

## Context & Owned Reports
- Owns certified semantic models backing [[Supply Chain Workspace__Daily Buyer Workbench]], [[Supply Chain Workspace__Lost Sales Summary]], and [[Commercial Workspace__Executive Margin & Churn Pulse]].
- Upstream tables bind to [[prod_catalog.silver.inventory_snapshots]], [[prod_catalog.gold.po_receipts]], and [[prod_catalog.gold.customer_invoices]].

## Pain Points
- Pain Point: [[Commercial Workspace__Executive Margin & Churn Pulse]] relies on a nested `SWITCH(TRUE(), ...)` DAX heuristic on trailing 90-day invoice drops with zero forward churn probability.
- Pain Point: Buyers export [[Supply Chain Workspace__Daily Buyer Workbench]] to Excel every Monday morning to override static 14-day reorder flags manually.

## Explicit Asks & Glossary
- Explicit Ask: Materialize daily ML predictions as Unity Catalog Gold tables keyed on `sku_loc_key` and `customer_id` so Power BI semantic models can join them cleanly without custom DAX math.
- Glossary — **Semantic Model Certification**: BI governance sign-off requiring `.pbip` Git version control, row-level security check, and scheduled Databricks SQL Warehouse bind.
- Open Question: What service principal workspace role is required to add a new `prod_catalog.gold` prediction table to a certified `.pbip` model?

## Scratchpad Credentials to Redact
- Legacy PBIP Git automation PAT pasted in chat: `ghp_0123456789abcdefghijklmnopqrstuvwxyzABCD`
- Temporary REST debug header: `Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ1c2VyLTEyMyIsInJvbGUiOiJiaV9hZG1pbiJ9.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c`
