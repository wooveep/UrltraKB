"""Checkpoint each accepted topic update and project at most its final prefix."""

import hashlib
import json
from dataclasses import dataclass

from ctree import CTree
from ctree.prompts import PROMPT_VERSION
from ctree.state import prefix_digest

from ultrakb.conversation_index import ChatIndexLLM
from ultrakb.llm_execution import check_model_stop, raise_if_terminal
from ultrakb.locks import session_lock
from ultrakb.storage.session_store import SessionConflict, SessionStore


@dataclass(frozen=True)
class MemoryState:
    policy_key: str
    tree_id: str | None
    indexed_turns: int
    computed_turns: int
    pending_turns: int
    prefix_digest: str
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreparedMemory:
    status: str
    reason: str
    state: MemoryState


class ConversationMemory:
    def __init__(
        self,
        store: SessionStore,
        session_id: str,
        *,
        policy_key: str,
        model: str,
        llm,
        check_cancelled=check_model_stop,
    ):
        store.validate_memory_policy(session_id, policy_key, model)
        self.store, self.session_id, self.policy_key = store, session_id, policy_key
        self.model, self.llm, self.check_cancelled = model, llm, check_cancelled

    @classmethod
    def from_executor(cls, store, session_id, executor):
        conversation = store.load(session_id)
        endpoint = executor.bindings.credentials().base_url or "provider-default"
        policy = {
            "kb_id": store.identity.kb_id,
            "session": session_id,
            "view": conversation.view_id,
            "models": executor.bindings.public_identity(),
            "endpoint": hashlib.sha256(endpoint.encode()).hexdigest(),
            "analysis_roles": ["user"],
            "content_roles": ["user"],
            "checkpoint_codec": 1,
            "prompt": PROMPT_VERSION,
        }
        policy_key = hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()
        return cls(
            store,
            session_id,
            policy_key=policy_key,
            model=executor.bindings.model("conversation_index"),
            llm=ChatIndexLLM(executor),
            check_cancelled=executor.policy.check,
        )

    def _check(self, revision):
        self.check_cancelled()
        self.store.check_revision(self.session_id, revision)

    def state(self, diagnostics=()) -> MemoryState:
        roots = self.store.memory_roots(self.session_id, self.policy_key)
        indexed = roots.projected.turn_count if roots.tree_id and roots.projected else 0
        if roots.projected and not roots.tree_id:
            diagnostics = (*diagnostics, "projection_unavailable")
        return MemoryState(
            self.policy_key,
            roots.tree_id,
            indexed,
            roots.computed.turn_count if roots.computed else 0,
            roots.total_turns - indexed,
            roots.projected.prefix_digest
            if roots.tree_id and roots.projected
            else prefix_digest([]),
            tuple(diagnostics),
        )

    def _restore(self):
        try:
            return self.store.restore_checkpoint(self.session_id, self.policy_key, llm=self.llm)
        except KeyError:
            return CTree(
                model=self.model,
                conversation_id=self.session_id,
                analysis_roles=("user",),
                llm=self.llm,
            )

    def prepare(
        self, *, committed_user_tokens: int, recall_budget: int, recent_turn_ids: set[str]
    ) -> PreparedMemory:
        """Use the existing capacity allocation before any model/index work."""
        from ultrakb.context.preparation import RECALL_THRESHOLD_TOKENS

        if any(
            type(value) is not int or value < 0 for value in (committed_user_tokens, recall_budget)
        ):
            raise ValueError("Recall capacity and user token counts must be nonnegative integers")
        if committed_user_tokens < RECALL_THRESHOLD_TOKENS:
            return PreparedMemory("recent_only", "below_recall_threshold", self.state())
        if recall_budget == 0:
            return PreparedMemory("recent_only", "recall_budget_zero", self.state())
        if not self.store.has_recall_candidates(self.session_id, recent_turn_ids):
            return PreparedMemory("recent_only", "no_recall_candidates", self.state())
        state = self.catch_up()
        return PreparedMemory(
            "ready" if state.pending_turns == 0 else "limited",
            "complete" if state.pending_turns == 0 else "index_lag",
            state,
        )

    def catch_up(self) -> MemoryState:
        def cancelled():
            self.check_cancelled()
            return False

        self.check_cancelled()
        with session_lock(self.store.root, self.session_id, cancelled=cancelled):
            self.check_cancelled()
            state = self.state()
            if state.pending_turns == 0:
                return state
            captured = self.store.load(self.session_id)
            self._check(captured.revision)
            tree = self._restore()
            diagnostics: tuple[str, ...] = ()
            for turn in captured.turns[len(tree.turns) :]:
                try:
                    self._check(captured.revision)
                    messages = {message["role"]: message["content"] for message in turn["messages"]}
                    tree.add_exchange(
                        turn["turn_id"],
                        messages["user"],
                        messages["assistant"],
                        system=messages.get("system"),
                    )
                    self._check(captured.revision)
                    self.store.save_checkpoint(
                        self.session_id,
                        self.policy_key,
                        tree.snapshot_state(),
                        expected_revision=captured.revision,
                        check_cancelled=self.check_cancelled,
                    )
                except Exception as error:
                    raise_if_terminal(error)
                    if isinstance(error, (FileNotFoundError, SessionConflict)):
                        raise
                    # A failed last acceptance must never export the newer in-memory turn.
                    tree = self._restore()
                    diagnostics = (f"index_lag:{type(error).__name__}",)
                    break
            self._check(captured.revision)
            roots = self.store.memory_roots(self.session_id, self.policy_key)
            if (
                roots.computed
                and roots.computed.turn_count
                and (not roots.tree_id or roots.computed != roots.projected)
            ):
                try:
                    exported = tree.export_retrieval_tree(content_roles=("user",))
                    self._check(captured.revision)
                    self.store.accept_projection(
                        self.session_id,
                        self.policy_key,
                        roots.computed,
                        exported,
                        expected_revision=captured.revision,
                        check_cancelled=self.check_cancelled,
                    )
                except Exception as error:
                    raise_if_terminal(error)
                    if isinstance(error, (FileNotFoundError, SessionConflict)):
                        raise
                    diagnostics = (*diagnostics, f"projection_lag:{type(error).__name__}")
            return self.state(diagnostics)
