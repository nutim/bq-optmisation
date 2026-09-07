variable "control_project_id" {
  description = "Project that hosts the FinOps control plane."
  type        = string
}

variable "region" {
  description = "Cloud Run and Pub/Sub region."
  type        = string
  default     = "europe-west1"
}

variable "bigquery_location" {
  description = "Location of the central FinOps dataset."
  type        = string
  default     = "EU"
}

variable "service_name" {
  type    = string
  default = "bigquery-finops-agent"
}

variable "container_image" {
  description = "Immutable Artifact Registry image digest to deploy."
  type        = string
}

variable "model" {
  description = "Evaluated production model ID."
  type        = string
  default     = "gemini-3.7-flash"
}

variable "vertex_location" {
  description = "Vertex AI model endpoint location."
  type        = string
  default     = "global"
}

variable "target_service_accounts" {
  description = "Existing target SAs the control-plane SA may impersonate. Security owns their BigQuery roles."
  type = map(object({
    project_id = string
    email      = string
  }))
  default = {}
}

variable "scheduler_cron" {
  type    = string
  default = "0 3 * * *"
}

variable "scheduler_time_zone" {
  type    = string
  default = "Europe/Berlin"
}

variable "atlassian_mcp_url" {
  description = "Atlassian Rovo MCP Streamable HTTP endpoint."
  type        = string
  default     = "https://mcp.atlassian.com/v1/mcp"
}

variable "atlassian_mcp_secret_id" {
  description = "Optional existing Secret Manager secret ID containing the Atlassian bearer credential."
  type        = string
  default     = null
  nullable    = true
}

variable "gitlab_mcp_url" {
  description = "Optional GitLab MCP endpoint, for example https://gitlab.example/api/v4/mcp."
  type        = string
  default     = null
  nullable    = true
}

variable "gitlab_mcp_secret_id" {
  description = "Optional existing Secret Manager secret ID containing the GitLab bearer credential."
  type        = string
  default     = null
  nullable    = true
}

variable "service_url" {
  description = "Public Cloud Run URL used as the Pub/Sub OIDC audience. Set after the first apply exposes the URL; empty disables audience pinning."
  type        = string
  default     = null
  nullable    = true
}
