"""Offline (deterministic) regression harness over the golden eval set.

Measures, without calling an LLM:
  * guardrail accuracy  - blocked cases are stopped by ``pipeline_input_gate``
  * triage accuracy     - ``classify_ops_request`` category/severity/service
  * trajectory validity - every expected tool exists and is allowlisted for
                          some agent; high-stakes tools require confirmation

Run: python -m evals.offline_eval   (exit code 1 on regression)
For LLM-in-the-loop evals use: adk eval agent_app evals/golden_evalset.json
--config_file_path evals/test_config.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from google.genai import types

from agent_app import tools
from agent_app.guardrails import AGENT_TOOL_ALLOWLIST, pipeline_input_gate

GOLDEN = Path(__file__).with_name("golden_evalset.json")
ALL_ALLOWED = set().union(*AGENT_TOOL_ALLOWLIST.values())
TOOL_OBJECTS = {t.name: t for t in (
    tools.classify_tool, tools.health_tool, tools.runbook_tool,
    tools.ticket_tool, tools.restart_tool, tools.access_tool)}


def load_cases(path: Path = GOLDEN) -> list[dict[str, Any]]:
  return json.loads(path.read_text())["eval_cases"]


def evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
  inv = case["conversation"][0]
  text = inv["user_content"]["parts"][0]["text"]
  labels = case["labels"]
  failures: list[str] = []

  ctx = SimpleNamespace(state={}, user_content=types.Content(role="user", parts=[types.Part(text=text)]))
  blocked = pipeline_input_gate(ctx) is not None
  if blocked != labels["blocked"]:
    failures.append(f"blocked={blocked} expected {labels['blocked']}")

  if not labels["blocked"]:
    out = tools.classify_ops_request({"request_text": text})
    for key in ("category", "severity", "service_name"):
      if out.get(key) != labels[key]:
        failures.append(f"{key}={out.get(key)!r} expected {labels[key]!r}")
    if "requires_human_approval" in labels and out["requires_human_approval"] != labels["requires_human_approval"]:
      failures.append("requires_human_approval mismatch")

  for use in inv["intermediate_data"]["tool_uses"]:
    name = use["name"]
    if name not in ALL_ALLOWED:
      failures.append(f"tool {name} not allowlisted")
    if name in tools.HIGH_STAKES_TOOLS and not TOOL_OBJECTS[name]._require_confirmation:
      failures.append(f"high-stakes tool {name} lacks HITL confirmation")
  return {"eval_id": case["eval_id"], "passed": not failures, "failures": failures}


def run(path: Path = GOLDEN) -> dict[str, Any]:
  results = [evaluate_case(c) for c in load_cases(path)]
  passed = sum(r["passed"] for r in results)
  return {"total": len(results), "passed": passed, "score": passed / max(len(results), 1), "results": results}


if __name__ == "__main__":
  report = run()
  print(json.dumps(report, indent=2))
  sys.exit(0 if report["score"] == 1.0 else 1)
