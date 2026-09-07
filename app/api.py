"""Combined ADK and FinOps control API for Cloud Run."""

from pathlib import Path
from typing import Annotated, Any

from fastapi import Header, HTTPException, Query, status
from google.adk.cli.fast_api import get_fast_api_app

from app.collector import ProjectCollector
from app.config import get_settings
from app.delivery import confluence_action, gitlab_action, jira_action
from app.detectors import DetectorService
from app.events import (
    decode_push_envelope,
    get_publisher,
    verify_pubsub_oidc,
)
from app.executor import ActionExecutor
from app.logging import configure_logging, get_logger
from app.models import (
    ActionRequest,
    Approval,
    CollectionEvent,
    ConfluenceDraftRequest,
    GitLabDraftRequest,
    GitLabMergeRequestPayload,
    JiraDraftRequest,
    ProjectRegistration,
)
from app.store import ControlStore, get_store

settings = get_settings()
configure_logging(settings.log_level)
log = get_logger(__name__)

api = get_fast_api_app(
    agents_dir=str(Path(__file__).resolve().parent.parent),
    web=settings.serve_adk_web_ui,
    session_service_uri=settings.session_service_uri,
    trace_to_cloud=settings.trace_to_cloud,
)
api.title = "BigQuery FinOps Agent"
api.description = "Multi-project BigQuery optimization control plane and ADK API"


def store() -> ControlStore:
    return get_store()


@api.on_event("startup")
def initialize_schema() -> None:
    store().initialize()


@api.get("/healthz")
def health() -> dict[str, str]:
    return {"status": "ok"}


@api.post("/v1/projects", status_code=status.HTTP_202_ACCEPTED)
def register_project(registration: ProjectRegistration) -> dict[str, Any]:
    """Add/update a project and trigger immediate collection when enabled."""
    repo = store()
    repo.upsert_project(registration)
    message_id = None
    if registration.enabled:
        message_id = get_publisher().publish_collection(
            CollectionEvent(
                project_id=registration.project_id,
                reason="project_registered",
                requested_locations=registration.locations,
            )
        )
    return {
        "project_id": registration.project_id,
        "enabled": registration.enabled,
        "collection_message_id": message_id,
    }


@api.get("/v1/projects")
def list_projects(enabled_only: bool = False) -> list[dict[str, Any]]:
    return store().list_projects(enabled_only=enabled_only)


@api.post("/v1/projects/{project_id}/collect", status_code=status.HTTP_202_ACCEPTED)
def request_collection(project_id: str) -> dict[str, str]:
    repo = store()
    project = repo.get_project(project_id)
    if not project or not project["enabled"]:
        raise HTTPException(status_code=404, detail="registered enabled project not found")
    message_id = get_publisher().publish_collection(
        CollectionEvent(project_id=project_id, reason="manual")
    )
    return {"project_id": project_id, "message_id": message_id}


@api.post("/v1/collect/all", status_code=status.HTTP_202_ACCEPTED)
def request_all_collections() -> dict[str, Any]:
    publisher = get_publisher()
    messages = {
        project["project_id"]: publisher.publish_collection(
            CollectionEvent(project_id=project["project_id"], reason="scheduled")
        )
        for project in store().list_projects(enabled_only=True)
    }
    return {"published": len(messages), "messages": messages}


def _require_push_auth(authorization: str | None) -> None:
    if not settings.verify_pubsub_tokens:
        return
    token = authorization[7:].strip() if authorization and authorization.startswith(
        "Bearer "
    ) else (authorization or "")
    if not token:
        raise HTTPException(status_code=401, detail="Pub/Sub OIDC token is required")
    try:
        verify_pubsub_oidc(token, settings)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@api.post("/v1/events/collection")
