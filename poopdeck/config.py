"""Configuration models and loader for Project Poopdeck."""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

DEFAULT_PII_COLUMN_PATTERNS: list[str] = [
    r"(?:^|[_.\s-])e[-_]?mails?(?:$|[_.\s-])",
    r"(?:^|[_.\s-])(?:ssn|social[_.\s-]?security(?:[_.\s-]?(?:number|num|no))?|tax[_.\s-]?id|national[_.\s-]?id|passport(?:[_.\s-]?(?:number|num|no))?|driver[_.\s-]?licen[sc]e)(?:$|[_.\s-])",
    r"(?:^|[_.\s-])(?:phone|phone[_.\s-]?(?:number|num|no|ext)|telephone|mobile|mobile[_.\s-]?(?:number|num|no|phone)|cell[_.\s-]?phone|fax|fax[_.\s-]?(?:number|num|no))(?:$|[_.\s-])",
    r"(?:^|[_.\s-])(?:dob|birth[_.\s-]?date|date[_.\s-]?of[_.\s-]?birth|birth[_.\s-]?day)(?:$|[_.\s-])",
    r"(?:^|[_.\s-])(?:salary|salaries|compensation|wage|wages|pay[_.\s-]?rate|bonus)(?:$|[_.\s-])",
    r"(?:^|[_.\s-])(?:address|addresses|street|street[_.\s-]?(?:line|num|number|1|2)?|apt[_.\s-]?no|zip|zip[_.\s-]?code|postal|postal[_.\s-]?code|post[_.\s-]?code)(?:$|[_.\s-])",
    r"(?:^|[_.\s-])(?:(?:first|last|middle|full|given|family|maiden|sur|nick|preferred|legal|user|login|customer|client|buyer|shopper|consumer|employees?|emp|staff|worker|personnel|manager|supervisor|person|personal|individual|contact|patient|member|subscriber|applicant|candidate|beneficiary|dependent|spouse|insured|policyholder|account[_.\s-]?holder|card[_.\s-]?holder|owner|author|speaker|attendee|recipient|sender|rep|sales[_.\s-]?rep|agent|driver|passenger)[_.\s-]?names?|name[_.\s-]?(?:first|last|middle|full))(?:$|[_.\s-])",
    r"^name$",
]

DEFAULT_CONFIDENCE_MULTIPLIERS: dict[str, float] = {
    "hypothesis": 0.60,
    "directional": 0.85,
    "finance_verified": 1.00,
}

DEFAULT_EXTERNAL_REMOTES: list[str] = [
    "mckenzm4",
    "github.com",
    "gitlab.com",
    "bitbucket.org",
]


def _reject_bool_float(v: Any) -> Any:
    if isinstance(v, bool):
        raise ValueError("Boolean is not a valid numeric value")
    if isinstance(v, float) and not math.isfinite(v):
        raise ValueError("Numeric value must be finite")
    return v


def _reject_bool_int(v: Any) -> Any:
    if isinstance(v, bool):
        raise ValueError("Boolean is not a valid integer value")
    return v


StrictFloat = Annotated[
    float, BeforeValidator(_reject_bool_float), Field(allow_inf_nan=False)
]
StrictInt = Annotated[int, BeforeValidator(_reject_bool_int)]
_BOOL_ADAPTER: TypeAdapter[bool] = TypeAdapter(bool)


class PrivacyConfig(BaseModel):
    """Privacy & redaction gate settings."""

    model_config = ConfigDict(
        populate_by_name=True, allow_inf_nan=False, validate_assignment=True
    )

    privacy_mode: Literal["safe_aggregates", "scrubbed_samples"] = "safe_aggregates"
    allow_sample_rows: bool = Field(
        default=False,
        validation_alias=AliasChoices("allow_sample_rows", "include_sample_rows"),
    )
    max_sample_rows: StrictInt = Field(default=5, ge=0)
    pii_column_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_PII_COLUMN_PATTERNS)
    )
    redact_secrets: bool = True

    @model_validator(mode="before")
    @classmethod
    def _reconcile_sample_row_aliases(cls, data: Any) -> Any:
        if (
            isinstance(data, dict)
            and "allow_sample_rows" in data
            and "include_sample_rows" in data
        ):
            merged = dict(data)
            raw_allow = merged["allow_sample_rows"]
            raw_include = merged["include_sample_rows"]
            allow_val = _BOOL_ADAPTER.validate_python(
                raw_allow.strip() if isinstance(raw_allow, str) else raw_allow
            )
            include_val = _BOOL_ADAPTER.validate_python(
                raw_include.strip() if isinstance(raw_include, str) else raw_include
            )
            merged["allow_sample_rows"] = allow_val or include_val
            merged.pop("include_sample_rows", None)
            return merged
        return data

    @property
    def include_sample_rows(self) -> bool:
        """Alias property for compatibility with `include_sample_rows` callers."""
        return self.allow_sample_rows

    @include_sample_rows.setter
    def include_sample_rows(self, value: bool) -> None:
        self.allow_sample_rows = _BOOL_ADAPTER.validate_python(
            value.strip() if isinstance(value, str) else value
        )


