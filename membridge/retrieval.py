"""Hybrid retrieval and memory lifecycle helpers for MemoryBridge.

Design goals
------------
* **Dependency-free by default.**  The MVP must keep running on a stock
  Python 3.11+ interpreter with no wheels, so the default embedder is a
  deterministic feature-hashing vectoriser (words + CJK bigrams + char
  trigrams).  That is a real vector space (TF-IDF-ish cosine), not a
  semantic model, but it is cheap, stable and offline.
* **Optional real semantics.**  ``MEMBRIDGE_EMBED_PROVIDER=http`` points at any
  OpenAI-compatible ``/embeddings`` endpoint, ``=model2vec`` uses a local
  static-embedding package, ``=hash`` forces the built-in one.  The storage
  layer records the model name per vector so switching models is a re-index,
  not a migration.
* **Hybrid ranking.**  BM25 (SQLite FTS5) + cosine similarity are fused with
  Reciprocal Rank Fusion, then modulated by a salience score (recency, type,
  confidence, importance, usage).  This mirrors what mem0 / Graphiti do at a
  much smaller scale.
* **Forgetting instead of deleting.**  ``decay_score`` decides when a memory
  is archived (``status='archived'``); nothing is ever hard-deleted by the
  automatic pass, and pinned / decision / lesson / preference memories are
  never auto-archived.
"""

from __future__ import annotations

import array
import hashlib
import json
import math
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

# ---------------------------------------------------------------------------
# text processing
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-zA-Z0-9_./+#@:-]+")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")

#: Words too common to carry meaning in this corpus.  Kept tiny on purpose:
#: BM25 already discounts frequent terms via IDF.
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "was", "were",
    "are", "is", "be", "of", "to", "in", "on", "at", "it", "as", "by", "or",
    "a", "an", "we", "you", "我", "你", "的", "了", "是", "在", "和", "与",
}


#: Chinese interrogatives and filler that carry no retrieval signal.  They are
#: stripped before bigram generation, otherwise "为什么" alone can outrank the
#: actual subject of the question.
_CJK_STOP_PHRASES = (
    "为什么", "为什么", "怎么", "怎样", "如何", "什么", "哪些", "哪个", "哪里",
    "是否", "可以", "需要", "一下", "请问", "帮我", "麻烦", "这个", "那个",
)

#: Small bilingual alias table.  Real embeddings solve this properly; until one
#: is configured, a curated map keeps the common bilingual case working
#: (our memories are mostly Chinese text with English identifiers).
_ALIASES = {
    "数据库": ("database", "db"),
    "锁": ("lock", "locked"),
    "代理": ("proxy",),
    "网关": ("gateway",),
    "隧道": ("tunnel",),
    "记忆": ("memory",),
    "检索": ("search", "retrieval"),
    "向量": ("vector", "embedding"),
    "部署": ("deploy", "deployment"),
    "配置": ("config", "configuration"),
    "插件": ("plugin",),
    "重启": ("restart",),
    "日志": ("log",),
    "证书": ("certificate",),
    "队列": ("queue",),
    "告警": ("alert",),
    "监控": ("monitor",),
    "备份": ("backup",),
    "缓存": ("cache",),
    "端口": ("port",),
    "账号": ("account",),
    "密码": ("password",),
    "权限": ("permission",),
    "审计": ("audit",),
    "比赛": ("competition",),
    "看板": ("dashboard",),
}


def _strip_cjk_stopwords(text: str) -> str:
    for phrase in _CJK_STOP_PHRASES:
        text = text.replace(phrase, " ")
    return text


def alias_terms(query: str) -> list[str]:
    """English aliases triggered by Chinese keywords in the query."""
    lower = (query or "").lower()
    out: list[str] = []
    for key, values in _ALIASES.items():
        if key in lower:
            out.extend(values)
    return out


def index_terms(text: str) -> list[str]:
    """Return searchable terms: latin words and CJK bigrams.

    Single CJK characters are only kept when they stand alone (rare), because
    characters like 机/器/人 are too common to discriminate.
    """
    lower = _strip_cjk_stopwords((text or "").lower())
    terms: list[str] = []
    for word in _WORD_RE.findall(lower):
        if word in _STOPWORDS:
            continue
        if len(word) >= 2 or word.isdigit():
            terms.append(word)
    for run in _CJK_RE.findall(lower):
        if len(run) == 1:
            terms.append(run)
            continue
        terms.extend(run[i : i + 2] for i in range(len(run) - 1))
    # Preserve order, drop duplicates.
    return list(dict.fromkeys(terms))



