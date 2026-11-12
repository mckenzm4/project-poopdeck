"""1-Page Executive & Microsoft Teams Brief Generator."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from poopdeck.models import (
    OpportunitySpec,
    ReportCard,
    compute_portfolio_schedule,
)
from poopdeck.vault_io import (
    _slugify_key,
    load_all_opportunities,
    load_questions_and_playbook,
    normalize_wikilink_target,
    parse_markdown_card,
)


class BriefResult(BaseModel):
    """Rendered executive or Microsoft Teams brief payload."""

    model_config = ConfigDict(allow_inf_nan=False, validate_assignment=True)

    opp_id: str | None = None
    brief_type: Literal["opportunity", "weekly_status", "verify_questions"] | str
    title: str
    markdown: str
    saved_path: str | None = None


def _find_opp_or_raise(vault_dir: Path, opp_id: str) -> OpportunitySpec:
    needle = opp_id.strip().upper()
    for _, opp, _ in load_all_opportunities(
        vault_dir, include_staging=True, include_archived=True
    ):
        if opp.id.strip().upper() == needle:
            return opp
    raise KeyError(f"Opportunity '{opp_id}' not found in vault")


def _lookup_supplemented_report_gaps(
    vault_dir: Path, opp: OpportunitySpec
) -> list[str]:
    rep_dir = Path(vault_dir) / "reports"
    if not rep_dir.is_dir() or not opp.existing_reports_supplemented:
        return []

    target_keys: set[str] = set()
    for sup in opp.existing_reports_supplemented:
        if sup.ref:
            norm = normalize_wikilink_target(sup.ref)
            if norm:
                target_keys.add(norm.casefold())
                slug = _slugify_key(norm)
                if slug:
                    target_keys.add(slug)

    gaps: list[str] = []
    for md_path in sorted(rep_dir.glob("*.md")):
        try:
            parsed = parse_markdown_card(md_path)
            rcard = ReportCard.model_validate(parsed.frontmatter)
        except Exception:
            continue
        aliases: set[str] = set()
        for raw_alias in (
            rcard.id,
            rcard.title,
            rcard.semantic_model,
            md_path.stem,
            f"reports/{md_path.stem}",
            f"{rcard.workspace}__{rcard.title}",
        ):
            if raw_alias and raw_alias.strip():
                aliases.add(raw_alias.strip().casefold())
                a_slug = _slugify_key(raw_alias)
                if a_slug:
                    aliases.add(a_slug)
        if target_keys & aliases:
            for g in rcard.bi_to_ds_gaps:
                gaps.append(f"`{rcard.title}`: {g}")
            for m in rcard.measures:
                if m.flagged_heuristics:
                    flags = ", ".join(m.flagged_heuristics)
                    gaps.append(
                        f"`{rcard.title}` measure `{m.name}` flagged: `{flags}`"
                    )
    return gaps


def generate_opportunity_brief(
    vault_dir: Path,
    opp_id: str,
    fmt: str = "teams",
) -> BriefResult:
    """Generate a 1-page Microsoft Teams or Executive Pitch brief for a single Opportunity."""
    vdir = Path(vault_dir)
    opp = _find_opp_or_raise(vdir, opp_id)

    low = opp.valuation.annual_benefit_low_usd or 0.0
    base = opp.valuation.annual_benefit_base_usd or 0.0
    high = opp.valuation.annual_benefit_high_usd or 0.0
    score = opp.computed_priority_score or 0.0
    mode = fmt.strip().lower()

    badges: list[str] = [f"Status: `{opp.status}`"]
    if opp.boss_mandate:
        badges.append("**P0: BOSS MANDATE**")
    if opp.status == "staging":
        badges.append("**STAGING (UNPROMOTED CANDIDATE)**")
    badges.append(f"Confidence: `{opp.valuation.confidence_tier}`")
    badges.append(f"Effort: `{opp.effort_weeks:.1f}w`")
    badges.append(f"Priority Score: `{score:.1f}/100`")

    report_gaps = _lookup_supplemented_report_gaps(vdir, opp)

    lines: list[str] = []
    if mode == "exec":
        lines.append(f"# Executive Opportunity Brief — {opp.id}: {opp.title}")
        lines.append("")
        lines.append(" | ".join(badges))
        lines.append("")
        lines.append("## 1. Value Proposition & Annualized Benefit Range")
    else:
        lines.append(f"## [{opp.id}] {opp.title}")
        lines.append("> " + " | ".join(badges))
        lines.append("")
        lines.append("### Value Range & Driver-Tree Formula")

    has_unverified = len(opp.valuation.verification_prompts) > 0
    caveat = (
        " _(Provisional range pending stakeholder verification below)_"
        if has_unverified
        else " _(All economic drivers Finance/Stakeholder verified)_"
    )
    lines.append(
        f"- **Estimated Annual Impact (Low / Base / High)**: **${low:,.0f} / ${base:,.0f} / ${high:,.0f}**{caveat}"
    )
    lines.append(f"- **Auditable Driver Formula**: `{opp.valuation.driver_formula}`")
    lines.append("")
    lines.append("| Driver | Assumed Value / Range | Verified | Source |")
    lines.append("|---|---|---|---|")
    for drv in opp.valuation.drivers:
        val_s = (
            f"[{drv.range[0]:g}, {drv.range[1]:g}, {drv.range[2]:g}]"
            if drv.range is not None
            else f"{drv.value:g}"
        )
        ver_s = "Verified" if drv.verified else "UNVERIFIED"
        lines.append(f"| `{drv.name}` | `{val_s}` | {ver_s} | {drv.source or '-'} |")

    if opp.valuation.verification_prompts:
        lines.append("")
        lines.append(
            "## 2. `[VERIFY WITH STAKEHOLDER]` Assumptions to Confirm"
            if mode == "exec"
            else "### `[VERIFY WITH STAKEHOLDER]` Assumptions to Confirm"
        )
        for prompt in opp.valuation.verification_prompts:
            lines.append(f"- {prompt}")

    lines.append("")
    lines.append(
        "## 3. Data Footprint & Power BI Delivery Surface"
        if mode == "exec"
        else "### Data Footprint & Power BI Delivery"
    )
    for ds in opp.internal_datasets:
        tx_list = (
            ds.transformations_needed
            if isinstance(ds.transformations_needed, list)
            else [str(ds.transformations_needed)]
        )
        tx = "; ".join(t for t in tx_list if t) or "Direct read"
        lines.append(
            f"- **Internal Dataset**: `{normalize_wikilink_target(ds.ref)}` ({ds.role or 'feature/target'}) — {tx}"
        )
    for ext in opp.external_data_required:
        lines.append(
            f"- **External Data**: **{ext.source}** (`{ext.acquisition}`, blocker: `{ext.blocker_severity}`) — {ext.purpose}"
        )
    for sup in opp.existing_reports_supplemented:
        lines.append(
            f"- **Enriches Power BI Report**: `{normalize_wikilink_target(sup.ref)}` — {sup.integration_mode}"
        )
    for nr in opp.new_reports_unlocked:
        lines.append(
            f"- **Unlocks Net-New Report**: **{nr.title}** ({nr.audience}) — {nr.why_net_new}"
        )
    for gap in report_gaps:
        lines.append(f"- **Current BI Bottleneck Addressed**: {gap}")

    lines.append("")
    lines.append(
        "## 4. Execution Plan & Stakeholders"
        if mode == "exec"
        else "### Pilot Steps & Counterparts"
    )
    if opp.stakeholders:
        cleaned_sh = [normalize_wikilink_target(s) for s in opp.stakeholders]
        lines.append(f"- **Stakeholders**: {', '.join(cleaned_sh)}")
    if opp.steps_to_completion:
        for idx, step in enumerate(opp.steps_to_completion, start=1):
            lines.append(f"{idx}. {step}")
    else:
        lines.append("1. Align on `[VERIFY]` assumptions and kick off scoped pilot.")

    return BriefResult(
        opp_id=opp.id,
        brief_type="opportunity",
        title=f"{opp.id}: {opp.title}",
        markdown="\n".join(lines).strip() + "\n",
    )


def generate_portfolio_brief(
    vault_dir: Path,
    capacity_weeks: float = 12.0,
) -> BriefResult:
    """Generate a Weekly Portfolio & Quarterly Capacity Cutline brief."""
    vdir = Path(vault_dir)
    all_items = load_all_opportunities(
        vdir, include_staging=True, include_archived=False
    )
    active_backlog = [opp for _, opp, _ in all_items if opp.status in {"backlog", "active"}]
    staged_items = [opp for _, opp, _ in all_items if opp.status == "staging"]

    schedule = compute_portfolio_schedule(
        active_backlog, quarterly_capacity_weeks=capacity_weeks
    )

    lines = [
        "# Weekly DS Portfolio & Quarterly Capacity Brief",
        "",
        "## Quarterly Capacity Utilization",
        f"- **Allocated / Available Capacity**: `{schedule.used_capacity_weeks:.1f}w` / `{schedule.capacity_weeks:.1f}w`",
        f"- **Boss-Mandate Pinned Effort**: `{schedule.mandate_weeks:.1f}w` | **Remaining Unallocated**: `{schedule.remaining_capacity_weeks:.1f}w`",
    ]
    if schedule.over_capacity:
        lines.append(
            f"- **⚠️ OVER CAPACITY ALERT**: Boss-mandated work exceeds quarterly capacity by `{schedule.overflow_weeks:.1f}w`!"
        )

    lines.extend(
        [
            "",
            "## Funded Above-Cutline Roadmap",
            "",
            "| Rank | ID | Title | Mandate | Effort | Base Benefit (Low–High) | Confidence | Score |",
            "|---:|---|---|---|---:|---|---|---:|",
        ]
    )
    funded = [item for item in schedule.items if item.above_cutline]
    if funded:
        for item in funded:
            mandate_s = "P0 Mandate" if item.boss_mandate else "No"
            val_s = (
                f"${item.annual_benefit_base_usd:,.0f} "
                f"(${item.annual_benefit_low_usd:,.0f}–${item.annual_benefit_high_usd:,.0f})"
            )
            lines.append(
                f"| {item.rank} | `{item.id}` | {item.title} | {mandate_s} | {item.effort_weeks:.1f}w | {val_s} | `{item.confidence_tier}` | {item.computed_priority_score:.1f} |"
            )
    else:
        lines.append("| - | - | _No funded items above cutline_ | - | - | - | - | - |")

    lines.extend(
        [
            "",
            "## Displaced by Boss Mandate (Shifted Past Quarterly Cutline)",
        ]
    )
    displaced = [item for item in schedule.items if item.displaced_by_mandate]
    if displaced:
        for item in displaced:
            lines.append(
                f"- **`{item.id}` — {item.title}** (`{item.effort_weeks:.1f}w`, Score `{item.computed_priority_score:.1f}`, Base `${item.annual_benefit_base_usd:,.0f}`): Was baseline rank #{item.baseline_rank} above cutline; displaced below quarterly capacity by P0 mandate."
            )
    else:
        lines.append("- _None — no ROI-funded items displaced by mandates._")

    below_non_displaced = [
        item
        for item in schedule.items
        if not item.above_cutline and not item.displaced_by_mandate
    ]
    lines.extend(["", "## Below-Cutline Backlog & Staging Gate"])
    lines.append(
        f"- **Unfunded Backlog Items**: {len(below_non_displaced)} | **Pending CFO Review in `vault/staging/`**: {len(staged_items)}"
    )
    for item in below_non_displaced:
        lines.append(
            f"  - Backlog `{item.id}`: {item.title} (`{item.effort_weeks:.1f}w`, Score `{item.computed_priority_score:.1f}`)"
        )
    for st_opp in staged_items:
        lines.append(
            f"  - Staging `{st_opp.id}`: {st_opp.title} (`{st_opp.effort_weeks:.1f}w`, Verdict: `{st_opp.cfo_critique_verdict or 'pending'}`)"
        )

    return BriefResult(
        opp_id=None,
        brief_type="weekly_status",
        title="Weekly DS Portfolio & Capacity Brief",
        markdown="\n".join(lines).strip() + "\n",
    )


def generate_verify_questions_brief(
    vault_dir: Path,
    opp_id: str | None = None,
) -> BriefResult:
    """Generate a 1-click Teams bundle of `[VERIFY WITH STAKEHOLDER]` prompts and open questions."""
    vdir = Path(vault_dir)
    if opp_id is not None and opp_id.strip():
        opp = _find_opp_or_raise(vdir, opp_id)
        stakeholders = (
            ", ".join(normalize_wikilink_target(s) for s in opp.stakeholders)
            or "Business & Finance Owners"
        )
        lines = [
            f"## Stakeholder Verification Questions — {opp.id}: {opp.title}",
            f"- **Counterparts**: {stakeholders}",
            f"- **Current Confidence Tier**: `{opp.valuation.confidence_tier}`",
            "",
        ]
        if opp.valuation.verification_prompts:
            for prompt in opp.valuation.verification_prompts:
                lines.append(f"- {prompt}")
        else:
            lines.append("- _All valuation drivers are already verified (`verified=True`)._")

        return BriefResult(
            opp_id=opp.id,
            brief_type="verify_questions",
            title=f"Verification Prompts — {opp.id}",
            markdown="\n".join(lines).strip() + "\n",
        )

    opps = [
        opp
        for _, opp, _ in load_all_opportunities(
            vdir, include_staging=True, include_archived=False
        )
    ]
    bank, _ = load_questions_and_playbook(vdir)

    lines = [
        "# Stakeholder Verification & Discovery Question Bundle",
        "",
        "## Opportunity `[VERIFY WITH STAKEHOLDER]` Assumptions",
    ]
    any_opp_prompts = False
    for opp in opps:
        if not opp.valuation.verification_prompts:
            continue
        any_opp_prompts = True
        sh_label = (
            ", ".join(normalize_wikilink_target(s) for s in opp.stakeholders)
            or "Unassigned Stakeholder"
        )
        lines.append(f"### {opp.id}: {opp.title} _(Counterparts: {sh_label})_")
        for prompt in opp.valuation.verification_prompts:
            lines.append(f"- {prompt}")
        lines.append("")

    if not any_opp_prompts:
        lines.append("- _No unverified opportunity drivers pending confirmation._")
        lines.append("")

    lines.append("## Open Week 1–2 Discovery Questions by Counterpart")
    open_qs = [q for q in bank.questions if q.status == "open"]
    if not open_qs:
        lines.append("- _All seeded discovery questions have been answered._")
    else:
        grouped: dict[str, list[str]] = {}
        for q in open_qs:
            grouped.setdefault(q.who_to_ask or "General", []).append(
                f"- **{q.id}** ({q.topic}): {q.question}"
            )
        for counterpart, bullets in grouped.items():
            lines.append(f"### {counterpart}")
            lines.extend(bullets)
            lines.append("")

    return BriefResult(
        opp_id=None,
        brief_type="verify_questions",
        title="Stakeholder Verification & Discovery Questions",
        markdown="\n".join(lines).strip() + "\n",
    )
