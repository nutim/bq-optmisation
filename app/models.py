"""API and persistence contracts."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import get_settings


class AutonomyLevel(StrEnum):
    OBSERVE = "A0"
    DRAFT = "A1"
    APPROVAL_REQUIRED = "A2"
    BOUNDED_AUTOMATION = "A3"


class ProjectRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
    locations: list[str] = Field(min_length=1)
    reader_service_account: str
    executor_service_account: str | None = None
    owner_email: str
    enabled: bool = True
    autonomy_level: AutonomyLevel = AutonomyLevel.OBSERVE
    labels: dict[str, str] = Field(default_factory=dict)

    @field_validator("locations")
    @classmethod
    def normalize_locations(cls, values: list[str]) -> list[str]:
        normalized = sorted({v.strip().lower() for v in values if v.strip()})
        if not normalized:
            raise ValueError("at least one non-empty BigQuery location is required")
        return normalized

    @field_validator("reader_service_account", "executor_service_account")
    @classmethod
    def validate_service_account(cls, value: str | None) -> str | None:
        if value is not None and not value.endswith(".iam.gserviceaccount.com"):
            raise ValueError("must be a service-account email")
        return value

    @model_validator(mode="after")
    def validate_service_account_project(self) -> "ProjectRegistration":
        """Reject target identities from unrelated projects unless allowlisted."""
        allowed = {
            project.strip()
            for project in get_settings().allowed_sa_projects.split(",")
            if project.strip()
        }
        for field in ("reader_service_account", "executor_service_account"):
            value = getattr(self, field)
            if value is None:
                continue
            sa_project = value.rsplit("@", 1)[1].removesuffix(".iam.gserviceaccount.com")
            if sa_project != self.project_id and sa_project not in allowed:
                raise ValueError(
                    f"{field} must belong to the registered project {self.project_id!r} "
                    f"or a project listed in FINOPS_ALLOWED_SA_PROJECTS"
                )
        return self


class RegisteredProject(ProjectRegistration):
    created_at: datetime
    updated_at: datetime
    last_collection_at: datetime | None = None
    last_collection_status: str | None = None


class FindingStatus(StrEnum):
    OPEN = "OPEN"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    VERIFIED = "VERIFIED"
    EXPIRED = "EXPIRED"


class Finding(BaseModel):
    finding_id: str
    project_id: str
    location: str
    category: str
    target: str
    title: str
    evidence: dict[str, object]
    expected_monthly_savings: float = 0
    confidence: float = Field(ge=0, le=1)
    effort: int = Field(ge=1, le=5)
    risk: int = Field(ge=1, le=5)
    impact: int = Field(ge=1, le=5)
    priority_score: float
    status: FindingStatus = FindingStatus.OPEN


class ApprovalDecision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class ActionType(StrEnum):
    CREATE_CHANGE_REQUEST = "CREATE_CHANGE_REQUEST"
    CREATE_JIRA_TICKET = "CREATE_JIRA_TICKET"
    CREATE_CONFLUENCE_PAGE = "CREATE_CONFLUENCE_PAGE"
    CREATE_GITLAB_MR = "CREATE_GITLAB_MR"
    SET_TABLE_EXPIRATION = "SET_TABLE_EXPIRATION"
    CANCEL_JOB = "CANCEL_JOB"


class SqlChangeDetails(BaseModel):
    """Auditable implementation details shared by tickets, pages, and merge requests."""

    model_config = ConfigDict(extra="forbid")

    current_sql: str = Field(min_length=1)
    proposed_sql: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    validation_plan: str = Field(min_length=1)
    rollback_plan: str = Field(min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1)


class JiraTicketPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cloud_id: str = Field(min_length=1)
    project_key: str = Field(min_length=1)
    issue_type: str = Field(default="Task", min_length=1)
    summary: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1)
    labels: list[str] = Field(default_factory=lambda: ["bigquery", "finops"])


class ConfluencePagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cloud_id: str = Field(min_length=1)
    space_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1)
    parent_id: str | None = None


class GitLabFileAction(BaseModel):
    """Intentionally excludes delete/move/chmod from agent-created changes."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["create", "update"]
    file_path: str = Field(min_length=1)
    content: str | None = None
    old_str: str | None = None
    new_str: str | None = None

    @field_validator("file_path")
    @classmethod
    def reject_unsafe_path(cls, value: str) -> str:
        if value.startswith("/") or ".." in value.split("/"):
            raise ValueError("file_path must be repository-relative and cannot contain '..'")
        return value

    @model_validator(mode="after")
    def validate_edit(self) -> "GitLabFileAction":
        if self.action == "create" and self.content is None:
            raise ValueError("create action requires content")
        if (
            self.action == "update"
            and self.content is None
            and not (self.old_str is not None and self.new_str is not None)
        ):
            raise ValueError("update requires content or both old_str and new_str")
        return self


class GitLabMergeRequestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1)
    source_branch: str = Field(min_length=1)
    target_branch: str = Field(default="main", min_length=1)
    commit_message: str = Field(min_length=1)
    actions: list[GitLabFileAction] = Field(min_length=1)
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1)
    labels: list[str] = Field(default_factory=lambda: ["bigquery", "finops"])
    squash: bool = True

    @model_validator(mode="after")
    def require_distinct_branches(self) -> "GitLabMergeRequestPayload":
        if self.source_branch == self.target_branch:
            raise ValueError("source_branch and target_branch must differ")
        return self


class JiraDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str
    change: SqlChangeDetails
    cloud_id: str
    jira_project_key: str
    requested_by: str
    issue_type: str = "Task"


class ConfluenceDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str
    change: SqlChangeDetails
    cloud_id: str
    space_id: str
    requested_by: str
    parent_id: str | None = None


class GitLabDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str
    change: SqlChangeDetails
    gitlab_project_id: str
    source_branch: str
    target_branch: str = "main"
    commit_message: str
    actions: list[GitLabFileAction] = Field(min_length=1)
    title: str
    requested_by: str
    labels: list[str] = Field(default_factory=lambda: ["bigquery", "finops"])
    squash: bool = True


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str
    project_id: str
    action_type: ActionType
    payload: dict[str, object]
    requested_by: str
    rollback_plan: str

    @model_validator(mode="after")
    def validate_delivery_payload(self) -> "ActionRequest":
        model: BaseModel | None = None
        document: str | None = None
        if self.action_type == ActionType.CREATE_JIRA_TICKET:
            model = JiraTicketPayload.model_validate(self.payload)
            document = model.description
        elif self.action_type == ActionType.CREATE_CONFLUENCE_PAGE:
            model = ConfluencePagePayload.model_validate(self.payload)
            document = model.body
        elif self.action_type == ActionType.CREATE_GITLAB_MR:
            model = GitLabMergeRequestPayload.model_validate(self.payload)
            document = model.description
        if model is not None:
            self.payload = model.model_dump()
            required = (
                "## Current SQL",
                "## Proposed SQL",
                "## Validation plan",
                "## Rollback plan",
            )
            if not document or any(heading not in document for heading in required):
                raise ValueError(
                    "delivery payload must contain the complete generated SQL document"
                )
        return self


class Approval(BaseModel):
    action_id: str
    decision: ApprovalDecision
    decided_by: str
    reason: str


class CollectionEvent(BaseModel):
    project_id: str
    reason: str = "scheduled"
    requested_locations: list[str] | None = None
