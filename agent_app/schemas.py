"""Strict Pydantic JSON schemas for every tool input and output.

These models are passed directly as tool parameters, so ADK publishes their
JSON Schema to Gemini as the function declaration, constraining the LLM's
arguments. Outputs are validated through the ``*Output`` models before being
returned, and every error uses the shared ``ToolError`` envelope.
"""

from __future__ import annotations

import enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
  """Base model: unknown fields are rejected, strings are stripped."""

  model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Severity(str, enum.Enum):
  SEV1 = "SEV1"  # Customer-facing outage
  SEV2 = "SEV2"  # Major degradation
  SEV3 = "SEV3"  # Minor degradation
  SEV4 = "SEV4"  # Question / low impact


class Category(str, enum.Enum):
  OUTAGE = "outage"
  PERFORMANCE = "performance"
  ACCESS_REQUEST = "access_request"
  SECURITY = "security"
  QUESTION = "question"


class Environment(str, enum.Enum):
  PROD = "prod"
  STAGING = "staging"
  DEV = "dev"


# --------------------------------------------------------------------------
# Shared error envelope (guided error handling)
# --------------------------------------------------------------------------
class ToolError(StrictModel):
  """Structured error returned to the LLM instead of raising."""

  status: Literal["error"] = "error"
  error_code: str = Field(description="Machine-readable error code.")
  message: str = Field(description="What went wrong, in plain language.")
  recovery_instructions: str = Field(
      description="Concrete next step the agent should take to recover."
  )
  retryable: bool = Field(description="Whether retrying the same call may work.")


# --------------------------------------------------------------------------
# classify_ops_request
# --------------------------------------------------------------------------
class ClassifyOpsRequestInput(StrictModel):
  request_text: str = Field(
      min_length=3,
      max_length=4000,
      description="The raw operations request from the user, verbatim.",
  )


class ClassifyOpsRequestOutput(StrictModel):
  status: Literal["success"] = "success"
  category: Category
  severity: Severity
  service_name: str | None = Field(
      default=None, description="Service detected in the request, if any."
  )
  requires_human_approval: bool
  rationale: str


# --------------------------------------------------------------------------
# get_service_health_status
# --------------------------------------------------------------------------
class ServiceHealthInput(StrictModel):
  service_name: str = Field(
      min_length=2,
      max_length=64,
      pattern=r"^[a-z0-9][a-z0-9\-]+$",
      description="Lowercase service ID from the CMDB, e.g. 'payments-api'.",
  )
  environment: Environment = Field(
      default=Environment.PROD, description="Deployment environment to inspect."
  )


class ServiceHealthOutput(StrictModel):
  status: Literal["success"] = "success"
  service_name: str
  environment: Environment
  health: Literal["healthy", "degraded", "down"]
  error_rate_pct: float = Field(ge=0, le=100)
  p95_latency_ms: int = Field(ge=0)
  owner_team: str
  last_deploy: str


# --------------------------------------------------------------------------
# search_runbook_knowledge_base
# --------------------------------------------------------------------------
class RunbookSearchInput(StrictModel):
  query: str = Field(
      min_length=3, max_length=300, description="Symptoms or keywords to search."
  )
  max_results: int = Field(default=3, ge=1, le=5, description="Results to return.")


class RunbookEntry(StrictModel):
  runbook_id: str
  title: str
  steps: list[str]
  risk_level: Literal["low", "medium", "high"]


class RunbookSearchOutput(StrictModel):
  status: Literal["success"] = "success"
  results: list[RunbookEntry]


# --------------------------------------------------------------------------
# create_incident_ticket
# --------------------------------------------------------------------------
class CreateIncidentTicketInput(StrictModel):
  title: str = Field(min_length=5, max_length=120, description="Short summary.")
  severity: Severity = Field(description="Incident severity SEV1-SEV4.")
  service_name: str = Field(
      min_length=2, max_length=64, description="Affected service ID."
  )
  summary: str = Field(
      min_length=10, max_length=2000, description="Impact, symptoms, and plan."
  )


class CreateIncidentTicketOutput(StrictModel):
  status: Literal["success"] = "success"
  ticket_id: str
  url: str
  assigned_team: str


# --------------------------------------------------------------------------
# restart_production_service (HIGH STAKES, HITL)
# --------------------------------------------------------------------------
class RestartServiceInput(StrictModel):
  service_name: str = Field(
      min_length=2,
      max_length=64,
      pattern=r"^[a-z0-9][a-z0-9\-]+$",
      description="Service ID to perform a rolling restart on.",
  )
  environment: Environment = Field(description="Target environment.")
  reason: str = Field(
      min_length=10, max_length=500, description="Why the restart is needed."
  )


class RestartServiceOutput(StrictModel):
  status: Literal["success"] = "success"
  service_name: str
  environment: Environment
  restart_id: str
  approved_by_human: bool


# --------------------------------------------------------------------------
# grant_temporary_access (HIGH STAKES, HITL)
# --------------------------------------------------------------------------
class GrantAccessInput(StrictModel):
  user_email: str = Field(
      max_length=254,
      pattern=r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$",
      description="Corporate email of the requester.",
  )
  role: Literal["viewer", "operator", "admin"] = Field(
      description="Least-privilege role to grant."
  )
  duration_hours: int = Field(ge=1, le=24, description="Access lifetime in hours.")
  justification: str = Field(
      min_length=10, max_length=500, description="Business justification."
  )


class GrantAccessOutput(StrictModel):
  status: Literal["success"] = "success"
  grant_id: str
  role: str
  expires_in_hours: int
  approved_by_human: bool


# --------------------------------------------------------------------------
# Reviewer self-evaluation (agent output_schema)
# --------------------------------------------------------------------------
class ReviewVerdict(StrictModel):
  verdict: Literal["approved", "needs_revision", "blocked"] = Field(
      description="Overall quality and safety verdict."
  )
  policy_violations: list[str] = Field(
      default_factory=list, description="Any policy rules that were broken."
  )
  pii_detected: bool = Field(description="True if the answer leaks PII.")
  grounded_in_tool_results: bool = Field(
      description="True if every factual claim is backed by a tool result."
  )
  final_answer: str = Field(description="The user-facing answer to return.")


ALL_TOOL_SCHEMAS: dict[str, tuple[type[BaseModel], type[BaseModel]]] = {
    "classify_ops_request": (ClassifyOpsRequestInput, ClassifyOpsRequestOutput),
    "get_service_health_status": (ServiceHealthInput, ServiceHealthOutput),
    "search_runbook_knowledge_base": (RunbookSearchInput, RunbookSearchOutput),
    "create_incident_ticket": (CreateIncidentTicketInput, CreateIncidentTicketOutput),
    "restart_production_service": (RestartServiceInput, RestartServiceOutput),
    "grant_temporary_access": (GrantAccessInput, GrantAccessOutput),
}
