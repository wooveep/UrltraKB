"""Planner admission leaves one actual pages request after overview traversal."""

from openkb.processing import ProcessingIncomplete, remaining_request_budget


class PlanningBudget:
    def __init__(self, limits, settings, *, mock=False):
        self.mock = mock
        self.limits = limits
        self.settings = settings
        self.requests = 0
        self.tokens = 0

    def cost(self, messages):
        options, tokens = self.limits.request(self.settings["model"], messages, {})
        return tokens + options["max_tokens"]

    def admit(self, messages, *, reserve_pages=0):
        cost = self.cost(messages)
        remaining = None if self.mock else remaining_request_budget()
        if remaining is None:
            remaining = {
                "requests": None
                if self.limits.max_requests is None
                else self.limits.max_requests - self.requests,
                "tokens": None
                if self.limits.max_tokens is None
                else self.limits.max_tokens - self.tokens,
            }
        if remaining["requests"] is not None and remaining["requests"] < 1 + bool(reserve_pages):
            raise ProcessingIncomplete(
                "pages_request_reserved" if reserve_pages else "request_budget_exhausted",
                "planning",
            )
        if remaining["tokens"] is not None and remaining["tokens"] < cost + reserve_pages:
            raise ProcessingIncomplete(
                "pages_tokens_reserved" if reserve_pages else "token_budget_exhausted", "planning"
            )
        self.requests += 1
        self.tokens += cost
