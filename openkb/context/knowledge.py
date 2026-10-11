"""ConDB selects candidates; only OpenKB's pinned original reader supplies evidence."""

from __future__ import annotations

import json
import threading

from contextdb import ConDB

from openkb.application.query_views import QuerySelection, read_selected_file
from openkb.context.condb_llm import ConDBLLMAdapter, RetrievalFailure
from openkb.context.projection import ensure_projection, fingerprint, open_runtime
from openkb.llm_execution import CompletionExecutor, StageBudgetExceeded, raise_if_terminal


def _source_selection_identity(reference: dict) -> str:
    return fingerprint(
        {
            key: reference.get(key)
            for key in (
                "path",
                "source_revision_id",
                "content_digest",
                "unit_revision_id",
                "native_location",
            )
        }
    )


class KnowledgeRetrieval:
    def __init__(self, selection: QuerySelection, executor: CompletionExecutor, *, images=None):
        self.selection, self.executor = selection, executor
        self._guard = threading.Lock()
        self.images = images

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
            from openkb.application.execution import activity, current_execution

            with activity("reading"):
                # A navigation title is not a distinct evidence identity.
                refs = list(
                    {
                        _source_selection_identity(reference): reference for reference in refs
                    }.values()
                )
                verified = {}
                for number, reference in enumerate(refs):
                    self.executor.policy.check()
                    path = reference["path"]
                    view = self.selection.views[0]
                    if view.files.get(path) != reference["content_digest"] or (
                        view.source_revisions_by_path.get(path) != reference["source_revision_id"]
                    ):
                        raise ValueError("Candidate belongs to a different source revision")
                    if path not in verified:
                        verified[path] = read_selected_file(view, path)
                    identity = _source_selection_identity(reference)
                    if identity in allocation["returned"]:
                        from openkb.source_reading import native_source, reading_action

                        hit = {
                            **reference,
                            "coverage": "already_returned",
                            "reading_action": reading_action(
                                native_source(view, path, data=verified[path]),
                                reference["native_location"],
                            ),
                        }
                    else:
                        # Reserve a share for each complementary candidate; unused space
                        # naturally becomes available to the remaining candidates.
                        hit = self._read(
                            reference,
                            data=verified[path],
                            token_budget=remaining // (len(refs) - number),
                        )
                    tokens = measure.payload_tokens(hit)
                    if tokens <= remaining:
                        hits.append(hit)
                        remaining -= tokens
                        allocation["tokens"] += tokens
                        if hit.get("content"):
                            allocation["returned"].add(identity)
                    else:
                        from openkb.source_reading import native_source, reading_action

                        # The automatic evidence allocation is exhausted, not the
                        # source. Native actions still pass the whole-question
                        # capacity gate at the query-tool boundary.
                        hits.append(
                            {
                                "path": path,
                                "coverage": "context_allocation_exhausted",
                                "reading_action": reading_action(
                                    native_source(view, path, data=verified[path]),
                                    reference["native_location"],
                                ),
                            }
                        )
                if execution := current_execution():
                    execution.record_reads({"hits": hits})
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
            from openkb.llm_execution import AnswerReserveReached

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
                else "answer_finish_reserved"
                if isinstance(error, AnswerReserveReached)
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

    def _read(
        self, reference: dict, *, data: bytes | None = None, token_budget: int | None = None
    ) -> dict:
        from openkb.agent.evidence_payload import project_evidence
        from openkb.original_context import original_context
        from openkb.source_reading import native_source, reading_action

        view = self.selection.views[0]
        path = reference["path"]
        if view.files.get(path) != reference["content_digest"] or (
            view.source_revisions_by_path.get(path) != reference["source_revision_id"]
        ):
            raise ValueError("Candidate belongs to a different source revision")
        if data is None:
            data = read_selected_file(view, path)
        location = reference["native_location"]
        source = native_source(view, path, data=data)
        raw = json.loads(data) if path.endswith(".json") else data.decode("utf-8")
        if path.endswith(".content.json"):
            assert isinstance(raw, dict)
            from openkb.block_package import read_block_selection
            from openkb.text_source import read_text_selection
            from openkb.workbooks.selection import select_cells

            if "blocks" in location:
                selected = read_block_selection(raw, location["blocks"])
            else:
                selected = read_text_selection(raw, location.get("chars"))
                if "cells" in location:
                    selected = select_cells(selected, location["cells"])
        elif path.endswith(".json"):
            from openkb.source_pages import read_page_selection

            selected = read_page_selection(raw, location["pages"])
            if selected["diagnostics"]:
                raise ValueError("Candidate native pages are outside the frozen source")
        else:
            a, b = map(int, location["chars"].split(":"))
            selected = {"content": raw[a:b], "source_spans": [[a, b]]}

        action = reading_action(source, location)

        def assemble(window):
            result = {
                **reference,
                **window,
                "source_context": original_context(source, raw, location, selected=window),
                "reading_action": action,
                "content_kind": "original_source",
                "interpretation": source.get(
                    "interpretation",
                    "Original extraction; generated navigation and image "
                    "interpretations are separate content",
                ),
                "range_complete": window.get("coverage") == "complete",
            }
            if window.get("continuation") is not None:
                result["continuation_action"] = reading_action(
                    source, {**location, "offset": window["continuation"]}
                )
            if self.images and "units" in window:
                result["image_coverage"] = self.images.listed(source, window["units"])
            return result

        measure = self.executor.question_capacity
        fits = None
        if token_budget is not None:
            if measure is None:
                raise ValueError("Knowledge context requires captured model capacity")

            def fits(window):
                return measure.payload_tokens(assemble(window)) <= token_budget

        return assemble(project_evidence(selected, fits=fits))
