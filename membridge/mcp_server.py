"""Minimal MCP stdio server for MemoryBridge.

The server intentionally speaks JSON-RPC 2.0 directly (newline-delimited) so it
can run with a stock Python interpreter and no third-party MCP SDK.  It exposes
tools, resources and one prompt; the bootstrap gate lives in the service layer.
"""

from __future__ import annotations

import json
import sys
import traceback

from . import __version__
from typing import Any

from .service import AgentSyncService, GateError

PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}

INSTRUCTIONS = (
    "MemoryBridge is the user's portable agent identity, memory, document and capability service. "
    "At the start of EVERY new conversation, before answering or using any other MemoryBridge tool, "
    "call `agent_bootstrap`. It returns the persona, global directives, project context, "
    "`projects`, auto-injected `global_memories`/`project_memories`, and a `context_version`. "
    "Pass that `context_version` to every subsequent memory, document and skill tool. "
    "If you skip bootstrap, those tools return BOOTSTRAP_REQUIRED. "
    "Use `project_list` to discover projects; `memory_search(scope='auto')` searches current project + global, "
    "while `scope='all'` searches across all projects. `memory_list(project_id=...)` batch-reads a known project. "
    "Documents follow the same scope model: `doc_search(scope='all')` / `doc_list(scope='all')` search across projects; "
    "`doc_search` returns snippets, so call `doc_read` for the full document. "
    "The web console is also editable (memory/document/skill/identity CRUD), but you remain responsible "
    "for maintaining durable context: when the user states a durable requirement, decision, "
    "lesson or preference, call `memory_write` directly with a stable `topic`, the right `scope` "
    "(project by default; global only for user-wide preferences), and a `rationale` plus `conditions`. "
    "Set `pinned=true` for core user-wide facts that must be auto-injected in every future conversation. "
    "Before ending a turn, scan the conversation for durable items. If the user says remember/save/don't forget, "
    "or states a stable UI/output preference, write it with `memory_write` before your final answer. "
    "If the user asks for a document or procedure, use `doc_add` / `skill_install` (or `skill_save_file`) "
    "and report the returned id; do not merely describe what you would save. "
    "Route content correctly: memory = compact always-on facts/preferences/decisions (1-3 sentences); "
    "document = long-form design/spec/ADR/notes (not auto-injected; search then read); "
    "skill = executable folder with SKILL.md and scripts/references. "
    "If a long document contains a conclusion that must always be known, store a short memory pointer."
    "Do not create a candidate or ask the user to approve it. Use `doc_add` for documents, "
    "`skill_install`/`skill_file` for folder-shaped skills, and `identity_update` for persona changes. "
    "Put tone, tics and verbal habits inside `persona`; there is no separate sentence_end field. "
    "A memory must be usable in context: say which project/condition it applies to, not a bare universal claim."
)


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


