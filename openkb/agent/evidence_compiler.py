"""Compile complete structural evidence into one proposed source contribution."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass

from openkb.agent.evidence_checkpoints import CompilationCheckpoints
from openkb.pageindex_store import indexed_reader
from openkb.processing import ProcessingIncomplete, RequestLimits
from openkb.progress import progress_scope


@dataclass(frozen=True)
class GenerationOmission:
    """A completed failure must not retain traceback frames containing model bodies."""

    reason: str


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
    navigation=None,
    resume_plan=False,
):
    from openkb.agent.compiler import (
        _update_index,
        _write_concept,
        _write_entity,
        _write_summary,
    )
    from openkb.lint import list_existing_wiki_targets

    limits = RequestLimits.from_config(settings)
    reader = indexed_reader(kb_dir, source, parsed, navigation)
    with (
        CompilationCheckpoints(kb_dir, source, parsed, settings, bundle) as checkpoints,
        ExitStack() as readers,
    ):
        from openkb.agent.evidence_facts import extract_facts

        facts = extract_facts(
            kb_dir,
            source,
            parsed,
            settings,
            limits,
            checkpoints,
            bundle,
            on_event,
            navigation=navigation,
        )
        from openkb.agent.evidence_plan import plan_topics

        wiki = workspace / "wiki"
        previous_targets = list_existing_wiki_targets(wiki)
        groups = plan_topics(
            sorted(facts.topics) if facts else [],
            workspace,
            settings,
            limits,
            checkpoints,
            bundle=bundle,
            on_event=on_event,
            navigation=navigation,
            resume=resume_plan,
        )
        from openkb.agent.dependency_preflight import preflight_dependencies

        planned_groups = groups
        groups = preflight_dependencies(reader, source, parsed, groups, facts, settings, on_event)
        from openkb.agent.dependency_preflight import known_omissions

        initial_omissions = known_omissions(parsed)
        from openkb.agent.evidence_pages import retract_retired_topics

        retract_retired_topics(
            wiki, source, f"summaries/{name}.md", {group["path"] for group in groups}
        )
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
            | {group["path"] for group in groups}
            | {f"summaries/{name}"}
        )
        from openkb.agent.evidence_parallel import parallel_batches
        from openkb.evidence_snapshot import EvidenceSnapshot

        generation_concurrency = max(1, min(4, limits.concurrency, len(groups)))
        if generation_concurrency > 1:
            reader = readers.enter_context(EvidenceSnapshot(reader))
        from openkb.agent.operation_context import OperationContext

        operations = OperationContext(reader, source, parsed)

        def generate_once(group):
            selected = facts.for_topics(group["members"])
            from openkb.agent.dependency_preflight import omission_context

            context = omission_context(
                reader, source, parsed, initial_omissions, group, facts, planned_groups
            )
            from openkb.agent.evidence_pages import generate_topic
            from openkb.agent.evidence_topic_cache import verified_topic

            content = verified_topic(
                group,
                selected,
                wiki,
                source,
                settings,
                checkpoints,
                known_targets,
                lambda: generate_topic(
                    group,
                    selected,
                    reader,
                    checkpoints,
                    wiki,
                    source,
                    settings,
                    limits,
                    bundle=bundle,
                    on_event=on_event,
                    known_targets=known_targets,
                    assets=assets,
                    omission_context=context,
                    operation_context=operations,
                ),
                omission_context=context,
            )
            return group, content

        failures = []
        from openkb.agent.compilation_storage import CandidateInventory

        accepted = CandidateInventory(checkpoints)

        def generate(group):
            try:
                return generate_once(group)
            except ProcessingIncomplete as exc:
                if exc.reason not in {
                    "topic_generation_incomplete",
                    "evidence_output_invalid",
                    "topic_title_conflict",
                    "evidence_verification_invalid",
                    "knowledge_evidence_mismatch",
                    "generated_asset_evidence_invalid",
                    "provider_temporarily_unavailable",
                    "provider_context_exceeded",
                    "topic_context_exceeds_request_budget",
                    "topic_evidence_exceeds_request_budget",
                }:
                    raise
                return group, GenerationOmission(exc.reason)

        from openkb.agent.review_batching import review_batching

        with progress_scope("generation", len(groups), "topics") as progress, review_batching():
            for _, (group, content) in parallel_batches(
                groups, generate, generation_concurrency, stage="generation"
            ):
                if isinstance(content, GenerationOmission):
                    failures.append((group["path"], content.reason))
                    on_event(
                        {
                            "stage": "generation",
                            "operation": "topic_pending",
                            "topic": group["title"],
                            "reason": content.reason,
                        }
                    )
                    progress.advance()
                    continue
                accepted.append((group, content))
                progress.advance()
        if failures:
            from openkb.compilation_report import report_content_omission

            for path, reason in failures:
                report_content_omission("generation", reason, [path])
        from openkb.agent.evidence_dependencies import protect_dependencies

        accepted = protect_dependencies(
            reader,
            source,
            parsed,
            accepted,
            facts,
            settings,
            checkpoints,
            bundle,
            on_event,
            groups=planned_groups,
        )
        from openkb.compilation_report import collect_compile_report

        with collect_compile_report() as report:
            from openkb.agent.evidence_review import retain_review_notes, unverified_facts
            from openkb.agent.evidence_selection import referenced_facts

            members = {member for group, _ in accepted for member in group["members"]}
            retain_review_notes(report, {group["path"] for group, _ in accepted})
            report.referenced_facts = {
                path: ids
                for path, ids in report.referenced_facts.items()
                if path in {group["path"] for group, _ in accepted}
            }
            pending_facts = unverified_facts(report)
            details = referenced_facts(report)
            report.published_facts.update(
                fact["id"]
                for fact in facts
                if fact["topic"] in members and fact["id"] not in pending_facts | details
            )
        # Excluded topics withdraw only this source's contribution. A page supported
        # by another source remains intact; stale text from this source cannot stand
        # in for a failed new version. All edits still belong to the private proposal.
        groups = [group for group, _ in accepted]
        retract_retired_topics(wiki, source, f"summaries/{name}.md", {g["path"] for g in groups})
        from openkb.agent.evidence_markup import normalize_links

        targets = (
            list_existing_wiki_targets(wiki) | {g["path"] for g in groups} | {f"summaries/{name}"}
        )
        for group, content in accepted:
            if report.referenced_facts.get(group["path"]):
                from openkb.agent.evidence_selection import link_source_details

                content = link_source_details(
                    content, source.source_id, name, settings.get("language", "en")
                )
            # Strip navigation to excluded targets while keeping the displayed
            # verified text and code examples unchanged.
            content = normalize_links(content, targets, assets, links_only=True)
            writer = _write_entity if group["kind"] == "entity" else _write_concept
            writer(
                wiki,
                group["name"],
                content,
                f"summaries/{name}.md",
                (wiki / f"{group['path']}.md").exists(),
                brief=group["title"],
                **({"type_": group["type"]} if group["kind"] == "entity" else {}),
            )
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

        prune_withdrawn_links(wiki, previous_targets - list_existing_wiki_targets(wiki))
