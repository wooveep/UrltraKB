"""PageIndex's narrow runtime adapter; no vendor dependency on UltraKB."""

import asyncio
from concurrent.futures import Future
from contextlib import nullcontext
from threading import Lock

from pageindex.llm import IndexResponse

from ultrakb.llm_execution import CompletionExecutor, ModelRequest


class PageIndexLLM:
    def __init__(self, executor: CompletionExecutor):
        self.executor = executor
        self.checkpoints = None
        self._responses: dict[str, Future[IndexResponse]] = {}
        self._response_lock = Lock()

    def _request(self, messages, stage):
        return ModelRequest(
            messages,
            operation="document_index",
            stage=stage,
            prompt_version="pageindex-content-v1",
            generation_options={"temperature": 0, "drop_params": True},
        )

    @staticmethod
    def _response(result):
        return IndexResponse(
            result.text, "max_output_reached" if result.finish_reason == "length" else "finished"
        )

    def fits(self, messages, *, stage):
        from ultrakb.request_budget import fits_request

        request = self._request(messages, stage)
        measure = self.executor.request_measure("document_index")
        if measure is None:
            from ultrakb.model_tokenizer import ContextCapacityError

            raise ContextCapacityError("Document index capacity is unknown")
        return fits_request(
            measure,
            self.executor.prepare_request("document_index", request),
            operation="document_index",
        )

    def stage_result(self, name, value=None):
        if self.checkpoints is None:
            return None
        if value is None:
            return self.checkpoints.load("stage:" + name)
        self.checkpoints.save("stage:" + name, value)

    def _key(self, messages, stage):
        from ultrakb.execution_capacity import request_input
        from ultrakb.index_stages import digest

        request = self._request(messages, stage)
        return "response:" + digest(
            {
                "stage": stage,
                "input": request_input(self.executor.prepare_request("document_index", request)),
            }
        )

    def _load(self, key):
        self.executor.policy.check()
        return self.checkpoints.load(key) if self.checkpoints else None

    def retained_response(self, messages, *, stage):
        """Read an exact frozen request's delivered response without sending it."""
        self.executor.policy.check()
        key = self._key(messages, stage)
        with self._response_lock:
            pending = self._responses.get(key)
        if pending is not None:
            return pending.result() if pending.done() else None
        cached = self._load(key)
        return IndexResponse(**cached) if cached is not None else None

    def _claim(self, key):
        with self._response_lock:
            if key in self._responses:
                return self._responses[key], False
            future: Future[IndexResponse] = Future()
            self._responses[key] = future
            return future, True

    def _receive(self, key):
        def save(response):
            if self.checkpoints:
                choice = response.choices[0]
                self.checkpoints.save(
                    key,
                    {
                        "text": choice.message.content or "",
                        "finish_reason": (
                            "max_output_reached" if choice.finish_reason == "length" else "finished"
                        ),
                    },
                )

        return save

    def _request_binding(self, key, stage):
        claim = getattr(self.checkpoints, "request", None)
        return claim(key, stage) if claim else nullcontext()

    @staticmethod
    def may_defer(error):
        import litellm
        from pageindex.errors import IndexCapacityError

        from ultrakb.model_tokenizer import ContextCapacityError

        return isinstance(error, (ContextCapacityError, IndexCapacityError, litellm.APIError))

    def complete(self, messages, *, stage):
        key = self._key(messages, stage)
        self.executor.policy.check()
        future, owner = self._claim(key)
        if owner:
            try:
                if (cached := self._load(key)) is not None:
                    result = IndexResponse(**cached)
                else:
                    with self._request_binding(key, stage):
                        result = self._response(
                            self.executor.complete(
                                "document_index",
                                self._request(messages, stage),
                                on_response=self._receive(key),
                            )
                        )
            except BaseException as error:
                future.set_exception(error)
                raise
            future.set_result(result)
        return future.result()

    async def acomplete(self, messages, *, stage):
        key = self._key(messages, stage)
        self.executor.policy.check()
        future, owner = self._claim(key)
        if not owner:
            return await asyncio.shield(asyncio.wrap_future(future))
        try:
            if (cached := self._load(key)) is not None:
                result = IndexResponse(**cached)
            else:
                with self._request_binding(key, stage):
                    result = self._response(
                        await self.executor.acomplete(
                            "document_index",
                            self._request(messages, stage),
                            on_response=self._receive(key),
                        )
                    )
        except BaseException as error:
            future.set_exception(error)
            raise
        future.set_result(result)
        return result
