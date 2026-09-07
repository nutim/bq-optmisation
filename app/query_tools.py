"""Safe query inspection and validation operations."""

from typing import Any

from google.cloud import bigquery

from app.auth import impersonate
from app.store import ControlStore


class QueryValidator:
    def __init__(self, store: ControlStore) -> None:
        self.store = store

    def dry_run(
        self, project_id: str, location: str, sql: str, maximum_bytes_billed: int
    ) -> dict[str, Any]:
        registration = self.store.get_project(project_id)
        if not registration or not registration["enabled"]:
            raise ValueError("project is not registered and enabled")
        credentials = impersonate(str(registration["reader_service_account"]))
        client = bigquery.Client(project=project_id, credentials=credentials, location=location)
        config = bigquery.QueryJobConfig(
            dry_run=True,
            use_query_cache=False,
            maximum_bytes_billed=maximum_bytes_billed,
        )
        job = client.query(sql, job_config=config, location=location)
        return {
            "project_id": project_id,
            "location": location,
            "total_bytes_processed": job.total_bytes_processed or 0,
            "maximum_bytes_billed": maximum_bytes_billed,
            "valid": True,
        }
