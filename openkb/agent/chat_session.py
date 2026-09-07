"""Chat session persistence for `openkb chat`.

Each session lives in ``<kb>/.openkb/chats/<id>.json`` and stores a sanitized
agent-SDK history (from ``RunResult.to_input_list()``) alongside the user
messages and full assistant replies kept as plain strings for display and
export. Large tool-returned image payloads are replaced with lightweight
references before the history is reused or persisted.
"""

from __future__ import annotations

import hashlib
import json
import random
import string
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openkb.locks import atomic_write_text, kb_ingest_lock, session_lock
from openkb.mutation import mutation_scope

_IMAGE_HISTORY_NOTE = "Image output omitted from chat history to avoid persisting raw data URLs."


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _gen_id() -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=3))
    return f"{ts}-{rand}"


def chats_dir(kb_dir: Path) -> Path:
    return kb_dir / ".openkb" / "chats"


def _title_from(msg: str, limit: int = 60) -> str:
    msg = " ".join(msg.strip().split())
    if len(msg) <= limit:
        return msg
    return msg[: limit - 1] + "\u2026"


def _image_history_placeholder(image_path: str | None) -> dict[str, str]:
    text = _IMAGE_HISTORY_NOTE
    if image_path:
        text += f" Source path: {image_path}."
    text += " Call get_image again if you need to inspect it."
    return {"type": "input_text", "text": text}


def _extract_get_image_path(item: dict[str, Any]) -> str | None:
    if item.get("type") != "function_call" or item.get("name") != "get_image":
        return None
    arguments = item.get("arguments")
    if not isinstance(arguments, str):
        return None
    try:
        payload = json.loads(arguments)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    image_path = payload.get("image_path")
    if isinstance(image_path, str) and image_path:
        return image_path
    return None


def _sanitize_history_value(value: Any, image_path: str | None = None) -> Any:
    if isinstance(value, list):
        return [_sanitize_history_value(item, image_path) for item in value]
    if not isinstance(value, dict):
        return value

    if value.get("type") == "input_image":
        image_url = value.get("image_url")
        if isinstance(image_url, str) and image_url.startswith("data:"):
            return _image_history_placeholder(image_path)

    return {key: _sanitize_history_value(item, image_path) for key, item in value.items()}


