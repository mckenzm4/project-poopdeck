---
description: "Export a 1-page Microsoft Teams or Executive Pitch Markdown brief for an opportunity or the weekly portfolio."
argument-hint: "<OPP-ID | --portfolio> [--format teams|exec] [--output PATH]"
allowed-tools: "Read, Glob, Grep, Write(exports/**), Bash(poopdeck *), Bash(python -m poopdeck *)"
---

# `/exec-brief` — 1-Page Microsoft Teams & Executive Pitch Brief Generator

Render a stakeholder-ready 1-page Markdown brief from `vault/` using user arguments `$ARGUMENTS`.

## Execution Steps
1. **Validate Target Arguments (`$ARGUMENTS`)**:
   - If `$ARGUMENTS` is empty or names an unknown `OPP-ID`, scan `vault/opportunities/*.md` and `vault/staging/opportunities/*.md`, display all available `OPP-*` IDs with titles and statuses, and ask the user which brief to generate without printing stack traces.
2. **Generate Brief via `poopdeck brief`**:
   - Execute `poopdeck brief $ARGUMENTS` using canonical CLI flags (`[OPP_ID]`, `--portfolio` or `--weekly`, `--format teams|exec`, `--output PATH`, `--vault PATH`, `--config PATH`).
   - When saving to disk (`--output`), write strictly inside gitignored `exports/` (for example `exports/<OPP-ID>-brief.md`).
3. **Required Brief Structure**:
   - **Opportunity Brief (`OPP_ID`, `--format teams|exec`)**:
     1. Value Proposition, `driver_formula`, and `low` / `base` / `high` annual USD range.
     2. `[VERIFY WITH STAKEHOLDER]` assumptions requiring business/finance confirmation.
     3. Internal/External Data Footprint & Power BI Delivery Surface (existing report enriched & DAX bottleneck addressed vs. net-new report unlocked).
     4. 4-Week Shadow Pilot Steps & Key Stakeholders.
   - **Portfolio Brief (`--portfolio` or `--weekly`)**:
     - Funded above-cutline roadmap vs. `quarterly_capacity_weeks`, `boss_mandate` P0 pins, `displaced_by_mandate` items pushed past the cutline, unfunded backlog, and staged CFO queue.
