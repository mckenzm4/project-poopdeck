# Project Poopdeck

Portable, corporate-safe **Discovery, Valuation, and Portfolio Operating System** starter kit for a solo Data Scientist ramping up in a **Databricks, Power BI, and Microsoft Teams** organization with **Claude Code**.

Project Poopdeck maps lakehouse tables and Power BI semantic models without requiring tenant-admin privileges, links stakeholder pain points to auditable **Driver-Tree ROI** formulas, red-teams AI project proposals through a **Skeptical CFO staging gate**, models **Boss-Mandate quarterly capacity displacement**, and serves a **Zero-Node FastAPI Local Command Center UI** directly over local Markdown files.

---

## Architecture & Two-Layer Corporate DLP Boundary

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ LAYER 1: SHAREABLE ENGINE (Root Git Repo — safe for mckenzm4/project-poopdeck)│
│  poopdeck/  .claude/  CLAUDE.md  notebooks/  queries/  scripts/  tests/      │
│  examples/demo_vault/ (100% synthetic supply-chain & finance seed cards)     │
└──────────────────────────────────────┬───────────────────────────────────────┘
                                       │  Enforced by:
                                       │  1) Root .gitignore (excludes vault/, .env*, *.pbix, *.vpax, exports/)
                                       │  2) scripts/install_dlp_hook.py (.git/hooks/pre-commit DLP blocker)
                                       │  3) Isolated vault/.git repository
                                       ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ LAYER 2: COMPANY-PROPRIETARY VAULT (vault/ — NEVER tracked in root Git)      │
