"""Privacy gate, PII column detection, and secret/inline-PII redaction."""

from __future__ import annotations

import copy
import json
import math
import re
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from poopdeck.config import DEFAULT_PII_COLUMN_PATTERNS, PrivacyConfig

_COMMENT_EXPLICIT_PII_RE = re.compile(
    r"(?i)(?<!non-)(?<!non_)(?<!non\s)(?<!not\s)\b(?:pii|phi|personally[\s_-]+identifiable)\b"
)

# Secret scrubbing rules ordered strictly so multiline/structured credentials
# are masked BEFORE generic token, email, or phone patterns run.
_SECRET_RULES: list[tuple[re.Pattern[str], str]] = [
    # 1. PEM Private Key blocks (bounded O(n) multiline & single-line header fallback)
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
    # 2. Authorization Bearer tokens (JWT or opaque >= 16 chars)
    (
        re.compile(r"(?i)\bBearer\s++(?!\[REDACTED:)[A-Za-z0-9_\-\.~+/]{16,}=*"),
        "[REDACTED:SECRET]",
    ),
]

# 3. Connection-string & key=value credentials (stateful O(n) line quote-parity scanner)
_KV_KEY_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(pwd|password|passphrase|accountkey|sharedaccesskey|"
    r"sharedaccesssignature|client_secret|clientsecret|api_key|apikey|access_key|"
    r"secret_key|aws_secret_access_key|auth_accesstoken|accesstoken|access_token|"
    r"refresh_token|personal_access_token)[ \t]*=[ \t]*"
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

_SECRET_RULES_POST: list[tuple[re.Pattern[str], str]] = [
    # 4. Standalone JWTs (bounded base64url header.payload.signature)
    (
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{5,256}+\.[A-Za-z0-9_-]{5,4096}+\.[A-Za-z0-9_-]{5,1024}+\b"
        ),
        "[REDACTED:SECRET]",
    ),
    # 5. URL basic-auth credentials (scheme://user:password@host) BEFORE email regex!
    (
        re.compile(
            r"\b([A-Za-z][A-Za-z0-9+.-]{0,31}://[^/\s:;?#,\x22\x27<>`()\[\]{}|]*:)"
            r"(?!\[REDACTED:[A-Z_]+\]@)(?!\d{1,5}(?:[,)|\]}>]|&[A-Za-z0-9_]+=))"
            r"([^/\s;?#,\x22\x27<>`()\[\]{}|]+)(@)(?=[A-Za-z0-9.\-]|\[[0-9A-Fa-f:])"
        ),
        r"\1[REDACTED:SECRET]\3",
    ),
    # 6. Azure SAS query parameter sig=...
    (
        re.compile(
            r"(?i)([?&]sig=)(?!\[REDACTED:[A-Z_]+\])([^&\s;`\x22\x27\\,()\[\]{}<>]+)"
        ),
        r"\1[REDACTED:SECRET]",
    ),
    # 7. Cloud, GitHub & AI PATs / API keys (specific prefixes before generic sk-)
    (re.compile(r"\bdapi[0-9a-fA-F]{16,64}\b"), "[REDACTED:SECRET]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,255}\b"), "[REDACTED:SECRET]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,255}\b"), "[REDACTED:SECRET]"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{12,255}\b"), "[REDACTED:SECRET]"),
    (re.compile(r"\bsk-(?:proj-|live-|test-)?[A-Za-z0-9_-]{16,255}\b"), "[REDACTED:SECRET]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED:SECRET]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,255}\b"), "[REDACTED:SECRET]"),
]

# Inline PII regexes ordered EMAIL -> SSN -> PHONE (RFC-bounded O(n) & multi-pass idempotent)
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]{1,64}@(?:[A-Za-z0-9-]{1,63}\.){1,10}[A-Za-z]{2,24}\b"
)
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PHONE_US_RE = re.compile(
    r"(?:\+1[-.\s]?(?:\(\d{3}\)[-.\s]?|\d{3}[-.\s])|\(\d{3}\)[-.\s]?|(?<![A-Za-z0-9_])(?<!\d[-.])(?:1[-.\s]?)?(?:\(\d{3}\)[-.\s]?|\d{3}[-.\s]))"
    r"\d{3}[-.\s]\d{4}(?:\s*(?:ext\.?|x)\s*\d{1,5})?(?![A-Za-z0-9_]|[-.]\d)",
    re.IGNORECASE,
)
_PHONE_INTL_RE = re.compile(
    r"(?:\+1\d{10}|\+\d{1,3}[\s-]\(?\d{1,4}\)?[\s-]\d{3,4}(?:[\s-]\d{2,4}){1,2})(?![A-Za-z0-9_]|[-.]\d)"
)


