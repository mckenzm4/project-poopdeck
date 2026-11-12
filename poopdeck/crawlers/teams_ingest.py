"""Microsoft Teams & context dropzone normalizer (.vtt, .docx, .md, .txt -> vault cards)."""

from __future__ import annotations

import os
import re
import shutil
import xml.etree.ElementTree as ET
import xml.parsers.expat as expat
import zipfile
from collections import OrderedDict
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from poopdeck.config import PoopdeckConfig, load_config
from poopdeck.crawlers import (
    SyncSummary,
    redact_crawler_text as redact_text,
    unwrap_prose_wikilinks,
)
from poopdeck.models import (
    DatasetCard,
    GlossaryCard,
    GlossaryTerm,
    OpenQuestionBank,
    OpportunitySpec,
    PlaybookTracker,
    QuestionItem,
    ReportCard,
    StakeholderCard,
    _normalize_question_category,
)
from poopdeck.templates import build_default_glossary, build_default_question_bank
from poopdeck.vault_io import (
    format_wikilink,
    normalize_wikilink_target,
    parse_markdown_card,
    render_glossary_body,
    render_questions_body,
    render_stakeholder_body,
    upsert_markdown_card,
)

_MAX_DOCX_ZIP_BYTES = 25 * 1024 * 1024
_MAX_DOCX_XML_BYTES = 20 * 1024 * 1024

_SUPPORTED_SUFFIXES: frozenset[str] = frozenset({".vtt", ".docx", ".md", ".txt"})

_PRUNED_INBOX_DIRS: frozenset[str] = frozenset(
    {"databricks", "powerbi", "processed", "archive", "__pycache__", ".git"}
)

_VAULT_ENTITY_DIRS: frozenset[str] = frozenset(
    {"datasets", "reports", "stakeholders", "questions", "staging", "opportunities"}
)

_WINDOWS_RESERVED_STEMS: frozenset[str] = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        "conin$",
        "conout$",
        "clock$",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)

_REDACTED_TOKEN_RE = re.compile(r"\[REDACTED:[A-Z_]+\]", re.IGNORECASE)

_GENERIC_SPEAKER_PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "unknown",
        "speaker",
        "moderator",
        "system",
        "host",
        "guest",
        "attendee",
        "user",
        "http",
        "https",
        "jdbc",
    }
)

_NON_SPEAKER_LABELS: frozenset[str] = frozenset(
    {
        "note",
        "notes",
        "summary",
        "action",
        "action item",
        "action items",
        "question",
        "questions",
        "open question",
        "open questions",
        "kpi",
        "kpis",
        "metric",
        "metrics",
        "term",
        "glossary",
        "definition",
        "topic",
        "date",
        "time",
        "attendees",
        "participants",
        "stakeholders",
        "agenda",
        "pain point",
        "pain points",
        "challenge",
        "blocker",
        "ask",
        "asks",
        "explicit ask",
        "explicit asks",
        "mandate",
        "boss mandate",
        "decision",
        "owner",
        "role",
        "title",
        "position",
        "department",
        "team",
        "stakeholder",
        "contact",
        "email",
        "phone",
        "mobile",
        "influence",
        "influence tier",
        "decision appetite",
        "appetite",
        "owned report",
        "owned reports",
        "reports owned",
        "status",
        "source",
        "formula",
        "token",
        "header",
        "http",
        "https",
        "jdbc",
        "todo",
        "follow up",
        "unknown",
        "speaker",
        "moderator",
        "system",
        "meeting",
        "meeting notes",
        "executive summary",
        "project update",
        "key takeaways",
        "takeaways",
        "next steps",
        "overview",
        "background",
        "context",
        "objective",
    }
)

_NAME_PARTICLES: frozenset[str] = frozenset(
    {"van", "von", "de", "del", "da", "di", "la", "le", "el", "al", "bin", "st", "mc"}
)

_NON_PERSON_NOUN_TOKENS: frozenset[str] = frozenset(
    {
        "header",
        "headers",
        "debug",
        "retry",
        "diagnostic",
        "token",
        "secret",
        "password",
        "credential",
        "connection",
        "query",
        "reconciliation",
        "legacy",
        "temporary",
        "report",
        "reports",
        "table",
        "tables",
        "dataset",
        "datasets",
        "model",
        "models",
        "pipeline",
        "workspace",
        "catalog",
        "schema",
        "memo",
        "notes",
        "note",
        "discovery",
        "summary",
        "review",
        "meeting",
        "sync",
        "checkin",
        "followup",
        "stakeholder",
        "contact",
        "glossary",
        "upstream",
        "scratchpad",
        "http",
        "https",
        "rest",
        "api",
        "sql",
        "jdbc",
    }
)

_DATASET_3PART_FQN_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]{0,127}\.[A-Za-z_][A-Za-z0-9_]{0,127}\.[A-Za-z_][A-Za-z0-9_]{0,127}$"
)
_INLINE_WIKILINK_EXTRACT_RE = re.compile(r"\[\[([^\[\]\r\n]{1,300}+)\]\]")

_TRIVIAL_MEETING_QUESTIONS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*(?:can|could)\s+you\s+(?:hear\s+me|see\s+my\s+screen)", re.IGNORECASE),
    re.compile(r"^\s*(?:right|okay|ok|makes\s+sense|you\s+know|isn't\s+it)\s*\?\s*$", re.IGNORECASE),
    re.compile(r"^\s*how\s+are\s+you", re.IGNORECASE),
)

_KNOWN_KPI_REGEX = re.compile(
    r"\b(?:OTIF|GMROI|EBITDA|NRR|GRR|ARR|MRR|CAC|LTV|MAPE|WMAPE|SLA|DSO|DIO|DPO|OEE|NPS|CSAT|AOV|ROAS|"
    r"Fill\s+Rate|Days\s+of\s+Supply|Gross\s+Margin(?:\s*%)?|Operating\s+Margin|Churn\s+Rate|"
    r"Retention\s+Rate|Stockout\s+Rate|Inventory\s+Turns?|On[-\s]Time\s+Delivery|Lead\s+Time|"
    r"Scrap\s+Rate|Yield\s+Rate|Forecast\s+Accuracy|Forecast\s+Bias|Backorder\s+Rate)\b",
    re.IGNORECASE,
)

_PAIN_SIGNAL_RE = re.compile(
    r"\b(?:struggl\w+|blind\s*spot|manual\s+excel|export\w*\s+to\s+excel|spreadsheet\s+hell|"
    r"too\s+late|firefight\w+|broken|inaccurate|lagging|pain\s+points?|frustrat\w+|"
    r"stockout\w*|overstock\w*|takes\s+(?:days|hours|weeks)|hardcoded|flying\s+blind|"
    r"don't\s+trust|stale\s+data|manual\s+workaround|bottleneck|mismatch)\b",
    re.IGNORECASE,
)

_ASK_SIGNAL_RE = re.compile(
    r"(?:\b(?:can\s+you\s+build|can\s+we\s+get|we\s+need\s+(?:a|an|to)|looking\s+for\s+(?:a|an)|"
    r"please\s+(?:prioritize|build|investigate|check)|would\s+love\s+to\s+(?:see|have)|"
    r"help\s+us\s+(?:predict|forecast|automate|score|identify)|want\s+you\s+to|our\s+ask\s+is)\b"
    r"|(?:\bexplicit[ \t]++asks?|^[ \t]*+asks?)[ \t]*+:)",
    re.IGNORECASE,
)

_MANDATE_SIGNAL_RE = re.compile(
    r"\b(?:boss\s+mandate|mandated?\b|non[-\s]negotiable|(?:ceo|cfo|coo|vp|board)\s+(?:wants|asked|mandated|demanded|requires)|"
    r"must\s+ship|p0\s+priority|top[-\s]down\s+priority|executive\s+directive)\b",
    re.IGNORECASE,
)


def _is_windows_reserved_stem(stem: str) -> bool:
    prefix = (stem or "").split(".", 1)[0].strip(" ").lower()
    if prefix in _WINDOWS_RESERVED_STEMS:
        return True
    is_res = getattr(os.path, "isreserved", lambda _: False)
    return bool(is_res(stem) or is_res(f"{stem}.md"))


def _clean_redacted_label(text: str, fallback: str) -> str:
    if "[REDACTED:" not in text:
        return text.strip() or fallback
    cleaned = re.sub(r"\(\s*\)|\[\s*\]|\{\s*\}", " ", _REDACTED_TOKEN_RE.sub(" ", text))
    collapsed = " ".join(cleaned.split()).strip(" \t\r\n\x0b\x0c_.-:;|/\\,")
    s = re.sub(r"([_-])[\s_-]+", r"\1", collapsed)
    return s or fallback


def _safe_source_filename(fp: Path, redact_secrets: bool) -> str:
    clean_stem = _clean_redacted_label(
        redact_text(fp.stem, redact_secrets=redact_secrets), "inbox-note"
    )
    return f"{clean_stem}{fp.suffix}"


def _safe_slug(text: str) -> str:
    cleaned = _REDACTED_TOKEN_RE.sub(" ", text or "").strip()
    slug = (
        re.sub(r"[^a-z0-9]+", "-", cleaned.lower()).strip("-")[:120].strip("-")
        or "stakeholder"
    )
    return f"{slug}-card" if _is_windows_reserved_stem(slug) else slug


def _slugify_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")


def _decode_file_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _rebase_vault_relative_path(
    vault_dir: Path,
    cfg: PoopdeckConfig,
    raw_cfg_path: str,
    default_subpath: tuple[str, ...],
) -> Path:
    cleaned = (raw_cfg_path or "").strip()
    if not cleaned:
        return vault_dir.joinpath(*default_subpath)
    p = Path(cleaned)
    if p.is_absolute():
        return p
    parts = p.parts
    if parts and parts[0].lower() == "vault":
        return vault_dir.joinpath(*parts[1:])
    cfg_vp = Path((cfg.vault_path or "vault").strip())
    if not cfg_vp.is_absolute() and cfg_vp.parts:
        vp_parts = cfg_vp.parts
        if len(parts) >= len(vp_parts) and tuple(x.lower() for x in parts[: len(vp_parts)]) == tuple(
            x.lower() for x in vp_parts
        ):
            return vault_dir.joinpath(*parts[len(vp_parts) :])
    cand_in_vault = vault_dir / p
    if cand_in_vault.exists():
        return cand_in_vault
    cand_sibling = vault_dir.parent / p
    if cand_sibling.exists():
        return cand_sibling
    return cand_in_vault


_DEDUP_SEEN_CACHE: OrderedDict[int, list[Any]] = OrderedDict()


