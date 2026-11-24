# GEAP Enterprise Ops Agent - Infrastructure as Code
#
# Provisions everything the agent needs on Google Cloud:
#   * required APIs (Vertex AI / Agent Engine, Secret Manager, DLP, Trace, ...)
#   * a least-privilege runtime service account + IAM
#   * Secret Manager secrets (values are added out-of-band, never in code/state)
#   * a versioned GCS staging bucket for Agent Engine artifacts
#   * log-based metrics for guardrail blocks and HITL decisions
#
# The Agent Engine (Vertex AI Reasoning Engine) itself is created by
# `adk deploy agent_engine` (see deploy.sh / agent_app/deploy.py), which
# runs as the service account created here.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.30.0"
    }
  }
  # Remote state (recommended): uncomment and set your bucket.
  # backend "gcs" {
  #   bucket = "YOUR_TF_STATE_BUCKET"
  #   prefix = "geap-enterprise-ops-agent"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  services = [
    "aiplatform.googleapis.com",
    "secretmanager.googleapis.com",
    "dlp.googleapis.com",
    "cloudtrace.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "cloudbuild.googleapis.com",
    "storage.googleapis.com",
    "iam.googleapis.com",
    "cloudresourcemanager.googleapis.com",
  ]
  agent_roles = [
    "roles/aiplatform.user",
    "roles/secretmanager.secretAccessor",
    "roles/dlp.user",
    "roles/cloudtrace.agent",
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/storage.objectAdmin",
  ]
  labels = {
    app         = "geap-enterprise-ops-agent"
    environment = var.environment
    managed_by  = "terraform"
  }
}

# ---------------------------------------------------------------- APIs ----
resource "google_project_service" "services" {
  for_each           = toset(local.services)
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# ------------------------------------------------- Runtime identity ------
resource "google_service_account" "agent_runtime" {
  project      = var.project_id
  account_id   = var.service_account_id
  display_name = "GEAP Enterprise Ops Agent runtime"
  depends_on   = [google_project_service.services]
}

resource "google_project_iam_member" "agent_runtime_roles" {
  for_each = toset(local.agent_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.agent_runtime.email}"
}

# ------------------------------------------------- Secret Manager --------
# Secret *containers* only. Add values with:
#   echo -n "$TOKEN" | gcloud secrets versions add ITSM_API_TOKEN --data-file=-
resource "google_secret_manager_secret" "agent_secrets" {
  for_each  = toset(var.secret_ids)
  project   = var.project_id
  secret_id = each.value
  labels    = local.labels

  replication {
    auto {}
  }
  depends_on = [google_project_service.services]
}

resource "google_secret_manager_secret_iam_member" "agent_secret_access" {
  for_each  = google_secret_manager_secret.agent_secrets
  project   = var.project_id
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent_runtime.email}"
}

# ------------------------------------------------- Staging bucket --------
resource "google_storage_bucket" "staging" {
  project                     = var.project_id
  name                        = "${var.project_id}-geap-ops-agent-staging"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  labels                      = local.labels

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type = "Delete"
    }
  }
  depends_on = [google_project_service.services]
}

# ------------------------------------------------- Observability ---------
resource "google_logging_metric" "guardrail_blocks" {
  project = var.project_id
  name    = "geap_ops_agent/guardrail_blocks"
  filter  = "jsonPayload.message=(\"guardrail.input_blocked\" OR \"guardrail.pipeline_blocked\" OR \"guardrail.tool_blocked\")"

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
  depends_on = [google_project_service.services]
}

resource "google_logging_metric" "hitl_decisions" {
  project = var.project_id
  name    = "geap_ops_agent/hitl_decisions"
  filter  = "jsonPayload.message=\"hitl.decision\""

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    labels {
      key         = "approved"
      value_type  = "STRING"
      description = "Whether the human approved the action"
    }
  }
  label_extractors = {
    "approved" = "EXTRACT(jsonPayload.approved)"
  }
  depends_on = [google_project_service.services]
}
