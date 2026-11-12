"""Non-destructive Markdown vault I/O, schema validation, mutations, and lineage graph builder."""

from __future__ import annotations

import datetime
import inspect
import os
import re
import shutil
import stat
import subprocess
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

from poopdeck.config import PoopdeckConfig, StrictInt
from poopdeck.models import (
    DatasetCard,
    GlossaryCard,
    OpenQuestionBank,
    OpportunitySpec,
    PlaybookTracker,
    QuestionItem,
    ReportCard,
    StakeholderCard,
    _canonical_model_key_rank,
    _ensure_finite_number,
    _stringify_dict_key,
    compute_priority_score,
)

HUMAN_NOTES_START = "<!-- HUMAN-NOTES-START -->"
HUMAN_NOTES_END = "<!-- HUMAN-NOTES-END -->"

DEFAULT_HUMAN_NOTES = (
    "\n_Add local context, stakeholder nuances, or meeting takeaways here. "
    "This block is preserved byte-for-byte across automated `poopdeck sync` runs._\n"
)

VAULT_SUBDIRS: tuple[str, ...] = (
    "datasets",
    "reports",
    "stakeholders",
    "questions",
    "staging/opportunities",
    "opportunities",
    "inbox/teams",
    "inbox/notes",
    "inbox/databricks",
    "inbox/powerbi",
    "inbox/processed",
    "archive",
)

_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)|\A---[ \t]*\r?\n---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)
_HUMAN_NOTES_RE = re.compile(
    re.escape(HUMAN_NOTES_START) + r"(.*?)" + re.escape(HUMAN_NOTES_END),
    re.DOTALL,
)
_WIKILINK_RE = re.compile(r"\[\[([^\[\]\r\n]+?)\]\]")


def _slugify_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")


def _validate_frontmatter_value(val: Any) -> Any:
    val = _ensure_finite_number(val)
    if isinstance(val, (datetime.datetime, datetime.date)):
        return val
    if isinstance(val, dict):
        out: dict[str, Any] = {}
        for k, sub_v in val.items():
            if not isinstance(k, str):
                raise ValueError(
                    f"Frontmatter mapping keys must be str, got {type(k).__name__}"
                )
            out[k] = _validate_frontmatter_value(sub_v)
        return out
    if isinstance(val, list):
        return [_validate_frontmatter_value(item) for item in val]
    if isinstance(val, tuple):
        return tuple(_validate_frontmatter_value(item) for item in val)
    if isinstance(val, (set, frozenset)):
        return sorted(
            (_validate_frontmatter_value(item) for item in val),
            key=_yaml_sort_key,
        )
    return val


def _validate_frontmatter_dict(v: Any) -> dict[str, Any]:
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise ValueError(f"frontmatter must be a mapping, got {type(v).__name__}")
    validated = _validate_frontmatter_value(v)
    assert isinstance(validated, dict)
    return validated


FrontmatterDict = Annotated[
    dict[str, Any], BeforeValidator(_validate_frontmatter_dict)
]


class ParsedCard(BaseModel):
    """Parsed representation of a Markdown vault card."""

    model_config = ConfigDict(allow_inf_nan=False, validate_assignment=True)

    frontmatter: FrontmatterDict = Field(default_factory=dict)
    body: str = ""
    human_notes: str | None = None
    raw_text: str = ""


class ValidationReport(BaseModel):
    """Structured vault validation summary."""

    model_config = ConfigDict(allow_inf_nan=False, validate_assignment=True)

    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    counts: dict[str, StrictInt] = Field(default_factory=dict)


def clean_git_env() -> dict[str, str]:
    """Return os.environ copy with all inherited GIT_* variables stripped."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def safe_rmtree(path: Path) -> None:
    """Remove directory tree safely on Windows by clearing read-only .git pack attributes."""
    target = Path(path)
    if not target.exists():
        return

    def _onexc(func: Any, failed_path: str, exc: BaseException) -> None:
        os.chmod(failed_path, stat.S_IWRITE)
        func(failed_path)

    if "onexc" in inspect.signature(shutil.rmtree).parameters:
        shutil.rmtree(target, onexc=_onexc)
    else:  # pragma: no cover - Python 3.11 fallback
        shutil.rmtree(
            target,
            onerror=lambda fn, p, _ei: (os.chmod(p, stat.S_IWRITE), fn(p)),
        )


def normalize_wikilink_target(ref: str) -> str:
    """Strip [[...]] brackets, |Alias suffixes, and #Heading anchors from a reference."""
    s = str(ref).strip()
    if s.startswith("[[") and s.endswith("]]"):
        s = s[2:-2].strip()
    if "|" in s:
        s = s.split("|", 1)[0].strip()
    if "#" in s:
        s = s.split("#", 1)[0].strip()
    return s


def format_wikilink(ref: str) -> str:
    """Wrap a target reference in [[...]] unless already wrapped."""
    s = str(ref).strip()
    if not s:
        return ""
    if s.startswith("[[") and s.endswith("]]"):
        return s
    return f"[[{s}]]"


def extract_wikilinks(text: str) -> list[str]:
    """Extract deduplicated [[Target]] and [[Target|Alias]] targets preserving order."""
    if not text:
        return []
    seen: dict[str, None] = {}
    for match in _WIKILINK_RE.finditer(text):
        raw = match.group(1).strip()
        target = raw.split("|", 1)[0].split("#", 1)[0].strip()
        if target and target not in seen:
            seen[target] = None
    return list(seen.keys())


def _yaml_sort_key(item: Any) -> tuple[str, str]:
    return (f"{type(item).__module__}.{type(item).__qualname__}", repr(item))


