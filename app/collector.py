"""Region-aware BigQuery metadata collection using impersonated credentials."""

from datetime import UTC, datetime, timedelta
from typing import Any

from google.cloud import bigquery

from app.auth import impersonate
from app.config import Settings, get_settings
from app.logging import get_logger
from app.scoring import query_fingerprint
from app.store import ControlStore

log = get_logger(__name__)


def information_schema_region(location: str) -> str:
    value = location.strip().lower()
    if not value.replace("-", "").isalnum():
        raise ValueError(f"invalid BigQuery location: {location}")
    return f"region-{value}"


class ProjectCollector:
    def __init__(self, store: ControlStore, settings: Settings | None = None) -> None:
        self.store = store
        self.settings = settings or get_settings()

    def collect(
        self, registration: dict[str, Any], locations: list[str] | None = None
    ) -> dict[str, int]:
        project_id = str(registration["project_id"])
        reader_sa = str(registration["reader_service_account"])
        requested_locations = locations or list(registration["locations"])
        credentials = impersonate(reader_sa)
        client = bigquery.Client(project=project_id, credentials=credentials)
        totals = {"jobs": 0, "tables": 0, "recommendations": 0}
        log.info(
            "collection_started",
            project_id=project_id,
            locations=requested_locations,
        )

        try:
            for location in requested_locations:
                job_rows = self._collect_jobs(client, project_id, location)
                table_rows = self._collect_storage(client, project_id, location)
                recommendation_rows = self._collect_recommendations(client, project_id, location)
                self.store.append_rows("jobs_history", job_rows)
                self.store.append_rows("table_storage_history", table_rows)
                self.store.append_rows("native_recommendations", recommendation_rows)
                totals["jobs"] += len(job_rows)
                totals["tables"] += len(table_rows)
                totals["recommendations"] += len(recommendation_rows)
                log.info(
                    "location_collected",
                    project_id=project_id,
                    location=location,
                    jobs=len(job_rows),
                    tables=len(table_rows),
                    recommendations=len(recommendation_rows),
                )
            self.store.update_collection_status(project_id, f"SUCCESS {totals}")
            log.info("collection_completed", project_id=project_id, **totals)
            return totals
        except Exception as exc:
            self.store.update_collection_status(project_id, f"FAILED {type(exc).__name__}: {exc}")
            log.error(
                "collection_failed",
                project_id=project_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise

    def _collect_jobs(
        self, client: bigquery.Client, project_id: str, location: str
    ) -> list[dict[str, Any]]:
        region = information_schema_region(location)
        since = datetime.now(UTC) - timedelta(days=self.settings.lookback_days)
        sql = f"""
        SELECT creation_time, start_time, end_time, job_id, user_email,
               statement_type, total_bytes_processed, total_bytes_billed,
               total_slot_ms, cache_hit, query, labels, referenced_tables,
               error_result
        FROM `{project_id}.{region}.INFORMATION_SCHEMA.JOBS_BY_PROJECT`
        WHERE creation_time >= @since
          AND (statement_type IS NULL OR statement_type != 'SCRIPT')
          AND job_type = 'QUERY'
        ORDER BY creation_time DESC
        LIMIT @row_limit
        """
        config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("since", "TIMESTAMP", since),
                bigquery.ScalarQueryParameter("row_limit", "INT64", self.settings.max_result_rows),
            ]
        )
        collected_at = datetime.now(UTC).isoformat()
        result = []
        for row in client.query(sql, job_config=config, location=location).result():
            query = row.query or ""
            referenced = [dict(item.items()) for item in (row.referenced_tables or [])]
            result.append(
                {
                    "collected_at": collected_at,
                    "project_id": project_id,
                    "location": location.lower(),
                    "creation_time": row.creation_time.isoformat(),
                    "start_time": row.start_time.isoformat() if row.start_time else None,
                    "end_time": row.end_time.isoformat() if row.end_time else None,
                    "job_id": row.job_id,
                    "user_email": row.user_email,
                    "statement_type": row.statement_type,
                    "total_bytes_processed": row.total_bytes_processed or 0,
                    "total_bytes_billed": row.total_bytes_billed or 0,
                    "total_slot_ms": row.total_slot_ms or 0,
                    "cache_hit": bool(row.cache_hit),
                    "query": query[:1_000_000],
                    "query_fingerprint": query_fingerprint(query),
                    "labels_json": dict(row.labels or {}),
                    "referenced_tables_json": referenced,
                    "error_json": row.error_result if row.error_result else None,
                }
            )
        return result

    def _collect_storage(
        self, client: bigquery.Client, project_id: str, location: str
    ) -> list[dict[str, Any]]:
        region = information_schema_region(location)
        sql = f"""
        SELECT table_schema, table_name, total_rows, total_partitions,
               total_logical_bytes, total_physical_bytes,
               active_logical_bytes, long_term_logical_bytes,
               time_travel_physical_bytes, fail_safe_physical_bytes,
               storage_last_modified_time
        FROM `{project_id}.{region}.INFORMATION_SCHEMA.TABLE_STORAGE_BY_PROJECT`
        WHERE table_type = 'BASE TABLE'
        LIMIT @row_limit
        """
        config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("row_limit", "INT64", self.settings.max_result_rows)
            ]
        )
        collected_at = datetime.now(UTC).isoformat()
        return [
            {
                "collected_at": collected_at,
                "project_id": project_id,
                "location": location.lower(),
                "table_schema": row.table_schema,
                "table_name": row.table_name,
                "total_rows": row.total_rows or 0,
                "total_partitions": row.total_partitions or 0,
                "total_logical_bytes": row.total_logical_bytes or 0,
                "total_physical_bytes": row.total_physical_bytes or 0,
                "active_logical_bytes": row.active_logical_bytes or 0,
                "long_term_logical_bytes": row.long_term_logical_bytes or 0,
                "time_travel_physical_bytes": row.time_travel_physical_bytes or 0,
                "fail_safe_physical_bytes": row.fail_safe_physical_bytes or 0,
                "storage_last_modified_time": (
                    row.storage_last_modified_time.isoformat()
                    if row.storage_last_modified_time
                    else None
                ),
            }
            for row in client.query(sql, job_config=config, location=location).result()
        ]

    def _collect_recommendations(
        self, client: bigquery.Client, project_id: str, location: str
    ) -> list[dict[str, Any]]:
        region = information_schema_region(location)
        sql = f"""
        SELECT recommendation_id, recommender, subtype, target_resources,
               overview, recommendation_details, state, last_updated_time
        FROM `{project_id}.{region}.INFORMATION_SCHEMA.RECOMMENDATIONS_BY_PROJECT`
        WHERE state = 'ACTIVE'
        LIMIT @row_limit
        """
        config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("row_limit", "INT64", self.settings.max_result_rows)
            ]
        )
        collected_at = datetime.now(UTC).isoformat()
        try:
            rows = client.query(sql, job_config=config, location=location).result()
            return [
                {
                    "collected_at": collected_at,
                    "project_id": project_id,
                    "location": location.lower(),
                    "recommendation_id": row.recommendation_id,
                    "recommender": row.recommender,
                    "subtype": row.subtype,
                    "target_resources_json": list(row.target_resources or []),
                    "overview_json": row.overview,
                    "details_json": row.recommendation_details,
                    "state": row.state,
                    "last_updated_time": row.last_updated_time.isoformat(),
                }
                for row in rows
            ]
        except Exception as exc:
            # RECOMMENDATIONS is pre-GA and can be unavailable in some regions/projects.
            log.warning(
                "recommendations_unavailable",
                project_id=project_id,
                location=location,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return []