def _dedup_append(target_list: list[str], item: str) -> None:
    s = (item or "").strip()
    if not s:
        return
    norm = " ".join(s.split()).casefold()
    lid = id(target_list)
    cached = _DEDUP_SEEN_CACHE.get(lid)
    if cached is not None and cached[0] is target_list and cached[1] == len(target_list):
        _DEDUP_SEEN_CACHE.move_to_end(lid)
        seen: set[str] = cached[2]
        if norm in seen:
            return
        target_list.append(s)
        seen.add(norm)
        cached[1] = len(target_list)
        return
    seen = {" ".join(existing.split()).casefold() for existing in target_list}
    if norm not in seen:
        target_list.append(s)
        seen.add(norm)
    _DEDUP_SEEN_CACHE[lid] = [target_list, len(target_list), seen]
    _DEDUP_SEEN_CACHE.move_to_end(lid)
    while len(_DEDUP_SEEN_CACHE) > 256:
        _DEDUP_SEEN_CACHE.popitem(last=False)


_SPEAKER_PAREN_RE = re.compile(r"^([^(\r\n]{1,150}+)\(([^()\r\n]{1,250}+)\)$")
_SPEAKER_DASH_RE = re.compile(r"[ \t\r\n]++(?:—|–|-)[ \t\r\n]++")


def _is_valid_person_name(raw_name: str, *, allow_prose_labels: bool = False) -> bool:
    s = (raw_name or "").strip().strip("*_#`\"' ")
    if not s or len(s) < 2 or len(s) > 70:
        return False
    if "[REDACTED:" in s or "REDACTED:" in s or "@" in s or "http://" in s.lower() or "https://" in s.lower():
        return False
    if re.search(r"[\d:|/\\=—–_]", s):
        return False
    low = s.lower()
    if low in _GENERIC_SPEAKER_PLACEHOLDERS:
        return False
    if not allow_prose_labels and low in _NON_SPEAKER_LABELS:
        return False
    if re.match(
        r"^(?:speaker|person|user|participant|guest|attendee|host|moderator|unknown)\s*\d*$",
        low,
    ):
        return False
    if not re.search(r"[A-Za-z]", s):
        return False
    words = s.split()
    max_words = 6 if allow_prose_labels else 4
    if len(words) > max_words:
        return False
    if not allow_prose_labels:
        for w in words:
            w_clean = w.strip(".,'()-")
            w_alpha = re.sub(r"[^A-Za-z]", "", w_clean)
            if not w_alpha:
                return False
            if w_alpha.lower() in _NON_PERSON_NOUN_TOKENS:
                return False
            if w_clean.lower() in _NAME_PARTICLES:
                continue
            if not w_clean[0].isupper():
                return False
            if len(w_alpha) >= 3 and w_alpha.isupper():
                return False
    return True


def _split_speaker_and_role(
    raw_speaker: str, *, allow_prose_labels: bool = False
) -> tuple[str, str, str]:
    """Parse `Sarah Jenkins (VP Operations - Supply Chain)` or `1:1 Notes — Sarah Jenkins (VP)`."""
    s = (raw_speaker or "").strip().strip("*_`")
    role = ""
    dept = ""
    m_paren = _SPEAKER_PAREN_RE.match(s) if len(s) <= 420 and s.endswith(")") else None
    if m_paren:
        s = m_paren.group(1).strip()
        inside = m_paren.group(2).strip()
        if " - " in inside:
            role, dept = [p.strip() for p in inside.split(" - ", 1)]
        elif " — " in inside:
            role, dept = [p.strip() for p in inside.split(" — ", 1)]
        elif "," in inside:
            role, dept = [p.strip() for p in inside.split(",", 1)]
        else:
            role = inside
    m_dash = _SPEAKER_DASH_RE.search(s) if len(s) <= 420 else None
    if m_dash:
        left = s[: m_dash.start()].strip()
        right = s[m_dash.end() :].strip()
        if _is_valid_person_name(left, allow_prose_labels=allow_prose_labels):
            s = left
            if not role:
                role = right
        elif _is_valid_person_name(right, allow_prose_labels=allow_prose_labels):
            s = right
    return s.strip(), role.strip(), dept.strip()


def _infer_influence_tier(role: str, text_blob: str = "") -> str:
    probe = f"{role} {text_blob[:200]}".lower()
    if re.search(
        r"\b(?:vp|vice\s+president|svp|evp|cfo|ceo|coo|cio|cto|cdo|chief|president|head\s+of)\b",
        probe,
    ):
        return "exec"
    if re.search(
        r"\b(?:director|manager|gm|general\s+manager|team\s+lead|lead\s+of)\b",
        probe,
    ):
        return "manager"
    if re.search(
        r"\b(?:engineer|analyst|developer|architect|scientist|specialist|dba|admin|administrator)\b",
        probe,
    ):
        return "sme"
    return "partner"


def _infer_decision_appetite(utterances: list[str]) -> str:
    blob = " ".join(utterances).lower()
    if not blob:
        return "unknown"
    if re.search(
        r"\b(?:ready\s+to\s+pilot|need\s+this\s+now|greenlight|top\s+priority|sponsor|mandate|p0|asap|move\s+fast)\b",
        blob,
    ):
        return "high"
    if re.search(
        r"\b(?:skeptic\w*|burned\s+before|no\s+appetite|pushed\s+back|budget\s+freeze|resistant)\b",
        blob,
    ):
        return "low"
    if re.search(
        r"\b(?:cautious|pilot\s+first|show\s+me|open\s+to|prove\s+it|directional)\b",
        blob,
    ):
        return "medium"
    return "unknown"


def _infer_department(role: str, text_blob: str = "") -> str:
    probe = f"{role} {text_blob}".lower()
    dept_map = (
        ("Supply Chain", r"\b(?:supply\s+chain|logistics|inventory|warehouse|procurement|fulfillment|s&op)\b"),
        ("Operations", r"\b(?:operations|manufacturing|plant|fleet|dispatch)\b"),
        ("Finance", r"\b(?:finance|fp&a|accounting|controller|cfo|margin|treasury)\b"),
        ("Commercial & Sales", r"\b(?:sales|commercial|revenue|gtm|marketing|merchandising|pricing)\b"),
        ("Data & Analytics", r"\b(?:data\s+engineering|analytics|bi\b|power\s+bi|platform|data\s+science)\b"),
        ("IT & Security", r"\b(?:it\b|security|infosec|infrastructure|governance)\b"),
    )
    for label, pat in dept_map:
        if re.search(pat, probe):
            return label
    return ""


def _infer_question_category(question_text: str, speaker_role: str = "") -> str:
    probe = f"{question_text} {speaker_role}".lower()
    if re.search(
        r"\b(?:token|pat\b|service\s+principal|admin|firewall|dlp|pii|security|permission|credential|sso|oauth)\b",
        probe,
    ):
        return "it_security"
    if re.search(
        r"\b(?:databricks|spark|unity\s+catalog|hive|schema|table|bronze|silver|gold|etl|warehouse|lineage|freshness|pipeline|delta)\b",
        probe,
    ):
        return "data_engineering"
    if re.search(
        r"\b(?:power\s*bi|pbi|dax|semantic\s+model|pbip|tmdl|dashboard|report|refresh|workspace)\b",
        probe,
    ):
        return "power_bi"
    if re.search(
        r"\b(?:roi|margin|revenue|cost|dollar|usd|budget|cfo|finance|baseline|savings|ebitda|price|valuation|payback)\b",
        probe,
    ):
        return "business_finance"
    return "manager"


# ---------------------------------------------------------------------------
# Format Parsers (.vtt, .docx, .md / .txt)
# ---------------------------------------------------------------------------

_VTT_TIMESTAMP_RE = re.compile(
    r"^\s*\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3}\s*-->\s*\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3}"
)
_VTT_VOICE_TAG_RE = re.compile(
    r"<v(?:\.[A-Za-z0-9_-]++)*+[ \t]++([^<>\r\n]{1,240}+)>|(</v[ \t]*>)",
    re.IGNORECASE,
)
_VTT_VOICE_CLOSED_RE = re.compile(
    r"<v(?:\.[A-Za-z0-9_-]++)*+[ \t]++([^<>\r\n]{1,240}+)>((?:[^<]|<(?!/v[ \t]*>))*+)</v[ \t]*>",
    re.IGNORECASE,
)
_VTT_VOICE_OPEN_RE = re.compile(
    r"^[ \t]*+<v(?:\.[A-Za-z0-9_-]++)*+[ \t]++([^<>\r\n]{1,240}+)>([\s\S]*)$",
    re.IGNORECASE,
)
_VTT_INLINE_TAGS_RE = re.compile(r"</?[A-Za-z0-9._:-]++(?:[ \t]++[^<>\r\n]*+)?+>")
_SPEAKER_COLON_RE = re.compile(
    r"^[ \t]*+(?:[-*][ \t]++)?(?:\*\*([^*\r\n]{2,65}+)\*\*|([A-Z][A-Za-z0-9 .,'()-]{0,59}?[A-Za-z0-9.)']))[ \t]*+:[ \t]++(.+)$"
)


