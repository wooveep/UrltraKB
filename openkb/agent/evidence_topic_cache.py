"""Reuse a fully verified topic when continuing an incomplete publication."""

import posixpath
from urllib.parse import unquote, urlsplit

from openkb.agent.answer_citations import _links
from openkb.agent.evidence_markup import normalize_links
from openkb.agent.evidence_pages import _previous_contribution
from openkb.implementation import module_revision
from openkb.schema import get_agents_md
from openkb.sources import content_id


def _valid_page_links(content, page, targets):
    if normalize_links(content, targets, {}, links_only=True) != content:
        return False
    # The shared Markdown parser includes reference/HTML links and excludes
    # literal code. Resolve ordinary relative URLs from this contribution's page.
    for link in _links(content):
        try:
            url = urlsplit(link)
        except ValueError:
            return False
        if url.scheme or url.netloc or not url.path:
            continue
        path = posixpath.normpath(posixpath.join(posixpath.dirname(page), unquote(url.path)))
        target = path.lstrip("/").removesuffix(".md")
        if posixpath.splitext(path)[1] not in {"", ".md"}:
            continue
        if target == "index" or target.startswith(("concepts/", "entities/", "summaries/")):
            if target not in targets:
                return False
    return True


def verified_topic(group, facts, wiki, source, settings, checkpoints, targets, generate):
    path = wiki / f"{group['path']}.md"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    retained, _, _ = _previous_contribution(existing, source.source_id)
    # The current source's old contribution is not an input to its replacement.
    # Changes to other sources, grouping, facts, schema, model, evidence readers
    # and verification contracts all invalidate this whole-topic receipt.
    payload = {
        "stage": "generation",
        "group": group,
        "facts": facts,
        "language": settings.get("language", "en"),
        "schema": get_agents_md(wiki),
        "coordinator": module_revision("openkb.agent.evidence_compiler"),
        "receipt": module_revision(__name__),
    }
    dependencies = content_id(retained.strip())
    # The compiler always creates index.md during publication.
    available = sorted(targets - {"index"})

    def receipt_key(links):
        return checkpoints.key(
            "verified-source-topic-v1", {**payload, "targets": links}, dependencies=dependencies
        )

    key = receipt_key(available)
    resume_key = checkpoints.key("retained-verified-topic-v1", payload, dependencies=dependencies)
    saved = checkpoints.load(key)
    if saved is None:
        previous = checkpoints.load_recovery(resume_key, "topic")
        if previous is not None:
            if not isinstance(previous, list) or not all(
                isinstance(link, str) for link in previous
            ):
                raise ValueError("Invalid verified topic target receipt")
            # The recovered targets locate the original immutable receipt; the
            # current facts, group and other-source content still bind its key.
            saved = checkpoints.load(receipt_key(previous))
    if saved is not None:
        if (
            not isinstance(saved, dict)
            or set(saved) != {"content", "title"}
            or not isinstance(saved["content"], str)
            or not isinstance(saved["title"], str)
            or not saved["title"].strip()
        ):
            raise ValueError("Invalid verified topic receipt")
        # New unrelated targets need no new prose. Removing or renaming a link
        # actually used by this content requires generation under current inputs.
        if _valid_page_links(saved["content"], group["path"], targets):
            group["title"] = saved["title"]
            return saved["content"]
    content = generate()
    checkpoints.save(key, {"content": content, "title": group["title"]})
    checkpoints.save_recovery(resume_key, "topic", available)
    return content