class DatabricksConfig(BaseModel):
    """Databricks dual-mode crawler configuration."""

    model_config = ConfigDict(allow_inf_nan=False, validate_assignment=True)

    mode: Literal["auto", "tier1", "tier2"] = "auto"
    host: str = ""
    warehouse_id: str = ""
    catalogs_include: list[str] = Field(default_factory=lambda: ["*"])
    catalogs_exclude: list[str] = Field(
        default_factory=lambda: ["samples", "system", "hive_metastore_scratch"]
    )
    lineage_lookback_days: StrictInt = Field(default=30, ge=1)
    bundle_path: str = "vault/inbox/databricks"

    @field_validator("mode", mode="before")
    @classmethod
    def _normalize_mode(cls, value: Any) -> Any:
        if isinstance(value, str):
            norm = value.strip().lower()
            alias_map = {
                "tier1_sdk": "tier1",
                "tier2_bundle": "tier2",
                "tier2_local": "tier2",
            }
            return alias_map.get(norm, norm)
        return value


class PowerBIConfig(BaseModel):
    """Power BI dual-mode crawler configuration."""

    model_config = ConfigDict(allow_inf_nan=False, validate_assignment=True)

    mode: Literal["auto", "tier1", "tier2"] = "auto"
    use_az_cli_token: bool = True
    workspaces_include: list[str] = Field(default_factory=lambda: ["*"])
    local_exports_path: str = "vault/inbox/powerbi"

    @field_validator("mode", mode="before")
    @classmethod
    def _normalize_mode(cls, value: Any) -> Any:
        if isinstance(value, str):
            norm = value.strip().lower()
            alias_map = {
                "tier1_rest": "tier1",
                "tier2_local": "tier2",
                "tier2_bundle": "tier2",
            }
            return alias_map.get(norm, norm)
        return value


class TeamsConfig(BaseModel):
    """Microsoft Teams & context dropzone configuration."""

    model_config = ConfigDict(allow_inf_nan=False, validate_assignment=True)

    inbox_path: str = "vault/inbox"
    archive_processed: bool = True


class ScoringWeightsConfig(BaseModel):
    """Composite priority scoring weights & ROI normalization parameters."""

    model_config = ConfigDict(allow_inf_nan=False, validate_assignment=True)

    roi_weight: StrictFloat = Field(default=0.40, ge=0.0, le=1.0)
    data_readiness_weight: StrictFloat = Field(default=0.25, ge=0.0, le=1.0)
    bi_adoption_ease_weight: StrictFloat = Field(default=0.20, ge=0.0, le=1.0)
    speed_to_value_weight: StrictFloat = Field(default=0.15, ge=0.0, le=1.0)
    existing_pbi_bonus_points: StrictFloat = Field(default=10.0, ge=0.0)
    roi_cap_usd: StrictFloat = Field(default=1_000_000.0, gt=0.0)
    confidence_multipliers: dict[str, StrictFloat] = Field(
        default_factory=lambda: dict(DEFAULT_CONFIDENCE_MULTIPLIERS)
    )

    @model_validator(mode="after")
    def _validate_weights(self) -> ScoringWeightsConfig:
        total = (
            self.roi_weight
            + self.data_readiness_weight
            + self.bi_adoption_ease_weight
            + self.speed_to_value_weight
        )
        if not math.isfinite(total) or abs(total - 1.0) > 1e-3:
            raise ValueError(
                f"Scoring component weights must sum to 1.0 (got {total:.4f})"
            )
        merged = dict(DEFAULT_CONFIDENCE_MULTIPLIERS)
        merged.update(self.confidence_multipliers)
        for tier, mult in merged.items():
            if (
                isinstance(mult, bool)
                or not math.isfinite(mult)
                or mult <= 0.0
                or mult > 1.0
            ):
                raise ValueError(
                    f"Confidence multiplier for '{tier}' must be in (0.0, 1.0], got {mult}"
                )
        object.__setattr__(self, "confidence_multipliers", merged)
        return self


