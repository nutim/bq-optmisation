"""BigQuery-backed control-plane repository."""

import json
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from google.cloud import bigquery

from app.auth import default_credentials
from app.config import Settings, get_settings
from app.logging import get_logger
from app.models import ActionRequest, Approval, ProjectRegistration

log = get_logger(__name__)

CONTROL_TABLES = (
    "project_registry",
    "jobs_history",
    "table_storage_history",
    "native_recommendations",
    "findings",
    "action_ledger",
    "outcomes",
)


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _serialize_json_fields(
    schema: Sequence[bigquery.SchemaField], rows: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return rows with dict/list values for JSON columns serialized to strings.

    The load API expects JSON-typed columns as serialized JSON strings, while
    the streaming API accepted objects directly.
    """
    json_fields = {field.name for field in schema if field.field_type == "JSON"}
    prepared = []
    for row in rows:
        item = dict(row)
        for name in json_fields:
            value = item.get(name)
            if isinstance(value, (dict, list)):
                item[name] = json.dumps(value, default=_json_default)
        prepared.append(item)
    return prepared


class ControlStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.client = bigquery.Client(
            project=self.settings.control_project_id,
            credentials=default_credentials(),
            location=self.settings.control_location,
        )
        self._schemas: dict[str, list[bigquery.SchemaField]] = {}

    def initialize(self) -> None:
        """Create missing control tables using idempotent DDL.

        Skips the DDL queries entirely when every expected table already
        exists, so cold starts stay fast and cheap.
        """
        existing = {table.table_id for table in self.client.list_tables(self.settings.dataset_ref)}
        if all(name in existing for name in CONTROL_TABLES):
            log.debug("control_schema_present", dataset=self.settings.dataset_ref)
            return
        schema_path = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"
        ddl = (
            schema_path.read_text()
            .replace("${DATASET}", self.settings.dataset_ref)
            .replace("${LOCATION}", self.settings.control_location)
        )
        for statement in (part.strip() for part in ddl.split(";\n") if part.strip()):
            self.client.query(statement, location=self.settings.control_location).result()
        log.info("control_schema_initialized", dataset=self.settings.dataset_ref)

    def _schema_for(self, table_ref: str) -> list[bigquery.SchemaField]:
        cached = self._schemas.get(table_ref)
        if cached is None:
            cached = list(self.client.get_table(table_ref).schema)
            self._schemas[table_ref] = cached
        return cached

    def _load_json(
        self,
        table_ref: str,
        rows: list[dict[str, Any]],
        schema: list[bigquery.SchemaField] | None = None,
    ) -> None:
        """Bulk-load rows with a free batch load job instead of billed streaming."""
        if schema is None:
            schema = self._schema_for(table_ref)
        prepared = _serialize_json_fields(schema, rows)
        config = bigquery.LoadJobConfig(
            schema=schema,
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        )
        job = self.client.load_table_from_json(prepared, table_ref, job_config=config)
        job.result()
        if job.errors:
            raise RuntimeError(f"BigQuery load failed for {table_ref}: {job.errors[:3]}")
        log.info("rows_loaded", table=table_ref, rows=len(prepared))

    def upsert_project(self, registration: ProjectRegistration) -> None:
        query = f"""
        MERGE `{self.settings.dataset_ref}.project_registry` T
        USING (SELECT @project_id AS project_id) S
        ON T.project_id = S.project_id
        WHEN MATCHED THEN UPDATE SET
          locations=@locations, reader_service_account=@reader_sa,
          executor_service_account=@executor_sa, owner_email=@owner_email,
          enabled=@enabled, autonomy_level=@autonomy_level,
          labels_json=PARSE_JSON(@labels_json),
          updated_at=CURRENT_TIMESTAMP()
        WHEN NOT MATCHED THEN INSERT
          (project_id, locations, reader_service_account, executor_service_account,
           owner_email, enabled, autonomy_level, labels_json, created_at, updated_at)
        VALUES
          (@project_id, @locations, @reader_sa, @executor_sa, @owner_email,
           @enabled, @autonomy_level, PARSE_JSON(@labels_json),
           CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP())
        """
        params = [
            bigquery.ScalarQueryParameter("project_id", "STRING", registration.project_id),
            bigquery.ArrayQueryParameter("locations", "STRING", registration.locations),
            bigquery.ScalarQueryParameter(
                "reader_sa", "STRING", registration.reader_service_account
            ),
            bigquery.ScalarQueryParameter(
                "executor_sa", "STRING", registration.executor_service_account
            ),
            bigquery.ScalarQueryParameter("owner_email", "STRING", registration.owner_email),
            bigquery.ScalarQueryParameter("enabled", "BOOL", registration.enabled),
            bigquery.ScalarQueryParameter(
                "autonomy_level", "STRING", registration.autonomy_level.value
            ),
            bigquery.ScalarQueryParameter("labels_json", "STRING", json.dumps(registration.labels)),
        ]
        config = bigquery.QueryJobConfig(query_parameters=params)
        self.client.query(query, job_config=config).result()

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        query = f"""
        SELECT * FROM `{self.settings.dataset_ref}.project_registry`
        WHERE project_id=@project_id LIMIT 1
        """
        config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("project_id", "STRING", project_id)]
        )
        rows = list(self.client.query(query, job_config=config).result())
        return dict(rows[0].items()) if rows else None

    def list_projects(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE enabled" if enabled_only else ""
        query = f"""
        SELECT * FROM `{self.settings.dataset_ref}.project_registry`
        {where} ORDER BY project_id
        """
        return [dict(row.items()) for row in self.client.query(query).result()]

    def update_collection_status(self, project_id: str, status: str) -> None:
        query = f"""
        UPDATE `{self.settings.dataset_ref}.project_registry`
        SET last_collection_at=CURRENT_TIMESTAMP(), last_collection_status=@status,
            updated_at=CURRENT_TIMESTAMP()
        WHERE project_id=@project_id
        """
        config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("project_id", "STRING", project_id),
                bigquery.ScalarQueryParameter("status", "STRING", status[:1000]),
            ]
        )
        self.client.query(query, job_config=config).result()

    def append_rows(self, table: str, rows: Iterable[dict[str, Any]]) -> None:
        payload = list(rows)
        if not payload:
            return
        self._load_json(f"{self.settings.dataset_ref}.{table}", payload)

    def upsert_findings(self, rows: Iterable[dict[str, Any]]) -> None:
        """Load findings through a temporary table and merge on deterministic ID."""
        payload = list(rows)
        if not payload:
            return
        findings_ref = f"{self.settings.dataset_ref}.findings"
        schema = self._schema_for(findings_ref)
        temp_name = f"_findings_{uuid.uuid4().hex}"
        temp_ref = f"{self.settings.dataset_ref}.{temp_name}"
        temp = bigquery.Table(temp_ref, schema=schema)
        temp.expires = datetime.now(UTC) + timedelta(hours=1)
        self.client.create_table(temp)
        try:
            self._load_json(temp_ref, payload, schema=schema)
            self.client.query(
                f"""
                MERGE `{self.settings.dataset_ref}.findings` T
                USING `{temp_ref}` S ON T.finding_id=S.finding_id
                WHEN MATCHED THEN UPDATE SET
                  detected_at=S.detected_at, title=S.title, evidence_json=S.evidence_json,
                  expected_monthly_savings=S.expected_monthly_savings,
                  confidence=S.confidence, effort=S.effort, risk=S.risk,
                  impact=S.impact, priority_score=S.priority_score,
                  status=IF(T.status IN ('EXECUTED','VERIFIED','REJECTED'), T.status, S.status)
                WHEN NOT MATCHED THEN INSERT ROW
                """
            ).result()
        finally:
            self.client.delete_table(temp_ref, not_found_ok=True)

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        config = bigquery.QueryJobConfig(query_parameters=params or [])
        rows = self.client.query(sql, job_config=config).result()
        return [dict(row.items()) for row in rows]

    def list_findings(
        self, project_id: str | None = None, status: str = "OPEN", limit: int = 100
    ) -> list[dict[str, Any]]:
        filters = ["status=@status"]
        params: list[Any] = [
            bigquery.ScalarQueryParameter("status", "STRING", status),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
        if project_id:
            filters.append("project_id=@project_id")
            params.append(bigquery.ScalarQueryParameter("project_id", "STRING", project_id))
        sql = f"""
        SELECT * FROM `{self.settings.dataset_ref}.findings`
        WHERE {" AND ".join(filters)}
        ORDER BY priority_score DESC LIMIT @limit
        """
        return self.query(sql, params)

    def get_finding(self, finding_id: str) -> dict[str, Any] | None:
        rows = self.query(
            f"SELECT * FROM `{self.settings.dataset_ref}.findings` "
            "WHERE finding_id=@finding_id ORDER BY detected_at DESC LIMIT 1",
            [bigquery.ScalarQueryParameter("finding_id", "STRING", finding_id)],
        )
        return rows[0] if rows else None

    def create_action(self, request: ActionRequest) -> str:
        finding = self.get_finding(request.finding_id)
        if not finding:
            raise ValueError("finding not found")
        if finding["project_id"] != request.project_id:
            raise ValueError("finding and action project do not match")
        action_id = str(uuid.uuid4())
        log.info(
            "action_created",
            action_id=action_id,
            finding_id=request.finding_id,
            project_id=request.project_id,
            action_type=request.action_type.value,
            requested_by=request.requested_by,
        )
        row = {
            "action_id": action_id,
            "finding_id": request.finding_id,
            "project_id": request.project_id,
            "action_type": request.action_type.value,
            "payload_json": json.loads(json.dumps(request.payload, default=_json_default)),
            "requested_by": request.requested_by,
            "rollback_plan": request.rollback_plan,
            "state": "PENDING_APPROVAL",
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        self.append_rows("action_ledger", [row])
        return action_id

    def decide_action(self, approval: Approval) -> bool:
        """Apply an approval decision; return False when no pending action matched."""
        next_state = "APPROVED" if approval.decision.value == "APPROVE" else "REJECTED"
        query = f"""
        UPDATE `{self.settings.dataset_ref}.action_ledger`
        SET state=@state, decided_by=@decided_by, decision_reason=@reason,
            decided_at=CURRENT_TIMESTAMP(), updated_at=CURRENT_TIMESTAMP()
        WHERE action_id=@action_id AND state='PENDING_APPROVAL'
        """
        params = [
            bigquery.ScalarQueryParameter("action_id", "STRING", approval.action_id),
            bigquery.ScalarQueryParameter("state", "STRING", next_state),
            bigquery.ScalarQueryParameter("decided_by", "STRING", approval.decided_by),
            bigquery.ScalarQueryParameter("reason", "STRING", approval.reason),
        ]
        job = self.client.query(query, job_config=bigquery.QueryJobConfig(query_parameters=params))
        job.result()
        decided = job.num_dml_affected_rows == 1
        if not decided:
            log.warning(
                "action_decision_noop",
                action_id=approval.action_id,
                decided_by=approval.decided_by,
            )
        return decided

    def get_action(self, action_id: str) -> dict[str, Any] | None:
        rows = self.query(
            f"SELECT * FROM `{self.settings.dataset_ref}.action_ledger` "
            "WHERE action_id=@action_id LIMIT 1",
            [bigquery.ScalarQueryParameter("action_id", "STRING", action_id)],
        )
        return rows[0] if rows else None

    def claim_action(self, action_id: str) -> bool:
        """Atomically prevent duplicate or concurrent execution of an approved action."""
        config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("action_id", "STRING", action_id)]
        )
        job = self.client.query(
            f"""
            UPDATE `{self.settings.dataset_ref}.action_ledger`
            SET state='EXECUTING', updated_at=CURRENT_TIMESTAMP()
            WHERE action_id=@action_id AND state='APPROVED'
            """,
            job_config=config,
        )
        job.result()
        return job.num_dml_affected_rows == 1

    def mark_action(self, action_id: str, state: str, result: dict[str, object]) -> None:
        self.query(
            f"""
            UPDATE `{self.settings.dataset_ref}.action_ledger`
            SET state=@state, result_json=PARSE_JSON(@result_json),
                executed_at=CURRENT_TIMESTAMP(), updated_at=CURRENT_TIMESTAMP()
            WHERE action_id=@action_id
            """,
            [
                bigquery.ScalarQueryParameter("action_id", "STRING", action_id),
                bigquery.ScalarQueryParameter("state", "STRING", state),
                bigquery.ScalarQueryParameter(
                    "result_json", "STRING", json.dumps(result, default=_json_default)
                ),
            ],
        )


@lru_cache
def get_store() -> ControlStore:
    """Return a shared store so BigQuery clients are not rebuilt per request."""
    return ControlStore()
