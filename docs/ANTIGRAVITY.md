# Antigravity setup

Antigravity is useful as the engineering and evaluation surface for this project. It does not
replace the deployed ADK runtime or the authenticated internal user interface.

## Install the Agent Platform skills

From the repository root:

```bash
uvx google-agents-cli setup --workspace
```

This installs workspace guidance for ADK code, evaluation, observability, scaffolding, and GCP
deployment. Keep those generated skills updated with `agents-cli update`.

## Add the live ADK documentation MCP server

In Antigravity, open the MCP store, select **Manage MCP Servers**, then **View raw config** and add:

```json
{
  "mcpServers": {
    "adk-docs-mcp": {
      "command": "uvx",
      "args": [
        "--from",
        "mcpdoc",
        "mcpdoc",
        "--urls",
        "AgentDevelopmentKit:https://adk.dev/llms.txt",
        "--transport",
        "stdio"
      ]
    }
  }
}
```

## Interactive development

Use either:

```bash
agents-cli playground
```

or run the combined service with the development UI enabled:

```bash
FINOPS_SERVE_ADK_WEB_UI=true uv run uvicorn app.api:api --reload --port 8080
```

Recommended Antigravity tasks:

- “Run the unit tests and fix only failures related to this change.”
- “Add an evaluation case for a prompt-injection attempt in a BigQuery table description.”
- “Review this Terraform plan for permissions granted directly to the control-plane identity.”
- “Compare the agent's proposed SQL tool calls against the golden evaluation dataset.”

Do not give Antigravity user credentials or static service-account keys. Use Cloud OAuth and
Application Default Credentials, and keep production execution behind the same approval API.

