"""Control-store bulk-load preparation and schema initialization fast path."""

from google.cloud import bigquery

from app.config import Settings
from app.store import CONTROL_TABLES, ControlStore, _serialize_json_fields


def test_serialize_json_fields_stringifies_objects_only() -> None:
    schema = [
        bigquery.SchemaField("labels_json", "JSON"),
        bigquery.SchemaField("refs_json", "JSON"),
        bigquery.SchemaField("query", "STRING"),
        bigquery.SchemaField("cache_hit", "BOOL"),
    ]
    rows = [
        {
            "labels_json": {"team": "data"},
            "refs_json": [{"projectId": "p", "datasetId": "d", "tableId": "t"}],
            "query": "SELECT 1",
            "cache_hit": True,
        }
    ]
    prepared = _serialize_json_fields(schema, rows)
    assert prepared[0]["labels_json"] == '{"team": "data"}'
    assert prepared[0]["refs_json"] == '[{"projectId": "p", "datasetId": "d", "tableId": "t"}]'
    assert prepared[0]["query"] == "SELECT 1"
    assert prepared[0]["cache_hit"] is True


class FakeTable:
    def __init__(self, table_id: str) -> None:
        self.table_id = table_id


class FakeClient:
    def __init__(self, existing: list[str]) -> None:
        self.existing = existing
        self.ddl: list[str] = []

    def list_tables(self, dataset: str) -> list[FakeTable]:
        return [FakeTable(name) for name in self.existing]

    def query(self, sql: str, location: str | None = None) -> "FakeClient":
        self.ddl.append(sql)
        return self

    def result(self) -> None:
        return None


def make_store(existing: list[str]) -> ControlStore:
    store = ControlStore.__new__(ControlStore)
    store.settings = Settings(control_project_id="test-control")
    store.client = FakeClient(existing)
    store._schemas = {}
    return store


def test_initialize_skips_ddl_when_schema_complete() -> None:
    store = make_store(list(CONTROL_TABLES))
    store.initialize()
    assert store.client.ddl == []


def test_initialize_runs_ddl_when_tables_missing() -> None:
    store = make_store([])
    store.initialize()
    assert store.client.ddl, "missing tables must trigger DDL"