│                                                                              │
│  [Databricks Tier 1 SDK/SQL or Tier 2 JSON/CSV] ──┐                          │
│  [Power BI Tier 1 REST or Tier 2 .pbip/TMDL/INFO.*]┼─► [poopdeck.redaction]  │
│  [Teams .vtt / .docx / .md in vault/inbox/] ──────┘    (safe_aggregates,     │
│                                                         PII & secret scrub)  │
│                                                                 │            │
│                                                                 ▼            │
│   vault/{datasets,reports,stakeholders,questions}/*.md + vault/glossary.md   │
│   (Pydantic YAML frontmatter + [[wikilinks]] + preserved HUMAN-NOTES)        │
│                                                                 │            │
│   [ds-opportunity-architect] ──► vault/staging/opportunities/*.md            │
│                                           │                                  │
│                                  [skeptical-cfo-critic]                      │
│                                           │                                  │
│                     [Human Gate: /review-staging or poopdeck ui]             │
│                                           │                                  │
│                                           ▼                                  │
│                             vault/opportunities/*.md                         │
│                  (Safe-AST Driver-Tree ROI + Boss-Pin Cutline)               │
│                                           │                                  │
│                                           ▼                                  │
│           [Zero-Node FastAPI UI (poopdeck ui) & Briefs (poopdeck brief)]     │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## Day-0 Practice Quickstart (Personal Laptop / Before Day 1)

Practice the entire workflow locally using the included synthetic supply-chain & commercial-finance demo vault (`examples/demo_vault/`):

```powershell
# 1. Install package in editable mode (add [dev,databricks] as needed)
pip install -e ".[dev]"

# 2. Seed vault/ from examples/demo_vault/
poopdeck demo --force

# 3. Validate all schemas, AST driver formulas, HUMAN-NOTES markers & [[wikilinks]]
poopdeck validate --strict

# 4. Inspect a 1-page Exec Pitch or Weekly Portfolio Cutline Brief
poopdeck brief OPP-004 --format exec
poopdeck brief --portfolio

# 5. Launch the Zero-Node Local Command Center UI at http://127.0.0.1:8420
poopdeck ui --no-browser

# 6. Run the 100% offline test suite
python -m pytest
```

---

## Day-1 Corporate Laptop Bootstrap & Three-Layer DLP Shield

When you clone the shareable engine onto your corporate machine on Day 1, bootstrap a clean, isolated company `vault/` with zero demo artifacts:

### Option A: One-Command Day-1 Scripts
- **Windows PowerShell (`scripts/day1_init.ps1`)**:
  ```powershell
  .\scripts\day1_init.ps1
  ```
- **macOS / Linux / Git Bash (`scripts/day1_init.sh`)**:
  ```sh
  sh scripts/day1_init.sh
  ```

### Option B: Direct CLI Commands
```powershell
# Reset any practice demo data and initialize an isolated company vault
poopdeck reset --yes
poopdeck init --company
```

What Day-1 initialization sets up automatically:
1. **Local Config (`config.yaml`)**: Copied from `config.example.yaml` (gitignored so corporate hostnames/workspace IDs stay local).
2. **Pre-Commit DLP Guard (`.git/hooks/pre-commit`)**: Installed via `scripts/install_dlp_hook.py`. Blocks root commits to external/non-corporate remotes (`mckenzm4`, `github.com`, `gitlab.com`, `bitbucket.org`, or repos lacking an explicit `dlp.corporate_remote_patterns` match) whenever staged files touch `vault/`, `.env*` (except `.env.example`), `*.pbix`, `*.vpax`, `exports/`, catalog/table dumps, or credentials (`dapi...`, `ghp_...`, `sk-ant-...`, `AKIA...`, `AccountKey=[REDACTED:SECRET]`, `PWD=[REDACTED:SECRET]`).
3. **Isolated `vault/.git`**: Gives your proprietary company notes independent local Git version history (which you can optionally pair with an internal GitHub Enterprise or Azure DevOps repo via `dlp.corporate_remote_patterns` in `config.yaml`).
4. **Day-1 Seed Bank**: Seeds the 15-question categorized Week 1–2 Discovery Questionnaire (`vault/questions/open-questions.md`), the 5-phase Solo DS Day 1–90 Execution Playbook (`vault/questions/day1-90-playbook.md`), and `vault/glossary.md`.

---

## Dual-Mode Workspace Ingestion & Privacy Gate

Every crawler supports **Tier 1 (Live SDK/REST)** when credentials are available and **Tier 2 (Zero-Admin Dropzone)** on locked-down corporate laptops:

| Source | Tier 1 (Live Automated) | Tier 2 (Zero-Admin Dropzone) | Output Cards |
|---|---|---|---|
| **Databricks** | `databricks-sdk` / SQL Warehouse querying Unity Catalog `system.information_schema`, `system.access.table_lineage`, workflows & 30d query stats (with automatic Hive Metastore `SHOW`/`DESCRIBE` fallback) | Run `notebooks/databricks_zero_admin_crawler.py` inside any Databricks notebook; drop JSON bundle or CSVs (`tables.csv`, `columns.csv`, `lineage.csv`, `profiles.csv`) into `vault/inbox/databricks/` and run `poopdeck sync databricks` | `vault/datasets/<fqn>.md` |
| **Power BI** | Fabric / Power BI REST APIs via `az account get-access-token` (`use_az_cli_token: true`) | Drop `.pbip`, `model.bim`, TMDL folders, or CSV/JSON exports from `queries/powerbi_dax_info_extract.dax` (`INFO.TABLES()`, `INFO.COLUMNS()`, `INFO.MEASURES()`, `INFO.RELATIONSHIPS()`) into `vault/inbox/powerbi/` and run `poopdeck sync powerbi` | `vault/reports/<slug>.md` (flags `nested_heuristic_tiers`, `naive_moving_avg_forecast`, `static_threshold_alert`, `backward_looking_only`) |
| **Microsoft Teams & Notes** | N/A (R5 forbids M365 Tenant Admin bots/webhooks) | Export Teams `.vtt` / `.docx` transcripts or write `.md` 1:1 notes into `vault/inbox/teams/` or `vault/inbox/notes/` and run `poopdeck sync teams` | `vault/stakeholders/<slug>.md`, `vault/glossary.md`, `vault/questions/open-questions.md` |

### Privacy & Non-Destructive Re-Sync Guarantees
- **`safe_aggregates` Default**: `sample_rows` defaults to `[]` unless `privacy.allow_sample_rows: true` (or `--allow-sample-rows`) is explicitly enabled, and PII columns (`email`, `ssn`, `phone`, `dob`, `salary`, `address`, `name`, etc.) are masked to `[REDACTED:PII]`.
- **`HUMAN-NOTES` Preservation**: Every vault card preserves user edits inside `<!-- HUMAN-NOTES-START --> ... <!-- HUMAN-NOTES-END -->` byte-for-byte across automated re-syncs.

---

## Auditable Driver-Tree ROI, Domain Packs & Boss-Pin Simulator

- **Safe-AST Driver-Tree Math**: Opportunity valuations require an explicit `driver_formula` evaluated via Python `ast` (`+`, `-`, `*`, `/`, unary `+/-`, declared driver variables) computing `low` / `base` / `high` annual USD benefits. Unverified drivers (`verified: false`) automatically emit `[VERIFY WITH STAKEHOLDER]` prompts, and `finance_verified` confidence is blocked until 100% of drivers are verified.
- **Weighted Priority Formula**:
  $$\text{Score} = \left(0.40 \cdot \text{ROI} + 0.25 \cdot \text{Readiness} + 0.20 \cdot \text{Adoption} + 0.15 \cdot \text{Speed}\right) \times \text{ConfidenceMultiplier}$$
  Supplementing an existing Power BI report awards `+10.0` adoption bonus points (`existing_pbi_bonus_points`).
- **Domain Lenses & External Data Catalog (`poopdeck/packs/external_data_catalog.yaml`)**: Switch `domain` in `config.yaml` (`ops_supply_chain`, `retail_cpg`, `finance_risk`, `b2b_saas`, `manufacturing`, `generic`) and pair internal tables with curated external sources (`fred_macro`, `us_census_acs_cbp`, `noaa_ghcn_daily`, `public_holidays_global`, `port_congestion_freight_index`, `commodity_spot_indices`, `nielsen_circana_syndicated`, `dnb_firmographics_credit`).
- **Boss-Mandate Capacity Displacement**: Pinning `boss_mandate: true` locks an executive mandate above the `quarterly_capacity_weeks` (`12.0` person-weeks) cutline and computes exact `displaced_ids` so capacity trade-offs with leadership stay factual and objective.

---

## Claude Code Harness, CLI Reference & Local Command Center UI

### 5 Specialist Subagents (`.claude/agents/`)
| Subagent | Responsibility |
|---|---|
| `catalog-cartographer` | Maps Databricks UC/Hive metadata & lineage into `vault/datasets/*.md` and logs DE questions |
| `bi-bridge-analyst` | Maps Power BI `.pbip`/`model.bim`/TMDL/`INFO.*` exports into `vault/reports/*.md` and flags DAX bottlenecks |
| `context-harvester` | Normalizes Teams `.vtt`/`.docx` & notes in `vault/inbox/` into stakeholders, glossary & asks |
| `ds-opportunity-architect` | Drafts candidate `OpportunitySpec` cards strictly in `vault/staging/opportunities/` (`status: staging`) |
| `skeptical-cfo-critic` | Mandatory red-team gate auditing leakage, SQL/DAX simplicity, driver formulas & `[VERIFY WITH STAKEHOLDER]` prompts |

### 7 Workflow Slash Commands (`.claude/commands/`)
| Command | Workflow |
|---|---|
| `/onboard-check` | Audits Day 1–90 phase readiness, vault entity counts, validation status & top open questions |
| `/sync-workspace` | Runs `poopdeck sync`, enriches dataset & report cards via subagents, and runs `poopdeck validate` |
| `/ingest-notes` | Sweeps `vault/inbox/` via `poopdeck sync teams` + `context-harvester` |
| `/discover-opportunities` | Runs `ds-opportunity-architect` -> mandatory `skeptical-cfo-critic` -> `poopdeck validate` |
| `/review-staging` | Interactive human gate to promote (`backlog`) or archive (`archived`) staged specs |
| `/boss-override` | Pins `boss_mandate: true` and exports an objective quarterly capacity displacement summary |
| `/exec-brief` | Generates a 1-page Microsoft Teams or Executive Pitch Markdown brief via `poopdeck brief` |

### `poopdeck` CLI Reference (`poopdeck <cmd>` or `python -m poopdeck <cmd>`)
```text
poopdeck init [--company] [--vault PATH] [--config PATH] [--repo-root PATH] [--skip-hook]
poopdeck sync [all|databricks|powerbi|teams] [--input PATH] [--bundle PATH] [--mode auto|tier1|tier2] [--allow-sample-rows] [--vault PATH] [--config PATH]
poopdeck validate [--vault PATH] [--config PATH] [--strict] [--json]
poopdeck demo [--vault PATH] [--config PATH] [--force]
poopdeck reset [--vault PATH] [--config PATH] [--yes] [--force]
poopdeck ui [--host 127.0.0.1] [--port 8420] [--vault PATH] [--config PATH] [--open-browser|--no-browser]
poopdeck brief [OPP_ID] [--portfolio|--weekly] [--format teams|exec] [--output PATH] [--vault PATH] [--config PATH]
```

### Local Command Center UI (`poopdeck ui`)
100% Python-launched FastAPI + offline vanilla HTML/CSS/SVG/JS single-page workspace featuring:
1. **Value-vs-Effort ROI Bubble Matrix & Boss-Pin Quarterly Capacity Simulator**
2. **Staging Gate & Skeptical CFO Review Inbox** (1-click Promote / Archive writeback to Markdown)
3. **Interactive Dataset-to-Opportunity-to-Power-BI Lineage Graph**
4. **Day 1–90 Readiness & Open Questions Tracker** (status & answer writeback)
5. **1-Click Microsoft Teams & Executive Pitch Brief Exporter**

---

## Architecture Prohibitions (R5 Guardrails)
- **Zero Node.js / npm**: No `package.json`, `node_modules/`, frontend build steps, or external CDN scripts.
- **Zero External Databases**: No Postgres, Redis, or SQLite — Markdown files in `vault/` are the database.
- **Zero M365 Tenant Admin Bots/Webhooks**: No automated Teams bots or Graph webhook listeners requiring IT admin consent.
- **Zero External Remote Pushes**: Never auto-sync or push `vault/` contents to personal/external Git remotes.