def _normalize_col_identifier(col_name: str) -> str:
    """Normalize SQL/DAX/camelCase column names into lowercase snake_case tokens."""
    s = col_name.strip()
    if "[" in s and s.endswith("]"):
        s = s[s.rfind("[") + 1 : -1].strip()
    elif "." in s and not s.startswith("."):
        s = s.rsplit(".", 1)[-1].strip()
    s = s.strip("'\"`")
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    s = re.sub(r"([A-Z])([A-Z][a-z])", r"\1_\2", s)
    s = re.sub(r"[\s\-\./]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s.lower()


def is_pii_column(
    col_name: str,
    comment: str | None = None,
    config: PrivacyConfig | None = None,
) -> bool:
    """Return True if column name or comment matches configured PII patterns."""
    if not col_name and not comment:
        return False

    patterns = (
        config.pii_column_patterns
        if config is not None
        else DEFAULT_PII_COLUMN_PATTERNS
    )
    norm_col = _normalize_col_identifier(col_name) if col_name else ""

    for pat in patterns:
        if norm_col and re.search(pat, norm_col, flags=re.IGNORECASE):
            return True

    if comment and comment.strip():
        clean_comment = comment.strip()
        if _COMMENT_EXPLICIT_PII_RE.search(clean_comment):
            return True
        for pat in patterns:
            if pat in (r"^name$", r"\bname\b"):
                continue
            if re.search(pat, clean_comment, flags=re.IGNORECASE):
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


def redact_text(text: str, redact_secrets: bool = True) -> str:
    """Scrub secret tokens (when enabled) and inline PII from free text."""
    if not text or not isinstance(text, str):
        return text if isinstance(text, str) else ""

    out = text
    if redact_secrets:
        for regex, repl in _SECRET_RULES:
            out = regex.sub(repl, out)
        out = _redact_kv_secrets(out)
        for regex, repl in _SECRET_RULES_POST:
            out = regex.sub(repl, out)

    out = _EMAIL_RE.sub("[REDACTED:EMAIL]", out)
    out = _SSN_RE.sub("[REDACTED:SSN]", out)
    out = _PHONE_US_RE.sub("[REDACTED:PHONE]", out)
    out = _PHONE_INTL_RE.sub(_replace_intl_phone, out)
    return out


def _deterministic_sort_key(item: Any) -> tuple[str, str]:
    return (f"{type(item).__module__}.{type(item).__qualname__}", repr(item))


def _canonical_key_rank(item: Any) -> str:
    """Return a PYTHONHASHSEED-independent total-order rank string for raw dict keys."""
    tag = f"{type(item).__module__}.{type(item).__qualname__}"
    if item is None or isinstance(item, (bool, int, str, bytes)):
        return f"{tag}:{item!r}"
    if isinstance(item, float):
        return f"{tag}:{'nan' if math.isnan(item) else repr(item)}"
    if isinstance(item, Decimal):
        return f"{tag}:{item}"
    if isinstance(item, (datetime, date)):
        return f"{tag}:{item.isoformat()}"
    if isinstance(item, Path):
        return f"{tag}:{item.as_posix()}"
    if isinstance(item, Enum):
        return f"{tag}:{_canonical_key_rank(item.value)}"
    if isinstance(item, (tuple, list)):
        return f"{tag}:[{','.join(_canonical_key_rank(x) for x in item)}]"
    if isinstance(item, (set, frozenset)):
        return f"{tag}:{{{','.join(sorted(_canonical_key_rank(x) for x in item))}}}"
    if isinstance(item, dict):
        pairs = sorted(
            f"{_canonical_key_rank(k)}={_canonical_key_rank(v)}"
            for k, v in item.items()
        )
        return f"{tag}:{{{','.join(pairs)}}}"
    return f"{tag}:{item}"


def _normalize_key_for_json(item: Any, redact_secrets: bool) -> Any:
    if isinstance(item, str):
        return redact_text(item, redact_secrets=redact_secrets)
    if item is None or isinstance(item, (bool, int)):
        return item
    if isinstance(item, float):
        if not math.isfinite(item):
            raise ValueError("Numeric value must be finite")
        return item
    if isinstance(item, Decimal):
        if not item.is_finite():
            raise ValueError("Numeric value must be finite")
        _, digits, exp = item.as_tuple()
        if isinstance(exp, int) and exp >= 0 and len(digits) + exp <= 38:
            return int(item)
        try:
            f = float(item)
        except OverflowError as exc:
            raise ValueError("Numeric value must be finite") from exc
        if not math.isfinite(f):
            raise ValueError("Numeric value must be finite")
        return f
    if isinstance(item, complex):
        raise ValueError("Numeric value must be finite")
    if isinstance(item, (datetime, date)):
        return item.isoformat()
    if isinstance(item, Path):
        return redact_text(item.as_posix(), redact_secrets=redact_secrets)
    if isinstance(item, Enum):
        return _normalize_key_for_json(item.value, redact_secrets)
    if isinstance(item, bytes):
        return redact_text(
            item.decode("utf-8", errors="replace"), redact_secrets=redact_secrets
        )
    if isinstance(item, (list, tuple)):
        return [_normalize_key_for_json(elem, redact_secrets) for elem in item]
    if isinstance(item, (set, frozenset)):
        return sorted(
            (_normalize_key_for_json(elem, redact_secrets) for elem in item),
            key=_deterministic_sort_key,
        )
    if isinstance(item, dict):
        out: dict[str, Any] = {}
        winners: dict[str, str] = {}
        for sub_k, sub_v in item.items():
            clean_k = _scrub_dict_key(sub_k, redact_secrets)
            norm_v = _normalize_key_for_json(sub_v, redact_secrets)
            rank = _canonical_key_rank(sub_k)
            if clean_k not in out or rank >= winners[clean_k]:
                out[clean_k] = norm_v
                winners[clean_k] = rank
        return {k: out[k] for k in sorted(out)}
    return redact_text(str(item), redact_secrets=redact_secrets)


def _scrub_dict_key(key: Any, redact_secrets: bool) -> str:
    if isinstance(key, str):
        return redact_text(key, redact_secrets=redact_secrets)
    if isinstance(key, Enum):
        return _scrub_dict_key(key.value, redact_secrets)
    if isinstance(key, (tuple, list, set, frozenset, dict)):
        return json.dumps(
            _normalize_key_for_json(key, redact_secrets),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
    norm_scalar = _normalize_key_for_json(key, redact_secrets)
    return norm_scalar if isinstance(norm_scalar, str) else str(norm_scalar)


def _key_matches_pii_column(
    key: Any,
    pii_cols_norm: set[str],
    cfg: PrivacyConfig,
) -> bool:
    if isinstance(key, str):
        return (
            key.casefold() in pii_cols_norm
            or _normalize_col_identifier(key) in pii_cols_norm
            or is_pii_column(key, config=cfg)
        )
    if isinstance(key, Enum):
        return _key_matches_pii_column(key.value, pii_cols_norm, cfg)
    if isinstance(key, (tuple, list, set, frozenset)):
        return any(
            _key_matches_pii_column(part, pii_cols_norm, cfg) for part in key
        )
    if isinstance(key, dict):
        return any(
            _key_matches_pii_column(sub_k, pii_cols_norm, cfg)
            or _key_matches_pii_column(sub_v, pii_cols_norm, cfg)
            for sub_k, sub_v in key.items()
        )
    return False


def _scrub_non_pii_stat(value: Any, redact_secrets: bool) -> Any:
    if isinstance(value, str):
        return redact_text(value, redact_secrets=redact_secrets)
    if isinstance(value, Decimal) and value.is_finite():
        _, digits, exp = value.as_tuple()
        if isinstance(exp, int) and exp >= 0 and len(digits) + exp <= 38:
            return int(value)
        try:
            f = float(value)
            if math.isfinite(f):
                return f
        except OverflowError:
            pass
        return value
    if isinstance(value, list):
        return [_scrub_non_pii_stat(v, redact_secrets) for v in value]
    if isinstance(value, tuple):
        return tuple(_scrub_non_pii_stat(v, redact_secrets) for v in value)
    if isinstance(value, (set, frozenset)):
        return sorted(
            (_scrub_non_pii_stat(v, redact_secrets) for v in value),
            key=_deterministic_sort_key,
        )
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        winners: dict[str, str] = {}
        for k, v in value.items():
            clean_k = _scrub_dict_key(k, redact_secrets)
            clean_v = _scrub_non_pii_stat(v, redact_secrets)
            rank = _canonical_key_rank(k)
            if clean_k not in out or rank >= winners[clean_k]:
                out[clean_k] = clean_v
                winners[clean_k] = rank
        return out
    return value


def redact_dataset_payload(
    payload: dict,
    config: PrivacyConfig | None = None,
) -> dict:
    """Return a deep-copied dataset payload with PII columns/stats/rows scrubbed."""
    cfg = config if config is not None else PrivacyConfig()
    out = copy.deepcopy(payload)

    for text_key in ("comment", "description", "grain"):
        if isinstance(out.get(text_key), str):
            out[text_key] = redact_text(out[text_key], redact_secrets=cfg.redact_secrets)
    if out.get("tags") is not None:
        out["tags"] = _scrub_non_pii_stat(out["tags"], cfg.redact_secrets)

    pii_cols_norm: set[str] = set()
    columns = out.get("columns")
    if isinstance(columns, (tuple, set, frozenset)):
        columns = (
            list(columns)
            if isinstance(columns, tuple)
            else sorted(columns, key=_deterministic_sort_key)
        )
        out["columns"] = columns
    if isinstance(columns, list):
        for col in columns:
            if not isinstance(col, dict):
                continue
            has_name = "name" in col and col.get("name") is not None
            raw_col_name = col.get("name") if has_name else col.get("column_name")
            col_name = (
                _scrub_dict_key(raw_col_name, cfg.redact_secrets)
                if raw_col_name is not None
                else ""
            )
            if has_name and col["name"] != col_name:
                col["name"] = col_name
            elif (
                not has_name
                and "column_name" in col
                and raw_col_name is not None
                and col["column_name"] != col_name
            ):
                col["column_name"] = col_name

            raw_comment = (
                col.get("comment") if isinstance(col.get("comment"), str) else None
            )
            flagged = (
                bool(col.get("is_pii", False))
                or _key_matches_pii_column(raw_col_name, set(), cfg)
                or is_pii_column(col_name, comment=raw_comment, config=cfg)
            )
            col["is_pii"] = flagged
            if raw_comment is not None:
                col["comment"] = redact_text(
                    raw_comment, redact_secrets=cfg.redact_secrets
                )

            if flagged:
                if col_name:
                    pii_cols_norm.add(col_name.casefold())
                    pii_cols_norm.add(_normalize_col_identifier(col_name))
                if isinstance(raw_col_name, str) and raw_col_name:
                    pii_cols_norm.add(raw_col_name.casefold())
                    pii_cols_norm.add(_normalize_col_identifier(raw_col_name))
                for stat_key in ("min_val", "max_val", "min_value", "max_value"):
                    if stat_key in col and col[stat_key] is not None:
                        col[stat_key] = "[REDACTED:PII]"
                if "sample_values" in col and col["sample_values"] is not None:
                    if isinstance(col["sample_values"], (list, tuple, set, frozenset)):
                        col["sample_values"] = [
                            "[REDACTED:PII]" for _ in col["sample_values"]
                        ]
                    else:
                        col["sample_values"] = "[REDACTED:PII]"
            else:
                for stat_key in (
                    "min_val",
                    "max_val",
                    "min_value",
                    "max_value",
                    "sample_values",
                ):
                    if stat_key in col and col[stat_key] is not None:
                        col[stat_key] = _scrub_non_pii_stat(
                            col[stat_key], cfg.redact_secrets
                        )

    # Sample Rows Gate: fail closed (sample_rows=[]) unless allow_sample_rows is True
    keep_samples = bool(cfg.allow_sample_rows) and cfg.max_sample_rows > 0
    raw_rows = out.get("sample_rows")
    if not keep_samples or not isinstance(raw_rows, (list, tuple, set, frozenset)):
        out["sample_rows"] = []
    else:
        rows_seq = (
            list(raw_rows)
            if isinstance(raw_rows, (list, tuple))
            else sorted(raw_rows, key=_deterministic_sort_key)
        )
        scrubbed_rows: list[dict[str, Any]] = []
        for row in rows_seq[: cfg.max_sample_rows]:
            if not isinstance(row, dict):
                continue
            clean_row: dict[str, Any] = {}
            row_winners: dict[str, str] = {}
            row_pii_flags: dict[str, bool] = {}
            for k, v in row.items():
                clean_key = _scrub_dict_key(k, cfg.redact_secrets)
                is_cell_pii = (
                    clean_key.casefold() in pii_cols_norm
                    or _normalize_col_identifier(clean_key) in pii_cols_norm
                    or _key_matches_pii_column(k, pii_cols_norm, cfg)
                )
                merged_pii = row_pii_flags.get(clean_key, False) or is_cell_pii
                row_pii_flags[clean_key] = merged_pii
                rank = _canonical_key_rank(k)
                if merged_pii:
                    clean_row[clean_key] = "[REDACTED:PII]"
                    row_winners[clean_key] = rank
                elif clean_key not in clean_row or rank >= row_winners[clean_key]:
                    clean_row[clean_key] = _scrub_non_pii_stat(v, cfg.redact_secrets)
                    row_winners[clean_key] = rank
            scrubbed_rows.append(clean_row)
        out["sample_rows"] = scrubbed_rows

    return out
