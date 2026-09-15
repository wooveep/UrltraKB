"""Retry a silent compiler request only after its local transport has finished."""

import time


class StreamTimeoutRetries:
    def __init__(self, budget):
        self.budget = budget
        self.count = 0

    @staticmethod
    def is_timeout(error):
        import litellm

        from openkb.processing import ProcessingIncomplete

        return isinstance(error, (TimeoutError, litellm.Timeout)) or (
            isinstance(error, ProcessingIncomplete) and error.reason == "request_timeout"
        )

    def retry(self, error, activity, done):
        budget = self.budget
        if activity is None or not self.is_timeout(error):
            return False
        # A stopped stream discards subsequent fragments and closes its owning
        # iterator. Never reuse its partial text as a settled model response.
        activity.stopped.set()
        if self.count >= budget.limits.timeout_retries or done is None:
            return False
        deadline = time.monotonic() + budget.limits.cleanup_timeout
        while not done.wait(0.025):
            budget.checkpoint()
            if time.monotonic() >= deadline:
                return False  # An unresponsive transport still owns its permit.
        budget.checkpoint()
        self.count += 1
        from openkb.runtime.diagnostics import report_model_retry

        report_model_retry(self.count, budget.limits.timeout_retries)
        deadline = time.monotonic() + 0.25 * 2 ** min(self.count - 1, 3)
        while time.monotonic() < deadline:
            budget.checkpoint()
            time.sleep(min(0.025, max(0, deadline - time.monotonic())))
        return True
