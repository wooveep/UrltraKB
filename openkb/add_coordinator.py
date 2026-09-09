from __future__ import annotations

import logging
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import click

from openkb.cancellation import OperationCancelled, check_cancelled
from openkb.locks import kb_ingest_lock_held
from openkb.mutation import MutationSnapshot, RecoveryRequired, mark_needs_repair, snapshot_paths

logger = logging.getLogger(__name__)

MutationBody = Callable[[MutationSnapshot], None]
PostCommitHook = Callable[[], None]


class DirtyRollbackError(RecoveryRequired):
    """A mutation's rollback failed, leaving an active journal on disk.

    The KB may be in a partially-applied state requiring controlled repair.
    The retained journal and repair gate block ordinary acquisitions. Batch owners
    (the parallel/serial ``add`` loops) MUST stop committing further mutations
    on top of this dirty state instead of continuing — otherwise the next
    recovery rolls this journal back over the shared paths it recorded
    (``hashes.json``, ``index.md``, ``concepts/``, ``entities/``) and silently
    clobbers the later commits. Single-mutation callers should let it propagate
    so the command fails loudly; rerunning alone cannot clear the repair gate.
    """

    def __init__(self, operation: str, journal_path: Path) -> None:
        super().__init__(
            f"Dirty rollback for {operation}; journal retained at {journal_path}. "
            f"Diagnose and repair the knowledge base before retrying."
        )
        self.operation = operation
        self.journal_path = journal_path


@dataclass(slots=True)
class AddMutationPlan:
    operation: str
    details: dict
    touched_paths: Sequence[Path]
    body: MutationBody
    post_commit_hooks: Sequence[PostCommitHook] = field(default_factory=tuple)
    hardlink_dirs: set[Path] = field(default_factory=set)
    staging_dirs: Sequence[Path | None] = field(default_factory=tuple)


def _cleanup_staging_dirs(staging_dirs: Sequence[Path | None]) -> None:
    for staging_dir in staging_dirs:
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)


def _rollback_snapshot(plan: AddMutationPlan, snapshot) -> Path | None:
    """Best-effort rollback; returns the retained journal path on dirty failure.

    Returns ``snapshot.journal_path`` when the snapshot existed but rollback
    FAILED (the active journal is retained for next-run recovery), otherwise
    ``None`` — covering both "snapshot is None" (nothing was applied; the
    failure happened during snapshot setup before the body ran) and a clean
    rollback that discarded its journal.
    """
    if snapshot is None:
        _cleanup_staging_dirs(plan.staging_dirs)
        return None
    rollback_error = snapshot.rollback_best_effort()
    if rollback_error is None:
        snapshot.discard_best_effort()
    else:
        mark_needs_repair(snapshot.kb_dir, snapshot.journal_path, rollback_error)
        click.echo(
            "  [ERROR] Rollback failed; mutation journal retained for recovery: "
            f"{snapshot.journal_path}"
        )
    _cleanup_staging_dirs(plan.staging_dirs)
    return snapshot.journal_path if rollback_error is not None else None


def _failure_target(details: dict) -> str:
    for key in ("name", "doc_name", "doc_id"):
        value = details.get(key)
        if value:
            return f" for {value}"
    return ""


def run_add_mutation(kb_dir: Path, plan: AddMutationPlan) -> bool:
    if not kb_ingest_lock_held(kb_dir / ".openkb"):
        raise RuntimeError("run_add_mutation requires the caller to hold kb_ingest_lock")
    snapshot = None
    try:
        snapshot = snapshot_paths(
            kb_dir,
            list(plan.touched_paths),
            operation=plan.operation,
            details=plan.details,
            hardlink_dirs=plan.hardlink_dirs,
        )
        plan.body(snapshot)
        check_cancelled()
        snapshot.mark_committed()
    except Exception as exc:
        dirty_journal = _rollback_snapshot(plan, snapshot)
        if dirty_journal is not None:
            # Rollback failed and left an active journal. Stop the batch rather
            # than committing more docs on top of dirty state that the next
            # recovery would roll back over.
            raise DirtyRollbackError(plan.operation, dirty_journal)
        if isinstance(exc, RecoveryRequired):
            raise
        click.echo(f"  [ERROR] {plan.operation} failed{_failure_target(plan.details)}: {exc}")
        logger.debug("%s mutation failed:", plan.operation, exc_info=True)
        return False
    except BaseException as exc:
        # Interrupt (KeyboardInterrupt / SystemExit): best-effort rollback for
        # its side-effects only. Do NOT raise DirtyRollbackError — propagate the
        # interrupt so the user's abort is honored. Any retained journal or
        # orphaned staging is recovered next run by the drain + reaper.
        dirty_journal = _rollback_snapshot(plan, snapshot)
        if isinstance(exc, OperationCancelled) and dirty_journal is not None:
            raise DirtyRollbackError(plan.operation, dirty_journal) from exc
        raise
    finally:
        _cleanup_staging_dirs(plan.staging_dirs)

    for hook in plan.post_commit_hooks:
        try:
            hook()
        except Exception as exc:
            from openkb.compilation_report import report_auxiliary_warning

            report_auxiliary_warning("post_commit_hook_failed")
            logger.warning("Post-commit hook failed for %s: %s", plan.operation, exc)

    cleanup_error = snapshot.discard_best_effort()
    if cleanup_error is not None:
        from openkb.compilation_report import report_auxiliary_warning

        report_auxiliary_warning("journal_cleanup_failed")
        click.echo(f"  [WARN] mutation journal cleanup failed: {cleanup_error}")
    return True
