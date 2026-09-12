"""Compile complete structural evidence into one proposed source contribution."""

from __future__ import annotations

from openkb.agent.evidence_checkpoints import CompilationCheckpoints
from openkb.evidence import ParseStore
from openkb.processing import ProcessingIncomplete, RequestLimits
from openkb.progress import progress_scope


def compile_evidence(
    kb_dir, workspace, source, parsed, name, settings, *, bundle=None, on_event=lambda event: None
):
    from openkb.agent.compiler import (
        _update_index,
        _write_concept,
        _write_entity,
        _write_summary,
    )
    from openkb.lint import list_existing_wiki_targets

    limits = RequestLimits.from_config(settings)
    reader = ParseStore(kb_dir).reader(source, parsed)
    checkpoints = CompilationCheckpoints(kb_dir, source, parsed, settings, bundle)

    from openkb.agent.evidence_facts import extract_facts

    facts = extract_facts(kb_dir, source, parsed, settings, limits, checkpoints, bundle, on_event)
    from openkb.agent.evidence_plan import plan_topics

    wiki = workspace / "wiki"
    groups = plan_topics(
        sorted({fact["topic"] for fact in facts}),
        workspace,
        settings,
        limits,
        checkpoints,
        bundle=bundle,
        on_event=on_event,
    )
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
        reader = EvidenceSnapshot(reader)

    def generate_once(group):
        selected = list(
            {fact["id"]: fact for fact in facts if fact["topic"] in group["members"]}.values()
        )
        from openkb.agent.evidence_pages import generate_topic

        content = generate_topic(
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
        )
        return group, content

    failures = []

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
            }:
                raise
            return group, exc

    with progress_scope("generation", len(groups), "topics") as progress:
        for _, (group, content) in parallel_batches(
            groups, generate, generation_concurrency, stage="generation"
        ):
            if isinstance(content, ProcessingIncomplete):
                failures.append(content)
                on_event(
                    {
                        "stage": "generation",
                        "operation": "topic_pending",
                        "topic": group["title"],
                        "reason": content.reason,
                    }
                )
                continue
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
            progress.advance()
        if failures:
            raise failures[0]
    _write_summary(
        wiki,
        name,
        "# "
        + source.name
        + "\n\n"
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
