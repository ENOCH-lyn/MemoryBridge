"""Lightweight, dependency-free candidate extraction.

This is the MVP "lite extractor": deterministic rules over conversation text.
It is intentionally conservative (only lines that match a rule become
candidates) and is designed to be replaced by an LLM extractor behind the same
`extract_candidates()` interface later.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

MAX_CANDIDATES = 8
MAX_CONTENT = 600

_RULES: list[tuple[str, list[str]]] = [
    (
        "lesson",
        [
            r"踩坑",
            r"教训",
            r"失败",
            r"报错",
            r"原来是",
            r"注意",
            r"不要再",
            r"避免",
            r"根因",
            r"\bbug\b",
            r"\bfailed\b",
            r"root cause",
            r"\bfix(ed)?\b",
        ],
    ),
    (
        "decision",
        [
            r"决定",
            r"选择",
            r"采用",
            r"改成",
            r"而不是",
            r"方案",
            r"架构",
            r"decided",
            r"chose",
            r"instead of",
        ],
    ),
    (
        "preference",
        [
            r"喜欢",
            r"偏好",
            r"习惯",
            r"以后都",
            r"请使用",
            r"不要用",
            r"记得",
            r"记住",
            r"prefer",
            r"always",
            r"never",
        ],
    ),
    (
        "procedure",
        [
            r"步骤",
            r"流程",
            r"发布前",
            r"运行",
            r"执行",
            r"检查清单",
            r"checklist",
            r"\bsteps?\b",
            r"\bbefore\b",
            r"\brun\b",
        ],
    ),
    (
        "fact",
        [
            r"使用",
            r"位于",
            r"版本",
            r"技术栈",
            r"依赖",
            r"端口",
            r"路径",
            r"\buses?\b",
            r"\bstack\b",
        ],
    ),
]

_BULLET_RE = re.compile(r"^[\s\-*•\d.、)）]+")
_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])\s*|\n+")


def normalize(text: str) -> str:
    return re.sub(r"[\W_]+", "", (text or "").lower(), flags=re.UNICODE)


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()[:32]


def _clean(unit: str) -> str:
    value = _BULLET_RE.sub("", unit.strip())
    value = value.strip(" \t\"'“”‘’")
    return value[:MAX_CONTENT].strip()


def _classify(unit: str) -> tuple[str, int, list[str]]:
    best_type = "fact"
    best_score = 0
    best_hits: list[str] = []
    for item_type, patterns in _RULES:
        hits = [pattern for pattern in patterns if re.search(pattern, unit, flags=re.IGNORECASE)]
        score = len(hits)
        if score > best_score:
            best_type, best_score, best_hits = item_type, score, hits
    return best_type, best_score, best_hits


def extract_candidates(text: str, *, max_candidates: int = MAX_CANDIDATES) -> list[dict[str, Any]]:
    """Return candidate memories from a raw conversation/document chunk."""
    units = [_clean(unit) for unit in _SPLIT_RE.split(text or "")]
    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for unit in units:
        if len(unit) < 6:
            continue
        item_type, score, hits = _classify(unit)
        if score == 0:
            continue
        normalized = normalize(unit)
        if normalized in seen:
            continue
        seen.add(normalized)
        confidence = 0.62 + 0.06 * min(score, 3) + (0.05 if len(unit) >= 20 else 0.0)
        candidates.append(
            {
                "type": item_type,
                "content": unit,
                "evidence_quote": unit,
                "confidence": round(min(0.92, confidence), 2),
                "content_hash": content_hash(unit),
                "rule_hits": hits,
                "status": "candidate",
            }
        )
        if len(candidates) >= max_candidates:
            break
    return candidates
