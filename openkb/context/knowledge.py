"""ConDB selects candidates; only OpenKB's pinned original reader supplies evidence."""

from __future__ import annotations

import json
import threading

from contextdb import ConDB

from openkb.application.query_views import QuerySelection, read_selected_file
from openkb.context.condb_llm import ConDBLLMAdapter, RetrievalFailure
from openkb.context.projection import ensure_projection, fingerprint, open_runtime
from openkb.llm_execution import CompletionExecutor, StageBudgetExceeded, raise_if_terminal


class KnowledgeRetrieval:
    def __init__(self, selection: QuerySelection, executor: CompletionExecutor):
        self.selection, self.executor = selection, executor
        self._guard = threading.Lock()

    def ensure_projection(self):
        credentials = self.executor.bindings.credentials()
        identity = {
            **self.executor.bindings.public_identity(),
            "endpoint": fingerprint(credentials.base_url),
        }
        return ensure_projection(self.selection, identity)

    def search(self, question: str) -> dict:
        if not question.strip():
            raise ValueError("Enter a knowledge search question")
        self.executor.policy.check()
        with self._guard:
            return self._search(question)

    def _search(self, question: str) -> dict:
        key, cached = None, None
        before = self.executor.calls
        try:
            projection = self.ensure_projection()
            key = fingerprint(
                {
                    "projection": projection.key,
                    "question": question,
                    "strategy": "beam",
                    "beam_size": 3,
                    "select_k": 6,
                    "role_policy": self.executor.fingerprint,
                }
            )
            cached = self.executor.request_cache.get(key)
            if isinstance(cached, RetrievalFailure):
                raise cached
            if cached is None:
                with open_runtime(self.selection.views[0].scope.kb_dir) as db:
                    result = ConDB(storage=db).query(
                        projection.tree_id,
                        question,
                        llm=ConDBLLMAdapter(self.executor, "knowledge_retrieval"),
                        strategy="beam",
                        beam_size=3,
                        select_k=6,
                        max_turns=8,
                        cache_enabled=False,  # Request results have their own cache below.
                    )
                    refs = []
                    for identity in result.node_ids:
                        node = db.get_node(projection.tree_id, identity)
                        if node and (
                            reference := json.loads(node.attrs_json or "{}").get("source_ref")
                        ):
                            refs.append(reference)
                self.executor.policy.check()
                self.executor.request_cache[key] = refs
            else:
                refs = cached
            allocation = self.executor.request_cache.setdefault(
                "knowledge_context", {"tokens": 0, "returned": set()}
            )
            hits, remaining = [], 6000 - allocation["tokens"]
            measure = self.executor.question_capacity
            if measure is None:
                raise ValueError("Knowledge context requires captured model capacity")
            for reference in refs:
                hit = self._read(reference)
                identity = fingerprint(reference)
                if identity in allocation["returned"]:
                    hit = {
                        **reference,
                        "coverage": "already_returned",
                        "reading_action": hit["reading_action"],
                    }
                tokens = measure.payload_tokens(hit)
                if tokens > remaining:
                    hit = {
                        **reference,
                        "coverage": "unread",
                        "content": "",
                        "reading_action": hit["reading_action"],
                    }
                    tokens = measure.payload_tokens(hit)
                if tokens <= remaining:
                    hits.append(hit)
                    remaining -= tokens
                    allocation["tokens"] += tokens
                    if hit.get("content"):
                        allocation["returned"].add(identity)
            return {
                "status": "completed",
                "hits": hits,
                "coverage": "selected_candidates",
                "cache_hit": cached is not None,
                "context_tokens": sum(measure.payload_tokens(hit) for hit in hits),
                "question_context_tokens": allocation["tokens"],
                "diagnostics": [
                    "Candidates are partial coverage; missing hits do not prove absence"
                ],
                "stage_sends": self.executor.stage_calls,
            }
        except Exception as error:
            raise_if_terminal(error)
            if key and isinstance(error, RetrievalFailure) and cached is None:
                error.consumed_sends = self.executor.calls - before
                self.executor.request_cache[key] = error
            return {
                "status": "budget_exhausted"
                if isinstance(error, StageBudgetExceeded)
                else "evidence_rejected"
                if "changed after selection" in str(error)
                else "degraded",
                "hits": [],
                "reason_code": error.code
                if isinstance(error, RetrievalFailure)
                else (
                    "retrieval_budget_exhausted"
                    if isinstance(error, StageBudgetExceeded)
                    else "retrieval_failed"
                ),
                "cache_hit": isinstance(cached, RetrievalFailure),
                "consumed_sends": error.consumed_sends
                if isinstance(error, RetrievalFailure)
                else self.executor.calls - before,
                "reading_action": {"tool": "read_file", "arguments": {"path": "index.md"}},
                "coverage": "limited",
                "diagnostics": [
                    str(error),
                    "Continue with read_file(index.md) and original readers",
                ],
                "stage_sends": self.executor.stage_calls,
            }

    def _read(self, reference: dict) -> dict:
        from openkb.source_reading import native_source, reading_action

        view = self.selection.views[0]
        path = reference["path"]
        if view.files.get(path) != reference["content_digest"] or (
            view.source_revisions_by_path.get(path) != reference["source_revision_id"]
        ):
            raise ValueError("Candidate belongs to a different source revision")
        data = read_selected_file(view, path)
        location = reference["native_location"]
        if path.endswith(".content.json"):
            from openkb.agent.evidence_payload import project_evidence
            from openkb.block_package import read_block_selection
            from openkb.text_source import read_text_selection
            from openkb.workbooks.selection import select_cells

            raw = json.loads(data)
            if "blocks" in location:
                selected = read_block_selection(raw, location["blocks"])
            else:
                selected = read_text_selection(raw, location.get("chars"))
                if "cells" in location:
                    selected = select_cells(selected, location["cells"])
            selected = project_evidence(selected, budget=20000)
        elif path.endswith(".json"):
            from openkb.source_pages import read_page_selection

            # Read bounded whole pages; preserve the full candidate range as a follow-up action.
            raw = json.loads(data)
            selected = read_page_selection(raw, location["pages"])
            if selected["diagnostics"]:
                raise ValueError("Candidate native pages are outside the frozen source")
            units, content = [], ""
            for unit in selected["units"][:4]:
                piece = f"[Physical page {unit['page']}]\n{unit['content']}\n"
                if len(content) + len(piece) > 20000:
                    break
                content += piece
                units.append(unit)
            selected = {
                "content": content,
                "units": units,
                "coverage": "partial",
            }
        else:
            text = data.decode("utf-8")
            selected = {
                "content": text if len(text) <= 20000 else "",
                "coverage": "complete" if len(text) <= 20000 else "unread",
            }
        return {
            **reference,
            **selected,
            "reading_action": reading_action(native_source(view, path), location),
            "content_kind": "original_source",
            "interpretation": "Original extraction; generated navigation and image interpretations "
            "are separate content",
        }
