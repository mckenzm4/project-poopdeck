"""Unit and offline integration tests for Milestone M2 crawlers & extraction scripts."""

from __future__ import annotations

import importlib.util
import json
import math
import os
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import notebooks.databricks_zero_admin_crawler as zero_admin_nb
import poopdeck.crawlers.databricks_ingest as dbx_mod
import poopdeck.crawlers.powerbi_ingest as pbi_mod
import poopdeck.crawlers.teams_ingest as teams_mod
from poopdeck.config import PoopdeckConfig, PrivacyConfig
from poopdeck.crawlers import (
    SyncSummary,
    detect_dax_heuristics,
    redact_crawler_text,
    sync_all,
    sync_databricks,
    sync_powerbi,
    sync_teams,
    unwrap_prose_wikilinks,
)
from poopdeck.models import (
    DatasetCard,
    GlossaryCard,
    OpenQuestionBank,
    ReportCard,
    StakeholderCard,
)
from poopdeck.vault_io import (
    HUMAN_NOTES_END,
    HUMAN_NOTES_START,
    build_lineage_graph,
    initialize_vault,
    parse_markdown_card,
    update_question_or_phase,
    validate_vault,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

_AMBIENT_CLOUD_ENV_VARS: tuple[str, ...] = (
    "DATABRICKS_HOST",
    "DATABRICKS_TOKEN",
    "DATABRICKS_WAREHOUSE_ID",
    "DATABRICKS_SQL_WAREHOUSE_ID",
    "DATABRICKS_HTTP_PATH",
    "DATABRICKS_CONFIG_PROFILE",
    "DATABRICKS_CLIENT_ID",
    "DATABRICKS_CLIENT_SECRET",
    "POWERBI_ACCESS_TOKEN",
    "FABRIC_ACCESS_TOKEN",
)


@pytest.fixture(autouse=True)
def _isolate_ambient_cloud_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for env_key in _AMBIENT_CLOUD_ENV_VARS:
        monkeypatch.delenv(env_key, raising=False)
    monkeypatch.setattr(pbi_mod, "_acquire_az_cli_token", lambda: (None, None))


# ---------------------------------------------------------------------------
# Helpers for Synthetic Fixtures (100% tmp_path isolated)
# ---------------------------------------------------------------------------


def _build_minimal_docx(path: Path, xml_body_inner: str, *, doctype_header: str = "") -> Path:
    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"{doctype_header}"
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{xml_body_inner}</w:body>"
        "</w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        "</Types>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("word/document.xml", doc_xml)
    return path


# ---------------------------------------------------------------------------
# 1. Package Contracts, SyncSummary & Submodule Import Order
# ---------------------------------------------------------------------------


def test_submodule_direct_imports_have_zero_circular_dependency() -> None:
    for mod_stmt in (
        "from poopdeck.crawlers.databricks_ingest import sync_databricks",
        "from poopdeck.crawlers.powerbi_ingest import sync_powerbi, detect_dax_heuristics",
        "from poopdeck.crawlers.teams_ingest import sync_teams",
    ):
        proc = subprocess.run(
            [sys.executable, "-B", "-c", mod_stmt],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr


def test_sync_summary_strict_validation_and_sync_all_contracts(tmp_path: Path) -> None:
    summary = SyncSummary(target="databricks", mode_used="tier2", cards_written=3)
    assert summary.items_synced == 3

    with pytest.raises(ValueError):
        SyncSummary(target="databricks", mode_used="tier2", cards_written=True)  # type: ignore[arg-type]

    vault_dir = tmp_path / "vault"
    initialize_vault(vault_dir, init_git=False)

    cfg = PoopdeckConfig()
    assert cfg.privacy.allow_sample_rows is False

    with pytest.raises(ValueError, match="Invalid sync target"):
        sync_all(vault_dir, config=cfg, target="snowflake")

    res = sync_all(vault_dir, config=cfg, target="all", allow_sample_rows=True)
    assert set(res.keys()) == {"databricks", "powerbi", "teams"}
    assert all(s.errors == [] for s in res.values())
    # Caller's config object must never be mutated
    assert cfg.privacy.allow_sample_rows is False


# ---------------------------------------------------------------------------
# 2. Feature F06: Databricks Dual-Mode Crawler & Zero-Admin Notebook
# ---------------------------------------------------------------------------


def test_databricks_tier1_uc_sql_runner_privacy_gate_and_sample_switch(
    tmp_path: Path,
) -> None:
    vault_dir = tmp_path / "vault"
    executed_queries: list[str] = []
    recent_ts = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()

    def fake_sql_runner(sql: str) -> list[dict[str, Any]]:
        executed_queries.append(sql)
        s_low = sql.lower()
        if "from system.information_schema.tables" in s_low:
            return [
                {
                    "table_catalog": "prod",
                    "table_schema": "bronze_erp",
                    "table_name": "orders_raw",
                    "table_type": "BASE TABLE",
                    "comment": "Ingested by svc@corp.com token dapi0123456789abcdef0123456789",
                },
                {
                    "table_catalog": "prod",
                    "table_schema": "gold_ops",
                    "table_name": "order_fulfillment",
                    "table_type": "MANAGED",
                    "comment": "Curated fulfillment KPI table",
                },
                {
                    "table_catalog": "samples",
                    "table_schema": "nyctaxi",
                    "table_name": "trips",
                    "table_type": "MANAGED",
                    "comment": "Excluded sample catalog",
                },
            ]
        if "from system.information_schema.columns" in s_low:
            return [
                {
                    "table_catalog": "prod",
                    "table_schema": "bronze_erp",
                    "table_name": "orders_raw",
                    "column_name": "order_id",
                    "full_data_type": "STRING",
                    "is_nullable": "NO",
                    "comment": "Primary key",
                    "ordinal_position": 1,
                },
                {
                    "table_catalog": "prod",
                    "table_schema": "bronze_erp",
                    "table_name": "orders_raw",
                    "column_name": "birth_date",
                    "full_data_type": "DATE",
                    "is_nullable": "YES",
                    "comment": "Customer DOB",
                    "ordinal_position": 2,
                },
                {
                    "table_catalog": "prod",
                    "table_schema": "bronze_erp",
                    "table_name": "orders_raw",
                    "column_name": "customer_email",
                    "full_data_type": "STRING",
                    "is_nullable": "YES",
                    "comment": "",
                    "ordinal_position": 3,
                },
                {
                    "table_catalog": "prod",
                    "table_schema": "bronze_erp",
                    "table_name": "orders_raw",
                    "column_name": "ingested_at",
                    "full_data_type": "TIMESTAMP",
                    "is_nullable": "NO",
                    "comment": "ETL load timestamp",
                    "ordinal_position": 4,
                },
                {
                    "table_catalog": "prod",
                    "table_schema": "gold_ops",
                    "table_name": "order_fulfillment",
                    "column_name": "order_id",
                    "full_data_type": "STRING",
                    "is_nullable": "NO",
                    "comment": "Order grain key",
                    "ordinal_position": 1,
                },
            ]
        if "from system.information_schema.table_tags" in s_low:
            return [
                {
                    "catalog_name": "prod",
                    "schema_name": "gold_ops",
                    "table_name": "order_fulfillment",
                    "tag_name": "domain",
                    "tag_value": "supply_chain",
                }
            ]
        if "from system.access.table_lineage" in s_low:
            return [
                {
                    "source_table_full_name": "prod.bronze_erp.orders_raw",
                    "target_table_full_name": "prod.gold_ops.order_fulfillment",
                    "entity_type": "JOB",
                    "entity_id": "nightly_fulfillment_etl",
                }
            ]
        if "from system.query.history" in s_low:
            return [
                {"statement_text": "SELECT * FROM prod.gold_ops.order_fulfillment WHERE 1=1"},
                {"statement_text": "SELECT count(*) FROM prod.gold_ops.order_fulfillment"},
            ]
        if "count(*) as row_count" in s_low and "prod.bronze_erp.orders_raw" in s_low:
            # Verify PII `birth_date` was NOT chosen as freshness timestamp
            assert "min(birth_date)" not in s_low
            assert "max(ingested_at)" in s_low
            return [
                {
                    "row_count": 1000,
                    "min_timestamp": "2026-01-01T00:00:00+00:00",
                    "max_timestamp": recent_ts,
                    "__nulls__0": 0,
                    "__distinct__0": 1000,
                    "__nulls__2": 50,
                    "__distinct__2": 920,
                }
            ]
        if "count(*) as row_count" in s_low:
            return [{"row_count": 950, "__nulls__0": 0, "__distinct__0": 950}]
        if "select * from prod.bronze_erp.orders_raw limit" in s_low:
            return [
                {
                    "order_id": "ORD-100",
                    "birth_date": "1985-04-12",
                    "customer_email": "alice@example.com",
                    "ingested_at": recent_ts,
                }
            ]
        return []

    # Default safe_aggregates: zero sample rows & zero SELECT * LIMIT queries
    summary_safe = sync_databricks(vault_dir, sql_runner=fake_sql_runner)
    assert summary_safe.mode_used == "tier1"
    assert summary_safe.cards_written == 2
    assert summary_safe.errors == []
    assert not any("select * from" in q.lower() for q in executed_queries)

    bronze_path = vault_dir / "datasets" / "prod.bronze_erp.orders_raw.md"
    gold_path = vault_dir / "datasets" / "prod.gold_ops.order_fulfillment.md"
    assert bronze_path.is_file() and gold_path.is_file()
    assert not (vault_dir / "datasets" / "samples.nyctaxi.trips.md").exists()

    bronze_card = DatasetCard.model_validate(parse_markdown_card(bronze_path).frontmatter)
    gold_card = DatasetCard.model_validate(parse_markdown_card(gold_path).frontmatter)

    assert bronze_card.medallion_layer == "bronze"
    assert bronze_card.sample_rows == []
    assert bronze_card.downstream_refs == ["[[prod.gold_ops.order_fulfillment]]"]
    assert gold_card.upstream_refs == ["[[prod.bronze_erp.orders_raw]]"]
    assert gold_card.query_count_30d == 2
    assert gold_card.tags.get("domain") == "supply_chain"

    by_col = {c.name: c for c in bronze_card.columns}
    assert by_col["customer_email"].is_pii is True
    assert by_col["customer_email"].null_pct == pytest.approx(5.0)
    assert by_col["birth_date"].is_pii is True
    assert "birth_date" not in bronze_card.join_keys
    assert "order_id" in bronze_card.join_keys
    assert "dapi0123456789" not in bronze_path.read_text(encoding="utf-8")

    # Explicit allow_sample_rows=True: queries LIMIT and masks PII cells
    summary_samples = sync_databricks(
        vault_dir, sql_runner=fake_sql_runner, allow_sample_rows=True
    )
    assert summary_samples.cards_written == 2
    bronze_with_samples = DatasetCard.model_validate(
        parse_markdown_card(bronze_path).frontmatter
    )
    assert len(bronze_with_samples.sample_rows) == 1
    row0 = bronze_with_samples.sample_rows[0]
    assert row0["order_id"] == "ORD-100"
    assert row0["customer_email"] == "[REDACTED:PII]"
    assert row0["birth_date"] == "[REDACTED:PII]"


def test_databricks_tier1_hive_metastore_fallback_on_uc_403(tmp_path: Path) -> None:
    vault_dir = tmp_path / "vault"

    def hive_only_runner(sql: str) -> Any:
        su = sql.strip().upper()
        if "SYSTEM.INFORMATION_SCHEMA" in su:
            raise PermissionError("403 Forbidden: Unity Catalog system tables disabled")
        if su == "SHOW DATABASES":
            return [("silver_supply",), ("information_schema",)]
        if su == "SHOW TABLES IN SILVER_SUPPLY":
            return [
                ("silver_supply", "inventory_snapshots", False),
                ("silver_supply", "temp_debug_view", True),
            ]
        if su == "DESCRIBE EXTENDED SILVER_SUPPLY.INVENTORY_SNAPSHOTS":
            return [
                ("sku_id", "string", "Product identifier"),
                ("warehouse_id", "string", "DC code"),
                ("supplier_phone", "string", "Vendor phone contact"),
                ("", "", ""),
                ("# Partition Information", "", ""),
                ("# col_name", "data_type", "comment"),
                ("snapshot_date", "date", "Daily partition"),
                ("", "", ""),
                ("# Detailed Table Information", "", ""),
                ("Database", "silver_supply", ""),
                ("Table", "inventory_snapshots", ""),
                ("Type", "EXTERNAL", ""),
                ("Table Properties", "[quality=silver,delta.minReaderVersion=1]", ""),
                ("Statistics", "12400 bytes, 420 rows", ""),
            ]
        if "COUNT(*) AS ROW_COUNT" in su:
            return [{"row_count": 420, "min_timestamp": "2026-10-01", "max_timestamp": "2026-11-10"}]
        return []

    summary = sync_databricks(vault_dir, sql_runner=hive_only_runner)
    assert summary.mode_used == "tier1"
    assert summary.errors == []
    assert any("Hive Metastore" in w for w in summary.warnings)
    assert summary.cards_written == 1

    card_path = vault_dir / "datasets" / "hive_metastore.silver_supply.inventory_snapshots.md"
    assert card_path.is_file()
    card = DatasetCard.model_validate(parse_markdown_card(card_path).frontmatter)
    assert card.catalog == "hive_metastore"
    assert card.table_type == "EXTERNAL"
    assert card.medallion_layer == "silver"
    assert card.row_count == 420
    assert card.tags.get("quality") == "silver"
    col_names = [c.name for c in card.columns]
    assert col_names == ["sku_id", "warehouse_id", "supplier_phone", "snapshot_date"]
    assert next(c for c in card.columns if c.name == "supplier_phone").is_pii is True


def test_databricks_tier1_sdk_mock_client_and_special_type_sanitization(
    tmp_path: Path,
) -> None:
    class FakeTableType(Enum):
        VIEW = "MATERIALIZED VIEW"

    class FakeColType(Enum):
        DECIMAL = "DECIMAL(18,2)"

    mock_client = MagicMock()  # statement_execution is an unconfigured MagicMock!
    mock_client.catalogs.list.return_value = [SimpleNamespace(name="corp_cat")]
    mock_client.schemas.list.return_value = [SimpleNamespace(name="gold_fin")]
    mock_client.tables.list.return_value = [
        SimpleNamespace(
            catalog_name="corp_cat",
            schema_name="gold_fin",
            name="margin_summary",
            full_name="corp_cat.gold_fin.margin_summary",
            table_type=FakeTableType.VIEW,
            comment="Contact owner@corp.com",
            properties={
                "source_path": Path("/mnt/secret/pwd=SuperSecret123/data"),
                "raw_note": b"token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234",
                "nan_metric": float("nan"),
                "inf_metric": Decimal("Infinity"),
            },
            columns=[
                SimpleNamespace(
                    name="account_id",
                    type_text=FakeColType.DECIMAL,
                    nullable=False,
                    comment="",
                    partition_index=0,
                ),
                SimpleNamespace(
                    name="employee_salary",
                    type_text="DOUBLE",
                    nullable=True,
                    comment="Annual compensation",
                    partition_index=None,
                ),
            ],
        )
    ]
    mock_client.jobs.list.return_value = [
        SimpleNamespace(
            settings=SimpleNamespace(
                name="refresh_margin_summary_pwd=TopSecret99",
                tasks=[],
            )
        )
    ]

    vault_dir = tmp_path / "vault"
    summary = sync_databricks(vault_dir, client=mock_client)
    assert summary.mode_used == "tier1"
    assert summary.cards_written == 1
    assert summary.errors == []

    card_path = vault_dir / "datasets" / "corp_cat.gold_fin.margin_summary.md"
    raw_md = card_path.read_text(encoding="utf-8")
    assert "SuperSecret123" not in raw_md
    assert "ghp_ABCDEF" not in raw_md
    assert "TopSecret99" not in raw_md
    assert "owner@corp.com" not in raw_md

    card = DatasetCard.model_validate(parse_markdown_card(card_path).frontmatter)
    assert card.table_type == "VIEW"
    assert card.medallion_layer == "gold"
    assert len(card.scheduled_jobs) == 1
    assert "[REDACTED:SECRET]" in card.scheduled_jobs[0]


def test_databricks_tier2_bom_csv_and_zero_admin_notebook_roundtrip(
    tmp_path: Path,
) -> None:
    # 1. Verify notebook header & standalone properties
    nb_src = (REPO_ROOT / "notebooks" / "databricks_zero_admin_crawler.py").read_text(
        encoding="utf-8"
    )
    assert nb_src.startswith("# Databricks notebook source\n")
    assert "# COMMAND ----------" in nb_src
    assert zero_admin_nb.INCLUDE_SAMPLE_ROWS is False

    # 2. Fake SparkSession driving `build_discovery_bundle` -> `export_discovery_bundle` -> `sync_databricks`
    class FakeDF:
        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self._rows = rows

        def collect(self) -> list[dict[str, Any]]:
            return self._rows

    class FakeSpark:
        def sql(self, query: str) -> FakeDF:
            ql = query.lower()
            if "system.information_schema.tables" in ql:
                return FakeDF(
                    [
                        {
                            "table_catalog": "main",
                            "table_schema": "silver_ops",
                            "table_name": "shipments",
                            "table_type": "MANAGED",
                            "comment": "Ops shipments",
                            "last_altered": "2026-11-10T00:00:00Z",
                        }
                    ]
                )
            if "system.information_schema.columns" in ql:
                return FakeDF(
                    [
                        {
                            "table_catalog": "main",
                            "table_schema": "silver_ops",
                            "table_name": "shipments",
                            "column_name": "shipment_id",
                            "full_data_type": "STRING",
                            "is_nullable": "NO",
                            "comment": "",
                            "ordinal_position": 1,
                            "partition_index": None,
                        },
                        {
                            "table_catalog": "main",
                            "table_schema": "silver_ops",
                            "table_name": "shipments",
                            "column_name": "driver_name",
                            "full_data_type": "STRING",
                            "is_nullable": "YES",
                            "comment": "",
                            "ordinal_position": 2,
                            "partition_index": None,
                        },
                    ]
                )
            if "count(*) as row_count" in ql:
                return FakeDF([{"row_count": 250, "__nulls__0": 0, "__distinct__0": 250, "__nulls__1": 5, "__distinct__1": 80}])
            return FakeDF([])

    bundle = zero_admin_nb.build_discovery_bundle(FakeSpark(), include_sample_rows=False)
    assert bundle["catalog_mode"] == "unity_catalog"
    assert bundle["tables"][0]["sample_rows"] == []

    bundle_file = tmp_path / "exports" / "dbx_bundle.json"
    zero_admin_nb.export_discovery_bundle(bundle, bundle_file)
    assert bundle_file.is_file()

    vault_dir = tmp_path / "vault"
    s1 = sync_databricks(vault_dir, input_path=bundle_file, mode="tier2")
    assert s1.cards_written == 1
    assert bundle_file.is_file()  # Tier 2 fixture preserved on disk

    # 3. UTF-8-BOM CSV directory (`tables.csv`, `columns.csv`, `lineage.csv`, `profiles.csv`)
    csv_dir = tmp_path / "csv_exports"
    csv_dir.mkdir()
    (csv_dir / "tables.csv").write_bytes(
        "\ufeffcatalog,schema_name,table_name,table_type,grain\n"
        "main,gold_ops,carrier_sla,MANAGED,One row per carrier_id\n".encode("utf-8")
    )
    (csv_dir / "columns.csv").write_bytes(
        "\ufeffcatalog,schema_name,table_name,column_name,data_type,comment\n"
        "main,gold_ops,carrier_sla,carrier_id,STRING,Carrier PK\n"
        "main,gold_ops,carrier_sla,contact_email,STRING,Dispatcher email\n".encode("utf-8")
    )
    (csv_dir / "profiles.csv").write_bytes(
        "\ufeffcatalog,schema_name,table_name,column_name,row_count,null_pct,distinct_count,min_val,max_val\n"
        "main,gold_ops,carrier_sla,,180,,,,\n"
        "main,gold_ops,carrier_sla,carrier_id,,0.0,180,CAR-001,CAR-180\n"
        "main,gold_ops,carrier_sla,contact_email,,2.5,175,a@b.com,z@b.com\n".encode("utf-8")
    )
    (csv_dir / "lineage.csv").write_bytes(
        "\ufeffsource_table_full_name,target_table_full_name,job_name\n"
        "main.silver_ops.shipments,main.gold_ops.carrier_sla,daily_carrier_sla\n".encode("utf-8")
    )

    s2 = sync_databricks(vault_dir, input_path=csv_dir, mode="tier2")
    assert s2.cards_written == 1
    sla_card = DatasetCard.model_validate(
        parse_markdown_card(vault_dir / "datasets" / "main.gold_ops.carrier_sla.md").frontmatter
    )
    assert sla_card.row_count == 180
    assert sla_card.upstream_refs == ["[[main.silver_ops.shipments]]"]
    assert sla_card.scheduled_jobs == ["daily_carrier_sla"]
    email_col = next(c for c in sla_card.columns if c.name == "contact_email")
    assert email_col.is_pii is True
    assert email_col.min_val == "[REDACTED:PII]"
    assert email_col.max_val == "[REDACTED:PII]"


# ---------------------------------------------------------------------------
# 3. Feature F07: Power BI Crawler, DAX INFO.* Queries & Heuristic Detector
# ---------------------------------------------------------------------------


def test_dax_info_extract_script_and_heuristic_detector_accuracy() -> None:
    dax_query_text = (REPO_ROOT / "queries" / "powerbi_dax_info_extract.dax").read_text(
        encoding="utf-8"
    )
    for required_fn in (
        "EVALUATE",
        "INFO.TABLES()",
        "INFO.COLUMNS()",
        "INFO.MEASURES()",
        "INFO.RELATIONSHIPS()",
    ):
        assert required_fn in dax_query_text

    # Positive heuristic detections (all 4 categories)
    f_tiers = detect_dax_heuristics(
        "Supplier Risk Tier",
        'SWITCH(TRUE(), [LateRate] > 0.25, "Critical", [LateRate] > 0.10, "High", "Low")',
    )
    assert "nested_heuristic_tiers" in f_tiers
    assert "static_threshold_alert" in f_tiers

    f_mavg = detect_dax_heuristics(
        "Next Quarter Demand Forecast",
        "AVERAGEX(DATESINPERIOD('Calendar'[Date], MAX('Calendar'[Date]), -3, MONTH), [Monthly Units])",
    )
    assert "naive_moving_avg_forecast" in f_mavg

    f_thresh = detect_dax_heuristics(
        "Stockout Alert Flag",
        'IF([DaysOfSupply] < 7, "Reorder", "Healthy")',
    )
    assert "static_threshold_alert" in f_thresh

    f_back = detect_dax_heuristics(
        "Revenue YoY",
        "CALCULATE(SUM(Sales[Revenue]), SAMEPERIODLASTYEAR('Calendar'[Date]))",
    )
    assert f_back == ["backward_looking_only"]

    # Clean DAX negative cases (must NOT false-positive)
    assert detect_dax_heuristics("Total Revenue", "SUM(Sales[Revenue])") == []
    assert detect_dax_heuristics("Order Count", "COUNTROWS(Sales)") == []
    assert detect_dax_heuristics("Avg Order Value", "DIVIDE([Total Revenue], [Order Count], 0)") == []
    assert (
        detect_dax_heuristics(
            "Safe Margin %",
            "// SWITCH(TRUE(), [x] > 5, 'Critical')\nIF([Total Revenue] > 0, DIVIDE([Margin], [Total Revenue], 0), 0)",
        )
        == []
    )
    assert (
        detect_dax_heuristics(
            "ML Predicted Demand",
            "SUM('DemandForecast'[forecast_ml_p50])",
        )
        == []
    )


def test_powerbi_tier1_rest_and_tier2_pbip_bim_tmdl_info_exports(
    tmp_path: Path,
) -> None:
    vault_dir = tmp_path / "vault"

    # 1. Tier 1 REST mock (with historical >90d failed refreshes + email owner redaction)
    rest_snapshot = {
        "workspaces": [{"id": "ws-1", "name": "Executive Ops"}],
        "datasets": {
            "ws-1": [
                {
                    "id": "ds-1",
                    "name": "Fulfillment Semantic Model",
                    "configuredBy": "jane.owner@corp.com",
                    "owners": ["Sarah Jenkins"],
                    "refreshSchedule": {"days": ["Monday", "Wednesday", "Friday"], "times": ["06:30"], "enabled": True},
                    "refreshes": [
                        {"status": "Failed", "endTime": "2025-05-10T06:35:00Z"},
                        {"status": "Failed", "endTime": "2025-05-25T06:35:00Z"},
                        {"status": "Completed", "endTime": "2025-05-28T06:35:00Z"},
                        {"status": "Failed", "endTime": "2025-02-01T06:35:00Z"},  # >30d before max_ts
                    ],
                    "tables": [
                        {
                            "name": "Fulfillment",
                            "measures": [
                                {
                                    "name": "OTIF YoY %",
                                    "expression": "CALCULATE([OTIF %], SAMEPERIODLASTYEAR('Date'[Date]))",
                                }
                            ],
                            "partitions": [
                                {
                                    "source": {
                                        "expression": 'Value.NativeQuery(Databricks.Catalogs("adb.azuredatabricks.net", "/sql/1.0/warehouses/abc"), "SELECT * FROM prod.gold_ops.order_fulfillment WHERE pwd=SecretPass99")'
                                    }
                                }
                            ],
                        }
                    ],
                }
            ]
        },
        "reports": {
            "ws-1": [
                {
                    "id": "rep-1",
                    "name": "Executive Fulfillment Scorecard",
                    "datasetId": "ds-1",
                    "high_excel_export": True,
                }
            ]
        },
    }

    s_t1 = sync_powerbi(vault_dir, rest_client=rest_snapshot)
    assert s_t1.mode_used == "tier1"
    assert s_t1.cards_written == 1
    rep1_path = vault_dir / "reports" / "executive-fulfillment-scorecard.md"
    rep1 = ReportCard.model_validate(parse_markdown_card(rep1_path).frontmatter)
    assert rep1.refresh_failures_30d == 2
    assert rep1.upstream_datasets == ["[[prod.gold_ops.order_fulfillment]]"]
    # Redacted email must be omitted from owners while valid display name is kept
    assert rep1.owners == ["[[Sarah Jenkins]]"]
    assert rep1.high_excel_export is True
    assert any("backward_looking_only" in m.flagged_heuristics for m in rep1.measures)
    assert "SecretPass99" not in rep1_path.read_text(encoding="utf-8")

    # 2. Tier 2: .pbip folder (with TMDL single-line, fenced, and indented multi-line measures)
    pbip_root = tmp_path / "pbi_exports"
    tmdl_def = pbip_root / "Inventory Health.SemanticModel" / "definition"
    (tmdl_def / "tables").mkdir(parents=True)
    (pbip_root / "Inventory Health.Report").mkdir(parents=True)
    (pbip_root / "Inventory Health.pbip").write_text('{"version": "1.0"}\n', encoding="utf-8")
    (tmdl_def / "expressions.tmdl").write_text(
        'expression CatalogName = "main" meta [IsParameterQuery=true]\n',
        encoding="utf-8",
    )
    (tmdl_def / "relationships.tmdl").write_text(
        "relationship rel_1\n"
        "\tfromColumn: 'Inventory Fact'.sku_id\n"
        "\ttoColumn: 'Product Dim'.sku_id\n"
        "\tisActive: true\n",
        encoding="utf-8",
    )
    (tmdl_def / "tables" / "Inventory Fact.tmdl").write_text(
        "table 'Inventory Fact'\n"
        "\tmeasure 'Total On Hand' = SUM('Inventory Fact'[OnHandQty])\n"
        "\t\tformatString: #,0\n\n"
        "\tmeasure 'Reorder Alert' = ```\n"
        '\t\tIF([Total On Hand] < 25, "Stockout", "OK")\n'
        "\t```\n"
        "\t\tdescription: Static reorder trigger\n\n"
        "\tmeasure '3M Rolling Forecast' =\n"
        "\t\tAVERAGEX(\n"
        "\t\t\tDATESINPERIOD('Date'[Date], MAX('Date'[Date]), -3, MONTH),\n"
        "\t\t\t[Total On Hand]\n"
        "\t\t)\n"
        "\t\tlineageTag: 1111-2222\n\n"
        "\tpartition 'Inventory Fact' = m\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        '\t\t\t\tSource = Databricks.Catalogs("host", "/sql"),\n'
        '\t\t\t\tCat = Source{[Name="main",Kind="Database"]}[Data],\n'
        '\t\t\t\tSch = Cat{[Name="gold_ops",Kind="Schema"]}[Data],\n'
        '\t\t\t\tTbl = Sch{[Name="carrier_sla",Kind="Table"]}[Data]\n'
        "\t\t\tin\n"
        "\t\t\t\tTbl\n",
        encoding="utf-8",
    )

    # 3. Tier 2: DAX INFO.* UTF-8-BOM CSV exports in subfolder
    info_dir = pbip_root / "margin_pulse"
    info_dir.mkdir(parents=True)
    (info_dir / "info_tables.csv").write_bytes(
        "\ufeff[ID],[Name],[StorageMode]\n10,MarginFact,Import\n20,CustomerDim,Import\n".encode("utf-8")
    )
    (info_dir / "info_columns.csv").write_bytes(
        "\ufeff[ID],[TableID],[ExplicitName]\n101,10,CustomerKey\n201,20,CustomerKey\n".encode("utf-8")
    )
    (info_dir / "info_measures.csv").write_bytes(
        '\ufeffINFO.MEASURES()[ID],INFO.MEASURES()[TableID],INFO.MEASURES()[Name],INFO.MEASURES()[Expression],INFO.MEASURES()[Description]\n'
        '1,10,Customer Tier,"SWITCH(TRUE(), [Margin] > 50000, ""Gold"", [Margin] > 10000, ""Silver"", ""Bronze"")",Manual tiering\n'.encode(
            "utf-8"
        )
    )
    (info_dir / "info_relationships.csv").write_bytes(
        "\ufeff[FromTableID],[FromColumnID],[ToTableID],[ToColumnID],[IsActive]\n10,101,20,201,TRUE\n".encode(
            "utf-8"
        )
    )

    s_t2 = sync_powerbi(vault_dir, input_path=pbip_root, mode="tier2")
    # Exactly 2 reports (pbip was NOT double-counted via inner tmdl files!)
    assert s_t2.cards_written == 2
    assert s_t2.errors == []

    inv_card = ReportCard.model_validate(
        parse_markdown_card(vault_dir / "reports" / "inventory-health.md").frontmatter
    )
    assert inv_card.upstream_datasets == ["[[main.gold_ops.carrier_sla]]"]
    assert len(inv_card.measures) == 3
    assert len(inv_card.relationships) == 1
    m_map = {m.name: m for m in inv_card.measures}
    assert m_map["Total On Hand"].flagged_heuristics == []
    assert "static_threshold_alert" in m_map["Reorder Alert"].flagged_heuristics
    assert "naive_moving_avg_forecast" in m_map["3M Rolling Forecast"].flagged_heuristics

    mp_card = ReportCard.model_validate(
        parse_markdown_card(vault_dir / "reports" / "margin-pulse.md").frontmatter
    )
    assert len(mp_card.measures) == 1
    assert mp_card.measures[0].table == "MarginFact"
    assert "nested_heuristic_tiers" in mp_card.measures[0].flagged_heuristics
    assert mp_card.relationships[0]["from_table"] == "MarginFact"
    assert mp_card.relationships[0]["to_column"] == "CustomerKey"


# ---------------------------------------------------------------------------
# 4. Feature F08: Microsoft Teams & Context Dropzone Normalizer
# ---------------------------------------------------------------------------


def test_teams_vtt_docx_md_redaction_dedup_archive_and_corrupt_zip_resilience(
    tmp_path: Path,
) -> None:
    vault_dir = tmp_path / "vault"
    initialize_vault(vault_dir, seed_questions=True, init_git=False)

    # Mark one seeded question as answered to prove sync_teams preserves it
    update_question_or_phase(
        vault_dir,
        "Q-MGR-01",
        status="answered",
        answer_summary="90-day win is automated fulfillment stockout scoring.",
    )

    # Create a known Power BI report so StakeholderCard.owned_reports resolves strictly
    sync_powerbi(
        vault_dir,
        rest_client={
            "workspaces": [{"id": "w1", "name": "Ops"}],
            "datasets": {"w1": [{"id": "d1", "name": "Weekly Ops Deck"}]},
            "reports": {"w1": [{"id": "r1", "name": "Weekly Ops Deck", "datasetId": "d1"}]},
        },
    )

    # 1. WebVTT transcript inside `vault/inbox/teams/`
    vtt_path = vault_dir / "inbox" / "teams" / "ops_kickoff.vtt"
    vtt_path.write_text(
        "WEBVTT\n\n"
        "1\n"
        "00:00:01.000 --> 00:00:08.000\n"
        "<v Sarah Jenkins (VP Supply Chain)>Our OTIF (On-Time In-Full delivery rate) is lagging in Weekly Ops Deck and my team is exporting to Excel every Monday. Email me at sarah.j@corp.com or 555-234-5678.</v>\n\n"
        "2\n"
        "00:00:09.000 --> 00:00:16.000\n"
        "<v Sarah Jenkins (VP Supply Chain)>This is a boss mandate from the COO: can you build an automated stockout risk forecast? What is the historical stockout rate in Bronze ERP?\n",
        encoding="utf-8",
    )

    # 2. OOXML .docx inside `vault/inbox/notes/` with `<w:p>`, `<w:del>`, AND `<w:tbl>`
    docx_xml = (
        "<w:p><w:r><w:t>Marcus Vance (Director of Finance): We track GMROI closely and are struggling with hardcoded margin tiers using token dapi999988887777666655554444.</w:t></w:r>"
        "<w:del><w:r><w:t>DELETED SECRET DO_NOT_INCLUDE</w:t></w:r></w:del></w:p>"
        "<w:tbl>"
        "<w:tr>"
        "<w:tc><w:p><w:r><w:t>Term</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Definition</w:t></w:r></w:p></w:tc>"
        "</w:tr>"
        "<w:tr>"
        "<w:tc><w:p><w:r><w:t>GMROI</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Gross Margin Return on Inventory Investment</w:t></w:r></w:p></w:tc>"
        "</w:tr>"
        "</w:tbl>"
    )
    docx_path = _build_minimal_docx(
        vault_dir / "inbox" / "notes" / "finance_sync.docx", docx_xml
    )

    # 3. Corrupt .docx and XXE .docx (must warn cleanly without aborting valid files)
    bad_docx = vault_dir / "inbox" / "teams" / "corrupt.docx"
    bad_docx.write_bytes(b"not-a-valid-zip-archive")
    xxe_docx = _build_minimal_docx(
        vault_dir / "inbox" / "teams" / "xxe.docx",
        "<w:p><w:r><w:t>Hi</w:t></w:r></w:p>",
        doctype_header='<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>',
    )

    # 4. File inside `inbox/databricks/` that sync_teams MUST skip
    ignored_dbx_md = vault_dir / "inbox" / "databricks" / "README_SKIP.md"
    ignored_dbx_md.write_text("# Should never be touched by sync_teams\n", encoding="utf-8")

    s_teams = sync_teams(vault_dir)
    assert s_teams.errors == []
    assert len(s_teams.warnings) >= 2  # corrupt.docx + xxe.docx logged cleanly
    assert not vtt_path.exists()
    assert not docx_path.exists()
    assert (vault_dir / "inbox" / "processed" / "ops_kickoff.vtt").is_file()
    assert (vault_dir / "inbox" / "processed" / "finance_sync.docx").is_file()
    assert ignored_dbx_md.is_file()

    sarah_path = vault_dir / "stakeholders" / "sarah-jenkins.md"
    marcus_path = vault_dir / "stakeholders" / "marcus-vance.md"
    assert sarah_path.is_file() and marcus_path.is_file()

    sarah = StakeholderCard.model_validate(parse_markdown_card(sarah_path).frontmatter)
    assert sarah.influence_tier == "exec"
    assert sarah.owned_reports == ["[[Weekly Ops Deck]]"]
    assert "OTIF" in sarah.kpis_cared_about
    assert len(sarah.pain_points) >= 1
    assert len(sarah.boss_mandate_asks) >= 1
    assert "sarah.j@corp.com" not in sarah_path.read_text(encoding="utf-8")
    assert "555-234-5678" not in sarah_path.read_text(encoding="utf-8")

    marcus_raw = marcus_path.read_text(encoding="utf-8")
    assert "dapi99998888" not in marcus_raw
    assert "DO_NOT_INCLUDE" not in marcus_raw
    # Verify `<w:tbl>` cell text was extracted once into glossary without duplicating paragraphs
    g_card = GlossaryCard.model_validate(
        parse_markdown_card(vault_dir / "glossary.md").frontmatter
    )
    terms_map = {t.term: t for t in g_card.terms}
    assert "OTIF" in terms_map and "GMROI" in terms_map
    assert terms_map["GMROI"].source == "finance_sync.docx"
    assert terms_map["GMROI"].source_refs == ["[[marcus-vance]]"]

    q_bank = OpenQuestionBank.model_validate(
        parse_markdown_card(vault_dir / "questions" / "open-questions.md").frontmatter
    )
    # All 15 seeded questions preserved + 1 new inbox question
    assert len(q_bank.questions) == 16
    q_mgr_01 = next(q for q in q_bank.questions if q.id == "Q-MGR-01")
    assert q_mgr_01.status == "answered"
    assert "90-day win" in (q_mgr_01.answer_summary or "")

    # 5. External fixture directory copy2 test (must NOT delete external fixture!)
    ext_dir = tmp_path / "external_fixtures"
    ext_dir.mkdir()
    ext_note = ext_dir / "followup_note.md"
    ext_note.write_text(
        "## Sarah Jenkins (VP Supply Chain)\n"
        "### Pain Points\n"
        "- Safety stock buffers are manually overridden in spreadsheets.\n",
        encoding="utf-8",
    )
    s_ext = sync_teams(vault_dir, input_path=ext_dir)
    assert s_ext.errors == []
    assert ext_note.is_file()  # External fixture NOT deleted!
    assert (vault_dir / "inbox" / "processed" / "followup_note.md").is_file()

    # Strict vault validation MUST pass with ZERO errors & ZERO warnings!
    val_report = validate_vault(vault_dir, strict=True)
    assert val_report.valid is True, val_report.errors
    assert val_report.errors == []
    assert val_report.warnings == []


# ---------------------------------------------------------------------------
# 5. End-to-End Cross-Crawler HUMAN-NOTES Preservation & Lineage Graph
# ---------------------------------------------------------------------------


def test_cross_crawler_sync_all_human_notes_byte_preservation_and_lineage(
    tmp_path: Path,
) -> None:
    vault_dir = tmp_path / "vault"
    initialize_vault(vault_dir, seed_questions=True, init_git=False)

    exports_root = tmp_path / "shared_exports"
    (exports_root / "databricks").mkdir(parents=True)
    (exports_root / "powerbi").mkdir(parents=True)
    (exports_root / "teams").mkdir(parents=True)

    dbx_bundle = {
        "tables": [
            {
                "fqn": "prod.gold.demand_actuals",
                "table_type": "MANAGED",
                "row_count": 5000,
                "columns": [
                    {"name": "sku_id", "data_type": "STRING", "null_pct": 0.0, "distinct_count": 200},
                    {"name": "demand_units", "data_type": "DOUBLE", "null_pct": 0.0, "distinct_count": 410},
                ],
            }
        ]
    }
    (exports_root / "databricks" / "bundle.json").write_text(
        json.dumps(dbx_bundle), encoding="utf-8"
    )

    bim_model = {
        "name": "Demand Planning Hub",
        "workspace": "Supply Chain BI",
        "owners": ["Elena Rostova"],
        "model": {
            "tables": [
                {
                    "name": "Demand",
                    "measures": [
                        {
                            "name": "Demand Forecast",
                            "expression": [
                                "AVERAGEX(",
                                "    DATESINPERIOD('Date'[Date], MAX('Date'[Date]), -4, WEEK),",
                                "    SUM(Demand[demand_units])",
                                ")",
                            ],
                        }
                    ],
                    "partitions": [
                        {
                            "source": {
                                "expression": 'SELECT sku_id, demand_units FROM prod.gold.demand_actuals'
                            }
                        }
                    ],
                }
            ]
        },
    }
    (exports_root / "powerbi" / "demand_planning.bim").write_text(
        json.dumps(bim_model), encoding="utf-8"
    )

    (exports_root / "teams" / "planning_sync.md").write_text(
        "# Planning Discovery\n\n"
        "## Attendees\n"
        "- **Elena Rostova** (Director of Demand Planning, Supply Chain)\n\n"
        "## Glossary\n"
        "- **WMAPE**: Weighted Mean Absolute Percentage Error across SKU-week forecasts\n\n"
        "## Open Questions\n"
        "- How many weeks of promo history exist in prod.gold.demand_actuals?\n\n"
        "Elena Rostova: Demand Planning Hub has high forecast error during seasonal promotions.\n",
        encoding="utf-8",
    )

    first_run = sync_all(vault_dir, target="all", input_path=exports_root)
    assert first_run["databricks"].cards_written == 1
    assert first_run["powerbi"].cards_written == 1
    assert first_run["teams"].cards_written >= 3  # stakeholder + glossary + open-questions

    # Inject custom byte-exact HUMAN-NOTES blocks across ALL 5 card types
    sentinel_notes = {
        "dataset": (
            vault_dir / "datasets" / "prod.gold.demand_actuals.md",
            "\n  My exact dataset notes!\n\n  - Bullet with trailing spaces:   \n  - Code: `SELECT 1`  \n",
        ),
        "report": (
            vault_dir / "reports" / "demand-planning-hub.md",
            "\n### Analyst Field Notes\nConfirmed Elena's team exports this visual every Tuesday.\n\n",
        ),
        "stakeholder": (
            vault_dir / "stakeholders" / "elena-rostova.md",
            "\n1:1 rapport note — prefers concise 3-bullet Teams summaries.\n",
        ),
        "glossary": (
            vault_dir / "glossary.md",
            "\nFinance uses fiscal-month WMAPE weighting.\n",
        ),
        "questions": (
            vault_dir / "questions" / "open-questions.md",
            "\nReview open questions in Friday standup.\n",
        ),
    }

    for card_path, custom_note in sentinel_notes.values():
        raw = card_path.read_text(encoding="utf-8")
        s_idx = raw.index(HUMAN_NOTES_START) + len(HUMAN_NOTES_START)
        e_idx = raw.index(HUMAN_NOTES_END)
        mutated = raw[:s_idx] + custom_note + raw[e_idx:]
        card_path.write_text(mutated, encoding="utf-8", newline="\n")

    # Update upstream row_count & re-run sync_all
    dbx_bundle["tables"][0]["row_count"] = 9999
    (exports_root / "databricks" / "bundle.json").write_text(
        json.dumps(dbx_bundle), encoding="utf-8"
    )

    second_run = sync_all(vault_dir, target="all", input_path=exports_root)
    assert all(s.errors == [] for s in second_run.values())

    # Verify updated metadata AND byte-for-byte HUMAN-NOTES preservation on all 5 cards!
    updated_ds = DatasetCard.model_validate(
        parse_markdown_card(sentinel_notes["dataset"][0]).frontmatter
    )
    assert updated_ds.row_count == 9999

    for kind, (card_path, expected_note) in sentinel_notes.items():
        parsed = parse_markdown_card(card_path)
        assert parsed.human_notes == expected_note, f"HUMAN-NOTES altered in {kind}"

    # Verify strict vault validation & connected lineage graph
    strict_res = validate_vault(vault_dir, strict=True)
    assert strict_res.valid is True, strict_res.errors
    assert strict_res.errors == []

    graph = build_lineage_graph(vault_dir)
    edge_kinds = {e.get("relation") or e.get("type") for e in graph["edges"]}
    assert "feeds_report" in edge_kinds
    assert "owns_report" in edge_kinds


def test_mode_validation_az_cli_fallback_statement_execution_and_idempotent_resync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_dir = tmp_path / "vault"
    initialize_vault(vault_dir, seed_questions=True, init_git=False)

    # 1. Invalid mode errors
    with pytest.raises(ValueError, match="Invalid Databricks sync mode"):
        sync_databricks(vault_dir, mode="tier99")
    with pytest.raises(ValueError, match="Invalid Power BI sync mode"):
        sync_powerbi(vault_dir, mode="tier99")

    # 2. Databricks `statement_execution.execute_statement` StatementResponse adapter
    def fake_exec_stmt(statement: str, warehouse_id: str = "auto", wait_timeout: str = "30s") -> Any:
        sl = statement.lower()
        if "system.information_schema.tables" in sl:
            cols = [
                SimpleNamespace(name="table_catalog"),
                SimpleNamespace(name="table_schema"),
                SimpleNamespace(name="table_name"),
                SimpleNamespace(name="table_type"),
                SimpleNamespace(name="comment"),
                SimpleNamespace(name="last_altered"),
            ]
            rows = [["corp", "bronze", "events", "MANAGED", "Event log", "2026-11-11"]]
        elif "system.information_schema.columns" in sl:
            cols = [
                SimpleNamespace(name="table_catalog"),
                SimpleNamespace(name="table_schema"),
                SimpleNamespace(name="table_name"),
                SimpleNamespace(name="column_name"),
                SimpleNamespace(name="full_data_type"),
                SimpleNamespace(name="data_type"),
                SimpleNamespace(name="is_nullable"),
                SimpleNamespace(name="comment"),
                SimpleNamespace(name="ordinal_position"),
                SimpleNamespace(name="partition_index"),
            ]
            rows = [["corp", "bronze", "events", "event_id", "STRING", "STRING", "NO", "", 1, None]]
        elif "count(*) as row_count" in sl:
            cols = [
                SimpleNamespace(name="row_count"),
                SimpleNamespace(name="__nulls__0"),
                SimpleNamespace(name="__distinct__0"),
            ]
            rows = [[50, 0, 50]]
        else:
            cols = [SimpleNamespace(name="empty")]
            rows = []
        return SimpleNamespace(
            status=SimpleNamespace(state="SUCCEEDED", error=None),
            manifest=SimpleNamespace(schema=SimpleNamespace(columns=cols)),
            result=SimpleNamespace(data_array=rows),
        )

    stmt_client = SimpleNamespace(
        statement_execution=SimpleNamespace(execute_statement=fake_exec_stmt),
        warehouses=SimpleNamespace(list=lambda: [SimpleNamespace(id="wh-123")]),
        jobs=SimpleNamespace(list=lambda: []),
    )
    dbx_res = sync_databricks(vault_dir, client=stmt_client)
    assert dbx_res.cards_written == 1
    assert (vault_dir / "datasets" / "corp.bronze.events.md").is_file()

    # 3. Power BI `POWERBI_ACCESS_TOKEN` HTTP failure gracefully falls back to Tier 2 local export
    pbi_inbox = vault_dir / "inbox" / "powerbi"
    pbi_inbox.mkdir(parents=True, exist_ok=True)
    (pbi_inbox / "fallback_model.bim").write_text(
        json.dumps(
            {
                "name": "Fallback Ops Report",
                "model": {
                    "tables": [
                        {
                            "name": "Events",
                            "measures": [{"name": "Event Count", "expression": "COUNTROWS(Events)"}],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("POWERBI_ACCESS_TOKEN", "fake-expired-token")

    class FailingHttpClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> FailingHttpClient:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, url: str) -> Any:
            return SimpleNamespace(status_code=401, json=lambda: {})

    import poopdeck.crawlers.powerbi_ingest as pbi_mod

    monkeypatch.setattr(pbi_mod.httpx, "Client", FailingHttpClient)
    pbi_res = sync_powerbi(vault_dir, mode="auto")
    assert pbi_res.mode_used == "tier2"
    assert pbi_res.cards_written == 1
    assert pbi_res.errors == []
    assert any("HTTP 401" in w for w in pbi_res.warnings)

    # 4. Idempotent `sync_teams` re-sync (`archive_processed=False`)
    cfg_no_arch = PoopdeckConfig()
    cfg_no_arch.teams.archive_processed = False
    note_p = vault_dir / "inbox" / "notes" / "repeat_note.md"
    note_p.write_text(
        "## Priya Nair (Lead BI Developer, Data & Analytics)\n"
        "- Struggling with broken DirectQuery refresh timeouts.\n"
        "## Glossary\n"
        "- **DQ**: DirectQuery live SQL connection mode\n"
        "## Open Questions\n"
        "- Which SQL warehouse backs the DirectQuery semantic models?\n",
        encoding="utf-8",
    )
    r1 = sync_teams(vault_dir, config=cfg_no_arch)
    r2 = sync_teams(vault_dir, config=cfg_no_arch)
    assert r1.errors == [] and r2.errors == []

    priya = StakeholderCard.model_validate(
        parse_markdown_card(vault_dir / "stakeholders" / "priya-nair.md").frontmatter
    )
    assert len(priya.pain_points) == 1
    g_terms = GlossaryCard.model_validate(
        parse_markdown_card(vault_dir / "glossary.md").frontmatter
    ).terms
    assert sum(1 for t in g_terms if t.term == "DQ") == 1
    q_items = OpenQuestionBank.model_validate(
        parse_markdown_card(vault_dir / "questions" / "open-questions.md").frontmatter
    ).questions
    assert len(q_items) == 16



# ---------------------------------------------------------------------------
# Iteration 2 Regression Tests (10 Comprehensive Coverage Suites)
# ---------------------------------------------------------------------------


def test_databricks_scrubbed_samples_mode_still_requires_allow_sample_rows(
    tmp_path: Path,
) -> None:
    vault_dir = tmp_path / "vault"
    initialize_vault(vault_dir, seed_questions=False, init_git=False)
    cfg = PoopdeckConfig(
        privacy=PrivacyConfig(privacy_mode="scrubbed_samples", allow_sample_rows=False)
    )
    executed_sql: list[str] = []

    def runner(sql: str) -> list[dict[str, Any]]:
        executed_sql.append(sql)
        sl = sql.lower()
        if "system.information_schema.tables" in sl:
            return [
                {
                    "table_catalog": "main",
                    "table_schema": "gold",
                    "table_name": "shipments",
                    "table_type": "MANAGED",
                    "comment": "Gold shipments",
                    "last_altered": "2026-11-10T00:00:00Z",
                }
            ]
        if "system.information_schema.columns" in sl:
            return [
                {
                    "table_catalog": "main",
                    "table_schema": "gold",
                    "table_name": "shipments",
                    "column_name": "shipment_id",
                    "data_type": "STRING",
                    "is_nullable": "NO",
                    "comment": "PK",
                }
            ]
        if "count(*)" in sl:
            return [{"row_count": 10, "__nulls__0": 0, "__distinct__0": 10}]
        return []

    t1 = sync_databricks(vault_dir, mode="tier1", config=cfg, sql_runner=runner)
    assert t1.errors == []
    assert not any("select *" in q.lower() for q in executed_sql)
    c1 = DatasetCard.model_validate(
        parse_markdown_card(vault_dir / "datasets" / "main.gold.shipments.md").frontmatter
    )
    assert c1.sample_rows == []

    bundle_path = tmp_path / "t2_bundle.json"
    bundle_path.write_text(
        json.dumps(
            {
                "tables": [
                    {
                        "fqn": "main.gold.shipments",
                        "columns": [{"name": "shipment_id", "data_type": "STRING"}],
                        "sample_rows": [{"shipment_id": "SHP-1"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    t2 = sync_databricks(vault_dir, mode="tier2", input_path=bundle_path, config=cfg)
    assert t2.errors == []
    c2 = DatasetCard.model_validate(
        parse_markdown_card(vault_dir / "datasets" / "main.gold.shipments.md").frontmatter
    )
    assert c2.sample_rows == []


def test_databricks_timestamp_dtype_warning_redaction_and_pre_str_types(
    tmp_path: Path,
) -> None:
    class ColKind(Enum):
        DECIMAL_18 = "DECIMAL(18,2)"

    vault_dir = tmp_path / "vault"
    initialize_vault(vault_dir, seed_questions=False, init_git=False)

    def runner(sql: str) -> list[dict[str, Any]]:
        sl = sql.lower()
        if "system.information_schema.tables" in sl:
            return [
                {
                    "table_catalog": "main",
                    "table_schema": "fin",
                    "table_name": "ledger",
                    "table_type": "MANAGED",
                    "comment": "Refs [[Unresolved Ghost Table]]",
                }
            ]
        if "system.information_schema.columns" in sl:
            return [
                {
                    "table_catalog": "main",
                    "table_schema": "fin",
                    "table_name": "ledger",
                    "column_name": "acct_code",
                    "data_type": b"VARCHAR(32)",
                    "is_nullable": "NO",
                    "comment": "Account code",
                },
                {
                    "table_catalog": "main",
                    "table_schema": "fin",
                    "table_name": "ledger",
                    "column_name": "amount",
                    "data_type": ColKind.DECIMAL_18,
                    "is_nullable": "YES",
                    "comment": "type pwd=TopSecret123 email a@b.com",
                },
                {
                    "table_catalog": "main",
                    "table_schema": "fin",
                    "table_name": "ledger",
                    "column_name": "updated_at",
                    "data_type": "STRUCT<tok:STRING> dapi1234567890abcdef1234567890ab",
                    "is_nullable": "YES",
                    "comment": "ts",
                },
            ]
        if "table_lineage" in sl:
            raise RuntimeError(
                "Lineage failed: pwd=LeakPass999 eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig9999999999"
            )
        if "count(*)" in sl:
            return [
                {
                    "row_count": 5,
                    "min_timestamp": "bad-ts alice@corp.com 123-45-6789",
                    "max_timestamp": "bad-ts +1 (555) 234-5678 pwd=InlineSecret",
                    "__nulls__0": 0,
                    "__distinct__0": 5,
                    "__nulls__1": 1,
                    "__distinct__1": 4,
                }
            ]
        return []

    res = sync_databricks(vault_dir, mode="tier1", sql_runner=runner)
    assert res.errors == []
    joined_warns = " ".join(res.warnings)
    assert "LeakPass999" not in joined_warns and "eyJhbGci" not in joined_warns
    card_path = vault_dir / "datasets" / "main.fin.ledger.md"
    raw_text = card_path.read_text(encoding="utf-8")
    for forbidden in (
        "TopSecret123",
        "dapi1234567890",
        "alice@corp.com",
        "123-45-6789",
        "555) 234-5678",
        "InlineSecret",
        "b'VARCHAR",
        "ColKind.",
    ):
        assert forbidden not in raw_text
    card = DatasetCard.model_validate(parse_markdown_card(card_path).frontmatter)
    dtypes = {c.name: c.data_type for c in card.columns}
    assert dtypes["acct_code"] == "VARCHAR(32)"
    assert dtypes["amount"] == "DECIMAL(18,2)"
    assert validate_vault(vault_dir, strict=True).valid is True


def test_databricks_corrupt_csv_isolation_dict_failed_hive_fallback_and_tuple_sql_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_dir = tmp_path / "vault"
    initialize_vault(vault_dir, seed_questions=False, init_git=False)
    bdir = tmp_path / "bundle"
    bdir.mkdir()
    (bdir / "tables.csv").write_text("fqn,table_type\nmain.ops.bad\x00tbl,MANAGED\n", encoding="utf-8")
    (bdir / "columns.csv").write_text(
        'fqn,column_name,data_type,comment\nmain.ops.good_tbl,id,STRING,"' + ("D" * 150_000) + '"\n',
        encoding="utf-8",
    )
    (bdir / "good.json").write_text(
        json.dumps({"tables": [{"fqn": "main.ops.good_tbl", "columns": [{"name": "id", "data_type": "STRING"}]}]}),
        encoding="utf-8",
    )
    r_csv = sync_databricks(vault_dir, mode="tier2", input_path=bdir)
    assert r_csv.errors == [] and r_csv.cards_written == 1 and r_csv.warnings

    def stmt_exec(statement: str, **_kw: Any) -> dict[str, Any]:
        sl = statement.lower()
        if "system.information_schema" in sl:
            return {"status": {"state": "FAILED", "error": {"message": "UC denied"}}}
        if "show databases" in sl:
            return {"rows": [{"databaseName": "ops"}]}
        if "show tables" in sl:
            return {"rows": [{"database": "ops", "tableName": "hive_orders", "isTemporary": False}]}
        if "describe extended" in sl:
            return {"rows": [{"col_name": "order_id", "data_type": "string", "comment": "pk"}]}
        return {"rows": [{"row_count": 7, "__nulls__0": 0, "__distinct__0": 7}]}

    client = SimpleNamespace(statement_execution=SimpleNamespace(execute_statement=stmt_exec))
    r_hive = sync_databricks(vault_dir, mode="tier1", client=client)
    assert r_hive.errors == [] and (vault_dir / "datasets" / "hive_metastore.ops.hive_orders.md").exists()

    recent_iso = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tuple_runner = lambda sql: (
        [("main", "gold", "kpi", "MANAGED", "KPIs", recent_iso)]
        if "system.information_schema.tables" in sql.lower()
        else [("main", "gold", "kpi", "ts", "TIMESTAMP", "TIMESTAMP", "NO", "", 1, None), ("main", "gold", "kpi", "val", "DOUBLE", "DOUBLE", "YES", "", 2, None)]
        if "system.information_schema.columns" in sql.lower()
        else [(20, "2026-11-01T00:00:00Z", recent_iso, 0, 20, 5, 15)]
        if "count(*)" in sql.lower()
        else []
    )
    assert sync_databricks(vault_dir, mode="tier1", sql_runner=tuple_runner).errors == []
    kpi = DatasetCard.model_validate(parse_markdown_card(vault_dir / "datasets" / "main.gold.kpi.md").frontmatter)
    val_col = next(c for c in kpi.columns if c.name == "val")
    assert kpi.max_timestamp is not None and kpi.freshness_lag_hours is not None
    assert val_col.null_pct == 25.0 and val_col.distinct_count == 15

    (vault_dir / "inbox" / "databricks" / "fb.json").write_text(
        json.dumps({"tables": [{"fqn": "main.ops.auto_fb", "columns": [{"name": "id", "data_type": "INT"}]}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABRICKS_HOST", "https://adb-offline.azuredatabricks.net")
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi0123456789abcdef0123456789ab")
    r_auto = sync_databricks(vault_dir, mode="auto")
    assert r_auto.errors == [] and r_auto.mode_used == "tier2" and r_auto.cards_written == 1 and r_auto.warnings


def test_zero_admin_notebook_redos_80k_budget_unclosed_secrets_nan_decimal_and_1col_hive() -> None:
    for payload in ("a." * 40_000, "-----BEGIN PRIVATE KEY-----x\n" * 2_500):
        t0 = time.perf_counter()
        zero_admin_nb.scrub_text_secrets_and_pii(payload)
        assert (time.perf_counter() - t0) < 0.10
    scrubbed = zero_admin_nb.scrub_text_secrets_and_pii(
        "conn pwd='SecretLeak123\n-----BEGIN RSA PRIVATE KEY-----MIICXAIBAAKBgQ"
    )
    assert "SecretLeak123" not in scrubbed and "MIICXAIBAAKBgQ" not in scrubbed
    assert zero_admin_nb._sanitize_json_value(Decimal("1e5000")) is None

    def spark_sql(sql: str) -> Any:
        sl = sql.lower()
        if "system.information_schema" in sl:
            raise RuntimeError("UC disabled")
        if "show databases" in sl:
            return [("ops",)]
        if "show tables" in sl:
            return [("orders",)]
        if "describe extended" in sl:
            return [("amt", "double", "")]
        return [{"row_count": 10, "__nulls__0": float("nan"), "__distinct__0": 4}]

    bundle = zero_admin_nb.build_discovery_bundle(SimpleNamespace(sql=spark_sql))
    assert bundle["tables"][0]["fqn"] == "hive_metastore.ops.orders"
    assert bundle["tables"][0]["columns"][0].get("null_pct") is None


def test_dax_heuristics_80k_redos_budget_and_measure_only_backward_looking() -> None:
    for adv_expr in ("[p10 " * 16_000, 'SWITCH(IF("' + ("critical " * 8_885)):
        t0 = time.perf_counter()
        detect_dax_heuristics("Stress Check", adv_expr)
        assert (time.perf_counter() - t0) < 0.10
    assert detect_dax_heuristics("Actuals vs Budget", "[Actuals] - [Budget]") == [
        "backward_looking_only"
    ]


def test_powerbi_m_catalog_database_params_and_windows_backslash_safety(
    tmp_path: Path,
) -> None:
    vault_dir = tmp_path / "vault"
    initialize_vault(vault_dir, seed_questions=False, init_git=False)

    bim_path = tmp_path / "win_cat.bim"
    bim_path.write_text(
        json.dumps(
            {
                "model": {
                    "expressions": [
                        {"name": "P_Cat", "expression": '"prod" meta [IsParameterQuery=true]'},
                        {"name": "P_Db", "expression": '"prod"'},
                        {"name": "P_Sch", "expression": '"gold_ops"'},
                        {"name": "P_Win", "expression": r'"C:\spark\warehouse"'},
                        {"name": "P_User", "expression": r'"CORP\svc_pbi"'},
                    ],
                    "tables": [
                        {
                            "name": "Orders",
                            "partitions": [
                                {
                                    "source": {
                                        "type": "m",
                                        "expression": (
                                            'let p = P_Win, u = P_User, s = Databricks.Catalogs("h", "/p", '
                                            '[Catalog=P_Cat, Database=P_Db]), '
                                            'db = s{[Name=P_Db,Kind="Database"]}[Data], '
                                            'sc = db{[Name=P_Sch,Kind="Schema"]}[Data], '
                                            't = sc{[Name="orders",Kind="Table"]}[Data] in t'
                                        ),
                                    }
                                }
                            ],
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    r_bim = sync_powerbi(vault_dir, mode="tier2", input_path=bim_path)
    assert r_bim.errors == []
    c_bim = ReportCard.model_validate(parse_markdown_card(Path(r_bim.written_paths[0])).frontmatter)
    assert c_bim.upstream_datasets == ["[[prod.gold_ops.orders]]"]

    tmdl_dir = tmp_path / "tmdl_pkg" / "definition"
    (tmdl_dir / "tables").mkdir(parents=True)
    (tmdl_dir / "expressions.tmdl").write_text(
        'expression WinRoot = "C:\\spark\\warehouse"\nexpression SvcAcct = "CORP\\svc_pbi"\nexpression CatP = "prod"\n',
        encoding="utf-8",
    )
    (tmdl_dir / "tables" / "Orders.tmdl").write_text(
        "table Orders\n\tpartition Orders = m\n\t\tsource =\n\t\t\tlet r = WinRoot, u = SvcAcct, "
        's = Databricks.Catalogs("h", "/p", [Catalog=CatP]), '
        'sc = s{[Name="gold_ops",Kind="Schema"]}[Data], t = sc{[Name="orders",Kind="Table"]}[Data] in t\n',
        encoding="utf-8",
    )
    assert sync_powerbi(vault_dir, mode="tier2", input_path=tmdl_dir.parent).errors == []

    rest_mock = {
        "workspaces": [{"id": "w1", "name": "Ops"}],
        "datasets": {
            "w1": [
                {
                    "id": "d1",
                    "name": "WinRest",
                    "expressions": [{"name": "WinP", "expression": r'"C:\spark\warehouse\CORP\svc_pbi"'}],
                    "tables": [{"name": "Orders", "partitions": [{"source": {"expression": "let x = WinP in SELECT * FROM prod.gold_ops.orders"}}]}],
                }
            ]
        },
        "reports": {"w1": [{"id": "r1", "name": "Win Rest Report", "datasetId": "d1"}]},
    }
    assert sync_powerbi(vault_dir, mode="tier1", rest_client=rest_mock).errors == []


def test_powerbi_cross_run_workspace_disambiguation_nan_rels_bad_int_and_duck_404(
    tmp_path: Path,
) -> None:
    vault_dir = tmp_path / "vault"
    initialize_vault(vault_dir, seed_questions=False, init_git=False)

    ops_client = {
        "workspaces": [{"id": "w_ops", "name": "Ops"}],
        "datasets": {"w_ops": [{"id": "d_ops", "name": "OpsModel", "relationships": [{"from": "Orders.Id", "weight": float("nan")}]}]},
        "reports": {"w_ops": [{"id": "r_ops", "name": "Executive Summary", "datasetId": "d_ops", "refresh_failures_30d": "N/A"}]},
    }
    fin_client = {
        "workspaces": [{"id": "w_fin", "name": "Finance"}],
        "datasets": {"w_fin": [{"id": "d_fin", "name": "FinModel"}]},
        "reports": {"w_fin": [{"id": "r_fin", "name": "Executive Summary", "datasetId": "d_fin", "refresh_failures_30d": True}]},
    }
    assert sync_powerbi(vault_dir, mode="tier1", rest_client=ops_client).errors == []
    assert sync_powerbi(vault_dir, mode="tier1", rest_client=fin_client).errors == []
    report_files = sorted((vault_dir / "reports").glob("*.md"))
    assert len(report_files) == 2
    cards = [ReportCard.model_validate(parse_markdown_card(p).frontmatter) for p in report_files]
    assert {c.workspace for c in cards} == {"Ops", "Finance"}
    assert all(c.refresh_failures_30d == 0 for c in cards)

    duck_client = SimpleNamespace(
        list_workspaces=lambda: [{"id": "w_dk", "name": "Supply"}],
        list_datasets=lambda _w: [{"id": "d_dk", "name": "SupplyModel"}],
        list_reports=lambda _w: [{"id": "r_dk", "name": "Duck 404 Check", "datasetId": "d_dk"}],
        get_refresh_schedule=lambda *_a: (_ for _ in ()).throw(RuntimeError("HTTP 404")),
        list_datasources=lambda *_a: (_ for _ in ()).throw(RuntimeError("HTTP 404")),
        list_refreshes=lambda *_a: [],
    )
    r_duck = sync_powerbi(vault_dir, mode="tier1", rest_client=duck_client)
    assert r_duck.errors == [] and r_duck.cards_written == 1


def test_sync_all_single_file_isolation_bad_teams_mode_and_auto_az_offline_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_dir = tmp_path / "vault"
    initialize_vault(vault_dir, seed_questions=False, init_git=False)
    inbox_note = vault_dir / "inbox" / "notes" / "untouched.md"
    inbox_note.write_text("## Alex Rivera (VP, Ops)\n- Waiting on forecast.\n", encoding="utf-8")

    ext_bim = tmp_path / "standalone.bim"
    ext_bim.write_text(
        json.dumps({"model": {"tables": [{"name": "Sales", "measures": [{"name": "Rev", "expression": "SUM(Sales[Amt])"}]}]}}),
        encoding="utf-8",
    )
    res = sync_all(vault_dir, input_path=ext_bim)
    assert res["powerbi"].cards_written == 1 and res["teams"].cards_written == 0
    assert inbox_note.exists()

    with pytest.raises(ValueError):
        sync_all(vault_dir, target="teams", mode="bad_mode")

    empty_vault = tmp_path / "empty_vault"
    initialize_vault(empty_vault, seed_questions=False, init_git=False)
    monkeypatch.setattr(pbi_mod, "_acquire_az_cli_token", lambda: (None, "Azure CLI token unavailable (offline)"))
    r_az = sync_powerbi(empty_vault, mode="auto")
    assert r_az.errors == [] and any("Azure CLI" in w for w in r_az.warnings)


def test_teams_docx_expat_xxe_rejection_across_utf16_utf32_and_4kb_padding(
    tmp_path: Path,
) -> None:
    def _write_raw_docx(name: str, xml_bytes: bytes) -> Path:
        p = tmp_path / name
        with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
            zf.writestr("word/document.xml", xml_bytes)
        return p

    pad = "<!--" + ("P" * 6200) + "-->"
    evil_tpl = (
        '<?xml version="1.0" encoding="{enc}"?>'
        f'{pad}<!DOCTYPE foo [<!ENTITY xxe "leak">]>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>'
    )
    for idx, (py_enc, decl_enc) in enumerate(
        [("utf-16", "UTF-16"), ("utf-16-le", "UTF-16LE"), ("utf-16-be", "UTF-16BE"), ("utf-32", "UTF-32"), ("utf-8", "UTF-8")]
    ):
        bad_p = _write_raw_docx(f"xxe_{idx}.docx", evil_tpl.format(enc=decl_enc).encode(py_enc))
        with pytest.raises(ValueError, match="Unsafe XML DOCTYPE/ENTITY declaration in .docx"):
            teams_mod._parse_docx_file(bad_p, redact_secrets=True)

    good_xml = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>Sarah Chen (VP Ops): OTIF check.</w:t></w:r></w:p></w:body></w:document>"
    ).encode("utf-16")
    parsed = teams_mod._parse_docx_file(_write_raw_docx("good_u16.docx", good_xml), redact_secrets=True)
    assert parsed["turns"] and parsed["turns"][0][0] == "Sarah Chen"


def test_teams_markdown_hr_vtt_regional_cue_scalar_fm_pii_filename_and_prose_wikilinks(
    tmp_path: Path,
) -> None:
    vault_dir = tmp_path / "vault"
    initialize_vault(vault_dir, seed_questions=True, init_git=False)

    bim_p = tmp_path / "ops_deck.bim"
    bim_p.write_text(
        json.dumps({"model": {"name": "Weekly Ops Deck", "tables": [{"name": "Ops", "measures": [{"name": "OTIF", "expression": "SUM(Ops[OK])"}]}]}}),
        encoding="utf-8",
    )
    assert sync_powerbi(vault_dir, mode="tier2", input_path=bim_p).errors == []

    (vault_dir / "inbox" / "notes" / "notes_alice@corp.com.md").write_text(
        "---\nquestions: 42\nglossary: true\nstakeholders: 99\n---\n"
        "# Meeting Notes\n\n---\n\n"
        "## Sarah Chen (VP Ops, Supply Chain)\n"
        "- Struggling with [[Weekly Ops Deck]], [[Legacy Freight Sheet]], and [[Missing|Alias]].\n\n---\n\n"
        "## Glossary\n- **OTIF**: On-Time In-Full delivery\n",
        encoding="utf-8",
    )
    (vault_dir / "inbox" / "notes" / "bad_cat.md").write_text(
        '---\nquestions:\n  - question: "Who owns carrier SLA penalties?"\n    category: bogus_cat\n---\n',
        encoding="utf-8",
    )
    (vault_dir / "inbox" / "teams" / "ops_call.vtt").write_text(
        "WEBVTT\n\n00:00:01.000 --> 00:00:06.000\n<v Sarah Chen (VP Ops)>\n"
        "Regional DC stockouts spiked last week.\nNote that broken OTIF alerts failed.\n",
        encoding="utf-8",
    )

    res = sync_teams(vault_dir)
    assert res.errors == []
    sh_dir = vault_dir / "stakeholders"
    assert (sh_dir / "sarah-chen.md").exists() and not (sh_dir / "meeting-notes.md").exists()
    sarah_raw = (sh_dir / "sarah-chen.md").read_text(encoding="utf-8")
    assert "[[Weekly Ops Deck]]" in sarah_raw
    assert "[[Legacy Freight Sheet]]" not in sarah_raw and "Legacy Freight Sheet" in sarah_raw
    assert "[[Missing|Alias]]" not in sarah_raw and "Alias" in sarah_raw
    assert "Regional DC stockouts" in sarah_raw and "Note that broken OTIF alerts failed." in sarah_raw
    assert "alice@corp.com" not in (vault_dir / "glossary.md").read_text(encoding="utf-8")

    v_rep = validate_vault(vault_dir, strict=True)
    assert v_rep.valid is True and v_rep.errors == [] and v_rep.warnings == []


# ---------------------------------------------------------------------------
# Iteration 3 Regression Tests (#22 - #25)
# ---------------------------------------------------------------------------


def test_windows_reserved_dos_stems_and_git_add_across_crawlers(tmp_path: Path) -> None:
    v = tmp_path / "vault"
    initialize_vault(v, seed_questions=False, init_git=True)
    bundle = tmp_path / "dos_dbx.json"
    bundle.write_text(
        json.dumps(
            {
                "tables": [
                    {"fqn": "CON.ops.orders", "columns": [{"name": "id", "data_type": "STRING"}]},
                    {"fqn": "NUL.ops.orders", "columns": [{"name": "id", "data_type": "STRING"}]},
                ]
            }
        ),
        encoding="utf-8",
    )
    assert sync_databricks(v, mode="tier2", input_path=bundle).errors == []
    con_ds = v / "datasets" / "_CON.ops.orders.md"
    assert con_ds.is_file() and (v / "datasets" / "_NUL.ops.orders.md").is_file()
    raw_ds = con_ds.read_text(encoding="utf-8").replace(
        f"{HUMAN_NOTES_START}\n", f"{HUMAN_NOTES_START}\nKeep DOS note!\n", 1
    )
    con_ds.write_text(raw_ds, encoding="utf-8")
    assert sync_databricks(v, mode="tier2", input_path=bundle).errors == []
    assert "Keep DOS note!" in con_ds.read_text(encoding="utf-8")

    con_bim = tmp_path / "CON.bim"
    con_bim.write_text(
        json.dumps(
            {
                "model": {
                    "tables": [
                        {
                            "name": "Orders",
                            "partitions": [{"source": {"expression": "SELECT * FROM CON.ops.orders"}}],
                            "measures": [{"name": "Cnt", "expression": "COUNTROWS(Orders)"}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    assert sync_powerbi(v, mode="tier2", input_path=con_bim).errors == []
    assert (v / "reports" / "con-card.md").is_file()
    assert "[[CON.ops.orders]]" in (v / "reports" / "con-card.md").read_text(encoding="utf-8")

    (v / "inbox" / "notes" / "CON.md").write_text(
        "Con (VP Ops): Struggling with stockouts on OTIF.\n", encoding="utf-8"
    )
    assert sync_teams(v).errors == []
    assert (v / "stakeholders" / "con-card.md").is_file()
    assert (v / "inbox" / "processed" / "_CON.md").is_file()
    is_res = getattr(os.path, "isreserved", lambda _: False)
    assert not any(is_res(p.name) for p in v.rglob("*") if ".git" not in p.parts)
    assert validate_vault(v, strict=True).valid is True
    git_env = {k: val for k, val in os.environ.items() if not k.startswith("GIT_")}
    proc = subprocess.run(["git", "-C", str(v), "add", "."], env=git_env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_databricks_failed_object_response_fallback_and_nested_sample_wikilinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_exec(warehouse_id: str, statement: str, **_: Any) -> Any:
        su = statement.strip().upper()
        if "INFORMATION_SCHEMA" in su:
            return SimpleNamespace(
                status=SimpleNamespace(
                    state="FAILED",
                    error=SimpleNamespace(message="403 dapi0123456789abcdef0123456789"),
                )
            )
        if su == "SHOW DATABASES":
            cols, rows = [SimpleNamespace(name="databaseName")], [["silver_ops"]]
        elif su == "SHOW TABLES IN SILVER_OPS":
            cols = [SimpleNamespace(name="database"), SimpleNamespace(name="tableName"), SimpleNamespace(name="isTemporary")]
            rows = [["silver_ops", "orders", False]]
        elif "DESCRIBE" in su and "SILVER_OPS.ORDERS" in su:
            cols = [SimpleNamespace(name="col_name"), SimpleNamespace(name="data_type"), SimpleNamespace(name="comment")]
            rows = [["order_id", "string", "PK"]]
        else:
            cols, rows = [SimpleNamespace(name="c")], []
        return SimpleNamespace(
            status=SimpleNamespace(state="SUCCEEDED", error=None),
            manifest=SimpleNamespace(schema=SimpleNamespace(columns=cols)),
            result=SimpleNamespace(data_array=rows),
        )

    v1 = tmp_path / "v1"
    initialize_vault(v1, seed_questions=False, init_git=False)
    client = SimpleNamespace(
        statement_execution=SimpleNamespace(execute_statement=fake_exec),
        warehouses=SimpleNamespace(list=lambda: [SimpleNamespace(id="wh-1")]),
        jobs=SimpleNamespace(list=lambda: []),
    )
    res1 = sync_databricks(v1, client=client)
    assert res1.cards_written == 1 and res1.errors == []
    assert not any("dapi012345" in w for w in res1.warnings)

    def _boom_spec(_name: str) -> Any:
        raise ImportError("broken meta_path")

    monkeypatch.setattr(importlib.util, "find_spec", _boom_spec)
    assert dbx_mod._has_databricks_sdk() is False

    v2 = tmp_path / "v2"
    initialize_vault(v2, seed_questions=False, init_git=False)
    b2 = tmp_path / "nested_samples.json"
    b2.write_text(
        json.dumps(
            {
                "tables": [
                    {
                        "fqn": "main.ops.nested",
                        "columns": [{"name": "payload", "data_type": "STRING"}],
                        "sample_rows": [{"[[k]]": {"ref": "[[ghost]]"}, "arr": [["[[g2]]"]]}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert sync_databricks(v2, mode="tier2", input_path=b2, allow_sample_rows=True).errors == []
    assert validate_vault(v2, strict=True).valid is True


def test_linear_100k_redos_budgets_and_unclosed_dax_comment_stripping() -> None:
    assert detect_dax_heuristics("Clean", 'SUM(Sales[Rev]) /* SWITCH(TRUE(), [X]>5, "Critical")') == []
    cases: list[tuple[str, Any]] = [
        ("dax_unclosed_cmt", lambda: detect_dax_heuristics("M", "/* " * 40_000)),
        ("dax_weighted_plus", lambda: detect_dax_heuristics("M", ("A" * 500 + " * ") * 160)),
        ("pbi_sql_ident", lambda: pbi_mod._extract_upstream_fqns_from_expression("databricks " + "FROM [a " * 15_000)),
        ("pbi_tmdl_expr", lambda: pbi_mod._parse_expressions_tmdl_text("\n" * 60_000 + "x")),
        ("vtt_unclosed_v", lambda: teams_mod._parse_vtt_content("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n" + "<v A>x " * 14_300, True)),
        ("vtt_lead_close_v", lambda: teams_mod._parse_vtt_content("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n</v>" + "<v A>x " * 14_300, True)),
        ("vtt_open_space", lambda: teams_mod._parse_vtt_content("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v " + " " * 100_000, True)),
        ("vtt_inline_tags", lambda: teams_mod._parse_vtt_content("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n" + "<b " * 33_334, True)),
        ("teams_paren", lambda: teams_mod._split_speaker_and_role("A" + " " * 50_000 + "(" + "a" * 50_000)),
        ("teams_md_bullets", lambda: teams_mod._extract_entities_from_doc(teams_mod._parse_markdown_or_text("- **Term" + "x" * 100_000 + "\n", True), "n.md", {}, {}, {}, [])),
    ]
    for label, fn in cases:
        t0 = time.perf_counter()
        fn()
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.25, f"{label} took {elapsed:.3f}s"


def test_underscore_filename_pii_scrubbing_hr_speaker_and_vtt_note_voice(tmp_path: Path) -> None:
    sample = "john.doe@corp.com_notes id_123-45-6789_v1 tok_dapi0123456789abcdef0123456789_end"
    for fn in (redact_crawler_text, zero_admin_nb.scrub_text_secrets_and_pii):
        out = fn(sample)
        assert "john.doe@corp.com" not in out and "123-45-6789" not in out and "dapi012345" not in out
        t0 = time.perf_counter()
        fn("safe_ident_123_col " * 6_000)
        assert (time.perf_counter() - t0) < 0.20

    v = tmp_path / "vault"
    initialize_vault(v, seed_questions=False, init_git=False)
    (v / "inbox" / "notes" / "john.doe@corp.com_notes.md").write_text(
        "---\nElena: Struggling with stockouts on OTIF!\n---\n## Glossary\n- **FillRate**: Shipped / Ordered\n",
        encoding="utf-8",
    )
    (v / "inbox" / "notes" / "fm.md").write_text(
        "---\ntitle: Sync\nparticipants:\n  - Dana Park (Director, Finance)\n---\nDana Park: Need forecast.\n",
        encoding="utf-8",
    )
    (v / "inbox" / "teams" / "call.vtt").write_text(
        "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\n<v Note (BI Analyst)>Struggling with broken refresh?</v>\n"
        "Note: skip colon label\n<v Unknown>skip generic</v>\n",
        encoding="utf-8",
    )
    (v / "inbox" / "teams" / "john.doe@corp.com_bad.docx").write_bytes(b"not-a-zip")

    t_res = sync_teams(v)
    assert (v / "stakeholders" / "elena.md").is_file()
    assert (v / "stakeholders" / "dana-park.md").is_file()
    assert (v / "stakeholders" / "note.md").is_file()
    assert not (v / "stakeholders" / "unknown.md").exists()
    assert "john.doe@corp.com" not in json.dumps(t_res.model_dump())
    g_raw = (v / "glossary.md").read_text(encoding="utf-8")
    assert "john.doe@corp.com" not in g_raw and "[REDACTED:" not in g_raw

    pbi_dir = tmp_path / "pbi_in"
    pbi_dir.mkdir()
    bim_body = json.dumps({"model": {"tables": [{"name": "T", "measures": [{"name": "M", "expression": "SUM(T[X])"}]}]}})
    (pbi_dir / "john.doe@corp.com_report.bim").write_text(bim_body, encoding="utf-8")
    (pbi_dir / "john.doe@corp.com.bim").write_text(bim_body, encoding="utf-8")
    p_res = sync_powerbi(v, mode="tier2", input_path=pbi_dir)
    assert (v / "reports" / "report.md").is_file()
    assert (v / "reports" / "powerbi-report.md").is_file()
    assert "john.doe@corp.com" not in json.dumps(p_res.model_dump())
    for md in v.rglob("*.md"):
        txt = md.read_text(encoding="utf-8")
        assert "john.doe@corp.com" not in txt
    assert "[REDACTED:" not in (v / "reports" / "report.md").read_text(encoding="utf-8")
    assert "[REDACTED:" not in (v / "reports" / "powerbi-report.md").read_text(encoding="utf-8")
    assert validate_vault(v, strict=True).valid is True


# ---------------------------------------------------------------------------
# Iteration 4 Regression Tests (#26 - #27)
# ---------------------------------------------------------------------------


def test_26_prose_wikilink_linear_perf_and_deep_strict_vault(tmp_path: Path) -> None:
    v = tmp_path / "vault"
    initialize_vault(v, seed_questions=False, init_git=False)
    (v / "inbox" / "databricks").mkdir(parents=True, exist_ok=True)
    (v / "inbox" / "powerbi").mkdir(parents=True, exist_ok=True)
    (v / "inbox" / "notes").mkdir(parents=True, exist_ok=True)
    (v / "reports" / "known.md").write_text(
        f"---\nid: Known\ntitle: Known\nworkspace: WS\nsemantic_model: Known\nupdated_at: 2026-03-01\n---\n{HUMAN_NOTES_START}\n{HUMAN_NOTES_END}\n",
        encoding="utf-8",
    )

    open_120k = "[" * 120_000
    semi_closed_120k = ("[" * 120_000) + "a]]"
    for fn in (
        unwrap_prose_wikilinks,
        dbx_mod._sanitize_prose_wikilinks,
        pbi_mod._unwrap_unresolved_wikilinks,
        teams_mod._sanitize_prose_wikilinks,
    ):
        t0 = time.perf_counter()
        assert fn(open_120k, {"known"}) == open_120k
        out_semi = fn(semi_closed_120k, {"known"})
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.10, f"{fn.__name__} took {elapsed:.4f}s"
        assert "]]" not in out_semi

    # Linearize helper stress checks (< 100 ms each at 120k chars / 4k items)
    t_h0 = time.perf_counter()
    pbi_mod._clean_redacted_identifier("a" + ("_" * 60_000) + "a [REDACTED:EMAIL] b", "fb")
    teams_mod._clean_redacted_label("a" + ("_" * 60_000) + "a [REDACTED:EMAIL] b", "fb")
    zero_admin_nb._normalize_col_identifier("A" * 120_000)
    acc_list: list[str] = []
    for i in range(4_000):
        teams_mod._dedup_append(acc_list, f"item {i % 2000}")
    assert len(acc_list) == 2_000
    assert (time.perf_counter() - t_h0) < 0.10

    deep12 = ("[[a " * 12) + "[[Known]] [[Ghost|Alias]]" + (" b]]" * 12)
    deep20k = ("[[a " * 20_000) + "[[Known]] [[Ghost]]" + (" b]]" * 20_000) + " [[[[Known]]]] []][Ghost2][[]"

    t_d0 = time.perf_counter()
    unwrapped_20k = unwrap_prose_wikilinks(deep20k, {"known"})
    assert (time.perf_counter() - t_d0) < 0.10
    assert "[[Known]]" in unwrapped_20k
    assert "[[[[Known]]]]" not in unwrapped_20k
    assert "[[Ghost" not in unwrapped_20k

    (v / "inbox" / "databricks" / "b.json").write_text(
        json.dumps(
            {
                "tables": [
                    {
                        "fqn": "main.ops.t1",
                        "catalog": "main",
                        "schema_name": "ops",
                        "table_name": "t1",
                        "comment": deep20k,
                        "columns": [{"name": "c1", "data_type": "STRING", "comment": deep12}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (v / "inbox" / "powerbi" / "m.bim").write_text(
        json.dumps(
            {
                "name": "PBI1",
                "model": {
                    "tables": [
                        {
                            "name": "T",
                            "measures": [
                                {"name": "M", "expression": "SUM(T[X])", "description": deep20k}
                            ],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    (v / "inbox" / "notes" / "n.md").write_text(
        f"Ana Silva (VP Ops): We have manual stockout {deep12} and [[[[Known]]]].\n",
        encoding="utf-8",
    )

    cfg = PoopdeckConfig.model_validate({"databricks": {"catalogs_include": ["*"]}})
    assert sync_databricks(v, config=cfg).errors == []
    assert sync_powerbi(v, config=cfg).errors == []
    assert sync_teams(v, config=cfg).errors == []

    ds_txt = (v / "datasets" / "main.ops.t1.md").read_text(encoding="utf-8")
    assert "[[Known]]" in ds_txt and "Alias" in ds_txt
    rep = validate_vault(v, strict=True)
    assert rep.valid is True, rep.errors


def test_27_pii_collision_disambiguation_preserves_cards_and_human_notes(tmp_path: Path) -> None:
    v = tmp_path / "vault"
    initialize_vault(v, seed_questions=False, init_git=False)
    pbi_dir = v / "inbox" / "powerbi"
    dbx_dir = v / "inbox" / "databricks"
    notes_dir = v / "inbox" / "notes"
    for d in (pbi_dir, dbx_dir, notes_dir):
        d.mkdir(parents=True, exist_ok=True)

    for fn, expr in (
        ("alice@corp.com.bim", "SUM(S[A1])"),
        ("bob@corp.com.bim", "SUM(S[B1])"),
        ("alice@corp.com_ops.bim", "SUM(S[A2])"),
        ("bob@corp.com_ops.bim", "SUM(S[B2])"),
    ):
        (pbi_dir / fn).write_text(
            json.dumps({"model": {"tables": [{"name": "S", "measures": [{"name": "M", "expression": expr}]}]}}),
            encoding="utf-8",
        )

    raw_dbx_bundle = {
        "tables": [
            {
                "fqn": "prod.sales.alice@corp.com",
                "catalog": "prod",
                "schema_name": "sales",
                "table_name": "alice@corp.com",
                "row_count": 10,
                "columns": [{"name": "a_id", "data_type": "INT"}],
            },
            {
                "fqn": "prod.sales.bob@corp.com",
                "catalog": "prod",
                "schema_name": "sales",
                "table_name": "bob@corp.com",
                "row_count": 20,
                "columns": [{"name": "b_id", "data_type": "INT"}],
            },
        ],
        "sample_rows": {
            "prod.sales.alice@corp.com": [{"a_id": 1}],
            "prod.sales.bob@corp.com": [{"b_id": 2}],
        },
    }
    masked_nb = zero_admin_nb.mask_bundle_pii(raw_dbx_bundle, include_sample_rows=True)
    masked_dump = json.dumps(masked_nb)
    assert "alice@corp.com" not in masked_dump and "bob@corp.com" not in masked_dump
    assert len({t["fqn"] for t in masked_nb["tables"]}) == 2
    assert len(masked_nb["sample_rows"]) == 2
    assert all("[REDACTED:" not in k for k in masked_nb["sample_rows"])

    (dbx_dir / "b.json").write_text(json.dumps(raw_dbx_bundle), encoding="utf-8")

    (notes_dir / "alice@corp.com.md").write_text(
        "alice@corp.com: OTIF (On-Time In-Full) stockouts spiked.\n",
        encoding="utf-8",
    )
    (notes_dir / "bob@corp.com.md").write_text(
        "Sarah Jenkins (VP Ops): Need inventory forecast urgently.\n",
        encoding="utf-8",
    )

    cfg = PoopdeckConfig.model_validate(
        {"databricks": {"catalogs_include": ["*"]}, "teams": {"archive_processed": True}}
    )
    assert sync_powerbi(v, config=cfg).cards_written == 4
    assert sync_databricks(v, config=cfg).cards_written == 2
    assert sync_teams(v, config=cfg).errors == []

    r_cards = sorted((v / "reports").glob("*.md"))
    d_cards = sorted((v / "datasets").glob("*.md"))
    assert len(r_cards) == 4 and len(d_cards) == 2

    all_r_text = "\n".join(p.read_text(encoding="utf-8") for p in r_cards)
    for expected_expr in ("SUM(S[A1])", "SUM(S[B1])", "SUM(S[A2])", "SUM(S[B2])"):
        assert expected_expr in all_r_text

    # Glossary term OTIF from alice@corp.com.md must not inherit Sarah Jenkins from bob@corp.com.md
    g_card = GlossaryCard.model_validate(parse_markdown_card(v / "glossary.md").frontmatter)
    otif_terms = [t for t in g_card.terms if t.term.upper() == "OTIF"]
    assert otif_terms and otif_terms[0].source_refs == []

    # Inject unique HUMAN-NOTES into every report & dataset card and verify byte-for-byte preservation on re-sync
    for idx, cp in enumerate([*r_cards, *d_cards]):
        updated = cp.read_text(encoding="utf-8").replace(
            f"{HUMAN_NOTES_START}\n",
            f"{HUMAN_NOTES_START}\nKEEP-NOTE-{idx}: exact bytes!\n",
            1,
        )
        cp.write_text(updated, encoding="utf-8")

    assert sync_powerbi(v, config=cfg).cards_written == 4
    assert sync_databricks(v, config=cfg).cards_written == 2
    for idx, cp in enumerate([*r_cards, *d_cards]):
        parsed = parse_markdown_card(cp)
        assert parsed.human_notes is not None
        assert f"KEEP-NOTE-{idx}: exact bytes!" in parsed.human_notes

    archived_mds = sorted((v / "inbox" / "processed").glob("*.md"))
    assert len(archived_mds) == 2

    for p in v.rglob("*"):
        if p.parent in (pbi_dir, dbx_dir):
            continue
        assert "alice@corp.com" not in p.name
        assert "bob@corp.com" not in p.name
        assert "[REDACTED:" not in p.name
        if p.is_file():
            txt = p.read_text(encoding="utf-8")
            assert "alice@corp.com" not in txt and "bob@corp.com" not in txt

    rep = validate_vault(v, strict=True)
    assert rep.valid is True, rep.errors


def test_28_powerbi_dax_info_csv_lineage_and_pbip_folder_single_card_resync(
    tmp_path: Path,
) -> None:
    # Part 1: UTF-8-BOM INFO.* CSVs with PascalCase/bracketed lineage in Description, Source, PartitionExpression
    info_vault = tmp_path / "info_vault"
    initialize_vault(info_vault, seed_questions=False, init_git=False)
    info_dir = tmp_path / "dax_info_bundle"
    info_dir.mkdir(parents=True)

    (info_dir / "info_tables.csv").write_text(
        "[ID],[Name],[Description],[Expression],[Source],[PartitionExpression]\n"
        "10,Dispatches,Curated events from telemetry_hub.curated.fleet_dispatches.,,SELECT * FROM telemetry_hub.curated.fleet_dispatches,\n"
        "20,Fuel,Fuel ledger rows,,,SELECT * FROM telemetry_hub.curated.fuel_ledgers\n",
        encoding="utf-8-sig",
    )
    (info_dir / "info_columns.csv").write_text(
        "ID,TableID,TableName,ExplicitName,DataType\n"
        "101,10,Dispatches,vehicle_id,String\n"
        "201,20,Fuel,vehicle_id,String\n",
        encoding="utf-8-sig",
    )
    (info_dir / "info_measures.csv").write_text(
        "ID,TableID,TableName,Name,Expression,Description\n"
        '301,10,Dispatches,Dispatch Lag Alert,"IF([Avg Transit Hours] > 18, ""CRITICAL"", ""OK"")",Static alert\n',
        encoding="utf-8-sig",
    )
    (info_dir / "info_relationships.csv").write_text(
        "ID,FromTableID,FromColumnID,ToTableID,ToColumnID,IsActive\n"
        "401,10,101,20,201,true\n",
        encoding="utf-8-sig",
    )

    s_info = sync_powerbi(info_vault, input_path=info_dir, mode="tier2")
    assert s_info.errors == []
    info_cards = sorted((info_vault / "reports").glob("*.md"))
    assert len(info_cards) == 1
    rc_info = ReportCard.model_validate(parse_markdown_card(info_cards[0]).frontmatter)
    assert set(rc_info.upstream_datasets) == {
        "[[telemetry_hub.curated.fleet_dispatches]]",
        "[[telemetry_hub.curated.fuel_ledgers]]",
    }
    assert len(rc_info.relationships) == 1

    # Part 2: Synthetic .pbip folder whose directory stem differs from model title
    pbip_vault = tmp_path / "pbip_vault"
    initialize_vault(pbip_vault, seed_questions=False, init_git=False)
    bundle_dir = tmp_path / "fleet_bundle_dir"
    rep_sub = bundle_dir / "Regional Fleet Dispatch Board.Report"
    sem_sub = bundle_dir / "Regional Fleet Dispatch Board.SemanticModel"
    rep_sub.mkdir(parents=True)
    sem_sub.mkdir(parents=True)

    (bundle_dir / "project.pbip").write_text(
        json.dumps(
            {
                "version": "1.0",
                "artifacts": [
                    {"report": {"path": "Regional Fleet Dispatch Board.Report"}},
                ],
                "settings": {"workspace": "Fleet Operations Workspace"},
            }
        ),
        encoding="utf-8",
    )
    (rep_sub / "report.json").write_text('{"config": "{}"}\n', encoding="utf-8")
    bim_payload: dict[str, Any] = {
        "name": "Regional Fleet Dispatch Board",
        "compatibilityLevel": 1567,
        "model": {
            "tables": [
                {
                    "name": "Dispatches",
                    "partitions": [
                        {
                            "name": "p1",
                            "source": {
                                "type": "m",
                                "expression": "SELECT * FROM telemetry_hub.curated.fleet_dispatches",
                            },
                        }
                    ],
                    "measures": [
                        {"name": "Total Dispatches", "expression": "COUNTROWS('Dispatches')"},
                    ],
                }
            ]
        },
    }
    (sem_sub / "model.bim").write_text(json.dumps(bim_payload), encoding="utf-8")

    s1 = sync_powerbi(pbip_vault, input_path=bundle_dir, mode="tier2")
    assert s1.errors == []
    rep_files_1 = sorted((pbip_vault / "reports").glob("*.md"))
    assert len(rep_files_1) == 1
    assert rep_files_1[0].name != "fleet-bundle-dir.md"
    rc_1 = ReportCard.model_validate(parse_markdown_card(rep_files_1[0]).frontmatter)
    assert rc_1.title == "Regional Fleet Dispatch Board"
    assert rc_1.workspace == "Fleet Operations Workspace"
    assert len(rc_1.measures) == 1

    custom_note = "FLEET-HUMAN-NOTE: verified dispatch SLA cutoffs with regional ops.\n  - bullet indent preserved"
    orig_md = rep_files_1[0].read_text(encoding="utf-8")
    rep_files_1[0].write_text(
        orig_md.replace(
            f"{HUMAN_NOTES_START}\n",
            f"{HUMAN_NOTES_START}\n{custom_note}\n",
            1,
        ),
        encoding="utf-8",
    )
    expected_human = parse_markdown_card(rep_files_1[0]).human_notes

    bim_payload["model"]["tables"][0]["measures"].append(
        {"name": "Active Vehicles", "expression": "DISTINCTCOUNT('Dispatches'[vehicle_id])"}
    )
    (sem_sub / "model.bim").write_text(json.dumps(bim_payload), encoding="utf-8")

    s2 = sync_powerbi(pbip_vault, input_path=bundle_dir, mode="tier2")
    assert s2.errors == []
    rep_files_2 = sorted((pbip_vault / "reports").glob("*.md"))
    assert len(rep_files_2) == 1
    parsed_2 = parse_markdown_card(rep_files_2[0])
    assert parsed_2.human_notes == expected_human
    rc_2 = ReportCard.model_validate(parsed_2.frontmatter)
    assert len(rc_2.measures) == 2


def test_29_teams_heading_prefix_colon_filter_standalone_owned_reports_and_docx_glossary(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    initialize_vault(vault, seed_questions=False, init_git=False)
    notes_dir = vault / "inbox" / "notes"
    teams_dir = vault / "inbox" / "teams"
    notes_dir.mkdir(parents=True, exist_ok=True)
    teams_dir.mkdir(parents=True, exist_ok=True)

    (notes_dir / "01_discovery_priya.md").write_text(
        "# 1:1 Discovery Sync — Priya Nair (Director of Fleet Analytics)\n"
        "- **Department**: Fleet Operations\n"
        "- **Influence Tier**: partner\n"
        "- **Decision Appetite**: high\n"
        "- Owned Reports: [[Regional Fleet Dispatch Board]]\n"
        "- Struggling with manual reconciliations in [[Unregistered Legacy Tracker]].\n"
        "- Diagnostic HTTP retry header: `x-trace-99 enabled`\n"
        "- Glossary — **Route Adherence**: Percentage of dispatches staying within planned corridor.\n",
        encoding="utf-8",
    )
    (notes_dir / "02_followup_priya.md").write_text(
        "# Follow-Up Check-In — Priya Nair\n"
        "Role: VP of Fleet Analytics\n"
        "Department: Global Fleet Operations\n"
        "- Explicit Ask: Build an automated dwell anomaly score for regional hubs.\n",
        encoding="utf-8",
    )
    (teams_dir / "03_fleet_standup.vtt").write_text(
        "WEBVTT\n\n"
        "1\n"
        "00:00:01.000 --> 00:00:09.000\n"
        "<v Priya Nair>Glossary note: FTD means First-Time Dispatch completion, and Yard Turnaround is gate-in to gate-out minutes.</v>\n",
        encoding="utf-8",
    )

    docx_xml = (
        "<w:tbl>"
        "<w:tr>"
        "<w:tc><w:p><w:r><w:t>Stakeholder Name</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Title</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>KPI Name</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Definition</w:t></w:r></w:p></w:tc>"
        "</w:tr>"
        "<w:tr>"
        "<w:tc><w:p><w:r><w:t>Mateo Alvarez</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Fleet Ops Lead</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>FRT</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>First-Response Dispatch Time across regional depots</w:t></w:r></w:p></w:tc>"
        "</w:tr>"
        "<w:tr>"
        "<w:tc><w:p><w:r><w:t>Mateo Alvarez</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Fleet Ops Lead</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Dwell Ratio</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Yard idle hours divided by active transit hours</w:t></w:r></w:p></w:tc>"
        "</w:tr>"
        "</w:tbl>"
    )
    _build_minimal_docx(notes_dir / "04_ops_matrix.docx", docx_xml)

    s_teams = sync_teams(vault)
    assert s_teams.errors == []

    sh_cards = [
        StakeholderCard.model_validate(parse_markdown_card(p).frontmatter)
        for p in sorted((vault / "stakeholders").glob("*.md"))
    ]
    assert {c.name for c in sh_cards} == {"Priya Nair", "Mateo Alvarez"}

    priya = next(c for c in sh_cards if c.name == "Priya Nair")
    assert priya.role == "VP of Fleet Analytics"
    assert priya.department == "Global Fleet Operations"
    assert priya.influence_tier == "partner"  # Explicit `partner` preserved!
    assert priya.decision_appetite == "high"
    assert priya.owned_reports == ["[[Regional Fleet Dispatch Board]]"]
    assert any("Unregistered Legacy Tracker" in p and "[[" not in p for p in priya.pain_points)
    assert any("dwell anomaly score" in a for a in priya.explicit_asks)

    g_card = GlossaryCard.model_validate(parse_markdown_card(vault / "glossary.md").frontmatter)
    g_terms = {t.term: t for t in g_card.terms}
    for expected_term in ("FRT", "Dwell Ratio", "Route Adherence", "FTD", "Yard Turnaround"):
        assert expected_term in g_terms

    # Standalone sync_teams passes non-strict vault validation cleanly
    val_non_strict = validate_vault(vault, strict=False)
    assert val_non_strict.valid is True, val_non_strict.errors

    # Once the referenced Power BI ReportCard exists, strict=True passes with 0 errors & 0 warnings
    sync_powerbi(
        vault,
        rest_client={
            "workspaces": [{"id": "w_fleet", "name": "Fleet Operations Workspace"}],
            "datasets": {
                "w_fleet": [{"id": "d_fleet", "name": "Regional Fleet Dispatch Board"}]
            },
            "reports": {
                "w_fleet": [
                    {
                        "id": "r_fleet",
                        "name": "Regional Fleet Dispatch Board",
                        "datasetId": "d_fleet",
                    }
                ]
            },
        },
    )
    val_strict = validate_vault(vault, strict=True)
    assert val_strict.valid is True, val_strict.errors
    assert val_strict.errors == []
    assert val_strict.warnings == []


