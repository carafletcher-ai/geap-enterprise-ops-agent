"""Central configuration and secure secret access.

No secrets are hardcoded anywhere in this repository. Secrets are resolved at
runtime from Google Cloud Secret Manager. For local development only, an
environment variable of the same name can be used as a fallback (populated from
an untracked ``.env`` file, never committed).
"""

from __future__ import annotations

import functools
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
  return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class Settings:
  """Runtime settings, all sourced from environment variables."""

  app_name: str = os.environ.get("APP_NAME", "geap_enterprise_ops_agent")
  project_id: str | None = os.environ.get("GOOGLE_CLOUD_PROJECT")
  location: str = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")

  # Strategic model routing: Flash for fast/cheap steps, Pro for planning.
  flash_model: str = os.environ.get("FLASH_MODEL", "gemini-2.5-flash")
  pro_model: str = os.environ.get("PRO_MODEL", "gemini-2.5-pro")

  # Persistence. Agent Engine (Vertex AI) when deployed, SQL DB locally.
  agent_engine_id: str | None = os.environ.get("AGENT_ENGINE_ID")
  session_db_url: str = os.environ.get(
      "SESSION_DB_URL", "sqlite+aiosqlite:///./.adk/sessions.db"
  )

  # Context management.
  compaction_interval: int = int(os.environ.get("COMPACTION_INTERVAL", "4"))
  compaction_overlap: int = int(os.environ.get("COMPACTION_OVERLAP", "1"))
  max_context_tokens: int = int(os.environ.get("MAX_CONTEXT_TOKENS", "24000"))
  max_history_contents: int = int(os.environ.get("MAX_HISTORY_CONTENTS", "30"))

  # Observability & privacy.
  enable_cloud_dlp: bool = _env_bool("ENABLE_CLOUD_DLP", False)
  enable_cloud_trace: bool = _env_bool("ENABLE_CLOUD_TRACE", False)
  log_level: str = os.environ.get("LOG_LEVEL", "INFO")

  # Guardrail policy.
  max_high_stakes_actions_per_session: int = int(
      os.environ.get("MAX_HIGH_STAKES_ACTIONS", "3")
  )
  max_access_hours: int = int(os.environ.get("MAX_ACCESS_HOURS", "8"))
  corporate_email_domain: str = os.environ.get("CORP_EMAIL_DOMAIN", "example.com")

  secret_names: tuple[str, ...] = field(
      default=("ITSM_API_TOKEN", "PAGER_WEBHOOK_URL")
  )


SETTINGS = Settings()


class SecretNotFoundError(RuntimeError):
  """Raised when a secret cannot be resolved from any secure source."""


@functools.lru_cache(maxsize=32)
def get_secret(secret_id: str, version: str = "latest") -> str:
  """Fetches a secret from Google Cloud Secret Manager.

  Resolution order:
    1. Secret Manager: ``projects/{project}/secrets/{secret_id}/versions/{version}``.
    2. Local-dev fallback: an environment variable named ``secret_id``.

  Args:
    secret_id: The Secret Manager secret ID (e.g. ``ITSM_API_TOKEN``).
    version: The secret version, defaults to ``latest``.

  Returns:
    The secret payload as a UTF-8 string.

  Raises:
    SecretNotFoundError: If the secret is unavailable from every source.
  """
  if SETTINGS.project_id:
    try:
      from google.cloud import secretmanager  # pylint: disable=import-outside-toplevel

      client = secretmanager.SecretManagerServiceClient()
      name = (
          f"projects/{SETTINGS.project_id}/secrets/{secret_id}/versions/{version}"
      )
      response = client.access_secret_version(request={"name": name})
      logger.info(
          "secret.resolved", extra={"secret_id": secret_id, "source": "secret_manager"}
      )
      return response.payload.data.decode("utf-8")
    except Exception as exc:  # pylint: disable=broad-except
      logger.warning(
          "secret.secret_manager_unavailable",
          extra={"secret_id": secret_id, "error": type(exc).__name__},
      )

  value = os.environ.get(secret_id)
  if value:
    logger.info("secret.resolved", extra={"secret_id": secret_id, "source": "env"})
    return value
  raise SecretNotFoundError(
      f"Secret '{secret_id}' not found in Secret Manager or environment."
  )


def has_secret(secret_id: str) -> bool:
  """Returns True if the secret can be resolved, without raising."""
  try:
    get_secret(secret_id)
    return True
  except SecretNotFoundError:
    return False
