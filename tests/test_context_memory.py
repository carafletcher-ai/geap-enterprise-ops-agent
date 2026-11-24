"""Context & Memory: constitution, compaction, persistence, async memory."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from google.adk.apps.llm_event_summarizer import LlmEventSummarizer
from google.adk.events import Event
from google.adk.memory import BaseMemoryService, InMemoryMemoryService
from google.adk.models import LlmRequest
from google.adk.sessions import DatabaseSessionService
from google.genai import types

from agent_app import memory, prompts
from agent_app.config import SETTINGS


def test_constitution_covers_persona_domain_constraints():
  text = prompts.GLOBAL_CONSTITUTION
  for section in ("## Mission", "## Domain knowledge", "## Non-negotiable constraints"):
    assert section in text
  assert "human approval" in text and "PII" in text and "recovery_instructions" in text


def test_sub_agent_prompts_inject_session_state():
  assert "{intake?}" in prompts.PLANNER_INSTRUCTION
  assert "{plan?}" in prompts.EXECUTOR_INSTRUCTION
  assert "{execution?}" in prompts.REVIEWER_INSTRUCTION


def test_compaction_config_uses_flash_summarizer():
  cfg = memory.build_compaction_config()
  assert isinstance(cfg.summarizer, LlmEventSummarizer)
  assert cfg.compaction_interval == SETTINGS.compaction_interval
  assert cfg.overlap_size == SETTINGS.compaction_overlap


def test_trim_history_sliding_window(monkeypatch):
  contents = [types.Content(role="user", parts=[types.Part(text=f"msg {i} " + "x" * 400)]) for i in range(80)]
  req = LlmRequest(model="m", contents=list(contents))
  memory.trim_history(SimpleNamespace(state={}), req)
  assert len(req.contents) <= SETTINGS.max_history_contents
  assert req.contents[0] is contents[0]          # original goal kept
  assert req.contents[-1] is contents[-1]        # most recent kept


def test_trim_history_noop_when_small():
  contents = [types.Content(role="user", parts=[types.Part(text="hello")])]
  req = LlmRequest(model="m", contents=list(contents))
  memory.trim_history(SimpleNamespace(state={}), req)
  assert req.contents == contents


@pytest.mark.asyncio
async def test_database_session_service_persists_state(tmp_path):
  url = f"sqlite+aiosqlite:///{tmp_path}/sessions.db"
  svc = memory.build_session_service(url)
  assert isinstance(svc, DatabaseSessionService)
  await svc.create_session(app_name="a", user_id="u", session_id="s", state={"user:pref": "pager"})
  fresh = memory.build_session_service(url)
  sess = await fresh.get_session(app_name="a", user_id="u", session_id="s")
  assert sess.state["user:pref"] == "pager"


def test_redact_session_scrubs_pii_before_memory():
  session = SimpleNamespace(id="s1", events=[
      Event(author="user", content=types.Content(role="user", parts=[types.Part(text="My SSN is 123-45-6789")]))
  ])
  clean = memory.redact_session(session)
  assert "123-45-6789" not in clean.events[0].content.parts[0].text
  assert "123-45-6789" in session.events[0].content.parts[0].text  # original untouched


class SlowMemory(BaseMemoryService):
  def __init__(self):
    self.stored = []

  async def add_session_to_memory(self, session):
    await asyncio.sleep(0.2)
    self.stored.append(session)

  async def search_memory(self, *, app_name, user_id, query):
    return None


@pytest.mark.asyncio
async def test_memory_consolidation_is_non_blocking():
  svc = SlowMemory()
  session = SimpleNamespace(id="s1", events=[])
  ctx = SimpleNamespace(_invocation_context=SimpleNamespace(memory_service=svc, session=session))
  loop = asyncio.get_running_loop()
  start = loop.time()
  memory.consolidate_memory_async(ctx)
  assert loop.time() - start < 0.05   # returned immediately
  assert svc.stored == []             # still running in background
  await memory.drain_background_tasks()
  assert len(svc.stored) == 1


def test_local_memory_backend_is_in_memory():
  assert isinstance(memory.build_memory_service(), InMemoryMemoryService)
