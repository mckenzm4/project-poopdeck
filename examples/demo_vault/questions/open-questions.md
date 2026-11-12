---
id: open-questions
title: Week 1-2 Discovery Questionnaire & Open Questions
questions:
- id: Q-MGR-01
  category: manager
  who_to_ask: Manager
  topic: Success Horizon
  question: In 90 days and in 12 months, what concrete outcome makes hiring a Data
    Scientist an obvious win to leadership?
  status: open
  answer_summary: null
  linked_refs: []
  phase: days_1_5
- id: Q-MGR-02
  category: manager
  who_to_ask: Manager
  topic: Existing Commitments
  question: Are there projects already promised to executives before I arrived (`Boss
    Mandates`), or is the slate genuinely open for ROI-ranked discovery?
  status: answered
  answer_summary: VP Sarah Jenkins committed Q1 Carrier SLA Board Attribution (OPP-002,
    5.0 person-weeks) to the Board Operations Committee; remaining Q1 capacity is
    open for ROI-ranked quick wins.
  linked_refs:
  - '[[Sarah Jenkins]]'
  - '[[OPP-002]]'
  phase: days_1_5
- id: Q-MGR-03
  category: manager
  who_to_ask: Manager
  topic: Decision Appetite
  question: Which business leaders actually change operations based on data today,
    and which teams ignore dashboards?
  status: open
  answer_summary: null
  linked_refs: []
  phase: days_6_15
- id: Q-MGR-04
  category: manager
  who_to_ask: Manager
  topic: Budget Envelope
  question: Do we have budget or appetite for external data APIs/vendors if ROI justifies
    it, or should Q1 stick strictly to internal + free public data (NOAA, Census,
    FRED)?
  status: open
  answer_summary: null
  linked_refs:
  - '[[OPP-006]]'
  phase: days_6_15
- id: Q-IT-01
  category: it_security
  who_to_ask: IT / Security / Platform Admin
  topic: Claude Code Hosting & Policy
  question: Is Claude Code routed through Anthropic Enterprise, AWS Bedrock, or Azure/Vertex,
    and what is our written policy on sending schema metadata vs. aggregate stats
    vs. sample rows?
  status: open
  answer_summary: null
  linked_refs: []
  phase: days_1_5
- id: Q-IT-02
  category: it_security
  who_to_ask: IT / Security / Platform Admin
  topic: Databricks Local Auth
  question: Are Personal Access Tokens (PATs) or `databricks auth login` OAuth enabled
    for local IDE/CLI connections, and do we have Unity Catalog `system.information_schema`
    & `system.access.table_lineage` enabled?
  status: answered
  answer_summary: Unity Catalog system.information_schema and system.access.table_lineage
    are enabled on prod_catalog; legacy EDI 210 freight invoices remain in hive_metastore.legacy_erp
    until Q2.
  linked_refs:
  - '[[prod_catalog.silver.inventory_snapshots]]'
  - '[[hive_metastore.legacy_erp.bronze_freight_invoices]]'
  phase: days_1_5
- id: Q-IT-03
  category: it_security
  who_to_ask: IT / Security / Platform Admin
  topic: Git & Package Proxies
  question: Where do internal DS repos live (GitHub Enterprise, Azure DevOps, GitLab),
    and is there a corporate SSL root cert / Artifactory mirror needed for `pip` /
    `uv`?
  status: open
  answer_summary: null
  linked_refs: []
  phase: days_1_5
- id: Q-DE-01
  category: data_engineering
  who_to_ask: Data Engineering
  topic: Trustworthy Gold vs. Trap Tables
  question: Which catalogs/schemas are production-governed SLAs vs. stale migration
    leftovers or analyst sandboxes?
  status: open
  answer_summary: null
  linked_refs: []
  phase: days_1_5
- id: Q-DE-02
  category: data_engineering
  who_to_ask: Data Engineering
  topic: Historical Point-in-Time Snapshots
  question: Where do we have true SCD Type 2 / daily snapshot history (safe for ML
    training without time-travel leakage) vs. overwritten current-state tables?
  status: open
  answer_summary: null
  linked_refs:
  - '[[prod_catalog.silver.inventory_snapshots]]'
  phase: days_6_15
- id: Q-DE-03
  category: data_engineering
  who_to_ask: Data Engineering
  topic: Path to Production
  question: When I build a batch scoring pipeline (e.g., daily Gold predictions table),
    do Data Scientists own scheduling Databricks Workflows directly, or hand off PRs
    to Data Engineering?
  status: open
  answer_summary: null
  linked_refs: []
  phase: days_6_15
