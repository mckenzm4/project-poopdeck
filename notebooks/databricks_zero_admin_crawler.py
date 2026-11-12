# Databricks notebook source
# MAGIC %md
# MAGIC # Project Poopdeck — Zero-Admin Databricks Discovery Crawler (Tier 2)
# MAGIC
# MAGIC Copy-paste or import this standalone Python notebook into any Databricks workspace
# MAGIC when local PAT / SQL Warehouse access is unavailable.
# MAGIC
# MAGIC - **Privacy Gate (`safe_aggregates` by default)**: `INCLUDE_SAMPLE_ROWS = False` emits
# MAGIC   schema metadata, lineage, and safe statistical aggregates (`row_count`, non-PII
# MAGIC   freshness timestamps, `null_pct`, `distinct_count`) with **zero raw sample rows**.
# MAGIC - **Automatic Metastore Fallback**: Queries Unity Catalog `system.information_schema`
# MAGIC   and `system.access.table_lineage` first, and automatically falls back to Hive
# MAGIC   Metastore (`SHOW DATABASES`, `SHOW TABLES`, `DESCRIBE EXTENDED`) when `system.*`
# MAGIC   is restricted or disabled.
# MAGIC - **Export**: Writes a redacted JSON bundle ready to drop into `vault/inbox/databricks/`
# MAGIC   for `poopdeck sync databricks --mode tier2`.

# COMMAND ----------

from __future__ import annotations

import builtins
import copy
import fnmatch
import json
import math
import re
from collections.abc import Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

# COMMAND ----------

# Notebook-level configuration switches (safe defaults)
INCLUDE_SAMPLE_ROWS: bool = False
MAX_SAMPLE_ROWS: int = 5
CATALOGS_INCLUDE: list[str] = ["*"]
CATALOGS_EXCLUDE: list[str] = ["samples", "system", "hive_metastore_scratch"]
LINEAGE_LOOKBACK_DAYS: int = 30
MAX_TABLES: int = 500
OUTPUT_BUNDLE_PATH: str = "/tmp/poopdeck_databricks_discovery_bundle.json"

# COMMAND ----------

_PII_COLUMN_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pat, re.IGNORECASE)
    for pat in (
        r"(?:^|[_.\s-])e[-_]?mails?(?:$|[_.\s-])",
        r"(?:^|[_.\s-])(?:ssn|social[_.\s-]?security(?:[_.\s-]?(?:number|num|no))?|tax[_.\s-]?id|national[_.\s-]?id|passport(?:[_.\s-]?(?:number|num|no))?|driver[_.\s-]?licen[sc]e)(?:$|[_.\s-])",
        r"(?:^|[_.\s-])(?:phone|phone[_.\s-]?(?:number|num|no|ext)|telephone|mobile|mobile[_.\s-]?(?:number|num|no|phone)|cell[_.\s-]?phone|fax|fax[_.\s-]?(?:number|num|no))(?:$|[_.\s-])",
        r"(?:^|[_.\s-])(?:dob|birth[_.\s-]?date|date[_.\s-]?of[_.\s-]?birth|birth[_.\s-]?day)(?:$|[_.\s-])",
        r"(?:^|[_.\s-])(?:salary|salaries|compensation|wage|wages|pay[_.\s-]?rate|bonus)(?:$|[_.\s-])",
        r"(?:^|[_.\s-])(?:address|addresses|street|street[_.\s-]?(?:line|num|number|1|2)?|apt[_.\s-]?no|zip|zip[_.\s-]?code|postal|postal[_.\s-]?code|post[_.\s-]?code)(?:$|[_.\s-])",
        r"(?:^|[_.\s-])(?:(?:first|last|middle|full|given|family|maiden|sur|nick|preferred|legal|user|login|customer|client|buyer|shopper|consumer|employees?|emp|staff|worker|personnel|manager|supervisor|person|personal|individual|contact|patient|member|subscriber|applicant|candidate|beneficiary|dependent|spouse|insured|policyholder|account[_.\s-]?holder|card[_.\s-]?holder|owner|author|speaker|attendee|recipient|sender|rep|sales[_.\s-]?rep|agent|driver|passenger)[_.\s-]?names?|name[_.\s-]?(?:first|last|middle|full))(?:$|[_.\s-])",
        r"^name$",
    )
)

_COMMENT_EXPLICIT_PII_RE = re.compile(
    r"(?i)(?<!non-)(?<!non_)(?<!non\s)(?<!not\s)\b(?:pii|phi|personally[\s_-]+identifiable)\b"
)

_PEM_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"-----BEGIN (?:[A-Z0-9 _]|-(?!-)){0,64}PRIVATE KEY-----"
            r"(?:[^-]|-(?!----(?:BEGIN|END) ))*"
            r"-----END (?:[A-Z0-9 _]|-(?!-)){0,64}PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[REDACTED:SECRET]",
    ),
    (
        re.compile(r"-----BEGIN (?:[A-Z0-9 _]|-(?!-)){0,64}PRIVATE KEY-----[^\r\n]*"),
        "[REDACTED:SECRET]",
    ),
)

_BEARER_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])Bearer[ \t]++(?!\[REDACTED:)[A-Za-z0-9_\-\.~+/]{16,}=*+"
)

_KV_KEY_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(pwd|password|passphrase|accountkey|sharedaccesskey|"
    r"sharedaccesssignature|client_secret|clientsecret|api_key|apikey|access_key|"
    r"secret_key|aws_secret_access_key|auth_accesstoken|accesstoken|access_token|"
    r"refresh_token|personal_access_token|secret)[ \t]*=[ \t]*"
)
_REDACTED_AT_RE = re.compile(r"\[REDACTED:[A-Z_]+\]")
_APOSTROPHE_PRE_RE = re.compile(r"(?:^|[\s,;(\[{])[A-Za-z]{1,15}\Z")
_APOSTROPHE_POST_RE = re.compile(r"(?i)(?:s|t|m|d|ll|ve|re)(?=$|[\s,.;:!?)}\]])")
_QUOTE_SCAN_RE = re.compile(r"\\[^\r\n]|[\x22\x27`\r\n]")
_CLOSED_DQ_RE = re.compile(r"\x22(?:\\[^\r\n]|\x22\x22|[^\x22\\\r\n])*\x22")
_CLOSED_SQ_RE = re.compile(r"\x27(?:\\[^\r\n]|\x27\x27|[^\x27\\\r\n])*\x27")
_BRACE_VAL_RE = re.compile(r"\{(?:\}\}|[^}{\r\n])*\}")
_UNCLOSED_Q_RE = re.compile(
    r"[\x22\x27](?!\[REDACTED:[A-Z_]+\])(?:\\[^\r\n]|[A-Za-z0-9_])"
    r"(?:\\[^\r\n]|[^\s;|&`\x22\x27\\,)\]}\r\n])*"
)
_UNQUOTED_VAL_RE = re.compile(r"[^\s;|&`\x22\x27\\]+")
_INNER_DQ_IN_SQ_RE = re.compile(
    r"\x22(?:\\[^\r\n]|\x22\x22|\x27\x27|[^\x22\x27\\\r\n])*\x22"
)
_INNER_SQ_IN_DQ_RE = re.compile(
    r"\x27(?:\\[^\r\n]|\x27\x27|\x22\x22|[^\x22\x27\\\r\n])*\x27"
)
_INNER_BRACE_DQ_RE = re.compile(r"\{(?:\}\}|\\[^\r\n]|[^}{\x22\\\r\n])*\}")
_INNER_BRACE_SQ_RE = re.compile(r"\{(?:\}\}|\\[^\r\n]|\x27\x27|[^}{\x27\\\r\n])*\}")
_INNER_UNQUOTED_DQ_RE = re.compile(r"(?:\\[^\r\n]|[^\s;|&`\x22\x27\\])+")
_INNER_UNQUOTED_SQ_RE = re.compile(r"(?:\\[^\r\n]|\x27\x27|[^\s;|&`\x22\x27\\])+")
_DOUBLED_DQ_INNER_RE = re.compile(r"\x22\x22[^\x22\r\n]+\x22\x22(?=\x22|[;\s,])")
_DOUBLED_SQ_INNER_RE = re.compile(r"\x27\x27[^\x27\r\n]+\x27\x27(?=\x27|[;\s,])")