def fts_text(text: str) -> str:
    """Space-joined terms for a ``unicode61`` FTS5 column."""
    return " ".join(index_terms(text))


def _match_expr(terms: Sequence[str]) -> str:
    escaped = ['"' + t.replace('"', '""') + '"' for t in terms[:40]]
    return " OR ".join(escaped)


def fts_match_query(query: str, *, expand: bool = False) -> str:
    """Build a safe FTS5 MATCH expression (OR of quoted terms).

    Alias terms are opt-in: they rescue cross-language queries but must not
    outrank the literal terms, so callers try the literal expression first.
    """
    terms = index_terms(query)
    if expand:
        for alias in alias_terms(query):
            if alias not in terms:
                terms.append(alias)
    return _match_expr(terms)



# ---------------------------------------------------------------------------
# vectors
# ---------------------------------------------------------------------------


def pack_vector(values: Sequence[float]) -> bytes:
    return array.array("f", values).tobytes()


def unpack_vector(blob: bytes) -> array.array:
    vec = array.array("f")
    vec.frombytes(blob)
    return vec


def _l2_normalize(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values))
    if norm <= 1e-12:
        return values
    return [v / norm for v in values]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)


def _stable_hash(token: str) -> int:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


class HashEmbedder:
    """Deterministic feature-hashing vectoriser (TF-IDF-ish, signed hashing)."""

    provider = "hash"

    def __init__(self, dim: int = 512) -> None:
        self.dim = int(dim)
        self.model = f"hash-{self.dim}-v1"

    def embed(self, text: str) -> list[float]:
        buckets = [0.0] * self.dim
        terms = index_terms(text)
        if not terms:
            return buckets
        counts: dict[str, int] = {}
        for term in terms:
            counts[term] = counts.get(term, 0) + 1
        if len(terms) == 1:
            grams = []
        else:
            compact = "".join(_CJK_RE.findall((text or "").lower()))
            grams = [compact[i : i + 3] for i in range(max(0, len(compact) - 2))][:400]
        for term, count in counts.items():
            weight = 1.0 + math.log(count)
            h = _stable_hash(term)
            bucket = h % self.dim
            sign = 1.0 if (h >> 63) & 1 else -1.0
            buckets[bucket] += sign * weight
        for gram in grams:
            h = _stable_hash("g:" + gram)
            buckets[h % self.dim] += 0.5
        return _l2_normalize(buckets)

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


class HttpEmbedder:
    """OpenAI-compatible ``POST {base_url}/embeddings`` client (stdlib only)."""

    provider = "http"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        *,
        dim: int | None = None,
        timeout: float = 20.0,
        batch_size: int = 32,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or ""
        self.dim = int(dim) if dim else 0
        self.timeout = timeout
        self.batch_size = max(1, batch_size)

    def _post(self, inputs: list[str]) -> list[list[float]]:
        payload = json.dumps({"model": self.model, "input": inputs}).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/embeddings",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "Mozilla/5.0 (MemoryBridge embedder)",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        data = body.get("data") or []
        if len(data) != len(inputs):
            raise RuntimeError(f"embeddings endpoint returned {len(data)} vectors for {len(inputs)} inputs")
        data = sorted(data, key=lambda item: item.get("index", 0))
        vectors = [_l2_normalize([float(x) for x in item["embedding"]]) for item in data]
        if vectors and not self.dim:
            self.dim = len(vectors[0])
        return vectors

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            out.extend(self._post(list(texts[start : start + self.batch_size])))
        return out

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]


class Model2VecEmbedder:
    """Optional static embeddings through the ``model2vec`` package."""

    provider = "model2vec"

    def __init__(
        self,
        model_name: str = "minishlab/potion-multilingual-128M",
        *,
        quantize_to: str | None = None,
    ) -> None:
        from model2vec import StaticModel  # type: ignore import-not-found

        kwargs: dict[str, Any] = {}
        if quantize_to:
            kwargs["quantize_to"] = quantize_to
        self._model = StaticModel.from_pretrained(model_name, **kwargs)
        self.model = f"model2vec:{model_name}"
        self.dim = int(self._model.dim)

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self._model.encode(list(texts))
        return [_l2_normalize([float(x) for x in v]) for v in vectors]

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]


_EMBEDDER: Any = None