def _to_yaml_serializable(value: Any) -> Any:
    """Convert BaseModel/Enum/tuple/set/frozenset/Path/Decimal objects recursively into SafeDumper-clean Python primitives."""
    if isinstance(value, BaseModel):
        return _to_yaml_serializable(value.model_dump(mode="python"))
    value = _ensure_finite_number(value)
    if isinstance(value, Enum):
        return _to_yaml_serializable(value.value)
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        winners: dict[str, str] = {}
        for k, v in value.items():
            clean_k = _stringify_dict_key(k)
            clean_v = _to_yaml_serializable(v)
            rank = _canonical_model_key_rank(k)
            if clean_k not in out or rank >= winners[clean_k]:
                out[clean_k] = clean_v
                winners[clean_k] = rank
        return out
    if isinstance(value, (list, tuple)):
        return [_to_yaml_serializable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(
            (_to_yaml_serializable(v) for v in value),
            key=_yaml_sort_key,
        )
    return value


def parse_markdown_card(path: Path) -> ParsedCard:
    """Parse YAML frontmatter, generated Markdown body, and exact HUMAN-NOTES block."""
    card_path = Path(path)
    raw_text = card_path.read_text(encoding="utf-8-sig")

    frontmatter: dict[str, Any] = {}
    rest = raw_text
    fm_match = _FRONTMATTER_RE.match(raw_text)
    if fm_match is not None:
        fm_content = fm_match.group(1) or ""
        loaded = yaml.safe_load(fm_content) if fm_content.strip() else {}
        if loaded is None:
            frontmatter = {}
        elif isinstance(loaded, dict):
            frontmatter = loaded
        else:
            raise ValueError(
                f"YAML frontmatter in {card_path} must be a mapping, got {type(loaded).__name__}"
            )
        rest = raw_text[fm_match.end() :]

    hn_match = _HUMAN_NOTES_RE.search(rest)
    if hn_match is not None:
        human_notes = hn_match.group(1)  # Exact substring — NEVER strip!
        body = (rest[: hn_match.start()] + rest[hn_match.end() :]).strip()
    else:
        human_notes = None
        body = rest.strip()

    return ParsedCard(
        frontmatter=frontmatter,
        body=body,
        human_notes=human_notes,
        raw_text=raw_text,
    )


def upsert_markdown_card(
    path: Path,
    frontmatter: dict[str, Any] | BaseModel,
    generated_body: str,
    *,
    human_notes_override: str | None = None,
) -> Path:
    """Write or update a Markdown vault card while preserving HUMAN-NOTES byte-for-byte."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    fm_dict = _to_yaml_serializable(frontmatter)
    if not isinstance(fm_dict, dict):
        raise TypeError(
            f"frontmatter must be a dict or BaseModel, got {type(frontmatter).__name__}"
        )

    yaml_text = yaml.safe_dump(
        fm_dict,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).rstrip("\n")

    caller_notes: str | None = None
    body_hn = _HUMAN_NOTES_RE.search(generated_body or "")
    if body_hn is not None:
        caller_notes = body_hn.group(1)
        clean_body = (
            generated_body[: body_hn.start()] + generated_body[body_hn.end() :]
        ).strip()
    else:
        clean_body = (generated_body or "").strip()

    preserved_notes: str
    if human_notes_override is not None:
        preserved_notes = human_notes_override
    elif target.exists():
        existing_raw = target.read_text(encoding="utf-8-sig")
        hn_match = _HUMAN_NOTES_RE.search(existing_raw)
        if hn_match is not None:
            preserved_notes = hn_match.group(1)
        elif HUMAN_NOTES_START in existing_raw and HUMAN_NOTES_END not in existing_raw:
            idx = existing_raw.index(HUMAN_NOTES_START) + len(HUMAN_NOTES_START)
            preserved_notes = existing_raw[idx:]
        else:
            fm_existing = _FRONTMATTER_RE.match(existing_raw)
            existing_prose = (
                existing_raw[fm_existing.end() :] if fm_existing else existing_raw
            ).strip()
            if existing_prose and existing_prose != clean_body:
                preserved_notes = f"\n{existing_prose}\n"
            else:
                preserved_notes = (
                    caller_notes if caller_notes is not None else DEFAULT_HUMAN_NOTES
                )
    else:
        preserved_notes = (
            caller_notes if caller_notes is not None else DEFAULT_HUMAN_NOTES
        )

    parts = [f"---\n{yaml_text}\n---"]
    if clean_body:
        parts.append(clean_body)
    parts.append(f"{HUMAN_NOTES_START}{preserved_notes}{HUMAN_NOTES_END}\n")
    output = "\n\n".join(parts)

    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(output)
    return target


# ---------------------------------------------------------------------------
# Canonical Markdown Body Renderers
# ---------------------------------------------------------------------------


def render_dataset_body(card: DatasetCard) -> str:
    """Render deterministic Markdown summary body for a DatasetCard."""
    lines = [
        f"# Dataset: `{card.fqn}`",
        "",
        f"- **Medallion Layer**: `{card.medallion_layer}`",
        f"- **Table Type**: `{card.table_type}`",
        f"- **Grain**: {card.grain}",
        f"- **Join Keys**: {', '.join(f'`{k}`' for k in card.join_keys) if card.join_keys else '_None declared_'}",
        f"- **Row Count**: {f'{card.row_count:,}' if card.row_count is not None else '_Unknown_'}",
        f"- **Freshness Lag (Hours)**: {f'{card.freshness_lag_hours:.1f}h' if card.freshness_lag_hours is not None else '_Unknown_'}",
        f"- **30d Query Count**: {card.query_count_30d if card.query_count_30d is not None else '_Unknown_'}",
        "",
        "## Lineage & Workflows",
        f"- **Upstream**: {', '.join(format_wikilink(r) for r in card.upstream_refs) if card.upstream_refs else '_None_'}",
        f"- **Downstream**: {', '.join(format_wikilink(r) for r in card.downstream_refs) if card.downstream_refs else '_None_'}",
        f"- **Scheduled Jobs**: {', '.join(f'`{j}`' for j in card.scheduled_jobs) if card.scheduled_jobs else '_None_'}",
        "",
        "## Column Schema & Safe Profile",
        "",
        "| Column | Type | Null % | Distinct | PII | Comment |",
        "|---|---|---:|---:|---|---|",
    ]
    if card.columns:
        for col in card.columns:
            null_s = f"{col.null_pct:.1f}%" if col.null_pct is not None else "-"
            dist_s = f"{col.distinct_count:,}" if col.distinct_count is not None else "-"
            pii_s = "REDACTED/PII" if col.is_pii else "No"
            comment_s = col.comment.replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| `{col.name}` | `{col.data_type}` | {null_s} | {dist_s} | {pii_s} | {comment_s} |"
            )
    else:
        lines.append("| _No columns profiled_ | - | - | - | - | - |")

    lines.extend(["", "## Sample Rows", ""])
    if not card.sample_rows:
        lines.append("_Zero sample rows stored (safe_aggregates privacy gate)._")
    else:
        headers = list(card.sample_rows[0].keys())
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("| " + " | ".join("---" for _ in headers) + " |")
        for row in card.sample_rows:
            cells = [
                str(row.get(h, "")).replace("|", "\\|").replace("\n", " ")
                for h in headers
            ]
            lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def render_report_body(card: ReportCard) -> str:
    """Render deterministic Markdown summary body for a ReportCard."""
    lines = [
        f"# Power BI Report: {card.title}",
        "",
        f"- **Workspace**: {card.workspace}",
        f"- **Semantic Model**: {card.semantic_model or '_Unspecified_'}",
        f"- **Refresh Schedule**: {card.refresh_schedule or '_Unspecified_'} (30d Failures: {card.refresh_failures_30d})",
        f"- **High Excel Export Signal**: {'Yes' if card.high_excel_export else 'No'}",
        "",
        "## Upstream Datasets & Owners",
        f"- **Upstream Datasets**: {', '.join(format_wikilink(d) for d in card.upstream_datasets) if card.upstream_datasets else '_None_'}",
        f"- **Owners**: {', '.join(format_wikilink(o) for o in card.owners) if card.owners else '_Unassigned_'}",
        "",
        "## BI-to-DS Replacement Gaps",
    ]
    if card.bi_to_ds_gaps:
        for gap in card.bi_to_ds_gaps:
            lines.append(f"- {gap}")
    else:
        lines.append("- _No heuristic gaps flagged yet._")

    lines.extend(
        [
            "",
            "## Semantic Model Measures",
            "",
            "| Measure | Table | Heuristic Flags | Expression |",
            "|---|---|---|---|",
        ]
    )
    if card.measures:
        for m in card.measures:
            flags = ", ".join(f"`{f}`" for f in m.flagged_heuristics) or "-"
            expr = m.expression.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| `{m.name}` | `{m.table}` | {flags} | `{expr}` |")
    else:
        lines.append("| _No measures extracted_ | - | - | - |")
    return "\n".join(lines)


def render_stakeholder_body(card: StakeholderCard) -> str:
    """Render deterministic Markdown summary body for a StakeholderCard."""
    role_suffix = f" ({card.role})" if card.role else ""
    lines = [
        f"# Stakeholder: {card.name}{role_suffix}",
        "",
        f"- **Department**: {card.department or '_Unspecified_'}",
        f"- **Influence Tier**: `{card.influence_tier}`",
        f"- **Decision Appetite**: `{card.decision_appetite}`",
        f"- **Owned Reports**: {', '.join(format_wikilink(r) for r in card.owned_reports) if card.owned_reports else '_None_'}",
        "",
        "## KPIs Cared About",
    ]
    lines.extend(
        [f"- {k}" for k in card.kpis_cared_about]
        if card.kpis_cared_about
        else ["- _None recorded yet._"]
    )
    lines.extend(["", "## Pain Points"])
    lines.extend(
        [f"- {p}" for p in card.pain_points]
        if card.pain_points
        else ["- _None recorded yet._"]
    )
    lines.extend(["", "## Explicit Asks"])
    lines.extend(
        [f"- {a}" for a in card.explicit_asks]
        if card.explicit_asks
        else ["- _None recorded yet._"]
    )
    lines.extend(["", "## Boss Mandate Asks"])
    lines.extend(
        [f"- {b}" for b in card.boss_mandate_asks]
        if card.boss_mandate_asks
        else ["- _None._"]
    )
    return "\n".join(lines)


def render_opportunity_body(
    opp: OpportunitySpec, cfo_critique: str | None = None
) -> str:
    """Render deterministic Markdown body for an OpportunitySpec."""
    low = opp.valuation.annual_benefit_low_usd or 0.0
    base = opp.valuation.annual_benefit_base_usd or 0.0
    high = opp.valuation.annual_benefit_high_usd or 0.0
    mandate_tag = "P0 BOSS MANDATE" if opp.boss_mandate else "ROI-Ranked"
    lines = [
        f"# {opp.id}: {opp.title}",
        "",
        f"- **Status**: `{opp.status}` ({mandate_tag})",
        f"- **Domain Pack**: `{opp.domain_pack}`",
        f"- **Confidence Tier**: `{opp.valuation.confidence_tier}`",
        f"- **Effort**: `{opp.effort_weeks:.1f}` person-weeks | **Priority Score**: `{opp.computed_priority_score:.2f}`",
        f"- **Annual Benefit Range (Low / Base / High)**: `${low:,.0f}` / `${base:,.0f}` / `${high:,.0f}`",
        "",
        "## Data & Engineering Footprint",
    ]
    if opp.internal_datasets:
        for ds in opp.internal_datasets:
            tx = "; ".join(ds.transformations_needed) or "Direct consumption"
            lines.append(
                f"- **Internal**: {format_wikilink(ds.ref)} ({ds.role or 'input'}) — _{tx}_"
            )
    else:
        lines.append("- _No internal datasets linked yet._")

    for ext in opp.external_data_required:
        lines.append(
            f"- **External**: **{ext.source}** (`{ext.acquisition}`, blocker: `{ext.blocker_severity}`) — {ext.purpose}"
        )

    lines.extend(["", "## Power BI Delivery Surface"])
    for rep in opp.existing_reports_supplemented:
        lines.append(
            f"- **Supplements Existing Report**: {format_wikilink(rep.ref)} — {rep.integration_mode}"
        )
    for nr in opp.new_reports_unlocked:
        lines.append(
            f"- **Unlocks Net-New Report**: **{nr.title}** ({nr.audience}) — {nr.why_net_new}"
        )
    if not opp.existing_reports_supplemented and not opp.new_reports_unlocked:
        lines.append("- _Delivery surface pending alignment._")

    lines.extend(
        [
            "",
            "## Driver-Tree Valuation & Verification Prompts",
            "",
            f"**Formula**: `{opp.valuation.driver_formula}`",
            "",
            "| Driver | Value / Range | Verified | Source |",
            "|---|---|---|---|",
        ]
    )
    for drv in opp.valuation.drivers:
        val_s = (
            f"[{drv.range[0]:g}, {drv.range[1]:g}, {drv.range[2]:g}]"
            if drv.range is not None
            else f"{drv.value:g}"
        )
        ver_s = "Yes" if drv.verified else "No (`[VERIFY]`)"
        lines.append(f"| `{drv.name}` | `{val_s}` | {ver_s} | {drv.source or '-'} |")

    if opp.valuation.verification_prompts:
        lines.extend(["", "### Required Stakeholder Verifications"])
        for prompt in opp.valuation.verification_prompts:
            lines.append(f"- {prompt}")

    lines.extend(["", "## Steps to Completion"])
    if opp.steps_to_completion:
        for idx, step in enumerate(opp.steps_to_completion, start=1):
            lines.append(f"{idx}. {step}")
    else:
        lines.append("1. _Define pilot milestones._")

    if cfo_critique or opp.cfo_critique_verdict or opp.cfo_critique_notes:
        lines.extend(["", "## Skeptical CFO Critique"])
        if opp.cfo_critique_verdict:
            lines.append(f"- **Verdict**: `{opp.cfo_critique_verdict}`")
        for note in opp.cfo_critique_notes:
            lines.append(f"- {note}")
        if cfo_critique:
            lines.append("")
            lines.append(cfo_critique.strip())

    return "\n".join(lines)


_QUESTION_SECTIONS: tuple[tuple[str, str], ...] = (
    ("manager", "### A. Manager (Mandate, Air Cover & Political Terrain)"),
    ("it_security", "### B. IT / Security / Platform Admin (Tooling & Guardrails)"),
    ("data_engineering", "### C. Data Engineering (Lakehouse Reality & Production Path)"),
    ("power_bi", "### D. Power BI / Analytics Owners (Delivery & Adoption)"),
    ("business_finance", "### E. Business Unit Leads & Finance (Grounding the Dollar Formula)"),
)


def render_questions_body(bank: OpenQuestionBank) -> str:
    """Render grouped Week 1-2 Discovery Questionnaire checklist body."""
    lines = ["# Week 1–2 Discovery Questionnaire & Open Questions", ""]
    by_cat: dict[str, list[QuestionItem]] = {k: [] for k, _ in _QUESTION_SECTIONS}
    for q in bank.questions:
        by_cat.setdefault(q.category, []).append(q)

    for cat_key, heading in _QUESTION_SECTIONS:
        items = by_cat.get(cat_key, [])
        if not items:
            continue
        lines.append(heading)
        lines.append("")
        for q in items:
            mark = "x" if q.status == "answered" else ("!" if q.status == "blocked" else " ")
            topic_prefix = f" ({q.topic})" if q.topic else ""
            lines.append(
                f"- [{mark}] **{q.id}{topic_prefix}** — {q.question} _(Ask: {q.who_to_ask} | Phase: `{q.phase}`)_"
            )
            if q.answer_summary:
                lines.append(f"  - **Answer**: {q.answer_summary}")
            if q.linked_refs:
                lines.append(
                    f"  - **Refs**: {', '.join(format_wikilink(r) for r in q.linked_refs)}"
                )
        lines.append("")
    return "\n".join(lines).rstrip()


def render_playbook_body(tracker: PlaybookTracker) -> str:
    """Render Day 1-90 Solo DS Execution Playbook table & checklists."""
    lines = [
        "# Solo DS Day 1–90 Execution Playbook",
        "",
        "| ID | Phase | Primary Goal | Tangible Output | Status |",
        "|---|---|---|---|---|",
    ]
    for p in tracker.phases:
        status_s = "Completed" if p.completed else "Pending"
        lines.append(
            f"| `{p.id}` | **{p.phase}** | {p.goal} | {p.output} | {status_s} |"
        )

    lines.append("")
    for p in tracker.phases:
        mark = "x" if p.completed else " "
        lines.append(f"## [{mark}] {p.id} — {p.phase}")
        lines.append(f"- **Goal**: {p.goal}")
        lines.append(f"- **Target Output**: {p.output}")
        for act in p.actions:
            lines.append(f"  - [{mark}] {act}")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_glossary_body(card: GlossaryCard) -> str:
    """Render Business & KPI Glossary table."""
    lines = [
        "# Business & KPI Glossary",
        "",
        "| Term | Definition | Source | Linked Entities |",
        "|---|---|---|---|",
    ]
    if not card.terms:
        lines.append("| _No proprietary glossary terms recorded yet._ | - | - | - |")
    else:
        for t in card.terms:
            refs = (
                ", ".join(format_wikilink(r) for r in t.linked_refs)
                if t.linked_refs
                else "-"
            )
            defn = t.definition.replace("|", "\\|").replace("\n", " ")
            src = (t.source or "-").replace("|", "\\|")
            lines.append(f"| **{t.term}** | {defn} | {src} | {refs} |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Vault Lifecycle, Validation, Mutations & Lineage Graph
# ---------------------------------------------------------------------------


def _resolve_playbook_path(vault_dir: Path) -> Path:
    canonical = Path(vault_dir) / "questions" / "day1-90-playbook.md"
    alt = Path(vault_dir) / "questions" / "day-1-90-playbook.md"
    if not canonical.exists() and alt.exists():
        return alt
    return canonical


def initialize_vault(
    vault_dir: Path,
    seed_questions: bool = True,
    init_git: bool = True,
) -> dict[str, Any]:
    """Idempotently create canonical vault subdirs, isolated vault/.git, and Day-1 seed cards."""
    from poopdeck.templates import seed_vault_templates

    vdir = Path(vault_dir)
    vdir.mkdir(parents=True, exist_ok=True)
    created_dirs: list[str] = []
    for rel in VAULT_SUBDIRS:
        sub = vdir / rel
        if not sub.exists():
            created_dirs.append(rel)
        sub.mkdir(parents=True, exist_ok=True)

    vault_gitignore = vdir / ".gitignore"
    if not vault_gitignore.exists():
        with open(vault_gitignore, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(".DS_Store\nThumbs.db\n*.tmp\n")

    git_initialized = False
    git_dir = vdir / ".git"
    if init_git and not git_dir.exists():
        env = clean_git_env()
        subprocess.run(
            ["git", "init"],
            cwd=vdir,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Poopdeck Vault"],
            cwd=vdir,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "poopdeck-vault@local"],
            cwd=vdir,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        git_initialized = True

    seeded_files: list[Path] = []
    if seed_questions:
        seeded_files = seed_vault_templates(vdir, overwrite=False)

    return {
        "vault_dir": str(vdir),
        "created_dirs": created_dirs,
        "git_initialized": git_initialized,
        "has_git": git_dir.is_dir(),
        "seeded_files": [str(p) for p in seeded_files],
    }


def _register_ref_keys(registry: set[str], *candidates: str | None) -> None:
    for cand in candidates:
        if not cand:
            continue
        clean = normalize_wikilink_target(cand)
        if not clean:
            continue
        registry.add(clean.casefold())
        slug = _slugify_key(clean)
        if slug:
            registry.add(slug)


def validate_vault(
    vault_dir: Path,
    config: PoopdeckConfig | None = None,
    strict: bool = False,
) -> ValidationReport:
    """Validate all canonical Markdown cards, schemas, folder invariants, and [[wikilinks]]."""
    vdir = Path(vault_dir)
    counts: dict[str, int] = {
        "datasets": 0,
        "reports": 0,
        "stakeholders": 0,
        "questions": 0,
        "playbook": 0,
        "glossary": 0,
        "staging_opportunities": 0,
        "opportunities": 0,
    }
    errors: list[str] = []
    warnings: list[str] = []

    if not vdir.exists() or not vdir.is_dir():
        return ValidationReport(
            valid=False,
            errors=[f"Vault directory does not exist: {vdir}"],
            warnings=[],
            counts=counts,
        )

    known_refs: set[str] = set()
    pending_links: list[tuple[str, str]] = []

    def _check_card_markers(rel_path: str, raw_text: str) -> None:
        n_start = raw_text.count(HUMAN_NOTES_START)
        n_end = raw_text.count(HUMAN_NOTES_END)
        if n_start != n_end or (
            n_start > 0 and raw_text.index(HUMAN_NOTES_START) > raw_text.index(HUMAN_NOTES_END)
        ):
            errors.append(f"{rel_path}: Unbalanced HUMAN-NOTES markers")
        elif n_start == 0:
            msg = f"{rel_path}: Missing HUMAN-NOTES markers"
            if strict:
                errors.append(msg)
            else:
                warnings.append(msg)

    def _queue_links(rel_path: str, body: str, extra_refs: list[str]) -> None:
        for target in extract_wikilinks(body):
            pending_links.append((rel_path, target))
        for raw_ref in extra_refs:
            if not raw_ref:
                continue
            for wk in extract_wikilinks(raw_ref):
                pending_links.append((rel_path, wk))
            if "[[" not in raw_ref:
                norm = normalize_wikilink_target(raw_ref)
                if norm:
                    pending_links.append((rel_path, norm))

    # 1. Datasets
    ds_dir = vdir / "datasets"
    if ds_dir.is_dir():
        for md_path in sorted(ds_dir.glob("*.md")):
            rel = md_path.relative_to(vdir).as_posix()
            try:
                parsed = parse_markdown_card(md_path)
                _check_card_markers(rel, parsed.raw_text)
                card = DatasetCard.model_validate(parsed.frontmatter)
                counts["datasets"] += 1
                _register_ref_keys(
                    known_refs,
                    card.id,
                    card.fqn,
                    card.table_name,
                    md_path.stem,
                    f"datasets/{md_path.stem}",
                )
                _queue_links(
                    rel,
                    parsed.body,
                    [*card.upstream_refs, *card.downstream_refs],
                )
            except (ValidationError, ValueError, yaml.YAMLError) as exc:
                errors.append(f"{rel}: {exc}")

    # 2. Reports
    rep_dir = vdir / "reports"
    if rep_dir.is_dir():
        for md_path in sorted(rep_dir.glob("*.md")):
            rel = md_path.relative_to(vdir).as_posix()
            try:
                parsed = parse_markdown_card(md_path)
                _check_card_markers(rel, parsed.raw_text)
                card = ReportCard.model_validate(parsed.frontmatter)
                counts["reports"] += 1
                _register_ref_keys(
                    known_refs,
                    card.id,
                    card.title,
                    card.semantic_model,
                    f"{card.workspace}__{card.title}",
                    md_path.stem,
                    f"reports/{md_path.stem}",
                )
                _queue_links(
                    rel,
                    parsed.body,
                    [*card.upstream_datasets, *card.owners],
                )
            except (ValidationError, ValueError, yaml.YAMLError) as exc:
                errors.append(f"{rel}: {exc}")

    # 3. Stakeholders
    sh_dir = vdir / "stakeholders"
    if sh_dir.is_dir():
        for md_path in sorted(sh_dir.glob("*.md")):
            rel = md_path.relative_to(vdir).as_posix()
            try:
                parsed = parse_markdown_card(md_path)
                _check_card_markers(rel, parsed.raw_text)
                card = StakeholderCard.model_validate(parsed.frontmatter)
                counts["stakeholders"] += 1
                _register_ref_keys(
                    known_refs,
                    card.id,
                    card.name,
                    md_path.stem,
                    f"stakeholders/{md_path.stem}",
                )
                _queue_links(rel, parsed.body, list(card.owned_reports))
            except (ValidationError, ValueError, yaml.YAMLError) as exc:
                errors.append(f"{rel}: {exc}")

    # 4. Questions & Playbook
    q_dir = vdir / "questions"
    if q_dir.is_dir():
        for md_path in sorted(q_dir.glob("*.md")):
            rel = md_path.relative_to(vdir).as_posix()
            try:
                parsed = parse_markdown_card(md_path)
                _check_card_markers(rel, parsed.raw_text)
                if "phases" in parsed.frontmatter or md_path.stem in (
                    "day1-90-playbook",
                    "day-1-90-playbook",
                ):
                    tracker = PlaybookTracker.model_validate(parsed.frontmatter)
                    counts["playbook"] += len(tracker.phases) or 1
                    _register_ref_keys(
                        known_refs,
                        tracker.id,
                        md_path.stem,
                        "day1-90-playbook",
                        "day-1-90-playbook",
                        *(p.id for p in tracker.phases),
                    )
                    _queue_links(rel, parsed.body, [])
                elif "questions" in parsed.frontmatter or md_path.stem == "open-questions":
                    bank = OpenQuestionBank.model_validate(parsed.frontmatter)
                    counts["questions"] += len(bank.questions) or 1
                    _register_ref_keys(
                        known_refs,
                        bank.id,
                        md_path.stem,
                        *(q.id for q in bank.questions),
                    )
                    q_refs = [r for q in bank.questions for r in q.linked_refs]
                    _queue_links(rel, parsed.body, q_refs)
                else:
                    q_item = QuestionItem.model_validate(parsed.frontmatter)
                    counts["questions"] += 1
                    _register_ref_keys(known_refs, q_item.id, md_path.stem)
                    _queue_links(rel, parsed.body, list(q_item.linked_refs))
            except (ValidationError, ValueError, yaml.YAMLError) as exc:
                errors.append(f"{rel}: {exc}")

    # 5. Glossary
    glossary_path = vdir / "glossary.md"
    if glossary_path.is_file():
        rel = glossary_path.relative_to(vdir).as_posix()
        try:
            parsed = parse_markdown_card(glossary_path)
            _check_card_markers(rel, parsed.raw_text)
            gcard = GlossaryCard.model_validate(parsed.frontmatter)
            counts["glossary"] += 1
            _register_ref_keys(
                known_refs,
                gcard.id,
                "glossary",
                *(t.term for t in gcard.terms),
            )
            g_refs = [r for t in gcard.terms for r in t.linked_refs]
            _queue_links(rel, parsed.body, g_refs)
        except (ValidationError, ValueError, yaml.YAMLError) as exc:
            errors.append(f"{rel}: {exc}")

    # 6. Staging & Active/Backlog Opportunities
    opp_folders = (
        (vdir / "staging" / "opportunities", "staging_opportunities", True),
        (vdir / "opportunities", "opportunities", False),
    )
    seen_opp_ids: dict[str, str] = {}
    for folder, count_key, is_staging_folder in opp_folders:
        if not folder.is_dir():
            continue
        for md_path in sorted(folder.glob("*.md")):
            rel = md_path.relative_to(vdir).as_posix()
            try:
                parsed = parse_markdown_card(md_path)
                _check_card_markers(rel, parsed.raw_text)
                opp = OpportunitySpec.model_validate(parsed.frontmatter)
                norm_opp_id = opp.id.strip().upper()
                if norm_opp_id in seen_opp_ids:
                    errors.append(
                        f"{rel}: Duplicate opportunity id {opp.id!r} "
                        f"(already defined in {seen_opp_ids[norm_opp_id]})"
                    )
                else:
                    seen_opp_ids[norm_opp_id] = rel
                if is_staging_folder and opp.status != "staging":
                    errors.append(
                        f"{rel}: Card in staging/opportunities must have status='staging', got {opp.status!r}"
                    )
                elif not is_staging_folder and opp.status == "staging":
                    errors.append(
                        f"{rel}: Card in opportunities/ must not have status='staging'"
                    )
                counts[count_key] += 1
                _register_ref_keys(
                    known_refs,
                    opp.id,
                    opp.title,
                    md_path.stem,
                    rel[:-3] if rel.endswith(".md") else rel,
                )
                opp_refs = [
                    *(d.ref for d in opp.internal_datasets),
                    *(r.ref for r in opp.existing_reports_supplemented),
                    *opp.stakeholders,
                ]
                _queue_links(rel, parsed.body, opp_refs)
            except (ValidationError, ValueError, yaml.YAMLError) as exc:
                errors.append(f"{rel}: {exc}")

    # 7. Check all queued [[wikilinks]] & structured refs
    seen_unresolved: set[tuple[str, str]] = set()
    for rel_path, target in pending_links:
        norm = normalize_wikilink_target(target)
        if not norm:
            continue
        probe_keys = {norm.casefold(), _slugify_key(norm)}
        if not (probe_keys & known_refs):
            key = (rel_path, norm)
            if key in seen_unresolved:
                continue
            seen_unresolved.add(key)
            msg = f"{rel_path}: Unresolved wikilink reference [[{norm}]]"
            if strict:
                errors.append(msg)
            else:
                warnings.append(msg)

    return ValidationReport(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        counts=counts,
    )


def load_all_opportunities(
    vault_dir: Path,
    include_staging: bool = False,
    include_archived: bool = False,
) -> list[tuple[Path, OpportunitySpec, ParsedCard]]:
    """Load validated OpportunitySpec cards from vault/opportunities (and optionally staging)."""
    vdir = Path(vault_dir)
    search_dirs: list[Path] = [vdir / "opportunities"]
    if include_staging:
        search_dirs.append(vdir / "staging" / "opportunities")

    loaded: list[tuple[Path, OpportunitySpec, ParsedCard]] = []
    for folder in search_dirs:
        if not folder.is_dir():
            continue
        for md_path in sorted(folder.glob("*.md")):
            parsed = parse_markdown_card(md_path)
            opp = OpportunitySpec.model_validate(parsed.frontmatter)
            if opp.computed_priority_score is None:
                opp.computed_priority_score = compute_priority_score(opp)
            if not include_archived and opp.status == "archived":
                continue
            if not include_staging and opp.status == "staging":
                continue
            loaded.append((md_path, opp, parsed))

    loaded.sort(key=lambda item: (item[1].id, item[0].name))
    return loaded


def _find_opportunity_file(
    folders: list[Path], opp_id: str
) -> tuple[Path, ParsedCard] | None:
    needle = opp_id.strip().upper()
    if not needle:
        return None
    for folder in folders:
        if not folder.is_dir():
            continue
        for md_path in sorted(folder.glob("*.md")):
            parsed = parse_markdown_card(md_path)
            fm_id = str(parsed.frontmatter.get("id") or "").strip().upper()
            stem_up = md_path.stem.upper()
            if (
                fm_id == needle
                or stem_up == needle
                or stem_up.startswith(f"{needle}-")
            ):
                return (md_path, parsed)
    return None


def promote_opportunity(
    vault_dir: Path,
    opp_id: str,
    target_status: str = "backlog",
) -> Path:
    """Promote an opportunity from staging/opportunities to opportunities/ preserving HUMAN-NOTES."""
    if target_status.strip().lower() == "staging":
        raise ValueError("promote_opportunity target_status cannot be 'staging'")

    vdir = Path(vault_dir)
    found = _find_opportunity_file([vdir / "staging" / "opportunities"], opp_id)
    if found is None:
        raise KeyError(f"Opportunity '{opp_id}' not found in staging/opportunities")

    src_path, parsed = found
    fm = dict(parsed.frontmatter)
    fm["status"] = target_status.strip().lower()
    fm["updated_at"] = datetime.date.today()
    opp = OpportunitySpec.model_validate(fm)

    dest_path = vdir / "opportunities" / src_path.name
    upsert_markdown_card(
        dest_path,
        opp,
        parsed.body,
        human_notes_override=parsed.human_notes,
    )
    if src_path.resolve() != dest_path.resolve() and src_path.exists():
        src_path.unlink()
    return dest_path


def archive_opportunity(
    vault_dir: Path,
    opp_id: str,
    reason: str | None = None,
) -> Path:
    """Archive a staged or active/backlog opportunity while preserving HUMAN-NOTES."""
    vdir = Path(vault_dir)
    found = _find_opportunity_file(
        [vdir / "staging" / "opportunities", vdir / "opportunities"],
        opp_id,
    )
    if found is None:
        raise KeyError(f"Opportunity '{opp_id}' not found")

    src_path, parsed = found
    fm = dict(parsed.frontmatter)
    fm["status"] = "archived"
    fm["updated_at"] = datetime.date.today()
    opp = OpportunitySpec.model_validate(fm)

    new_body = parsed.body
    if reason and reason.strip():
        reason_block = f"## Archive Reason\n\n{reason.strip()}"
        if reason_block not in new_body:
            new_body = f"{new_body}\n\n{reason_block}".strip()

    dest_path = vdir / "opportunities" / src_path.name
    upsert_markdown_card(
        dest_path,
        opp,
        new_body,
        human_notes_override=parsed.human_notes,
    )
    if src_path.resolve() != dest_path.resolve() and src_path.exists():
        src_path.unlink()
    return dest_path


def set_boss_mandate(
    vault_dir: Path,
    opp_id: str,
    boss_mandate: bool | None = None,
) -> tuple[Path, OpportunitySpec]:
    """Set or toggle `boss_mandate` on an opportunity card, preserving HUMAN-NOTES."""
    vdir = Path(vault_dir)
    found = _find_opportunity_file(
        [vdir / "opportunities", vdir / "staging" / "opportunities"],
        opp_id,
    )
    if found is None:
        raise KeyError(f"Opportunity '{opp_id}' not found")

    card_path, parsed = found
    current = OpportunitySpec.model_validate(parsed.frontmatter)
    new_flag = (not current.boss_mandate) if boss_mandate is None else bool(boss_mandate)

    fm = dict(parsed.frontmatter)
    fm["boss_mandate"] = new_flag
    fm["updated_at"] = datetime.date.today()
    updated = OpportunitySpec.model_validate(fm)

    upsert_markdown_card(
        card_path,
        updated,
        parsed.body,
        human_notes_override=parsed.human_notes,
    )
    return (card_path, updated)


def load_questions_and_playbook(
    vault_dir: Path,
) -> tuple[OpenQuestionBank, PlaybookTracker]:
    """Load OpenQuestionBank and PlaybookTracker from vault/questions/."""
    vdir = Path(vault_dir)
    q_path = vdir / "questions" / "open-questions.md"
    pb_path = _resolve_playbook_path(vdir)

    if q_path.is_file():
        q_parsed = parse_markdown_card(q_path)
        bank = OpenQuestionBank.model_validate(q_parsed.frontmatter)
    else:
        bank = OpenQuestionBank(questions=[])

    if pb_path.is_file():
        pb_parsed = parse_markdown_card(pb_path)
        tracker = PlaybookTracker.model_validate(pb_parsed.frontmatter)
    else:
        tracker = PlaybookTracker(phases=[])

    return (bank, tracker)


def update_question_or_phase(
    vault_dir: Path,
    item_id: str,
    status: str | None = None,
    answer_summary: str | None = None,
    completed: bool | None = None,
    *,
    answer: str | None = None,
) -> Path:
    """Update a QuestionItem or PlaybookPhase in place while preserving HUMAN-NOTES."""
    if answer_summary is None and answer is not None:
        answer_summary = answer

    needle = item_id.strip().upper()
    if not needle:
        raise KeyError("item_id must not be empty")

    vdir = Path(vault_dir)
    pb_path = _resolve_playbook_path(vdir)
    if pb_path.is_file():
        pb_parsed = parse_markdown_card(pb_path)
        tracker = PlaybookTracker.model_validate(pb_parsed.frontmatter)
        for phase in tracker.phases:
            if phase.id.strip().upper() == needle:
                if completed is not None:
                    phase.completed = bool(completed)
                elif status is not None:
                    st = status.strip().lower()
                    if st in ("completed", "answered", "done", "true"):
                        phase.completed = True
                    elif st in ("open", "pending", "false"):
                        phase.completed = False
                tracker.updated_at = datetime.date.today()
                return upsert_markdown_card(
                    pb_path,
                    tracker,
                    render_playbook_body(tracker),
                    human_notes_override=pb_parsed.human_notes,
                )

    q_path = vdir / "questions" / "open-questions.md"
    if q_path.is_file():
        q_parsed = parse_markdown_card(q_path)
        bank = OpenQuestionBank.model_validate(q_parsed.frontmatter)
        for q in bank.questions:
            if q.id.strip().upper() == needle:
                if status is not None:
                    q.status = status.strip().lower()  # type: ignore[assignment]
                elif completed is not None:
                    q.status = "answered" if completed else "open"
                if answer_summary is not None:
                    q.answer_summary = answer_summary
                bank.updated_at = datetime.date.today()
                return upsert_markdown_card(
                    q_path,
                    bank,
                    render_questions_body(bank),
                    human_notes_override=q_parsed.human_notes,
                )

    raise KeyError(f"Question or playbook phase '{item_id}' not found")


def build_lineage_graph(
    vault_dir: Path,
    include_staging: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Build end-to-end External/Dataset -> Opportunity -> Power BI / Stakeholder lineage graph."""
    vdir = Path(vault_dir)
    nodes_by_id: dict[str, dict[str, Any]] = {}
    alias_to_node_id: dict[str, str] = {}
    typed_alias_to_node_id: dict[tuple[str, str], str] = {}
    edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str, str]] = set()

    def _add_node(
        node_id: str,
        label: str,
        node_type: str,
        exists_in_vault: bool = True,
        aliases: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        if node_id not in nodes_by_id:
            nodes_by_id[node_id] = {
                "id": node_id,
                "label": label,
                "type": node_type,
                "exists_in_vault": exists_in_vault,
                "metadata": metadata or {},
            }
        elif exists_in_vault and not nodes_by_id[node_id]["exists_in_vault"]:
            nodes_by_id[node_id]["exists_in_vault"] = True
            nodes_by_id[node_id]["label"] = label
            nodes_by_id[node_id]["type"] = node_type
            nodes_by_id[node_id]["metadata"] = metadata or {}

        for alias in [node_id, label, *(aliases or [])]:
            clean = normalize_wikilink_target(alias)
            if clean:
                cf = clean.casefold()
                typed_alias_to_node_id.setdefault((node_type, cf), node_id)
                alias_to_node_id.setdefault(cf, node_id)
                slug = _slugify_key(clean)
                if slug:
                    typed_alias_to_node_id.setdefault((node_type, slug), node_id)
                    alias_to_node_id.setdefault(slug, node_id)
        return node_id

    def _resolve_or_placeholder(raw_ref: str, default_type: str) -> str:
        clean = normalize_wikilink_target(raw_ref)
        if not clean:
            return ""
        candidates = (clean.casefold(), _slugify_key(clean))
        for key in candidates:
            if key and (default_type, key) in typed_alias_to_node_id:
                return typed_alias_to_node_id[(default_type, key)]
        for key in candidates:
            if key and key in alias_to_node_id:
                return alias_to_node_id[key]
        placeholder_id = f"{default_type}:{clean}"
        return _add_node(
            node_id=placeholder_id,
            label=clean,
            node_type=default_type,
            exists_in_vault=False,
            aliases=[clean],
            metadata={"unresolved_ref": clean},
        )

    def _add_edge(src: str, dst: str, relation: str) -> None:
        if not src or not dst or src == dst:
            return
        key = (src, dst, relation)
        if key not in seen_edges:
            seen_edges.add(key)
            edges.append({"source": src, "target": dst, "relation": relation})

    # Pass 1: Load concrete vault entities so aliases resolve accurately
    loaded_datasets: list[tuple[str, DatasetCard, ParsedCard]] = []
    ds_dir = vdir / "datasets"
    if ds_dir.is_dir():
        for md_path in sorted(ds_dir.glob("*.md")):
            try:
                parsed = parse_markdown_card(md_path)
                card = DatasetCard.model_validate(parsed.frontmatter)
                nid = _add_node(
                    node_id=f"dataset:{card.fqn}",
                    label=card.fqn,
                    node_type="dataset",
                    exists_in_vault=True,
                    aliases=[
                        card.id,
                        card.fqn,
                        card.table_name,
                        md_path.stem,
                        f"datasets/{md_path.stem}",
                    ],
                    metadata={
                        "fqn": card.fqn,
                        "medallion_layer": card.medallion_layer,
                        "row_count": card.row_count,
                    },
                )
                loaded_datasets.append((nid, card, parsed))
            except (ValidationError, ValueError, yaml.YAMLError):
                continue

    loaded_reports: list[tuple[str, ReportCard, ParsedCard]] = []
    rep_dir = vdir / "reports"
    if rep_dir.is_dir():
        for md_path in sorted(rep_dir.glob("*.md")):
            try:
                parsed = parse_markdown_card(md_path)
                card = ReportCard.model_validate(parsed.frontmatter)
                nid = _add_node(
                    node_id=f"existing_report:{card.id}",
                    label=card.title,
                    node_type="existing_report",
                    exists_in_vault=True,
                    aliases=[
                        card.id,
                        card.title,
                        card.semantic_model,
                        f"{card.workspace}__{card.title}",
                        md_path.stem,
                        f"reports/{md_path.stem}",
                    ],
                    metadata={
                        "workspace": card.workspace,
                        "high_excel_export": card.high_excel_export,
                        "bi_to_ds_gaps": card.bi_to_ds_gaps,
                    },
                )
                loaded_reports.append((nid, card, parsed))
            except (ValidationError, ValueError, yaml.YAMLError):
                continue

    loaded_stakeholders: list[tuple[str, StakeholderCard, ParsedCard]] = []
    sh_dir = vdir / "stakeholders"
    if sh_dir.is_dir():
        for md_path in sorted(sh_dir.glob("*.md")):
            try:
                parsed = parse_markdown_card(md_path)
                card = StakeholderCard.model_validate(parsed.frontmatter)
                nid = _add_node(
                    node_id=f"stakeholder:{card.id}",
                    label=card.name,
                    node_type="stakeholder",
                    exists_in_vault=True,
                    aliases=[
                        card.id,
                        card.name,
                        md_path.stem,
                        f"stakeholders/{md_path.stem}",
                    ],
                    metadata={
                        "role": card.role,
                        "influence_tier": card.influence_tier,
                    },
                )
                loaded_stakeholders.append((nid, card, parsed))
            except (ValidationError, ValueError, yaml.YAMLError):
                continue

    loaded_opps = load_all_opportunities(
        vdir, include_staging=include_staging, include_archived=False
    )
    opp_nodes: list[tuple[str, OpportunitySpec, ParsedCard]] = []
    for md_path, opp, parsed in loaded_opps:
        nid = _add_node(
            node_id=f"opportunity:{opp.id}",
            label=f"{opp.id}: {opp.title}",
            node_type="opportunity",
            exists_in_vault=True,
            aliases=[opp.id, opp.title, md_path.stem],
            metadata={
                "opp_id": opp.id,
                "status": opp.status,
                "boss_mandate": opp.boss_mandate,
                "effort_weeks": opp.effort_weeks,
                "computed_priority_score": opp.computed_priority_score,
                "annual_benefit_base_usd": opp.valuation.annual_benefit_base_usd,
            },
        )
        opp_nodes.append((nid, opp, parsed))

    # Pass 2: Wire directed lineage edges (creating placeholder nodes if missing)
    for ds_nid, card, _ in loaded_datasets:
        for up in card.upstream_refs:
            up_nid = _resolve_or_placeholder(up, "dataset")
            _add_edge(up_nid, ds_nid, "feeds_dataset")
        for down in card.downstream_refs:
            down_nid = _resolve_or_placeholder(down, "dataset")
            _add_edge(ds_nid, down_nid, "feeds_dataset")

    for rep_nid, card, _ in loaded_reports:
        for ds_ref in card.upstream_datasets:
            ds_nid = _resolve_or_placeholder(ds_ref, "dataset")
            _add_edge(ds_nid, rep_nid, "feeds_report")
        for owner_ref in card.owners:
            sh_nid = _resolve_or_placeholder(owner_ref, "stakeholder")
            _add_edge(sh_nid, rep_nid, "owns_report")

    for sh_nid, card, _ in loaded_stakeholders:
        for rep_ref in card.owned_reports:
            rep_nid = _resolve_or_placeholder(rep_ref, "existing_report")
            _add_edge(sh_nid, rep_nid, "owns_report")

    for opp_nid, opp, parsed in opp_nodes:
        structured_link_keys: set[str] = set()
        for raw_structured in (
            *(d.ref for d in opp.internal_datasets),
            *(r.ref for r in opp.existing_reports_supplemented),
            *opp.stakeholders,
        ):
            norm_s = normalize_wikilink_target(raw_structured)
            if norm_s:
                structured_link_keys.add(norm_s.casefold())
                s_slug = _slugify_key(norm_s)
                if s_slug:
                    structured_link_keys.add(s_slug)

        for ds_use in opp.internal_datasets:
            ds_nid = _resolve_or_placeholder(ds_use.ref, "dataset")
            _add_edge(ds_nid, opp_nid, "inputs_to_opportunity")

        for ext in opp.external_data_required:
            ext_slug = _slugify_key(ext.source) or ext.source
            ext_nid = _add_node(
                node_id=f"external_data:{ext_slug}",
                label=ext.source,
                node_type="external_data",
                exists_in_vault=True,
                aliases=[ext.source],
                metadata={
                    "acquisition": ext.acquisition,
                    "blocker_severity": ext.blocker_severity,
                },
            )
            _add_edge(ext_nid, opp_nid, "enriches_opportunity")

        for sup in opp.existing_reports_supplemented:
            rep_nid = _resolve_or_placeholder(sup.ref, "existing_report")
            _add_edge(opp_nid, rep_nid, "supplements_report")

        for nr in opp.new_reports_unlocked:
            nr_slug = _slugify_key(nr.title) or nr.title
            nr_nid = _add_node(
                node_id=f"new_report:{opp.id}:{nr_slug}",
                label=nr.title,
                node_type="new_report",
                exists_in_vault=True,
                aliases=[nr.title],
                metadata={"audience": nr.audience, "why_net_new": nr.why_net_new},
            )
            _add_edge(opp_nid, nr_nid, "unlocks_new_report")

        for sh_ref in opp.stakeholders:
            sh_nid = _resolve_or_placeholder(sh_ref, "stakeholder")
            _add_edge(sh_nid, opp_nid, "stakeholder_for")

        for body_link in extract_wikilinks(parsed.body):
            key = body_link.casefold()
            slug = _slugify_key(body_link)
            if key in structured_link_keys or (slug and slug in structured_link_keys):
                continue
            matched_nid = alias_to_node_id.get(key) or alias_to_node_id.get(slug)
            if matched_nid and matched_nid != opp_nid:
                target_type = nodes_by_id[matched_nid]["type"]
                if target_type == "dataset":
                    _add_edge(matched_nid, opp_nid, "inputs_to_opportunity")
                elif target_type == "existing_report":
                    _add_edge(opp_nid, matched_nid, "supplements_report")
                elif target_type == "stakeholder":
                    _add_edge(matched_nid, opp_nid, "stakeholder_for")

    return {"nodes": list(nodes_by_id.values()), "edges": edges}
