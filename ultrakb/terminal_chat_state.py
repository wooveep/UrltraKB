"""Small, disposable display state for the interactive terminal.

The shared application owns conversation persistence and model context. This
adapter retains only the latest five exchanges needed for its resume preview.
"""

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ultrakb.application.conversations import ConversationView, read_conversation
from ultrakb.knowledge_scope import KnowledgeScope, resolve_scope


@dataclass
class TerminalConversation:
    kb_dir: Path
    id: str
    model: str
    language: str
    scope: KnowledgeScope
    history: ConversationView | None = None

    @classmethod
    def new(cls, kb_dir, model, language, *, scope=None):
        selected = resolve_scope(kb_dir, scope)
        return cls(selected.kb_dir, uuid4().hex, model, language, selected)

    @classmethod
    def resume(cls, kb_dir, session_id, *, scope=None):
        selected = resolve_scope(kb_dir, scope)
        session = cls(selected.kb_dir, session_id, "", "", selected)
        session.reload()
        return session

    @property
    def persisted(self) -> bool:
        return self.history is not None

    @property
    def turn_count(self) -> int:
        return self.history.total_turns if self.history else 0

    @property
    def user_turns(self) -> list[str]:
        return [user for user, _ in self.history.turns] if self.history else []

    @property
    def assistant_texts(self) -> list[str]:
        return [answer for _, answer in self.history.turns] if self.history else []

    def require_view(self, view_id: str) -> None:
        if self.scope.view_id != view_id:
            raise ValueError("Conversation scope and view do not match")

    def reload(self, *, context=None) -> None:
        try:
            history = read_conversation(
                self.kb_dir, self.id, scope=self.scope, limit=5, context=context
            )
        except FileNotFoundError:
            if self.persisted:
                raise
            return
        self.history = history
        self.model, self.language = history.model, history.language
