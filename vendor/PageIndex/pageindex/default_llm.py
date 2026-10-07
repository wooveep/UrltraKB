"""Explicit standalone LiteLLM client; integrations inject their own IndexLLM."""
import litellm

from .config import get_llm_params
from .llm import IndexResponse


class DefaultIndexLLM:
    def __init__(self, model):
        self.model = model.removeprefix("litellm/") if model else model

    def _options(self, messages):
        return {"model": self.model, "messages": messages, **get_llm_params(),
                "num_retries": 0, "max_retries": 0}

    @staticmethod
    def _result(response):
        choice = response.choices[0]
        return IndexResponse(choice.message.content or "", "max_output_reached"
                             if getattr(choice, "finish_reason", None) == "length" else "finished")

    def complete(self, messages, *, stage):
        from .index.utils import _usage_call
        with _usage_call(self.model, stage) as usage:
            response = litellm.completion(**self._options(messages))
            if usage:
                usage.finish(response)
        return self._result(response)

    async def acomplete(self, messages, *, stage):
        from .index.utils import _usage_call
        with _usage_call(self.model, stage) as usage:
            response = await litellm.acompletion(**self._options(messages))
            if usage:
                usage.finish(response)
        return self._result(response)
