"""Public entry point for best-effort Markdown document planning."""

from __future__ import annotations

from typing import Any, Callable

from openkb.agent.document_plan import DocumentPlan
from openkb.agent.document_planning_result import PlanningResult
from openkb.progress import progress_scope


def plan_document(
    kb_dir: Any,
    workspace: Any,
    source: Any,
    parsed: Any,
    navigation: dict[str, Any] | None,
    settings: dict[str, Any],
    checkpoints: Any,
    *,
    bundle: Any = None,
    on_event: Callable[[dict[str, Any]], None] = lambda event: None,
    resume: bool = False,
    retry_skipped: bool = False,
    plan_only: bool = False,
    mock_caller: Callable[..., Any] | None = None,
    return_result: bool = False,
) -> DocumentPlan | PlanningResult | None:
    """Plan each window's overview and pages independently from saved source."""
    from openkb.agent.document_markdown_planner import plan_markdown_document

    with progress_scope("planning", 1) as progress:
        result = plan_markdown_document(
            kb_dir,
            workspace,
            source,
            parsed,
            navigation,
            settings,
            checkpoints,
            bundle=bundle,
            on_event=on_event,
            resume=resume,
            retry_skipped=retry_skipped,
            plan_only=plan_only,
            mock_caller=mock_caller,
        )
        progress.advance()
    return result if return_result else result.plan
