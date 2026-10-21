"""Command line interface for MemoryBridge."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .db import utcnow
from .mcp_server import run_stdio
from .seed import seed as seed_data
from .service import AgentSyncService


DEFAULT_DB = os.environ.get("MEMBRIDGE_DB", "membridge.db")


def _print(data, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                print(f"{key}:")
                print(json.dumps(value, ensure_ascii=False, indent=2))
            else:
                print(f"{key}: {value}")
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


def _service(args) -> AgentSyncService:
    return AgentSyncService(args.db)


def cmd_init(args) -> int:
    svc = _service(args)
    print(f"initialized: {Path(args.db).resolve()}")
    svc.close()
    return 0


def cmd_seed(args) -> int:
    svc = _service(args)
    result = seed_data(svc)
    _print(result, args.json)
    svc.close()
    return 0


def cmd_status(args) -> int:
    svc = _service(args)
    _print(svc.status(), args.json)
    svc.close()
    return 0


def cmd_bootstrap(args) -> int:
    svc = _service(args)
    result = svc.bootstrap(project_path=args.project_path, agent_id=args.agent_id, client=args.client)
    _print(result, True)
    svc.close()
    return 0


def cmd_persona(args) -> int:
    svc = _service(args)
    if args.persona_command == "show":
        _print(svc.get_identity(), True)
    else:
        boundaries = args.boundary if args.boundary else None
        identity = svc.set_identity(
            name=args.name,
            persona=args.persona,
            tone=args.tone,
            suffix=args.suffix,
            address=args.address,
            language=args.language,
            boundaries=boundaries,
        )
        _print(identity, True)
    svc.close()
    return 0


def cmd_directive(args) -> int:
    svc = _service(args)
    if args.directive_command == "list":
        _print({"items": svc.list_directives(None)}, True)
    else:
        result = svc.add_directive(
            args.content,
            scope=args.scope,
            project_path=args.project_path,
            priority=args.priority,
        )
        _print(result, True)
    svc.close()
    return 0


def cmd_remember(args) -> int:
    svc = _service(args)
    bootstrap = svc.bootstrap(project_path=args.project_path, client="cli")
    result = svc.memory_add(
        bootstrap["context_version"],
        args.content,
        type=args.memory_type,
        confidence=args.confidence,
        source="cli",
    )
    result["context_version"] = bootstrap["context_version"]
    _print(result, True)
    svc.close()
    return 0


def cmd_search(args) -> int:
    svc = _service(args)
    bootstrap = svc.bootstrap(project_path=args.project_path, client="cli")
    result = svc.memory_search(
        bootstrap["context_version"],
        args.query,
        scope=args.scope,
        k=args.k,
    )
    _print(result, args.json)
    svc.close()
    return 0


def cmd_docs(args) -> int:
    svc = _service(args)
    if args.docs_command == "add":
        file_path = Path(args.file).expanduser()
        content = file_path.read_text(encoding="utf-8")
        bootstrap = svc.bootstrap(project_path=args.project_path, client="cli")
        result = svc.doc_add(
            bootstrap["context_version"],
            path=args.path or file_path.name,
            content=content,
            title=args.title,
        )
    elif args.docs_command == "search":
        bootstrap = svc.bootstrap(project_path=args.project_path, client="cli")
        result = svc.doc_search(bootstrap["context_version"], args.query, k=args.k)
    elif args.docs_command == "read":
        bootstrap = svc.bootstrap(project_path=args.project_path, client="cli")
        result = svc.doc_read(bootstrap["context_version"], args.document_id)
    else:
        raise SystemExit("unknown docs command")
    _print(result, True)
    svc.close()
    return 0


def cmd_skills(args) -> int:
    svc = _service(args)
    if args.skills_command == "list":
        bootstrap = svc.bootstrap(project_path=args.project_path, client="cli")
        result = svc.skill_list(bootstrap["context_version"], query=args.query)
    elif args.skills_command == "get":
        bootstrap = svc.bootstrap(project_path=args.project_path, client="cli")
        result = svc.skill_get(bootstrap["context_version"], args.skill_id)
    elif args.skills_command == "add":
        bootstrap = svc.bootstrap(project_path=args.project_path, client="cli")
        result = svc.skill_add(
            bootstrap["context_version"],
            name=args.name,
            trigger=args.trigger,
            steps=args.step or [],
        )
    else:
        raise SystemExit("unknown skills command")
    _print(result, True)
    svc.close()
    return 0


def cmd_serve(args) -> int:
    svc = _service(args)
    run_stdio(svc, default_project=args.project)
    svc.close()
    return 0


def cmd_export(args) -> int:
    svc = _service(args)
    data = svc.export_data()
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"exported: {out.resolve()}")
    svc.close()
    return 0


def cmd_import(args) -> int:
    svc = _service(args)
    data = json.loads(Path(args.input).expanduser().read_text(encoding="utf-8"))
    result = svc.import_data(data)
    _print(result, True)
    svc.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="membridge", description="Portable agent identity/memory sync over MCP")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite database path (default: {DEFAULT_DB})")
    parser.add_argument("--json", action="store_true", help="print JSON where supported")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="initialize the database")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("seed", help="insert demo identity, memories, docs and skills")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("status", help="show counts and identity")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("bootstrap", help="simulate an agent bootstrap and print the context pack")
    p.add_argument("--project-path", default=None)
    p.add_argument("--agent-id", default=None)
    p.add_argument("--client", default="cli")
    p.set_defaults(func=cmd_bootstrap)

    p = sub.add_parser("serve", help="run the MCP stdio server")
    p.add_argument("--project", default=None, help="default project path when agent_bootstrap has no project_path")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("persona", help="show or set the identity/persona")
    p_sub = p.add_subparsers(dest="persona_command", required=True)
    show = p_sub.add_parser("show")
    show.set_defaults(func=cmd_persona, persona_command="show")
    st = p_sub.add_parser("set")
    st.add_argument("--name")
    st.add_argument("--persona")
    st.add_argument("--tone")
    st.add_argument("--suffix")
    st.add_argument("--address")
    st.add_argument("--language")
    st.add_argument("--boundary", action="append")
    st.set_defaults(func=cmd_persona, persona_command="set")

    p = sub.add_parser("directive", help="list or add standing directives")
    p_sub = p.add_subparsers(dest="directive_command", required=True)
    ls = p_sub.add_parser("list")
    ls.set_defaults(func=cmd_directive, directive_command="list")
    add = p_sub.add_parser("add")
    add.add_argument("content")
    add.add_argument("--scope", choices=["global", "project"], default="global")
    add.add_argument("--project-path", default=None)
    add.add_argument("--priority", type=int, default=50)
    add.set_defaults(func=cmd_directive, directive_command="add")

    p = sub.add_parser("remember", help="store a memory in the current/global scope")
    p.add_argument("content")
    p.add_argument("--type", dest="memory_type", choices=["fact", "preference", "decision", "lesson", "procedure", "reference"], default="fact")
    p.add_argument("--project-path", default=None)
    p.add_argument("--confidence", type=float, default=0.8)
    p.set_defaults(func=cmd_remember)

    p = sub.add_parser("search", help="search memory")
    p.add_argument("query")
    p.add_argument("--project-path", default=None)
    p.add_argument("--scope", choices=["auto", "project", "global"], default="auto")
    p.add_argument("--k", type=int, default=8)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("docs", help="documents")
    p_sub = p.add_subparsers(dest="docs_command", required=True)
    add = p_sub.add_parser("add")
    add.add_argument("file")
    add.add_argument("--project-path", default=None)
    add.add_argument("--path", default=None)
    add.add_argument("--title", default=None)
    add.set_defaults(func=cmd_docs, docs_command="add")
    search = p_sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--project-path", default=None)
    search.add_argument("--k", type=int, default=5)
    search.set_defaults(func=cmd_docs, docs_command="search")
    read = p_sub.add_parser("read")
    read.add_argument("document_id")
    read.add_argument("--project-path", default=None)
    read.set_defaults(func=cmd_docs, docs_command="read")

    p = sub.add_parser("skills", help="skills/procedures")
    p_sub = p.add_subparsers(dest="skills_command", required=True)
    ls = p_sub.add_parser("list")
    ls.add_argument("--project-path", default=None)
    ls.add_argument("--query", default=None)
    ls.set_defaults(func=cmd_skills, skills_command="list")
    get = p_sub.add_parser("get")
    get.add_argument("skill_id")
    get.add_argument("--project-path", default=None)
    get.set_defaults(func=cmd_skills, skills_command="get")
    add = p_sub.add_parser("add")
    add.add_argument("name")
    add.add_argument("--trigger", required=True)
    add.add_argument("--step", action="append")
    add.add_argument("--project-path", default=None)
    add.set_defaults(func=cmd_skills, skills_command="add")

    p = sub.add_parser("export", help="export all data as JSON")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("import", help="import a JSON export")
    p.add_argument("--in", dest="input", required=True)
    p.set_defaults(func=cmd_import)

    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raw = list(sys.argv[1:] if argv is None else argv)
    json_flag = "--json" in raw
    raw = [item for item in raw if item != "--json"]
    parser = build_parser()
    args = parser.parse_args(raw)
    args.json = json_flag or bool(getattr(args, "json", False))
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