class DLPConfig(BaseModel):
    """Corporate DLP remote classification patterns."""

    model_config = ConfigDict(allow_inf_nan=False, validate_assignment=True)

    corporate_remote_patterns: list[str] = Field(default_factory=list)
    external_remote_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXTERNAL_REMOTES)
    )


class PoopdeckConfig(BaseModel):
    """Root configuration model for Project Poopdeck."""

    model_config = ConfigDict(
        populate_by_name=True, allow_inf_nan=False, validate_assignment=True
    )

    project_name: str = "project-poopdeck"
    vault_path: str = "vault"
    domain: str = "ops_supply_chain"
    quarterly_capacity_weeks: StrictFloat = Field(default=12.0, gt=0.0)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    databricks: DatabricksConfig = Field(default_factory=DatabricksConfig)
    powerbi: PowerBIConfig = Field(default_factory=PowerBIConfig)
    teams: TeamsConfig = Field(default_factory=TeamsConfig)
    scoring_weights: ScoringWeightsConfig = Field(default_factory=ScoringWeightsConfig)
    dlp: DLPConfig = Field(default_factory=DLPConfig)

    @field_validator("domain", mode="before")
    @classmethod
    def _normalize_domain(cls, value: Any) -> Any:
        if isinstance(value, str):
            norm = value.strip()
            if not norm:
                raise ValueError("domain must not be empty")
            if norm.lower() == "general":
                return "generic"
            return norm
        return value

    def resolve_vault_dir(self, vault_override: Path | str | None = None) -> Path:
        """Resolve active vault directory honoring CLI override -> POOPDECK_VAULT_DIR -> vault_path."""
        if vault_override is not None and str(vault_override).strip():
            return Path(vault_override)
        env_vault = os.environ.get("POOPDECK_VAULT_DIR", "").strip()
        if env_vault:
            return Path(env_vault)
        return Path(self.vault_path)


def _resolve_config_file(config_path: Path | str | None = None) -> Path | None:
    if config_path is not None and str(config_path).strip():
        explicit = Path(config_path)
        if not explicit.is_file():
            raise FileNotFoundError(f"Configuration file not found: {explicit}")
        return explicit

    env_path = os.environ.get("POOPDECK_CONFIG_PATH", "").strip()
    if env_path:
        env_candidate = Path(env_path)
        if not env_candidate.is_file():
            raise FileNotFoundError(
                f"POOPDECK_CONFIG_PATH points to missing file: {env_candidate}"
            )
        return env_candidate

    cwd = Path.cwd()
    for default_name in ("config.yaml", "config.example.yaml"):
        candidate = cwd / default_name
        if candidate.is_file():
            return candidate

    repo_example = Path(__file__).resolve().parent.parent / "config.example.yaml"
    if repo_example.is_file():
        return repo_example

    return None


def load_config(config_path: Path | str | None = None) -> PoopdeckConfig:
    """Load PoopdeckConfig following PROJECT.md precedence and env overrides."""
    resolved = _resolve_config_file(config_path)
    raw_data: dict[str, Any] = {}
    if resolved is not None:
        text = resolved.read_text(encoding="utf-8-sig")
        loaded = yaml.safe_load(text)
        if loaded is None:
            raw_data = {}
        elif isinstance(loaded, dict):
            raw_data = dict(loaded)
        else:
            raise ValueError(
                f"Configuration file {resolved} must contain a YAML mapping, got {type(loaded).__name__}"
            )

    env_vault = os.environ.get("POOPDECK_VAULT_DIR", "").strip()
    if env_vault:
        raw_data["vault_path"] = env_vault

    return PoopdeckConfig.model_validate(raw_data)