def _parse_vtt_content(raw_text: str, redact_secrets: bool) -> dict[str, Any]:
    lines = raw_text.splitlines()
    turns: list[tuple[str, str, str, str]] = []  # (name, role, dept, text)
    free_lines: list[str] = []

    active_speaker: tuple[str, str, str] | None = None
    in_header_block = False
    in_cue = False

    for raw_line in lines:
        s = raw_line.strip()
        if not s:
            in_cue = False
            in_header_block = False
            active_speaker = None
            continue
        if _VTT_TIMESTAMP_RE.match(s):
            in_cue = True
            in_header_block = False
            active_speaker = None
            continue
        if not in_cue:
            if s.upper().startswith("WEBVTT"):
                continue
            if re.match(r"^(?:NOTE|STYLE|REGION)(?:\s|$)", s):
                in_header_block = True
                continue
            if in_header_block:
                continue
            # Cue identifier line (digits or UUID right before timestamp)
            if re.fullmatch(r"[0-9a-fA-F-]{1,40}", s):
                continue

        s_low = s.lower()
        if "<v" in s_low and "</v" in s_low:
            ms = list(_VTT_VOICE_TAG_RE.finditer(s))
            pairs = [
                (ms[i].group(1), s[ms[i].end() : ms[i + 1].start()])
                for i in range(len(ms) - 1)
                if ms[i].group(1) and ms[i + 1].group(2)
            ]
            if pairs:
                for sp_grp, utt_grp in pairs:
                    sp_raw = redact_text(sp_grp.strip(), redact_secrets=redact_secrets)
                    utt = redact_text(
                        _VTT_INLINE_TAGS_RE.sub("", utt_grp).strip(),
                        redact_secrets=redact_secrets,
                    )
                    name, role, dept = _split_speaker_and_role(
                        sp_raw, allow_prose_labels=True
                    )
                    if _is_valid_person_name(name, allow_prose_labels=True) and utt:
                        turns.append((name, role, dept, utt))
                        active_speaker = (name, role, dept)
                    elif utt:
                        free_lines.append(utt)
                if ms[-1].group(1):
                    sp_raw = redact_text(
                        ms[-1].group(1).strip(), redact_secrets=redact_secrets
                    )
                    utt = redact_text(
                        _VTT_INLINE_TAGS_RE.sub("", s[ms[-1].end() :]).strip(),
                        redact_secrets=redact_secrets,
                    )
                    name, role, dept = _split_speaker_and_role(
                        sp_raw, allow_prose_labels=True
                    )
                    if _is_valid_person_name(name, allow_prose_labels=True):
                        active_speaker = (name, role, dept)
                        if utt:
                            turns.append((name, role, dept, utt))
                    elif utt:
                        free_lines.append(utt)
                continue

        m_open = _VTT_VOICE_OPEN_RE.match(s) if "<v" in s_low else None
        if m_open:
            sp_raw = redact_text(m_open.group(1).strip(), redact_secrets=redact_secrets)
            utt = redact_text(
                _VTT_INLINE_TAGS_RE.sub("", m_open.group(2)).strip(),
                redact_secrets=redact_secrets,
            )
            name, role, dept = _split_speaker_and_role(sp_raw, allow_prose_labels=True)
            if _is_valid_person_name(name, allow_prose_labels=True):
                active_speaker = (name, role, dept)
                if utt:
                    turns.append((name, role, dept, utt))
            elif utt:
                free_lines.append(utt)
            continue

        clean_s = redact_text(
            _VTT_INLINE_TAGS_RE.sub("", s).strip(), redact_secrets=redact_secrets
        )
        if not clean_s:
            continue

        m_col = _SPEAKER_COLON_RE.match(clean_s)
        if m_col:
            sp_cand = (m_col.group(1) or m_col.group(2) or "").strip()
            utt_cand = m_col.group(3).strip()
            name, role, dept = _split_speaker_and_role(sp_cand)
            if _is_valid_person_name(name):
                active_speaker = (name, role, dept)
                turns.append((name, role, dept, utt_cand))
                continue

        if active_speaker is not None:
            prev_n, prev_r, prev_d, prev_t = turns[-1] if turns else (*active_speaker, "")
            if turns and (prev_n, prev_r, prev_d) == active_speaker:
                turns[-1] = (prev_n, prev_r, prev_d, f"{prev_t} {clean_s}".strip())
            else:
                turns.append((*active_speaker, clean_s))
        else:
            free_lines.append(clean_s)

    return {
        "frontmatter": {},
        "turns": turns,
        "lines": free_lines,
        "tables": [],
    }


def _local_xml_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _extract_w_p_text(p_elem: ET.Element) -> tuple[str, str]:
    """Extract text and paragraph style from `<w:p>` while skipping `<w:del>` revisions."""
    style_val = ""
    has_list_num = False
    chunks: list[str] = []

    def _walk_inline(node: ET.Element) -> None:
        nonlocal style_val, has_list_num
        lname = _local_xml_name(node.tag)
        if lname == "del":
            return
        if lname == "pPr":
            for child in node:
                cl = _local_xml_name(child.tag)
                if cl == "pStyle":
                    for attr_k, attr_v in child.attrib.items():
                        if _local_xml_name(attr_k) == "val":
                            style_val = attr_v
                elif cl == "numPr":
                    has_list_num = True
            return
        if lname == "t":
            if node.text:
                chunks.append(node.text)
            return
        if lname == "tab":
            chunks.append("\t")
            return
        if lname in {"br", "cr"}:
            chunks.append("\n")
            return
        for child in node:
            _walk_inline(child)

    _walk_inline(p_elem)
    text = "".join(chunks).strip()
    if not text:
        return "", style_val
    if style_val.lower().startswith("heading1"):
        return f"# {text}", style_val
    if style_val.lower().startswith("heading"):
        return f"## {text}", style_val
    if has_list_num or "list" in style_val.lower():
        return f"- {text}", style_val
    return text, style_val


def _parse_safe_docx_xml(raw_xml: bytes) -> ET.Element:
    enc: str | None = None
    if raw_xml.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        enc = "utf-32"
    elif len(raw_xml) >= 4 and raw_xml[:4] in (
        b"<\x00\x00\x00",
        b" \x00\x00\x00",
        b"\t\x00\x00\x00",
        b"\n\x00\x00\x00",
        b"\r\x00\x00\x00",
    ):
        enc = "utf-32-le"
    elif len(raw_xml) >= 4 and raw_xml[:4] in (
        b"\x00\x00\x00<",
        b"\x00\x00\x00 ",
        b"\x00\x00\x00\t",
        b"\x00\x00\x00\n",
        b"\x00\x00\x00\r",
    ):
        enc = "utf-32-be"
    elif raw_xml.startswith((b"\xff\xfe", b"\xfe\xff")):
        enc = "utf-16"
    elif len(raw_xml) >= 2 and raw_xml[1:2] == b"\x00" and raw_xml[0:1] != b"\x00":
        enc = "utf-16-le"
    elif len(raw_xml) >= 2 and raw_xml[0:1] == b"\x00" and raw_xml[1:2] != b"\x00":
        enc = "utf-16-be"

    if enc is not None:
        decoded = raw_xml.decode(enc)
        decoded = re.sub(
            r'(^\s*<\?xml\b[^>]*?\bencoding\s*=\s*["\'])[^"\']+(["\'])',
            r"\1UTF-8\2",
            decoded,
            count=1,
            flags=re.IGNORECASE,
        )
        norm_xml = decoded.encode("utf-8")
    else:
        norm_xml = raw_xml[3:] if raw_xml.startswith(b"\xef\xbb\xbf") else raw_xml

    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", norm_xml, re.IGNORECASE):
        raise ValueError("Unsafe XML DOCTYPE/ENTITY declaration in .docx")

    ep = expat.ParserCreate()

    def _reject_dtd(*_args: object) -> None:
        raise ValueError("Unsafe XML DOCTYPE/ENTITY declaration in .docx")

    ep.StartDoctypeDeclHandler = _reject_dtd
    ep.EntityDeclHandler = _reject_dtd
    ep.UnparsedEntityDeclHandler = _reject_dtd
    ep.NotationDeclHandler = _reject_dtd
    ep.ExternalEntityRefHandler = lambda *_a: (_reject_dtd(), 0)[1]
    try:
        ep.Parse(norm_xml, True)
    except expat.ExpatError as exc:
        raise ET.ParseError(str(exc)) from exc
    return ET.fromstring(norm_xml)


def _parse_docx_file(docx_path: Path, redact_secrets: bool) -> dict[str, Any]:
    fsize = docx_path.stat().st_size
    if fsize > _MAX_DOCX_ZIP_BYTES:
        raise ValueError(
            f"DOCX archive {docx_path.name} exceeds {_MAX_DOCX_ZIP_BYTES // (1024 * 1024)} MB safety limit"
        )

    with zipfile.ZipFile(docx_path, "r") as zf:
        if "word/document.xml" not in zf.namelist():
            raise ValueError(f"{docx_path.name} is missing word/document.xml")
        with zf.open("word/document.xml", "r") as xml_fh:
            xml_bytes = xml_fh.read(_MAX_DOCX_XML_BYTES + 1)
        if len(xml_bytes) > _MAX_DOCX_XML_BYTES:
            raise ValueError(f"{docx_path.name} word/document.xml exceeds 20 MB limit")

    root = _parse_safe_docx_xml(xml_bytes)
    body_elem: ET.Element | None = None
    for child in root:
        if _local_xml_name(child.tag) == "body":
            body_elem = child
            break
    if body_elem is None:
        body_elem = root

    paragraphs: list[str] = []
    tables: list[list[list[str]]] = []

    def _walk_body_blocks(container: ET.Element) -> None:
        for elem in container:
            lname = _local_xml_name(elem.tag)
            if lname == "p":
                p_text, _ = _extract_w_p_text(elem)
                if p_text:
                    paragraphs.append(
                        redact_text(p_text, redact_secrets=redact_secrets)
                    )
            elif lname == "tbl":
                rows_out: list[list[str]] = []
                for tr in elem:
                    if _local_xml_name(tr.tag) != "tr":
                        continue
                    row_cells: list[str] = []
                    for tc in tr:
                        if _local_xml_name(tc.tag) != "tc":
                            continue
                        cell_parts: list[str] = []
                        for tc_child in tc:
                            if _local_xml_name(tc_child.tag) == "p":
                                ct, _ = _extract_w_p_text(tc_child)
                                if ct:
                                    # Strip synthetic list prefix inside table cell
                                    cell_parts.append(re.sub(r"^[-#]+\s+", "", ct))
                        cell_str = redact_text(
                            " ".join(cell_parts).strip(), redact_secrets=redact_secrets
                        )
                        row_cells.append(cell_str)
                    if any(row_cells):
                        rows_out.append(row_cells)
                if rows_out:
                    tables.append(rows_out)
            elif lname == "sdt":
                for sdt_child in elem:
                    if _local_xml_name(sdt_child.tag) == "sdtContent":
                        _walk_body_blocks(sdt_child)

    _walk_body_blocks(body_elem)

    # Also parse speaker turns inside DOCX paragraphs while keeping section lines
    turns: list[tuple[str, str, str, str]] = []
    lines: list[str] = []
    for p_line in paragraphs:
        p_strip = p_line.lstrip()
        m_col = (
            _SPEAKER_COLON_RE.match(p_line)
            if not p_strip.startswith(("#", "-", "*", "•")) and "|" not in p_line
            else None
        )
        if m_col:
            sp_cand = (m_col.group(1) or m_col.group(2) or "").strip()
            utt_cand = m_col.group(3).strip()
            name, role, dept = _split_speaker_and_role(sp_cand)
            if _is_valid_person_name(name):
                turns.append((name, role, dept, utt_cand))
                continue
        lines.append(p_line)

    return {
        "frontmatter": {},
        "turns": turns,
        "lines": lines,
        "tables": tables,
    }


_FRONTMATTER_BLOCK_RE = re.compile(
    r"\A---[ \t]*\r?\n(?:([\s\S]*?)\r?\n)?---[ \t]*(?:\r?\n|$)"
)
_FM_IDENT_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_FM_LOWER_KEY_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,63}$")
_FM_KNOWN_META_KEYS: frozenset[str] = frozenset(
    {
        "stakeholders",
        "attendees",
        "participants",
        "questions",
        "open_questions",
        "glossary",
        "terms",
        "definitions",
        "title",
        "date",
        "meeting_date",
        "name",
        "role",
        "department",
        "tags",
        "summary",
        "topic",
        "author",
        "owner",
        "id",
        "status",
        "category",
        "type",
        "source",
        "channel",
        "team",
        "project",
    }
)


def _scrub_nested(val: Any, redact_secrets: bool) -> Any:
    if isinstance(val, str):
        return redact_text(val, redact_secrets=redact_secrets)
    if isinstance(val, list):
        return [_scrub_nested(v, redact_secrets) for v in val]
    if isinstance(val, dict):
        return {
            redact_text(str(k), redact_secrets=redact_secrets): _scrub_nested(
                v, redact_secrets
            )
            for k, v in val.items()
        }
    return val


