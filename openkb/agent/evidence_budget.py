"""Per-question limits around the SDK model boundary, including its retries."""

from dataclasses import replace

from agents import RunConfig
from agents.models.interface import Model

from openkb.agent.token_usage import TokenUsage, add_usage
from openkb.llm_execution import ModelBudgetExceeded

MAX_GENERATION_REQUESTS = 21
MAX_QUESTION_REQUESTS = 24  # Leave room for review, one repair and re-review.
MAX_OBSERVED_TOKENS = 400000
MAX_TOOL_BYTES = 320000


class EvidenceBudget:
    def __init__(self):
        self.requests = self.observed_tokens = self.tool_bytes = 0
        self.reviewing = False
        self.usage = None
        self.stop_reason: str | None = None

    def reserve(self):
        limit = MAX_QUESTION_REQUESTS if self.reviewing else MAX_GENERATION_REQUESTS
        if self.requests >= limit or self.observed_tokens >= MAX_OBSERVED_TOKENS:
            self.stop_reason = "question_model_budget"
            raise ModelBudgetExceeded("Question model budget exhausted")
        self.requests += 1

    def observe(self, response):
        usage = getattr(response, "openkb_usage", getattr(response, "usage", None))
        self.usage = add_usage(self.usage, TokenUsage.from_provider(usage).to_dict())
        for key in ("input_tokens", "output_tokens"):
            value = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
            if type(value) is int and value >= 0:
                self.observed_tokens += value

    def tool_output(self, output):
        # UTF-8 bytes bound text tokens conservatively without assuming a provider
        # tokenizer. Count repeated returns too, not only unique original reads.
        text = (
            output
            if isinstance(output, str)
            else "\n".join(getattr(item, "text", "") for item in output if hasattr(item, "text"))
            if isinstance(output, list)
            else getattr(output, "text", "")
        )
        size = len(text.encode("utf-8"))
        if self.tool_bytes + size > MAX_TOOL_BYTES:
            self.stop_reason = "question_tool_budget"
            raise ValueError("Question tool-output budget exhausted; answer verified parts only")
        self.tool_bytes += size
        return output


class _BudgetModel(Model):
    def __init__(self, model, budget: EvidenceBudget, provider):
        self.model, self.budget, self.provider = model, budget, provider

    @property
    def inner(self):
        if not isinstance(self.model, Model):
            self.model = self.provider.get_model(self.model)
        return self.model

    def get_retry_advice(self, request):
        return self.inner.get_retry_advice(request)

    async def get_response(self, *args, **kwargs):
        self.budget.reserve()
        response = await self.inner.get_response(*args, **kwargs)
        self.budget.observe(response)
        return response

    async def stream_response(self, *args, **kwargs):
        self.budget.reserve()
        stream = self.inner.stream_response(*args, **kwargs)
        try:
            async for event in stream:
                if event.type == "response.completed":
                    self.budget.observe(event.response)
                yield event
        finally:
            await stream.aclose()


def budget_run_config(agent, run_config, budget: EvidenceBudget):
    """Preserve configured provider, credentials, retry policy and tracing."""
    config = run_config or RunConfig()
    model = config.model or agent.model
    return replace(config, model=_BudgetModel(model, budget, config.model_provider))


async def bounded_review_run(*args, **kwargs):
    from agents import Runner

    try:
        return await Runner.run(*args, **kwargs)
    except ModelBudgetExceeded:
        return None
