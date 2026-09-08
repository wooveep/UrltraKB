"""Conversation deletion and transcript export under session → KB leases."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from openkb.agent.chat_session import ChatSession, _session_path, deletion_marker, load_session
from openkb.application.execution import ExecutionContext
from openkb.application.file_state import contained_paths
from openkb.locks import atomic_write_text, kb_ingest_lock, session_lock
from openkb.mutation import mutation_scope


@dataclass(frozen=True)
class SessionResult:
    status: Literal["deleted", "exported", "missing", "conflict"]
    resources: tuple[str, ...] = ()
    changes: tuple[str, ...] = ()


def delete_conversation(
    kb_dir: Path,
    session_id: str,
    *,
    version: str | None = None,
    context: ExecutionContext | None = None,
) -> SessionResult:
    root = kb_dir.resolve()
    path = _session_path(root, session_id)
    cancelled = context.cancelled if context else None
    on_wait = context.waiting if context else None
    with (
        session_lock(root, session_id, cancelled=cancelled, on_wait=on_wait),
        kb_ingest_lock(root / ".openkb", cancelled=cancelled, on_wait=on_wait),
    ):
        contained_paths(root, [path])
        if not path.exists():
            return SessionResult("missing")
        if version is not None and hashlib.sha256(path.read_bytes()).hexdigest() != version:
            return SessionResult("conflict")
        with context.begin(root) if context else nullcontext():
            marker = deletion_marker(root, session_id)
            contained_paths(root, [marker])
            with mutation_scope(root, [path, marker], operation="delete-conversation"):
                atomic_write_text(marker, "deleted\n")
                path.unlink()
            return SessionResult(
                "deleted", changes=(f"deleted: {path.relative_to(root).as_posix()}",)
            )


def export_conversation(
    kb_dir: Path,
    session: ChatSession | str,
    name: str | None = None,
    *,
    unique: bool = False,
    context: ExecutionContext | None = None,
) -> SessionResult:
    """Export completed history; desktop uses copies, CLI keeps its overwrite policy.

    A persisted conversation is reloaded under both leases. The CLI can also
    export a newly created in-memory session which has never been persisted.
    """
    root = kb_dir.resolve()
    identity = session if isinstance(session, str) else session.id
    source = _session_path(root, identity)
    if isinstance(session, ChatSession) and session.path.resolve() != source.resolve():
        raise ValueError("Conversation belongs to another knowledge base")
    cancelled = context.cancelled if context else None
    on_wait = context.waiting if context else None
    with (
        session_lock(root, identity, cancelled=cancelled, on_wait=on_wait),
        kb_ingest_lock(root / ".openkb", cancelled=cancelled, on_wait=on_wait),
    ):
        contained_paths(root, [source])
        if isinstance(session, str) or session._version is not None or source.exists():
            if not source.exists():
                return SessionResult("missing")
            session = load_session(root, identity)
        base = name or session.title or (session.user_turns[0] if session.user_turns else identity)
        slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:60] or identity
        date = re.sub(r"[^0-9]", "", session.created_at[:10])
        directory = root / "wiki/explorations"
        path = directory / f"{slug}-{date}.md"
        contained_paths(root, [path])
        counter = 1
        while unique and path.exists():
            path = directory / f"{slug}-{date}-{counter}.md"
            contained_paths(root, [path])
            counter += 1
        change = "updated" if path.exists() else "created"
        with context.begin(root) if context else nullcontext():
            content = _transcript(root, session)
            with mutation_scope(root, [path], operation="export-conversation"):
                atomic_write_text(path, content)
            return SessionResult(
                "exported",
                resources=(str(path),),
                changes=(f"{change}: {path.relative_to(root).as_posix()}",),
            )


def _transcript(kb_dir: Path, session: ChatSession) -> str:
    from openkb.lint import build_norm_index, list_existing_wiki_targets, strip_ghost_wikilinks

    known = list_existing_wiki_targets(kb_dir / "wiki")
    norm_index = build_norm_index(known)
    lines = [
        "---",
        f"session: {json.dumps(session.id, ensure_ascii=False)}",
        f"model: {json.dumps(session.model, ensure_ascii=False)}",
        f"created: {json.dumps(session.created_at, ensure_ascii=False)}",
        "---",
        "",
        f"# Chat transcript  {session.title or session.id}",
        "",
    ]
    from openkb.agent.answer_text import visible_answer

    for index in range(len(session.user_turns) + 1):
        for entry in session.incomplete:
            if entry["after_turn"] == index:
                lines.extend(
                    [f"## [unfinished] {entry['message']}", "", "_(answer unfinished)_", ""]
                )
        if index == len(session.user_turns):
            break
        user, answer = session.user_turns[index], session.assistant_texts[index]
        answer = visible_answer(answer)
        index += 1
        lines.extend([f"## [{index}] {user}", ""])
        if answer:
            cleaned, _ = strip_ghost_wikilinks(answer, known, norm_index=norm_index)
            lines.append(cleaned)
        else:
            lines.append("_(no response recorded)_")
        lines.append("")
    return "\n".join(lines)