def _parse_markdown_or_text(raw_text: str, redact_secrets: bool) -> dict[str, Any]:
    fm: dict[str, Any] = {}
    body = raw_text
    m_fm = _FRONTMATTER_BLOCK_RE.match(raw_text)
    if m_fm:
        raw_fm = m_fm.group(1) or ""
        if not raw_fm.strip():
            body = raw_text[m_fm.end() :]
        elif not re.search(r"(?m)^#{1,6}\s+\S", raw_fm):
            try:
                loaded = yaml.safe_load(raw_fm)
            except Exception:
                loaded = None
            if (
                isinstance(loaded, dict)
                and loaded
                and all(isinstance(k, str) and _FM_IDENT_KEY_RE.match(k) for k in loaded)
                and (
                    all(_FM_LOWER_KEY_RE.match(k) for k in loaded)
                    or bool(set(loaded) & _FM_KNOWN_META_KEYS)
                )
            ):
                fm = _scrub_nested(loaded, redact_secrets)
                body = raw_text[m_fm.end() :]

    scrubbed_body = redact_text(body, redact_secrets=redact_secrets)
    turns: list[tuple[str, str, str, str]] = []
    lines: list[str] = []
    tables: list[list[list[str]]] = []
    cur_table: list[list[str]] = []

    for raw_ln in scrubbed_body.splitlines():
        s = raw_ln.strip()
        if s.startswith("|") and s.endswith("|") and s.count("|") >= 2:
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c or "---") for c in cells):
                continue
            cur_table.append(cells)
            continue
        else:
            if cur_table:
                tables.append(cur_table)
                cur_table = []

        if not s:
            lines.append("")
            continue
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", s):
            continue

        if not s.startswith(("#", "-", "*", "•")) and "|" not in s:
            m_col = _SPEAKER_COLON_RE.match(s)
            if m_col:
                sp_cand = (m_col.group(1) or m_col.group(2) or "").strip()
                utt_cand = m_col.group(3).strip()
                name, role, dept = _split_speaker_and_role(sp_cand)
                if _is_valid_person_name(name):
                    turns.append((name, role, dept, utt_cand))
                    continue

        lines.append(s)

    if cur_table:
        tables.append(cur_table)

    return {
        "frontmatter": fm,
        "turns": turns,
        "lines": lines,
        "tables": tables,
    }


# ---------------------------------------------------------------------------
# Entity Extraction & Vault Knowledge Index
# ---------------------------------------------------------------------------


def _build_vault_ref_catalog(
    vault_dir: Path,
) -> tuple[set[str], dict[str, str]]:
    """Return `(known_keys, report_lookup)` for strict wikilink safety."""
    known_keys: set[str] = set()
    report_lookup: dict[str, str] = {}

    def _reg(*vals: str) -> None:
        for v in vals:
            s = (v or "").strip()
            if s:
                known_keys.add(s.casefold())
                sk = _slugify_key(s)
                if sk:
                    known_keys.add(sk)

    ds_dir = vault_dir / "datasets"
    if ds_dir.is_dir():
        for p in sorted(ds_dir.glob("*.md")):
            try:
                c = DatasetCard.model_validate(parse_markdown_card(p).frontmatter)
                _reg(c.id, c.fqn, c.table_name, p.stem, f"datasets/{p.stem}")
            except Exception:
                _reg(p.stem)

    rep_dir = vault_dir / "reports"
    if rep_dir.is_dir():
        for p in sorted(rep_dir.glob("*.md")):
            try:
                rc = ReportCard.model_validate(parse_markdown_card(p).frontmatter)
                ws_title = f"{rc.workspace}__{rc.title}"
                _reg(
                    rc.id,
                    rc.title,
                    rc.semantic_model,
                    ws_title,
                    f"{rc.workspace}__{rc.semantic_model}" if rc.semantic_model else "",
                    p.stem,
                    f"reports/{p.stem}",
                )
                report_lookup[rc.title.casefold()] = rc.title
                report_lookup[rc.id.casefold()] = rc.title
                report_lookup[ws_title.casefold()] = rc.title
                report_lookup[p.stem.casefold()] = rc.title
            except Exception:
                _reg(p.stem)

    sh_dir = vault_dir / "stakeholders"
    if sh_dir.is_dir():
        for p in sorted(sh_dir.glob("*.md")):
            try:
                sc = StakeholderCard.model_validate(parse_markdown_card(p).frontmatter)
                _reg(sc.id, sc.name, p.stem, f"stakeholders/{p.stem}")
            except Exception:
                _reg(p.stem)

    g_path = vault_dir / "glossary.md"
    if g_path.is_file():
        _reg("glossary")
        try:
            gcard = GlossaryCard.model_validate(parse_markdown_card(g_path).frontmatter)
            _reg(*(t.term for t in gcard.terms))
        except Exception:
            pass

    q_dir = vault_dir / "questions"
    if q_dir.is_dir():
        for p in sorted(q_dir.glob("*.md")):
            _reg(p.stem, f"questions/{p.stem}")
            if p.stem in {"day1-90-playbook", "day-1-90-playbook"}:
                _reg("day1-90-playbook", "day-1-90-playbook")
            try:
                fm_q = parse_markdown_card(p).frontmatter
                if "questions" in fm_q:
                    bank = OpenQuestionBank.model_validate(fm_q)
                    _reg(*(q.id for q in bank.questions))
                elif "phases" in fm_q:
                    tracker = PlaybookTracker.model_validate(fm_q)
                    _reg(*(ph.id for ph in tracker.phases))
            except Exception:
                pass

    for opp_rel in ("opportunities", "staging/opportunities"):
        odir = vault_dir / opp_rel
        if odir.is_dir():
            for p in sorted(odir.glob("*.md")):
                _reg(p.stem, f"{opp_rel}/{p.stem}")
                try:
                    opp = OpportunitySpec.model_validate(parse_markdown_card(p).frontmatter)
                    _reg(opp.id, opp.title)
                except Exception:
                    pass

    return known_keys, report_lookup


_BULLET_TERM_RE = re.compile(
    r"^[-*•][ \t]*+(?:\*\*([^*\r\n]{2,50}+)\*\*|([A-Z][A-Z0-9 _/-]{1,35}))[ \t]*+(?::|—|–|-|=)[ \t]*+([^\r\n]{5,250}+)$"
)
_PREFIXED_GLOSSARY_LINE_RE = re.compile(
    r"^(?:[-*•][ \t]*+)?(?:glossary(?:[ \t]+(?:note|term))?|term|definition)[ \t]*+(?::|—|–|-)[ \t]*+(.+)$",
    re.IGNORECASE,
)
_PREFIXED_TERM_PAIR_RE = re.compile(
    r"^(?:\*\*([^*\r\n]{2,60}+)\*\*|([A-Z][A-Za-z0-9 _/-]{1,45}))[ \t]*+(?::|—|–|-|=)[ \t]*+([^\r\n]{5,250}+)$"
)
_GLOSSARY_NOTE_CLAUSE_RE = re.compile(
    r"\bglossary(?:[ \t]++note)?[ \t]*+:[ \t]*+([^.!?\r\n]{8,400})",
    re.IGNORECASE,
)
_GLOSSARY_CLAUSE_PAIR_RE = re.compile(
    r"^[ \t]*+(?:\*{1,2})?([A-Z][A-Za-z0-9/-]*(?:[ \t]++(?:of|in|per|on|to|for|by|and|[A-Z][A-Za-z0-9/-]*)){0,5})(?:\*{1,2})?[ \t]++(?:stands[ \t]++for|means|is[ \t]++defined[ \t]++as|equals|is)[ \t]++(.{5,200}?)[ \t]*+$"
)
_KV_META_SEGMENT_RE = re.compile(
    r"^(?:[-*•][ \t]*+)?(?:\*{1,2})?(stakeholder|name|role|title|position|department|team|influence(?:[ \t]++tier)?|decision[ \t]++appetite|appetite|owned[ \t]++reports?|reports[ \t]++owned|pain[ \t]++points?|explicit[ \t]++asks?|asks?|boss[ \t]++mandates?|mandates?|contact|email|phone|mobile)(?:\*{1,2})?[ \t]*+:[ \t]*+(.+)$",
    re.IGNORECASE,
)
_OWNERSHIP_CLAUSE_RE = re.compile(
    r"\b(?:owns?|owner\s+of|maintains?|stewards?|certified\s+semantic\s+models?\s+backing)\b",
    re.IGNORECASE,
)
_ATTENDEE_BULLET_RE = re.compile(
    r"^[-*•][ \t]*+(?:\*{1,2})?([A-Z][A-Za-z .'-]{1,45}?)(?:\*{1,2})?[ \t]*+(?:—|–|-|:|\()[ \t]*+([^)\r\n]{1,240})\)?$"
)


def _sanitize_prose_wikilinks(text: str, known_keys: set[str]) -> str:
    return unwrap_prose_wikilinks(text, known_keys)


