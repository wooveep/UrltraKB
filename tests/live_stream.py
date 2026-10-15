"""Decode original DeepSeek SSE frames and settle their real usage receipt."""

import copy

import httpx
from live_errors import ProviderHTTPError
from live_network import admitted_transport
from live_sse import StreamCapture


class DeepSeekStream:
    def __init__(self, owner, payload, fingerprint, identifier, period, headers, timeout, *, lease):
        self.owner = owner
        self.payload = payload
        self.fingerprint = fingerprint
        self.identifier = identifier
        self.period = period
        self.headers = headers
        self.timeout = timeout
        self._lease = lease
        self._client = None
        self._response = None
        self._lines = None
        self._capture = StreamCapture()
        self._chunks = self._capture.chunks
        self._settled = False
        self._closed = False

    async def start(self):
        self._client = httpx.AsyncClient(timeout=self.timeout, trust_env=False)
        try:
            request = self._client.build_request(
                "POST", self.owner._url, headers=self.headers, json=self.payload
            )
            with admitted_transport(
                self._client, self.owner.budget, self.identifier, self.fingerprint
            ):
                self._response = await self._client.send(request, stream=True)
            if not self._response.is_success:
                await self._response.aread()
                raise ProviderHTTPError(self._response)
            self._lines = self._response.aiter_lines().__aiter__()
            return self
        except BaseException as error:
            self.owner.budget.uncertain(self.identifier, type(error).__name__)
            await self._client.aclose()
            raise

    def __aiter__(self):
        return self

    def _accept(self, value):
        chunk = self._capture.accept(value)
        if not self._settled and self._capture.usage is not None:
            self.owner.budget.settle(self.identifier, self._capture.usage, peak=self.period)
            self._settled = True
            self.owner.receipts.append(
                {"fingerprint": self.fingerprint, "usage": copy.deepcopy(self._capture.usage)}
            )
        return chunk

    async def __anext__(self):
        from litellm import ModelResponseStream

        if self._closed:
            raise StopAsyncIteration
        try:
            async for line in self._lines:
                if not line.startswith("data:"):
                    continue
                chunk = self._accept(line[5:].strip())
                if chunk is None:
                    await self.aclose()
                    raise StopAsyncIteration
                return ModelResponseStream(**chunk)
            await self.aclose()
            raise StopAsyncIteration
        except StopAsyncIteration:
            raise
        except BaseException as error:
            self._capture.failed = True
            self.owner.budget.uncertain(self.identifier, type(error).__name__)
            await self.aclose()
            raise

    async def aclose(self):
        if self._closed:
            return
        # Cancellation/early consumer close still drains the provider's final
        # receipt; it never makes another model request or invents stream chunks.
        try:
            if not self._capture.failed and not self._capture.done and self._lines is not None:
                async for line in self._lines:
                    if not line.startswith("data:"):
                        continue
                    if self._accept(line[5:].strip()) is None:
                        break
            if self._capture.cacheable:
                self.owner._save_response(self.fingerprint, {"stream_chunks": self._chunks})
            if not self._settled:
                self.owner.budget.uncertain(self.identifier, "stream_missing_usage")
        except BaseException as error:
            self._capture.failed = True
            self.owner.budget.uncertain(self.identifier, type(error).__name__)
            raise
        finally:
            self._closed = True
            try:
                if self._response is not None:
                    await self._response.aclose()
                if self._client is not None:
                    await self._client.aclose()
            finally:
                self._lease.__exit__(None, None, None)


class ReusedStream:
    """Unchanged frames from an identical real request in the same test run."""

    def __init__(self, chunks):
        self._chunks = iter(copy.deepcopy(chunks))
        self._closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        from litellm import ModelResponseStream

        if self._closed:
            raise StopAsyncIteration
        value = next(self._chunks, None)
        if value is None:
            raise StopAsyncIteration
        return ModelResponseStream(**value)

    async def aclose(self):
        self._closed = True
