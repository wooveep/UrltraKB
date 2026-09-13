"""Retain provider length stops that the pinned Agents adapter otherwise drops."""

from contextvars import ContextVar

from agents.extensions.models.litellm_model import LitellmModel
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

_ENDING: ContextVar[dict | None] = ContextVar("knowledge_response_ending", default=None)


def _incomplete(output):
    # A truncated tool call must not execute, even if its partial arguments parse.
    messages = [item for item in output if item.type == "message"]
    return [item.model_copy(update={"status": "incomplete"}) for item in messages] or [
        ResponseOutputMessage(
            id="incomplete-answer",
            type="message",
            role="assistant",
            status="incomplete",
            content=[ResponseOutputText(type="output_text", text="", annotations=[])],
        )
    ]


class CompletionAwareModel(LitellmModel):
    async def _fetch_response(self, *args, **kwargs):
        value = await super()._fetch_response(*args, **kwargs)
        ending = _ENDING.get()
        if ending is None:
            return value

        def observe(response):
            if response.choices and response.choices[0].finish_reason == "length":
                ending["truncated"] = True

        if not kwargs.get("stream"):
            observe(value)
            return value
        response, stream = value

        async def observed():
            try:
                async for chunk in stream:
                    observe(chunk)
                    yield chunk
            finally:
                if close := getattr(stream, "aclose", None):
                    await close()

        return response, observed()

    async def get_response(self, *args, **kwargs):
        ending = {}
        token = _ENDING.set(ending)
        try:
            response = await super().get_response(*args, **kwargs)
            if ending.get("truncated"):
                response.output = _incomplete(response.output)
            return response
        finally:
            _ENDING.reset(token)

    async def stream_response(self, *args, **kwargs):
        ending = {}
        token = _ENDING.set(ending)
        try:
            async for event in super().stream_response(*args, **kwargs):
                if event.type == "response.completed" and ending.get("truncated"):
                    event = event.model_copy(
                        update={
                            "response": event.response.model_copy(
                                update={"output": _incomplete(event.response.output)}
                            )
                        }
                    )
                yield event
        finally:
            _ENDING.reset(token)


def answer_truncated(result):
    responses = getattr(result, "raw_responses", [])
    return bool(responses) and any(
        getattr(item, "status", None) == "incomplete" for item in responses[-1].output
    )
