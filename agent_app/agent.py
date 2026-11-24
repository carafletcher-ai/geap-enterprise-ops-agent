"""GEAP Enterprise Ops Agent - multi-agent workflow definition.

Problem: enterprise on-call engineers lose critical minutes triaging
incidents, hunting for runbooks, and coordinating risky remediations.
Solution: a SequentialAgent pipeline that triages -> plans -> executes (with
human approval for high-stakes actions) -> self-reviews.

    root_agent (SequentialAgent "ops_pipeline")
      1. intake_agent    Flash  classify severity/category       -> state.intake
      2. planner_agent   Pro    health + runbooks + memory plan  -> state.plan
      3. executor_agent  Flash  tickets / restarts / access(HITL)-> state.execution
      4. reviewer_agent  Flash  self-eval guardrail (ReviewVerdict) -> state.review
"""

from __future__ import annotations

from typing import Any

from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.apps import App
from google.adk.apps.app import ResumabilityConfig
from google.adk.tools import load_memory
from google.genai import types

from . import prompts
from .config import SETTINGS
from .guardrails import (
    GovernancePlugin,
    handle_tool_error,
    pipeline_input_gate,
    input_safety_guardrail,
    output_pii_guardrail,
    tool_policy_guardrail,
)
from .memory import (
    build_compaction_config,
    build_context_cache_config,
    consolidate_memory_async,
    trim_history,
)
from .observability import (
    configure_logging,
    configure_tracing,
    log_agent_end,
    log_agent_start,
    log_tool_intent,
    log_tool_outcome,
)
from .routing import STATIC_ROUTES, dynamic_model_router
from .schemas import ReviewVerdict
from .tools import (
    access_tool,
    classify_tool,
    health_tool,
    restart_tool,
    runbook_tool,
    ticket_tool,
)

configure_logging()
configure_tracing()

APP_NAME = "agent_app"

# Shared callback stacks (order matters: first non-None return short-circuits).
BEFORE_MODEL = [input_safety_guardrail, trim_history, dynamic_model_router]
AFTER_MODEL = [output_pii_guardrail]
BEFORE_TOOL = [log_tool_intent, tool_policy_guardrail]
AFTER_TOOL = [log_tool_outcome]

_LOW_TEMP = types.GenerateContentConfig(temperature=0.1)


def _llm_agent(name: str, **kwargs: Any) -> LlmAgent:
  return LlmAgent(
      name=name,
      global_instruction=prompts.GLOBAL_CONSTITUTION,
      generate_content_config=_LOW_TEMP,
      before_model_callback=BEFORE_MODEL,
      after_model_callback=AFTER_MODEL,
      before_tool_callback=BEFORE_TOOL,
      after_tool_callback=AFTER_TOOL,
      on_tool_error_callback=handle_tool_error,
      before_agent_callback=log_agent_start,
      after_agent_callback=log_agent_end,
      **kwargs,
  )


def init_session_state(callback_context: Any) -> None:
  """before_agent_callback (root): seed persistent, user-scoped state."""
  state = callback_context.state
  state["user:total_turns"] = int(state.get("user:total_turns", 0)) + 1
  if "user:preferred_escalation_channel" not in state:
    state["user:preferred_escalation_channel"] = "pager"
  state["app:pipeline_version"] = "1.0.0"
  return None


def build_pipeline(model_override: Any = None) -> SequentialAgent:
  """Builds a fresh SequentialAgent pipeline.

  Args:
    model_override: Optional BaseLlm/model name used for every sub-agent
      (used by the offline test-suite with a scripted LLM).
  """

  def m(name: str) -> Any:
    return model_override or STATIC_ROUTES[name]

  intake_agent = _llm_agent(
      "intake_agent",
      model=m("intake_agent"),
      description="Classifies the request's category and severity (fast, Flash).",
      instruction=prompts.INTAKE_INSTRUCTION,
      tools=[classify_tool],
      output_key="intake",
  )
  planner_agent = _llm_agent(
      "planner_agent",
      model=m("planner_agent"),
      description="Builds a runbook-grounded remediation plan (deep reasoning, Pro).",
      instruction=prompts.PLANNER_INSTRUCTION,
      tools=[health_tool, runbook_tool, load_memory],
      output_key="plan",
  )
  executor_agent = _llm_agent(
      "executor_agent",
      model=m("executor_agent"),
      description="Executes the plan; high-stakes tools pause for human approval.",
      instruction=prompts.EXECUTOR_INSTRUCTION,
      tools=[ticket_tool, restart_tool, access_tool, health_tool],
      output_key="execution",
  )
  reviewer_agent = _llm_agent(
      "reviewer_agent",
      model=m("reviewer_agent"),
      description="Self-evaluation guardrail: grounding, PII and policy review.",
      instruction=prompts.REVIEWER_INSTRUCTION,
      output_schema=ReviewVerdict,
      output_key="review",
  )
  return SequentialAgent(
      name="ops_pipeline",
      description="Triage -> plan -> execute (HITL) -> review pipeline for IT ops.",
      sub_agents=[intake_agent, planner_agent, executor_agent, reviewer_agent],
      before_agent_callback=[pipeline_input_gate, init_session_state],
      after_agent_callback=consolidate_memory_async,
  )


def build_app(root: SequentialAgent | None = None, *, compaction: bool = True) -> App:
  """Wraps the pipeline in an ADK App with compaction, caching, HITL resume."""
  return App(
      name=APP_NAME,
      root_agent=root or build_pipeline(),
      plugins=[GovernancePlugin()],
      events_compaction_config=build_compaction_config() if compaction else None,
      context_cache_config=build_context_cache_config(),
      # Required so HITL confirmations can pause and resume the pipeline.
      resumability_config=ResumabilityConfig(is_resumable=True),
  )


root_agent = build_pipeline()
app = build_app(root_agent)

__all__ = ["root_agent", "app", "SETTINGS"]
