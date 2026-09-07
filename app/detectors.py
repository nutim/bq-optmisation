"""Deterministic BigQuery cost and storage opportunity detectors."""

import hashlib
from datetime import UTC, datetime
from typing import Any

from google.cloud import bigquery

from app.config import Settings, get_settings
from app.logging import get_logger
from app.scoring import impact_level, priority_score
from app.store import ControlStore

log = get_logger(__name__)

TIB = 1024**4
GIB = 1024**3


def _id(*parts: object) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()


class DetectorService:
    def __init__(self, store: ControlStore, settings: Settings | None = None) -> None:
        self.store = store
        self.settings = settings or get_settings()

    def run(self, project_id: str | None = None) -> dict[str, int]:
        rows = [
            *self._expensive_queries(project_id),
            *self._select_star_queries(project_id),
            *self._partitioned_full_scans(project_id),
            *self._cache_miss_queries(project_id),
            *self._large_tables(project_id),
            *self._unused_tables(project_id),
            *self._time_travel_overhead(project_id),
            *self._spend_commitment_review(project_id),
            *self._native_recommendations(project_id),
        ]
        self.store.upsert_findings(rows)
        counts: dict[str, int] = {}
        for row in rows:
            category = str(row["category"])
            counts[category] = counts.get(category, 0) + 1
        log.info("detectors_completed", project_id=project_id or "all", findings=len(rows))
        return counts

    def _base_finding(
        self,
        *,
        project_id: str,
        location: str,
        category: str,
        target: str,
        title: str,
        evidence: dict[str, object],
        savings: float,
        confidence: float,
        effort: int,
        risk: int,
        recurrence: float = 1.0,
    ) -> dict[str, Any]:
        return {
            "finding_id": _id(project_id, location, category, target),
            "detected_at": datetime.now(UTC).isoformat(),
            "project_id": project_id,
            "location": location,
            "category": category,
            "target": target,
            "title": title,
            "evidence_json": evidence,
            "expected_monthly_savings": round(savings, 2),
            "confidence": confidence,
            "effort": effort,
            "risk": risk,
            "impact": impact_level(savings),
            "priority_score": priority_score(savings, confidence, recurrence, 1.0, effort, risk),
            "status": "OPEN",
        }

    def _project_clause(
        self, project_id: str | None
    ) -> tuple[str, list[bigquery.ScalarQueryParameter]]:
        if not project_id:
            return "", []
        return (
            " AND project_id=@project_id",
            [bigquery.ScalarQueryParameter("project_id", "STRING", project_id)],
        )

    def _window_params(self, project_id: str | None) -> list[bigquery.ScalarQueryParameter]:
        return [
            bigquery.ScalarQueryParameter("lookback", "INT64", self.settings.lookback_days),
            *self._project_clause(project_id)[1],
        ]

    def _latest_jobs_cte(self, project_id: str | None) -> str:
        project_clause, _ = self._project_clause(project_id)
        return f"""
        latest AS (
          SELECT * EXCEPT(row_num) FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY project_id, location, job_id
                                         ORDER BY collected_at DESC) row_num
            FROM `{self.settings.dataset_ref}.jobs_history`
            WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @lookback DAY)
              {project_clause}
          ) WHERE row_num=1
        )
        """

    def _latest_storage_cte(self, project_id: str | None) -> str:
        project_clause, _ = self._project_clause(project_id)
        return f"""
        latest_storage AS (
          SELECT * EXCEPT(row_num) FROM (
            SELECT *, ROW_NUMBER() OVER (
              PARTITION BY project_id, location, table_schema, table_name
              ORDER BY collected_at DESC) row_num
            FROM `{self.settings.dataset_ref}.table_storage_history`
            WHERE TRUE {project_clause}
          ) WHERE row_num=1
        )
        """

    def _expensive_queries(self, project_id: str | None) -> list[dict[str, Any]]:
        sql = f"""
        WITH {self._latest_jobs_cte(project_id)}
        SELECT project_id, location, query_fingerprint,
               ANY_VALUE(SUBSTR(query, 1, 20000)) sample_query,
               COUNT(*) executions, SUM(total_bytes_billed) bytes_billed,
               SUM(total_slot_ms) slot_ms, COUNTIF(cache_hit) cache_hits
        FROM latest
        GROUP BY project_id, location, query_fingerprint
        HAVING bytes_billed >= @expensive_bytes
        ORDER BY bytes_billed DESC LIMIT 500
        """
        params = [
            *self._window_params(project_id),
            bigquery.ScalarQueryParameter(
                "expensive_bytes", "INT64", self.settings.expensive_query_bytes
            ),
        ]
        result = []
        for row in self.store.query(sql, params):
            cost = row["bytes_billed"] / TIB * self.settings.on_demand_usd_per_tib
            savings = cost * self.settings.savings_ratio_expensive_query
            result.append(
                self._base_finding(
                    project_id=row["project_id"],
                    location=row["location"],
                    category="EXPENSIVE_RECURRING_QUERY",
                    target=row["query_fingerprint"],
                    title=f"Optimize recurring query executed {row['executions']} times",
                    evidence={
                        "executions": row["executions"],
                        "bytes_billed": row["bytes_billed"],
                        "slot_ms": row["slot_ms"],
                        "cache_hits": row["cache_hits"],
                        "sample_query": row["sample_query"],
                        "assumption": (
                            f"{self.settings.savings_ratio_expensive_query:.0%} reducible scan "
                            "under configured on-demand rate"
                        ),
                    },
                    savings=savings,
                    confidence=0.7,
                    effort=3,
                    risk=2,
                    recurrence=min(2.0, 1 + row["executions"] / 100),
                )
            )
        return result

    def _select_star_queries(self, project_id: str | None) -> list[dict[str, Any]]:
        sql = f"""
        WITH {self._latest_jobs_cte(project_id)}
        SELECT project_id, location, query_fingerprint,
               ANY_VALUE(SUBSTR(query, 1, 20000)) sample_query,
               COUNT(*) executions, SUM(total_bytes_billed) bytes_billed
        FROM latest
        WHERE REGEXP_CONTAINS(query, r'(?is)\\bselect\\s+(?:\\w+\\.)?\\*')
        GROUP BY project_id, location, query_fingerprint
        HAVING bytes_billed > 0
        ORDER BY bytes_billed DESC LIMIT 500
        """
        result = []
        for row in self.store.query(sql, self._window_params(project_id)):
            cost = row["bytes_billed"] / TIB * self.settings.on_demand_usd_per_tib
            result.append(
                self._base_finding(
                    project_id=row["project_id"],
                    location=row["location"],
                    category="SELECT_STAR",
                    target=row["query_fingerprint"],
                    title="Replace SELECT * with required columns",
                    evidence={
                        "executions": row["executions"],
                        "bytes_billed": row["bytes_billed"],
                        "sample_query": row["sample_query"],
                        "assumption": (
                            f"{self.settings.savings_ratio_select_star:.0%} reducible scan "
                            "under configured on-demand rate"
                        ),
                    },
                    savings=cost * self.settings.savings_ratio_select_star,
                    confidence=0.75,
                    effort=2,
                    risk=2,
                )
            )
        return result

    def _partitioned_full_scans(self, project_id: str | None) -> list[dict[str, Any]]:
        sql = f"""
        WITH {self._latest_jobs_cte(project_id)},
        {self._latest_storage_cte(project_id)},
        job_tables AS (
          SELECT j.project_id, j.location, j.query_fingerprint,
                 JSON_VALUE(ref, '$.projectId') ref_project,
                 JSON_VALUE(ref, '$.datasetId') ref_dataset,
                 JSON_VALUE(ref, '$.tableId') ref_table,
                 COUNT(*) executions,
                 SUM(j.total_bytes_billed) bytes_billed,
                 ANY_VALUE(SUBSTR(j.query, 1, 20000)) sample_query
          FROM latest j, UNNEST(JSON_QUERY_ARRAY(j.referenced_tables_json)) ref
          GROUP BY j.project_id, j.location, j.query_fingerprint,
                   ref_project, ref_dataset, ref_table
        )
        SELECT jt.project_id, jt.location, jt.query_fingerprint, jt.sample_query,
               jt.executions, jt.bytes_billed,
               jt.ref_dataset table_schema, jt.ref_table table_name,
               s.total_partitions, s.total_logical_bytes
        FROM job_tables jt
        JOIN latest_storage s
          ON jt.project_id=s.project_id AND jt.location=s.location
         AND jt.ref_dataset=s.table_schema AND jt.ref_table=s.table_name
         AND COALESCE(jt.ref_project, jt.project_id)=s.project_id
        WHERE s.total_partitions > 1
          AND s.total_logical_bytes > 0
          AND SAFE_DIVIDE(jt.bytes_billed, jt.executions)
              >= @scan_ratio * s.total_logical_bytes
        ORDER BY jt.bytes_billed DESC LIMIT 200
        """
        params = [
            *self._window_params(project_id),
            bigquery.ScalarQueryParameter(
                "scan_ratio", "FLOAT64", self.settings.partition_scan_ratio
            ),
        ]
        result = []
        for row in self.store.query(sql, params):
            cost = row["bytes_billed"] / TIB * self.settings.on_demand_usd_per_tib
            table = f"{row['project_id']}.{row['table_schema']}.{row['table_name']}"
            result.append(
                self._base_finding(
                    project_id=row["project_id"],
                    location=row["location"],
                    category="PARTITIONED_TABLE_FULL_SCAN",
                    target=f"{table}:{row['query_fingerprint']}",
                    title=f"Add partition filters for recurring scans of {row['table_name']}",
                    evidence={
                        "executions": row["executions"],
                        "bytes_billed": row["bytes_billed"],
                        "per_execution_bytes": row["bytes_billed"] // max(1, row["executions"]),
                        "total_partitions": row["total_partitions"],
                        "total_logical_bytes": row["total_logical_bytes"],
                        "sample_query": row["sample_query"],
                        "assumption": (
                            f"query bills at least {self.settings.partition_scan_ratio:.0%} of the "
                            f"table per run; {self.settings.savings_ratio_partition_scan:.0%} "
                            "reducible with partition pruning"
                        ),
                    },
                    savings=cost * self.settings.savings_ratio_partition_scan,
                    confidence=0.6,
                    effort=2,
                    risk=2,
                    recurrence=min(2.0, 1 + row["executions"] / 100),
                )
            )
        return result

    def _cache_miss_queries(self, project_id: str | None) -> list[dict[str, Any]]:
        sql = f"""
        WITH {self._latest_jobs_cte(project_id)}
        SELECT project_id, location, query_fingerprint,
               ANY_VALUE(SUBSTR(query, 1, 20000)) sample_query,
               COUNT(*) executions, COUNTIF(cache_hit) cache_hits,
               SUM(total_bytes_billed) bytes_billed
        FROM latest
        GROUP BY project_id, location, query_fingerprint
        HAVING executions >= @min_executions
          AND cache_hits = 0
          AND bytes_billed >= @cache_bytes
        ORDER BY bytes_billed DESC LIMIT 200
        """
        params = [
            *self._window_params(project_id),
            bigquery.ScalarQueryParameter(
                "min_executions", "INT64", self.settings.min_cache_executions
            ),
            bigquery.ScalarQueryParameter(
                "cache_bytes", "INT64", self.settings.cache_miss_query_bytes
            ),
        ]
        result = []
        for row in self.store.query(sql, params):
            cost = row["bytes_billed"] / TIB * self.settings.on_demand_usd_per_tib
            result.append(
                self._base_finding(
                    project_id=row["project_id"],
                    location=row["location"],
                    category="REPEATED_QUERY_CACHE_MISS",
                    target=row["query_fingerprint"],
                    title=(
                        f"Identical query ran {row['executions']} times with zero cache hits"
                    ),
                    evidence={
                        "executions": row["executions"],
                        "cache_hits": row["cache_hits"],
                        "bytes_billed": row["bytes_billed"],
                        "sample_query": row["sample_query"],
                        "guidance": (
                            "Check for non-deterministic functions (CURRENT_TIMESTAMP, RAND), "
                            "recently modified source tables, or sub-minute reruns that defeat "
                            "the automatic query cache"
                        ),
                        "assumption": (
                            f"{self.settings.savings_ratio_cache_miss:.0%} of runs could be "
                            "served from cache"
                        ),
                    },
                    savings=cost * self.settings.savings_ratio_cache_miss,
                    confidence=0.5,
                    effort=2,
                    risk=1,
                    recurrence=min(2.0, 1 + row["executions"] / 100),
                )
            )
        return result

    def _large_tables(self, project_id: str | None) -> list[dict[str, Any]]:
        sql = f"""
        WITH {self._latest_storage_cte(project_id)}
        SELECT *
        FROM latest_storage
        WHERE total_logical_bytes >= @large_bytes
        ORDER BY total_logical_bytes DESC LIMIT 500
        """
        params = [
            *self._project_clause(project_id)[1],
            bigquery.ScalarQueryParameter("large_bytes", "INT64", self.settings.large_table_bytes),
        ]
        result = []
        for row in self.store.query(sql, params):
            monthly_storage = (
                row["active_logical_bytes"]
                / GIB
                * self.settings.active_storage_usd_per_gib_month
            )
            target = f"{row['project_id']}.{row['table_schema']}.{row['table_name']}"
            result.append(
                self._base_finding(
                    project_id=row["project_id"],
                    location=row["location"],
                    category="LARGE_TABLE_LIFECYCLE_REVIEW",
                    target=target,
                    title="Review retention, partition expiry, and storage billing",
                    evidence={
                        "total_logical_bytes": row["total_logical_bytes"],
                        "total_physical_bytes": row["total_physical_bytes"],
                        "time_travel_physical_bytes": row["time_travel_physical_bytes"],
                        "last_modified": str(row["storage_last_modified_time"]),
                        "assumption": (
                            f"{self.settings.savings_ratio_large_table:.0%} of active logical "
                            "storage may be reducible"
                        ),
                    },
                    savings=monthly_storage * self.settings.savings_ratio_large_table,
                    confidence=0.45,
                    effort=3,
                    risk=4,
                )
            )
        return result

    def _unused_tables(self, project_id: str | None) -> list[dict[str, Any]]:
        sql = f"""
        WITH {self._latest_storage_cte(project_id)},
        latest_jobs AS (
          SELECT * EXCEPT(row_num) FROM (
            SELECT *, ROW_NUMBER() OVER (
              PARTITION BY project_id, location, job_id ORDER BY collected_at DESC) row_num
            FROM `{self.settings.dataset_ref}.jobs_history`
            WHERE creation_time >= TIMESTAMP_SUB(
              CURRENT_TIMESTAMP(), INTERVAL @unused_days DAY)
              {self._project_clause(project_id)[0]}
          ) WHERE row_num=1
        ), reads AS (
          SELECT j.project_id, j.location,
                 JSON_VALUE(ref, '$.datasetId') table_schema,
                 JSON_VALUE(ref, '$.tableId') table_name,
                 MAX(j.creation_time) last_read_time
          FROM latest_jobs j,
          UNNEST(JSON_QUERY_ARRAY(j.referenced_tables_json)) ref
          GROUP BY project_id, location, table_schema, table_name
        )
        SELECT s.*, r.last_read_time
        FROM latest_storage s
        LEFT JOIN reads r USING(project_id, location, table_schema, table_name)
        WHERE r.last_read_time IS NULL
          AND s.storage_last_modified_time < TIMESTAMP_SUB(
            CURRENT_TIMESTAMP(), INTERVAL @unused_days DAY)
          AND s.active_logical_bytes > 0
        ORDER BY s.active_logical_bytes DESC LIMIT 500
        """
        params = [
            *self._project_clause(project_id)[1],
            bigquery.ScalarQueryParameter("unused_days", "INT64", self.settings.unused_table_days),
        ]
        result = []
        for row in self.store.query(sql, params):
            monthly_storage = (
                row["active_logical_bytes"]
                / GIB
                * self.settings.active_storage_usd_per_gib_month
            )
            target = f"{row['project_id']}.{row['table_schema']}.{row['table_name']}"
            result.append(
                self._base_finding(
                    project_id=row["project_id"],
                    location=row["location"],
                    category="UNUSED_TABLE_CANDIDATE",
                    target=target,
                    title=(
                        f"No recorded reads in {self.settings.unused_table_days} days; "
                        "verify owner and lineage"
                    ),
                    evidence={
                        "active_logical_bytes": row["active_logical_bytes"],
                        "last_read_time": str(row["last_read_time"]),
                        "last_modified": str(row["storage_last_modified_time"]),
                        "caveat": (
                            "Reads via the BigQuery Storage Read API, exports, and external "
                            "tools do not create query jobs; confirm lineage before acting"
                        ),
                        "required_checks": [
                            "owner",
                            "lineage",
                            "retention policy",
                            "recovery plan",
                        ],
                    },
                    savings=monthly_storage,
                    confidence=0.4,
                    effort=2,
                    risk=5,
                )
            )
        return result

    def _time_travel_overhead(self, project_id: str | None) -> list[dict[str, Any]]:
        sql = f"""
        WITH {self._latest_storage_cte(project_id)}
        SELECT *,
               time_travel_physical_bytes + fail_safe_physical_bytes overhead_bytes
        FROM latest_storage
        WHERE total_physical_bytes > 0
          AND time_travel_physical_bytes + fail_safe_physical_bytes
              >= @tt_ratio * total_physical_bytes
          AND time_travel_physical_bytes + fail_safe_physical_bytes >= @tt_min_bytes
        ORDER BY overhead_bytes DESC LIMIT 200
        """
        params = [
            *self._project_clause(project_id)[1],
            bigquery.ScalarQueryParameter(
                "tt_ratio", "FLOAT64", self.settings.time_travel_overhead_ratio
            ),
            bigquery.ScalarQueryParameter(
                "tt_min_bytes", "INT64", self.settings.time_travel_min_bytes
            ),
        ]
        result = []
        for row in self.store.query(sql, params):
            overhead_usd = (
                row["overhead_bytes"] / GIB * self.settings.active_storage_usd_per_gib_month
            )
            target = f"{row['project_id']}.{row['table_schema']}.{row['table_name']}"
            result.append(
                self._base_finding(
                    project_id=row["project_id"],
                    location=row["location"],
                    category="TIME_TRAVEL_OVERHEAD",
                    target=target,
                    title="Time-travel and fail-safe storage dominate table storage",
                    evidence={
                        "overhead_bytes": row["overhead_bytes"],
                        "total_physical_bytes": row["total_physical_bytes"],
                        "time_travel_physical_bytes": row["time_travel_physical_bytes"],
                        "fail_safe_physical_bytes": row["fail_safe_physical_bytes"],
                        "assumption": (
                            f"overhead is at least {self.settings.time_travel_overhead_ratio:.0%} "
                            "of physical bytes; "
                            f"{self.settings.savings_ratio_time_travel:.0%} reducible with a "
                            "shorter time-travel window"
                        ),
                    },
                    savings=overhead_usd * self.settings.savings_ratio_time_travel,
                    confidence=0.5,
                    effort=2,
                    risk=3,
                )
            )
        return result

    def _spend_commitment_review(self, project_id: str | None) -> list[dict[str, Any]]:
        sql = f"""
        WITH {self._latest_jobs_cte(project_id)}
        SELECT project_id, COUNT(*) jobs, SUM(total_bytes_billed) bytes_billed,
               SUM(total_slot_ms) slot_ms
        FROM latest
        GROUP BY project_id
        HAVING bytes_billed / {TIB} * @rate >= @threshold
        ORDER BY bytes_billed DESC
        """
        params = [
            *self._window_params(project_id),
            bigquery.ScalarQueryParameter("rate", "FLOAT64", self.settings.on_demand_usd_per_tib),
            bigquery.ScalarQueryParameter(
                "threshold", "FLOAT64", self.settings.commitment_review_usd
            ),
        ]
        result = []
        for row in self.store.query(sql, params):
            monthly_estimate = row["bytes_billed"] / TIB * self.settings.on_demand_usd_per_tib
            result.append(
                self._base_finding(
                    project_id=row["project_id"],
                    location="multi",
                    category="COMMITMENT_REVIEW",
                    target=row["project_id"],
                    title="Evaluate editions and capacity commitments for sustained spend",
                    evidence={
                        "jobs": row["jobs"],
                        "bytes_billed": row["bytes_billed"],
                        "slot_ms": row["slot_ms"],
                        "estimated_monthly_on_demand_usd": round(monthly_estimate, 2),
                        "assumption": (
                            "on-demand list rate; sustained spend above the configured threshold "
                            "usually prices better on editions with commitments"
                        ),
                    },
                    savings=0,
                    confidence=0.4,
                    effort=3,
                    risk=3,
                )
            )
        return result

    def _native_recommendations(self, project_id: str | None) -> list[dict[str, Any]]:
        project_clause, params = self._project_clause(project_id)
        sql = f"""
        SELECT * EXCEPT(row_num) FROM (
          SELECT *, ROW_NUMBER() OVER (
            PARTITION BY project_id, location, recommendation_id
            ORDER BY collected_at DESC) row_num
          FROM `{self.settings.dataset_ref}.native_recommendations`
          WHERE state='ACTIVE' {project_clause}
        ) WHERE row_num=1
        """
        result = []
        for row in self.store.query(sql, params):
            target = ",".join(row["target_resources_json"] or [])
            result.append(
                self._base_finding(
                    project_id=row["project_id"],
                    location=row["location"],
                    category="NATIVE_BIGQUERY_RECOMMENDATION",
                    target=target or row["recommendation_id"],
                    title=f"Apply BigQuery {row['subtype'] or row['recommender']} recommendation",
                    evidence={
                        "recommendation_id": row["recommendation_id"],
                        "recommender": row["recommender"],
                        "overview": row["overview_json"],
                        "details": row["details_json"],
                    },
                    savings=0,
                    confidence=0.9,
                    effort=3,
                    risk=3,
                )
            )
        return result
