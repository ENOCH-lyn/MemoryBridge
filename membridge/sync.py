"""Local project sync: docs, folder-shaped skills, and an inbox for transcripts.

The goal is that the user does not have to click "capture" for every session:
drop files into ``.membridge/inbox`` (or let a hook do it), keep docs under
``docs/`` and skill folders under ``.membridge/skills``; ``membridge watch``
pushes changes to the Hub on a timer.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

INBOX_EXTS = {".md", ".txt", ".jsonl", ".log"}
SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv"}


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _skill_files(folder: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.stat().st_size > 1024 * 1024:
            continue
        text = _read_text(path)
        if text is not None:
            files[path.relative_to(folder).as_posix()] = text
    return files


def init_project(path: str | Path) -> dict[str, Any]:
    root = Path(path).expanduser().resolve()
    base = root / ".membridge"
    inbox = base / "inbox"
    skills = base / "skills"
    inbox.mkdir(parents=True, exist_ok=True)
    skills.mkdir(parents=True, exist_ok=True)
    sample = skills / "example"
    sample.mkdir(parents=True, exist_ok=True)
    skill_md = sample / "SKILL.md"
    if not skill_md.exists():
        skill_md.write_text(
            "---\n"
            "name: Example skill\n"
            "description: Replace this with a real reusable procedure.\n"
            "status: candidate\n"
            "steps:\n"
            "  - Read the trigger conditions\n"
            "  - Follow the steps\n"
            "  - Check the result\n"
            "---\n\n"
            "# Example skill\n\n"
            "A folder-shaped skill. Add scripts, references, or assets next to this file.\n",
            encoding="utf-8",
        )
    gitignore = base / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("state.json\n", encoding="utf-8")
    readme = base / "README.md"
    if not readme.exists():
        readme.write_text(
            "# .membridge\n\n"
            "- `inbox/`: drop transcripts/notes here; `membridge watch` ingests them.\n"
            "- `skills/<name>/SKILL.md`: folder-shaped skills; sibling files are synced too.\n"
            "- `state.json`: local sync state (ignored by git).\n",
            encoding="utf-8",
        )
    return {"project": str(root), "membridge": str(base), "created": True}


class SyncEngine:
    def __init__(self, service: Any, project_path: str | Path, auto_activate: bool = False):
        self.service = service
        self.project = Path(project_path).expanduser().resolve()
        self.base = self.project / ".membridge"
        self.state_path = self.base / "state.json"
        self.auto_activate = auto_activate

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"version": 1, "docs": {}, "skills": {}, "inbox": {}}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "docs": {}, "skills": {}, "inbox": {}}
        for key in ("docs", "skills", "inbox"):
            data.setdefault(key, {})
        return data

    def _save_state(self, state: dict[str, Any]) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    def sync_once(self) -> dict[str, Any]:
        boot = self.service.bootstrap(project_path=str(self.project), client="sync")
        ctx = boot["context_version"]
        state = self._load_state()
        summary = {"docs": 0, "skills": 0, "inbox": 0, "skipped": 0, "candidates": 0}

        # docs/
        docs_root = self.project / "docs"
        if docs_root.exists():
            for path in sorted(docs_root.rglob("*.md")):
                if any(part in SKIP_DIRS for part in path.parts):
                    continue
                text = _read_text(path)
                if text is None:
                    continue
                rel = path.relative_to(self.project).as_posix()
                digest = _hash_text(text)
                if state["docs"].get(rel) == digest:
                    summary["skipped"] += 1
                    continue
                title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), path.stem)
                self.service.doc_add(ctx, path=rel, content=text, title=title)
                state["docs"][rel] = digest
                summary["docs"] += 1

        # .membridge/skills/<name>/SKILL.md
        skills_root = self.base / "skills"
        if skills_root.exists():
            for skill_md in sorted(skills_root.glob("*/SKILL.md")):
                folder = skill_md.parent
                files = _skill_files(folder)
                digest = _hash_text(json.dumps(files, sort_keys=True, ensure_ascii=False))
                rel = folder.relative_to(self.base).as_posix()
                if state["skills"].get(rel) == digest:
                    summary["skipped"] += 1
                    continue
                self.service.skill_install(ctx, files=files)
                state["skills"][rel] = digest
                summary["skills"] += 1

        # .membridge/inbox/*.md|txt|jsonl
        inbox_root = self.base / "inbox"
        if inbox_root.exists():
            for path in sorted(inbox_root.iterdir()):
                if not path.is_file() or path.suffix.lower() not in INBOX_EXTS:
                    continue
                text = _read_text(path)
                if not text or not text.strip():
                    continue
                digest = _hash_text(text)
                rel = path.relative_to(self.base).as_posix()
                if state["inbox"].get(rel) == digest:
                    summary["skipped"] += 1
                    continue
                result = self.service.memory_ingest(
                    ctx,
                    text,
                    source=f"inbox:{path.name}",
                    auto_activate=self.auto_activate,
                )
                state["inbox"][rel] = digest
                summary["inbox"] += 1
                summary["candidates"] += int(result.get("counts", {}).get("created", 0))

        self._save_state(state)
        summary["project"] = str(self.project)
        summary["context_version"] = ctx
        return summary


def watch(service: Any, project_path: str | Path, interval: float = 5.0, auto_activate: bool = False) -> None:
    engine = SyncEngine(service, project_path, auto_activate=auto_activate)
    print(f"watching {engine.project} every {interval:g}s; Ctrl+C to stop", flush=True)
    while True:
        try:
            result = engine.sync_once()
            print(
                f"sync docs={result['docs']} skills={result['skills']} inbox={result['inbox']} "
                f"candidates={result['candidates']} skipped={result['skipped']}",
                flush=True,
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"sync error: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(max(1.0, interval))
