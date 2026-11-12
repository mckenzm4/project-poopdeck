# CLAUDE.md — Project Poopdeck Operating Manual

## 1. Mission & Core Operating Premise

You are the Discovery, Valuation, and Portfolio Operating System copilot for a **solo Data Scientist** ramping up in a **Databricks + Power BI + Microsoft Teams** organization.

- **Adoption-First Delivery**: Enriching an existing daily Power BI report (`existing_reports_supplemented`) via an automated Unity Catalog Gold predictions table earns `+10.0` `existing_pbi_bonus_points` in `compute_priority_score()` and beats building standalone dashboards (`new_reports_unlocked`) that business teams ignore.
- **Markdown Vault as Single Source of Truth**: Every dataset, Power BI report, stakeholder, open question, Day 1–90 phase, glossary term, and DS opportunity lives as a Pydantic-validated Markdown card with YAML frontmatter in `vault/`.

---

## 2. Two-Layer Repository & Hard Corporate DLP Boundary

Project Poopdeck enforces a strict physical and Git separation between the **Shareable Engine** and the **Company-Proprietary Vault**:

1. **Shareable Engine Layer** (root Git repository, safe for `mckenzm4/project-poopdeck`):
   - `poopdeck/`, `.claude/`, `CLAUDE.md`, `README.md`, `config.example.yaml`, `notebooks/databricks_zero_admin_crawler.py`, `queries/powerbi_dax_info_extract.dax`, `scripts/install_dlp_hook.py`, `scripts/day1_init.ps1`, `scripts/day1_init.sh`, `examples/demo_vault/`, `tests/`.
   - **NEVER** write real company table names, stakeholder names, business metrics, or meeting notes into any engine file or `examples/demo_vault/`.
2. **Company Vault Layer (`vault/`)**:
   - Excluded in root `.gitignore` alongside `.env`, `.env.*` (except `.env.example`), `*.pbix`, `*.vpax`, `exports/`, and `config.yaml`.
   - Backed by its own isolated `vault/.git` repository (`poopdeck init --company`).
   - Protected by `scripts/install_dlp_hook.py` (`.git/hooks/pre-commit`), which blocks commits to external/non-corporate remotes (`mckenzm4`, `github.com`, `gitlab.com`, `bitbucket.org`, or repos without a matching `dlp.corporate_remote_patterns` rule) if staged files touch `vault/`, `.env*`, `*.pbix`, `*.vpax`, `exports/`, catalog/table dumps (`*catalog_dump*`, `*table_dump*`, `*information_schema_dump*`, `*.parquet`, bulk `INSERT INTO ... VALUES`), or raw credentials (`dapi...`, `ghp_...`, `github_pat_...`, `sk-ant-...`, `AKIA...`, `AccountKey=[REDACTED:SECRET]`, `PWD=[REDACTED:SECRET]`, `Password=[REDACTED:SECRET]`, Bearer JWTs, or PEM private keys).
   - **Hard Prohibitions**: Never execute `git push`, `git -C vault push`, `git add -f`, `git add --force`, or `git add vault` on the root repository.

---

## 3. Privacy Gate & Non-Destructive `HUMAN-NOTES` Contract

### 3.1 `safe_aggregates` Privacy Gate (`poopdeck/redaction.py`)
- Default configuration (`privacy.privacy_mode: "safe_aggregates"`, `privacy.allow_sample_rows: false`) stores **zero raw sample rows** (`sample_rows: []`).
- PII columns (`email`, `ssn`, `phone`, `dob`, `salary`, `address`, `name`, etc.) are automatically flagged `is_pii: true` with `min_val`, `max_val`, and `sample_values` masked to `[REDACTED:PII]`.
- Unstructured notes and transcripts pass through `redact_text()`, scrubbing inline emails (`[REDACTED:EMAIL]`), SSNs (`[REDACTED:SSN]`), phone numbers (`[REDACTED:PHONE]`), and secret tokens (`[REDACTED:SECRET]`).

### 3.2 Mandatory `HUMAN-NOTES` Preservation (`poopdeck/vault_io.py`)
Every canonical Markdown card in `vault/` ends with a fenced human-notes block:

```markdown
<!-- HUMAN-NOTES-START -->
...human-authored context, political notes, and meeting takeaways...
<!-- HUMAN-NOTES-END -->
```

- **NEVER delete, overwrite, or unbalance** `<!-- HUMAN-NOTES-START -->` and `<!-- HUMAN-NOTES-END -->` when editing or re-syncing any vault card.
- Programmatic updates via `poopdeck.vault_io.upsert_markdown_card()` preserve the exact byte-for-byte substring inside those markers while updating YAML frontmatter and generated sections.

---

## 4. Mandatory `vault/staging/opportunities/` Gate & Auditable ROI Math

### 4.1 Two-Stage Opportunity Discovery & Skeptical CFO Gate
1. AI-generated opportunity specs (`ds-opportunity-architect`) MUST be written **exclusively** to `vault/staging/opportunities/<id>-<slug>.md` with `status: staging`.
2. **NEVER** write AI-discovered specs directly into `vault/opportunities/`.
3. Every staged candidate MUST be audited in place by `skeptical-cfo-critic` (checking point-in-time snapshot leakage, simpler 20-line SQL/DAX alternatives, driver-tree unit economics, `[VERIFY WITH STAKEHOLDER]` prompts, and external data procurement blockers) before human promotion via `/review-staging` or `poopdeck ui`.