def sanitize_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip large image payloads from model history while keeping a re-fetch hint."""
    image_paths_by_call_id: dict[str, str] = {}
    sanitized: list[dict[str, Any]] = []

    for item in history:
        if not isinstance(item, dict):
            sanitized.append(item)
            continue

        image_path = _extract_get_image_path(item)
        call_id = item.get("call_id")
        if image_path and isinstance(call_id, str):
            image_paths_by_call_id[call_id] = image_path

        history_image_path = None
        if item.get("type") == "function_call_output" and isinstance(call_id, str):
            history_image_path = image_paths_by_call_id.get(call_id)

        sanitized.append(_sanitize_history_value(item, history_image_path))

    return sanitized


@dataclass
class ChatSession:
    id: str
    created_at: str
    updated_at: str
    model: str
    language: str
    title: str
    turn_count: int
    history: list[dict[str, Any]]
    user_turns: list[str]
    assistant_texts: list[str]
    # Per-turn ordered trace (narration text + tool reads), parallel to
    # assistant_texts. Empty for turns saved before this existed (and for
    # CLI-recorded turns): the frontend falls back to the flat text for those.
    assistant_traces: list[list[dict[str, Any]]]
    path: Path
    _version: str | None = field(default=None, repr=False)

    @classmethod
    def new(cls, kb_dir: Path, model: str, language: str) -> "ChatSession":
        now = _utcnow_iso()
        sid = _gen_id()
        return cls(
            id=sid,
            created_at=now,
            updated_at=now,
            model=model,
            language=language,
            title="",
            turn_count=0,
            history=[],
            user_turns=[],
            assistant_texts=[],
            assistant_traces=[],
            path=chats_dir(kb_dir) / f"{sid}.json",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "model": self.model,
            "language": self.language,
            "title": self.title,
            "turn_count": self.turn_count,
            "history": self.history,
            "user_turns": self.user_turns,
            "assistant_texts": self.assistant_texts,
            "assistant_traces": self.assistant_traces,
        }

    def save(self) -> None:
        kb_dir = self.path.parent.parent.parent
        with session_lock(kb_dir, self.id), kb_ingest_lock(kb_dir / ".openkb"):
            if self._version is not None:
                # Missing is deletion, not permission to recreate this identity.
                current = self.path.read_bytes()
                if hashlib.sha256(current).hexdigest() != self._version:
                    raise RuntimeError("Conversation changed; reload its latest completed history")
            elif self.path.exists() or self.path.is_symlink():
                raise RuntimeError("Conversation changed; session identity already exists")
            text = json.dumps(self.to_dict(), ensure_ascii=False, indent=2, default=str)
            with mutation_scope(kb_dir, [self.path], operation="save-chat-turn"):
                atomic_write_text(self.path, text)
            self._version = hashlib.sha256(text.encode("utf-8")).hexdigest()

    def reload(self) -> None:
        """Refresh a continuing session inside its full-turn execution lease."""
        if self._version is not None or self.path.exists():
            latest = load_session(self.path.parent.parent.parent, self.id)
            self.__dict__.update(latest.__dict__)

    def record_turn(
        self,
        user_message: str,
        assistant_text: str,
        new_history: list[dict[str, Any]],
        trace: list[dict[str, Any]] | None = None,
    ) -> None:
        previous = deepcopy(self.__dict__)
        try:
            self._record_turn(user_message, assistant_text, new_history, trace)
        except BaseException:
            self.__dict__.update(previous)
            raise

    def _record_turn(
        self,
        user_message: str,
        assistant_text: str,
        new_history: list[dict[str, Any]],
        trace: list[dict[str, Any]] | None,
    ) -> None:
        self.history = sanitize_history(new_history)
        self.user_turns.append(user_message)
        self.assistant_texts.append(assistant_text)
        # Keep assistant_traces aligned 1:1 with assistant_texts. A session
        # created before traces existed (or via the CLI, which passes none)
        # back-fills empty traces for its earlier turns so index i always maps
        # to the same turn; an empty trace makes the frontend fall back to the
        # flat assistant_text for that turn.
        while len(self.assistant_traces) < len(self.assistant_texts) - 1:
            self.assistant_traces.append([])
        self.assistant_traces.append(trace or [])
        self.turn_count = len(self.user_turns)
        if not self.title:
            self.title = _title_from(user_message)
        self.updated_at = _utcnow_iso()
        self.save()


def load_session(kb_dir: Path, session_id: str) -> ChatSession:
    path = _session_path(kb_dir, session_id)
    content = path.read_bytes()
    data = json.loads(content.decode("utf-8"))
    if not isinstance(data, dict) or data.get("id") != session_id:
        raise ValueError("Conversation identity does not match its file")
    if any(not isinstance(data.get(key), str) for key in ("created_at", "updated_at", "model")):
        raise ValueError("Invalid conversation metadata")
    if any(not isinstance(data.get(key, ""), str) for key in ("title", "language")):
        raise ValueError("Invalid conversation display metadata")
    for key in ("user_turns", "assistant_texts"):
        value = data.get(key, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError("Invalid completed conversation text")
    for key in ("history", "assistant_traces"):
        if not isinstance(data.get(key, []), list):
            raise ValueError("Invalid conversation history")
    if type(data.get("turn_count", 0)) is not int or data.get("turn_count", 0) < 0:
        raise ValueError("Invalid completed conversation count")
    return ChatSession(
        id=data["id"],
        created_at=data["created_at"],
        updated_at=data["updated_at"],
        model=data["model"],
        language=data.get("language", "en"),
        title=data.get("title", ""),
        turn_count=data.get("turn_count", 0),
        history=sanitize_history(data.get("history", [])),
        user_turns=data.get("user_turns", []),
        assistant_texts=data.get("assistant_texts", []),
        assistant_traces=data.get("assistant_traces", []),
        path=path,
        _version=hashlib.sha256(content).hexdigest(),
    )


def list_sessions(kb_dir: Path) -> list[dict[str, Any]]:
    """Return session metadata dicts, most recently updated first."""
    d = chats_dir(kb_dir)
    if not d.exists():
        return []
    out: list[dict[str, Any]] = []
    for p in d.glob("*.json"):
        try:
            session = load_session(kb_dir, p.stem)
        except (ValueError, OSError):
            continue
        out.append(
            {
                "id": session.id,
                "title": session.title,
                "turn_count": session.turn_count,
                "updated_at": session.updated_at,
                "model": session.model,
            }
        )
    out.sort(key=lambda s: (s["updated_at"], s["id"]), reverse=True)
    return out


def resolve_session_id(kb_dir: Path, query: str) -> str | None:
    """Resolve a query to a full session id.

    ``query`` may be:
    - ``"__latest__"`` — returns the most recently updated session id.
    - A full session id — returned as-is if it exists.
    - A unique prefix of a session id — expanded to the full id.

    Returns ``None`` if no session matches. Raises ``ValueError`` when a
    prefix is ambiguous.
    """
    sessions = list_sessions(kb_dir)
    if not sessions:
        return None
    if query == "__latest__":
        return sessions[0]["id"]
    for s in sessions:
        if s["id"] == query:
            return s["id"]
    matches = [s["id"] for s in sessions if s["id"].startswith(query)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"Ambiguous session prefix '{query}' matches: {', '.join(matches)}")
    return None


def delete_session(kb_dir: Path, session_id: str) -> bool:
    from openkb.application.sessions import delete_conversation

    return delete_conversation(kb_dir, session_id).status == "deleted"


def relative_time(iso_str: str) -> str:
    """Render an ISO-8601 timestamp as a short relative string."""
    try:
        t = datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return iso_str or ""
    now = datetime.now(timezone.utc)
    seconds = int((now - t).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    if seconds < 86400 * 7:
        return f"{seconds // 86400}d ago"
    return t.strftime("%Y-%m-%d")


def _session_path(kb_dir: Path, session_id: str) -> Path:
    if (
        not isinstance(session_id, str)
        or not session_id
        or any(c in session_id for c in "/\\")
        or session_id.startswith(".")
    ):
        raise ValueError("Invalid conversation ID")
    path = chats_dir(kb_dir) / f"{session_id}.json"
    if not path.resolve().is_relative_to(kb_dir.resolve()):
        raise ValueError("Conversation path escapes the knowledge base")
    return path
