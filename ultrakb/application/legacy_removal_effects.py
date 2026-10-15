"""Aggregate legacy cleanup before any individual mutation is admitted."""

from dataclasses import dataclass

from ultrakb import frontmatter
from ultrakb.application.removal import preview_removal
from ultrakb.artifact_references import list_artifact_references
from ultrakb.source_catalog import list_sources
from ultrakb.state import HashRegistry


@dataclass(frozen=True)
class LegacyEffects:
    names: dict
    deleted: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    retained: tuple[str, ...] = ()
    empty_pages: tuple[str, ...] = ()


def legacy_effects(root, identities, scope, *, keep_raw, keep_empty):
    entries = HashRegistry(root / ".ultrakb/hashes.json").all_entries()
    sources = list_sources(root)
    selected = {item.value for item in identities if item.kind == "legacy"}
    names, deleted, modified, retained = {}, set(), set(), set()
    markers, candidates, images, own_paths = set(), set(), set(), []
    for identity in selected:
        meta = entries.get(identity)
        if meta is None or any(
            identity in {source.source_id, source.legacy_hash} for source in sources
        ):
            raise ValueError("旧资料身份已改变或与新版来源冲突；请重新核对。")
        doc_name = meta.get("doc_name") or meta.get("name", "").rsplit(".", 1)[0]
        if any(source.doc_name == doc_name for source in sources) or any(
            key != identity
            and (other.get("doc_name") or other.get("name", "").rsplit(".", 1)[0]) == doc_name
            for key, other in entries.items()
        ):
            raise ValueError("旧资料正文名称与其他资料共享，不能安全独立清理。")
        preview = preview_removal(
            root, identity, scope=scope, keep_raw=keep_raw, keep_empty=keep_empty
        )
        if preview.status != "ready" or preview.plan is None or preview.plan.file_hash != identity:
            raise ValueError("旧资料清理目标已改变；请重新预览。")
        plan = preview.plan
        names[identity] = f"{plan.name} · {meta.get('raw_path') or '旧式登记'}"
        markers.add(f"summaries/{plan.doc_name}.md")
        own_paths.extend((plan.summary_path, plan.source_md, plan.source_json, plan.images_dir))
        if plan.images_dir.is_dir():
            images.add(plan.images_dir)
        for action in plan.actions:
            if action.tag not in {"DELETE", "MODIFY", "KEEP"}:
                continue
            path = action.target.split("  (", 1)[0].rstrip("/")
            {"DELETE": deleted, "MODIFY": modified, "KEEP": retained}[action.tag].add(path)
        if plan.raw_path or plan.kept_raw:
            candidates.add(plan.raw_path or plan.kept_raw)
    empty = set()
    for category in ("concepts", "entities"):
        for path in (scope.wiki_dir / category).glob("*.md"):
            refs = frontmatter.parse(path.read_text()).get("sources", [])
            if not isinstance(refs, list) or not set(refs) & markers:
                continue
            relative = str(path.relative_to(root))
            if not set(refs) - markers:
                empty.add(relative)
                if not keep_empty:
                    deleted.add(relative)
                    modified.discard(relative)
                    own_paths.append(path)
                else:
                    modified.add(relative)
                    retained.add(relative)
            else:
                modified.add(relative)
    references = list_artifact_references(
        root, excluding_registry_entries=frozenset(selected), excluding_live_paths=tuple(own_paths)
    )
    for candidate in candidates:
        relative = str(candidate.relative_to(root))
        other_owner = any(
            key not in selected
            and other.get("raw_path")
            and (root / other["raw_path"]).resolve() == candidate
            for key, other in entries.items()
        )
        if (
            not keep_raw
            and not other_owner
            and references.complete
            and candidate not in references.paths
        ):
            retained.discard(relative)
            deleted.add(relative)
        else:
            deleted.discard(relative)
            retained.add(relative)
    for directory in images:
        relative = str(directory.relative_to(root))
        if references.complete and not any(
            path.is_relative_to(directory) for path in references.paths
        ):
            deleted.add(relative)
            retained.discard(relative)
        else:
            deleted.discard(relative)
            retained.add(relative)
    return LegacyEffects(
        names,
        tuple(sorted(deleted)),
        tuple(sorted(modified - deleted)),
        tuple(sorted(retained)),
        tuple(sorted(empty)),
    )
