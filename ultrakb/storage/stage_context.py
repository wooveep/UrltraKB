"""Parent stage metadata propagates across tasks without granting storage ownership."""

from contextlib import contextmanager
from contextvars import ContextVar

_PARENT: ContextVar[tuple | None] = ContextVar("ultrakb_storage_stage", default=None)


def parent_stage(root, identity, execution_id):
    parent = _PARENT.get()
    return parent[3] if parent and parent[:3] == (root.resolve(), identity, execution_id) else None


@contextmanager
def bind_stage(root, identity, execution_id, stage_id):
    token = _PARENT.set((root.resolve(), identity, execution_id, stage_id))
    try:
        yield
    finally:
        _PARENT.reset(token)
