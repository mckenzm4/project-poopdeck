"""Zero-Node FastAPI Local Command Center UI & REST API."""

from __future__ import annotations

from collections import deque
import datetime
import math
from pathlib import Path
import re
import shutil
import tempfile
import threading
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)
import yaml

from poopdeck import __version__
from poopdeck.brief import (
    BriefResult,
    generate_opportunity_brief,
    generate_portfolio_brief,
    generate_verify_questions_brief,
)
from poopdeck.config import PoopdeckConfig, ScoringWeightsConfig, load_config
from poopdeck.models import (
    DatasetCard,
    OpenQuestionBank,
    OpportunitySpec,
    PlaybookPhase,
    PlaybookTracker,
    QuestionItem,
    ReportCard,
    ScheduledOpportunity,
    StakeholderCard,
    _normalize_question_category,
    compute_portfolio_schedule,
    compute_priority_score,
)
from poopdeck.packs import list_external_datasets, load_external_data_catalog
from poopdeck.vault_io import (
    ParsedCard,
    _resolve_playbook_path,
    _slugify_key,
    archive_opportunity,
    build_lineage_graph,
    extract_wikilinks,
    load_questions_and_playbook,
    normalize_wikilink_target,
    parse_markdown_card,
    promote_opportunity,
    render_playbook_body,
    render_questions_body,
    set_boss_mandate,
    update_question_or_phase,
    upsert_markdown_card,
    validate_vault,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

_LOCKS_GUARD = threading.Lock()
_VAULT_LOCKS: dict[Path, threading.RLock] = {}

_CATEGORY_LABELS: dict[str, str] = {
    "manager": "Hiring Manager",
    "it_security": "IT / Security & Platform Admin",
    "data_engineering": "Data Engineering",
    "power_bi": "Power BI & Analytics Owners",
    "business_finance": "Business Unit Leads & Finance",
}

_VALID_QUESTION_CATEGORIES: tuple[str, ...] = (
    "manager",
    "it_security",
    "data_engineering",
    "power_bi",
    "business_finance",
)

_CFO_SECTION_RE = re.compile(
    r"(?im)^#{1,3}\s+[^\n]*(?:skeptical\s+cfo|cfo\s+critique)[^\n]*$\r?\n?(.*?)(?=^#{1,2}\s+|\Z)",
    flags=re.DOTALL,
)

_CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'sameorigin'"
)

_SAFE_SCHEDULE_EFFORT_CEILING = 1e100


def _vault_lock(vault_dir: Path) -> threading.RLock:
    resolved = Path(vault_dir).resolve()
    with _LOCKS_GUARD:
        lock = _VAULT_LOCKS.get(resolved)
        if lock is None:
            lock = threading.RLock()
            _VAULT_LOCKS[resolved] = lock
        return lock


def _has_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in text)


def _escape_surrogates(text: str) -> str:
    return (
        text.encode("utf-8", errors="backslashreplace").decode("utf-8")
        if _has_surrogate(text)
        else text
    )


def _strict_str(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected a string value")
    if "\x00" in value:
        raise ValueError("Null bytes are not permitted")
    if _has_surrogate(value):
        raise ValueError("Unpaired UTF-16 surrogates are not permitted")
    return value


def _strict_lower_str(value: Any) -> str:
    text = _strict_no_ctrl_str(value).strip().lower()
    if not text:
        raise ValueError("String value must not be empty")
    return text


def _strict_no_ctrl_str(value: Any) -> str:
    raw = _strict_str(value)
    if any(
        ord(ch) < 32 or ord(ch) == 127 or 0xD800 <= ord(ch) <= 0xDFFF for ch in raw
    ):
        raise ValueError("Control characters are not permitted")
    return raw


def _ensure_clean_query_str(value: str | None, param_name: str) -> str | None:
    if value is None:
        return None
    if any(
        ord(ch) < 32 or ord(ch) == 127 or 0xD800 <= ord(ch) <= 0xDFFF for ch in value
    ):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid control or surrogate characters in {param_name}",
        )
    return value


def _assert_valid_json_tree(value: Any, max_depth: int = 64) -> Any:
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > max_depth:
            raise ValueError("Maximum JSON nesting depth exceeded")
        if (
            isinstance(node, float)
            and not isinstance(node, bool)
            and not math.isfinite(node)
        ):
            raise ValueError(
                "Out-of-range float values (NaN/Infinity) are not valid JSON"
            )
        if isinstance(node, str):
            if _has_surrogate(node):
                raise ValueError("Unpaired UTF-16 surrogates are not permitted")
        elif isinstance(node, dict):
            child_depth = depth + 1
            if child_depth > max_depth:
                raise ValueError("Maximum JSON nesting depth exceeded")
            for k, v in node.items():
                if isinstance(k, str) and _has_surrogate(k):
                    raise ValueError("Unpaired UTF-16 surrogates are not permitted")
                stack.append((k, child_depth))
                stack.append((v, child_depth))
        elif isinstance(node, (list, tuple)):
            child_depth = depth + 1
            if child_depth > max_depth:
                raise ValueError("Maximum JSON nesting depth exceeded")
            for item in node:
                stack.append((item, child_depth))
    return value


_assert_no_nonfinite_floats = _assert_valid_json_tree


def _sanitize_validation_err_value(
    obj: Any, _depth: int = 0, _max_depth: int = 32, _max_items: int = 128
) -> Any:
    if _depth >= _max_depth:
        return "[truncated nested input]"
    if isinstance(obj, float) and not math.isfinite(obj):
        return repr(obj)
    if isinstance(obj, str):
        return _escape_surrogates(obj)
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return _escape_surrogates(bytes(obj).decode("utf-8", errors="replace"))
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for idx, (k, v) in enumerate(obj.items()):
            if idx >= _max_items:
                out["__truncated__"] = f"[truncated after {_max_items} keys]"
                break
            out[_escape_surrogates(str(k))] = _sanitize_validation_err_value(
                v, _depth + 1, _max_depth, _max_items
            )
        return out
    if isinstance(obj, (list, tuple, set, frozenset)):
        res: list[Any] = []
        for idx, v in enumerate(obj):
            if idx >= _max_items:
                res.append(f"[truncated after {_max_items} items]")
                break
            res.append(
                _sanitize_validation_err_value(v, _depth + 1, _max_depth, _max_items)
            )
        return res
    if isinstance(obj, BaseException):
        return _escape_surrogates(str(obj))
    return obj


def _validate_safe_identifier(value: Any) -> str:
    raw = _strict_str(value)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise ValueError("Control characters are not permitted in identifier")
    cleaned = raw.strip()
    if not cleaned:
        raise ValueError("Identifier must not be empty or whitespace")
    if "/" in cleaned or "\\" in cleaned or ".." in cleaned:
        raise ValueError("Unsafe path traversal characters in identifier")
    return cleaned


def _validate_optional_safe_identifier(value: Any) -> str | None:
    if value is None:
        return None
    raw = _strict_str(value)
    if raw == "":
        return None
    return _validate_safe_identifier(raw)


def _strict_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("Expected strict boolean true or false")
    return value