CTX = {"type": "string", "description": "The context_version returned by agent_bootstrap."}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "agent_bootstrap",
        "title": "Bootstrap this agent",
        "description": (
            "MUST be called first in every new conversation. Returns the user's persona, "
            "global directives, current project context, available memory counts and a context_version. "
            "Do not use any other MemoryBridge tool before this call."
        ),
        "inputSchema": _schema(
            {
                "project_path": {"type": "string", "description": "Absolute path of the current project/workspace."},
                "agent_id": {"type": "string", "description": "Stable agent identifier; defaults to the stored agent id."},
                "client": {"type": "string", "description": "Client name, e.g. claude-code, cursor, dsh."},
            }
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "agent_whoami",
        "title": "Who am I",
        "description": "Return the stored identity without requiring a bootstrap (safe, read-only).",
        "inputSchema": _schema({}),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "agent_status",
        "title": "Status",
        "description": "Return database path, identity and row counts (safe, read-only).",
        "inputSchema": _schema({}),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "memory_search",
        "title": "Search memory",
        "description": (
            "Hybrid search (BM25 + vector, RRF-fused, salience-weighted) over active memories. "
            "scope=auto (current project + global), project, global, or all (cross-project). "
            "Each hit carries a scores breakdown (lex_rank/vec_rank/salience). Requires agent_bootstrap first."
        ),
        "inputSchema": _schema(
            {
                "context_version": CTX,
                "query": {"type": "string"},
                "project_id": {"type": "string"},
                "scope": {"type": "string", "enum": ["auto", "project", "global", "all"]},
                "k": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            ["context_version", "query"],
        ),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "memory_add",
        "title": "Add memory",
        "description": "Store a durable fact, preference, decision, lesson or procedure. Requires bootstrap.",
        "inputSchema": _schema(
            {
                "context_version": CTX,
                "content": {"type": "string"},
                "type": {"type": "string", "enum": ["fact", "preference", "decision", "lesson", "procedure", "reference"]},
                "project_id": {"type": "string", "description": "Project id; omit for current project, use 'global' for global."},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "source": {"type": "string"},
            },
            ["context_version", "content"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "memory_write",
        "title": "Write memory directly",
        "description": (
            "The normal AI-facing write path: immediately upsert an ACTIVE memory. No candidate/approval queue. "
            "Use a stable topic (e.g. decision.storage.sqlite) so a later correction updates the same memory. "
            "Default scope is the current project; use scope=global only for user-wide preferences. "
            "rationale is required for decision/lesson; conditions records where it applies. Requires bootstrap."
        ),
        "inputSchema": _schema(
            {
                "context_version": CTX,
                "content": {"type": "string"},
                "topic": {"type": "string", "description": "Stable lowercase key, e.g. decision.storage.sqlite"},
                "type": {"type": "string", "enum": ["fact", "preference", "decision", "lesson", "procedure", "reference"]},
                "scope": {"type": "string", "enum": ["project", "global"]},
                "project_id": {"type": "string"},
                "rationale": {"type": "string"},
                "conditions": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "source": {"type": "string"},
                "evidence_quote": {"type": "string"},
                "pinned": {"type": "boolean", "description": "Inject into future bootstrap context; use for catgirl core preferences."},
                "importance": {"type": "integer", "minimum": 1, "maximum": 5, "description": "1=trivia, 5=must never forget. Defaults by type."},
                "force": {"type": "boolean", "description": "Write even when a nearly identical memory exists (cosine >= 0.92)."},
            },
            ["context_version", "content", "topic"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "identity_update",
        "title": "Update identity",
        "description": (
            "Let the AI update the user's portable persona/identity (name, persona, address, language, boundaries). "
            "Put tone/tic/verbal habits inside persona; do not add a separate sentence_end field. Requires bootstrap."
        ),
        "inputSchema": _schema(
            {
                "context_version": CTX,
                "name": {"type": "string"},
                "persona": {"type": "string"},
                "address_user": {"type": "string"},
                "language": {"type": "string"},
                "boundaries": {"type": "array", "items": {"type": "string"}},
            },
            ["context_version"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "audit_list",
        "title": "Recent activity",
        "description": "Read-only recent memory/document/skill/identity writes (the UI activity view).",
        "inputSchema": _schema({"limit": {"type": "integer", "minimum": 1, "maximum": 200}}),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "memory_list",
        "title": "List memory",
        "description": "List active memory in the current project + global (scope=auto), project, global, or all. Requires bootstrap.",
        "inputSchema": _schema(
            {
                "context_version": CTX,
                "project_id": {"type": "string"},
                "scope": {"type": "string", "enum": ["auto", "project", "global", "all"]},
                "status": {"type": "string", "enum": ["active", "candidate", "rejected", "deleted", "all"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            ["context_version"],
        ),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "memory_update",
        "title": "Update memory",
        "description": "Update a memory's content, confidence or importance (1-5); bumps version and re-indexes. Requires bootstrap.",
        "inputSchema": _schema(
            {
                "context_version": CTX,
                "memory_id": {"type": "string"},
                "content": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "importance": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            ["context_version", "memory_id"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "memory_archive",
        "title": "Archive memory",
        "description": (
            "Move a memory to status=archived (recoverable with memory_restore). "
            "Use instead of deleting when a fact is outdated but historically useful. Requires bootstrap."
        ),
        "inputSchema": _schema(
            {
                "context_version": CTX,
                "memory_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            ["context_version", "memory_id"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "memory_restore",
        "title": "Restore memory",
        "description": "Bring a deleted or archived memory back to active and re-index it. Requires bootstrap.",
        "inputSchema": _schema({"context_version": CTX, "memory_id": {"type": "string"}}, ["context_version", "memory_id"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "memory_maintain",
        "title": "Run forgetting pass",
        "description": (
            "Backfill the vector/BM25 index and archive stale low-value memories (decay-based, never deletes, "
            "never touches pinned/decision/lesson/preference). dry_run=true only reports. Requires bootstrap."
        ),
        "inputSchema": _schema(
            {
                "context_version": CTX,
                "dry_run": {"type": "boolean"},
                "threshold": {"type": "number", "minimum": 0, "maximum": 1},
            },
            ["context_version"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "memory_stats",
        "title": "Memory statistics",
        "description": "Counts by status/type, average content size and index coverage (read-only). Requires bootstrap.",
        "inputSchema": _schema({"context_version": CTX}, ["context_version"]),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "memory_delete",
        "title": "Delete memory",
        "description": "Soft-delete a memory (tombstone); it stops appearing in search. Requires bootstrap.",
        "inputSchema": _schema({"context_version": CTX, "memory_id": {"type": "string"}}, ["context_version", "memory_id"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
    },
    {
        "name": "memory_feedback",
        "title": "Memory feedback",
        "description": "Record whether a memory was helpful or harmful. Requires bootstrap.",
        "inputSchema": _schema(
            {
                "context_version": CTX,
                "memory_id": {"type": "string"},
                "signal": {"type": "string", "enum": ["helpful", "harmful", "used"]},
            },
            ["context_version", "memory_id"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "doc_search",
        "title": "Search documents",
        "description": "Search Markdown/ADR documents. scope=auto (current project + global), project, global, or all. Returns snippets; call doc_read for full content. Requires bootstrap.",
        "inputSchema": _schema(
            {"context_version": CTX, "query": {"type": "string"}, "project_id": {"type": "string"}, "scope": {"type": "string", "enum": ["auto", "project", "global", "all"]}, "k": {"type": "integer", "minimum": 1, "maximum": 20}},
            ["context_version", "query"],
        ),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "doc_list",
        "title": "List documents",
        "description": "List documents. scope=auto (current project + global), project, global, or all. Requires bootstrap.",
        "inputSchema": _schema(
            {"context_version": CTX, "project_id": {"type": "string"}, "scope": {"type": "string", "enum": ["auto", "project", "global", "all"]}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}},
            ["context_version"],
        ),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "doc_read",
        "title": "Read document",
        "description": "Read a document by document_id (or path). scope=all allows a document from another project. Requires bootstrap.",
        "inputSchema": _schema({"context_version": CTX, "document_id": {"type": "string"}, "scope": {"type": "string", "enum": ["auto", "project", "global", "all"]}}, ["context_version", "document_id"]),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "doc_add",
        "title": "Add/update document",
        "description": "Attach a Markdown document to the current project, or pass project_id='global' for a shared document. Requires bootstrap.",
        "inputSchema": _schema(
            {
                "context_version": CTX,
                "path": {"type": "string"},
                "content": {"type": "string"},
                "title": {"type": "string"},
                "project_id": {"type": "string"},
            },
            ["context_version", "path", "content"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "skill_list",
        "title": "List skills",
        "description": "List folder-shaped skills. scope=auto (current project + global), project, global, or all. Requires bootstrap.",
        "inputSchema": _schema({"context_version": CTX, "project_id": {"type": "string"}, "scope": {"type": "string", "enum": ["auto", "project", "global", "all"]}, "query": {"type": "string"}}, ["context_version"]),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "skill_get",
        "title": "Get skill",
        "description": "Read one skill folder (manifest, file list and SKILL.md). Use skill_file for scripts/references. Requires bootstrap.",
        "inputSchema": _schema({"context_version": CTX, "skill_id": {"type": "string"}}, ["context_version", "skill_id"]),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "skill_add",
        "title": "Add skill",
        "description": "Legacy text-only skill helper; prefer skill_install for a real folder package. Requires bootstrap.",
        "inputSchema": _schema(
            {
                "context_version": CTX,
                "name": {"type": "string"},
                "trigger": {"type": "string"},
                "steps": {"type": "array", "items": {"type": "string"}},
                "project_id": {"type": "string"},
            },
            ["context_version", "name", "trigger", "steps"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "skill_install",
        "title": "Install skill folder",
        "description": (
            "Install a folder-shaped skill package: a map of relative path -> text content, "
            "which MUST include SKILL.md (YAML frontmatter + body). Skills are global by default; "
            "pass project_id for a repository-specific skill. Files are stored under the skill directory. "
            "Requires bootstrap."
        ),
        "inputSchema": _schema(
            {
                "context_version": CTX,
                "files": {"type": "object", "additionalProperties": {"type": "string"}},
                "name": {"type": "string"},
                "project_id": {"type": "string"},
                "status": {"type": "string", "enum": ["candidate", "validated", "global"]},
            },
            ["context_version", "files"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "doc_delete",
        "title": "Delete document",
        "description": "Delete a document by document_id (or path). Requires bootstrap.",
        "inputSchema": _schema({"context_version": CTX, "document_id": {"type": "string"}}, ["context_version", "document_id"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
    },
    {
        "name": "skill_file",
        "title": "Read skill file",
        "description": "Read one file from a skill folder (scripts/references/SKILL.md). Requires bootstrap.",
        "inputSchema": _schema(
            {"context_version": CTX, "skill_id": {"type": "string"}, "path": {"type": "string"}},
            ["context_version", "skill_id", "path"],
        ),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "skill_save_file",
        "title": "Save skill file",
        "description": "Edit one text file inside a skill folder. Requires bootstrap.",
        "inputSchema": _schema(
            {"context_version": CTX, "skill_id": {"type": "string"}, "path": {"type": "string"}, "content": {"type": "string"}},
            ["context_version", "skill_id", "path", "content"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "skill_delete_file",
        "title": "Delete skill file",
        "description": "Delete one file from a skill folder (SKILL.md is protected). Requires bootstrap.",
        "inputSchema": _schema(
            {"context_version": CTX, "skill_id": {"type": "string"}, "path": {"type": "string"}},
            ["context_version", "skill_id", "path"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
    },
    {
        "name": "skill_delete",
        "title": "Delete skill",
        "description": "Delete a skill folder and its DB row. Requires bootstrap.",
        "inputSchema": _schema({"context_version": CTX, "skill_id": {"type": "string"}}, ["context_version", "skill_id"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
    },
    {
        "name": "project_delete",
        "title": "Soft-delete project",
        "description": "Soft-delete a project so it disappears from project_list/bootstrap projects. Its data is retained. Requires bootstrap.",
        "inputSchema": _schema({"context_version": CTX, "project_id": {"type": "string"}}, ["context_version", "project_id"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
    },
    {
        "name": "project_list",
        "title": "List projects",
        "description": "List all known projects with ids, keys, paths and memory/document/skill counts. Use this to discover project_id for batch reads. Requires bootstrap.",
        "inputSchema": _schema({"context_version": CTX}, ["context_version"]),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "project_context",
        "title": "Project context",
        "description": "Return the current project's conventions, decisions, lessons and directives. Requires bootstrap.",
        "inputSchema": _schema({"context_version": CTX}, ["context_version"]),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False},
    },
]

TOOL_BY_NAME = {tool["name"]: tool for tool in TOOLS}


class MethodNotFound(Exception):
    pass


def _ok(data: Any) -> dict[str, Any]:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": False}
    if isinstance(data, dict):
        result["structuredContent"] = data
    return result


def _err(message: str, *, code: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": message}], "isError": True}
    if code:
        result["structuredContent"] = {"code": code, "message": message}
    return result


def dispatch(service: AgentSyncService, name: str, args: dict[str, Any], default_project: str | None = None) -> dict[str, Any]:
    if name == "agent_bootstrap":
        project_path = args.get("project_path") or default_project
        return _ok(
            service.bootstrap(
                project_path=project_path,
                agent_id=args.get("agent_id"),
                client=args.get("client", "mcp"),
            )
        )
    if name == "agent_whoami":
        identity = service.get_identity()
        return _ok({"agent_id": service.get_agent_id(), "identity": identity})
    if name == "agent_status":
        return _ok(service.status())
    if name == "audit_list":
        return _ok(service.audit_list(int(args.get("limit", 50))))

    ctx = args.get("context_version")
    if name == "memory_write":
        return _ok(
            service.memory_write(
                ctx,
                args["content"],
                type=args.get("type", "fact"),
                topic=args["topic"],
                scope=args.get("scope", "project"),
                project_id=args.get("project_id"),
                rationale=args.get("rationale", ""),
                conditions=args.get("conditions", ""),
                confidence=float(args.get("confidence", 0.85)),
                source=args.get("source", "agent"),
                evidence_quote=args.get("evidence_quote"),
                pinned=args.get("pinned"),
                importance=args.get("importance"),
                force=bool(args.get("force", False)),
            )
        )
    if name == "identity_update":
        return _ok(
            service.identity_update(
                ctx,
                name=args.get("name"),
                persona=args.get("persona"),
                address_user=args.get("address_user"),
                language=args.get("language"),
                boundaries=args.get("boundaries"),
            )
        )
    if name == "memory_search":
        return _ok(
            service.memory_search(
                ctx,
                args["query"],
                project_id=args.get("project_id"),
                scope=args.get("scope", "auto"),
                k=int(args.get("k", 8)),
            )
        )
    if name == "memory_add":
        return _ok(
            service.memory_add(
                ctx,
                args["content"],
                type=args.get("type", "fact"),
                project_id=args.get("project_id"),
                confidence=float(args.get("confidence", 0.8)),
                source=args.get("source", "agent"),
            )
        )
    if name == "memory_list":
        return _ok(
            service.memory_list(
                ctx,
                project_id=args.get("project_id"),
                scope=args.get("scope", "auto"),
                status=args.get("status", "active"),
                limit=int(args.get("limit", 50)),
            )
        )
    if name == "memory_update":
        return _ok(
            service.memory_update(
                ctx,
                args["memory_id"],
                content=args.get("content"),
                confidence=args.get("confidence"),
                importance=args.get("importance"),
            )
        )
    if name == "memory_archive":
        return _ok(service.memory_archive(ctx, args["memory_id"], reason=args.get("reason", "manual")))
    if name == "memory_restore":
        return _ok(service.memory_restore(ctx, args["memory_id"]))
    if name == "memory_maintain":
        return _ok(
            service.memory_maintain(
                ctx,
                dry_run=bool(args.get("dry_run", False)),
                threshold=args.get("threshold"),
            )
        )
    if name == "memory_stats":
        return _ok(service.memory_stats(ctx))
    if name == "memory_delete":
        return _ok(service.memory_delete(ctx, args["memory_id"]))
    if name == "memory_feedback":
        return _ok(service.memory_feedback(ctx, args["memory_id"], args.get("signal", "helpful")))
    if name == "doc_search":
        return _ok(
            service.doc_search(
                ctx,
                args["query"],
                project_id=args.get("project_id"),
                scope=args.get("scope", "auto"),
                k=int(args.get("k", 5)),
            )
        )
    if name == "doc_list":
        return _ok(
            service.doc_list(
                ctx,
                project_id=args.get("project_id"),
                scope=args.get("scope", "auto"),
                limit=int(args.get("limit", 100)),
            )
        )
    if name == "doc_read":
        return _ok(service.doc_read(ctx, args["document_id"], scope=args.get("scope", "auto")))
    if name == "doc_add":
        return _ok(
            service.doc_add(
                ctx,
                path=args["path"],
                content=args["content"],
                title=args.get("title"),
                project_id=args.get("project_id"),
            )
        )
    if name == "doc_delete":
        return _ok(service.doc_delete(ctx, args["document_id"]))
    if name == "skill_list":
        return _ok(service.skill_list(ctx, project_id=args.get("project_id"), scope=args.get("scope", "auto"), query=args.get("query")))
    if name == "skill_get":
        return _ok(service.skill_get(ctx, args["skill_id"]))
    if name == "skill_add":
        return _ok(
            service.skill_add(
                ctx,
                name=args["name"],
                trigger=args["trigger"],
                steps=list(args.get("steps", [])),
                project_id=args.get("project_id"),
            )
        )
    if name == "skill_install":
        return _ok(
            service.skill_install(
                ctx,
                files=dict(args["files"]),
                name=args.get("name"),
                project_id=args.get("project_id"),
                status=args.get("status"),
            )
        )
    if name == "skill_file":
        return _ok(service.skill_file(ctx, args["skill_id"], args["path"]))
    if name == "skill_save_file":
        return _ok(service.skill_save_file(ctx, args["skill_id"], args["path"], args["content"]))
    if name == "skill_delete_file":
        return _ok(service.skill_delete_file(ctx, args["skill_id"], args["path"]))
    if name == "skill_delete":
        return _ok(service.skill_delete(ctx, args["skill_id"]))
    if name == "project_delete":
        return _ok(service.project_delete(ctx, args["project_id"]))
    if name == "project_list":
        return _ok(service.project_list())
    if name == "project_context":
        return _ok(service.project_context(ctx))
    return _err(f"Unknown tool: {name}")


def call_tool(service: AgentSyncService, name: str, args: dict[str, Any], default_project: str | None = None) -> dict[str, Any]:
    if name not in TOOL_BY_NAME:
        return _err(f"Unknown tool: {name}")
    try:
        return dispatch(service, name, args, default_project)
    except GateError as exc:
        return _err(f"{exc.code}: {exc}", code=exc.code)
    except (KeyError, TypeError, ValueError) as exc:
        return _err(f"Invalid arguments: {exc}")
    except Exception as exc:  # pragma: no cover - defensive, surfaced to the model
        traceback.print_exc(file=sys.stderr)
        return _err(f"Internal error: {type(exc).__name__}: {exc}")


def handle_request(service: AgentSyncService, req: dict[str, Any], default_project: str | None = None) -> dict[str, Any] | None:
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        requested = params.get("protocolVersion")
        protocol = requested if requested in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION
        return {
            "protocolVersion": protocol,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
                "prompts": {"listChanged": False},
            },
            "serverInfo": {"name": "membridge", "version": __version__},
            "instructions": INSTRUCTIONS,
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        return call_tool(service, name, args, default_project)
    if method == "resources/list":
        return {
            "resources": [
                {
                    "uri": "memory://identity",
                    "name": "identity",
                    "title": "Current MemoryBridge identity",
                    "description": "The persona and boundaries stored for this user.",
                    "mimeType": "application/json",
                }
            ]
        }
    if method == "resources/read":
        uri = params.get("uri", "")
        if uri == "memory://identity":
            data = service.get_identity()
            return {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(data, ensure_ascii=False, indent=2)}]}
        return {"contents": []}
    if method == "prompts/list":
        return {
            "prompts": [
                {
                    "name": "recall_for_task",
                    "title": "Recall for task",
                    "description": "Bootstrap this agent and recall context relevant to a task.",
                    "arguments": [{"name": "task", "description": "What you are about to do", "required": True}],
                }
            ]
        }
    if method == "prompts/get":
        prompt_name = params.get("name")
        if prompt_name != "recall_for_task":
            return {"description": "Unknown prompt", "messages": []}
        task = (params.get("arguments") or {}).get("task", "the current task")
        return {
            "description": "Use MemoryBridge before starting",
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": f"Call agent_bootstrap first. Then search MemoryBridge for context relevant to: {task}. Cite memory ids when you use them.",
                    },
                }
            ],
        }
    if req_id is None:
        return None
    raise MethodNotFound(f"Unknown method: {method}")


def run_stdio(service: AgentSyncService, default_project: str | None = None) -> None:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}), flush=True)
            continue
        if "id" not in req:
            continue
        try:
            result = handle_request(service, req, default_project)
            if result is None:
                continue
            resp = {"jsonrpc": "2.0", "id": req["id"], "result": result}
        except MethodNotFound as exc:
            resp = {
                "jsonrpc": "2.0",
                "id": req["id"],
                "error": {"code": -32601, "message": str(exc)},
            }
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            resp = {
                "jsonrpc": "2.0",
                "id": req["id"],
                "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"},
            }
        print(json.dumps(resp, ensure_ascii=False), flush=True)
