output "agent_service_account_email" {
  description = "Runtime service account for adk deploy agent_engine --service_account."
  value       = google_service_account.agent_runtime.email
}

output "staging_bucket" {
  description = "GCS bucket for Agent Engine staging artifacts."
  value       = google_storage_bucket.staging.url
}

output "secret_ids" {
  description = "Secret Manager secrets provisioned for the agent."
  value       = [for s in google_secret_manager_secret.agent_secrets : s.secret_id]
}
