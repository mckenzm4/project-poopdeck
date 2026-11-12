"""Dual-mode workspace crawlers and context dropzone normalizers for Project Poopdeck."""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from poopdeck.config import PoopdeckConfig, StrictInt, load_config
from poopdeck.redaction import redact_text

_USCORE_URL_CRED_RE = re.compile(
    r"(?<![A-Za-z0-9+.-])([A-Za-z][A-Za-z0-9+.-]{0,31}://[^/\s:;?#,\x22\x27<>`()\[\]{}|]*:)"
    r"(?!\[REDACTED:[A-Z_]+\]@)(?!\d{1,5}(?:[,)|\]}>]|&[A-Za-z0-9_]+=))"
    r"([^/\s;?#,\x22\x27<>`()\[\]{}|]+)(@)(?=[A-Za-z0-9.\-]|\[[0-9A-Fa-f:])"
)
_USCORE_BEARER_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])Bearer[ \t]++(?!\[REDACTED:)[A-Za-z0-9_\-\.~+/]{16,}=*+"
)
_USCORE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:dapi[0-9a-fA-F]{16,64}+|github_pat_[A-Za-z0-9_]{20,255}+"
    r"|(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,255}+|sk-ant-[A-Za-z0-9_-]{12,255}+"
    r"|sk-(?:proj-|live-|test-)?[A-Za-z0-9_-]{16,255}+|(?:AKIA|ASIA)[0-9A-Z]{16}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,255}+|eyJ[A-Za-z0-9_-]{5,256}+\.[A-Za-z0-9_-]{5,4096}+\.[A-Za-z0-9_-]{5,1024}+)(?![A-Za-z0-9])"
)
_USCORE_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}+@(?:[A-Za-z0-9-]{1,63}+\.){1,10}[A-Za-z]{2,24}(?![A-Za-z0-9-])"
)
_USCORE_SSN_RE = re.compile(r"(?<![A-Za-z0-9])\d{3}-\d{2}-\d{4}(?![A-Za-z0-9])")
_USCORE_PHONE_US_RE = re.compile(
    r"(?:\+1[-.\s]?(?:\(\d{3}\)[-.\s]?|\d{3}[-.\s])|\(\d{3}\)[-.\s]?|(?<![A-Za-z0-9])(?<!\d[-.])(?:1[-.\s]?)?(?:\(\d{3}\)[-.\s]?|\d{3}[-.\s]))"
    r"\d{3}[-.\s]\d{4}(?:\s*(?:ext\.?|x)\s*\d{1,5})?(?![A-Za-z0-9]|[-.]\d)",
    re.IGNORECASE,
)
_USCORE_PHONE_INTL_RE = re.compile(
    r"(?:\+1\d{10}|\+\d{1,3}[\s-]\(?\d{1,4}\)?[\s-]\d{3,4}(?:[\s-]\d{2,4}){1,2})(?![A-Za-z0-9]|[-.]\d)"
)


def _repl_uscore_intl_phone(m: re.Match[str]) -> str:
    raw = m.group(0)
    return "[REDACTED:PHONE]" if 10 <= sum(ch.isdigit() for ch in raw) <= 15 else raw


def redact_crawler_text(text: str, redact_secrets: bool = True) -> str:
    """Scrub secrets and PII including underscore-delimited filename/identifier tokens."""
    if not text or not isinstance(text, str):
        return text if isinstance(text, str) else ""
    out = (
        _USCORE_URL_CRED_RE.sub(r"\1[REDACTED:SECRET]\3", text)
        if ("_" in text and redact_secrets and "://" in text)
        else text
    )
    out = redact_text(out, redact_secrets=redact_secrets)
    if "_" not in text:
        return out
    if redact_secrets:
        if "bearer" in out.lower():
            out = _USCORE_BEARER_RE.sub("[REDACTED:SECRET]", out)
        out = _USCORE_TOKEN_RE.sub("[REDACTED:SECRET]", out)
    if "@" in out:
        out = _USCORE_EMAIL_RE.sub("[REDACTED:EMAIL]", out)
    if "-" in out:
        out = _USCORE_SSN_RE.sub("[REDACTED:SSN]", out)
    if any(c in out for c in "-.() "):
        out = _USCORE_PHONE_US_RE.sub("[REDACTED:PHONE]", out)
    if "+" in out:
        out = _USCORE_PHONE_INTL_RE.sub(_repl_uscore_intl_phone, out)
    return out


_PROSE_WIKILINK_RE = re.compile(r"(?<!\[)(\[{2,}+)([^\[\]\r\n]++)(\]{2,}+)")
_MULTI_BRACKET_RE = re.compile(r"\[{2,}+|\]{2,}+")
_WIKILINK_SLUG_RE = re.compile(r"[^a-z0-9]+")


