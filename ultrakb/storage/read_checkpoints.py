"""Replay exact reader outputs and their paid ranking calls within retained agent work."""

import json
from contextvars import ContextVar

from litellm import ModelResponse

from ultrakb.compile_checkpoints import digest
from ultrakb.locks import LockCancelled
from ultrakb.mutation import RecoveryRequired

from .compile_binding import model_invocation
from .compile_deliveries import provider_response
from .coordination import storage_stage

_READER: ContextVar["ReaderCheckpoint | None"] = ContextVar("retained_reader", default=None)


class ReaderCheckpoint:
    def __init__(self, work, key):
        self.work, self.key, self.sequence = work, key, 0
        self.failure = None

    def complete(self, executor, role, request):
        key = f"{self.key}:model:{self.sequence}"
        self.sequence += 1
        signature = digest(
            {"role": role, "request": request.payload(), "policy": executor.fingerprint}
        )
        invocation = None
        try:
            self.work.require_request(key, signature)
            raw = self.work.load(key, signature)
            if raw is None:
                with storage_stage(self.work.root, self.work.identity, read_only=True) as db:
                    received = provider_response(db, self.work, key, signature)
                if received is not None:
                    raw = received[0]
                else:
                    invocation = self.work.claim_generation(key, signature, stage="reader.rank.v1")
        except Exception as error:
            self.reject(error)
        # Provider failures retain the existing retrieval failure protocol. Only
        # checkpoint I/O is classified as unconfirmed acceptance.
        if invocation is not None:
            with model_invocation(self.work, invocation):
                result = executor.complete(role, request)
            raw = result.response.model_dump(mode="json", exclude_unset=True)
        try:
            self.work.save(key, signature, raw)
        except Exception as error:
            self.reject(error)
        return executor._result(ModelResponse(**raw), None, key)

    def reject(self, error):
        # The reader's normal error envelope must not swallow a checkpoint failure.
        self.failure = (
            error
            if isinstance(error, (LockCancelled, RecoveryRequired))
            else (
                RecoveryRequired("The received reader model response needs checkpoint acceptance")
            )
        )
        raise self.failure from error


def complete_read(executor, role, request):
    retained = _READER.get()
    return (
        retained.complete(executor, role, request) if retained else executor.complete(role, request)
    )


def checkpoint_reader(tool, work, executor):
    invoke = tool.on_invoke_tool

    async def read(context, arguments):
        call_id = getattr(context, "tool_call_id", None)
        if not call_id:
            raise ValueError("A retained reader requires its accepted SDK tool-call identity")
        key = "maintenance-tool:" + digest(call_id)
        signature = digest({"tool": tool.name, "arguments": json.loads(arguments)})
        try:
            work.require_request(key, signature)
            retained = work.load(key, signature)
        except LockCancelled:
            raise
        except Exception as error:
            raise RecoveryRequired("The reader checkpoint needs recovery") from error
        if retained is None:
            checkpoint = ReaderCheckpoint(work, key)
            token = _READER.set(checkpoint)
            try:
                output = await invoke(context, arguments)
                if checkpoint.failure is not None:
                    raise checkpoint.failure
            finally:
                _READER.reset(token)
            if not isinstance(output, str):
                raise ValueError("A retained reader must return literal text")
            allocation = executor.request_cache.get("knowledge_context", {})
            retained = {
                "output": output,
                "allocation_tokens": allocation.get("tokens", 0),
                "returned": sorted(allocation.get("returned", ())),
                "repairs": executor.request_cache.get("retrieval_repairs", 0),
                "rank_cache": {
                    key: value
                    for key, value in executor.request_cache.items()
                    if isinstance(value, list) and all(isinstance(item, dict) for item in value)
                },
            }
            try:
                work.save(key, signature, retained)
            except LockCancelled:
                raise
            except Exception as error:
                raise RecoveryRequired("The reader result needs checkpoint acceptance") from error
        allocation = executor.request_cache.setdefault(
            "knowledge_context", {"tokens": 0, "returned": set()}
        )
        allocation["tokens"] = max(allocation["tokens"], retained["allocation_tokens"])
        allocation["returned"].update(retained["returned"])
        executor.request_cache["retrieval_repairs"] = max(
            executor.request_cache.get("retrieval_repairs", 0), retained["repairs"]
        )
        executor.request_cache.update(retained["rank_cache"])
        return retained["output"]

    tool.on_invoke_tool = read
