"""Compile a durable document plan into private, source-bound page candidates."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict, dataclass
from typing import Any

from openkb.agent.evidence_checkpoints import CompilationCheckpoints
from openkb.evidence import Evidence
from openkb.pageindex_store import indexed_reader
from openkb.processing import ProcessingIncomplete, RequestLimits
from openkb.progress import progress_scope


@dataclass(frozen=True)
class GenerationOmission:
    """A completed page failure must not retain model bodies in traceback frames."""

    reason: str


def _save_plan_state(checkpoints: Any, plan: Any) -> None:
    """Persist a linked execution receipt without making it publication state."""

    from openkb.agent.document_plan import to_dict

    key = plan.metadata.get("recovery_key")
    if isinstance(key, str):
        checkpoints.save_recovery(key, "plan", to_dict(plan))


def _planned_groups(plan: Any, *, executable_only: bool = True) -> list[dict[str, Any]]:
    """Project compact page references into the existing publication shape."""

    groups = []
    for page in plan.pages:
        if executable_only and page.state != "ready":
            continue
        path = page.target or page.name
        if not path.startswith(("concepts/", "entities/")):
            path = f"{page.kind}s/{path}"
        groups.append(
            {
                "key": page.key,
                "name": path.split("/", 1)[-1],
                "path": path,
                "title": page.title,
                "kind": page.kind,
                "type": page.type,
                "page": page,
            }
        )
    return groups


def _group_known_omissions(group: dict[str, Any], plan: Any) -> list[dict[str, Any]]:
    """Project a page's omission suffix only while that page is dispatched."""

    return [
        item.to_dict()
        for item in plan.unresolved
        if item.status == "open" and group["key"] in item.affected_pages
    ]


def _source_only_occurrences(plan: Any, source: Any, parsed: Any) -> list[dict[str, Any]]:
    """Record explicit source-only routes without pretending they are page evidence."""

    from openkb.agent.document_plan import range_intervals

    rows = []
    ordinal = 0
    for item in plan.source_only:
        for value in item.ranges:
            for index, start, end in range_intervals(value, parsed, "source only"):
                block = parsed.blocks[index]
                if "attachment" in getattr(block, "location", {}):
                    continue
                ordinal += 1
                rows.append(
                    {
                        "id": f"source-only:{ordinal}",
                        "reference": asdict(
                            Evidence(source.source_id, source.id, parsed.id, block.id, start, end)
                        ),
                        "route": "source_only",
                        "reason": item.reason,
                    }
                )
    return rows


def _unresolved_occurrences(plan: Any, source: Any, parsed: Any) -> list[dict[str, Any]]:
    """Expose open missing-material locations in the same original-range ledger."""

    from openkb.agent.document_plan import range_intervals

    rows = []
    ordinal = 0
    for item in plan.unresolved:
        if item.status != "open":
            continue
        for value in item.location:
            for index, start, end in range_intervals(value, parsed, f"unresolved {item.key}"):
                block = parsed.blocks[index]
                if "attachment" in getattr(block, "location", {}):
                    continue
                ordinal += 1
                rows.append(
                    {
                        "id": f"unresolved:{ordinal}",
                        "reference": asdict(
                            Evidence(source.source_id, source.id, parsed.id, block.id, start, end)
                        ),
                        "route": "unresolved",
                        "reason": item.reason,
                    }
                )
    return rows


def _record_plan_routes(
    report: Any, groups: list[dict[str, Any]], source: Any, parsed: Any, plan: Any
) -> None:
    """Keep coverage account based on source ranges and destinations, not facts."""

    from openkb.agent.document_page_evidence import (
        page_occurrence_descriptors,
        page_resolution_ranges,
    )

    for group in groups:
        for occurrence in page_occurrence_descriptors(
            group["page"],
            source,
            parsed,
            resolution_ranges=page_resolution_ranges(plan, group["page"]),
        ):
            routes = occurrence["routes"]
            resolution_evidence = any(row["route"] == "resolution_evidence" for row in routes)
            route = (
                "page_body"
                if any(row["route"] == "page_body" for row in routes)
                else "context_only"
            )
            identity = f"{group['key']}:{occurrence['id']}"
            report.source_occurrences[identity] = {
                "reference": occurrence["reference"],
                "route": route,
                "page": group["path"],
                "reason": "planned_page_body"
                if route == "page_body"
                else "resolved_dependency_evidence"
                if resolution_evidence
                else "planned_necessary_context",
            }
    for occurrence in _source_only_occurrences(plan, source, parsed):
        report.source_occurrences[occurrence["id"]] = occurrence
    for occurrence in _unresolved_occurrences(plan, source, parsed):
        report.source_occurrences[occurrence["id"]] = occurrence


