"""Reuse a fully verified topic when continuing an incomplete publication."""

from openkb.agent.evidence_pages import _previous_contribution
from openkb.implementation import module_revision
from openkb.schema import get_agents_md
from openkb.sources import content_id


def verified_topic(group, facts, wiki, source, settings, checkpoints, targets, generate):
    path = wiki / f"{group['path']}.md"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    retained, _, _ = _previous_contribution(existing, source.source_id)
    # The current source's old contribution is not an input to its replacement.
    # Changes to other sources, grouping, facts, schema, model, evidence readers
    # and verification contracts all invalidate this whole-topic receipt.
    key = checkpoints.key(
        "verified-source-topic-v1",
        {
            "stage": "generation",
            "group": group,
            "facts": facts,
            "language": settings.get("language", "en"),
            # The compiler always creates index.md during publication.
            "targets": sorted(targets - {"index"}),
            "schema": get_agents_md(wiki),
            "coordinator": module_revision("openkb.agent.evidence_compiler"),
            "receipt": module_revision(__name__),
        },
        dependencies=content_id(retained.strip()),
    )
    saved = checkpoints.load(key)
    if saved is not None:
        if (
            not isinstance(saved, dict)
            or set(saved) != {"content", "title"}
            or not isinstance(saved["content"], str)
            or not isinstance(saved["title"], str)
            or not saved["title"].strip()
        ):
            raise ValueError("Invalid verified topic receipt")
        group["title"] = saved["title"]
        return saved["content"]
    content = generate()
    checkpoints.save(key, {"content": content, "title": group["title"]})
    return content
