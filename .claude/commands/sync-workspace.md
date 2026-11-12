---
description: "Run dual-mode Databricks and Power BI ingestion, enrich dataset and report cards via specialist subagents, and validate the vault."
argument-hint: "[all|databricks|powerbi|teams] [--input PATH] [--mode auto|tier1|tier2]"
allowed-tools: "Read, Glob, Grep, Edit(vault/**), Write(vault/**), Bash(poopdeck *), Bash(python -m poopdeck *)"
---

# `/sync-workspace` — Dual-Mode Workspace Ingestion & Enrichment

Synchronize Databricks, Power BI, and/or Microsoft Teams metadata into `vault/` using user arguments `$ARGUMENTS` (default target: `all`).

## Execution Steps
1. **Run `poopdeck sync`**:
   - Parse `$ARGUMENTS` and execute `poopdeck sync` using only canonical CLI flags (`[all|databricks|powerbi|teams]`, `--input PATH`, `--bundle PATH`, `--mode auto|tier1|tier2`, `--allow-sample-rows`, `--vault PATH`, `--config PATH`).
   - By default, keep `safe_aggregates` privacy mode active (`sample_rows: []`, PII columns masked `[REDACTED:PII]`).
2. **Tier 2 Zero-Admin Fallback Guidance (When Credentials & Dropzones Are Empty)**:
   - If live Tier 1 SDK/REST credentials are unavailable and `vault/inbox/databricks/` or `vault/inbox/powerbi/` contains no local bundles yet, print clear step-by-step Tier 2 instructions:
     - **Databricks**: Run `notebooks/databricks_zero_admin_crawler.py` inside a Databricks workspace notebook and drop the JSON discovery bundle (or `tables.csv`, `columns.csv`, `lineage.csv`, `profiles.csv`) into `vault/inbox/databricks/`, then run `poopdeck sync databricks`.
     - **Power BI**: Drop `.pbip` / `model.bim` / TMDL files or CSV/JSON exports from `queries/powerbi_dax_info_extract.dax` (`INFO.TABLES()`, `INFO.COLUMNS()`, `INFO.MEASURES()`, `INFO.RELATIONSHIPS()`) into `vault/inbox/powerbi/`, then run `poopdeck sync powerbi`.
3. **Delegate Specialist Subagent Enrichment**:
   - Invoke `catalog-cartographer` on synced `vault/datasets/*.md` cards to refine table grain, join keys, `[[wikilinks]]`, and `data_engineering` questions in `vault/questions/open-questions.md` while preserving `<!-- HUMAN-NOTES-START --> ... <!-- HUMAN-NOTES-END -->` byte-for-byte.
   - Invoke `bi-bridge-analyst` on synced `vault/reports/*.md` cards to verify upstream dataset `[[wikilinks]]`, review DAX heuristic flags (`nested_heuristic_tiers`, `naive_moving_avg_forecast`, `static_threshold_alert`, `backward_looking_only`), and log `power_bi` open questions while preserving `HUMAN-NOTES`.
4. **Final Validation**:
   - Run `poopdeck validate` and summarize newly created/updated dataset cards, report cards, flagged DAX bottlenecks, and open questions.
