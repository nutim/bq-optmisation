from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.delivery import sql_change_description
from app.executor import ActionExecutor
from app.models import GitLabFileAction, SqlChangeDetails


def finding() -> dict[str, Any]:
    return {
        "finding_id": "finding-1",
        "project_id": "analytics-production",
        "location": "eu",
        "target": "job-query-1",
        "title": "Partition filter missing",
        "evidence_json": {"bytes_billed": 1_000_000},
        "expected_monthly_savings": 125.5,
        "confidence": 0.9,
        "risk": 2,
    }


def change() -> SqlChangeDetails:
    return SqlChangeDetails(
        current_sql="SELECT * FROM sales",
        proposed_sql="SELECT id FROM sales WHERE sale_date = CURRENT_DATE()",
        rationale="Reduce full-table scans.",
        validation_plan="Dry-run both queries and compare sampled outputs.",
        rollback_plan="Revert this commit.",
        acceptance_criteria=["Output is equivalent", "Bytes processed fall by at least 80%"],
    )


def test_description_contains_complete_sql_change_context() -> None:
    description = sql_change_description(finding(), change())
    assert "## Current SQL" in description
    assert "## Proposed SQL" in description
    assert "## Validation plan" in description
    assert "## Rollback plan" in description
    assert "- [ ] Output is equivalent" in description
    assert "analytics-production" in description


def test_gitlab_actions_reject_delete_and_unsafe_paths() -> None:
    with pytest.raises(ValidationError):
        GitLabFileAction(action="delete", file_path="query.sql")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        GitLabFileAction(action="create", file_path="../query.sql", content="SELECT 1")


class FakeStore:
    def __init__(self) -> None:
        self.marked: tuple[str, str, dict[str, object]] | None = None

    def get_action(self, action_id: str) -> dict[str, Any]:
        return {
            "action_id": action_id,
            "state": "APPROVED",
            "project_id": "analytics-production",
            "action_type": "CREATE_GITLAB_MR",
            "payload_json": {
                "project_id": "group/warehouse",
                "source_branch": "finops/finding-1",
                "target_branch": "main",
                "commit_message": "Optimize sales query",
                "actions": [
                    {"action": "update", "file_path": "sql/sales.sql", "content": "SELECT 1"}
                ],
                "title": "Optimize sales query",
                "description": "reviewed description",
                "labels": ["bigquery", "finops"],
                "squash": True,
            },
        }

    def get_project(self, project_id: str) -> dict[str, Any]:
        return {"project_id": project_id, "enabled": True, "executor_service_account": None}

    def claim_action(self, action_id: str) -> bool:
        return True

    def mark_action(self, action_id: str, state: str, result: dict[str, object]) -> None:
        self.marked = (action_id, state, result)


class FakeMcp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(
        self, server_url: str, secret_resource: str, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((tool_name, arguments))
        return {"tool": tool_name, "structured_content": {"ok": True}, "content": []}


def test_gitlab_execution_commits_then_opens_mr_without_target_sa() -> None:
    store = FakeStore()
    gateway = FakeMcp()
    settings = Settings(
        control_project_id="test-control",
        gitlab_mcp_url="https://gitlab.example/api/v4/mcp",
        gitlab_mcp_auth_secret="projects/test/secrets/gitlab/versions/latest",
    )
    result = ActionExecutor(store, settings, gateway).execute("action-1")  # type: ignore[arg-type]
    assert [name for name, _ in gateway.calls] == ["add_commit", "save_merge_request"]
    assert gateway.calls[0][1]["start_branch"] == "main"
    assert gateway.calls[1][1]["source_branch"] == "finops/finding-1"
    assert result["auto_merge"] is False
    assert store.marked and store.marked[1] == "EXECUTED"
