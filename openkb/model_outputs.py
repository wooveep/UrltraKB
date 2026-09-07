"""Commit model tool files independently from a final answer or chat turn."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

from openkb.file_state import file_versions
from openkb.locks import DelegatedWriteLease, atomic_write_text
from openkb.mutation import RecoveryRequired, mutation_scope, repair_marker


@dataclass
class ModelOutputs:
    resources: tuple[str, ...] = ()
    changes: tuple[str, ...] = ()


@dataclass
class _ToolWriter:
    root: Path
    lease: DelegatedWriteLease
    result: ModelOutputs
    guard: Lock = field(default_factory=Lock)
    failure: RecoveryRequired | None = None

    def write(self, path: Path, content: str) -> None:
        # Some SDKs dispatch synchronous tools to threads. Serialize their
        # journal and facts updates without granting general KB reentrancy.
        with self.guard:
            if repair_marker(self.root).exists():
                self.failure = RecoveryRequired(f"Knowledge base needs repair: {self.root}")
                self.lease.revoke()
            if self.failure:
                raise self.failure
            if not self.lease.authorizes(self.root / ".openkb/ingest.lock"):
                raise RuntimeError("Model output writer has finished")
            before = file_versions(self.root, [path])
            try:
                with mutation_scope(
                    self.root, [path], operation="model-tool-output", delegated_lease=self.lease
                ):
                    atomic_write_text(path, content)
                    after = file_versions(self.root, [path])
            except RecoveryRequired as exc:
                # The SDK turns tool exceptions into model-readable text.
                # Latch a fatal write failure so that cannot resume mutations
                # or turn the model's later final text into a completed turn.
                self.failure = exc
                self.lease.revoke()
                raise
            # Publish facts only after the journal's commit signal succeeds.
            self.result.resources = tuple(dict.fromkeys((*self.result.resources, str(path))))
            if before != after:
                relative = path.relative_to(self.root).as_posix()
                change = f"{'updated' if before else 'created'}: {relative}"
                self.result.changes = tuple(dict.fromkeys((*self.result.changes, change)))


_writer: ContextVar[_ToolWriter | None] = ContextVar("model_output_writer", default=None)


@contextmanager
def model_output_scope(kb_dir: Path, result: ModelOutputs | None = None):
    """Delegate individual file commits while the parent owns full execution.

    The caller must settle its SDK work before leaving. A copied context cannot
    write after revocation. Completed writes survive later model/turn failure;
    an abrupt exit recovers only the individual write that did not commit.
    """
    root = kb_dir.resolve()
    result = result if result is not None else ModelOutputs()
    lease = DelegatedWriteLease(root / ".openkb/ingest.lock")
    writer = _ToolWriter(root, lease, result)
    token = _writer.set(writer)
    try:
        yield result
    finally:
        with writer.guard:
            lease.revoke()
        _writer.reset(token)
        if writer.failure:
            raise writer.failure


def write_model_output(root: Path, path: Path, content: str) -> bool:
    """Use the current chat/review writer, or leave other coordinators in charge."""
    writer = _writer.get()
    if writer is None:
        return False
    if writer.root != root.resolve():
        raise RuntimeError("Model output belongs to another knowledge base")
    writer.write(path, content)
    return True
