"""Power BI dual-mode crawler (Tier 1 REST & Tier 2 .pbip/BIM/TMDL/DAX INFO.* + heuristic detector)."""

from __future__ import annotations

import csv
import fnmatch
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

import httpx

from poopdeck.config import PoopdeckConfig, PowerBIConfig, load_config
from poopdeck.crawlers import SyncSummary, redact_crawler_text, unwrap_prose_wikilinks
from poopdeck.models import DaxMeasure, ReportCard
from poopdeck.vault_io import (
    format_wikilink,
    normalize_wikilink_target,
    parse_markdown_card,
    render_report_body,
    upsert_markdown_card,
)

_PBI_MODE_ALIASES: dict[str, str] = {
    "auto": "auto",
    "tier1": "tier1",
    "tier1_rest": "tier1",
    "tier2": "tier2",
    "tier2_local": "tier2",
    "tier2_bundle": "tier2",
}

_DBX_ONLY_CSV_NAMES: frozenset[str] = frozenset(
    {
        "tables.csv",
        "columns.csv",
        "lineage.csv",
        "profiles.csv",
        "tags.csv",
        "table_tags.csv",
        "workflows.csv",
        "jobs.csv",
        "query_stats.csv",
        "usage.csv",
    }
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

# ---------------------------------------------------------------------------
# DAX Comment Stripping & Heuristic Detector
# ---------------------------------------------------------------------------

_DAX_STRING_OR_COMMENT_RE = re.compile(
    r'"(?:""|[^"])*"|\'(?:\'\'|[^\'])*\'|/\*(?:[^*]|\*(?!/))*(?:\*/|\Z)|//[^\r\n]*|--[^\r\n]*'
)

_SWITCH_TRUE_RE = re.compile(r"\bSWITCH\s*\(\s*TRUE\s*\(\s*\)", re.IGNORECASE)
_IF_CALL_RE = re.compile(r"\bIF\s*\(", re.IGNORECASE)
_SWITCH_CALL_RE = re.compile(r"\bSWITCH\s*\(", re.IGNORECASE)
_BRACKET_REF_RE = re.compile(r"\[([^\[\]\r\n]+)\]")
_DAX_STRING_LIT_RE = re.compile(r'"((?:""|[^"])*)"')
_TIER_KW_RE = re.compile(
    r"\b(?:critical|high|medium|med|low|red|yellow|amber|green|bronze|silver|gold|tier\s*\d+|grade\s*[a-f]|at[\s_-]*risk|healthy|warning)\b",
    re.IGNORECASE,
)
_WEIGHTED_SCORE_EXPR_RE = re.compile(
    r"(?:\b0?\.\d+\s*\*[^+\r\n]{1,120}\+\s*0?\.\d+\s*\*|\*[\s]*0?\.\d+[^+\r\n]{1,120}\+[^+\r\n]{1,120}\*[\s]*0?\.\d+)"
)
_SCORE_NAME_RE = re.compile(
    r"\b(?:score|scorecard|health|risk|tier|index|rating|priority|rank)\b",
    re.IGNORECASE,
)

_AVERAGEX_RE = re.compile(r"\bAVERAGEX\s*\(", re.IGNORECASE)
_ROLLING_WINDOW_FN_RE = re.compile(
    r"\b(?:DATESINPERIOD|DATESBETWEEN|WINDOW)\s*\(", re.IGNORECASE
)
_FORECAST_NAME_RE = re.compile(
    r"(?:forecast|project(?:ed|ion)|predict(?:ed|ion)?|run[\s_-]*rate|moving[\s_-]*avg|rolling[\s_-]*avg|outlook|extrapolat)",
    re.IGNORECASE,
)
_TRAILING_LOOKBACK_OR_GROWTH_RE = re.compile(
    r"(?:\b(?:AVERAGE|AVERAGEX|DATESINPERIOD|DATESBETWEEN|DATEADD|SAMEPERIODLASTYEAR|PARALLELPERIOD|PREVIOUS(?:DAY|MONTH|QUARTER|YEAR))\s*\(|\*\s*1\.\d+)",
    re.IGNORECASE,
)

_CONDITIONAL_FN_RE = re.compile(r"\b(?:IF|SWITCH|FILTER)\s*\(", re.IGNORECASE)
_NONZERO_NUMERIC_CMP_RE = re.compile(
    r"(?:<=|>=|<>|<|>|=)\s*-?(?:0\.\d*[1-9]\d*|[1-9]\d*(?:\.\d+)?)\b"
)
_ALERT_KW_RE = re.compile(
    r"\b(?:reorder|at[\s_-]*risk|sla[\s_-]*breach|breach|critical|alert|overdue|stockout|expedite|escalat\w*|action[\s_-]*required|out[\s_-]*of[\s_-]*stock)\b",
    re.IGNORECASE,
)

_FORWARD_ML_KW_RE = re.compile(
    r"(?:predict|forecast_ml|prob_|probability|propensity|p10|p50|p90|quantile|shap_|ml_score)",
    re.IGNORECASE,
)
_VARIANCE_SUB_RE = re.compile(
    r"\b(?:CALCULATE|DIVIDE|SUM|SUMX|AVERAGE|COUNT|COUNTROWS|DISTINCTCOUNT)\b|\[[^\[\]\r\n]+\]",
    re.IGNORECASE,
)
_TIME_INTEL_LOOKBACK_RE = re.compile(
    r"\b(?:SAMEPERIODLASTYEAR|DATEADD|PREVIOUS(?:DAY|MONTH|QUARTER|YEAR)|PARALLELPERIOD|TOTAL(?:YTD|QTD|MTD)|DATES(?:YTD|QTD|MTD)|OPENINGBALANCE(?:YEAR|QUARTER|MONTH)|CLOSINGBALANCE(?:YEAR|QUARTER|MONTH)|LASTDATE)\s*\(",
    re.IGNORECASE,
)
_BACKWARD_NAME_RE = re.compile(
    r"\b(?:yoy|mom|qoq|wow|ytd|qtd|mtd|ly|py|prior[\s_-]*(?:year|month|quarter|period)|last[\s_-]*(?:year|month|quarter|week)|variance|historical|actuals?[\s_-]*vs)\b",
    re.IGNORECASE,
)

_HEURISTIC_GAP_DESCRIPTIONS: dict[str, str] = {
    "nested_heuristic_tiers": "replace hand-tuned nested IF/SWITCH rules with calibrated ML risk/propensity scoring in Databricks",
    "naive_moving_avg_forecast": "replace trailing DAX moving-average/run-rate heuristic with governed time-series forecasting in Databricks",
    "static_threshold_alert": "replace hardcoded static DAX alert cutoffs with dynamic uncertainty-aware anomaly/reorder thresholds",
    "backward_looking_only": "supplement retrospective time-intelligence reporting with forward-looking driver & scenario predictions",
}


def _strip_dax_comments(expr: str) -> str:
    def _repl(match: re.Match[str]) -> str:
        tok = match.group(0)
        if tok.startswith(('"', "'")):
            return tok
        return " "

    return _DAX_STRING_OR_COMMENT_RE.sub(_repl, expr)


def detect_dax_heuristics(measure_name: str, expression: str) -> list[str]:
    """Deterministically detect BI-to-DS replacement heuristics in a DAX measure."""
    if not expression or not isinstance(expression, str):
        return []
    clean = _strip_dax_comments(expression).strip()
    if not clean:
        return []

    m_name = str(measure_name or "").strip()
    flags: list[str] = []

    bracket_refs = _BRACKET_REF_RE.findall(clean)
    str_lits = [s.replace('""', '"') for s in _DAX_STRING_LIT_RE.findall(clean)]
    has_forward_ml_col = any(_FORWARD_ML_KW_RE.search(r) for r in bracket_refs)
    tier_lit_count = sum(1 for s in str_lits if _TIER_KW_RE.search(s))
    has_alert_lit = any(_ALERT_KW_RE.search(s) for s in str_lits)

    # 1. nested_heuristic_tiers
    if_count = len(_IF_CALL_RE.findall(clean))
    has_weighted_score = bool(
        "+" in clean
        and _WEIGHTED_SCORE_EXPR_RE.search(clean)
        and (_SCORE_NAME_RE.search(m_name) or _SCORE_NAME_RE.search(clean))
    )
    if (
        _SWITCH_TRUE_RE.search(clean)
        or if_count >= 2
        or (_SWITCH_CALL_RE.search(clean) and tier_lit_count >= 2)
        or has_weighted_score
    ):
        flags.append("nested_heuristic_tiers")

    # 2. naive_moving_avg_forecast
    is_rolling_avgx = bool(
        _AVERAGEX_RE.search(clean) and _ROLLING_WINDOW_FN_RE.search(clean)
    )
    is_forecast_heuristic = bool(
        _FORECAST_NAME_RE.search(m_name)
        and _TRAILING_LOOKBACK_OR_GROWTH_RE.search(clean)
        and not has_forward_ml_col
    )
    if is_rolling_avgx or is_forecast_heuristic:
        flags.append("naive_moving_avg_forecast")

    # 3. static_threshold_alert
    if _CONDITIONAL_FN_RE.search(clean) and (
        _NONZERO_NUMERIC_CMP_RE.search(clean) or has_alert_lit
    ):
        flags.append("static_threshold_alert")

    # 4. backward_looking_only
    if "naive_moving_avg_forecast" not in flags and not has_forward_ml_col:
        if _TIME_INTEL_LOOKBACK_RE.search(clean) or (
            _BACKWARD_NAME_RE.search(m_name) and _VARIANCE_SUB_RE.search(clean)
        ):
            flags.append("backward_looking_only")

    return flags


# ---------------------------------------------------------------------------
# Path, Encoding & Lineage Helpers
# ---------------------------------------------------------------------------


def _decode_file_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


_REDACTED_PLACEHOLDER_RE = re.compile(r"\[REDACTED:[A-Z_]+\]", re.IGNORECASE)


def _redact_pbi_text(text: str, redact_secrets: bool = True) -> str:
    if not text or not isinstance(text, str):
        return text if isinstance(text, str) else ""
    out = redact_crawler_text(text, redact_secrets=redact_secrets)
    return (
        out.replace("<!-- HUMAN-NOTES-", "<!-- HUMAN_NOTES-")
        if "<!-- HUMAN-NOTES-" in out
        else out
    )


redact_text = _redact_pbi_text


def _clean_redacted_identifier(text: str, fallback: str) -> str:
    if "[REDACTED:" not in text:
        return text.strip() or fallback
    cleaned = re.sub(
        r"\(\s*\)|\[\s*\]|\{\s*\}",
        " ",
        _REDACTED_PLACEHOLDER_RE.sub(" ", text),
    )
    collapsed = " ".join(cleaned.split()).strip(" \t\r\n\x0b\x0c_.-:;|/\\,")
    s = re.sub(r"([_-])[\s_-]+", r"\1", collapsed)
    return s or fallback


def _is_windows_reserved_name(name: str) -> bool:
    prefix = (name or "").split(".", 1)[0].strip(" ").lower()
    if prefix in _WINDOWS_RESERVED_STEMS:
        return True
    is_res = getattr(os.path, "isreserved", lambda _: False)
    return bool(is_res(name) or is_res(f"{name}.md"))


def _safe_slug(text: str, fallback: str = "powerbi-report") -> str:
    cleaned = _REDACTED_PLACEHOLDER_RE.sub(" ", text or "").strip()
    slug = (
        "powerbi-report"
        if cleaned.lower() == "power bi report"
        else (re.sub(r"[^a-z0-9]+", "-", cleaned.lower()).strip("-") or fallback)
    )
    if len(slug) > 120:
        slug = f"{slug[:108].rstrip('-')}-{hashlib.sha1(cleaned.lower().encode('utf-8')).hexdigest()[:10]}"
    return f"{slug}-card" if _is_windows_reserved_name(f"{slug}.md") else slug


def _normalize_mode(mode: str) -> str:
    norm = (mode or "").strip().lower()
    if norm not in _PBI_MODE_ALIASES:
        raise ValueError(
            f"Invalid Power BI sync mode {mode!r}; expected 'auto', 'tier1', or 'tier2'"
        )
    return _PBI_MODE_ALIASES[norm]


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


def _join_bim_lines(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, list):
        strs = [str(x) for x in val if x is not None]
        if any(s.endswith("\n") for s in strs):
            return "".join(strs)
        return "\n".join(strs)
    return str(val)


_SQL_IDENT_PART = (
    r'(?:`([^`\r\n]{1,256})`|\[([^\[\]\r\n]{1,256})\]|"([^"\r\n]{1,256})"|([A-Za-z_][A-Za-z0-9_]{0,255}))'
)
_SQL_3PART_RE = re.compile(
    rf"\b(?:FROM|JOIN)\s+{_SQL_IDENT_PART}\s*\.\s*{_SQL_IDENT_PART}\s*\.\s*{_SQL_IDENT_PART}",
    re.IGNORECASE,
)
_SQL_2PART_RE = re.compile(
    rf"\b(?:FROM|JOIN)\s+{_SQL_IDENT_PART}\s*\.\s*{_SQL_IDENT_PART}(?![A-Za-z0-9_`\x5b\"]|\s*\.)",
    re.IGNORECASE,
)
_M_NAV_STEP_RE = re.compile(
    r'\{\s*\[\s*Name\s*=\s*"([^"\r\n]{1,256})"\s*,\s*Kind\s*=\s*"(Database|Catalog|Schema|Table|View)"\s*\]\s*\}',
    re.IGNORECASE,
)
_M_CATALOG_OPT_RE = re.compile(
    r'\b(?:Catalog|Database)\s*=\s*"([^"\r\n]{1,256})"', re.IGNORECASE
)


_M_TOKEN_SUB_RE = re.compile(
    r'#"(?:""|[^"])*"\s*=|#"((?:""|[^"])*)"|"(?:""|[^"])*"|\b([A-Za-z_][A-Za-z0-9_]*)\b(\s*=)?'
)

_BARE_3PART_FQN_RE = re.compile(
    r"(?<![A-Za-z0-9_@:/.\-])([A-Za-z_][A-Za-z0-9_]{0,127})\.([A-Za-z_][A-Za-z0-9_]{0,127})\.([A-Za-z_][A-Za-z0-9_]{0,127})(?![A-Za-z0-9_@:/\-])(?!\.[A-Za-z0-9_])(?!\s*\()"
)
_NON_FQN_PARTS: frozenset[str] = frozenset(
    {
        "com",
        "net",
        "org",
        "io",
        "gov",
        "edu",
        "local",
        "internal",
        "azuredatabricks",
        "databricks",
        "windows",
        "microsoft",
        "powerbi",
        "fabric",
        "amazonaws",
        "sys",
        "dbo",
        "information_schema",
        "json",
        "csv",
        "tmdl",
        "bim",
        "pbip",
        "pbix",
    }
)
_CAMEL_ACRONYM_SPLIT_RE = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_WORD_SPLIT_RE = re.compile(r"([a-z0-9])([A-Z])")


def _safe_nonneg_int(val: Any, default: int = 0) -> int:
    while isinstance(val, Enum):
        val = val.value
    if val is None or isinstance(val, bool):
        return default
    if isinstance(val, int):
        return max(0, val)
    if isinstance(val, Decimal):
        if not val.is_finite():
            return default
        _, digits, exp = val.as_tuple()
        if isinstance(exp, int) and exp >= 0 and len(digits) + exp <= 38:
            return max(0, int(val))
    if isinstance(val, (float, Decimal)):
        try:
            f = float(val)
        except OverflowError:
            return default
        return max(0, int(round(f))) if math.isfinite(f) else default
    if isinstance(val, (bytes, bytearray)):
        val = val.decode("utf-8", errors="replace")
    if isinstance(val, str):
        s = val.strip().replace(",", "")
        if not s or s.lower() in {"null", "none", "nan", "inf", "-inf", "n/a", "-"}:
            return default
        try:
            f = float(s)
        except (ValueError, OverflowError):
            return default
        return max(0, int(round(f))) if math.isfinite(f) else default
    return default


def _unquote_m_param_expr(ev: str) -> str | None:
    if not isinstance(ev, str):
        return None
    m = re.match(r'^\s*"((?:""|[^"])*)"', ev)
    if m:
        return m.group(1).replace('""', '"')
    return None


def _extract_upstream_fqns_from_expression(
    expr_text: str,
    param_map: dict[str, str] | None = None,
) -> list[str]:
    if not expr_text or not isinstance(expr_text, str):
        return []
    expanded = expr_text
    clean_params = {
        k: str(v) for k, v in (param_map or {}).items() if k and v is not None and str(v)
    }
    if clean_params:
        def _repl_param(m: re.Match[str]) -> str:
            if m.group(1) is not None:
                key = m.group(1).replace('""', '"')
                if key in clean_params:
                    return '"' + clean_params[key].replace('"', '""') + '"'
                return m.group(0)
            ident, eq_tok = m.group(2), m.group(3)
            if ident is not None and not eq_tok and ident in clean_params:
                return '"' + clean_params[ident].replace('"', '""') + '"'
            return m.group(0)

        expanded = _M_TOKEN_SUB_RE.sub(_repl_param, expanded)

    found: list[str] = []
    seen: set[str] = set()

    def _add_fqn(fqn: str) -> None:
        clean = fqn.strip().strip("`[]\"' ")
        if not clean or "[REDACTED:" in clean:
            return
        low = clean.casefold()
        if low in {"sys.tables", "information_schema.tables", "information_schema.columns"}:
            return
        if low not in seen:
            seen.add(low)
            found.append(clean)

    # 1. Power Query M Databricks.Catalogs / AzureDatabricks.Contents navigation
    nav_steps = _M_NAV_STEP_RE.findall(expanded)
    if nav_steps:
        opt_cats = _M_CATALOG_OPT_RE.findall(expanded)
        cur_cat = opt_cats[0].strip() if opt_cats else ""
        cur_sch = ""
        for name_val, kind_val in nav_steps:
            ku = kind_val.lower()
            if ku in {"database", "catalog"}:
                cur_cat = name_val.strip()
            elif ku == "schema":
                cur_sch = name_val.strip()
            elif ku in {"table", "view"}:
                tbl = name_val.strip()
                cat = cur_cat or "hive_metastore"
                if cur_sch and tbl:
                    _add_fqn(f"{cat}.{cur_sch}.{tbl}")

    # 2. SQL 3-part FROM / JOIN `catalog`.`schema`.`table`
    for m3 in _SQL_3PART_RE.finditer(expanded):
        cat = next(g for g in m3.groups()[0:4] if g is not None)
        sch = next(g for g in m3.groups()[4:8] if g is not None)
        tbl = next(g for g in m3.groups()[8:12] if g is not None)
        _add_fqn(f"{cat}.{sch}.{tbl}")

    # 3. SQL 2-part Hive / Databricks FROM <schema>.<table> when Databricks connector present
    if "databricks" in expanded.lower() or "spark" in expanded.lower() or "hive_metastore" in expanded.lower():
        opt_cats = _M_CATALOG_OPT_RE.findall(expanded)
        default_cat = opt_cats[0].strip() if opt_cats else "hive_metastore"
        for m2 in _SQL_2PART_RE.finditer(expanded):
            sch = next(g for g in m2.groups()[0:4] if g is not None)
            tbl = next(g for g in m2.groups()[4:8] if g is not None)
            if sch.lower() not in {"dbo", "sys", "information_schema"}:
                _add_fqn(f"{default_cat}.{sch}.{tbl}")

    # 4. Standalone 3-part catalog.schema.table fallback (e.g. metadata/description/partition text)
    for mb in _BARE_3PART_FQN_RE.finditer(expanded):
        c_p, s_p, t_p = mb.group(1), mb.group(2), mb.group(3)
        if not ({c_p.lower(), s_p.lower(), t_p.lower()} & _NON_FQN_PARTS):
            _add_fqn(f"{c_p}.{s_p}.{t_p}")

    return found


def _clean_owners_list(raw_owners: Sequence[Any], redact_secrets: bool) -> list[str]:
    owners: list[str] = []
    seen: set[str] = set()
    for raw_o in raw_owners:
        if raw_o is None:
            continue
        s = redact_text(str(raw_o).strip(), redact_secrets=redact_secrets)
        norm = normalize_wikilink_target(s)
        if not norm or "[REDACTED:" in norm or "REDACTED:" in norm or "@" in norm:
            continue
        if norm.casefold() not in seen:
            seen.add(norm.casefold())
            owners.append(format_wikilink(norm))
    return owners


def _clean_upstream_list(raw_refs: Sequence[Any], redact_secrets: bool) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for raw_r in raw_refs:
        if raw_r is None:
            continue
        s = redact_text(str(raw_r).strip(), redact_secrets=redact_secrets)
        norm = normalize_wikilink_target(s)
        if not norm or "[REDACTED:" in norm or "REDACTED:" in norm:
            continue
        if norm.casefold() not in seen:
            seen.add(norm.casefold())
            refs.append(format_wikilink(norm))
    return refs


def _coerce_pbi_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, (list, tuple)):
        return _join_bim_lines([_coerce_pbi_str(x) for x in val if x is not None])
    if isinstance(val, dict):
        return json.dumps(val, sort_keys=True, ensure_ascii=False, default=str)
    return str(val)


