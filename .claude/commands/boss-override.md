---
description: "Pin an urgent manager priority as boss_mandate: true and generate a quarterly person-week capacity displacement summary."
argument-hint: "<OPP-ID or urgent boss ask>"
allowed-tools: "Read, Glob, Grep, Edit(vault/**), Write(vault/**), Write(exports/**), Bash(poopdeck *), Bash(python -m poopdeck *)"
---

# `/boss-override` — Boss-Mandate P0 Pinning & Quarterly Capacity Displacement

Pin an existing opportunity or capture a net-new urgent executive/manager mandate with `boss_mandate: true` and compute objective person-week cutline displacement against `quarterly_capacity_weeks`. Target input: `$ARGUMENTS`.

## Execution Steps
1. **Handle Empty `$ARGUMENTS` Safely**:
   - If `$ARGUMENTS` is empty, list all opportunities in `vault/opportunities/*.md` (and `vault/staging/opportunities/*.md`) showing `id`, `title`, `status`, `effort_weeks`, `computed_priority_score`, and `boss_mandate`, and ask the user which `OPP-ID` or urgent ask to pin — without mutating any files.
2. **Apply `boss_mandate: true` (`poopdeck.vault_io.set_boss_mandate`)**:
   - **Existing `OPP-ID`**: If the card is still in `vault/staging/opportunities/`, promote it to `vault/opportunities/` (`status: "backlog"`) first; then set `boss_mandate: true` while preserving `<!-- HUMAN-NOTES-START --> ... <!-- HUMAN-NOTES-END -->` byte-for-byte.
   - **Net-New Urgent Boss Ask**: Create a schema-valid `OpportunitySpec` in `vault/opportunities/<OPP-ID>-<slug>.md` with `status: "backlog"` (or `"active"`), `boss_mandate: true`, `confidence_tier: "hypothesis"` (or `"directional"`), explicit safe-AST `driver_formula`, `verified: false` drivers carrying `[VERIFY WITH STAKEHOLDER]` prompts, valid `[[wikilinks]]`, and balanced `HUMAN-NOTES` markers.
3. **Validate & Compute Quarterly Cutline Displacement**:
   - Run `poopdeck validate` to confirm schema and formula integrity.
   - Run `poopdeck brief --portfolio` (evaluating `compute_portfolio_schedule` against `quarterly_capacity_weeks`, default `12.0` person-weeks) to compare baseline ROI-ranked cutline vs. mandate-pinned cutline.
4. **Present Objective Trade-Off Summary**:
   - Output a concise, non-defensive Microsoft Teams update showing `capacity_weeks`, `mandate_weeks`, `used_capacity_weeks`, `funded_ids`, and exact `displaced_ids` (`displaced_by_mandate: true` items pushed into next quarter, including their base annual USD benefit) so leadership can confirm the capacity trade-off transparently.
