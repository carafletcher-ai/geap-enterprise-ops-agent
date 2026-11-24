"""Observability: structured JSON logging, OpenTelemetry tracing, and
intent-vs-outcome capture for every tool call and agent step.

* ``configure_logging`` installs a JSON formatter (Cloud Logging compatible:
  ``severity``, ``message``, ``logging.googleapis.com/trace``) plus the PII
  ``RedactionFilter`` on the root handler.
* ``configure_tracing`` installs an OpenTelemetry ``TracerProvider``. When
  ``ENABLE_CLOUD_TRACE=true`` spans are exported to Cloud Trace; ADK's built-in
  spans (invocation -> agent_run -> call_llm -> execute_tool) join the same trace.
* ``log_tool_intent`` / ``log_tool_outcome`` are ADK before/after tool
  callbacks that record what the agent *intends* to do and what *actually*
  happened, correlated by ``function_call_id`` and trace ID.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import sys
import time
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .config import SETTINGS
from .pii import RedactionFilter, redact_obj

TRACER_NAME = "geap.enterprise_ops_agent"
_STD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
) | {"message", "asctime"}

_SEVERITY = {
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "WARNING": "WARNING",
    "ERROR": "ERROR",
    "CRITICAL": "CRITICAL",
}


class JsonFormatter(logging.Formatter):
  """Formats log records as single-line JSON for Cloud Logging."""

  def format(self, record: logging.LogRecord) -> str:
    payload: dict[str, Any] = {
        "timestamp": _dt.datetime.fromtimestamp(
            record.created, tz=_dt.timezone.utc
        ).isoformat(),
        "severity": _SEVERITY.get(record.levelname, record.levelname),
        "message": record.getMessage(),
        "logger": record.name,
        "module": record.module,
        "function": record.funcName,
        "line": record.lineno,
    }
    span_ctx = trace.get_current_span().get_span_context()
    if span_ctx and span_ctx.is_valid:
      trace_id = format(span_ctx.trace_id, "032x")
      payload["trace_id"] = trace_id
      payload["span_id"] = format(span_ctx.span_id, "016x")
      if SETTINGS.project_id:
        payload["logging.googleapis.com/trace"] = (
            f"projects/{SETTINGS.project_id}/traces/{trace_id}"
        )
    for key, value in record.__dict__.items():
      if key not in _STD_ATTRS and not key.startswith("_"):
        payload[key] = value
    if record.exc_info:
      payload["exception"] = self.formatException(record.exc_info)
    return json.dumps(payload, default=str)


_CONFIGURED = {"logging": False, "tracing": False}


def configure_logging(level: str | None = None) -> None:
  """Installs JSON logging with PII redaction on the root logger (idempotent)."""
  if _CONFIGURED["logging"]:
    return
  handler = logging.StreamHandler(sys.stdout)
  handler.setFormatter(JsonFormatter())
  handler.addFilter(RedactionFilter())
  root = logging.getLogger()
  root.handlers = [handler]
  root.setLevel(level or SETTINGS.log_level)
  _CONFIGURED["logging"] = True


def configure_tracing() -> trace.Tracer:
  """Installs an OpenTelemetry TracerProvider (Cloud Trace when enabled)."""
  if not _CONFIGURED["tracing"]:
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": SETTINGS.app_name, "cloud.region": SETTINGS.location}
        )
    )
    if SETTINGS.enable_cloud_trace:
      try:
        from opentelemetry.exporter.cloud_trace import (  # pylint: disable=import-outside-toplevel
            CloudTraceSpanExporter,
        )

        provider.add_span_processor(
            BatchSpanProcessor(CloudTraceSpanExporter(project_id=SETTINGS.project_id))
        )
      except Exception as exc:  # pylint: disable=broad-except
        logging.getLogger(__name__).warning(
            "tracing.cloud_trace_unavailable", extra={"error": type(exc).__name__}
        )
    try:
      trace.set_tracer_provider(provider)
    except Exception:  # pylint: disable=broad-except
      pass  # A provider was already installed (e.g. by Agent Engine).
    _CONFIGURED["tracing"] = True
  return trace.get_tracer(TRACER_NAME)


def get_tracer() -> trace.Tracer:
  return trace.get_tracer(TRACER_NAME)


logger = logging.getLogger("geap.audit")

# Open spans keyed by function_call_id so before/after callbacks link up.
_OPEN_TOOL_SPANS: dict[str, tuple[Any, float]] = {}


def _call_id(tool_context: Any) -> str:
  return str(getattr(tool_context, "function_call_id", None) or id(tool_context))


def _session_fields(ctx: Any) -> dict[str, Any]:
  fields: dict[str, Any] = {}
  for attr in ("invocation_id", "agent_name", "user_id"):
    try:
      fields[attr] = getattr(ctx, attr)
    except Exception:  # pylint: disable=broad-except
      pass
  return fields


def log_tool_intent(tool: Any, args: dict[str, Any], tool_context: Any) -> None:
  """ADK before_tool_callback: logs the INTENDED action and opens a span."""
  call_id = _call_id(tool_context)
  span = get_tracer().start_span(
      f"tool.{tool.name}",
      attributes={
          "tool.name": tool.name,
          "tool.call_id": call_id,
          "tool.args": json.dumps(redact_obj(args, use_dlp=False), default=str),
      },
  )
  _OPEN_TOOL_SPANS[call_id] = (span, time.perf_counter())
  logger.info(
      "tool.intent",
      extra={
          "event_type": "intent",
          "tool": tool.name,
          "call_id": call_id,
          "intended_args": args,
          **_session_fields(tool_context),
      },
  )
  return None


def log_tool_outcome(
    tool: Any, args: dict[str, Any], tool_context: Any, tool_response: Any
) -> None:
  """ADK after_tool_callback: logs the ACTUAL outcome and closes the span."""
  call_id = _call_id(tool_context)
  span, started = _OPEN_TOOL_SPANS.pop(call_id, (None, time.perf_counter()))
  status = (
      tool_response.get("status", "success")
      if isinstance(tool_response, dict)
      else "success"
  )
  latency_ms = round((time.perf_counter() - started) * 1000, 2)
  if span is not None:
    span.set_attribute("tool.status", status)
    span.set_attribute("tool.latency_ms", latency_ms)
    if status == "error":
      span.set_status(trace.Status(trace.StatusCode.ERROR))
    span.end()
  logger.info(
      "tool.outcome",
      extra={
          "event_type": "outcome",
          "tool": tool.name,
          "call_id": call_id,
          "outcome_status": status,
          "latency_ms": latency_ms,
          "result": tool_response,
          **_session_fields(tool_context),
      },
  )
  return None


def log_agent_start(callback_context: Any) -> None:
  """ADK before_agent_callback: records which sub-agent is about to act."""
  logger.info(
      "agent.intent",
      extra={"event_type": "intent", **_session_fields(callback_context)},
  )
  return None


def log_agent_end(callback_context: Any) -> None:
  """ADK after_agent_callback: records the sub-agent finished."""
  logger.info(
      "agent.outcome",
      extra={"event_type": "outcome", **_session_fields(callback_context)},
  )
  return None