def _coerce_pbi_str_list(val: Any) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        return [val] if val.strip() else []
    if isinstance(val, dict):
        s = _coerce_pbi_str(val).strip()
        return [s] if s else []
    if isinstance(val, Sequence):
        out: list[str] = []
        for item in val:
            s = _coerce_pbi_str(item).strip()
            if s:
                out.append(s)
        return out
    s = str(val).strip()
    return [s] if s else []


def _build_measures_and_gaps(
    raw_measures: list[dict[str, Any]],
    explicit_gaps: Any,
    high_excel_export: bool,
    redact_secrets: bool,
) -> tuple[list[DaxMeasure], list[str]]:
    measures: list[DaxMeasure] = []
    seen_m: set[tuple[str, str]] = set()
    gaps: list[str] = []
    seen_gaps: set[str] = set()

    def _add_gap(g: str) -> None:
        sg = redact_text(g.strip(), redact_secrets=redact_secrets)
        if sg and sg.casefold() not in seen_gaps:
            seen_gaps.add(sg.casefold())
            gaps.append(sg)

    for eg in _coerce_pbi_str_list(explicit_gaps):
        _add_gap(eg)

    for rm in raw_measures:
        if not isinstance(rm, dict):
            continue
        m_name = redact_text(
            _coerce_pbi_str(rm.get("name")).strip(), redact_secrets=redact_secrets
        )
        if not m_name:
            continue
        m_table = redact_text(
            _coerce_pbi_str(rm.get("table")).strip(), redact_secrets=redact_secrets
        )
        raw_expr = _coerce_pbi_str(rm.get("expression"))
        detected = detect_dax_heuristics(m_name, raw_expr)
        pre_flags = [
            sf
            for f in _coerce_pbi_str_list(rm.get("flagged_heuristics"))
            if (sf := redact_text(f, redact_secrets=redact_secrets).strip())
        ]
        merged_flags: list[str] = []
        for fl in [*pre_flags, *detected]:
            if fl not in merged_flags:
                merged_flags.append(fl)

        scrubbed_expr = redact_text(raw_expr.strip(), redact_secrets=redact_secrets)
        scrubbed_desc = redact_text(
            _coerce_pbi_str(rm.get("description")).strip(), redact_secrets=redact_secrets
        )
        key = (m_table.casefold(), m_name.casefold())
        if key in seen_m:
            continue
        seen_m.add(key)

        measures.append(
            DaxMeasure(
                name=m_name,
                table=m_table,
                expression=scrubbed_expr,
                description=scrubbed_desc,
                flagged_heuristics=merged_flags,
            )
        )
        if merged_flags:
            primary = _HEURISTIC_GAP_DESCRIPTIONS.get(
                merged_flags[0],
                "replace complex DAX heuristic logic with governed Databricks ML/feature table",
            )
            flag_tags = ", ".join(merged_flags)
            _add_gap(f"Measure `{m_name}` ({flag_tags}): {primary}")

    if high_excel_export:
        _add_gap(
            "High manual Excel export activity detected — users are extracting raw slices offline due to missing predictive/decision support."
        )

    return measures, gaps


# ---------------------------------------------------------------------------
# Tier 1 Power BI / Fabric REST Scanner
# ---------------------------------------------------------------------------


