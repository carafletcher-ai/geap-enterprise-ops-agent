variable "project_id" {
  description = "Google Cloud project ID to deploy into."
  type        = string
}

variable "region" {
  description = "Region for Vertex AI Agent Engine and the staging bucket."
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "Deployment environment label (dev, staging, prod)."
  type        = string
  default     = "dev"
}

variable "service_account_id" {
  description = "Account ID of the agent runtime service account."
  type        = string
  default     = "geap-ops-agent"
}

variable "secret_ids" {
  description = "Secret Manager secret IDs the agent reads at runtime."
  type        = list(string)
  default     = ["ITSM_API_TOKEN", "PAGER_WEBHOOK_URL"]
}
