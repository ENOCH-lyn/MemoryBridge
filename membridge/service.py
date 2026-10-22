"""Core MemoryBridge service: identity, bootstrap, memory, docs and skills."""

from __future__ import annotations

import hashlib
import math
import os
import re
import configparser
import shutil
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import urlparse

from .db import (
    connect,
    init_db,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)
from .auth import ensure_default_admin


def _content_hash(text: str) -> str:
    normalized = re.sub(r"[\W_]+", "", (text or "").lower(), flags=re.UNICODE)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


MEMORY_TYPES = {"fact", "preference", "decision", "lesson", "procedure", "reference"}
TYPE_PRIOR = {
    "decision": 1.0,
    "lesson": 0.98,
    "preference": 0.92,
    "procedure": 0.92,
    "fact": 0.82,
    "reference": 0.72,
}

_WORD_RE = re.compile(r"[a-zA-Z0-9_./+#@-]+")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")


class GateError(Exception):
    """Raised when a tool is called without a valid bootstrap context."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def tokenize(text: str) -> list[str]:
    """Tokenize English words and CJK bigrams.

    FTS5's default tokenizer does not segment Chinese well, and the MVP dataset
    is small enough for Python scoring.  English words and CJK bigrams cover the
    common cases without an embedding dependency.
    """
    lower = (text or "").lower()
    words = [w for w in _WORD_RE.findall(lower) if len(w) >= 2 or w.isdigit()]
    grams: list[str] = []
    for run in _CJK_RE.findall(lower):
        if len(run) == 1:
            grams.append(run)
        else:
            grams.extend(run[i : i + 2] for i in range(len(run) - 1))
    return list(dict.fromkeys(words + grams))


def _snippet(text: str, terms: Iterable[str], width: int = 100) -> str:
    lower = text.lower()
    pos = -1
    for term in terms:
        pos = lower.find(term)
        if pos >= 0:
            break
    if pos < 0:
        pos = 0
    start = max(0, pos - width // 3)
    end = min(len(text), start + width)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end].replace("\n", " ") + suffix


def _recency(updated_at: str) -> float:
    try:
        then = datetime.strptime(updated_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.5
    days = max(0.0, (datetime.now(timezone.utc) - then).total_seconds() / 86400.0)
    return math.exp(-days / 180.0)


def _score(
    query: str,
    text: str,
    *,
    confidence: float = 0.8,
    updated_at: str = "",
    item_type: str = "fact",
) -> float:
    q = (query or "").strip().lower()
    if not q:
        return 0.0
    terms = tokenize(q)
    if not terms:
        return 0.0
    haystack = (text or "").lower()
    hits = sum(1 for term in terms if term in haystack)
    if hits == 0:
        return 0.0
    coverage = hits / len(terms)
    exact = 1.0 if q in haystack else 0.0
    prior = TYPE_PRIOR.get(item_type, 0.8)
    return (
        0.55 * coverage
        + 0.15 * exact
        + 0.10 * max(0.0, min(1.0, float(confidence)))
        + 0.10 * _recency(updated_at)
        + 0.10 * prior
    )


class AgentSyncService:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        base = Path(db_path).expanduser()
        if str(db_path) == ":memory:":
            self.skill_root = Path.cwd() / ".membridge-memory" / "skills"
        else:
            self.skill_root = base.resolve().parent / "skills"
        self.conn = connect(db_path)
        init_db(self.conn)
        ensure_default_admin(self.conn)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _dict(row: Any) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def _audit(self, action: str, entity: str | None = None, detail: dict | None = None, actor: str = "system") -> None:
        self.conn.execute(
            "INSERT INTO audit_log(at, actor, action, entity, detail_json) VALUES (?, ?, ?, ?, ?)",
            (utcnow(), actor, action, entity, json_dumps(detail or {})),
        )

    def _meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def get_agent_id(self) -> str:
        return self._meta("agent_id", "agent_default") or "agent_default"

    # ------------------------------------------------------------------
    # identity / directives
    # ------------------------------------------------------------------
    def get_identity(self) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM identity WHERE id = 1").fetchone()
        if row is None:
            raise RuntimeError("identity row missing; database was not initialized")
        return {
            "name": row["name"],
            "persona": row["persona"],
            "style": json_loads(row["style_json"], {}),
            "boundaries": json_loads(row["boundaries_json"], []),
            "version": row["version"],
            "updated_at": row["updated_at"],
        }

    def set_identity(
        self,
        *,
        name: str | None = None,
        persona: str | None = None,
        tone: str | None = None,
        suffix: str | None = None,
        address: str | None = None,
        language: str | None = None,
        boundaries: list[str] | None = None,
    ) -> dict[str, Any]:
        if name is not None and not name.strip():
            raise ValueError("name cannot be empty")
        if persona is not None and not persona.strip():
            raise ValueError("persona cannot be empty")
        current = self.get_identity()
        style = dict(current["style"])
        if tone is not None:
            style["tone"] = tone
        if suffix is not None:
            style["sentence_end"] = suffix
        if address is not None:
            style["address_user"] = address
        if language is not None:
            style["language"] = language
        new_boundaries = list(boundaries) if boundaries is not None else current["boundaries"]
        self.conn.execute(
            "UPDATE identity SET name = ?, persona = ?, style_json = ?, boundaries_json = ?, "
            "version = version + 1, updated_at = ? WHERE id = 1",
            (
                name if name is not None else current["name"],
                persona if persona is not None else current["persona"],
                json_dumps(style),
                json_dumps(new_boundaries),
                utcnow(),
            ),
        )
        self._audit("identity.update", "identity", {"name": name, "persona": persona})
        self.conn.commit()
        return self.get_identity()

    def identity_update(
        self,
        context_version: str,
        *,
        name: str | None = None,
        persona: str | None = None,
        address_user: str | None = None,
        language: str | None = None,
        boundaries: list[str] | None = None,
    ) -> dict[str, Any]:
        self.validate_context(context_version)
        identity = self.set_identity(
            name=name,
            persona=persona,
            address=address_user,
            language=language,
            boundaries=boundaries,
        )
        self._audit("identity.update", "identity", {"via": "mcp", "name": identity["name"]})
        self.conn.commit()
        return identity

    def add_directive(
        self,
        content: str,
        *,
        scope: str = "global",
        project_path: str | None = None,
        priority: int = 50,
    ) -> dict[str, Any]:
        if scope not in {"global", "project"}:
            raise ValueError("scope must be global or project")
        project_id = None
        if scope == "project":
            if not project_path:
                raise ValueError("project scope requires project_path")
            project_id = self.ensure_project(project_path)["id"]
        directive_id = new_id("dir")
        self.conn.execute(
            "INSERT INTO directives(id, scope, project_id, content, priority, active, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?)",
            (directive_id, scope, project_id, content.strip(), priority, utcnow()),
        )
        self._audit("directive.add", directive_id, {"scope": scope, "project_id": project_id})
        self.conn.commit()
        return {"directive_id": directive_id, "scope": scope, "project_id": project_id}

    def list_directives(self, project_id: str | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM directives WHERE active = 1 AND "
            "(scope = 'global' OR (scope = 'project' AND project_id = ?)) "
            "ORDER BY priority DESC, created_at ASC",
            (project_id,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "scope": r["scope"],
                "project_id": r["project_id"],
                "content": r["content"],
                "priority": r["priority"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # projects
    # ------------------------------------------------------------------
    def ensure_project(
        self,
        project_path: str,
        *,
        name: str | None = None,
        conventions: list[str] | None = None,
    ) -> dict[str, Any]:
        raw = (project_path or "").strip()
        if not raw:
            raise ValueError("project_path is required")
        path_obj = Path(raw).expanduser()
        try:
            normalized = str(path_obj.resolve()) if path_obj.exists() or "/" in raw or "\\" in raw else raw
        except OSError:
            normalized = raw
        project_key = self._project_key(normalized, name)
        project_id = "proj_" + hashlib.sha1(project_key.encode("utf-8")).hexdigest()[:12]
        row = self.conn.execute("SELECT * FROM projects WHERE key = ?", (project_key,)).fetchone()
        now = utcnow()
        if row is None:
            marker_name = None
            marker = Path(normalized) / ".membridge-project.json"
            if marker.exists():
                try:
                    marker_name = str(json_loads(marker.read_text(encoding="utf-8"), {}).get("name") or "").strip() or None
                except OSError:
                    marker_name = None
            project_name = name or marker_name or Path(normalized).name or normalized
            self.conn.execute(
                "INSERT INTO projects(id, key, name, path, conventions_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (project_id, project_key, project_name, normalized, json_dumps(conventions or []), now, now),
            )
            self._audit("project.create", project_id, {"key": project_key, "path": normalized, "name": project_name})
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        else:
            self.conn.execute(
                "UPDATE projects SET name = ?, path = ?, conventions_json = ?, updated_at = ? WHERE id = ?",
                (
                    name or row["name"],
                    normalized,
                    json_dumps(conventions if conventions is not None else json_loads(row["conventions_json"], [])),
                    now,
                    project_id,
                ),
            )
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return self._project_dict(row)

    def _git_remote(self, path: str) -> str | None:
        root = Path(path)
        git_path = root / ".git"
        if not git_path.exists():
            return None
        config_path: Path
        if git_path.is_file():
            text = git_path.read_text(encoding="utf-8", errors="replace")
            match = re.search(r"gitdir:\s*(.+)", text)
            if not match:
                return None
            git_dir = Path(match.group(1).strip())
            if not git_dir.is_absolute():
                git_dir = (root / git_dir).resolve()
            config_path = git_dir / "config"
        else:
            config_path = git_path / "config"
        if not config_path.exists():
            return None
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        try:
            parser.read(config_path, encoding="utf-8")
        except (configparser.Error, OSError):
            return None
        if parser.has_option('remote "origin"', "url"):
            return parser.get('remote "origin"', "url").strip() or None
        return None

    @staticmethod
    def _normalize_remote(remote: str) -> str:
        value = remote.strip()
        host = ""
        path = value
        if value.startswith("git@") and ":" in value:
            host, path = value[4:].split(":", 1)
        elif "://" in value:
            parsed = urlparse(value)
            host = parsed.hostname or ""
            path = parsed.path
        path = path.strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        return f"git:{host.lower()}/{path.lower()}"

    def _project_key(self, normalized: str, name: str | None = None) -> str:
        marker = Path(normalized) / ".membridge-project.json"
        if marker.exists():
            try:
                data = json_loads(marker.read_text(encoding="utf-8"), {})
                key = str(data.get("key") or "").strip()
                if key:
                    return key
            except OSError:
                pass
        remote = self._git_remote(normalized)
        if remote:
            return self._normalize_remote(remote)
        # The key must be stable across devices; the display name is not.
        base = Path(normalized).name or normalized
        return "name:" + base.strip().lower()

    @staticmethod
    def _project_dict(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "key": row["key"],
            "name": row["name"],
            "path": row["path"],
            "conventions": json_loads(row["conventions_json"], []),
        }

    def get_project(self, project_id: str | None) -> dict[str, Any] | None:
        if not project_id:
            return None
        row = self.conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return self._project_dict(row) if row else None

    def project_list(self) -> dict[str, Any]:
        rows = self.conn.execute("SELECT * FROM projects WHERE deleted_at IS NULL ORDER BY updated_at DESC").fetchall()
        items = []
        for row in rows:
            item = self._project_dict(row)
            item["memories"] = self.conn.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE project_id = ? AND status = 'active'", (row["id"],)
            ).fetchone()["n"]
            item["documents"] = self.conn.execute(
                "SELECT COUNT(*) AS n FROM documents WHERE project_id = ?", (row["id"],)
            ).fetchone()["n"]
            item["skills"] = self.conn.execute(
                "SELECT COUNT(*) AS n FROM skills WHERE project_id = ?", (row["id"],)
            ).fetchone()["n"]
            item["updated_at"] = row["updated_at"]
            items.append(item)
        global_memories = self.conn.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE project_id IS NULL AND status = 'active'"
        ).fetchone()["n"]
        return {"items": items, "count": len(items), "global_memories": global_memories}

    def project_delete(self, context_version: str, project_id: str) -> dict[str, Any]:
        self.validate_context(context_version)
        row = self.conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown project: {project_id}")
        self.conn.execute("UPDATE projects SET deleted_at = ?, updated_at = ? WHERE id = ?", (utcnow(), utcnow(), project_id))
        self._audit("project.delete", project_id, {"key": row["key"], "name": row["name"], "soft": True})
        self.conn.commit()
        return {"project_id": project_id, "deleted": True, "soft": True}

    # ------------------------------------------------------------------
    # bootstrap / context gate
    # ------------------------------------------------------------------
    def _context_payload(
        self,
        identity: dict[str, Any],
        directives: list[dict[str, Any]],
        project: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "identity_version": identity["version"],
            "identity_name": identity["name"],
            "directives": [(d["id"], d["content"], d["priority"]) for d in directives],
            "project_id": project["id"] if project else None,
            "conventions": project["conventions"] if project else [],
        }

    def _context_version(self, payload: dict[str, Any]) -> str:
        raw = json_dumps(payload).encode("utf-8")
        return "ctx_" + hashlib.sha256(raw).hexdigest()[:24]

    def bootstrap(
        self,
        *,
        project_path: str | None = None,
        agent_id: str | None = None,
        client: str = "unknown",
    ) -> dict[str, Any]:
        project = self.ensure_project(project_path) if project_path else None
        identity = self.get_identity()
        directives = self.list_directives(project["id"] if project else None)
        context_version = self._context_version(self._context_payload(identity, directives, project))
        now = utcnow()
        expires = datetime.now(timezone.utc).replace(microsecond=0)
        expires = expires.timestamp() + 24 * 3600
        expires_at = datetime.fromtimestamp(expires, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        resolved_agent = agent_id or self._meta("agent_id", "agent_default") or "agent_default"
        if agent_id:
            self._set_meta("agent_id", resolved_agent)
        self.conn.execute(
            "INSERT OR REPLACE INTO sessions(context_version, agent_id, project_id, identity_version, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                context_version,
                resolved_agent,
                project["id"] if project else None,
                identity["version"],
                now,
                expires_at,
            ),
        )
        self._audit("bootstrap", context_version, {"agent_id": resolved_agent, "client": client, "project_id": project["id"] if project else None})
        self.conn.commit()

        project_id = project["id"] if project else None
        recent = self._recent_items(project_id)
        projects = self.project_list()
        global_memories = self._bootstrap_memories(None)
        project_memories = self._bootstrap_memories(project_id) if project_id else []
        available = {
            "memories": self._count_memories(project_id, "active"),
            "candidates": self._count_memories(project_id, "candidate"),
            "documents": self._count("documents", project_id),
            "skills": self._count("skills", project_id),
            "projects": projects["count"],
        }
        return {
            "agent_id": resolved_agent,
            "client": client,
            "identity": identity,
            "global_directives": [d["content"] for d in directives if d["scope"] == "global"],
            "project_directives": [d["content"] for d in directives if d["scope"] == "project"],
            "project": project,
            "projects": projects["items"],
            "global_memories": global_memories,
            "project_memories": project_memories,
            "recent_decisions": recent["decisions"],
            "recent_lessons": recent["lessons"],
            "available": available,
            "context_version": context_version,
            "next_actions": [
                "Use project_list to see all projects and their memory counts",
                "Use memory_search(scope='auto') for current project + global; scope='all' for cross-project",
                "Use memory_list(project_id=...) to batch-read a known project",
                "Use doc_search / doc_read / doc_add for project documents",
                "Use skill_list / skill_get / skill_file / skill_save_file for folder-shaped skills",
                "When the user states a durable requirement, call memory_write directly with topic/scope/rationale",
                "Use identity_update for persona changes; put tone/tic inside persona",
            ],
            "capture_contract": {
                "mode": "ai_initiated",
                "triggers": [
                    "user states a durable preference, requirement, decision or lesson",
                    "user says remember / save / do not forget",
                    "user states a stable UI or output preference",
                    "user asks to create or update a document or skill",
                ],
                "required_tools": {
                    "memory": "memory_write (scope=project by default; global pinned for user-wide facts)",
                    "document": "doc_add; doc_delete only on explicit delete request",
                    "skill": "skill_install / skill_save_file; skill_delete only on explicit delete request",
                    "identity": "identity_update",
                },
                "routing": {
                    "memory": "compact always-on facts, preferences, decisions and lessons (1-3 sentences)",
                    "document": "long-form design/spec/ADR/notes; not auto-injected; use doc_search then doc_read",
                    "skill": "executable folder with SKILL.md and scripts/references",
                    "memory_pointer": "for a long document whose conclusion must always be known",
                },
                "before_final_answer": "Scan for the triggers above; perform the write, then answer.",
                "approval": "none",
                "note": "You and the web console can both write. The server cannot force a model to call a tool; this contract is also returned by bootstrap so the client can enforce its own system prompt.",
            },
        }

    def _bootstrap_memories(self, project_id: str | None, limit: int | None = None) -> list[dict[str, Any]]:
        if limit is None:
            try:
                limit = int(os.environ.get("MEMBRIDGE_BOOTSTRAP_MEMORY_LIMIT", "20"))
            except ValueError:
                limit = 20
        rows = self.conn.execute(
            "SELECT * FROM memories WHERE status = 'active' AND project_id IS ? "
            "ORDER BY pinned DESC, updated_at DESC LIMIT ?",
            (project_id, max(1, min(int(limit), 100))),
        ).fetchall()
        return [self._memory_dict(row) for row in rows]

    def _count(self, table: str, project_id: str | None) -> int:
        if project_id:
            row = self.conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE project_id = ? OR project_id IS NULL",
                (project_id,),
            ).fetchone()
        else:
            row = self.conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE project_id IS NULL").fetchone()
        return int(row["n"]) if row else 0

    def _count_memories(self, project_id: str | None, status: str) -> int:
        if project_id:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE status = ? AND (project_id = ? OR project_id IS NULL)",
                (status, project_id),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE status = ? AND project_id IS NULL",
                (status,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def _recent_items(self, project_id: str | None) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {"decisions": [], "lessons": []}
        for item_type, key in (("decision", "decisions"), ("lesson", "lessons")):
            if project_id:
                rows = self.conn.execute(
                    "SELECT * FROM memories WHERE status = 'active' AND type = ? "
                    "AND (project_id = ? OR project_id IS NULL) ORDER BY updated_at DESC LIMIT 5",
                    (item_type, project_id),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM memories WHERE status = 'active' AND type = ? "
                    "AND project_id IS NULL ORDER BY updated_at DESC LIMIT 5",
                    (item_type,),
                ).fetchall()
            result[key] = [self._memory_dict(r) for r in rows]
        return result

    def validate_context(self, context_version: str | None) -> dict[str, Any]:
        if not context_version:
            raise GateError("BOOTSTRAP_REQUIRED", "Call agent_bootstrap first in this conversation.")
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE context_version = ?", (context_version,)
        ).fetchone()
        if row is None:
            raise GateError(
                "BOOTSTRAP_REQUIRED",
                "Unknown context_version. Call agent_bootstrap first in this conversation.",
            )
        if row["expires_at"] <= utcnow():
            raise GateError("CONTEXT_STALE", "Bootstrap session expired. Call agent_bootstrap again.")
        project = self.get_project(row["project_id"])
        identity = self.get_identity()
        directives = self.list_directives(project["id"] if project else None)
        current = self._context_version(self._context_payload(identity, directives, project))
        if current != context_version:
            raise GateError(
                "CONTEXT_STALE",
                "Identity or project context changed. Call agent_bootstrap again to refresh.",
            )
        return {
            "context_version": context_version,
            "agent_id": row["agent_id"],
            "project_id": row["project_id"],
        }

    # ------------------------------------------------------------------
    # memory
    # ------------------------------------------------------------------
    @staticmethod
    def _memory_dict(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "type": row["type"],
            "content": row["content"],
            "confidence": row["confidence"],
            "source": row["source"],
            "status": row["status"],
            "topic": row["topic"],
            "rationale": row["rationale"],
            "conditions": row["conditions"],
            "pinned": bool(row["pinned"]) if "pinned" in row.keys() else False,
            "episode_id": row["episode_id"],
            "evidence_quote": row["evidence_quote"],
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _find_duplicate(
        self,
        project_id: str | None,
        content_hash: str,
        statuses: tuple[str, ...] = ("active", "candidate"),
    ) -> Any:
        placeholders = ",".join("?" for _ in statuses)
        return self.conn.execute(
            f"SELECT * FROM memories WHERE content_hash = ? AND status IN ({placeholders}) "
            "AND project_id IS ? LIMIT 1",
            (content_hash, *statuses, project_id),
        ).fetchone()

    def memory_write(
        self,
        context_version: str,
        content: str,
        *,
        type: str = "fact",
        topic: str,
        scope: str = "project",
        project_id: str | None = None,
        rationale: str = "",
        conditions: str = "",
        confidence: float = 0.85,
        source: str = "agent",
        evidence_quote: str | None = None,
        pinned: bool | None = None,
    ) -> dict[str, Any]:
        """The normal AI-facing write path: direct active upsert, no approval queue.

        A stable ``topic`` is mandatory so a later correction updates the same
        memory instead of accumulating near-duplicates. ``rationale`` and
        ``conditions`` are required for decisions/lessons so a project-scoped
        fact does not silently become a global rule.
        """
        session = self.validate_context(context_version)
        if type not in MEMORY_TYPES:
            raise ValueError(f"type must be one of {sorted(MEMORY_TYPES)}")
        if scope not in {"project", "global"}:
            raise ValueError("scope must be project or global")
        if not content.strip():
            raise ValueError("content is required")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,79}", topic or ""):
            raise ValueError("topic must be a stable lowercase key like decision.storage.sqlite")
        if type in {"decision", "lesson"} and not rationale.strip():
            raise ValueError(f"rationale is required for {type} memories")
        if scope == "global":
            target_project = None
        else:
            target_project = project_id if project_id is not None else session["project_id"]
            if not target_project:
                raise ValueError("project scope requires a project; bootstrap with project_path or use scope=global")
            if self.get_project(target_project) is None:
                raise ValueError(f"unknown project_id: {target_project}")
        digest = _content_hash(content)
        existing = self.conn.execute(
            "SELECT * FROM memories WHERE project_id IS ? AND topic = ? AND status = 'active' LIMIT 1",
            (target_project, topic),
        ).fetchone()
        now = utcnow()
        if existing:
            self.conn.execute(
                "UPDATE memories SET type = ?, content = ?, confidence = ?, source = ?, topic = ?, "
                "rationale = ?, conditions = ?, evidence_quote = ?, content_hash = ?, pinned = ?, "
                "version = version + 1, updated_at = ? WHERE id = ?",
                (
                    type,
                    content.strip(),
                    float(confidence),
                    source,
                    topic,
                    rationale.strip(),
                    conditions.strip(),
                    evidence_quote,
                    digest,
                    existing["pinned"] if pinned is None else (1 if pinned else 0),
                    now,
                    existing["id"],
                ),
            )
            self._audit("memory.write", existing["id"], {"topic": topic, "project_id": target_project, "updated": True})
            self.conn.commit()
            return {"memory_id": existing["id"], "project_id": target_project, "status": "active", "version": existing["version"] + 1}
        memory_id = new_id("mem")
        self.conn.execute(
            "INSERT INTO memories(id, project_id, type, content, confidence, source, status, topic, rationale, "
            "conditions, evidence_quote, content_hash, pinned, version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (
                memory_id,
                target_project,
                type,
                content.strip(),
                float(confidence),
                source,
                topic,
                rationale.strip(),
                conditions.strip(),
                evidence_quote,
                digest,
                1 if pinned else 0,
                now,
                now,
            ),
        )
        self._audit("memory.write", memory_id, {"topic": topic, "project_id": target_project, "updated": False})
        self.conn.commit()
        return {"memory_id": memory_id, "project_id": target_project, "status": "active", "version": 1}

    def memory_add(
        self,
        context_version: str,
        content: str,
        *,
        type: str = "fact",
        project_id: str | None = None,
        confidence: float = 0.8,
        source: str = "agent",
        episode_id: str | None = None,
        evidence_quote: str | None = None,
    ) -> dict[str, Any]:
        session = self.validate_context(context_version)
        if type not in MEMORY_TYPES:
            raise ValueError(f"type must be one of {sorted(MEMORY_TYPES)}")
        target_project = project_id if project_id is not None else session["project_id"]
        if target_project == "global":
            target_project = None
        if target_project and self.get_project(target_project) is None:
            raise ValueError(f"unknown project_id: {target_project}")
        digest = _content_hash(content)
        duplicate = self._find_duplicate(target_project, digest)
        if duplicate is not None:
            return {
                "memory_id": duplicate["id"],
                "project_id": duplicate["project_id"],
                "type": duplicate["type"],
                "status": duplicate["status"],
                "duplicate": True,
            }
        memory_id = new_id("mem")
        now = utcnow()
        self.conn.execute(
            "INSERT INTO memories(id, project_id, type, content, confidence, source, status, "
            "episode_id, evidence_quote, content_hash, version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, 1, ?, ?)",
            (
                memory_id,
                target_project,
                type,
                content.strip(),
                float(confidence),
                source,
                episode_id,
                evidence_quote,
                digest,
                now,
                now,
            ),
        )
        self._audit("memory.add", memory_id, {"project_id": target_project, "type": type, "episode_id": episode_id})
        self.conn.commit()
        return {"memory_id": memory_id, "project_id": target_project, "type": type, "status": "active"}

    def memory_search(
        self,
        context_version: str,
        query: str,
        *,
        project_id: str | None = None,
        scope: str = "auto",
        k: int = 8,
    ) -> dict[str, Any]:
        session = self.validate_context(context_version)
        selected = project_id if project_id is not None else session["project_id"]
        if scope not in {"auto", "project", "global", "all"}:
            raise ValueError("scope must be auto, project, global or all")
        if scope == "all":
            allowed = None
        elif scope == "global":
            allowed = {None}
        elif scope == "project":
            allowed = {selected} if selected else {None}
        else:
            allowed = {selected, None} if selected else {None}

        project_map = {row["id"]: self._project_dict(row) for row in self.conn.execute("SELECT * FROM projects").fetchall()}
        rows = self.conn.execute(
            "SELECT * FROM memories WHERE status = 'active' ORDER BY updated_at DESC"
        ).fetchall()
        scored: list[tuple[float, Any]] = []
        for row in rows:
            if allowed is not None and row["project_id"] not in allowed:
                continue
            score = _score(
                query,
                row["content"],
                confidence=row["confidence"],
                updated_at=row["updated_at"],
                item_type=row["type"],
            )
            if score > 0:
                scored.append((score, row))
        scored.sort(key=lambda x: (x[0], x[1]["updated_at"]), reverse=True)
        terms = tokenize(query)
        items = []
        for score, row in scored[: max(1, min(int(k), 50))]:
            item = self._memory_dict(row)
            project = project_map.get(row["project_id"])
            item["project_key"] = project["key"] if project else "global"
            item["project_name"] = project["name"] if project else "global"
            item["score"] = round(score, 3)
            item["snippet"] = _snippet(row["content"], terms)
            item["citation"] = f"[mem:{row['id']}]"
            items.append(item)
        return {"query": query, "scope": scope, "project_id": selected, "items": items, "count": len(items)}

    def memory_list(
        self,
        context_version: str,
        *,
        project_id: str | None = None,
        scope: str = "auto",
        status: str = "active",
        limit: int = 50,
    ) -> dict[str, Any]:
        session = self.validate_context(context_version)
        if status not in {"active", "candidate", "rejected", "deleted", "all"}:
            raise ValueError("status must be active, candidate, rejected, deleted or all")
        if scope not in {"auto", "project", "global", "all"}:
            raise ValueError("scope must be auto, project, global or all")
        selected = project_id if project_id is not None else session["project_id"]
        status_clause = "" if status == "all" else "status = ? AND "
        params: list[Any] = [] if status == "all" else [status]
        if scope == "global":
            project_clause, project_params = "project_id IS NULL", []
        elif scope == "all":
            project_clause, project_params = "1 = 1", []
        elif scope == "project":
            project_clause, project_params = "project_id IS ?", [selected]
        else:
            project_clause, project_params = "(project_id IS ? OR project_id IS NULL)", [selected]
        rows = self.conn.execute(
            f"SELECT * FROM memories WHERE {status_clause}{project_clause} "
            "ORDER BY updated_at DESC LIMIT ?",
            (*params, *project_params, max(1, min(limit, 200))),
        ).fetchall()
        project_map = {row["id"]: self._project_dict(row) for row in self.conn.execute("SELECT * FROM projects").fetchall()}
        items = []
        for row in rows:
            item = self._memory_dict(row)
            project = project_map.get(row["project_id"])
            item["project_key"] = project["key"] if project else "global"
            item["project_name"] = project["name"] if project else "global"
            items.append(item)
        return {"items": items, "count": len(items), "status": status, "scope": scope}

    def memory_feedback(
        self,
        context_version: str,
        memory_id: str,
        signal: str = "helpful",
    ) -> dict[str, Any]:
        session = self.validate_context(context_version)
        row = self.conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown memory_id: {memory_id}")
        if row["project_id"] not in {session["project_id"], None}:
            raise ValueError("memory is outside the current project scope")
        if signal not in {"helpful", "harmful", "used"}:
            raise ValueError("signal must be helpful, harmful or used")
        delta = {"helpful": 0.02, "harmful": -0.05, "used": 0.0}[signal]
        confidence = max(0.1, min(1.0, float(row["confidence"]) + delta))
        self.conn.execute(
            "UPDATE memories SET uses = uses + 1, helpful = helpful + ?, confidence = ?, updated_at = ? WHERE id = ?",
            (1 if signal == "helpful" else 0, confidence, utcnow(), memory_id),
        )
        self._audit("memory.feedback", memory_id, {"signal": signal})
        self.conn.commit()
        return {"memory_id": memory_id, "signal": signal, "confidence": round(confidence, 3)}

    def memory_update(
        self,
        context_version: str,
        memory_id: str,
        *,
        content: str | None = None,
        confidence: float | None = None,
    ) -> dict[str, Any]:
        session = self.validate_context(context_version)
        row = self.conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown memory_id: {memory_id}")
        if row["project_id"] not in {session["project_id"], None}:
            raise ValueError("memory is outside the current project scope")
        if row["status"] == "deleted":
            raise ValueError("memory is deleted")
        new_content = content.strip() if content is not None else row["content"]
        new_confidence = float(confidence) if confidence is not None else row["confidence"]
        digest = _content_hash(new_content)
        self.conn.execute(
            "UPDATE memories SET content = ?, confidence = ?, content_hash = ?, "
            "version = version + 1, updated_at = ? WHERE id = ?",
            (new_content, new_confidence, digest, utcnow(), memory_id),
        )
        self._audit("memory.update", memory_id, {"version": row["version"] + 1})
        self.conn.commit()
        return {"memory_id": memory_id, "version": row["version"] + 1}

    def memory_delete(self, context_version: str, memory_id: str) -> dict[str, Any]:
        session = self.validate_context(context_version)
        row = self.conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown memory_id: {memory_id}")
        if row["project_id"] not in {session["project_id"], None}:
            raise ValueError("memory is outside the current project scope")
        if row["status"] == "deleted":
            return {"memory_id": memory_id, "status": "deleted", "already": True}
        self.conn.execute(
            "UPDATE memories SET status = 'deleted', deleted_at = ?, version = version + 1, updated_at = ? WHERE id = ?",
            (utcnow(), utcnow(), memory_id),
        )
        self._audit("memory.delete", memory_id, {"previous_status": row["status"]})
        self.conn.commit()
        return {"memory_id": memory_id, "status": "deleted"}

    # ------------------------------------------------------------------
    # documents
    # ------------------------------------------------------------------
    def doc_add(
        self,
        context_version: str,
        *,
        path: str,
        content: str,
        title: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        session = self.validate_context(context_version)
        target = project_id if project_id is not None else session["project_id"]
        if target == "global":
            target = None
        existing = self.conn.execute(
            "SELECT * FROM documents WHERE path = ? AND project_id IS ?",
            (path, target),
        ).fetchone()
        now = utcnow()
        doc_title = title or Path(path).stem
        if existing:
            self.conn.execute(
                "UPDATE documents SET title = ?, content = ?, updated_at = ? WHERE id = ?",
                (doc_title, content, now, existing["id"]),
            )
            doc_id = existing["id"]
            action = "doc.update"
        else:
            doc_id = new_id("doc")
            self.conn.execute(
                "INSERT INTO documents(id, project_id, path, title, content, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (doc_id, target, path, doc_title, content, now),
            )
            action = "doc.add"
        self._audit(action, doc_id, {"path": path, "project_id": target})
        self.conn.commit()
        return {"document_id": doc_id, "path": path, "title": doc_title, "project_id": target}

    def doc_search(
        self,
        context_version: str,
        query: str,
        *,
        project_id: str | None = None,
        scope: str = "auto",
        k: int = 5,
    ) -> dict[str, Any]:
        session = self.validate_context(context_version)
        if scope not in {"auto", "project", "global", "all"}:
            raise ValueError("scope must be auto, project, global or all")
        selected = project_id if project_id is not None else session["project_id"]
        rows = self.conn.execute("SELECT * FROM documents ORDER BY updated_at DESC").fetchall()
        scored: list[tuple[float, Any]] = []
        for row in rows:
            if not self._doc_scope_allows(row["project_id"], selected, scope):
                continue
            text = f"{row['title']}\n{row['content']}"
            score = _score(query, text, updated_at=row["updated_at"], item_type="reference")
            if score > 0:
                scored.append((score, row))
        scored.sort(key=lambda x: x[0], reverse=True)
        terms = tokenize(query)
        items = []
        for score, row in scored[: max(1, min(int(k), 20))]:
            items.append(
                {
                    "document_id": row["id"],
                    "title": row["title"],
                    "path": row["path"],
                    "project_id": row["project_id"],
                    "score": round(score, 3),
                    "snippet": _snippet(row["content"], terms),
                    "citation": f"[doc:{row['id']}]",
                }
            )
        return {"query": query, "scope": scope, "items": items, "count": len(items)}

    @staticmethod
    def _doc_scope_allows(project_id: str | None, selected: str | None, scope: str) -> bool:
        if scope == "all":
            return True
        if scope == "global":
            return project_id is None
        if scope == "project":
            return project_id == selected
        return project_id in {selected, None}

    def doc_list(
        self,
        context_version: str,
        *,
        project_id: str | None = None,
        scope: str = "auto",
        limit: int = 100,
    ) -> dict[str, Any]:
        session = self.validate_context(context_version)
        if scope not in {"auto", "project", "global", "all"}:
            raise ValueError("scope must be auto, project, global or all")
        selected = project_id if project_id is not None else session["project_id"]
        if scope == "all":
            rows = self.conn.execute("SELECT * FROM documents ORDER BY updated_at DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
        elif scope == "global":
            rows = self.conn.execute("SELECT * FROM documents WHERE project_id IS NULL ORDER BY updated_at DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
        elif scope == "project":
            rows = self.conn.execute("SELECT * FROM documents WHERE project_id = ? ORDER BY updated_at DESC LIMIT ?", (selected, max(1, min(limit, 200)))).fetchall()
        elif selected:
            rows = self.conn.execute(
                "SELECT * FROM documents WHERE project_id = ? OR project_id IS NULL "
                "ORDER BY updated_at DESC LIMIT ?",
                (selected, max(1, min(limit, 200))),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM documents WHERE project_id IS NULL ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        items = []
        for row in rows:
            preview = (row["content"] or "").replace("\n", " ")[:160]
            items.append(
                {
                    "document_id": row["id"],
                    "title": row["title"],
                    "path": row["path"],
                    "project_id": row["project_id"],
                    "updated_at": row["updated_at"],
                    "preview": preview,
                }
            )
        return {"items": items, "count": len(items), "scope": scope}

    def doc_read(self, context_version: str, document_id: str, *, scope: str = "auto") -> dict[str, Any]:
        session = self.validate_context(context_version)
        if scope not in {"auto", "project", "global", "all"}:
            raise ValueError("scope must be auto, project, global or all")
        row = self.conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        if row is None:
            row = self.conn.execute("SELECT * FROM documents WHERE path = ?", (document_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown document: {document_id}")
        if not self._doc_scope_allows(row["project_id"], session["project_id"], scope):
            raise ValueError("document is outside the current scope")
        return {
            "document_id": row["id"],
            "title": row["title"],
            "path": row["path"],
            "project_id": row["project_id"],
            "content": row["content"],
        }

    def doc_delete(self, context_version: str, document_id: str) -> dict[str, Any]:
        session = self.validate_context(context_version)
        row = self.conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        if row is None:
            row = self.conn.execute("SELECT * FROM documents WHERE path = ?", (document_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown document: {document_id}")
        if row["project_id"] not in {session["project_id"], None}:
            raise ValueError("document is outside the current project scope")
        self.conn.execute("DELETE FROM documents WHERE id = ?", (row["id"],))
        self._audit("doc.delete", row["id"], {"path": row["path"], "project_id": row["project_id"]})
        self.conn.commit()
        return {"document_id": row["id"], "deleted": True}

    # ------------------------------------------------------------------
    # skills
    # ------------------------------------------------------------------
    @staticmethod
    def _skill_dict(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "name": row["name"],
            "slug": row["slug"],
            "trigger": row["trigger"],
            "description": row["description"],
            "steps": json_loads(row["steps_json"], []),
            "manifest": json_loads(row["manifest_json"], {}),
            "dir_path": row["dir_path"],
            "file_count": row["file_count"],
            "status": row["status"],
            "success_count": row["success_count"],
            "failure_count": row["failure_count"],
        }

    @staticmethod
    def _safe_skill_path(rel: str) -> str:
        value = (rel or "").replace("\\", "/").strip()
        if not value or value.startswith("/") or len(value) > 300:
            raise ValueError(f"invalid skill file path: {rel!r}")
        parts = PurePosixPath(value).parts
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError(f"invalid skill file path: {rel!r}")
        return "/".join(parts)

    @staticmethod
    def _parse_skill_md(text: str) -> dict[str, Any]:
        manifest: dict[str, Any] = {}
        body = text
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end >= 0:
                header = text[3:end]
                body = text[end + 4 :]
                current_key: str | None = None
                for raw in header.splitlines():
                    line = raw.rstrip()
                    if not line.strip() or line.lstrip().startswith("#"):
                        continue
                    if line.lstrip().startswith("- "):
                        if current_key:
                            manifest.setdefault(current_key, []).append(line.lstrip()[2:].strip().strip("'\""))
                        continue
                    if ":" in line and not line.startswith(" "):
                        key, value = line.split(":", 1)
                        key = key.strip().lower()
                        value = value.strip().strip("'\"")
                        if value:
                            manifest[key] = value
                            current_key = None
                        else:
                            manifest[key] = []
                            current_key = key
        title = ""
        for line in body.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
        description = str(manifest.get("description") or "").strip()
        if not description:
            paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
            for para in paragraphs:
                if not para.startswith("#"):
                    description = para.replace("\n", " ")[:300]
                    break
        manifest["title"] = title
        manifest["description"] = description
        manifest["body"] = body.strip()
        return manifest

    def _skill_dir(self, skill_id: str) -> Path:
        return self.skill_root / skill_id

    def _write_skill_files(self, skill_id: str, files: dict[str, str]) -> list[dict[str, Any]]:
        if not isinstance(files, dict) or not files:
            raise ValueError("files must be a non-empty object")
        if len(files) > 200:
            raise ValueError("too many skill files (max 200)")
        target = self._skill_dir(skill_id)
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        total = 0
        written: list[dict[str, Any]] = []
        for rel, content in files.items():
            if not isinstance(content, str):
                raise ValueError(f"skill file must be text: {rel}")
            safe = self._safe_skill_path(rel)
            encoded = content.encode("utf-8")
            if len(encoded) > 1024 * 1024:
                raise ValueError(f"skill file too large: {safe}")
            total += len(encoded)
            if total > 8 * 1024 * 1024:
                raise ValueError("skill package too large (max 8MB)")
            dest = target / safe
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
            written.append({"path": safe, "size": len(encoded)})
        return sorted(written, key=lambda item: item["path"])

    def _skill_files(self, skill_id: str) -> list[dict[str, Any]]:
        root = self._skill_dir(skill_id)
        if not root.exists():
            return []
        result = []
        for path in sorted(root.rglob("*")):
            if path.is_file():
                result.append({"path": path.relative_to(root).as_posix(), "size": path.stat().st_size})
        return result

    def skill_install(
        self,
        context_version: str,
        *,
        files: dict[str, str],
        name: str | None = None,
        project_id: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        session = self.validate_context(context_version)
        # Skills are portable capabilities: global by default. Pass project_id
        # explicitly for a skill that only makes sense inside one repository.
        target = project_id if project_id is not None else None
        if target == "global":
            target = None
        lowered = {key.lower(): key for key in files}
        skill_md_key = lowered.get("skill.md")
        if not skill_md_key:
            raise ValueError("skill package must contain SKILL.md")
        normalized_files = dict(files)
        if skill_md_key != "SKILL.md":
            normalized_files["SKILL.md"] = normalized_files.pop(skill_md_key)
        manifest = self._parse_skill_md(normalized_files["SKILL.md"])
        resolved_name = (name or str(manifest.get("name") or "") or manifest.get("title") or "skill").strip()
        if not resolved_name:
            raise ValueError("skill name is required")
        slug_base = re.sub(r"[^a-z0-9]+", "-", resolved_name.lower()).strip("-")
        slug = slug_base or "skill-" + hashlib.sha1(resolved_name.encode("utf-8")).hexdigest()[:8]
        skill_id = "skill_" + hashlib.sha1(f"{target or 'global'}:{slug}".encode("utf-8")).hexdigest()[:16]
        resolved_status = status or str(manifest.get("status") or "candidate")
        if resolved_status not in {"candidate", "validated", "global"}:
            resolved_status = "candidate"
        steps = manifest.get("steps") or []
        if isinstance(steps, str):
            steps = [steps]
        steps = [str(step) for step in steps if str(step).strip()]
        written = self._write_skill_files(skill_id, normalized_files)
        raw_trigger = manifest.get("description") or manifest.get("triggers") or resolved_name
        trigger = " / ".join(str(x) for x in raw_trigger) if isinstance(raw_trigger, list) else str(raw_trigger)
        description = str(manifest.get("description") or "")
        now = utcnow()
        existing = self.conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE skills SET name = ?, slug = ?, trigger = ?, description = ?, steps_json = ?, "
                "manifest_json = ?, dir_path = ?, file_count = ?, status = ?, updated_at = ? WHERE id = ?",
                (
                    resolved_name,
                    slug,
                    trigger,
                    description,
                    json_dumps(steps),
                    json_dumps(manifest),
                    skill_id,
                    len(written),
                    resolved_status,
                    now,
                    skill_id,
                ),
            )
            action = "skill.update"
        else:
            self.conn.execute(
                "INSERT INTO skills(id, project_id, name, slug, trigger, description, steps_json, manifest_json, "
                "dir_path, file_count, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    skill_id,
                    target,
                    resolved_name,
                    slug,
                    trigger,
                    description,
                    json_dumps(steps),
                    json_dumps(manifest),
                    skill_id,
                    len(written),
                    resolved_status,
                    now,
                    now,
                ),
            )
            action = "skill.install"
        self._audit(action, skill_id, {"project_id": target, "files": len(written), "name": resolved_name})
        self.conn.commit()
        return {
            "skill_id": skill_id,
            "project_id": target,
            "name": resolved_name,
            "slug": slug,
            "status": resolved_status,
            "dir_path": skill_id,
            "file_count": len(written),
            "files": written,
        }

    def skill_add(
        self,
        context_version: str,
        *,
        name: str,
        trigger: str,
        steps: list[str],
        project_id: str | None = None,
        status: str = "candidate",
    ) -> dict[str, Any]:
        step_lines = "\n".join(f"  - {step}" for step in steps)
        skill_md = (
            "---\n"
            f"name: {json_dumps(name)}\n"
            f"description: {json_dumps(trigger)}\n"
            f"status: {status}\n"
            "steps:\n"
            f"{step_lines}\n"
            "---\n\n"
            f"# {name}\n\n"
            f"{trigger}\n"
        )
        result = self.skill_install(
            context_version,
            files={"SKILL.md": skill_md},
            name=name,
            project_id=project_id,
            status=status,
        )
        self.conn.execute(
            "UPDATE skills SET steps_json = ? WHERE id = ?",
            (json_dumps(steps), result["skill_id"]),
        )
        self.conn.commit()
        return result

    def skill_list(
        self,
        context_version: str,
        *,
        project_id: str | None = None,
        scope: str = "auto",
        query: str | None = None,
    ) -> dict[str, Any]:
        session = self.validate_context(context_version)
        if scope not in {"auto", "project", "global", "all"}:
            raise ValueError("scope must be auto, project, global or all")
        selected = project_id if project_id is not None else session["project_id"]
        rows = self.conn.execute("SELECT * FROM skills ORDER BY updated_at DESC").fetchall()
        items = []
        for row in rows:
            if scope == "all":
                allowed = True
            elif scope == "global":
                allowed = row["project_id"] is None
            elif scope == "project":
                allowed = row["project_id"] == selected
            else:
                allowed = row["project_id"] in {selected, None}
            if not allowed:
                continue
            if query:
                text = f"{row['name']}\n{row['trigger']}\n{' '.join(json_loads(row['steps_json'], []))}"
                if _score(query, text, updated_at=row["updated_at"], item_type="procedure") <= 0:
                    continue
            items.append(self._skill_dict(row))
        return {"items": items, "count": len(items), "scope": scope}

    def skill_get(self, context_version: str, skill_id: str) -> dict[str, Any]:
        session = self.validate_context(context_version)
        row = self.conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown skill: {skill_id}")
        if row["project_id"] not in {session["project_id"], None}:
            raise ValueError("skill is outside the current project scope")
        result = self._skill_dict(row)
        result["files"] = self._skill_files(skill_id)
        skill_md = self._skill_dir(skill_id) / "SKILL.md"
        result["skill_md"] = skill_md.read_text(encoding="utf-8") if skill_md.exists() else ""
        return result

    def skill_file(self, context_version: str, skill_id: str, path: str) -> dict[str, Any]:
        session = self.validate_context(context_version)
        row = self.conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown skill: {skill_id}")
        if row["project_id"] not in {session["project_id"], None}:
            raise ValueError("skill is outside the current project scope")
        safe = self._safe_skill_path(path)
        target = self._skill_dir(skill_id) / safe
        if not target.exists() or not target.is_file():
            raise ValueError(f"skill file not found: {safe}")
        if target.stat().st_size > 1024 * 1024:
            raise ValueError("skill file too large to return")
        return {"skill_id": skill_id, "path": safe, "content": target.read_text(encoding="utf-8")}

    def skill_save_file(
        self,
        context_version: str,
        skill_id: str,
        path: str,
        content: str,
    ) -> dict[str, Any]:
        session = self.validate_context(context_version)
        row = self.conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown skill: {skill_id}")
        if row["project_id"] not in {session["project_id"], None}:
            raise ValueError("skill is outside the current project scope")
        files: dict[str, str] = {}
        for item in self._skill_files(skill_id):
            target = self._skill_dir(skill_id) / item["path"]
            try:
                files[item["path"]] = target.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
        safe = self._safe_skill_path(path)
        files[safe] = content
        result = self.skill_install(
            context_version,
            files=files,
            name=row["name"],
            project_id=row["project_id"],
            status=row["status"],
        )
        return result

    def skill_delete(self, context_version: str, skill_id: str) -> dict[str, Any]:
        session = self.validate_context(context_version)
        row = self.conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown skill: {skill_id}")
        if row["project_id"] not in {session["project_id"], None}:
            raise ValueError("skill is outside the current project scope")
        self.conn.execute("DELETE FROM skills WHERE id = ?", (skill_id,))
        self._audit("skill.delete", skill_id, {"project_id": row["project_id"], "name": row["name"]})
        self.conn.commit()
        target = self._skill_dir(skill_id)
        if target.exists():
            shutil.rmtree(target)
        return {"skill_id": skill_id, "deleted": True}

    def skill_delete_file(self, context_version: str, skill_id: str, path: str) -> dict[str, Any]:
        session = self.validate_context(context_version)
        row = self.conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown skill: {skill_id}")
        if row["project_id"] not in {session["project_id"], None}:
            raise ValueError("skill is outside the current project scope")
        safe = self._safe_skill_path(path)
        if safe == "SKILL.md":
            raise ValueError("SKILL.md cannot be deleted; edit it instead")
        target = self._skill_dir(skill_id) / safe
        if not target.exists() or not target.is_file():
            raise ValueError(f"skill file not found: {safe}")
        target.unlink()
        files: dict[str, str] = {}
        for item in self._skill_files(skill_id):
            file_path = self._skill_dir(skill_id) / item["path"]
            try:
                files[item["path"]] = file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
        self.skill_install(
            context_version,
            files=files,
            name=row["name"],
            project_id=row["project_id"],
            status=row["status"],
        )
        return {"skill_id": skill_id, "path": safe, "deleted": True}

    # ------------------------------------------------------------------
    # project context / status / export
    # ------------------------------------------------------------------
    def project_context(self, context_version: str) -> dict[str, Any]:
        session = self.validate_context(context_version)
        project = self.get_project(session["project_id"])
        if project is None:
            return {"project": None, "directives": [], "decisions": [], "lessons": []}
        recent = self._recent_items(project["id"])
        return {
            "project": project,
            "directives": [d["content"] for d in self.list_directives(project["id"])],
            "decisions": recent["decisions"],
            "lessons": recent["lessons"],
        }

    def audit_list(self, limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        rows = self.conn.execute(
            "SELECT id, at, actor, action, entity, detail_json FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        items = []
        for row in rows:
            items.append(
                {
                    "id": row["id"],
                    "at": row["at"],
                    "actor": row["actor"],
                    "action": row["action"],
                    "entity": row["entity"],
                    "detail": json_loads(row["detail_json"], {}),
                }
            )
        return {"items": items, "count": len(items)}

    def status(self) -> dict[str, Any]:
        counts = {}
        for table in ("projects", "memories", "documents", "skills", "directives", "episodes", "sessions"):
            row = self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
            counts[table] = int(row["n"])
        for status in ("active", "candidate", "rejected", "deleted"):
            row = self.conn.execute("SELECT COUNT(*) AS n FROM memories WHERE status = ?", (status,)).fetchone()
            counts[f"memories_{status}"] = int(row["n"])
        return {
            "db": self.db_path,
            "identity": self.get_identity(),
            "counts": counts,
            "seed": self._meta("seed", "0") == "1",
        }

    def export_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"format": "membridge-export-v1", "exported_at": utcnow()}
        for table in ("identity", "projects", "directives", "episodes", "memories", "documents", "skills"):
            data[table] = [dict(r) for r in self.conn.execute(f"SELECT * FROM {table}").fetchall()]
        skill_files: dict[str, dict[str, str]] = {}
        for row in data.get("skills", []):
            skill_id = row.get("id")
            if not skill_id:
                continue
            files = {}
            for item in self._skill_files(skill_id):
                target = self._skill_dir(skill_id) / item["path"]
                files[item["path"]] = target.read_text(encoding="utf-8")
            if files:
                skill_files[skill_id] = files
        data["skill_files"] = skill_files
        return data

    def import_data(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("format") != "membridge-export-v1":
            raise ValueError("unsupported import format")
        imported = 0
        identity_rows = payload.get("identity") or []
        if identity_rows:
            row = identity_rows[0]
            self.conn.execute(
                "UPDATE identity SET name = ?, persona = ?, style_json = ?, boundaries_json = ?, "
                "version = ?, updated_at = ? WHERE id = 1",
                (
                    row.get("name", "默认"),
                    row.get("persona", ""),
                    row.get("style_json", "{}"),
                    row.get("boundaries_json", "[]"),
                    int(row.get("version", 1)),
                    row.get("updated_at", utcnow()),
                ),
            )
            imported += 1
        for table in ("projects", "directives", "episodes", "memories", "documents", "skills"):
            rows = payload.get(table, [])
            if not rows:
                continue
            columns = list(rows[0].keys())
            placeholders = ",".join("?" for _ in columns)
            sql = f"INSERT OR IGNORE INTO {table}({','.join(columns)}) VALUES ({placeholders})"
            for row in rows:
                self.conn.execute(sql, [row.get(c) for c in columns])
                imported += 1
        for skill_id, files in (payload.get("skill_files") or {}).items():
            if not isinstance(files, dict) or not files:
                continue
            written = self._write_skill_files(skill_id, files)
            self.conn.execute(
                "UPDATE skills SET dir_path = ?, file_count = ? WHERE id = ?",
                (skill_id, len(written), skill_id),
            )
        self._audit("import", None, {"imported": imported})
        self.conn.commit()
        return {"imported": imported}

