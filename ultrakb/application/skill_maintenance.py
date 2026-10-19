"""Explicit Skill restoration and artifact review through native storage."""

from pathlib import Path, PurePosixPath

from ultrakb.knowledge_scope import KnowledgeScope, resolve_scope
from ultrakb.mutation import RecoveryRequired
from ultrakb.skill import validate_skill_name
from ultrakb.storage.artifact_store import ArtifactStore


def rollback_skill(kb_dir: Path, name: str, *, iteration: int | None = None) -> Path:
    from ultrakb.compile_checkpoints import digest
    from ultrakb.storage.checkpoint_store import CompileCheckpointStore
    from ultrakb.storage.generation_store import GenerationDetails, GenerationStore

    from .execution import ExecutionContext
    from .generation_acceptance import accept_generation
    from .storage_execution import begin_storage_operation
    from .storage_generation_replacement import archive_generation, generation_paths
    from .storage_generators import _marketplace

    error = validate_skill_name(name)
    if error:
        raise ValueError(error)
    if iteration is not None and (type(iteration) is not int or iteration < 1):
        raise ValueError("Choose a positive iteration number")
    scope = resolve_scope(kb_dir, None)
    assert scope.knowledge is not None
    root, identity = scope.kb_dir, scope.knowledge.identity
    artifacts = ArtifactStore(root, identity)
    epoch = artifacts.epoch()
    target = f"output/skills/{name}"
    workspace = target + "-workspace"
    iterations = set()
    for path in generation_paths(scope, workspace, epoch):
        part = PurePosixPath(path).relative_to(workspace).parts[0]
        suffix = part.removeprefix("iteration-")
        if part.startswith("iteration-") and suffix.isdigit():
            iterations.add(int(suffix))
    chosen = max(iterations, default=0) if iteration is None else iteration
    if not chosen or chosen not in iterations:
        raise FileNotFoundError("The selected retained skill iteration does not exist")
    source = f"{workspace}/iteration-{chosen}"
    files = {
        target + "/" + PurePosixPath(path).relative_to(source).as_posix(): artifacts.read(
            path, epoch=epoch
        )
        for path in generation_paths(scope, source, epoch)
    }
    if not files:
        raise FileNotFoundError("The selected retained skill iteration is empty")
    context = ExecutionContext(storage_identity=identity)
    with begin_storage_operation(root, context, "rollback-skill"):
        execution = context.storage_execution
        assert execution is not None
        request = digest({"rollback": target, "iteration": chosen, "epoch": epoch})
        work = CompileCheckpointStore(root, identity).for_work(
            execution.model_budget_id,
            object_id={"operation": "rollback-skill", "request": request},
            conditions={"protocol": 1},
            execution_id=execution.execution_id,
        )
        if next(generation_paths(scope, target, epoch), None) is not None:
            archived = archive_generation(scope, work, request, target, epoch, context)
            if archived.status != "completed":
                raise RecoveryRequired(
                    "The previous skill remains retained; archive needs confirmation"
                )
            epoch = archived.artifact_epoch
        files[".claude-plugin/marketplace.json"] = _marketplace(scope, name, epoch, {})
        plan = GenerationStore(root, identity).prepare(
            work,
            request,
            scope.publication_id,
            epoch,
            GenerationDetails(
                target_type="skill",
                output_dir=target,
                artifact_path=target + "/SKILL.md",
                validation_known=False,
            ),
            files,
        )
        result = accept_generation(scope, plan, context)
        if result.status != "completed":
            raise RecoveryRequired(
                "Skill restoration is incomplete; accepted files remain retained"
            )
        if result.projection_pending:
            raise RecoveryRequired(
                "Skill restored in the main store; output synchronization is pending"
            )
        return root / target


async def critique_artifact(
    kb_dir: Path, path: str, *, scope: KnowledgeScope | None = None
) -> Path:
    from ultrakb.agent.skill_runner import run_skill
    from ultrakb.model_outputs import ModelOutputs
    from ultrakb.storage.wiki_paths import wiki_path

    from .execution import ExecutionContext
    from .storage_model_outputs import storage_model_output_scope
    from .task_stages import task_stage

    scope = resolve_scope(kb_dir, scope)
    assert scope.knowledge is not None
    if scope.historical:
        raise ValueError("Review the current artifact publication before changing it")
    wiki_path(path)
    if path.startswith("output/"):
        ArtifactStore(scope.kb_dir, scope.knowledge.identity).read(path)
    elif path.startswith("wiki/explorations/"):
        scope.knowledge.read(path.removeprefix("wiki/"))
    else:
        raise ValueError("Critique target must be an output or exploration")
    context = ExecutionContext(storage_identity=scope.knowledge.identity)
    with context.begin(scope.kb_dir) as bundle:
        assert context.snapshot is not None and context.storage_execution is not None
        config = context.snapshot.values()["effective"]
        with (
            storage_model_output_scope(
                scope,
                ModelOutputs(),
                check_cancelled=context.check_stop,
                execution_id=context.storage_execution.execution_id,
                allowed_paths=frozenset({path}),
            ),
            task_stage(context, "artifact.critique.v1"),
        ):
            await run_skill(
                skill_name="ultrakb-html-critic",
                intent=f"Critique and patch the HTML file at: {path}",
                kb_dir=scope.kb_dir,
                model=config["model"],
                language=config["language"],
                max_turns=40,
                scope=scope,
                bundle=bundle,
            )
        context._storage_result = "completed"
    return scope.kb_dir / path
