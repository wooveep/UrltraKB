"""Explicit artifact selections with fixed output and knowledge versions."""

import zipfile
from pathlib import PurePosixPath
from uuid import uuid4

from ultrakb.knowledge_scope import resolve_scope
from ultrakb.storage.artifact_store import ArtifactStore
from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.wiki_paths import wiki_path

from .artifacts import Artifact, ArtifactPage


def commit_artifact(
    store, path, payload, *, expected_epoch, execution_id, evidence_publication_id, check_cancelled
):
    """Confirm a lost acknowledgement against this exact independently saved request."""
    identity = uuid4().hex
    request = {
        "expected_epoch": expected_epoch,
        "execution_id": execution_id,
        "evidence_publication_id": evidence_publication_id,
    }
    try:
        return store.save(
            path, payload, **request, revision_id=identity, check_cancelled=check_cancelled
        )
    except Exception:
        revision = store.committed_write(identity, path=path, payload=payload, **request)
        if revision is None:
            raise
        return revision


def artifact_catalog(root, scope, *, after, epoch, limit):
    scope = resolve_scope(root, scope)
    knowledge = scope.knowledge
    if knowledge is None:
        raise ValueError("Artifact catalog requires a main-store scope")
    if type(limit) is not int or not 1 <= limit <= 128:
        raise ValueError("Artifact page size must be between 1 and 128")
    namespaces = ("output/", "wiki/explorations/", "wiki/reports/")
    if after is not None:
        wiki_path(after.removesuffix("/"))
        if not after.startswith(namespaces):
            raise ValueError("Select a generated artifact or a saved report")
    store = ArtifactStore(scope.kb_dir, knowledge.identity)
    selected = store.files(epoch=epoch, limit=1).epoch
    index = next(
        (i for i, prefix in enumerate(namespaces) if after and after.startswith(prefix)), 0
    )
    cursor, items = after, []
    for _ in range(limit):
        path = None
        while index < len(namespaces):
            prefix = namespaces[index]
            if prefix == "output/":
                page = store.files(epoch=selected, after=cursor, limit=1)
                if page.files:
                    path = page.files[0].path
                    break
                if page.next_after is not None:
                    cursor = page.next_after
                    break
            else:
                paths = knowledge.file_page(
                    prefix=prefix.removeprefix("wiki/"),
                    after=cursor.removeprefix("wiki/") if cursor else None,
                    limit=1,
                )
                if paths:
                    path = "wiki/" + paths[0]
                    break
            index, cursor = index + 1, None
        if index == len(namespaces):
            return ArtifactPage(tuple(items), None, selected, knowledge.publication_id)
        if path is None:
            continue
        parts = path.split("/")
        hidden = next(
            (
                i
                for i, part in enumerate(parts)
                if part.startswith(".") or part.endswith("-workspace")
            ),
            None,
        )
        if hidden is not None:
            cursor = path if hidden == len(parts) - 1 else "/".join(parts[: hidden + 1]) + "/"
            continue
        if len(parts) >= 4 and parts[:2] in (["output", "skills"], ["output", "decks"]):
            path = "/".join(parts[:3])
            kind = "Skill" if parts[1] == "skills" else "幻灯片"
            cursor = path + "/"
        else:
            cursor = path
            kind = (
                "检查报告"
                if path.startswith("wiki/reports/")
                else "探索笔记"
                if path.startswith("wiki/explorations/")
                else "知识图谱"
                if path == "output/visualize/graph.html"
                else "文件"
            )
        items.append(Artifact(path, kind))
    return ArtifactPage(tuple(items), cursor, selected, knowledge.publication_id)


class StoredArtifact:
    def __init__(self, root, relative, scope, *, epoch=None):
        self.scope = resolve_scope(root, scope)
        self.knowledge = self.scope.knowledge
        if self.knowledge is None:
            raise ValueError("Artifact originals require a main-store scope")
        wiki_path(relative)
        if not relative.startswith(("output/", "wiki/reports/", "wiki/explorations/")):
            raise ValueError("Select a generated artifact or a saved report")
        self.relative = relative
        self.store = ArtifactStore(self.scope.kb_dir, self.knowledge.identity)
        self.epoch = self.store.files(epoch=epoch, limit=1).epoch
        try:
            if relative.startswith("output/"):
                self.store.entry(relative, epoch=self.epoch)
            else:
                self.knowledge.file_digest(relative.removeprefix("wiki/"))
            self.is_directory = False
        except FileNotFoundError:
            self.is_directory = True
            if next(self.paths(), None) is None:
                raise FileNotFoundError(relative) from None

    def paths(self):
        if not self.is_directory:
            yield self.relative
            return
        after = None
        if self.relative.startswith("output/"):
            while True:
                page = self.store.files(prefix=self.relative + "/", epoch=self.epoch, after=after)
                yield from (item.path for item in page.files)
                if page.next_after is None:
                    return
                after = page.next_after
        else:
            while True:
                paths = self.knowledge.file_page(
                    prefix=self.relative.removeprefix("wiki/") + "/", after=after
                )
                yield from ("wiki/" + path for path in paths)
                if len(paths) < 63:
                    return
                after = paths[-1]

    def copy(self, path, output):
        from ultrakb.storage.blobs import copy_blob

        with storage_stage(self.scope.kb_dir, self.knowledge.identity, read_only=True) as db:
            digest = (
                self.store.entry(path, epoch=self.epoch).digest
                if path.startswith("output/")
                else self.knowledge.file_digest(path.removeprefix("wiki/"))
            )
            copy_blob(db, digest, output)

    def archive(self, output):
        # Actual complete export protects selected originals throughout the read.
        # It never holds the ingest lease or a SQL transaction across ZIP I/O.
        with (
            storage_stage(self.scope.kb_dir, self.knowledge.identity, read_only=True),
            zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive,
        ):
            parent = PurePosixPath(self.relative).parent
            for path in self.paths():
                entry = zipfile.ZipInfo(PurePosixPath(path).relative_to(parent).as_posix())
                entry.compress_type = zipfile.ZIP_DEFLATED
                with archive.open(entry, "w", force_zip64=True) as target:
                    self.copy(path, target)