def collect_from_pubsub(
    envelope: dict[str, object],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Pub/Sub push worker.

    Permanent failures (malformed events, unregistered projects) are acked with
    HTTP 200 so Pub/Sub does not redeliver them to the dead-letter topic.
    Transient failures return HTTP 500 so Pub/Sub retries with backoff.
    """
    _require_push_auth(authorization)
    try:
        event = decode_push_envelope(envelope)
    except ValueError:
        log.warning("pubsub_event_rejected", reason="invalid_envelope")
        return {"status": "rejected", "reason": "invalid Pub/Sub push envelope"}
    repo = store()
    project = repo.get_project(event.project_id)
    if not project or not project["enabled"]:
        log.warning(
            "pubsub_event_rejected", project_id=event.project_id, reason="not_registered_enabled"
        )
        return {
            "status": "rejected",
            "reason": "project is not registered and enabled",
        }
    try:
        result = ProjectCollector(repo, settings).collect(
            project, locations=event.requested_locations
        )
        detections = DetectorService(repo, settings).run(event.project_id)
    except Exception as exc:
        log.error(
            "pubsub_collection_failed",
            project_id=event.project_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise HTTPException(
            status_code=500, detail="collection failed; Pub/Sub will retry"
        ) from exc
    return {"collection": result, "detections": detections}


@api.post("/v1/detect")
def run_detectors(project_id: str | None = None) -> dict[str, int]:
    return DetectorService(store(), settings).run(project_id)


@api.get("/v1/findings")
def list_findings(
    project_id: str | None = None,
    finding_status: str = Query(default="OPEN", alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return store().list_findings(project_id, finding_status, limit)


@api.post("/v1/actions", status_code=status.HTTP_201_CREATED)
def create_action(request: ActionRequest) -> dict[str, str]:
    repo = store()
    if not repo.get_project(request.project_id):
        raise HTTPException(status_code=404, detail="registered project not found")
    try:
        action_id = repo.create_action(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"action_id": action_id, "state": "PENDING_APPROVAL"}


def _finding_or_404(finding_id: str) -> dict[str, Any]:
    finding = store().get_finding(finding_id)
    if not finding:
        raise HTTPException(status_code=404, detail="finding not found")
    return finding


@api.post("/v1/delivery/jira", status_code=status.HTTP_201_CREATED)
def draft_jira_ticket(request: JiraDraftRequest) -> dict[str, str]:
    repo = store()
    action = jira_action(
        _finding_or_404(request.finding_id),
        request.change,
        cloud_id=request.cloud_id,
        jira_project_key=request.jira_project_key,
        requested_by=request.requested_by,
        issue_type=request.issue_type,
    )
    return {"action_id": repo.create_action(action), "state": "PENDING_APPROVAL"}


@api.post("/v1/delivery/confluence", status_code=status.HTTP_201_CREATED)
def draft_confluence_page(request: ConfluenceDraftRequest) -> dict[str, str]:
    repo = store()
    action = confluence_action(
        _finding_or_404(request.finding_id),
        request.change,
        cloud_id=request.cloud_id,
        space_id=request.space_id,
        requested_by=request.requested_by,
        parent_id=request.parent_id,
    )
    return {"action_id": repo.create_action(action), "state": "PENDING_APPROVAL"}


@api.post("/v1/delivery/gitlab-mr", status_code=status.HTTP_201_CREATED)
def draft_gitlab_merge_request(request: GitLabDraftRequest) -> dict[str, str]:
    repo = store()
    finding = _finding_or_404(request.finding_id)
    payload = GitLabMergeRequestPayload(
        project_id=request.gitlab_project_id,
        source_branch=request.source_branch,
        target_branch=request.target_branch,
        commit_message=request.commit_message,
        actions=request.actions,
        title=request.title,
        description="Generated from approved BigQuery finding.",
        labels=request.labels,
        squash=request.squash,
    )
    action = gitlab_action(finding, request.change, payload, request.requested_by)
    return {"action_id": repo.create_action(action), "state": "PENDING_APPROVAL"}


@api.post("/v1/actions/{action_id}/decision")
def decide_action(action_id: str, approval: Approval) -> dict[str, str]:
    if approval.action_id != action_id:
        raise HTTPException(status_code=400, detail="action IDs do not match")
    if not store().decide_action(approval):
        raise HTTPException(
            status_code=404, detail="action not found or no longer pending approval"
        )
    log.info(
        "action_decided",
        action_id=action_id,
        decision=approval.decision.value,
        decided_by=approval.decided_by,
    )
    next_state = "APPROVED" if approval.decision.value == "APPROVE" else "REJECTED"
    return {"action_id": action_id, "state": next_state}


@api.post("/v1/actions/{action_id}/execute")
def execute_action(action_id: str) -> dict[str, Any]:
    try:
        return ActionExecutor(store(), settings).execute(action_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
