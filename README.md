# BigQuery FinOps Agent

A deployable, multi-project BigQuery cost and storage optimization agent for Google Cloud.

## What is implemented

- Registry-driven onboarding for any number of BigQuery projects.
- Immediate collection when an enabled project is added, plus daily scheduled collection.
- Per-project and per-region `INFORMATION_SCHEMA` collection.
- Service-account impersonation with no service-account keys.
- Separate read and execution identities for every target project.
- Job, table-storage, and native BigQuery recommendation history.
- Deterministic detectors for expensive recurring queries, `SELECT *`, full scans of
  partitioned tables (missing partition filters), repeated queries with zero cache hits,
  large-table lifecycle review, unused-table candidates, time-travel storage overhead,
  sustained spend commitment review, and native recommendations.
- ADK agent with interactive cost and finding tools.
- Query dry runs using the target project's impersonated reader identity.
- Approval ledger and restricted execution for table expiration and job cancellation.
- Optional Atlassian MCP delivery to create complete Jira tickets or Confluence pages.
- Optional GitLab MCP delivery that commits approved SQL changes and opens an MR without merging it.
- Cloud Run API, Pub/Sub fan-out, Cloud Scheduler, Terraform, Cloud Build, and tests.

## Security model

The Cloud Run control-plane service account has **no direct role in target data projects**.
For each registered project:

1. Security creates a target reader service account and optionally a target executor service account.
2. The target project grants permissions to those target identities.
3. The control-plane identity receives only `roles/iam.serviceAccountTokenCreator` on the specific target identities.
4. Collection obtains short-lived credentials for the registered reader identity.
5. Approved changes obtain separate short-lived credentials for the registered executor identity.
6. No service-account keys are created or stored.

MCP connector credentials are a separate security domain. The app service account receives
Secret Manager access only to the explicitly configured connector secrets. It does not reuse a
target BigQuery identity, and the connector's Jira/GitLab account should have least privilege.

The reader identity normally needs:

- permission to create BigQuery query jobs in the target project;
- project-level BigQuery resource metadata/job visibility;
- table metadata visibility;
- permission to view the relevant BigQuery Recommender results.

Use custom roles if the standard BigQuery roles expose more than company policy allows. The
executor identity should contain only the exact update/cancel permissions approved for that
project. It must not reuse the reader identity.

Target reader and executor service accounts must live in the registered project unless the
hosting project is listed in `FINOPS_ALLOWED_SA_PROJECTS`. This prevents registering an
identity from an unrelated project to exfiltrate its job history or metadata.

## Project onboarding

Adding an enabled project to the registry publishes a collection event immediately. Scheduled
runs also enumerate all enabled registry rows, so no static project list is compiled into the
application.

```bash
SERVICE_URL="https://YOUR-SERVICE-URL"
TOKEN="$(gcloud auth print-identity-token)"

curl -X POST "${SERVICE_URL}/v1/projects" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "analytics-production",
    "locations": ["eu", "europe-west1"],
    "reader_service_account": "bq-finops-reader@analytics-production.iam.gserviceaccount.com",
    "executor_service_account": "bq-finops-executor@analytics-production.iam.gserviceaccount.com",
    "owner_email": "data-platform@example.com",
    "enabled": true,
    "autonomy_level": "A0",
    "labels": {"cost_center": "data", "environment": "production"}
  }'
```

The response contains the Pub/Sub message ID for the initial collection. Use `enabled=false`
to register configuration without collecting it.

## Local setup

Prerequisites:

- Python 3.12+
- `uv`
- Google Cloud Application Default Credentials
- A control project and an existing `finops_control` dataset, or permission to create it

```bash
uv sync --dev

export FINOPS_CONTROL_PROJECT_ID="your-finops-control-project"
export FINOPS_CONTROL_DATASET="finops_control"
export FINOPS_CONTROL_LOCATION="EU"
export FINOPS_TRACE_TO_CLOUD="false" # use true when local ADC may write Cloud Trace
export FINOPS_VERIFY_PUBSUB_TOKENS="false" # keep true in production
export GOOGLE_CLOUD_PROJECT="your-finops-control-project"
export GOOGLE_GENAI_USE_VERTEXAI="TRUE"

uv run python -m app.cli init
uv run pytest
uv run uvicorn app.api:api --reload --port 8080
```

The standard ADK endpoints are served by the same application. For a local interactive UI:

```bash
export FINOPS_SERVE_ADK_WEB_UI="true"
uv run uvicorn app.api:api --reload --port 8080
```

Do not enable the development UI on the production Cloud Run service.

## Deploy

Build and push an immutable container image, then provide its digest to Terraform:

```bash
cd infra/terraform
terraform init
terraform apply \
  -var="control_project_id=YOUR_CONTROL_PROJECT" \
  -var="container_image=europe-west1-docker.pkg.dev/PROJECT/REPOSITORY/IMAGE@sha256:DIGEST"
```

To let Terraform manage only the impersonation trust—not target BigQuery roles—supply:

```hcl
target_service_accounts = {
  analytics_reader = {
    project_id = "analytics-production"
    email      = "bq-finops-reader@analytics-production.iam.gserviceaccount.com"
  }
  analytics_executor = {
    project_id = "analytics-production"
    email      = "bq-finops-executor@analytics-production.iam.gserviceaccount.com"
  }
}
```

The identity running Terraform needs permission to update IAM policy on those target service
accounts. If the security team manages that binding separately, leave the map empty.

## Main endpoints

| Endpoint | Purpose |
|---|---|
| `POST /v1/projects` | Add or update a project and trigger initial collection |
| `GET /v1/projects` | List registered projects |
| `POST /v1/projects/{id}/collect` | Trigger one project |
| `POST /v1/collect/all` | Publish collection events for every enabled project |
| `POST /v1/events/collection` | Authenticated Pub/Sub push worker |
| `POST /v1/detect` | Run deterministic detectors |
| `GET /v1/findings` | Retrieve ranked findings |
| `POST /v1/actions` | Create an approval-bound action |
| `POST /v1/delivery/jira` | Draft a complete Jira issue action |
| `POST /v1/delivery/confluence` | Draft a complete Confluence page action |
| `POST /v1/delivery/gitlab-mr` | Draft a GitLab commit + MR action |
| `POST /v1/actions/{id}/decision` | Approve or reject an action |
| `POST /v1/actions/{id}/execute` | Execute an already approved action |
| `/list-apps`, `/run`, `/run_sse` | Standard ADK interactive endpoints |

## Approval example

```bash
curl -X POST "${SERVICE_URL}/v1/actions" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "finding_id": "FINDING_ID",
    "project_id": "analytics-production",
    "action_type": "SET_TABLE_EXPIRATION",
    "payload": {
      "table_ref": "analytics-production.scratch.old_result",
      "expires_at": "2026-10-01T00:00:00Z"
    },
    "requested_by": "engineer@example.com",
    "rollback_plan": "Clear the expiration before the deadline if the owner rejects it."
  }'
```

Approval and execution are intentionally separate requests. A conversation with the LLM cannot
approve or execute an action.

## Jira, Confluence, and GitLab MCP delivery

These connectors are optional. Configure an endpoint and a Secret Manager secret, then use a
delivery endpoint or ask the agent to draft a delivery request. Every generated description
contains the finding evidence, current SQL, proposed SQL, rationale, expected savings,
validation, rollback, acceptance criteria, and required review checks.

The GitLab flow calls `add_commit` and then `save_merge_request`. The request must specify the
GitLab project, source and target branches, and repository-relative file changes. Only `create`
and `update` file actions are accepted; deletion, move, chmod, force-push, merge, and auto-merge
are not available.

Store either a plain bearer token or JSON such as
`{"authorization":"Bearer REDACTED","headers":{"X-Optional-Header":"value"}}` in each
configured secret. Never put the credential in Terraform state or an environment variable.
Atlassian Cloud defaults to `https://mcp.atlassian.com/v1/mcp`; GitLab uses
`https://GITLAB_HOST/api/v4/mcp`.

See [MCP delivery architecture](docs/MCP_DELIVERY.md) for request examples, IAM, connector
permissions, execution sequence, and failure behavior.

## Antigravity

Antigravity adds substantial **development** value:

- it can build and refactor the code while preserving workspace instructions;
- ADK skills give it current framework, deployment, evaluation, and observability guidance;
- it can run the local ADK playground and evaluations;
- it can inspect traces, test failures, and infrastructure changes;
- its agent manager can parallelize independent engineering work.

It is not the production interaction channel for FinOps users. Use the authenticated ADK API,
the development web UI locally, or connect a company UI/ChatOps client to `/run_sse`.

Install the current ADK development skills into the workspace:

```bash
uvx google-agents-cli setup --workspace
```

An optional ADK documentation MCP configuration is in [`docs/ANTIGRAVITY.md`](docs/ANTIGRAVITY.md).

## Operational notes

- `INFORMATION_SCHEMA` is region-scoped. Every registry row must list all locations used by the project.
- Job history is de-duplicated by project, location, and job ID during analysis.
- History is loaded with BigQuery batch load jobs (free) rather than billed streaming inserts;
  `jobs_history` and `native_recommendations` partitions expire automatically after 540 and
  365 days. Findings and the action ledger are retained indefinitely as the audit trail.
- The Pub/Sub push worker verifies the OIDC token minted for the invoker service account
  (`FINOPS_VERIFY_PUBSUB_TOKENS`, on by default). Malformed events and unregistered projects
  are acknowledged with HTTP 200 so they are not redelivered; transient failures return 500
  and Pub/Sub retries with backoff into the dead-letter topic after five attempts.
- Raw SQL can contain sensitive literals. Restrict the control dataset and add redaction if policy requires it.
- Native BigQuery recommendations are pre-GA in some contexts and collection continues when unavailable.
- Savings estimates use configurable rates and assumption ratios (`FINOPS_SAVINGS_RATIO_*`);
  billing export reconciliation should be added to company-specific deployment inputs.
- GitLab commit and MR creation is available only when its MCP connector is configured; it never merges the MR.
