"""Automatic desktop chat continuity, drafts and asynchronous task ownership."""

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from ultrakb.agent.answer_text import visible_answer
from ultrakb.application.conversations import read_conversation
from ultrakb.desktop.chat_outbox import ChatOutbox
from ultrakb.desktop.conversation_activity import failure_status, task_status
from ultrakb.desktop.io import _defer_wait
from ultrakb.knowledge_scope import KnowledgeScope, live_scope
from ultrakb.runtime.records import TERMINAL, TaskView
from ultrakb.runtime.requests import ContinueConversation


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
    activity: TaskView | None = None
    failure: str = ""
    answer: str = ""
    usage: dict | None = None
    pending_usage: dict | None = None
    view_id: str = "legacy"
    answer_outcome: str | None = None
    answers: tuple = ()
    focus: tuple = ()
    request_focus: tuple = ()
    scope: KnowledgeScope | None = None

    def belongs_to(self, scope):
        return bool(
            self.scope
            and scope
            and self.scope.knowledge
            and scope.knowledge
            and self.scope.knowledge.identity == scope.knowledge.identity
            and self.view_id == scope.view_id
        )


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

    def _key(self, root, view_id=None):
        view_id = view_id or getattr(self.window, "view_id", None) or "legacy"
        return "chat/" + sha256((str(root) + "/" + view_id).encode()).hexdigest()

    def _remember(self, chat):
        self.window.shell.preferences.setValue(
            self._key(chat.root, chat.view_id), chat.identity or "new"
        )
        self.window.shell.preferences.sync()

    def reset(self):
        self._focus_key = None
        if self.active:
            self.active.draft = self.window.question.toPlainText()
        self.active = self.recent.get(
            (self.window.kb, getattr(self.window, "view_id", None) or "legacy")
        )
        if self.active and not self.active.belongs_to(self.window.scope):
            self.active = None
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
        if chat.scope is None:
            chat.scope = self.window.scope
        if self.active:
            self.active.draft = self.window.question.toPlainText()
        self.active = chat
        self.recent[chat.root, chat.view_id] = chat
        if chat.identity:
            self.saved[chat.root, chat.identity] = chat
        self.awaiting_catalog = False
        self._remember(chat)
        self.render()
        self.window.workspaces.show_answer()
        self.window.question.setFocus()

    def new(self):
        if self.window.kb:
            self.select(
                Chat(self.window.kb, view_id=getattr(self.window, "view_id", None) or "legacy")
            )

    def open(self, identity):
        root = self.window.kb
        if not root or root in self.window._deleting_kbs:
            return
        chat = self.saved.get((root, identity))
        if chat and not chat.belongs_to(self.window.scope):
            chat = None
        if chat is None:
            chat = Chat(
                root,
                identity,
                persisted=True,
                view_id=getattr(self.window, "view_id", None) or "legacy",
            )
        self.select(chat)
        # Reading a session while its worker holds the write lease would leave
        # the drawer waiting for the answer. The in-memory transcript is current.
        if not chat.running:
            self.load(chat)

    def load(self, chat):
        captured_scope = chat.scope or (
            self.window.scope
            if self.window.kb == chat.root
            else live_scope(chat.root, chat.view_id)
        )
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
            self._focus_key = None
            if error:
                chat.read_error = "无法读取对话，请重新打开历史记录，或开始新对话。"
            else:
                chat.turns = list(session.timeline)
                chat.answers = session.timeline_answers
                chat.title = session.title or "对话"
                chat.status = chat.read_error = ""
                if not any(after == len(session.turns) for after, _ in session.incomplete):
                    chat.failure = ""
                if not chat.failure and any(
                    after == len(session.turns) for after, _ in session.incomplete
                ):
                    candidates = [
                        task
                        for task in self.window.manager.tasks()
                        if task.kb_dir == str(chat.root)
                        and any(row.session_id == session.id for row in task.results)
                    ]
                    latest = max(candidates, key=lambda task: task.started_at or "", default=None)
                    if latest and latest.state in TERMINAL and latest.state != "completed":
                        chat.failure = failure_status(
                            latest.error or (latest.results[-1].error if latest.results else None),
                            stopped=latest.state == "stopped",
                        )
                chat.persisted = True
                chat.completed_count = (
                    session.total_turns if session.total_turns is not None else len(session.turns)
                )
                chat.usage = session.usage
                chat.pending_usage = None
                chat.answer_outcome = (
                    session.answer_outcomes[-1] if session.answer_outcomes else None
                )
            if self.active is chat:
                self.render(restore_draft=False)

        self.window.io.submit(
            lambda: read_conversation(
                chat.root, chat.identity, view_id=chat.view_id, scope=captured_scope
            ),
            loaded,
            kb=chat.root,
            snapshot=True,
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
        focus = chat.focus
        w.onboarding.ensure(
            chat.root,
            "发送已填写的问题",
            lambda: self._send_prepared(chat, question, focus=focus),
            valid=lambda: self.active is chat and (w.view_id or "legacy") == chat.view_id,
        )

    def _send_prepared(self, chat, question, *, focus=()):
        w = self.window
        accepted = None
        try:
            if chat.identity is None:
                chat.identity = uuid4().hex
                chat.title = question.splitlines()[0][:60]
                self.saved[chat.root, chat.identity] = chat
            accepted = self.outbox.accept(
                chat.root,
                chat.identity,
                question,
                new=not chat.persisted,
                after_turn=chat.completed_count,
                view_id=chat.view_id,
                database_identity=w.read_scope.knowledge.identity if w.read_scope else None,
            )
            chat.attempt = accepted.id
            request = ContinueConversation(
                question,
                chat.identity if chat.persisted else None,
                new_session_id=None if chat.persisted else chat.identity,
                attempt_id=chat.attempt,
                submission_order=accepted.order,
                view_id=None if chat.view_id == "legacy" else chat.view_id,
                focus=focus,
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
        chat.request_focus = focus
        chat.activity = None
        chat.answer = ""
        chat.pending_usage = None
        chat.failure = ""
        chat.status = "已排队，等待执行…"
        draft = w.question.toPlainText()
        chat.draft = "" if draft.strip() == question else draft
        self.tasks[task] = chat
        if draft.strip() == question:
            w.question.clear()
        self.render()
        w.workspaces.show_answer()

    def stop(self):
        if self.busy():
            self.window.request_task_stop(self.active.task)

    def observe(self, task):
        chat = self.tasks.get(task.id)
        if not chat or task.id != chat.task or task.state in TERMINAL:
            return
        status = task_status(task)
        chat.activity = task
        if self.active is chat and hasattr(self.window.conversation_notice, "set_task"):
            self.window.conversation_notice.set_task(task)
        answer = visible_answer(task.text, streaming=True) if not task.text_truncated else ""
        if chat.status != status or chat.answer != answer or chat.pending_usage != task.usage:
            chat.status = status
            chat.answer = answer
            chat.pending_usage = task.usage
            if self.active is chat:
                self.render(restore_draft=False)

    def finished(self, task):
        chat = self.tasks.get(task.id)
        if not chat or chat.task != task.id:
            return
        chat.running = False
        chat.answer = ""
        chat.activity = None
        chat.failure = (
            failure_status(
                task.error or (task.results[-1].error if task.results else None),
                stopped=task.state == "stopped",
            )
            if task.state != "completed"
            else ""
        )
        if task.results and task.results[-1].session_id:
            chat.identity = task.results[-1].session_id
            chat.persisted = True
            self.saved[chat.root, chat.identity] = chat
            if task.state == "completed":
                chat.turns.append((chat.question, visible_answer(task.results[-1].output)))
                chat.answer_outcome = task.results[-1].answer_outcome
            chat.question = chat.status = ""
            if self.recent.get((chat.root, chat.view_id)) is chat:
                self._remember(chat)
            self.load(chat)
            if chat.attempt:
                self.outbox.discard(chat.root, chat.attempt)
        else:
            # Incomplete model text can contain tool narration or reasoning.
            # Keep the submitted question visible, without presenting it as an answer.
            chat.status = chat.failure
            chat.turns.append((chat.question, chat.status))
            chat.question = ""
            self.recover(chat.root, chat=chat)
        if self.active is chat:
            self.render(restore_draft=False)

    def clear_focus(self):
        if self.active and not self.active.running:
            self.active.focus = ()
            self.render(restore_draft=False)

    def refresh_focus(self):
        if self.active and self.active.focus:
            from ultrakb.desktop.document_focus import update_header

            self._focus_key = None
            update_header(self)

    def render(self, *, restore_draft=True):
        from ultrakb.desktop.document_focus import update_header

        update_header(self)
        w, chat = self.window, self.active
        w._chat_task = chat.task if chat else None
        if restore_draft:
            w.question.setPlainText(chat.draft if chat else "")
        w.conversation_title.setText(chat.title if chat else "新对话")
        w.conversation_title.setToolTip(chat.title if chat else "新对话")
        notice = (
            (
                chat.status
                if chat.running
                else "正在读取对话…"
                if chat.loading
                else chat.read_error
                or chat.failure
                or {
                    "partial": "已回答有依据的部分，其余事项暂留缺口。",
                    "insufficient_evidence": "本次未获得足够的已核实证据。",
                    "evidence_rejected": "生成的结论未通过证据检查。",
                }.get(chat.answer_outcome, "")
            )
            if chat
            else ""
        )
        if chat and chat.running and chat.activity and hasattr(w.conversation_notice, "set_task"):
            w.conversation_notice.set_task(chat.activity)
        else:
            w.conversation_notice.setText(notice)
        w.conversation_notice.setVisible(bool(notice))
        if chat:
            pending = (chat.question, chat.answer) if chat.question else None
            w.chat.show_turns(
                chat.turns,
                (chat.scope or live_scope(chat.root, chat.view_id)).wiki_dir,
                pending=pending,
                identity=id(chat),
                answers=chat.answers,
            )
        else:
            w.chat.show_temporary("")
        self.update_controls()
