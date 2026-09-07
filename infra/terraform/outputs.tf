output "service_url" {
  value = google_cloud_run_v2_service.agent.uri
}

output "control_plane_service_account" {
  value = google_service_account.app.email
}

output "invoker_service_account" {
  value = google_service_account.invoker.email
}

output "target_impersonation_binding_examples" {
  value = {
    for key, value in var.target_service_accounts : key => {
      target = value.email
      member = google_service_account.app.email
      role   = "roles/iam.serviceAccountTokenCreator"
    }
  }
}

