"""Shared request responses remain separate from source bindings and publication receipts."""

import json
import time
from contextlib import contextmanager

from openkb.agent.analysis_flights import claimed
from openkb.agent.shared_analysis import SharedAnalysis
from openkb.execution_measurement import record_analysis
from openkb.execution_receipt import ModelText
from openkb.locks import atomic_write_json
from openkb.sources import content_id


def _response_record(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"response"}
        or not isinstance(value["response"], str)
    ):
        return False
    try:
        return isinstance(json.loads(value["response"]), dict)
    except ValueError:
        return False


class RequestAnalysis:
    def __init__(self, checkpoints, stage, request, options, *, rules=()):
        self.request = request
        self.shared = SharedAnalysis(
            checkpoints,
            stage,
            request[0]["content"],
            options=options,
            rules=rules,
            value_valid=_response_record,
        )
        self.payload = {"messages": list(request)}
        self.output_tokens = self.shared.output_tokens()

    def load(self):
        value = self.shared.load(self.payload, output_tokens=self.output_tokens)
        if not _response_record(value):
            return None
        decode = getattr(self.request, "decode_response", lambda raw: raw)
        raw = decode(value["response"])
        self.bind()
        return ModelText(raw, self.output_tokens)

    def save(self, raw, *, receipt=None):
        actual = getattr(raw if receipt is None else receipt, "output_tokens", None)
        if type(actual) is not int or actual <= 0:
            return  # Legacy drafts have no known dispatch cap and cannot authorize shared reuse.
        encode = getattr(self.request, "encode_response", lambda raw: raw)
        self.output_tokens = actual
        self.shared.save(self.payload, {"response": encode(raw)}, output_tokens=actual)
        self.bind()

    def bind(self):
        started = time.monotonic()
        shared = self.shared
        record = {
            "analysis": content_id(shared.key(self.payload, output_tokens=self.output_tokens)),
            "source": shared.cp.input,
            "identities": dict(getattr(self.request, "identities", {})),
        }
        path = shared.cp.store.owned_path(
            shared.root / "bindings" / shared.cp.input["version"] / f"{content_id(record)}.json"
        )
        atomic_write_json(path, record)
        record_analysis(shared.stage, "binding", record["analysis"], time.monotonic() - started)

    def run(self, produce, validate, *, cacheable=lambda value: True):
        """Validate a hit again against current original ranges before using it."""
        with self.pending() as raw:
            if raw is not None:
                value = json.loads(raw)
                validate(value)
                return value
            raw = produce()
            value = json.loads(raw)
            validate(value)
            if cacheable(value):
                self.save(raw)
            return value

    @contextmanager
    def pending(self):
        raw = self.load()
        if raw is not None:
            yield raw
            return
        with claimed(self.shared, [self.payload]) as flights:
            flight = flights[0]
            if flight is not None and not flight.owner:
                flight.wait(self.shared.stage)
            raw = self.load()
            if raw is None and flight is not None and not flight.owner:
                from openkb.agent.evidence_retry import ResponseIncomplete

                raise ResponseIncomplete("analysis_executor_incomplete", self.shared.stage)
            yield raw