_URL_CRED_RE = re.compile(
    r"(?<![A-Za-z0-9+.-])([A-Za-z][A-Za-z0-9+.-]{0,31}://[^/\s:;?#,\x22\x27<>`()\[\]{}|]*:)"
    r"(?!\[REDACTED:[A-Z_]+\]@)(?!\d{1,5}(?:[,)|\]}>]|&[A-Za-z0-9_]+=))"
    r"([^/\s;?#,\x22\x27<>`()\[\]{}|]+)(@)(?=[A-Za-z0-9.\-]|\[[0-9A-Fa-f:])"
)

_SECRET_RULES_POST: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)([?&]sig=)(?!\[REDACTED:[A-Z_]+\])([^&\s;`\x22\x27\\,()\[\]{}<>]+)"
        ),
        r"\1[REDACTED:SECRET]",
    ),
    (
        re.compile(
            r"(?<![A-Za-z0-9])(?:dapi[0-9a-fA-F]{16,64}+|github_pat_[A-Za-z0-9_]{20,255}+"
            r"|(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,255}+|sk-ant-[A-Za-z0-9_-]{12,255}+"
            r"|sk-(?:proj-|live-|test-)?[A-Za-z0-9_-]{16,255}+|(?:AKIA|ASIA)[0-9A-Z]{16}"
            r"|xox[baprs]-[A-Za-z0-9-]{10,255}+|eyJ[A-Za-z0-9_-]{5,256}+\.[A-Za-z0-9_-]{5,4096}+\.[A-Za-z0-9_-]{5,1024}+)(?![A-Za-z0-9])"
        ),
        "[REDACTED:SECRET]",
    ),
)

_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}+@(?:[A-Za-z0-9-]{1,63}+\.){1,10}[A-Za-z]{2,24}(?![A-Za-z0-9-])"
)
_SSN_RE = re.compile(r"(?<![A-Za-z0-9])\d{3}-\d{2}-\d{4}(?![A-Za-z0-9])")
_PHONE_US_RE = re.compile(
    r"(?:\+1[-.\s]?(?:\(\d{3}\)[-.\s]?|\d{3}[-.\s])|\(\d{3}\)[-.\s]?|(?<![A-Za-z0-9])(?<!\d[-.])(?:1[-.\s]?)?(?:\(\d{3}\)[-.\s]?|\d{3}[-.\s]))"
    r"\d{3}[-.\s]\d{4}(?:\s*(?:ext\.?|x)\s*\d{1,5})?(?![A-Za-z0-9]|[-.]\d)",
    re.IGNORECASE,
)
_PHONE_INTL_RE = re.compile(
    r"(?:\+1\d{10}|\+\d{1,3}[\s-]\(?\d{1,4}\)?[\s-]\d{3,4}(?:[\s-]\d{2,4}){1,2})(?![A-Za-z0-9]|[-.]\d)"
)

_HIVE_META_KEYS: frozenset[str] = frozenset(
    {
        "catalog",
        "database",
        "table",
        "owner",
        "created time",
        "last access",
        "created by",
        "type",
        "provider",
        "table properties",
        "location",
        "serde library",
        "inputformat",
        "outputformat",
        "storage properties",
        "partition provider",
        "statistics",
        "comment",
        "view text",
        "view original text",
    }
)


def _normalize_col_identifier(raw: str) -> str:
    s = raw.strip()
    if "[" in s and s.endswith("]"):
        s = s[s.rfind("[") + 1 : -1]
    elif "." in s:
        s = s.rsplit(".", 1)[-1]
    s = s.strip("`\"' ")
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    s = re.sub(r"([A-Z])([A-Z][a-z])", r"\1_\2", s)
    return re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_").lower()


def is_pii_column_name(col_name: str, comment: str | None = None) -> bool:
    """Return True if column name or comment indicates PII / sensitive personal data."""
    norm = _normalize_col_identifier(str(col_name or ""))
    if norm and any(p.search(norm) for p in _PII_COLUMN_PATTERNS):
        return True
    if comment:
        if _COMMENT_EXPLICIT_PII_RE.search(comment):
            return True
        norm_comment = re.sub(r"[^a-zA-Z0-9]+", "_", comment).strip("_").lower()
        for pat in _PII_COLUMN_PATTERNS[:-1]:  # exclude standalone ^name$ on prose
            if pat.search(norm_comment):
                return True
    return False


def _replace_intl_phone(match: re.Match[str]) -> str:
    raw = match.group(0)
    digits = sum(ch.isdigit() for ch in raw)
    return "[REDACTED:PHONE]" if 10 <= digits <= 15 else raw


def _scan_escaped_inner_quote(text: str, start: int, oq: str) -> int:
    n, i = len(text), start + 2
    while i < n and text[i] not in "\r\n":
        if text[i] == oq:
            return i
        if text[i] == "\\":
            j = i
            while j < n and text[j] == "\\":
                j += 1
            bs = j - i
            if j < n and text[j] == oq:
                if bs % 2 == 0:
                    return j
                if bs % 4 == 1:
                    return j + 1
                i = j + 1
            else:
                i = j + 1 if (bs % 2 == 1 and j < n and text[j] not in "\r\n") else j
        else:
            i += 1
    return i


def _redact_kv_secrets(text: str) -> str:
    if "=" not in text or (m := _KV_KEY_RE.search(text)) is None:
        return text
    n = len(text)
    out: list[str] = []
    cur = spos = flr = 0
    oq: str | None = None
    bt = dq_f = sq_f = iq_f = False
    while m is not None:
        ks, vs, key = m.start(), m.end(), m.group(1)
        for tk in _QUOTE_SCAN_RE.finditer(text, spos, vs):
            s, p = tk.group(0), tk.start()
            if s in ("\r", "\n"):
                oq, bt, dq_f, sq_f, iq_f = None, False, False, False, False
            elif s == "`":
                if oq is None:
                    bt, oq = not bt, None
                elif bt:
                    bt, oq, iq_f = False, None, False
            elif s == '"':
                if oq is None:
                    oq, iq_f = '"', False
                elif oq == '"':
                    oq, iq_f = None, False
            elif s == "'":
                ap = (
                    _APOSTROPHE_PRE_RE.search(text, max(flr, p - 16), p) is not None
                    and _APOSTROPHE_POST_RE.match(text, p + 1) is not None
                )
                if oq is None and not ap:
                    oq, iq_f = "'", False
                elif oq == "'":
                    oq, iq_f = None, False
        spos = vs
        if vs >= n or text[vs] in "\r\n":
            out.append(text[cur:vs])
            cur = spos = vs
        elif (mr := _REDACTED_AT_RE.match(text, vs)) is not None:
            out.append(text[cur : mr.end()])
            cur = spos = flr = mr.end()
        elif (
            oq is not None
            and text[vs] == oq
            and (_DOUBLED_DQ_INNER_RE if oq == '"' else _DOUBLED_SQ_INNER_RE).match(
                text, vs
            )
            is None
        ):
            out.append(text[cur : vs + 1])
            oq, iq_f = None, False
            cur = spos = vs + 1
        else:
            ve = None
            if oq is None:
                ch = text[vs]
                if ch == '"':
                    mv = None if dq_f else _CLOSED_DQ_RE.match(text, vs)
                    dq_f = dq_f or mv is None
                    mv = mv or _UNCLOSED_Q_RE.match(text, vs)
                elif ch == "'":
                    mv = None if sq_f else _CLOSED_SQ_RE.match(text, vs)
                    sq_f = sq_f or mv is None
                    mv = mv or _UNCLOSED_Q_RE.match(text, vs)
                elif ch == "{":
                    mv = _BRACE_VAL_RE.match(text, vs) or _UNQUOTED_VAL_RE.match(
                        text, vs
                    )
                else:
                    mv = _UNQUOTED_VAL_RE.match(text, vs)
                ve = mv.end() if mv else None
            elif text[vs : vs + 2] == "\\" + oq:
                ei = _scan_escaped_inner_quote(text, vs, oq)
                ve = ei if ei > vs + 2 else None
            elif text[vs] == oq:
                mv = (
                    _DOUBLED_DQ_INNER_RE if oq == '"' else _DOUBLED_SQ_INNER_RE
                ).match(text, vs)
                ve = mv.end() if mv else None
            elif text[vs] in ('"', "'"):
                mv = (
                    None
                    if iq_f
                    else (
                        _INNER_SQ_IN_DQ_RE if oq == '"' else _INNER_DQ_IN_SQ_RE
                    ).match(text, vs)
                )
                iq_f = iq_f or mv is None
                mv = mv or _UNCLOSED_Q_RE.match(text, vs)
                ve = mv.end() if mv else None
            elif text[vs] == "{":
                mv = (
                    _INNER_BRACE_DQ_RE if oq == '"' else _INNER_BRACE_SQ_RE
                ).match(text, vs) or (
                    _INNER_UNQUOTED_DQ_RE if oq == '"' else _INNER_UNQUOTED_SQ_RE
                ).match(text, vs)
                ve = mv.end() if mv else None
            else:
                mv = (
                    _INNER_UNQUOTED_DQ_RE if oq == '"' else _INNER_UNQUOTED_SQ_RE
                ).match(text, vs)
                ve = mv.end() if mv else None
            if ve is not None:
                out.append(text[cur:ks])
                out.append(f"{key}=[REDACTED:SECRET]")
                cur = spos = flr = ve
            else:
                out.append(text[cur:vs])
                cur = spos = vs
        m = _KV_KEY_RE.search(text, cur)
    out.append(text[cur:])
    return "".join(out)


def scrub_text_secrets_and_pii(text: str) -> str:
    """Scrub secret tokens and inline email/SSN/phone PII from text in O(n) time."""
    if not text or not isinstance(text, str):
        return ""
    out = text
    if "PRIVATE KEY-----" in out:
        for pat, repl in _PEM_RULES:
            out = pat.sub(repl, out)
    if "bearer" in out.lower():
        out = _BEARER_RE.sub("[REDACTED:SECRET]", out)
    out = _redact_kv_secrets(out)
    if "://" in out:
        out = _URL_CRED_RE.sub(r"\1[REDACTED:SECRET]\3", out)
    for pat, repl in _SECRET_RULES_POST:
        out = pat.sub(repl, out)
    if "@" in out:
        out = _EMAIL_RE.sub("[REDACTED:EMAIL]", out)
    if "-" in out:
        out = _SSN_RE.sub("[REDACTED:SSN]", out)
    if any(c in out for c in "-.() "):
        out = _PHONE_US_RE.sub("[REDACTED:PHONE]", out)
    if "+" in out:
        out = _PHONE_INTL_RE.sub(_replace_intl_phone, out)
    return out


def _finite_float(val: Any) -> float | None:
    while isinstance(val, Enum):
        val = val.value
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float, Decimal)):
        try:
            f = float(val)
        except (OverflowError, ValueError):
            return None
        return f if math.isfinite(f) else None
    if isinstance(val, str):
        s = val.strip().replace(",", "")
        if not s:
            return None
        try:
            f = float(s)
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def _finite_nonneg_int(val: Any) -> int | None:
    while isinstance(val, Enum):
        val = val.value
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val if val >= 0 else None
    if isinstance(val, Decimal):
        if not val.is_finite():
            return None
        _, digits, exp = val.as_tuple()
        if isinstance(exp, int) and exp >= 0 and len(digits) + exp <= 38:
            iv = int(val)
            return iv if iv >= 0 else None
    f = _finite_float(val)
    if f is None:
        return None
    iv = int(round(f))
    return iv if iv >= 0 else None


