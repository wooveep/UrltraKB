"""Facts and shared preparation policy for navigation-based document planning."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from openkb import frontmatter
from openkb.sources import SourceStore, content_id
from openkb.state import HashRegistry

POLICY_VERSION = "planning-runtime-v1"
SOURCE_REQUEST_LIMIT = 6


def preparation_max_chars(limits) -> int:
    """Existing character admission policy, not a token conversion guarantee."""
    return max(1000, limits.input_capacity * 2)


def read_catalog(wiki: Path, targets: set[str], entity_types=()):
    """Read each existing page once; retain missing metadata as unknown."""
    entries, metadata = [], {}
    for target in sorted(targets):
        if not target.startswith(("concepts/", "entities/")):
            continue
        path = wiki / f"{target}.md"
        if path.is_symlink():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        fields = frontmatter.parse(content)
        body = (frontmatter.split(content) or ("", content))[1]
        heading = next(
            (
                m[1].strip()
                for line in body.splitlines()[:20]
                if (m := re.match(r"^#\s+(.+)$", line))
            ),
            "",
        )
        title = fields.get("title")
        if not isinstance(title, str) or not title.strip():
            title = heading or target.rsplit("/", 1)[-1].replace("-", " ")
        brief, origin = "", "body"
        for key in ("description", "brief"):
            value = fields.get(key)
            if isinstance(value, str) and value.strip():
                brief, origin = value.strip(), key
                break
        if not brief:
            brief = next(
                (
                    line.strip()
                    for line in body.splitlines()
                    if line.strip() and not line.startswith("#")
                ),
                "",
            )
        sources = fields.get("sources")
        valid_sources = isinstance(sources, list) and all(
            isinstance(value, str) and value.strip() for value in sources
        )
        page_type = fields.get("type")
        entries.append((target, title, brief[:150]))
        metadata[target] = {
            "kind": "concept" if target.startswith("concepts/") else "entity",
            "type": page_type.lower()
            if isinstance(page_type, str) and page_type.lower() in entity_types
            else None,
            "source_count": len({value.strip() for value in sources})
            if valid_sources and isinstance(sources, list)
            else None,
            "metadata_status": {
                "brief_origin": origin,
                "sources": "available" if valid_sources else "unknown",
            },
        }
    return entries, metadata


def registered_sources(kb: Path, source):
    """Count registered logical documents, not pending source-store versions."""
    entries = HashRegistry(kb / ".openkb/hashes.json").all_entries()
    # SourceStore is an identity map only; it does not contribute countable documents.
    versions = SourceStore(kb).list_sources() if entries else ()
    by_origin = {row.origin: row.source_id for row in versions}
    by_origin[getattr(source, "origin", "")] = source.source_id
    identities, unknown = set(), []
    for key, record in entries.items():
        origin = record.get("origin")
        mapped = by_origin.get(origin) if origin else None
        identity = key if re.fullmatch(r"[0-9a-f]{32}", key) else mapped
        if identity and mapped and identity != mapped:
            raise ValueError("Registered document identity conflicts with source origin")
        if identity is None:
            unknown.append(key)
        elif identity != source.source_id:
            identities.add(identity)
    # Binding detects other-document registry changes, even if the count stays equal.
    others = {
        key: value
        for key, value in entries.items()
        if key != source.source_id and by_origin.get(value.get("origin", "")) != source.source_id
    }
    return {
        "registered_other_sources": None if unknown else len(identities),
        "registry_status": "unknown_identity" if unknown else "available",
        "registry_binding": content_id(others),
    }


def build_planning_runtime(source, parsed, facts, limits):
    """One immutable fact object; projected catalogue size never determines KB stage."""
    targets = facts["targets"]
    total = len(targets)
    read = facts["read"]
    count = facts["registered_other_sources"]
    if count is None or read != total:
        stage = "unknown"
    elif count >= 3 or (count == 0 and total):
        stage = "established"
    else:
        stage = "initial"
    chars = sum(block.chars for block in parsed.blocks if "attachment" not in block.location)
    maximum = preparation_max_chars(limits)
    return {
        "policy_version": POLICY_VERSION,
        "document_name": getattr(source, "name", source.source_id),
        "kb_stage": stage,
        "registered_other_sources": count,
        "registry_status": facts["registry_status"],
        "registry_binding": facts["registry_binding"],
        "existing_concepts": sum(path.startswith("concepts/") for path in targets),
        "existing_entities": sum(path.startswith("entities/") for path in targets),
        "catalog_status": {
            "total": total,
            "read": read,
            "shown": read,
            "unread": total - read,
            "omitted": 0,
        },
        "source_body_chars": chars,
        "preparation_max_chars": maximum,
        "whole_source_exceeds_preparation": chars > maximum,
    }


def read_planning_inputs(kb, wiki, source, parsed, settings, limits):
    """Production and comparison use the same catalogue/KB fact collection."""
    from openkb.config import resolve_entity_types
    from openkb.lint import list_existing_wiki_targets

    targets = list_existing_wiki_targets(wiki)
    entries, metadata = read_catalog(wiki, targets, resolve_entity_types(settings))
    runtime = build_planning_runtime(
        source,
        parsed,
        {
            **registered_sources(kb, source),
            "targets": {path for path in targets if path.startswith(("concepts/", "entities/"))},
            "read": len(entries),
        },
        limits,
    )
    return {
        "catalog_targets": sorted(targets),
        "catalog": entries,
        "catalog_types": {path: row["type"] for path, row in metadata.items() if row["type"]},
        "catalog_metadata": metadata,
        "runtime": runtime,
    }


def selection_counts(pages, existing_targets):
    counts = {kind: {"create": 0, "update": 0} for kind in ("concept", "entity")}
    seen = set()
    for page in pages:
        kind = page.get("kind")
        if kind not in counts:
            continue
        identity = page.get("target") or page.get("name") or page["key"]
        if (kind, identity) in seen:
            continue
        seen.add((kind, identity))
        action = "update" if identity in existing_targets else "create"
        counts[kind][action] += 1
    return counts


def selection_guidance(runtime: dict[str, Any]) -> str:
    stage = runtime.get("kb_stage", "unknown")
    concepts = {
        "initial": (
            "Across this whole document, normally create at most 2–3 foundational concepts; "
            "zero is valid. Updates and repeated suggestions do not consume this advisory count. "
            "Use carry.selection_counts for document-wide progress, not just displayed pages. "
            "This is guidance, not a hard quota."
        ),
        "established": (
            "Prefer updating existing concepts. Create a concept only for a distinct reusable "
            "knowledge gap; there is no fixed creation quota."
        ),
        "unknown": (
            "The KB stage is unknown. Select a few reusable concepts and prefer existing pages; "
            "do not assume this is the first document."
        ),
    }[stage]
    if runtime.get("whole_source_exceeds_preparation"):
        scope = (
            "The whole source exceeds the current original-preparation character allowance. "
            "Carry known chapters relevant to each page's purpose. If the page genuinely needs "
            "the whole source, preserve that choice; preparation may skip it. "
            "Never drop necessary conditions to fit."
        )
    else:
        scope = (
            "Carry known relevant chapters without artificially fragmenting them. "
            "Unknown positions may be omitted; source locations are optional hints, "
            "not a selection gate."
        )
    return f"\n\nconcept_selection_guidance: {concepts}\n\nscope_guidance: {scope}"