def unwrap_prose_wikilinks(text: str, known_keys: set[str]) -> str:
    """Unwrap unresolved [[...]] prose brackets in strict O(N) while preserving valid links."""
    if not text or "[[" not in text or "]]" not in text:
        return text
    sentinel, idx = "\x00WL", 0
    while sentinel in text:
        idx += 1
        sentinel = f"\x00WL{idx}_"
    stashed: list[str] = []

    def _is_known(raw: str) -> bool:
        if not raw or sentinel in raw:
            return False
        target = raw.partition("|")[0].split("#", 1)[0].strip()
        if not target or "[REDACTED:" in target or "REDACTED:" in target:
            return False
        slug = _WIKILINK_SLUG_RE.sub("-", target.lower()).strip("-")
        return target.casefold() in known_keys or (bool(slug) and slug in known_keys)

    def _stash(g1: str, raw: str, g3: str) -> str:
        tok, k = f"{sentinel}{len(stashed)}\x00", min(len(g1), len(g3))
        stashed.append(f"[[{raw}]]")
        return f"{g1[:-k]}{tok}{g3[k:]}"

    def _repl(m: re.Match[str]) -> str:
        g1, g2, g3 = m.group(1), m.group(2), m.group(3)
        raw = g2.strip()
        if _is_known(raw):
            return _stash(g1, raw, g3)
        k = min(len(g1), len(g3))
        if sentinel in raw:
            return f"{g1[:-k]}{raw}{g3[k:]}"
        target_part, sep, alias_part = raw.partition("|")
        target = target_part.split("#", 1)[0].strip()
        alias = alias_part.strip() if sep else ""
        return f"{g1[:-k]}{alias or target or raw}{g3[k:]}"

    out = text
    for _ in range(4):
        if "[[" not in out or "]]" not in out:
            break
        nxt = _PROSE_WIKILINK_RE.sub(_repl, out)
        if nxt == out:
            break
        out = nxt
    if "[[" in out and "]]" in out:
        out = _PROSE_WIKILINK_RE.sub(
            lambda m: (
                _stash(m.group(1), m.group(2).strip(), m.group(3))
                if _is_known(m.group(2).strip())
                else m.group(0)
            ),
            out,
        )
        out = _MULTI_BRACKET_RE.sub(" ", out)
    if stashed:
        restore_re = re.compile(re.escape(sentinel) + r"(\d++)\x00")
        out = restore_re.sub(lambda m: stashed[int(m.group(1))], out)
    return out


class SyncSummary(BaseModel):
    """Structured summary returned by every workspace crawler run."""

    model_config = ConfigDict(allow_inf_nan=False, validate_assignment=True)

    target: str
    mode_used: str
    cards_written: StrictInt = Field(default=0, ge=0)
    written_paths: list[str] = Field(default_factory=list)
    processed_files: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @property
    def items_synced(self) -> int:
        """Compatibility alias returning the count of vault cards written/updated."""
        return self.cards_written


from poopdeck.crawlers.databricks_ingest import sync_databricks  # noqa: E402
from poopdeck.crawlers.powerbi_ingest import (  # noqa: E402
    detect_dax_heuristics,
    sync_powerbi,
)
from poopdeck.crawlers.teams_ingest import sync_teams  # noqa: E402


_SYNC_MODE_ALIASES: dict[str, str] = {
    "auto": "auto",
    "tier1": "tier1",
    "tier1_sdk": "tier1",
    "tier1_rest": "tier1",
    "tier2": "tier2",
    "tier2_bundle": "tier2",
    "tier2_local": "tier2",
}

