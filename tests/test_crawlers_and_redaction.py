"""Requirement acceptance tests for F03, F06, F07 & F08: Privacy Redaction & Dual-Mode Crawlers.

Covers:
- F03 (12 tests: 6 Tier 1 + 6 Tier 2): `poopdeck.redaction` (`is_pii_column`, `redact_text`,
  `redact_dataset_payload`) across PII columns, `\\bname\\b` non-PII lookalikes, inline PII,
  cloud/GitHub/AI/JWT/JDBC/SQL/AccountKey/PEM secrets, and the `safe_aggregates` vs.
  `allow_sample_rows` / `include_sample_rows` privacy gate.
- F06 (11 tests: 5 Tier 1 + 6 Tier 2): `poopdeck.crawlers.databricks_ingest.sync_databricks`
  across Tier 1 mocked Unity Catalog + Hive Metastore fallback, Tier 2 JSON/CSV bundles
  (including UTF-8 BOM `\\ufeff`), `notebooks/databricks_zero_admin_crawler.py` compilation,
  catalog include/exclude filtering, malformed bundle atomicity, and `HUMAN-NOTES` preservation.
- F07 (11 tests: 5 Tier 1 + 6 Tier 2): `poopdeck.crawlers.powerbi_ingest` (`sync_powerbi`,
  `detect_dax_heuristics`) across `queries/powerbi_dax_info_extract.dax`, all 4 DAX heuristic
  classes, Tier 2 `.pbip`/`model.bim`/TMDL/`INFO.*` CSV & JSON exports, Tier 1 mocked REST,
  UTF-8 BOM & JDBC/PAT scrubbing, malformed BIM safety, and `HUMAN-NOTES` preservation.
- F08 (10 tests: 5 Tier 1 + 5 Tier 2): `poopdeck.crawlers.teams_ingest.sync_teams` across
  WebVTT `.vtt`, stdlib OOXML `.docx` paragraphs & tables, and `.md` 1:1 notes, verifying
  stakeholder/glossary/open-questions updates, `vault/inbox/processed/` archiving, inline
  PII/secret scrubbing, corrupted `.docx` batch resilience, and `HUMAN-NOTES` preservation.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import shutil
from types import SimpleNamespace
from typing import Any

import pytest


def _summary_to_dict(summary: Any) -> dict[str, Any]:
    """Normalize a crawler SyncSummary (Pydantic model, dataclass, or dict) into a plain dict."""
    if summary is None:
        return {"_errors": []}
    if isinstance(summary, dict):
        data = dict(summary)
    elif hasattr(summary, "model_dump") and callable(summary.model_dump):
        data = dict(summary.model_dump())
    elif hasattr(summary, "__dict__"):
        data = dict(vars(summary))
    else:
        data = {"value": summary}

    errors: list[Any] = []
    for err_key in ("errors", "failed_files", "failures"):
        val = data.get(err_key)
        if isinstance(val, list):
            errors.extend(val)
    data["_errors"] = errors
    return data


def _load_cards(folder: Path) -> list[tuple[Path, Any]]:
    """Parse all Markdown cards in `folder` sorted deterministically by filename."""
    from poopdeck.vault_io import parse_markdown_card

    if not folder.is_dir():
        return []
    return [(path, parse_markdown_card(path)) for path in sorted(folder.glob("*.md"))]


def _strip_wikilink(ref: str) -> str:
    """Normalize `[[target|alias]]` or `[[target]]` into bare `target`."""
    cleaned = ref.strip()
    if cleaned.startswith("[[") and cleaned.endswith("]]"):
        cleaned = cleaned[2:-2].strip()
    if "|" in cleaned:
        cleaned = cleaned.split("|", 1)[0].strip()
    return cleaned


def _inject_human_notes(card_path: Path, custom_notes: str) -> None:
    """Replace the substring between HUMAN-NOTES markers on disk with `custom_notes`."""
    from poopdeck.vault_io import HUMAN_NOTES_END, HUMAN_NOTES_START

    raw = card_path.read_text(encoding="utf-8")
    start_idx = raw.index(HUMAN_NOTES_START) + len(HUMAN_NOTES_START)
    end_idx = raw.index(HUMAN_NOTES_END, start_idx)
    updated = raw[:start_idx] + custom_notes + raw[end_idx:]
    card_path.write_text(updated, encoding="utf-8", newline="\n")


# ============================================================================
# F03 — Privacy Gate & PII / Secret Redaction (12 tests: 6 Tier 1 + 6 Tier 2)
# ============================================================================


@pytest.mark.tier1
def test_f03_t1_is_pii_column_positive_names_and_comments(fixtures_dir: Path) -> None:
    """Verify is_pii_column flags all 24 canonical PII column names and comment-triggered PII fields."""
    from poopdeck.redaction import is_pii_column

    vectors = json.loads(
        (fixtures_dir / "redaction" / "pii_and_secrets_samples.json").read_text(encoding="utf-8")
    )
    pos_cols = vectors["positive_pii_columns"]
    assert len(pos_cols) == 24
    for item in pos_cols:
        actual = is_pii_column(item["col_name"], comment=item["comment"])
        assert actual is True, f"Expected is_pii_column({item['col_name']!r}) to be True"

    comment_cols = vectors["comment_pii_columns"]
    assert len(comment_cols) == 6
    for item in comment_cols:
        actual = is_pii_column(item["col_name"], comment=item["comment"])
        assert actual is item["expected_is_pii"], (
            f"Mismatch for ({item['col_name']!r}, {item['comment']!r}): got {actual}"
        )


@pytest.mark.tier1
def test_f03_t1_redact_text_scrubs_cloud_github_ai_and_jwt_tokens(fixtures_dir: Path) -> None:
    """Verify redact_text scrubs Databricks/GitHub PATs, Anthropic/OpenAI/AWS keys, and Bearer/standalone JWTs."""
    from poopdeck.redaction import redact_text

    vectors = json.loads(
        (fixtures_dir / "redaction" / "pii_and_secrets_samples.json").read_text(encoding="utf-8")
    )
    target_ids = {f"SEC-{idx:02d}" for idx in range(1, 9)}
    matched = [
        vec
        for vec in vectors["secret_text_vectors"]
        if any(vec["id"].startswith(prefix) for prefix in target_ids)
    ]
    assert len(matched) == 8

    for vec in matched:
        out = redact_text(vec["input"], redact_secrets=True)
        assert vec["expected_marker"] in out, f"Missing marker in {vec['id']}: {out}"
        for forbidden in vec["forbidden_substrings"]:
            assert forbidden not in out, f"Leaked '{forbidden}' in {vec['id']}: {out}"
        for preserved in vec["must_preserve_substrings"]:
            assert preserved in out, f"Lost surrounding text '{preserved}' in {vec['id']}: {out}"


@pytest.mark.tier1
def test_f03_t1_redact_text_scrubs_jdbc_sql_account_key_and_pem_secrets(fixtures_dir: Path) -> None:
    """Verify redact_text scrubs JDBC PWD, SQL Server Password, Azure AccountKey, and PEM private keys."""
    from poopdeck.redaction import redact_text

    vectors = json.loads(
        (fixtures_dir / "redaction" / "pii_and_secrets_samples.json").read_text(encoding="utf-8")
    )
    target_ids = {"SEC-09", "SEC-10", "SEC-11", "SEC-12"}
    matched = [
        vec
        for vec in vectors["secret_text_vectors"]
        if any(vec["id"].startswith(prefix) for prefix in target_ids)
    ]
    assert len(matched) == 4

    for vec in matched:
        out = redact_text(vec["input"], redact_secrets=True)
        assert "[REDACTED:SECRET]" in out, f"Missing [REDACTED:SECRET] in {vec['id']}: {out}"
        for forbidden in vec["forbidden_substrings"]:
            assert forbidden not in out, f"Leaked '{forbidden}' in {vec['id']}: {out}"
        for preserved in vec["must_preserve_substrings"]:
            assert preserved in out, f"Lost context '{preserved}' in {vec['id']}: {out}"


@pytest.mark.tier1
def test_f03_t1_redact_text_scrubs_inline_email_ssn_and_phone_preserving_safe_numbers(
    fixtures_dir: Path,
) -> None:
    """Verify redact_text scrubs emails, SSNs, and phones while preserving IDs, ISO dates & USD metrics."""
    from poopdeck.redaction import redact_text

    vectors = json.loads(
        (fixtures_dir / "redaction" / "pii_and_secrets_samples.json").read_text(encoding="utf-8")
    )
    pii_vecs = vectors["inline_pii_text_vectors"]
    assert len(pii_vecs) == 6

    for vec in pii_vecs:
        out = redact_text(vec["input"])
        if vec["expected_marker"] is not None:
            assert vec["expected_marker"] in out, f"Expected {vec['expected_marker']} in {vec['id']}"
        for forbidden in vec["forbidden_substrings"]:
            assert forbidden not in out, f"Unexpected '{forbidden}' in {vec['id']}: {out}"
        for preserved in vec["must_preserve_substrings"]:
            assert preserved in out, f"Expected '{preserved}' preserved in {vec['id']}: {out}"


@pytest.mark.tier1
def test_f03_t1_redact_dataset_payload_safe_aggregates_default_drops_rows_and_masks_pii_stats(
    fixtures_dir: Path,
) -> None:
    """Verify default safe_aggregates mode drops all sample_rows and masks PII min_val/max_val/sample_values."""
    from poopdeck.config import PrivacyConfig
    from poopdeck.redaction import redact_dataset_payload

    vectors = json.loads(
        (fixtures_dir / "redaction" / "pii_and_secrets_samples.json").read_text(encoding="utf-8")
    )["dataset_payload_vectors"]
    raw_payload = vectors["raw_payload"]
    expected = vectors["expected_safe_aggregates_default"]

    out = redact_dataset_payload(raw_payload, config=PrivacyConfig())
    assert out["sample_rows"] == []

    by_name = {col["name"]: col for col in out["columns"]}
    for pii_col in expected["expected_pii_column_names"]:
        col = by_name[pii_col]
        assert col["is_pii"] is True
        assert col["min_val"] == "[REDACTED:PII]"
        assert col["max_val"] == "[REDACTED:PII]"
        assert all(v == "[REDACTED:PII]" for v in col["sample_values"])

    for safe_col in expected["expected_non_pii_column_names"]:
        assert by_name[safe_col]["is_pii"] is False
    assert by_name["order_id"]["min_val"] == "ORD-000001"
    assert by_name["table_namespace"]["min_val"] == "americas_east"


@pytest.mark.tier1
def test_f03_t1_redact_dataset_payload_allow_sample_rows_masks_pii_and_scrubs_free_text(
    fixtures_dir: Path,
) -> None:
    """Verify allow_sample_rows=True retains up to max_sample_rows while masking PII cells and scrubbing free-text notes."""
    from poopdeck.config import PrivacyConfig
    from poopdeck.redaction import redact_dataset_payload

    raw_payload = json.loads(
        (fixtures_dir / "redaction" / "pii_and_secrets_samples.json").read_text(encoding="utf-8")
    )["dataset_payload_vectors"]["raw_payload"]

    cfg = PrivacyConfig(
        privacy_mode="scrubbed_samples",
        allow_sample_rows=True,
        max_sample_rows=5,
        redact_secrets=True,
    )
    out = redact_dataset_payload(raw_payload, config=cfg)
    assert len(out["sample_rows"]) == 4

    for row in out["sample_rows"]:
        assert row["customer_email"] == "[REDACTED:PII]"
        assert row["salary"] == "[REDACTED:PII]"
        assert row["col_04"] == "[REDACTED:PII]"

    assert out["sample_rows"][0]["order_id"] == "ORD-000001"
    assert out["sample_rows"][0]["table_namespace"] == "americas_east"
    assert "[REDACTED:EMAIL]" in out["sample_rows"][0]["delivery_notes"]
    assert "[REDACTED:PHONE]" in out["sample_rows"][0]["delivery_notes"]
    assert "dock.lead@corp-example.com" not in out["sample_rows"][0]["delivery_notes"]
    assert "867-5309" not in out["sample_rows"][0]["delivery_notes"]

    assert "[REDACTED:SECRET]" in out["sample_rows"][1]["delivery_notes"]
    assert "dapi0123456789abcdef0123456789abcdef" not in out["sample_rows"][1]["delivery_notes"]


@pytest.mark.tier2
def test_f03_t2_is_pii_column_word_boundary_rejects_non_pii_lookalikes(fixtures_dir: Path) -> None:
    """Verify \\bname\\b word-boundary accuracy rejects non-PII lookalikes like table_namespace and schema_name."""
    from poopdeck.config import DEFAULT_PII_COLUMN_PATTERNS, PrivacyConfig
    from poopdeck.redaction import is_pii_column

    vectors = json.loads(
        (fixtures_dir / "redaction" / "pii_and_secrets_samples.json").read_text(encoding="utf-8")
    )
    neg_cols = vectors["negative_non_pii_columns"]
    assert len(neg_cols) == 9

    explicit_word_boundary_cfg = PrivacyConfig(
        pii_column_patterns=[*DEFAULT_PII_COLUMN_PATTERNS, r"\bname\b"]
    )
    for item in neg_cols:
        assert is_pii_column(item["col_name"], comment=item["comment"]) is False, (
            f"False positive on {item['col_name']!r}"
        )
        assert (
            is_pii_column(
                item["col_name"],
                comment=item["comment"],
                config=explicit_word_boundary_cfg,
            )
            is False
        ), f"False positive under \\bname\\b config on {item['col_name']!r}"

    for extra_safe in ("rename_flag", "filename", "fileName"):
        assert is_pii_column(extra_safe) is False
        assert is_pii_column(extra_safe, config=explicit_word_boundary_cfg) is False

    assert is_pii_column("name") is True
    assert is_pii_column("NAME", config=explicit_word_boundary_cfg) is True


@pytest.mark.tier2
def test_f03_t2_redact_text_secrets_false_preserves_tokens_while_still_scrubbing_pii(
    fixtures_dir: Path,
) -> None:
    """Verify redact_secrets=False preserves secret tokens while unconditionally scrubbing inline PII."""
    from poopdeck.redaction import redact_text

    toggle = json.loads(
        (fixtures_dir / "redaction" / "pii_and_secrets_samples.json").read_text(encoding="utf-8")
    )["redact_secrets_toggle_vector"]

    out_true = redact_text(toggle["input"], redact_secrets=True)
    for marker in toggle["when_redact_secrets_true"]["must_contain"]:
        assert marker in out_true
    for forbidden in toggle["when_redact_secrets_true"]["forbidden_substrings"]:
        assert forbidden not in out_true

    out_false = redact_text(toggle["input"], redact_secrets=False)
    for marker in toggle["when_redact_secrets_false"]["must_contain"]:
        assert marker in out_false
    for forbidden in toggle["when_redact_secrets_false"]["forbidden_substrings"]:
        assert forbidden not in out_false

    extra_out = redact_text("SSN 123-45-6789 phone (555) 867-5309", redact_secrets=False)
    assert "[REDACTED:SSN]" in extra_out
    assert "[REDACTED:PHONE]" in extra_out
    assert "123-45-6789" not in extra_out
    assert "867-5309" not in extra_out


@pytest.mark.tier2
def test_f03_t2_redact_dataset_payload_caps_sample_rows_and_blocks_without_allow_flag(
    fixtures_dir: Path,
) -> None:
    """Verify scrubbed_samples without allow_sample_rows still drops rows, and allow_sample_rows enforces max_sample_rows."""
    from poopdeck.config import PrivacyConfig
    from poopdeck.redaction import redact_dataset_payload

    vectors = json.loads(
        (fixtures_dir / "redaction" / "pii_and_secrets_samples.json").read_text(encoding="utf-8")
    )["dataset_payload_vectors"]
    raw_payload = vectors["raw_payload"]

    # Part A: scrubbed_samples with allow_sample_rows=False must fail closed (sample_rows=[])
    cfg_blocked = PrivacyConfig(
        privacy_mode="scrubbed_samples",
        allow_sample_rows=False,
        max_sample_rows=5,
    )
    out_blocked = redact_dataset_payload(raw_payload, config=cfg_blocked)
    assert out_blocked["sample_rows"] == []

    # Part B: allow_sample_rows=True with max_sample_rows=3 truncates 4th row and leaks zero PII/secrets
    expected_capped = vectors["expected_allow_sample_rows_capped"]
    cfg_capped = PrivacyConfig(**expected_capped["config"])
    out_capped = redact_dataset_payload(raw_payload, config=cfg_capped)
    assert len(out_capped["sample_rows"]) == 3

    serialized = json.dumps(out_capped)
    for forbidden in expected_capped["forbidden_anywhere_in_serialized_output"]:
        assert forbidden not in serialized, f"Leaked '{forbidden}' in redacted payload output"


@pytest.mark.tier2
def test_f03_t2_redact_dataset_payload_preserves_non_pii_aggregates_and_scrubs_comment_pats(
    fixtures_dir: Path,
) -> None:
    """Verify numeric/timestamp aggregates survive intact while table and column comments are scrubbed of PATs & emails."""
    from poopdeck.redaction import redact_dataset_payload

    raw_payload = copy.deepcopy(
        json.loads(
            (fixtures_dir / "redaction" / "pii_and_secrets_samples.json").read_text(encoding="utf-8")
        )["dataset_payload_vectors"]["raw_payload"]
    )
    raw_payload["columns"].append(
        {
            "name": "on_hand_qty",
            "data_type": "DOUBLE",
            "null_pct": 1.5,
            "distinct_count": 4850,
            "min_val": 0.0,
            "max_val": 25000.0,
            "is_pii": False,
            "comment": "Extracted using dapi0123456789abcdef0123456789abcdef by ops.lead@corp-example.com",
        }
    )

    out = redact_dataset_payload(raw_payload)
    assert out["row_count"] == 125400
    assert out["min_timestamp"] == "2024-01-01T00:00:00Z"
    assert out["max_timestamp"] == "2026-11-11T06:00:00Z"
    assert out["freshness_lag_hours"] == 2.5
    assert "[REDACTED:SECRET]" in out["comment"]
    assert "[REDACTED:EMAIL]" in out["comment"]
    assert "dapi0123456789abcdef0123456789abcdef" not in out["comment"]
    assert "ops.lead@corp-example.com" not in out["comment"]

    qty_col = next(c for c in out["columns"] if c["name"] == "on_hand_qty")
    assert qty_col["is_pii"] is False
    assert qty_col["min_val"] == 0.0
    assert qty_col["max_val"] == 25000.0
    assert qty_col["null_pct"] == 1.5
    assert qty_col["distinct_count"] == 4850
    assert "[REDACTED:SECRET]" in qty_col["comment"]
    assert "[REDACTED:EMAIL]" in qty_col["comment"]


@pytest.mark.tier2
def test_f03_t2_redact_dataset_payload_does_not_mutate_caller_input_and_handles_empty_null_shapes() -> None:
    """Verify redact_dataset_payload deep-copies caller dicts without mutation and handles empty/null shapes."""
    from poopdeck.config import PrivacyConfig
    from poopdeck.redaction import redact_dataset_payload

    caller_payload = {
        "fqn": "prod.silver.users",
        "comment": "Token dapi0123456789abcdef0123456789abcdef",
        "columns": [
            {
                "name": "email",
                "data_type": "STRING",
                "min_val": "a@example.com",
                "max_val": "z@example.com",
                "sample_values": ["a@example.com"],
            }
        ],
        "sample_rows": [{"email": "a@example.com"}],
    }
    snapshot = copy.deepcopy(caller_payload)

    out = redact_dataset_payload(
        caller_payload,
        config=PrivacyConfig(privacy_mode="scrubbed_samples", allow_sample_rows=True),
    )
    assert caller_payload == snapshot
    assert out["columns"][0]["min_val"] == "[REDACTED:PII]"
    assert out["sample_rows"][0]["email"] == "[REDACTED:PII]"

    assert redact_dataset_payload({})["sample_rows"] == []
    null_shape = redact_dataset_payload(
        {
            "columns": None,
            "sample_rows": None,
            "comment": None,
            "tags": {"owner": "sarah.chen@corp-example.com"},
        }
    )
    assert null_shape["sample_rows"] == []
    assert null_shape["tags"]["owner"] == "[REDACTED:EMAIL]"


@pytest.mark.tier2
def test_f03_t2_privacy_config_include_sample_rows_alias_drives_redaction_gate(
    fixtures_dir: Path,
) -> None:
    """Verify include_sample_rows alias drives redact_dataset_payload and max_sample_rows=0 forces empty rows."""
    from poopdeck.config import PrivacyConfig
    from poopdeck.redaction import redact_dataset_payload

    raw_payload = json.loads(
        (fixtures_dir / "redaction" / "pii_and_secrets_samples.json").read_text(encoding="utf-8")
    )["dataset_payload_vectors"]["raw_payload"]

    cfg_inc = PrivacyConfig.model_validate(
        {
            "privacy_mode": "scrubbed_samples",
            "include_sample_rows": True,
            "max_sample_rows": 2,
        }
    )
    assert cfg_inc.allow_sample_rows is True
    out_inc = redact_dataset_payload(raw_payload, config=cfg_inc)
    assert len(out_inc["sample_rows"]) == 2

    cfg_zero = PrivacyConfig(
        privacy_mode="scrubbed_samples",
        allow_sample_rows=True,
        max_sample_rows=0,
    )
    out_zero = redact_dataset_payload(raw_payload, config=cfg_zero)
    assert out_zero["sample_rows"] == []


# ============================================================================
# F06 — Databricks Dual-Mode Crawler & Zero-Admin Notebook (11 tests: 5 T1 + 6 T2)
# ============================================================================


@pytest.mark.tier1
def test_f06_t1_zero_admin_notebook_exists_and_compiles_cleanly(project_root: Path) -> None:
    """Verify notebooks/databricks_zero_admin_crawler.py exists, compiles as valid Python, and queries UC metadata."""
    nb_path = project_root / "notebooks" / "databricks_zero_admin_crawler.py"
    assert nb_path.is_file(), f"Missing Databricks zero-admin notebook at {nb_path}"

    source = nb_path.read_text(encoding="utf-8-sig")
    assert len(source.strip()) > 0
    compile(source, str(nb_path), "exec")

    lowered = source.lower()
    assert "information_schema" in lowered
    assert "json" in lowered


@pytest.mark.tier1
def test_f06_t1_sync_databricks_tier2_json_bundle_writes_redacted_dataset_cards(
    vault_sandbox: Path,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """Verify Tier 2 JSON bundle ingestion writes all 5 DatasetCards with PII stats masked, PATs scrubbed & 0 sample rows."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.databricks_ingest import sync_databricks
    from poopdeck.models import DatasetCard
    from poopdeck.vault_io import initialize_vault, validate_vault

    initialize_vault(vault_sandbox, seed_questions=True, init_git=False)
    bundle_copy = tmp_path / "discovery_bundle.json"
    shutil.copy2(fixtures_dir / "databricks" / "discovery_bundle.json", bundle_copy)

    sync_databricks(vault_sandbox, PoopdeckConfig(), input_path=bundle_copy, mode="tier2")

    ds_dir = vault_sandbox / "datasets"
    expected_tables = {
        "prod_catalog.bronze.raw_erp_inventory_feed": ("EXTERNAL", "bronze", 950000),
        "prod_catalog.silver.inventory_snapshots": ("MANAGED", "silver", 5183000),
        "prod_catalog.silver.shipment_events": ("MANAGED", "silver", 88400),
        "prod_catalog.gold.po_receipts": ("MANAGED", "gold", 31200),
        "prod_catalog.gold.customer_invoices": ("VIEW", "gold", 125400),
    }

    cards_by_fqn: dict[str, Any] = {}
    for md_path, parsed in _load_cards(ds_dir):
        raw_text = md_path.read_text(encoding="utf-8")
        assert "dapi0123456789abcdef0123456789abcdef" not in raw_text
        assert "billing-ops@corp-example.com" not in raw_text
        card = DatasetCard.model_validate(parsed.frontmatter)
        cards_by_fqn[card.fqn] = card

    assert set(expected_tables).issubset(cards_by_fqn), (
        f"Missing expected DatasetCards: {set(expected_tables) - set(cards_by_fqn)}"
    )
    for fqn, (exp_type, exp_layer, exp_rows) in expected_tables.items():
        c = cards_by_fqn[fqn]
        assert c.table_type == exp_type
        assert c.medallion_layer == exp_layer
        assert c.row_count == exp_rows
        assert c.sample_rows == []

    assert cards_by_fqn["prod_catalog.silver.inventory_snapshots"].query_count_30d == 418

    inv_card = cards_by_fqn["prod_catalog.gold.customer_invoices"]
    cols_by_name = {col.name: col for col in inv_card.columns}
    for pii_col in ("customer_name", "email", "ssn", "phone", "dob", "salary", "address"):
        assert cols_by_name[pii_col].is_pii is True
        assert cols_by_name[pii_col].min_val == "[REDACTED:PII]"
    assert cols_by_name["table_namespace"].is_pii is False

    report = validate_vault(vault_sandbox)
    assert report.valid is True, f"Vault validation failed: {report.errors}"


