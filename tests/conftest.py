"""Shared pytest fixtures, including a deterministic scripted LLM so the full
multi-agent pipeline (tools, guardrails, HITL, persistence) runs offline."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncGenerator

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "false")

from google.adk.models import BaseLlm, LlmRequest, LlmResponse  # noqa: E402
from google.genai import types  # noqa: E402


def _fc(name: str, args: dict[str, Any]) -> LlmResponse:
  return LlmResponse(
      content=types.Content(
          role="model",
          parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))],
      )
  )


def _text(text: str) -> LlmResponse:
  return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


AGENT_SCRIPTS: dict[str, list[tuple[str, dict[str, Any]]]] = {
    "Intake Triage Agent": [
        ("classify_ops_request", {"request": {"request_text": "payments-api is down with 5xx errors, please restart it"}}),
    ],
    "Remediation Planner Agent": [
        ("get_service_health_status", {"request": {"service_name": "payments-api", "environment": "prod"}}),
        ("search_runbook_knowledge_base", {"request": {"query": "service down 5xx restart", "max_results": 2}}),
    ],
    "Remediation Executor Agent": [
        ("create_incident_ticket", {"request": {
            "title": "payments-api outage", "severity": "SEV1",
            "service_name": "payments-api",
            "summary": "payments-api returning 5xx; restarting per RB-202."}}),
        ("restart_production_service", {"request": {
            "service_name": "payments-api", "environment": "prod",
            "reason": "Crash loop per runbook RB-202"}}),
    ],
}

FINAL_TEXT = {
    "Intake Triage Agent": json.dumps({
        "category": "outage", "severity": "SEV1", "service_name": "payments-api",
        "requires_human_approval": True, "user_goal": "Restore payments-api"}),
    "Remediation Planner Agent": "1. Create ticket (RB-202)\n2. Rolling restart [REQUIRES HUMAN APPROVAL] (RB-202)",
    "Remediation Executor Agent": "Ticket created; restart executed after approval.",
    "Safety & Quality Reviewer Agent": json.dumps({
        "verdict": "approved", "policy_violations": [], "pii_detected": False,
        "grounded_in_tool_results": True,
        "final_answer": "payments-api SEV1: ticket opened, restart approved and executed."}),
}


class ScriptedLlm(BaseLlm):
  """Deterministic fake Gemini: picks the next scripted step per agent."""

  model: str = "scripted-llm"
  calls: list[str] = []

  @classmethod
  def supported_models(cls) -> list[str]:
    return [r"scripted-.*"]

  async def generate_content_async(
      self, llm_request: LlmRequest, stream: bool = False
  ) -> AsyncGenerator[LlmResponse, None]:
    system = str(getattr(llm_request.config, "system_instruction", "") or "")
    agent_key = next((k for k in FINAL_TEXT if k in system), None)
    self.calls.append(agent_key or "unknown")
    script = AGENT_SCRIPTS.get(agent_key, [])
    tool_names = {name for name, _ in script}
    done = 0
    for content in llm_request.contents or []:
      for part in content.parts or []:
        fr = getattr(part, "function_response", None)
        if fr is not None and fr.name in tool_names:
          done += 1
    if done < len(script):
      name, args = script[done]
      yield _fc(name, args)
    else:
      yield _text(FINAL_TEXT.get(agent_key, "ok"))


@pytest.fixture
def scripted_llm() -> ScriptedLlm:
  llm = ScriptedLlm()
  llm.calls = []
  return llm


class FakeToolContext:
  """Minimal stand-in for ADK ToolContext in unit tests."""

  def __init__(self, agent_name: str = "executor_agent", confirmed: bool | None = None, state=None):
    self.agent_name = agent_name
    self.state = state if state is not None else {}
    self.function_call_id = "call-1"
    self.invocation_id = "inv-1"
    self.tool_confirmation = (
        None if confirmed is None else SimpleNamespace(confirmed=confirmed)
    )


class FakeTool:
  def __init__(self, name: str):
    self.name = name


@pytest.fixture
def tool_ctx():
  return FakeToolContext