def _sanitize_json_value(val: Any) -> Any:
    if val is None or isinstance(val, (bool, int)):
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
    if isinstance(val, Enum):
        return _sanitize_json_value(val.value)
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    if isinstance(val, Path):
        return scrub_text_secrets_and_pii(val.as_posix())
    if isinstance(val, (bytes, bytearray)):
        return scrub_text_secrets_and_pii(val.decode("utf-8", errors="replace"))
    if isinstance(val, str):
        return scrub_text_secrets_and_pii(val)
    if isinstance(val, (list, tuple, set, frozenset)):
        return [_sanitize_json_value(v) for v in val]
    if isinstance(val, dict):
        out_d: dict[str, Any] = {}
        for k, v in val.items():
            sk = _sanitize_json_value(k)
            ks = sk if isinstance(sk, str) else scrub_text_secrets_and_pii(str(sk))
            out_d[ks] = _sanitize_json_value(v)
        return out_d
    return scrub_text_secrets_and_pii(str(val))


def mask_bundle_pii(
    bundle: dict[str, Any],
    *,
    include_sample_rows: bool = False,
    max_sample_rows: int = 5,
) -> dict[str, Any]:
    """Deep-copy and scrub a discovery bundle according to Poopdeck privacy rules."""
    out = copy.deepcopy(bundle)
    keep_samples = bool(include_sample_rows) and max_sample_rows > 0

    table_pii_cols: dict[str, set[str]] = {}

    def _scrub_col_dict(col: dict[str, Any], fqn_key: str) -> dict[str, Any]:
        clean_col = _sanitize_json_value(col)
        c_name = str(clean_col.get("name") or clean_col.get("column_name") or "").strip()
        raw_comment = str(col.get("comment") or "")
        flagged = bool(col.get("is_pii", False)) or is_pii_column_name(
            c_name, comment=raw_comment
        )
        clean_col["is_pii"] = flagged
        if flagged:
            if fqn_key:
                s = table_pii_cols.setdefault(fqn_key.casefold(), set())
                s.add(c_name.casefold())
                s.add(_normalize_col_identifier(c_name))
            for k in ("min_val", "max_val", "min_value", "max_value"):
                if k in clean_col and clean_col[k] is not None:
                    clean_col[k] = "[REDACTED:PII]"
            if "sample_values" in clean_col and clean_col["sample_values"] is not None:
                sv = clean_col["sample_values"]
                clean_col["sample_values"] = (
                    ["[REDACTED:PII]" for _ in sv]
                    if isinstance(sv, list)
                    else ["[REDACTED:PII]"]
                )
        return clean_col

    raw_tables = out.get("tables")
    _nb_red_re = re.compile(r"\[REDACTED:[A-Z_]+\]", re.IGNORECASE)
    fqn_remap: dict[str, str] = {}
    used_clean_fqns: set[str] = set()

    def _clean_nb_seg(raw_seg: Any, fallback: str) -> tuple[str, bool]:
        red = scrub_text_secrets_and_pii(str(raw_seg or "").strip())
        if "[REDACTED:" not in red:
            return (red.strip() or fallback), False
        cleaned = _nb_red_re.sub(" ", red)
        collapsed = " ".join(cleaned.split()).strip(" \t\r\n\x0b\x0c_.-:;|/\\,")
        seg = re.sub(r"([_-])[\s_-]+", r"\1", collapsed)
        return (seg or fallback), True

    def _resolve_nb_fqn(
        raw_fqn: str, raw_cat: Any = None, raw_sch: Any = None, raw_tbl: Any = None
    ) -> tuple[str, str, str, str]:
        f_str = str(raw_fqn or "").strip()
        c_str = str(raw_cat or "").strip()
        s_str = str(raw_sch or "").strip()
        t_str = str(raw_tbl or "").strip()
        if f_str and (not c_str or not s_str or not t_str):
            parts = [p.strip("`\"' ") for p in f_str.split(".") if p.strip("`\"' ")]
            if len(parts) >= 4 and "@" in f_str:
                if "@" in parts[-2] or "@" in parts[-1]:
                    c_str, s_str, t_str = c_str or parts[0], s_str or parts[1], t_str or ".".join(parts[2:])
                elif "@" in parts[0] and "@" not in parts[-2] and "@" not in parts[-1]:
                    c_str, s_str, t_str = c_str or ".".join(parts[:-2]), s_str or parts[-2], t_str or parts[-1]
                elif len(parts) == 4 and "@" in parts[1]:
                    c_str, s_str, t_str = c_str or parts[0], s_str or f"{parts[1]}.{parts[2]}", t_str or parts[3]
                else:
                    c_str, s_str, t_str = c_str or parts[0], s_str or parts[1], t_str or ".".join(parts[2:])
            elif len(parts) >= 3:
                c_str, s_str, t_str = c_str or parts[0], s_str or parts[1], t_str or ".".join(parts[2:])
            elif len(parts) == 2:
                c_str, s_str, t_str = c_str or "hive_metastore", s_str or parts[0], t_str or parts[1]
            elif len(parts) == 1:
                c_str, s_str, t_str = c_str or "hive_metastore", s_str or "default", t_str or parts[0]
        lookup_key = (f_str or f"{c_str}.{s_str}.{t_str}").casefold()
        if lookup_key in fqn_remap:
            mapped = fqn_remap[lookup_key]
            mp = mapped.split(".", 2)
            return mapped, mp[0], mp[1] if len(mp) > 1 else "default", mp[2] if len(mp) > 2 else mapped
        bc, c_red = _clean_nb_seg(c_str, "catalog")
        bs, s_red = _clean_nb_seg(s_str, "schema")
        bt, t_red = _clean_nb_seg(t_str, "dataset")
        cc, cs, ct = bc, bs, bt
        cand = f"{cc}.{cs}.{ct}"
        if cand.casefold() in used_clean_fqns:
            n = 2
            while True:
                if t_red or (not s_red and not c_red):
                    cc, cs, ct = bc, bs, f"{bt}_{n}"
                elif s_red:
                    cc, cs, ct = bc, f"{bs}_{n}", bt
                else:
                    cc, cs, ct = f"{bc}_{n}", bs, bt
                cand = f"{cc}.{cs}.{ct}"
                if cand.casefold() not in used_clean_fqns:
                    break
                n += 1
        used_clean_fqns.add(cand.casefold())
        if lookup_key:
            fqn_remap[lookup_key] = cand
        return cand, cc, cs, ct

    def _remap_fqn_str(val: Any) -> str:
        raw_s = str(val or "").strip()
        if not raw_s:
            return ""
        if raw_s.casefold() in fqn_remap:
            return fqn_remap[raw_s.casefold()]
        cand, _, _, _ = _resolve_nb_fqn(raw_s)
        return cand

    if isinstance(raw_tables, list):
        clean_tables: list[dict[str, Any]] = []
        for tbl in raw_tables:
            if not isinstance(tbl, dict):
                continue
            raw_cols = tbl.get("columns") if isinstance(tbl.get("columns"), list) else []
            raw_rows = (
                tbl.get("sample_rows")
                if isinstance(tbl.get("sample_rows"), list)
                else []
            )
            fqn = str(tbl.get("fqn") or "").strip()
            clean_fqn, clean_cat, clean_sch, clean_tname = _resolve_nb_fqn(
                fqn,
                tbl.get("catalog"),
                tbl.get("schema_name") or tbl.get("schema"),
                tbl.get("table_name"),
            )
            clean_tbl = _sanitize_json_value(
                {k: v for k, v in tbl.items() if k not in ("columns", "sample_rows")}
            )
            clean_tbl["fqn"] = clean_fqn
            if "catalog" in clean_tbl:
                clean_tbl["catalog"] = clean_cat
            if "schema_name" in clean_tbl:
                clean_tbl["schema_name"] = clean_sch
            if "table_name" in clean_tbl:
                clean_tbl["table_name"] = clean_tname
            for ref_k in ("upstream_refs", "downstream_refs"):
                if isinstance(tbl.get(ref_k), list):
                    clean_tbl[ref_k] = [_remap_fqn_str(x) for x in tbl[ref_k] if str(x or "").strip()]
            clean_tbl["columns"] = [
                _scrub_col_dict(c, fqn or clean_fqn) for c in raw_cols if isinstance(c, dict)
            ]
            pii_set = table_pii_cols.get((fqn or clean_fqn).casefold(), set())
            if not keep_samples:
                clean_tbl["sample_rows"] = []
            else:
                scrubbed_rows: list[dict[str, Any]] = []
                for row in raw_rows[:max_sample_rows]:
                    if not isinstance(row, dict):
                        continue
                    s_row: dict[str, Any] = {}
                    for rk, rv in row.items():
                        ks = str(rk)
                        if (
                            ks.casefold() in pii_set
                            or _normalize_col_identifier(ks) in pii_set
                            or is_pii_column_name(ks)
                        ):
                            s_row[ks] = "[REDACTED:PII]"
                        else:
                            s_row[ks] = _sanitize_json_value(rv)
                    scrubbed_rows.append(s_row)
                clean_tbl["sample_rows"] = scrubbed_rows
            clean_tables.append(clean_tbl)
        out["tables"] = clean_tables

    if isinstance(out.get("columns"), list):
        remapped_cols: list[dict[str, Any]] = []
        for c in out["columns"]:
            if not isinstance(c, dict):
                continue
            raw_cfqn = str(c.get("fqn") or "")
            sc = _scrub_col_dict(c, raw_cfqn)
            if raw_cfqn:
                sc["fqn"] = _remap_fqn_str(raw_cfqn)
            remapped_cols.append(sc)
        out["columns"] = remapped_cols

    for section in ("lineage", "tags", "workflows", "query_stats", "profiles", "warnings"):
        if section in out:
            raw_sec = out[section]
            san_sec = _sanitize_json_value(raw_sec)
            if isinstance(raw_sec, list) and isinstance(san_sec, list):
                for orig_item, san_item in zip(raw_sec, san_sec):
                    if isinstance(orig_item, dict) and isinstance(san_item, dict):
                        for fk in (
                            "fqn",
                            "source_table_full_name",
                            "target_table_full_name",
                            "upstream_fqn",
                            "downstream_fqn",
                        ):
                            if fk in orig_item and orig_item[fk]:
                                san_item[fk] = _remap_fqn_str(orig_item[fk])
                        if isinstance(orig_item.get("tables"), list):
                            san_item["tables"] = [
                                _remap_fqn_str(t) for t in orig_item["tables"] if t
                            ]
            elif isinstance(raw_sec, dict) and isinstance(san_sec, dict) and section in {
                "tags",
                "query_stats",
                "profiles",
            }:
                san_sec = {
                    _remap_fqn_str(ok): _sanitize_json_value(ov) for ok, ov in raw_sec.items()
                }
            out[section] = san_sec

    if "sample_rows" in out:
        if not keep_samples or not isinstance(out["sample_rows"], dict):
            out["sample_rows"] = {}
        else:
            top_samples: dict[str, list[dict[str, Any]]] = {}
            for fqn_k, rows_v in out["sample_rows"].items():
                if not isinstance(rows_v, list):
                    continue
                raw_k_str = str(fqn_k)
                clean_k_str = _remap_fqn_str(raw_k_str)
                pii_set = (
                    table_pii_cols.get(raw_k_str.casefold(), set())
                    | table_pii_cols.get(clean_k_str.casefold(), set())
                )
                s_list: list[dict[str, Any]] = []
                for row in rows_v[:max_sample_rows]:
                    if not isinstance(row, dict):
                        continue
                    s_row = {}
                    for rk, rv in row.items():
                        ks = str(rk)
                        if (
                            ks.casefold() in pii_set
                            or _normalize_col_identifier(ks) in pii_set
                            or is_pii_column_name(ks)
                        ):
                            s_row[ks] = "[REDACTED:PII]"
                        else:
                            s_row[ks] = _sanitize_json_value(rv)
                    s_list.append(s_row)
                top_samples[clean_k_str] = s_list
            out["sample_rows"] = top_samples

    return out


