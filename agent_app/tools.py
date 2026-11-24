"""Enterprise Ops tools.

Every tool:
  * has a specific, action-oriented name (``restart_production_service`` not
    ``do_action``),
  * takes a single strict Pydantic input model (ADK publishes its JSON Schema
    to Gemini) and returns a dict validated by a Pydantic output model,
  * never raises to the LLM: failures return the ``ToolError`` envelope with
    ``recovery_instructions`` the model can follow.

The CMDB / runbook / ITSM backends are deterministic in-memory fakes so the
project runs end-to-end without external systems; swap in real clients behind
the same schemas for production.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import re
from typing import Any, Callable

from google.adk.tools import FunctionTool, ToolContext
from pydantic import ValidationError

from .config import SETTINGS, SecretNotFoundError, get_secret
from .observability import get_tracer
from .schemas import (
    Category,
    ClassifyOpsRequestInput,
    ClassifyOpsRequestOutput,
    CreateIncidentTicketInput,
    CreateIncidentTicketOutput,
    Environment,
    GrantAccessInput,
    GrantAccessOutput,
    RestartServiceInput,
    RestartServiceOutput,
    RunbookEntry,
    RunbookSearchInput,
    RunbookSearchOutput,
    ServiceHealthInput,
    ServiceHealthOutput,
    Severity,
    ToolError,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Fake backends (deterministic)
# --------------------------------------------------------------------------
SERVICE_CATALOG: dict[str, dict[str, Any]] = {
    "payments-api": {
        "health": "degraded", "error_rate_pct": 7.4, "p95_latency_ms": 2400,
        "owner_team": "payments-sre", "last_deploy": "2026-11-24T14:05:00Z",
    },
    "checkout-web": {
        "health": "healthy", "error_rate_pct": 0.2, "p95_latency_ms": 310,
        "owner_team": "commerce-web", "last_deploy": "2026-11-20T09:00:00Z",
    },
    "auth-service": {
        "health": "down", "error_rate_pct": 100.0, "p95_latency_ms": 0,
        "owner_team": "identity-sre", "last_deploy": "2026-11-24T17:40:00Z",
    },
    "data-pipeline": {
        "health": "healthy", "error_rate_pct": 0.0, "p95_latency_ms": 900,
        "owner_team": "data-platform", "last_deploy": "2026-11-18T12:00:00Z",
    },
}

RUNBOOKS: list[dict[str, Any]] = [
    {
        "runbook_id": "RB-101", "title": "High error rate after deploy",
        "keywords": ["error", "5xx", "deploy", "rollback", "degraded", "latency"],
        "steps": ["Check last deploy diff", "Compare error budget burn",
                  "Roll back if burn > 10x", "Rolling restart if memory leak"],
        "risk_level": "medium",
    },
    {
        "runbook_id": "RB-202", "title": "Service down / crash loop",
        "keywords": ["down", "outage", "crash", "unavailable", "500", "restart"],
        "steps": ["Confirm health checks failing", "Inspect crash logs",
                  "Page owning team", "Rolling restart (requires approval)"],
        "risk_level": "high",
    },
    {
        "runbook_id": "RB-303", "title": "Temporary elevated access",
        "keywords": ["access", "permission", "role", "admin", "grant"],
        "steps": ["Validate justification", "Grant least privilege",
                  "Time-box to <= 8h", "Audit on expiry"],
        "risk_level": "high",
    },
    {
        "runbook_id": "RB-404", "title": "Slow responses / latency",
        "keywords": ["slow", "latency", "timeout", "p95", "performance"],
        "steps": ["Check p95 dashboards", "Scale out replicas",
                  "Check downstream dependencies"],
        "risk_level": "low",
    },
]

_TICKETS: dict[str, dict[str, Any]] = {}


# --------------------------------------------------------------------------
# Guided error handling
# --------------------------------------------------------------------------
class ToolExecutionError(Exception):
  """Business-rule failure that should be reported back to the LLM."""

  def __init__(self, error_code: str, message: str, recovery: str, retryable: bool = False):
    super().__init__(message)
    self.error = ToolError(
        error_code=error_code,
        message=message,
        recovery_instructions=recovery,
        retryable=retryable,
    )


def tool_error(error_code: str, message: str, recovery: str, retryable: bool = False) -> dict:
  """Builds the structured ``ToolError`` dict."""
  return ToolError(
      error_code=error_code,
      message=message,
      recovery_instructions=recovery,
      retryable=retryable,
  ).model_dump()


def validation_error_to_tool_error(exc: ValidationError) -> dict:
  """Translates a Pydantic ValidationError into recovery guidance."""
  problems = "; ".join(
      f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
  )
  return tool_error(
      "INVALID_ARGUMENTS",
      f"Arguments failed schema validation: {problems}",
      "Fix the listed fields to match the tool's JSON schema and call the tool "
      "again. Ask the user for any missing information instead of guessing.",
      retryable=True,
  )


def ops_tool(func: Callable[..., dict]) -> Callable[..., dict]:
  """Decorator: tracing span + structured error envelope for a tool."""

  @functools.wraps(func)
  def wrapper(*args: Any, **kwargs: Any) -> dict:
    with get_tracer().start_as_current_span(f"tool_impl.{func.__name__}") as span:
      try:
        result = func(*args, **kwargs)
        span.set_attribute("tool.status", result.get("status", "success"))
        return result
      except ToolExecutionError as exc:
        span.set_attribute("tool.status", "error")
        span.set_attribute("tool.error_code", exc.error.error_code)
        logger.warning(
            "tool.business_error",
            extra={"tool": func.__name__, "error_code": exc.error.error_code},
        )
        return exc.error.model_dump()
      except ValidationError as exc:
        span.set_attribute("tool.status", "error")
        return validation_error_to_tool_error(exc)
      except Exception as exc:  # pylint: disable=broad-except
        span.record_exception(exc)
        span.set_attribute("tool.status", "error")
        logger.exception("tool.unexpected_error", extra={"tool": func.__name__})
        return tool_error(
            "INTERNAL_ERROR",
            f"{func.__name__} failed unexpectedly ({type(exc).__name__}).",
            "Do not retry more than once. Tell the user the action could not be "
            "completed, and offer to create an incident ticket instead.",
            retryable=True,
        )

  return wrapper


def _coerce(model: type, request: Any):
  """Accepts a model instance or a dict (for direct/unit-test calls)."""
  return request if isinstance(request, model) else model.model_validate(request)


def _require_known_service(service_name: str) -> dict[str, Any]:
  service = SERVICE_CATALOG.get(service_name)
  if service is None:
    raise ToolExecutionError(
        "SERVICE_NOT_FOUND",
        f"Service '{service_name}' is not in the CMDB.",
        f"Ask the user to confirm the service ID. Known services: "
        f"{', '.join(sorted(SERVICE_CATALOG))}.",
    )
  return service


def _stable_id(prefix: str, *parts: str) -> str:
  digest = hashlib.sha256("|".join(parts).encode()).hexdigest()[:8].upper()
  return f"{prefix}-{digest}"


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------
_SEV1_TERMS = ("outage", "down", "unavailable", "all customers", "crash", "breach")
_SEV2_TERMS = ("degraded", "error rate", "5xx", "failing", "errors")
_SEV3_TERMS = ("slow", "latency", "timeout", "intermittent")
_ACCESS_TERMS = ("access", "permission", "grant", "role", "admin rights")
_SECURITY_TERMS = ("breach", "phishing", "leaked", "compromised", "malware")


@ops_tool
def classify_ops_request(request: ClassifyOpsRequestInput) -> dict:
  """Classifies an IT operations request into a category and severity.

  Use this FIRST for every new user request so downstream agents know how
  urgent it is and whether a human must approve any remediation.

  Args:
    request: ClassifyOpsRequestInput with ``request_text`` - the user's raw
      request, verbatim (3-4000 chars).

  Returns:
    ClassifyOpsRequestOutput dict: ``category`` (outage | performance |
    access_request | security | question), ``severity`` (SEV1-SEV4),
    ``service_name`` (detected CMDB service or null),
    ``requires_human_approval`` and a short ``rationale``. On failure a
    ToolError dict with ``recovery_instructions``.
  """
  req = _coerce(ClassifyOpsRequestInput, request)
  text = req.request_text.lower()
  service = next((s for s in SERVICE_CATALOG if s in text), None)

  if any(t in text for t in _SECURITY_TERMS):
    category, severity = Category.SECURITY, Severity.SEV1
  elif any(t in text for t in _ACCESS_TERMS):
    category, severity = Category.ACCESS_REQUEST, Severity.SEV3
  elif any(t in text for t in _SEV1_TERMS):
    category, severity = Category.OUTAGE, Severity.SEV1
  elif any(t in text for t in _SEV2_TERMS):
    category, severity = Category.OUTAGE, Severity.SEV2
  elif any(t in text for t in _SEV3_TERMS):
    category, severity = Category.PERFORMANCE, Severity.SEV3
  else:
    category, severity = Category.QUESTION, Severity.SEV4

  needs_approval = category in (Category.ACCESS_REQUEST, Category.SECURITY) or (
      severity in (Severity.SEV1, Severity.SEV2) and bool(re.search(r"restart|reboot|rollback", text))
  )
  return ClassifyOpsRequestOutput(
      category=category,
      severity=severity,
      service_name=service,
      requires_human_approval=needs_approval,
      rationale=f"Matched {category.value} signals; severity {severity.value}.",
  ).model_dump(mode="json")


@ops_tool
def get_service_health_status(request: ServiceHealthInput) -> dict:
  """Returns live health metrics for one service from the CMDB/monitoring.

  Args:
    request: ServiceHealthInput with ``service_name`` (lowercase CMDB ID such
      as 'payments-api') and ``environment`` (prod | staging | dev, default
      prod).

  Returns:
    ServiceHealthOutput dict: ``health`` (healthy | degraded | down),
    ``error_rate_pct``, ``p95_latency_ms``, ``owner_team`` and
    ``last_deploy``. On failure (e.g. unknown service) a ToolError dict whose
    ``recovery_instructions`` list the valid service IDs.
  """
  req = _coerce(ServiceHealthInput, request)
  svc = _require_known_service(req.service_name)
  return ServiceHealthOutput(
      service_name=req.service_name, environment=req.environment, **svc
  ).model_dump(mode="json")


@ops_tool
def search_runbook_knowledge_base(request: RunbookSearchInput) -> dict:
  """Searches approved SRE runbooks for remediation steps matching symptoms.

  Only recommend remediation steps that come from a runbook returned here.

  Args:
    request: RunbookSearchInput with ``query`` (symptoms/keywords, 3-300
      chars) and ``max_results`` (1-5, default 3).

  Returns:
    RunbookSearchOutput dict with ``results``: a list of runbooks, each with
    ``runbook_id``, ``title``, ordered ``steps`` and ``risk_level``. Returns a
    ToolError with ``NO_RUNBOOK_MATCH`` and rephrasing guidance if empty.
  """
  req = _coerce(RunbookSearchInput, request)
  terms = set(re.findall(r"[a-z0-9]+", req.query.lower()))
  scored = sorted(
      ((len(terms & set(rb["keywords"])), rb) for rb in RUNBOOKS),
      key=lambda x: -x[0],
  )
  hits = [rb for score, rb in scored if score > 0][: req.max_results]
  if not hits:
    raise ToolExecutionError(
        "NO_RUNBOOK_MATCH",
        f"No runbook matched '{req.query}'.",
        "Rephrase the query with concrete symptoms (e.g. 'error rate', 'down', "
        "'latency', 'access'). If still nothing, escalate by creating an "
        "incident ticket rather than inventing steps.",
        retryable=True,
    )
  return RunbookSearchOutput(
      results=[RunbookEntry(**{k: v for k, v in rb.items() if k != "keywords"}) for rb in hits]
  ).model_dump(mode="json")


@ops_tool
def create_incident_ticket(request: CreateIncidentTicketInput, tool_context: ToolContext) -> dict:
  """Creates an incident ticket in the ITSM system and assigns the owning team.

  Credentials for the ITSM API come from Secret Manager (``ITSM_API_TOKEN``);
  without them the ticket is recorded in the local fake ITSM.

  Args:
    request: CreateIncidentTicketInput with ``title`` (5-120 chars),
      ``severity`` (SEV1-SEV4), ``service_name`` and ``summary`` (impact,
      symptoms, plan; 10-2000 chars).
    tool_context: Injected by ADK; used to persist the ticket ID in session
      state.

  Returns:
    CreateIncidentTicketOutput dict with ``ticket_id``, ``url`` and
    ``assigned_team``, or a ToolError with recovery instructions.
  """
  req = _coerce(CreateIncidentTicketInput, request)
  svc = _require_known_service(req.service_name)
  try:
    get_secret("ITSM_API_TOKEN")  # Real client would authenticate with this.
    backend = "itsm"
  except SecretNotFoundError:
    backend = "local_fake"
  ticket_id = _stable_id("INC", req.title, req.service_name, req.severity.value)
  _TICKETS[ticket_id] = {**req.model_dump(mode="json"), "backend": backend}
  tickets = list(tool_context.state.get("open_ticket_ids", []))
  tickets.append(ticket_id)
  tool_context.state["open_ticket_ids"] = tickets
  return CreateIncidentTicketOutput(
      ticket_id=ticket_id,
      url=f"https://itsm.example.com/incidents/{ticket_id}",
      assigned_team=svc["owner_team"],
  ).model_dump(mode="json")


@ops_tool
def restart_production_service(request: RestartServiceInput, tool_context: ToolContext) -> dict:
  """Performs a rolling restart of a service. HIGH STAKES - human approval.

  This tool is wrapped with ADK ``require_confirmation=True``: execution pauses
  and a human must explicitly approve before this code runs.

  Args:
    request: RestartServiceInput with ``service_name`` (CMDB ID),
      ``environment`` (prod | staging | dev) and ``reason`` (10-500 chars).
    tool_context: Injected by ADK; carries the human confirmation and session
      state.

  Returns:
    RestartServiceOutput dict with ``restart_id`` and ``approved_by_human``,
    or a ToolError (e.g. ``HUMAN_APPROVAL_REQUIRED``, ``SERVICE_NOT_FOUND``).
  """
  req = _coerce(RestartServiceInput, request)
  _require_known_service(req.service_name)
  confirmation = getattr(tool_context, "tool_confirmation", None)
  if req.environment == Environment.PROD and not (confirmation and confirmation.confirmed):
    raise ToolExecutionError(
        "HUMAN_APPROVAL_REQUIRED",
        "Production restarts require explicit human approval, which was not given.",
        "Stop. Tell the user the restart was NOT performed and ask them to "
        "approve it explicitly, or create an incident ticket instead.",
    )
  restart_id = _stable_id("RST", req.service_name, req.environment.value, req.reason)
  tool_context.state["last_restart_id"] = restart_id
  return RestartServiceOutput(
      service_name=req.service_name,
      environment=req.environment,
      restart_id=restart_id,
      approved_by_human=bool(confirmation and confirmation.confirmed),
  ).model_dump(mode="json")


@ops_tool
def grant_temporary_access(request: GrantAccessInput, tool_context: ToolContext) -> dict:
  """Grants time-boxed, least-privilege access. HIGH STAKES - human approval.

  Wrapped with ADK ``require_confirmation=True``. Only corporate-domain
  identities are eligible and duration is capped by policy (MAX_ACCESS_HOURS).

  Args:
    request: GrantAccessInput with ``user_email`` (corporate address),
      ``role`` (viewer | operator | admin), ``duration_hours`` (1-24, policy
      cap applies) and ``justification`` (10-500 chars).
    tool_context: Injected by ADK; carries the human confirmation.

  Returns:
    GrantAccessOutput dict with ``grant_id``, ``role``, ``expires_in_hours``
    and ``approved_by_human``, or a ToolError with recovery instructions.
  """
  req = _coerce(GrantAccessInput, request)
  if not req.user_email.lower().endswith("@" + SETTINGS.corporate_email_domain):
    raise ToolExecutionError(
        "NON_CORPORATE_IDENTITY",
        "Access can only be granted to corporate identities.",
        f"Ask the user for their @{SETTINGS.corporate_email_domain} address.",
    )
  if req.duration_hours > SETTINGS.max_access_hours:
    raise ToolExecutionError(
        "DURATION_EXCEEDS_POLICY",
        f"Requested {req.duration_hours}h exceeds the {SETTINGS.max_access_hours}h cap.",
        f"Retry with duration_hours <= {SETTINGS.max_access_hours}.",
        retryable=True,
    )
  confirmation = getattr(tool_context, "tool_confirmation", None)
  if not (confirmation and confirmation.confirmed):
    raise ToolExecutionError(
        "HUMAN_APPROVAL_REQUIRED",
        "Access grants require explicit human approval, which was not given.",
        "Tell the user access was NOT granted and that an approver must confirm.",
    )
  return GrantAccessOutput(
      grant_id=_stable_id("GRT", req.user_email, req.role, str(req.duration_hours)),
      role=req.role,
      expires_in_hours=req.duration_hours,
      approved_by_human=True,
  ).model_dump(mode="json")


# --------------------------------------------------------------------------
# ADK tool objects
# --------------------------------------------------------------------------
HIGH_STAKES_TOOLS = frozenset({"restart_production_service", "grant_temporary_access"})

classify_tool = FunctionTool(classify_ops_request)
health_tool = FunctionTool(get_service_health_status)
runbook_tool = FunctionTool(search_runbook_knowledge_base)
ticket_tool = FunctionTool(create_incident_ticket)
# Human-in-the-loop: ADK pauses the run and emits an `adk_request_confirmation`
# event; the tool body only executes after a human approves.
restart_tool = FunctionTool(restart_production_service, require_confirmation=True)
access_tool = FunctionTool(grant_temporary_access, require_confirmation=True)
