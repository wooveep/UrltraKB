"""Independently retained semantic report for a fixed, repaired wiki."""

import asyncio

from agents import RunConfig
from agents.model_settings import ModelSettings
from pydantic import BaseModel, ConfigDict

from ultrakb.agent.checkpoint_model import CheckpointAgentModel
from ultrakb.agent.linter import run_knowledge_lint
from ultrakb.agent.page_response import PageResponseError
from ultrakb.compile_checkpoints import digest
from ultrakb.locks import LockCancelled
from ultrakb.mutation import RecoveryRequired
from ultrakb.storage.checkpoint_store import CompileCheckpointStore
from ultrakb.storage.compile_binding import compile_binding

from .task_stages import task_stage


class SemanticReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    text: str
    quality: list[str]


async def semantic_report(scope, private, options, context, bundle):
    base, execution = scope.knowledge, context.storage_execution
    work = CompileCheckpointStore(base.root, base.identity).for_work(
        execution.model_budget_id,
        object_id={
            "operation": "semantic-lint",
            "publication_id": base.publication_id,
            "view_id": scope.view_id,
            "fix": options.fix,
        },
        conditions={"fingerprint": context.executor.fingerprint, "linter": 1},
        execution_id=execution.execution_id,
    )
    signature = digest({"operation": "semantic-lint-v1", "fix": options.fix})
    with (
        task_stage(context, "knowledge.lint.semantic.v1", root=base.root) as progress,
        compile_binding(work, view_id=scope.view_id, document="semantic-lint"),
    ):
        retained = work.load("semantic-lint", signature)
        if retained is not None:
            progress["status"] = "reused"
            return SemanticReport.model_validate(retained)
        issues = []
        model = context.snapshot.values()["effective"]["model"]
        managed = CheckpointAgentModel(context.executor, work)
        try:
            text = await run_knowledge_lint(
                base.root,
                model,
                bundle=bundle,
                run_config=RunConfig(
                    model=managed,
                    model_settings=ModelSettings(retry=managed.retry_settings()),
                ),
                on_issue=issues.append,
                scope=private,
            )
        except (LockCancelled, asyncio.CancelledError, RecoveryRequired, PageResponseError):
            raise
        except Exception as error:
            progress["status"] = "failed"
            progress["reason_code"] = type(error).__name__
            return SemanticReport(
                text=f"Knowledge lint failed ({type(error).__name__})",
                quality=["semantic_lint_failed"],
            )
        report = SemanticReport(text=text, quality=issues)
        work.save("semantic-lint", signature, report.model_dump(mode="json"))
        return report