def _clean_owned_report_wikilinks(raw_refs: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for r in raw_refs:
        norm = normalize_wikilink_target(str(r))
        if not norm or "[REDACTED:" in norm or "REDACTED:" in norm:
            continue
        if _DATASET_3PART_FQN_RE.fullmatch(norm):
            continue
        if norm.casefold() not in seen:
            seen.add(norm.casefold())
            out.append(format_wikilink(norm))
    return out


def _extract_report_refs_from_text(text: str, *, allow_bare: bool = False) -> list[str]:
    refs: list[str] = []
    wl_matches = [m.group(1) for m in _INLINE_WIKILINK_EXTRACT_RE.finditer(text)]
    if wl_matches:
        for raw_wl in wl_matches:
            norm = normalize_wikilink_target(raw_wl)
            if norm and not _DATASET_3PART_FQN_RE.fullmatch(norm) and "[REDACTED:" not in norm:
                refs.append(format_wikilink(norm))
        return refs
    if allow_bare:
        for part in re.split(r"[,;]", text):
            norm = normalize_wikilink_target(part.strip().strip("*_`\"' "))
            if norm and not _DATASET_3PART_FQN_RE.fullmatch(norm) and "[REDACTED:" not in norm:
                refs.append(format_wikilink(norm))
    return refs


def _filter_valid_wikilinks(raw_refs: list[str], known_keys: set[str]) -> list[str]:
    valid: list[str] = []
    seen: set[str] = set()
    for r in raw_refs:
        norm = normalize_wikilink_target(str(r))
        if not norm or "[REDACTED:" in norm or "REDACTED:" in norm:
            continue
        probes = {norm.casefold(), _slugify_key(norm)}
        if probes & known_keys and norm.casefold() not in seen:
            seen.add(norm.casefold())
            valid.append(format_wikilink(norm))
    return valid


def _as_seq(val: Any) -> list[Any]:
    if val is None or isinstance(val, (bool, int, float)):
        return []
    if isinstance(val, (list, tuple, set, frozenset)):
        return list(val)
    if isinstance(val, str):
        return [val] if val.strip() else []
    if isinstance(val, dict):
        return [val]
    return []


def _extract_entities_from_doc(
    doc: dict[str, Any],
    source_filename: str,
    report_lookup: dict[str, str],
    stakeholders_acc: dict[str, dict[str, Any]],
    glossary_acc: dict[str, dict[str, Any]],
    questions_acc: list[dict[str, Any]],
) -> None:
    fm: dict[str, Any] = doc["frontmatter"] if isinstance(doc.get("frontmatter"), dict) else {}
    turns: list[tuple[str, str, str, str]] = doc.get("turns") or []
    lines: list[str] = doc.get("lines") or []
    tables: list[list[list[str]]] = doc.get("tables") or []

    doc_stakeholder_ids: list[str] = []
    doc_glossary_keys: list[str] = []
    q_start_idx = len(questions_acc)

    def _ensure_sh(
        name: str,
        role: str = "",
        dept: str = "",
        *,
        allow_prose_labels: bool = False,
    ) -> dict[str, Any] | None:
        clean_name = re.sub(r"\s+", " ", (name or "").strip().strip("*_#`\"' "))
        if not _is_valid_person_name(clean_name, allow_prose_labels=allow_prose_labels):
            return None
        sh_id = _safe_slug(clean_name)
        entry = stakeholders_acc.get(sh_id)
        if entry is None:
            entry = {
                "id": sh_id,
                "name": clean_name,
                "role": "",
                "department": "",
                "influence_tier": "partner",
                "decision_appetite": "unknown",
                "owned_reports": [],
                "kpis_cared_about": [],
                "pain_points": [],
                "explicit_asks": [],
                "boss_mandate_asks": [],
                "_utterances": [],
            }
            stakeholders_acc[sh_id] = entry
        if role and not entry["role"]:
            entry["role"] = role
        if dept and not entry["department"]:
            entry["department"] = dept
        if sh_id not in doc_stakeholder_ids:
            doc_stakeholder_ids.append(sh_id)
        return entry

    def _add_glossary(
        term: str,
        definition: str,
        formula: str | None = None,
        speaker_id: str | None = None,
    ) -> None:
        t_clean = term.strip().strip("*_`\"' ")
        d_clean = definition.strip().strip("*_`\"' ")
        if not t_clean or not d_clean or len(t_clean) > 60 or "[REDACTED:" in t_clean:
            return
        key = t_clean.casefold()
        existing = glossary_acc.get(key)
        refs = [speaker_id] if speaker_id else (doc_stakeholder_ids[:1] if doc_stakeholder_ids else [])
        if existing is None:
            doc_glossary_keys.append(key)
            glossary_acc[key] = {
                "term": t_clean,
                "definition": d_clean,
                "source": source_filename,
                "formula_or_logic": formula,
                "source_refs": list(refs),
            }
        else:
            if len(d_clean) > len(existing["definition"]):
                existing["definition"] = d_clean
            if formula and not existing.get("formula_or_logic"):
                existing["formula_or_logic"] = formula
            for r in refs:
                if r not in existing["source_refs"]:
                    existing["source_refs"].append(r)

    def _add_question(
        q_text: str,
        who: str = "",
        category: str = "",
        speaker_id: str | None = None,
    ) -> None:
        qc = re.sub(r"^[-*•\d.)\s]+", "", (q_text or "").strip()).strip()
        qc = re.sub(r"^(?:open\s+question|question|q|todo)\s*:\s*", "", qc, flags=re.IGNORECASE).strip()
        if len(qc) < 12 or "[REDACTED:" in qc and len(qc) < 25:
            return
        if any(p.search(qc) for p in _TRIVIAL_MEETING_QUESTIONS):
            return
        refs = [speaker_id] if speaker_id else (doc_stakeholder_ids[:1] if doc_stakeholder_ids else [])
        norm_cat = ""
        if isinstance(category, str) and category.strip():
            try:
                norm_cat = _normalize_question_category(category)
            except Exception:
                norm_cat = ""
        resolved_cat = (
            norm_cat
            if norm_cat in {"manager", "it_security", "data_engineering", "power_bi", "business_finance"}
            else _infer_question_category(qc, who)
        )
        questions_acc.append(
            {
                "question": qc,
                "who_to_ask": who or "Manager",
                "category": resolved_cat,
                "linked_refs": list(refs),
                "topic": source_filename,
            }
        )

    # 1. Frontmatter extraction
    fm_sh_list: list[Any] = [
        *_as_seq(fm.get("stakeholders")),
        *_as_seq(fm.get("attendees")),
        *_as_seq(fm.get("participants")),
    ]
    if isinstance(fm.get("name"), str) and fm["name"].strip() and (
        fm.get("role") or fm.get("pain_points") or fm.get("kpis_cared_about")
    ):
        fm_sh_list.append(fm)

    for item in fm_sh_list:
        if isinstance(item, str):
            n, r, d = _split_speaker_and_role(item)
            _ensure_sh(n, r, d)
        elif isinstance(item, dict):
            raw_nm = item.get("name")
            if not isinstance(raw_nm, str) or not raw_nm.strip():
                continue
            sh = _ensure_sh(
                raw_nm,
                item["role"] if isinstance(item.get("role"), str) else "",
                item["department"] if isinstance(item.get("department"), str) else "",
            )
            if sh is not None:
                if item.get("influence_tier") in {"exec", "manager", "partner", "sme"}:
                    sh["influence_tier"] = str(item["influence_tier"])
                if item.get("decision_appetite") in {"high", "medium", "low", "unknown"}:
                    sh["decision_appetite"] = str(item["decision_appetite"])
                for lk in (
                    "owned_reports",
                    "kpis_cared_about",
                    "pain_points",
                    "explicit_asks",
                    "boss_mandate_asks",
                ):
                    for v in _as_seq(item.get(lk)):
                        if isinstance(v, str) and v.strip():
                            _dedup_append(sh[lk], v.strip())

    raw_gloss = (
        fm.get("glossary")
        if fm.get("glossary") is not None
        else (fm.get("terms") if fm.get("terms") is not None else fm.get("definitions"))
    )
    if isinstance(raw_gloss, dict):
        if "term" in raw_gloss or "acronym" in raw_gloss:
            gt = raw_gloss.get("term") or raw_gloss.get("acronym")
            gd = raw_gloss.get("definition") or raw_gloss.get("meaning")
            gf = raw_gloss.get("formula")
            if isinstance(gt, str) and isinstance(gd, str):
                _add_glossary(gt, gd, formula=gf if isinstance(gf, str) and gf.strip() else None)
        else:
            for gk, gv in raw_gloss.items():
                if isinstance(gk, str) and isinstance(gv, str):
                    _add_glossary(gk, gv)
    else:
        for g_item in _as_seq(raw_gloss):
            if isinstance(g_item, dict):
                gt = g_item.get("term") or g_item.get("acronym")
                gd = g_item.get("definition") or g_item.get("meaning")
                gf = g_item.get("formula")
                if isinstance(gt, str) and isinstance(gd, str):
                    _add_glossary(gt, gd, formula=gf if isinstance(gf, str) and gf.strip() else None)
            elif isinstance(g_item, str) and ":" in g_item:
                gt, gd = g_item.split(":", 1)
                _add_glossary(gt, gd)

    raw_qs = fm.get("questions") if fm.get("questions") is not None else fm.get("open_questions")
    for q_item in _as_seq(raw_qs):
        if isinstance(q_item, str):
            _add_question(q_item)
        elif isinstance(q_item, dict):
            q_txt = q_item.get("question") or q_item.get("prompt")
            if isinstance(q_txt, str):
                q_who = q_item.get("who_to_ask") or q_item.get("owner")
                q_cat = q_item.get("category")
                _add_question(
                    q_txt,
                    who=q_who if isinstance(q_who, str) else "",
                    category=q_cat if isinstance(q_cat, str) else "",
                )

    def _extract_glossary_note_clauses(text: str, speaker_id: str | None = None) -> list[str]:
        extracted_terms: list[str] = []
        for m_gn in _GLOSSARY_NOTE_CLAUSE_RE.finditer(text):
            clause_blob = m_gn.group(1).strip()
            for clause in re.split(r",\s+and\s+|;\s*", clause_blob):
                m_cp = _GLOSSARY_CLAUSE_PAIR_RE.match(clause)
                if m_cp:
                    t_c = m_cp.group(1).strip()
                    d_c = m_cp.group(2).strip()
                    if t_c and d_c:
                        _add_glossary(t_c, d_c, speaker_id=speaker_id)
                        extracted_terms.append(t_c)
        return extracted_terms

    # 2. Tables extraction (.docx and Markdown tables — supports pure & composite tables)
    for tbl in tables:
        if len(tbl) < 2:
            continue
        hdrs = [re.sub(r"[^a-z0-9]+", "_", c.lower()).strip("_") for c in tbl[0]]
        htoks = [set(h.split("_")) for h in hdrs]

        # Stakeholder / Attendee columns (check first so composite tables link glossary terms)
        n_idx = next(
            (
                i
                for i, (h, t) in enumerate(zip(hdrs, htoks))
                if bool(t & {"stakeholder", "person", "attendee", "owner"})
                or h in {"name", "full_name", "contact_name"}
            ),
            None,
        )
        t_idx = next(
            (
                i
                for i, t in enumerate(htoks)
                if i != n_idx and bool(t & {"term", "terms", "acronym", "metric", "kpi", "driver"})
            ),
            None,
        )
        d_idx = next(
            (
                i
                for i, t in enumerate(htoks)
                if i not in {n_idx, t_idx}
                and bool(t & {"definition", "definitions", "meaning", "description"})
            ),
            None,
        )
        is_glossary_tbl = t_idx is not None and d_idx is not None

        if n_idx is not None:
            r_idx = next(
                (i for i, t in enumerate(htoks) if i != n_idx and bool(t & {"role", "title", "position"})),
                None,
            )
            dept_idx = next(
                (i for i, t in enumerate(htoks) if bool(t & {"department", "team", "org"})),
                None,
            )
            rep_idx = next(
                (i for i, t in enumerate(htoks) if bool(t & {"report", "reports", "dashboard", "dashboards"})),
                None,
            )
            kpi_idx = next(
                (i for i, t in enumerate(htoks) if i != n_idx and bool(t & {"kpi", "kpis", "metric", "metrics"})),
                None,
            )
            pain_idx = next(
                (i for i, t in enumerate(htoks) if bool(t & {"pain", "challenge", "concern", "blocker"})),
                None,
            )
            ask_idx = next(
                (i for i, t in enumerate(htoks) if bool(t & {"ask", "asks", "request", "need"})),
                None,
            )
            for row in tbl[1:]:
                if len(row) <= n_idx:
                    continue
                sh = _ensure_sh(
                    row[n_idx],
                    row[r_idx] if r_idx is not None and len(row) > r_idx else "",
                    row[dept_idx] if dept_idx is not None and len(row) > dept_idx else "",
                )
                if sh is not None:
                    if rep_idx is not None and len(row) > rep_idx and row[rep_idx]:
                        for rr in _extract_report_refs_from_text(row[rep_idx], allow_bare=True):
                            _dedup_append(sh["owned_reports"], rr)
                    if kpi_idx is not None and len(row) > kpi_idx and row[kpi_idx]:
                        for k in re.split(r"[,;]", row[kpi_idx]):
                            _dedup_append(sh["kpis_cared_about"], k)
                    if pain_idx is not None and len(row) > pain_idx and row[pain_idx]:
                        _dedup_append(sh["pain_points"], row[pain_idx])
                    if ask_idx is not None and len(row) > ask_idx and row[ask_idx]:
                        _dedup_append(sh["explicit_asks"], row[ask_idx])

        if is_glossary_tbl and t_idx is not None and d_idx is not None:
            f_idx = next(
                (
                    i
                    for i, t in enumerate(htoks)
                    if i not in {n_idx, t_idx, d_idx}
                    and bool(t & {"formula", "logic", "value", "baseline", "benchmark"})
                ),
                None,
            )
            for row in tbl[1:]:
                if len(row) > max(t_idx, d_idx):
                    fm_val = (
                        row[f_idx].strip()
                        if f_idx is not None and len(row) > f_idx and row[f_idx].strip()
                        else None
                    )
                    row_sp_id: str | None = None
                    if n_idx is not None and len(row) > n_idx and row[n_idx].strip():
                        cand_sh = _ensure_sh(row[n_idx])
                        if cand_sh is not None:
                            row_sp_id = cand_sh["id"]
                    _add_glossary(row[t_idx], row[d_idx], formula=fm_val, speaker_id=row_sp_id)

    # 3. Speaker turns analysis (.vtt, .docx, .md, .txt)
    inline_def_re = re.compile(
        r"\b([A-Z][A-Z0-9/-]{1,9})\s*(?:\(([^)]{5,90})\)|(?:stands\s+for|means|is\s+defined\s+as)\s+([^.;\n]{5,140}?)(?=,\s+and\s+[A-Z]|[.;\n]|$))"
    )
    role_intro_re = re.compile(
        r"\b(?:I\s+am|I'm|as)\s+(?:the\s+)?((?:VP|Vice\s+President|SVP|Director|Senior\s+Director|Head|Manager|Lead|Chief\s+\w+\s+Officer|CFO|COO|CIO)(?:\s+of\s+[A-Za-z &]+)?)",
        re.IGNORECASE,
    )

    for name, role, dept, utt in turns:
        sh = _ensure_sh(name, role, dept, allow_prose_labels=True)
        if sh is None:
            continue
        sh["_utterances"].append(utt)
        if not sh["role"]:
            m_intro = role_intro_re.search(utt)
            if m_intro:
                sh["role"] = m_intro.group(1).strip()

        for kpi_m in _KNOWN_KPI_REGEX.finditer(utt):
            _dedup_append(sh["kpis_cared_about"], kpi_m.group(0))

        for gn_t in _extract_glossary_note_clauses(utt, speaker_id=sh["id"]):
            _dedup_append(sh["kpis_cared_about"], gn_t)

        for def_m in inline_def_re.finditer(utt):
            acro = def_m.group(1)
            meaning = (def_m.group(2) or def_m.group(3) or "").strip()
            if meaning and acro not in {"P0", "P1", "Q1", "Q2", "Q3", "Q4", "IT", "BI", "DS", "ML", "AI", "US", "UK"}:
                _add_glossary(acro, meaning, speaker_id=sh["id"])
                _dedup_append(sh["kpis_cared_about"], acro)

        for sentence in re.split(r"(?<=[.!?])\s+", utt):
            sent = sentence.strip()
            if not sent:
                continue
            is_ask_or_mandate = False
            if _MANDATE_SIGNAL_RE.search(sent):
                _dedup_append(sh["boss_mandate_asks"], sent)
                is_ask_or_mandate = True
            if _ASK_SIGNAL_RE.search(sent):
                _dedup_append(sh["explicit_asks"], sent)
                is_ask_or_mandate = True
            if _PAIN_SIGNAL_RE.search(sent):
                _dedup_append(sh["pain_points"], sent)
            if sent.endswith("?") and not is_ask_or_mandate:
                _add_question(sent, who=sh["name"], speaker_id=sh["id"])

        utt_low = utt.casefold()
        for rep_key, rep_title in report_lookup.items():
            if len(rep_key) >= 4 and rep_key in utt_low:
                _dedup_append(sh["owned_reports"], format_wikilink(rep_title))
        if _OWNERSHIP_CLAUSE_RE.search(utt) and not utt_low.lstrip().startswith("upstream"):
            for rr in _extract_report_refs_from_text(utt, allow_bare=False):
                _dedup_append(sh["owned_reports"], rr)

    # 4. Structured sections & bullets in lines (.md, .txt, .docx paragraphs)
    current_section = ""
    active_sh_in_doc: dict[str, Any] | None = (
        stakeholders_acc[doc_stakeholder_ids[0]] if len(doc_stakeholder_ids) == 1 else None
    )

    for raw_ln in lines:
        s = raw_ln.strip()
        if not s:
            continue
        if s.startswith("#"):
            hdr = s.lstrip("#").strip()
            hl = hdr.lower()
            has_dash_or_paren = bool(
                len(hdr) <= 420
                and (_SPEAKER_DASH_RE.search(hdr) or (hdr.endswith(")") and "(" in hdr))
            )
            n_h, r_h, d_h = _split_speaker_and_role(hdr)
            if (r_h or has_dash_or_paren) and _is_valid_person_name(n_h):
                active_sh_in_doc = _ensure_sh(n_h, r_h, d_h)
                current_section = ""
                continue
            if any(k in hl for k in ("owned report", "reports owned")):
                current_section = "owned_reports"
            elif any(k in hl for k in ("attendee", "participant", "stakeholder", "who")):
                current_section = "attendees"
            elif any(k in hl for k in ("glossary", "acronym", "definition", "terminology")):
                current_section = "glossary"
            elif any(k in hl for k in ("question", "follow-up", "follow up", "unknown", "verify")):
                current_section = "questions"
            elif "mandate" in hl:
                current_section = "mandates"
            elif any(k in hl for k in ("pain", "challenge", "blocker", "problem", "issue")):
                current_section = "pain_points"
            elif any(k in hl for k in ("ask", "request", "action item", "need")):
                current_section = "asks"
            elif any(k in hl for k in ("kpi", "metric", "measure")):
                current_section = "kpis"
            else:
                current_section = ""
            continue

        # Check prefixed glossary line anywhere (e.g. `- Glossary — **Term**: Def`)
        if len(s) <= 450:
            m_pg = _PREFIXED_GLOSSARY_LINE_RE.match(s)
            if m_pg:
                m_tp = _PREFIXED_TERM_PAIR_RE.match(m_pg.group(1).strip())
                if m_tp:
                    t_pg = (m_tp.group(1) or m_tp.group(2) or "").strip()
                    _add_glossary(t_pg, m_tp.group(3).strip())
                    continue

        # Check structured KV metadata lines (pipe-separated or bulleted/plain)
        kv_segments = (
            re.split(r"(?<!\[)[ \t]++\|[ \t]++(?!\])", s)
            if len(s) <= 1000 and "|" in s and ":" in s
            else ([s] if len(s) <= 500 else [])
        )
        kv_matches = [
            _KV_META_SEGMENT_RE.match(seg.strip())
            for seg in kv_segments
            if len(seg) <= 500
        ]
        if kv_matches and all(m is not None for m in kv_matches):
            for m_kv in kv_matches:
                assert m_kv is not None
                k_norm = re.sub(r"\s+", " ", m_kv.group(1).strip().lower())
                v_raw = m_kv.group(2).strip()
                if not v_raw:
                    continue
                if k_norm in {"stakeholder", "name"}:
                    n_kv, r_kv, d_kv = _split_speaker_and_role(v_raw)
                    sh_kv = _ensure_sh(n_kv, r_kv, d_kv)
                    if sh_kv is not None:
                        active_sh_in_doc = sh_kv
                    continue
                target_kv = active_sh_in_doc or (
                    stakeholders_acc[doc_stakeholder_ids[0]] if doc_stakeholder_ids else None
                )
                if target_kv is None:
                    continue
                if k_norm in {"role", "title", "position"}:
                    target_kv["role"] = v_raw.strip("*_` ")
                elif k_norm in {"department", "team"}:
                    target_kv["department"] = v_raw.strip("*_` ")
                elif k_norm in {"influence", "influence tier"}:
                    v_tier = v_raw.strip("*_` ").lower()
                    if v_tier in {"exec", "manager", "partner", "sme"}:
                        target_kv["influence_tier"] = v_tier
                        target_kv["_explicit_tier"] = True
                elif k_norm in {"decision appetite", "appetite"}:
                    v_app = v_raw.strip("*_` ").lower()
                    if v_app in {"high", "medium", "low", "unknown"}:
                        target_kv["decision_appetite"] = v_app
                elif k_norm in {"owned report", "owned reports", "reports owned"}:
                    for rr in _extract_report_refs_from_text(v_raw, allow_bare=True):
                        _dedup_append(target_kv["owned_reports"], rr)
                elif k_norm in {"pain point", "pain points"}:
                    _dedup_append(target_kv["pain_points"], v_raw)
                    for kpi_m in _KNOWN_KPI_REGEX.finditer(v_raw):
                        _dedup_append(target_kv["kpis_cared_about"], kpi_m.group(0))
                elif k_norm in {"explicit ask", "explicit asks", "ask", "asks"}:
                    if _MANDATE_SIGNAL_RE.search(v_raw):
                        _dedup_append(target_kv["boss_mandate_asks"], v_raw)
                    _dedup_append(target_kv["explicit_asks"], v_raw)
                    for kpi_m in _KNOWN_KPI_REGEX.finditer(v_raw):
                        _dedup_append(target_kv["kpis_cared_about"], kpi_m.group(0))
                elif k_norm in {"boss mandate", "boss mandates", "mandate", "mandates"}:
                    _dedup_append(target_kv["boss_mandate_asks"], v_raw)
            continue

        bullet_body = re.sub(r"^[-*•\d.)]+\s*", "", s).strip()
        is_bullet = bullet_body != s

        if current_section == "attendees" and is_bullet:
            m_att = _ATTENDEE_BULLET_RE.match(s) if len(s) <= 350 else None
            if m_att:
                n_a, r_a, d_a = _split_speaker_and_role(
                    f"{m_att.group(1).strip()} ({m_att.group(2).strip()})"
                )
                sh_new = _ensure_sh(n_a, r_a, d_a)
                if sh_new is not None and active_sh_in_doc is None:
                    active_sh_in_doc = sh_new
                continue
            n_b, r_b, d_b = _split_speaker_and_role(bullet_body)
            if _is_valid_person_name(n_b):
                sh_new = _ensure_sh(n_b, r_b, d_b)
                if sh_new is not None and active_sh_in_doc is None:
                    active_sh_in_doc = sh_new
                continue

        if current_section == "glossary":
            m_bt = _BULLET_TERM_RE.match(s) if len(s) <= 350 else None
            if m_bt:
                term_s = (m_bt.group(1) or m_bt.group(2) or "").strip()
                _add_glossary(term_s, m_bt.group(3).strip())
                continue

        if current_section == "questions" and bullet_body:
            _add_question(
                bullet_body,
                who=active_sh_in_doc["name"] if active_sh_in_doc else "Manager",
                speaker_id=active_sh_in_doc["id"] if active_sh_in_doc else None,
            )
            continue

        target_sh = active_sh_in_doc or (
            stakeholders_acc[doc_stakeholder_ids[0]] if doc_stakeholder_ids else None
        )

        if (
            target_sh is not None
            and bullet_body
            and not bullet_body.lower().startswith("upstream")
            and (current_section == "owned_reports" or _OWNERSHIP_CLAUSE_RE.search(bullet_body))
        ):
            extracted_owned = _extract_report_refs_from_text(
                bullet_body,
                allow_bare=(current_section == "owned_reports" and "[[" not in bullet_body and len(bullet_body) <= 80),
            )
            for rr in extracted_owned:
                _dedup_append(target_sh["owned_reports"], rr)

        if current_section == "pain_points" and bullet_body and target_sh is not None:
            _dedup_append(target_sh["pain_points"], bullet_body)
            continue
        if current_section == "asks" and bullet_body and target_sh is not None:
            _dedup_append(target_sh["explicit_asks"], bullet_body)
            continue
        if current_section == "mandates" and bullet_body and target_sh is not None:
            _dedup_append(target_sh["boss_mandate_asks"], bullet_body)
            continue
        if current_section == "kpis" and bullet_body and target_sh is not None:
            _dedup_append(target_sh["kpis_cared_about"], bullet_body)
            continue

        # General inline detections outside dedicated sections
        _extract_glossary_note_clauses(s, speaker_id=target_sh["id"] if target_sh else None)
        for def_m in inline_def_re.finditer(s):
            acro = def_m.group(1)
            meaning = (def_m.group(2) or def_m.group(3) or "").strip()
            if meaning and acro not in {"P0", "P1", "Q1", "Q2", "Q3", "Q4", "IT", "BI", "DS", "ML", "AI"}:
                _add_glossary(
                    acro,
                    meaning,
                    speaker_id=target_sh["id"] if target_sh else None,
                )

        if re.match(r"^(?:open\s+question|question|todo|\[verify\])", bullet_body, re.IGNORECASE) or (
            bullet_body.endswith("?") and len(bullet_body) >= 15
        ):
            _add_question(
                bullet_body,
                who=target_sh["name"] if target_sh else "Manager",
                speaker_id=target_sh["id"] if target_sh else None,
            )

        if target_sh is not None:
            for kpi_m in _KNOWN_KPI_REGEX.finditer(s):
                _dedup_append(target_sh["kpis_cared_about"], kpi_m.group(0))
            if _MANDATE_SIGNAL_RE.search(bullet_body):
                _dedup_append(target_sh["boss_mandate_asks"], bullet_body)
            if _ASK_SIGNAL_RE.search(bullet_body):
                _dedup_append(target_sh["explicit_asks"], bullet_body)
            if _PAIN_SIGNAL_RE.search(bullet_body):
                _dedup_append(target_sh["pain_points"], bullet_body)
            s_low = bullet_body.casefold()
            for rep_key, rep_title in report_lookup.items():
                if len(rep_key) >= 4 and rep_key in s_low:
                    _dedup_append(target_sh["owned_reports"], format_wikilink(rep_title))

    # Finalize inferred tier/department/appetite for discovered stakeholders
    for sh_id in doc_stakeholder_ids:
        sh = stakeholders_acc[sh_id]
        utts = sh.get("_utterances") or []
        blob = " ".join(utts)
        if not sh.get("_explicit_tier") and sh["influence_tier"] == "partner" and sh["role"]:
            sh["influence_tier"] = _infer_influence_tier(sh["role"], blob)
        if sh["decision_appetite"] == "unknown":
            sh["decision_appetite"] = _infer_decision_appetite(
                [*utts, *sh["explicit_asks"], *sh["boss_mandate_asks"], *sh["pain_points"]]
            )
        if not sh["department"]:
            sh["department"] = _infer_department(sh["role"], blob)

    # Backfill primary document stakeholder ref onto glossary terms & questions from this file
    if doc_stakeholder_ids:
        primary_sh_id = doc_stakeholder_ids[0]
        for g_key in doc_glossary_keys:
            g_entry = glossary_acc.get(g_key)
            if g_entry is not None and not g_entry.get("source_refs"):
                g_entry["source_refs"] = [primary_sh_id]
        for q_entry in questions_acc[q_start_idx:]:
            if not q_entry.get("linked_refs"):
                q_entry["linked_refs"] = [primary_sh_id]


# ---------------------------------------------------------------------------
# Inbox File Discovery, Vault Upsert & Archive
# ---------------------------------------------------------------------------


def _discover_inbox_files(vault_dir: Path, target_path: Path) -> list[Path]:
    if target_path.is_file():
        if (
            target_path.suffix.lower() in _SUPPORTED_SUFFIXES
            and not target_path.name.startswith((".", "~$"))
        ):
            return [target_path]
        return []

    try:
        is_vault_root = target_path.resolve() == vault_dir.resolve()
    except OSError:
        is_vault_root = False

    found: list[Path] = []
    for root, dirs, files in os.walk(target_path):
        pruned: list[str] = []
        for d in sorted(dirs):
            dl = d.lower()
            if d.startswith(".") or dl in _PRUNED_INBOX_DIRS:
                continue
            if dl.endswith((".pbip", ".semanticmodel", ".dataset", ".report")):
                continue
            if is_vault_root and Path(root).resolve() == vault_dir.resolve() and dl in _VAULT_ENTITY_DIRS:
                continue
            pruned.append(d)
        dirs[:] = pruned

        rp = Path(root)
        for f in sorted(files):
            if f.startswith((".", "~$")):
                continue
            if is_vault_root and rp.resolve() == vault_dir.resolve() and f.lower() == "glossary.md":
                continue
            fp = rp / f
            if fp.suffix.lower() in _SUPPORTED_SUFFIXES:
                found.append(fp)
    return found


def _archive_inbox_file(
    vault_dir: Path, src_file: Path, redact_secrets: bool = True
) -> Path:
    processed_dir = vault_dir / "inbox" / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    red_stem = redact_text(src_file.stem, redact_secrets=redact_secrets)
    base_stem = (
        _clean_redacted_label(red_stem, "inbox-note")
        if "[REDACTED:" in red_stem or "@" in src_file.stem
        else src_file.stem
    )
    if _is_windows_reserved_stem(base_stem):
        base_stem = f"_{base_stem}"
    safe_name = f"{base_stem}{src_file.suffix}"
    base_p = Path(safe_name)
    dest = processed_dir / safe_name
    counter = 1
    while dest.exists():
        dest = processed_dir / f"{base_p.stem}_{counter}{base_p.suffix}"
        counter += 1

    try:
        inside_vault = src_file.resolve().is_relative_to(vault_dir.resolve())
    except (OSError, ValueError):
        inside_vault = False

    if src_file.suffix.lower() in {".md", ".txt", ".vtt"}:
        scrubbed = redact_text(_decode_file_text(src_file), redact_secrets=redact_secrets)
        dest.write_text(scrubbed, encoding="utf-8", newline="\n")
        if inside_vault:
            src_file.unlink(missing_ok=True)
    elif inside_vault:
        shutil.move(str(src_file), str(dest))
    else:
        shutil.copy2(str(src_file), str(dest))
    return dest


def sync_teams(
    vault_dir: Path,
    config: PoopdeckConfig | None = None,
    input_path: Path | None = None,
) -> SyncSummary:
    """Ingest Teams `.vtt`, `.docx`, and `.md`/`.txt` context notes into vault cards."""
    vdir = Path(vault_dir)
    cfg = load_config() if config is None else config
    redact_secrets = cfg.privacy.redact_secrets

    in_path = (
        Path(input_path)
        if input_path is not None and str(input_path).strip()
        else None
    )
    target_path = (
        in_path
        if in_path is not None
        else _rebase_vault_relative_path(vdir, cfg, cfg.teams.inbox_path, ("inbox",))
    )

    warnings: list[str] = []
    errors: list[str] = []
    processed_files: list[str] = []
    written_paths: list[str] = []

    def _scrub_msgs(items: list[str]) -> list[str]:
        return [redact_text(str(x), redact_secrets=redact_secrets) for x in items]

    if in_path is not None and not in_path.exists():
        errors.append(
            redact_text(
                f"Teams dropzone input path does not exist: {in_path}",
                redact_secrets=redact_secrets,
            )
        )
        return SyncSummary(
            target="teams",
            mode_used="dropzone",
            cards_written=0,
            warnings=_scrub_msgs(warnings),
            errors=_scrub_msgs(errors),
        )

    if not target_path.exists():
        warnings.append(
            redact_text(
                f"Teams inbox directory does not exist yet: {target_path}",
                redact_secrets=redact_secrets,
            )
        )
        return SyncSummary(
            target="teams",
            mode_used="dropzone",
            cards_written=0,
            warnings=_scrub_msgs(warnings),
            errors=_scrub_msgs(errors),
        )

    candidates = _discover_inbox_files(vdir, target_path)
    if not candidates:
        warnings.append(
            redact_text(
                f"No Teams/context dropzone files found in {target_path}",
                redact_secrets=redact_secrets,
            )
        )
        return SyncSummary(
            target="teams",
            mode_used="dropzone",
            cards_written=0,
            warnings=_scrub_msgs(warnings),
            errors=_scrub_msgs(errors),
        )

    known_keys, report_lookup = _build_vault_ref_catalog(vdir)

    stakeholders_acc: dict[str, dict[str, Any]] = {}
    glossary_acc: dict[str, dict[str, Any]] = {}
    questions_acc: list[dict[str, Any]] = []
    cleanly_parsed_files: list[Path] = []
    explicit_single_file = in_path is not None and in_path.is_file()

    for fp in candidates:
        suf = fp.suffix.lower()
        try:
            if suf == ".vtt":
                doc = _parse_vtt_content(_decode_file_text(fp), redact_secrets)
            elif suf == ".docx":
                doc = _parse_docx_file(fp, redact_secrets)
            else:
                doc = _parse_markdown_or_text(_decode_file_text(fp), redact_secrets)
            safe_source = _safe_source_filename(fp, redact_secrets)
            _extract_entities_from_doc(
                doc=doc,
                source_filename=safe_source,
                report_lookup=report_lookup,
                stakeholders_acc=stakeholders_acc,
                glossary_acc=glossary_acc,
                questions_acc=questions_acc,
            )
        except Exception as exc:
            msg = redact_text(
                f"Failed parsing inbox file {fp.name}: {exc}",
                redact_secrets=redact_secrets,
            )
            if explicit_single_file:
                errors.append(msg)
            else:
                warnings.append(msg)
            continue

        cleanly_parsed_files.append(fp)
        processed_files.append(fp.as_posix())

    if not cleanly_parsed_files:
        return SyncSummary(
            target="teams",
            mode_used="dropzone",
            cards_written=0,
            written_paths=[],
            processed_files=[],
            warnings=_scrub_msgs(warnings),
            errors=_scrub_msgs(errors),
        )

    # Pre-register newly discovered batch stakeholders into known_keys for cross-card prose refs
    for b_sh_id, b_raw_sh in stakeholders_acc.items():
        for b_val in (b_sh_id, b_raw_sh.get("name")):
            if isinstance(b_val, str) and b_val.strip():
                known_keys.add(b_val.strip().casefold())
                sk_b = _slugify_key(b_val.strip())
                if sk_b:
                    known_keys.add(sk_b)

    # 1. Upsert Stakeholder Cards (`vault/stakeholders/<slug>.md`)
    sh_dir = vdir / "stakeholders"
    sh_dir.mkdir(parents=True, exist_ok=True)
    existing_sh_by_key: dict[str, tuple[Path, StakeholderCard]] = {}
    for md_p in sorted(sh_dir.glob("*.md")):
        try:
            sc = StakeholderCard.model_validate(parse_markdown_card(md_p).frontmatter)
            existing_sh_by_key[sc.id.casefold()] = (md_p, sc)
            existing_sh_by_key[sc.name.casefold()] = (md_p, sc)
            existing_sh_by_key[md_p.stem.casefold()] = (md_p, sc)
        except Exception:
            continue

    for sh_id in sorted(stakeholders_acc):
        raw_sh = stakeholders_acc[sh_id]
        matched = existing_sh_by_key.get(sh_id.casefold()) or existing_sh_by_key.get(
            raw_sh["name"].casefold()
        )
        if matched is not None:
            sh_path, prev = matched
            if _is_windows_reserved_stem(sh_path.stem):
                sh_path = sh_dir / f"{_safe_slug(prev.name)}.md"
            merged_role = raw_sh["role"] or prev.role
            merged_dept = raw_sh["department"] or prev.department
            merged_tier = (
                raw_sh["influence_tier"]
                if raw_sh.get("_explicit_tier") or raw_sh["influence_tier"] != "partner"
                else prev.influence_tier
            )
            merged_app = (
                raw_sh["decision_appetite"]
                if raw_sh["decision_appetite"] != "unknown"
                else prev.decision_appetite
            )
            owned_r = list(prev.owned_reports)
            for r in raw_sh["owned_reports"]:
                _dedup_append(owned_r, r)
            kpis = list(prev.kpis_cared_about)
            for k in raw_sh["kpis_cared_about"]:
                _dedup_append(kpis, k)
            pains = list(prev.pain_points)
            for p in raw_sh["pain_points"]:
                _dedup_append(pains, p)
            asks = list(prev.explicit_asks)
            for a in raw_sh["explicit_asks"]:
                _dedup_append(asks, a)
            mandates = list(prev.boss_mandate_asks)
            for b in raw_sh["boss_mandate_asks"]:
                _dedup_append(mandates, b)
            final_id = prev.id
        else:
            sh_path = sh_dir / f"{sh_id}.md"
            merged_role = raw_sh["role"]
            merged_dept = raw_sh["department"]
            merged_tier = raw_sh["influence_tier"]
            merged_app = raw_sh["decision_appetite"]
            owned_r = list(raw_sh["owned_reports"])
            kpis = list(raw_sh["kpis_cared_about"])
            pains = list(raw_sh["pain_points"])
            asks = list(raw_sh["explicit_asks"])
            mandates = list(raw_sh["boss_mandate_asks"])
            final_id = sh_id

        valid_owned_reports = _clean_owned_report_wikilinks(owned_r)
        card = StakeholderCard(
            id=final_id,
            name=_sanitize_prose_wikilinks(raw_sh["name"], known_keys),
            role=_sanitize_prose_wikilinks(merged_role, known_keys),
            department=_sanitize_prose_wikilinks(merged_dept, known_keys),
            influence_tier=merged_tier,  # type: ignore[arg-type]
            decision_appetite=merged_app,  # type: ignore[arg-type]
            owned_reports=valid_owned_reports,
            kpis_cared_about=[_sanitize_prose_wikilinks(x, known_keys) for x in kpis],
            pain_points=[_sanitize_prose_wikilinks(x, known_keys) for x in pains],
            explicit_asks=[_sanitize_prose_wikilinks(x, known_keys) for x in asks],
            boss_mandate_asks=[_sanitize_prose_wikilinks(x, known_keys) for x in mandates],
            updated_at=date.today(),
        )
        upsert_markdown_card(sh_path, card, render_stakeholder_body(card))
        written_paths.append(sh_path.as_posix())
        # Register new/updated stakeholder in known_keys for glossary & questions
        known_keys.add(card.id.casefold())
        known_keys.add(_slugify_key(card.id))
        known_keys.add(card.name.casefold())
        known_keys.add(_slugify_key(card.name))
        known_keys.add(sh_path.stem.casefold())

    # 2. Seed / Merge Glossary (`vault/glossary.md`)
    glossary_path = vdir / "glossary.md"
    glossary_existed = glossary_path.is_file()
    if glossary_existed:
        try:
            g_card = GlossaryCard.model_validate(parse_markdown_card(glossary_path).frontmatter)
        except Exception:
            g_card = build_default_glossary()
    else:
        g_card = build_default_glossary()

    glossary_changed = not glossary_existed
    terms_by_key: dict[str, GlossaryTerm] = {t.term.casefold(): t for t in g_card.terms}
    for g_key, g_raw in glossary_acc.items():
        valid_srefs = _filter_valid_wikilinks(g_raw.get("source_refs") or [], known_keys)
        san_def = _sanitize_prose_wikilinks(g_raw["definition"], known_keys)
        san_src = _sanitize_prose_wikilinks(g_raw.get("source") or "", known_keys)
        raw_fl = g_raw.get("formula_or_logic")
        san_fl = _sanitize_prose_wikilinks(raw_fl, known_keys) if isinstance(raw_fl, str) else None
        if g_key in terms_by_key:
            existing_t = terms_by_key[g_key]
            if san_def and not existing_t.definition:
                existing_t.definition = san_def
                glossary_changed = True
            if san_fl and not existing_t.formula_or_logic:
                existing_t.formula_or_logic = san_fl
                glossary_changed = True
            if san_src and not existing_t.source:
                existing_t.source = san_src
                glossary_changed = True
            merged_sr = list(existing_t.source_refs)
            for vr in valid_srefs:
                if vr not in merged_sr:
                    merged_sr.append(vr)
                    glossary_changed = True
            existing_t.source_refs = merged_sr
        else:
            new_term = GlossaryTerm(
                term=_sanitize_prose_wikilinks(g_raw["term"], known_keys),
                definition=san_def,
                source=san_src,
                formula_or_logic=san_fl,
                source_refs=valid_srefs,
            )
            g_card.terms.append(new_term)
            terms_by_key[g_key] = new_term
            glossary_changed = True

    if glossary_changed:
        g_card.updated_at = date.today()
        upsert_markdown_card(glossary_path, g_card, render_glossary_body(g_card))
        written_paths.append(glossary_path.as_posix())
        known_keys.add("glossary")
        for t in g_card.terms:
            known_keys.add(t.term.casefold())
            known_keys.add(_slugify_key(t.term))

    # 3. Seed / Merge Open Questions (`vault/questions/open-questions.md`)
    q_path = vdir / "questions" / "open-questions.md"
    q_existed = q_path.is_file()
    if q_existed:
        try:
            q_bank = OpenQuestionBank.model_validate(parse_markdown_card(q_path).frontmatter)
        except Exception:
            q_bank = build_default_question_bank()
    else:
        q_bank = build_default_question_bank()

    q_changed = not q_existed
    existing_q_norms: set[str] = {
        re.sub(r"[^a-z0-9]+", " ", q.question.lower()).strip() for q in q_bank.questions
    }
    existing_q_ids: set[str] = {q.id.upper() for q in q_bank.questions}
    next_inb_idx = 1

    for q_raw in questions_acc:
        san_q = _sanitize_prose_wikilinks(q_raw["question"], known_keys)
        norm_q = re.sub(r"[^a-z0-9]+", " ", san_q.lower()).strip()
        if not norm_q or norm_q in existing_q_norms:
            continue
        existing_q_norms.add(norm_q)
        while f"Q-INB-{next_inb_idx:02d}" in existing_q_ids:
            next_inb_idx += 1
        qid = f"Q-INB-{next_inb_idx:02d}"
        existing_q_ids.add(qid)
        next_inb_idx += 1

        valid_lrefs = _filter_valid_wikilinks(q_raw.get("linked_refs") or [], known_keys)
        q_bank.questions.append(
            QuestionItem(
                id=qid,
                category=q_raw.get("category") or "manager",  # type: ignore[arg-type]
                who_to_ask=_sanitize_prose_wikilinks(q_raw.get("who_to_ask") or "Manager", known_keys),
                topic=_sanitize_prose_wikilinks(q_raw.get("topic") or "Inbox Discovery", known_keys),
                question=san_q,
                status="open",
                answer_summary=None,
                linked_refs=valid_lrefs,
                phase="days_6_15",
            )
        )
        q_changed = True

    if q_changed:
        q_bank.updated_at = date.today()
        upsert_markdown_card(q_path, q_bank, render_questions_body(q_bank))
        written_paths.append(q_path.as_posix())

    # 4. Archive processed files if enabled
    if cfg.teams.archive_processed:
        for clean_fp in cleanly_parsed_files:
            try:
                _archive_inbox_file(vdir, clean_fp, redact_secrets=redact_secrets)
            except Exception as exc:
                warnings.append(
                    redact_text(
                        f"Could not archive {clean_fp.name}: {exc}",
                        redact_secrets=redact_secrets,
                    )
                )

    return SyncSummary(
        target="teams",
        mode_used="dropzone",
        cards_written=len(written_paths),
        written_paths=written_paths,
        processed_files=_scrub_msgs(processed_files),
        warnings=_scrub_msgs(warnings),
        errors=_scrub_msgs(errors),
    )
