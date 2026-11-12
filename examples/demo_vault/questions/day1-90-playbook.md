---
id: day1-90-playbook
title: Solo DS Day 1-90 Execution Playbook
phases:
- id: PHASE-1
  phase: 'Days 1-5: Wire & Map'
  goal: Zero-friction setup & factual terrain map
  actions:
  - Clone `mckenzm4` engine
  - Run `poopdeck init --company`
  - Drop Zero-Admin Databricks discovery notebook output & top 5 Power BI `INFO.*`
    exports
  - Run `/sync-workspace`
  output: Clean map of Gold/Silver tables, lineage, and top Power BI semantic models;
    prioritized IT/DE question list.
  completed: true
- id: PHASE-2
  phase: 'Days 6-15: Listen & Ground'
  goal: Tie data to real human decisions & dollars
  actions:
  - Hold 30-min 1:1s using the Stakeholder Question Bank
  - Drop Teams transcripts into `vault/inbox/`
  - Run `/ingest-notes` and `/discover-opportunities`
  output: 10–15 CFO-red-teamed candidate projects in `vault/staging/` with explicit
    `[VERIFY]` financial assumptions.
  completed: false
- id: PHASE-3
  phase: 'Days 16-30: Align & Lock Quick Win'
  goal: Co-validate ROI numbers & ship 1st visible win
  actions:
  - Review top 5 candidates + Boss-Pin board with your manager
  - Verify unit economics with business owners
  - Pick 1 Quick Win (<3 weeks) that enriches an existing daily Power BI report
  output: 'Signed-off Q1 DS Backlog + Quick Win #1 in active build.'
  completed: false
- id: PHASE-4
  phase: 'Days 31-60: Ship & Prove Value'
  goal: 'Productionize Quick Win #1 & start Strategic Bet #1'
  actions:
  - Land automated Databricks Workflows scoring table
  - Wire into existing Power BI report
  - Track baseline vs. post-launch KPI
  - Kick off first medium-horizon project
  output: Live production predictions in Power BI; documented dollar impact; earned
    trust.
  completed: false
- id: PHASE-5
  phase: 'Days 61-90: Institutionalize'
  goal: Move from ad-hoc requests to portfolio governance
  actions:
  - Run monthly `/sync-workspace` + `/discover-opportunities` refresh
  - Review Portfolio & Capacity Simulator biweekly with boss to manage incoming asks
    against roadmap ROI
  output: Repeatable solo-DS operating cadence & Q2 roadmap.
  completed: false
updated_at: 2026-11-11
---

# Solo DS Day 1–90 Execution Playbook

| ID | Phase | Primary Goal | Tangible Output | Status |
|---|---|---|---|---|
| `PHASE-1` | **Days 1-5: Wire & Map** | Zero-friction setup & factual terrain map | Clean map of Gold/Silver tables, lineage, and top Power BI semantic models; prioritized IT/DE question list. | Completed |
| `PHASE-2` | **Days 6-15: Listen & Ground** | Tie data to real human decisions & dollars | 10–15 CFO-red-teamed candidate projects in `vault/staging/` with explicit `[VERIFY]` financial assumptions. | Pending |
| `PHASE-3` | **Days 16-30: Align & Lock Quick Win** | Co-validate ROI numbers & ship 1st visible win | Signed-off Q1 DS Backlog + Quick Win #1 in active build. | Pending |
| `PHASE-4` | **Days 31-60: Ship & Prove Value** | Productionize Quick Win #1 & start Strategic Bet #1 | Live production predictions in Power BI; documented dollar impact; earned trust. | Pending |
| `PHASE-5` | **Days 61-90: Institutionalize** | Move from ad-hoc requests to portfolio governance | Repeatable solo-DS operating cadence & Q2 roadmap. | Pending |

## [x] PHASE-1 — Days 1-5: Wire & Map
- **Goal**: Zero-friction setup & factual terrain map
- **Target Output**: Clean map of Gold/Silver tables, lineage, and top Power BI semantic models; prioritized IT/DE question list.
  - [x] Clone `mckenzm4` engine
  - [x] Run `poopdeck init --company`
  - [x] Drop Zero-Admin Databricks discovery notebook output & top 5 Power BI `INFO.*` exports
  - [x] Run `/sync-workspace`

## [ ] PHASE-2 — Days 6-15: Listen & Ground
- **Goal**: Tie data to real human decisions & dollars
- **Target Output**: 10–15 CFO-red-teamed candidate projects in `vault/staging/` with explicit `[VERIFY]` financial assumptions.
  - [ ] Hold 30-min 1:1s using the Stakeholder Question Bank
  - [ ] Drop Teams transcripts into `vault/inbox/`
  - [ ] Run `/ingest-notes` and `/discover-opportunities`

## [ ] PHASE-3 — Days 16-30: Align & Lock Quick Win
- **Goal**: Co-validate ROI numbers & ship 1st visible win
- **Target Output**: Signed-off Q1 DS Backlog + Quick Win #1 in active build.
  - [ ] Review top 5 candidates + Boss-Pin board with your manager
  - [ ] Verify unit economics with business owners
  - [ ] Pick 1 Quick Win (<3 weeks) that enriches an existing daily Power BI report

## [ ] PHASE-4 — Days 31-60: Ship & Prove Value
- **Goal**: Productionize Quick Win #1 & start Strategic Bet #1
- **Target Output**: Live production predictions in Power BI; documented dollar impact; earned trust.
  - [ ] Land automated Databricks Workflows scoring table
  - [ ] Wire into existing Power BI report
  - [ ] Track baseline vs. post-launch KPI
  - [ ] Kick off first medium-horizon project

## [ ] PHASE-5 — Days 61-90: Institutionalize
- **Goal**: Move from ad-hoc requests to portfolio governance
- **Target Output**: Repeatable solo-DS operating cadence & Q2 roadmap.
  - [ ] Run monthly `/sync-workspace` + `/discover-opportunities` refresh
  - [ ] Review Portfolio & Capacity Simulator biweekly with boss to manage incoming asks against roadmap ROI

<!-- HUMAN-NOTES-START -->
## Human Notes
- `PHASE-1` completed ahead of schedule; currently executing `PHASE-2` stakeholder verification sessions.
<!-- HUMAN-NOTES-END -->
