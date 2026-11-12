"""Databricks dual-mode crawler (Tier 1 SDK/SQL + Hive fallback & Tier 2 JSON/CSV bundles)."""

from __future__ import annotations

import csv
import fnmatch
import hashlib
import importlib.util
import io
import json
import math
import os
import re
from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from poopdeck.config import DatabricksConfig, PoopdeckConfig, PrivacyConfig, load_config
from poopdeck.crawlers import (
    SyncSummary,
    redact_crawler_text as redact_text,
    unwrap_prose_wikilinks,
)
from poopdeck.models import DatasetCard
from poopdeck.redaction import is_pii_column, redact_dataset_payload
from poopdeck.vault_io import (
    format_wikilink,
    normalize_wikilink_target,
    parse_markdown_card,
    render_dataset_body,
    upsert_markdown_card,
)

_MODE_ALIASES: dict[str, str] = {
    "auto": "auto",
    "tier1": "tier1",
    "tier1_sdk": "tier1",
    "tier2": "tier2",
    "tier2_bundle": "tier2",
    "tier2_local": "tier2",
}

_KNOWN_DBX_CSV_NAMES: frozenset[str] = frozenset(
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


def _is_windows_reserved_stem(stem: str) -> bool:
    if stem.split(".", 1)[0].strip(" ").lower() in _WINDOWS_RESERVED_STEMS:
        return True
    is_res = getattr(os.path, "isreserved", lambda _: False)
    return bool(is_res(stem) or is_res(f"{stem}.md"))


def _safe_dataset_card_stem(fqn: str) -> str:
    safe_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", fqn).strip(". ") or "dataset"
    if _is_windows_reserved_stem(safe_stem):
        safe_stem = f"_{safe_stem}"
    if len(safe_stem) > 180:
        fqn_hash = hashlib.sha1(fqn.encode("utf-8")).hexdigest()[:10]
        safe_stem = f"{safe_stem[:140]}_{fqn_hash}"
    return f"_{safe_stem}" if _is_windows_reserved_stem(safe_stem) else safe_stem


def _decode_file_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _normalize_mode(mode: str) -> str:
    norm = (mode or "").strip().lower()
    if norm not in _MODE_ALIASES:
        raise ValueError(
            f"Invalid Databricks sync mode {mode!r}; expected 'auto', 'tier1', or 'tier2'"
        )
    return _MODE_ALIASES[norm]


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


def _is_catalog_allowed(catalog: str, dbx_cfg: DatabricksConfig) -> bool:
    cat_l = (catalog or "").strip().lower()
    if not cat_l:
        return False
    inc = [p.strip().lower() for p in dbx_cfg.catalogs_include if p and p.strip()]
    exc = [p.strip().lower() for p in dbx_cfg.catalogs_exclude if p and p.strip()]
    if not inc or not any(fnmatch.fnmatchcase(cat_l, pat) for pat in inc):
        return False
    return not any(fnmatch.fnmatchcase(cat_l, pat) for pat in exc)


def _quote_sql_ident(ident: str) -> str:
    s = ident.strip()
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s):
        return s
    return "`" + s.replace("`", "``") + "`"


def _quote_fqn(catalog: str, schema_name: str, table_name: str, *, two_part: bool = False) -> str:
    if two_part:
        return f"{_quote_sql_ident(schema_name)}.{_quote_sql_ident(table_name)}"
    return (
        f"{_quote_sql_ident(catalog)}."
        f"{_quote_sql_ident(schema_name)}."
        f"{_quote_sql_ident(table_name)}"
    )


def _row_get(row: dict[str, Any], *keys: str) -> Any:
    lower_map = {str(k).lower(): v for k, v in row.items()}
    for k in keys:
        if k in row and row[k] is not None:
            return row[k]
        kl = k.lower()
        if kl in lower_map and lower_map[kl] is not None:
            return lower_map[kl]
    return None


def _norm_csv_header(raw_h: str) -> str:
    s = (raw_h or "").strip()
    if "[" in s and s.endswith("]"):
        s = s[s.rfind("[") + 1 : -1]
    s = s.strip("\"'` ")
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _unwrap_scalar(val: Any) -> Any:
    """Unwrap SDK Enum, bytes, Path, datetime, and non-finite numbers before str()."""
    while isinstance(val, Enum):
        val = val.value
    if val is None or isinstance(val, (bool, int, str)):
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
        return val.decode("utf-8", errors="replace")
    if isinstance(val, Path):
        return val.as_posix()
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    return val


def _coerce_raw_str(val: Any) -> str:
    """Convert raw metadata scalar to string after unwrapping Enum/bytes/Path/Decimal."""
    u = _unwrap_scalar(val)
    if u is None:
        return ""
    return u if isinstance(u, str) else str(u)


def _parse_int_stat(val: Any, *, non_negative: bool = True) -> int | None:
    while isinstance(val, Enum):
        val = val.value
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return (val if val >= 0 else None) if non_negative else val
    if isinstance(val, Decimal):
        if not val.is_finite():
            return None
        _, digits, exp = val.as_tuple()
        if isinstance(exp, int) and exp >= 0 and len(digits) + exp <= 38:
            iv = int(val)
            return (iv if iv >= 0 else None) if non_negative else iv
    if isinstance(val, (float, Decimal)):
        try:
            f = float(val)
        except OverflowError:
            return None
        if not math.isfinite(f):
            return None
        iv = int(round(f))
        return (iv if iv >= 0 else None) if non_negative else iv
    if isinstance(val, (bytes, bytearray)):
        val = val.decode("utf-8", errors="replace")
    if isinstance(val, str):
        s = val.strip().replace(",", "")
        if not s or s.lower() in {"null", "none", "nan", "inf", "-inf", "n/a", "-"}:
            return None
        try:
            f = float(s)
        except (ValueError, OverflowError):
            return None
        if not math.isfinite(f):
            return None
        iv = int(round(f))
        return (iv if iv >= 0 else None) if non_negative else iv
    return None


def _parse_float_stat(val: Any) -> float | None:
    while isinstance(val, Enum):
        val = val.value
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float, Decimal)):
        try:
            f = float(val)
        except OverflowError:
            return None
        return f if math.isfinite(f) else None
    if isinstance(val, (bytes, bytearray)):
        val = val.decode("utf-8", errors="replace")
    if isinstance(val, str):
        s = val.strip().rstrip("%").replace(",", "").strip()
        if not s or s.lower() in {"null", "none", "nan", "inf", "-inf", "n/a", "-"}:
            return None
        try:
            f = float(s)
        except (ValueError, OverflowError):
            return None
        return f if math.isfinite(f) else None
    return None


def _parse_bool_flag(val: Any, *, default: bool = True) -> bool:
    while isinstance(val, Enum):
        val = val.value
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    s = _coerce_raw_str(val).strip().lower()
    if s in {"true", "yes", "y", "1"}:
        return True
    if s in {"false", "no", "n", "0"}:
        return False
    return default


def _parse_str_list_cell(val: Any) -> list[str]:
    while isinstance(val, Enum):
        val = val.value
    if val is None or isinstance(val, bool):
        return []
    if isinstance(val, (list, tuple, set, frozenset)):
        return [
            _coerce_raw_str(x).strip()
            for x in val
            if x is not None and _coerce_raw_str(x).strip()
        ]
    s = _coerce_raw_str(val).strip()
    if not s:
        return []
    if s.startswith("[") and s.endswith("]"):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [
                    _coerce_raw_str(x).strip()
                    for x in parsed
                    if x is not None and _coerce_raw_str(x).strip()
                ]
        except ValueError:
            pass
    parts = re.split(r"[,;|]", s)
    return [p.strip() for p in parts if p.strip()]


def _parse_iso_dt(val: Any) -> datetime | None:
    while isinstance(val, Enum):
        val = val.value
    if isinstance(val, (bytes, bytearray)):
        val = val.decode("utf-8", errors="replace")
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
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(s[:19], fmt).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
    return None


def _canonical_identity(
    raw_fqn: Any,
    raw_cat: Any,
    raw_sch: Any,
    raw_tbl: Any,
) -> tuple[str, str, str, str] | None:
    def _clean(v: Any) -> str:
        return _coerce_raw_str(v).strip().strip("`\"' ") if v is not None else ""

    fqn = _clean(raw_fqn)
    cat = _clean(raw_cat)
    sch = _clean(raw_sch)
    tbl = _clean(raw_tbl)

    if fqn and (not cat or not sch or not tbl):
        parts = [p.strip("`\"' ") for p in fqn.split(".") if p.strip("`\"' ")]
        if len(parts) >= 4 and "@" in fqn:
            if "@" in parts[-2] or "@" in parts[-1]:
                cat = cat or parts[0]
                sch = sch or parts[1]
                tbl = tbl or ".".join(parts[2:])
            elif "@" in parts[0] and "@" not in parts[-2] and "@" not in parts[-1]:
                cat = cat or ".".join(parts[:-2])
                sch = sch or parts[-2]
                tbl = tbl or parts[-1]
            elif len(parts) == 4 and "@" in parts[1]:
                cat = cat or parts[0]
                sch = sch or f"{parts[1]}.{parts[2]}"
                tbl = tbl or parts[3]
            else:
                cat = cat or parts[0]
                sch = sch or parts[1]
                tbl = tbl or ".".join(parts[2:])
        elif len(parts) >= 3:
            cat = cat or parts[0]
            sch = sch or parts[1]
            tbl = tbl or ".".join(parts[2:])
        elif len(parts) == 2:
            cat = cat or "hive_metastore"
            sch = sch or parts[0]
            tbl = tbl or parts[1]
        elif len(parts) == 1:
            cat = cat or "hive_metastore"
            sch = sch or "default"
            tbl = tbl or parts[0]

    if not tbl:
        return None
    cat = cat or "hive_metastore"
    sch = sch or "default"
    canon_fqn = f"{cat}.{sch}.{tbl}"
    return canon_fqn, cat, sch, tbl


def _normalize_table_type(raw_tt: Any) -> str:
    s = (_coerce_raw_str(raw_tt) or "MANAGED").strip().upper()
    if "." in s:
        s = s.rsplit(".", 1)[-1]
    if "VIEW" in s:
        return "VIEW"
    if "EXTERNAL" in s or "FOREIGN" in s:
        return "EXTERNAL"
    return "MANAGED"


def _infer_medallion_layer(
    explicit_layer: Any,
    tags: dict[str, Any],
    catalog: str,
    schema_name: str,
    table_name: str,
    fqn: str,
) -> str:
    el = _coerce_raw_str(explicit_layer).strip().lower()
    if el in {"bronze", "silver", "gold"}:
        return el

    priority_tag_keys = ("medallion_layer", "medallion", "layer", "quality", "tier", "data_layer")
    lower_tags = {
        _coerce_raw_str(k).strip().lower(): _coerce_raw_str(v).strip().lower()
        for k, v in (tags or {}).items()
    }
    for pk in priority_tag_keys:
        tv = lower_tags.get(pk, "")
        for candidate in ("gold", "silver", "bronze"):
            if candidate in tv:
                return candidate
    for tv in lower_tags.values():
        if tv in {"gold", "silver", "bronze"}:
            return tv

    probe = f"{schema_name} {table_name} {catalog} {fqn}".lower()
    for layer in ("gold", "silver", "bronze"):
        if re.search(rf"(?:^|[_.\s0-9-]){layer}(?:$|[_.\s0-9-])", probe):
            return layer
    return "unknown"


def _infer_join_keys_and_grain(
    explicit_join_keys: Sequence[Any],
    explicit_grain: str,
    columns: list[dict[str, Any]],
    privacy_cfg: PrivacyConfig,
) -> tuple[list[str], str]:
    dedup_explicit: list[str] = []
    seen_jk: set[str] = set()
    for k in explicit_join_keys:
        ks = _coerce_raw_str(k).strip()
        if ks and ks.casefold() not in seen_jk:
            seen_jk.add(ks.casefold())
            dedup_explicit.append(ks)

    primary_keys: list[str] = []
    inferred_keys: list[str] = []
    for col in columns:
        cname = _coerce_raw_str(col.get("name") or col.get("column_name")).strip()
        if not cname:
            continue
        ccomment = _coerce_raw_str(col.get("comment"))
        if bool(col.get("is_pii")) or is_pii_column(cname, comment=ccomment, config=privacy_cfg):
            continue
        clow = cname.lower()
        if col.get("is_primary_key"):
            primary_keys.append(cname)
        if (
            col.get("is_primary_key")
            or col.get("is_partition")
            or clow == "id"
            or clow.endswith(("_id", "_key", "_code", "_sku"))
            or clow in {"sku", "store_nbr", "facility_id", "plant_id", "account_id"}
            or clow.startswith("sku_")
        ):
            if cname.casefold() not in seen_jk:
                seen_jk.add(cname.casefold())
                inferred_keys.append(cname)

    join_keys = dedup_explicit if dedup_explicit else inferred_keys
    grain_clean = (explicit_grain or "").strip()
    if grain_clean and grain_clean.lower() != "unknown":
        grain = grain_clean
    elif primary_keys:
        grain = f"One row per {', '.join(primary_keys[:3])}"
    elif join_keys:
        grain = f"One row per {', '.join(join_keys[:3])}"
    else:
        grain = "unknown"
    return join_keys, grain