_DBX_CSV_NAMES: frozenset[str] = frozenset(
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


def _classify_single_file(in_path: Path) -> set[str]:
    """Classify an explicit single input file to its owning crawler(s)."""
    suffix = in_path.suffix.lower()
    name_low = in_path.name.lower()
    if suffix in {".vtt", ".docx", ".txt", ".md"}:
        return {"teams"}
    if suffix in {".pbip", ".bim", ".tmdl"} or name_low.startswith(("info.", "info_")):
        return {"powerbi"}
    if suffix == ".csv":
        if name_low in _DBX_CSV_NAMES:
            return {"databricks"}
        try:
            raw = in_path.read_bytes()[:65536]
            txt = (
                raw.decode("utf-16", errors="replace")
                if raw.startswith((b"\xff\xfe", b"\xfe\xff"))
                else raw.decode("utf-8-sig", errors="replace")
            )
            hdr = next(csv.reader(io.StringIO(txt)), [])
            raw_h = [h.strip() for h in hdr if h and h.strip()]
            norm_h = {h.strip("[]\"'` ").lower().replace(" ", "_") for h in raw_h}
            if any(h.startswith("[") and h.endswith("]") for h in raw_h) or (
                norm_h
                & {
                    "tableid",
                    "fromcolumnid",
                    "tocolumnid",
                    "fromtableid",
                    "totableid",
                    "explicitname",
                    "crossfilteringbehavior",
                }
            ):
                return {"powerbi"}
            if norm_h & {
                "fqn",
                "table_full_name",
                "full_name",
                "table_catalog",
                "catalog_name",
                "catalog",
                "schema_name",
                "table_schema",
                "medallion_layer",
                "table_type",
                "source_table_full_name",
                "target_table_full_name",
                "null_pct",
                "distinct_count",
                "query_count_30d",
            }:
                return {"databricks"}
        except Exception:
            pass
        if any(k in name_low for k in ("pbi", "powerbi", "dax", "measure", "relationship")):
            return {"powerbi"}
        return {"databricks"}
    if suffix == ".json":
        try:
            raw = in_path.read_bytes()
            txt = (
                raw.decode("utf-16", errors="replace")
                if raw.startswith((b"\xff\xfe", b"\xfe\xff"))
                else raw.decode("utf-8-sig", errors="replace")
            )
            loaded = json.loads(txt)
            if isinstance(loaded, dict):
                if (
                    any(
                        k in loaded
                        for k in (
                            "compatibilityLevel",
                            "createOrReplace",
                            "SemanticModel",
                            "reportId",
                            "datasetId",
                            "semantic_model",
                            "bi_to_ds_gaps",
                            "refresh_failures_30d",
                        )
                    )
                    or isinstance(loaded.get("model"), dict)
                    or isinstance(loaded.get("reports"), list)
                ):
                    return {"powerbi"}
                tbls = loaded.get("tables")
                if isinstance(tbls, list) and tbls and isinstance(tbls[0], dict):
                    has_dbx = any(
                        k in tbls[0]
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
                    has_pbi = any(
                        k in tbls[0]
                        for k in ("measures", "partitions", "lineageTag", "hierarchies")
                    )
                    if has_pbi and not has_dbx:
                        return {"powerbi"}
                if any(
                    k in loaded
                    for k in (
                        "bundle_version",
                        "catalog_mode",
                        "datasets",
                        "tables",
                        "lineage",
                        "profiles",
                        "query_stats",
                        "workflows",
                        "fqn",
                        "full_name",
                        "table_full_name",
                        "table_catalog",
                        "catalog_name",
                        "schema_name",
                        "table_name",
                    )
                ):
                    return {"databricks"}
            elif isinstance(loaded, list) and loaded and isinstance(loaded[0], dict):
                ks = set(loaded[0].keys())
                if any(str(k).startswith("[") for k in ks) or (
                    ks
                    & {
                        "ExplicitName",
                        "FromColumnID",
                        "TableID",
                        "semantic_model",
                        "bi_to_ds_gaps",
                    }
                ):
                    return {"powerbi"}
                return {"databricks"}
        except Exception:
            return (
                {"powerbi"}
                if any(k in name_low for k in ("pbi", "powerbi", "bim", "report", "dax"))
                else {"databricks"}
            )
        return (
            {"powerbi"}
            if any(k in name_low for k in ("pbi", "powerbi", "bim", "report", "dax"))
            else {"databricks", "powerbi"}
        )
    return set()


def _route_explicit_input(
    vault_dir: Path,
    in_path: Path,
) -> dict[str, Path | None]:
    """Route an explicit file or directory across Databricks, Power BI, and Teams."""
    if not in_path.exists():
        return {"databricks": in_path, "powerbi": in_path, "teams": in_path}

    if in_path.is_file():
        owners = _classify_single_file(in_path)
        return {
            t: (in_path if t in owners else None)
            for t in ("databricks", "powerbi", "teams")
        }

    try:
        same_as_vault = in_path.resolve() == vault_dir.resolve()
    except OSError:
        same_as_vault = False

    if same_as_vault:
        inbox = vault_dir / "inbox"
        return {
            "databricks": (inbox / "databricks") if (inbox / "databricks").is_dir() else None,
            "powerbi": (inbox / "powerbi") if (inbox / "powerbi").is_dir() else None,
            "teams": inbox if inbox.is_dir() else None,
        }

    has_dbx_sub = (in_path / "databricks").is_dir()
    has_pbi_sub = (in_path / "powerbi").is_dir()
    has_teams_sub = (in_path / "teams").is_dir()
    has_notes_sub = (in_path / "notes").is_dir()

    if has_dbx_sub or has_pbi_sub or has_teams_sub or has_notes_sub:
        return {
            "databricks": (in_path / "databricks") if has_dbx_sub else in_path,
            "powerbi": (in_path / "powerbi") if has_pbi_sub else in_path,
            "teams": in_path,
        }

    return {"databricks": in_path, "powerbi": in_path, "teams": in_path}


def sync_all(
    vault_dir: Path,
    config: PoopdeckConfig | None = None,
    target: str = "all",
    input_path: Path | None = None,
    mode: str = "auto",
    allow_sample_rows: bool | None = None,
) -> dict[str, SyncSummary]:
    """Run one or all dual-mode crawlers (`all`, `databricks`, `powerbi`, `teams`)."""
    vdir = Path(vault_dir)
    norm_target = (target if isinstance(target, str) else "").strip().lower()
    valid_targets = {"all", "databricks", "powerbi", "teams"}
    if norm_target not in valid_targets:
        raise ValueError(
            f"Invalid sync target {target!r}; expected one of: all, databricks, powerbi, teams"
        )

    norm_mode = _SYNC_MODE_ALIASES.get(
        (mode if isinstance(mode, str) else "").strip().lower()
    )
    if norm_mode is None:
        raise ValueError(
            f"Invalid sync mode {mode!r}; expected 'auto', 'tier1', or 'tier2'"
        )

    base_cfg = load_config() if config is None else config
    if allow_sample_rows is not None:
        cfg = base_cfg.model_copy(deep=True)
        cfg.privacy.allow_sample_rows = bool(allow_sample_rows)
    else:
        cfg = base_cfg

    in_path = (
        Path(input_path)
        if input_path is not None and str(input_path).strip()
        else None
    )

    results: dict[str, SyncSummary] = {}
    if norm_target == "databricks":
        results["databricks"] = sync_databricks(
            vdir,
            config=cfg,
            input_path=in_path,
            mode=norm_mode,
            allow_sample_rows=allow_sample_rows,
        )
        return results

    if norm_target == "powerbi":
        results["powerbi"] = sync_powerbi(
            vdir,
            config=cfg,
            input_path=in_path,
            mode=norm_mode,
        )
        return results

    if norm_target == "teams":
        results["teams"] = sync_teams(
            vdir,
            config=cfg,
            input_path=in_path,
        )
        return results

    if in_path is None:
        results["databricks"] = sync_databricks(
            vdir,
            config=cfg,
            input_path=None,
            mode=norm_mode,
            allow_sample_rows=allow_sample_rows,
        )
        results["powerbi"] = sync_powerbi(
            vdir,
            config=cfg,
            input_path=None,
            mode=norm_mode,
        )
        results["teams"] = sync_teams(
            vdir,
            config=cfg,
            input_path=None,
        )
        return results

    routed = _route_explicit_input(vdir, in_path)
    no_owners = in_path.is_file() and not any(routed.values())

    if routed["databricks"] is not None or (norm_mode == "tier1" and in_path.is_dir()):
        results["databricks"] = sync_databricks(
            vdir,
            config=cfg,
            input_path=routed["databricks"],
            mode=norm_mode,
            allow_sample_rows=allow_sample_rows,
        )
    else:
        dbx_warns = (
            [
                redact_crawler_text(
                    f"Skipped unsupported input file {in_path.name}",
                    redact_secrets=cfg.privacy.redact_secrets,
                )
            ]
            if no_owners
            else []
        )
        results["databricks"] = SyncSummary(
            target="databricks",
            mode_used="none",
            cards_written=0,
            warnings=dbx_warns,
        )

    if routed["powerbi"] is not None or (norm_mode == "tier1" and in_path.is_dir()):
        results["powerbi"] = sync_powerbi(
            vdir,
            config=cfg,
            input_path=routed["powerbi"],
            mode=norm_mode,
        )
    else:
        results["powerbi"] = SyncSummary(
            target="powerbi",
            mode_used="none",
            cards_written=0,
        )

    if routed["teams"] is not None:
        results["teams"] = sync_teams(
            vdir,
            config=cfg,
            input_path=routed["teams"],
        )
    else:
        results["teams"] = SyncSummary(
            target="teams",
            mode_used="none",
            cards_written=0,
        )
    return results


__all__ = [
    "SyncSummary",
    "detect_dax_heuristics",
    "sync_all",
    "sync_databricks",
    "sync_powerbi",
    "sync_teams",
]
