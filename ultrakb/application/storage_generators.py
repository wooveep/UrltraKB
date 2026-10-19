"""Generate from fixed knowledge and retain output through the main artifact store."""

import asyncio
import json
from dataclasses import asdict, replace

from agents import RunConfig, Runner
from agents.model_settings import ModelSettings

from ultrakb.agent.checkpoint_model import CheckpointAgentModel, raise_checkpoint_failure
from ultrakb.agent.page_response import PageResponseError
from ultrakb.agent.query_evidence import restrict_query_agent
from ultrakb.compile_checkpoints import digest
from ultrakb.config import LlmCredentialBundle
from ultrakb.context.workers import run_blocking
from ultrakb.locks import LockCancelled
from ultrakb.mutation import RecoveryRequired
from ultrakb.skill.creator import build_skill_create_agent, finish_skill_create
from ultrakb.skill.marketplace import build_marketplace_manifest
from ultrakb.skill.validator import validate_skill
from ultrakb.storage.artifact_store import ArtifactStore
from ultrakb.storage.checkpoint_store import CompileCheckpointStore
from ultrakb.storage.compile_binding import compile_binding
from ultrakb.storage.compile_workspace import materialize_baseline
from ultrakb.storage.generation_store import GenerationDetails, GenerationStore, generation_request

from .generation_acceptance import accept_generation
from .generators import GenerationPreview, GenerationResult, _prepare, preflight_generation
from .query_views import resolve_query_views
from .task_stages import task_stage


def _selection(scope, target_type, name, *, skill_name=None):
    prepared = _prepare(scope.kb_dir, target_type, name, skill_name, scope=scope)
    path = f"output/{'skills' if target_type == 'skill' else 'decks'}/{name}"
    if prepared is not None and prepared.output_path is not None:
        path = prepared.output_path.relative_to(scope.kb_dir).as_posix()
    store = ArtifactStore(scope.kb_dir, scope.knowledge.identity)
    epoch = store.epoch()
    try:
        store.entry(path, epoch=epoch)
        exists = True
    except FileNotFoundError:
        after = None
        while True:
            page = store.files(prefix=path + "/", epoch=epoch, after=after, limit=1)
            exists = bool(page.files)
            if exists or page.next_after is None:
                break
            after = page.next_after
    version = digest(
        {
            "operation": "generate-v1",
            "generation": scope.knowledge.identity.generation,
            "publication_id": scope.publication_id,
            "path": path,
            "epoch": epoch,
            "definition": [prepared.name, prepared.body, prepared.metadata] if prepared else None,
        }
    )
    return GenerationPreview(scope.kb_dir / path, exists, version), epoch


def preview_stored_generation(scope, target_type, name, *, skill_name=None):
    preview, _ = _selection(scope, target_type, name, skill_name=skill_name)
    return preview


def _marketplace(scope, name, epoch, config):
    artifacts = ArtifactStore(scope.kb_dir, scope.knowledge.identity)
    names, after = {name}, None
    while True:
        page = artifacts.files(prefix="output/skills/", epoch=epoch, after=after)
        for entry in page.files:
            parts = entry.path.split("/")
            if len(parts) == 4 and parts[-1] == "SKILL.md":
                names.add(parts[2])
        if page.next_after is None:
            break
        after = page.next_after
    return (
        json.dumps(
            build_marketplace_manifest(
                scope.kb_dir, sorted(names), version=str(config.get("version", "0.1.0"))
            ),
            indent=2,
        )
        + "\n"
    ).encode()


