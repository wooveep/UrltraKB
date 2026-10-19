"""Explicit generated-artifact archives and private overlay preparation."""

from pathlib import PurePosixPath

from ultrakb.compile_checkpoints import digest
from ultrakb.storage.artifact_store import ArtifactStore
from ultrakb.storage.generation_store import GenerationDetails, GenerationStore

from .generation_acceptance import accept_generation
from .task_stages import task_stage


def retained_files(store, target, epoch):
    try:
        store.entry(target, epoch=epoch)
    except FileNotFoundError:
        after = None
        while True:
            page = store.files(prefix=target + "/", epoch=epoch, after=after)
            yield from page.files
            if page.next_after is None:
                return
            after = page.next_after
    else:
        yield store.entry(target, epoch=epoch)


def generation_paths(scope, target, epoch):
    if not target.startswith("wiki/"):
        store = ArtifactStore(scope.kb_dir, scope.knowledge.identity)
        for entry in retained_files(store, target, epoch):
            yield entry.path
        return
    relative = target.removeprefix("wiki/")
    try:
        scope.knowledge.file_digest(relative)
    except FileNotFoundError:
        after = None
        while True:
            page = scope.knowledge.file_page(prefix=relative + "/", after=after)
            if not page:
                return
            yield from ("wiki/" + path for path in page)
            after = page[-1]
    else:
        yield target


def read_generation_file(scope, path, epoch):
    if path.startswith("wiki/"):
        return scope.knowledge.read(path.removeprefix("wiki/"))
    return ArtifactStore(scope.kb_dir, scope.knowledge.identity).read(path, epoch=epoch)


def materialize_overlay(scope, target, epoch, output):
    store = ArtifactStore(scope.kb_dir, scope.knowledge.identity)
    for entry in retained_files(store, target, epoch):
        if entry.path == target:
            raise ValueError("A generated directory cannot overlay an existing regular file")
        path = output / PurePosixPath(entry.path).relative_to(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(store.read(entry.path, epoch=epoch))


def skill_archive_diff(scope, archived, epoch, output, temporary):
    from ultrakb.skill.workspace import write_diff

    archive = archived.archive_path.relative_to(scope.kb_dir).as_posix()
    previous = temporary / ".archive-previous"
    materialize_overlay(scope, archive, epoch, previous)
    diff = temporary / ".archive-diff.md"
    write_diff(previous, output, diff)
    return {archive + "/diff.md": diff.read_bytes()}


def archive_generation(scope, work, request_key, target, epoch, context, *, target_type="skill"):
    store = GenerationStore(scope.kb_dir, scope.knowledge.identity)
    archive_key = digest({"operation": "generator-archive-v1", "request": request_key})
    plan = store.find(work.model_budget_id, archive_key)
    if plan is None:
        workspace = target + "-workspace"
        iterations = 0
        for path in generation_paths(scope, workspace, epoch):
            relative = PurePosixPath(path).relative_to(workspace)
            if not relative.parts:
                raise ValueError("An archive workspace cannot replace a regular file")
            part = relative.parts[0]
            suffix = part.removeprefix("iteration-")
            if part.startswith("iteration-") and suffix.isdigit():
                iterations = max(iterations, int(suffix))
        archive = f"{workspace}/iteration-{iterations + 1}"
        files = {}
        knowledge_inputs = {}
        artifact = "SKILL.md" if target_type == "skill" else "index.html"
        for path in generation_paths(scope, target, epoch):
            relative = (
                PurePosixPath(path).name
                if path == target
                else PurePosixPath(path).relative_to(target).as_posix()
            )
            if path == target:
                artifact = relative
            files[f"{archive}/{relative}"] = read_generation_file(scope, path, epoch)
            files[path] = None
            if path.startswith("wiki/") and path.endswith(".md"):
                knowledge_inputs[f"{archive}/{relative}".removeprefix("wiki/")] = tuple(
                    unit.unit_revision_id
                    for unit in scope.knowledge.input_units(page=path.removeprefix("wiki/"))
                )
        if not files:
            raise ValueError("The reviewed artifact no longer has a retained original")
        with task_stage(context, "generator.archive.prepare.v1", root=scope.kb_dir):
            plan = store.prepare(
                work,
                archive_key,
                scope.publication_id,
                epoch,
                GenerationDetails(
                    target_type=target_type,
                    output_dir=archive,
                    artifact_path=archive + "/" + artifact,
                    archive_path=archive,
                    validation_known=False,
                ),
                files,
                knowledge_inputs=knowledge_inputs,
            )
    return accept_generation(scope, plan, context)
