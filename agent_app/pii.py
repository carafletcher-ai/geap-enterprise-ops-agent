"""PII redaction: Google Cloud DLP first, deterministic regex fallback.

Used by (1) the logging pipeline (``RedactionFilter``), (2) the memory
pipeline before sessions are consolidated into long-term memory, and (3) the
before-model guardrail before user text is sent to the LLM.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .config import SETTINGS

logger = logging.getLogger(__name__)

# Order matters: more specific patterns first.
_REGEX_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL_ADDRESS", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("US_SOCIAL_SECURITY_NUMBER", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("CREDIT_CARD_NUMBER", re.compile(r"\b(?:\d[ \-]?){13,16}\b")),
    ("API_KEY", re.compile(r"\b(?:AIza[0-9A-Za-z\-_]{35}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{36})\b")),
    ("PHONE_NUMBER", re.compile(r"(?<!\w)(?:\+?1[ .\-]?)?\(?\d{3}\)?[ .\-]?\d{3}[ .\-]?\d{4}\b")),
    ("IP_ADDRESS", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
]

DLP_INFO_TYPES = [
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "US_SOCIAL_SECURITY_NUMBER",
    "CREDIT_CARD_NUMBER",
    "IP_ADDRESS",
    "PERSON_NAME",
]


def _luhn_ok(digits: str) -> bool:
  nums = [int(c) for c in digits if c.isdigit()]
  if not 13 <= len(nums) <= 16:
    return False
  total = 0
  for i, n in enumerate(reversed(nums)):
    if i % 2 == 1:
      n *= 2
      if n > 9:
        n -= 9
    total += n
  return total % 10 == 0


def redact_with_regex(text: str) -> str:
  """Deterministic, offline PII scrubber (always applied as defense in depth)."""
  if not text:
    return text
  for info_type, pattern in _REGEX_RULES:
    if info_type == "CREDIT_CARD_NUMBER":
      text = pattern.sub(
          lambda m: f"[{'CREDIT_CARD_NUMBER'}]" if _luhn_ok(m.group()) else m.group(),
          text,
      )
    else:
      text = pattern.sub(f"[{info_type}]", text)
  return text


def redact_with_cloud_dlp(text: str, project_id: str | None = None) -> str:
  """De-identifies text with the Cloud DLP API (replace-with-info-type).

  Raises on any failure so the caller can fall back to regex.
  """
  from google.cloud import dlp_v2  # pylint: disable=import-outside-toplevel

  project = project_id or SETTINGS.project_id
  if not project:
    raise RuntimeError("GOOGLE_CLOUD_PROJECT is required for Cloud DLP.")
  client = dlp_v2.DlpServiceClient()
  response = client.deidentify_content(
      request={
          "parent": f"projects/{project}/locations/global",
          "inspect_config": {
              "info_types": [{"name": t} for t in DLP_INFO_TYPES],
              "min_likelihood": dlp_v2.Likelihood.POSSIBLE,
          },
          "deidentify_config": {
              "info_type_transformations": {
                  "transformations": [
                      {"primitive_transformation": {"replace_with_info_type_config": {}}}
                  ]
              }
          },
          "item": {"value": text},
      }
  )
  return response.item.value


def redact_text(text: str, use_dlp: bool | None = None) -> str:
  """Redacts PII from text. Cloud DLP when enabled, regex fallback always.

  Args:
    text: Text that may contain PII.
    use_dlp: Override for ``ENABLE_CLOUD_DLP``.

  Returns:
    Text with PII replaced by ``[INFO_TYPE]`` tokens.
  """
  if not isinstance(text, str) or not text:
    return text
  use_dlp = SETTINGS.enable_cloud_dlp if use_dlp is None else use_dlp
  if use_dlp:
    try:
      text = redact_with_cloud_dlp(text)
    except Exception as exc:  # pylint: disable=broad-except
      # Never log the text itself here: it is not yet redacted.
      logging.getLogger("pii.fallback").debug(
          "dlp_unavailable_falling_back_to_regex: %s", type(exc).__name__
      )
  return redact_with_regex(text)


def redact_obj(obj: Any, use_dlp: bool | None = None) -> Any:
  """Recursively redacts strings inside dicts/lists/tuples."""
  if isinstance(obj, str):
    return redact_text(obj, use_dlp=use_dlp)
  if isinstance(obj, dict):
    return {k: redact_obj(v, use_dlp) for k, v in obj.items()}
  if isinstance(obj, (list, tuple)):
    return type(obj)(redact_obj(v, use_dlp) for v in obj)
  return obj


def contains_pii(text: str) -> bool:
  """True if the regex scrubber would change the text."""
  return bool(text) and redact_with_regex(text) != text


class RedactionFilter(logging.Filter):
  """Logging filter that scrubs PII from messages and structured extras.

  Uses the fast in-process regex scrubber on the hot logging path (Cloud DLP
  is used on the memory/storage pipeline, see ``memory.redact_session``).
  """

  _STD_ATTRS = frozenset(
      logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
  ) | {"message", "asctime"}

  def filter(self, record: logging.LogRecord) -> bool:
    if isinstance(record.msg, str):
      record.msg = redact_with_regex(record.msg)
    if record.args:
      record.args = redact_obj(record.args, use_dlp=False)
    for key, value in list(record.__dict__.items()):
      if key not in self._STD_ATTRS:
        setattr(record, key, redact_obj(value, use_dlp=False))
    return True
