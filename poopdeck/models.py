"""Pydantic v2 vault schemas, safe AST Driver-Tree ROI engine, and portfolio capacity scheduler."""

from __future__ import annotations

import ast
import json
import keyword
import math
import re
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

from poopdeck.config import ScoringWeightsConfig


def _ensure_finite_number(v: Any) -> Any:
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ValueError("Numeric value must be finite")
        return v
    if isinstance(v, Decimal):
        if not v.is_finite():
            raise ValueError("Numeric value must be finite")
        _, digits, exp = v.as_tuple()
        if isinstance(exp, int) and exp >= 0 and len(digits) + exp <= 38:
            return int(v)
        try:
            f = float(v)
        except OverflowError as exc:
            raise ValueError("Numeric value must be finite") from exc
        if not math.isfinite(f):
            raise ValueError("Numeric value must be finite")
        return f
    if isinstance(v, complex):
        raise ValueError("Numeric value must be finite")
    return v


def _reject_bool_float(v: Any) -> Any:
    if isinstance(v, bool):
        raise ValueError("Boolean is not a valid numeric value")
    return _ensure_finite_number(v)


def _reject_bool_int(v: Any) -> Any:
    if isinstance(v, bool):
        raise ValueError("Boolean is not a valid integer value")
    return v


def _coerce_date(v: Any) -> date:
    if v is None or v == "":
        return date.today()
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        return date.fromisoformat(v.strip()[:10])
    raise ValueError(f"Invalid ISO date: {v!r}")


def _coerce_optional_iso_str(v: Any) -> str | None:
    if v is None:
        return None
    v = _ensure_finite_number(v)
    if isinstance(v, bool):
        raise ValueError("Boolean is not a valid string value")
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, (list, tuple, set, frozenset, dict)):
        raise ValueError(f"Expected optional ISO string, got {type(v).__name__}")
    s = str(v).strip()
    return s if s else None


def _model_sort_key(item: Any) -> tuple[str, str]:
    return (f"{type(item).__module__}.{type(item).__qualname__}", repr(item))


def _canonical_model_key_rank(item: Any) -> str:
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
        return f"{tag}:{_canonical_model_key_rank(item.value)}"
    if isinstance(item, (tuple, list)):
        return f"{tag}:[{','.join(_canonical_model_key_rank(x) for x in item)}]"
    if isinstance(item, (set, frozenset)):
        return f"{tag}:{{{','.join(sorted(_canonical_model_key_rank(x) for x in item))}}}"
    if isinstance(item, dict):
        pairs = sorted(
            f"{_canonical_model_key_rank(k)}={_canonical_model_key_rank(v)}"
            for k, v in item.items()
        )
        return f"{tag}:{{{','.join(pairs)}}}"
    return f"{tag}:{item}"


def _to_json_stat_compatible(v: Any) -> Any:
    v = _ensure_finite_number(v)
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Path):
        return v.as_posix()
    if isinstance(v, Enum):
        return _to_json_stat_compatible(v.value)
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    if isinstance(v, (list, tuple)):
        return [_to_json_stat_compatible(item) for item in v]
    if isinstance(v, (set, frozenset)):
        return sorted(
            (_to_json_stat_compatible(item) for item in v),
            key=_model_sort_key,
        )
    if isinstance(v, dict):
        out: dict[str, Any] = {}
        winners: dict[str, str] = {}
        for k, val in v.items():
            clean_k = _stringify_dict_key(k)
            clean_v = _to_json_stat_compatible(val)
            rank = _canonical_model_key_rank(k)
            if clean_k not in out or rank >= winners[clean_k]:
                out[clean_k] = clean_v
                winners[clean_k] = rank
        return {k: out[k] for k in sorted(out)}
    return str(v)


