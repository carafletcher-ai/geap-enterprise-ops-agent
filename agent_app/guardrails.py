"""Guardrails & policy enforcement (ADK callbacks + an ADK Plugin).

Layers:
  0. ``pipeline_input_gate`` (root before_agent): halts the entire
     SequentialAgent on prompt injection / destructive requests.
  1. ``input_safety_guardrail`` (before_model): blocks prompt injection and
     destructive requests before they reach Gemini, and redacts PII from the
     outgoing request (data minimization).
  2. ``tool_policy_guardrail`` (before_tool): per-agent tool allowlist,
     severity-based policy for high-stakes tools, and a per-session rate limit.
  3. ``output_pii_guardrail`` (after_model): scrubs PII from model output.
  4. ``handle_tool_error`` (on_tool_error): converts raised exceptions (e.g.
     schema validation inside ADK) into guided ``ToolError`` responses.
  5. ``GovernancePlugin``: app-wide ADK plugin applying audit + policy to every
     agent, including future ones.
  6. ``reviewer_agent`` (see agent.py) performs LLM self-evaluation.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from google.adk.models import LlmRequest, LlmResponse
from google.adk.plugins import BasePlugin
from google.genai import types
from pydantic import ValidationError

from .config import SETTINGS
from .pii import contains_pii, redact_text
from .tools import HIGH_STAKES_TOOLS, tool_error, validation_error_to_tool_error

logger = logging.getLogger("geap.guardrails")

INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ignore (all |any )?(previous|prior|above) instructions",
        r"disregard (the |your )?(system|previous) (prompt|instructions)",
        r"reveal (your|the) (system )?prompt",
        r"you are now (in )?(dan|developer mode)",
        r"bypass (the )?(approval|guardrail|policy)",
        r"without (human )?approval",
    )
]
DESTRUCTIVE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bdrop\s+(database|table)\b",
        r"\brm\s+-rf\s+/",
        r"\bdelete\s+all\s+(data|backups|users)\b",
        r"\bdisable\s+(audit|logging|mfa)\b",
    )
]

# Which tools each sub-agent may call (least privilege).
AGENT_TOOL_ALLOWLIST: dict[str, frozenset[str]] = {
    "intake_agent": frozenset({"classify_ops_request"}),
    "planner_agent": frozenset(
        {"get_service_health_status", "search_runbook_knowledge_base", "load_memory"}
    ),
    "executor_agent": frozenset(
        {"create_incident_ticket", "restart_production_service", "grant_temporary_access",
         "get_service_health_status"}
    ),
    "reviewer_agent": frozenset(),
}

HIGH_STAKES_COUNTER_KEY = "guardrail:high_stakes_actions"


def _blocked_response(reason: str) -> LlmResponse:
  return LlmResponse(
      content=types.Content(
          role="model",
          parts=[types.Part(text=(
              "I can't help with that request: " + reason + " If this is a "
              "legitimate operational need, please open a change request so a "
              "human approver can review it."
          ))],
      )
  )


def check_user_text(text: str) -> str | None:
  """Returns a block reason if the text violates input policy, else None."""
  for pattern in INJECTION_PATTERNS:
    if pattern.search(text):
      return "it attempts to override safety instructions (prompt injection)."
  for pattern in DESTRUCTIVE_PATTERNS:
    if pattern.search(text):
      return "it requests a destructive, irreversible operation."
  return None


def _last_user_text(llm_request: LlmRequest) -> str:
  for content in reversed(llm_request.contents or []):
    if content.role == "user":
      texts = [p.text for p in (content.parts or []) if getattr(p, "text", None)]
      if texts:
        return "\n".join(texts)
  return ""


def _content_text(content: Any) -> str:
  if not content or not getattr(content, "parts", None):
    return ""
  return "\n".join(p.text for p in content.parts if getattr(p, "text", None))


def pipeline_input_gate(callback_context: Any) -> types.Content | None:
  """before_agent_callback (root): stop the WHOLE pipeline on unsafe input.

  Returning Content from a before_agent_callback makes ADK skip the agent
  (and all its sub-agents), so no model or tool ever sees the request.
  """
  text = _content_text(getattr(callback_context, "user_content", None))
  reason = check_user_text(text) if text else None
  if not reason:
    return None
  logger.warning("guardrail.pipeline_blocked", extra={"reason": reason})
  callback_context.state["guardrail:last_block_reason"] = reason
  return _blocked_response(reason).content


def input_safety_guardrail(callback_context: Any, llm_request: LlmRequest) -> LlmResponse | None:
  """before_model_callback: block unsafe input; redact PII sent to the model."""
  text = _content_text(getattr(callback_context, "user_content", None)) or _last_user_text(llm_request)
  reason = check_user_text(text) if text else None
  if reason:
    logger.warning(
        "guardrail.input_blocked",
        extra={"reason": reason, "agent_name": getattr(callback_context, "agent_name", None)},
    )
    callback_context.state["guardrail:last_block_reason"] = reason
    return _blocked_response(reason)

  for content in llm_request.contents or []:
    if content.role != "user":
      continue
    for part in content.parts or []:
      if getattr(part, "text", None) and contains_pii(part.text):
        part.text = redact_text(part.text)
        logger.info("guardrail.input_pii_redacted")
  return None


def output_pii_guardrail(callback_context: Any, llm_response: LlmResponse) -> LlmResponse | None:
  """after_model_callback: scrub PII from model text before it is stored/shown."""
  content = getattr(llm_response, "content", None)
  if not content or not content.parts:
    return None
  changed = False
  for part in content.parts:
    if getattr(part, "text", None) and contains_pii(part.text):
      part.text = redact_text(part.text)
      changed = True
  if changed:
    logger.info("guardrail.output_pii_redacted")
    return llm_response
  return None


def tool_policy_guardrail(tool: Any, args: dict[str, Any], tool_context: Any) -> dict | None:
  """before_tool_callback: least-privilege + high-stakes policy enforcement."""
  agent_name = getattr(tool_context, "agent_name", None)
  allowed = AGENT_TOOL_ALLOWLIST.get(agent_name)
  if allowed is not None and tool.name not in allowed:
    logger.warning(
        "guardrail.tool_blocked",
        extra={"tool": tool.name, "agent_name": agent_name, "reason": "not_allowlisted"},
    )
    return tool_error(
        "TOOL_NOT_PERMITTED",
        f"Agent '{agent_name}' is not permitted to call '{tool.name}'.",
        "Do not call this tool. Continue with your permitted tools and leave this "
        "step for the agent that owns it.",
    )

  if tool.name in HIGH_STAKES_TOOLS:
    intake = tool_context.state.get("intake") or ""
    if tool.name == "restart_production_service" and "SEV4" in str(intake):
      return tool_error(
          "POLICY_SEVERITY_TOO_LOW",
          "Production restarts are not permitted for SEV4 (question-level) requests.",
          "Do not restart. Share diagnostic findings and runbook steps instead.",
      )
    count = int(tool_context.state.get(HIGH_STAKES_COUNTER_KEY, 0))
    if count >= SETTINGS.max_high_stakes_actions_per_session:
      return tool_error(
          "RATE_LIMITED",
          "Maximum number of high-stakes actions for this session reached.",
          "Stop and escalate to the incident commander via create_incident_ticket.",
      )
    tool_context.state[HIGH_STAKES_COUNTER_KEY] = count + 1
  return None


def handle_tool_error(tool: Any, args: dict[str, Any], tool_context: Any, error: Exception) -> dict:
  """on_tool_error_callback: never crash - return recovery guidance."""
  logger.warning(
      "tool.error_recovered", extra={"tool": tool.name, "error": type(error).__name__}
  )
  if isinstance(error, ValidationError):
    return validation_error_to_tool_error(error)
  return tool_error(
      "TOOL_EXECUTION_FAILED",
      f"{tool.name} raised {type(error).__name__}.",
      "Check the arguments against the tool schema and retry once. If it fails "
      "again, report the failure to the user and create an incident ticket.",
      retryable=True,
  )


class GovernancePlugin(BasePlugin):
  """App-wide ADK plugin: global audit trail + input policy for every agent."""

  def __init__(self) -> None:
    super().__init__(name="governance_plugin")

  async def on_user_message_callback(self, *, invocation_context: Any, user_message: types.Content):
    text = " ".join(p.text for p in (user_message.parts or []) if getattr(p, "text", None))
    logger.info(
        "request.received",
        extra={"event_type": "intent", "user_text": redact_text(text),
               "invocation_id": getattr(invocation_context, "invocation_id", None)},
    )
    return None

  async def before_tool_callback(self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any):
    if tool.name in HIGH_STAKES_TOOLS:
      logger.info(
          "audit.high_stakes_tool_requested",
          extra={"tool": tool.name, "tool_args": tool_args,
                 "agent_name": getattr(tool_context, "agent_name", None)},
      )
    return None

  async def after_run_callback(self, *, invocation_context: Any) -> None:
    logger.info(
        "request.completed",
        extra={"event_type": "outcome",
               "invocation_id": getattr(invocation_context, "invocation_id", None)},
    )