def _acquire_az_cli_token() -> tuple[str | None, str | None]:
    az_bin = shutil.which("az") or "az"
    try:
        proc = subprocess.run(
            [
                az_bin,
                "account",
                "get-access-token",
                "--resource",
                "https://analysis.windows.net/powerbi/api",
                "--output",
                "json",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        return None, f"Azure CLI token probe failed: {exc}"

    if proc.returncode != 0:
        stderr_short = (proc.stderr or "").strip().splitlines()
        detail = stderr_short[0] if stderr_short else f"exit code {proc.returncode}"
        return None, f"Azure CLI token unavailable ({detail})"

    try:
        payload = json.loads(proc.stdout or "{}")
        tok = str(payload.get("accessToken") or payload.get("access_token") or "").strip()
        return (tok, None) if tok else (None, "Azure CLI returned empty accessToken.")
    except ValueError as exc:
        return None, f"Azure CLI returned invalid JSON: {exc}"


class _PowerBIRestAdapter:
    """Uniform wrapper over httpx.Client, duck-typed REST mocks, callables, or dicts."""

    BASE_URL = "https://api.powerbi.com/v1.0/myorg"

    def __init__(self, client: Any) -> None:
        self._client = client

    def _get_json(self, rel_path: str) -> Any:
        c = self._client
        if isinstance(c, dict):
            return c.get(rel_path) or c.get(rel_path.lstrip("/"))
        if callable(c) and not hasattr(c, "get"):
            return c(rel_path)
        get_fn = getattr(c, "get", None)
        if callable(get_fn):
            full_url = f"{self.BASE_URL}/{rel_path.lstrip('/')}"
            resp = get_fn(full_url)
            if resp is None:
                resp = get_fn(f"/{rel_path.lstrip('/')}")
            status = getattr(resp, "status_code", 200)
            if isinstance(status, int) and status >= 400:
                raise RuntimeError(f"HTTP {status} for {rel_path}")
            if hasattr(resp, "json") and callable(resp.json):
                return resp.json()
            return resp
        raise RuntimeError("Unsupported Power BI REST client object")

    @staticmethod
    def _unwrap_items(raw: Any, fallback_key: str = "value") -> list[dict[str, Any]]:
        if raw is None:
            return []
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
        if isinstance(raw, dict):
            for k in (fallback_key, "value", "workspaces", "groups", "items", "data"):
                if isinstance(raw.get(k), list):
                    return [x for x in raw[k] if isinstance(x, dict)]
        return []

    def list_workspaces(self) -> list[dict[str, Any]]:
        for m in ("list_workspaces", "get_workspaces", "get_groups", "list_groups"):
            fn = getattr(self._client, m, None)
            if callable(fn):
                return self._unwrap_items(fn(), "workspaces")
        if isinstance(self._client, dict) and "workspaces" in self._client:
            return self._unwrap_items(self._client["workspaces"])
        return self._unwrap_items(self._get_json("/groups"))

    def list_datasets(self, workspace_id: str) -> list[dict[str, Any]]:
        for m in ("list_datasets", "get_datasets"):
            fn = getattr(self._client, m, None)
            if callable(fn):
                return self._unwrap_items(fn(workspace_id), "datasets")
        if isinstance(self._client, dict) and isinstance(self._client.get("datasets"), dict):
            return self._unwrap_items(self._client["datasets"].get(workspace_id))
        return self._unwrap_items(self._get_json(f"/groups/{workspace_id}/datasets"))

    def list_reports(self, workspace_id: str) -> list[dict[str, Any]]:
        for m in ("list_reports", "get_reports"):
            fn = getattr(self._client, m, None)
            if callable(fn):
                return self._unwrap_items(fn(workspace_id), "reports")
        if isinstance(self._client, dict) and isinstance(self._client.get("reports"), dict):
            return self._unwrap_items(self._client["reports"].get(workspace_id))
        return self._unwrap_items(self._get_json(f"/groups/{workspace_id}/reports"))

    def get_refresh_schedule(self, workspace_id: str, dataset_id: str) -> dict[str, Any] | str | None:
        fn = getattr(self._client, "get_refresh_schedule", None)
        if callable(fn):
            try:
                try:
                    res = fn(workspace_id, dataset_id)
                except TypeError:
                    res = fn(dataset_id)
                return res if isinstance(res, (dict, str)) else None
            except Exception:
                return None
        if isinstance(self._client, dict) and isinstance(self._client.get("refresh_schedules"), dict):
            res = self._client["refresh_schedules"].get(dataset_id)
            return res if isinstance(res, (dict, str)) else None
        try:
            res = self._get_json(
                f"/groups/{workspace_id}/datasets/{dataset_id}/refreshSchedule"
            )
            return res if isinstance(res, (dict, str)) else None
        except Exception:
            return None

    def list_refreshes(self, workspace_id: str, dataset_id: str) -> list[dict[str, Any]]:
        for m in ("list_refreshes", "get_refreshes", "get_refresh_history"):
            fn = getattr(self._client, m, None)
            if callable(fn):
                try:
                    try:
                        raw_r = fn(workspace_id, dataset_id)
                    except TypeError:
                        raw_r = fn(dataset_id)
                    return self._unwrap_items(raw_r)
                except Exception:
                    return []
        if isinstance(self._client, dict) and isinstance(self._client.get("refreshes"), dict):
            return self._unwrap_items(self._client["refreshes"].get(dataset_id))
        try:
            return self._unwrap_items(
                self._get_json(f"/groups/{workspace_id}/datasets/{dataset_id}/refreshes")
            )
        except Exception:
            return []

    def list_datasources(self, workspace_id: str, dataset_id: str) -> list[dict[str, Any]]:
        for m in ("list_datasources", "get_datasources"):
            fn = getattr(self._client, m, None)
            if callable(fn):
                try:
                    try:
                        raw_ds = fn(workspace_id, dataset_id)
                    except TypeError:
                        raw_ds = fn(dataset_id)
                    return self._unwrap_items(raw_ds)
                except Exception:
                    return []
        if isinstance(self._client, dict) and isinstance(self._client.get("datasources"), dict):
            return self._unwrap_items(self._client["datasources"].get(dataset_id))
        try:
            return self._unwrap_items(
                self._get_json(f"/groups/{workspace_id}/datasets/{dataset_id}/datasources")
            )
        except Exception:
            return []


def _format_refresh_schedule(raw_sched: Any) -> str | None:
    if raw_sched is None:
        return None
    if isinstance(raw_sched, str):
        return raw_sched.strip() or None
    if isinstance(raw_sched, dict):
        if raw_sched.get("enabled") is False:
            return "Disabled"
        days = raw_sched.get("days") or []
        times = raw_sched.get("times") or []
        days_s = (
            "Daily"
            if isinstance(days, list) and len(days) == 7
            else (", ".join(str(d) for d in days) if isinstance(days, list) and days else "Scheduled")
        )
        times_s = ", ".join(str(t) for t in times) if isinstance(times, list) and times else ""
        return f"{days_s} @ {times_s}".strip(" @") if (days or times) else "Enabled"
    return str(raw_sched).strip() or None


def _parse_rest_ts(val: Any) -> datetime | None:
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    if isinstance(val, str) and val.strip():
        s = val.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _count_failed_refreshes_30d(refreshes: list[dict[str, Any]]) -> int:
    if not refreshes:
        return 0
    now_utc = datetime.now(timezone.utc)
    all_ts: list[datetime] = []
    for r in refreshes:
        ts = _parse_rest_ts(r.get("endTime") or r.get("startTime") or r.get("timestamp"))
        if ts is not None:
            all_ts.append(ts)

    if all_ts:
        max_ts = max(all_ts)
        anchor = max_ts if (now_utc - max_ts) > timedelta(days=90) else now_utc
        cutoff = anchor - timedelta(days=30)
    else:
        cutoff = None

    failed = 0
    for r in refreshes:
        status = str(r.get("status") or "").strip().lower()
        if status not in {"failed", "error"}:
            continue
        ts = _parse_rest_ts(r.get("endTime") or r.get("startTime") or r.get("timestamp"))
        if ts is None or cutoff is None or ts >= cutoff:
            failed += 1
    return failed


def _crawl_tier1_rest(
    adapter: _PowerBIRestAdapter,
    pbi_cfg: PowerBIConfig,
    redact_secrets: bool,
    warnings: list[str],
    errors: list[str],
) -> list[dict[str, Any]]:
    raw_cards: list[dict[str, Any]] = []
    try:
        workspaces = adapter.list_workspaces()
    except Exception as exc:
        errors.append(
            redact_text(f"Power BI Tier 1 workspace scan failed: {exc}", redact_secrets=redact_secrets)
        )
        return []

    inc_pats = [p.strip().lower() for p in pbi_cfg.workspaces_include if p and p.strip()]
    for ws in workspaces:
        ws_id = str(ws.get("id") or ws.get("workspaceId") or "").strip()
        ws_name = str(ws.get("name") or ws.get("workspaceName") or "Default Workspace").strip()
        if inc_pats and not any(fnmatch.fnmatchcase(ws_name.lower(), pat) for pat in inc_pats):
            continue
        if not ws_id:
            continue

        try:
            datasets = adapter.list_datasets(ws_id)
        except Exception as exc:
            warnings.append(
                redact_text(
                    f"Failed listing datasets in workspace {ws_name!r}: {exc}",
                    redact_secrets=redact_secrets,
                )
            )
            datasets = []

        try:
            reports = adapter.list_reports(ws_id)
        except Exception as exc:
            warnings.append(
                redact_text(
                    f"Failed listing reports in workspace {ws_name!r}: {exc}",
                    redact_secrets=redact_secrets,
                )
            )
            reports = []

        ds_info_by_id: dict[str, dict[str, Any]] = {}
        for ds in datasets:
            ds_id = str(ds.get("id") or ds.get("datasetId") or "").strip()
            ds_name = str(ds.get("name") or "").strip()
            sched = _format_refresh_schedule(
                ds.get("refreshSchedule") or adapter.get_refresh_schedule(ws_id, ds_id)
            )
            refreshes = (
                ds.get("refreshes")
                if isinstance(ds.get("refreshes"), list)
                else adapter.list_refreshes(ws_id, ds_id)
            )
            failures_30d = _count_failed_refreshes_30d(refreshes)

            upstream: list[str] = list(ds.get("upstream_datasets") or [])
            param_map: dict[str, str] = {}
            for expr_item in ds.get("expressions") or []:
                if isinstance(expr_item, dict):
                    en = str(expr_item.get("name") or "").strip()
                    ev = _join_bim_lines(expr_item.get("expression")).strip()
                    if en and ev:
                        unq = _unquote_m_param_expr(ev)
                        param_map[en] = unq if unq is not None else ev
                        upstream.extend(_extract_upstream_fqns_from_expression(ev))

            measures: list[dict[str, Any]] = []
            for tbl in ds.get("tables") or []:
                if not isinstance(tbl, dict):
                    continue
                tname = str(tbl.get("name") or "").strip()
                for m in tbl.get("measures") or []:
                    if isinstance(m, dict):
                        measures.append(
                            {
                                "name": m.get("name") or "",
                                "table": tname,
                                "expression": _join_bim_lines(m.get("expression")),
                                "description": m.get("description") or "",
                            }
                        )
                for src in tbl.get("source") or tbl.get("partitions") or []:
                    if isinstance(src, dict):
                        s_expr = _join_bim_lines(
                            src.get("expression")
                            or (src.get("source") or {}).get("expression")
                        )
                        upstream.extend(_extract_upstream_fqns_from_expression(s_expr, param_map))

            for dsrc in adapter.list_datasources(ws_id, ds_id):
                conn = dsrc.get("connectionDetails") or {}
                if isinstance(conn, dict):
                    cat = str(conn.get("catalog") or conn.get("database") or "").strip()
                    sch = str(conn.get("schema") or "").strip()
                    tbl = str(conn.get("table") or "").strip()
                    if cat and sch and tbl:
                        upstream.append(f"{cat}.{sch}.{tbl}")
                    if isinstance(conn.get("query"), str):
                        upstream.extend(_extract_upstream_fqns_from_expression(conn["query"], param_map))

            owners: list[str] = []
            for ok in ("configuredBy", "owner", "owners"):
                ov = ds.get(ok)
                if isinstance(ov, list):
                    owners.extend(str(x) for x in ov if x)
                elif ov:
                    owners.append(str(ov))

            ds_info_by_id[ds_id] = {
                "name": ds_name,
                "refresh_schedule": sched,
                "refresh_failures_30d": failures_30d,
                "upstream_datasets": upstream,
                "measures": measures,
                "relationships": ds.get("relationships") or [],
                "owners": owners,
                "used": False,
            }

        for rep in reports:
            r_title = str(rep.get("name") or rep.get("title") or "").strip()
            if not r_title:
                continue
            ds_id = str(rep.get("datasetId") or "").strip()
            ds_meta = ds_info_by_id.get(ds_id, {})
            if ds_id in ds_info_by_id:
                ds_info_by_id[ds_id]["used"] = True

            rep_owners = list(ds_meta.get("owners") or [])
            for ok in ("owner", "owners", "configuredBy", "modifiedBy"):
                ov = rep.get(ok)
                if isinstance(ov, list):
                    rep_owners.extend(str(x) for x in ov if x)
                elif ov:
                    rep_owners.append(str(ov))

            rep_measures = list(ds_meta.get("measures") or [])
            for m in rep.get("measures") or []:
                if isinstance(m, dict):
                    rep_measures.append(m)

            rep_id = str(rep.get("id") or rep.get("reportId") or "").strip()
            raw_cards.append(
                {
                    "_source_path": f"tier1:report:{ws_id}:{rep_id or r_title}",
                    "title": r_title,
                    "workspace": ws_name,
                    "semantic_model": str(
                        rep.get("semantic_model") or ds_meta.get("name") or r_title
                    ),
                    "refresh_schedule": ds_meta.get("refresh_schedule"),
                    "refresh_failures_30d": _safe_nonneg_int(
                        rep.get("refresh_failures_30d")
                        if rep.get("refresh_failures_30d") is not None
                        else ds_meta.get("refresh_failures_30d")
                    ),
                    "upstream_datasets": [
                        *(ds_meta.get("upstream_datasets") or []),
                        *(rep.get("upstream_datasets") or []),
                    ],
                    "measures": rep_measures,
                    "relationships": ds_meta.get("relationships") or rep.get("relationships") or [],
                    "bi_to_ds_gaps": rep.get("bi_to_ds_gaps") or [],
                    "high_excel_export": bool(
                        rep.get("high_excel_export") or rep.get("manual_excel_export_flag")
                    ),
                    "owners": rep_owners,
                }
            )

        # Emit standalone semantic models that had 0 attached reports
        for ds_id, ds_meta in ds_info_by_id.items():
            if not ds_meta["used"] and ds_meta.get("name"):
                raw_cards.append(
                    {
                        "_source_path": f"tier1:dataset:{ws_id}:{ds_id or ds_meta['name']}",
                        "title": ds_meta["name"],
                        "workspace": ws_name,
                        "semantic_model": ds_meta["name"],
                        "refresh_schedule": ds_meta.get("refresh_schedule"),
                        "refresh_failures_30d": _safe_nonneg_int(
                            ds_meta.get("refresh_failures_30d")
                        ),
                        "upstream_datasets": ds_meta.get("upstream_datasets") or [],
                        "measures": ds_meta.get("measures") or [],
                        "relationships": ds_meta.get("relationships") or [],
                        "bi_to_ds_gaps": [],
                        "high_excel_export": False,
                        "owners": ds_meta.get("owners") or [],
                    }
                )

    return raw_cards


# ---------------------------------------------------------------------------
# Tier 2 Parsers (.pbip, model.bim, TMDL, DAX INFO.*)
# ---------------------------------------------------------------------------


def _parse_bim_dict(
    loaded: dict[str, Any],
    fallback_title: str,
    fallback_workspace: str = "Default Workspace",
) -> dict[str, Any] | None:
    model_obj: dict[str, Any] | None = None
    if isinstance(loaded.get("model"), dict):
        model_obj = loaded["model"]
    elif isinstance(loaded.get("create"), dict) and isinstance(loaded["create"].get("database"), dict):
        model_obj = loaded["create"]["database"].get("model")
    elif isinstance(loaded.get("createOrReplace"), dict) and isinstance(
        loaded["createOrReplace"].get("database"), dict
    ):
        model_obj = loaded["createOrReplace"]["database"].get("model")
    elif "compatibilityLevel" in loaded or (
        isinstance(loaded.get("tables"), list)
        and any(
            isinstance(t, dict) and ("measures" in t or "partitions" in t)
            for t in loaded["tables"]
        )
    ):
        model_obj = loaded

    if not isinstance(model_obj, dict):
        return None

    title = str(
        loaded.get("title")
        or loaded.get("report_title")
        or loaded.get("name")
        or model_obj.get("name")
        or fallback_title
    ).strip()
    if title.lower() in {"model", "semanticmodel", "dataset"} and fallback_title:
        title = fallback_title

    workspace = str(loaded.get("workspace") or fallback_workspace or "Default Workspace").strip()
    sem_model = str(
        loaded.get("semantic_model") or loaded.get("name") or model_obj.get("name") or title
    ).strip()

    param_map: dict[str, str] = {}
    upstream: list[str] = list(loaded.get("upstream_datasets") or [])
    for exp in model_obj.get("expressions") or []:
        if not isinstance(exp, dict):
            continue
        ename = str(exp.get("name") or "").strip()
        e_text = _join_bim_lines(exp.get("expression"))
        unq = _unquote_m_param_expr(e_text)
        if ename and unq is not None:
            param_map[ename] = unq
        upstream.extend(_extract_upstream_fqns_from_expression(e_text, param_map))

    measures: list[dict[str, Any]] = []
    for tbl in model_obj.get("tables") or []:
        if not isinstance(tbl, dict):
            continue
        tname = str(tbl.get("name") or "").strip()
        if tname.startswith(("DateTableTemplate_", "LocalDateTable_")):
            continue
        for m in tbl.get("measures") or []:
            if not isinstance(m, dict):
                continue
            measures.append(
                {
                    "name": _coerce_pbi_str(m.get("name")).strip(),
                    "table": tname,
                    "expression": _coerce_pbi_str(m.get("expression")),
                    "description": m.get("description"),
                    "flagged_heuristics": m.get("flagged_heuristics") or [],
                }
            )
        for part in tbl.get("partitions") or []:
            if not isinstance(part, dict):
                continue
            src = part.get("source")
            if isinstance(src, dict):
                p_expr = _join_bim_lines(src.get("expression") or src.get("query"))
                upstream.extend(_extract_upstream_fqns_from_expression(p_expr, param_map))
            elif isinstance(src, str):
                upstream.extend(_extract_upstream_fqns_from_expression(src, param_map))

    relationships: list[dict[str, Any]] = []
    for rel in model_obj.get("relationships") or []:
        if not isinstance(rel, dict):
            continue
        relationships.append(
            {
                "from_table": str(rel.get("fromTable") or rel.get("from_table") or ""),
                "from_column": str(rel.get("fromColumn") or rel.get("from_column") or ""),
                "to_table": str(rel.get("toTable") or rel.get("to_table") or ""),
                "to_column": str(rel.get("toColumn") or rel.get("to_column") or ""),
                "is_active": bool(rel.get("isActive", rel.get("is_active", True))),
                "cross_filter_direction": str(
                    rel.get("crossFilteringBehavior")
                    or rel.get("cross_filter_direction")
                    or "singleDirection"
                ),
            }
        )

    owners_raw = loaded.get("owners") or ([loaded["owner"]] if loaded.get("owner") else [])
    return {
        "title": title or fallback_title or "Power BI Report",
        "workspace": workspace,
        "semantic_model": sem_model,
        "refresh_schedule": loaded.get("refresh_schedule"),
        "refresh_failures_30d": _safe_nonneg_int(loaded.get("refresh_failures_30d")),
        "upstream_datasets": upstream,
        "measures": measures,
        "relationships": relationships,
        "bi_to_ds_gaps": loaded.get("bi_to_ds_gaps") or [],
        "high_excel_export": bool(
            loaded.get("high_excel_export") or loaded.get("manual_excel_export_flag")
        ),
        "owners": owners_raw if isinstance(owners_raw, list) else [str(owners_raw)],
    }


def _unquote_tmdl_ident(raw: str) -> str:
    s = raw.strip()
    if len(s) >= 2 and s.startswith("'") and s.endswith("'"):
        return s[1:-1].replace("''", "'")
    if len(s) >= 2 and s.startswith('"') and s.endswith('"'):
        return s[1:-1].replace('""', '"')
    return s


def _split_tmdl_table_col(raw_ref: str) -> tuple[str, str]:
    s = raw_ref.strip()
    m_quoted = re.match(r"^'((?:''|[^'])+)'\.(.+)$", s)
    if m_quoted:
        return m_quoted.group(1).replace("''", "'"), _unquote_tmdl_ident(m_quoted.group(2))
    if "." in s:
        t_part, c_part = s.split(".", 1)
        return _unquote_tmdl_ident(t_part), _unquote_tmdl_ident(c_part)
    return "", _unquote_tmdl_ident(s)


_TMDL_MEASURE_HEADER_RE = re.compile(
    r"^(\s*)measure\s+('(?:''|[^'])+'|[^\s=]+)\s*=\s*(.*)$"
)
_TMDL_META_LINE_RE = re.compile(
    r"^\s*(?:formatString|lineageTag|displayFolder|description|dataCategory|summarizeBy|sortByColumn|changedProperty|annotation|kpi)\b"
    r"|^\s*isHidden\b",
    re.IGNORECASE,
)
_TMDL_SIBLING_DECL_RE = re.compile(
    r"^\s*(?:measure|column|partition|hierarchy|table|calculationGroup|annotation)\s+",
    re.IGNORECASE,
)


def _parse_table_tmdl_text(
    text: str,
    fallback_table_name: str,
    param_map: dict[str, str] | None = None,
) -> tuple[str, list[dict[str, Any]], list[str]]:
    lines = text.splitlines()
    table_name = fallback_table_name
    for ln in lines[:10]:
        mt = re.match(r"^\s*table\s+('(?:''|[^'])+'|[^\s]+)", ln)
        if mt:
            table_name = _unquote_tmdl_ident(mt.group(1))
            break

    if table_name.startswith(("DateTableTemplate_", "LocalDateTable_")):
        return table_name, [], []

    measures: list[dict[str, Any]] = []
    upstream: list[str] = []
    pending_doc: list[str] = []

    idx = 0
    n_lines = len(lines)
    while idx < n_lines:
        line = lines[idx]
        stripped = line.strip()

        if stripped.startswith("///"):
            pending_doc.append(stripped[3:].strip())
            idx += 1
            continue

        m_hdr = _TMDL_MEASURE_HEADER_RE.match(line)
        if m_hdr:
            base_indent = len(m_hdr.group(1).expandtabs(4))
            m_name = _unquote_tmdl_ident(m_hdr.group(2))
            rhs = m_hdr.group(3).strip()
            desc = " ".join(pending_doc).strip()
            pending_doc = []
            expr_lines: list[str] = []

            if rhs.startswith("```"):
                after_open = rhs[3:]
                if after_open.endswith("```") and len(after_open) >= 3:
                    expr_lines.append(after_open[:-3].strip())
                    idx += 1
                else:
                    if after_open.strip():
                        expr_lines.append(after_open)
                    idx += 1
                    while idx < n_lines:
                        fl = lines[idx]
                        if fl.strip().startswith("```"):
                            idx += 1
                            break
                        if fl.rstrip().endswith("```"):
                            expr_lines.append(fl.rstrip()[:-3])
                            idx += 1
                            break
                        expr_lines.append(fl)
                        idx += 1
            elif rhs:
                expr_lines.append(rhs)
                idx += 1
                # Continuation lines if expression explicitly continues across indented lines
                while idx < n_lines:
                    nl = lines[idx]
                    ns = nl.strip()
                    if not ns:
                        idx += 1
                        continue
                    n_indent = len(nl[: len(nl) - len(nl.lstrip())].expandtabs(4))
                    if n_indent <= base_indent or _TMDL_META_LINE_RE.match(nl) or _TMDL_SIBLING_DECL_RE.match(nl):
                        if ns.lower().startswith("description:"):
                            desc = _unquote_tmdl_ident(ns.split(":", 1)[1].strip())
                            idx += 1
                            continue
                        break
                    expr_lines.append(ns)
                    idx += 1
            else:
                idx += 1
                while idx < n_lines:
                    nl = lines[idx]
                    ns = nl.strip()
                    if not ns:
                        if expr_lines:
                            expr_lines.append("")
                        idx += 1
                        continue
                    n_indent = len(nl[: len(nl) - len(nl.lstrip())].expandtabs(4))
                    if n_indent <= base_indent or _TMDL_SIBLING_DECL_RE.match(nl):
                        break
                    if _TMDL_META_LINE_RE.match(nl):
                        if ns.lower().startswith("description:"):
                            desc = _unquote_tmdl_ident(ns.split(":", 1)[1].strip())
                        idx += 1
                        break
                    expr_lines.append(nl.strip())
                    idx += 1

            # Check immediate trailing metadata lines for `description:`
            while idx < n_lines:
                tl = lines[idx]
                ts = tl.strip()
                if not ts:
                    idx += 1
                    continue
                t_indent = len(tl[: len(tl) - len(tl.lstrip())].expandtabs(4))
                if t_indent <= base_indent or _TMDL_SIBLING_DECL_RE.match(tl):
                    break
                if ts.lower().startswith("description:"):
                    desc = _unquote_tmdl_ident(ts.split(":", 1)[1].strip())
                if _TMDL_META_LINE_RE.match(tl):
                    idx += 1
                    continue
                break

            measures.append(
                {
                    "name": m_name,
                    "table": table_name,
                    "expression": "\n".join(expr_lines).strip(),
                    "description": desc,
                }
            )
            continue

        pending_doc = []
        # Partition M/SQL source block
        m_src = re.match(r"^(\s*)source\s*=\s*(.*)$", line)
        if m_src:
            s_indent = len(m_src.group(1).expandtabs(4))
            src_buf = [m_src.group(2)]
            idx += 1
            while idx < n_lines:
                sl = lines[idx]
                ss = sl.strip()
                if not ss:
                    idx += 1
                    continue
                cur_ind = len(sl[: len(sl) - len(sl.lstrip())].expandtabs(4))
                if cur_ind <= s_indent:
                    break
                src_buf.append(sl)
                idx += 1
            upstream.extend(
                _extract_upstream_fqns_from_expression("\n".join(src_buf), param_map)
            )
            continue

        idx += 1

    return table_name, measures, upstream


def _parse_relationships_tmdl_text(text: str) -> list[dict[str, Any]]:
    rels: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None

    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("///"):
            continue
        if re.match(r"^relationship\s+", s, re.IGNORECASE):
            if cur is not None and (cur.get("from_table") or cur.get("to_table")):
                rels.append(cur)
            cur = {
                "from_table": "",
                "from_column": "",
                "to_table": "",
                "to_column": "",
                "is_active": True,
                "cross_filter_direction": "singleDirection",
            }
            continue
        if cur is None:
            continue
        if ":" in s:
            k, v = s.split(":", 1)
            kl = k.strip().lower()
            vs = v.strip()
            if kl == "fromcolumn":
                ft, fc = _split_tmdl_table_col(vs)
                cur["from_table"] = ft
                cur["from_column"] = fc
            elif kl == "tocolumn":
                tt, tc = _split_tmdl_table_col(vs)
                cur["to_table"] = tt
                cur["to_column"] = tc
            elif kl == "isactive":
                cur["is_active"] = vs.lower() not in {"false", "0", "no"}
            elif kl == "crossfilteringbehavior":
                cur["cross_filter_direction"] = vs

    if cur is not None and (cur.get("from_table") or cur.get("to_table")):
        rels.append(cur)
    return rels


def _parse_expressions_tmdl_text(text: str) -> tuple[dict[str, str], list[str]]:
    params: dict[str, str] = {}
    upstream: list[str] = []
    for m in re.finditer(
        r"^[ \t]*expression[ \t]+('(?:''|[^'\r\n])+'|[^\s=]+)[ \t]*=[ \t]*([^\r\n]+)$",
        text,
        re.MULTILINE,
    ):
        ename = _unquote_tmdl_ident(m.group(1))
        rhs = m.group(2).strip()
        unq = _unquote_m_param_expr(rhs)
        if unq is not None:
            params[ename] = unq
        upstream.extend(_extract_upstream_fqns_from_expression(rhs))
    upstream.extend(_extract_upstream_fqns_from_expression(text, params))
    return params, upstream


def _parse_tmdl_definition_dir(
    def_dir: Path,
    fallback_title: str,
    fallback_workspace: str = "Default Workspace",
    *,
    redact_secrets: bool = True,
) -> tuple[dict[str, Any], list[Path]]:
    consumed: list[Path] = []
    param_map: dict[str, str] = {}
    upstream: list[str] = []
    measures: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []

    model_tmdl = def_dir / "model.tmdl"
    if model_tmdl.is_file():
        consumed.append(model_tmdl)

    expr_tmdl = def_dir / "expressions.tmdl"
    if expr_tmdl.is_file():
        consumed.append(expr_tmdl)
        p_map, up_e = _parse_expressions_tmdl_text(_decode_file_text(expr_tmdl))
        param_map.update(p_map)
        upstream.extend(up_e)

    rel_tmdl = def_dir / "relationships.tmdl"
    if rel_tmdl.is_file():
        consumed.append(rel_tmdl)
        relationships.extend(_parse_relationships_tmdl_text(_decode_file_text(rel_tmdl)))

    tables_dir = def_dir / "tables"
    tmdl_files = (
        sorted(tables_dir.glob("*.tmdl"))
        if tables_dir.is_dir()
        else [
            p
            for p in sorted(def_dir.glob("*.tmdl"))
            if p.name.lower() not in {"model.tmdl", "expressions.tmdl", "relationships.tmdl"}
        ]
    )
    for tf in tmdl_files:
        consumed.append(tf)
        safe_tf_stem = _clean_redacted_identifier(
            _redact_pbi_text(tf.stem, redact_secrets=redact_secrets), "Table"
        )
        _, t_measures, t_up = _parse_table_tmdl_text(
            _decode_file_text(tf), safe_tf_stem, param_map=param_map
        )
        measures.extend(t_measures)
        upstream.extend(t_up)

    return (
        {
            "title": fallback_title,
            "workspace": fallback_workspace,
            "semantic_model": fallback_title,
            "refresh_schedule": None,
            "refresh_failures_30d": 0,
            "upstream_datasets": upstream,
            "measures": measures,
            "relationships": relationships,
            "bi_to_ds_gaps": [],
            "high_excel_export": False,
            "owners": [],
        },
        consumed,
    )


def _norm_info_header(raw_h: str) -> str:
    s = (raw_h or "").strip()
    if "[" in s and s.endswith("]"):
        s = s[s.rfind("[") + 1 : -1]
    s = s.strip("\"'` ")
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _info_row_keys(raw_k: str) -> tuple[str, str]:
    s = (raw_k or "").strip()
    if "[" in s and s.endswith("]"):
        s = s[s.rfind("[") + 1 : -1]
    s = s.strip("\"'` ")
    split_s = _CAMEL_WORD_SPLIT_RE.sub(
        r"\1_\2", _CAMEL_ACRONYM_SPLIT_RE.sub(r"\1_\2", s)
    )
    return _norm_info_header(raw_k), _norm_info_header(split_s)


def _norm_dict_list(raw_rows: list[Any]) -> list[dict[str, Any]]:
    out_j: list[dict[str, Any]] = []
    for item in raw_rows:
        if isinstance(item, dict):
            row_d: dict[str, Any] = {}
            for k, v in item.items():
                k_flat, k_snake = _info_row_keys(str(k))
                if k_flat and (k_flat not in row_d or row_d[k_flat] in ("", None)):
                    row_d[k_flat] = v
                if k_snake and (k_snake not in row_d or row_d[k_snake] in ("", None)):
                    row_d[k_snake] = v
            out_j.append(row_d)
    return out_j


def _extract_info_json_bundle(
    loaded: Any,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]] | None:
    if not isinstance(loaded, dict):
        return None
    has_pbi_measures = isinstance(loaded.get("measures"), list)
    has_pbi_rels = isinstance(loaded.get("relationships"), list)
    raw_tbls = loaded.get("tables")
    has_pbi_tables = False
    if isinstance(raw_tbls, list) and raw_tbls and isinstance(raw_tbls[0], dict):
        t0_keys = {_info_row_keys(str(k))[1] for k in raw_tbls[0]}
        if (
            t0_keys
            & {
                "model_id",
                "lineage_tag",
                "source_expression",
                "storage_mode",
                "is_hidden",
                "data_category",
            }
            and "catalog_name" not in t0_keys
            and "table_catalog" not in t0_keys
        ):
            has_pbi_tables = True
    if not (has_pbi_measures or has_pbi_rels or has_pbi_tables):
        return None

    sections: dict[str, list[dict[str, Any]]] = {}
    for sec in ("tables", "columns", "measures", "relationships"):
        val = loaded.get(sec)
        if isinstance(val, list):
            norm_rows = _norm_dict_list(val)
            if norm_rows:
                sections[sec] = norm_rows
    if not sections:
        return None

    meta: dict[str, Any] = {}
    for tk in ("title", "report_title", "report_name", "name"):
        tv = str(loaded.get(tk) or "").strip()
        if tv:
            meta["title"] = tv
            break
    ws = str(loaded.get("workspace") or "").strip()
    if ws:
        meta["workspace"] = ws
    sm = str(loaded.get("semantic_model") or meta.get("title") or "").strip()
    if sm:
        meta["semantic_model"] = sm
    raw_owners = loaded.get("owners")
    if isinstance(raw_owners, str) and raw_owners.strip():
        meta["owners"] = [raw_owners.strip()]
    elif isinstance(raw_owners, list):
        meta["owners"] = [o for o in raw_owners if o]
    return sections, meta


def _load_info_rows(path: Path) -> list[dict[str, Any]]:
    text = _decode_file_text(path)
    if path.suffix.lower() == ".json":
        loaded = json.loads(text)
        raw_list: list[Any] = []
        if isinstance(loaded, list):
            raw_list = loaded
        elif isinstance(loaded, dict):
            for k in ("rows", "tables", "data", "value", "results"):
                if isinstance(loaded.get(k), list):
                    raw_list = loaded[k]
                    break
        return _norm_dict_list(raw_list)

    reader = csv.DictReader(io.StringIO(text))
    out_c: list[dict[str, Any]] = []
    for r in reader:
        nr: dict[str, str] = {}
        for k, v in r.items():
            if k is None:
                continue
            sv = (v or "").strip()
            k_flat, k_snake = _info_row_keys(k)
            if k_flat and (k_flat not in nr or not nr[k_flat]):
                nr[k_flat] = sv
            if k_snake and (k_snake not in nr or not nr[k_snake]):
                nr[k_snake] = sv
        if any(nr.values()):
            out_c.append(nr)
    return out_c


def _classify_info_export(path: Path, rows: list[dict[str, Any]]) -> str | None:
    nl = path.name.lower()
    if nl in _DBX_ONLY_CSV_NAMES:
        return None
    if "relationship" in nl:
        return "relationships"
    if "measure" in nl:
        return "measures"
    if "column" in nl and ("info" in nl or path.suffix.lower() in {".csv", ".json"}):
        if rows and any(k in rows[0] for k in ("explicit_name", "explicitname", "inferred_name", "inferredname", "table_id", "tableid")):
            return "columns"
        if "info" in nl:
            return "columns"
    if "table" in nl and "info" in nl:
        return "tables"

    if not rows:
        return None
    hdrs = set(rows[0].keys())
    if {"from_column_id", "to_column_id"} <= hdrs or {"fromcolumnid", "tocolumnid"} <= hdrs or (
        ("from_table" in hdrs or "from_table_id" in hdrs or "fromtableid" in hdrs)
        and ("to_table" in hdrs or "to_table_id" in hdrs or "totableid" in hdrs)
    ):
        return "relationships"
    if "expression" in hdrs and any(
        k in hdrs for k in ("table_id", "tableid", "table", "table_name", "measure_name", "name")
    ):
        return "measures"
    if any(k in hdrs for k in ("explicit_name", "explicitname", "inferred_name", "inferredname")):
        return "columns"
    if "storage_mode" in hdrs or "storagemode" in hdrs or ("lineage_tag" in hdrs and "name" in hdrs and "expression" not in hdrs):
        return "tables"
    return None


def _iget(row: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in row and row[k] is not None and str(row[k]) != "":
            return row[k]
    return None


def _parse_info_group(
    dir_path: Path,
    grouped_files: dict[str, list[tuple[Path, list[dict[str, Any]]]]],
    root_dir: Path,
    *,
    redact_secrets: bool = True,
    meta_overrides: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[Path]]:
    consumed: list[Path] = []
    seen_consumed: set[Path] = set()

    def _mark_consumed(p: Path) -> None:
        rp = p.resolve()
        if rp not in seen_consumed:
            seen_consumed.add(rp)
            consumed.append(p)

    table_id_to_name: dict[str, str] = {}
    col_id_to_pair: dict[str, tuple[str, str]] = {}
    upstream: list[str] = []

    for p, rows in grouped_files.get("tables", []):
        _mark_consumed(p)
        for r in rows:
            tid = str(_iget(r, "id", "table_id", "tableid") or "").strip()
            tname = str(_iget(r, "name", "table_name", "table") or "").strip()
            if tid and tname:
                table_id_to_name[tid] = tname
            for col_k in (
                "expression",
                "source_expression",
                "partition_expression",
                "m_expression",
                "query",
                "source",
                "data_source",
                "table_storage",
                "description",
            ):
                val = str(_iget(r, col_k) or "").strip()
                if val:
                    upstream.extend(_extract_upstream_fqns_from_expression(val))

    for p, rows in grouped_files.get("columns", []):
        _mark_consumed(p)
        for r in rows:
            cid = str(_iget(r, "id", "column_id", "columnid") or "").strip()
            tid = str(_iget(r, "table_id", "tableid") or "").strip()
            tname = (
                str(_iget(r, "table", "table_name") or "").strip()
                or table_id_to_name.get(tid, "")
            )
            cname = str(
                _iget(
                    r,
                    "explicit_name",
                    "explicitname",
                    "inferred_name",
                    "inferredname",
                    "name",
                    "column_name",
                )
                or ""
            ).strip()
            if cid and cname:
                col_id_to_pair[cid] = (tname, cname)

    seen_measures: dict[str, dict[str, Any]] = {}
    for p, rows in grouped_files.get("measures", []):
        _mark_consumed(p)
        for r in rows:
            mname = str(_iget(r, "name", "measure_name", "measure") or "").strip()
            if not mname:
                continue
            tid = str(_iget(r, "table_id", "tableid") or "").strip()
            tname = (
                str(_iget(r, "table", "table_name") or "").strip()
                or table_id_to_name.get(tid, "")
            )
            expr = str(_iget(r, "expression", "dax", "formula") or "")
            desc = str(_iget(r, "description", "comment") or "")
            mk = mname.casefold()
            entry = {
                "name": mname,
                "table": tname,
                "expression": expr,
                "description": desc,
            }
            if mk not in seen_measures:
                seen_measures[mk] = entry
            else:
                prev_m = seen_measures[mk]
                for fld in ("table", "expression", "description"):
                    if not prev_m.get(fld) and entry.get(fld):
                        prev_m[fld] = entry[fld]
    measures: list[dict[str, Any]] = list(seen_measures.values())

    seen_rels: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for p, rows in grouped_files.get("relationships", []):
        _mark_consumed(p)
        for r in rows:
            ft_id = str(_iget(r, "from_table_id", "fromtableid") or "").strip()
            fc_id = str(_iget(r, "from_column_id", "fromcolumnid") or "").strip()
            tt_id = str(_iget(r, "to_table_id", "totableid") or "").strip()
            tc_id = str(_iget(r, "to_column_id", "tocolumnid") or "").strip()

            ft = (
                str(_iget(r, "from_table", "fromtable") or "").strip()
                or table_id_to_name.get(ft_id, "")
                or col_id_to_pair.get(fc_id, ("", ""))[0]
            )
            fc = (
                str(_iget(r, "from_column", "fromcolumn") or "").strip()
                or col_id_to_pair.get(fc_id, ("", fc_id))[1]
            )
            tt = (
                str(_iget(r, "to_table", "totable") or "").strip()
                or table_id_to_name.get(tt_id, "")
                or col_id_to_pair.get(tc_id, ("", ""))[0]
            )
            tc = (
                str(_iget(r, "to_column", "tocolumn") or "").strip()
                or col_id_to_pair.get(tc_id, ("", tc_id))[1]
            )
            raw_act = _iget(r, "is_active", "isactive")
            is_act = (
                str(raw_act).strip().lower() not in {"false", "0", "no"}
                if raw_act is not None
                else True
            )
            xdir = str(
                _iget(
                    r,
                    "cross_filtering_behavior",
                    "crossfilteringbehavior",
                    "cross_filter_direction",
                )
                or "singleDirection"
            )
            rk = (ft.casefold(), fc.casefold(), tt.casefold(), tc.casefold())
            seen_rels[rk] = {
                "from_table": ft,
                "from_column": fc,
                "to_table": tt,
                "to_column": tc,
                "is_active": is_act,
                "cross_filter_direction": xdir,
            }
    relationships: list[dict[str, Any]] = list(seen_rels.values())

    if dir_path.resolve() != root_dir.resolve() and dir_path.name.lower() not in {
        "powerbi",
        "inbox",
        "exports",
    }:
        clean_dir = _clean_redacted_identifier(
            _redact_pbi_text(dir_path.name, redact_secrets=redact_secrets), ""
        )
        raw_title = (
            clean_dir.replace("_", " ").replace("-", " ").strip().title()
            if clean_dir
            else "Power BI Report"
        )
    else:
        first_stem = consumed[0].stem if consumed else "Power BI Semantic Model"
        cleaned_stem = re.sub(
            r"(?i)^info[._-]*(?:tables|columns|measures|relationships|export)[._-]*",
            "",
            first_stem,
        ).strip(" _-")
        scrubbed_stem = (
            _clean_redacted_identifier(
                _redact_pbi_text(cleaned_stem, redact_secrets=redact_secrets), ""
            )
            if cleaned_stem
            else ""
        )
        raw_title = (
            scrubbed_stem.replace("_", " ").title()
            if scrubbed_stem
            else ("Power BI Report" if cleaned_stem else "Power BI Semantic Model")
        )

    meta = meta_overrides or {}
    final_title = str(meta.get("title") or raw_title).strip() or raw_title
    final_ws = str(meta.get("workspace") or "Default Workspace").strip() or "Default Workspace"
    final_sm = str(meta.get("semantic_model") or final_title).strip() or final_title
    final_owners = list(meta.get("owners") or [])

    return (
        {
            "title": final_title,
            "workspace": final_ws,
            "semantic_model": final_sm,
            "refresh_schedule": None,
            "refresh_failures_30d": 0,
            "upstream_datasets": upstream,
            "measures": measures,
            "relationships": relationships,
            "bi_to_ds_gaps": [],
            "high_excel_export": False,
            "owners": final_owners,
        },
        consumed,
    )


_GENERIC_MODEL_STEMS: frozenset[str] = frozenset(
    {"", "model", "project", "report", "dataset", "semanticmodel", "default"}
)


def _merge_card_dicts(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    if extra.get("title") and str(out.get("title") or "").strip().lower() in _GENERIC_MODEL_STEMS:
        out["title"] = extra["title"]
    if (
        extra.get("semantic_model")
        and str(out.get("semantic_model") or "").strip().lower() in _GENERIC_MODEL_STEMS
    ):
        out["semantic_model"] = extra["semantic_model"]
    if extra.get("workspace") and out.get("workspace") in {"", None, "Default Workspace"}:
        out["workspace"] = extra["workspace"]
    seen_m: dict[str, dict[str, Any]] = {}
    for m in [*(out.get("measures") or []), *(extra.get("measures") or [])]:
        if not isinstance(m, dict):
            continue
        mk = str(m.get("name") or "").strip().casefold()
        if not mk:
            continue
        if mk not in seen_m:
            seen_m[mk] = dict(m)
        else:
            prev = seen_m[mk]
            for fld in ("table", "expression", "description"):
                if not prev.get(fld) and m.get(fld):
                    prev[fld] = m[fld]
    out["measures"] = list(seen_m.values())
    seen_r: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for r in [*(out.get("relationships") or []), *(extra.get("relationships") or [])]:
        if not isinstance(r, dict):
            continue
        rk = (
            str(r.get("from_table") or "").casefold(),
            str(r.get("from_column") or "").casefold(),
            str(r.get("to_table") or "").casefold(),
            str(r.get("to_column") or "").casefold(),
        )
        seen_r[rk] = r
    out["relationships"] = list(seen_r.values())
    out["upstream_datasets"] = [
        *(out.get("upstream_datasets") or []),
        *(extra.get("upstream_datasets") or []),
    ]
    out["bi_to_ds_gaps"] = [*(out.get("bi_to_ds_gaps") or []), *(extra.get("bi_to_ds_gaps") or [])]
    out["owners"] = [*(out.get("owners") or []), *(extra.get("owners") or [])]
    out["high_excel_export"] = bool(out.get("high_excel_export") or extra.get("high_excel_export"))
    return out


def _crawl_tier2_local(
    target_path: Path,
    warnings: list[str],
    errors: list[str],
    processed_files: list[str],
    *,
    redact_secrets: bool = True,
    explicit_input: bool = False,
) -> list[dict[str, Any]]:
    if not target_path.exists():
        return []

    orig_target = target_path.resolve()
    raw_cards: list[dict[str, Any]] = []
    claimed_paths: set[Path] = set()

    def _claim_tree(p: Path) -> None:
        claimed_paths.add(p.resolve())
        if p.is_dir():
            for sub in p.rglob("*"):
                claimed_paths.add(sub.resolve())

    # Single-file direct input
    if target_path.is_file():
        suf = target_path.suffix.lower()
        if suf == ".pbip":
            # Delegate to parent dir scan
            target_path = target_path.parent
        elif suf in {".bim", ".json", ".tmdl", ".csv"}:
            try:
                if suf in {".bim", ".json"}:
                    try:
                        loaded = json.loads(_decode_file_text(target_path))
                    except Exception as exc:
                        errors.append(
                            redact_text(
                                f"Invalid JSON in {target_path.name}: {exc}",
                                redact_secrets=redact_secrets,
                            )
                        )
                        return []
                    if isinstance(loaded, dict):
                        parsed = _parse_bim_dict(loaded, fallback_title=target_path.stem)
                        if parsed is not None:
                            processed_files.append(target_path.as_posix())
                            return [parsed]
                        bundle_res = _extract_info_json_bundle(loaded)
                        if bundle_res is not None:
                            b_secs, b_meta = bundle_res
                            b_grouped = {
                                sk: [(target_path, srows)] for sk, srows in b_secs.items()
                            }
                            card_b, _ = _parse_info_group(
                                target_path.parent,
                                b_grouped,
                                target_path.parent,
                                redact_secrets=redact_secrets,
                                meta_overrides=b_meta,
                            )
                            processed_files.append(target_path.as_posix())
                            return [card_b]
                    rows = _load_info_rows(target_path)
                    kind = _classify_info_export(target_path, rows)
                    if kind is not None:
                        card_d, _ = _parse_info_group(
                            target_path.parent,
                            {kind: [(target_path, rows)]},
                            target_path.parent,
                            redact_secrets=redact_secrets,
                        )
                        processed_files.append(target_path.as_posix())
                        return [card_d]
                elif suf == ".tmdl":
                    if target_path.name.lower() == "relationships.tmdl":
                        rels = _parse_relationships_tmdl_text(_decode_file_text(target_path))
                        processed_files.append(target_path.as_posix())
                        safe_parent = _clean_redacted_identifier(
                            _redact_pbi_text(
                                target_path.parent.name, redact_secrets=redact_secrets
                            ),
                            "TMDL Model",
                        )
                        return [
                            {
                                "title": safe_parent,
                                "workspace": "Default Workspace",
                                "semantic_model": safe_parent,
                                "measures": [],
                                "relationships": rels,
                                "upstream_datasets": [],
                                "bi_to_ds_gaps": [],
                                "high_excel_export": False,
                                "owners": [],
                            }
                        ]
                    safe_tstem = _clean_redacted_identifier(
                        _redact_pbi_text(target_path.stem, redact_secrets=redact_secrets),
                        "Power BI Report",
                    )
                    tname, ms, up = _parse_table_tmdl_text(
                        _decode_file_text(target_path), safe_tstem
                    )
                    processed_files.append(target_path.as_posix())
                    return [
                        {
                            "title": tname,
                            "workspace": "Default Workspace",
                            "semantic_model": tname,
                            "measures": ms,
                            "relationships": [],
                            "upstream_datasets": up,
                            "bi_to_ds_gaps": [],
                            "high_excel_export": False,
                            "owners": [],
                        }
                    ]
                elif suf == ".csv":
                    rows = _load_info_rows(target_path)
                    kind = _classify_info_export(target_path, rows)
                    if kind is not None:
                        card_d, _ = _parse_info_group(
                            target_path.parent,
                            {kind: [(target_path, rows)]},
                            target_path.parent,
                            redact_secrets=redact_secrets,
                        )
                        processed_files.append(target_path.as_posix())
                        return [card_d]
            except Exception as exc:
                errors.append(
                    redact_text(
                        f"Failed parsing {target_path.name}: {exc}",
                        redact_secrets=redact_secrets,
                    )
                )
                return []
            return []
        else:
            return []

    skip_dirs = {"databricks", "teams", "notes", "processed", "archive", "__pycache__", ".git"}

    # Pass 1: *.pbip projects, *.SemanticModel / *.Dataset folders, and hybrid model.bim + definition/ dirs
    def _load_merged_model_root(
        model_root: Path,
        fallback_title: str,
        *,
        claim_whole_dir: bool,
        is_explicit_target: bool,
    ) -> dict[str, Any] | None:
        if claim_whole_dir:
            _claim_tree(model_root)
        card_acc: dict[str, Any] | None = None
        bim_p = model_root / "model.bim"
        def_dir = model_root / "definition"
        if not claim_whole_dir:
            if bim_p.is_file():
                claimed_paths.add(bim_p.resolve())
            if def_dir.is_dir():
                _claim_tree(def_dir)
        if bim_p.is_file():
            try:
                loaded_b = json.loads(_decode_file_text(bim_p))
                if isinstance(loaded_b, dict):
                    card_acc = _parse_bim_dict(loaded_b, fallback_title=fallback_title)
                    if card_acc is not None:
                        processed_files.append(bim_p.as_posix())
            except Exception as exc:
                msg = redact_text(
                    f"Failed parsing {bim_p.name}: {exc}", redact_secrets=redact_secrets
                )
                if is_explicit_target and not def_dir.is_dir():
                    errors.append(msg)
                else:
                    warnings.append(msg)
        if def_dir.is_dir():
            try:
                tmdl_card, tmdl_used = _parse_tmdl_definition_dir(
                    def_dir, fallback_title=fallback_title, redact_secrets=redact_secrets
                )
                processed_files.extend(p.as_posix() for p in tmdl_used)
                card_acc = _merge_card_dicts(card_acc, tmdl_card) if card_acc else tmdl_card
            except Exception as exc:
                msg = redact_text(
                    f"Failed parsing TMDL in {def_dir.name}: {exc}",
                    redact_secrets=redact_secrets,
                )
                if is_explicit_target and card_acc is None:
                    errors.append(msg)
                else:
                    warnings.append(msg)
        return card_acc

    # Pass 1a: *.pbip descriptor-anchored projects (subfolder or flat layout)
    pbips_by_dir: dict[Path, list[Path]] = {}
    for root, dirs, files in os.walk(target_path):
        dirs[:] = sorted(
            d for d in dirs if not d.startswith(".") and d.lower() not in skip_dirs
        )
        rp = Path(root)
        for fname in sorted(files):
            if not fname.startswith((".", "~$")) and fname.lower().endswith(".pbip"):
                pbips_by_dir.setdefault(rp, []).append(rp / fname)

    for pdir, pbip_list in pbips_by_dir.items():
        for pbip_fp in pbip_list:
            if pbip_fp.resolve() in claimed_paths:
                continue
            claimed_paths.add(pbip_fp.resolve())
            processed_files.append(pbip_fp.as_posix())
            pstem = pbip_fp.stem.strip()

            pbip_obj: dict[str, Any] = {}
            try:
                loaded_pbip = json.loads(_decode_file_text(pbip_fp))
                if isinstance(loaded_pbip, dict):
                    pbip_obj = loaded_pbip
            except Exception as exc:
                warnings.append(
                    redact_text(
                        f"Failed parsing {pbip_fp.name}: {exc}", redact_secrets=redact_secrets
                    )
                )

            settings = (
                pbip_obj.get("settings") if isinstance(pbip_obj.get("settings"), dict) else {}
            )
            pbip_ws = str(pbip_obj.get("workspace") or settings.get("workspace") or "").strip()
            raw_pbip_owners = pbip_obj.get("owners") or settings.get("owners") or []
            pbip_owners: list[Any] = (
                [raw_pbip_owners]
                if isinstance(raw_pbip_owners, str)
                else list(raw_pbip_owners)
                if isinstance(raw_pbip_owners, list)
                else []
            )
            explicit_pbip_title = str(
                pbip_obj.get("title") or pbip_obj.get("name") or ""
            ).strip()

            art_rep_rel = ""
            art_sem_rel = ""
            artifacts = pbip_obj.get("artifacts")
            if isinstance(artifacts, list):
                for art in artifacts:
                    if not isinstance(art, dict):
                        continue
                    rep_blk = art.get("report")
                    if isinstance(rep_blk, dict) and rep_blk.get("path") and not art_rep_rel:
                        art_rep_rel = str(rep_blk["path"]).strip()
                    sem_blk = art.get("semanticModel") or art.get("dataset")
                    if isinstance(sem_blk, dict) and sem_blk.get("path") and not art_sem_rel:
                        art_sem_rel = str(sem_blk["path"]).strip()

            rep_dir: Path | None = None
            if art_rep_rel and (pdir / art_rep_rel).is_dir():
                rep_dir = pdir / art_rep_rel
            elif (pdir / f"{pstem}.Report").is_dir():
                rep_dir = pdir / f"{pstem}.Report"
            elif len(pbip_list) == 1:
                cand_reps = [
                    d
                    for d in sorted(pdir.iterdir())
                    if d.is_dir()
                    and d.name.lower().endswith(".report")
                    and d.resolve() not in claimed_paths
                ]
                if len(cand_reps) == 1:
                    rep_dir = cand_reps[0]

            if rep_dir is not None and rep_dir.is_dir():
                _claim_tree(rep_dir)
                rep_json = rep_dir / "report.json"
                if rep_json.is_file():
                    processed_files.append(rep_json.as_posix())

            sdir: Path | None = None
            if art_sem_rel and (pdir / art_sem_rel).is_dir():
                sdir = pdir / art_sem_rel
            elif (pdir / f"{pstem}.SemanticModel").is_dir():
                sdir = pdir / f"{pstem}.SemanticModel"
            elif (pdir / f"{pstem}.Dataset").is_dir():
                sdir = pdir / f"{pstem}.Dataset"
            elif len(pbip_list) == 1:
                cand_sems = [
                    d
                    for d in sorted(pdir.iterdir())
                    if d.is_dir()
                    and d.name.lower().endswith((".semanticmodel", ".dataset"))
                    and d.resolve() not in claimed_paths
                ]
                if len(cand_sems) == 1:
                    sdir = cand_sems[0]
                elif (pdir / "model.bim").is_file() or (pdir / "definition").is_dir():
                    sdir = pdir

            if sdir is None:
                continue

            rep_stem = (
                re.sub(r"(?i)\.report$", "", rep_dir.name).strip()
                if rep_dir is not None
                else ""
            )
            sem_stem = (
                re.sub(r"(?i)\.(?:semanticmodel|dataset)$", "", sdir.name).strip()
                if sdir != pdir
                else ""
            )
            preferred_title = ""
            for cand_t in (explicit_pbip_title, pstem, rep_stem, sem_stem):
                if cand_t and cand_t.lower() not in _GENERIC_MODEL_STEMS:
                    preferred_title = cand_t
                    break
            fb_title = preferred_title or pdir.name or pstem or "Power BI Model"

            is_exp = bool(
                explicit_input
                and orig_target in {pbip_fp.resolve(), pdir.resolve(), sdir.resolve()}
            )
            card_acc = _load_merged_model_root(
                sdir,
                fb_title,
                claim_whole_dir=(sdir != pdir),
                is_explicit_target=is_exp,
            )
            if card_acc is not None:
                if preferred_title:
                    card_acc["title"] = preferred_title
                if (
                    str(card_acc.get("semantic_model") or "").strip().lower()
                    in _GENERIC_MODEL_STEMS
                ):
                    card_acc["semantic_model"] = sem_stem or card_acc["title"]
                if pbip_ws:
                    card_acc["workspace"] = pbip_ws
                if pbip_owners:
                    card_acc["owners"] = [*(card_acc.get("owners") or []), *pbip_owners]
                card_acc["_source_path"] = pbip_fp.resolve().as_posix()
                raw_cards.append(card_acc)

    # Pass 1b: Remaining unclaimed *.SemanticModel / *.Dataset folders & hybrid model.bim + definition/ dirs
    sem_dirs: list[tuple[Path, bool]] = []
    if target_path.name.lower().endswith((".semanticmodel", ".dataset")):
        sem_dirs.append((target_path, True))
    else:
        if (
            target_path.resolve() not in claimed_paths
            and (target_path / "model.bim").is_file()
            and (target_path / "definition").is_dir()
            and (target_path / "model.bim").resolve() not in claimed_paths
        ):
            sem_dirs.append((target_path, False))
        for root, dirs, _files in os.walk(target_path):
            dirs[:] = sorted(
                d for d in dirs if not d.startswith(".") and d.lower() not in skip_dirs
            )
            rp = Path(root)
            for d in list(dirs):
                dp = rp / d
                if d.lower().endswith((".semanticmodel", ".dataset")):
                    sem_dirs.append((dp, True))
                elif (
                    dp.resolve() not in claimed_paths
                    and (dp / "model.bim").is_file()
                    and (dp / "definition").is_dir()
                    and (dp / "model.bim").resolve() not in claimed_paths
                ):
                    sem_dirs.append((dp, False))

    for sdir, is_sem_pkg in sem_dirs:
        if sdir.resolve() in claimed_paths:
            continue
        if not is_sem_pkg and (
            (sdir / "model.bim").resolve() in claimed_paths
            or (sdir / "definition").resolve() in claimed_paths
        ):
            continue
        base_stem = re.sub(r"(?i)\.(?:semanticmodel|dataset)$", "", sdir.name).strip()
        report_title = base_stem
        if is_sem_pkg:
            sibling_rep = sdir.parent / f"{base_stem}.Report"
            sibling_pbip = sdir.parent / f"{base_stem}.pbip"
            if sibling_rep.is_dir():
                _claim_tree(sibling_rep)
                rep_json = sibling_rep / "report.json"
                if rep_json.is_file():
                    processed_files.append(rep_json.as_posix())
            if sibling_pbip.is_file():
                claimed_paths.add(sibling_pbip.resolve())
                processed_files.append(sibling_pbip.as_posix())

        card_acc = _load_merged_model_root(
            sdir,
            report_title,
            claim_whole_dir=is_sem_pkg,
            is_explicit_target=bool(explicit_input and sdir.resolve() == orig_target),
        )
        if card_acc is not None:
            if is_sem_pkg:
                card_acc["title"] = report_title
                card_acc["semantic_model"] = base_stem
            card_acc["_source_path"] = sdir.resolve().as_posix()
            raw_cards.append(card_acc)

    # Pass 2: Unclaimed TMDL definition directories (`definition/tables/*.tmdl` or dir with `*.tmdl`)
    tmdl_roots: list[Path] = []
    if (target_path / "tables").is_dir() or any(target_path.glob("*.tmdl")):
        if target_path.resolve() not in claimed_paths:
            tmdl_roots.append(target_path)
    for root, dirs, files in os.walk(target_path):
        dirs[:] = sorted(
            d for d in dirs if not d.startswith(".") and d.lower() not in skip_dirs
        )
        rp = Path(root)
        if rp.resolve() in claimed_paths:
            continue
        if rp.name.lower() == "definition" or (
            any(f.lower().endswith(".tmdl") for f in files)
            and rp.name.lower() != "tables"
        ):
            tmdl_roots.append(rp)

    for troot in tmdl_roots:
        if troot.resolve() in claimed_paths:
            continue
        _claim_tree(troot)
        parent_label = (
            troot.parent.name
            if troot.name.lower() == "definition" and troot.parent != target_path.parent
            else troot.name
        )
        red_parent = _redact_pbi_text(parent_label, redact_secrets=redact_secrets)
        clean_parent = _clean_redacted_identifier(red_parent, "")
        fallback_t = clean_parent.replace("_", " ").strip() or (
            "Power BI Report" if "[REDACTED:" in red_parent else "Power BI Model"
        )
        try:
            tmdl_card, tmdl_used = _parse_tmdl_definition_dir(
                troot, fallback_title=fallback_t, redact_secrets=redact_secrets
            )
            if tmdl_used:
                processed_files.extend(p.as_posix() for p in tmdl_used)
                tmdl_card["_source_path"] = troot.resolve().as_posix()
                raw_cards.append(tmdl_card)
        except Exception as exc:
            msg = redact_text(
                f"Failed parsing TMDL in {troot.name}: {exc}", redact_secrets=redact_secrets
            )
            if explicit_input and troot.resolve() == orig_target:
                errors.append(msg)
            else:
                warnings.append(msg)

    # Pass 3: Unclaimed standalone *.bim / model JSON files & DAX INFO.* exports per directory
    info_by_dir: dict[Path, dict[str, list[tuple[Path, list[dict[str, Any]]]]]] = {}
    info_meta_by_dir: dict[Path, dict[str, Any]] = {}

    for root, dirs, files in os.walk(target_path):
        dirs[:] = sorted(
            d for d in dirs if not d.startswith(".") and d.lower() not in skip_dirs
        )
        rp = Path(root)
        for fname in sorted(files):
            if fname.startswith((".", "~$")):
                continue
            fp = rp / fname
            if fp.resolve() in claimed_paths:
                continue
            suf = fp.suffix.lower()
            if suf in {".bim", ".json"}:
                try:
                    loaded = json.loads(_decode_file_text(fp))
                except Exception as exc:
                    if suf == ".bim":
                        warnings.append(
                            redact_text(
                                f"Failed parsing {fp.name}: {exc}",
                                redact_secrets=redact_secrets,
                            )
                        )
                    continue
                if isinstance(loaded, dict):
                    fb_title = (
                        rp.name
                        if fp.stem.lower() == "model" and rp.resolve() != target_path.resolve()
                        else fp.stem
                    )
                    try:
                        bim_card = _parse_bim_dict(loaded, fallback_title=fb_title)
                    except Exception as exc:
                        warnings.append(
                            redact_text(
                                f"Failed parsing {fp.name}: {exc}",
                                redact_secrets=redact_secrets,
                            )
                        )
                        continue
                    if bim_card is not None:
                        claimed_paths.add(fp.resolve())
                        processed_files.append(fp.as_posix())
                        bim_card["_source_path"] = fp.resolve().as_posix()
                        raw_cards.append(bim_card)
                        continue
                    bundle_res = _extract_info_json_bundle(loaded)
                    if bundle_res is not None:
                        b_secs, b_meta = bundle_res
                        claimed_paths.add(fp.resolve())
                        for sk, srows in b_secs.items():
                            info_by_dir.setdefault(rp, {}).setdefault(sk, []).append(
                                (fp, srows)
                            )
                        if b_meta:
                            cur_m = info_meta_by_dir.setdefault(rp, {})
                            for mk, mv in b_meta.items():
                                if mk == "owners":
                                    cur_m["owners"] = [*(cur_m.get("owners") or []), *mv]
                                elif mv and not cur_m.get(mk):
                                    cur_m[mk] = mv
                        continue
                # Check if JSON is a single-table DAX INFO.* export
                try:
                    j_rows = _load_info_rows(fp)
                    kind = _classify_info_export(fp, j_rows)
                    if kind is not None:
                        info_by_dir.setdefault(rp, {}).setdefault(kind, []).append((fp, j_rows))
                except Exception:
                    pass
            elif suf == ".csv":
                if fname.lower() in _DBX_ONLY_CSV_NAMES:
                    continue
                try:
                    c_rows = _load_info_rows(fp)
                except Exception as exc:
                    warnings.append(
                        redact_text(
                            f"Failed reading CSV {fp.name}: {exc}",
                            redact_secrets=redact_secrets,
                        )
                    )
                    continue
                kind = _classify_info_export(fp, c_rows)
                if kind is not None:
                    info_by_dir.setdefault(rp, {}).setdefault(kind, []).append((fp, c_rows))

    for dir_p, grouped in sorted(
        info_by_dir.items(), key=lambda kv: kv[0].resolve().as_posix().casefold()
    ):
        try:
            info_card, used_paths = _parse_info_group(
                dir_p,
                grouped,
                root_dir=target_path,
                redact_secrets=redact_secrets,
                meta_overrides=info_meta_by_dir.get(dir_p),
            )
            processed_files.extend(p.as_posix() for p in used_paths)
            info_card["_source_path"] = dir_p.resolve().as_posix()
            raw_cards.append(info_card)
        except Exception as exc:
            msg = redact_text(
                f"Failed parsing INFO export in {dir_p.name}: {exc}",
                redact_secrets=redact_secrets,
            )
            if explicit_input and dir_p.resolve() == orig_target:
                errors.append(msg)
            else:
                warnings.append(msg)

    return raw_cards


def _slug_pbi_ref(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _unwrap_unresolved_wikilinks(text: str, known_keys: set[str]) -> str:
    return unwrap_prose_wikilinks(text, known_keys)


def _sanitize_pbi_value(
    val: Any,
    redact_secrets: bool,
    known_keys: set[str] | None = None,
) -> Any:
    while isinstance(val, Enum):
        val = val.value
    if val is None or isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return val if math.isfinite(val) else None
    if isinstance(val, Decimal):
        if not val.is_finite():
            return None
        _, digits, exp = val.as_tuple()
        if isinstance(exp, int) and exp >= 0 and len(digits) + exp <= 38:
            return int(val)
        try:
            f = float(val)
        except OverflowError:
            return None
        return f if math.isfinite(f) else None
    if isinstance(val, (bytes, bytearray)):
        val = val.decode("utf-8", errors="replace")
    elif isinstance(val, Path):
        val = val.as_posix()
    elif isinstance(val, (datetime, date)):
        val = val.isoformat()
    if isinstance(val, str):
        s = redact_text(val, redact_secrets=redact_secrets)
        return _unwrap_unresolved_wikilinks(s, known_keys) if known_keys is not None else s
    if isinstance(val, dict):
        out_d: dict[str, Any] = {}
        for k, v in val.items():
            sk = _sanitize_pbi_value(k, redact_secrets, known_keys)
            ks = sk if isinstance(sk, str) else str(sk)
            out_d[ks] = _sanitize_pbi_value(v, redact_secrets, known_keys)
        return out_d
    if isinstance(val, (list, tuple, set, frozenset)):
        return [_sanitize_pbi_value(v, redact_secrets, known_keys) for v in val]
    s = redact_text(str(val), redact_secrets=redact_secrets)
    return _unwrap_unresolved_wikilinks(s, known_keys) if known_keys is not None else s


def _build_pbi_known_vault_keys(vdir: Path, raw_cards: list[dict[str, Any]]) -> set[str]:
    keys: set[str] = set()

    def _reg(*vals: Any) -> None:
        for v in vals:
            if v is None:
                continue
            s = str(v).strip()
            if s:
                keys.add(s.casefold())
                sl = _slug_pbi_ref(s)
                if sl:
                    keys.add(sl)

    for rc in raw_cards:
        t = str(rc.get("title") or "").strip()
        w = str(rc.get("workspace") or "Default Workspace").strip()
        _reg(t, rc.get("semantic_model"), rc.get("id"), f"{w}__{t}" if t else None)
    for sub in ("datasets", "reports", "stakeholders", "opportunities", "staging/opportunities"):
        sdir = vdir / sub
        if sdir.is_dir():
            for md_p in sdir.glob("*.md"):
                _reg(md_p.stem, f"{sub}/{md_p.stem}")
                try:
                    pcard = parse_markdown_card(md_p)
                    fm = pcard.frontmatter if isinstance(pcard.frontmatter, dict) else {}
                    _reg(
                        fm.get("id"),
                        fm.get("fqn"),
                        fm.get("table_name"),
                        fm.get("title"),
                        fm.get("semantic_model"),
                        fm.get("name"),
                    )
                except Exception:
                    pass
    if (vdir / "glossary.md").is_file():
        _reg("glossary")
    if (vdir / "questions").is_dir():
        _reg("open-questions", "day1-90-playbook", "day-1-90-playbook")
    return keys


def _ws_compatible(cid: str, cws: str, eid: str, ews: str) -> bool:
    if eid and cid == eid:
        return True
    if ews == "__unparsed__":
        return False
    return (
        cws == ews
        or cws in {"", "default workspace"}
        or ews in {"", "default workspace"}
    )


def _index_existing_report_cards(
    reports_dir: Path,
) -> tuple[dict[str, tuple[Path, str, str]], dict[Path, tuple[str, str]]]:
    by_key: dict[str, tuple[Path, str, str]] = {}
    by_path: dict[Path, tuple[str, str]] = {}
    if not reports_dir.is_dir():
        return by_key, by_path
    for md_p in sorted(reports_dir.glob("*.md")):
        stem_k = f"stem:{md_p.stem.casefold()}"
        try:
            parsed = parse_markdown_card(md_p)
            card = ReportCard.model_validate(parsed.frontmatter)
            cid = card.id.casefold()
            cws = card.workspace.casefold()
            ctitle = card.title.casefold()
            rec = (md_p, cid, cws)
            by_path[md_p] = (cid, cws)
            by_key[f"id:{cid}"] = rec
            by_key[f"wst:{cws}::{ctitle}"] = rec
            by_key.setdefault(f"title:{ctitle}", rec)
            by_key[stem_k] = rec
        except Exception:
            by_path[md_p] = ("", "__unparsed__")
            by_key.setdefault(stem_k, (md_p, "", "__unparsed__"))
    return by_key, by_path


def sync_powerbi(
    vault_dir: Path,
    config: PoopdeckConfig | None = None,
    input_path: Path | None = None,
    mode: str = "auto",
    rest_client: Any = None,
) -> SyncSummary:
    """Sync Power BI reports, semantic models, and DAX heuristic flags into `vault/reports/*.md`."""
    vdir = Path(vault_dir)
    cfg = load_config() if config is None else config
    pbi_cfg = cfg.powerbi
    redact_secrets = cfg.privacy.redact_secrets

    norm_mode = _normalize_mode(mode)
    in_path = (
        Path(input_path)
        if input_path is not None and str(input_path).strip()
        else None
    )

    warnings: list[str] = []
    errors: list[str] = []
    processed_files: list[str] = []

    default_exports_dir = _rebase_vault_relative_path(
        vdir, cfg, pbi_cfg.local_exports_path, ("inbox", "powerbi")
    )
    tier2_target = in_path if in_path is not None else default_exports_dir

    def _has_local_pbi_exports(p: Path) -> bool:
        if not p.exists():
            return False
        if p.is_file():
            return p.suffix.lower() in {".pbip", ".bim", ".tmdl", ".csv", ".json"}
        for _, dirs, files in os.walk(p):
            dirs[:] = [
                d
                for d in dirs
                if not d.startswith(".")
                and d.lower() not in {"databricks", "teams", "notes", "processed", "archive"}
            ]
            if any(d.lower().endswith((".SemanticModel".lower(), ".dataset", ".report")) for d in dirs):
                return True
            for fn in files:
                fl = fn.lower()
                if fl in _DBX_ONLY_CSV_NAMES or fl.startswith((".", "~$")):
                    continue
                if fl.endswith((".pbip", ".bim", ".tmdl")) or fl.startswith(("info.", "info_")):
                    return True
                if fl.endswith((".json", ".csv")):
                    return True
        return False

    raw_cards: list[dict[str, Any]] = []
    resolved_mode: str

    if norm_mode == "tier1":
        resolved_mode = "tier1"
        if rest_client is not None:
            raw_cards = _crawl_tier1_rest(
                _PowerBIRestAdapter(rest_client), pbi_cfg, redact_secrets, warnings, errors
            )
        else:
            token = os.environ.get("POWERBI_ACCESS_TOKEN", "").strip()
            if not token and pbi_cfg.use_az_cli_token:
                az_tok, az_warn = _acquire_az_cli_token()
                if az_tok:
                    token = az_tok
                elif az_warn:
                    warnings.append(redact_text(az_warn, redact_secrets=redact_secrets))
            if not token:
                errors.append(
                    "Power BI Tier 1 requires `rest_client`, `POWERBI_ACCESS_TOKEN`, or active Azure CLI login."
                )
            else:
                with httpx.Client(
                    headers={"Authorization": f"Bearer {token}"}, timeout=10.0
                ) as http_client:
                    raw_cards = _crawl_tier1_rest(
                        _PowerBIRestAdapter(http_client),
                        pbi_cfg,
                        redact_secrets,
                        warnings,
                        errors,
                    )

    elif norm_mode == "tier2":
        resolved_mode = "tier2"
        if in_path is not None and not in_path.exists():
            errors.append(
                redact_text(
                    f"Power BI Tier 2 input path does not exist: {in_path}",
                    redact_secrets=redact_secrets,
                )
            )
        else:
            raw_cards = _crawl_tier2_local(
                tier2_target,
                warnings,
                errors,
                processed_files,
                redact_secrets=redact_secrets,
                explicit_input=(in_path is not None),
            )
            if not raw_cards and not errors:
                warnings.append(
                    redact_text(
                        f"No Power BI Tier 2 exports found in {tier2_target}",
                        redact_secrets=redact_secrets,
                    )
                )

    else:
        # Auto mode resolution
        if rest_client is not None:
            resolved_mode = "tier1"
            raw_cards = _crawl_tier1_rest(
                _PowerBIRestAdapter(rest_client), pbi_cfg, redact_secrets, warnings, errors
            )
        elif in_path is not None:
            resolved_mode = "tier2"
            if not in_path.exists():
                errors.append(
                    redact_text(
                        f"Power BI input path does not exist: {in_path}",
                        redact_secrets=redact_secrets,
                    )
                )
            else:
                raw_cards = _crawl_tier2_local(
                    tier2_target,
                    warnings,
                    errors,
                    processed_files,
                    redact_secrets=redact_secrets,
                    explicit_input=True,
                )
        elif pbi_cfg.mode in {"tier1", "tier2"}:
            return sync_powerbi(
                vdir, config=cfg, input_path=in_path, mode=pbi_cfg.mode, rest_client=rest_client
            )
        elif os.environ.get("POWERBI_ACCESS_TOKEN", "").strip():
            token = os.environ["POWERBI_ACCESS_TOKEN"].strip()
            tier1_errs: list[str] = []
            with httpx.Client(
                headers={"Authorization": f"Bearer {token}"}, timeout=10.0
            ) as http_client:
                raw_cards = _crawl_tier1_rest(
                    _PowerBIRestAdapter(http_client),
                    pbi_cfg,
                    redact_secrets,
                    warnings,
                    tier1_errs,
                )
            if tier1_errs and _has_local_pbi_exports(tier2_target):
                warnings.extend(tier1_errs)
                resolved_mode = "tier2"
                raw_cards = _crawl_tier2_local(
                    tier2_target,
                    warnings,
                    errors,
                    processed_files,
                    redact_secrets=redact_secrets,
                    explicit_input=False,
                )
            elif tier1_errs and not raw_cards:
                warnings.extend(tier1_errs)
                resolved_mode = "none"
            else:
                resolved_mode = "tier1"
                errors.extend(tier1_errs)
        elif _has_local_pbi_exports(tier2_target):
            resolved_mode = "tier2"
            raw_cards = _crawl_tier2_local(
                tier2_target,
                warnings,
                errors,
                processed_files,
                redact_secrets=redact_secrets,
                explicit_input=False,
            )
        elif pbi_cfg.use_az_cli_token:
            az_tok, az_warn = _acquire_az_cli_token()
            if az_tok:
                tier1_errs = []
                with httpx.Client(
                    headers={"Authorization": f"Bearer {az_tok}"}, timeout=10.0
                ) as http_client:
                    raw_cards = _crawl_tier1_rest(
                        _PowerBIRestAdapter(http_client),
                        pbi_cfg,
                        redact_secrets,
                        warnings,
                        tier1_errs,
                    )
                if tier1_errs and not raw_cards:
                    warnings.extend(tier1_errs)
                    if _has_local_pbi_exports(tier2_target):
                        resolved_mode = "tier2"
                        raw_cards = _crawl_tier2_local(
                            tier2_target,
                            warnings,
                            errors,
                            processed_files,
                            redact_secrets=redact_secrets,
                            explicit_input=False,
                        )
                    else:
                        resolved_mode = "none"
                else:
                    resolved_mode = "tier1"
                    errors.extend(tier1_errs)
            else:
                if az_warn:
                    warnings.append(redact_text(az_warn, redact_secrets=redact_secrets))
                warnings.append("No Power BI REST credentials or Tier 2 local exports found.")
                resolved_mode = "none"
        else:
            warnings.append("No Power BI REST credentials or Tier 2 local exports found.")
            resolved_mode = "none"

    def _scrub_msgs(items: list[str]) -> list[str]:
        return [redact_text(str(x), redact_secrets=redact_secrets) for x in items]

    if not raw_cards:
        return SyncSummary(
            target="powerbi",
            mode_used=resolved_mode,
            cards_written=0,
            written_paths=[],
            processed_files=_scrub_msgs(processed_files),
            warnings=_scrub_msgs(warnings),
            errors=_scrub_msgs(errors),
        )

    buckets: dict[str, list[tuple[tuple[str, str], int, dict[str, Any]]]] = {}
    used_ids: set[str] = set()
    used_wst: set[tuple[str, str]] = set()
    for idx, rc in enumerate(raw_cards):
        raw_t = _coerce_pbi_str(rc.get("title")).strip()
        raw_w = _coerce_pbi_str(rc.get("workspace")).strip()
        raw_sm = _coerce_pbi_str(rc.get("semantic_model")).strip()
        raw_id = _coerce_pbi_str(rc.get("id")).strip()
        raw_src = _coerce_pbi_str(rc.pop("_source_path", "")).strip()
        clean_t = _clean_redacted_identifier(
            _redact_pbi_text(raw_t, redact_secrets=redact_secrets),
            "Power BI Report",
        )
        clean_w = _clean_redacted_identifier(
            _redact_pbi_text(raw_w, redact_secrets=redact_secrets),
            "Default Workspace",
        )
        clean_sm = _clean_redacted_identifier(
            _redact_pbi_text(raw_sm, redact_secrets=redact_secrets),
            clean_t,
        )
        red_id = _redact_pbi_text(raw_id, redact_secrets=redact_secrets) if raw_id else ""
        cand_id = _clean_redacted_identifier(red_id, "") if raw_id else ""
        clean_id = (
            cand_id
            if cand_id and ("[REDACTED:" not in red_id or cand_id.casefold() != clean_w.casefold())
            else f"{clean_w}__{clean_t}"
        )
        src_key = (
            (raw_id or f"{raw_w or 'Default Workspace'}__{raw_t or 'Power BI Report'}").casefold(),
            raw_src.casefold(),
        )
        rc.update(title=clean_t, workspace=clean_w, semantic_model=clean_sm, id=clean_id)
        buckets.setdefault(clean_id.casefold(), []).append((src_key, idx, rc))
        used_ids.add(clean_id.casefold())
        used_wst.add((clean_w.casefold(), clean_t.casefold()))

    slots: list[dict[str, Any]] = [{}] * len(raw_cards)
    for items in buckets.values():
        ordered = sorted(items, key=lambda it: (it[0], it[1]))
        assigned: dict[tuple[str, str], tuple[str, str] | None] = {}
        n = 2
        for sk, _, rc_i in ordered:
            if sk not in assigned:
                if not assigned:
                    assigned[sk] = None
                else:
                    bid = str(rc_i["id"])
                    bt = str(rc_i["title"])
                    w_cf = str(rc_i["workspace"]).casefold()
                    while (
                        f"{bid}__{n}".casefold() in used_ids
                        or (w_cf, f"{bt} ({n})".casefold()) in used_wst
                    ):
                        n += 1
                    assigned[sk] = (f"{bid}__{n}", f"{bt} ({n})")
                    used_ids.add(f"{bid}__{n}".casefold())
                    used_wst.add((w_cf, f"{bt} ({n})".casefold()))
                    n += 1
            if (pair := assigned[sk]) is not None:
                if str(rc_i.get("semantic_model") or "").casefold() == str(rc_i["title"]).casefold():
                    rc_i["semantic_model"] = pair[1]
                rc_i["id"], rc_i["title"] = pair
        for (_, orig_i, _), (_, _, rc_i) in zip(items, ordered):
            slots[orig_i] = rc_i
    raw_cards = slots

    reports_dir = vdir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    by_key, by_path = _index_existing_report_cards(reports_dir)
    known_vault_keys = _build_pbi_known_vault_keys(vdir, raw_cards)
    claimed_output_paths: dict[Path, str] = {}
    written_paths: list[str] = []

    for rc in raw_cards:
        title = (
            _unwrap_unresolved_wikilinks(str(rc["title"]).strip(), known_vault_keys).strip()
            or "Power BI Report"
        )
        workspace = (
            _unwrap_unresolved_wikilinks(str(rc["workspace"]).strip(), known_vault_keys).strip()
            or "Default Workspace"
        )
        sem_model = (
            _unwrap_unresolved_wikilinks(
                str(rc.get("semantic_model") or title).strip(), known_vault_keys
            ).strip()
            or title
        )
        sched_raw = rc.get("refresh_schedule")
        sched = (
            _unwrap_unresolved_wikilinks(
                redact_text(str(sched_raw).strip(), redact_secrets=redact_secrets),
                known_vault_keys,
            )
            if sched_raw is not None and str(sched_raw).strip()
            else None
        )
        high_excel = bool(rc.get("high_excel_export", False))

        measures_raw, gaps_raw = _build_measures_and_gaps(
            rc.get("measures") or [],
            rc.get("bi_to_ds_gaps") or [],
            high_excel_export=high_excel,
            redact_secrets=redact_secrets,
        )
        measures = [
            DaxMeasure(
                name=_unwrap_unresolved_wikilinks(m.name, known_vault_keys).strip() or "Measure",
                table=_unwrap_unresolved_wikilinks(m.table, known_vault_keys),
                expression=_unwrap_unresolved_wikilinks(m.expression, known_vault_keys),
                description=_unwrap_unresolved_wikilinks(m.description, known_vault_keys),
                flagged_heuristics=[
                    uf
                    for f in m.flagged_heuristics
                    if (uf := _unwrap_unresolved_wikilinks(f, known_vault_keys).strip())
                ],
            )
            for m in measures_raw
        ]
        gaps = [_unwrap_unresolved_wikilinks(g, known_vault_keys) for g in gaps_raw]

        clean_rels: list[dict[str, Any]] = []
        for rel in rc.get("relationships") or []:
            if not isinstance(rel, dict):
                continue
            san_rel = _sanitize_pbi_value(rel, redact_secrets, known_vault_keys)
            if isinstance(san_rel, dict):
                clean_rels.append(san_rel)

        card_id = (
            _unwrap_unresolved_wikilinks(
                str(rc.get("id") or f"{workspace}__{title}").strip(),
                known_vault_keys,
            ).strip()
            or f"{workspace}__{title}"
        )
        try:
            card = ReportCard(
                id=card_id,
                title=title,
                workspace=workspace,
                semantic_model=sem_model,
                refresh_schedule=sched,
                refresh_failures_30d=_safe_nonneg_int(rc.get("refresh_failures_30d")),
                upstream_datasets=_clean_upstream_list(
                    rc.get("upstream_datasets") or [], redact_secrets
                ),
                measures=measures,
                relationships=clean_rels,
                bi_to_ds_gaps=gaps,
                high_excel_export=high_excel,
                owners=_clean_owners_list(rc.get("owners") or [], redact_secrets),
                updated_at=date.today(),
            )
        except Exception as exc:
            errors.append(
                redact_text(
                    f"Validation failed for Power BI report {title!r}: {exc}",
                    redact_secrets=redact_secrets,
                )
            )
            continue

        title_slug = _safe_slug(card.title)
        ws_slug = _safe_slug(card.workspace)
        cid_cf = card.id.casefold()
        cws_cf = card.workspace.casefold()
        ctitle_cf = card.title.casefold()

        matched_path: Path | None = None
        direct_rec = by_key.get(f"id:{cid_cf}") or by_key.get(f"wst:{cws_cf}::{ctitle_cf}")
        if direct_rec is not None:
            matched_path = direct_rec[0]
        else:
            for fb_k in (f"title:{ctitle_cf}", f"stem:{title_slug}"):
                fb_rec = by_key.get(fb_k)
                if fb_rec is not None and _ws_compatible(cid_cf, cws_cf, fb_rec[1], fb_rec[2]):
                    matched_path = fb_rec[0]
                    break

        if matched_path is not None and (
            matched_path not in claimed_output_paths
            or claimed_output_paths[matched_path] == cid_cf
        ):
            out_path = matched_path
        else:
            cand = reports_dir / f"{title_slug}.md"
            if (cand in claimed_output_paths and claimed_output_paths[cand] != cid_cf) or (
                cand in by_path and not _ws_compatible(cid_cf, cws_cf, *by_path[cand])
            ):
                cand = reports_dir / f"{ws_slug}__{title_slug}.md"
                if (cand in claimed_output_paths and claimed_output_paths[cand] != cid_cf) or (
                    cand in by_path and not _ws_compatible(cid_cf, cws_cf, *by_path[cand])
                ):
                    cand = reports_dir / f"{ws_slug}__{title_slug}__{_safe_slug(card.id)}.md"
            if _is_windows_reserved_name(cand.name):
                cand = reports_dir / f"{cand.stem}-card.md"
            out_path = cand

        claimed_output_paths[out_path] = cid_cf
        by_path[out_path] = (cid_cf, cws_cf)
        upsert_markdown_card(out_path, card, render_report_body(card))
        if out_path.as_posix() not in written_paths:
            written_paths.append(out_path.as_posix())

    return SyncSummary(
        target="powerbi",
        mode_used=resolved_mode,
        cards_written=len(written_paths),
        written_paths=written_paths,
        processed_files=_scrub_msgs(processed_files),
        warnings=_scrub_msgs(warnings),
        errors=_scrub_msgs(errors),
    )
