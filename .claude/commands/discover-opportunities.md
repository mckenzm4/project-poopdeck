---
description: "Generate candidate DS Opportunity Specs in vault/staging/opportunities/ and run the mandatory Skeptical CFO red-team critique."
argument-hint: "[domain-or-report-focus]"
allowed-tools: "Read, Glob, Grep, Edit(vault/**), Write(vault/**), Bash(poopdeck *), Bash(python -m poopdeck *)"
---

# `/discover-opportunities` — Two-Stage DS Opportunity Discovery & Skeptical CFO Gate

Synthesize candidate Data Science opportunities grounded in `vault/` entities and `poopdeck/packs/external_data_catalog.yaml`, optionally scoped by `$ARGUMENTS` (such as a target domain lens, stakeholder, or Power BI report).

## Mandatory Three-Stage Pipeline (Never Skip Stage 2)
1. **Stage 1 — Candidate Synthesis (`ds-opportunity-architect`)**:
   - Invoke `ds-opportunity-architect` over `vault/datasets/*.md`, `vault/reports/*.md`, `vault/stakeholders/*.md`, `vault/glossary.md`, `config.yaml` / `config.example.yaml`, and `poopdeck/packs/external_data_catalog.yaml`.
   - Write candidate `OpportunitySpec` cards **STRICTLY** into `vault/staging/opportunities/<OPP-ID>-<slug>.md` with `status: "staging"`, safe-AST `driver_formula`, `drivers[]` (`verified: false` + `verify_question` on unverified inputs), Power BI delivery surface (`existing_reports_supplemented` prioritized), valid `[[wikilinks]]`, and balanced `<!-- HUMAN-NOTES-START --> ... <!-- HUMAN-NOTES-END -->` markers.
   - **NEVER** write candidate cards directly into `vault/opportunities/`. If the vault is sparse, draft `hypothesis`-tier candidates with explicit `[VERIFY WITH STAKEHOLDER]` prompts.
2. **Stage 2 — Mandatory Adversarial Red-Team (`skeptical-cfo-critic`)**:
   - Immediately invoke `skeptical-cfo-critic` on every newly created or updated file in `vault/staging/opportunities/*.md` before returning control to the user.
   - Verify point-in-time snapshot leakage risks, challenge whether a 20-line SQL/DAX baseline suffices (`SIMPLIFY_TO_SQL_DAX`), audit `driver_formula` math and `[VERIFY WITH STAKEHOLDER]` prompts, flag external data procurement blockers, and stamp `cfo_critique_verdict`, `cfo_critique_notes`, and `## Skeptical CFO Critique` while preserving `HUMAN-NOTES`.
3. **Stage 3 — Vault Validation & Review Table**:
   - Run `poopdeck validate` (and confirm zero schema errors).
   - Present a ranked summary table of staged candidates showing `id`, `title`, `annual_benefit_{low,base,high}_usd`, `effort_weeks`, `computed_priority_score`, `cfo_critique_verdict`, and `[VERIFY WITH STAKEHOLDER]` prompt count, then direct the user to `/review-staging` or `poopdeck ui`.
