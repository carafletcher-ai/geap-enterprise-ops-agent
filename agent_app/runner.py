"""Interactive CLI runner with persistent sessions and human-in-the-loop.

Usage:
    python -m agent_app.runner --user oncall-1 --session my-incident

When a high-stakes tool (restart / access grant) is requested, ADK pauses and
emits an ``adk_request_confirmation`` function call. This runner stops, shows
the proposed action to the human, and only resumes if they type ``yes``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any, AsyncIterator, Callable

from google.adk.apps import App
from google.adk.runners import Runner
from google.genai import types

from .agent import app as default_app
from .memory import build_memory_service, build_session_service, drain_background_tasks
from .observability import get_tracer
from .pii import redact_text

logger = logging.getLogger("geap.runner")
CONFIRMATION_CALL = "adk_request_confirmation"

ApprovalFn = Callable[[dict[str, Any]], bool]


def cli_approval(call_args: dict[str, Any]) -> bool:
  """Prompts the human operator to approve a high-stakes action."""
  original = call_args.get("originalFunctionCall", {}) or {}
  print("\n=== HUMAN APPROVAL REQUIRED ===")
  print(f"Action : {original.get('name')}")
  print(f"Args   : {original.get('args')}")
  answer = input("Approve this action? [yes/NO]: ").strip().lower()
  return answer in {"y", "yes"}


def build_runner(app: App | None = None, session_service=None, memory_service=None) -> Runner:
  return Runner(
      app=app or default_app,
      session_service=session_service or build_session_service(),
      memory_service=memory_service or build_memory_service(),
  )


async def _stream(runner: Runner, user_id: str, session_id: str, message: types.Content) -> AsyncIterator[Any]:
  async for event in runner.run_async(
      user_id=user_id, session_id=session_id, new_message=message
  ):
    yield event


async def run_turn(
    runner: Runner,
    user_id: str,
    session_id: str,
    text: str,
    approve: ApprovalFn = cli_approval,
) -> dict[str, Any]:
  """Runs one user turn, handling HITL confirmations until completion.

  Returns:
    dict with ``final_text``, ``approvals`` (list of decisions) and ``events``.
  """
  tracer = get_tracer()
  approvals: list[dict[str, Any]] = []
  events: list[Any] = []
  final_text = ""
  message = types.Content(role="user", parts=[types.Part(text=text)])
  with tracer.start_as_current_span("ops_request") as span:
    span.set_attribute("user.id", user_id)
    span.set_attribute("session.id", session_id)
    span.set_attribute("request.text", redact_text(text))
    while message is not None:
      pending: list[types.FunctionCall] = []
      async for event in _stream(runner, user_id, session_id, message):
        events.append(event)
        for call in event.get_function_calls() or []:
          if call.name == CONFIRMATION_CALL:
            pending.append(call)
        if event.content and event.content.parts:
          texts = [p.text for p in event.content.parts if getattr(p, "text", None)]
          if texts and not event.partial:
            final_text = "\n".join(texts)
      message = None
      if pending:
        parts = []
        for call in pending:
          decision = approve(dict(call.args or {}))
          approvals.append({"call_id": call.id, "approved": decision})
          logger.info(
              "hitl.decision",
              extra={"event_type": "outcome", "call_id": call.id, "approved": decision},
          )
          parts.append(types.Part(function_response=types.FunctionResponse(
              id=call.id, name=CONFIRMATION_CALL, response={"confirmed": decision},
          )))
        message = types.Content(role="user", parts=parts)
    span.set_attribute("hitl.approvals", len(approvals))
  return {"final_text": final_text, "approvals": approvals, "events": events}


async def _main(user_id: str, session_id: str) -> None:
  runner = build_runner()
  svc = runner.session_service
  session = await svc.get_session(app_name=runner.app_name, user_id=user_id, session_id=session_id)
  if session is None:
    await svc.create_session(app_name=runner.app_name, user_id=user_id, session_id=session_id)
  print("GEAP Enterprise Ops Agent. Type 'exit' to quit.")
  while True:
    text = input("\nyou> ").strip()
    if text.lower() in {"exit", "quit"}:
      break
    if not text:
      continue
    result = await run_turn(runner, user_id, session_id, text)
    print(f"\nagent> {result['final_text']}")
  await drain_background_tasks()


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--user", default="oncall-engineer")
  parser.add_argument("--session", default="default-session")
  args = parser.parse_args()
  asyncio.run(_main(args.user, args.session))


if __name__ == "__main__":
  main()
