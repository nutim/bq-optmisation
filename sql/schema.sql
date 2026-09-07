CREATE SCHEMA IF NOT EXISTS `${DATASET}` OPTIONS(location="${LOCATION}");

CREATE TABLE IF NOT EXISTS `${DATASET}.project_registry` (
  project_id STRING NOT NULL,
  locations ARRAY<STRING> NOT NULL,
  reader_service_account STRING NOT NULL,
  executor_service_account STRING,
  owner_email STRING NOT NULL,
  enabled BOOL NOT NULL,
  autonomy_level STRING NOT NULL,
  labels_json JSON,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL,
  last_collection_at TIMESTAMP,
  last_collection_status STRING
) CLUSTER BY project_id;

CREATE TABLE IF NOT EXISTS `${DATASET}.jobs_history` (
  collected_at TIMESTAMP NOT NULL,
  project_id STRING NOT NULL,
  location STRING NOT NULL,
  creation_time TIMESTAMP NOT NULL,
  start_time TIMESTAMP,
  end_time TIMESTAMP,
  job_id STRING NOT NULL,
  user_email STRING,
  statement_type STRING,
  total_bytes_processed INT64,
  total_bytes_billed INT64,
  total_slot_ms INT64,
  cache_hit BOOL,
  query STRING,
  query_fingerprint STRING,
  labels_json JSON,
  referenced_tables_json JSON,
  error_json JSON
) PARTITION BY DATE(creation_time)
  CLUSTER BY project_id, location, query_fingerprint
  OPTIONS(partition_expiration_days=540);

CREATE TABLE IF NOT EXISTS `${DATASET}.table_storage_history` (
  collected_at TIMESTAMP NOT NULL,
  project_id STRING NOT NULL,
  location STRING NOT NULL,
  table_schema STRING NOT NULL,
  table_name STRING NOT NULL,
  total_rows INT64,
  total_partitions INT64,
  total_logical_bytes INT64,
  total_physical_bytes INT64,
  active_logical_bytes INT64,
  long_term_logical_bytes INT64,
  time_travel_physical_bytes INT64,
  fail_safe_physical_bytes INT64,
  storage_last_modified_time TIMESTAMP
) PARTITION BY DATE(collected_at)
  CLUSTER BY project_id, location, table_schema, table_name;

CREATE TABLE IF NOT EXISTS `${DATASET}.native_recommendations` (
  collected_at TIMESTAMP NOT NULL,
  project_id STRING NOT NULL,
  location STRING NOT NULL,
  recommendation_id STRING NOT NULL,
  recommender STRING,
  subtype STRING,
  target_resources_json JSON,
  overview_json JSON,
  details_json JSON,
  state STRING,
  last_updated_time TIMESTAMP
) PARTITION BY DATE(collected_at)
  CLUSTER BY project_id, location, recommender
  OPTIONS(partition_expiration_days=365);

CREATE TABLE IF NOT EXISTS `${DATASET}.findings` (
  finding_id STRING NOT NULL,
  detected_at TIMESTAMP NOT NULL,
  project_id STRING NOT NULL,
  location STRING NOT NULL,
  category STRING NOT NULL,
  target STRING NOT NULL,
  title STRING NOT NULL,
  evidence_json JSON NOT NULL,
  expected_monthly_savings FLOAT64 NOT NULL,
  confidence FLOAT64 NOT NULL,
  effort INT64 NOT NULL,
  risk INT64 NOT NULL,
  impact INT64 NOT NULL,
  priority_score FLOAT64 NOT NULL,
  status STRING NOT NULL
) PARTITION BY DATE(detected_at)
  CLUSTER BY status, project_id, category;

CREATE TABLE IF NOT EXISTS `${DATASET}.action_ledger` (
  action_id STRING NOT NULL,
  finding_id STRING NOT NULL,
  project_id STRING NOT NULL,
  action_type STRING NOT NULL,
  payload_json JSON NOT NULL,
  requested_by STRING NOT NULL,
  rollback_plan STRING NOT NULL,
  state STRING NOT NULL,
  decided_by STRING,
  decision_reason STRING,
  result_json JSON,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL,
  decided_at TIMESTAMP,
  executed_at TIMESTAMP
) PARTITION BY DATE(created_at)
  CLUSTER BY project_id, state, action_type;

CREATE TABLE IF NOT EXISTS `${DATASET}.outcomes` (
  recommendation_id STRING NOT NULL,
  project_id STRING NOT NULL,
  measured_at TIMESTAMP NOT NULL,
  expected_monthly_savings FLOAT64,
  realized_monthly_savings FLOAT64,
  cost_before FLOAT64,
  cost_after FLOAT64,
  latency_change_percent FLOAT64,
  error_rate_change_percent FLOAT64,
  notes STRING
) PARTITION BY DATE(measured_at)
  CLUSTER BY project_id, recommendation_id;