def get_embedder() -> Any:
    """Return the configured embedder (cached per process).

    ``MEMBRIDGE_EMBED_PROVIDER``: ``auto`` (default) / ``hash`` / ``http`` /
    ``model2vec``.  ``auto`` prefers an HTTP endpoint when one is configured,
    then model2vec when importable, then the built-in hashing embedder.
    """
    global _EMBEDDER
    if _EMBEDDER is not None:
        return _EMBEDDER

    provider = (os.environ.get("MEMBRIDGE_EMBED_PROVIDER") or "auto").strip().lower()
    url = (os.environ.get("MEMBRIDGE_EMBED_URL") or "").strip()
    model = (os.environ.get("MEMBRIDGE_EMBED_MODEL") or "").strip()
    key = (os.environ.get("MEMBRIDGE_EMBED_KEY") or "").strip()
    dim_env = os.environ.get("MEMBRIDGE_EMBED_DIM")

    if provider in {"auto", "http"} and url and model:
        _EMBEDDER = HttpEmbedder(url, model, key, dim=int(dim_env) if dim_env else None)
        return _EMBEDDER
    # A model name that is not an existing directory would trigger a Hugging
    # Face download; in ``auto`` mode that is a silent 30s+ hang when the
    # network is down, so only local paths are picked up automatically.
    local_model = bool(model) and Path(model).expanduser().exists()
    if provider == "model2vec" or (provider == "auto" and local_model):
        try:
            _EMBEDDER = Model2VecEmbedder(
                model or "minishlab/potion-multilingual-128M",
                quantize_to=(os.environ.get("MEMBRIDGE_EMBED_DTYPE") or None),
            )
            return _EMBEDDER
        except Exception:  # noqa: BLE001 - fall back to hashing when unavailable
            if provider == "model2vec":
                raise
    _EMBEDDER = HashEmbedder(int(dim_env) if dim_env else 512)
    return _EMBEDDER


def reset_embedder() -> None:
    """Drop the cached embedder (used by tests and after config changes)."""
    global _EMBEDDER
    _EMBEDDER = None


# ---------------------------------------------------------------------------
# ranking
# ---------------------------------------------------------------------------

#: Patterns that look like an instruction smuggled into stored memory.  We do
#: not try to be clever - we flag and neutralise the obvious ones, and the
#: bootstrap payload tells the client to treat memories as data.
_INJECTION_PATTERNS = (
    re.compile(r"ignore (all )?(previous|prior|above) (instructions|prompts?)", re.I),
    re.compile(r"忽略(以上|之前|前面)(所有)?(的)?(指令|提示|要求)"),
    re.compile(r"^\s*system\s*:", re.I | re.M),
    re.compile(r"</?memory-data>", re.I),
    re.compile(r"你现在是|从现在开始你(必须|要)"),
)


def sanitize_memory_text(text: str) -> tuple[str, int]:
    """Neutralise obvious prompt-injection patterns in stored text.

    Returns the cleaned text plus how many patterns fired, so callers can
    report it in the write response instead of silently changing content.
    """
    cleaned = text or ""
    hits = 0
    for pattern in _INJECTION_PATTERNS:
        cleaned, count = pattern.subn("[已屏蔽]", cleaned)
        hits += count
    return cleaned, hits


def first_sentence(text: str, limit: int = 60) -> str:
    """One-line digest used by the bootstrap memory map and related hints."""
    body = (text or "").strip().replace("\n", " ")
    for separator in ("。", "；", "！", "？", ". ", "; "):
        index = body.find(separator)
        if 0 < index <= limit:
            return body[: index + len(separator)].strip()
    return body[:limit].strip() + ("…" if len(body) > limit else "")


#: Type weights used both in ranking and in the forgetting pass.
TYPE_WEIGHT = {
    "preference": 1.0,
    "decision": 0.95,
    "lesson": 0.95,
    "procedure": 0.85,
    "fact": 0.8,
    "reference": 0.7,
}

#: Types that are never auto-archived: they encode the user's rules and history.
PROTECTED_TYPES = {"preference", "decision", "lesson"}


