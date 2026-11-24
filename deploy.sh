#!/usr/bin/env bash
# Deploys the GEAP Enterprise Ops Agent:
#   1) Terraform IaC (terraform/main.tf)
#   2) adk deploy agent_engine  (Vertex AI Agent Engine)
# Secrets are never passed here; the runtime reads them from Secret Manager.
#
# Usage: PROJECT_ID=my-proj REGION=us-central1 ./deploy.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID}"
REGION="${REGION:-us-central1}"
ENVIRONMENT="${ENVIRONMENT:-dev}"
DISPLAY_NAME="${DISPLAY_NAME:-geap-enterprise-ops-agent}"
AGENT_ENGINE_ID="${AGENT_ENGINE_ID:-}"

echo ">> Running tests and offline golden evals"
python -m pytest -q
python -m evals.offline_eval > /dev/null

echo ">> Provisioning infrastructure with Terraform"
terraform -chdir=terraform init -input=false
terraform -chdir=terraform validate
terraform -chdir=terraform plan -input=false -out=tfplan \
  -var="project_id=${PROJECT_ID}" -var="region=${REGION}" -var="environment=${ENVIRONMENT}"
terraform -chdir=terraform apply -input=false -auto-approve tfplan

SERVICE_ACCOUNT="$(terraform -chdir=terraform output -raw agent_service_account_email)"

echo ">> Deploying to Vertex AI Agent Engine with the ADK CLI"
EXTRA_ARGS=()
if [[ -n "${AGENT_ENGINE_ID}" ]]; then
  EXTRA_ARGS+=("--agent_engine_id=${AGENT_ENGINE_ID}")
fi
adk deploy agent_engine \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --display_name="${DISPLAY_NAME}" \
  --service_account="${SERVICE_ACCOUNT}" \
  --trace_to_cloud \
  "${EXTRA_ARGS[@]}" \
  agent_app

echo ">> Done. Set AGENT_ENGINE_ID to the printed resource ID to enable"
echo "   VertexAiSessionService + Memory Bank persistence."
