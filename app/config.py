"""Runtime configuration loaded from environment variables."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FINOPS_", case_sensitive=False)

    control_project_id: str
    control_dataset: str = "finops_control"
    control_location: str = "EU"
    collection_topic: str = "bigquery-finops-collection"
    model: str = "gemini-3.7-flash"
    vertex_location: str = "global"
    lookback_days: int = Field(default=30, ge=1, le=180)
    expensive_query_bytes: int = Field(default=1_000_000_000_000, ge=1)
    large_table_bytes: int = Field(default=1_000_000_000_000, ge=1)
    unused_table_days: int = Field(default=90, ge=7)
    max_result_rows: int = Field(default=5000, ge=1, le=50_000)
    on_demand_usd_per_tib: float = Field(default=6.25, ge=0)
    active_storage_usd_per_gib_month: float = Field(default=0.02, ge=0)
    savings_ratio_expensive_query: float = Field(default=0.25, ge=0, le=1)
    savings_ratio_select_star: float = Field(default=0.15, ge=0, le=1)
    savings_ratio_large_table: float = Field(default=0.2, ge=0, le=1)
    savings_ratio_partition_scan: float = Field(default=0.5, ge=0, le=1)
    savings_ratio_cache_miss: float = Field(default=0.3, ge=0, le=1)
    savings_ratio_time_travel: float = Field(default=0.5, ge=0, le=1)
    min_cache_executions: int = Field(default=10, ge=2)
    cache_miss_query_bytes: int = Field(default=100_000_000_000, ge=1)
    partition_scan_ratio: float = Field(default=0.9, gt=0, le=1)
    time_travel_overhead_ratio: float = Field(default=0.25, gt=0, le=1)
    time_travel_min_bytes: int = Field(default=10_000_000_000, ge=0)
    commitment_review_usd: float = Field(default=5_000, ge=0)
    allowed_sa_projects: str = ""
    verify_pubsub_tokens: bool = True
    pubsub_invoker_service_account: str | None = None
    service_url: str | None = None
    serve_adk_web_ui: bool = False
    session_service_uri: str | None = None
    trace_to_cloud: bool = True
    log_level: str = "INFO"
    atlassian_mcp_url: str = "https://mcp.atlassian.com/v1/mcp"
    atlassian_mcp_auth_secret: str | None = None
    gitlab_mcp_url: str | None = None
    gitlab_mcp_auth_secret: str | None = None
    mcp_timeout_seconds: int = Field(default=60, ge=5, le=300)

    @property
    def dataset_ref(self) -> str:
        return f"{self.control_project_id}.{self.control_dataset}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