def _stringify_dict_key(key: Any) -> str:
    key = _ensure_finite_number(key)
    if isinstance(key, str):
        return key
    if isinstance(key, Enum):
        return _stringify_dict_key(key.value)
    if isinstance(key, (tuple, list, set, frozenset, dict)):
        return json.dumps(
            _to_json_stat_compatible(key),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
    if isinstance(key, (datetime, date)):
        return key.isoformat()
    if isinstance(key, Path):
        return key.as_posix()
    if isinstance(key, bytes):
        return key.decode("utf-8", errors="replace")
    return str(key)


def _normalize_nested_container(v: Any, *, preserve_dates: bool = False) -> Any:
    v = _ensure_finite_number(v)
    if isinstance(v, (datetime, date)):
        return v if preserve_dates else v.isoformat()
    if isinstance(v, Path):
        return v.as_posix()
    if isinstance(v, Enum):
        return _normalize_nested_container(v.value, preserve_dates=preserve_dates)
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    if isinstance(v, list):
        return [
            _normalize_nested_container(item, preserve_dates=preserve_dates)
            for item in v
        ]
    if isinstance(v, tuple):
        return tuple(
            _normalize_nested_container(item, preserve_dates=preserve_dates)
            for item in v
        )
    if isinstance(v, (set, frozenset)):
        return sorted(
            (
                _normalize_nested_container(item, preserve_dates=preserve_dates)
                for item in v
            ),
            key=_model_sort_key,
        )
    if isinstance(v, dict):
        out: dict[str, Any] = {}
        winners: dict[str, str] = {}
        for k, val in v.items():
            clean_k = _stringify_dict_key(k)
            clean_v = _normalize_nested_container(val, preserve_dates=preserve_dates)
            rank = _canonical_model_key_rank(k)
            if clean_k not in out or rank >= winners[clean_k]:
                out[clean_k] = clean_v
                winners[clean_k] = rank
        return out
    return v


def _coerce_scalar_stat(v: Any) -> Any:
    v = _ensure_finite_number(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Path):
        return v.as_posix()
    if isinstance(v, Enum):
        return _coerce_scalar_stat(v.value)
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    if isinstance(v, (dict, list, tuple, set, frozenset)):
        return json.dumps(
            _to_json_stat_compatible(v),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
    return v


def _coerce_sample_values(v: Any) -> list[Any]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v != "" else []
    if isinstance(v, (list, tuple)):
        return [_normalize_nested_container(item) for item in v]
    if isinstance(v, (set, frozenset)):
        return sorted(
            (_normalize_nested_container(item) for item in v),
            key=_model_sort_key,
        )
    return [_normalize_nested_container(v)]


def _coerce_sample_rows(v: Any) -> Any:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [_normalize_nested_container(row) for row in v]
    if isinstance(v, (set, frozenset)):
        return sorted(
            (_normalize_nested_container(row) for row in v),
            key=_model_sort_key,
        )
    return v


def _coerce_non_null_str(v: Any) -> str:
    if v is None:
        return ""
    v = _ensure_finite_number(v)
    if isinstance(v, bool):
        raise ValueError("Boolean is not a valid string value")
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, (list, tuple, set, frozenset, dict)):
        return json.dumps(
            _to_json_stat_compatible(v),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
    return str(v)


def _coerce_str_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        return [s] if s else []
    if isinstance(v, (list, tuple)):
        out: list[str] = []
        for item in v:
            if item is None:
                continue
            norm_item = _stringify_dict_key(item)
            s = norm_item.strip()
            if s:
                out.append(s)
        return out
    if isinstance(v, (set, frozenset)):
        out_set: list[str] = []
        for item in v:
            if item is None:
                continue
            norm_item = _stringify_dict_key(item)
            s = norm_item.strip()
            if s:
                out_set.append(s)
        return sorted(out_set)
    raise ValueError(f"Expected str or list[str], got {type(v).__name__}")


def _coerce_str_dict(v: Any) -> dict[str, str]:
    if v is None:
        return {}
    if isinstance(v, dict):
        out: dict[str, str] = {}
        winners: dict[str, str] = {}
        for k, val in v.items():
            clean_k = _stringify_dict_key(k)
            clean_v = "" if val is None else _stringify_dict_key(val)
            rank = _canonical_model_key_rank(k)
            if clean_k not in out or rank >= winners[clean_k]:
                out[clean_k] = clean_v
                winners[clean_k] = rank
        return out
    raise ValueError(f"Expected dict[str, str], got {type(v).__name__}")


def _lowercase_str(v: Any) -> Any:
    return v.strip().lower() if isinstance(v, str) else v


def _normalize_question_category(v: Any) -> Any:
    if not isinstance(v, str):
        return v
    norm = re.sub(r"[\s/\-]+", "_", v.strip().lower()).strip("_")
    mapping = {
        "manager": "manager",
        "mgr": "manager",
        "it_security": "it_security",
        "it_security_platform_admin": "it_security",
        "it": "it_security",
        "security": "it_security",
        "data_engineering": "data_engineering",
        "de": "data_engineering",
        "power_bi": "power_bi",
        "power_bi_analytics_owners": "power_bi",
        "pbi": "power_bi",
        "bi": "power_bi",
        "business_finance": "business_finance",
        "business_unit_leads_&_finance": "business_finance",
        "business_unit_leads_and_finance": "business_finance",
        "finance": "business_finance",
        "business": "business_finance",
    }
    return mapping.get(norm, norm)


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-") or "unknown"


StrictFloat = Annotated[
    float, BeforeValidator(_reject_bool_float), Field(allow_inf_nan=False)
]
StrictInt = Annotated[int, BeforeValidator(_reject_bool_int)]
DefaultDate = Annotated[date, BeforeValidator(_coerce_date)]
OptionalIsoStr = Annotated[str | None, BeforeValidator(_coerce_optional_iso_str)]
ScalarStat = Annotated[
    str | int | float | bool | None, BeforeValidator(_coerce_scalar_stat)
]
SampleValuesList = Annotated[list[Any], BeforeValidator(_coerce_sample_values)]
SampleRowsList = Annotated[
    list[dict[str, Any]], BeforeValidator(_coerce_sample_rows)
]
NonNullStr = Annotated[str, BeforeValidator(_coerce_non_null_str)]
StrList = Annotated[list[str], BeforeValidator(_coerce_str_list)]
StrDict = Annotated[dict[str, str], BeforeValidator(_coerce_str_dict)]
LowerStr = BeforeValidator(_lowercase_str)


class VaultBaseModel(BaseModel):
    """Shared base model for canonical Markdown vault entities."""

    model_config = ConfigDict(
        populate_by_name=True,
        extra="ignore",
        validate_assignment=True,
        allow_inf_nan=False,
    )


class ColumnProfile(VaultBaseModel):
    """Schema & statistical profile for a single dataset column."""

    name: str = Field(
        min_length=1, validation_alias=AliasChoices("name", "column_name")
    )
    data_type: NonNullStr = Field(
        default="STRING",
        validation_alias=AliasChoices("data_type", "type", "col_type"),
    )
    nullable: bool = True
    null_pct: StrictFloat | None = Field(default=None, ge=0.0, le=100.0)
    distinct_count: StrictInt | None = Field(default=None, ge=0)
    min_val: ScalarStat = Field(
        default=None, validation_alias=AliasChoices("min_val", "min_value")
    )
    max_val: ScalarStat = Field(
        default=None, validation_alias=AliasChoices("max_val", "max_value")
    )
    sample_values: SampleValuesList = Field(default_factory=list)
    is_pii: bool = False
    comment: NonNullStr = ""


class DatasetCard(VaultBaseModel):
    """Canonical Databricks / Lakehouse Dataset vault card."""

    id: str = ""
    fqn: str = ""
    catalog: str = ""
    schema_name: str = Field(
        default="",
        validation_alias=AliasChoices("schema_name", "schema", "database"),
    )
    table_name: str = Field(
        default="", validation_alias=AliasChoices("table_name", "table")
    )
    table_type: Literal["MANAGED", "EXTERNAL", "VIEW"] = "MANAGED"
    medallion_layer: Literal["bronze", "silver", "gold", "unknown"] = "unknown"
    row_count: StrictInt | None = Field(default=None, ge=0)
    min_timestamp: OptionalIsoStr = None
    max_timestamp: OptionalIsoStr = None
    freshness_lag_hours: StrictFloat | None = Field(default=None, ge=0.0)
    query_count_30d: StrictInt | None = Field(default=None, ge=0)
    grain: NonNullStr = "unknown"
    join_keys: StrList = Field(default_factory=list)
    columns: list[ColumnProfile] = Field(default_factory=list)
    sample_rows: SampleRowsList = Field(default_factory=list)
    upstream_refs: StrList = Field(default_factory=list)
    downstream_refs: StrList = Field(default_factory=list)
    scheduled_jobs: StrList = Field(default_factory=list)
    tags: StrDict = Field(default_factory=dict)
    updated_at: DefaultDate = Field(default_factory=date.today)

    @model_validator(mode="before")
    @classmethod
    def _reconcile_identifiers_and_enums(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)

        raw_tt = out.get("table_type")
        if isinstance(raw_tt, str):
            tt_up = raw_tt.strip().upper()
            tt_map = {
                "BASE TABLE": "MANAGED",
                "MANAGED TABLE": "MANAGED",
                "EXTERNAL TABLE": "EXTERNAL",
                "FOREIGN": "EXTERNAL",
                "MATERIALIZED VIEW": "VIEW",
                "STREAMING TABLE": "MANAGED",
            }
            out["table_type"] = tt_map.get(tt_up, tt_up)

        def _clean_str_ident(val: Any) -> str:
            return val.strip() if isinstance(val, str) else ""

        raw_fqn = out.get("fqn")
        raw_cat = out.get("catalog")
        raw_sch = (
            out.get("schema_name")
            if out.get("schema_name") is not None
            else (
                out.get("schema")
                if out.get("schema") is not None
                else out.get("database")
            )
        )
        raw_tbl = (
            out.get("table_name")
            if out.get("table_name") is not None
            else out.get("table")
        )

        fqn = _clean_str_ident(raw_fqn)
        catalog = _clean_str_ident(raw_cat)
        schema_val = _clean_str_ident(raw_sch)
        table_val = _clean_str_ident(raw_tbl)

        if fqn and (not catalog or not schema_val or not table_val):
            parts = [p.strip() for p in fqn.split(".") if p.strip()]
            if len(parts) >= 3:
                catalog = catalog or parts[0]
                schema_val = schema_val or parts[1]
                table_val = table_val or ".".join(parts[2:])
            elif len(parts) == 2:
                catalog = catalog or "hive_metastore"
                schema_val = schema_val or parts[0]
                table_val = table_val or parts[1]
                fqn = f"{catalog}.{schema_val}.{table_val}"

        if not fqn and catalog and schema_val and table_val:
            fqn = f"{catalog}.{schema_val}.{table_val}"

        if catalog and (raw_cat is None or isinstance(raw_cat, str)):
            out["catalog"] = catalog
        if schema_val and (raw_sch is None or isinstance(raw_sch, str)):
            out["schema_name"] = schema_val
        if table_val and (raw_tbl is None or isinstance(raw_tbl, str)):
            out["table_name"] = table_val
        if fqn and (raw_fqn is None or isinstance(raw_fqn, str)):
            out["fqn"] = fqn
        raw_id = out.get("id")
        if (raw_id is None or (isinstance(raw_id, str) and not raw_id.strip())) and fqn:
            out["id"] = fqn

        raw_layer = out.get("medallion_layer")
        if isinstance(raw_layer, str) and raw_layer.strip():
            out["medallion_layer"] = raw_layer.strip().lower()
        elif raw_layer is None or isinstance(raw_layer, str):
            probe = f"{schema_val} {table_val} {fqn}".lower()
            inferred = "unknown"
            for layer in ("gold", "silver", "bronze"):
                if re.search(rf"(?:^|[_.\s-]){layer}(?:$|[_.\s-])", probe):
                    inferred = layer
                    break
            out["medallion_layer"] = inferred

        return out

    @model_validator(mode="after")
    def _validate_non_empty_identity(self) -> DatasetCard:
        for attr in ("id", "fqn", "catalog", "schema_name", "table_name"):
            if not getattr(self, attr).strip():
                raise ValueError(f"DatasetCard.{attr} must be non-empty")
        return self


class DaxMeasure(VaultBaseModel):
    """Power BI semantic model DAX measure & heuristic flags."""

    name: str = Field(min_length=1)
    table: NonNullStr = ""
    expression: NonNullStr = ""
    description: NonNullStr = ""
    flagged_heuristics: StrList = Field(default_factory=list)


class ReportCard(VaultBaseModel):
    """Canonical Power BI Report & Semantic Model vault card."""

    id: str = ""
    title: str = Field(min_length=1)
    workspace: NonNullStr = "Default Workspace"
    semantic_model: NonNullStr = ""
    refresh_schedule: OptionalIsoStr = None
    refresh_failures_30d: StrictInt = Field(default=0, ge=0)
    upstream_datasets: StrList = Field(default_factory=list)
    measures: list[DaxMeasure] = Field(default_factory=list)
    relationships: SampleRowsList = Field(default_factory=list)
    bi_to_ds_gaps: StrList = Field(default_factory=list)
    high_excel_export: bool = Field(
        default=False,
        validation_alias=AliasChoices("high_excel_export", "manual_excel_export_flag"),
    )
    owners: StrList = Field(default_factory=list)
    updated_at: DefaultDate = Field(default_factory=date.today)

    @model_validator(mode="after")
    def _ensure_id(self) -> ReportCard:
        if not self.id.strip():
            ws = self.workspace.strip()
            object.__setattr__(
                self,
                "id",
                f"{ws}__{self.title.strip()}" if ws else self.title.strip(),
            )
        return self


class StakeholderCard(VaultBaseModel):
    """Canonical Business / Technical Stakeholder vault card."""

    id: str = ""
    name: str = Field(min_length=1)
    role: NonNullStr = ""
    department: NonNullStr = ""
    influence_tier: Annotated[
        Literal["exec", "manager", "partner", "sme"], LowerStr
    ] = "partner"
    decision_appetite: Annotated[
        Literal["high", "medium", "low", "unknown"], LowerStr
    ] = "unknown"
    owned_reports: StrList = Field(default_factory=list)
    kpis_cared_about: StrList = Field(default_factory=list)
    pain_points: StrList = Field(default_factory=list)
    explicit_asks: StrList = Field(default_factory=list)
    boss_mandate_asks: StrList = Field(default_factory=list)
    updated_at: DefaultDate = Field(default_factory=date.today)

    @model_validator(mode="after")
    def _ensure_id(self) -> StakeholderCard:
        if not self.id.strip():
            object.__setattr__(self, "id", _slugify(self.name))
        return self


class QuestionItem(VaultBaseModel):
    """Single categorized Week 1-2 discovery or stakeholder question."""

    id: str = Field(min_length=1)
    category: Annotated[
        Literal[
            "manager",
            "it_security",
            "data_engineering",
            "power_bi",
            "business_finance",
        ],
        BeforeValidator(_normalize_question_category),
    ] = "manager"
    who_to_ask: NonNullStr = Field(
        default="Manager",
        validation_alias=AliasChoices("who_to_ask", "owner"),
    )
    topic: NonNullStr = ""
    question: str = Field(
        min_length=1,
        validation_alias=AliasChoices("question", "prompt"),
    )
    status: Annotated[Literal["open", "answered", "blocked"], LowerStr] = "open"
    answer_summary: str | None = Field(
        default=None,
        validation_alias=AliasChoices("answer_summary", "answer"),
    )
    linked_refs: StrList = Field(default_factory=list)
    phase: NonNullStr = "days_1_5"

    @property
    def owner(self) -> str:
        return self.who_to_ask

    @property
    def prompt(self) -> str:
        return self.question

    @property
    def answer(self) -> str | None:
        return self.answer_summary


class OpenQuestionBank(VaultBaseModel):
    """Collection of categorized Week 1-2 discovery questions."""

    id: NonNullStr = "open-questions"
    title: NonNullStr = "Week 1-2 Discovery Questionnaire & Open Questions"
    questions: list[QuestionItem] = Field(default_factory=list)
    updated_at: DefaultDate = Field(default_factory=date.today)


class PlaybookPhase(VaultBaseModel):
    """Single Day 1-90 onboarding execution phase."""

    id: str = Field(min_length=1)
    phase: str = Field(min_length=1)
    goal: NonNullStr = ""
    actions: StrList = Field(default_factory=list)
    output: NonNullStr = ""
    completed: bool = False


class PlaybookTracker(VaultBaseModel):
    """Solo DS Day 1-90 execution playbook tracker."""

    id: NonNullStr = "day1-90-playbook"
    title: NonNullStr = "Solo DS Day 1-90 Execution Playbook"
    phases: list[PlaybookPhase] = Field(default_factory=list)
    updated_at: DefaultDate = Field(default_factory=date.today)


class GlossaryTerm(VaultBaseModel):
    """Single business acronym, metric, or domain definition."""

    term: str = Field(
        min_length=1,
        validation_alias=AliasChoices("term", "name", "acronym"),
    )
    definition: NonNullStr = Field(
        default="",
        validation_alias=AliasChoices("definition", "meaning", "description"),
    )
    source: NonNullStr = ""
    formula_or_logic: str | None = Field(
        default=None,
        validation_alias=AliasChoices("formula_or_logic", "formula"),
    )
    source_refs: StrList = Field(
        default_factory=list,
        validation_alias=AliasChoices("source_refs", "linked_refs", "sources"),
    )
    owner: str | None = None

    @property
    def linked_refs(self) -> list[str]:
        return self.source_refs


class GlossaryCard(VaultBaseModel):
    """Business & KPI glossary card (`vault/glossary.md`)."""

    id: NonNullStr = "glossary"
    title: NonNullStr = "Business & KPI Glossary"
    terms: list[GlossaryTerm] = Field(default_factory=list)
    updated_at: DefaultDate = Field(default_factory=date.today)


class InternalDatasetUse(VaultBaseModel):
    """Internal dataset dependency & required engineering work for an Opportunity."""

    ref: str = Field(min_length=1)
    role: NonNullStr = ""
    transformations_needed: StrList = Field(default_factory=list)


class ExternalDataReq(VaultBaseModel):
    """External free or vendor dataset requirement for an Opportunity."""

    source: str = Field(min_length=1)
    purpose: NonNullStr = ""
    acquisition: Annotated[
        Literal["none", "free_public_api", "existing_vendor", "net_new_budget"],
        LowerStr,
    ] = "free_public_api"
    blocker_severity: Annotated[
        Literal["low", "medium", "high"], LowerStr
    ] = "low"


class ReportSupplement(VaultBaseModel):
    """Existing Power BI report enriched by an Opportunity."""

    ref: str = Field(min_length=1)
    integration_mode: NonNullStr = ""


class NewReportSpec(VaultBaseModel):
    """Net-new Power BI report or decision tool unlocked by an Opportunity."""

    title: str = Field(min_length=1)
    audience: NonNullStr = ""
    why_net_new: NonNullStr = ""


class DriverSpec(VaultBaseModel):
    """Declared economic driver input for Driver-Tree ROI valuation."""

    name: str = Field(min_length=1)
    value: StrictFloat | None = None
    range: tuple[StrictFloat, StrictFloat, StrictFloat] | None = None
    source: NonNullStr = ""
    verified: bool = False
    verify_question: str | None = None

    @model_validator(mode="after")
    def _validate_driver(self) -> DriverSpec:
        if not self.name.isidentifier() or keyword.iskeyword(self.name):
            raise ValueError(
                f"Driver name {self.name!r} must be a valid non-keyword Python identifier"
            )
        if self.value is None and self.range is None:
            raise ValueError(
                f"Driver {self.name!r} must specify either scalar 'value' or 3-tuple 'range'"
            )
        if self.value is not None and not math.isfinite(float(self.value)):
            raise ValueError(f"Driver {self.name!r} value must be finite")
        if self.range is not None:
            low, base, high = self.range
            if not (
                math.isfinite(float(low))
                and math.isfinite(float(base))
                and math.isfinite(float(high))
            ):
                raise ValueError(f"Driver {self.name!r} range values must be finite")
            if not (low <= base <= high):
                raise ValueError(
                    f"Driver {self.name!r} range must satisfy low <= base <= high, got {self.range}"
                )
        return self

    def as_triplet(self) -> tuple[float, float, float]:
        """Return (low, base, high) numeric values for AST evaluation."""
        if self.range is not None:
            return (
                float(self.range[0]),
                float(self.range[1]),
                float(self.range[2]),
            )
        assert self.value is not None
        v = float(self.value)
        return (v, v, v)


_MAX_DRIVER_FORMULA_AST_NODES = 256


def _eval_ast_node(node: ast.AST, env: dict[str, float]) -> float:
    if isinstance(node, ast.Expression):
        return _eval_ast_node(node.body, env)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError(f"Disallowed literal in driver_formula: {node.value!r}")
        try:
            val = float(node.value)
        except OverflowError as exc:
            raise ValueError(
                f"Numeric literal overflow in driver_formula: {exc}"
            ) from exc
        if not math.isfinite(val):
            raise ValueError("Non-finite literal in driver_formula")
        return val
    if isinstance(node, ast.Name):
        if not isinstance(node.ctx, ast.Load):
            raise ValueError(f"Invalid variable context for {node.id!r}")
        if node.id not in env:
            raise ValueError(
                f"Undeclared driver variable {node.id!r} in driver_formula"
            )
        val = env[node.id]
        if isinstance(val, bool) or not math.isfinite(val):
            raise ValueError(
                f"Non-finite value for driver variable {node.id!r} in driver_formula"
            )
        return val
    if isinstance(node, ast.UnaryOp):
        operand = _eval_ast_node(node.operand, env)
        if isinstance(node.op, ast.UAdd):
            res = +operand
        elif isinstance(node.op, ast.USub):
            res = -operand
        else:
            raise ValueError(
                f"Disallowed unary operator {type(node.op).__name__} in driver_formula"
            )
        if not math.isfinite(res):
            raise ValueError("Non-finite unary result in driver_formula")
        return res
    if isinstance(node, ast.BinOp):
        left = _eval_ast_node(node.left, env)
        right = _eval_ast_node(node.right, env)
        try:
            if isinstance(node.op, ast.Add):
                res = left + right
            elif isinstance(node.op, ast.Sub):
                res = left - right
            elif isinstance(node.op, ast.Mult):
                res = left * right
            elif isinstance(node.op, ast.Div):
                if right == 0.0:
                    raise ValueError("Division by zero in driver_formula")
                res = left / right
            else:
                raise ValueError(
                    f"Disallowed binary operator {type(node.op).__name__} in driver_formula"
                )
        except (OverflowError, ZeroDivisionError) as exc:
            raise ValueError(f"Arithmetic error in driver_formula: {exc}") from exc
        if not math.isfinite(res):
            raise ValueError("Non-finite arithmetic result in driver_formula")
        return res
    raise ValueError(f"Disallowed AST construct {type(node).__name__} in driver_formula")


def evaluate_driver_formula(
    formula: str,
    drivers: Sequence[DriverSpec],
) -> tuple[float, float, float]:
    """Safely evaluate arithmetic `driver_formula` over declared drivers without `eval()`."""
    if not isinstance(formula, str):
        raise ValueError("driver_formula must be a string")
    expr_text = formula.strip()
    if not expr_text:
        raise ValueError("driver_formula must not be empty")

    try:
        tree = ast.parse(expr_text, mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        preview = expr_text if len(expr_text) <= 120 else expr_text[:117] + "..."
        raise ValueError(
            f"Invalid or overly complex syntax in driver_formula {preview!r}: {exc}"
        ) from exc

    node_count = 0
    for _ in ast.walk(tree):
        node_count += 1
        if node_count > _MAX_DRIVER_FORMULA_AST_NODES:
            raise ValueError(
                f"driver_formula exceeds maximum AST complexity ({_MAX_DRIVER_FORMULA_AST_NODES} nodes)"
            )

    driver_map: dict[str, tuple[float, float, float]] = {}
    for d in drivers:
        if d.name in driver_map:
            raise ValueError(f"Duplicate driver name {d.name!r} in ValuationSpec")
        triplet = d.as_triplet()
        if any(not math.isfinite(x) for x in triplet):
            raise ValueError(f"Non-finite triplet for driver {d.name!r}")
        driver_map[d.name] = triplet

    referenced_names = {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    }
    if not referenced_names:
        raise ValueError(
            "driver_formula must reference at least one declared driver variable"
        )

    results: list[float] = []
    for idx in (0, 1, 2):
        env = {name: triplet[idx] for name, triplet in driver_map.items()}
        try:
            val = _eval_ast_node(tree, env)
        except (OverflowError, ZeroDivisionError, RecursionError) as exc:
            raise ValueError(
                f"Arithmetic overflow or recursion error in driver_formula: {exc}"
            ) from exc
        if not math.isfinite(val):
            raise ValueError("Non-finite evaluation result in driver_formula")
        results.append(val)

    r_low = round(min(results[0], results[1], results[2]), 2)
    r_base = round(results[1], 2)
    r_high = round(max(results[0], results[1], results[2]), 2)
    return (r_low, r_base, r_high)


class ValuationSpec(VaultBaseModel):
    """Auditable Driver-Tree ROI valuation with anti-hallucination & CFO guards."""

    confidence_tier: Annotated[
        Literal["hypothesis", "directional", "finance_verified"], LowerStr
    ]
    driver_formula: str = Field(min_length=1)
    drivers: list[DriverSpec] = Field(min_length=1)
    annual_benefit_low_usd: StrictFloat | None = None
    annual_benefit_base_usd: StrictFloat | None = None
    annual_benefit_high_usd: StrictFloat | None = None
    verification_prompts: list[str] = Field(default_factory=list)

    def compute_from_drivers(self) -> tuple[float, float, float]:
        """Compute (low, base, high) USD directly from `driver_formula` and `drivers`."""
        return evaluate_driver_formula(self.driver_formula, self.drivers)

    @property
    def unverified_drivers_count(self) -> int:
        return sum(1 for d in self.drivers if not d.verified)

    @model_validator(mode="after")
    def _validate_and_populate_valuation(self) -> ValuationSpec:
        if self.confidence_tier == "finance_verified":
            unverified = [d.name for d in self.drivers if not d.verified]
            if unverified:
                raise ValueError(
                    "confidence_tier='finance_verified' requires all drivers verified=True; "
                    f"unverified drivers: {unverified}"
                )

        calc_low, calc_base, calc_high = evaluate_driver_formula(
            self.driver_formula, self.drivers
        )
        low_was_none = self.annual_benefit_low_usd is None
        base_was_none = self.annual_benefit_base_usd is None
        high_was_none = self.annual_benefit_high_usd is None

        for attr, expected in (
            ("annual_benefit_low_usd", calc_low),
            ("annual_benefit_base_usd", calc_base),
            ("annual_benefit_high_usd", calc_high),
        ):
            provided = getattr(self, attr)
            if provided is not None:
                prov_f = float(provided)
                if not math.isfinite(prov_f):
                    raise ValueError(f"{attr} must be finite")
                if expected == 0.0:
                    if abs(prov_f) > 1e-6:
                        raise ValueError(
                            f"{attr}={prov_f} diverges from driver_formula output 0.0"
                        )
                else:
                    rel_err = abs(prov_f - expected) / abs(expected)
                    if rel_err > 0.05 + 1e-9:
                        raise ValueError(
                            f"{attr}={prov_f} diverges from driver_formula output "
                            f"{expected} by {rel_err:.1%} (> 5% max tolerance)"
                        )

        if base_was_none:
            object.__setattr__(self, "annual_benefit_base_usd", calc_base)
        assert self.annual_benefit_base_usd is not None
        final_base = float(self.annual_benefit_base_usd)

        if low_was_none:
            object.__setattr__(self, "annual_benefit_low_usd", min(calc_low, final_base))
        if high_was_none:
            object.__setattr__(
                self, "annual_benefit_high_usd", max(calc_high, final_base)
            )

        assert self.annual_benefit_low_usd is not None
        assert self.annual_benefit_base_usd is not None
        assert self.annual_benefit_high_usd is not None
        if not (
            self.annual_benefit_low_usd
            <= self.annual_benefit_base_usd
            <= self.annual_benefit_high_usd
        ):
            raise ValueError(
                "Annual benefit USD values must satisfy low <= base <= high"
            )

        prompts: list[str] = []
        prefix = "[VERIFY WITH STAKEHOLDER]"
        for d in self.drivers:
            if not d.verified:
                raw_q = (d.verify_question or "").strip()
                if raw_q.upper().startswith(prefix):
                    raw_q = raw_q[len(prefix) :].lstrip(" :-")
                if raw_q:
                    prompts.append(f"{prefix} {d.name}: {raw_q}")
                else:
                    val_repr = (
                        f"range={list(d.range)}"
                        if d.range is not None
                        else f"value={d.value}"
                    )
                    src_label = d.source or "unverified assumption"
                    prompts.append(
                        f"{prefix} {d.name}: Confirm {val_repr} (source: {src_label}) with stakeholder."
                    )
        object.__setattr__(self, "verification_prompts", prompts)
        return self


class OpportunitySpec(VaultBaseModel):
    """Canonical DS Opportunity Specification (`staging/opportunities` & `opportunities`)."""

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    status: Annotated[
        Literal["staging", "backlog", "active", "blocked", "shipped", "archived"],
        LowerStr,
    ] = "staging"
    boss_mandate: bool = False
    domain_pack: NonNullStr = "ops_supply_chain"
    created_at: DefaultDate = Field(default_factory=date.today)
    updated_at: DefaultDate = Field(default_factory=date.today)
    internal_datasets: list[InternalDatasetUse] = Field(default_factory=list)
    external_data_required: list[ExternalDataReq] = Field(default_factory=list)
    existing_reports_supplemented: list[ReportSupplement] = Field(default_factory=list)
    new_reports_unlocked: list[NewReportSpec] = Field(default_factory=list)
    valuation: ValuationSpec
    effort_weeks: StrictFloat = Field(gt=0.0)
    data_readiness_score: StrictInt = Field(ge=1, le=5)
    adoption_friction_score: StrictInt = Field(ge=1, le=5)
    computed_priority_score: StrictFloat | None = None
    steps_to_completion: StrList = Field(default_factory=list)
    stakeholders: StrList = Field(default_factory=list)
    cfo_critique_verdict: str | None = None
    cfo_critique_notes: StrList = Field(default_factory=list)

    @model_validator(mode="after")
    def _populate_or_check_priority_score(self) -> OpportunitySpec:
        if self.computed_priority_score is None:
            object.__setattr__(
                self, "computed_priority_score", compute_priority_score(self)
            )
        else:
            score = float(self.computed_priority_score)
            if not math.isfinite(score) or score < 0.0 or score > 100.0:
                raise ValueError(
                    f"computed_priority_score must be between 0.0 and 100.0, got {score}"
                )
        return self


def compute_priority_score(
    opp: OpportunitySpec,
    weights: ScoringWeightsConfig | dict[str, Any] | None = None,
) -> float:
    """Compute weighted composite priority score in [0, 100] scaled by confidence tier."""
    if weights is None:
        w = ScoringWeightsConfig()
    elif isinstance(weights, dict):
        w = ScoringWeightsConfig.model_validate(weights)
    else:
        w = weights

    base_usd = max(0.0, float(opp.valuation.annual_benefit_base_usd or 0.0))
    cap_usd = max(1.0, float(w.roi_cap_usd))
    norm_roi = min(100.0, (base_usd / cap_usd) * 100.0)

    norm_readiness = ((float(opp.data_readiness_score) - 1.0) / 4.0) * 100.0

    bonus_cap = min(100.0, max(0.0, float(w.existing_pbi_bonus_points)))
    base_adoption_span = 100.0 - bonus_cap
    pbi_bonus = bonus_cap if len(opp.existing_reports_supplemented) > 0 else 0.0
    norm_adoption = min(
        100.0,
        max(
            0.0,
            ((5.0 - float(opp.adoption_friction_score)) / 4.0) * base_adoption_span
            + pbi_bonus,
        ),
    )

    norm_speed = (4.0 / (4.0 + float(opp.effort_weeks))) * 100.0

    raw_score = (
        w.roi_weight * norm_roi
        + w.data_readiness_weight * norm_readiness
        + w.bi_adoption_ease_weight * norm_adoption
        + w.speed_to_value_weight * norm_speed
    )
    raw_mult = w.confidence_multipliers.get(opp.valuation.confidence_tier, 0.60)
    if (
        isinstance(raw_mult, bool)
        or not isinstance(raw_mult, (int, float))
        or not math.isfinite(raw_mult)
        or raw_mult <= 0.0
        or raw_mult > 1.0
    ):
        raise ValueError(
            f"Invalid confidence multiplier for {opp.valuation.confidence_tier!r}: {raw_mult!r}"
        )
    conf_mult = float(raw_mult)
    if not math.isfinite(raw_score):
        raise ValueError("Non-finite intermediate priority score")
    return round(min(100.0, max(0.0, raw_score * conf_mult)), 2)


class ScheduledOpportunity(VaultBaseModel):
    """Single ranked opportunity in a quarterly capacity simulation."""

    rank: StrictInt
    baseline_rank: StrictInt
    id: str
    title: str
    status: Literal["staging", "backlog", "active", "blocked", "shipped", "archived"]
    boss_mandate: bool
    domain_pack: str
    effort_weeks: StrictFloat
    cumulative_effort_weeks: StrictFloat
    computed_priority_score: StrictFloat
    confidence_tier: Literal["hypothesis", "directional", "finance_verified"]
    annual_benefit_low_usd: StrictFloat
    annual_benefit_base_usd: StrictFloat
    annual_benefit_high_usd: StrictFloat
    above_cutline: bool
    baseline_above_cutline: bool
    displaced_by_mandate: bool
    opportunity: OpportunitySpec


class PortfolioSchedule(VaultBaseModel):
    """Quarterly capacity cutline & boss-mandate displacement schedule."""

    capacity_weeks: StrictFloat
    used_capacity_weeks: StrictFloat
    mandate_weeks: StrictFloat
    remaining_capacity_weeks: StrictFloat
    overflow_weeks: StrictFloat = 0.0
    over_capacity: bool
    items: list[ScheduledOpportunity] = Field(default_factory=list)
    funded_ids: list[str] = Field(default_factory=list)
    displaced_ids: list[str] = Field(default_factory=list)


def compute_portfolio_schedule(
    opportunities: Sequence[OpportunitySpec],
    quarterly_capacity_weeks: float = 12.0,
    weights: ScoringWeightsConfig | dict[str, Any] | None = None,
) -> PortfolioSchedule:
    """Compute baseline vs boss-mandate quarterly person-week cutline & displacement."""
    if (
        isinstance(quarterly_capacity_weeks, bool)
        or not isinstance(quarterly_capacity_weeks, (int, float))
        or not math.isfinite(quarterly_capacity_weeks)
        or quarterly_capacity_weeks < 0.0
    ):
        raise ValueError(
            f"quarterly_capacity_weeks must be a finite number >= 0.0, got {quarterly_capacity_weeks!r}"
        )

    cap = float(quarterly_capacity_weeks)
    eligible = [
        opp for opp in opportunities if opp.status in {"backlog", "active"}
    ]

    scores: dict[str, float] = {}
    for opp in eligible:
        if weights is not None:
            scores[opp.id] = compute_priority_score(opp, weights)
        elif opp.computed_priority_score is not None:
            scores[opp.id] = float(opp.computed_priority_score)
        else:
            scores[opp.id] = compute_priority_score(opp)

    # Pass 1: Unpinned pure-ROI baseline cutline
    baseline_sorted = sorted(
        eligible,
        key=lambda o: (-scores[o.id], float(o.effort_weeks), o.id),
    )
    baseline_rank_map: dict[str, int] = {}
    baseline_above_map: dict[str, bool] = {}
    base_cum = 0.0
    for idx, opp in enumerate(baseline_sorted, start=1):
        base_cum += float(opp.effort_weeks)
        baseline_rank_map[opp.id] = idx
        baseline_above_map[opp.id] = base_cum <= cap + 1e-9

    # Pass 2: Boss-mandate pinned cutline
    pinned_sorted = sorted(
        eligible,
        key=lambda o: (
            not o.boss_mandate,
            -scores[o.id],
            float(o.effort_weeks),
            o.id,
        ),
    )
    items: list[ScheduledOpportunity] = []
    pin_cum = 0.0
    for rank, opp in enumerate(pinned_sorted, start=1):
        effort = float(opp.effort_weeks)
        pin_cum += effort
        above = True if opp.boss_mandate else (pin_cum <= cap + 1e-9)
        base_above = baseline_above_map[opp.id]
        displaced = bool(base_above and not above)
        items.append(
            ScheduledOpportunity(
                rank=rank,
                baseline_rank=baseline_rank_map[opp.id],
                id=opp.id,
                title=opp.title,
                status=opp.status,
                boss_mandate=opp.boss_mandate,
                domain_pack=opp.domain_pack,
                effort_weeks=round(effort, 2),
                cumulative_effort_weeks=round(pin_cum, 2),
                computed_priority_score=round(scores[opp.id], 2),
                confidence_tier=opp.valuation.confidence_tier,
                annual_benefit_low_usd=float(
                    opp.valuation.annual_benefit_low_usd or 0.0
                ),
                annual_benefit_base_usd=float(
                    opp.valuation.annual_benefit_base_usd or 0.0
                ),
                annual_benefit_high_usd=float(
                    opp.valuation.annual_benefit_high_usd or 0.0
                ),
                above_cutline=above,
                baseline_above_cutline=base_above,
                displaced_by_mandate=displaced,
                opportunity=opp,
            )
        )

    raw_used = sum(
        float(i.opportunity.effort_weeks) for i in items if i.above_cutline
    )
    raw_mandate = sum(
        float(i.opportunity.effort_weeks) for i in items if i.boss_mandate
    )
    over_capacity = raw_used > cap + 1e-9
    used_weeks = round(raw_used, 2)
    mandate_weeks = round(raw_mandate, 2)
    if over_capacity:
        if used_weeks <= round(cap, 2):
            used_weeks = round(raw_used, 6) or raw_used
        remaining_weeks = 0.0
        raw_overflow = max(0.0, raw_used - cap)
        overflow_weeks = round(raw_overflow, 2)
        if overflow_weeks == 0.0:
            overflow_weeks = round(raw_overflow, 6) or raw_overflow
    else:
        remaining_weeks = round(max(0.0, cap - raw_used), 2)
        overflow_weeks = 0.0

    return PortfolioSchedule(
        capacity_weeks=round(cap, 2),
        used_capacity_weeks=used_weeks,
        mandate_weeks=mandate_weeks,
        remaining_capacity_weeks=remaining_weeks,
        overflow_weeks=overflow_weeks,
        over_capacity=over_capacity,
        items=items,
        funded_ids=[i.id for i in items if i.above_cutline],
        displaced_ids=[i.id for i in items if i.displaced_by_mandate],
    )
