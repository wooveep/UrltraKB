"""Opt-in request and stage observations without a process-wide profiler."""

import hashlib
import json
import sys
import time
from collections import Counter
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from functools import wraps
from importlib import import_module
from os.path import commonprefix
from pathlib import Path
from threading import RLock
from unittest.mock import patch

from openkb.agent.source_protocol import request_payload
from openkb.locks import atomic_write_text


def serial(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    raise TypeError(type(value).__name__)


class Audit:
    """Observe existing dispatch receipts and a few target functions unchanged."""

    def __init__(self, output, credential):
        self.output, self.credential = output, credential
        self.calls = Counter()
        self.pending = {}
        self.requests, self.responses, self.timings = [], [], []
        self.phase = "step2"
        self.started = time.monotonic()
        self.context = ContextVar("online_audit_request", default=None)
        self.lock = RLock()
        self.first_artifact_seconds = None

    def write(self, name, value):
        text = json.dumps(value, ensure_ascii=False, indent=2, default=serial) + "\n"
        if self.credential:
            text = text.replace(self.credential, "<REDACTED>")
        atomic_write_text(self.output / name, text)
        if name in {"step2-parsed.json", "overview.md", "step3-plan.json"}:
            if self.first_artifact_seconds is None:
                self.first_artifact_seconds = time.monotonic() - self.started

    @contextmanager
    def stage(self, name):
        started, phase = time.monotonic(), self.phase
        try:
            yield
        finally:
            self.timings.append(
                {"phase": phase, "stage": name, "wall_seconds": time.monotonic() - started}
            )

    def _begin(self, measured, options):
        context = self.context.get()
        messages = context["messages"] if context else options.get("messages", [])
        try:
            payload = request_payload(messages)
        except (IndexError, KeyError, TypeError, ValueError):
            payload = {}
        row = {
            "phase": self.phase,
            "stage": context["stage"] if context else measured["operation"],
            "messages": list(messages),
            "inverse": dict(getattr(messages, "inverse", {})),
            "evidence_sha256": hashlib.sha256(
                json.dumps(payload.get("evidence"), ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest(),
            "options": measured["effective_options"],
            "measurement_id": measured["id"],
            "decode_response": context.get("decode_response", True) if context else True,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "options_semantics": "safe_display_projection_not_sdk_replay_parameters",
        }
        with self.lock:
            content = messages[-1]["content"] if messages else ""
            prefix = content.split(
                ',"subtask":' if payload.get("planning_context") else ',"stage":', 1
            )[0]
            if payload.get("planning_dialogue") == "v1":
                # The reusable source prefix is now a complete earlier message.
                prefix = json.dumps(list(messages[:2]), ensure_ascii=False, separators=(",", ":"))
                content = json.dumps(list(messages), ensure_ascii=False, separators=(",", ":"))
            previous = self.requests[-1]["messages"] if self.requests else []
            previous_content = (
                json.dumps(previous, ensure_ascii=False, separators=(",", ":"))
                if payload.get("planning_dialogue") == "v1"
                else previous[-1]["content"]
                if previous
                else ""
            )
            blocks = payload.get("evidence", {}).get("blocks", [])
            row.update(
                prefix_sha256=hashlib.sha256(prefix.encode()).hexdigest(),
                prefix_chars=len(prefix),
                previous_common_prefix_chars=len(commonprefix([previous_content, content]))
                if self.requests and self.requests[-1]["messages"]
                else None,
                original_block_occurrences=len(blocks),
                unique_original_blocks=len({b.get("id") for b in blocks}),
                planning_context_sha256=hashlib.sha256(
                    json.dumps(
                        payload["planning_context"], ensure_ascii=False, sort_keys=True
                    ).encode()
                ).hexdigest()
                if "planning_context" in payload
                else None,
            )
            self.requests.append(row)
            number = len(self.requests)
            self.pending[measured["id"]] = (number, row, messages, measured)
            self.write(f"request-{number:02d}.json", row)
            self.write(
                f"response-{number:02d}.json",
                {"request": number, "phase": self.phase, "pending": True},
            )
        print(
            json.dumps({"request": number, "phase": self.phase, "stage": row["stage"]}), flush=True
        )

    def _response(self, measured, usage, response):
        with self.lock:
            saved = self.pending.pop(measured["id"], None)
            if saved is None:
                return
            number, request, messages, _ = saved
            choices = getattr(response, "choices", [])
            content = choices[0].message.content if choices else None
            row = {
                "request": number,
                "phase": request["phase"],
                "elapsed_seconds": measured["request_seconds"],
                "finish_reason": choices[0].finish_reason if choices else None,
                "usage": usage,
                "provider_content": content,
                "measurement": dict(measured),
            }
            try:
                row["decoded_content"] = (
                    getattr(messages, "decode_response", lambda v: v)(content)
                    if request["decode_response"]
                    else content
                )
            except (ValueError, TypeError, KeyError) as exc:
                row["decode_error"] = type(exc).__name__
            self.responses.append(row)
            self.write(f"response-{number:02d}.json", row)

    @contextmanager
    def capture(self):
        for module in (
            "openkb.parsing",
            "openkb.navigation",
            "openkb.navigation_structure",
            "openkb.agent.evidence_compiler",
            "openkb.agent.document_pages",
            "openkb.knowledge_commit",
        ):
            import_module(module)
        from openkb.agent import compiler
        from openkb.evidence import ParseStore
        from openkb.execution_measurement import Measurement

        original_call = compiler._llm_call
        original_begin, original_usage = Measurement.begin_request, Measurement.provider_usage
        original_reader = ParseStore.reader

        @wraps(original_call)
        def call(model, messages, step_name, *args, **kwargs):
            token = self.context.set(
                {
                    "messages": messages,
                    "stage": step_name,
                    "decode_response": kwargs.get("decode_response", True),
                }
            )
            try:
                return original_call(model, messages, step_name, *args, **kwargs)
            finally:
                self.context.reset(token)

        def begin(measurement, *args, **kwargs):
            row = original_begin(measurement, *args, **kwargs)
            self._begin(row, kwargs.get("options") or {})
            return row

        def usage(measurement, row, usage, *, response=None):
            original_usage(measurement, row, usage, response=response)
            self._response(row, usage, response)

        @wraps(original_reader)
        def reader(*args, **kwargs):
            self.calls[self.phase + ":reader_validation"] += 1
            with self.stage("reader_validation"):
                return original_reader(*args, **kwargs)

        def observe(function, name):
            @wraps(function)
            def wrapped(*args, **kwargs):
                self.calls[self.phase + ":" + name] += 1
                with self.stage(name):
                    return function(*args, **kwargs)

            return wrapped

        # Replace only already-loaded aliases of these exact target functions.
        # Later imports obtain the wrapped defining-module exports as usual.
        names = {
            "parse_document",
            "prepare_navigation",
            "generate_document_page",
            "publish_proposal",
            "compile_evidence",
            "_window",
        }
        wrappers = {original_call: call}
        with ExitStack() as stack:
            stack.enter_context(patch.object(Measurement, "begin_request", begin))
            stack.enter_context(patch.object(Measurement, "provider_usage", usage))
            stack.enter_context(patch.object(ParseStore, "reader", reader))
            for module_name, module in list(sys.modules.items()):
                if module is None or not module_name.startswith("openkb."):
                    continue
                for name, value in list(vars(module).items()):
                    if name == "_llm_call" and value is original_call:
                        stack.enter_context(patch.object(module, name, call))
                    elif name in names and callable(value):
                        if value not in wrappers:
                            wrappers[value] = observe(value, name)
                        stack.enter_context(patch.object(module, name, wrappers[value]))
            try:
                yield self
            finally:
                for number, request, _, measured in self.pending.values():
                    self.write(
                        f"response-{number:02d}.json",
                        {
                            "request": number,
                            "phase": request["phase"],
                            "response_observed": False,
                            "measurement": dict(measured),
                            "usage": None,
                        },
                    )
                self.write(
                    "phase-timings.json",
                    {
                        "stages": self.timings,
                        "calls": dict(self.calls),
                        "first_artifact_seconds": self.first_artifact_seconds,
                    },
                )
