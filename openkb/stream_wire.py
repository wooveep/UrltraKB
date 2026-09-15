"""Observe OpenAI-format SSE counters before SDK normalization invents missing usage."""

import json


class WireStream:
    def __init__(self, iterator):
        self.iterator = iterator
        self.usage = None
        self.finished = False

    def __iter__(self):
        return self

    def __next__(self):
        line = next(self.iterator)
        text = line.decode("utf-8") if isinstance(line, bytes) else line
        if isinstance(text, str) and text.startswith("data:"):
            data = text[5:].strip()
            if data == "[DONE]":
                self.finished = True
            else:
                try:
                    self.observe(json.loads(data))
                except ValueError:
                    pass
        elif hasattr(line, "model_dump"):
            self.observe(line.model_dump(exclude_unset=True))
        elif isinstance(line, dict):
            self.observe(line)
        return line

    def observe(self, value):
        if isinstance(value, dict) and isinstance(value.get("choices"), list):
            self.finished |= any(
                isinstance(choice, dict) and bool(choice.get("finish_reason"))
                for choice in value["choices"]
            )
            usage = value.get("usage")
            if isinstance(usage, dict) and all(
                type(usage.get(key)) is int and usage[key] >= 0
                for key in ("prompt_tokens", "completion_tokens")
            ):
                self.usage = usage

    def close(self):
        close = getattr(self.iterator, "close", None)
        if callable(close):
            close()


def observe_wire(stream):
    # The pinned SDK exposes this iterator before both Usage defaults and
    # synthetic EOF/usage chunks. Other protocols keep their own adapter rules.
    from litellm.llms.openai.chat.gpt_transformation import OpenAIChatCompletionStreamingHandler

    adapter = getattr(stream, "completion_stream", None)
    from openai import Stream

    if isinstance(adapter, Stream):
        observed = WireStream(adapter)
        stream.completion_stream = observed
        return observed
    if not isinstance(adapter, OpenAIChatCompletionStreamingHandler):
        return None
    observed = WireStream(adapter.response_iterator)
    adapter.response_iterator = observed
    return observed
