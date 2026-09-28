"""Durable page-source attempts counted inside the existing transport allowance."""

import math
from contextlib import contextmanager

from openkb.agent.document_planning_runtime import SOURCE_REQUEST_LIMIT
from openkb.execution_allowance import active_allowance


class LocationAllowanceExhausted(Exception):
    """An optional local operation ended; the document budget remains usable."""


class LocationAllowance:
    def __init__(self, state, page_key, limits, save):
        self.state, self.page_key, self.limits, self.save = state, page_key, limits, save
        self.parent = active_allowance()
        self.request = None
        self.failure = None

    def used(self, page=False):
        return sum(
            row["round"] == self.state["round"] and (not page or row["page"] == self.page_key)
            for row in self.state["attempts"]
        )

    def remaining(self):
        return self.parent.remaining() if self.parent else math.inf

    def before_reserve(self, options, reserved):
        if self.used() >= SOURCE_REQUEST_LIMIT or self.used(page=True) >= self.limits.max_attempts:
            from openkb.processing import ProcessingIncomplete, _transient, _uncertain_transport

            if isinstance(self.failure, ProcessingIncomplete):
                raise self.failure
            if self.failure is not None and _transient(self.failure):
                raise ProcessingIncomplete("provider_temporarily_unavailable", "planning")
            if self.failure is not None and _uncertain_transport(self.failure):
                raise ProcessingIncomplete("request_outcome_unknown", "planning")
            raise LocationAllowanceExhausted("page_sources_attempt_limit")
        if self.parent:
            self.parent.before_reserve(options, reserved)
        # LiteLLM transport retries must go through ExecutionBudget, not its SDK.
        options["num_retries"] = 0
        options["max_retries"] = 0
        self.request = {
            "round": self.state["round"],
            "page": self.page_key,
            "ordinal": len(self.state["attempts"]) + 1,
            "status": "reserved",
        }
        self.state["attempts"].append(self.request)
        self.save()

    @contextmanager
    def admission(self, options, checkpoint):
        checkpoint()
        if self.request is None:
            raise RuntimeError("Page-source dispatch has no reserved attempt")
        self.request["status"] = "dispatched"
        self.save()
        checkpoint()
        try:
            yield
        except Exception as exc:
            # Keep the transport failure when a following retry reaches our cap.
            self.failure = exc
            raise
        else:
            self.failure = None

    def request_timed_out(self, options):
        if self.parent:
            self.parent.request_timed_out(options)
