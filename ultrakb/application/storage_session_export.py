"""Publish an explicit transcript from the authoritative conversation originals."""

import json
import re
from dataclasses import replace

from ultrakb.agent.answer_text import visible_answer
from ultrakb.lint import build_norm_index, strip_ghost_wikilinks, wiki_targets
from ultrakb.locks import session_lock
from ultrakb.storage.session_catalog import SessionCatalog
from ultrakb.storage.session_store import SessionConflict

from .execution import ExecutionContext
from .publications import publish_knowledge_changes
from .sessions import SessionResult
from .storage_execution import begin_storage_operation
from .task_stages import task_stage


def _transcript(scope, session_id, context):
    """The owning session lock fixes all bounded pages, including pending submissions."""
    catalog = SessionCatalog(scope.kb_dir, scope.knowledge.identity)
    pages, before, version = [], None, None
    known = wiki_targets(scope.knowledge.files())
    norm_index = build_norm_index(known)
    while True:
        context.check_stop()
        page = catalog.read(session_id, view_id=scope.view_id, limit=128, before=before)
        if version is not None and page.version != version:
            raise SessionConflict("Conversation changed during transcript capture")
        version = page.version
        pending, cursor = list(page.pending), page.next_pending
        while cursor is not None:
            context.check_stop()
            more = catalog.read(
                session_id, view_id=scope.view_id, limit=128, before=before, pending_after=cursor
            )
            if more.version != version:
                raise SessionConflict("Conversation changed during transcript capture")
            pending.extend(more.pending)
            cursor = more.next_pending
        pages.append((page, pending))
        before = page.older_before
        if before is None:
            break
    latest = pages[0][0]
    lines = [
        "---",
        f"session: {json.dumps(session_id, ensure_ascii=False)}",
        f"model: {json.dumps(latest.model, ensure_ascii=False)}",
        f"created: {json.dumps(latest.created_at, ensure_ascii=False)}",
        "---",
        "",
        f"# Chat transcript  {latest.title or session_id}",
        "",
    ]
    for page, pending in reversed(pages):
        for index in range(len(page.turns) + 1):
            context.check_stop()
            for position, message in pending:
                if position == index:
                    lines.extend([f"## [unfinished] {message}", "", "_(answer unfinished)_", ""])
            if index == len(page.turns):
                break
            messages = {item["role"]: item["content"] for item in page.turns[index]["messages"]}
            lines.extend([f"## [{page.start_ordinal + index + 1}] {messages['user']}", ""])
            answer = visible_answer(messages["assistant"])
            if answer:
                cleaned, _ = strip_ghost_wikilinks(answer, known, norm_index=norm_index)
                lines.append(cleaned)
            else:
                lines.append("_(no response recorded)_")
            lines.append("")
    earliest = pages[-1][0]
    first_question = (
        {item["role"]: item["content"] for item in earliest.turns[0]["messages"]}["user"]
        if earliest.turns
        else None
    )
    return latest, first_question, "\n".join(lines)


def export_stored_conversation(scope, session_id, name, *, unique, context):
    if not isinstance(session_id, str):
        raise ValueError("Export requires a retained conversation identity")
    bound = scope.knowledge
    context = context or ExecutionContext()
    if context.storage_identity not in (None, bound.identity):
        raise ValueError("Conversation export belongs to another database generation")
    context.storage_identity = bound.identity
    result = None
    try:
        with (
            begin_storage_operation(scope.kb_dir, context, "export-conversation"),
            session_lock(
                scope.kb_dir,
                session_id,
                cancelled=context.cancelled,
                on_wait=context.waiting,
                database_identity=bound.identity,
            ),
            task_stage(context, "session.export.v1", root=scope.kb_dir),
        ):
            try:
                history, first_question, content = _transcript(scope, session_id, context)
            except FileNotFoundError:
                return SessionResult("missing")
            base = name or history.title or first_question or session_id
            slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:60] or session_id
            date = re.sub(r"[^0-9]", "", history.created_at[:10])
            relative = f"explorations/{slug}-{date}.md"
            paths, counter = bound.files(), 1
            while unique and relative in paths:
                relative = f"explorations/{slug}-{date}-{counter}.md"
                counter += 1
            outcome = publish_knowledge_changes(
                bound,
                {relative: content.encode("utf-8")},
                page_inputs={
                    relative: tuple(unit.unit_revision_id for unit in bound.input_units())
                },
                check_cancelled=context.check_stop,
                execution_id=context.storage_execution.execution_id,
            )
            if outcome.publication is None:
                return SessionResult(
                    "conflict",
                    candidate_revision_id=outcome.candidate_revision_id,
                    reason_code=outcome.reason_code,
                )
            result = SessionResult(
                "exported",
                resources=(str(scope.wiki_dir / relative),),
                changes=(f"{'updated' if relative in paths else 'created'}: wiki/{relative}",),
                publication_id=outcome.publication.publication_id,
                projection=outcome.projection,
                candidate_revision_id=outcome.candidate_revision_id,
                unfinished=("synchronize wiki",)
                if outcome.projection.status not in {"synced", "obsolete"}
                else (),
            )
            context._storage_result = "completed"
    except Exception:
        if result is None:
            raise
        result = replace(result, unfinished=(*result.unfinished, "confirm execution cleanup"))
    return result
