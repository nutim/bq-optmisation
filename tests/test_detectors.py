"""Detector SQL construction is parameterized and uses the collected schema keys."""

from typing import Any

from google.cloud import bigquery

from app.config import Settings
from app.detectors import DetectorService


class FakeStore:
    def __init__(self) -> None:
        self.queries: list[tuple[str, list[Any] | None]] = []

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        self.queries.append((sql, params))
        return []

    def upsert_findings(self, rows: list[dict[str, Any]]) -> None:
        self.captured_findings = list(rows)


def make_service() -> tuple[DetectorService, FakeStore]:
    store = FakeStore()
    settings = Settings(
        control_project_id="test-control",
        lookback_days=45,
        unused_table_days=120,
        expensive_query_bytes=2_000_000_000_000,
    )
    return DetectorService(store, settings), store


def test_detector_queries_are_parameterized() -> None:
    service, store = make_service()
    service.run("analytics-production")
    assert store.queries, "detectors must issue queries"

    for sql, params in store.queries:
        assert "project_id='analytics" not in sql.replace("ANALYTICS", "analytics")
        assert "@project_id" in sql
        names = {param.name for param in (params or [])}
        assert "project_id" in names


def test_detector_window_uses_configured_lookback() -> None:
    service, store = make_service()
    service.run("analytics-production")
    for sql, params in store.queries:
        if "jobs_history" in sql:
            assert "INTERVAL @lookback DAY" in sql
            assert any(
                isinstance(param, bigquery.ScalarQueryParameter)
                and param.name == "lookback"
                and param.value == 45
                for param in (params or [])
            )
            break


def test_unused_tables_uses_information_schema_json_keys() -> None:
    """Regression: referenced_tables stores camelCase keys from INFORMATION_SCHEMA."""
    service, store = make_service()
    service.run(None)
    unused_sql = next(sql for sql, _ in store.queries if "last_read_time" in sql)
    assert "$.datasetId" in unused_sql
    assert "$.tableId" in unused_sql
    assert "$.dataset_id" not in unused_sql
    assert "$.table_id" not in unused_sql


def test_all_detector_families_execute() -> None:
    service, store = make_service()
    service.run(None)
    joined = "\n".join(sql for sql, _ in store.queries)
    assert "@expensive_bytes" in joined
    assert "@scan_ratio" in joined
    assert "@min_executions" in joined
    assert "@cache_bytes" in joined
    assert "@tt_ratio" in joined
    assert "@threshold" in joined
    assert "@large_bytes" in joined
    assert "@unused_days" in joined
