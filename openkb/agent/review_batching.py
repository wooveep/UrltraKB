"""Coalesce at most three small candidates sharing exact original evidence."""

import json
import threading
from concurrent.futures import Future, TimeoutError
from contextlib import contextmanager
from contextvars import ContextVar

from openkb.agent.evidence_units import JSON_FORMAT, messages
from openkb.agent.model_json import unique_fields
from openkb.config import compilation_model_options
from openkb.processing import (
    InputTooLarge,
    ProcessingIncomplete,
    RequestLimits,
    processing_checkpoint,
)
from openkb.sources import content_id

_BATCHER = ContextVar("related_review_batcher", default=None)
CONTRACT = (
    "\nReview each candidate independently under the preceding rules. Return "
    '{"reviews":[{"id":"exact supplied id","review":{the individual verdict, reason, '
    "issues and advisories}}]}. Cover every id exactly once. Occurrence IDs belong "
    "only to their own candidate. Never return one verdict for the whole batch."
)


class ReviewBatcher:
    def __init__(self):
        self.lock = threading.RLock()
        self.pending = {}

    def review(self, payload, system, settings, checkpoints, bundle, single):
        individual = self._individual_key(payload, system, settings, checkpoints)
        if checkpoints.load_recovery(individual, "review") is not None:
            return single()
        # Exact source occurrence identities are stronger than merely sharing a
        # heading. Different operations and corrected candidates remain separate.
        if payload.get("correction_review") or len(json.dumps(payload)) > 16000:
            return single()
        key = content_id(
            {
                "scopes": sorted(
                    {
                        content_id(row["scope"] if "scope" in row else row["reference"])
                        for row in payload["evidence"]
                    }
                ),
                "omissions": payload.get("known_omissions"),
                "system": system,
                "options": compilation_model_options(settings, verification=True),
            }
        )
        future = Future()
        with self.lock:
            leader = key not in self.pending
            self.pending.setdefault(key, []).append((payload, future, single))
        if leader:
            # A short finite collection window never waits for a queued task that
            # needs this worker. With one ready candidate use the normal path.
            threading.Event().wait(0.05)
            with self.lock:
                rows = self.pending.pop(key)
            try:
                for start in range(0, len(rows), 3):
                    chunk = rows[start : start + 3]
                    value = self._dispatch(chunk, system, settings, checkpoints, bundle)
                    for (_, waiting, _), review in zip(chunk, value, strict=True):
                        if isinstance(review, BaseException):
                            waiting.set_exception(review)
                        else:
                            waiting.set_result(review)
            except BaseException as exc:
                for _, waiting, _ in rows:
                    if not waiting.done():
                        waiting.set_exception(exc)
        while True:
            processing_checkpoint("generation")
            try:
                return future.result(timeout=0.05)
            except TimeoutError:
                continue

    def _dispatch(self, rows, system, settings, checkpoints, bundle):
        from openkb.agent.compiler import _llm_call
        from openkb.agent.evidence_verifier import _parse_review

        if len(rows) == 1:
            return [rows[0][2]()]
        candidates = [{"id": content_id(payload), **payload} for payload, _, _ in rows]
        payload = {"stage": "verification_batch", "candidates": candidates}
        request = messages(system + CONTRACT, payload)
        options = {
            "response_format": JSON_FORMAT,
            **compilation_model_options(settings, verification=True),
        }
        limits = RequestLimits.from_config(settings)
        try:
            _, tokens = limits.request(settings["model"], request, options)
            if tokens > 24000:
                raise InputTooLarge()
        except InputTooLarge:
            if len(rows) == 1:
                raise
            return [
                review
                for row in rows
                for review in self._dispatch([row], system, settings, checkpoints, bundle)
            ]
        key = checkpoints.review_key(
            [*request, {"candidate_identities": [row["id"] for row in candidates]}],
            settings["model"],
            options,
            0,
        )
        saved = checkpoints.load_recovery(key, "review")
        if saved is None:
            raw = _llm_call(settings["model"], request, "verification", bundle=bundle, **options)
            saved = {"response": str(raw)}
            checkpoints.save_recovery(key, "review", saved)
        try:
            value = json.loads(saved["response"], object_pairs_hook=unique_fields)
            if set(value) != {"reviews"} or not isinstance(value["reviews"], list):
                raise ValueError("Invalid batch review")
            mapping = {}
            for row in value["reviews"]:
                if set(row) != {"id", "review"} or row["id"] in mapping:
                    raise ValueError("Duplicate or malformed batch verdict")
                mapping[row["id"]] = row["review"]
            if set(mapping) != {candidate["id"] for candidate in candidates}:
                raise ValueError("Missing independent candidate verdict")
            reviews = []
            for (individual, _, _), candidate in zip(rows, candidates, strict=True):
                raw = json.dumps(mapping[candidate["id"]])
                try:
                    review = _parse_review(raw, candidate)
                except ProcessingIncomplete as exc:
                    reviews.append(exc)
                else:
                    key = self._individual_key(individual, system, settings, checkpoints)
                    checkpoints.save_recovery(key, "review", {"response": raw})
                    reviews.append(review)
            return reviews
        except (ValueError, TypeError, KeyError):
            raise ProcessingIncomplete("evidence_verification_invalid", "generation") from None

    @staticmethod
    def _individual_key(payload, system, settings, checkpoints):
        request = messages(system, payload)
        return checkpoints.review_key(
            [*request, {"omission_identity": content_id(payload["known_omissions"])}]
            if payload.get("known_omissions")
            else request,
            settings["model"],
            compilation_model_options(settings, verification=True),
            0,
        )


def current_batcher():
    return _BATCHER.get()


@contextmanager
def review_batching():
    token = _BATCHER.set(ReviewBatcher())
    try:
        yield
    finally:
        _BATCHER.reset(token)
