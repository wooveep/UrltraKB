"""Explicit standalone provider; CTree itself never reads credentials."""
from typing import Protocol


class ChatLLM(Protocol):
    def complete(self, messages: list[dict], *, stage: str, prompt_version: str) -> str: ...


class LiteLLMClient:
    def __init__(self, *, model, api_key, base_url=None, timeout=None):
        self.options = dict(model=model, api_key=api_key, base_url=base_url,
                            num_retries=0, max_retries=0)
        if timeout is not None:
            self.options["timeout"] = timeout

    def complete(self, messages, *, stage, prompt_version):
        import litellm
        response = litellm.completion(messages=messages, **self.options)
        return response.choices[0].message.content or ""


def create_client(*, model, api_key, base_url=None, timeout=None):
    return LiteLLMClient(model=model, api_key=api_key, base_url=base_url, timeout=timeout)
