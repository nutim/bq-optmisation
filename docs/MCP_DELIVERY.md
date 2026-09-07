# MCP delivery architecture

## Outcome

An engineer or the ADK agent can draft a complete SQL implementation package for Jira,
Confluence, or GitLab. Drafting writes only to the BigQuery action ledger. A different authorized
principal must approve it, and a later execution request performs the external mutation.

```text
Finding + SQL proposal
        |
        v
Deterministic change document
        |
        v
PENDING_APPROVAL action ledger
        |
        | human/policy approval
        v
Cloud Run executor ---> Secret Manager ---> Atlassian or GitLab MCP
        |
        v
EXECUTED / FAILED result in ledger
```

MCP tools are not registered as direct LLM tools. The agent can call only the draft function,
which prevents a prompt or malicious SQL comment from bypassing approval.

## Connector operations

| Destination | MCP operation | Result |
|---|---|---|
| Jira | `createJiraIssue` | One issue with full SQL implementation details |
| Confluence | `createConfluencePage` | One review/runbook page |
| GitLab | `add_commit`, then `save_merge_request` | One branch commit and one open MR |

GitLab execution deliberately has no merge operation. File actions are restricted to create and
update, paths must be repository-relative, and source and target branches must differ.

## Authentication and IAM

1. Create connector identities in Atlassian and GitLab with access only to the intended Jira
   projects, Confluence spaces, and GitLab repositories.
2. Complete the connector's OAuth flow or create the organization-approved bearer credential.
3. Store the resulting bearer value in an existing Secret Manager secret in the control project.
4. Pass only the secret ID to Terraform. Terraform grants the Cloud Run app service account
   `roles/secretmanager.secretAccessor` on that individual secret.
5. Do not grant the external connector identity any BigQuery IAM role. BigQuery reads and writes
   continue to use per-project service-account impersonation.

For strict separation of duties, the principal allowed to call the decision endpoint should not
be the connector identity or the agent runtime identity. Put Cloud Run behind IAP/API Gateway or
an internal service that authenticates end users and enforces approver groups.

## Terraform configuration

```hcl
atlassian_mcp_secret_id = "atlassian-mcp"
gitlab_mcp_url          = "https://gitlab.example.com/api/v4/mcp"
gitlab_mcp_secret_id    = "gitlab-mcp"
```

The secrets must already exist. Their values are intentionally not Terraform inputs.

## Jira draft example

```json
POST /v1/delivery/jira
{
  "finding_id": "FINDING_ID",
  "cloud_id": "ATLASSIAN_CLOUD_ID",
  "jira_project_key": "DATA",
  "issue_type": "Task",
  "requested_by": "engineer@example.com",
  "change": {
    "current_sql": "SELECT * FROM `prod.sales.orders`",
    "proposed_sql": "SELECT order_id, total FROM `prod.sales.orders` WHERE order_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)",
    "rationale": "Avoid scanning unused columns and historical partitions.",
    "validation_plan": "Dry-run both statements; compare row counts and keyed output on a fixed date range.",
    "rollback_plan": "Revert the SQL commit and redeploy the previous job version.",
    "acceptance_criteria": [
      "Results match the approved business definition",
      "Dry-run bytes fall by at least 80%",
      "The scheduled job completes within its SLA"
    ]
  }
}
```

The equivalent Confluence request replaces the Jira fields with `space_id` and optional
`parent_id`.

## GitLab MR draft example

```json
POST /v1/delivery/gitlab-mr
{
  "finding_id": "FINDING_ID",
  "gitlab_project_id": "data/warehouse",
  "source_branch": "finops/FINDING_ID",
  "target_branch": "main",
  "commit_message": "Optimize weekly orders query",
  "title": "Reduce BigQuery scan for weekly orders",
  "requested_by": "engineer@example.com",
  "actions": [
    {
      "action": "update",
      "file_path": "sql/orders.sql",
      "old_str": "SELECT * FROM `prod.sales.orders`",
      "new_str": "SELECT order_id, total FROM `prod.sales.orders` WHERE order_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)"
    }
  ],
  "change": {
    "current_sql": "SELECT * FROM `prod.sales.orders`",
    "proposed_sql": "SELECT order_id, total FROM `prod.sales.orders` WHERE order_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)",
    "rationale": "Reduce scanned columns and partitions.",
    "validation_plan": "Dry-run, compare outputs, then run the repository test suite.",
    "rollback_plan": "Revert the MR commit.",
    "acceptance_criteria": ["Output equivalence is reviewed", "Bytes processed fall by 80%"]
  }
}
```

The response is a `PENDING_APPROVAL` action ID. Approve it through
`POST /v1/actions/{id}/decision`, then call `POST /v1/actions/{id}/execute`.

## Failure and idempotency behavior

Execution first atomically changes the ledger row from `APPROVED` to `EXECUTING`, preventing
concurrent or repeated execution of the same action. MCP errors are written as `FAILED`. A GitLab
failure after `add_commit` but before `save_merge_request` can leave a branch and commit without an
MR. Operators should inspect the existing branch before creating a replacement action. Use unique
finding-based branch names and add reconciliation that detects an existing branch/MR first.

Jira and Confluence creation can similarly succeed remotely before a response is lost. Add an
external-id label/property based on the action ID if strict create-once behavior is required by
your workflow.