@pytest.mark.tier1
def test_f06_t1_sync_databricks_tier2_csv_bundle_ingests_tables_columns_lineage_and_profiles(
    vault_sandbox: Path,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """Verify Tier 2 CSV directory bundle joins tables, columns, lineage, and profiles into valid DatasetCards."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.databricks_ingest import sync_databricks
    from poopdeck.models import DatasetCard

    csv_copy = tmp_path / "csv_bundle"
    shutil.copytree(fixtures_dir / "databricks" / "csv_bundle", csv_copy)

    sync_databricks(vault_sandbox, PoopdeckConfig(), input_path=csv_copy, mode="tier2")

    cards_by_fqn: dict[str, Any] = {}
    for md_path, parsed in _load_cards(vault_sandbox / "datasets"):
        assert "dapi0123456789abcdef0123456789abcdef" not in md_path.read_text(encoding="utf-8")
        card = DatasetCard.model_validate(parsed.frontmatter)
        cards_by_fqn[card.fqn] = card

    assert {
        "prod_catalog.silver.inventory_snapshots",
        "prod_catalog.gold.po_receipts",
        "prod_catalog.gold.customer_invoices",
    }.issubset(cards_by_fqn)

    snap = cards_by_fqn["prod_catalog.silver.inventory_snapshots"]
    assert snap.row_count == 5183000
    assert snap.freshness_lag_hours == 2.0
    assert snap.query_count_30d == 418
    assert any("wf_daily_supply_chain_medallion" in job for job in snap.scheduled_jobs)
    snap_down = {_strip_wikilink(r) for r in snap.downstream_refs}
    assert "prod_catalog.gold.po_receipts" in snap_down
    assert "prod_catalog.gold.customer_invoices" in snap_down

    po = cards_by_fqn["prod_catalog.gold.po_receipts"]
    po_up = {_strip_wikilink(r) for r in po.upstream_refs}
    assert "prod_catalog.silver.inventory_snapshots" in po_up
    assert len(po.columns) == 5


@pytest.mark.tier1
def test_f06_t1_sync_databricks_tier1_mocked_uc_populates_lineage_workflows_and_stats(
    vault_sandbox: Path,
) -> None:
    """Verify Tier 1 mocked Unity Catalog crawler queries information_schema & table_lineage and writes linked cards."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.databricks_ingest import sync_databricks
    from poopdeck.models import DatasetCard
    from poopdeck.vault_io import HUMAN_NOTES_END, HUMAN_NOTES_START

    executed_sql: list[str] = []

    def mock_sql_runner(sql: str, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        executed_sql.append(sql)
        q = sql.lower()
        if "information_schema.tables" in q:
            return [
                {
                    "table_catalog": "prod_catalog",
                    "catalog_name": "prod_catalog",
                    "table_schema": "silver",
                    "schema_name": "silver",
                    "table_name": "inventory_snapshots",
                    "table_type": "MANAGED",
                    "comment": "Silver inventory balances; token dapi0123456789abcdef0123456789abcdef",
                },
                {
                    "table_catalog": "prod_catalog",
                    "catalog_name": "prod_catalog",
                    "table_schema": "gold",
                    "schema_name": "gold",
                    "table_name": "po_receipts",
                    "table_type": "MANAGED",
                    "comment": "Gold PO dock receipts",
                },
            ]
        if "information_schema.columns" in q:
            return [
                {
                    "table_catalog": "prod_catalog",
                    "table_schema": "silver",
                    "table_name": "inventory_snapshots",
                    "column_name": "sku_id",
                    "name": "sku_id",
                    "data_type": "STRING",
                    "full_data_type": "STRING",
                    "ordinal_position": 1,
                    "is_nullable": "NO",
                    "nullable": False,
                    "comment": "SKU key",
                },
                {
                    "table_catalog": "prod_catalog",
                    "table_schema": "silver",
                    "table_name": "inventory_snapshots",
                    "column_name": "snapshot_ts",
                    "name": "snapshot_ts",
                    "data_type": "TIMESTAMP",
                    "full_data_type": "TIMESTAMP",
                    "ordinal_position": 2,
                    "is_nullable": "NO",
                    "nullable": False,
                    "comment": "Snapshot timestamp",
                },
                {
                    "table_catalog": "prod_catalog",
                    "table_schema": "gold",
                    "table_name": "po_receipts",
                    "column_name": "po_id",
                    "name": "po_id",
                    "data_type": "STRING",
                    "full_data_type": "STRING",
                    "ordinal_position": 1,
                    "is_nullable": "NO",
                    "nullable": False,
                    "comment": "PO key",
                },
            ]
        if "table_tags" in q:
            return [
                {
                    "catalog_name": "prod_catalog",
                    "table_catalog": "prod_catalog",
                    "schema_name": "silver",
                    "table_schema": "silver",
                    "table_name": "inventory_snapshots",
                    "tag_name": "domain",
                    "tag_value": "ops_supply_chain",
                }
            ]
        if "table_lineage" in q:
            return [
                {
                    "source_table_full_name": "prod_catalog.silver.inventory_snapshots",
                    "source_table_catalog": "prod_catalog",
                    "source_table_schema": "silver",
                    "source_table_name": "inventory_snapshots",
                    "target_table_full_name": "prod_catalog.gold.po_receipts",
                    "target_table_catalog": "prod_catalog",
                    "target_table_schema": "gold",
                    "target_table_name": "po_receipts",
                    "entity_type": "JOB",
                }
            ]
        if "query.history" in q or "access.audit" in q or "statement_text" in q:
            return [
                {
                    "fqn": "prod_catalog.silver.inventory_snapshots",
                    "table_full_name": "prod_catalog.silver.inventory_snapshots",
                    "query_count_30d": 418,
                    "query_count": 418,
                    "cnt": 418,
                },
                {
                    "fqn": "prod_catalog.gold.po_receipts",
                    "table_full_name": "prod_catalog.gold.po_receipts",
                    "query_count_30d": 312,
                    "query_count": 312,
                    "cnt": 312,
                },
            ]
        if "count(" in q:
            row_cnt = 5183000 if "inventory_snapshots" in q else 31200
            row_data: dict[str, Any] = {
                "row_count": row_cnt,
                "cnt": row_cnt,
                "total_rows": row_cnt,
                "min_timestamp": "2025-11-12T06:00:00Z",
                "min_ts": "2025-11-12T06:00:00Z",
                "max_timestamp": "2026-11-11T06:00:00Z",
                "max_ts": "2026-11-11T06:00:00Z",
                "freshness_lag_hours": 2.0,
            }
            for alias in re.findall(r"\bas\s+([A-Za-z_][A-Za-z0-9_]*)", sql, flags=re.IGNORECASE):
                al = alias.lower()
                if al not in row_data:
                    if "null" in al:
                        row_data[alias] = 0
                    elif "distinct" in al or "count" in al or "cnt" in al:
                        row_data[alias] = 100
                    elif "min" in al:
                        row_data[alias] = "2025-11-12T06:00:00Z"
                    elif "max" in al:
                        row_data[alias] = "2026-11-11T06:00:00Z"
                    else:
                        row_data[alias] = 0
            return [row_data]
        return []

    mock_job = SimpleNamespace(
        job_id=90101,
        name="wf_daily_supply_chain_medallion",
        settings=SimpleNamespace(
            name="wf_daily_supply_chain_medallion",
            schedule=SimpleNamespace(quartz_cron_expression="0 0 6 * * ?"),
            tasks=[
                SimpleNamespace(
                    task_key="build_po_receipts",
                    notebook_task=SimpleNamespace(
                        notebook_path="/Repos/de/prod_catalog.gold.po_receipts"
                    ),
                    sql_task=SimpleNamespace(
                        query=SimpleNamespace(
                            query="INSERT INTO prod_catalog.gold.po_receipts SELECT * FROM prod_catalog.silver.inventory_snapshots"
                        )
                    ),
                )
            ],
        ),
    )
    mock_client = SimpleNamespace(jobs=SimpleNamespace(list=lambda **kw: [mock_job]))

    cfg = PoopdeckConfig()
    cfg.databricks.catalogs_include = ["prod_catalog"]
    sync_databricks(
        vault_sandbox,
        cfg,
        mode="tier1",
        client=mock_client,
        sql_runner=mock_sql_runner,
    )

    joined_sql = "\n".join(executed_sql).lower()
    assert "information_schema" in joined_sql
    assert "table_lineage" in joined_sql

    cards_by_fqn: dict[str, Any] = {}
    for md_path, parsed in _load_cards(vault_sandbox / "datasets"):
        raw_md = md_path.read_text(encoding="utf-8")
        assert HUMAN_NOTES_START in raw_md and HUMAN_NOTES_END in raw_md
        assert "dapi0123456789abcdef0123456789abcdef" not in raw_md
        card = DatasetCard.model_validate(parsed.frontmatter)
        cards_by_fqn[card.fqn] = card

    assert "prod_catalog.silver.inventory_snapshots" in cards_by_fqn
    assert "prod_catalog.gold.po_receipts" in cards_by_fqn

    snap_down = {_strip_wikilink(r) for r in cards_by_fqn["prod_catalog.silver.inventory_snapshots"].downstream_refs}
    po_up = {_strip_wikilink(r) for r in cards_by_fqn["prod_catalog.gold.po_receipts"].upstream_refs}
    assert "prod_catalog.gold.po_receipts" in snap_down
    assert "prod_catalog.silver.inventory_snapshots" in po_up


@pytest.mark.tier1
def test_f06_t1_sync_databricks_resync_updates_stats_and_preserves_human_notes_verbatim(
    vault_sandbox: Path,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """Verify re-syncing updated Databricks stats refreshes frontmatter while preserving HUMAN-NOTES byte-for-byte."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.databricks_ingest import sync_databricks
    from poopdeck.models import DatasetCard
    from poopdeck.vault_io import parse_markdown_card

    bundle_path = tmp_path / "bundle.json"
    shutil.copy2(fixtures_dir / "databricks" / "discovery_bundle.json", bundle_path)

    sync_databricks(vault_sandbox, PoopdeckConfig(), input_path=bundle_path, mode="tier2")

    target_card_path = vault_sandbox / "datasets" / "prod_catalog.silver.inventory_snapshots.md"
    assert target_card_path.is_file()

    custom_notes = (
        "\n### Warehouse Steward Notes\n"
        "- Verified grain with DE team for [[OPP-004]]\n"
        "- Local notebook backup: `C:\\Users\\ds\\notes\\inv_audit.sql`\n"
        "| Check | Status |\n|---|---|\n| Dedup | Passed |\n"
    )
    _inject_human_notes(target_card_path, custom_notes)

    bundle_obj = json.loads(bundle_path.read_text(encoding="utf-8"))
    for prof in bundle_obj["profiles"]:
        if prof["fqn"] == "prod_catalog.silver.inventory_snapshots":
            prof["row_count"] = 5299100
            prof["freshness_lag_hours"] = 0.75
    for tbl in bundle_obj["tables"]:
        if tbl["fqn"] == "prod_catalog.silver.inventory_snapshots":
            tbl["row_count"] = 5299100
            tbl["freshness_lag_hours"] = 0.75
    bundle_path.write_text(json.dumps(bundle_obj, indent=2), encoding="utf-8", newline="\n")

    sync_databricks(vault_sandbox, PoopdeckConfig(), input_path=bundle_path, mode="tier2")

    parsed = parse_markdown_card(target_card_path)
    updated_card = DatasetCard.model_validate(parsed.frontmatter)
    assert updated_card.row_count == 5299100
    assert updated_card.freshness_lag_hours == 0.75
    assert parsed.human_notes == custom_notes


@pytest.mark.tier2
def test_f06_t2_sync_databricks_tier1_hive_metastore_fallback_on_uc_permission_denied(
    vault_sandbox: Path,
    fixtures_dir: Path,
) -> None:
    """Verify Tier 1 falls back cleanly to Hive SHOW/DESCRIBE when system.information_schema raises PermissionError."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.databricks_ingest import sync_databricks
    from poopdeck.models import DatasetCard

    hive_data = json.loads(
        (fixtures_dir / "databricks" / "hive_mock_payloads.json").read_text(encoding="utf-8")
    )

    def hive_sql_runner(sql: str, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        q = sql.strip().lower()
        if "system." in q or "information_schema" in q:
            raise PermissionError(hive_data["uc_error"]["message"])
        if q.startswith("show databases") or q.startswith("show schemas"):
            return list(hive_data["show_databases"])
        if q.startswith("show tables"):
            if "legacy_hive" in q:
                return list(hive_data["show_tables_by_db"]["legacy_hive"])
            return list(hive_data["show_tables_by_db"]["default"])
        if q.startswith("describe"):
            return list(hive_data["describe_extended"]["legacy_hive.manual_promo_overrides"])
        if "count(" in q:
            agg = dict(hive_data["safe_aggregates"]["legacy_hive.manual_promo_overrides"])
            agg["cnt"] = agg["row_count"]
            return [agg]
        return []

    cfg = PoopdeckConfig()
    cfg.databricks.catalogs_include = ["*"]
    sync_databricks(vault_sandbox, cfg, mode="tier1", sql_runner=hive_sql_runner)

    matches = list((vault_sandbox / "datasets").glob("*manual_promo_overrides.md"))
    assert len(matches) == 1, f"Expected 1 Hive DatasetCard, found: {matches}"

    raw_md = matches[0].read_text(encoding="utf-8")
    assert "dapi0123456789abcdef0123456789abcdef" not in raw_md
    assert "sarah.chen@corp-example.com" not in raw_md

    _, parsed = _load_cards(vault_sandbox / "datasets")[0]
    card = DatasetCard.model_validate(parsed.frontmatter)
    assert card.table_name == "manual_promo_overrides"
    assert card.table_type == "EXTERNAL"

    col_names = [c.name for c in card.columns]
    assert col_names == [
        "override_id",
        "sku_id",
        "buyer_email",
        "promo_lift_pct",
        "effective_date",
    ]
    cols_by_name = {c.name: c for c in card.columns}
    assert cols_by_name["buyer_email"].is_pii is True


@pytest.mark.tier2
def test_f06_t2_sync_databricks_csv_bundle_utf8_bom_strips_feff_from_headers_and_fields(
    vault_sandbox: Path,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """Verify UTF-8 BOM (\\ufeff) CSV & JSON bundles are ingested cleanly via utf-8-sig with zero BOM residue."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.databricks_ingest import sync_databricks
    from poopdeck.models import DatasetCard

    tables_csv = fixtures_dir / "databricks" / "csv_bundle" / "tables.csv"
    cols_csv = fixtures_dir / "databricks" / "csv_bundle" / "columns.csv"
    assert tables_csv.read_bytes().startswith(b"\xef\xbb\xbf")
    assert cols_csv.read_bytes().startswith(b"\xef\xbb\xbf")

    # 1. Fixture csv_bundle verification
    csv_copy = tmp_path / "csv_bom_bundle"
    shutil.copytree(fixtures_dir / "databricks" / "csv_bundle", csv_copy)

    sync_databricks(vault_sandbox, PoopdeckConfig(), input_path=csv_copy, mode="tier2")

    cards_by_fqn: dict[str, Any] = {}
    for md_path, parsed in _load_cards(vault_sandbox / "datasets"):
        raw_text = md_path.read_text(encoding="utf-8")
        assert "\ufeff" not in raw_text
        card = DatasetCard.model_validate(parsed.frontmatter)
        assert not card.fqn.startswith("\ufeff")
        cards_by_fqn[card.fqn] = card

    inv = cards_by_fqn["prod_catalog.gold.customer_invoices"]
    assert len(inv.columns) == 15
    billing_notes_col = next(c for c in inv.columns if c.name == "billing_notes")
    assert billing_notes_col.null_pct is None

    # 2. Strict BOM-first CSV headers with zero un-prefixed fqn/full_name or column_name fallbacks
    csv_bom_no_fallback = tmp_path / "csv_bom_no_fallback"
    csv_bom_no_fallback.mkdir(parents=True, exist_ok=True)
    (csv_bom_no_fallback / "tables.csv").write_bytes(
        b"\xef\xbb\xbftable_catalog,table_schema,table_name,table_type,comment\n"
        b"prod_catalog,gold,bom_only_orders,MANAGED,Clean BOM test\n"
    )
    (csv_bom_no_fallback / "columns.csv").write_bytes(
        b"\xef\xbb\xbfname,table_catalog,table_schema,table_name,data_type\n"
        b"order_id,prod_catalog,gold,bom_only_orders,STRING\n"
    )
    vault_bom_only = tmp_path / "vault_bom_only"
    vault_bom_only.mkdir(parents=True, exist_ok=True)
    summary_bom_only = sync_databricks(
        vault_bom_only, PoopdeckConfig(), input_path=csv_bom_no_fallback, mode="tier2"
    )
    assert _summary_to_dict(summary_bom_only)["_errors"] == []

    bom_only_by_fqn: dict[str, Any] = {}
    for md_path, parsed in _load_cards(vault_bom_only / "datasets"):
        raw_text = md_path.read_text(encoding="utf-8")
        assert "\ufeff" not in raw_text
        card = DatasetCard.model_validate(parsed.frontmatter)
        assert not card.fqn.startswith("\ufeff")
        bom_only_by_fqn[card.fqn] = card

    assert "prod_catalog.gold.bom_only_orders" in bom_only_by_fqn
    bom_card = bom_only_by_fqn["prod_catalog.gold.bom_only_orders"]
    assert len(bom_card.columns) == 1
    assert bom_card.columns[0].name == "order_id"
    assert not bom_card.columns[0].name.startswith("\ufeff")

    # 3. UTF-8 BOM-prefixed JSON discovery bundle (raises JSONDecodeError if read with encoding="utf-8")
    bom_bundle = tmp_path / "bom_bundle.json"
    bom_bundle.write_bytes(
        b"\xef\xbb\xbf" + (fixtures_dir / "databricks" / "discovery_bundle.json").read_bytes()
    )
    vault_bom_json = tmp_path / "vault_bom_json"
    vault_bom_json.mkdir(parents=True, exist_ok=True)
    summary_bom_json = sync_databricks(
        vault_bom_json, PoopdeckConfig(), input_path=bom_bundle, mode="tier2"
    )
    assert _summary_to_dict(summary_bom_json)["_errors"] == []

    bom_json_by_fqn: dict[str, Any] = {}
    for md_path, parsed in _load_cards(vault_bom_json / "datasets"):
        raw_text = md_path.read_text(encoding="utf-8")
        assert "\ufeff" not in raw_text
        assert "dapi0123456789abcdef0123456789abcdef" not in raw_text
        card = DatasetCard.model_validate(parsed.frontmatter)
        assert not card.fqn.startswith("\ufeff")
        bom_json_by_fqn[card.fqn] = card

    assert {
        "prod_catalog.bronze.raw_erp_inventory_feed",
        "prod_catalog.silver.inventory_snapshots",
        "prod_catalog.silver.shipment_events",
        "prod_catalog.gold.po_receipts",
        "prod_catalog.gold.customer_invoices",
    }.issubset(bom_json_by_fqn)


@pytest.mark.tier2
def test_f06_t2_sync_databricks_malformed_bundle_fails_cleanly_without_corrupt_cards(
    vault_sandbox: Path,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """Verify truncated malformed_bundle.json either raises ValueError/JSONDecodeError or records SyncSummary errors with 0 cards written."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.databricks_ingest import sync_databricks

    bad_path = tmp_path / "malformed_bundle.json"
    shutil.copy2(fixtures_dir / "databricks" / "malformed_bundle.json", bad_path)

    raised_clean_error = False
    summary_errors: list[Any] = []
    try:
        summary = sync_databricks(vault_sandbox, PoopdeckConfig(), input_path=bad_path, mode="tier2")
        summary_errors = _summary_to_dict(summary)["_errors"]
    except (ValueError, json.JSONDecodeError):
        raised_clean_error = True

    assert raised_clean_error or len(summary_errors) >= 1, (
        "Expected malformed_bundle.json to raise ValueError/JSONDecodeError or record SyncSummary errors"
    )
    assert list((vault_sandbox / "datasets").glob("*.md")) == []


@pytest.mark.tier2
def test_f06_t2_sync_databricks_catalogs_include_and_exclude_filtering(
    vault_sandbox: Path,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """Verify catalogs_include and catalogs_exclude filter out non-permitted catalogs during sync_databricks."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.databricks_ingest import sync_databricks
    from poopdeck.models import DatasetCard

    bundle_obj = json.loads(
        (fixtures_dir / "databricks" / "discovery_bundle.json").read_text(encoding="utf-8")
    )
    bundle_obj["tables"].extend(
        [
            {
                "fqn": "samples.tpch.orders",
                "catalog": "samples",
                "table_catalog": "samples",
                "schema_name": "tpch",
                "table_schema": "tpch",
                "table_name": "orders",
                "table_type": "MANAGED",
                "medallion_layer": "bronze",
                "grain": "1 row per sample order",
                "join_keys": ["o_orderkey"],
                "comment": "Databricks built-in sample table",
            },
            {
                "fqn": "sandbox_catalog.dev.scratch_tbl",
                "catalog": "sandbox_catalog",
                "table_catalog": "sandbox_catalog",
                "schema_name": "dev",
                "table_schema": "dev",
                "table_name": "scratch_tbl",
                "table_type": "MANAGED",
                "medallion_layer": "unknown",
                "grain": "ad-hoc scratch",
                "join_keys": [],
                "comment": "Personal scratch table",
            },
        ]
    )
    multi_cat_path = tmp_path / "multi_cat_bundle.json"
    multi_cat_path.write_text(json.dumps(bundle_obj, indent=2), encoding="utf-8", newline="\n")

    cfg = PoopdeckConfig()
    cfg.databricks.catalogs_include = ["prod_catalog"]
    cfg.databricks.catalogs_exclude = ["samples", "sandbox_catalog"]

    sync_databricks(vault_sandbox, cfg, input_path=multi_cat_path, mode="tier2")

    written_fqns = {
        DatasetCard.model_validate(parsed.frontmatter).fqn
        for _, parsed in _load_cards(vault_sandbox / "datasets")
    }
    assert len(written_fqns) == 5
    assert all(fqn.startswith("prod_catalog.") for fqn in written_fqns)
    assert "samples.tpch.orders" not in written_fqns
    assert "sandbox_catalog.dev.scratch_tbl" not in written_fqns


@pytest.mark.tier2
def test_f06_t2_sync_databricks_allow_sample_rows_caps_and_masks_pii_on_disk(
    vault_sandbox: Path,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """Verify opted-in allow_sample_rows caps rows at max_sample_rows and masks all PII cells on disk."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.databricks_ingest import sync_databricks
    from poopdeck.models import DatasetCard
    from poopdeck.vault_io import parse_markdown_card

    bundle_copy = tmp_path / "discovery_bundle.json"
    shutil.copy2(fixtures_dir / "databricks" / "discovery_bundle.json", bundle_copy)

    cfg = PoopdeckConfig()
    cfg.privacy.privacy_mode = "scrubbed_samples"
    cfg.privacy.allow_sample_rows = True
    cfg.privacy.max_sample_rows = 4

    sync_databricks(vault_sandbox, cfg, input_path=bundle_copy, mode="tier2")

    inv_path = vault_sandbox / "datasets" / "prod_catalog.gold.customer_invoices.md"
    raw_md = inv_path.read_text(encoding="utf-8")
    for leaked in (
        "alice.martin@client-example.com",
        "zoe.patel@client-example.com",
        "ar@corp-example.com",
        "123-45-6789",
        "987-65-4321",
        "867-5309",
        "dapi0123456789abcdef0123456789abcdef",
        "INV-100005",
        "INV-100006",
    ):
        assert leaked not in raw_md, f"Leaked '{leaked}' in {inv_path.name}"

    card = DatasetCard.model_validate(parse_markdown_card(inv_path).frontmatter)
    assert 1 <= len(card.sample_rows) <= 4
    for row in card.sample_rows:
        for pii_field in ("customer_name", "email", "ssn", "phone", "dob", "salary", "address"):
            assert row[pii_field] == "[REDACTED:PII]"
        assert row["table_namespace"] == "billing_core"


@pytest.mark.tier2
def test_f06_t2_sync_databricks_empty_bundle_directory_is_graceful_noop(
    vault_sandbox: Path,
    tmp_path: Path,
) -> None:
    """Verify an empty Tier 2 Databricks bundle directory completes gracefully with zero DatasetCards written."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.databricks_ingest import sync_databricks

    empty_dir = tmp_path / "empty_dbx"
    empty_dir.mkdir(parents=True, exist_ok=True)

    sync_databricks(vault_sandbox, PoopdeckConfig(), input_path=empty_dir, mode="tier2")
    assert list((vault_sandbox / "datasets").glob("*.md")) == []


# ============================================================================
# F07 — Power BI Crawler, DAX INFO.* & Heuristics (11 tests: 5 Tier 1 + 6 Tier 2)
# ============================================================================


@pytest.mark.tier1
def test_f07_t1_dax_info_extract_query_file_contains_all_four_info_functions(
    project_root: Path,
) -> None:
    """Verify queries/powerbi_dax_info_extract.dax exists and includes all four INFO.* DMV queries."""
    dax_path = project_root / "queries" / "powerbi_dax_info_extract.dax"
    assert dax_path.is_file(), f"Missing DAX INFO.* query script at {dax_path}"

    content = dax_path.read_text(encoding="utf-8-sig")
    upper = content.upper()
    assert "EVALUATE" in upper
    for fn_name in (
        "INFO.TABLES()",
        "INFO.COLUMNS()",
        "INFO.MEASURES()",
        "INFO.RELATIONSHIPS()",
    ):
        assert fn_name in upper, f"Missing required DAX function {fn_name} in {dax_path.name}"


@pytest.mark.tier1
def test_f07_t1_detect_dax_heuristics_flags_all_four_heuristic_classes() -> None:
    """Verify detect_dax_heuristics flags nested_heuristic_tiers, naive_moving_avg_forecast, static_threshold_alert & backward_looking_only."""
    from poopdeck.crawlers.powerbi_ingest import detect_dax_heuristics

    nested_expr = (
        "SWITCH(TRUE(), [Days of Supply] < 7, "
        'IF(SUM(\'Inventory\'[on_hand_qty]) <= 0, "P0 Stockout", '
        'IF(AVERAGE(\'Inventory\'[actual_lead_time_days]) > 21, "P1 Critical", "P2 Expedite")), '
        '[Days of Supply] < 14, "P2 Watchlist", "Healthy")'
    )
    assert "nested_heuristic_tiers" in detect_dax_heuristics("SKU Stockout Risk Tier", nested_expr)

    moving_avg_expr = (
        "AVERAGEX(DATESINPERIOD('Sales'[invoice_date], MAX('Sales'[invoice_date]), -3, MONTH), "
        "CALCULATE(SUM('Sales'[order_qty])))"
    )
    assert "naive_moving_avg_forecast" in detect_dax_heuristics(
        "Naive 3M Moving Avg Demand Forecast",
        moving_avg_expr,
    )

    static_alert_expr = (
        'IF([Days of Supply] < 14 || [Fill Rate Pct] < 0.85, '
        '"ALERT: Reorder Immediately - At Risk", "OK")'
    )
    assert "static_threshold_alert" in detect_dax_heuristics(
        "Static 14-Day Reorder Alert",
        static_alert_expr,
    )

    backward_expr = (
        "CALCULATE(SUM('Sales'[net_amount_usd]), SAMEPERIODLASTYEAR('Sales'[invoice_date]))"
    )
    assert "backward_looking_only" in detect_dax_heuristics(
        "Prior Year Net Revenue",
        backward_expr,
    )


@pytest.mark.tier1
def test_f07_t1_sync_powerbi_tier2_sample_pbip_creates_report_card_with_lineage_and_gaps(
    vault_sandbox: Path,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """Verify Tier 2 .pbip project ingestion links Databricks upstream tables and populates all 4 DAX heuristic flags."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.powerbi_ingest import sync_powerbi
    from poopdeck.models import ReportCard

    pbip_copy = tmp_path / "sample_pbip"
    shutil.copytree(fixtures_dir / "powerbi" / "sample_pbip", pbip_copy)

    sync_powerbi(vault_sandbox, PoopdeckConfig(), input_path=pbip_copy, mode="tier2")

    cards = [
        ReportCard.model_validate(parsed.frontmatter)
        for _, parsed in _load_cards(vault_sandbox / "reports")
    ]
    assert len(cards) >= 1

    target = next((c for c in cards if "Daily Buyer Workbench" in c.title), cards[0])
    assert bool(target.title.strip())
    assert bool(target.workspace.strip())

    upstream = {_strip_wikilink(ref) for ref in target.upstream_datasets}
    assert {
        "prod_catalog.gold.customer_invoices",
        "prod_catalog.silver.inventory_snapshots",
        "prod_catalog.gold.po_receipts",
    }.issubset(upstream)

    all_flags = {flag for m in target.measures for flag in m.flagged_heuristics}
    assert {
        "nested_heuristic_tiers",
        "naive_moving_avg_forecast",
        "static_threshold_alert",
        "backward_looking_only",
    }.issubset(all_flags)
    assert len(target.bi_to_ds_gaps) >= 1
    assert len(target.relationships) >= 1


@pytest.mark.tier1
def test_f07_t1_sync_powerbi_tier2_dax_info_csv_and_json_exports_create_report_cards(
    vault_sandbox: Path,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """Verify Tier 2 DAX INFO.* CSV directory and JSON bundle exports both create valid ReportCards."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.powerbi_ingest import sync_powerbi
    from poopdeck.models import ReportCard

    # Part A: CSV-only INFO.* exports directory
    csv_only_dir = tmp_path / "csv_only"
    csv_only_dir.mkdir(parents=True, exist_ok=True)
    src_dax_dir = fixtures_dir / "powerbi" / "dax_info_exports"
    for csv_name in (
        "info_tables.csv",
        "info_columns.csv",
        "info_measures.csv",
        "info_relationships.csv",
    ):
        shutil.copy2(src_dax_dir / csv_name, csv_only_dir / csv_name)

    sync_powerbi(vault_sandbox, PoopdeckConfig(), input_path=csv_only_dir, mode="tier2")

    csv_cards = [
        ReportCard.model_validate(parsed.frontmatter)
        for _, parsed in _load_cards(vault_sandbox / "reports")
    ]
    assert len(csv_cards) == 1
    csv_card = csv_cards[0]
    assert len(csv_card.measures) == 6
    assert len(csv_card.relationships) >= 1
    csv_up = {_strip_wikilink(r) for r in csv_card.upstream_datasets}
    assert "prod_catalog.gold.customer_invoices" in csv_up
    assert "prod_catalog.silver.inventory_snapshots" in csv_up
    assert len(csv_card.bi_to_ds_gaps) >= 1

    # Part B: JSON INFO.* export file
    json_vault = tmp_path / "vault_json"
    json_vault.mkdir(parents=True, exist_ok=True)
    json_copy = tmp_path / "info_export.json"
    shutil.copy2(src_dax_dir / "info_export.json", json_copy)

    sync_powerbi(json_vault, PoopdeckConfig(), input_path=json_copy, mode="tier2")

    json_cards = [
        ReportCard.model_validate(parsed.frontmatter)
        for _, parsed in _load_cards(json_vault / "reports")
    ]
    assert len(json_cards) == 1
    jcard = json_cards[0]
    assert bool(jcard.title.strip())
    assert bool(jcard.workspace.strip())
    assert len(jcard.measures) == 6
    assert len(jcard.relationships) >= 1
    json_up = {_strip_wikilink(r) for r in jcard.upstream_datasets}
    assert "prod_catalog.gold.customer_invoices" in json_up
    assert "prod_catalog.silver.inventory_snapshots" in json_up
    assert len(jcard.bi_to_ds_gaps) >= 1


class _MockPBIResponse:
    """Minimal offline HTTP response stub for Power BI / Fabric REST client tests."""

    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        return copy.deepcopy(self._payload)


class _MockPowerBIRestClient:
    """Offline dual-protocol mock supporting Power BI /groups, Fabric /workspaces, and adapter calls."""

    def __init__(self, workspaces: list[dict[str, Any]] | None = None) -> None:
        specs = workspaces or [
            {
                "id": "ws-comm-01",
                "name": "Commercial Workspace",
                "report_id": "rpt-churn-01",
                "report_name": "Executive Margin & Churn Pulse",
                "dataset_id": "ds-churn-01",
                "configured_by": "David Park",
                "upstream_table": "prod_catalog.gold.customer_invoices",
                "failed_days_ago": [3, 7],
            }
        ]
        self._specs_by_ws: dict[str, dict[str, Any]] = {s["id"]: dict(s) for s in specs}
        self._specs_by_ds: dict[str, dict[str, Any]] = {s["dataset_id"]: dict(s) for s in specs}
        self._default_ws_id: str = specs[0]["id"]

    def __enter__(self) -> _MockPowerBIRestClient:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def close(self) -> None:
        return None

    def list_workspaces(self) -> list[dict[str, Any]]:
        return [
            {
                "id": s["id"],
                "name": s["name"],
                "displayName": s["name"],
            }
            for s in self._specs_by_ws.values()
        ]

    get_workspaces = list_workspaces

    def list_reports(self, workspace_id: str) -> list[dict[str, Any]]:
        s = self._specs_by_ws.get(workspace_id)
        if s is None:
            return []
        return [
            {
                "id": s["report_id"],
                "name": s["report_name"],
                "displayName": s["report_name"],
                "datasetId": s["dataset_id"],
                "semanticModelId": s["dataset_id"],
            }
        ]

    get_reports = list_reports

    def list_datasets(self, workspace_id: str) -> list[dict[str, Any]]:
        s = self._specs_by_ws.get(workspace_id)
        if s is None:
            return []
        tbl = s["upstream_table"]
        return [
            {
                "id": s["dataset_id"],
                "name": s["report_name"],
                "displayName": s["report_name"],
                "configuredBy": s["configured_by"],
                "upstreamTables": [tbl],
                "expression": f"SELECT * FROM {tbl}",
            }
        ]

    get_datasets = list_datasets
    list_semantic_models = list_datasets
    get_semantic_models = list_datasets

    def get_refresh_history(self, workspace_id: str, dataset_id: str) -> list[dict[str, Any]]:
        from datetime import datetime, timedelta, timezone

        s = self._specs_by_ds.get(dataset_id) or self._specs_by_ws.get(workspace_id)
        if s is None:
            return []
        now_utc = datetime.now(timezone.utc)
        ok_ts = (now_utc - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        history: list[dict[str, Any]] = [
            {"status": "Completed", "startTime": ok_ts, "endTime": ok_ts}
        ]
        for days_ago in s.get("failed_days_ago", [3, 7]):
            fail_ts = (now_utc - timedelta(days=int(days_ago))).strftime("%Y-%m-%dT%H:%M:%SZ")
            history.append({"status": "Failed", "startTime": fail_ts, "endTime": fail_ts})
        return history

    list_refreshes = get_refresh_history

    def get_refresh_schedule(self, workspace_id: str, dataset_id: str) -> dict[str, Any]:
        return {"enabled": True, "days": ["Daily"], "times": ["06:00"]}

    def get_datasources(self, workspace_id: str, dataset_id: str) -> list[dict[str, Any]]:
        s = self._specs_by_ds.get(dataset_id) or self._specs_by_ws.get(workspace_id)
        if s is None:
            return []
        tbl = s["upstream_table"]
        catalog = tbl.split(".", 1)[0]
        return [
            {
                "datasourceType": "Extension",
                "connectionDetails": {
                    "path": "adb-12345.azuredatabricks.net",
                    "database": catalog,
                    "query": f"SELECT * FROM {tbl}",
                },
            }
        ]

    def get(self, url: str, *args: Any, **kwargs: Any) -> _MockPBIResponse:
        path_part = url.split("?", 1)[0].rstrip("/")
        u_low = path_part.lower()
        if u_low.endswith("/groups") or u_low.endswith("/workspaces"):
            return _MockPBIResponse({"value": self.list_workspaces()})

        ws_match = re.search(r"/(?:groups|workspaces)/([^/?]+)", path_part, flags=re.IGNORECASE)
        ws_id = ws_match.group(1) if ws_match else self._default_ws_id
        default_ds = self._specs_by_ws.get(ws_id, {}).get("dataset_id", "")
        ds_match = re.search(
            r"/(?:datasets|semanticmodels)/([^/?]+)",
            path_part,
            flags=re.IGNORECASE,
        )
        ds_id = ds_match.group(1) if ds_match else default_ds

        if u_low.endswith("/reports"):
            return _MockPBIResponse({"value": self.list_reports(ws_id)})
        if u_low.endswith("/refreshes"):
            return _MockPBIResponse({"value": self.get_refresh_history(ws_id, ds_id)})
        if u_low.endswith("/refreshschedule"):
            return _MockPBIResponse(self.get_refresh_schedule(ws_id, ds_id))
        if u_low.endswith("/datasources"):
            return _MockPBIResponse({"value": self.get_datasources(ws_id, ds_id)})
        if u_low.endswith("/datasets") or u_low.endswith("/semanticmodels"):
            return _MockPBIResponse({"value": self.list_datasets(ws_id)})
        if u_low.endswith("/items"):
            q_str = (url.split("?", 1)[1] if "?" in url else "").lower()
            params = kwargs.get("params") or {}
            combined = f"{q_str} {params.get('type', '')}".lower()
            if "report" in combined:
                return _MockPBIResponse({"value": self.list_reports(ws_id)})
            if "semanticmodel" in combined or "dataset" in combined:
                return _MockPBIResponse({"value": self.list_datasets(ws_id)})
            items = [{**r, "type": "Report"} for r in self.list_reports(ws_id)] + [
                {**d, "type": "SemanticModel"} for d in self.list_datasets(ws_id)
            ]
            return _MockPBIResponse({"value": items})
        return _MockPBIResponse({"value": []})

    def post(self, url: str, *args: Any, **kwargs: Any) -> _MockPBIResponse:
        return _MockPBIResponse(
            {
                "results": [
                    {
                        "tables": [
                            {
                                "rows": [
                                    {
                                        "[Name]": "Prior Year Net Revenue",
                                        "[TableName]": "Sales",
                                        "[Expression]": "CALCULATE(SUM('Sales'[net_amount_usd]), SAMEPERIODLASTYEAR('Sales'[invoice_date]))",
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        )

    def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> _MockPBIResponse:
        if method.upper() == "POST":
            return self.post(url, *args, **kwargs)
        return self.get(url, *args, **kwargs)


@pytest.mark.tier1
def test_f07_t1_sync_powerbi_tier1_mocked_rest_client_scans_workspaces_and_refreshes(
    vault_sandbox: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Tier 1 mocked Power BI REST client scans workspaces, reports, refreshes & upstream SQL expressions."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.powerbi_ingest import sync_powerbi
    from poopdeck.models import ReportCard

    monkeypatch.setenv("POWERBI_ACCESS_TOKEN", "mock-offline-token")

    sync_powerbi(
        vault_sandbox,
        PoopdeckConfig(),
        mode="tier1",
        rest_client=_MockPowerBIRestClient(),
    )

    cards = [
        ReportCard.model_validate(parsed.frontmatter)
        for _, parsed in _load_cards(vault_sandbox / "reports")
    ]
    assert len(cards) >= 1
    card = next((c for c in cards if "Executive Margin & Churn Pulse" in c.title), cards[0])
    assert card.title == "Executive Margin & Churn Pulse"
    assert card.workspace == "Commercial Workspace"
    assert card.refresh_failures_30d == 2
    upstream = {_strip_wikilink(r) for r in card.upstream_datasets}
    assert "prod_catalog.gold.customer_invoices" in upstream


@pytest.mark.tier2
def test_f07_t2_detect_dax_heuristics_clean_aggregations_return_no_false_positives() -> None:
    """Verify simple SUM, DISTINCTCOUNT, DIVIDE, and blank DAX measures return zero heuristic flags."""
    from poopdeck.crawlers.powerbi_ingest import detect_dax_heuristics

    assert detect_dax_heuristics("Net Revenue", "SUM('Sales'[net_amount_usd])") == []
    assert detect_dax_heuristics("Distinct Orders", "DISTINCTCOUNT('Sales'[invoice_id])") == []
    assert (
        detect_dax_heuristics(
            "Days of Supply",
            "DIVIDE(SUM('Inventory'[on_hand_qty]), SUM('Inventory'[trailing_30d_daily_demand]), 0)",
        )
        == []
    )
    assert detect_dax_heuristics("Empty Measure", "") == []


@pytest.mark.tier2
def test_f07_t2_sync_powerbi_utf8_bom_measures_csv_and_secret_scrubbing_in_bim_and_tmdl(
    vault_sandbox: Path,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """Verify UTF-8 BOM across model.bim, TMDL & Name-first info_measures.csv and scrub PAT/JDBC/AccountKey secrets."""
    import csv
    import io

    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.powerbi_ingest import sync_powerbi
    from poopdeck.models import ReportCard

    measures_csv = fixtures_dir / "powerbi" / "dax_info_exports" / "info_measures.csv"
    assert measures_csv.read_bytes().startswith(b"\xef\xbb\xbf")
    account_key_secret = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY0123456789=="

    # 1. BOM-prefixed model.bim in .pbip folder (definition/ removed so model.bim JSON path is isolated)
    pbip_bim = tmp_path / "pbip_bim"
    shutil.copytree(fixtures_dir / "powerbi" / "sample_pbip", pbip_bim)
    shutil.rmtree(pbip_bim / "definition")
    bim_path = pbip_bim / "model.bim"
    bim_obj = json.loads(bim_path.read_text(encoding="utf-8-sig"))
    for tbl in bim_obj["model"]["tables"]:
        for msr in tbl.get("measures", []):
            if msr.get("name") == "Static 14-Day Reorder Alert":
                msr["description"] = (
                    f"{msr.get('description', '')}; "
                    "JDBC jdbc:databricks://adb-1234567890123456.7.azuredatabricks.net:443/default;"
                    f"PWD=SuperSecretPass123!; ADLS AccountKey={account_key_secret}"
                )
    bim_path.write_bytes(b"\xef\xbb\xbf" + json.dumps(bim_obj, indent=2).encode("utf-8"))
    assert bim_path.read_bytes().startswith(b"\xef\xbb\xbf")
    summary_bim = sync_powerbi(vault_sandbox, PoopdeckConfig(), input_path=pbip_bim, mode="tier2")
    assert _summary_to_dict(summary_bim)["_errors"] == []

    # 2. TMDL-only folder with UTF-8 BOM across *.tmdl, non-matching file stems, and measure-level secrets
    tmdl_only = tmp_path / "pbip_tmdl_only"
    shutil.copytree(fixtures_dir / "powerbi" / "sample_pbip", tmdl_only)
    (tmdl_only / "model.bim").unlink()
    tables_dir = tmdl_only / "definition" / "tables"
    sales_orig = tables_dir / "Sales.tmdl"
    sales_tmdl = tables_dir / "fact_sales_src.tmdl"
    sales_tmdl_text = sales_orig.read_text(encoding="utf-8-sig").replace(
        "token=dapi0123456789abcdef0123456789abcdef",
        f"token=dapi0123456789abcdef0123456789abcdef;AccountKey={account_key_secret}",
    )
    assert f"AccountKey={account_key_secret}" in sales_tmdl_text
    sales_orig.unlink()
    sales_tmdl.write_bytes(b"\xef\xbb\xbf" + sales_tmdl_text.encode("utf-8"))
    assert sales_tmdl.read_bytes().startswith(b"\xef\xbb\xbftable Sales\n")

    inv_orig = tables_dir / "Inventory.tmdl"
    inv_tmdl = tables_dir / "dim_inventory_src.tmdl"
    inv_tmdl_text = (
        inv_orig.read_text(encoding="utf-8-sig")
        .replace(
            "/// Buyer alert rule; warehouse PAT dapi0123456789abcdef0123456789abcdef",
            "/// Buyer alert rule; JDBC jdbc:databricks://adb-1234567890123456.7.azuredatabricks.net:443/default;"
            f"PWD=SuperSecretPass123!; warehouse PAT dapi0123456789abcdef0123456789abcdef; ADLS AccountKey={account_key_secret}",
        )
        .replace(
            '"ALERT: Reorder Immediately - At Risk", "OK")',
            '"ALERT: Reorder Immediately - At Risk [webhook PAT dapi0123456789abcdef0123456789abcdef]", "OK") '
            "/* JDBC jdbc:databricks://adb-1234567890123456.7.azuredatabricks.net:443/default;"
            f"PWD=SuperSecretPass123!; PAT dapi0123456789abcdef0123456789abcdef; ADLS AccountKey={account_key_secret} */",
        )
    )
    assert "PWD=SuperSecretPass123!" in inv_tmdl_text
    assert f"AccountKey={account_key_secret}" in inv_tmdl_text
    inv_orig.unlink()
    inv_tmdl.write_bytes(b"\xef\xbb\xbf" + inv_tmdl_text.encode("utf-8"))
    assert inv_tmdl.read_bytes().startswith(b"\xef\xbb\xbftable Inventory\n")

    for aux_tmdl in (
        tmdl_only / "definition" / "relationships.tmdl",
        tmdl_only / "definition" / "model.tmdl",
    ):
        aux_tmdl.write_bytes(
            b"\xef\xbb\xbf" + aux_tmdl.read_text(encoding="utf-8-sig").encode("utf-8")
        )
        assert aux_tmdl.read_bytes().startswith(b"\xef\xbb\xbf")

    vault_tmdl = tmp_path / "vault_tmdl"
    vault_tmdl.mkdir(parents=True, exist_ok=True)
    summary_tmdl = sync_powerbi(vault_tmdl, PoopdeckConfig(), input_path=tmdl_only, mode="tier2")
    assert _summary_to_dict(summary_tmdl)["_errors"] == []

    # 3. BOM CSV exports folder rewritten with Name at column 1 immediately after \xef\xbb\xbf (ID omitted)
    dax_csv_copy = tmp_path / "dax_csv_bom"
    dax_csv_copy.mkdir(parents=True, exist_ok=True)
    for csv_file in (fixtures_dir / "powerbi" / "dax_info_exports").glob("*.csv"):
        if csv_file.name != "info_measures.csv":
            shutil.copy2(csv_file, dax_csv_copy / csv_file.name)

    with measures_csv.open("r", encoding="utf-8-sig", newline="") as f_in:
        src_rows = list(csv.DictReader(f_in))
    assert len(src_rows) == 6
    expected_measure_names = {row["Name"] for row in src_rows}

    csv_buf = io.StringIO()
    writer = csv.DictWriter(
        csv_buf,
        fieldnames=["Name", "TableName", "Expression", "Description"],
        lineterminator="\n",
    )
    writer.writeheader()
    for row in src_rows:
        desc = row["Description"]
        if row["Name"] == "Static 14-Day Reorder Alert":
            desc = (
                f"{desc}; PAT dapi0123456789abcdef0123456789abcdef; "
                f"ADLS AccountKey={account_key_secret}"
            )
        writer.writerow(
            {
                "Name": row["Name"],
                "TableName": row["TableName"],
                "Expression": row["Expression"],
                "Description": desc,
            }
        )
    rewritten_measures_bytes = b"\xef\xbb\xbf" + csv_buf.getvalue().encode("utf-8")
    assert rewritten_measures_bytes.startswith(b"\xef\xbb\xbfName,TableName,Expression,Description\n")
    (dax_csv_copy / "info_measures.csv").write_bytes(rewritten_measures_bytes)

    vault_dax = tmp_path / "vault_dax"
    vault_dax.mkdir(parents=True, exist_ok=True)
    summary_dax = sync_powerbi(vault_dax, PoopdeckConfig(), input_path=dax_csv_copy, mode="tier2")
    assert _summary_to_dict(summary_dax)["_errors"] == []

    for target_vault, min_measures in (
        (vault_sandbox, 7),
        (vault_tmdl, 7),
        (vault_dax, 6),
    ):
        loaded = _load_cards(target_vault / "reports")
        assert len(loaded) >= 1
        for md_path, parsed in loaded:
            raw_text = md_path.read_text(encoding="utf-8")
            assert "\ufeff" not in raw_text
            assert "SuperSecretPass123!" not in raw_text
            assert "dapi0123456789abcdef0123456789abcdef" not in raw_text
            assert account_key_secret not in raw_text
            assert "[REDACTED:SECRET]" in raw_text
            card = ReportCard.model_validate(parsed.frontmatter)
            assert len(card.measures) >= min_measures
            for m in card.measures:
                assert not m.name.startswith("\ufeff")
                assert not m.table.startswith("\ufeff")
            assert expected_measure_names.issubset({m.name for m in card.measures})

    tmdl_cards = [
        ReportCard.model_validate(parsed.frontmatter)
        for _, parsed in _load_cards(vault_tmdl / "reports")
    ]
    assert len(tmdl_cards) == 1
    assert len(tmdl_cards[0].measures) == 7
    assert {m.table for m in tmdl_cards[0].measures} == {"Sales", "Inventory"}
    assert len(tmdl_cards[0].relationships) >= 1

    dax_cards = [
        ReportCard.model_validate(parsed.frontmatter)
        for _, parsed in _load_cards(vault_dax / "reports")
    ]
    assert len(dax_cards) == 1
    assert len(dax_cards[0].measures) == 6
    assert {m.name for m in dax_cards[0].measures} == expected_measure_names


@pytest.mark.tier2
def test_f07_t2_sync_powerbi_malformed_model_bim_fails_cleanly_without_corrupt_cards(
    vault_sandbox: Path,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """Verify malformed_model.bim either raises ValueError/JSONDecodeError or records SyncSummary errors with 0 cards written."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.powerbi_ingest import sync_powerbi

    bad_bim = tmp_path / "malformed_model.bim"
    shutil.copy2(fixtures_dir / "powerbi" / "malformed_model.bim", bad_bim)

    raised_clean_error = False
    summary_errors: list[Any] = []
    try:
        summary = sync_powerbi(vault_sandbox, PoopdeckConfig(), input_path=bad_bim, mode="tier2")
        summary_errors = _summary_to_dict(summary)["_errors"]
    except (ValueError, json.JSONDecodeError):
        raised_clean_error = True

    assert raised_clean_error or len(summary_errors) >= 1
    assert list((vault_sandbox / "reports").glob("*.md")) == []


@pytest.mark.tier2
def test_f07_t2_sync_powerbi_resync_preserves_human_notes_byte_for_byte(
    vault_sandbox: Path,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """Verify re-syncing Power BI exports preserves custom HUMAN-NOTES blocks byte-for-byte."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.powerbi_ingest import sync_powerbi
    from poopdeck.models import ReportCard
    from poopdeck.vault_io import parse_markdown_card

    pbip_copy = tmp_path / "sample_pbip"
    shutil.copytree(fixtures_dir / "powerbi" / "sample_pbip", pbip_copy)

    sync_powerbi(vault_sandbox, PoopdeckConfig(), input_path=pbip_copy, mode="tier2")

    report_files = list((vault_sandbox / "reports").glob("*.md"))
    assert len(report_files) == 1
    report_path = report_files[0]

    custom_notes = (
        "\n### BI Owner Notes\n"
        "- Confirmed Monday Excel export w/ [[Sarah Chen]]\n"
        "- Path: `C:\\Reports\\BuyerWorkbench.pbix`\n"
    )
    _inject_human_notes(report_path, custom_notes)

    sync_powerbi(vault_sandbox, PoopdeckConfig(), input_path=pbip_copy, mode="tier2")

    parsed = parse_markdown_card(report_path)
    assert parsed.human_notes == custom_notes
    ReportCard.model_validate(parsed.frontmatter)


@pytest.mark.tier2
def test_f07_t2_sync_powerbi_empty_or_missing_local_exports_path_resilience(
    vault_sandbox: Path,
    tmp_path: Path,
) -> None:
    """Verify empty or nonexistent Power BI export paths produce zero corrupt ReportCards."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.powerbi_ingest import sync_powerbi

    empty_pbi = tmp_path / "empty_pbi"
    empty_pbi.mkdir(parents=True, exist_ok=True)
    sync_powerbi(vault_sandbox, PoopdeckConfig(), input_path=empty_pbi, mode="tier2")
    assert list((vault_sandbox / "reports").glob("*.md")) == []

    missing_pbi = tmp_path / "does_not_exist_pbi"
    try:
        sync_powerbi(vault_sandbox, PoopdeckConfig(), input_path=missing_pbi, mode="tier2")
    except FileNotFoundError:
        pass
    assert list((vault_sandbox / "reports").glob("*.md")) == []


@pytest.mark.tier2
def test_f07_t2_sync_powerbi_workspaces_include_filter_excludes_unmatched_workspaces(
    vault_sandbox: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify workspaces_include in Tier 1 REST mode writes matched workspaces and excludes unmatched ones."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.powerbi_ingest import sync_powerbi
    from poopdeck.models import ReportCard

    monkeypatch.setenv("POWERBI_ACCESS_TOKEN", "mock-offline-token")

    two_ws_client = _MockPowerBIRestClient(
        workspaces=[
            {
                "id": "ws-fin-01",
                "name": "Finance Executive Workspace",
                "report_id": "rpt-fin-01",
                "report_name": "CFO Cashflow & Working Capital Pulse",
                "dataset_id": "ds-fin-01",
                "configured_by": "Elena Rostova",
                "upstream_table": "prod_catalog.gold.customer_invoices",
                "failed_days_ago": [3],
            },
            {
                "id": "ws-comm-01",
                "name": "Commercial Workspace",
                "report_id": "rpt-churn-01",
                "report_name": "Executive Margin & Churn Pulse",
                "dataset_id": "ds-churn-01",
                "configured_by": "David Park",
                "upstream_table": "prod_catalog.gold.customer_invoices",
                "failed_days_ago": [3, 7],
            },
        ]
    )

    cfg = PoopdeckConfig()
    cfg.powerbi.workspaces_include = ["Finance Executive Workspace"]

    sync_powerbi(
        vault_sandbox,
        cfg,
        mode="tier1",
        rest_client=two_ws_client,
    )

    cards = [
        ReportCard.model_validate(parsed.frontmatter)
        for _, parsed in _load_cards(vault_sandbox / "reports")
    ]
    assert len(cards) == 1
    assert cards[0].workspace == "Finance Executive Workspace"
    assert cards[0].title == "CFO Cashflow & Working Capital Pulse"
    assert all(c.workspace != "Commercial Workspace" for c in cards)
    assert all("Executive Margin & Churn Pulse" not in c.title for c in cards)


# ============================================================================
# F08 — Microsoft Teams & Context Dropzone Normalizer (10 tests: 5 T1 + 5 T2)
# ============================================================================


@pytest.mark.tier1
def test_f08_t1_sync_teams_webvtt_extracts_speakers_kpis_and_boss_mandate(
    vault_sandbox: Path,
    fixtures_dir: Path,
) -> None:
    """Verify WebVTT (.vtt) parsing merges speaker turns and extracts KPIs, asks, and boss_mandate_asks."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.teams_ingest import sync_teams
    from poopdeck.models import StakeholderCard
    from poopdeck.vault_io import initialize_vault

    initialize_vault(vault_sandbox, seed_questions=True, init_git=False)
    shutil.copy2(
        fixtures_dir / "teams" / "ops_sync_meeting.vtt",
        vault_sandbox / "inbox" / "teams" / "ops_sync_meeting.vtt",
    )

    sync_teams(vault_sandbox, PoopdeckConfig())

    by_name: dict[str, Any] = {}
    for _, parsed in _load_cards(vault_sandbox / "stakeholders"):
        card = StakeholderCard.model_validate(parsed.frontmatter)
        by_name[card.name] = card

    assert "Sarah Chen" in by_name
    assert "Marcus Vance" in by_name

    sarah = by_name["Sarah Chen"]
    sarah_blob = sarah.model_dump_json().lower()
    assert any(kpi in sarah_blob for kpi in ("otif", "days of supply", "lost margin"))
    assert "daily buyer workbench" in sarah_blob or "inventory_snapshots" in sarah_blob

    marcus = by_name["Marcus Vance"]
    assert len(marcus.boss_mandate_asks) >= 1
    mandate_blob = " ".join(marcus.boss_mandate_asks).lower()
    assert "weekly executive service-level" in mandate_blob or "boss mandate" in mandate_blob


@pytest.mark.tier1
def test_f08_t1_sync_teams_ooxml_docx_extracts_paragraphs_and_tables_via_stdlib(
    vault_sandbox: Path,
    fixtures_dir: Path,
) -> None:
    """Verify stdlib OOXML .docx ingestion extracts both word/document.xml paragraphs and <w:tbl> rows."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.teams_ingest import sync_teams
    from poopdeck.models import StakeholderCard
    from poopdeck.vault_io import initialize_vault

    initialize_vault(vault_sandbox, seed_questions=True, init_git=False)
    shutil.copy2(
        fixtures_dir / "teams" / "finance_review.docx",
        vault_sandbox / "inbox" / "teams" / "finance_review.docx",
    )

    sync_teams(vault_sandbox, PoopdeckConfig())

    stakeholder_entries: list[tuple[Any, Any]] = []
    for _, parsed in _load_cards(vault_sandbox / "stakeholders"):
        card = StakeholderCard.model_validate(parsed.frontmatter)
        stakeholder_entries.append((card, parsed))

    vault_text_parts: list[str] = [
        md_file.read_text(encoding="utf-8")
        for md_file in sorted(vault_sandbox.rglob("*.md"))
        if "inbox" not in {part.lower() for part in md_file.relative_to(vault_sandbox).parts}
        and not any(part.startswith(".") for part in md_file.relative_to(vault_sandbox).parts)
    ]

    assert len(stakeholder_entries) >= 1
    elena_entry = next(
        (
            (c, p)
            for c, p in stakeholder_entries
            if "elena rostova" in c.name.lower()
            or "elena rostova"
            in (json.dumps(p.frontmatter, default=str).lower() + " " + p.body.lower())
        ),
        None,
    )
    assert elena_entry is not None
    _, elena_parsed = elena_entry
    elena_blob = (
        json.dumps(elena_parsed.frontmatter, default=str).lower()
        + " "
        + elena_parsed.body.lower()
    )
    assert "elena rostova" in elena_blob
    assert "finance" in elena_blob

    combined_vault_lower = "\n".join(vault_text_parts).lower()
    assert "churn unit cost" in combined_vault_lower
    assert any(
        tbl_detail in combined_vault_lower
        for tbl_detail in (
            "$18,500",
            "18,500",
            "91.4%",
            "on-time in-full",
            "mid-market b2b",
            "trailing 14-day",
        )
    )


@pytest.mark.tier1
def test_f08_t1_sync_teams_markdown_1on1_notes_extracts_wikilinks_and_asks(
    vault_sandbox: Path,
    fixtures_dir: Path,
) -> None:
    """Verify raw 1:1 Markdown notes populate StakeholderCard role, owned_reports wikilinks, pain_points & asks."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.teams_ingest import sync_teams
    from poopdeck.models import StakeholderCard
    from poopdeck.vault_io import initialize_vault

    initialize_vault(vault_sandbox, seed_questions=True, init_git=False)
    shutil.copy2(
        fixtures_dir / "teams" / "quick_1on1_notes.md",
        vault_sandbox / "inbox" / "notes" / "quick_1on1_notes.md",
    )

    sync_teams(vault_sandbox, PoopdeckConfig())

    stakeholder_entries = [
        (StakeholderCard.model_validate(parsed.frontmatter), parsed)
        for _, parsed in _load_cards(vault_sandbox / "stakeholders")
    ]
    assert len(stakeholder_entries) >= 1
    david_entry = next(
        (
            (c, p)
            for c, p in stakeholder_entries
            if "david park" in c.name.lower()
            or "david park"
            in (json.dumps(p.frontmatter, default=str).lower() + " " + p.body.lower())
        ),
        None,
    )
    assert david_entry is not None
    david, david_parsed = david_entry
    david_blob = (
        json.dumps(david_parsed.frontmatter, default=str).lower()
        + " "
        + david_parsed.body.lower()
    )
    assert any(
        tok in david_blob
        for tok in ("power bi", "semantic model", "enterprise analytics", "sme")
    )
    owned = {_strip_wikilink(r) for r in david.owned_reports}
    assert any("Daily Buyer Workbench" in r for r in owned)
    assert len(david.pain_points) >= 1
    assert len(david.explicit_asks) >= 1


@pytest.mark.tier1
def test_f08_t1_sync_teams_updates_glossary_and_open_questions_cards(
    vault_sandbox: Path,
    fixtures_dir: Path,
) -> None:
    """Verify sync_teams appends harvested glossary terms and open questions without losing the 15 seeded questions."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.teams_ingest import sync_teams
    from poopdeck.models import GlossaryCard, OpenQuestionBank
    from poopdeck.vault_io import initialize_vault, parse_markdown_card

    initialize_vault(vault_sandbox, seed_questions=True, init_git=False)
    before_bank = OpenQuestionBank.model_validate(
        parse_markdown_card(vault_sandbox / "questions" / "open-questions.md").frontmatter
    )
    seeded_ids = {q.id for q in before_bank.questions}
    assert len(seeded_ids) == 15

    for fname in ("ops_sync_meeting.vtt", "finance_review.docx", "quick_1on1_notes.md"):
        shutil.copy2(
            fixtures_dir / "teams" / fname,
            vault_sandbox / "inbox" / "teams" / fname,
        )

    sync_teams(vault_sandbox, PoopdeckConfig())

    gcard = GlossaryCard.model_validate(
        parse_markdown_card(vault_sandbox / "glossary.md").frontmatter
    )
    assert len(gcard.terms) >= 2
    term_names_lower = {t.term.lower() for t in gcard.terms}
    assert term_names_lower & {
        "otif",
        "days of supply",
        "lost margin",
        "churn unit cost",
        "semantic model certification",
    }

    after_bank = OpenQuestionBank.model_validate(
        parse_markdown_card(vault_sandbox / "questions" / "open-questions.md").frontmatter
    )
    after_ids = {q.id for q in after_bank.questions}
    assert seeded_ids.issubset(after_ids)
    assert len(after_bank.questions) > 15


@pytest.mark.tier1
def test_f08_t1_sync_teams_archives_processed_inbox_files_when_enabled(
    vault_sandbox: Path,
    fixtures_dir: Path,
) -> None:
    """Verify sync_teams moves ingested files into vault/inbox/processed/ when archive_processed=True."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.teams_ingest import sync_teams
    from poopdeck.vault_io import initialize_vault

    initialize_vault(vault_sandbox, seed_questions=True, init_git=False)
    vtt_src = vault_sandbox / "inbox" / "teams" / "ops_sync_meeting.vtt"
    md_src = vault_sandbox / "inbox" / "notes" / "quick_1on1_notes.md"
    shutil.copy2(fixtures_dir / "teams" / "ops_sync_meeting.vtt", vtt_src)
    shutil.copy2(fixtures_dir / "teams" / "quick_1on1_notes.md", md_src)

    cfg = PoopdeckConfig()
    cfg.teams.archive_processed = True
    sync_teams(vault_sandbox, cfg)

    assert not vtt_src.exists()
    assert not md_src.exists()
    processed_dir = vault_sandbox / "inbox" / "processed"
    assert len(list(processed_dir.rglob("ops_sync_meeting.vtt"))) == 1
    assert len(list(processed_dir.rglob("quick_1on1_notes.md"))) == 1


@pytest.mark.tier2
def test_f08_t2_sync_teams_scrubs_all_inline_pii_and_secrets_across_vtt_docx_and_md(
    vault_sandbox: Path,
    fixtures_dir: Path,
) -> None:
    """Verify zero raw emails, SSNs, phone numbers, PATs, JDBC passwords, or JWTs leak into vault cards."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.teams_ingest import sync_teams
    from poopdeck.vault_io import initialize_vault

    initialize_vault(vault_sandbox, seed_questions=True, init_git=False)
    for fname in ("ops_sync_meeting.vtt", "finance_review.docx", "quick_1on1_notes.md"):
        shutil.copy2(
            fixtures_dir / "teams" / fname,
            vault_sandbox / "inbox" / "teams" / fname,
        )

    sync_teams(vault_sandbox, PoopdeckConfig())

    forbidden_leaks = (
        "sarah.chen@corp-example.com",
        "elena.rostova@corp-example.com",
        "david.park@corp-example.com",
        "123-45-6789",
        "987-65-4321",
        "867-5309",
        "234-5678",
        "301-9988",
        "dapi0123456789abcdef0123456789abcdef",
        "SuperSecretPass123!",
        "ghp_0123456789abcdefghijklmnopqrstuvwxyzABCD",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
    )

    checked_files = 0
    for md_file in vault_sandbox.rglob("*.md"):
        if "inbox" in md_file.relative_to(vault_sandbox).parts:
            continue
        checked_files += 1
        raw_text = md_file.read_text(encoding="utf-8")
        for secret_or_pii in forbidden_leaks:
            assert secret_or_pii not in raw_text, (
                f"Leaked '{secret_or_pii}' into {md_file.relative_to(vault_sandbox)}"
            )
    assert checked_files >= 5


@pytest.mark.tier2
def test_f08_t2_sync_teams_corrupted_docx_does_not_abort_valid_sibling_batch(
    vault_sandbox: Path,
    fixtures_dir: Path,
) -> None:
    """Verify an invalid 66-byte corrupted.docx does not crash sibling .vtt and .md ingestion."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.teams_ingest import sync_teams
    from poopdeck.models import StakeholderCard
    from poopdeck.vault_io import initialize_vault

    initialize_vault(vault_sandbox, seed_questions=True, init_git=False)
    for fname in ("corrupted.docx", "ops_sync_meeting.vtt", "quick_1on1_notes.md"):
        shutil.copy2(
            fixtures_dir / "teams" / fname,
            vault_sandbox / "inbox" / "teams" / fname,
        )

    sync_teams(vault_sandbox, PoopdeckConfig())

    names = {
        StakeholderCard.model_validate(parsed.frontmatter).name
        for _, parsed in _load_cards(vault_sandbox / "stakeholders")
    }
    assert {"Sarah Chen", "Marcus Vance", "David Park"}.issubset(names)
    assert not any("corrupted" in n.lower() for n in names)


@pytest.mark.tier2
def test_f08_t2_sync_teams_empty_vtt_produces_no_corrupt_stakeholder_cards(
    vault_sandbox: Path,
    fixtures_dir: Path,
) -> None:
    """Verify minimal empty WEBVTT file creates zero corrupt StakeholderCards and leaves vault valid."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.teams_ingest import sync_teams
    from poopdeck.vault_io import initialize_vault, validate_vault

    initialize_vault(vault_sandbox, seed_questions=True, init_git=False)
    shutil.copy2(
        fixtures_dir / "teams" / "empty_transcript.vtt",
        vault_sandbox / "inbox" / "teams" / "empty_transcript.vtt",
    )

    sync_teams(vault_sandbox, PoopdeckConfig())

    assert list((vault_sandbox / "stakeholders").glob("*.md")) == []
    assert validate_vault(vault_sandbox).valid is True


@pytest.mark.tier2
def test_f08_t2_sync_teams_archive_processed_false_leaves_inbox_files_in_place(
    vault_sandbox: Path,
    fixtures_dir: Path,
) -> None:
    """Verify archive_processed=False leaves inbox source files untouched in place while writing StakeholderCards."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.teams_ingest import sync_teams
    from poopdeck.vault_io import initialize_vault

    initialize_vault(vault_sandbox, seed_questions=True, init_git=False)
    src = vault_sandbox / "inbox" / "teams" / "ops_sync_meeting.vtt"
    shutil.copy2(fixtures_dir / "teams" / "ops_sync_meeting.vtt", src)

    cfg = PoopdeckConfig()
    cfg.teams.archive_processed = False
    sync_teams(vault_sandbox, cfg)

    assert src.is_file()
    processed_files = [
        p for p in (vault_sandbox / "inbox" / "processed").rglob("*") if p.is_file()
    ]
    assert processed_files == []
    assert len(list((vault_sandbox / "stakeholders").glob("*.md"))) >= 2


@pytest.mark.tier2
def test_f08_t2_sync_teams_resync_preserves_stakeholder_human_notes_byte_for_byte(
    vault_sandbox: Path,
    fixtures_dir: Path,
) -> None:
    """Verify re-running sync_teams preserves custom HUMAN-NOTES inside StakeholderCard files byte-for-byte."""
    from poopdeck.config import PoopdeckConfig
    from poopdeck.crawlers.teams_ingest import sync_teams
    from poopdeck.models import StakeholderCard
    from poopdeck.vault_io import initialize_vault, parse_markdown_card

    initialize_vault(vault_sandbox, seed_questions=True, init_git=False)
    shutil.copy2(
        fixtures_dir / "teams" / "ops_sync_meeting.vtt",
        vault_sandbox / "inbox" / "teams" / "ops_sync_meeting.vtt",
    )

    cfg = PoopdeckConfig()
    cfg.teams.archive_processed = False
    sync_teams(vault_sandbox, cfg)

    sarah_path = next(
        path
        for path, parsed in _load_cards(vault_sandbox / "stakeholders")
        if StakeholderCard.model_validate(parsed.frontmatter).name == "Sarah Chen"
    )
    custom_notes = (
        "\n### 1:1 Follow-up\n"
        "- Prefers Teams async updates before 9am EST\n"
        "- Link: [[OPP-004]]\n"
    )
    _inject_human_notes(sarah_path, custom_notes)

    sync_teams(vault_sandbox, cfg)

    parsed_after = parse_markdown_card(sarah_path)
    assert parsed_after.human_notes == custom_notes
    StakeholderCard.model_validate(parsed_after.frontmatter)
