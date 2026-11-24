"""Observability & Tracing: JSON logs, intent/outcome, OTel spans, PII."""

from __future__ import annotations

import io
import json
import logging

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent_app import observability, pii
from tests.conftest import FakeTool, FakeToolContext


@pytest.fixture
def json_log():
  stream = io.StringIO()
  handler = logging.StreamHandler(stream)
  handler.setFormatter(observability.JsonFormatter())
  handler.addFilter(pii.RedactionFilter())
  log = logging.getLogger("geap.audit")
  log.addHandler(handler)
  log.setLevel(logging.INFO)
  yield stream
  log.removeHandler(handler)


@pytest.fixture(scope="module")
def spans():
  exporter = InMemorySpanExporter()
  provider = TracerProvider()
  provider.add_span_processor(SimpleSpanProcessor(exporter))
  trace._TRACER_PROVIDER = None  # pylint: disable=protected-access
  trace._TRACER_PROVIDER_SET_ONCE = trace.Once()  # pylint: disable=protected-access
  trace.set_tracer_provider(provider)
  return exporter


def _records(stream):
  return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_json_formatter_emits_structured_fields(json_log):
  logging.getLogger("geap.audit").info("hello", extra={"tool": "x", "latency_ms": 3})
  rec = _records(json_log)[-1]
  assert rec["message"] == "hello" and rec["severity"] == "INFO"
  assert rec["tool"] == "x" and rec["latency_ms"] == 3 and "timestamp" in rec


def test_intent_and_outcome_are_logged_and_traced(json_log, spans):
  spans.clear()
  tool, ctx = FakeTool("get_service_health_status"), FakeToolContext()
  args = {"request": {"service_name": "payments-api"}}
  observability.log_tool_intent(tool, args, ctx)
  observability.log_tool_outcome(tool, args, ctx, {"status": "success", "health": "degraded"})
  recs = _records(json_log)
  intent = next(r for r in recs if r["message"] == "tool.intent")
  outcome = next(r for r in recs if r["message"] == "tool.outcome")
  assert intent["event_type"] == "intent" and intent["intended_args"] == args
  assert outcome["event_type"] == "outcome" and outcome["outcome_status"] == "success"
  assert intent["call_id"] == outcome["call_id"]
  finished = [s for s in spans.get_finished_spans() if s.name == "tool.get_service_health_status"]
  assert finished and finished[0].attributes["tool.status"] == "success"


def test_error_outcome_marks_span_error(spans):
  spans.clear()
  tool, ctx = FakeTool("restart_production_service"), FakeToolContext()
  observability.log_tool_intent(tool, {}, ctx)
  observability.log_tool_outcome(tool, {}, ctx, {"status": "error", "error_code": "X"})
  span = [s for s in spans.get_finished_spans() if s.name == "tool.restart_production_service"][0]
  assert span.status.status_code == trace.StatusCode.ERROR


def test_logs_include_trace_id_inside_span(json_log, spans):
  with trace.get_tracer("t").start_as_current_span("ops_request"):
    logging.getLogger("geap.audit").info("inside")
  rec = _records(json_log)[-1]
  assert len(rec["trace_id"]) == 32 and len(rec["span_id"]) == 16


def test_logging_pipeline_redacts_pii(json_log):
  logging.getLogger("geap.audit").info(
      "user bob@example.com", extra={"payload": {"phone": "415-555-0123", "ssn": "123-45-6789"}})
  raw = json_log.getvalue()
  for secret in ("bob@example.com", "415-555-0123", "123-45-6789"):
    assert secret not in raw
  assert "[EMAIL_ADDRESS]" in raw and "[US_SOCIAL_SECURITY_NUMBER]" in raw


@pytest.mark.parametrize("text,token", [
    ("mail jane.doe@corp.com now", "[EMAIL_ADDRESS]"),
    ("ssn 123-45-6789", "[US_SOCIAL_SECURITY_NUMBER]"),
    ("card 4111 1111 1111 1111", "[CREDIT_CARD_NUMBER]"),
    ("call (415) 555-0123", "[PHONE_NUMBER]"),
    ("host 10.1.2.3 down", "[IP_ADDRESS]"),
    ("key " + "AIza" + "B" * 35, "[API_KEY]"),
])
def test_regex_redaction(text, token):
  assert token in pii.redact_with_regex(text)


def test_non_luhn_numbers_not_flagged_as_cards():
  assert "[CREDIT_CARD_NUMBER]" not in pii.redact_with_regex("order 1234567812345678")


def test_dlp_failure_falls_back_to_regex(monkeypatch):
  def boom(_text, project_id=None):
    raise RuntimeError("DLP down")

  monkeypatch.setattr(pii, "redact_with_cloud_dlp", boom)
  assert pii.redact_text("x@y.com", use_dlp=True) == "[EMAIL_ADDRESS]"


def test_dlp_used_when_enabled(monkeypatch):
  monkeypatch.setattr(pii, "redact_with_cloud_dlp", lambda t, project_id=None: t.replace("Alice", "[PERSON_NAME]"))
  assert pii.redact_text("Alice at a@b.com", use_dlp=True) == "[PERSON_NAME] at [EMAIL_ADDRESS]"
