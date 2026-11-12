---
description: "Audit Week 1-4 and Day 1-90 onboarding readiness, vault health, and top unanswered stakeholder questions."
argument-hint: "[phase-or-category]"
allowed-tools: "Read, Glob, Grep, Bash(poopdeck *), Bash(python -m poopdeck *)"
---

# `/onboard-check` — Day 1–90 Readiness & Open Questions Audit

Evaluate the current Project Poopdeck onboarding state, vault schema health, and highest-priority counterpart questions. Optional filter from user: `$ARGUMENTS`.

## Execution Steps
1. **Check Configuration & Vault Presence**:
   - Read `config.yaml` if present, falling back to `config.example.yaml` (`domain`, `quarterly_capacity_weeks`, `privacy.privacy_mode`).
   - Check whether `vault/` exists. If `vault/` is missing or uninitialized, do **not** error — instruct the user cleanly to run:
     - `poopdeck init --company` (or `.\scripts\day1_init.ps1` / `sh scripts/day1_init.sh`) on a corporate laptop, OR
     - `poopdeck demo` for Day-0 synthetic practice.
2. **Validate Vault Integrity**:
   - Run `poopdeck validate` (or `python -m poopdeck validate`) and report validation status, errors, and warnings.
3. **Inventory Mapped Entities & Day 1–90 Playbook Progress**:
   - Count cards across `vault/datasets/*.md`, `vault/reports/*.md`, `vault/stakeholders/*.md`, `vault/staging/opportunities/*.md`, `vault/opportunities/*.md`, and `vault/glossary.md`.
   - Inspect `vault/questions/day1-90-playbook.md` (`PHASE-1` through `PHASE-5`) and identify completed phases vs. the active focus phase.
4. **Surface Prioritized Open Questions**:
   - Read `vault/questions/open-questions.md` (`manager`, `it_security`, `data_engineering`, `power_bi`, `business_finance`).
   - If `$ARGUMENTS` is non-empty, filter the output to the requested phase (e.g., `PHASE-1`, `days_1_5`) or counterpart category (e.g., `data_engineering`, `power_bi`, `business_finance`).
   - Otherwise, display a concise readiness scorecard plus the top open (`status: open` or `blocked`) questions grouped by `who_to_ask` for this week's 1:1 meetings.
