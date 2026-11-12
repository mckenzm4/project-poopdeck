"""Domain lenses, DS opportunity archetypes, and external data catalog loader."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from poopdeck.config import StrictFloat

logger = logging.getLogger(__name__)

CATALOG_PATH = Path(__file__).resolve().parent / "external_data_catalog.yaml"


class DictAccessMixin:
    """Allow both typed attribute access and dict-style subscripting on catalog models."""

    def __getitem__(self, key: str) -> Any:
        fields = getattr(type(self), "model_fields", {})
        if key in fields:
            return getattr(self, key)
        raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        fields = getattr(type(self), "model_fields", {})
        return isinstance(key, str) and key in fields

    def get(self, key: str, default: Any = None) -> Any:
        fields = getattr(type(self), "model_fields", {})
        return getattr(self, key) if key in fields else default

    def keys(self) -> list[str]:
        return list(getattr(type(self), "model_fields", {}).keys())

    def items(self) -> list[tuple[str, Any]]:
        return [(k, getattr(self, k)) for k in self.keys()]


class ArchetypeSpec(DictAccessMixin, BaseModel):
    """Domain-agnostic Data Science opportunity archetype."""

    model_config = ConfigDict(
        extra="ignore", allow_inf_nan=False, validate_assignment=True
    )

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    ml_framing: str = ""
    replaces_bi_bottleneck: str = ""
    canonical_driver_formula: str = ""
    typical_effort_weeks: StrictFloat = Field(default=3.0, gt=0.0)


class DomainLens(DictAccessMixin, BaseModel):
    """Industry/functional lens guiding discovery & KPI prioritization."""

    model_config = ConfigDict(
        extra="ignore", allow_inf_nan=False, validate_assignment=True
    )

    slug: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = ""
    typical_kpis: list[str] = Field(default_factory=list)
    common_dax_bottlenecks: list[str] = Field(default_factory=list)
    opportunity_archetypes: list[str] = Field(default_factory=list)
    recommended_external_data_ids: list[str] = Field(default_factory=list)


class ExternalDatasetEntry(DictAccessMixin, BaseModel):
    """Curated public or commercial external dataset catalog entry."""

    model_config = ConfigDict(
        extra="ignore", allow_inf_nan=False, validate_assignment=True
    )

    id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    provider: str = ""
    category: str = ""
    acquisition: Literal[
        "none", "free_public_api", "existing_vendor", "net_new_budget"
    ] = "free_public_api"
    blocker_severity: Literal["low", "medium", "high"] = "low"
    purpose: str = ""
    join_keys_hint: list[str] = Field(default_factory=list)
    applicable_domains: list[str] = Field(default_factory=list)


class ExternalDataCatalog(DictAccessMixin, BaseModel):
    """Validated root container for `external_data_catalog.yaml`."""

    model_config = ConfigDict(
        extra="ignore", allow_inf_nan=False, validate_assignment=True
    )

    version: str = "1.0"
    archetypes: list[ArchetypeSpec] = Field(default_factory=list)
    domain_lenses: dict[str, DomainLens] = Field(default_factory=dict)
    external_datasets: list[ExternalDatasetEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ensure_generic_lens(self) -> ExternalDataCatalog:
        if "generic" not in self.domain_lenses:
            raise ValueError(
                "external_data_catalog.yaml must define a 'generic' fallback lens in domain_lenses"
            )
        return self


def load_external_data_catalog(
    catalog_path: Path | str | None = None,
) -> ExternalDataCatalog:
    """Load and validate the external data & domain lens catalog from YAML."""
    target = Path(catalog_path) if catalog_path is not None else CATALOG_PATH
    if not target.is_file():
        raise FileNotFoundError(f"External data catalog not found: {target}")
    raw = yaml.safe_load(target.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError(
            f"External data catalog {target} must be a YAML mapping, got {type(raw).__name__}"
        )
    return ExternalDataCatalog.model_validate(raw)


def get_domain_lens(
    domain: str | None = None,
    catalog: ExternalDataCatalog | None = None,
) -> DomainLens:
    """Return the requested DomainLens, falling back cleanly to `'generic'` if unknown."""
    cat = catalog if catalog is not None else load_external_data_catalog()
    key = (domain or "generic").strip().lower()
    if key == "general":
        key = "generic"
    if key in cat.domain_lenses:
        return cat.domain_lenses[key]
    logger.warning("Unknown domain %r; falling back to 'generic' lens", domain)
    return cat.domain_lenses["generic"]


def list_external_datasets(
    domain: str | None = None,
    free_only: bool = False,
    catalog: ExternalDataCatalog | None = None,
) -> list[ExternalDatasetEntry]:
    """List curated external datasets filtered by domain lens and optional free-API filter."""
    cat = catalog if catalog is not None else load_external_data_catalog()
    norm = (domain or "").strip().lower()
    filter_all = norm in ("", "all", "*")

    resolved_slug = "generic"
    if not filter_all:
        resolved_slug = get_domain_lens(norm, catalog=cat).slug

    results: list[ExternalDatasetEntry] = []
    for entry in cat.external_datasets:
        if free_only and entry.acquisition != "free_public_api":
            continue
        if filter_all or (
            resolved_slug in entry.applicable_domains
            or "generic" in entry.applicable_domains
        ):
            results.append(entry)
    return results


get_external_datasets_for_domain = list_external_datasets

__all__ = [
    "CATALOG_PATH",
    "ArchetypeSpec",
    "DomainLens",
    "ExternalDatasetEntry",
    "ExternalDataCatalog",
    "load_external_data_catalog",
    "get_domain_lens",
    "list_external_datasets",
    "get_external_datasets_for_domain",
]
