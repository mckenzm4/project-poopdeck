---
description: "Walk through staged DS opportunities in vault/staging/opportunities/ to promote winners to backlog or archive rejected proposals."
argument-hint: "[OPP-ID]"
allowed-tools: "Read, Glob, Grep, Edit(vault/**), Write(vault/**), Bash(poopdeck *), Bash(python -m poopdeck *)"
---

# `/review-staging` — Interactive Human Gate Over `vault/staging/opportunities/`

Guide the human Data Scientist through staged candidate specs in `vault/staging/opportunities/*.md` (filtered to `$ARGUMENTS` when a specific `OPP-ID` is provided).

## Execution Steps
1. **Load Staged Candidates**:
   - Inspect `vault/staging/opportunities/*.md` (matching `$ARGUMENTS` if non-empty).
   - If `vault/staging/opportunities/` is empty, report cleanly with zero file changes and suggest running `/discover-opportunities`.
2. **Present Candidate Dossier & CFO Warnings**:
   - For each staged candidate, summarize:
     - Internal datasets (`internal_datasets`), external data & `blocker_severity` (`external_data_required`), and Power BI delivery surface (`existing_reports_supplemented` vs. `new_reports_unlocked`).
     - Safe-AST `driver_formula`, `low` / `base` / `high` annual USD benefit, `effort_weeks`, and `computed_priority_score`.
     - `cfo_critique_verdict` (`READY_FOR_REVIEW`, `NEEDS_VERIFICATION`, or `SIMPLIFY_TO_SQL_DAX`), `## Skeptical CFO Critique` notes, and all `[VERIFY WITH STAKEHOLDER]` prompts.
   - Explicitly warn the human reviewer before promoting any candidate that still carries `high` external data blockers, `SIMPLIFY_TO_SQL_DAX`, or unresolved `[VERIFY WITH STAKEHOLDER]` unit-economics questions.
3. **Execute Human Gate Decision (Preserving `HUMAN-NOTES` & CFO Critique)**:
   - **Promote to Backlog**: Move the card from `vault/staging/opportunities/<file>.md` to `vault/opportunities/<file>.md` with `status: "backlog"` (`poopdeck.vault_io.promote_opportunity`), preserving `<!-- HUMAN-NOTES-START --> ... <!-- HUMAN-NOTES-END -->` and `## Skeptical CFO Critique` byte-for-byte.
   - **Archive**: Move the card to `vault/opportunities/<file>.md` with `status: "archived"` (`poopdeck.vault_io.archive_opportunity`), preserving `HUMAN-NOTES` and recording the archive rationale.
   - **Keep / Revise in Staging**: Update drivers or notes in place inside `vault/staging/opportunities/` keeping `status: "staging"`.
4. **Validate**:
   - Run `poopdeck validate` after applying decisions and confirm zero validation errors.
