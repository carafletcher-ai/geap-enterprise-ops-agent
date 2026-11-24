"""Tool & Interface Design: docstrings, naming, schemas, guided errors."""

from __future__ import annotations

import inspect

import pytest

from agent_app import tools
from agent_app.schemas import ALL_TOOL_SCHEMAS, ToolError

TOOL_FUNCS = [
    tools.classify_ops_request,
    tools.get_service_health_status,
    tools.search_runbook_knowledge_base,
    tools.create_incident_ticket,
    tools.restart_production_service,
    tools.grant_temporary_access,
]
TOOL_OBJS = [
    tools.classify_tool, tools.health_tool, tools.runbook_tool,
    tools.ticket_tool, tools.restart_tool, tools.access_tool,
]


@pytest.mark.parametrize("func", TOOL_FUNCS, ids=lambda f: f.__name__)
def test_every_tool_has_comprehensive_docstring(func):
  doc = inspect.getdoc(func)
  assert doc and len(doc) > 150
  assert "Args:" in doc and "Returns:" in doc
  assert "request" in doc  # Documents its parameter.


@pytest.mark.parametrize("func", TOOL_FUNCS, ids=lambda f: f.__name__)
def test_tool_names_are_descriptive_verb_noun(func):
  name = func.__name__
  assert "_" in name and len(name) >= 15
  assert name.split("_")[0] in {"classify", "get", "search", "create", "restart", "grant"}
  assert name not in {"run", "do_action", "update", "helper"}


@pytest.mark.parametrize("tool", TOOL_OBJS, ids=lambda t: t.name)
def test_tools_publish_strict_json_schema(tool):
  decl = tool._get_declaration()
  schema = decl.parameters_json_schema
  assert schema, "Tool must expose a JSON schema to the LLM"
  assert schema["required"] == ["request"]
  assert "tool_context" not in schema["properties"]
  assert tool.name in ALL_TOOL_SCHEMAS
  in_model, out_model = ALL_TOOL_SCHEMAS[tool.name]
  assert in_model.model_config["extra"] == "forbid"
  assert out_model.model_json_schema()["properties"]


def test_high_stakes_tools_require_confirmation():
  assert tools.restart_tool._require_confirmation is True
  assert tools.access_tool._require_confirmation is True
  assert tools.classify_tool._require_confirmation is False


def test_classify_outage_vs_question():
  out = tools.classify_ops_request({"request_text": "auth-service is down for all customers"})
  assert out["status"] == "success"
  assert out["severity"] == "SEV1" and out["category"] == "outage"
  assert out["service_name"] == "auth-service"
  q = tools.classify_ops_request({"request_text": "what is the on-call rotation policy?"})
  assert q["severity"] == "SEV4" and q["category"] == "question"


def test_service_health_success_and_output_schema():
  out = tools.get_service_health_status({"service_name": "payments-api"})
  assert out["health"] == "degraded" and out["owner_team"] == "payments-sre"


def test_unknown_service_returns_guided_error():
  out = tools.get_service_health_status({"service_name": "unknown-svc"})
  ToolError.model_validate(out)
  assert out["error_code"] == "SERVICE_NOT_FOUND"
  assert "payments-api" in out["recovery_instructions"]


def test_invalid_arguments_return_guided_error_not_exception():
  out = tools.get_service_health_status({"service_name": "BAD NAME!"})
  assert out["status"] == "error" and out["error_code"] == "INVALID_ARGUMENTS"
  assert out["retryable"] is True and "schema" in out["recovery_instructions"]


def test_extra_fields_rejected():
  out = tools.search_runbook_knowledge_base({"query": "down", "drop_tables": True})
  assert out["error_code"] == "INVALID_ARGUMENTS"


def test_runbook_search_and_no_match_recovery():
  ok = tools.search_runbook_knowledge_base({"query": "service down crash"})
  assert ok["results"][0]["runbook_id"] == "RB-202"
  miss = tools.search_runbook_knowledge_base({"query": "zzz qqq"})
  assert miss["error_code"] == "NO_RUNBOOK_MATCH" and miss["retryable"]


def test_create_ticket_persists_to_state(tool_ctx):
  ctx = tool_ctx()
  out = tools.create_incident_ticket(
      {"title": "Checkout slow", "severity": "SEV3", "service_name": "checkout-web",
       "summary": "p95 latency elevated for 10 minutes"}, ctx)
  assert out["ticket_id"].startswith("INC-")
  assert ctx.state["open_ticket_ids"] == [out["ticket_id"]]


def test_restart_requires_human_confirmation(tool_ctx):
  req = {"service_name": "payments-api", "environment": "prod", "reason": "memory leak per RB-101"}
  denied = tools.restart_production_service(req, tool_ctx(confirmed=False))
  assert denied["error_code"] == "HUMAN_APPROVAL_REQUIRED"
  ok = tools.restart_production_service(req, tool_ctx(confirmed=True))
  assert ok["status"] == "success" and ok["approved_by_human"] is True


def test_grant_access_policy_errors(tool_ctx):
  base = {"role": "operator", "justification": "Debug SEV2 incident INC-1"}
  bad_domain = tools.grant_temporary_access(
      {**base, "user_email": "x@gmail.com", "duration_hours": 2}, tool_ctx(confirmed=True))
  assert bad_domain["error_code"] == "NON_CORPORATE_IDENTITY"
  too_long = tools.grant_temporary_access(
      {**base, "user_email": "a@example.com", "duration_hours": 20}, tool_ctx(confirmed=True))
  assert too_long["error_code"] == "DURATION_EXCEEDS_POLICY" and too_long["retryable"]
  ok = tools.grant_temporary_access(
      {**base, "user_email": "a@example.com", "duration_hours": 4}, tool_ctx(confirmed=True))
  assert ok["status"] == "success"


def test_unexpected_exception_is_wrapped(monkeypatch):
  monkeypatch.setattr(tools, "_require_known_service", lambda _: 1 / 0)
  out = tools.get_service_health_status({"service_name": "payments-api"})
  assert out["error_code"] == "INTERNAL_ERROR"
  assert "incident ticket" in out["recovery_instructions"]