async def generate_stored_artifact(scope, options, context, *, bundle=None, model=None):
    base, root = scope.knowledge, scope.kb_dir
    if context.storage_identity not in (None, base.identity):
        raise ValueError("Generation belongs to another database generation")
    context.storage_identity = base.identity
    if bundle is not None:
        if context.snapshot is not None and context.snapshot.values()["credentials"] != asdict(
            bundle
        ):
            raise ValueError("Generator credentials differ from the captured execution")
        if context.executor is not None and context.executor.bindings.credentials() != bundle:
            raise ValueError("Generator credentials differ from the active executor")
        context.credential_bundle = LlmCredentialBundle(**asdict(bundle))
    store = GenerationStore(root, base.identity)
    request_key = generation_request(scope, options)
    previous = context.storage_execution
    if previous is None and context.resume_storage_execution_id is not None:
        from ultrakb.storage.execution_store import ExecutionStore

        previous = ExecutionStore(root, base.identity).execution(
            context.resume_storage_execution_id
        )
    retained = store.find(previous.model_budget_id, request_key) if previous is not None else None
    if retained is not None and not store.has_pending(retained):
        return accept_generation(scope, retained, context)
    error = preflight_generation(root, options.name, scope=scope)
    if error:
        return GenerationResult("invalid", message=error)
    if options.target_type != "skill":
        return GenerationResult("blocked", unfinished=("deck_generation",))
    result, archived = None, None

    async def execute(credentials):
        nonlocal result, archived
        if retained is not None:
            result = accept_generation(scope, retained, context)
            return
        captured = context.snapshot.values()["effective"]["model"]
        if model is not None and model != captured:
            raise ValueError("Generator model differs from the captured execution")
        execution = context.storage_execution
        work = CompileCheckpointStore(root, base.identity).for_work(
            execution.model_budget_id,
            object_id={
                "operation": "generate-skill",
                "request": request_key,
            },
            conditions={"generator": 1},
            execution_id=execution.execution_id,
        )
        selection_key = digest({"protocol": "generator-selection-v1", "request": request_key})
        selection = work.load("generator-selection", selection_key)
        if selection is None:
            preview, initial_epoch = _selection(
                scope, options.target_type, options.name, skill_name=options.skill_name
            )
            selection = {
                "path": preview.target.relative_to(root).as_posix(),
                "exists": preview.exists,
                "version": preview.version,
                "epoch": initial_epoch,
                "fingerprint": context.executor.fingerprint,
            }
            work.save("generator-selection", selection_key, selection)
        if selection["fingerprint"] != context.executor.fingerprint:
            raise RecoveryRequired("Generation requires its original captured model configuration")
        preview = GenerationPreview(
            root / selection["path"], selection["exists"], selection["version"]
        )
        initial_epoch = selection["epoch"]
        if options.version is not None and options.version != preview.version:
            result = GenerationResult(
                "conflict", message="Artifacts changed; review and confirm again"
            )
            return
        if preview.exists and options.overwrite == "refuse":
            result = GenerationResult(
                "conflict", message="Artifact already exists; review replacement"
            )
            return
        if preview.exists:
            try:
                ArtifactStore(root, base.identity).entry(selection["path"], epoch=initial_epoch)
            except FileNotFoundError:
                pass
            else:
                result = GenerationResult(
                    "conflict", message="A skill directory cannot replace a regular file"
                )
                return
        if preview.exists and options.overwrite == "archive":
            from .storage_generation_replacement import archive_generation

            archived = archive_generation(
                scope, work, request_key, selection["path"], initial_epoch, context
            )
            if archived.status != "completed":
                result = archived
                return
            initial_epoch = archived.artifact_epoch
        with materialize_baseline(base, ()) as baseline:
            output = baseline.directory / ".generated" / options.name
            if preview.exists and options.overwrite == "overlay":
                from .storage_generation_replacement import materialize_overlay

                with task_stage(context, "generator.overlay.prepare.v1", root=root):
                    materialize_overlay(scope, selection["path"], initial_epoch, output)
            agent = build_skill_create_agent(
                wiki_root=str(baseline.directory),
                skill_root=str(output),
                skill_name=options.name,
                intent=options.intent,
                model=captured,
                bundle=credentials,
                scope=scope,
            )
            agent = restrict_query_agent(
                agent, resolve_query_views(root, scope=scope), checkpoints=work
            )
            managed = CheckpointAgentModel(context.executor, work)
            failure, validation, extra, quality = None, None, {}, ()
            try:
                with (
                    task_stage(context, "generator.skill.generate.v1", root=root),
                    compile_binding(work, view_id=scope.view_id, document=options.name),
                ):
                    await Runner.run(
                        agent,
                        f"Compile the skill '{options.name}'. Follow the system prompt's "
                        "working method. Read the wiki, then write the skill files.",
                        max_turns=80,
                        run_config=RunConfig(
                            model=managed,
                            model_settings=ModelSettings(retry=managed.retry_settings()),
                        ),
                    )
                with task_stage(context, "generator.skill.validate.v1", root=root):
                    finish_skill_create(output)
                    validation = validate_skill(output)
                with task_stage(context, "generator.marketplace.v1", root=root):
                    extra[".claude-plugin/marketplace.json"] = _marketplace(
                        scope, options.name, initial_epoch, context.snapshot.values()["effective"]
                    )
                if archived is not None:
                    from .storage_generation_replacement import skill_archive_diff

                    try:
                        with task_stage(context, "generator.skill.diff.v1", root=root):
                            extra.update(
                                skill_archive_diff(
                                    scope, archived, initial_epoch, output, baseline.directory
                                )
                            )
                    except RecoveryRequired:
                        raise
                    except Exception:
                        quality = ("artifact_diff_failed",)
            except (LockCancelled, asyncio.CancelledError, RecoveryRequired, PageResponseError):
                raise
            except Exception as error:
                raise_checkpoint_failure(error)
                failure = error
            with task_stage(context, "generator.persist.v1", root=root):
                plan = store.prepare(
                    work,
                    request_key,
                    base.publication_id,
                    initial_epoch,
                    GenerationDetails(
                        target_type=options.target_type,
                        output_dir=preview.target.relative_to(root).as_posix(),
                        artifact_path=(preview.target / "SKILL.md").relative_to(root).as_posix(),
                        validation_errors=tuple(validation.errors)
                        if validation is not None
                        else (),
                        validation_warnings=tuple(validation.warnings)
                        if validation is not None
                        else (),
                        validation_known=validation is not None,
                        generation_complete=failure is None,
                        error_type=type(failure).__name__ if failure else None,
                        unfinished=("generation",) if failure else (),
                        archive_path=archived.archive_path.relative_to(root).as_posix()
                        if archived and archived.archive_path
                        else None,
                        quality=quality,
                    ),
                    {
                        (preview.target / file.relative_to(output))
                        .relative_to(root)
                        .as_posix(): file.read_bytes()
                        for file in output.rglob("*")
                        if file.is_file()
                    }
                    | extra,
                )
            result = accept_generation(scope, plan, context)

    try:
        with context.begin(root) as credentials:
            await run_blocking(lambda: asyncio.run(execute(credentials)))
    except (Exception, asyncio.CancelledError) as error:
        if result is not None:
            return replace(result, unfinished=(*result.unfinished, "confirm execution cleanup"))
        if archived is not None:
            return replace(
                archived,
                status="blocked" if isinstance(error, RecoveryRequired) else "failed",
                unfinished=("generation",),
                error_type=type(error).__name__,
            )
        if isinstance(error, (LockCancelled, asyncio.CancelledError)):
            raise
        return GenerationResult(
            "blocked" if isinstance(error, RecoveryRequired) else "failed",
            error_type=type(error).__name__,
            unfinished=("generation",),
        )
    return result
