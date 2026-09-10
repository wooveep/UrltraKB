"""Account for knowledge-agent requests alongside secondary visual requests."""

import json
from contextlib import contextmanager

from agents import RunHooks

from openkb.config import resolve_effective_config
from openkb.processing import (
    external_request_usage,
    processing_scope,
    request_budget_settings,
)


@contextmanager
def visual_task_budget(kb_dir):
    config = resolve_effective_config(kb_dir)[0]
    if not config.get("image_understanding", {}).get("enabled", False):
        yield
        return
    with processing_scope(config):
        yield


class RequestBudgetHooks(RunHooks):
    def __init__(self):
        self.pending = []

    async def on_llm_start(self, context, agent, system_prompt, input_items):
        limits = request_budget_settings()
        if limits is None:
            return
        # A UTF-8 byte bound is conservative when a custom tokenizer is unknown.
        inputs = len((system_prompt or "").encode()) + len(json.dumps(input_items).encode())
        usage = external_request_usage(inputs + limits["max_tokens"], "knowledge_model")
        self.pending.append((usage, usage.__enter__()))

    async def on_llm_end(self, context, agent, response):
        if not self.pending:
            return
        usage, receipt = self.pending.pop()
        tokens = getattr(response.usage, "total_tokens", None)
        if type(tokens) is int and tokens > 0:
            receipt["tokens"] = tokens
        usage.__exit__(None, None, None)

    def close(self):
        while self.pending:
            usage, _ = self.pending.pop()
            usage.__exit__(None, None, None)
