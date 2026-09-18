"""Bounded document-local work with shared cancellation and deterministic result order."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextvars import copy_context
from threading import Event

from openkb.cancellation import cancellation_scope
from openkb.processing import processing_checkpoint


def parallel_batches(batches, operation, concurrency, *, stage="facts"):
    """Yield indexed results as completed, admitting only one batch per worker.

    A failure cancels siblings before joining them. Model transports run inside
    the shared execution budget, so abandoned requests cannot publish knowledge.
    The caller coordinates progress and restores source order before later stages.
    """
    from openkb.resource_budget import current_resources

    resources = current_resources()
    if concurrency == 1:
        for index, batch in enumerate(batches):
            if resources:
                resources.admit(stage=stage)
            yield index, operation(batch)
        return
    stopped = Event()
    pending = {}
    iterator = enumerate(batches)

    def run(batch):
        with cancellation_scope(stopped.is_set):
            processing_checkpoint(stage)
            return operation(batch)

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="openkb-" + stage) as pool:

        def submit():
            processing_checkpoint(stage)
            if resources and not resources.admit(stage=stage, active=bool(pending)):
                return
            try:
                index, batch = next(iterator)
            except StopIteration:
                return
            pending[pool.submit(copy_context().run, run, batch)] = index

        try:
            for _ in range(concurrency):
                submit()
            while pending:
                processing_checkpoint(stage)
                finished, _ = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
                # Observe every ready failure before admitting further work.
                results = [(future, future.result()) for future in finished]
                for future, result in results:
                    index = pending.pop(future)
                    yield index, result
                    submit()
        finally:
            stopped.set()
            for future in pending:
                future.cancel()
