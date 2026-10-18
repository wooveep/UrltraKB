"""Observe consumed HTTP body bytes without changing the SDK's stream protocol."""

from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class _BodyReceipt:
    call: Any
    identity: str
    response: httpx.Response
    content: bytearray = field(default_factory=bytearray)
    finished: bool = False

    def finish(self, *, complete: bool):
        if self.finished:
            return
        if not complete and not self.content:
            return
        # Decode through HTTPX's own Content-Encoding handling. Do not mutate
        # the response that the provider SDK is currently consuming.
        try:
            observed = httpx.Response(
                self.response.status_code,
                headers=self.response.headers,
                content=bytes(self.content),
                request=self.response.request,
            )
        except httpx.DecodingError:
            # Malformed provider encoding must not discard bytes already received.
            observed = httpx.Response(
                self.response.status_code,
                content=bytes(self.content),
                request=self.response.request,
            )
            complete = False
        try:
            self.call.received(self.identity, observed, complete=complete)
        except Exception as error:
            if self.call.scope.storage.failure is error:
                self.finished = True  # Its durable repair packet now owns these bytes.
                self.content.clear()
            raise
        self.finished = True
        self.content.clear()


class _SyncReceiptStream(httpx.SyncByteStream):
    def __init__(self, stream, receipt):
        self.stream, self.receipt = stream, receipt

    def __iter__(self):
        for chunk in self.stream:
            self.receipt.content.extend(chunk)
            yield chunk
        self.receipt.finish(complete=True)

    def close(self):
        try:
            self.receipt.finish(complete=False)
        finally:
            self.stream.close()


class _AsyncReceiptStream(httpx.AsyncByteStream):
    def __init__(self, stream, receipt):
        self.stream, self.receipt = stream, receipt

    async def __aiter__(self):
        async for chunk in self.stream:
            self.receipt.content.extend(chunk)
            yield chunk
        self.receipt.finish(complete=True)

    async def aclose(self):
        try:
            self.receipt.finish(complete=False)
        finally:
            await self.stream.aclose()


def observe_response_stream(call, identity: str, response: httpx.Response):
    if call.scope is None or call.scope.storage is None or response.is_stream_consumed:
        return
    receipt = _BodyReceipt(call, identity, response)
    call.body_receipts.append(receipt)
    response.stream = (
        _AsyncReceiptStream(response.stream, receipt)
        if isinstance(response.stream, httpx.AsyncByteStream)
        else _SyncReceiptStream(response.stream, receipt)
    )
