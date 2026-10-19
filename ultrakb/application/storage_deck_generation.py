"""Retained Deck producer and optional critic on one fixed knowledge basis."""

import asyncio
from dataclasses import replace

from agents import RunConfig, Runner
from agents.model_settings import ModelSettings

from ultrakb.agent.checkpoint_model import CheckpointAgentModel, raise_checkpoint_failure
from ultrakb.agent.page_response import PageResponseError
from ultrakb.agent.query_evidence import restrict_query_agent
from ultrakb.agent.skill_runner import (
    MAX_TURNS,
    MAX_TURNS_WITH_CRITIQUE,
    finish_skill_run,
    prepare_skill_run,
)
from ultrakb.agent.skills import PreparedSkill, SkillNotFoundError, prepare_skill
from ultrakb.deck.creator import CRITIC_MAX_TURNS, CRITIC_SKILL
from ultrakb.locks import LockCancelled
from ultrakb.mutation import RecoveryRequired
from ultrakb.storage.checkpoint_store import CompileCheckpointStore
from ultrakb.storage.compile_binding import compile_binding
from ultrakb.storage.compile_workspace import materialize_baseline
from ultrakb.storage.generation_store import GenerationDetails, GenerationStore
from ultrakb.storage.read_checkpoints import checkpoint_reader

from .deck_workspace import DeckWorkspace
from .query_views import resolve_query_views
from .task_stages import task_stage


def freeze_skill(root, definition):
    return {
        "name": definition.name,
        "body": definition.body,
        "metadata": definition.metadata,
        "output_path": definition.output_path.relative_to(root).as_posix()
        if definition.output_path
        else None,
    }


def thaw_skill(root, value):
    return PreparedSkill(
        value["name"],
        value["body"],
        value["metadata"],
        root / value["output_path"] if value["output_path"] else None,
    )


def capture_critic(scope):
    try:
        return freeze_skill(scope.kb_dir, prepare_skill(scope.kb_dir, CRITIC_SKILL, scope=scope))
    except SkillNotFoundError:
        return None


async def _run(
    scope, workspace, definition, intent, context, credentials, request_key, phase, turns
):
    execution = context.storage_execution
    work = CompileCheckpointStore(scope.kb_dir, scope.knowledge.identity).for_work(
        execution.model_budget_id,
        object_id={"operation": "generate-deck", "request": request_key, "phase": phase},
        conditions={"generator": 1},
        execution_id=execution.execution_id,
    )
    request = prepare_skill_run(
        skill_name=definition.name,
        intent=intent,
        kb_dir=scope.kb_dir,
        model=context.snapshot.values()["effective"]["model"],
        max_turns=turns,
        bundle=credentials,
        prepared=definition,
        scope=scope,
    )
    tools = workspace.tools()
    checkpoint_reader(tools[1], work, context.executor)
    agent = request.agent.clone(
        tools=[
            tool
            for tool in request.agent.tools
            if tool.name not in {"write_file", "read_output_or_skill_file"}
        ]
        + list(tools)
    )
    agent = restrict_query_agent(
        agent, resolve_query_views(scope.kb_dir, scope=scope), checkpoints=work
    )
    managed = CheckpointAgentModel(context.executor, work)
    with (
        task_stage(context, f"generator.deck.{phase}.v1", root=scope.kb_dir),
        compile_binding(work, view_id=scope.view_id, document=definition.name),
    ):
        await Runner.run(
            agent,
            request.seed,
            max_turns=request.max_turns,
            run_config=RunConfig(
                model=managed, model_settings=ModelSettings(retry=managed.retry_settings())
            ),
        )


async def prepare_deck_generation(
    scope, options, context, credentials, work, request_key, selection, epoch, archived
):
    root = scope.kb_dir
    definition = thaw_skill(root, selection["prepared"])
    artifact = (
        definition.output_path.relative_to(root).as_posix()
        if definition.output_path
        else f"output/decks/{options.name}/index.html"
    )
    with (
        materialize_baseline(scope.knowledge, ()) as baseline,
        DeckWorkspace(
            scope, baseline.directory / ".generated-deck", selection["path"], epoch, context
        ) as workspace,
    ):
        failure, validation = None, None
        if selection["exists"] and options.overwrite == "overlay":
            with task_stage(context, "generator.overlay.prepare.v1", root=root):
                workspace.overlay()
        try:
            await _run(
                scope,
                workspace,
                definition,
                options.intent,
                context,
                credentials,
                request_key,
                "generate",
                MAX_TURNS_WITH_CRITIQUE if options.critique else MAX_TURNS,
            )
            with task_stage(context, "generator.deck.validate.v1", root=root):
                delivered = finish_skill_run(
                    replace(
                        definition,
                        output_path=workspace.directory / artifact
                        if definition.output_path
                        else None,
                    )
                )
                validation = delivered.validation
            if options.critique and selection["critic"] is not None:
                critic = thaw_skill(root, selection["critic"])
                await _run(
                    scope,
                    workspace,
                    critic,
                    f"Critique and patch the HTML deck at: {artifact}\n"
                    f"Original user brief (for context, do NOT change content):\n{options.intent}",
                    context,
                    credentials,
                    request_key,
                    "critique",
                    CRITIC_MAX_TURNS,
                )
        except (LockCancelled, asyncio.CancelledError, RecoveryRequired, PageResponseError):
            raise
        except Exception as error:
            raise_checkpoint_failure(error)
            failure = error
        with task_stage(context, "generator.persist.v1", root=root):
            return GenerationStore(root, scope.knowledge.identity).prepare(
                work,
                request_key,
                scope.publication_id,
                epoch,
                GenerationDetails(
                    target_type="deck",
                    output_dir=(root / selection["path"]).parent.relative_to(root).as_posix()
                    if selection["path"] == artifact
                    else selection["path"],
                    artifact_path=artifact,
                    validation_errors=tuple(validation.errors) if validation else (),
                    validation_warnings=tuple(validation.warnings) if validation else (),
                    validation_known=validation is not None,
                    generation_complete=failure is None,
                    error_type=type(failure).__name__ if failure else None,
                    unfinished=("generation",) if failure else (),
                    archive_path=archived.archive_path.relative_to(root).as_posix()
                    if archived and archived.archive_path
                    else None,
                    quality=tuple(workspace.quality),
                ),
                workspace.files,
                knowledge_base=selection.get("knowledge_base"),
            )
