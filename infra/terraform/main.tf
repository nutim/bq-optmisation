locals {
  required_apis = toset([
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "bigquery.googleapis.com",
    "cloudbuild.googleapis.com",
    "iamcredentials.googleapis.com",
    "pubsub.googleapis.com",
    "run.googleapis.com",
    "cloudscheduler.googleapis.com",
    "secretmanager.googleapis.com",
  ])
  mcp_secrets = {
    for key, secret_id in {
      atlassian = var.atlassian_mcp_secret_id
      gitlab    = var.gitlab_mcp_secret_id
    } : key => secret_id if secret_id != null
  }
}

data "google_project" "control" {
  project_id = var.control_project_id
}

resource "google_project_service" "required" {
  for_each           = local.required_apis
  project            = var.control_project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_service_account" "app" {
  project      = var.control_project_id
  account_id   = "bq-finops-agent"
  display_name = "BigQuery FinOps agent control plane"
}

resource "google_service_account" "invoker" {
  project      = var.control_project_id
  account_id   = "bq-finops-invoker"
  display_name = "BigQuery FinOps scheduler and push invoker"
}

resource "google_artifact_registry_repository" "agent" {
  project       = var.control_project_id
  location      = var.region
  repository_id = "finops"
  format        = "DOCKER"
  description   = "BigQuery FinOps agent images"
  depends_on    = [google_project_service.required]
}

resource "google_bigquery_dataset" "control" {
  project                     = var.control_project_id
  dataset_id                  = "finops_control"
  location                    = var.bigquery_location
  delete_contents_on_destroy  = false
  default_table_expiration_ms = null
  labels = {
    application = "bigquery-finops-agent"
  }
  depends_on = [google_project_service.required]
}

resource "google_bigquery_dataset_iam_member" "app_editor" {
  project    = var.control_project_id
  dataset_id = google_bigquery_dataset.control.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.app.email}"
}

resource "google_project_iam_member" "app_job_user" {
  project = var.control_project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.app.email}"
}

resource "google_project_iam_member" "app_vertex_user" {
  project = var.control_project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.app.email}"
}

resource "google_project_iam_member" "app_log_writer" {
  project = var.control_project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.app.email}"
}

resource "google_secret_manager_secret_iam_member" "mcp_credentials" {
  for_each = local.mcp_secrets

  project   = var.control_project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.app.email}"
}

resource "google_pubsub_topic" "collection" {
  project    = var.control_project_id
  name       = "bigquery-finops-collection"
  depends_on = [google_project_service.required]
}

resource "google_pubsub_topic_iam_member" "app_publisher" {
  project = var.control_project_id
  topic   = google_pubsub_topic.collection.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.app.email}"
}

resource "google_service_account_iam_member" "target_impersonation" {
  for_each = var.target_service_accounts

  service_account_id = "projects/${each.value.project_id}/serviceAccounts/${each.value.email}"
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.app.email}"
}

resource "google_service_account_iam_member" "pubsub_can_mint_invoker_token" {
  service_account_id = google_service_account.invoker.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.control.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_cloud_run_v2_service" "agent" {
  project  = var.control_project_id
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account                  = google_service_account.app.email
    timeout                          = "900s"
    max_instance_request_concurrency = 20
    scaling {
      min_instance_count = 0
      max_instance_count = 10
    }
    containers {
      image = var.container_image
      resources {
        limits = {
          cpu    = "2"
          memory = "2Gi"
        }
      }
      env {
        name  = "FINOPS_CONTROL_PROJECT_ID"
        value = var.control_project_id
      }
      env {
        name  = "FINOPS_CONTROL_DATASET"
        value = google_bigquery_dataset.control.dataset_id
      }
      env {
        name  = "FINOPS_CONTROL_LOCATION"
        value = var.bigquery_location
      }
      env {
        name  = "FINOPS_COLLECTION_TOPIC"
        value = google_pubsub_topic.collection.name
      }
      env {
        name  = "FINOPS_MODEL"
        value = var.model
      }
      env {
        name  = "FINOPS_TRACE_TO_CLOUD"
        value = "true"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.control_project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.vertex_location
      }
      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "TRUE"
      }
      dynamic "env" {
        for_each = var.atlassian_mcp_secret_id == null ? [] : [var.atlassian_mcp_secret_id]
        content {
          name  = "FINOPS_ATLASSIAN_MCP_AUTH_SECRET"
          value = "projects/${var.control_project_id}/secrets/${env.value}/versions/latest"
        }
      }
      dynamic "env" {
        for_each = var.gitlab_mcp_secret_id == null ? [] : [var.gitlab_mcp_secret_id]
        content {
          name  = "FINOPS_GITLAB_MCP_AUTH_SECRET"
          value = "projects/${var.control_project_id}/secrets/${env.value}/versions/latest"
        }
      }
      dynamic "env" {
        for_each = var.gitlab_mcp_url == null ? [] : [var.gitlab_mcp_url]
        content {
          name  = "FINOPS_GITLAB_MCP_URL"
          value = env.value
        }
      }
      env {
        name  = "FINOPS_ATLASSIAN_MCP_URL"
        value = var.atlassian_mcp_url
      }
      env {
        name  = "FINOPS_VERIFY_PUBSUB_TOKENS"
        value = "true"
      }
      env {
        name  = "FINOPS_PUBSUB_INVOKER_SERVICE_ACCOUNT"
        value = google_service_account.invoker.email
      }
      dynamic "env" {
        for_each = var.service_url == null ? [] : [var.service_url]
        content {
          name  = "FINOPS_SERVICE_URL"
          value = env.value
        }
      }
    }
  }

  depends_on = [
    google_project_service.required,
    google_bigquery_dataset_iam_member.app_editor,
    google_project_iam_member.app_job_user,
    google_secret_manager_secret_iam_member.mcp_credentials,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "invoker" {
  project  = var.control_project_id
  location = var.region
  name     = google_cloud_run_v2_service.agent.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.invoker.email}"
}

resource "google_pubsub_subscription" "collection_push" {
  project = var.control_project_id
  name    = "bigquery-finops-collection-push"
  topic   = google_pubsub_topic.collection.id

  ack_deadline_seconds       = 600
  message_retention_duration = "86400s"
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }
  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = 5
  }
  push_config {
    push_endpoint = "${google_cloud_run_v2_service.agent.uri}/v1/events/collection"
    oidc_token {
      service_account_email = google_service_account.invoker.email
      audience              = google_cloud_run_v2_service.agent.uri
    }
  }
}

resource "google_pubsub_topic" "dead_letter" {
  project = var.control_project_id
  name    = "bigquery-finops-dead-letter"
}

resource "google_pubsub_topic_iam_member" "pubsub_dead_letter_publisher" {
  project = var.control_project_id
  topic   = google_pubsub_topic.dead_letter.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:service-${data.google_project.control.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_project_iam_member" "pubsub_subscription_reader" {
  project = var.control_project_id
  role    = "roles/pubsub.subscriber"
  member  = "serviceAccount:service-${data.google_project.control.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_cloud_scheduler_job" "daily" {
  project          = var.control_project_id
  region           = var.region
  name             = "bigquery-finops-daily-collection"
  schedule         = var.scheduler_cron
  time_zone        = var.scheduler_time_zone
  attempt_deadline = "180s"

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.agent.uri}/v1/collect/all"
    oidc_token {
      service_account_email = google_service_account.invoker.email
      audience              = google_cloud_run_v2_service.agent.uri
    }
  }
}
