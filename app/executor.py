"""Approval-bound BigQuery and MCP delivery actions."""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from google.cloud import bigquery

from app.auth import BIGQUERY_WRITE_SCOPES, impersonate
from app.config import Settings, get_settings
from app.logging import get_logger
from app.mcp_client import McpGateway
from app.models import ConfluencePagePayload, GitLabMergeRequestPayload, JiraTicketPayload
from app.store import ControlStore

log = get_logger(__name__)


class ActionExecutor:
    def __init__(
        self,
        store: ControlStore,
        settings: Settings | None = None,
        mcp: McpGateway | None = None,
    ) -> None:
        self.store = store
        self.settings = settings or get_settings()
        self.mcp = mcp or McpGateway(self.settings.mcp_timeout_seconds)

    def execute(self, action_id: str) -> dict[str, Any]:
        action = self.store.get_action(action_id)
        if not action:
            raise ValueError("action not found")
        if action["state"] != "APPROVED":
            raise PermissionError("action must be explicitly approved")
        registration = self.store.get_project(action["project_id"])
        if not registration or not registration["enabled"]:
            raise PermissionError("target project is not enabled")
        if not self.store.claim_action(action_id):
            raise PermissionError("action was already claimed or is no longer approved")
        payload = action["payload_json"]
        if isinstance(payload, str):
            payload = json.loads(payload)

        try:
            if action["action_type"] in {"SET_TABLE_EXPIRATION", "CANCEL_JOB"}:
                executor_sa = registration.get("executor_service_account")
                if not executor_sa:
                    raise PermissionError("no executor service account is registered")
                credentials = impersonate(str(executor_sa), BIGQUERY_WRITE_SCOPES)
                client = bigquery.Client(project=action["project_id"], credentials=credentials)
            if action["action_type"] == "SET_TABLE_EXPIRATION":
                result = self._set_table_expiration(client, payload)
            elif action["action_type"] == "CANCEL_JOB":
                result = self._cancel_job(client, payload)
            elif action["action_type"] == "CREATE_CHANGE_REQUEST":
                result = self._record_change_request(payload)
            elif action["action_type"] == "CREATE_JIRA_TICKET":
                result = self._create_jira_ticket(payload)
            elif action["action_type"] == "CREATE_CONFLUENCE_PAGE":
                result = self._create_confluence_page(payload)
            elif action["action_type"] == "CREATE_GITLAB_MR":
                result = self._create_gitlab_mr(payload)
            else:
                raise ValueError(f"unsupported action: {action['action_type']}")
            self.store.mark_action(action_id, "EXECUTED", result)
            log.info(
                "action_executed",
                action_id=action_id,
                action_type=action["action_type"],
                project_id=action["project_id"],
            )
            return result
        except Exception as exc:
            self.store.mark_action(
                action_id,
                "FAILED",
                {"error": type(exc).__name__, "message": str(exc)},
            )
            log.error(
                "action_failed",
                action_id=action_id,
                action_type=str(action["action_type"]),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise

    def _set_table_expiration(
        self, client: bigquery.Client, payload: dict[str, Any]
    ) -> dict[str, Any]:
        table_ref = str(payload["table_ref"])
        expires_at = datetime.fromisoformat(str(payload["expires_at"]).replace("Z", "+00:00"))
        if expires_at <= datetime.now(UTC):
            raise ValueError("expiration must be in the future")
        table = client.get_table(table_ref)
        previous = table.expires.isoformat() if table.expires else None
        table.expires = expires_at
        client.update_table(table, ["expires"])
        return {
            "table_ref": table_ref,
            "previous_expiration": previous,
            "new_expiration": expires_at.isoformat(),
        }

    def _cancel_job(self, client: bigquery.Client, payload: dict[str, Any]) -> dict[str, Any]:
        location = str(payload["location"])
        job_id = str(payload["job_id"])
        client.cancel_job(job_id, location=location)
        return {"job_id": job_id, "location": location, "cancel_requested": True}

    def _record_change_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Repository-provider integration is intentionally decoupled. This creates the
        # immutable reviewed artifact that a GitHub/GitLab/Cloud Build adapter consumes.
        required = {"repository", "path", "proposed_content"}
        if not required.issubset(payload):
            raise ValueError(f"change request requires {sorted(required)}")
        return {
            "status": "READY_FOR_REPOSITORY_ADAPTER",
            "repository": payload["repository"],
            "path": payload["path"],
            "content_sha256": __import__("hashlib")
            .sha256(str(payload["proposed_content"]).encode())
            .hexdigest(),
        }

    def _mcp_call(
        self,
        server_url: str | None,
        secret_resource: str | None,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if not server_url or not secret_resource:
            raise ValueError(f"{tool_name} MCP connector is not configured")
        return asyncio.run(self.mcp.call_tool(server_url, secret_resource, tool_name, arguments))

    def _create_jira_ticket(self, raw: dict[str, Any]) -> dict[str, Any]:
        payload = JiraTicketPayload.model_validate(raw)
        return self._mcp_call(
            self.settings.atlassian_mcp_url,
            self.settings.atlassian_mcp_auth_secret,
            "createJiraIssue",
            {
                "cloudId": payload.cloud_id,
                "projectKey": payload.project_key,
                "issueTypeName": payload.issue_type,
                "summary": payload.summary,
                "description": payload.description,
                "additional_fields": {"labels": payload.labels},
            },
        )

    def _create_confluence_page(self, raw: dict[str, Any]) -> dict[str, Any]:
        payload = ConfluencePagePayload.model_validate(raw)
        arguments: dict[str, Any] = {
            "cloudId": payload.cloud_id,
            "spaceId": payload.space_id,
            "title": payload.title,
            "body": payload.body,
        }
        if payload.parent_id:
            arguments["parentId"] = payload.parent_id
        return self._mcp_call(
            self.settings.atlassian_mcp_url,
            self.settings.atlassian_mcp_auth_secret,
            "createConfluencePage",
            arguments,
        )

    def _create_gitlab_mr(self, raw: dict[str, Any]) -> dict[str, Any]:
        payload = GitLabMergeRequestPayload.model_validate(raw)
        commit = self._mcp_call(
            self.settings.gitlab_mcp_url,
            self.settings.gitlab_mcp_auth_secret,
            "add_commit",
            {
                "project_id": payload.project_id,
                "branch": payload.source_branch,
                "start_branch": payload.target_branch,
                "commit_message": payload.commit_message,
                "actions": [action.model_dump(exclude_none=True) for action in payload.actions],
            },
        )
        merge_request = self._mcp_call(
            self.settings.gitlab_mcp_url,
            self.settings.gitlab_mcp_auth_secret,
            "save_merge_request",
            {
                "project_id": payload.project_id,
                "source_branch": payload.source_branch,
                "target_branch": payload.target_branch,
                "title": payload.title,
                "description": payload.description,
                "labels": payload.labels,
                "squash": payload.squash,
            },
        )
        return {"commit": commit, "merge_request": merge_request, "auto_merge": False}