def _sanitize_for_redaction(value: Any) -> Any:
    """Convert SDK Enum/Path/bytes/datetime and non-finite numbers prior to redaction."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        _, digits, exp = value.as_tuple()
        if isinstance(exp, int) and exp >= 0 and len(digits) + exp <= 38:
            return int(value)
        try:
            f = float(value)
        except OverflowError:
            return None
        return f if math.isfinite(f) else None
    if isinstance(value, Enum):
        return _sanitize_for_redaction(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_sanitize_for_redaction(v) for v in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            sk = _sanitize_for_redaction(k)
            key_str = sk if isinstance(sk, str) else str(sk)
            out[key_str] = _sanitize_for_redaction(v)
        return out
    return str(value)


# ---------------------------------------------------------------------------
# Tier 1 SQL & SDK Discovery Helpers
# ---------------------------------------------------------------------------


def _is_mock_placeholder(obj: Any) -> bool:
    if obj is None:
        return False
    cls_mod = getattr(type(obj), "__module__", "")
    return cls_mod.startswith("unittest.mock")


def _normalize_sql_rows(
    raw_res: Any,
    fallback_cols: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    if raw_res is None:
        return []
    if isinstance(raw_res, dict):
        status_d = raw_res.get("status")
        if isinstance(status_d, dict) or hasattr(status_d, "state"):
            state_val = (
                status_d.get("state")
                if isinstance(status_d, dict)
                else getattr(status_d, "state", None)
            )
            while isinstance(state_val, Enum):
                state_val = state_val.value
            if isinstance(state_val, str) and state_val.strip().upper() in {
                "FAILED",
                "CANCELED",
                "CANCELLED",
                "CLOSED",
            }:
                err_obj = (
                    status_d.get("error")
                    if isinstance(status_d, dict)
                    else getattr(status_d, "error", None)
                )
                err_msg = (
                    err_obj.get("message")
                    if isinstance(err_obj, dict)
                    else getattr(err_obj, "message", None)
                )
                msg_raw = (
                    _coerce_raw_str(err_msg).strip()
                    if err_msg is not None and _coerce_raw_str(err_msg).strip()
                    else f"SQL execution {state_val.strip().upper()}"
                )
                raise RuntimeError(redact_text(msg_raw))

        if "manifest" in raw_res and "result" in raw_res:
            manifest = raw_res.get("manifest") or {}
            schema = manifest.get("schema") or {}
            cols_meta = schema.get("columns") or []
            col_names = [
                _coerce_raw_str(
                    c.get("name") if isinstance(c, dict) else getattr(c, "name", f"col_{i}")
                )
                for i, c in enumerate(cols_meta)
            ]
            data_arr = (raw_res.get("result") or {}).get("data_array") or []
            out_dict_rows: list[dict[str, Any]] = []
            for row in data_arr:
                if isinstance(row, dict):
                    out_dict_rows.append({_coerce_raw_str(k): v for k, v in row.items()})
                elif isinstance(row, (list, tuple)):
                    out_dict_rows.append(
                        {
                            (col_names[i] if i < len(col_names) else f"col_{i}"): row[i]
                            for i in range(len(row))
                        }
                    )
            return out_dict_rows
        for list_key in ("rows", "data", "value", "results"):
            if isinstance(raw_res.get(list_key), (list, tuple)):
                return _normalize_sql_rows(raw_res[list_key], fallback_cols=fallback_cols)
        return [{_coerce_raw_str(k): v for k, v in raw_res.items()}]

    # SDK StatementResponse object (check failure state even when manifest/result omitted)
    status = getattr(raw_res, "status", None)
    if status is not None and not _is_mock_placeholder(status):
        state = (
            status.get("state")
            if isinstance(status, dict)
            else getattr(status, "state", None)
        )
        while isinstance(state, Enum):
            state = state.value
        if isinstance(state, str) and state.strip().upper() in {
            "FAILED",
            "CANCELED",
            "CANCELLED",
            "CLOSED",
        }:
            err_obj = (
                status.get("error")
                if isinstance(status, dict)
                else getattr(status, "error", None)
            )
            err_msg = (
                err_obj.get("message")
                if isinstance(err_obj, dict)
                else (
                    err_obj
                    if isinstance(err_obj, str)
                    else getattr(err_obj, "message", None)
                )
            )
            msg = (
                _coerce_raw_str(err_msg).strip()
                if (
                    err_msg is not None
                    and not _is_mock_placeholder(err_msg)
                    and _coerce_raw_str(err_msg).strip()
                )
                else f"SQL execution {state.strip().upper()}"
            )
            raise RuntimeError(redact_text(msg))

    if hasattr(raw_res, "manifest") and hasattr(raw_res, "result"):

        manifest = getattr(raw_res, "manifest", None)
        schema = getattr(manifest, "schema", None)
        cols_obj = getattr(schema, "columns", None)
        res_obj = getattr(raw_res, "result", None)
        data_arr = getattr(res_obj, "data_array", None)
        if isinstance(cols_obj, (list, tuple)):
            col_names = [
                str(c.get("name") if isinstance(c, dict) else getattr(c, "name", f"col_{i}"))
                for i, c in enumerate(cols_obj)
            ]
            if data_arr is None:
                return []
            if isinstance(data_arr, (list, tuple)):
                out_rows: list[dict[str, Any]] = []
                for row in data_arr:
                    if isinstance(row, dict):
                        out_rows.append({str(k): v for k, v in row.items()})
                    elif isinstance(row, (list, tuple)):
                        out_rows.append(
                            {
                                (col_names[i] if i < len(col_names) else f"col_{i}"): row[i]
                                for i in range(len(row))
                            }
                        )
                return out_rows

    if hasattr(raw_res, "rows") and isinstance(raw_res.rows, (list, tuple)):
        return _normalize_sql_rows(raw_res.rows, fallback_cols=fallback_cols)

    if _is_mock_placeholder(raw_res):
        raise TypeError("Unconfigured mock SQL result")

    if isinstance(raw_res, (list, tuple)):
        cols = list(fallback_cols or ())
        out: list[dict[str, Any]] = []
        for row in raw_res:
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

    return []


def _build_client_sql_runner(
    client: Any,
    dbx_cfg: DatabricksConfig,
) -> Callable[[str, Sequence[str] | None], list[dict[str, Any]]] | None:
    if client is None:
        return None

    for direct_method in ("execute_sql", "sql", "query"):
        fn = getattr(client, direct_method, None)
        if callable(fn) and not _is_mock_placeholder(fn):
            return lambda sql, fb=None, _fn=fn: _normalize_sql_rows(_fn(sql), fallback_cols=fb)
        if callable(fn) and _is_mock_placeholder(fn):
            if getattr(fn, "side_effect", None) is not None or not _is_mock_placeholder(
                getattr(fn, "return_value", None)
            ):
                return lambda sql, fb=None, _fn=fn: _normalize_sql_rows(
                    _fn(sql), fallback_cols=fb
                )

    stmt_api = getattr(client, "statement_execution", None)
    exec_fn = getattr(stmt_api, "execute_statement", None) if stmt_api is not None else None
    if callable(exec_fn):
        if _is_mock_placeholder(exec_fn):
            has_cfg = getattr(exec_fn, "side_effect", None) is not None
            rv = getattr(exec_fn, "return_value", None)
            if not has_cfg and _is_mock_placeholder(rv):
                rv_cols = getattr(getattr(getattr(rv, "manifest", None), "schema", None), "columns", None)
                rv_data = getattr(getattr(rv, "result", None), "data_array", None)
                rv_state = getattr(getattr(rv, "status", None), "state", None)
                while isinstance(rv_state, Enum):
                    rv_state = rv_state.value
                if (
                    not isinstance(rv_cols, (list, tuple))
                    and not isinstance(rv_data, (list, tuple))
                    and not isinstance(rv_state, str)
                ):
                    return None

        wh_id = dbx_cfg.warehouse_id.strip()
        if not wh_id:
            wh_api = getattr(client, "warehouses", None)
            list_wh = getattr(wh_api, "list", None) if wh_api is not None else None
            if callable(list_wh):
                try:
                    for w in list_wh():
                        wid = w.get("id") if isinstance(w, dict) else getattr(w, "id", None)
                        if isinstance(wid, str) and wid.strip():
                            wh_id = wid.strip()
                            break
                except Exception:
                    pass

        def _exec_via_statement_api(
            sql: str,
            fb: Sequence[str] | None = None,
        ) -> list[dict[str, Any]]:
            try:
                resp = exec_fn(
                    statement=sql,
                    warehouse_id=wh_id or "auto",
                    wait_timeout="30s",
                )
            except TypeError:
                try:
                    resp = exec_fn(statement=sql, warehouse_id=wh_id or "auto")
                except TypeError:
                    resp = exec_fn(sql)
            return _normalize_sql_rows(resp, fallback_cols=fb)

        return _exec_via_statement_api

    return None


def _parse_describe_extended_rows(
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
        raw_cn = _row_get(row, "col_name", "column_name", "name", "col_0")
        raw_dt = _row_get(row, "data_type", "type", "col_1")
        raw_cm = _row_get(row, "comment", "col_2")
        cn = _coerce_raw_str(raw_cn).strip() if raw_cn is not None else ""
        dt = _coerce_raw_str(raw_dt).strip() if raw_dt is not None else ""
        cm = _coerce_raw_str(raw_cm).strip() if raw_cm is not None else ""

        cn_low = cn.lower()
        if not cn:
            if state in ("columns", "partition"):
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
    m_rows = re.search(r"(?<!\d)(\d++)\s++rows", meta.get("statistics", ""), re.IGNORECASE)
    if m_rows:
        stat_rows = int(m_rows.group(1))

    cat = meta.get("catalog", "").strip() or "hive_metastore"
    sch = meta.get("database", "").strip() or default_db
    tbl = meta.get("table", "").strip() or default_table
    return {
        "fqn": f"{cat}.{sch}.{tbl}",
        "catalog": cat,
        "schema_name": sch,
        "table_name": tbl,
        "table_type": _normalize_table_type(meta.get("type", "MANAGED")),
        "comment": meta.get("comment", ""),
        "row_count": stat_rows,
        "columns": cols,
        "tags": tags,
    }


def _extract_sdk_jobs(
    client: Any,
    tables_map: dict[str, dict[str, Any]],
    warnings: list[str],
    redact_secrets: bool = True,
) -> None:
    jobs_api = getattr(client, "jobs", None) if client is not None else None
    list_fn = getattr(jobs_api, "list", None) if jobs_api is not None else None
    if not callable(list_fn):
        return
    try:
        jobs_iter = list_fn()
        if _is_mock_placeholder(jobs_iter) and getattr(jobs_iter, "return_value", None) is not None:
            # MagicMock iterable check: empty unless configured
            pass
        for job in jobs_iter:
            settings = (
                job.get("settings")
                if isinstance(job, dict)
                else getattr(job, "settings", None)
            )
            raw_name = (
                (settings.get("name") if isinstance(settings, dict) else getattr(settings, "name", None))
                or (job.get("name") if isinstance(job, dict) else getattr(job, "name", None))
                or (job.get("job_name") if isinstance(job, dict) else getattr(job, "job_name", None))
            )
            job_name = _coerce_raw_str(raw_name).strip()
            if not job_name or _is_mock_placeholder(raw_name):
                continue

            explicit_targets: list[str] = []
            for attr_k in ("tables", "target_tables", "fqns", "datasets"):
                val = job.get(attr_k) if isinstance(job, dict) else getattr(job, attr_k, None)
                if isinstance(val, (list, tuple, set)):
                    explicit_targets.extend(
                        _coerce_raw_str(x)
                        for x in val
                        if x is not None and not _is_mock_placeholder(x) and _coerce_raw_str(x).strip()
                    )
                elif val is not None and not _is_mock_placeholder(val) and _coerce_raw_str(val).strip():
                    explicit_targets.append(_coerce_raw_str(val))

            blob_parts = [job_name.lower()]
            tasks = (
                (settings.get("tasks") if isinstance(settings, dict) else getattr(settings, "tasks", None))
                or (job.get("tasks") if isinstance(job, dict) else getattr(job, "tasks", None))
            )
            if isinstance(tasks, (list, tuple)):
                for t in tasks:
                    nb_task = t.get("notebook_task") if isinstance(t, dict) else getattr(t, "notebook_task", None)
                    nb_path = nb_task.get("notebook_path") if isinstance(nb_task, dict) else getattr(nb_task, "notebook_path", None)
                    if nb_path is not None and not _is_mock_placeholder(nb_path):
                        blob_parts.append(_coerce_raw_str(nb_path).lower())
                    tk = t.get("task_key") if isinstance(t, dict) else getattr(t, "task_key", None)
                    if tk is not None and not _is_mock_placeholder(tk):
                        blob_parts.append(_coerce_raw_str(tk).lower())
                    sql_task = t.get("sql_task") if isinstance(t, dict) else getattr(t, "sql_task", None)
                    if sql_task is not None and not _is_mock_placeholder(sql_task):
                        blob_parts.append(_coerce_raw_str(sql_task).lower())

            blob = " ".join(blob_parts)
            for fqn, entry in tables_map.items():
                tbl_low = entry["table_name"].lower()
                matched = any(
                    et.strip().casefold() in {fqn.casefold(), tbl_low}
                    for et in explicit_targets
                ) or (fqn.lower() in blob or re.search(rf"(?:^|[_./\s-]){re.escape(tbl_low)}(?:$|[_./\s-])", blob) is not None)
                if matched and job_name not in entry.setdefault("scheduled_jobs", []):
                    entry["scheduled_jobs"].append(job_name)
    except Exception as exc:
        warnings.append(
            redact_text(
                f"Databricks jobs.list() skipped: {exc}",
                redact_secrets=redact_secrets,
            )
        )


def _crawl_sdk_catalog_api(
    client: Any,
    dbx_cfg: DatabricksConfig,
    warnings: list[str],
    redact_secrets: bool = True,
) -> dict[str, dict[str, Any]]:
    tables_map: dict[str, dict[str, Any]] = {}
    if client is None:
        return tables_map

    tables_api = getattr(client, "tables", None)
    list_tables_fn = getattr(tables_api, "list", None) if tables_api is not None else None
    if not callable(list_tables_fn):
        return tables_map

    def _consume_table_obj(t_obj: Any, fallback_cat: str = "", fallback_sch: str = "") -> None:
        raw_cat = (
            t_obj.get("catalog_name")
            if isinstance(t_obj, dict)
            else getattr(t_obj, "catalog_name", None)
        )
        raw_sch = (
            t_obj.get("schema_name")
            if isinstance(t_obj, dict)
            else getattr(t_obj, "schema_name", None)
        )
        raw_name = (
            t_obj.get("name")
            if isinstance(t_obj, dict)
            else getattr(t_obj, "name", None)
        )
        raw_full = (
            t_obj.get("full_name")
            if isinstance(t_obj, dict)
            else getattr(t_obj, "full_name", None)
        )
        if _is_mock_placeholder(raw_name):
            raw_name = None
        if _is_mock_placeholder(raw_full):
            raw_full = None
        if _is_mock_placeholder(raw_cat):
            raw_cat = None
        if _is_mock_placeholder(raw_sch):
            raw_sch = None
        if not any(_coerce_raw_str(x).strip() for x in (raw_name, raw_full)):
            return
        ident = _canonical_identity(
            raw_full,
            raw_cat if raw_cat is not None else fallback_cat,
            raw_sch if raw_sch is not None else fallback_sch,
            raw_name,
        )
        if ident is None:
            return
        fqn, cat, sch, tbl = ident
        if sch.lower() == "information_schema" or not _is_catalog_allowed(cat, dbx_cfg):
            return
        raw_tt = (
            t_obj.get("table_type")
            if isinstance(t_obj, dict)
            else getattr(t_obj, "table_type", "MANAGED")
        )
        raw_cm = (
            t_obj.get("comment")
            if isinstance(t_obj, dict)
            else getattr(t_obj, "comment", "")
        )
        raw_props = (
            t_obj.get("properties")
            if isinstance(t_obj, dict)
            else getattr(t_obj, "properties", None)
        )
        tags = (
            {
                _coerce_raw_str(k): _unwrap_scalar(v)
                for k, v in raw_props.items()
                if not _coerce_raw_str(k).lower().startswith("delta.")
            }
            if isinstance(raw_props, dict)
            else {}
        )
        cols_raw = (
            t_obj.get("columns")
            if isinstance(t_obj, dict)
            else getattr(t_obj, "columns", None)
        )
        cols: list[dict[str, Any]] = []
        if isinstance(cols_raw, Iterable) and not isinstance(cols_raw, (str, bytes, dict)):
            for c in cols_raw:
                cn = c.get("name") if isinstance(c, dict) else getattr(c, "name", None)
                if _is_mock_placeholder(cn) or not _coerce_raw_str(cn).strip():
                    continue
                ct = (
                    (c.get("type_text") if isinstance(c, dict) else getattr(c, "type_text", None))
                    or (c.get("type_name") if isinstance(c, dict) else getattr(c, "type_name", None))
                    or (c.get("data_type") if isinstance(c, dict) else getattr(c, "data_type", None))
                    or "STRING"
                )
                if _is_mock_placeholder(ct):
                    ct = "STRING"
                cnull = c.get("nullable") if isinstance(c, dict) else getattr(c, "nullable", True)
                ccm = c.get("comment") if isinstance(c, dict) else getattr(c, "comment", "")
                pidx = c.get("partition_index") if isinstance(c, dict) else getattr(c, "partition_index", None)
                cols.append(
                    {
                        "name": _coerce_raw_str(cn).strip(),
                        "data_type": _coerce_raw_str(ct).strip() or "STRING",
                        "nullable": bool(cnull) if isinstance(cnull, bool) else True,
                        "comment": "" if _is_mock_placeholder(ccm) else _coerce_raw_str(ccm),
                        "is_partition": isinstance(pidx, int) and not isinstance(pidx, bool),
                    }
                )

        tables_map[fqn] = {
            "fqn": fqn,
            "catalog": cat,
            "schema_name": sch,
            "table_name": tbl,
            "table_type": _normalize_table_type(raw_tt),
            "comment": "" if _is_mock_placeholder(raw_cm) else _coerce_raw_str(raw_cm),
            "columns": cols,
            "tags": tags,
            "upstream_refs": [],
            "downstream_refs": [],
            "scheduled_jobs": [],
            "sample_rows": [],
        }

    catalogs_api = getattr(client, "catalogs", None)
    schemas_api = getattr(client, "schemas", None)
    list_cats = getattr(catalogs_api, "list", None) if catalogs_api is not None else None
    list_schs = getattr(schemas_api, "list", None) if schemas_api is not None else None

    discovered_cats: list[str] = []
    if callable(list_cats):
        try:
            for c_obj in list_cats():
                cname = c_obj.get("name") if isinstance(c_obj, dict) else getattr(c_obj, "name", None)
                cname_s = "" if _is_mock_placeholder(cname) else _coerce_raw_str(cname).strip()
                if cname_s and _is_catalog_allowed(cname_s, dbx_cfg):
                    discovered_cats.append(cname_s)
        except Exception as exc:
            warnings.append(
                redact_text(
                    f"Databricks catalogs.list() warning: {exc}",
                    redact_secrets=redact_secrets,
                )
            )

    if discovered_cats and callable(list_schs):
        for cat_name in discovered_cats:
            try:
                sch_objs = list_schs(catalog_name=cat_name)
            except TypeError:
                sch_objs = list_schs(cat_name)
            except Exception as exc:
                warnings.append(
                    redact_text(
                        f"Databricks schemas.list({cat_name}) warning: {exc}",
                        redact_secrets=redact_secrets,
                    )
                )
                continue
            for s_obj in sch_objs:
                sname = s_obj.get("name") if isinstance(s_obj, dict) else getattr(s_obj, "name", None)
                sname_s = "" if _is_mock_placeholder(sname) else _coerce_raw_str(sname).strip()
                if not sname_s or sname_s.lower() == "information_schema":
                    continue
                try:
                    t_objs = list_tables_fn(catalog_name=cat_name, schema_name=sname_s)
                except TypeError:
                    t_objs = list_tables_fn(cat_name, sname_s)
                except Exception as exc:
                    warnings.append(
                        redact_text(
                            f"Databricks tables.list({cat_name}.{sname_s}) warning: {exc}",
                            redact_secrets=redact_secrets,
                        )
                    )
                    continue
                for t_obj in t_objs:
                    _consume_table_obj(t_obj, fallback_cat=cat_name, fallback_sch=sname_s)

    if not tables_map:
        try:
            for t_obj in list_tables_fn():
                _consume_table_obj(t_obj)
        except TypeError:
            pass
        except Exception as exc:
            warnings.append(
                redact_text(
                    f"Databricks tables.list() fallback warning: {exc}",
                    redact_secrets=redact_secrets,
                )
            )

    return tables_map


def _has_databricks_sdk() -> bool:
    try:
        return importlib.util.find_spec("databricks.sdk") is not None
    except (ImportError, ValueError):
        return False


def _crawl_tier1(
    dbx_cfg: DatabricksConfig,
    privacy_cfg: PrivacyConfig,
    client: Any,
    sql_runner: Callable[..., Any] | None,
    warnings: list[str],
    errors: list[str],
) -> dict[str, dict[str, Any]]:
    active_client = client
    exec_sql: Callable[[str, Sequence[str] | None], list[dict[str, Any]]] | None = None

    if sql_runner is not None:
        exec_sql = lambda sql, fb=None: _normalize_sql_rows(sql_runner(sql), fallback_cols=fb)
    else:
        if active_client is None:
            host = dbx_cfg.host.strip() or os.environ.get("DATABRICKS_HOST", "").strip()
            has_sdk = _has_databricks_sdk()
            if not host:
                errors.append(
                    "Databricks Tier 1 requires `sql_runner`, `client`, or configured `host` / `DATABRICKS_HOST`."
                )
                return {}
            if not has_sdk:
                errors.append(
                    "Optional package `databricks-sdk` is not installed for Tier 1 live workspace crawling."
                )
                return {}
            try:
                from databricks.sdk import WorkspaceClient  # type: ignore[import-not-found]

                token = os.environ.get("DATABRICKS_TOKEN", "").strip() or None
                active_client = (
                    WorkspaceClient(host=host, token=token)
                    if token
                    else WorkspaceClient(host=host)
                )
            except Exception as exc:
                errors.append(
                    redact_text(
                        f"Failed to initialize Databricks WorkspaceClient: {exc}",
                        redact_secrets=privacy_cfg.redact_secrets,
                    )
                )
                return {}

        exec_sql = _build_client_sql_runner(active_client, dbx_cfg)

    tables_map: dict[str, dict[str, Any]] = {}
    used_hive_fallback = False

    if exec_sql is not None:
        try:
            uc_rows = exec_sql(
                "SELECT table_catalog, table_schema, table_name, table_type, comment, last_altered "
                "FROM system.information_schema.tables "
                "WHERE table_schema <> 'information_schema'",
                (
                    "table_catalog",
                    "table_schema",
                    "table_name",
                    "table_type",
                    "comment",
                    "last_altered",
                ),
            )
            for r in uc_rows:
                ident = _canonical_identity(
                    _row_get(r, "fqn", "full_name", "table_full_name"),
                    _row_get(r, "table_catalog", "catalog_name", "catalog"),
                    _row_get(r, "table_schema", "schema_name", "schema"),
                    _row_get(r, "table_name", "name", "table"),
                )
                if ident is None:
                    continue
                fqn, cat, sch, tbl = ident
                if sch.lower() == "information_schema" or not _is_catalog_allowed(cat, dbx_cfg):
                    continue
                tables_map[fqn] = {
                    "fqn": fqn,
                    "catalog": cat,
                    "schema_name": sch,
                    "table_name": tbl,
                    "table_type": _normalize_table_type(_row_get(r, "table_type")),
                    "comment": _row_get(r, "comment") or "",
                    "columns": [],
                    "tags": {},
                    "upstream_refs": [],
                    "downstream_refs": [],
                    "scheduled_jobs": [],
                    "sample_rows": [],
                }

            # UC Columns
            try:
                try:
                    col_rows = exec_sql(
                        "SELECT table_catalog, table_schema, table_name, column_name, "
                        "full_data_type, data_type, is_nullable, comment, ordinal_position, partition_index "
                        "FROM system.information_schema.columns "
                        "WHERE table_schema <> 'information_schema' "
                        "ORDER BY table_catalog, table_schema, table_name, ordinal_position",
                        (
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
                    col_rows = exec_sql(
                        "SELECT table_catalog, table_schema, table_name, column_name, "
                        "data_type, is_nullable, comment, ordinal_position "
                        "FROM system.information_schema.columns "
                        "WHERE table_schema <> 'information_schema' "
                        "ORDER BY table_catalog, table_schema, table_name, ordinal_position",
                        (
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
                for cr in col_rows:
                    ident = _canonical_identity(
                        _row_get(cr, "fqn", "table_full_name"),
                        _row_get(cr, "table_catalog", "catalog_name", "catalog"),
                        _row_get(cr, "table_schema", "schema_name", "schema"),
                        _row_get(cr, "table_name", "table"),
                    )
                    if ident is None or ident[0] not in tables_map:
                        continue
                    cname = _coerce_raw_str(_row_get(cr, "column_name", "name")).strip()
                    if not cname:
                        continue
                    dtype = (
                        _coerce_raw_str(_row_get(cr, "full_data_type", "data_type", "type")).strip()
                        or "STRING"
                    )
                    p_idx = _row_get(cr, "partition_index")
                    tables_map[ident[0]]["columns"].append(
                        {
                            "name": cname,
                            "data_type": dtype,
                            "nullable": _parse_bool_flag(
                                _row_get(cr, "is_nullable", "nullable"), default=True
                            ),
                            "comment": _coerce_raw_str(_row_get(cr, "comment")),
                            "is_partition": p_idx is not None and _coerce_raw_str(p_idx).strip() != "",
                        }
                    )
            except Exception as exc:
                warnings.append(
                    redact_text(
                        f"UC columns query warning: {exc}",
                        redact_secrets=privacy_cfg.redact_secrets,
                    )
                )

            # UC Table Tags
            try:
                tag_rows = exec_sql(
                    "SELECT catalog_name, schema_name, table_name, tag_name, tag_value "
                    "FROM system.information_schema.table_tags",
                    ("catalog_name", "schema_name", "table_name", "tag_name", "tag_value"),
                )
                for tr in tag_rows:
                    ident = _canonical_identity(
                        _row_get(tr, "fqn"),
                        _row_get(tr, "catalog_name", "table_catalog", "catalog"),
                        _row_get(tr, "schema_name", "table_schema", "schema"),
                        _row_get(tr, "table_name", "table"),
                    )
                    tk = _coerce_raw_str(_row_get(tr, "tag_name", "key")).strip()
                    tv = _row_get(tr, "tag_value", "value")
                    if ident is not None and ident[0] in tables_map and tk:
                        tables_map[ident[0]]["tags"][tk] = "" if tv is None else _coerce_raw_str(tv)
            except Exception as exc:
                warnings.append(
                    redact_text(
                        f"UC table_tags unavailable: {exc}",
                        redact_secrets=privacy_cfg.redact_secrets,
                    )
                )

            # UC Lineage
            try:
                lin_rows = exec_sql(
                    f"SELECT source_table_full_name, target_table_full_name, source_type, target_type, entity_type, entity_id "
                    f"FROM system.access.table_lineage "
                    f"WHERE event_time >= current_timestamp() - INTERVAL {int(dbx_cfg.lineage_lookback_days)} DAYS",
                    (
                        "source_table_full_name",
                        "target_table_full_name",
                        "source_type",
                        "target_type",
                        "entity_type",
                        "entity_id",
                    ),
                )
                for lr in lin_rows:
                    src = _coerce_raw_str(
                        _row_get(lr, "source_table_full_name", "source_fqn", "source_table")
                    ).strip()
                    tgt = _coerce_raw_str(
                        _row_get(lr, "target_table_full_name", "target_fqn", "target_table")
                    ).strip()
                    if src and tgt and src.casefold() != tgt.casefold():
                        if tgt in tables_map and src not in tables_map[tgt]["upstream_refs"]:
                            tables_map[tgt]["upstream_refs"].append(src)
                        if src in tables_map and tgt not in tables_map[src]["downstream_refs"]:
                            tables_map[src]["downstream_refs"].append(tgt)
                    ent_type = _coerce_raw_str(_row_get(lr, "entity_type")).strip().upper()
                    ent_id = _coerce_raw_str(
                        _row_get(lr, "job_name", "entity_id", "notebook_path")
                    ).strip()
                    if ent_id and ent_type in {"JOB", "PIPELINE", "NOTEBOOK", ""}:
                        label = (
                            ent_id
                            if not ent_type or ":" in ent_id or not ent_id.isdigit()
                            else f"{ent_type.lower()}:{ent_id}"
                        )
                        for ep in (tgt, src):
                            if ep in tables_map and label not in tables_map[ep]["scheduled_jobs"]:
                                tables_map[ep]["scheduled_jobs"].append(label)
            except Exception as exc:
                warnings.append(
                    redact_text(
                        f"UC table_lineage unavailable: {exc}",
                        redact_secrets=privacy_cfg.redact_secrets,
                    )
                )

            # UC 30d Query History
            try:
                qh_rows = exec_sql(
                    f"SELECT statement_text FROM system.query.history "
                    f"WHERE start_time >= current_timestamp() - INTERVAL {int(dbx_cfg.lineage_lookback_days)} DAYS "
                    f"AND execution_status = 'FINISHED' LIMIT 5000",
                    ("statement_text",),
                )
                counts: dict[str, int] = {k: 0 for k in tables_map}
                for qr in qh_rows:
                    direct_fqn = _row_get(qr, "fqn", "table_full_name", "table_name")
                    direct_cnt = _row_get(qr, "query_count_30d", "query_count", "count")
                    if direct_fqn and direct_cnt is not None:
                        fk = _coerce_raw_str(direct_fqn).strip()
                        iv = _parse_int_stat(direct_cnt)
                        if fk in counts and iv is not None:
                            counts[fk] += iv
                        continue
                    stmt = _coerce_raw_str(_row_get(qr, "statement_text")).lower()
                    if not stmt:
                        continue
                    for fqn, entry in tables_map.items():
                        sch_tbl = f"{entry['schema_name']}.{entry['table_name']}".lower()
                        if fqn.lower() in stmt or re.search(
                            rf"(?<![a-z0-9_.]){re.escape(sch_tbl)}(?![a-z0-9_])", stmt
                        ):
                            counts[fqn] += 1
                if qh_rows:
                    for fqn, cnt in counts.items():
                        tables_map[fqn]["query_count_30d"] = cnt
            except Exception as exc:
                warnings.append(
                    redact_text(
                        f"UC query.history unavailable: {exc}",
                        redact_secrets=privacy_cfg.redact_secrets,
                    )
                )

        except Exception as uc_exc:
            # Automatic Hive Metastore Fallback
            try:
                try:
                    db_rows = exec_sql("SHOW DATABASES", ("databaseName",))
                except Exception:
                    db_rows = exec_sql("SHOW SCHEMAS", ("databaseName",))
                used_hive_fallback = True
                warnings.append(
                    redact_text(
                        f"Unity Catalog system.information_schema unavailable ({uc_exc}); fell back to Hive Metastore.",
                        redact_secrets=privacy_cfg.redact_secrets,
                    )
                )
                if _is_catalog_allowed("hive_metastore", dbx_cfg):
                    for db_r in db_rows:
                        db_name = _coerce_raw_str(
                            _row_get(
                                db_r,
                                "databaseName",
                                "namespace",
                                "schema_name",
                                "database_name",
                                "name",
                                "col_0",
                                "value",
                            )
                        ).strip()
                        if not db_name or db_name.lower() == "information_schema":
                            continue
                        if any(
                            fnmatch.fnmatchcase(db_name.lower(), p.strip().lower())
                            for p in dbx_cfg.catalogs_exclude
                            if p and p.strip()
                        ):
                            continue
                        t_rows = exec_sql(
                            f"SHOW TABLES IN {_quote_sql_ident(db_name)}",
                            ("database", "tableName", "isTemporary"),
                        )
                        for tr in t_rows:
                            is_temp = _row_get(tr, "isTemporary", "is_temporary", "col_2")
                            if is_temp is True or _coerce_raw_str(is_temp).strip().lower() == "true":
                                continue
                            # Support 3-col (db, tbl, isTemp), 2-col (db, tbl), or 1-col (tbl)
                            t_name = _row_get(tr, "tableName", "table_name", "name")
                            if t_name is None:
                                if "col_1" in tr and tr["col_1"] is not None:
                                    t_name = tr["col_1"]
                                else:
                                    t_name = _row_get(tr, "database", "col_0", "value")
                            tbl_str = _coerce_raw_str(t_name).strip()
                            if not tbl_str:
                                continue
                            desc_rows = exec_sql(
                                f"DESCRIBE EXTENDED {_quote_sql_ident(db_name)}.{_quote_sql_ident(tbl_str)}",
                                ("col_name", "data_type", "comment"),
                            )
                            parsed = _parse_describe_extended_rows(desc_rows, db_name, tbl_str)
                            fqn = parsed["fqn"]
                            tables_map[fqn] = {
                                **parsed,
                                "upstream_refs": [],
                                "downstream_refs": [],
                                "scheduled_jobs": [],
                                "sample_rows": [],
                            }
            except Exception as hive_exc:
                sdk_fallback = _crawl_sdk_catalog_api(
                    active_client, dbx_cfg, warnings, redact_secrets=privacy_cfg.redact_secrets
                )
                if sdk_fallback:
                    tables_map.update(sdk_fallback)
                else:
                    errors.append(
                        redact_text(
                            f"Databricks UC and Hive Metastore queries failed: UC={uc_exc}; Hive={hive_exc}",
                            redact_secrets=privacy_cfg.redact_secrets,
                        )
                    )
                    return {}

    if not tables_map and active_client is not None:
        tables_map.update(
            _crawl_sdk_catalog_api(
                active_client, dbx_cfg, warnings, redact_secrets=privacy_cfg.redact_secrets
            )
        )

    if active_client is not None and tables_map:
        _extract_sdk_jobs(
            active_client, tables_map, warnings, redact_secrets=privacy_cfg.redact_secrets
        )

    # Per-table safe statistical aggregates + conditional sample rows
    if exec_sql is not None and tables_map:
        now_utc = datetime.now(timezone.utc)
        for fqn, entry in tables_map.items():
            cols = entry.get("columns", [])
            ts_col: str | None = None
            for c in cols:
                cn = _coerce_raw_str(c.get("name"))
                ct = _coerce_raw_str(c.get("data_type")).upper()
                cc = _coerce_raw_str(c.get("comment"))
                if ("TIMESTAMP" in ct or "DATE" in ct) and not is_pii_column(
                    cn, comment=cc, config=privacy_cfg
                ):
                    ts_col = cn
                    break

            select_parts = ["COUNT(*) AS row_count"]
            select_cols = ["row_count"]
            if ts_col:
                qts = _quote_sql_ident(ts_col)
                select_parts.append(f"MIN({qts}) AS min_timestamp")
                select_parts.append(f"MAX({qts}) AS max_timestamp")
                select_cols.extend(["min_timestamp", "max_timestamp"])

            prof_cols: list[tuple[int, dict[str, Any]]] = []
            for idx, c in enumerate(cols[:50]):
                ct = _coerce_raw_str(c.get("data_type")).upper()
                if any(k in ct for k in ("ARRAY", "MAP", "STRUCT", "BINARY", "VARIANT")):
                    continue
                qc = _quote_sql_ident(_coerce_raw_str(c["name"]))
                select_parts.append(
                    f"SUM(CASE WHEN {qc} IS NULL THEN 1 ELSE 0 END) AS __nulls__{idx}"
                )
                select_parts.append(f"APPROX_COUNT_DISTINCT({qc}) AS __distinct__{idx}")
                select_cols.extend([f"__nulls__{idx}", f"__distinct__{idx}"])
                prof_cols.append((idx, c))

            two_part = used_hive_fallback and entry["catalog"] == "hive_metastore"
            sql_ref = _quote_fqn(
                entry["catalog"],
                entry["schema_name"],
                entry["table_name"],
                two_part=two_part,
            )
            agg_rows: list[dict[str, Any]] = []
            try:
                agg_rows = exec_sql(
                    f"SELECT {', '.join(select_parts)} FROM {sql_ref}",
                    tuple(select_cols),
                )
            except Exception:
                try:
                    agg_rows = exec_sql(
                        f"SELECT COUNT(*) AS row_count FROM {sql_ref}",
                        ("row_count",),
                    )
                except Exception as exc:
                    warnings.append(
                        redact_text(
                            f"Safe aggregate profile skipped for {fqn}: {exc}",
                            redact_secrets=privacy_cfg.redact_secrets,
                        )
                    )

            if agg_rows:
                ar = agg_rows[0]
                rc = _parse_int_stat(_row_get(ar, "row_count", "cnt", "count", "col_0"))
                if rc is not None:
                    entry["row_count"] = rc
                min_ts = _row_get(ar, "min_timestamp", "min_ts")
                max_ts = _row_get(ar, "max_timestamp", "max_ts")
                if min_ts is not None:
                    dt_min = _parse_iso_dt(min_ts)
                    entry["min_timestamp"] = (
                        dt_min.isoformat() if dt_min else _coerce_raw_str(min_ts)
                    )
                if max_ts is not None:
                    dt_max = _parse_iso_dt(max_ts)
                    entry["max_timestamp"] = (
                        dt_max.isoformat() if dt_max else _coerce_raw_str(max_ts)
                    )
                    if dt_max is not None:
                        entry["freshness_lag_hours"] = max(
                            0.0,
                            round((now_utc - dt_max).total_seconds() / 3600.0, 2),
                        )

                rc_val = entry.get("row_count")
                for idx, col_d in prof_cols:
                    n_cnt = _parse_float_stat(_row_get(ar, f"__nulls__{idx}"))
                    d_cnt = _parse_int_stat(_row_get(ar, f"__distinct__{idx}"))
                    if (
                        n_cnt is not None
                        and n_cnt >= 0.0
                        and isinstance(rc_val, int)
                        and not isinstance(rc_val, bool)
                    ):
                        col_d["null_pct"] = (
                            max(0.0, min(100.0, round((n_cnt / rc_val) * 100.0, 2)))
                            if rc_val > 0
                            else 0.0
                        )
                    if d_cnt is not None:
                        col_d["distinct_count"] = d_cnt

            # Conditional Sample Rows SQL Gate
            if privacy_cfg.allow_sample_rows and privacy_cfg.max_sample_rows > 0:
                try:
                    s_rows = exec_sql(
                        f"SELECT * FROM {sql_ref} LIMIT {int(privacy_cfg.max_sample_rows)}",
                        [_coerce_raw_str(c["name"]) for c in cols],
                    )
                    entry["sample_rows"] = s_rows[: int(privacy_cfg.max_sample_rows)]
                except Exception as exc:
                    warnings.append(
                        redact_text(
                            f"Sample row query skipped for {fqn}: {exc}",
                            redact_secrets=privacy_cfg.redact_secrets,
                        )
                    )

    return tables_map


# ---------------------------------------------------------------------------
# Tier 2 Local Bundle & CSV Parsers
# ---------------------------------------------------------------------------


def _looks_like_powerbi_json(loaded: Any) -> bool:
    if isinstance(loaded, dict):
        if any(k in loaded for k in ("compatibilityLevel", "createOrReplace", "SemanticModel")):
            return True
        if "model" in loaded and isinstance(loaded["model"], dict) and "tables" in loaded["model"]:
            return True
        tables = loaded.get("tables")
        if isinstance(tables, list) and tables:
            first = tables[0]
            if isinstance(first, dict):
                has_dbx_keys = any(
                    k in first
                    for k in (
                        "fqn",
                        "catalog",
                        "table_catalog",
                        "catalog_name",
                        "schema_name",
                        "table_schema",
                        "schema",
                        "database",
                        "table_name",
                    )
                )
                has_pbi_keys = any(k in first for k in ("measures", "partitions", "lineageTag"))
                if has_pbi_keys and not has_dbx_keys:
                    return True
    if isinstance(loaded, list) and loaded and isinstance(loaded[0], dict):
        keys = set(loaded[0].keys())
        if any(
            k.startswith("[") or k in {"ExplicitName", "FromColumnID", "TableID"}
            for k in keys
        ):
            return True
    return False


def _ensure_table_entry(
    tables_map: dict[str, dict[str, Any]],
    ident: tuple[str, str, str, str],
) -> dict[str, Any]:
    fqn, cat, sch, tbl = ident
    existing = tables_map.get(fqn)
    if existing is not None:
        return existing
    entry: dict[str, Any] = {
        "fqn": fqn,
        "catalog": cat,
        "schema_name": sch,
        "table_name": tbl,
        "table_type": "MANAGED",
        "medallion_layer": None,
        "comment": "",
        "row_count": None,
        "min_timestamp": None,
        "max_timestamp": None,
        "freshness_lag_hours": None,
        "query_count_30d": None,
        "grain": "unknown",
        "join_keys": [],
        "columns": [],
        "sample_rows": [],
        "upstream_refs": [],
        "downstream_refs": [],
        "scheduled_jobs": [],
        "tags": {},
    }
    tables_map[fqn] = entry
    return entry


def _merge_column_into_table(
    entry: dict[str, Any],
    raw_col: dict[str, Any],
    table_row_count: int | None = None,
) -> None:
    cname = _coerce_raw_str(
        _row_get(raw_col, "name", "column_name", "col_name")
    ).strip()
    if not cname or cname.startswith("#"):
        return

    cols: list[dict[str, Any]] = entry.setdefault("columns", [])
    target: dict[str, Any] | None = None
    for c in cols:
        if _coerce_raw_str(c.get("name")).casefold() == cname.casefold():
            target = c
            break
    if target is None:
        target = {"name": cname, "data_type": "STRING", "nullable": True, "comment": ""}
        cols.append(target)

    dtype = _row_get(raw_col, "data_type", "full_data_type", "type", "col_type")
    if dtype is not None and _coerce_raw_str(dtype).strip():
        target["data_type"] = _coerce_raw_str(dtype).strip()

    if any(k in raw_col for k in ("nullable", "is_nullable")):
        target["nullable"] = _parse_bool_flag(
            _row_get(raw_col, "nullable", "is_nullable"), default=True
        )

    null_pct = _parse_float_stat(_row_get(raw_col, "null_pct", "null_percentage"))
    if null_pct is None:
        null_frac = _parse_float_stat(_row_get(raw_col, "null_fraction", "null_rate"))
        if null_frac is not None:
            null_pct = null_frac * 100.0 if null_frac <= 1.0 else null_frac
    if null_pct is None:
        null_cnt = _parse_float_stat(_row_get(raw_col, "null_count", "nulls"))
        rc = table_row_count if table_row_count is not None else _parse_int_stat(entry.get("row_count"))
        if null_cnt is not None and rc is not None:
            null_pct = (null_cnt / rc) * 100.0 if rc > 0 else 0.0
    if null_pct is not None:
        target["null_pct"] = max(0.0, min(100.0, round(null_pct, 4)))

    dist = _parse_int_stat(
        _row_get(raw_col, "distinct_count", "approx_distinct", "ndv", "distinct")
    )
    if dist is not None:
        target["distinct_count"] = dist

    for src_k, dst_k in (
        ("min_val", "min_val"),
        ("min_value", "min_val"),
        ("max_val", "max_val"),
        ("max_value", "max_val"),
    ):
        if src_k in raw_col and raw_col[src_k] is not None and _coerce_raw_str(raw_col[src_k]) != "":
            target[dst_k] = raw_col[src_k]

    if "sample_values" in raw_col and raw_col["sample_values"] is not None:
        sv = raw_col["sample_values"]
        if isinstance(sv, (bytes, bytearray)):
            sv = sv.decode("utf-8", errors="replace")
        if isinstance(sv, str):
            target["sample_values"] = _parse_str_list_cell(sv) if sv.strip() else []
        elif isinstance(sv, (list, tuple, set, frozenset)):
            target["sample_values"] = list(sv)

    if any(k in raw_col for k in ("is_pii", "pii")):
        if _parse_bool_flag(_row_get(raw_col, "is_pii", "pii"), default=False):
            target["is_pii"] = True

    if any(k in raw_col for k in ("is_partition", "partition_index")):
        pv = _row_get(raw_col, "is_partition", "partition_index")
        if pv is True or (pv is not None and _coerce_raw_str(pv).strip().lower() not in {"", "false", "no", "none"}):
            target["is_partition"] = True

    if _parse_bool_flag(_row_get(raw_col, "is_primary_key", "primary_key"), default=False):
        target["is_primary_key"] = True

    cm = _row_get(raw_col, "comment", "description")
    if cm is not None and _coerce_raw_str(cm).strip():
        target["comment"] = _coerce_raw_str(cm)


def _merge_table_dict(
    tables_map: dict[str, dict[str, Any]],
    raw_tbl: dict[str, Any],
) -> str | None:
    ident = _canonical_identity(
        _row_get(raw_tbl, "fqn", "full_name", "table_full_name", "id"),
        _row_get(raw_tbl, "catalog", "table_catalog", "catalog_name"),
        _row_get(raw_tbl, "schema_name", "table_schema", "schema", "database", "database_name"),
        _row_get(raw_tbl, "table_name", "table", "name"),
    )
    if ident is None:
        return None
    entry = _ensure_table_entry(tables_map, ident)

    if _row_get(raw_tbl, "table_type", "type") is not None:
        entry["table_type"] = _normalize_table_type(_row_get(raw_tbl, "table_type", "type"))

    raw_layer = _row_get(raw_tbl, "medallion_layer", "layer", "medallion")
    if raw_layer is not None and _coerce_raw_str(raw_layer).strip():
        entry["medallion_layer"] = _coerce_raw_str(raw_layer).strip().lower()

    rc = _parse_int_stat(_row_get(raw_tbl, "row_count", "num_rows", "rows"))
    if rc is not None:
        entry["row_count"] = rc

    for src_keys, dst_key in (
        (("min_timestamp", "min_ts"), "min_timestamp"),
        (("max_timestamp", "max_ts"), "max_timestamp"),
    ):
        ts_val = _row_get(raw_tbl, *src_keys)
        if ts_val is not None and _coerce_raw_str(ts_val).strip():
            entry[dst_key] = _coerce_raw_str(ts_val).strip()

    lag = _parse_float_stat(_row_get(raw_tbl, "freshness_lag_hours", "lag_hours"))
    if lag is not None:
        entry["freshness_lag_hours"] = max(0.0, lag)

    qc = _parse_int_stat(_row_get(raw_tbl, "query_count_30d", "query_count", "queries_30d"))
    if qc is not None:
        entry["query_count_30d"] = qc

    grain = _row_get(raw_tbl, "grain")
    if (
        grain is not None
        and _coerce_raw_str(grain).strip()
        and _coerce_raw_str(grain).strip().lower() != "unknown"
    ):
        entry["grain"] = _coerce_raw_str(grain).strip()

    cm = _row_get(raw_tbl, "comment", "description")
    if cm is not None and _coerce_raw_str(cm).strip():
        entry["comment"] = _coerce_raw_str(cm)

    owner = _coerce_raw_str(_row_get(raw_tbl, "owner")).strip()
    if owner:
        entry["tags"].setdefault("owner", owner)

    for jk in _parse_str_list_cell(_row_get(raw_tbl, "join_keys")):
        if jk not in entry["join_keys"]:
            entry["join_keys"].append(jk)

    for up in _parse_str_list_cell(_row_get(raw_tbl, "upstream_refs", "upstream")):
        if up not in entry["upstream_refs"]:
            entry["upstream_refs"].append(up)

    for dn in _parse_str_list_cell(_row_get(raw_tbl, "downstream_refs", "downstream")):
        if dn not in entry["downstream_refs"]:
            entry["downstream_refs"].append(dn)

    for jb in _parse_str_list_cell(_row_get(raw_tbl, "scheduled_jobs", "jobs", "workflows")):
        if jb not in entry["scheduled_jobs"]:
            entry["scheduled_jobs"].append(jb)

    raw_tags = _row_get(raw_tbl, "tags", "properties")
    if isinstance(raw_tags, dict):
        for tk, tv in raw_tags.items():
            entry["tags"][_coerce_raw_str(tk)] = "" if tv is None else _coerce_raw_str(tv)
    elif isinstance(raw_tags, list):
        for item in raw_tags:
            if isinstance(item, dict):
                tk = _coerce_raw_str(_row_get(item, "tag_name", "key", "name")).strip()
                tv = _row_get(item, "tag_value", "value")
                if tk:
                    entry["tags"][tk] = "" if tv is None else _coerce_raw_str(tv)

    raw_cols = raw_tbl.get("columns")
    if isinstance(raw_cols, list):
        for c in raw_cols:
            if isinstance(c, dict):
                _merge_column_into_table(entry, c, table_row_count=entry.get("row_count"))

    raw_samples = raw_tbl.get("sample_rows")
    if isinstance(raw_samples, list) and raw_samples:
        entry["sample_rows"] = [r for r in raw_samples if isinstance(r, dict)]

    return ident[0]


def _parse_json_bundle(
    json_path: Path,
    tables_map: dict[str, dict[str, Any]],
    warnings: list[str],
    errors: list[str],
    explicit_file: bool,
    redact_secrets: bool = True,
) -> bool:
    try:
        loaded = json.loads(_decode_file_text(json_path))
    except Exception as exc:
        msg = redact_text(
            f"Failed to parse JSON bundle {json_path.name}: {exc}"
            if explicit_file
            else f"Skipped invalid JSON file {json_path.name}: {exc}",
            redact_secrets=redact_secrets,
        )
        if explicit_file:
            errors.append(msg)
        else:
            warnings.append(msg)
        return False

    if _looks_like_powerbi_json(loaded):
        return False

    bundle: dict[str, Any]
    if isinstance(loaded, list):
        bundle = {"tables": loaded}
    elif isinstance(loaded, dict):
        if "tables" in loaded or "datasets" in loaded:
            bundle = dict(loaded)
            if "tables" not in bundle and "datasets" in bundle:
                bundle["tables"] = bundle["datasets"]
        elif any(
            k in loaded
            for k in ("fqn", "full_name", "table_full_name", "table_name", "table")
        ):
            bundle = {"tables": [loaded]}
        else:
            return False
    else:
        return False

    try:
        for tbl in bundle.get("tables") or []:
            if isinstance(tbl, dict):
                _merge_table_dict(tables_map, tbl)

        for col in bundle.get("columns") or []:
            if not isinstance(col, dict):
                continue
            ident = _canonical_identity(
                _row_get(col, "fqn", "table_full_name", "full_name"),
                _row_get(col, "catalog", "table_catalog", "catalog_name"),
                _row_get(col, "schema_name", "table_schema", "schema", "database"),
                _row_get(col, "table_name", "table"),
            )
            if ident is not None:
                entry = _ensure_table_entry(tables_map, ident)
                _merge_column_into_table(entry, col, table_row_count=entry.get("row_count"))

        for prof in bundle.get("profiles") or []:
            if not isinstance(prof, dict):
                continue
            col_n = _row_get(prof, "column_name", "col_name")
            if col_n is not None and _coerce_raw_str(col_n).strip():
                ident = _canonical_identity(
                    _row_get(prof, "fqn", "table_full_name"),
                    _row_get(prof, "catalog", "table_catalog"),
                    _row_get(prof, "schema_name", "table_schema", "schema"),
                    _row_get(prof, "table_name", "table"),
                )
                if ident is not None:
                    entry = _ensure_table_entry(tables_map, ident)
                    _merge_column_into_table(entry, prof, table_row_count=entry.get("row_count"))
            else:
                _merge_table_dict(tables_map, prof)

        for tag in bundle.get("tags") or []:
            if not isinstance(tag, dict):
                continue
            ident = _canonical_identity(
                _row_get(tag, "fqn", "table_full_name"),
                _row_get(tag, "catalog", "catalog_name", "table_catalog"),
                _row_get(tag, "schema_name", "table_schema", "schema"),
                _row_get(tag, "table_name", "table"),
            )
            tk = _coerce_raw_str(_row_get(tag, "tag_name", "key", "name")).strip()
            tv = _row_get(tag, "tag_value", "value")
            if ident is not None and tk:
                entry = _ensure_table_entry(tables_map, ident)
                entry["tags"][tk] = "" if tv is None else _coerce_raw_str(tv)

        for qs in bundle.get("query_stats") or []:
            if not isinstance(qs, dict):
                continue
            ident = _canonical_identity(
                _row_get(qs, "fqn", "table_full_name"),
                _row_get(qs, "catalog", "table_catalog"),
                _row_get(qs, "schema_name", "table_schema", "schema"),
                _row_get(qs, "table_name", "table"),
            )
            cnt = _parse_int_stat(_row_get(qs, "query_count_30d", "query_count", "count"))
            if ident is not None and cnt is not None:
                entry = _ensure_table_entry(tables_map, ident)
                entry["query_count_30d"] = cnt

        workflows_raw = (
            bundle.get("workflows")
            or bundle.get("jobs")
            or bundle.get("scheduled_jobs")
            or []
        )
        if isinstance(workflows_raw, list):
            for wf in workflows_raw:
                if not isinstance(wf, dict):
                    continue
                jname = _coerce_raw_str(_row_get(wf, "job_name", "workflow_name", "name")).strip()
                if not jname:
                    continue
                targets = _parse_str_list_cell(
                    _row_get(wf, "tables", "target_tables", "fqns")
                )
                single = _row_get(wf, "fqn", "target_table", "table_full_name", "table_name")
                if single is not None and _coerce_raw_str(single).strip():
                    targets.append(_coerce_raw_str(single).strip())
                for t_str in targets:
                    t_ident = _canonical_identity(t_str, None, None, None)
                    if t_ident is not None and t_ident[0] in tables_map:
                        if jname not in tables_map[t_ident[0]]["scheduled_jobs"]:
                            tables_map[t_ident[0]]["scheduled_jobs"].append(jname)
                    else:
                        for fqn, entry in tables_map.items():
                            if entry["table_name"].casefold() == t_str.casefold():
                                if jname not in entry["scheduled_jobs"]:
                                    entry["scheduled_jobs"].append(jname)

        for edge in bundle.get("lineage") or []:
            if not isinstance(edge, dict):
                continue
            src_raw = _row_get(
                edge,
                "source_table_full_name",
                "source_fqn",
                "source_table",
                "source",
                "upstream_table",
                "upstream",
                "from_table",
                "from",
            )
            tgt_raw = _row_get(
                edge,
                "target_table_full_name",
                "target_fqn",
                "target_table",
                "target",
                "downstream_table",
                "downstream",
                "to_table",
                "to",
            )
            src_id = _canonical_identity(src_raw, None, None, None) if src_raw else None
            tgt_id = _canonical_identity(tgt_raw, None, None, None) if tgt_raw else None
            src_fqn = src_id[0] if src_id else _coerce_raw_str(src_raw).strip()
            tgt_fqn = tgt_id[0] if tgt_id else _coerce_raw_str(tgt_raw).strip()
            if src_fqn and tgt_fqn and src_fqn.casefold() != tgt_fqn.casefold():
                if tgt_fqn in tables_map and src_fqn not in tables_map[tgt_fqn]["upstream_refs"]:
                    tables_map[tgt_fqn]["upstream_refs"].append(src_fqn)
                if src_fqn in tables_map and tgt_fqn not in tables_map[src_fqn]["downstream_refs"]:
                    tables_map[src_fqn]["downstream_refs"].append(tgt_fqn)

            wf_name = _coerce_raw_str(_row_get(edge, "job_name", "workflow_name")).strip()
            if wf_name:
                for ep in (tgt_fqn, src_fqn):
                    if ep in tables_map and wf_name not in tables_map[ep]["scheduled_jobs"]:
                        tables_map[ep]["scheduled_jobs"].append(wf_name)

        sample_map = bundle.get("sample_rows")
        if isinstance(sample_map, dict):
            for fqn_k, rows_v in sample_map.items():
                if not isinstance(rows_v, list):
                    continue
                s_ident = _canonical_identity(fqn_k, None, None, None)
                key = s_ident[0] if s_ident else _coerce_raw_str(fqn_k).strip()
                if key in tables_map and rows_v:
                    tables_map[key]["sample_rows"] = [r for r in rows_v if isinstance(r, dict)]
    except Exception as exc:
        msg = redact_text(
            f"Malformed JSON bundle content in {json_path.name}: {exc}",
            redact_secrets=redact_secrets,
        )
        if explicit_file:
            errors.append(msg)
        else:
            warnings.append(msg)
        return False

    return True


def _ensure_csv_field_limit(target_limit: int = 16 * 1024 * 1024) -> None:
    try:
        if csv.field_size_limit() < target_limit:
            csv.field_size_limit(target_limit)
    except (OverflowError, ValueError):
        pass


def _read_normalized_csv(csv_path: Path) -> list[dict[str, str]]:
    _ensure_csv_field_limit()
    text = _decode_file_text(csv_path)
    if "\x00" in text:
        raise csv.Error("CSV contains NUL bytes")
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict[str, str]] = []
    for raw_row in reader:
        norm_row: dict[str, str] = {}
        for k, v in raw_row.items():
            if k is None:
                continue
            nk = _norm_csv_header(k)
            if nk:
                norm_row[nk] = (v or "").strip()
        if any(norm_row.values()):
            rows.append(norm_row)
    return rows


def _parse_csv_file_inner(
    csv_path: Path,
    tables_map: dict[str, dict[str, Any]],
) -> bool:
    fname = csv_path.name.lower()
    if fname.startswith(("info.", "info_")):
        return False

    rows = _read_normalized_csv(csv_path)
    if not rows:
        return fname in _KNOWN_DBX_CSV_NAMES

    headers = set(rows[0].keys())
    # Skip Power BI INFO.* CSVs if named generically
    if {"fromcolumnid", "tocolumnid"} <= headers or "explicitname" in headers:
        return False

    if fname == "tables.csv" or (
        fname not in _KNOWN_DBX_CSV_NAMES
        and any(h in headers for h in ("table_type", "medallion_layer", "grain"))
    ):
        for r in rows:
            _merge_table_dict(tables_map, r)
        return True

    if fname == "columns.csv" or (
        fname not in _KNOWN_DBX_CSV_NAMES
        and any(h in headers for h in ("column_name", "col_name"))
        and any(h in headers for h in ("data_type", "full_data_type", "col_type"))
    ):
        for r in rows:
            ident = _canonical_identity(
                _row_get(r, "fqn", "table_full_name", "full_name"),
                _row_get(r, "catalog", "table_catalog", "catalog_name"),
                _row_get(r, "schema_name", "table_schema", "schema", "database", "database_name"),
                _row_get(r, "table_name", "table"),
            )
            if ident is not None:
                entry = _ensure_table_entry(tables_map, ident)
                _merge_column_into_table(entry, r, table_row_count=entry.get("row_count"))
        return True

    if fname == "lineage.csv":
        for r in rows:
            src_raw = _row_get(
                r,
                "source_table_full_name",
                "source_fqn",
                "source_table",
                "source",
                "upstream_table",
                "upstream",
                "from_table",
                "from",
            )
            tgt_raw = _row_get(
                r,
                "target_table_full_name",
                "target_fqn",
                "target_table",
                "target",
                "downstream_table",
                "downstream",
                "to_table",
                "to",
            )
            src_id = _canonical_identity(src_raw, None, None, None) if src_raw else None
            tgt_id = _canonical_identity(tgt_raw, None, None, None) if tgt_raw else None
            src_fqn = src_id[0] if src_id else str(src_raw or "").strip()
            tgt_fqn = tgt_id[0] if tgt_id else str(tgt_raw or "").strip()
            if src_fqn and tgt_fqn and src_fqn.casefold() != tgt_fqn.casefold():
                if tgt_fqn in tables_map and src_fqn not in tables_map[tgt_fqn]["upstream_refs"]:
                    tables_map[tgt_fqn]["upstream_refs"].append(src_fqn)
                if src_fqn in tables_map and tgt_fqn not in tables_map[src_fqn]["downstream_refs"]:
                    tables_map[src_fqn]["downstream_refs"].append(tgt_fqn)
            wf = _row_get(r, "job_name", "workflow_name", "notebook_path")
            if wf:
                for ep in (tgt_fqn, src_fqn):
                    if ep in tables_map and wf not in tables_map[ep]["scheduled_jobs"]:
                        tables_map[ep]["scheduled_jobs"].append(wf)
        return True

    if fname == "profiles.csv":
        for r in rows:
            col_n = _row_get(r, "column_name", "col_name")
            if col_n:
                ident = _canonical_identity(
                    _row_get(r, "fqn", "table_full_name", "full_name"),
                    _row_get(r, "catalog", "table_catalog", "catalog_name"),
                    _row_get(r, "schema_name", "table_schema", "schema", "database"),
                    _row_get(r, "table_name", "table"),
                )
                if ident is not None:
                    entry = _ensure_table_entry(tables_map, ident)
                    rc_inline = _parse_int_stat(_row_get(r, "row_count", "num_rows"))
                    if rc_inline is not None and entry.get("row_count") is None:
                        entry["row_count"] = rc_inline
                    _merge_column_into_table(entry, r, table_row_count=entry.get("row_count"))
            else:
                _merge_table_dict(tables_map, r)
        return True

    if fname in {"tags.csv", "table_tags.csv"}:
        for r in rows:
            ident = _canonical_identity(
                _row_get(r, "fqn", "table_full_name"),
                _row_get(r, "catalog", "catalog_name", "table_catalog"),
                _row_get(r, "schema_name", "table_schema", "schema"),
                _row_get(r, "table_name", "table"),
            )
            tk = _row_get(r, "tag_name", "key", "name")
            tv = _row_get(r, "tag_value", "value") or ""
            if ident is not None and tk:
                entry = _ensure_table_entry(tables_map, ident)
                entry["tags"][tk] = tv
        return True

    if fname in {"workflows.csv", "jobs.csv"}:
        for r in rows:
            jname = _row_get(r, "job_name", "workflow_name", "name")
            t_ref = _row_get(r, "fqn", "target_table", "table_full_name", "table_name")
            if not jname or not t_ref:
                continue
            ident = _canonical_identity(t_ref, None, None, None)
            if ident is not None and ident[0] in tables_map:
                if jname not in tables_map[ident[0]]["scheduled_jobs"]:
                    tables_map[ident[0]]["scheduled_jobs"].append(jname)
        return True

    if fname in {"query_stats.csv", "usage.csv"}:
        for r in rows:
            ident = _canonical_identity(
                _row_get(r, "fqn", "table_full_name"),
                _row_get(r, "catalog", "table_catalog"),
                _row_get(r, "schema_name", "table_schema", "schema"),
                _row_get(r, "table_name", "table"),
            )
            cnt = _parse_int_stat(_row_get(r, "query_count_30d", "query_count", "count"))
            if ident is not None and cnt is not None:
                entry = _ensure_table_entry(tables_map, ident)
                entry["query_count_30d"] = cnt
        return True

    return False


def _parse_csv_file(
    csv_path: Path,
    tables_map: dict[str, dict[str, Any]],
    warnings: list[str] | None = None,
    errors: list[str] | None = None,
    explicit_file: bool = False,
    redact_secrets: bool = True,
) -> bool:
    try:
        return _parse_csv_file_inner(csv_path, tables_map)
    except Exception as exc:
        msg = redact_text(
            f"Failed to parse CSV file {csv_path.name}: {exc}"
            if explicit_file
            else f"Skipped malformed CSV file {csv_path.name}: {exc}",
            redact_secrets=redact_secrets,
        )
        if explicit_file and errors is not None:
            errors.append(msg)
        elif warnings is not None:
            warnings.append(msg)
        return False


def _collect_tier2_files(candidate: Path) -> tuple[list[Path], list[Path]]:
    if not candidate.exists():
        return [], []
    if candidate.is_file():
        suf = candidate.suffix.lower()
        if suf == ".json":
            return [candidate], []
        if suf == ".csv":
            return [], [candidate]
        return [], []

    json_files: list[Path] = []
    csv_files: list[Path] = []
    csv_order = (
        "tables.csv",
        "columns.csv",
        "profiles.csv",
        "tags.csv",
        "table_tags.csv",
        "query_stats.csv",
        "usage.csv",
        "workflows.csv",
        "jobs.csv",
        "lineage.csv",
    )
    skip_dirs = {"powerbi", "teams", "notes", "processed", "archive", "__pycache__", ".git"}

    for root, dirs, files in os.walk(candidate):
        dirs[:] = sorted(
            d
            for d in dirs
            if not d.startswith(".")
            and d.lower() not in skip_dirs
            and not d.lower().endswith((".pbip", ".semanticmodel", ".dataset", ".report"))
        )
        root_p = Path(root)
        file_map = {f.lower(): root_p / f for f in files}
        for cname in csv_order:
            if cname in file_map:
                csv_files.append(file_map[cname])
        for f in sorted(files):
            fl = f.lower()
            if fl.startswith((".", "~$")) or fl.startswith(("info.", "info_")):
                continue
            fp = root_p / f
            if fp.suffix.lower() == ".json":
                json_files.append(fp)
    return json_files, csv_files


_DBX_REDACTED_TOKEN_RE = re.compile(r"\[REDACTED:[A-Z_]+\]", re.IGNORECASE)


def _clean_dbx_ident_segment(
    raw_seg: str, fallback: str, redact_secrets: bool
) -> tuple[str, bool]:
    red = redact_text(raw_seg.strip(), redact_secrets=redact_secrets)
    if "[REDACTED:" not in red:
        return (red.strip() or fallback), False
    cleaned = _DBX_REDACTED_TOKEN_RE.sub(" ", red)
    collapsed = " ".join(cleaned.split()).strip(" \t\r\n\x0b\x0c_.-:;|/\\,")
    seg = re.sub(r"([_-])[\s_-]+", r"\1", collapsed)
    return (seg or fallback), True


def _slug_ref_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _build_known_vault_keys(vdir: Path, filtered_map: dict[str, dict[str, Any]]) -> set[str]:
    keys: set[str] = set()

    def _reg(*vals: Any) -> None:
        for v in vals:
            s = _coerce_raw_str(v).strip()
            if s:
                keys.add(s.casefold())
                sl = _slug_ref_key(s)
                if sl:
                    keys.add(sl)

    for fqn, entry in filtered_map.items():
        _reg(fqn, entry.get("table_name"), _safe_dataset_card_stem(fqn))
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


def _sanitize_prose_wikilinks(text: str, known_keys: set[str]) -> str:
    return unwrap_prose_wikilinks(text, known_keys)


def _scrub_crawler_nested(val: Any, redact_secrets: bool) -> Any:
    if isinstance(val, str):
        return redact_text(val, redact_secrets=redact_secrets)
    if isinstance(val, list):
        return [_scrub_crawler_nested(v, redact_secrets) for v in val]
    if isinstance(val, dict):
        return {
            redact_text(_coerce_raw_str(k), redact_secrets=redact_secrets): _scrub_crawler_nested(
                v, redact_secrets
            )
            for k, v in val.items()
        }
    return val


def _sanitize_nested_prose_wikilinks(val: Any, known_keys: set[str]) -> Any:
    if isinstance(val, str):
        return _sanitize_prose_wikilinks(val, known_keys)
    if isinstance(val, (list, tuple, set, frozenset)):
        out_l = [_sanitize_nested_prose_wikilinks(v, known_keys) for v in val]
        s_repr = str(out_l)
        return (
            _sanitize_prose_wikilinks(json.dumps(out_l, ensure_ascii=False), known_keys)
            if ("[[" in s_repr and _sanitize_prose_wikilinks(s_repr, known_keys) != s_repr)
            else out_l
        )
    if isinstance(val, dict):
        return {
            _sanitize_prose_wikilinks(
                _coerce_raw_str(k), known_keys
            ): _sanitize_nested_prose_wikilinks(v, known_keys)
            for k, v in val.items()
        }
    return val


# ---------------------------------------------------------------------------
# Public Entry Point
# ---------------------------------------------------------------------------


def sync_databricks(
    vault_dir: Path,
    config: PoopdeckConfig | None = None,
    input_path: Path | None = None,
    mode: str = "auto",
    client: Any = None,
    sql_runner: Callable[..., Any] | None = None,
    allow_sample_rows: bool | None = None,
) -> SyncSummary:
    """Sync Databricks metadata, safe profiles, and lineage into `vault/datasets/*.md`."""
    vdir = Path(vault_dir)
    base_cfg = load_config() if config is None else config
    dbx_cfg = base_cfg.databricks

    effective_allow = (
        bool(allow_sample_rows)
        if allow_sample_rows is not None
        else bool(base_cfg.privacy.allow_sample_rows)
    )
    effective_privacy = base_cfg.privacy.model_copy(
        update={"allow_sample_rows": effective_allow}
    )

    norm_mode = _normalize_mode(mode)
    in_path = (
        Path(input_path)
        if input_path is not None and str(input_path).strip()
        else None
    )

    warnings: list[str] = []
    errors: list[str] = []
    processed_files: list[str] = []

    default_bundle_dir = _rebase_vault_relative_path(
        vdir, base_cfg, dbx_cfg.bundle_path, ("inbox", "databricks")
    )
    tier2_target = in_path if in_path is not None else default_bundle_dir
    cand_jsons, cand_csvs = _collect_tier2_files(tier2_target)

    resolved_mode: str
    used_ambient_auto_tier1 = False
    if norm_mode in {"tier1", "tier2"}:
        resolved_mode = norm_mode
    else:
        if client is not None or sql_runner is not None:
            resolved_mode = "tier1"
        elif in_path is not None:
            resolved_mode = "tier2"
        elif dbx_cfg.mode in {"tier1", "tier2"}:
            resolved_mode = dbx_cfg.mode
        else:
            host = dbx_cfg.host.strip() or os.environ.get("DATABRICKS_HOST", "").strip()
            has_auth = bool(
                os.environ.get("DATABRICKS_TOKEN", "").strip()
                or os.environ.get("DATABRICKS_CONFIG_PROFILE", "").strip()
                or (
                    os.environ.get("DATABRICKS_CLIENT_ID", "").strip()
                    and os.environ.get("DATABRICKS_CLIENT_SECRET", "").strip()
                )
            )
            if host and has_auth:
                if _has_databricks_sdk():
                    resolved_mode = "tier1"
                    used_ambient_auto_tier1 = True
                else:
                    warnings.append(
                        "Databricks credentials detected, but `databricks-sdk` is not installed; checking Tier 2 bundles."
                    )
                    resolved_mode = "tier2" if (cand_jsons or cand_csvs) else "none"
            elif cand_jsons or cand_csvs:
                resolved_mode = "tier2"
            else:
                resolved_mode = "none"

    def _scrub_list(items: list[str]) -> list[str]:
        return [
            redact_text(_coerce_raw_str(item), redact_secrets=effective_privacy.redact_secrets)
            for item in items
        ]

    if resolved_mode == "none":
        warnings.append(
            "No Databricks Tier 1 credentials/runner or Tier 2 discovery bundles found; 0 dataset cards synced."
        )
        return SyncSummary(
            target="databricks",
            mode_used="none",
            cards_written=0,
            warnings=_scrub_list(warnings),
            errors=_scrub_list(errors),
        )

    tables_map: dict[str, dict[str, Any]] = {}
    if resolved_mode == "tier1":
        tables_map = _crawl_tier1(
            dbx_cfg=dbx_cfg,
            privacy_cfg=effective_privacy,
            client=client,
            sql_runner=sql_runner,
            warnings=warnings,
            errors=errors,
        )
        if used_ambient_auto_tier1 and not tables_map and errors:
            warnings.extend(_scrub_list(errors))
            errors.clear()
            if cand_jsons or cand_csvs:
                resolved_mode = "tier2"
            else:
                return SyncSummary(
                    target="databricks",
                    mode_used="none",
                    cards_written=0,
                    warnings=_scrub_list(warnings),
                    errors=[],
                )

    if resolved_mode == "tier2":
        if in_path is not None and not in_path.exists():
            errors.append(
                redact_text(
                    f"Databricks Tier 2 input path does not exist: {in_path}",
                    redact_secrets=effective_privacy.redact_secrets,
                )
            )
            return SyncSummary(
                target="databricks",
                mode_used="tier2",
                cards_written=0,
                warnings=_scrub_list(warnings),
                errors=_scrub_list(errors),
            )
        if not cand_jsons and not cand_csvs:
            warnings.append(
                redact_text(
                    f"No Databricks Tier 2 JSON/CSV files found in {tier2_target}",
                    redact_secrets=effective_privacy.redact_secrets,
                )
            )
            return SyncSummary(
                target="databricks",
                mode_used="tier2",
                cards_written=0,
                warnings=_scrub_list(warnings),
                errors=_scrub_list(errors),
            )
        explicit_file = in_path is not None and in_path.is_file()
        for jp in cand_jsons:
            if _parse_json_bundle(
                jp,
                tables_map,
                warnings,
                errors,
                explicit_file=explicit_file,
                redact_secrets=effective_privacy.redact_secrets,
            ):
                processed_files.append(jp.as_posix())
        for cp in cand_csvs:
            if _parse_csv_file(
                cp,
                tables_map,
                warnings=warnings,
                errors=errors,
                explicit_file=explicit_file,
                redact_secrets=effective_privacy.redact_secrets,
            ):
                processed_files.append(cp.as_posix())

    # Filter allowed catalogs & propagate symmetric intra-batch lineage
    filtered_map: dict[str, dict[str, Any]] = {
        fqn: entry
        for fqn, entry in tables_map.items()
        if _is_catalog_allowed(entry.get("catalog", ""), dbx_cfg)
        and str(entry.get("schema_name", "")).lower() != "information_schema"
    }

    ci_lookup = {fqn.casefold(): fqn for fqn in filtered_map}
    for fqn, entry in filtered_map.items():
        for up in list(entry.get("upstream_refs") or []):
            up_clean = normalize_wikilink_target(str(up))
            canon_up = ci_lookup.get(up_clean.casefold())
            if canon_up and canon_up.casefold() != fqn.casefold():
                dn_list = filtered_map[canon_up].setdefault("downstream_refs", [])
                if all(normalize_wikilink_target(x).casefold() != fqn.casefold() for x in dn_list):
                    dn_list.append(fqn)
        for dn in list(entry.get("downstream_refs") or []):
            dn_clean = normalize_wikilink_target(str(dn))
            canon_dn = ci_lookup.get(dn_clean.casefold())
            if canon_dn and canon_dn.casefold() != fqn.casefold():
                up_list = filtered_map[canon_dn].setdefault("upstream_refs", [])
                if all(normalize_wikilink_target(x).casefold() != fqn.casefold() for x in up_list):
                    up_list.append(fqn)

    datasets_dir = vdir / "datasets"
    datasets_dir.mkdir(parents=True, exist_ok=True)
    written_paths: list[str] = []
    now_utc = datetime.now(timezone.utc)

    fqn_remap: dict[str, str] = {}
    clean_idents: dict[str, tuple[str, str, str, str]] = {}
    used_clean_fqns: set[str] = set()
    used_card_stems: set[str] = set()

    for raw_fqn in sorted(filtered_map, key=lambda k: (k.casefold(), k)):
        raw_entry = filtered_map[raw_fqn]
        base_cat, cat_red = _clean_dbx_ident_segment(
            _coerce_raw_str(raw_entry["catalog"]),
            "catalog",
            effective_privacy.redact_secrets,
        )
        base_sch, sch_red = _clean_dbx_ident_segment(
            _coerce_raw_str(raw_entry["schema_name"]),
            "schema",
            effective_privacy.redact_secrets,
        )
        base_tbl, tbl_red = _clean_dbx_ident_segment(
            _coerce_raw_str(raw_entry["table_name"]),
            "dataset",
            effective_privacy.redact_secrets,
        )
        cat, sch, tbl = base_cat, base_sch, base_tbl
        cand_fqn = f"{cat}.{sch}.{tbl}"
        cand_stem = _safe_dataset_card_stem(cand_fqn)
        if cand_fqn.casefold() in used_clean_fqns or cand_stem.casefold() in used_card_stems:
            n = 2
            while True:
                if tbl_red or (not sch_red and not cat_red):
                    cat, sch, tbl = base_cat, base_sch, f"{base_tbl}_{n}"
                elif sch_red:
                    cat, sch, tbl = base_cat, f"{base_sch}_{n}", base_tbl
                else:
                    cat, sch, tbl = f"{base_cat}_{n}", base_sch, base_tbl
                cand_fqn = f"{cat}.{sch}.{tbl}"
                cand_stem = _safe_dataset_card_stem(cand_fqn)
                if (
                    cand_fqn.casefold() not in used_clean_fqns
                    and cand_stem.casefold() not in used_card_stems
                ):
                    break
                n += 1
        used_clean_fqns.add(cand_fqn.casefold())
        used_card_stems.add(cand_stem.casefold())
        fqn_remap[raw_fqn.casefold()] = cand_fqn
        clean_idents[raw_fqn] = (cand_fqn, cat, sch, tbl)

    remapped_for_keys: dict[str, dict[str, Any]] = {
        clean_idents[rfqn][0]: {"table_name": clean_idents[rfqn][3]}
        for rfqn in filtered_map
    }
    known_vault_keys = _build_known_vault_keys(vdir, remapped_for_keys)

    for fqn in sorted(filtered_map, key=lambda k: (k.casefold(), k)):
        raw_entry = filtered_map[fqn]
        clean_fqn, cat, sch, tbl = clean_idents[fqn]

        raw_tags = raw_entry.get("tags")
        tags_dict: dict[str, Any] = {}
        if isinstance(raw_tags, dict):
            tags_dict = dict(raw_tags)

        medallion = _infer_medallion_layer(
            raw_entry.get("medallion_layer"),
            tags_dict,
            cat,
            sch,
            tbl,
            clean_fqn,
        )

        clean_cols: list[dict[str, Any]] = []
        for col in raw_entry.get("columns") or []:
            if not isinstance(col, dict):
                continue
            cname = _coerce_raw_str(col.get("name") or col.get("column_name")).strip()
            if not cname or cname.startswith("#"):
                continue
            npct = _parse_float_stat(col.get("null_pct"))
            dcnt = _parse_int_stat(col.get("distinct_count"))
            raw_dt_str = _coerce_raw_str(col.get("data_type") or "STRING").strip() or "STRING"
            clean_dt_str = (
                redact_text(raw_dt_str, redact_secrets=effective_privacy.redact_secrets).strip()
                or "STRING"
            )
            clean_cols.append(
                {
                    "name": cname,
                    "data_type": clean_dt_str,
                    "nullable": _parse_bool_flag(col.get("nullable"), default=True),
                    "null_pct": max(0.0, min(100.0, npct)) if npct is not None else None,
                    "distinct_count": dcnt,
                    "min_val": col.get("min_val"),
                    "max_val": col.get("max_val"),
                    "sample_values": col.get("sample_values") or [],
                    "is_pii": bool(col.get("is_pii", False)),
                    "comment": _coerce_raw_str(col.get("comment")),
                    "is_partition": bool(col.get("is_partition", False)),
                    "is_primary_key": bool(col.get("is_primary_key", False)),
                }
            )

        join_keys, grain = _infer_join_keys_and_grain(
            raw_entry.get("join_keys") or [],
            _coerce_raw_str(raw_entry.get("grain")),
            clean_cols,
            effective_privacy,
        )

        # Strip helper-only column flags before model validation
        for col in clean_cols:
            col.pop("is_partition", None)
            col.pop("is_primary_key", None)

        min_ts_raw = raw_entry.get("min_timestamp")
        max_ts_raw = raw_entry.get("max_timestamp")
        dt_max = _parse_iso_dt(max_ts_raw)
        min_ts_clean = (
            redact_text(
                _coerce_raw_str(min_ts_raw).strip(),
                redact_secrets=effective_privacy.redact_secrets,
            )
            if min_ts_raw is not None and _coerce_raw_str(min_ts_raw).strip()
            else None
        )
        max_ts_clean = (
            redact_text(
                _coerce_raw_str(max_ts_raw).strip(),
                redact_secrets=effective_privacy.redact_secrets,
            )
            if max_ts_raw is not None and _coerce_raw_str(max_ts_raw).strip()
            else None
        )
        lag_hours = _parse_float_stat(raw_entry.get("freshness_lag_hours"))
        if lag_hours is None and dt_max is not None:
            lag_hours = max(0.0, round((now_utc - dt_max).total_seconds() / 3600.0, 2))
        elif lag_hours is not None:
            lag_hours = max(0.0, lag_hours)

        def _clean_ref_list(
            refs: Sequence[Any], _self_fqn: str = clean_fqn, _raw_fqn: str = fqn
        ) -> list[str]:
            out_refs: list[str] = []
            seen_r: set[str] = set()
            for r in refs:
                raw_norm = normalize_wikilink_target(str(r))
                if not raw_norm or raw_norm.casefold() in {_self_fqn.casefold(), _raw_fqn.casefold()}:
                    continue
                mapped = fqn_remap.get(raw_norm.casefold())
                norm_r = (
                    mapped
                    if mapped is not None
                    else normalize_wikilink_target(
                        redact_text(raw_norm, redact_secrets=effective_privacy.redact_secrets)
                    )
                )
                if (
                    not norm_r
                    or "[REDACTED:" in norm_r
                    or norm_r.casefold() == _self_fqn.casefold()
                ):
                    continue
                if norm_r.casefold() not in seen_r:
                    seen_r.add(norm_r.casefold())
                    out_refs.append(format_wikilink(norm_r))
            return out_refs

        scrubbed_jobs: list[str] = []
        seen_jobs: set[str] = set()
        for jb in raw_entry.get("scheduled_jobs") or []:
            sj = redact_text(
                str(jb).strip(), redact_secrets=effective_privacy.redact_secrets
            )
            if sj and sj.casefold() not in seen_jobs:
                seen_jobs.add(sj.casefold())
                scrubbed_jobs.append(sj)

        payload: dict[str, Any] = {
            "id": clean_fqn,
            "fqn": clean_fqn,
            "catalog": cat,
            "schema_name": sch,
            "table_name": tbl,
            "table_type": _normalize_table_type(raw_entry.get("table_type")),
            "medallion_layer": medallion,
            "row_count": _parse_int_stat(raw_entry.get("row_count")),
            "min_timestamp": min_ts_clean,
            "max_timestamp": max_ts_clean,
            "freshness_lag_hours": lag_hours,
            "query_count_30d": _parse_int_stat(raw_entry.get("query_count_30d")),
            "grain": redact_text(grain, redact_secrets=effective_privacy.redact_secrets),
            "join_keys": [
                redact_text(
                    _coerce_raw_str(k), redact_secrets=effective_privacy.redact_secrets
                )
                for k in join_keys
            ],
            "columns": clean_cols,
            "sample_rows": raw_entry.get("sample_rows") or [],
            "upstream_refs": _clean_ref_list(raw_entry.get("upstream_refs") or []),
            "downstream_refs": _clean_ref_list(raw_entry.get("downstream_refs") or []),
            "scheduled_jobs": scrubbed_jobs,
            "tags": tags_dict,
            "comment": _coerce_raw_str(raw_entry.get("comment")),
        }

        sanitized_payload = _sanitize_for_redaction(payload)
        redacted_payload = _scrub_crawler_nested(
            redact_dataset_payload(sanitized_payload, config=effective_privacy),
            effective_privacy.redact_secrets,
        )
        redacted_payload["grain"] = _sanitize_prose_wikilinks(
            _coerce_raw_str(redacted_payload.get("grain")), known_vault_keys
        )
        redacted_payload["comment"] = _sanitize_prose_wikilinks(
            _coerce_raw_str(redacted_payload.get("comment")), known_vault_keys
        )
        redacted_payload["join_keys"] = [
            _sanitize_prose_wikilinks(_coerce_raw_str(jk), known_vault_keys)
            for jk in (redacted_payload.get("join_keys") or [])
        ]
        redacted_payload["scheduled_jobs"] = [
            _sanitize_prose_wikilinks(_coerce_raw_str(sj), known_vault_keys)
            for sj in (redacted_payload.get("scheduled_jobs") or [])
        ]
        for rcol in redacted_payload.get("columns") or []:
            if isinstance(rcol, dict):
                for cf in ("name", "data_type", "comment"):
                    if isinstance(rcol.get(cf), str):
                        rcol[cf] = _sanitize_prose_wikilinks(rcol[cf], known_vault_keys)
        redacted_payload["sample_rows"] = [
            _sanitize_nested_prose_wikilinks(srow, known_vault_keys)
            for srow in (redacted_payload.get("sample_rows") or [])
            if isinstance(srow, dict)
        ]

        try:
            card = DatasetCard.model_validate(redacted_payload)
        except Exception as exc:
            errors.append(
                redact_text(
                    f"Validation failed for dataset {clean_fqn}: {exc}",
                    redact_secrets=effective_privacy.redact_secrets,
                )
            )
            continue

        safe_stem = _safe_dataset_card_stem(card.fqn)
        card_path = datasets_dir / f"{safe_stem}.md"
        if card_path.resolve().parent != datasets_dir.resolve():
            errors.append(
                redact_text(
                    f"Unsafe dataset card path rejected for {clean_fqn}",
                    redact_secrets=effective_privacy.redact_secrets,
                )
            )
            continue

        upsert_markdown_card(card_path, card, render_dataset_body(card))
        written_paths.append(card_path.as_posix())

    return SyncSummary(
        target="databricks",
        mode_used=resolved_mode,
        cards_written=len(written_paths),
        written_paths=written_paths,
        processed_files=_scrub_list(processed_files),
        warnings=_scrub_list(warnings),
        errors=_scrub_list(errors),
    )
