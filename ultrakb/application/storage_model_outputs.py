"""Revocable model note writes, each committed independently from the final turn."""

import hashlib
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from threading import RLock
from uuid import uuid4

from ultrakb.locks import LockCancelled
from ultrakb.mutation import RecoveryRequired
from ultrakb.storage.artifact_store import ArtifactStore
from ultrakb.storage.knowledge_store import KnowledgeStore
from ultrakb.storage.wiki_paths import wiki_path

from .publications import publish_knowledge_changes


class _StorageWriter:
    def __init__(self, scope, outputs, check_cancelled, execution_id):
        assert scope.knowledge is not None
        self.scope, self.base = scope, scope.knowledge
        self.outputs, self.check_cancelled = outputs, check_cancelled
        self.execution_id = execution_id
        self.guard, self.active, self.failure, self.latest = RLock(), True, None, None
        self.accepted = None
        self.artifacts = ArtifactStore(scope.kb_dir, scope.knowledge.identity)
        self.artifact_epoch = self.artifacts.epoch()
        self.artifact_outputs = ()

    def settle(self):
        with self.guard:
            self.active = False
            if self.failure:
                raise self.failure

    def publication_fields(self):
        fields = {"artifact_outputs": self.artifact_outputs}
        if self.latest is None:
            return fields
        return fields | {
            "result_publication_id": self.accepted.publication.publication_id
            if self.accepted
            else None,
            "projection": self.accepted.projection if self.accepted else None,
            "candidate_revision_id": self.latest.candidate_revision_id,
            "reason_code": self.latest.reason_code,
        }

    def write(self, scope, path, content):
        with self.guard:
            if self.failure:
                raise self.failure
            if not self.active:
                raise RuntimeError("Model output writer has finished")
            if scope != self.scope:
                raise ValueError("Model output belongs to another fixed knowledge scope")
            self.check_cancelled()
            if path.startswith("output/"):
                try:
                    return self._write_artifact(path, content)
                except (RecoveryRequired, LockCancelled) as error:
                    self.failure = error
                    raise
                except (OSError, sqlite3.Error) as error:
                    self.failure = RecoveryRequired("The artifact commit needs confirmation")
                    raise self.failure from error
            relative = path.removeprefix("wiki/")
            before = self.base.files().get(relative)
            payload = content.encode("utf-8")
            try:
                outcome = publish_knowledge_changes(
                    self.base,
                    {relative: payload},
                    page_inputs={
                        relative: tuple(unit.unit_revision_id for unit in self.base.input_units())
                    }
                    if relative.endswith(".md")
                    else None,
                    check_cancelled=self.check_cancelled,
                    execution_id=self.execution_id,
                )
                self.latest = outcome
                if outcome.publication is None:
                    raise RecoveryRequired("Knowledge changed; review the retained model note")
                self.accepted = outcome
                resource = str(scope.kb_dir / path)
                self.outputs.resources = tuple(dict.fromkeys((*self.outputs.resources, resource)))
                if before != hashlib.sha256(payload).hexdigest():
                    change = f"{'updated' if before else 'created'}: {path}"
                    self.outputs.changes = tuple(dict.fromkeys((*self.outputs.changes, change)))
                self.base = KnowledgeStore(self.base.root, self.base.identity).bind(
                    outcome.publication.publication_id, view_id=scope.view_id
                )
            except (RecoveryRequired, LockCancelled) as error:
                self.failure = error
                raise
            except (OSError, sqlite3.Error) as error:
                self.failure = RecoveryRequired("The model note commit needs confirmation")
                raise self.failure from error
            return f"Written: {path}"

    def _write_artifact(self, path, content):
        from ultrakb.artifact_history import history_write_allowed

        if not history_write_allowed(self.scope.kb_dir / path):
            return "Access denied: archived artifact versions are read-only."
        identity, payload = uuid4().hex, content.encode("utf-8")
        request = {
            "expected_epoch": self.artifact_epoch,
            "execution_id": self.execution_id,
            "evidence_publication_id": self.scope.knowledge.publication_id,
        }
        try:
            revision = self.artifacts.save(
                path,
                payload,
                **request,
                revision_id=identity,
                check_cancelled=self.check_cancelled,
            )
        except Exception:
            revision = self.artifacts.committed_write(
                identity, path=path, payload=payload, **request
            )
            if revision is None:
                raise
        if revision.status == "conflict":
            self.artifact_outputs += ({**asdict(revision), "projection": None},)
            raise RecoveryRequired("Artifacts changed; review the retained artifact candidate")
        self.artifact_epoch = revision.epoch
        resource = str(self.scope.kb_dir / path)
        self.outputs.resources = tuple(dict.fromkeys((*self.outputs.resources, resource)))
        self.outputs.changes = tuple(dict.fromkeys((*self.outputs.changes, f"saved: {path}")))
        projection = self.artifacts.sync(
            path, revision_id=revision.revision_id, check_cancelled=self.check_cancelled
        )
        self.artifact_outputs += (
            {
                **asdict(revision),
                "projection": projection.status,
                "error_code": projection.error_code,
            },
        )
        return (
            f"Written: {path}"
            if projection.status == "applied"
            else f"Saved: {path}; file synchronization is {projection.status}."
        )


_writer: ContextVar[_StorageWriter | None] = ContextVar("storage_model_output_writer", default=None)


@contextmanager
def storage_model_output_scope(scope, outputs, *, check_cancelled, execution_id):
    writer = _StorageWriter(scope, outputs, check_cancelled, execution_id)
    token = _writer.set(writer)
    try:
        yield writer
    finally:
        try:
            writer.settle()
        finally:
            _writer.reset(token)


def write_storage_model_output(scope, path, content):
    try:
        wiki_path(path)
    except ValueError:
        return "Access denied: output requires a logical relative file path."
    if not path.startswith(("wiki/explorations/", "output/")):
        return "Access denied: output requires wiki/explorations/ or output/ files."
    if not isinstance(content, str):
        return "Access denied: output content must be text."
    writer = _writer.get()
    if writer is None:
        return "Access denied: model output requires an active application writer."
    return writer.write(scope, path, content)