- id: Q-PBI-01
  category: power_bi
  who_to_ask: Power BI / Analytics Owners
  topic: Top 10 High-Traffic Reports
  question: Which 5–10 Power BI reports have the highest daily executive/operational
    viewership (Usage Metrics)?
  status: open
  answer_summary: null
  linked_refs: []
  phase: days_1_5
- id: Q-PBI-02
  category: power_bi
  who_to_ask: Power BI / Analytics Owners
  topic: The 'Export to Excel' Signal
  question: Which Power BI tables do business users constantly export to Excel every
    Monday to run manual formulas, adjustments, or what-if guesses?
  status: answered
  answer_summary: 14 regional buyers export Daily Buyer Workbench every Monday morning
    for manual lead-time buffers, and AP analysts export Freight & Working Capital
    Finance Tracker weekly.
  linked_refs:
  - '[[Priya Nair]]'
  - '[[Daily Buyer Workbench]]'
  - '[[Freight & Working Capital Finance Tracker]]'
  phase: days_6_15
- id: Q-PBI-03
  category: power_bi
  who_to_ask: Power BI / Analytics Owners
  topic: Semantic Model Governance
  question: Are semantic models managed via `.pbip`/Git or published `.pbix` files,
    and what is the process to add a new Databricks Gold prediction table into an
    existing certified dataset?
  status: open
  answer_summary: null
  linked_refs:
  - '[[Priya Nair]]'
  - '[[Daily Buyer Workbench]]'
  phase: days_6_15
- id: Q-FIN-01
  category: business_finance
  who_to_ask: Business Unit Leads & Finance
  topic: Action Lever
  question: If you knew 2 weeks earlier which [customer / SKU / order / invoice /
    asset] would fail or spike, what specific action would your team take, and how
    many could they realistically act on per week?
  status: open
  answer_summary: null
  linked_refs:
  - '[[Marcus Vance]]'
  - '[[OPP-004]]'
  phase: days_6_15
- id: Q-FIN-02
  category: business_finance
  who_to_ask: Business Unit Leads & Finance
  topic: Unit Economics
  question: When Finance calculates the dollar cost of a [churned account / stockout
    / late shipment / write-off / markdown], what standard unit margin or cost figure
    do they trust?
  status: open
  answer_summary: null
  linked_refs:
  - '[[Elena Rostova]]'
  - '[[OPP-004]]'
  - '[[OPP-005]]'
  phase: days_16_30
updated_at: 2026-11-11
---

# Week 1–2 Discovery Questionnaire & Open Questions

### A. Manager (Mandate, Air Cover & Political Terrain)

- [ ] **Q-MGR-01 (Success Horizon)** — In 90 days and in 12 months, what concrete outcome makes hiring a Data Scientist an obvious win to leadership? _(Ask: Manager | Phase: `days_1_5`)_
- [x] **Q-MGR-02 (Existing Commitments)** — Are there projects already promised to executives before I arrived (`Boss Mandates`), or is the slate genuinely open for ROI-ranked discovery? _(Ask: Manager | Phase: `days_1_5`)_
  - **Answer**: VP Sarah Jenkins committed Q1 Carrier SLA Board Attribution (OPP-002, 5.0 person-weeks) to the Board Operations Committee; remaining Q1 capacity is open for ROI-ranked quick wins.
  - **Refs**: [[Sarah Jenkins]], [[OPP-002]]
- [ ] **Q-MGR-03 (Decision Appetite)** — Which business leaders actually change operations based on data today, and which teams ignore dashboards? _(Ask: Manager | Phase: `days_6_15`)_
- [ ] **Q-MGR-04 (Budget Envelope)** — Do we have budget or appetite for external data APIs/vendors if ROI justifies it, or should Q1 stick strictly to internal + free public data (NOAA, Census, FRED)? _(Ask: Manager | Phase: `days_6_15`)_
  - **Refs**: [[OPP-006]]

### B. IT / Security / Platform Admin (Tooling & Guardrails)

