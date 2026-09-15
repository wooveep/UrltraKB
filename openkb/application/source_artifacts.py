"""Bounded, read-only previews of saved compilation work for a source version."""

from pathlib import Path

from openkb.evidence import ParseStore
from openkb.locks import kb_read_lock
from openkb.sources import SourceStore, content_id, read_object, valid_id


def published_source_pages(kb_dir, source_id, version_id):
    from openkb import frontmatter
    from openkb.application.pages import read_page
    from openkb.state import HashRegistry

    kb_dir = Path(kb_dir)
    with kb_read_lock(kb_dir / ".openkb"):
        source = SourceStore(kb_dir).version(version_id)
        if source.source_id != source_id:
            raise ValueError("Source identity mismatch")
        entry = HashRegistry(kb_dir / ".openkb/hashes.json").get(source_id)
        if not entry or entry.get("source_version") != version_id:
            return []
        name = entry.get("doc_name")
        if not isinstance(name, str) or not name or Path(name).name != name or "\\" in name:
            raise ValueError("Invalid published source name")
        summary = f"summaries/{name}.md"
        metadata = frontmatter.parse(read_page(kb_dir, summary).content)
        if not metadata or metadata.get("source_version") != version_id:
            raise ValueError("Published source version mismatch")
        pages = [summary]
        for folder in ("concepts", "entities"):
            for path in sorted((kb_dir / "wiki" / folder).glob("*.md")):
                target = folder + "/" + path.name
                metadata = frontmatter.parse(read_page(kb_dir, target).content) or {}
                if summary in metadata.get("sources", []):
                    pages.append(target)
        return pages


def compilation_artifacts(kb_dir, source_id, version_id, parse_id, stage, *, offset=0, limit=10):
    if stage not in {"facts", "planning", "generation"}:
        raise ValueError("Invalid compilation stage")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("Invalid artifact page")
    with kb_read_lock(Path(kb_dir) / ".openkb"):
        store = SourceStore(kb_dir)
        source = store.version(version_id)
        parsed = ParseStore(kb_dir).load(parse_id)
        if source.source_id != source_id or parsed.input_key != source.input_key:
            raise ValueError("Source and parsing identities do not match")
        records = saved_compilation_records(
            store, source_id, version_id, parse_id, drafts=stage == "generation"
        )
        matches, count = [], 0
        for record in records:
            value, key, draft = record["value"], record["key"], record["draft"]
            identity = record["input"]
            found = artifact_stage(record)
            if found != stage:
                continue
            if offset <= count < offset + limit:
                text = _preview(value, stage)
                matches.append(
                    {
                        "key": key,
                        "draft": draft,
                        "text": text[:16_000],
                        "truncated": len(text) > 16_000,
                        "model": identity.get("model", ""),
                        "evidence": _related_facts(
                            value,
                            saved_compilation_records(store, source_id, version_id, parse_id),
                            stage,
                        ),
                    }
                )
            count += 1
        return {
            "records": matches,
            "offset": offset,
            "total": count,
            "next_offset": offset + limit if offset + limit < count else None,
        }


def _preview(value, stage):
    if stage == "generation":
        receipt = value.get("_verification") or {}
        verdict = "已通过原文校验" if receipt.get("verdict") == "supported" else "尚未通过校验"
        return verdict + "\n\n" + str(value.get("title", "")) + "\n\n" + str(value["content"])
    parts = []
    if stage == "facts":
        for unit in value["units"]:
            parts.append("原文单元 " + str(unit.get("id", ""))[:12])
            for fact in unit.get("facts", []):
                parts.append(
                    f"主题：{fact.get('topic', '')}\n{fact.get('statement', '')}\n"
                    f"原文引文：{fact.get('quote', '')}"
                )
            if not unit.get("facts"):
                parts.append("无事实：" + str(unit.get("empty_reason", "未记录原因")))
    else:
        for topic in value["topics"]:
            parts.append(
                f"{topic.get('title', '')}  →  {topic.get('name', '')}\n涵盖主题："
                + "、".join(topic.get("members", []))
            )
    return "\n\n".join(parts)


def saved_compilation_records(store, source_id, version_id, parse_id, *, drafts=False):
    """Iterate identity- and digest-checked records under the caller's KB read lock."""
    root = store.owned_path(store.root / "compilation")
    latest = store.owned_path(root / "latest" / f"{version_id}.json")
    keys = read_object(latest).get("checkpoints") if latest.exists() else []
    if not isinstance(keys, list):
        raise ValueError("Invalid compilation checkpoint index")
    paths = [(store.owned_path(root / f"{valid_id(key)}.json"), key, False) for key in keys]
    if drafts:
        paths += [
            (store.owned_path(path), valid_id(path.name.removesuffix("-draft.json")), True)
            for path in sorted(store.owned_path(root / "recovery").glob("*-draft.json"))
        ]
    for path, key, draft in paths:
        record = read_object(path)
        identity = record.get("input")
        if not isinstance(identity, dict) or (
            identity.get("source"),
            identity.get("version"),
            identity.get("parse"),
        ) != (source_id, version_id, parse_id):
            continue
        value = record.get("value")
        if record.get("key") != key or record.get(
            "digest" if draft else "value_digest"
        ) != content_id(value):
            raise ValueError("Compilation artifact digest mismatch")
        if draft:
            if record.get("kind") != "draft":
                raise ValueError("Invalid draft artifact")
            # A verified response supersedes its older pending draft.
            if key in keys:
                continue
            value = (
                value.get("output") or value.get("revision") if isinstance(value, dict) else None
            )
        if not isinstance(value, dict):
            continue
        contract = record.get("contract")
        if contract is not None and content_id(contract) != key:
            raise ValueError("Compilation artifact contract mismatch")
        yield {**record, "value": value, "draft": draft}


def artifact_stage(record):
    value = record["value"]
    stage = (record.get("contract") or {}).get("payload", {}).get("stage")
    if stage is not None and stage not in {"facts", "planning", "generation"}:
        return None
    if "units" in value:
        return "facts"
    if "topics" in value and all(
        isinstance(row, dict) and {"name", "kind", "members"} <= row.keys()
        for row in value["topics"]
    ):
        return "planning"
    if "content" in value:
        return "generation"
    return None


def _related_facts(value, records, stage):
    if stage == "generation":
        return ""
    topics = {member for group in value.get("topics", []) for member in group["members"]}
    rows = []
    size = 0
    for record in [{"value": value}] if stage == "facts" else records:
        for unit in record["value"].get("units", []):
            for fact in unit.get("facts", []):
                if stage == "facts" or fact.get("topic") in topics:
                    text = (
                        f"主题：{fact.get('topic', '')}\n事实：{fact.get('statement', '')}\n"
                        f"原文引文：{fact.get('quote', '')}"
                    )
                    if text not in rows:
                        remaining = 16_000 - size
                        rows.append(text[:remaining])
                        size += len(text) + 2
                        if size >= 16_000:
                            return "\n\n".join(rows) + "\n\n［引文预览达到 16,000 字符上限］"
    return "\n\n".join(rows) or "尚无可关联的已保存事实与引文。"
