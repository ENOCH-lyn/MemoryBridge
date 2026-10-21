"""Optional LLM extractor using an OpenAI-compatible chat completions API.

The product works without an API key (rule-based extractor).  When
``MEMBRIDGE_LLM_BASE_URL`` / ``MEMBRIDGE_LLM_API_KEY`` / ``MEMBRIDGE_LLM_MODEL``
are configured, ``memory_ingest`` can use this extractor instead.  Any failure is
reported and the caller falls back to the deterministic rules.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .extract import content_hash

MEMORY_TYPES = {"fact", "preference", "decision", "lesson", "procedure", "reference"}

SYSTEM_PROMPT = """You extract durable memories from a conversation or document.
Return ONLY a JSON array. Each item:
{"type": "fact|preference|decision|lesson|procedure|reference",
 "content": "one self-contained sentence in the user's language",
 "evidence_quote": "the exact short quote from the input that supports it",
 "confidence": 0.0-1.0}
Rules:
- Only durable, reusable information (not small talk, not tool logs).
- Never invent facts; if the input does not support it, omit it.
- Prefer the user's own wording for evidence_quote.
- At most 12 items.
"""


@dataclass(frozen=True)
class LLMConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout: float = 45.0

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(
            base_url=os.environ.get("MEMBRIDGE_LLM_BASE_URL", "").strip(),
            api_key=os.environ.get("MEMBRIDGE_LLM_API_KEY", "").strip(),
            model=os.environ.get("MEMBRIDGE_LLM_MODEL", "").strip(),
            timeout=float(os.environ.get("MEMBRIDGE_LLM_TIMEOUT", "45") or 45),
        )

    def public(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "model": self.model, "base_url": self.base_url}


def _strip_code_fence(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_items(raw: str) -> list[dict[str, Any]]:
    text = _strip_code_fence(raw)
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("LLM response did not contain a JSON array")
    payload = json.loads(text[start : end + 1])
    if isinstance(payload, dict):
        payload = payload.get("memories", [])
    if not isinstance(payload, list):
        raise ValueError("LLM response was not a list")
    items: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content", "")).strip()
        evidence = str(item.get("evidence_quote", "")).strip() or content
        if len(content) < 4:
            continue
        item_type = str(item.get("type", "fact")).strip().lower()
        if item_type not in MEMORY_TYPES:
            item_type = "fact"
        try:
            confidence = float(item.get("confidence", 0.75))
        except (TypeError, ValueError):
            confidence = 0.75
        confidence = round(max(0.1, min(1.0, confidence)), 2)
        items.append(
            {
                "type": item_type,
                "content": content[:600],
                "evidence_quote": evidence[:600],
                "confidence": confidence,
                "content_hash": content_hash(content),
                "status": "candidate",
            }
        )
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if item["content_hash"] in seen:
            continue
        seen.add(item["content_hash"])
        deduped.append(item)
        if len(deduped) >= 12:
            break
    return deduped


def extract_with_llm(text: str, config: LLMConfig) -> list[dict[str, Any]]:
    if not config.enabled:
        raise RuntimeError("LLM is not configured")
    body = {
        "model": config.model,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": (text or "")[:16000]},
        ],
    }
    request = urllib.request.Request(
        config.base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {config.api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"LLM HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LLM unreachable: {exc.reason}") from exc
    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError("LLM returned no choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("LLM returned empty content")
    return _parse_items(content)