- [ ] **Q-IT-01 (Claude Code Hosting & Policy)** — Is Claude Code routed through Anthropic Enterprise, AWS Bedrock, or Azure/Vertex, and what is our written policy on sending schema metadata vs. aggregate stats vs. sample rows? _(Ask: IT / Security / Platform Admin | Phase: `days_1_5`)_
- [x] **Q-IT-02 (Databricks Local Auth)** — Are Personal Access Tokens (PATs) or `databricks auth login` OAuth enabled for local IDE/CLI connections, and do we have Unity Catalog `system.information_schema` & `system.access.table_lineage` enabled? _(Ask: IT / Security / Platform Admin | Phase: `days_1_5`)_
  - **Answer**: Unity Catalog system.information_schema and system.access.table_lineage are enabled on prod_catalog; legacy EDI 210 freight invoices remain in hive_metastore.legacy_erp until Q2.
  - **Refs**: [[prod_catalog.silver.inventory_snapshots]], [[hive_metastore.legacy_erp.bronze_freight_invoices]]
- [ ] **Q-IT-03 (Git & Package Proxies)** — Where do internal DS repos live (GitHub Enterprise, Azure DevOps, GitLab), and is there a corporate SSL root cert / Artifactory mirror needed for `pip` / `uv`? _(Ask: IT / Security / Platform Admin | Phase: `days_1_5`)_

### C. Data Engineering (Lakehouse Reality & Production Path)

- [ ] **Q-DE-01 (Trustworthy Gold vs. Trap Tables)** — Which catalogs/schemas are production-governed SLAs vs. stale migration leftovers or analyst sandboxes? _(Ask: Data Engineering | Phase: `days_1_5`)_
- [ ] **Q-DE-02 (Historical Point-in-Time Snapshots)** — Where do we have true SCD Type 2 / daily snapshot history (safe for ML training without time-travel leakage) vs. overwritten current-state tables? _(Ask: Data Engineering | Phase: `days_6_15`)_
  - **Refs**: [[prod_catalog.silver.inventory_snapshots]]
- [ ] **Q-DE-03 (Path to Production)** — When I build a batch scoring pipeline (e.g., daily Gold predictions table), do Data Scientists own scheduling Databricks Workflows directly, or hand off PRs to Data Engineering? _(Ask: Data Engineering | Phase: `days_6_15`)_

### D. Power BI / Analytics Owners (Delivery & Adoption)

- [ ] **Q-PBI-01 (Top 10 High-Traffic Reports)** — Which 5–10 Power BI reports have the highest daily executive/operational viewership (Usage Metrics)? _(Ask: Power BI / Analytics Owners | Phase: `days_1_5`)_
- [x] **Q-PBI-02 (The 'Export to Excel' Signal)** — Which Power BI tables do business users constantly export to Excel every Monday to run manual formulas, adjustments, or what-if guesses? _(Ask: Power BI / Analytics Owners | Phase: `days_6_15`)_
  - **Answer**: 14 regional buyers export Daily Buyer Workbench every Monday morning for manual lead-time buffers, and AP analysts export Freight & Working Capital Finance Tracker weekly.
  - **Refs**: [[Priya Nair]], [[Daily Buyer Workbench]], [[Freight & Working Capital Finance Tracker]]
- [ ] **Q-PBI-03 (Semantic Model Governance)** — Are semantic models managed via `.pbip`/Git or published `.pbix` files, and what is the process to add a new Databricks Gold prediction table into an existing certified dataset? _(Ask: Power BI / Analytics Owners | Phase: `days_6_15`)_
  - **Refs**: [[Priya Nair]], [[Daily Buyer Workbench]]

### E. Business Unit Leads & Finance (Grounding the Dollar Formula)

- [ ] **Q-FIN-01 (Action Lever)** — If you knew 2 weeks earlier which [customer / SKU / order / invoice / asset] would fail or spike, what specific action would your team take, and how many could they realistically act on per week? _(Ask: Business Unit Leads & Finance | Phase: `days_6_15`)_
  - **Refs**: [[Marcus Vance]], [[OPP-004]]
- [ ] **Q-FIN-02 (Unit Economics)** — When Finance calculates the dollar cost of a [churned account / stockout / late shipment / write-off / markdown], what standard unit margin or cost figure do they trust? _(Ask: Business Unit Leads & Finance | Phase: `days_16_30`)_
  - **Refs**: [[Elena Rostova]], [[OPP-004]], [[OPP-005]]

<!-- HUMAN-NOTES-START -->
## Human Notes
- Schedule 30-min unit-economics review with [[Elena Rostova]] and [[Marcus Vance]] in Week 2 to close `Q-FIN-01` and `Q-FIN-02`.
<!-- HUMAN-NOTES-END -->
