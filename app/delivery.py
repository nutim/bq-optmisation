"""Deterministic SQL change documents and approval-bound delivery drafts."""

import json
from typing import Any

from app.models import (
    ActionRequest,
    ActionType,
    ConfluencePagePayload,
    GitLabMergeRequestPayload,
    JiraTicketPayload,
    SqlChangeDetails,
)


def sql_change_description(finding: dict[str, Any], change: SqlChangeDetails) -> str:
    evidence = finding.get("evidence_json", finding.get("evidence", {}))
    checklist = "\n".join(f"- [ ] {item}" for item in change.acceptance_criteria)
    return f"""# BigQuery optimization: {finding["title"]}

## Scope
- BigQuery project: `{finding["project_id"]}`
- Location: `{finding["location"]}`
- Target: `{finding["target"]}`
- Finding ID: `{finding["finding_id"]}`
- Expected monthly savings: `{float(finding.get("expected_monthly_savings", 0)):.2f}`
- Confidence: `{float(finding.get("confidence", 0)):.2f}`
- Risk: `{finding.get("risk", "unknown")}/5`

## Evidence
```json
{json.dumps(evidence, indent=2, default=str)}
```

## Rationale
{change.rationale}

## Current SQL
```sql
{change.current_sql}
```

## Proposed SQL
```sql
{change.proposed_sql}
```

## Validation plan
{change.validation_plan}

## Rollback plan
{change.rollback_plan}

## Acceptance criteria
{checklist}

## Required review
Confirm output equivalence, data freshness, privacy controls, downstream dependencies,
query dry-run bytes, and rollback readiness before merge or deployment.
"""


def jira_action(
    finding: dict[str, Any],
    change: SqlChangeDetails,
    *,
    cloud_id: str,
    jira_project_key: str,
    requested_by: str,
    issue_type: str = "Task",
) -> ActionRequest:
    payload = JiraTicketPayload(
        cloud_id=cloud_id,
        project_key=jira_project_key,
        issue_type=issue_type,
        summary=f"[BigQuery FinOps] {finding['title']}",
        description=sql_change_description(finding, change),
    )
    return ActionRequest(
        finding_id=str(finding["finding_id"]),
        project_id=str(finding["project_id"]),
        action_type=ActionType.CREATE_JIRA_TICKET,
        payload=payload.model_dump(),
        requested_by=requested_by,
        rollback_plan="Ticket creation has no data-plane effect; close the ticket if withdrawn.",
    )


def confluence_action(
    finding: dict[str, Any],
    change: SqlChangeDetails,
    *,
    cloud_id: str,
    space_id: str,
    requested_by: str,
    parent_id: str | None = None,
) -> ActionRequest:
    payload = ConfluencePagePayload(
        cloud_id=cloud_id,
        space_id=space_id,
        title=f"BigQuery FinOps - {finding['title']}",
        body=sql_change_description(finding, change),
        parent_id=parent_id,
    )
    return ActionRequest(
        finding_id=str(finding["finding_id"]),
        project_id=str(finding["project_id"]),
        action_type=ActionType.CREATE_CONFLUENCE_PAGE,
        payload=payload.model_dump(),
        requested_by=requested_by,
        rollback_plan="Archive the generated page if the proposal is withdrawn.",
    )


def gitlab_action(
    finding: dict[str, Any],
    change: SqlChangeDetails,
    payload: GitLabMergeRequestPayload,
    requested_by: str,
) -> ActionRequest:
    enriched = payload.model_copy(update={"description": sql_change_description(finding, change)})
    return ActionRequest(
        finding_id=str(finding["finding_id"]),
        project_id=str(finding["project_id"]),
        action_type=ActionType.CREATE_GITLAB_MR,
        payload=enriched.model_dump(),
        requested_by=requested_by,
        rollback_plan=change.rollback_plan,
    )
