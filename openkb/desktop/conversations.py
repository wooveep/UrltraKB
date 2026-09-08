"""Automatic desktop chat continuity, drafts and asynchronous task ownership."""

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

from openkb.agent.answer_text import visible_answer
from openkb.agent.chat_session import ChatSession
from openkb.application.conversations import read_conversation
from openkb.desktop.chat_outbox import ChatOutbox
from openkb.desktop.io import _defer_wait
from openkb.runtime.records import TERMINAL
from openkb.runtime.requests import ContinueConversation


@dataclass(eq=False)
class Chat:
    root: Path
    identity: str | None = None
    title: str = "新对话"
    turns: list[tuple[str, str]] = field(default_factory=list)
    draft: str = ""
    question: str = ""
    task: str | None = None
    status: str = ""
    loading: bool = False
    running: bool = False
    persisted: bool = False
    completed_count: int = 0
    attempt: str | None = None
    read_error: str = ""
    generation: int = 0


class Conversations:
    def __init__(self, window):
        self.window = window
        self.active = None
        self.recent = {}
        self.tasks = {}
        self.saved = {}
        self.outbox = ChatOutbox(window.manager.history_dir.parent / "chat-outbox")
        self.awaiting_catalog = False
        window.question.textChanged.connect(self.update_controls)

    def _key(self, root):
        return "chat/" + sha256(str(root).encode()).hexdigest()

    def _remember(self, chat):
        self.window.shell.preferences.setValue(self._key(chat.root), chat.identity or "new")
        self.window.shell.preferences.sync()

    def reset(self):
        if self.active:
            self.active.draft = self.window.question.toPlainText()
        self.active = self.recent.get(self.window.kb)
        self.awaiting_catalog = bool(self.window.kb and self.active is None)
        self.render()

    def recover(self, root, *, chat=None):
        # No model requests are replayed. Recovery waits off the UI thread and
        # takes session -> KB leases in the same order as normal chat execution.
        excluded = {c.attempt for c in self.tasks.values() if c.running}

        def obsolete():
            return self.window._quitting or root in self.window._deleting_kbs

        def loaded(_, error):
            if error:
                self.window._error(error)
                if self.window.kb == root:
                    self.window._refresh()
                return
            if chat and chat.identity and not chat.running:
                self.load(chat)
            if self.window.kb == root:
                self.window._refresh()

        self.window.io.submit(
            lambda: self.outbox.recover(root, exclude=excluded, on_wait=_defer_wait),
            loaded,
            obsolete=obsolete,
        )

    def catalog_loaded(self, sessions):
        identities = [item["id"] for item in sessions]
        chat = self.active
        if (
            chat
            and chat.identity
            and chat.identity not in identities
            and chat.persisted
            and not chat.loading
            and not self.busy()
        ):
            self.new()
        if not self.awaiting_catalog or not self.window.kb:
            return
        self.awaiting_catalog = False
        saved = self.window.shell.preferences.value(self._key(self.window.kb), "")
        identities = [item["id"] for item in sessions]
        identity = saved if saved in identities else identities[0] if identities else None
        if identity and saved != "new":
            self.open(identity)
        else:
            self.new()

    def select(self, chat):
        if self.active:
            self.active.draft = self.window.question.toPlainText()
        self.active = chat
        self.recent[chat.root] = chat
        if chat.identity:
            self.saved[chat.root, chat.identity] = chat
        self.awaiting_catalog = False
        self._remember(chat)
        self.render()
        self.window.workspaces.show_answer()
        self.window.question.setFocus()

    def new(self):
        if self.window.kb:
            self.select(Chat(self.window.kb))

    def open(self, identity):
        root = self.window.kb
        if not root or root in self.window._deleting_kbs:
            return
        chat = self.saved.get((root, identity))
        if chat is None:
            chat = Chat(root, identity, persisted=True)
        self.select(chat)
        # Reading a session while its worker holds the write lease would leave
        # the drawer waiting for the answer. The in-memory transcript is current.
        if not chat.running:
            self.load(chat)

    def load(self, chat):
        chat.generation += 1
        generation = chat.generation
        chat.loading = True
        self.update_controls()

        def obsolete():
            return (
                self.window._quitting
                or chat.root in self.window._deleting_kbs
                or generation != chat.generation
            )

        def loaded(session, error):
            if obsolete():
                return
            chat.loading = False
            if error:
                chat.read_error = "无法读取对话，请重新打开历史记录，或开始新对话。"
            else:
                chat.turns = list(session.timeline)
                chat.title = session.title or "对话"
                chat.status = chat.read_error = ""
                chat.persisted = True
                chat.completed_count = len(session.turns)
            if self.active is chat:
                self.render(restore_draft=False)

        self.window.io.submit(
            lambda: read_conversation(chat.root, chat.identity),
            loaded,
            kb=chat.root,
            obsolete=obsolete,
        )

    def busy(self):
        return bool(self.active and self.active.running)

    def update_controls(self):
        w, chat = self.window, self.active
        busy = self.busy()
        w.ask_button.setVisible(not busy)
        w.stop_answer.setVisible(busy)
        w.ask_button.setEnabled(
            bool(
                w.kb
                and chat
                and not chat.loading
                and not chat.read_error
                and not busy
                and w.question.toPlainText().strip()
            )
        )
        w.question.setEnabled(bool(w.kb))
        w.stop_answer.setEnabled(busy)

    def send(self):
        w = self.window
        if not w.kb or w.kb in w._deleting_kbs or w._quitting:
            return
        if self.active is None:
            return
        chat = self.active
        question = w.question.toPlainText().strip()
        if not question or chat.loading or chat.read_error or self.busy():
            return
        accepted = None
        try:
            if chat.identity is None:
                chat.identity = ChatSession.new(chat.root, "", "").id
                chat.title = question.splitlines()[0][:60]
                self.saved[chat.root, chat.identity] = chat
            accepted = self.outbox.accept(
                chat.root,
                chat.identity,
                question,
                new=not chat.persisted,
                after_turn=chat.completed_count,
            )
            chat.attempt = accepted.id
            request = ContinueConversation(
                question,
                chat.identity if chat.persisted else None,
                new_session_id=None if chat.persisted else chat.identity,
                attempt_id=chat.attempt,
                submission_order=accepted.order,
            )
            task = w.manager.submit(chat.root, [request])
            self._remember(chat)
        except Exception as error:
            if accepted:
                self.outbox.discard(chat.root, accepted.id)
                chat.attempt = None
            w._error(error)
            return
        chat.generation += 1
        chat.question, chat.task = question, task
        chat.running = True
        chat.status = "已排队，等待执行…"
        chat.draft = ""
        self.tasks[task] = chat
        w.question.clear()
        self.render()
        w.workspaces.show_answer()

    def stop(self):
        if self.busy():
            self.window.manager.stop(self.active.task)

    def observe(self, task):
        chat = self.tasks.get(task.id)
        if not chat or task.id != chat.task or task.state in TERMINAL:
            return
        status = {
            "queued": "已排队，等待执行…",
            "waiting": "等待知识库可用…",
            "stopping": "正在停止…",
        }.get(task.state, "正在查阅资料并整理回答…")
        if chat.status != status:
            chat.status = status
            if self.active is chat:
                self.render(restore_draft=False)

    def finished(self, task):
        chat = self.tasks.get(task.id)
        if not chat or chat.task != task.id:
            return
        chat.running = False
        if task.results and task.results[-1].session_id:
            chat.identity = task.results[-1].session_id
            chat.persisted = True
            self.saved[chat.root, chat.identity] = chat
            if task.state == "completed":
                chat.turns.append((chat.question, visible_answer(task.results[-1].output)))
            chat.question = chat.status = ""
            if self.recent.get(chat.root) is chat:
                self._remember(chat)
            self.load(chat)
            if chat.attempt:
                self.outbox.discard(chat.root, chat.attempt)
        else:
            # Incomplete model text can contain tool narration or reasoning.
            # Keep the submitted question visible, without presenting it as an answer.
            chat.status = (
                "回答已停止。可以继续提问。"
                if task.state == "stopped"
                else "暂时未能完成回答，可在任务中查看原因。"
            )
            chat.turns.append((chat.question, chat.status))
            chat.question = ""
            self.recover(chat.root, chat=chat)
        if self.active is chat:
            self.render(restore_draft=False)

    def render(self, *, restore_draft=True):
        w, chat = self.window, self.active
        w._chat_task = chat.task if chat else None
        if restore_draft:
            w.question.setPlainText(chat.draft if chat else "")
        w.conversation_title.setText(chat.title if chat else "新对话")
        w.conversation_title.setToolTip(chat.title if chat else "新对话")
        notice = ("正在读取对话…" if chat.loading else chat.read_error) if chat else ""
        w.conversation_notice.setText(notice)
        w.conversation_notice.setVisible(bool(notice))
        if chat:
            pending = (chat.question, chat.status) if chat.question else None
            w.chat.show_turns(chat.turns, chat.root / "wiki", pending=pending)
        else:
            w.chat.show_temporary("")
        self.update_controls()
