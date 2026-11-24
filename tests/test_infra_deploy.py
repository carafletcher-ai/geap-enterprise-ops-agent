"""Infrastructure & CI/CD: IaC, deploy tooling, CI configs, secret hygiene."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_app import config, deploy

ROOT = Path(__file__).resolve().parents[1]

SECRET_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z\-_]{35}"),
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"sk-[A-Za-z0-9]{32,}"),
    re.compile(r"-----BEGIN (RSA )?PRIVATE KEY-----"),
    re.compile(r"(?i)(api_key|password|token)\s*=\s*['\"][A-Za-z0-9_\-]{12,}['\"]"),
]


@pytest.mark.parametrize("rel", [
    "terraform/main.tf", "terraform/variables.tf", "terraform/outputs.tf",
    "deploy.sh", "cloudbuild.yaml", ".github/workflows/ci.yml",
    "agent_app/deploy.py", "evals/golden_evalset.json", "README.md",
    "requirements.txt", ".env.example",
])
def test_required_infra_files_exist(rel):
  assert (ROOT / rel).is_file(), rel


def test_terraform_resources_match_deploy_module():
  assert set(deploy.terraform_resources_in_main_tf()) == set(deploy.TERRAFORM_IAC_RESOURCES)


def test_terraform_provisions_secret_manager_and_least_privilege_sa():
  tf = (ROOT / "terraform/main.tf").read_text()
  assert "google_secret_manager_secret" in tf
  assert "roles/secretmanager.secretAccessor" in tf
  assert "roles/owner" not in tf and "roles/editor" not in tf
  assert "secret_data" not in tf  # secret values never in IaC/state


def test_adk_deploy_agent_engine_command():
  cfg = deploy.DeployConfig(project_id="p", region="us-central1", service_account="sa@p.iam")
  cmd = deploy.adk_deploy_agent_engine_command(cfg)
  assert cmd[:3] == ["adk", "deploy", "agent_engine"]
  assert "--project=p" in cmd and "--trace_to_cloud" in cmd and cmd[-1] == "agent_app"
  assert "--service_account=sa@p.iam" in cmd


def test_deploy_dry_run_issues_terraform_then_adk(capsys):
  issued = deploy.deploy(deploy.DeployConfig(project_id="p"), execute=False)
  assert [c[2] if c[0] == "terraform" else c[1] for c in issued] == [
      "init", "validate", "plan", "apply", "deploy"]
  assert "[dry-run]" in capsys.readouterr().out


def test_shell_and_ci_configs_use_adk_cli_and_terraform():
  for rel in ("deploy.sh", "cloudbuild.yaml"):
    text = (ROOT / rel).read_text()
    assert "adk deploy agent_engine" in text and "terraform" in text
  ci = (ROOT / ".github/workflows/ci.yml").read_text()
  assert "pytest" in ci and "terraform" in ci and "offline_eval" in ci
  assert "secretManager" in (ROOT / "cloudbuild.yaml").read_text()


def test_no_hardcoded_secrets_in_repo():
  offenders = []
  for path in ROOT.rglob("*"):
    if not path.is_file() or any(p in path.parts for p in (".git", ".venv", "__pycache__", ".terraform", "tests")):
      continue
    if path.suffix not in {".py", ".tf", ".yaml", ".yml", ".sh", ".json", ".md", ".txt", ".example", ""}:
      continue
    text = path.read_text(errors="ignore")
    offenders += [f"{path}: {p.pattern}" for p in SECRET_PATTERNS if p.search(text)]
  assert not offenders, offenders


def test_get_secret_uses_secret_manager_then_env(monkeypatch):
  config.get_secret.cache_clear()
  monkeypatch.setenv("UNIT_TEST_SECRET", "from-env")
  assert config.get_secret("UNIT_TEST_SECRET") == "from-env"
  config.get_secret.cache_clear()
  monkeypatch.delenv("UNIT_TEST_SECRET")
  with pytest.raises(config.SecretNotFoundError):
    config.get_secret("UNIT_TEST_SECRET")
  assert config.has_secret("UNIT_TEST_SECRET") is False
  config.get_secret.cache_clear()


def test_get_secret_reads_secret_manager_when_project_set(monkeypatch):
  import sys
  import types as pytypes

  calls = {}

  class FakeClient:
    def access_secret_version(self, request):
      calls["name"] = request["name"]
      return pytypes.SimpleNamespace(payload=pytypes.SimpleNamespace(data=b"sm-value"))

  fake_mod = pytypes.SimpleNamespace(SecretManagerServiceClient=FakeClient)
  monkeypatch.setitem(sys.modules, "google.cloud.secretmanager", fake_mod)
  import google.cloud
  monkeypatch.setattr(google.cloud, "secretmanager", fake_mod, raising=False)
  monkeypatch.setattr(config, "SETTINGS", config.Settings(project_id="proj-x"))
  config.get_secret.cache_clear()
  assert config.get_secret("ITSM_API_TOKEN") == "sm-value"
  assert calls["name"] == "projects/proj-x/secrets/ITSM_API_TOKEN/versions/latest"
  config.get_secret.cache_clear()
