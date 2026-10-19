"""Bind the actual compiler and gateway to one exact managed work item."""

from contextlib import contextmanager
from contextvars import ContextVar

_COMPILATION = ContextVar("main_store_compilation", default=None)
_MODEL_INVOCATION = ContextVar("main_store_model_invocation", default=None)


@contextmanager
def model_invocation(work, invocation_id):
    token = _MODEL_INVOCATION.set((work, invocation_id))
    try:
        yield
    finally:
        _MODEL_INVOCATION.reset(token)


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
    if selected := _MODEL_INVOCATION.get():
        work, invocation_id = selected
        if (root.resolve(), identity, execution_id) != (
            work.root,
            work.identity,
            work.execution_id,
        ):
            raise ValueError("The model invocation belongs to another execution")
        return "compile:" + invocation_id
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
