"""Clean Day-1 seed templates for Week 1-2 Discovery Questions, Day 1-90 Playbook, and Glossary."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from poopdeck.models import (
    GlossaryCard,
    OpenQuestionBank,
    PlaybookPhase,
    PlaybookTracker,
    QuestionItem,
)


def build_default_question_bank(today: date | None = None) -> OpenQuestionBank:
    """Return the canonical 15-question Week 1-2 Discovery Questionnaire across 5 categories."""
    stamp = today or date.today()
    questions = [
        # A. For Your Manager (Mandate, Air Cover & Political Terrain)
        QuestionItem(
            id="Q-MGR-01",
            category="manager",
            who_to_ask="Manager",
            topic="Success Horizon",
            question="In 90 days and in 12 months, what concrete outcome makes hiring a Data Scientist an obvious win to leadership?",
            status="open",
            phase="days_1_5",
        ),
        QuestionItem(
            id="Q-MGR-02",
            category="manager",
            who_to_ask="Manager",
            topic="Existing Commitments",
            question="Are there projects already promised to executives before I arrived (`Boss Mandates`), or is the slate genuinely open for ROI-ranked discovery?",
            status="open",
            phase="days_1_5",
        ),
        QuestionItem(
            id="Q-MGR-03",
            category="manager",
            who_to_ask="Manager",
            topic="Decision Appetite",
            question="Which business leaders actually change operations based on data today, and which teams ignore dashboards?",
            status="open",
            phase="days_6_15",
        ),
        QuestionItem(
            id="Q-MGR-04",
            category="manager",
            who_to_ask="Manager",
            topic="Budget Envelope",
            question="Do we have budget or appetite for external data APIs/vendors if ROI justifies it, or should Q1 stick strictly to internal + free public data (NOAA, Census, FRED)?",
            status="open",
            phase="days_6_15",
        ),
        # B. For IT / Security / Platform Admin (Tooling & Guardrails)
        QuestionItem(
            id="Q-IT-01",
            category="it_security",
            who_to_ask="IT / Security / Platform Admin",
            topic="Claude Code Hosting & Policy",
            question="Is Claude Code routed through Anthropic Enterprise, AWS Bedrock, or Azure/Vertex, and what is our written policy on sending schema metadata vs. aggregate stats vs. sample rows?",
            status="open",
            phase="days_1_5",
        ),
        QuestionItem(
            id="Q-IT-02",
            category="it_security",
            who_to_ask="IT / Security / Platform Admin",
            topic="Databricks Local Auth",
            question="Are Personal Access Tokens (PATs) or `databricks auth login` OAuth enabled for local IDE/CLI connections, and do we have Unity Catalog `system.information_schema` & `system.access.table_lineage` enabled?",
            status="open",
            phase="days_1_5",
        ),
        QuestionItem(
            id="Q-IT-03",
            category="it_security",
            who_to_ask="IT / Security / Platform Admin",
            topic="Git & Package Proxies",
            question="Where do internal DS repos live (GitHub Enterprise, Azure DevOps, GitLab), and is there a corporate SSL root cert / Artifactory mirror needed for `pip` / `uv`?",
            status="open",
            phase="days_1_5",
        ),
        # C. For Data Engineering (Lakehouse Reality & Production Path)
        QuestionItem(
            id="Q-DE-01",
            category="data_engineering",
            who_to_ask="Data Engineering",
            topic="Trustworthy Gold vs. Trap Tables",
            question="Which catalogs/schemas are production-governed SLAs vs. stale migration leftovers or analyst sandboxes?",
            status="open",
            phase="days_1_5",
        ),
        QuestionItem(
            id="Q-DE-02",
            category="data_engineering",
            who_to_ask="Data Engineering",
            topic="Historical Point-in-Time Snapshots",
            question="Where do we have true SCD Type 2 / daily snapshot history (safe for ML training without time-travel leakage) vs. overwritten current-state tables?",
            status="open",
            phase="days_6_15",
        ),
        QuestionItem(
            id="Q-DE-03",
            category="data_engineering",
            who_to_ask="Data Engineering",
            topic="Path to Production",
            question="When I build a batch scoring pipeline (e.g., daily Gold predictions table), do Data Scientists own scheduling Databricks Workflows directly, or hand off PRs to Data Engineering?",
            status="open",
            phase="days_6_15",
        ),
        # D. For Power BI / Analytics Owners (Delivery & Adoption)
        QuestionItem(
            id="Q-PBI-01",
            category="power_bi",
            who_to_ask="Power BI / Analytics Owners",
            topic="Top 10 High-Traffic Reports",
            question="Which 5–10 Power BI reports have the highest daily executive/operational viewership (Usage Metrics)?",
            status="open",
            phase="days_1_5",
        ),
        QuestionItem(
            id="Q-PBI-02",
            category="power_bi",
            who_to_ask="Power BI / Analytics Owners",
            topic="The 'Export to Excel' Signal",
            question="Which Power BI tables do business users constantly export to Excel every Monday to run manual formulas, adjustments, or what-if guesses?",
            status="open",
            phase="days_6_15",
        ),
        QuestionItem(
            id="Q-PBI-03",
            category="power_bi",
            who_to_ask="Power BI / Analytics Owners",
            topic="Semantic Model Governance",
            question="Are semantic models managed via `.pbip`/Git or published `.pbix` files, and what is the process to add a new Databricks Gold prediction table into an existing certified dataset?",
            status="open",
            phase="days_6_15",
        ),
        # E. For Business Unit Leads & Finance (Grounding the Dollar Formula)
        QuestionItem(
            id="Q-FIN-01",
            category="business_finance",
            who_to_ask="Business Unit Leads & Finance",
            topic="Action Lever",
            question="If you knew 2 weeks earlier which [customer / SKU / order / invoice / asset] would fail or spike, what specific action would your team take, and how many could they realistically act on per week?",
            status="open",
            phase="days_6_15",
        ),
        QuestionItem(
            id="Q-FIN-02",
            category="business_finance",
            who_to_ask="Business Unit Leads & Finance",
            topic="Unit Economics",
            question="When Finance calculates the dollar cost of a [churned account / stockout / late shipment / write-off / markdown], what standard unit margin or cost figure do they trust?",
            status="open",
            phase="days_16_30",
        ),
    ]
    return OpenQuestionBank(
        id="open-questions",
        title="Week 1-2 Discovery Questionnaire & Open Questions",
        questions=questions,
        updated_at=stamp,
    )


def build_default_playbook(today: date | None = None) -> PlaybookTracker:
    """Return the canonical 5-phase Day 1-90 Solo DS Execution Playbook."""
    stamp = today or date.today()
    phases = [
        PlaybookPhase(
            id="PHASE-1",
            phase="Days 1-5: Wire & Map",
            goal="Zero-friction setup & factual terrain map",
            actions=[
                "Clone `mckenzm4` engine",
                "Run `poopdeck init --company`",
                "Drop Zero-Admin Databricks discovery notebook output & top 5 Power BI `INFO.*` exports",
                "Run `/sync-workspace`",
            ],
            output="Clean map of Gold/Silver tables, lineage, and top Power BI semantic models; prioritized IT/DE question list.",
            completed=False,
        ),
        PlaybookPhase(
            id="PHASE-2",
            phase="Days 6-15: Listen & Ground",
            goal="Tie data to real human decisions & dollars",
            actions=[
                "Hold 30-min 1:1s using the Stakeholder Question Bank",
                "Drop Teams transcripts into `vault/inbox/`",
                "Run `/ingest-notes` and `/discover-opportunities`",
            ],
            output="10–15 CFO-red-teamed candidate projects in `vault/staging/` with explicit `[VERIFY]` financial assumptions.",
            completed=False,
        ),
        PlaybookPhase(
            id="PHASE-3",
            phase="Days 16-30: Align & Lock Quick Win",
            goal="Co-validate ROI numbers & ship 1st visible win",
            actions=[
                "Review top 5 candidates + Boss-Pin board with your manager",
                "Verify unit economics with business owners",
                "Pick 1 Quick Win (<3 weeks) that enriches an existing daily Power BI report",
            ],
            output="Signed-off Q1 DS Backlog + Quick Win #1 in active build.",
            completed=False,
        ),
        PlaybookPhase(
            id="PHASE-4",
            phase="Days 31-60: Ship & Prove Value",
            goal="Productionize Quick Win #1 & start Strategic Bet #1",
            actions=[
                "Land automated Databricks Workflows scoring table",
                "Wire into existing Power BI report",
                "Track baseline vs. post-launch KPI",
                "Kick off first medium-horizon project",
            ],
            output="Live production predictions in Power BI; documented dollar impact; earned trust.",
            completed=False,
        ),
        PlaybookPhase(
            id="PHASE-5",
            phase="Days 61-90: Institutionalize",
            goal="Move from ad-hoc requests to portfolio governance",
            actions=[
                "Run monthly `/sync-workspace` + `/discover-opportunities` refresh",
                "Review Portfolio & Capacity Simulator biweekly with boss to manage incoming asks against roadmap ROI",
            ],
            output="Repeatable solo-DS operating cadence & Q2 roadmap.",
            completed=False,
        ),
    ]
    return PlaybookTracker(
        id="day1-90-playbook",
        title="Solo DS Day 1-90 Execution Playbook",
        phases=phases,
        updated_at=stamp,
    )


def build_default_glossary(today: date | None = None) -> GlossaryCard:
    """Return a clean Day-1 starter GlossaryCard with zero proprietary terms."""
    stamp = today or date.today()
    return GlossaryCard(
        id="glossary",
        title="Business & KPI Glossary",
        terms=[],
        updated_at=stamp,
    )


def seed_vault_templates(
    vault_dir: Path,
    overwrite: bool = False,
    today: date | None = None,
) -> list[Path]:
    """Write open-questions.md, day1-90-playbook.md, and glossary.md if missing (or if overwrite=True)."""
    from poopdeck.vault_io import (
        render_glossary_body,
        render_playbook_body,
        render_questions_body,
        upsert_markdown_card,
    )

    written: list[Path] = []
    questions_path = vault_dir / "questions" / "open-questions.md"
    playbook_path = vault_dir / "questions" / "day1-90-playbook.md"
    glossary_path = vault_dir / "glossary.md"

    if overwrite or not questions_path.exists():
        bank = build_default_question_bank(today=today)
        upsert_markdown_card(questions_path, bank, render_questions_body(bank))
        written.append(questions_path)

    if overwrite or not playbook_path.exists():
        tracker = build_default_playbook(today=today)
        upsert_markdown_card(playbook_path, tracker, render_playbook_body(tracker))
        written.append(playbook_path)

    if overwrite or not glossary_path.exists():
        glossary = build_default_glossary(today=today)
        upsert_markdown_card(glossary_path, glossary, render_glossary_body(glossary))
        written.append(glossary_path)

    return written