def _parse_positive_float(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("Boolean is not a valid numeric value")
    if isinstance(value, str):
        text = _strict_no_ctrl_str(value).strip()
        if not text or text.lower() in {
            "true",
            "false",
            "nan",
            "+nan",
            "-nan",
            "inf",
            "+inf",
            "-inf",
            "infinity",
            "+infinity",
            "-infinity",
        }:
            raise ValueError("Expected a finite positive float")
        parsed = float(text)
    elif isinstance(value, (int, float)):
        parsed = float(value)
    else:
        raise ValueError("Expected a finite positive float")

    if not math.isfinite(parsed) or parsed <= 0.0:
        raise ValueError("Value must be finite and greater than 0")
    return parsed


StrictStr = Annotated[str, BeforeValidator(_strict_str)]
StrictNoCtrlStr = Annotated[str, BeforeValidator(_strict_no_ctrl_str)]
StrictLowerStr = Annotated[str, BeforeValidator(_strict_lower_str)]
SafeIdStr = Annotated[str, BeforeValidator(_validate_safe_identifier)]
OptionalSafeIdStr = Annotated[
    str | None, BeforeValidator(_validate_optional_safe_identifier)
]
StrictBool = Annotated[bool, BeforeValidator(_strict_bool)]
StrictPositiveFloat = Annotated[
    float,
    BeforeValidator(_parse_positive_float),
    Field(gt=0.0, allow_inf_nan=False),
]


class RequestBaseModel(BaseModel):
    """Strict base model for UI REST mutation & brief payloads."""

    model_config = ConfigDict(
        populate_by_name=True,
        extra="ignore",
        allow_inf_nan=False,
        validate_assignment=True,
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_nonfinite_json_floats(cls, data: Any) -> Any:
        return _assert_valid_json_tree(data)


class PromoteRequest(RequestBaseModel):
    """Payload for `POST /api/promote`."""

    opp_id: SafeIdStr = Field(validation_alias=AliasChoices("id", "opp_id"))
    target_status: Annotated[
        Literal["backlog", "active", "blocked", "shipped", "archived"],
        BeforeValidator(_strict_lower_str),
    ] = Field(
        default="backlog",
        validation_alias=AliasChoices("target_status", "status", "new_status"),
    )
    action: (
        Annotated[
            Literal["promote", "archive"],
            BeforeValidator(_strict_lower_str),
        ]
        | None
    ) = None
    reason: StrictStr | None = None

    @model_validator(mode="after")
    def _validate_promote_status(self) -> PromoteRequest:
        if self.action != "archive" and self.target_status not in {
            "backlog",
            "active",
            "blocked",
            "shipped",
        }:
            raise ValueError(
                "target_status for promote must be one of: backlog, active, blocked, shipped"
            )
        return self


class ArchiveRequest(RequestBaseModel):
    """Payload for `POST /api/archive`."""

    opp_id: SafeIdStr = Field(validation_alias=AliasChoices("id", "opp_id"))
    reason: StrictStr | None = None


class BossPinRequest(RequestBaseModel):
    """Payload for `POST /api/boss-pin`."""

    opp_id: SafeIdStr = Field(validation_alias=AliasChoices("id", "opp_id"))
    boss_mandate: StrictBool | None = Field(
        default=None,
        validation_alias=AliasChoices("boss_mandate", "mandate", "pinned"),
    )
    capacity_weeks: StrictPositiveFloat | None = Field(
        default=None,
        validation_alias=AliasChoices("capacity_weeks", "quarterly_capacity_weeks"),
    )
    domain: StrictNoCtrlStr | None = "all"
    include_archived: StrictBool | None = False


class QuestionUpdateRequest(RequestBaseModel):
    """Payload for `PATCH|POST /api/questions*` and `/api/readiness*`."""

    item_id: SafeIdStr | None = Field(
        default=None,
        validation_alias=AliasChoices("id", "item_id", "question_id", "phase_id"),
    )
    status: (
        Annotated[
            Literal["open", "answered", "blocked", "completed", "pending", "done"],
            BeforeValidator(_strict_lower_str),
        ]
        | None
    ) = None
    answer_summary: StrictStr | None = Field(
        default=None,
        validation_alias=AliasChoices("answer_summary", "answer", "notes"),
    )
    completed: StrictBool | None = None

    @model_validator(mode="after")
    def _require_non_empty_mutation(self) -> QuestionUpdateRequest:
        if (
            self.status is None
            and self.answer_summary is None
            and self.completed is None
        ):
            raise ValueError(
                "At least one of status, answer/answer_summary, or completed must be provided"
            )
        return self


class BriefRequest(RequestBaseModel):
    """Payload for `POST /api/brief`."""

    opp_id: OptionalSafeIdStr = Field(
        default=None,
        validation_alias=AliasChoices("opp_id", "id"),
    )
    brief_type: (
        Annotated[
            Literal[
                "opportunity",
                "weekly_status",
                "weekly",
                "portfolio",
                "verify_questions",
            ],
            BeforeValidator(_strict_lower_str),
        ]
        | None
    ) = Field(
        default=None,
        validation_alias=AliasChoices("brief_type", "type"),
    )
    fmt: Annotated[
        Literal["teams", "exec"],
        BeforeValidator(_strict_lower_str),
    ] = Field(
        default="teams",
        validation_alias=AliasChoices("format", "fmt"),
    )
    capacity_weeks: StrictPositiveFloat | None = Field(
        default=None,
        validation_alias=AliasChoices("capacity_weeks", "quarterly_capacity_weeks"),
    )
    save_export: StrictBool = False

    @model_validator(mode="after")
    def _validate_brief_target(self) -> BriefRequest:
        if self.brief_type in {"weekly", "portfolio"}:
            self.brief_type = "weekly_status"
        effective_type = self.brief_type
        if effective_type is None:
            if self.opp_id and self.opp_id.strip().lower() in {
                "weekly",
                "weekly_status",
                "portfolio",
                "verify_questions",
            }:
                return self
            effective_type = "opportunity"
        if effective_type == "opportunity" and not self.opp_id:
            raise ValueError("opp_id is required when brief_type='opportunity'")
        return self


_OPEN_FM_FENCE_RE = re.compile(r"\A---[ \t]*(?:\n|\Z)")
_CLOSE_FM_FENCE_RE = re.compile(r"(?m)^---[ \t]*(?:\n|\Z)")
_MD_HEADING_RE = re.compile(r"(?m)(?:\n[ \t]*\n#{1,6}[ \t]+|^#{2,6}[ \t]+)")
_LEAD_CMT_RE = re.compile(r"\A(?:[ \t]*\n|[ \t]*#[^\n]*(?:\n|\Z))*")
_FLOW_TOK_RE = re.compile(
    r"'(?:''|[^'])*'|\"(?:\\[\s\S]|[^\"\\])*\"|'(?:''|[^'])*\Z|\"(?:\\[\s\S]|[^\"\\])*\Z|(?:(?<=\s)|(?<=[{[,:])|\A)#[^\n]*|[{}\[\],:]|[^\s{}\[\],:'\"]+"
)
_COL0_SKIP_RE = re.compile(
    r"(?m)^(?![ \t#-])(?:'(?![iI][dD]')(?:''|[^'\n])*'[ \t]*:|\"(?![iI][dD]\")(?:\\.|[^\"\\\n])*\"[ \t]*:|(?![iI][dD][ \t]*:(?:[ \t\n]|\Z))[^:\s#'\"{}\[\]-][^:\n]*?[ \t]*:(?=[ \t\n]|\Z))[ \t]*(?:'(?:''|[^'])*'|\"(?:\\[\s\S]|[^\"\\])*\"|\{(?:[^{}'\"]|'(?:''|[^'])*'|\"(?:\\[\s\S]|[^\"\\])*\")*\}|\[(?:[^\[\]'\"]|'(?:''|[^'])*'|\"(?:\\[\s\S]|[^\"\\])*\")*\]|'(?:''|[^'])*\Z|\"(?:\\[\s\S]|[^\"\\])*\Z|\{[\s\S]*\Z)"
)
_COL0_ID_RE = re.compile(
    r"(?im)^(?:'id'[ \t]*:|\"id\"[ \t]*:|id[ \t]*:(?=[ \t\n]|\Z))[ \t]*(?:('(?:''|[^'])*'|\"(?:\\[\s\S]|[^\"\\])*\")[ \t]*(?:#[^\n]*)?$|([^\s{}\[\]|>#'\"][^\n]*?)(?:[ \t]+#[^\n]*)?$|$)"
)
_YAML_NON_ID = frozenset({"", "~", "null", "true", "false"})


def _ensure_untruncated_frontmatter(parsed: ParsedCard) -> None:
    raw = (
        (parsed.raw_text or "")
        .lstrip("\ufeff")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    m = _OPEN_FM_FENCE_RE.match(raw)
    if m is not None and _CLOSE_FM_FENCE_RE.search(raw[m.end() :]) is None:
        raise ValueError("Truncated or unclosed YAML frontmatter")


def _validate_utf8_str(
    value: str | None, field_name: str, *, forbid_ctrl: bool = False
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Invalid string type for {field_name}")
    if "\x00" in value:
        raise ValueError(f"Null bytes are not permitted in {field_name}")
    if forbid_ctrl and any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"Control characters are not permitted in {field_name}")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(
            f"Unpaired UTF-16 surrogates are not permitted in {field_name}"
        ) from None
    return value


class _AtomicVaultWrite:
    def __init__(
        self, paths: list[Path], watch_dirs: list[Path] | None = None
    ) -> None:
        self._orig: dict[Path, bytes | None] = {}
        for p in paths:
            rp = p.resolve()
            if rp not in self._orig:
                self._orig[rp] = rp.read_bytes() if rp.is_file() else None
        self._watch: dict[Path, set[Path]] = {}
        for d in watch_dirs or []:
            rd = d.resolve()
            self._watch[rd] = (
                {f.resolve() for f in rd.glob("*.md")} if rd.is_dir() else set()
            )

    def rollback(self) -> None:
        for rp, raw in self._orig.items():
            try:
                if raw is None:
                    rp.unlink(missing_ok=True)
                else:
                    rp.parent.mkdir(parents=True, exist_ok=True)
                    rp.write_bytes(raw)
            except OSError:
                pass
        for rd, pre_files in self._watch.items():
            if rd.is_dir():
                for cur in list(rd.glob("*.md")):
                    rc = cur.resolve()
                    if rc not in pre_files and rc not in self._orig:
                        try:
                            rc.unlink(missing_ok=True)
                        except OSError:
                            pass

    def __enter__(self) -> _AtomicVaultWrite:
        return self

    def has_changes(self) -> bool:
        for rp, raw in self._orig.items():
            if (rp.read_bytes() if rp.is_file() else None) != raw:
                return True
        for rd, pre_files in self._watch.items():
            if rd.is_dir() and any(
                f.resolve() not in pre_files for f in rd.glob("*.md")
            ):
                return True
        return False

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is not None:
            self.rollback()
        return False


def _is_target_write_failure(
    exc: Exception, tx: _AtomicVaultWrite, target_paths: tuple[Path, ...]
) -> bool:
    if isinstance(exc, UnicodeEncodeError) or tx.has_changes():
        return True
    if isinstance(exc, OSError):
        if not exc.filename:
            return True
        try:
            err_path = Path(exc.filename).resolve()
        except OSError:
            return True
        return any(err_path == p.resolve() for p in target_paths)
    return False


def _load_opportunities_safe(
    vault_dir: Path,
    *,
    include_staging: bool = False,
    include_archived: bool = False,
) -> tuple[list[tuple[Path, OpportunitySpec, ParsedCard]], list[str]]:
    """Load OpportunitySpec cards safely, capturing parse & duplicate-ID warnings instead of 500s."""
    vdir = Path(vault_dir)
    search_dirs: list[Path] = [vdir / "opportunities"]
    if include_staging:
        search_dirs.append(vdir / "staging" / "opportunities")

    loaded: list[tuple[Path, OpportunitySpec, ParsedCard]] = []
    warnings: list[str] = []
    seen_ids: dict[str, Path] = {}

    for folder in search_dirs:
        if not folder.is_dir():
            continue
        for md_path in sorted(folder.glob("*.md")):
            try:
                parsed = parse_markdown_card(md_path)
                _ensure_untruncated_frontmatter(parsed)
                opp = OpportunitySpec.model_validate(parsed.frontmatter)
                if opp.computed_priority_score is None:
                    opp.computed_priority_score = compute_priority_score(opp)
            except (ValidationError, ValueError, yaml.YAMLError, OSError) as exc:
                warnings.append(f"{md_path.name}: {exc}")
                continue

            norm_id = opp.id.strip().upper()
            first_path = seen_ids.get(norm_id)
            if first_path is not None:
                rel_cur = _relative_or_posix(md_path, vdir) or md_path.name
                rel_first = _relative_or_posix(first_path, vdir) or first_path.name
                warnings.append(
                    f"{rel_cur}: Duplicate opportunity id {opp.id!r} "
                    f"(already defined in {rel_first})"
                )
                continue
            seen_ids[norm_id] = md_path

            if not include_archived and opp.status == "archived":
                continue
            if not include_staging and opp.status == "staging":
                continue
            loaded.append((md_path, opp, parsed))

    loaded.sort(key=lambda item: (item[1].id, item[0].name))
    return loaded, warnings


def _find_opp_file_safe(
    folders: list[Path], opp_id: str
) -> tuple[Path, ParsedCard] | None:
    needle = opp_id.strip().upper()
    if not needle:
        return None
    for folder in folders:
        if not folder.is_dir():
            continue
        for md_path in sorted(folder.glob("*.md")):
            try:
                parsed = parse_markdown_card(md_path)
                _ensure_untruncated_frontmatter(parsed)
            except (ValidationError, ValueError, yaml.YAMLError, OSError):
                continue
            fm_id = str(parsed.frontmatter.get("id") or "").strip().upper()
            stem_up = md_path.stem.upper()
            if (
                fm_id == needle
                or stem_up == needle
                or stem_up.startswith(f"{needle}-")
            ):
                return md_path, parsed
    return None


def _unquote_yaml(t: str) -> str:
    return (
        ""
        if len(t) < 2 or t[-1] != t[0]
        else (
            t[1:-1].replace("''", "'").strip()
            if t[0] == "'"
            else re.sub(
                r"\\([\s\S])",
                lambda m: {"n": "\n", "r": "\r", "t": "\t"}.get(m.group(1), m.group(1)),
                t[1:-1],
            ).strip()
        )
    )


def _corrupt_file_matches_opp_id(md_path: Path, opp_id: str) -> bool:
    needle, stem_up = opp_id.strip().upper(), md_path.stem.upper()
    if not needle or stem_up == needle or stem_up.startswith(f"{needle}-"):
        return bool(needle)
    try:
        raw = (
            md_path.read_bytes()[:65536]
            .decode("utf-8-sig", errors="replace")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
        )
    except OSError:
        return False
    fm_open = _OPEN_FM_FENCE_RE.match(raw)
    after = (
        raw[fm_open.end() :]
        .split("<!-- HUMAN-NOTES-START", 1)[0]
        .split("<!-- BEGIN HUMAN-NOTES", 1)[0]
        if fm_open
        else ""
    )
    fc = _CLOSE_FM_FENCE_RE.search(after) if fm_open else None
    fm = (
        (
            after[: fc.start()]
            if fc is not None
            else _MD_HEADING_RE.split(after[:4096], maxsplit=1)[0]
        )
        if fm_open
        else ""
    )
    if not fm.strip():
        return False
    try:
        loaded = yaml.safe_load(fm)
    except (yaml.YAMLError, RecursionError, ValueError, TypeError):
        fm_fb = _MD_HEADING_RE.split(fm, maxsplit=1)[0]
        root, found = fm_fb[_LEAD_CMT_RE.match(fm_fb).end() :], None
        if root and root[0] == "{":
            b, k, i, ek, toks = (
                0,
                0,
                0,
                False,
                [t for t in _FLOW_TOK_RE.findall(root) if t[0] != "#"],
            )
            while i < len(toks):
                t = toks[i]
                if b == 1 and k == 0 and ek and t not in "{},[]:":
                    if t[0] in "'\"" and (len(t) < 2 or t[-1] != t[0]):
                        break
                    key, ek = (_unquote_yaml(t) if t[0] in "'\"" else t), False
                    if i + 1 < len(toks) and toks[i + 1] == ":":
                        i += 2
                        if key.lower() == "id":
                            found = (
                                (
                                    (
                                        _unquote_yaml(vt)
                                        if vt[0] in "'\""
                                        else (
                                            ""
                                            if vt.lower() in _YAML_NON_ID
                                            else vt
                                        )
                                    )
                                    or None
                                )
                                if i < len(toks) and (vt := toks[i]) not in "{},[]:"
                                else None
                            )
                        continue
                elif t in "{}":
                    b += 1 if t == "{" else -1
                    if b <= 0:
                        break
                    ek = t == "{" and b == 1 and k == 0
                elif t in "[]":
                    k = k + 1 if t == "[" else max(0, k - 1)
                elif t == "," and b == 1 and k == 0:
                    ek = True
                i += 1
        elif root and root[0] != "[":
            for m in _COL0_ID_RE.finditer(_COL0_SKIP_RE.sub("", fm_fb)):
                qv, pv = m.group(1), (m.group(2) or "").strip()
                found = (
                    _unquote_yaml(qv)
                    if qv
                    else ("" if pv.lower() in _YAML_NON_ID else pv)
                ) or None
        return bool(found) and found.upper() == needle
    if not isinstance(loaded, dict):
        return False
    vals = [
        v for k, v in loaded.items() if isinstance(k, str) and k.strip().lower() == "id"
    ]
    val = vals[-1] if vals else None
    return (
        isinstance(val, (str, int, float))
        and not isinstance(val, bool)
        and str(val).strip().upper() == needle
    )


def _file_matches_opp_needle(
    md_path: Path, parsed: ParsedCard | None, needle: str
) -> bool:
    if parsed is None:
        return _corrupt_file_matches_opp_id(md_path, needle)
    raw_id = parsed.frontmatter.get("id")
    fm_id = (
        raw_id.strip().upper()
        if isinstance(raw_id, str)
        else str(raw_id or "").strip().upper()
    )
    stem_up = md_path.stem.upper()
    return (
        fm_id == needle
        or stem_up == needle
        or stem_up.startswith(f"{needle}-")
    )


def _locate_target_opportunity(
    folders: list[Path], opp_id: str
) -> tuple[Path, ParsedCard]:
    """Find unique target card; raise KeyError if missing, ValueError if duplicated, or parse error if corrupt."""
    needle = opp_id.strip().upper()
    if not needle:
        raise KeyError("Opportunity id must not be empty")

    scanned: list[tuple[Path, ParsedCard | None, Exception | None]] = []
    for folder in folders:
        if not folder.is_dir():
            continue
        for md_path in sorted(folder.glob("*.md")):
            try:
                parsed = parse_markdown_card(md_path)
                _ensure_untruncated_frontmatter(parsed)
                OpportunitySpec.model_validate(parsed.frontmatter)
                scanned.append((md_path, parsed, None))
            except (ValidationError, ValueError, yaml.YAMLError, OSError) as exc:
                scanned.append((md_path, None, exc))

    matches = [
        (md_path, parsed, exc)
        for md_path, parsed, exc in scanned
        if _file_matches_opp_needle(md_path, parsed, needle)
    ]
    if not matches:
        raise KeyError(f"Opportunity '{opp_id}' not found")
    if len(matches) > 1:
        names = ", ".join(m[0].name for m in matches)
        raise ValueError(
            f"Duplicate opportunity id '{opp_id}' matched multiple files: {names}"
        )

    matched_path, matched_parsed, matched_exc = matches[0]
    if matched_exc is not None or matched_parsed is None:
        assert matched_exc is not None
        raise matched_exc

    raw_fm_id = matched_parsed.frontmatter.get("id")
    canonical_id = (
        raw_fm_id.strip().upper()
        if isinstance(raw_fm_id, str)
        else str(raw_fm_id or "").strip().upper()
    )
    if canonical_id and canonical_id != needle:
        id_matches = [
            md_path
            for md_path, parsed, _ in scanned
            if _file_matches_opp_needle(md_path, parsed, canonical_id)
        ]
        if len(id_matches) > 1:
            names = ", ".join(p.name for p in id_matches)
            raise ValueError(
                f"Duplicate opportunity id '{canonical_id}' matched multiple files: {names}"
            )

    return matched_path, matched_parsed


def _ensure_no_live_opp_collision(
    vault_dir: Path, src_path: Path, opp_id: str
) -> None:
    """Reject staged -> live move if opportunities/ already owns target filename or opp_id."""
    live_dir = vault_dir / "opportunities"
    dest_path = live_dir / src_path.name
    if src_path.resolve() == dest_path.resolve():
        return
    if dest_path.exists():
        raise ValueError(
            f"Cannot overwrite existing live opportunity file '{dest_path.name}' "
            f"for opportunity '{opp_id}'"
        )
    if not live_dir.is_dir():
        return
    needle = opp_id.strip().upper()
    if not needle:
        return
    for live_md in sorted(live_dir.glob("*.md")):
        if live_md.resolve() == src_path.resolve():
            continue
        try:
            live_parsed = parse_markdown_card(live_md)
            _ensure_untruncated_frontmatter(live_parsed)
            live_opp = OpportunitySpec.model_validate(live_parsed.frontmatter)
        except (ValidationError, ValueError, yaml.YAMLError, OSError):
            if _corrupt_file_matches_opp_id(live_md, needle):
                raise ValueError(
                    f"Live opportunities/ already contains corrupt card '{live_md.name}' "
                    f"matching '{opp_id}'"
                )
            continue
        if live_opp.id.strip().upper() == needle:
            raise ValueError(
                f"Live opportunities/ already contains '{live_md.name}' "
                f"with duplicate id '{live_opp.id}'"
            )


def _extract_cfo_critique(opp: OpportunitySpec, parsed: ParsedCard | None) -> str:
    if parsed is not None:
        raw_fm = parsed.frontmatter.get("cfo_critique")
        if isinstance(raw_fm, str) and raw_fm.strip():
            return raw_fm.strip()
        match = _CFO_SECTION_RE.search(parsed.body or "")
        if match and match.group(1).strip():
            return match.group(1).strip()

    parts: list[str] = []
    if opp.cfo_critique_verdict:
        parts.append(f"Verdict: {opp.cfo_critique_verdict}")
    for note in opp.cfo_critique_notes:
        if note and note.strip():
            parts.append(f"- {note.strip()}")
    return "\n".join(parts)


def _relative_or_posix(md_path: Path | None, vault_dir: Path | None) -> str | None:
    if md_path is None:
        return None
    resolved_md = md_path.resolve()
    if vault_dir is not None:
        resolved_vault = vault_dir.resolve()
        if resolved_md.is_relative_to(resolved_vault):
            return resolved_md.relative_to(resolved_vault).as_posix()
    return resolved_md.as_posix()


def _serialize_opportunity(
    opp: OpportunitySpec,
    *,
    md_path: Path | None = None,
    vault_dir: Path | None = None,
    parsed: ParsedCard | None = None,
    sched_item: ScheduledOpportunity | None = None,
    include_frontmatter: bool = False,
) -> dict[str, Any]:
    data = opp.model_dump(mode="json")
    prompts = list(opp.valuation.verification_prompts)
    low_usd = float(opp.valuation.annual_benefit_low_usd or 0.0)
    base_usd = float(opp.valuation.annual_benefit_base_usd or 0.0)
    high_usd = float(opp.valuation.annual_benefit_high_usd or 0.0)
    unverified_count = int(opp.valuation.unverified_drivers_count)

    val = dict(data.get("valuation") or {})
    val["verification_prompts"] = list(prompts)
    val["verify_questions"] = list(prompts)
    val["verify_prompts"] = list(prompts)
    val["unverified_drivers_count"] = unverified_count
    val["low_usd"] = low_usd
    val["base_usd"] = base_usd
    val["high_usd"] = high_usd
    data["valuation"] = val

    data["filename"] = md_path.name if md_path is not None else f"{opp.id}.md"
    data["path"] = _relative_or_posix(md_path, vault_dir)
    data["confidence_tier"] = opp.valuation.confidence_tier
    data["annual_benefit_low_usd"] = low_usd
    data["annual_benefit_base_usd"] = base_usd
    data["annual_benefit_high_usd"] = high_usd
    data["unverified_drivers_count"] = unverified_count
    data["verification_prompts"] = list(prompts)
    data["verify_questions"] = list(prompts)
    data["verify_prompts"] = list(prompts)
    data["cfo_critique"] = _extract_cfo_critique(opp, parsed)
    data["body"] = parsed.body if parsed is not None else ""
    data["human_notes"] = parsed.human_notes if parsed is not None else None

    if include_frontmatter:
        data["frontmatter"] = (
            jsonable_encoder(parsed.frontmatter) if parsed is not None else {}
        )

    if sched_item is not None:
        data["computed_priority_score"] = sched_item.computed_priority_score
        data["rank"] = sched_item.rank
        data["baseline_rank"] = sched_item.baseline_rank
        data["cumulative_effort_weeks"] = sched_item.cumulative_effort_weeks
        data["above_cutline"] = sched_item.above_cutline
        data["baseline_above_cutline"] = sched_item.baseline_above_cutline
        data["displaced_by_mandate"] = sched_item.displaced_by_mandate
        data["displaced"] = sched_item.displaced_by_mandate
    else:
        data["rank"] = None
        data["baseline_rank"] = None
        data["cumulative_effort_weeks"] = None
        data["above_cutline"] = False
        data["baseline_above_cutline"] = False
        data["displaced_by_mandate"] = False
        data["displaced"] = False

    return data


def _list_available_domains(extra_domains: list[str] | None = None) -> list[str]:
    domains: list[str] = []
    try:
        catalog = load_external_data_catalog()
        domains.extend(catalog.domain_lenses.keys())
    except (FileNotFoundError, ValueError, ValidationError, yaml.YAMLError):
        domains.extend(
            [
                "ops_supply_chain",
                "retail_cpg",
                "finance_risk",
                "b2b_saas",
                "manufacturing",
                "generic",
            ]
        )
    for dom in extra_domains or []:
        clean = dom.strip()
        if clean and clean not in domains:
            domains.append(clean)
    return domains


def _build_portfolio_payload(
    vault_dir: Path,
    cfg: PoopdeckConfig,
    *,
    capacity_weeks: float | None = None,
    domain: str = "all",
    include_archived: bool = False,
) -> dict[str, Any]:
    cap = (
        float(capacity_weeks)
        if capacity_weeks is not None
        else float(cfg.quarterly_capacity_weeks)
    )
    with _vault_lock(vault_dir):
        loaded, warnings = _load_opportunities_safe(
            vault_dir,
            include_staging=False,
            include_archived=include_archived,
        )
        all_with_staging, staging_warnings = _load_opportunities_safe(
            vault_dir,
            include_staging=True,
            include_archived=False,
        )
    for w in staging_warnings:
        if w not in warnings:
            warnings.append(w)

    staging_count = sum(1 for _, o, _ in all_with_staging if o.status == "staging")
    seen_domains = [o.domain_pack for _, o, _ in loaded if o.domain_pack]
    available_domains = _list_available_domains(seen_domains)

    raw_dom = (domain or "all").strip().lower()
    if raw_dom == "general":
        raw_dom = "generic"
    if raw_dom not in {"", "all", "*"}:
        filtered = [
            (p, o, pc)
            for p, o, pc in loaded
            if o.domain_pack.strip().lower() == raw_dom
        ]
    else:
        raw_dom = "all"
        filtered = list(loaded)

    weights = (
        cfg.scoring_weights
        if cfg.scoring_weights != ScoringWeightsConfig()
        else None
    )
    raw_opps = [o for _, o, _ in filtered]
    try:
        schedule = compute_portfolio_schedule(
            raw_opps,
            quarterly_capacity_weeks=cap,
            weights=weights,
        )
    except (ValidationError, OverflowError):
        if not (
            any(float(o.effort_weeks) > _SAFE_SCHEDULE_EFFORT_CEILING for o in raw_opps)
            or cap > _SAFE_SCHEDULE_EFFORT_CEILING
        ):
            raise
        clamped_opps = [
            o.model_copy(
                update={
                    "effort_weeks": min(
                        float(o.effort_weeks), _SAFE_SCHEDULE_EFFORT_CEILING
                    )
                }
            )
            if float(o.effort_weeks) > _SAFE_SCHEDULE_EFFORT_CEILING
            else o
            for o in raw_opps
        ]
        schedule = compute_portfolio_schedule(
            clamped_opps,
            quarterly_capacity_weeks=min(cap, _SAFE_SCHEDULE_EFFORT_CEILING),
            weights=weights,
        )
        warnings.append(
            "Cumulative effort_weeks overflowed float64; clamped schedule totals "
            f"to {_SAFE_SCHEDULE_EFFORT_CEILING:g}w ceiling."
        )

    by_id: dict[str, tuple[Path, OpportunitySpec, ParsedCard]] = {
        o.id: (p, o, pc) for p, o, pc in filtered
    }
    scheduled_ids: set[str] = set()
    enriched_scheduled: list[dict[str, Any]] = []
    for s_item in schedule.items:
        scheduled_ids.add(s_item.id)
        md_path, opp, parsed = by_id[s_item.id]
        enriched_scheduled.append(
            _serialize_opportunity(
                opp,
                md_path=md_path,
                vault_dir=vault_dir,
                parsed=parsed,
                sched_item=s_item,
            )
        )

    non_scheduled: list[dict[str, Any]] = []
    for md_path, opp, parsed in filtered:
        if opp.id not in scheduled_ids:
            non_scheduled.append(
                _serialize_opportunity(
                    opp,
                    md_path=md_path,
                    vault_dir=vault_dir,
                    parsed=parsed,
                    sched_item=None,
                )
            )

    combined_items = enriched_scheduled + non_scheduled
    displaced_titles = [
        s_item.title for s_item in schedule.items if s_item.displaced_by_mandate
    ]

    schedule_dump = schedule.model_dump(mode="json")
    schedule_dump["items"] = enriched_scheduled

    cutline_summary = {
        "capacity_weeks": schedule.capacity_weeks,
        "used_capacity_weeks": schedule.used_capacity_weeks,
        "mandate_weeks": schedule.mandate_weeks,
        "remaining_capacity_weeks": schedule.remaining_capacity_weeks,
        "overflow_weeks": schedule.overflow_weeks,
        "over_capacity": schedule.over_capacity,
        "over_capacity_warning": schedule.over_capacity,
        "funded_count": len(schedule.funded_ids),
        "displaced_count": len(schedule.displaced_ids),
        "funded_ids": list(schedule.funded_ids),
        "displaced_ids": list(schedule.displaced_ids),
        "displaced_titles": displaced_titles,
    }

    return {
        "capacity_weeks": schedule.capacity_weeks,
        "used_capacity_weeks": schedule.used_capacity_weeks,
        "mandate_weeks": schedule.mandate_weeks,
        "remaining_capacity_weeks": schedule.remaining_capacity_weeks,
        "overflow_weeks": schedule.overflow_weeks,
        "over_capacity": schedule.over_capacity,
        "over_capacity_warning": schedule.over_capacity,
        "funded_ids": list(schedule.funded_ids),
        "displaced_ids": list(schedule.displaced_ids),
        "displaced_titles": displaced_titles,
        "staging_count": staging_count,
        "domain": raw_dom,
        "default_domain": cfg.domain,
        "available_domains": available_domains,
        "include_archived": bool(include_archived),
        "cutline_summary": cutline_summary,
        "schedule": schedule_dump,
        "opportunities": combined_items,
        "items": combined_items,
        "warnings": warnings,
    }


def _collect_lineage_warnings(
    vault_dir: Path, *, include_staging: bool
) -> tuple[list[str], bool]:
    vdir = Path(vault_dir)
    warnings: list[str] = []
    has_corrupt_opp = False

    entity_specs: list[tuple[Path, Any]] = [
        (vdir / "datasets", DatasetCard),
        (vdir / "reports", ReportCard),
        (vdir / "stakeholders", StakeholderCard),
    ]
    for folder, model_cls in entity_specs:
        if not folder.is_dir():
            continue
        for md_path in sorted(folder.glob("*.md")):
            try:
                parsed = parse_markdown_card(md_path)
                _ensure_untruncated_frontmatter(parsed)
                model_cls.model_validate(parsed.frontmatter)
            except (ValidationError, ValueError, yaml.YAMLError, OSError) as exc:
                warnings.append(f"{md_path.name}: {exc}")

    opp_dirs = [vdir / "opportunities"]
    if include_staging:
        opp_dirs.append(vdir / "staging" / "opportunities")
    seen_opp_ids: dict[str, Path] = {}
    for folder in opp_dirs:
        if not folder.is_dir():
            continue
        for md_path in sorted(folder.glob("*.md")):
            try:
                parsed = parse_markdown_card(md_path)
                _ensure_untruncated_frontmatter(parsed)
                opp = OpportunitySpec.model_validate(parsed.frontmatter)
            except (ValidationError, ValueError, yaml.YAMLError, OSError) as exc:
                has_corrupt_opp = True
                warnings.append(f"{md_path.name}: {exc}")
                continue
            norm_id = opp.id.strip().upper()
            first_path = seen_opp_ids.get(norm_id)
            if first_path is not None:
                has_corrupt_opp = True
                rel_cur = _relative_or_posix(md_path, vdir) or md_path.name
                rel_first = _relative_or_posix(first_path, vdir) or first_path.name
                warnings.append(
                    f"{rel_cur}: Duplicate opportunity id {opp.id!r} "
                    f"(already defined in {rel_first})"
                )
            else:
                seen_opp_ids[norm_id] = md_path

    return warnings, has_corrupt_opp


def _build_lineage_graph_resilient(
    vault_dir: Path, *, include_staging: bool
) -> dict[str, list[dict[str, Any]]]:
    """Fallback lineage builder skipping corrupt opportunity cards cleanly."""
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

    loaded_datasets: list[tuple[str, DatasetCard]] = []
    ds_dir = vdir / "datasets"
    if ds_dir.is_dir():
        for md_path in sorted(ds_dir.glob("*.md")):
            try:
                parsed = parse_markdown_card(md_path)
                card = DatasetCard.model_validate(parsed.frontmatter)
            except (ValidationError, ValueError, yaml.YAMLError, OSError):
                continue
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
            loaded_datasets.append((nid, card))

    loaded_reports: list[tuple[str, ReportCard]] = []
    rep_dir = vdir / "reports"
    if rep_dir.is_dir():
        for md_path in sorted(rep_dir.glob("*.md")):
            try:
                parsed = parse_markdown_card(md_path)
                card = ReportCard.model_validate(parsed.frontmatter)
            except (ValidationError, ValueError, yaml.YAMLError, OSError):
                continue
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
            loaded_reports.append((nid, card))

    loaded_stakeholders: list[tuple[str, StakeholderCard]] = []
    sh_dir = vdir / "stakeholders"
    if sh_dir.is_dir():
        for md_path in sorted(sh_dir.glob("*.md")):
            try:
                parsed = parse_markdown_card(md_path)
                card = StakeholderCard.model_validate(parsed.frontmatter)
            except (ValidationError, ValueError, yaml.YAMLError, OSError):
                continue
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
            loaded_stakeholders.append((nid, card))

    loaded_opps, _ = _load_opportunities_safe(
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

    for ds_nid, card in loaded_datasets:
        for up in card.upstream_refs:
            _add_edge(_resolve_or_placeholder(up, "dataset"), ds_nid, "feeds_dataset")
        for down in card.downstream_refs:
            _add_edge(ds_nid, _resolve_or_placeholder(down, "dataset"), "feeds_dataset")

    for rep_nid, card in loaded_reports:
        for ds_ref in card.upstream_datasets:
            _add_edge(
                _resolve_or_placeholder(ds_ref, "dataset"), rep_nid, "feeds_report"
            )
        for owner_ref in card.owners:
            _add_edge(
                _resolve_or_placeholder(owner_ref, "stakeholder"),
                rep_nid,
                "owns_report",
            )

    for sh_nid, card in loaded_stakeholders:
        for rep_ref in card.owned_reports:
            _add_edge(
                sh_nid,
                _resolve_or_placeholder(rep_ref, "existing_report"),
                "owns_report",
            )

    for opp_nid, opp, parsed in opp_nodes:
        structured_keys: set[str] = set()
        for raw_s in (
            *(d.ref for d in opp.internal_datasets),
            *(r.ref for r in opp.existing_reports_supplemented),
            *opp.stakeholders,
        ):
            norm_s = normalize_wikilink_target(raw_s)
            if norm_s:
                structured_keys.add(norm_s.casefold())
                s_slug = _slugify_key(norm_s)
                if s_slug:
                    structured_keys.add(s_slug)

        for ds_use in opp.internal_datasets:
            _add_edge(
                _resolve_or_placeholder(ds_use.ref, "dataset"),
                opp_nid,
                "inputs_to_opportunity",
            )
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
            _add_edge(
                opp_nid,
                _resolve_or_placeholder(sup.ref, "existing_report"),
                "supplements_report",
            )
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
            _add_edge(
                _resolve_or_placeholder(sh_ref, "stakeholder"),
                opp_nid,
                "stakeholder_for",
            )
        for body_link in extract_wikilinks(parsed.body):
            key = body_link.casefold()
            slug = _slugify_key(body_link)
            if key in structured_keys or (slug and slug in structured_keys):
                continue
            matched_nid = alias_to_node_id.get(key) or alias_to_node_id.get(slug)
            if matched_nid and matched_nid != opp_nid:
                t_type = nodes_by_id[matched_nid]["type"]
                if t_type == "dataset":
                    _add_edge(matched_nid, opp_nid, "inputs_to_opportunity")
                elif t_type == "existing_report":
                    _add_edge(opp_nid, matched_nid, "supplements_report")
                elif t_type == "stakeholder":
                    _add_edge(matched_nid, opp_nid, "stakeholder_for")

    return {"nodes": list(nodes_by_id.values()), "edges": edges}


def _match_focus_probe(
    nodes: list[dict[str, Any]],
    needle: str,
    *,
    allow_slug: bool = True,
) -> str | None:
    if not needle:
        return None
    cf = needle.casefold()

    for node in nodes:
        if node["id"] == needle:
            return str(node["id"])

    for node in nodes:
        if str(node["id"]).casefold() == cf:
            return str(node["id"])

    for node in nodes:
        nid = str(node["id"])
        suffix = nid.split(":", 1)[1] if ":" in nid else nid
        meta = node.get("metadata") or {}
        candidates = (
            suffix,
            str(node.get("label") or ""),
            str(meta.get("opp_id") or ""),
            str(meta.get("fqn") or ""),
        )
        for cand in candidates:
            if cand and (cand == needle or cand.casefold() == cf):
                return nid

    if allow_slug:
        slug = _slugify_key(needle)
        if slug:
            for node in nodes:
                nid = str(node["id"])
                suffix = nid.split(":", 1)[1] if ":" in nid else nid
                meta = node.get("metadata") or {}
                candidates = (
                    suffix,
                    str(node.get("label") or ""),
                    str(meta.get("opp_id") or ""),
                    str(meta.get("fqn") or ""),
                )
                for cand in candidates:
                    if cand and _slugify_key(cand) == slug:
                        return nid
    return None


def _match_focus_node(nodes: list[dict[str, Any]], focus_id: str) -> str | None:
    raw = focus_id.strip()
    if not raw:
        return None

    if raw.startswith("[[") and raw.endswith("]]"):
        inner = raw[2:-2].strip()
        if inner:
            matched = _match_focus_probe(nodes, inner, allow_slug=False)
            if matched is not None:
                return matched
        clean = normalize_wikilink_target(raw).strip()
        if clean:
            return _match_focus_probe(nodes, clean, allow_slug=True)
        return None

    matched = _match_focus_probe(nodes, raw, allow_slug=True)
    if matched is not None:
        return matched

    clean = normalize_wikilink_target(raw).strip()
    if clean and clean != raw:
        return _match_focus_probe(nodes, clean, allow_slug=True)
    return None


def _filter_lineage_cone(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    matched_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    parents: dict[str, list[str]] = {}
    children: dict[str, list[str]] = {}
    for edge in edges:
        src = str(edge["source"])
        dst = str(edge["target"])
        children.setdefault(src, []).append(dst)
        parents.setdefault(dst, []).append(src)

    ancestors: set[str] = {matched_id}
    q_up: deque[str] = deque([matched_id])
    while q_up:
        curr = q_up.popleft()
        for par in parents.get(curr, ()):
            if par not in ancestors:
                ancestors.add(par)
                q_up.append(par)

    descendants: set[str] = {matched_id}
    q_down: deque[str] = deque([matched_id])
    while q_down:
        curr = q_down.popleft()
        for ch in children.get(curr, ()):
            if ch not in descendants:
                descendants.add(ch)
                q_down.append(ch)

    keep = ancestors | descendants
    filtered_nodes = [n for n in nodes if n["id"] in keep]
    filtered_edges = [
        e for e in edges if e["source"] in keep and e["target"] in keep
    ]
    return filtered_nodes, filtered_edges


def _serialize_question(q: QuestionItem) -> dict[str, Any]:
    d = q.model_dump(mode="json")
    d["category_label"] = _CATEGORY_LABELS.get(q.category, q.category)
    d["owner"] = q.who_to_ask
    d["prompt"] = q.question
    d["answer"] = q.answer_summary
    return d


def _serialize_phase(p: PlaybookPhase) -> dict[str, Any]:
    d = p.model_dump(mode="json")
    d["items"] = list(p.actions)
    d["status"] = "completed" if p.completed else "open"
    return d


def _load_questions_and_playbook_detailed(
    vault_dir: Path,
) -> tuple[
    OpenQuestionBank,
    Exception | None,
    PlaybookTracker,
    Exception | None,
    list[str],
]:
    vdir = Path(vault_dir)
    q_path = vdir / "questions" / "open-questions.md"
    pb_path = _resolve_playbook_path(vdir)
    try:
        bank, tracker = load_questions_and_playbook(vault_dir)
        if q_path.is_file():
            _ensure_untruncated_frontmatter(parse_markdown_card(q_path))
        if pb_path.is_file():
            _ensure_untruncated_frontmatter(parse_markdown_card(pb_path))
        return bank, None, tracker, None, []
    except (ValidationError, ValueError, yaml.YAMLError, OSError) as exc:
        root_exc: Exception = exc

    file_warnings: list[str] = []
    q_err: Exception | None = None
    pb_err: Exception | None = None

    bank = OpenQuestionBank(questions=[])
    if q_path.is_file():
        try:
            parsed_q = parse_markdown_card(q_path)
            _ensure_untruncated_frontmatter(parsed_q)
            bank = OpenQuestionBank.model_validate(parsed_q.frontmatter)
        except (ValidationError, ValueError, yaml.YAMLError, OSError) as exc:
            q_err = exc
            file_warnings.append(f"{q_path.name}: {exc}")

    tracker = PlaybookTracker(phases=[])
    if pb_path.is_file():
        try:
            parsed_pb = parse_markdown_card(pb_path)
            _ensure_untruncated_frontmatter(parsed_pb)
            tracker = PlaybookTracker.model_validate(parsed_pb.frontmatter)
        except (ValidationError, ValueError, yaml.YAMLError, OSError) as exc:
            pb_err = exc
            file_warnings.append(f"{pb_path.name}: {exc}")

    warnings = file_warnings if file_warnings else [str(root_exc)]
    return bank, q_err, tracker, pb_err, warnings


def _load_questions_and_playbook_safe(
    vault_dir: Path,
) -> tuple[OpenQuestionBank, PlaybookTracker, list[str]]:
    bank, _, tracker, _, warnings = _load_questions_and_playbook_detailed(vault_dir)
    out_warnings = list(warnings)

    seen_q: set[str] = set()
    unique_questions: list[QuestionItem] = []
    for q in bank.questions:
        qid = q.id.strip().upper()
        if qid in seen_q:
            out_warnings.append(
                f"open-questions.md: Duplicate question id {q.id!r}; skipped duplicate"
            )
            continue
        seen_q.add(qid)
        unique_questions.append(q)
    if len(unique_questions) != len(bank.questions):
        bank = bank.model_copy(update={"questions": unique_questions})

    seen_p: set[str] = set()
    unique_phases: list[PlaybookPhase] = []
    for p in tracker.phases:
        pid = p.id.strip().upper()
        if pid in seen_p:
            out_warnings.append(
                f"day1-90-playbook.md: Duplicate playbook phase id {p.id!r}; skipped duplicate"
            )
            continue
        seen_p.add(pid)
        unique_phases.append(p)
    if len(unique_phases) != len(tracker.phases):
        tracker = tracker.model_copy(update={"phases": unique_phases})

    return bank, tracker, out_warnings


def _build_readiness_summary(
    bank: OpenQuestionBank,
    tracker: PlaybookTracker,
    *,
    filtered_count: int | None = None,
) -> dict[str, Any]:
    total_q = len(bank.questions)
    answered_q = sum(1 for q in bank.questions if q.status == "answered")
    open_q = sum(1 for q in bank.questions if q.status == "open")
    blocked_q = sum(1 for q in bank.questions if q.status == "blocked")

    by_cat: dict[str, dict[str, int]] = {
        cat: {"total": 0, "answered": 0, "open": 0, "blocked": 0}
        for cat in _VALID_QUESTION_CATEGORIES
    }
    for q in bank.questions:
        bucket = by_cat.setdefault(
            q.category, {"total": 0, "answered": 0, "open": 0, "blocked": 0}
        )
        bucket["total"] += 1
        bucket[q.status] = bucket.get(q.status, 0) + 1

    total_p = len(tracker.phases)
    completed_p = sum(1 for p in tracker.phases if p.completed)

    playbook_pct = round((completed_p / total_p) * 100.0, 2) if total_p else 0.0
    question_pct = round((answered_q / total_q) * 100.0, 2) if total_q else 0.0
    if total_p and total_q:
        readiness_pct = round((playbook_pct + question_pct) / 2.0, 2)
    elif total_p:
        readiness_pct = playbook_pct
    elif total_q:
        readiness_pct = question_pct
    else:
        readiness_pct = 0.0

    return {
        "total_questions": total_q,
        "filtered_questions_count": (
            filtered_count if filtered_count is not None else total_q
        ),
        "answered": answered_q,
        "answered_count": answered_q,
        "open": open_q,
        "open_count": open_q,
        "blocked": blocked_q,
        "blocked_count": blocked_q,
        "by_category": by_cat,
        "completed_phases": completed_p,
        "total_phases": total_p,
        "playbook_progress_pct": playbook_pct,
        "question_progress_pct": question_pct,
        "readiness_pct": readiness_pct,
    }


def _vault_has_duplicate_opp_ids(vault_dir: Path) -> bool:
    """Return True if multiple valid opportunity cards across opportunities/ and staging/ share an id."""
    seen: set[str] = set()
    for rel_sub in ("opportunities", "staging/opportunities"):
        folder = vault_dir / rel_sub
        if not folder.is_dir():
            continue
        for md_path in sorted(folder.glob("*.md")):
            try:
                parsed = parse_markdown_card(md_path)
                _ensure_untruncated_frontmatter(parsed)
                opp = OpportunitySpec.model_validate(parsed.frontmatter)
            except (ValidationError, ValueError, yaml.YAMLError, OSError):
                continue
            norm_id = opp.id.strip().upper()
            if norm_id in seen:
                return True
            seen.add(norm_id)
    return False


def _run_brief_in_shadow_vault(
    vault_dir: Path,
    *,
    resolved_kind: Literal["opportunity", "weekly_status", "verify_questions"],
    canonical_opp_id: str | None,
    mode: str,
    cap: float,
) -> tuple[BriefResult, list[str]]:
    warnings: list[str] = []
    with tempfile.TemporaryDirectory() as tmp_dir:
        shadow_vdir = Path(tmp_dir) / "vault"
        seen_ids: dict[str, Path] = {}
        for rel_sub in ("opportunities", "staging/opportunities"):
            src_dir = vault_dir / rel_sub
            if not src_dir.is_dir():
                continue
            dst_dir = shadow_vdir / rel_sub
            for md_path in sorted(src_dir.glob("*.md")):
                try:
                    parsed = parse_markdown_card(md_path)
                    _ensure_untruncated_frontmatter(parsed)
                    opp = OpportunitySpec.model_validate(parsed.frontmatter)
                except (ValidationError, ValueError, yaml.YAMLError, OSError) as exc:
                    warnings.append(f"{md_path.name}: {exc}")
                    continue
                norm_id = opp.id.strip().upper()
                first_path = seen_ids.get(norm_id)
                if first_path is not None:
                    rel_cur = _relative_or_posix(md_path, vault_dir) or md_path.name
                    rel_first = (
                        _relative_or_posix(first_path, vault_dir) or first_path.name
                    )
                    warnings.append(
                        f"{rel_cur}: Duplicate opportunity id {opp.id!r} "
                        f"(already defined in {rel_first})"
                    )
                    continue
                seen_ids[norm_id] = md_path
                dst_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(md_path, dst_dir / md_path.name)

        if resolved_kind == "opportunity":
            rep_src = vault_dir / "reports"
            if rep_src.is_dir():
                rep_dst = shadow_vdir / "reports"
                for md_path in sorted(rep_src.glob("*.md")):
                    try:
                        parsed = parse_markdown_card(md_path)
                        _ensure_untruncated_frontmatter(parsed)
                        ReportCard.model_validate(parsed.frontmatter)
                    except (ValidationError, ValueError, yaml.YAMLError, OSError) as exc:
                        warnings.append(f"{md_path.name}: {exc}")
                        continue
                    rep_dst.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(md_path, rep_dst / md_path.name)

        if resolved_kind == "verify_questions" and canonical_opp_id is None:
            q_src = vault_dir / "questions" / "open-questions.md"
            if q_src.is_file():
                try:
                    parsed_q = parse_markdown_card(q_src)
                    _ensure_untruncated_frontmatter(parsed_q)
                    OpenQuestionBank.model_validate(parsed_q.frontmatter)
                    q_dst = shadow_vdir / "questions"
                    q_dst.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(q_src, q_dst / "open-questions.md")
                except (ValidationError, ValueError, yaml.YAMLError, OSError) as exc:
                    warnings.append(f"{q_src.name}: {exc}")
            pb_src = _resolve_playbook_path(vault_dir)
            if pb_src.is_file():
                try:
                    parsed_pb = parse_markdown_card(pb_src)
                    _ensure_untruncated_frontmatter(parsed_pb)
                    PlaybookTracker.model_validate(parsed_pb.frontmatter)
                except (ValidationError, ValueError, yaml.YAMLError, OSError) as exc:
                    warnings.append(f"{pb_src.name}: {exc}")

        if resolved_kind == "opportunity":
            assert canonical_opp_id is not None
            return (
                generate_opportunity_brief(shadow_vdir, canonical_opp_id, fmt=mode),
                warnings,
            )
        if resolved_kind == "weekly_status":
            try:
                return (
                    generate_portfolio_brief(shadow_vdir, capacity_weeks=cap),
                    warnings,
                )
            except (ValidationError, OverflowError):
                clamped_any = False
                for rel_sub in ("opportunities", "staging/opportunities"):
                    sub_dir = shadow_vdir / rel_sub
                    if not sub_dir.is_dir():
                        continue
                    for shadow_md in sorted(sub_dir.glob("*.md")):
                        sp = parse_markdown_card(shadow_md)
                        s_opp = OpportunitySpec.model_validate(sp.frontmatter)
                        if float(s_opp.effort_weeks) > _SAFE_SCHEDULE_EFFORT_CEILING:
                            clamped_any = True
                            clamped_opp = s_opp.model_copy(
                                update={"effort_weeks": _SAFE_SCHEDULE_EFFORT_CEILING}
                            )
                            upsert_markdown_card(
                                shadow_md,
                                clamped_opp,
                                sp.body,
                                human_notes_override=sp.human_notes,
                            )
                if not clamped_any and cap <= _SAFE_SCHEDULE_EFFORT_CEILING:
                    raise
                warnings.append(
                    "Cumulative effort_weeks overflowed float64; clamped brief schedule "
                    f"totals to {_SAFE_SCHEDULE_EFFORT_CEILING:g}w ceiling."
                )
                return (
                    generate_portfolio_brief(
                        shadow_vdir,
                        capacity_weeks=min(cap, _SAFE_SCHEDULE_EFFORT_CEILING),
                    ),
                    warnings,
                )
        return (
            generate_verify_questions_brief(shadow_vdir, opp_id=canonical_opp_id),
            warnings,
        )


def _generate_brief_payload(
    vault_dir: Path,
    cfg: PoopdeckConfig,
    *,
    opp_id: str | None,
    brief_type: str | None,
    fmt: str,
    capacity_weeks: float | None,
    save_export: bool,
) -> dict[str, Any]:
    mode = fmt.strip().lower()
    if mode not in {"teams", "exec"}:
        raise HTTPException(status_code=422, detail=f"Invalid brief format: {fmt!r}")

    cap = (
        float(capacity_weeks)
        if capacity_weeks is not None
        else float(cfg.quarterly_capacity_weeks)
    )
    if not math.isfinite(cap) or cap <= 0.0:
        raise HTTPException(
            status_code=422, detail="capacity_weeks must be finite and > 0"
        )

    effective_type = brief_type.strip().lower() if brief_type else None
    if effective_type in {"weekly", "portfolio"}:
        effective_type = "weekly_status"
    elif effective_type is not None and effective_type not in {
        "opportunity",
        "weekly_status",
        "verify_questions",
    }:
        raise HTTPException(
            status_code=422, detail=f"Invalid brief_type: {brief_type!r}"
        )

    target_opp_id = opp_id.strip() if opp_id and opp_id.strip() else None

    result: BriefResult
    warnings: list[str] = []
    saved_posix: str | None = None

    with _vault_lock(vault_dir):
        opp_folders = [
            vault_dir / "opportunities",
            vault_dir / "staging" / "opportunities",
        ]
        resolved_kind: Literal["opportunity", "weekly_status", "verify_questions"]
        canonical_opp_id: str | None = None

        try:
            if effective_type is None:
                if target_opp_id is None:
                    resolved_kind = "weekly_status"
                else:
                    kw = target_opp_id.lower()
                    if (
                        kw in {"weekly", "weekly_status", "portfolio"}
                        and _find_opp_file_safe(opp_folders, target_opp_id) is None
                    ):
                        resolved_kind = "weekly_status"
                    elif (
                        kw == "verify_questions"
                        and _find_opp_file_safe(opp_folders, target_opp_id) is None
                    ):
                        resolved_kind = "verify_questions"
                    else:
                        _, target_parsed = _locate_target_opportunity(
                            opp_folders, target_opp_id
                        )
                        validated_target = OpportunitySpec.model_validate(
                            target_parsed.frontmatter
                        )
                        resolved_kind = "opportunity"
                        canonical_opp_id = validated_target.id
            elif effective_type == "opportunity":
                if not target_opp_id:
                    raise HTTPException(
                        status_code=422,
                        detail="opp_id is required when brief_type='opportunity'",
                    )
                _, target_parsed = _locate_target_opportunity(
                    opp_folders, target_opp_id
                )
                validated_target = OpportunitySpec.model_validate(
                    target_parsed.frontmatter
                )
                resolved_kind = "opportunity"
                canonical_opp_id = validated_target.id
            elif effective_type == "weekly_status":
                resolved_kind = "weekly_status"
            else:
                if target_opp_id and target_opp_id.lower() in {
                    "all",
                    "*",
                    "verify_questions",
                }:
                    target_opp_id = None
                if target_opp_id is not None:
                    _, target_parsed = _locate_target_opportunity(
                        opp_folders, target_opp_id
                    )
                    validated_target = OpportunitySpec.model_validate(
                        target_parsed.frontmatter
                    )
                    canonical_opp_id = validated_target.id
                resolved_kind = "verify_questions"
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValidationError, ValueError, yaml.YAMLError, OSError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        try:
            if resolved_kind in {
                "weekly_status",
                "verify_questions",
            } and _vault_has_duplicate_opp_ids(vault_dir):
                result, warnings = _run_brief_in_shadow_vault(
                    vault_dir,
                    resolved_kind=resolved_kind,
                    canonical_opp_id=canonical_opp_id,
                    mode=mode,
                    cap=cap,
                )
            else:
                try:
                    if resolved_kind == "opportunity":
                        assert canonical_opp_id is not None
                        result = generate_opportunity_brief(
                            vault_dir, canonical_opp_id, fmt=mode
                        )
                    elif resolved_kind == "weekly_status":
                        result = generate_portfolio_brief(
                            vault_dir, capacity_weeks=cap
                        )
                    else:
                        if canonical_opp_id is None:
                            q_check = vault_dir / "questions" / "open-questions.md"
                            if q_check.is_file():
                                _ensure_untruncated_frontmatter(
                                    parse_markdown_card(q_check)
                                )
                            pb_check = _resolve_playbook_path(vault_dir)
                            if pb_check.is_file():
                                _ensure_untruncated_frontmatter(
                                    parse_markdown_card(pb_check)
                                )
                        result = generate_verify_questions_brief(
                            vault_dir, opp_id=canonical_opp_id
                        )
                except (
                    ValidationError,
                    ValueError,
                    yaml.YAMLError,
                    OSError,
                    OverflowError,
                ):
                    result, warnings = _run_brief_in_shadow_vault(
                        vault_dir,
                        resolved_kind=resolved_kind,
                        canonical_opp_id=canonical_opp_id,
                        mode=mode,
                        cap=cap,
                    )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (
            ValidationError,
            ValueError,
            yaml.YAMLError,
            OSError,
            OverflowError,
        ) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        if save_export:
            exports_dir = (vault_dir.resolve().parent / "exports").resolve()
            raw_base = result.opp_id or result.brief_type or "brief"
            safe_stem = (
                re.sub(r"[^A-Za-z0-9._-]+", "-", str(raw_base)).strip(".-_")
                or "brief"
            )
            out_path = (exports_dir / f"{safe_stem}-brief.md").resolve()
            if out_path.parent != exports_dir or not out_path.is_relative_to(
                exports_dir
            ):
                raise HTTPException(status_code=422, detail="Unsafe export target path")
            exports_dir.mkdir(parents=True, exist_ok=True)
            out_path.write_text(result.markdown, encoding="utf-8", newline="\n")
            saved_posix = out_path.as_posix()

    return {
        "opp_id": result.opp_id,
        "brief_type": result.brief_type,
        "format": mode,
        "title": result.title,
        "markdown": result.markdown,
        "content": result.markdown,
        "saved_path": saved_posix,
        "warnings": warnings,
    }


def create_app(
    vault_dir: Path | str | None = None,
    config_path: Path | str | None = None,
) -> FastAPI:
    """Create the Zero-Node FastAPI Local Command Center application."""
    vault_override = vault_dir
    config_override = config_path

    if config_override is not None and str(config_override).strip():
        explicit_cfg = Path(config_override)
        if not explicit_cfg.is_file():
            raise FileNotFoundError(f"Configuration file not found: {explicit_cfg}")

    fastapi_app = FastAPI(
        title="Project Poopdeck — Local Command Center",
        version=__version__,
    )

    fastapi_app.mount(
        "/static",
        StaticFiles(directory=str(STATIC_DIR), check_dir=False),
        name="static",
    )

    def _resolve_context() -> tuple[Path, PoopdeckConfig]:
        try:
            cfg = load_config(config_override)
            vdir = cfg.resolve_vault_dir(vault_override=vault_override)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValidationError, ValueError, TypeError, yaml.YAMLError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if "\x00" in str(vdir) or _has_surrogate(str(vdir)):
            raise HTTPException(
                status_code=422,
                detail="Invalid null byte or surrogate in vault path",
            )
        return vdir, cfg

    @fastapi_app.middleware("http")
    async def _security_headers_middleware(
        request: Request, call_next: Any
    ) -> Response:
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Content-Security-Policy", _CSP_POLICY)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @fastapi_app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        sanitized = _sanitize_validation_err_value(exc.errors())
        return JSONResponse(
            status_code=422,
            content={"detail": jsonable_encoder(sanitized)},
        )

    @fastapi_app.exception_handler(RecursionError)
    async def _recursion_exception_handler(
        request: Request, exc: RecursionError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {
                        "type": "value_error",
                        "loc": ["body"],
                        "msg": "Maximum JSON nesting depth exceeded",
                    }
                ]
            },
        )

    @fastapi_app.get("/", include_in_schema=False)
    def serve_spa_index() -> FileResponse:
        index_path = STATIC_DIR / "index.html"
        if not index_path.is_file():
            raise HTTPException(status_code=404, detail="UI index.html not found")
        return FileResponse(index_path, media_type="text/html; charset=utf-8")

    @fastapi_app.get("/api/health")
    def api_health() -> dict[str, Any]:
        vdir, cfg = _resolve_context()
        with _vault_lock(vdir):
            vault_exists = vdir.is_dir()
            resolved_posix = vdir.resolve().as_posix()
        return {
            "status": "ok",
            "vault_dir": resolved_posix,
            "vault_exists": vault_exists,
            "domain": cfg.domain,
            "quarterly_capacity_weeks": cfg.quarterly_capacity_weeks,
            "version": __version__,
        }

    @fastapi_app.get("/api/portfolio")
    def api_get_portfolio(
        capacity_weeks: StrictPositiveFloat | None = Query(default=None),
        domain: str = Query(default="all"),
        include_archived: bool = Query(default=False),
    ) -> dict[str, Any]:
        _ensure_clean_query_str(domain, "domain")
        vdir, cfg = _resolve_context()
        with _vault_lock(vdir):
            try:
                return _build_portfolio_payload(
                    vdir,
                    cfg,
                    capacity_weeks=capacity_weeks,
                    domain=domain,
                    include_archived=include_archived,
                )
            except (ValidationError, ValueError, OverflowError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

    @fastapi_app.get("/api/staging")
    def api_get_staging() -> dict[str, Any]:
        vdir, _ = _resolve_context()
        with _vault_lock(vdir):
            loaded, warnings = _load_opportunities_safe(
                vdir, include_staging=True, include_archived=False
            )
            items: list[dict[str, Any]] = []
            for md_path, opp, parsed in loaded:
                if opp.status != "staging":
                    continue
                items.append(
                    _serialize_opportunity(
                        opp,
                        md_path=md_path,
                        vault_dir=vdir,
                        parsed=parsed,
                        sched_item=None,
                        include_frontmatter=True,
                    )
                )
        return {
            "count": len(items),
            "items": items,
            "opportunities": items,
            "warnings": warnings,
        }

    @fastapi_app.post("/api/promote")
    def api_post_promote(payload: PromoteRequest) -> dict[str, Any]:
        try:
            _validate_utf8_str(payload.opp_id, "opp_id", forbid_ctrl=True)
            _validate_utf8_str(
                payload.target_status, "target_status", forbid_ctrl=True
            )
            _validate_utf8_str(payload.action, "action", forbid_ctrl=True)
            _validate_utf8_str(payload.reason, "reason", forbid_ctrl=False)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        vdir, _ = _resolve_context()
        with _vault_lock(vdir):
            try:
                if payload.action == "archive":
                    src_path, src_parsed = _locate_target_opportunity(
                        [
                            vdir / "staging" / "opportunities",
                            vdir / "opportunities",
                        ],
                        payload.opp_id,
                    )
                    OpportunitySpec.model_validate(src_parsed.frontmatter)
                    fm_check = dict(src_parsed.frontmatter)
                    fm_check["status"] = "archived"
                    fm_check["updated_at"] = datetime.date.today()
                    validated_opp = OpportunitySpec.model_validate(fm_check)
                    _ensure_no_live_opp_collision(vdir, src_path, validated_opp.id)
                    action_status = "archived"
                else:
                    src_path, src_parsed = _locate_target_opportunity(
                        [vdir / "staging" / "opportunities"],
                        payload.opp_id,
                    )
                    OpportunitySpec.model_validate(src_parsed.frontmatter)
                    fm_check = dict(src_parsed.frontmatter)
                    fm_check["status"] = payload.target_status.strip().lower()
                    fm_check["updated_at"] = datetime.date.today()
                    validated_opp = OpportunitySpec.model_validate(fm_check)
                    _ensure_no_live_opp_collision(vdir, src_path, validated_opp.id)
                    action_status = "promoted"

                with _AtomicVaultWrite(
                    [
                        src_path,
                        vdir / "opportunities" / src_path.name,
                        vdir / "archive" / src_path.name,
                    ],
                    [
                        vdir / "opportunities",
                        vdir / "staging" / "opportunities",
                        vdir / "archive",
                    ],
                ) as tx:
                    if action_status == "archived":
                        try:
                            dest = archive_opportunity(
                                vdir, payload.opp_id, reason=payload.reason
                            )
                        except (
                            ValidationError,
                            ValueError,
                            yaml.YAMLError,
                            OSError,
                        ) as mut_exc:
                            if _is_target_write_failure(
                                mut_exc,
                                tx,
                                (src_path, vdir / "opportunities" / src_path.name),
                            ):
                                raise
                            tx.rollback()
                            new_body = src_parsed.body
                            if payload.reason and payload.reason.strip():
                                reason_block = (
                                    f"## Archive Reason\n\n{payload.reason.strip()}"
                                )
                                if reason_block not in new_body:
                                    new_body = f"{new_body}\n\n{reason_block}".strip()
                            dest = vdir / "opportunities" / src_path.name
                            upsert_markdown_card(
                                dest,
                                validated_opp,
                                new_body,
                                human_notes_override=src_parsed.human_notes,
                            )
                            if (
                                src_path.resolve() != dest.resolve()
                                and src_path.exists()
                            ):
                                src_path.unlink()
                    else:
                        try:
                            dest = promote_opportunity(
                                vdir,
                                payload.opp_id,
                                target_status=payload.target_status,
                            )
                        except (
                            ValidationError,
                            ValueError,
                            yaml.YAMLError,
                            OSError,
                        ) as mut_exc:
                            if _is_target_write_failure(
                                mut_exc,
                                tx,
                                (src_path, vdir / "opportunities" / src_path.name),
                            ):
                                raise
                            tx.rollback()
                            dest = vdir / "opportunities" / src_path.name
                            upsert_markdown_card(
                                dest,
                                validated_opp,
                                src_parsed.body,
                                human_notes_override=src_parsed.human_notes,
                            )
                            if (
                                src_path.resolve() != dest.resolve()
                                and src_path.exists()
                            ):
                                src_path.unlink()

                    reparsed = parse_markdown_card(dest)
                    _ensure_untruncated_frontmatter(reparsed)
                    updated_opp = OpportunitySpec.model_validate(reparsed.frontmatter)
                    opp_dict = _serialize_opportunity(
                        updated_opp,
                        md_path=dest,
                        vault_dir=vdir,
                        parsed=reparsed,
                        sched_item=None,
                        include_frontmatter=True,
                    )
                    resp: dict[str, Any] = {
                        "status": action_status,
                        "id": updated_opp.id,
                        "opp_id": updated_opp.id,
                        "new_status": updated_opp.status,
                        "source_path": src_path.resolve().as_posix(),
                        "target_path": dest.resolve().as_posix(),
                        "path": dest.resolve().as_posix(),
                        "opportunity": opp_dict,
                    }
                    if payload.reason is not None or action_status == "archived":
                        resp["reason"] = payload.reason
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except (ValidationError, ValueError, yaml.YAMLError, OSError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        return resp

    @fastapi_app.post("/api/archive")
    def api_post_archive(payload: ArchiveRequest) -> dict[str, Any]:
        try:
            _validate_utf8_str(payload.opp_id, "opp_id", forbid_ctrl=True)
            _validate_utf8_str(payload.reason, "reason", forbid_ctrl=False)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        vdir, _ = _resolve_context()
        with _vault_lock(vdir):
            try:
                src_path, src_parsed = _locate_target_opportunity(
                    [
                        vdir / "staging" / "opportunities",
                        vdir / "opportunities",
                    ],
                    payload.opp_id,
                )
                OpportunitySpec.model_validate(src_parsed.frontmatter)
                fm_check = dict(src_parsed.frontmatter)
                fm_check["status"] = "archived"
                fm_check["updated_at"] = datetime.date.today()
                validated_opp = OpportunitySpec.model_validate(fm_check)
                _ensure_no_live_opp_collision(vdir, src_path, validated_opp.id)
                with _AtomicVaultWrite(
                    [
                        src_path,
                        vdir / "opportunities" / src_path.name,
                        vdir / "archive" / src_path.name,
                    ],
                    [
                        vdir / "opportunities",
                        vdir / "staging" / "opportunities",
                        vdir / "archive",
                    ],
                ) as tx:
                    try:
                        dest = archive_opportunity(
                            vdir, payload.opp_id, reason=payload.reason
                        )
                    except (
                        ValidationError,
                        ValueError,
                        yaml.YAMLError,
                        OSError,
                    ) as mut_exc:
                        if _is_target_write_failure(
                            mut_exc,
                            tx,
                            (src_path, vdir / "opportunities" / src_path.name),
                        ):
                            raise
                        tx.rollback()
                        new_body = src_parsed.body
                        if payload.reason and payload.reason.strip():
                            reason_block = (
                                f"## Archive Reason\n\n{payload.reason.strip()}"
                            )
                            if reason_block not in new_body:
                                new_body = f"{new_body}\n\n{reason_block}".strip()
                        dest = vdir / "opportunities" / src_path.name
                        upsert_markdown_card(
                            dest,
                            validated_opp,
                            new_body,
                            human_notes_override=src_parsed.human_notes,
                        )
                        if (
                            src_path.resolve() != dest.resolve()
                            and src_path.exists()
                        ):
                            src_path.unlink()
                    reparsed = parse_markdown_card(dest)
                    _ensure_untruncated_frontmatter(reparsed)
                    updated_opp = OpportunitySpec.model_validate(reparsed.frontmatter)
                    opp_dict = _serialize_opportunity(
                        updated_opp,
                        md_path=dest,
                        vault_dir=vdir,
                        parsed=reparsed,
                        sched_item=None,
                        include_frontmatter=True,
                    )
                    resp = {
                        "status": "archived",
                        "id": updated_opp.id,
                        "opp_id": updated_opp.id,
                        "new_status": updated_opp.status,
                        "source_path": src_path.resolve().as_posix(),
                        "target_path": dest.resolve().as_posix(),
                        "path": dest.resolve().as_posix(),
                        "reason": payload.reason,
                        "opportunity": opp_dict,
                    }
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except (ValidationError, ValueError, yaml.YAMLError, OSError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        return resp

    @fastapi_app.post("/api/boss-pin")
    def api_post_boss_pin(payload: BossPinRequest) -> dict[str, Any]:
        try:
            _validate_utf8_str(payload.opp_id, "opp_id", forbid_ctrl=True)
            _validate_utf8_str(payload.domain, "domain", forbid_ctrl=True)
            if payload.capacity_weeks is not None:
                _parse_positive_float(payload.capacity_weeks)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        vdir, cfg = _resolve_context()
        with _vault_lock(vdir):
            try:
                card_path, src_parsed = _locate_target_opportunity(
                    [vdir / "opportunities", vdir / "staging" / "opportunities"],
                    payload.opp_id,
                )
                current_opp = OpportunitySpec.model_validate(src_parsed.frontmatter)
                new_flag = (
                    (not current_opp.boss_mandate)
                    if payload.boss_mandate is None
                    else bool(payload.boss_mandate)
                )
                fm_check = dict(src_parsed.frontmatter)
                fm_check["boss_mandate"] = new_flag
                fm_check["updated_at"] = datetime.date.today()
                validated_opp = OpportunitySpec.model_validate(fm_check)

                with _AtomicVaultWrite(
                    [card_path],
                    [vdir / "opportunities", vdir / "staging" / "opportunities"],
                ) as tx:
                    try:
                        card_path, updated_opp = set_boss_mandate(
                            vdir, payload.opp_id, boss_mandate=payload.boss_mandate
                        )
                    except (
                        ValidationError,
                        ValueError,
                        yaml.YAMLError,
                        OSError,
                    ) as mut_exc:
                        if _is_target_write_failure(mut_exc, tx, (card_path,)):
                            raise
                        tx.rollback()
                        upsert_markdown_card(
                            card_path,
                            validated_opp,
                            src_parsed.body,
                            human_notes_override=src_parsed.human_notes,
                        )
                        updated_opp = validated_opp

                    reparsed = parse_markdown_card(card_path)
                    _ensure_untruncated_frontmatter(reparsed)
                    portfolio_payload = _build_portfolio_payload(
                        vdir,
                        cfg,
                        capacity_weeks=payload.capacity_weeks,
                        domain=payload.domain or "all",
                        include_archived=bool(payload.include_archived),
                    )

                    sched_item_match: ScheduledOpportunity | None = None
                    opp_dict: dict[str, Any] | None = None
                    for item_dict in portfolio_payload["opportunities"]:
                        if item_dict["id"] == updated_opp.id:
                            opp_dict = dict(item_dict)
                            break
                    if opp_dict is None:
                        opp_dict = _serialize_opportunity(
                            updated_opp,
                            md_path=card_path,
                            vault_dir=vdir,
                            parsed=reparsed,
                            sched_item=sched_item_match,
                            include_frontmatter=True,
                        )
                    resp = {
                        "status": "ok",
                        "id": updated_opp.id,
                        "opp_id": updated_opp.id,
                        "boss_mandate": updated_opp.boss_mandate,
                        "updated_path": card_path.resolve().as_posix(),
                        "path": card_path.resolve().as_posix(),
                        "opportunity": opp_dict,
                        "capacity_weeks": portfolio_payload["capacity_weeks"],
                        "used_capacity_weeks": portfolio_payload[
                            "used_capacity_weeks"
                        ],
                        "mandate_weeks": portfolio_payload["mandate_weeks"],
                        "remaining_capacity_weeks": portfolio_payload[
                            "remaining_capacity_weeks"
                        ],
                        "overflow_weeks": portfolio_payload["overflow_weeks"],
                        "over_capacity": portfolio_payload["over_capacity"],
                        "over_capacity_warning": portfolio_payload[
                            "over_capacity_warning"
                        ],
                        "funded_ids": portfolio_payload["funded_ids"],
                        "displaced_ids": portfolio_payload["displaced_ids"],
                        "displaced_titles": portfolio_payload["displaced_titles"],
                        "schedule": portfolio_payload["schedule"],
                        "portfolio": portfolio_payload,
                    }
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except (
                ValidationError,
                ValueError,
                yaml.YAMLError,
                OSError,
                OverflowError,
            ) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        return resp

    @fastapi_app.get("/api/lineage")
    def api_get_lineage(
        include_staging: bool = Query(default=True),
        focus_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        _ensure_clean_query_str(focus_id, "focus_id")
        vdir, _ = _resolve_context()

        with _vault_lock(vdir):
            warnings, has_corrupt_opp = _collect_lineage_warnings(
                vdir, include_staging=include_staging
            )
            if has_corrupt_opp:
                raw_graph = _build_lineage_graph_resilient(
                    vdir, include_staging=include_staging
                )
            else:
                try:
                    raw_graph = build_lineage_graph(
                        vdir, include_staging=include_staging
                    )
                except (ValidationError, ValueError, yaml.YAMLError, OSError) as exc:
                    warnings.append(str(exc))
                    raw_graph = _build_lineage_graph_resilient(
                        vdir, include_staging=include_staging
                    )

        all_nodes = [dict(n) for n in raw_graph.get("nodes", [])]
        all_edges = [dict(e) for e in raw_graph.get("edges", [])]

        matched_focus: str | None = None
        if focus_id is not None and focus_id.strip():
            matched_focus = _match_focus_node(all_nodes, focus_id)
            if matched_focus is None:
                nodes_out: list[dict[str, Any]] = []
                edges_out: list[dict[str, Any]] = []
            else:
                nodes_out, edges_out = _filter_lineage_cone(
                    all_nodes, all_edges, matched_focus
                )
        else:
            nodes_out = all_nodes
            edges_out = all_edges

        enriched_nodes: list[dict[str, Any]] = []
        by_type: dict[str, int] = {}
        for node in nodes_out:
            item = dict(node)
            exists_flag = bool(item.get("exists_in_vault", False))
            item["exists_in_vault"] = exists_flag
            item["exists"] = exists_flag
            item["focused"] = bool(matched_focus and item["id"] == matched_focus)
            ntype = str(item.get("type") or "unknown")
            by_type[ntype] = by_type.get(ntype, 0) + 1
            enriched_nodes.append(item)

        return {
            "nodes": enriched_nodes,
            "edges": edges_out,
            "focus_id": focus_id,
            "matched_focus_id": matched_focus,
            "summary": {
                "node_count": len(enriched_nodes),
                "edge_count": len(edges_out),
                "by_type": by_type,
            },
            "warnings": warnings,
        }

    def _handle_get_questions(
        category: str, status: str
    ) -> dict[str, Any]:
        _ensure_clean_query_str(category, "category")
        _ensure_clean_query_str(status, "status")
        vdir, _ = _resolve_context()
        st_norm = (status or "all").strip().lower()
        if st_norm in {"", "*"}:
            st_norm = "all"
        if st_norm not in {"all", "open", "answered", "blocked"}:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid question status filter: {status!r}",
            )

        cat_raw = (category or "all").strip()
        cat_norm = (
            "all"
            if cat_raw.lower() in {"", "*", "all"}
            else str(_normalize_question_category(cat_raw))
        )

        with _vault_lock(vdir):
            bank, tracker, warnings = _load_questions_and_playbook_safe(vdir)

        filtered_questions: list[dict[str, Any]] = []
        for q in bank.questions:
            if cat_norm != "all" and q.category != cat_norm:
                continue
            if st_norm != "all" and q.status != st_norm:
                continue
            filtered_questions.append(_serialize_question(q))

        phases_out = [_serialize_phase(p) for p in tracker.phases]
        summary = _build_readiness_summary(
            bank, tracker, filtered_count=len(filtered_questions)
        )

        return {
            "questions": filtered_questions,
            "phases": phases_out,
            "playbook_phases": phases_out,
            "summary": summary,
            "warnings": warnings,
        }

    @fastapi_app.get("/api/questions")
    def api_get_questions(
        category: str = Query(default="all"),
        status: str = Query(default="all"),
    ) -> dict[str, Any]:
        return _handle_get_questions(category=category, status=status)

    @fastapi_app.get("/api/readiness")
    def api_get_readiness(
        category: str = Query(default="all"),
        status: str = Query(default="all"),
    ) -> dict[str, Any]:
        return _handle_get_questions(category=category, status=status)

    def _handle_mutate_question_or_phase(
        path_item_id: str | None, payload: QuestionUpdateRequest
    ) -> dict[str, Any]:
        _ensure_clean_query_str(path_item_id, "item_id")
        try:
            _validate_utf8_str(path_item_id, "item_id", forbid_ctrl=True)
            _validate_utf8_str(payload.item_id, "item_id", forbid_ctrl=True)
            _validate_utf8_str(payload.status, "status", forbid_ctrl=True)
            _validate_utf8_str(
                payload.answer_summary, "answer_summary", forbid_ctrl=False
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        vdir, _ = _resolve_context()
        resolved_path_id: str | None = None
        if path_item_id is not None and path_item_id != "":
            try:
                resolved_path_id = _validate_safe_identifier(path_item_id)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        if (
            resolved_path_id is not None
            and payload.item_id is not None
            and resolved_path_id.upper() != payload.item_id.upper()
        ):
            raise HTTPException(
                status_code=422,
                detail="Path item_id does not match request body id",
            )

        target_id = resolved_path_id or payload.item_id
        if not target_id:
            raise HTTPException(
                status_code=422,
                detail="item_id is required",
            )

        needle = target_id.strip().upper()

        with _vault_lock(vdir):
            bank, q_err, tracker, pb_err, _ = _load_questions_and_playbook_detailed(
                vdir
            )

            phase_matches = [
                phase
                for phase in tracker.phases
                if phase.id.strip().upper() == needle
            ]
            question_matches = [
                q for q in bank.questions if q.id.strip().upper() == needle
            ]
            if len(phase_matches) + len(question_matches) > 1:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Duplicate question or playbook phase id "
                        f"'{target_id}' matched multiple items"
                    ),
                )
            matched_kind: Literal["phase", "question"] | None = (
                "phase"
                if phase_matches
                else ("question" if question_matches else None)
            )

            if matched_kind is None:
                if needle.startswith(("PHASE-", "PHASE_")):
                    if pb_err is not None:
                        raise HTTPException(
                            status_code=422, detail=str(pb_err)
                        ) from pb_err
                    raise HTTPException(
                        status_code=404,
                        detail=f"Question or playbook phase '{target_id}' not found",
                    )
                if needle.startswith(("Q-", "Q_")):
                    if q_err is not None:
                        raise HTTPException(
                            status_code=422, detail=str(q_err)
                        ) from q_err
                    raise HTTPException(
                        status_code=404,
                        detail=f"Question or playbook phase '{target_id}' not found",
                    )
                if pb_err is not None:
                    try:
                        pb_raw = _resolve_playbook_path(vdir).read_text(
                            encoding="utf-8", errors="replace"
                        )
                    except OSError:
                        pb_raw = ""
                    if needle in pb_raw.upper():
                        raise HTTPException(
                            status_code=422, detail=str(pb_err)
                        ) from pb_err
                if q_err is not None:
                    try:
                        q_raw = (vdir / "questions" / "open-questions.md").read_text(
                            encoding="utf-8", errors="replace"
                        )
                    except OSError:
                        q_raw = ""
                    if needle in q_raw.upper():
                        raise HTTPException(
                            status_code=422, detail=str(q_err)
                        ) from q_err
                raise HTTPException(
                    status_code=404,
                    detail=f"Question or playbook phase '{target_id}' not found",
                )

            if matched_kind == "phase":
                if payload.completed is None and payload.status is None:
                    raise HTTPException(
                        status_code=422,
                        detail="Playbook phase update requires completed or status",
                    )
                if payload.status is not None and payload.status not in {
                    "completed",
                    "answered",
                    "done",
                    "open",
                    "pending",
                }:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Invalid playbook phase status: {payload.status!r}",
                    )
            else:
                if payload.status is not None and payload.status not in {
                    "open",
                    "answered",
                    "blocked",
                }:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Invalid question status: {payload.status!r}",
                    )

            q_target = vdir / "questions" / "open-questions.md"
            pb_target = _resolve_playbook_path(vdir)
            updated_path: Path | None = None
            try:
                with _AtomicVaultWrite(
                    [q_target, pb_target],
                    [vdir / "questions", pb_target.parent],
                ) as tx:
                    try:
                        updated_path = update_question_or_phase(
                            vdir,
                            target_id,
                            status=payload.status,
                            answer_summary=payload.answer_summary,
                            completed=payload.completed,
                        )
                    except (
                        ValidationError,
                        ValueError,
                        yaml.YAMLError,
                        OSError,
                    ) as mut_exc:
                        if _is_target_write_failure(
                            mut_exc,
                            tx,
                            (pb_target if matched_kind == "phase" else q_target,),
                        ):
                            raise
                        tx.rollback()
                        if matched_kind == "phase":
                            pb_parsed = parse_markdown_card(pb_target)
                            _ensure_untruncated_frontmatter(pb_parsed)
                            tracker_mut = PlaybookTracker.model_validate(
                                pb_parsed.frontmatter
                            )
                            for phase in tracker_mut.phases:
                                if phase.id.strip().upper() == needle:
                                    if payload.completed is not None:
                                        phase.completed = bool(payload.completed)
                                    elif payload.status is not None:
                                        st = payload.status.strip().lower()
                                        if st in (
                                            "completed",
                                            "answered",
                                            "done",
                                            "true",
                                        ):
                                            phase.completed = True
                                        elif st in ("open", "pending", "false"):
                                            phase.completed = False
                                    tracker_mut.updated_at = datetime.date.today()
                                    updated_path = upsert_markdown_card(
                                        pb_target,
                                        tracker_mut,
                                        render_playbook_body(tracker_mut),
                                        human_notes_override=pb_parsed.human_notes,
                                    )
                                    break
                        else:
                            q_parsed = parse_markdown_card(q_target)
                            _ensure_untruncated_frontmatter(q_parsed)
                            bank_mut = OpenQuestionBank.model_validate(
                                q_parsed.frontmatter
                            )
                            for q in bank_mut.questions:
                                if q.id.strip().upper() == needle:
                                    if payload.status is not None:
                                        q.status = payload.status.strip().lower()  # type: ignore[assignment]
                                    elif payload.completed is not None:
                                        q.status = (
                                            "answered"
                                            if payload.completed
                                            else "open"
                                        )
                                    if payload.answer_summary is not None:
                                        q.answer_summary = payload.answer_summary
                                    bank_mut.updated_at = datetime.date.today()
                                    updated_path = upsert_markdown_card(
                                        q_target,
                                        bank_mut,
                                        render_questions_body(bank_mut),
                                        human_notes_override=q_parsed.human_notes,
                                    )
                                    break
                        if updated_path is None:
                            raise KeyError(
                                f"Question or playbook phase '{target_id}' not found"
                            )
                    refreshed_bank, refreshed_tracker, warnings = (
                        _load_questions_and_playbook_safe(vdir)
                    )
                    summary = _build_readiness_summary(
                        refreshed_bank, refreshed_tracker
                    )
                    posix_path = updated_path.resolve().as_posix()

                    if matched_kind == "phase":
                        updated_phase = next(
                            p
                            for p in refreshed_tracker.phases
                            if p.id.strip().upper() == needle
                        )
                        item_dict = _serialize_phase(updated_phase)
                        resp: dict[str, Any] = {
                            **item_dict,
                            "kind": "phase",
                            "updated_path": posix_path,
                            "path": posix_path,
                            "item": item_dict,
                            "phase_item": item_dict,
                            "summary": summary,
                            "warnings": warnings,
                        }
                    else:
                        updated_q = next(
                            q
                            for q in refreshed_bank.questions
                            if q.id.strip().upper() == needle
                        )
                        item_dict = _serialize_question(updated_q)
                        resp = {
                            **item_dict,
                            "kind": "question",
                            "updated_path": posix_path,
                            "path": posix_path,
                            "item": item_dict,
                            "question_item": item_dict,
                            "summary": summary,
                            "warnings": warnings,
                        }
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except (ValidationError, ValueError, yaml.YAMLError, OSError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        return resp

    @fastapi_app.patch("/api/questions/{item_id:path}")
    def api_patch_question_by_path(
        item_id: str, payload: QuestionUpdateRequest
    ) -> dict[str, Any]:
        return _handle_mutate_question_or_phase(item_id, payload)

    @fastapi_app.post("/api/questions/{item_id:path}")
    def api_post_question_by_path(
        item_id: str, payload: QuestionUpdateRequest
    ) -> dict[str, Any]:
        return _handle_mutate_question_or_phase(item_id, payload)

    @fastapi_app.patch("/api/readiness/{item_id:path}")
    def api_patch_readiness_by_path(
        item_id: str, payload: QuestionUpdateRequest
    ) -> dict[str, Any]:
        return _handle_mutate_question_or_phase(item_id, payload)

    @fastapi_app.post("/api/readiness/{item_id:path}")
    def api_post_readiness_by_path(
        item_id: str, payload: QuestionUpdateRequest
    ) -> dict[str, Any]:
        return _handle_mutate_question_or_phase(item_id, payload)

    @fastapi_app.patch("/api/questions")
    def api_patch_question_body(payload: QuestionUpdateRequest) -> dict[str, Any]:
        return _handle_mutate_question_or_phase(None, payload)

    @fastapi_app.post("/api/questions")
    def api_post_question_body(payload: QuestionUpdateRequest) -> dict[str, Any]:
        return _handle_mutate_question_or_phase(None, payload)

    @fastapi_app.patch("/api/readiness")
    def api_patch_readiness_body(payload: QuestionUpdateRequest) -> dict[str, Any]:
        return _handle_mutate_question_or_phase(None, payload)

    @fastapi_app.post("/api/readiness")
    def api_post_readiness_body(payload: QuestionUpdateRequest) -> dict[str, Any]:
        return _handle_mutate_question_or_phase(None, payload)

    @fastapi_app.get("/api/brief")
    def api_get_brief(
        opp_id: str | None = Query(default=None),
        id_alias: str | None = Query(default=None, alias="id"),
        brief_type: str | None = Query(default=None),
        type_alias: str | None = Query(default=None, alias="type"),
        mode_alias: str | None = Query(default=None, alias="mode"),
        fmt: str | None = Query(default=None, alias="format"),
        fmt_alt: str | None = Query(default=None, alias="fmt"),
        capacity_weeks: StrictPositiveFloat | None = Query(default=None),
    ) -> dict[str, Any]:
        _ensure_clean_query_str(opp_id, "opp_id")
        _ensure_clean_query_str(id_alias, "id")
        _ensure_clean_query_str(brief_type, "brief_type")
        _ensure_clean_query_str(type_alias, "type")
        _ensure_clean_query_str(mode_alias, "mode")
        _ensure_clean_query_str(fmt, "format")
        _ensure_clean_query_str(fmt_alt, "fmt")
        vdir, cfg = _resolve_context()
        raw_opp_id = opp_id if opp_id is not None else id_alias
        safe_opp_id: str | None = None
        if raw_opp_id is not None and raw_opp_id != "":
            try:
                safe_opp_id = _validate_safe_identifier(raw_opp_id)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        raw_type = brief_type if brief_type is not None else type_alias
        explicit_fmt = fmt if fmt is not None else fmt_alt
        if mode_alias is not None:
            mode_clean = mode_alias.strip().lower()
            if mode_clean in {"teams", "exec"} and explicit_fmt is None:
                explicit_fmt = mode_alias
            elif raw_type is None:
                raw_type = mode_alias
            elif explicit_fmt is None:
                explicit_fmt = mode_alias
        raw_fmt = explicit_fmt if explicit_fmt is not None else "teams"
        return _generate_brief_payload(
            vdir,
            cfg,
            opp_id=safe_opp_id,
            brief_type=raw_type,
            fmt=raw_fmt,
            capacity_weeks=capacity_weeks,
            save_export=False,
        )

    @fastapi_app.get("/api/brief/{opp_id:path}")
    def api_get_brief_by_id(
        opp_id: str,
        brief_type: str | None = Query(default=None),
        type_alias: str | None = Query(default=None, alias="type"),
        mode_alias: str | None = Query(default=None, alias="mode"),
        fmt: str | None = Query(default=None, alias="format"),
        fmt_alt: str | None = Query(default=None, alias="fmt"),
        capacity_weeks: StrictPositiveFloat | None = Query(default=None),
    ) -> dict[str, Any]:
        _ensure_clean_query_str(opp_id, "opp_id")
        _ensure_clean_query_str(brief_type, "brief_type")
        _ensure_clean_query_str(type_alias, "type")
        _ensure_clean_query_str(mode_alias, "mode")
        _ensure_clean_query_str(fmt, "format")
        _ensure_clean_query_str(fmt_alt, "fmt")
        vdir, cfg = _resolve_context()
        raw_type = brief_type if brief_type is not None else type_alias
        explicit_fmt = fmt if fmt is not None else fmt_alt
        if mode_alias is not None:
            mode_clean = mode_alias.strip().lower()
            if mode_clean in {"teams", "exec"} and explicit_fmt is None:
                explicit_fmt = mode_alias
            elif raw_type is None:
                raw_type = mode_alias
            elif explicit_fmt is None:
                explicit_fmt = mode_alias
        raw_fmt = explicit_fmt if explicit_fmt is not None else "teams"
        if opp_id == "":
            return _generate_brief_payload(
                vdir,
                cfg,
                opp_id=None,
                brief_type=raw_type,
                fmt=raw_fmt,
                capacity_weeks=capacity_weeks,
                save_export=False,
            )
        try:
            safe_opp_id = _validate_safe_identifier(opp_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        return _generate_brief_payload(
            vdir,
            cfg,
            opp_id=safe_opp_id,
            brief_type=raw_type,
            fmt=raw_fmt,
            capacity_weeks=capacity_weeks,
            save_export=False,
        )

    @fastapi_app.post("/api/brief")
    def api_post_brief(payload: BriefRequest) -> dict[str, Any]:
        vdir, cfg = _resolve_context()
        return _generate_brief_payload(
            vdir,
            cfg,
            opp_id=payload.opp_id,
            brief_type=payload.brief_type,
            fmt=payload.fmt,
            capacity_weeks=payload.capacity_weeks,
            save_export=payload.save_export,
        )

    @fastapi_app.get("/api/packs")
    def api_get_packs(
        domain: str = Query(default="all"),
        free_only: bool = Query(default=False),
    ) -> dict[str, Any]:
        _ensure_clean_query_str(domain, "domain")
        _resolve_context()
        try:
            catalog = load_external_data_catalog()
            datasets = list_external_datasets(
                domain=domain, free_only=free_only, catalog=catalog
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValidationError, ValueError, yaml.YAMLError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "version": catalog.version,
            "domain": domain,
            "free_only": free_only,
            "domain_lenses": {
                slug: lens.model_dump(mode="json")
                for slug, lens in catalog.domain_lenses.items()
            },
            "archetypes": [a.model_dump(mode="json") for a in catalog.archetypes],
            "external_datasets": [d.model_dump(mode="json") for d in datasets],
        }

    @fastapi_app.get("/api/validate")
    def api_get_validate(
        strict: bool = Query(default=False),
    ) -> dict[str, Any]:
        vdir, cfg = _resolve_context()
        with _vault_lock(vdir):
            report = validate_vault(vdir, config=cfg, strict=strict)
        return report.model_dump(mode="json")

    return fastapi_app


app = create_app()

__all__ = ["app", "create_app"]
