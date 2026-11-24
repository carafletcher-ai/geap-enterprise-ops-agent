"""Context & memory management.

* Persistent session state: ``VertexAiSessionService`` (Agent Engine) in the
  cloud, ``DatabaseSessionService`` (SQLite/Cloud SQL via SQLAlchemy) locally,
  so conversation history and ``state`` survive restarts.
* Long-term memory: ``VertexAiMemoryBankService`` (Agent Engine Memory Bank)
  in the cloud, ``InMemoryMemoryService`` locally.
* History compaction: ADK ``EventsCompactionConfig`` with an
  ``LlmEventSummarizer`` (Flash) summarizes old turns, plus a token-budget
  sliding window (``trim_history``) as a hard backstop and Gemini context
  caching for the static constitution.
* Async memory consolidation: ``consolidate_memory_async`` schedules the
  (expensive) memory generation as a background ``asyncio`` task after each
  turn, so the user never waits on it. The session is PII-redacted first.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any

from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps.app import EventsCompactionConfig
from google.adk.apps.llm_event_summarizer import LlmEventSummarizer
from google.adk.memory import BaseMemoryService, InMemoryMemoryService
from google.adk.models import Gemini, LlmRequest
from google.adk.sessions import BaseSessionService, InMemorySessionService

from .config import SETTINGS
from .observability import get_tracer
from .pii import redact_text

logger = logging.getLogger("geap.memory")


# --------------------------------------------------------------------------
# Persistent sessions & long-term memory
# --------------------------------------------------------------------------
def build_session_service(db_url: str | None = None) -> BaseSessionService:
  """Returns a persistent session service.

  Agent Engine sessions when ``AGENT_ENGINE_ID`` is set; otherwise a SQL
  database (``SESSION_DB_URL``, default local SQLite file).
  """
  if SETTINGS.agent_engine_id:
    from google.adk.sessions import VertexAiSessionService  # pylint: disable=import-outside-toplevel

    logger.info("session.backend", extra={"backend": "vertex_ai_agent_engine"})
    return VertexAiSessionService(
        project=SETTINGS.project_id,
        location=SETTINGS.location,
        agent_engine_id=SETTINGS.agent_engine_id,
    )
  url = db_url or SETTINGS.session_db_url
  try:
    from google.adk.sessions import DatabaseSessionService  # pylint: disable=import-outside-toplevel

    if url.startswith("sqlite") and ":///" in url:
      import pathlib  # pylint: disable=import-outside-toplevel

      path = url.split(":///", 1)[1]
      if path and path != ":memory:":
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    logger.info("session.backend", extra={"backend": "database", "dialect": url.split(":")[0]})
    return DatabaseSessionService(db_url=url)
  except ImportError:
    logger.warning("session.backend_fallback", extra={"backend": "in_memory"})
    return InMemorySessionService()


def build_memory_service() -> BaseMemoryService:
  """Returns Agent Engine Memory Bank in the cloud, in-memory locally."""
  if SETTINGS.agent_engine_id:
    from google.adk.memory import VertexAiMemoryBankService  # pylint: disable=import-outside-toplevel

    logger.info("memory.backend", extra={"backend": "vertex_ai_memory_bank"})
    return VertexAiMemoryBankService(
        project=SETTINGS.project_id,
        location=SETTINGS.location,
        agent_engine_id=SETTINGS.agent_engine_id,
    )
  return InMemoryMemoryService()


# --------------------------------------------------------------------------
# History compaction
# --------------------------------------------------------------------------
def build_compaction_config() -> EventsCompactionConfig:
  """ADK event compaction: summarize every N turns with Flash."""
  return EventsCompactionConfig(
      summarizer=LlmEventSummarizer(llm=Gemini(model=SETTINGS.flash_model)),
      compaction_interval=SETTINGS.compaction_interval,
      overlap_size=SETTINGS.compaction_overlap,
  )


def build_context_cache_config() -> ContextCacheConfig:
  """Gemini context caching for the large, static system instruction."""
  return ContextCacheConfig(cache_intervals=10, ttl_seconds=1800, min_tokens=2048)


def estimate_tokens(llm_request: LlmRequest) -> int:
  """Cheap token estimate (~4 chars/token) used for the sliding window."""
  chars = 0
  for content in llm_request.contents or []:
    for part in content.parts or []:
      chars += len(getattr(part, "text", None) or "")
  return chars // 4


def trim_history(callback_context: Any, llm_request: LlmRequest) -> None:
  """before_model_callback: token-budget sliding window over history.

  Keeps the first user turn (original goal) plus the most recent contents
  that fit within ``MAX_CONTEXT_TOKENS`` / ``MAX_HISTORY_CONTENTS``.
  """
  contents = llm_request.contents or []
  if not contents:
    return None
  over_count = len(contents) > SETTINGS.max_history_contents
  over_budget = estimate_tokens(llm_request) > SETTINGS.max_context_tokens
  if not (over_count or over_budget):
    return None
  head, tail = contents[:1], contents[1:]
  kept: list[Any] = []
  budget = SETTINGS.max_context_tokens
  for content in reversed(tail):
    size = sum(len(getattr(p, "text", None) or "") for p in (content.parts or [])) // 4
    if len(kept) + 1 >= SETTINGS.max_history_contents or size > budget:
      break
    kept.insert(0, content)
    budget -= size
  # Never start the window on a dangling function response.
  while kept and any(getattr(p, "function_response", None) for p in (kept[0].parts or [])):
    kept.pop(0)
  llm_request.contents = head + kept
  logger.info(
      "context.trimmed",
      extra={"before": len(contents), "after": len(llm_request.contents)},
  )
  return None


# --------------------------------------------------------------------------
# Async memory consolidation
# --------------------------------------------------------------------------
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def redact_session(session: Any) -> Any:
  """Deep-copies a session and scrubs PII from every text part (DLP+regex)."""
  clean = copy.deepcopy(session)
  for event in getattr(clean, "events", []) or []:
    content = getattr(event, "content", None)
    for part in (getattr(content, "parts", None) or []):
      if getattr(part, "text", None):
        part.text = redact_text(part.text)
  return clean


async def _consolidate(memory_service: BaseMemoryService, session: Any) -> None:
  with get_tracer().start_as_current_span("memory.consolidate") as span:
    span.set_attribute("session.id", str(getattr(session, "id", "")))
    try:
      await memory_service.add_session_to_memory(redact_session(session))
      logger.info("memory.consolidated", extra={"session_id": getattr(session, "id", None)})
    except Exception as exc:  # pylint: disable=broad-except
      span.record_exception(exc)
      logger.warning("memory.consolidation_failed", extra={"error": type(exc).__name__})


def schedule_memory_consolidation(memory_service: BaseMemoryService, session: Any) -> asyncio.Task | None:
  """Fire-and-forget: runs consolidation in the background event loop."""
  try:
    loop = asyncio.get_running_loop()
  except RuntimeError:
    return None
  task = loop.create_task(_consolidate(memory_service, session))
  _BACKGROUND_TASKS.add(task)
  task.add_done_callback(_BACKGROUND_TASKS.discard)
  return task


async def drain_background_tasks(timeout: float = 30.0) -> None:
  """Awaits outstanding memory tasks (call on graceful shutdown/tests)."""
  if _BACKGROUND_TASKS:
    await asyncio.wait(list(_BACKGROUND_TASKS), timeout=timeout)


def consolidate_memory_async(callback_context: Any) -> None:
  """after_agent_callback (root): schedule non-blocking memory consolidation."""
  inv = getattr(callback_context, "_invocation_context", None)
  memory_service = getattr(inv, "memory_service", None)
  session = getattr(inv, "session", None)
  if memory_service is None or session is None:
    return None
  schedule_memory_consolidation(memory_service, session)
  logger.info("memory.consolidation_scheduled", extra={"session_id": getattr(session, "id", None)})
  return None
