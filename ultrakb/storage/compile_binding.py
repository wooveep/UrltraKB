"""Bind the actual compiler and gateway to one exact managed work item."""

from contextlib import contextmanager
from contextvars import ContextVar

_COMPILATION = ContextVar("main_store_compilation", default=None)


@contextmanager
def compile_binding(work, *, view_id, document, inputs=None):
    token = _COMPILATION.set((work, view_id, document, inputs))
    try:
        yield
    finally:
        _COMPILATION.reset(token)


def bound_checkpoints(root, view_id, document):
    binding = _COMPILATION.get()
    if binding is None:
        return None
    work, selected_view, selected_document, _ = binding
    if root.resolve() != work.root or (view_id, document) != (selected_view, selected_document):
        raise ValueError("Compiler checkpoint belongs to another input")
    return work


def model_work_key(root, identity, execution_id, stage):
    binding = _COMPILATION.get()
    if binding is None:
        return None
    work = binding[0]
    if (root.resolve(), identity, execution_id) != (work.root, work.identity, work.execution_id):
        raise ValueError("Compiler request belongs to another execution")
    return work.model_work_key(stage)


def bound_inputs():
    binding = _COMPILATION.get()
    return binding[3] if binding else None