### 4.2 Safe-AST Driver-Tree Valuation (`poopdeck/models.py`)
- **Zero Ungrounded Dollar Estimates**: Every `OpportunitySpec` requires an explicit arithmetic `valuation.driver_formula` evaluated via safe `ast.parse` (`+`, `-`, `*`, `/`, unary `+/-`, parentheses, numeric constants, and valid Python-identifier `drivers[].name` variables ONLY — never `**` or function calls).
- `annual_benefit_low_usd`, `annual_benefit_base_usd`, and `annual_benefit_high_usd` MUST match `driver_formula` within `<= 5%` validation tolerance (or omit them on draft load to auto-populate exact AST values).
- Any driver without verified internal evidence MUST set `verified: false` and supply a concrete `verify_question`, generating a `[VERIFY WITH STAKEHOLDER]` prompt.
- Setting `confidence_tier: finance_verified` when any driver has `verified: false` is a hard schema validation error (`directional` or `hypothesis` required).
- **Boss-Mandate Capacity Displacement**: Setting `boss_mandate: true` pins an opportunity to P0 ahead of pure ROI rank against `quarterly_capacity_weeks` (default `12.0` person-weeks) and flags `displaced_by_mandate: true` on higher-ROI items pushed below the quarterly cutline.

---

## 5. Canonical Vault Layout, CLI Reference & Claude Harness

### 5.1 Vault & Extraction Paths
- `vault/datasets/*.md` — Databricks Unity Catalog & Hive Metastore `DatasetCard` entities
- `vault/reports/*.md` — Power BI `ReportCard` entities with DAX heuristic bottleneck flags (`nested_heuristic_tiers`, `naive_moving_avg_forecast`, `static_threshold_alert`, `backward_looking_only`)
- `vault/stakeholders/*.md` — `StakeholderCard` profiles (`exec | manager | partner | sme`)
- `vault/questions/open-questions.md` — 15-question Week 1–2 Discovery Questionnaire (`manager`, `it_security`, `data_engineering`, `power_bi`, `business_finance`)
- `vault/questions/day1-90-playbook.md` — 5-phase Solo DS Execution Playbook (`PHASE-1`..`PHASE-5`)
- `vault/glossary.md` — `GlossaryCard` KPI & business definitions
- `vault/staging/opportunities/*.md` — Staged `OpportunitySpec` cards (`status: staging`) awaiting CFO & human review
- `vault/opportunities/*.md` — Promoted `backlog`, `active`, `blocked`, `shipped`, or `archived` specs
- `vault/inbox/` — Dropzones: `vault/inbox/databricks/`, `vault/inbox/powerbi/`, `vault/inbox/teams/`, `vault/inbox/notes/`, and `vault/inbox/processed/`
- `notebooks/databricks_zero_admin_crawler.py` — Tier 2 zero-admin Databricks metadata/lineage notebook
- `queries/powerbi_dax_info_extract.dax` — Tier 2 DAX Studio `INFO.TABLES()`, `INFO.COLUMNS()`, `INFO.MEASURES()`, `INFO.RELATIONSHIPS()` extractor
- `poopdeck/packs/external_data_catalog.yaml` — Domain lenses (`generic`, `ops_supply_chain`, `retail_cpg`, `finance_risk`, `b2b_saas`, `manufacturing`) & external datasets (`fred_macro`, `us_census_acs_cbp`, `noaa_ghcn_daily`, `public_holidays_global`, `port_congestion_freight_index`, `commodity_spot_indices`, `nielsen_circana_syndicated`, `dnb_firmographics_credit`)

### 5.2 `poopdeck` CLI Commands (`poopdeck <cmd>` or `python -m poopdeck <cmd>`)
- `poopdeck init [--company] [--vault PATH] [--config PATH] [--repo-root PATH] [--skip-hook]`
- `poopdeck sync [all|databricks|powerbi|teams] [--input PATH] [--bundle PATH] [--mode auto|tier1|tier2] [--allow-sample-rows] [--vault PATH] [--config PATH]`
- `poopdeck validate [--vault PATH] [--config PATH] [--strict] [--json]`
- `poopdeck demo [--vault PATH] [--config PATH] [--force]`
- `poopdeck reset [--vault PATH] [--config PATH] [--yes] [--force]`
- `poopdeck ui [--host 127.0.0.1] [--port 8420] [--vault PATH] [--config PATH] [--open-browser|--no-browser]`
- `poopdeck brief [OPP_ID] [--portfolio|--weekly] [--format teams|exec] [--output PATH] [--vault PATH] [--config PATH]`

Always run `poopdeck validate` (or `poopdeck validate --strict`) after creating or modifying vault cards.

### 5.3 Specialist Subagents (`.claude/agents/`) & Slash Commands (`.claude/commands/`)
- **Subagents**: `catalog-cartographer`, `bi-bridge-analyst`, `context-harvester`, `ds-opportunity-architect`, `skeptical-cfo-critic`
- **Slash Commands**: `/onboard-check`, `/sync-workspace`, `/ingest-notes`, `/discover-opportunities`, `/review-staging`, `/boss-override`, `/exec-brief`

### 5.4 Architecture Prohibitions (R5 — Non-Negotiable)
- **Zero Node.js / npm**: Never create `package.json`, `node_modules/`, frontend bundlers, or external CDN `<script>`/`<link>` dependencies.
- **Zero External Databases**: Never introduce Postgres, Redis, SQLite, or background DB servers.
- **Zero M365 Tenant Admin Bots/Webhooks**: Never build automated Teams bots or Graph webhook listeners; rely strictly on local dropzone ingestion (`vault/inbox/`).
- **Zero External Remote Pushes**: Never push `vault/` or repository commits to external/personal remotes.