def compile_evidence(
    kb_dir,
    workspace,
    source,
    parsed,
    name,
    settings,
    *,
    bundle=None,
    on_event=lambda event: None,
    on_deterministic_cleanup=lambda paths: None,
    navigation=None,
    resume_plan=False,
    allow_planning_omission=True,
    plan_only=False,
):
    """Plan the full document first, then generate only verified planned pages."""

    from openkb.agent.compiler import _update_index, _write_concept, _write_entity, _write_summary
    from openkb.agent.document_orchestrator import plan_document
    from openkb.agent.document_page_contracts import (
        page_retained_identity,
        publication_candidate_identity,
        reference_document_page_candidate,
        restore_document_page_candidate,
        restore_published_document_page_candidate,
    )
    from openkb.agent.document_page_verification import reverify_normalized_candidate
    from openkb.agent.document_pages import generate_document_page
    from openkb.agent.evidence_pages import retired_topic_targets, retract_retired_topics
    from openkb.agent.evidence_parallel import parallel_batches
    from openkb.agent.shared_resources import SharedResourcePool
    from openkb.compilation_report import collect_compile_report, report_content_omission
    from openkb.evidence_snapshot import EvidenceSnapshot
    from openkb.execution_measurement import document_group_scope
    from openkb.lint import list_existing_wiki_targets
    from openkb.schema import get_agents_md

    limits = RequestLimits.from_config(settings)
    reader = indexed_reader(kb_dir, source, parsed, navigation)
    with (
        CompilationCheckpoints(kb_dir, source, parsed, settings, bundle) as checkpoints,
        ExitStack() as readers,
    ):
        wiki = workspace / "wiki"
        page_settings = {**settings, "_document_schema": get_agents_md(wiki)}
        previous_targets = list_existing_wiki_targets(wiki)
        has_committed_knowledge = any(
            target.startswith(("concepts/", "entities/")) for target in previous_targets
        )
        source_unparsed = not parsed.blocks and any(
            isinstance(row, dict)
            and isinstance(row.get("reason"), str)
            and row["reason"].startswith("source_content_unparsed:")
            for row in getattr(parsed, "quality", [])
        )
        if source_unparsed and not allow_planning_omission:
            # A replacement must not turn a failed extraction into a proposal
            # that could overwrite or require acceptance for an existing
            # unowned summary.  New-source intake, by contrast, records the
            # original as an honest zero-page result below.
            raise ValueError("Source parse has no usable blocks")
        try:
            plan = plan_document(
                kb_dir,
                workspace,
                source,
                parsed,
                navigation,
                settings,
                checkpoints,
                bundle=bundle,
                on_event=on_event,
                resume=resume_plan,
                plan_only=plan_only,
            )
        except ProcessingIncomplete as exc:
            # Plan-only is an inspection boundary, not permission to turn a
            # recoverable planner failure into a fake ready DocumentPlan.
            # Leave the source pending so the UI never offers a nonexistent
            # formal plan for review or publication.
            if plan_only:
                raise
            # A source must remain available with an honest zero-page summary
            # when planning cannot safely classify its evidence.  This is not
            # a license to invent a page from an invalid response: retain the
            # original, record every affected source block as omitted, and let
            # a later Continue retry the planner.
            if (
                exc.reason
                not in {
                    "document_plan_invalid",
                    "planned_evidence_unavailable",
                    "evidence_context_exceeds_request_budget",
                    "provider_temporarily_unavailable",
                    "provider_context_exceeded",
                    "input_budget_exceeded",
                }
                or not allow_planning_omission
                or has_committed_knowledge
            ):
                raise
            report_content_omission(
                "planning", exc.reason, [block.id for block in parsed.blocks] or [source.id]
            )
            on_event(
                {
                    "stage": "planning",
                    "operation": "source_pending",
                    "reason": exc.reason,
                }
            )
            # ``document_pipeline`` still owns the source-level transaction
            # below this boundary and completes its inspectable summary from
            # this private baseline.  Materialize only that source-owned
            # summary: unlike the normal path, do not touch indexes, retired
            # contributions, a plan recovery record, or a publication intent.
            _write_summary(
                wiki,
                name,
                "# "
                + source.name
                + "\n\n本次生成知识：0 条。可手动编辑本页补充，或稍后继续处理。\n",
            )
            # This is an explicit source-level omission, not an empty formal
            # DocumentPlan. The caller may publish the inspectable source
            # summary and its omission notice, but no plan receipt, page
            # publication intent, or recovery proof may claim that a readable
            # source produced a valid zero-page proposal. Continue starts
            # planning afresh from the retained source evidence.
            return None
        all_groups = _planned_groups(plan, executable_only=False)
        ready_groups = [group for group in all_groups if group["page"].state == "ready"]

        with collect_compile_report() as report:
            _record_plan_routes(report, all_groups, source, parsed, plan)
            for page in plan.pages:
                if page.state == "blocked":
                    report_content_omission(
                        "planning", "unresolved_prerequisite_blocked", [page.name]
                    )
            for unresolved in plan.unresolved:
                if unresolved.status == "open" and unresolved.blocking:
                    report_content_omission(
                        "planning", unresolved.reason, unresolved.affected_pages
                    )

        if plan_only:
            on_event({"stage": "planning", "status": "ready", "plan_only": True})
            return plan

        used_assets = {asset for block in parsed.blocks for asset in block.assets}
        assets = {
            path.stem: "../sources/" + directory + "/" + path.name
            for directory in ("images", "attachments")
            for path in (wiki / "sources" / directory).glob("*")
            if path.stem in used_assets and path.is_file()
        }
        if set(assets) != used_assets:
            raise ProcessingIncomplete("source_assets_incomplete", "generation")
        known_targets = (
            list_existing_wiki_targets(wiki)
            | {group["path"] for group in ready_groups}
            | {f"summaries/{name}"}
        )

        # The outer batches and the per-request admission use the same shared
        # pool, so generation, verification and a bounded correction cannot each
        # independently fill the model/service budget.
        pool = SharedResourcePool(
            limits.concurrency,
            limits.context_tokens
            if limits.shared_context
            else limits.input_capacity + (limits.max_output_tokens or limits.output_tokens),
        )

        def reusable_published_reference(group: dict[str, Any]):
            """Return a prior page only when its full private contract still matches."""

            page = group["page"]

            def miss(reason: str):
                on_event(
                    {
                        "stage": "generation",
                        "operation": "published_page_regeneration",
                        "page": group["path"],
                        "reason": reason,
                    }
                )
                return None

            receipt = plan.metadata.get("publication_receipt")
            page_receipts = plan.metadata.get("publication_page_receipts")
            if (
                page.quality != "published"
                or not isinstance(receipt, dict)
                or group["path"] + ".md" not in receipt.get("pages", [])
                or not isinstance(page_receipts, dict)
                or group["path"] + ".md" not in page_receipts
            ):
                return miss("publication_receipt_unavailable")
            try:
                from openkb.agent.document_page_evidence import (
                    page_occurrence_descriptors,
                    page_resolution_ranges,
                )

                candidate = restore_published_document_page_candidate(checkpoints, page)
                occurrences = page_occurrence_descriptors(
                    page,
                    source,
                    parsed,
                    resolution_ranges=page_resolution_ranges(plan, page),
                )
                if (
                    tuple(item["id"] for item in occurrences) != candidate.occurrence_ids
                    or page_retained_identity(wiki, page, source.source_id)
                    != candidate.retained_identity
                    or page.review_receipt.get("publication_identity")
                    != publication_candidate_identity(
                        checkpoints,
                        page,
                        occurrences,
                        candidate.content,
                        candidate.retained_identity,
                        settings=page_settings,
                        known_omissions=_group_known_omissions(group, plan),
                    )
                ):
                    return miss("publication_input_changed")
            except (AttributeError, KeyError, TypeError, ValueError):
                return miss("publication_candidate_recovery_invalid")
            on_event(
                {
                    "stage": "generation",
                    "operation": "published_page_reused",
                    "page": group["path"],
                }
            )
            return reference_document_page_candidate(candidate, page_key=group["key"])

        retained = []
        groups = []
        for group in ready_groups:
            if reference := reusable_published_reference(group):
                retained.append((group, reference))
                continue
            # A structural terminal-plan reconstruction can carry a previous
            # publication state, but any changed private input requires a new
            # generation/review before that page may remain in the proposal.
            if group["page"].quality == "published":
                group["page"].quality = "planned"
                group["page"].review_receipt = None
            groups.append(group)

        concurrency = max(1, min(pool.concurrency, len(groups)))
        if concurrency > 1:
            reader = readers.enter_context(EvidenceSnapshot(reader))

        from openkb.agent.document_page_evidence import page_resolution_ranges

        def generate_once(group: dict[str, Any]):
            with pool.get_page_lock(group["path"]), document_group_scope(group["key"]):
                candidate = generate_document_page(
                    group["page"],
                    reader,
                    source,
                    parsed,
                    checkpoints,
                    wiki,
                    page_settings,
                    limits,
                    bundle=bundle,
                    on_event=on_event,
                    known_targets=known_targets,
                    assets=assets,
                    known_omissions=_group_known_omissions(group, plan),
                    resolution_ranges=page_resolution_ranges(plan, group["page"]),
                    pool=pool,
                )
            return group, candidate

        failures: list[tuple[str, str]] = []
        # Each generated body is saved by ``generate_document_page`` before it
        # returns. Keep only its compact recovery pointer here: a long document
        # must not retain every complete candidate through final normalization
        # and publication.
        accepted: list[tuple[dict[str, Any], Any]] = []
        has_drafts = False

        def restore_candidate(reference: Any):
            try:
                return restore_document_page_candidate(checkpoints, reference)
            except ValueError as exc:
                raise ProcessingIncomplete(
                    "document_candidate_recovery_invalid", "generation"
                ) from exc

        def page_omission_reason(exc: ProcessingIncomplete) -> str:
            """Map a bounded page failure to its recoverable omission reason."""

            if exc.reason in {
                "document_generation_incomplete",
                "document_response_invalid",
                "document_verification_invalid",
                "knowledge_evidence_mismatch",
                "planned_evidence_unavailable",
                "planned_page_has_no_readable_evidence",
                "planned_page_evidence_exceeds_request_budget",
                "provider_temporarily_unavailable",
                "provider_context_exceeded",
                "input_budget_exceeded",
            }:
                return exc.reason
            if exc.reason.startswith("document_review_"):
                return "knowledge_evidence_mismatch"
            raise exc

        def generate(group: dict[str, Any]):
            try:
                return generate_once(group)
            except ProcessingIncomplete as exc:
                return group, GenerationOmission(page_omission_reason(exc))

        with progress_scope("generation", len(groups), "pages") as progress:
            for _, (group, candidate) in parallel_batches(
                groups, generate, concurrency, stage="generation"
            ):
                if isinstance(candidate, GenerationOmission):
                    failures.append((group["path"], candidate.reason))
                    on_event(
                        {
                            "stage": "generation",
                            "operation": "page_pending",
                            "page": group["path"],
                            "reason": candidate.reason,
                        }
                    )
                elif candidate.quality == "verified":
                    accepted.append(
                        (
                            group,
                            reference_document_page_candidate(candidate, page_key=group["key"]),
                        )
                    )
                    group["page"].quality = "generated"
                    _save_plan_state(checkpoints, plan)
                    group["page"].quality = "verified"
                    group["page"].review_receipt = candidate.review_receipt
                else:
                    # none-mode candidates are private receipts, never a silent
                    # path into the normal publication transaction.
                    has_drafts = True
                    failures.append((group["path"], "draft_unverified"))
                    group["page"].quality = "generated"
                    _save_plan_state(checkpoints, plan)
                    group["page"].quality = "unverified"
                    group["page"].review_receipt = candidate.review_receipt
                progress.advance()

        _save_plan_state(checkpoints, plan)

        with collect_compile_report() as report:
            for path, reason in failures:
                report_content_omission("generation", reason, [path])
        reported_failures = len(failures)

        if has_drafts or settings.get("review_mode", "critical") == "none":
            # A none-mode run is private-only. It must not retract prior
            # contributions, update indexes, or write a source summary.
            raise ProcessingIncomplete("draft_unverified", "generation")

        for group, reference in accepted:
            if (
                page_retained_identity(wiki, group["page"], source.source_id)
                != reference.retained_identity
            ):
                raise ProcessingIncomplete("page_contribution_changed", "generation")
        # A new source version must not leave a failed page's contribution from
        # the old version published as if it still described this source.
        # Blocked planning work is different: it has not reached a generation
        # decision, so keep its existing contribution until the plan can resume.
        existing_targets = list_existing_wiki_targets(wiki)
        preserved = {
            page.name
            for page in plan.pages
            if page.state == "blocked" and page.name in existing_targets
        }
        from openkb.agent.evidence_markup import normalize_links

        # Link normalization changes the bytes that receive semantic approval.
        # A page whose re-review cannot pass is therefore dropped locally.  If
        # that removal changes the target catalogue, repeat normalization for
        # every published candidate so none retains a link to the dropped page.
        normalization_changed = False
        while True:
            publication_groups = [*retained, *accepted]
            planned_targets = {group["path"] for group, _ in publication_groups} | preserved
            retired = retired_topic_targets(wiki, source, planned_targets)
            targets = (existing_targets - retired) | planned_targets | {f"summaries/{name}"}
            rebound_retained = []
            rebound_accepted = []
            dropped_page = False
            for was_retained, candidates in ((True, retained), (False, accepted)):
                for group, reference in candidates:
                    candidate = restore_candidate(reference)
                    content = normalize_links(candidate.content, targets, assets, links_only=True)
                    if content == candidate.content:
                        destination = rebound_retained if was_retained else rebound_accepted
                        destination.append((group, reference))
                        continue
                    try:
                        with document_group_scope(group["key"]):
                            candidate = reverify_normalized_candidate(
                                group["page"],
                                reader,
                                source,
                                parsed,
                                checkpoints,
                                wiki,
                                page_settings,
                                limits,
                                candidate,
                                content,
                                bundle=bundle,
                                on_event=on_event,
                                known_omissions=_group_known_omissions(group, plan),
                                resolution_ranges=page_resolution_ranges(plan, group["page"]),
                                pool=pool,
                            )
                    except ProcessingIncomplete as exc:
                        reason = page_omission_reason(exc)
                        failures.append((group["path"], reason))
                        on_event(
                            {
                                "stage": "generation",
                                "operation": "page_pending",
                                "page": group["path"],
                                "reason": reason,
                            }
                        )
                        # The original candidate was reviewed before its links
                        # changed. It remains a private retry receipt, never a
                        # verified publication input.
                        group["page"].quality = "planned"
                        group["page"].review_receipt = None
                        normalization_changed = True
                        dropped_page = True
                        continue
                    # A changed retained page must become part of this proposal:
                    # its fresh whole-page receipt authorizes the new bytes and
                    # the publication receipt must advance with that candidate.
                    group["page"].quality = "verified"
                    group["page"].review_receipt = candidate.review_receipt
                    rebound_accepted.append(
                        (group, reference_document_page_candidate(candidate, page_key=group["key"]))
                    )
                    normalization_changed = True
            retained, accepted = rebound_retained, rebound_accepted
            if not dropped_page:
                break
        publication_groups = [*retained, *accepted]
        groups = [group for group, _ in publication_groups]
        if normalization_changed:
            _save_plan_state(checkpoints, plan)

        with collect_compile_report() as report:
            for path, reason in failures[reported_failures:]:
                report_content_omission("generation", reason, [path])
            for group, reference in [*retained, *accepted]:
                for occurrence in reference.occurrence_ids:
                    report.published_occurrences.add(f"{group['key']}:{occurrence}")

        retract_retired_topics(
            wiki, source, f"summaries/{name}.md", {g["path"] for g in groups} | preserved
        )
        actual_targets = (
            list_existing_wiki_targets(wiki) | {g["path"] for g in groups} | {f"summaries/{name}"}
        )
        if actual_targets != targets:
            raise ProcessingIncomplete("publication_targets_changed", "generation")
        for group, reference in accepted:
            candidate = restore_candidate(reference)
            writer = _write_entity if group["kind"] == "entity" else _write_concept
            writer(
                wiki,
                group["name"],
                candidate.content,
                f"summaries/{name}.md",
                (wiki / f"{group['path']}.md").exists(),
                brief=group["title"],
                **({"type_": group["type"]} if group["kind"] == "entity" else {}),
            )
        _save_plan_state(checkpoints, plan)
        # The overview remains an inspectable private planning result. The source
        # summary lists verified pages but does not auto-publish it as knowledge.
        _write_summary(
            wiki,
            name,
            "# "
            + source.name
            + "\n\n"
            + ("本次生成知识：0 条。可手动编辑本页补充，或稍后继续处理。\n\n" if not groups else "")
            + "\n".join(f"- [[{group['path']}|{group['title']}]]" for group in groups),
        )
        _update_index(
            wiki,
            name,
            [group["name"] for group in groups if group["kind"] == "concept"],
            entity_names=[group["name"] for group in groups if group["kind"] == "entity"],
            entity_meta={
                group["name"]: (group["type"], group["title"])
                for group in groups
                if group["kind"] == "entity"
            },
        )
        from openkb.compilation_omissions import prune_withdrawn_links

        # Current candidates were normalized and, if bytes changed, critically
        # reviewed above. Existing cross-source pages are different: cleanup
        # remains a proposed protected diff until its owner explicitly accepts
        # it, rather than becoming an unreviewed silent rewrite.
        on_deterministic_cleanup(
            prune_withdrawn_links(wiki, previous_targets - list_existing_wiki_targets(wiki))
        )
        return plan
