---
description: "Sweep Microsoft Teams .vtt/.docx transcripts and 1:1 notes in vault/inbox/ into stakeholder cards, glossary terms, and linked pain points."
argument-hint: "[--input PATH]"
allowed-tools: "Read, Glob, Grep, Edit(vault/**), Write(vault/**), Bash(poopdeck *), Bash(python -m poopdeck *)"
---

# `/ingest-notes` — Microsoft Teams & 1:1 Context Dropzone Sweep

Normalize local `.vtt` meeting transcripts, `.docx` documents, and `.md`/`.txt` 1:1 notes from `vault/inbox/` (or an explicit `--input PATH` passed via `$ARGUMENTS`) without M365 Tenant Admin bots or Graph webhooks.

## Execution Steps
1. **Inspect Dropzone (`vault/inbox/`)**:
   - Check `vault/inbox/`, `vault/inbox/teams/`, and `vault/inbox/notes/` (or the `--input` path supplied in `$ARGUMENTS`) for unprocessed `.vtt`, `.docx`, `.md`, or `.txt` files.
   - If no unprocessed files are present, report a clean no-op with zero mutations and remind the user where to drop Teams `.vtt`/`.docx` transcripts (`vault/inbox/teams/`) and 1:1 notes (`vault/inbox/notes/`).
2. **Run Privacy-Redacted CLI Sync**:
   - Execute `poopdeck sync teams $ARGUMENTS` so `poopdeck/redaction.py` scrubs inline emails (`[REDACTED:EMAIL]`), SSNs (`[REDACTED:SSN]`), phone numbers (`[REDACTED:PHONE]`), and credentials (`[REDACTED:SECRET]`) and archives raw dropzone files under `vault/inbox/processed/`.
3. **Delegate to `context-harvester`**:
   - Invoke `context-harvester` to enrich `vault/stakeholders/*.md` (`influence_tier`, `decision_appetite`, `owned_reports`, `kpis_cared_about`, `pain_points`, `explicit_asks`, `boss_mandate_asks`), `vault/glossary.md`, and `vault/questions/open-questions.md`.
   - Ensure all mentioned datasets and Power BI reports use valid `[[wikilinks]]` and **preserve `<!-- HUMAN-NOTES-START --> ... <!-- HUMAN-NOTES-END -->` byte-for-byte**.
4. **Validate & Summarize**:
   - Run `poopdeck validate` and summarize updated stakeholders, newly captured KPI glossary definitions, answered questions, and any `boss_mandate_asks` requiring `/boss-override` review.
