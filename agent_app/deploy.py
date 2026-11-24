"""Deployment entry point (Python): Terraform IaC + ``adk deploy agent_engine``.

This module mirrors ``deploy.sh`` in Python so the whole infrastructure and
deployment story is visible from code:

  1. **Infrastructure as Code** - provisions ``terraform/main.tf``
     (APIs, runtime service account + IAM, Secret Manager secrets, staging
     bucket, log-based metrics) via ``terraform init/plan/apply``.
  2. **Agent CLI deploy** - ships the ADK app to Vertex AI Agent Engine with::

         adk deploy agent_engine --project=$PROJECT --region=$REGION \\
             --display_name=geap-enterprise-ops-agent --trace_to_cloud \\
             --service_account=$SA agent_app

  3. **Secrets** - never passed on the command line or baked into images;
     the runtime reads them from Secret Manager (``config.get_secret``).

Usage::

    python -m agent_app.deploy --project my-proj                # dry run
    python -m agent_app.deploy --project my-proj --execute      # real deploy
    python -m agent_app.deploy --project my-proj --execute --skip-terraform
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TERRAFORM_DIR = ROOT / "terraform"
AGENT_DIR_NAME = "agent_app"
DISPLAY_NAME = "geap-enterprise-ops-agent"

logger = logging.getLogger("geap.deploy")

# Resources declared in terraform/main.tf (kept in sync by tests).
TERRAFORM_IAC_RESOURCES: tuple[str, ...] = (
    "google_project_service.services",
    "google_service_account.agent_runtime",
    "google_project_iam_member.agent_runtime_roles",
    "google_secret_manager_secret.agent_secrets",
    "google_secret_manager_secret_iam_member.agent_secret_access",
    "google_storage_bucket.staging",
    "google_logging_metric.guardrail_blocks",
    "google_logging_metric.hitl_decisions",
)

# Equivalent HCL of the core resources, exposed for tooling / reviewers.
TERRAFORM_IAC_SNIPPET = """\
resource "google_service_account" "agent_runtime" {
  account_id   = var.service_account_id
  display_name = "GEAP Enterprise Ops Agent runtime"
}

resource "google_secret_manager_secret" "agent_secrets" {
  for_each  = toset(var.secret_ids)   # ITSM_API_TOKEN, PAGER_WEBHOOK_URL
  secret_id = each.value
  replication { auto {} }
}

resource "google_storage_bucket" "staging" {
  name                        = "${var.project_id}-geap-ops-agent-staging"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
}
"""


@dataclass(frozen=True)
class DeployConfig:
  project_id: str
  region: str = "us-central1"
  environment: str = "dev"
  service_account: str | None = None
  agent_engine_id: str | None = None  # Set to update an existing engine.


def terraform_resources_in_main_tf(path: Path = TERRAFORM_DIR / "main.tf") -> list[str]:
  """Parses ``resource "type" "name"`` blocks from main.tf."""
  text = path.read_text()
  return [f"{t}.{n}" for t, n in re.findall(r'^resource\s+"([^"]+)"\s+"([^"]+)"', text, re.M)]


def terraform_commands(cfg: DeployConfig) -> list[list[str]]:
  """The Terraform IaC commands to provision infrastructure."""
  tf_vars = [
      f"-var=project_id={cfg.project_id}",
      f"-var=region={cfg.region}",
      f"-var=environment={cfg.environment}",
  ]
  return [
      ["terraform", f"-chdir={TERRAFORM_DIR}", "init", "-input=false"],
      ["terraform", f"-chdir={TERRAFORM_DIR}", "validate"],
      ["terraform", f"-chdir={TERRAFORM_DIR}", "plan", "-input=false", "-out=tfplan", *tf_vars],
      ["terraform", f"-chdir={TERRAFORM_DIR}", "apply", "-input=false", "-auto-approve", "tfplan"],
  ]


def adk_deploy_agent_engine_command(cfg: DeployConfig) -> list[str]:
  """Builds the ``adk deploy agent_engine`` CLI invocation."""
  cmd = [
      "adk", "deploy", "agent_engine",
      f"--project={cfg.project_id}",
      f"--region={cfg.region}",
      f"--display_name={DISPLAY_NAME}",
      "--description=Enterprise IT ops triage/remediation multi-agent (ADK)",
      "--trace_to_cloud",
  ]
  if cfg.service_account:
    cmd.append(f"--service_account={cfg.service_account}")
  if cfg.agent_engine_id:
    cmd.append(f"--agent_engine_id={cfg.agent_engine_id}")
  cmd.append(AGENT_DIR_NAME)
  return cmd


def _run(cmd: list[str], execute: bool, cwd: Path = ROOT) -> str:
  printable = " ".join(shlex.quote(c) for c in cmd)
  logger.info("deploy.intent", extra={"command": printable, "execute": execute})
  print(("$ " if execute else "[dry-run] $ ") + printable)
  if not execute:
    return ""
  result = subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)
  logger.info("deploy.outcome", extra={"command": cmd[:3], "returncode": result.returncode})
  return result.stdout


def terraform_output_service_account(execute: bool) -> str | None:
  if not execute:
    return None
  out = _run(["terraform", f"-chdir={TERRAFORM_DIR}", "output", "-json"], execute=True)
  return json.loads(out).get("agent_service_account_email", {}).get("value")


def deploy(cfg: DeployConfig, execute: bool = False, skip_terraform: bool = False) -> list[list[str]]:
  """Runs IaC then the Agent Engine deploy. Returns the commands issued."""
  issued: list[list[str]] = []
  if not skip_terraform:
    for cmd in terraform_commands(cfg):
      _run(cmd, execute)
      issued.append(cmd)
    sa = terraform_output_service_account(execute)
    if sa and not cfg.service_account:
      cfg = DeployConfig(**{**cfg.__dict__, "service_account": sa})
  cmd = adk_deploy_agent_engine_command(cfg)
  _run(cmd, execute)
  issued.append(cmd)
  return issued


def main(argv: list[str] | None = None) -> None:
  parser = argparse.ArgumentParser(description="Deploy GEAP Enterprise Ops Agent")
  parser.add_argument("--project", required=True)
  parser.add_argument("--region", default="us-central1")
  parser.add_argument("--environment", default="dev")
  parser.add_argument("--service-account")
  parser.add_argument("--agent-engine-id")
  parser.add_argument("--execute", action="store_true", help="Actually run (default: dry run)")
  parser.add_argument("--skip-terraform", action="store_true")
  args = parser.parse_args(argv)
  cfg = DeployConfig(
      project_id=args.project,
      region=args.region,
      environment=args.environment,
      service_account=args.service_account,
      agent_engine_id=args.agent_engine_id,
  )
  deploy(cfg, execute=args.execute, skip_terraform=args.skip_terraform)


if __name__ == "__main__":
  main()
