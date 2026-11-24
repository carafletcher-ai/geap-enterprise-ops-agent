"""Strategic model routing: Flash for fast/cheap work, Pro for deep reasoning.

Static routing: each sub-agent is pinned to the cheapest model that can do its
job (intake/executor/reviewer -> Flash, planner -> Pro).

Dynamic routing: ``dynamic_model_router`` is a before_model callback that
re-routes each LLM call based on the triage result stored in session state,
e.g. SEV1 security incidents escalate the executor to Pro, while SEV4
questions downgrade the planner to Flash to save latency and cost.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from google.adk.models import LlmRequest

from .config import SETTINGS

logger = logging.getLogger("geap.routing")

STATIC_ROUTES: dict[str, str] = {
    "intake_agent": SETTINGS.flash_model,
    "planner_agent": SETTINGS.pro_model,
    "executor_agent": SETTINGS.flash_model,
    "reviewer_agent": SETTINGS.flash_model,
}


def _parse_intake(raw: Any) -> dict[str, Any]:
  if isinstance(raw, dict):
    return raw
  if isinstance(raw, str):
    text = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    try:
      return json.loads(text)
    except (ValueError, TypeError):
      sev = next((s for s in ("SEV1", "SEV2", "SEV3", "SEV4") if s in raw), None)
      return {"severity": sev} if sev else {}
  return {}


def select_model(agent_name: str, intake: Any) -> tuple[str, str]:
  """Chooses a model for ``agent_name`` given the triage result.

  Returns:
    (model_name, reason)
  """
  triage = _parse_intake(intake)
  severity = str(triage.get("severity", "")).upper()
  category = str(triage.get("category", "")).lower()
  default = STATIC_ROUTES.get(agent_name, SETTINGS.flash_model)

  if agent_name == "planner_agent" and severity == "SEV4" and category == "question":
    return SETTINGS.flash_model, "simple question: planner downgraded to Flash"
  if agent_name in ("executor_agent", "reviewer_agent") and (
      severity == "SEV1" or category == "security"
  ):
    return SETTINGS.pro_model, "SEV1/security: escalated to Pro for careful reasoning"
  return default, "static route"


def dynamic_model_router(callback_context: Any, llm_request: LlmRequest) -> None:
  """before_model_callback: rewrites ``llm_request.model`` per routing policy."""
  agent_name = getattr(callback_context, "agent_name", "")
  model, reason = select_model(agent_name, callback_context.state.get("intake"))
  if llm_request.model != model:
    logger.info(
        "routing.model_selected",
        extra={"agent_name": agent_name, "from_model": llm_request.model,
               "to_model": model, "reason": reason},
    )
    llm_request.model = model
  callback_context.state[f"routing:{agent_name}"] = model
  return None