# COMMAND ----------


def _get_notebook_global(name: str) -> Any:
    if name in globals():
        return globals()[name]
    return getattr(builtins, name, None)


def _is_catalog_selected(
    catalog: str,
    include_patterns: Sequence[str],
    exclude_patterns: Sequence[str],
) -> bool:
    cat_l = catalog.strip().lower()
    if not cat_l:
        return False
    inc = [p.strip().lower() for p in include_patterns if p and p.strip()]
    exc = [p.strip().lower() for p in exclude_patterns if p and p.strip()]
    if not inc or not any(fnmatch.fnmatchcase(cat_l, pat) for pat in inc):
        return False
    return not any(fnmatch.fnmatchcase(cat_l, pat) for pat in exc)


def _quote_ident(ident: str) -> str:
    s = ident.strip()
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s):
        return s
    return "`" + s.replace("`", "``") + "`"


def _quote_fqn(catalog: str, schema: str, table: str, *, two_part: bool = False) -> str:
    if two_part:
        return f"{_quote_ident(schema)}.{_quote_ident(table)}"
    return f"{_quote_ident(catalog)}.{_quote_ident(schema)}.{_quote_ident(table)}"


def _run_spark_sql(
    spark_obj: Any,
    sql: str,
    fallback_cols: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    df_or_rows = spark_obj.sql(sql)
    raw_rows = df_or_rows.collect() if hasattr(df_or_rows, "collect") else df_or_rows
    if not raw_rows:
        return []
    out: list[dict[str, Any]] = []
    cols = list(fallback_cols or ())
    if hasattr(df_or_rows, "columns") and isinstance(df_or_rows.columns, (list, tuple)):
        cols = [str(c) for c in df_or_rows.columns]
    for row in raw_rows:
        if isinstance(row, dict):
            out.append({str(k): v for k, v in row.items()})
        elif hasattr(row, "asDict") and callable(row.asDict):
            out.append({str(k): v for k, v in row.asDict(recursive=True).items()})
        elif hasattr(row, "_asdict") and callable(row._asdict):
            out.append({str(k): v for k, v in row._asdict().items()})
        elif isinstance(row, (list, tuple)):
            keys = cols if cols else [f"col_{i}" for i in range(len(row))]
            out.append(
                {
                    (keys[i] if i < len(keys) else f"col_{i}"): row[i]
                    for i in range(len(row))
                }
            )
        else:
            key = cols[0] if cols else "value"
            out.append({key: row})
    return out


def _row_ci_get(row: dict[str, Any], *candidates: str) -> Any:
    lower_map = {k.lower(): v for k, v in row.items()}
    for c in candidates:
        if c in row and row[c] is not None:
            return row[c]
        cl = c.lower()
        if cl in lower_map and lower_map[cl] is not None:
            return lower_map[cl]
    return None


def _parse_iso_dt(val: Any) -> datetime | None:
    if isinstance(val, datetime):
        return val if val.tzinfo is not None else val.replace(tzinfo=timezone.utc)
    if isinstance(val, date):
        return datetime(val.year, val.month, val.day, tzinfo=timezone.utc)
    if isinstance(val, str) and val.strip():
        s = val.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _parse_describe_extended(
    rows: list[dict[str, Any]],
    default_db: str,
    default_table: str,
) -> dict[str, Any]:
    cols: list[dict[str, Any]] = []
    seen_cols: set[str] = set()
    partition_cols: list[str] = []
    meta: dict[str, str] = {}
    state = "columns"

    for row in rows:
        raw_cn = _row_ci_get(row, "col_name", "column_name", "name", "col_0")
        raw_dt = _row_ci_get(row, "data_type", "type", "col_1")
        raw_cm = _row_ci_get(row, "comment", "col_2")
        cn = str(raw_cn).strip() if raw_cn is not None else ""
        dt = str(raw_dt).strip() if raw_dt is not None else ""
        cm = str(raw_cm).strip() if raw_cm is not None else ""

        cn_low = cn.lower()
        if not cn:
            if state == "columns":
                state = "after_blank"
            elif state == "partition":
                state = "after_blank"
            continue

        if cn_low.startswith("# partition information") or cn_low.startswith(
            "# clustering information"
        ):
            state = "partition"
            continue
        if cn_low.startswith("# detailed table information"):
            state = "metadata"
            continue
        if cn.startswith("#"):
            continue

        if state in ("after_blank", "metadata") and cn_low in _HIVE_META_KEYS:
            state = "metadata"
            meta[cn_low] = dt
            continue

        if state == "metadata":
            meta[cn_low] = dt
            continue

        if state == "partition":
            partition_cols.append(cn)
            if cn_low not in seen_cols:
                seen_cols.add(cn_low)
                cols.append(
                    {
                        "name": cn,
                        "data_type": dt or "STRING",
                        "nullable": True,
                        "comment": cm,
                        "is_partition": True,
                    }
                )
            continue

        if state in ("columns", "after_blank"):
            if cn_low not in seen_cols:
                seen_cols.add(cn_low)
                cols.append(
                    {
                        "name": cn,
                        "data_type": dt or "STRING",
                        "nullable": True,
                        "comment": cm,
                    }
                )

    tags: dict[str, str] = {}
    props_raw = meta.get("table properties", "").strip()
    if props_raw.startswith("[") and props_raw.endswith("]"):
        for chunk in props_raw[1:-1].split(","):
            if "=" in chunk:
                k, v = chunk.split("=", 1)
                ks = k.strip()
                if ks and not ks.lower().startswith(("delta.", "transient_lastddltime")):
                    tags[ks] = v.strip()

    stat_rows: int | None = None
    stats_str = meta.get("statistics", "")
    m_rows = re.search(r"(?<!\d)(\d++)\s++rows", stats_str, re.IGNORECASE)
    if m_rows:
        stat_rows = int(m_rows.group(1))

    catalog = meta.get("catalog", "").strip() or "hive_metastore"
    schema = meta.get("database", "").strip() or default_db
    table = meta.get("table", "").strip() or default_table
    return {
        "catalog": catalog,
        "schema_name": schema,
        "table_name": table,
        "fqn": f"{catalog}.{schema}.{table}",
        "table_type": meta.get("type", "MANAGED").strip().upper() or "MANAGED",
        "comment": meta.get("comment", ""),
        "columns": cols,
        "partition_cols": partition_cols,
        "tags": tags,
        "row_count": stat_rows,
    }


def build_discovery_bundle(
    spark_session: Any = None,
    *,
    catalogs_include: Sequence[str] = tuple(CATALOGS_INCLUDE),
    catalogs_exclude: Sequence[str] = tuple(CATALOGS_EXCLUDE),
    lineage_lookback_days: int = LINEAGE_LOOKBACK_DAYS,
    include_sample_rows: bool = INCLUDE_SAMPLE_ROWS,
    max_sample_rows: int = MAX_SAMPLE_ROWS,
    max_tables: int = MAX_TABLES,
) -> dict[str, Any]:
    """Query Databricks UC (or Hive fallback) and return a PII-masked Tier 2 bundle."""
    spark_obj = spark_session if spark_session is not None else _get_notebook_global("spark")
    if spark_obj is None:
        raise RuntimeError(
            "No active SparkSession provided or found in notebook globals (`spark`)."
        )

    warnings: list[str] = []
    tables_map: dict[str, dict[str, Any]] = {}
    lineage_edges: list[dict[str, str]] = []
    tags_list: list[dict[str, str]] = []
    workflows_list: list[dict[str, str]] = []
    query_stats_list: list[dict[str, Any]] = []
    profiles_list: list[dict[str, Any]] = []
    catalog_mode = "unity_catalog"

    try:
        uc_tables = _run_spark_sql(
            spark_obj,
            "SELECT table_catalog, table_schema, table_name, table_type, comment, last_altered "
            "FROM system.information_schema.tables "
            "WHERE table_schema <> 'information_schema'",
            fallback_cols=(
                "table_catalog",
                "table_schema",
                "table_name",
                "table_type",
                "comment",
                "last_altered",
            ),
        )
        for row in uc_tables:
            cat = str(_row_ci_get(row, "table_catalog", "catalog_name", "catalog") or "").strip()
            sch = str(_row_ci_get(row, "table_schema", "schema_name", "schema") or "").strip()
            tbl = str(_row_ci_get(row, "table_name", "name", "table") or "").strip()
            if not cat or not sch or not tbl or sch.lower() == "information_schema":
                continue
            if not _is_catalog_selected(cat, catalogs_include, catalogs_exclude):
                continue
            if len(tables_map) >= max_tables:
                break
            fqn = f"{cat}.{sch}.{tbl}"
            tables_map[fqn] = {
                "fqn": fqn,
                "catalog": cat,
                "schema_name": sch,
                "table_name": tbl,
                "table_type": str(_row_ci_get(row, "table_type") or "MANAGED").strip().upper(),
                "comment": str(_row_ci_get(row, "comment") or ""),
                "columns": [],
                "tags": {},
                "upstream_refs": [],
                "downstream_refs": [],
                "scheduled_jobs": [],
                "sample_rows": [],
            }

        try:
            uc_cols = _run_spark_sql(
                spark_obj,
                "SELECT table_catalog, table_schema, table_name, column_name, "
                "full_data_type, data_type, is_nullable, comment, ordinal_position, partition_index "
                "FROM system.information_schema.columns "
                "WHERE table_schema <> 'information_schema' "
                "ORDER BY table_catalog, table_schema, table_name, ordinal_position",
                fallback_cols=(
                    "table_catalog",
                    "table_schema",
                    "table_name",
                    "column_name",
                    "full_data_type",
                    "data_type",
                    "is_nullable",
                    "comment",
                    "ordinal_position",
                    "partition_index",
                ),
            )
        except Exception:
            uc_cols = _run_spark_sql(
                spark_obj,
                "SELECT table_catalog, table_schema, table_name, column_name, "
                "data_type, is_nullable, comment, ordinal_position "
                "FROM system.information_schema.columns "
                "WHERE table_schema <> 'information_schema' "
                "ORDER BY table_catalog, table_schema, table_name, ordinal_position",
                fallback_cols=(
                    "table_catalog",
                    "table_schema",
                    "table_name",
                    "column_name",
                    "data_type",
                    "is_nullable",
                    "comment",
                    "ordinal_position",
                ),
            )
        for crow in uc_cols:
            cat = str(_row_ci_get(crow, "table_catalog", "catalog_name", "catalog") or "").strip()
            sch = str(_row_ci_get(crow, "table_schema", "schema_name", "schema") or "").strip()
            tbl = str(_row_ci_get(crow, "table_name", "table") or "").strip()
            fqn = f"{cat}.{sch}.{tbl}"
            if fqn not in tables_map:
                continue
            cname = str(_row_ci_get(crow, "column_name", "name") or "").strip()
            if not cname:
                continue
            dtype = str(
                _row_ci_get(crow, "full_data_type", "data_type", "type") or "STRING"
            ).strip()
            raw_null = _row_ci_get(crow, "is_nullable", "nullable")
            nullable = (
                str(raw_null).strip().upper() not in {"NO", "FALSE", "0"}
                if raw_null is not None
                else True
            )
            p_idx = _row_ci_get(crow, "partition_index")
            tables_map[fqn]["columns"].append(
                {
                    "name": cname,
                    "data_type": dtype,
                    "nullable": nullable,
                    "comment": str(_row_ci_get(crow, "comment") or ""),
                    "is_partition": p_idx is not None and str(p_idx).strip() != "",
                }
            )

        try:
            tag_rows = _run_spark_sql(
                spark_obj,
                "SELECT catalog_name, schema_name, table_name, tag_name, tag_value "
                "FROM system.information_schema.table_tags",
                fallback_cols=(
                    "catalog_name",
                    "schema_name",
                    "table_name",
                    "tag_name",
                    "tag_value",
                ),
            )
            for trow in tag_rows:
                cat = str(_row_ci_get(trow, "catalog_name", "table_catalog") or "").strip()
                sch = str(_row_ci_get(trow, "schema_name", "table_schema") or "").strip()
                tbl = str(_row_ci_get(trow, "table_name") or "").strip()
                fqn = f"{cat}.{sch}.{tbl}"
                tk = str(_row_ci_get(trow, "tag_name", "key") or "").strip()
                tv = str(_row_ci_get(trow, "tag_value", "value") or "")
                if fqn in tables_map and tk:
                    tables_map[fqn]["tags"][tk] = tv
                    tags_list.append({"fqn": fqn, "tag_name": tk, "tag_value": tv})
        except Exception as exc:
            warnings.append(f"system.information_schema.table_tags unavailable: {exc}")

        try:
            lin_rows = _run_spark_sql(
                spark_obj,
                f"SELECT source_table_full_name, target_table_full_name, entity_type, entity_id "
                f"FROM system.access.table_lineage "
                f"WHERE event_time >= current_timestamp() - INTERVAL {int(lineage_lookback_days)} DAYS",
                fallback_cols=(
                    "source_table_full_name",
                    "target_table_full_name",
                    "entity_type",
                    "entity_id",
                ),
            )
            for lrow in lin_rows:
                src = str(_row_ci_get(lrow, "source_table_full_name", "source_fqn") or "").strip()
                tgt = str(_row_ci_get(lrow, "target_table_full_name", "target_fqn") or "").strip()
                ent_type = str(_row_ci_get(lrow, "entity_type") or "").strip().upper()
                ent_id = str(_row_ci_get(lrow, "entity_id") or "").strip()
                if src and tgt and src.casefold() != tgt.casefold():
                    lineage_edges.append(
                        {"source_table_full_name": src, "target_table_full_name": tgt}
                    )
                    if tgt in tables_map and src not in tables_map[tgt]["upstream_refs"]:
                        tables_map[tgt]["upstream_refs"].append(src)
                    if src in tables_map and tgt not in tables_map[src]["downstream_refs"]:
                        tables_map[src]["downstream_refs"].append(tgt)
                if ent_type in {"JOB", "PIPELINE", "NOTEBOOK"} and ent_id:
                    job_label = f"{ent_type.lower()}:{ent_id}"
                    for endpoint in (tgt, src):
                        if (
                            endpoint in tables_map
                            and job_label not in tables_map[endpoint]["scheduled_jobs"]
                        ):
                            tables_map[endpoint]["scheduled_jobs"].append(job_label)
                            workflows_list.append({"fqn": endpoint, "job_name": job_label})
        except Exception as exc:
            warnings.append(f"system.access.table_lineage unavailable: {exc}")

        try:
            q_rows = _run_spark_sql(
                spark_obj,
                f"SELECT statement_text FROM system.query.history "
                f"WHERE start_time >= current_timestamp() - INTERVAL {int(lineage_lookback_days)} DAYS "
                f"AND execution_status = 'FINISHED' LIMIT 5000",
                fallback_cols=("statement_text",),
            )
            counts_by_fqn: dict[str, int] = {k: 0 for k in tables_map}
            for qrow in q_rows:
                direct_fqn = _row_ci_get(qrow, "fqn", "table_full_name")
                direct_cnt = _row_ci_get(qrow, "query_count_30d", "query_count", "count")
                if direct_fqn and direct_cnt is not None:
                    fk = str(direct_fqn).strip()
                    iv_q = _finite_nonneg_int(direct_cnt)
                    if fk in counts_by_fqn and iv_q is not None:
                        counts_by_fqn[fk] += iv_q
                    continue
                stmt = str(_row_ci_get(qrow, "statement_text") or "").lower()
                if not stmt:
                    continue
                for fqn in tables_map:
                    if fqn.lower() in stmt:
                        counts_by_fqn[fqn] += 1
            for fqn, cnt in counts_by_fqn.items():
                tables_map[fqn]["query_count_30d"] = cnt
                query_stats_list.append({"fqn": fqn, "query_count_30d": cnt})
        except Exception as exc:
            warnings.append(f"system.query.history unavailable: {exc}")

    except Exception as uc_exc:
        catalog_mode = "hive_metastore"
        warnings.append(
            f"Unity Catalog system.information_schema unavailable ({uc_exc}); fell back to Hive Metastore."
        )
        if _is_catalog_selected("hive_metastore", catalogs_include, catalogs_exclude):
            try:
                db_rows = _run_spark_sql(
                    spark_obj, "SHOW DATABASES", fallback_cols=("databaseName",)
                )
            except Exception:
                db_rows = _run_spark_sql(
                    spark_obj, "SHOW SCHEMAS", fallback_cols=("databaseName",)
                )
            for db_row in db_rows:
                db_name = str(
                    _row_ci_get(
                        db_row,
                        "databaseName",
                        "namespace",
                        "schema_name",
                        "database_name",
                        "name",
                        "col_0",
                        "value",
                    )
                    or ""
                ).strip()
                if not db_name or db_name.lower() == "information_schema":
                    continue
                if any(
                    fnmatch.fnmatchcase(db_name.lower(), p.strip().lower())
                    for p in catalogs_exclude
                    if p and p.strip()
                ):
                    continue
                t_rows = _run_spark_sql(
                    spark_obj,
                    f"SHOW TABLES IN {_quote_ident(db_name)}",
                    fallback_cols=("database", "tableName", "isTemporary"),
                )
                for trow in t_rows:
                    if len(tables_map) >= max_tables:
                        break
                    is_temp = _row_ci_get(trow, "isTemporary", "is_temporary", "col_2")
                    if is_temp is True or str(is_temp).strip().lower() == "true":
                        continue
                    t_raw = _row_ci_get(trow, "tableName", "table_name", "name")
                    if t_raw is None:
                        t_raw = (
                            trow["col_1"]
                            if ("col_1" in trow and trow["col_1"] is not None)
                            else _row_ci_get(trow, "database", "col_0", "value")
                        )
                    t_name = str(t_raw or "").strip()
                    if not t_name:
                        continue
                    desc_rows = _run_spark_sql(
                        spark_obj,
                        f"DESCRIBE EXTENDED {_quote_ident(db_name)}.{_quote_ident(t_name)}",
                        fallback_cols=("col_name", "data_type", "comment"),
                    )
                    parsed = _parse_describe_extended(desc_rows, db_name, t_name)
                    fqn = parsed["fqn"]
                    tables_map[fqn] = {
                        "fqn": fqn,
                        "catalog": parsed["catalog"],
                        "schema_name": parsed["schema_name"],
                        "table_name": parsed["table_name"],
                        "table_type": parsed["table_type"],
                        "comment": parsed["comment"],
                        "row_count": parsed["row_count"],
                        "columns": parsed["columns"],
                        "tags": parsed["tags"],
                        "upstream_refs": [],
                        "downstream_refs": [],
                        "scheduled_jobs": [],
                        "sample_rows": [],
                    }

    # Per-table safe statistical aggregates (and conditional sample rows)
    now_utc = datetime.now(timezone.utc)
    for fqn, tbl_entry in tables_map.items():
        cols = tbl_entry.get("columns", [])
        ts_col: str | None = None
        for c in cols:
            c_name = str(c.get("name") or "")
            c_type = str(c.get("data_type") or "").upper()
            c_comment = str(c.get("comment") or "")
            if ("TIMESTAMP" in c_type or "DATE" in c_type) and not is_pii_column_name(
                c_name, comment=c_comment
            ):
                ts_col = c_name
                break

        select_exprs = ["COUNT(*) AS row_count"]
        select_cols = ["row_count"]
        if ts_col:
            q_ts = _quote_ident(ts_col)
            select_exprs.append(f"MIN({q_ts}) AS min_timestamp")
            select_cols.append("min_timestamp")
            select_exprs.append(f"MAX({q_ts}) AS max_timestamp")
            select_cols.append("max_timestamp")

        profiled_cols: list[tuple[int, dict[str, Any]]] = []
        for idx, c in enumerate(cols[:50]):
            c_type = str(c.get("data_type") or "").upper()
            if any(
                k in c_type for k in ("ARRAY", "MAP", "STRUCT", "BINARY", "VARIANT")
            ):
                continue
            qc = _quote_ident(str(c["name"]))
            select_exprs.append(
                f"SUM(CASE WHEN {qc} IS NULL THEN 1 ELSE 0 END) AS __nulls__{idx}"
            )
            select_cols.append(f"__nulls__{idx}")
            select_exprs.append(f"APPROX_COUNT_DISTINCT({qc}) AS __distinct__{idx}")
            select_cols.append(f"__distinct__{idx}")
            profiled_cols.append((idx, c))

        two_part = catalog_mode == "hive_metastore" and tbl_entry["catalog"] == "hive_metastore"
        target_sql_ref = _quote_fqn(
            tbl_entry["catalog"],
            tbl_entry["schema_name"],
            tbl_entry["table_name"],
            two_part=two_part,
        )
        agg_sql = f"SELECT {', '.join(select_exprs)} FROM {target_sql_ref}"
        try:
            agg_rows = _run_spark_sql(
                spark_obj, agg_sql, fallback_cols=tuple(select_cols)
            )
        except Exception:
            try:
                agg_rows = _run_spark_sql(
                    spark_obj,
                    f"SELECT COUNT(*) AS row_count FROM {target_sql_ref}",
                    fallback_cols=("row_count",),
                )
            except Exception as exc:
                warnings.append(f"Aggregate profile skipped for {fqn}: {exc}")
                agg_rows = []

        if agg_rows:
            arow = agg_rows[0]
            rc_raw = _row_ci_get(arow, "row_count", "cnt", "col_0")
            rc = _finite_nonneg_int(rc_raw)
            if rc is not None:
                tbl_entry["row_count"] = rc
            min_ts = _row_ci_get(arow, "min_timestamp")
            max_ts = _row_ci_get(arow, "max_timestamp")
            if min_ts is not None:
                dt_min = _parse_iso_dt(min_ts)
                tbl_entry["min_timestamp"] = (
                    dt_min.isoformat() if dt_min else str(min_ts)
                )
            if max_ts is not None:
                dt_max = _parse_iso_dt(max_ts)
                tbl_entry["max_timestamp"] = (
                    dt_max.isoformat() if dt_max else str(max_ts)
                )
                if dt_max is not None:
                    lag_h = max(
                        0.0, round((now_utc - dt_max).total_seconds() / 3600.0, 2)
                    )
                    tbl_entry["freshness_lag_hours"] = lag_h

            rc_val = tbl_entry.get("row_count")
            for idx, col_dict in profiled_cols:
                n_val = _finite_float(_row_ci_get(arow, f"__nulls__{idx}"))
                d_val = _finite_nonneg_int(_row_ci_get(arow, f"__distinct__{idx}"))
                if (
                    n_val is not None
                    and n_val >= 0.0
                    and isinstance(rc_val, int)
                    and not isinstance(rc_val, bool)
                ):
                    if rc_val > 0:
                        pct = max(0.0, min(100.0, round((n_val / rc_val) * 100.0, 2)))
                        col_dict["null_pct"] = pct
                    else:
                        col_dict["null_pct"] = 0.0
                if d_val is not None:
                    col_dict["distinct_count"] = d_val

            profiles_list.append(
                {
                    "fqn": fqn,
                    "row_count": tbl_entry.get("row_count"),
                    "min_timestamp": tbl_entry.get("min_timestamp"),
                    "max_timestamp": tbl_entry.get("max_timestamp"),
                    "freshness_lag_hours": tbl_entry.get("freshness_lag_hours"),
                }
            )

        if include_sample_rows and max_sample_rows > 0:
            try:
                s_rows = _run_spark_sql(
                    spark_obj,
                    f"SELECT * FROM {target_sql_ref} LIMIT {int(max_sample_rows)}",
                )
                tbl_entry["sample_rows"] = s_rows[: int(max_sample_rows)]
            except Exception as exc:
                warnings.append(f"Sample row query skipped for {fqn}: {exc}")

    flat_columns: list[dict[str, Any]] = []
    for fqn, tbl_entry in tables_map.items():
        for c in tbl_entry.get("columns", []):
            flat_columns.append({"fqn": fqn, **c})

    raw_bundle: dict[str, Any] = {
        "bundle_version": "1.0",
        "generated_at": now_utc.isoformat(),
        "catalog_mode": catalog_mode,
        "tables": list(tables_map.values()),
        "columns": flat_columns,
        "lineage": lineage_edges,
        "tags": tags_list,
        "workflows": workflows_list,
        "query_stats": query_stats_list,
        "profiles": profiles_list,
        "warnings": warnings,
    }
    return mask_bundle_pii(
        raw_bundle,
        include_sample_rows=include_sample_rows,
        max_sample_rows=max_sample_rows,
    )


def export_discovery_bundle(
    bundle: dict[str, Any],
    output_path: str | Path = OUTPUT_BUNDLE_PATH,
    dbutils_obj: Any = None,
) -> str:
    """Write a redacted discovery bundle to DBFS or local filesystem as UTF-8 JSON."""
    payload = json.dumps(bundle, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    out_str = str(output_path)
    resolved_dbutils = (
        dbutils_obj if dbutils_obj is not None else _get_notebook_global("dbutils")
    )
    if (
        out_str.startswith("dbfs:/")
        and resolved_dbutils is not None
        and hasattr(getattr(resolved_dbutils, "fs", None), "put")
    ):
        resolved_dbutils.fs.put(out_str, payload, True)
        return out_str

    target = Path(out_str)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(payload)
    return target.as_posix()


# COMMAND ----------

# Auto-execute ONLY inside an active Databricks notebook session where `spark` is bound
_active_spark = _get_notebook_global("spark")
if _active_spark is not None:
    _discovered_bundle = build_discovery_bundle(
        _active_spark,
        catalogs_include=CATALOGS_INCLUDE,
        catalogs_exclude=CATALOGS_EXCLUDE,
        lineage_lookback_days=LINEAGE_LOOKBACK_DAYS,
        include_sample_rows=INCLUDE_SAMPLE_ROWS,
        max_sample_rows=MAX_SAMPLE_ROWS,
        max_tables=MAX_TABLES,
    )
    export_discovery_bundle(_discovered_bundle, OUTPUT_BUNDLE_PATH)