def rrf_fuse(rankings: Iterable[Sequence[str]], *, k: int = 60) -> dict[str, float]:
    """Reciprocal Rank Fusion over id lists (best first)."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item_id in enumerate(ranking):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank + 1)
    return scores


def weighted_rrf(
    rankings: Iterable[tuple[Sequence[str], float]], *, k: int = 60
) -> dict[str, float]:
    """RRF with per-list weights (lexical usually deserves more trust than a
    hashed vector in the dependency-free fallback)."""
    scores: dict[str, float] = {}
    for ranking, weight in rankings:
        if weight <= 0:
            continue
        for rank, item_id in enumerate(ranking):
            scores[item_id] = scores.get(item_id, 0.0) + weight / (k + rank + 1)
    return scores


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except (TypeError, ValueError):
        try:
            return time.mktime(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
        except (TypeError, ValueError):
            return None


def age_days(value: str | None, now: float | None = None) -> float:
    ts = _parse_ts(value)
    if ts is None:
        return 365.0
    now = now or time.time()
    return max(0.0, (now - ts) / 86400.0)


def salience(
    *,
    item_type: str,
    confidence: float,
    importance: int,
    hits: int,
    updated_at: str | None,
    last_hit_at: str | None = None,
    half_life_days: float = 90.0,
    now: float | None = None,
) -> float:
    """0..1-ish score describing "how much this memory still matters".

    Used for both bootstrap injection ordering and the forgetting pass so the
    two never disagree about what is important.
    """
    signal_at = last_hit_at or updated_at
    days = age_days(signal_at, now)
    freshness = math.exp(-math.log(2) * days / max(1.0, half_life_days))
    # Usage counts decay too, otherwise a burst of queries (or a test run)
    # keeps a memory artificially "popular" forever.
    hit_age = age_days(last_hit_at, now) if last_hit_at else 3650.0
    usage = min(1.0, math.log1p(max(0, hits)) / math.log(11)) * math.exp(-math.log(2) * hit_age / 90.0)
    type_w = TYPE_WEIGHT.get(item_type, 0.75)
    imp = min(1.0, max(0.0, (int(importance or 3) - 1) / 4.0))
    conf = min(1.0, max(0.0, float(confidence or 0.5)))
    return 0.34 * freshness + 0.18 * usage + 0.20 * type_w + 0.16 * imp + 0.12 * conf


def decay_score(
    *,
    item_type: str,
    confidence: float,
    importance: int,
    hits: int,
    updated_at: str | None,
    last_hit_at: str | None = None,
    now: float | None = None,
) -> float:
    """Forgetting score: below the archive threshold means "archive me".

    Deliberately stricter than ``salience`` (which is a ranking input): a memory
    must be old, low-importance and unused to be archived.
    """
    signal_at = last_hit_at or updated_at
    days = age_days(signal_at, now)
    half_life = 45.0 if item_type in {"fact", "reference"} else 120.0
    freshness = math.exp(-math.log(2) * days / half_life)
    type_w = TYPE_WEIGHT.get(item_type, 0.75)
    imp = min(1.0, max(0.0, (int(importance or 3) - 1) / 4.0))
    conf = min(1.0, max(0.0, float(confidence or 0.5)))
    usage = 1.0 + 0.4 * math.log1p(max(0, hits))
    return freshness * type_w * (0.5 + 0.5 * imp) * (0.5 + 0.5 * conf) * usage


#: Defaults for the forgetting pass; all overridable through the service.
ARCHIVE_THRESHOLD = 0.18
ARCHIVE_MIN_AGE_DAYS = 45.0


def should_archive(row: Any, *, now: float | None = None, threshold: float = ARCHIVE_THRESHOLD) -> bool:
    item_type = row["type"] if "type" in row.keys() else "fact"
    if item_type in PROTECTED_TYPES:
        return False
    if "pinned" in row.keys() and row["pinned"]:
        return False
    updated = row["updated_at"] if "updated_at" in row.keys() else None
    if age_days(updated, now) < ARCHIVE_MIN_AGE_DAYS:
        return False
    score = decay_score(
        item_type=item_type,
        confidence=row["confidence"] if "confidence" in row.keys() else 0.8,
        importance=row["importance"] if "importance" in row.keys() else 3,
        hits=row["hits"] if "hits" in row.keys() else 0,
        updated_at=updated,
        last_hit_at=row["last_hit_at"] if "last_hit_at" in row.keys() else None,
        now=now,
    )
    return score < threshold


# ---------------------------------------------------------------------------
# storage helpers
# ---------------------------------------------------------------------------


def upsert_vector(
    conn: sqlite3.Connection,
    owner_kind: str,
    owner_id: str,
    model: str,
    vector: Sequence[float],
    *,
    now: str,
) -> None:
    conn.execute(
        "INSERT INTO vectors(owner_kind, owner_id, model, dim, vec, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(owner_kind, owner_id) DO UPDATE SET "
        "model = excluded.model, dim = excluded.dim, vec = excluded.vec, updated_at = excluded.updated_at",
        (owner_kind, owner_id, model, len(vector), pack_vector(vector), now),
    )


def delete_vector(conn: sqlite3.Connection, owner_kind: str, owner_id: str) -> None:
    conn.execute("DELETE FROM vectors WHERE owner_kind = ? AND owner_id = ?", (owner_kind, owner_id))


def load_vectors(
    conn: sqlite3.Connection, owner_kind: str, owner_ids: Sequence[str] | None = None, model: str | None = None
) -> dict[str, list[float]]:
    sql = "SELECT owner_id, model, vec FROM vectors WHERE owner_kind = ?"
    params: list[Any] = [owner_kind]
    if model:
        sql += " AND model = ?"
        params.append(model)
    if owner_ids is not None:
        if not owner_ids:
            return {}
        placeholders = ",".join("?" for _ in owner_ids)
        sql += f" AND owner_id IN ({placeholders})"
        params.extend(owner_ids)
    out: dict[str, list[float]] = {}
    for row in conn.execute(sql, params):
        out[row["owner_id"]] = list(unpack_vector(row["vec"]))
    return out


def upsert_fts(conn: sqlite3.Connection, owner_kind: str, owner_id: str, tokens: str) -> None:
    conn.execute("DELETE FROM search_fts WHERE owner_kind = ? AND owner_id = ?", (owner_kind, owner_id))
    conn.execute(
        "INSERT INTO search_fts(owner_kind, owner_id, tokens) VALUES (?, ?, ?)",
        (owner_kind, owner_id, tokens),
    )


def delete_fts(conn: sqlite3.Connection, owner_kind: str, owner_id: str) -> None:
    conn.execute("DELETE FROM search_fts WHERE owner_kind = ? AND owner_id = ?", (owner_kind, owner_id))


def bm25_ranking(
    conn: sqlite3.Connection,
    query: str,
    owner_kind: str,
    *,
    limit: int = 200,
) -> list[tuple[str, float]]:
    """Return ``[(owner_id, bm25_score)]`` best first (higher is better).

    Literal terms are tried first.  Only when they return fewer than three
    rows do the bilingual aliases get a chance, with their scores discounted,
    so "数据库" still ranks the Chinese memory first when one exists.
    """
    primary = _run_match(conn, fts_match_query(query), owner_kind, limit)
    if len(primary) >= 3:
        return primary
    expanded = _run_match(conn, fts_match_query(query, expand=True), owner_kind, limit)
    seen = {oid for oid, _ in primary}
    extra = [(oid, score * 0.85) for oid, score in expanded if oid not in seen]
    return (primary + extra)[:limit]


def _run_match(conn: sqlite3.Connection, match: str, owner_kind: str, limit: int) -> list[tuple[str, float]]:
    if not match:
        return []
    try:
        rows = conn.execute(
            "SELECT owner_id, bm25(search_fts) AS rank FROM search_fts "
            "WHERE search_fts MATCH ? AND owner_kind = ? ORDER BY rank LIMIT ?",
            (match, owner_kind, int(limit)),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(row["owner_id"], -float(row["rank"])) for row in rows]



def vector_ranking(
    vectors: dict[str, list[float]],
    query_vector: Sequence[float],
    *,
    limit: int = 200,
    min_cosine: float = 0.25,
    relative: float = 0.75,
) -> list[tuple[str, float]]:
    """Rank by cosine, keeping only plausible hits.

    Two gates matter for precision: an absolute floor (unrelated memories sit
    near 0) and a relative floor compared to the best hit, so a weak best match
    does not drag a tail of noise into the RRF fusion.
    """
    scored = []
    for owner_id, vec in vectors.items():
        sim = cosine(query_vector, vec)
        if sim >= min_cosine:
            scored.append((owner_id, sim))
    scored.sort(key=lambda item: item[1], reverse=True)
    if scored:
        cutoff = max(min_cosine, scored[0][1] * relative)
        scored = [item for item in scored if item[1] >= cutoff]
    return scored[:limit]


def needs_reindex(row_columns: Iterable[str]) -> bool:
    return "importance" not in set(row_columns)


def dumps_breakdown(**kwargs: Any) -> str:
    return json.dumps({k: round(v, 4) if isinstance(v, float) else v for k, v in kwargs.items()}, ensure_ascii=False)


@dataclass
class SearchWeights:
    """Knobs for the final blend (kept together so tests can assert them)."""

    lex_weight: float = 1.0
    vec_weight: float = 1.0
    rrf_k: int = 60
    salience_floor: float = 0.55
    pinned_boost: float = 1.12
