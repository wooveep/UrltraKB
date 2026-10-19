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


def archive_generation(scope, work, request_key, target, epoch, context):
    store = GenerationStore(scope.kb_dir, scope.knowledge.identity)
    archive_key = digest({"operation": "generator-archive-v1", "request": request_key})
    plan = store.find(work.model_budget_id, archive_key)
    if plan is None:
        artifacts = ArtifactStore(scope.kb_dir, scope.knowledge.identity)
        workspace = target + "-workspace"
        after, iterations = None, 0
        while True:
            page = artifacts.files(prefix=workspace + "/", epoch=epoch, after=after)
            for entry in page.files:
                part = PurePosixPath(entry.path).relative_to(workspace).parts[0]
                suffix = part.removeprefix("iteration-")
                if part.startswith("iteration-") and suffix.isdigit():
                    iterations = max(iterations, int(suffix))
            if page.next_after is None:
                break
            after = page.next_after
        archive = f"{workspace}/iteration-{iterations + 1}"
        files = {}
        for entry in retained_files(artifacts, target, epoch):
            relative = (
                PurePosixPath(entry.path).name
                if entry.path == target
                else PurePosixPath(entry.path).relative_to(target).as_posix()
            )
            files[f"{archive}/{relative}"] = artifacts.read(entry.path, epoch=epoch)
            files[entry.path] = None
        if not files:
            raise ValueError("The reviewed artifact no longer has a retained original")
        with task_stage(context, "generator.archive.prepare.v1", root=scope.kb_dir):
            plan = store.prepare(
                work,
                archive_key,
                scope.publication_id,
                epoch,
                GenerationDetails(
                    target_type="skill",
                    output_dir=archive,
                    artifact_path=archive + "/SKILL.md",
                    archive_path=archive,
                    validation_known=False,
                ),
                files,
            )
    return accept_generation(scope, plan, context)
