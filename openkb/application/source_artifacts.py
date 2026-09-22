"""Bounded, read-only previews of saved compilation work for a source version."""

import json
from pathlib import Path

from openkb.agent.checkpoint_artifacts import (
    artifact_summary,
    ledger_progress_preview,
    valid_artifact_summary,
)
from openkb.agent.compilation_index import (
    artifact_page as indexed_artifact_page,
)
from openkb.agent.compilation_index import (
    artifact_summaries as indexed_artifact_summaries,
)
from openkb.agent.compilation_index import (
    legacy_artifact_summaries,
)
from openkb.agent.compilation_index import (
    verified_candidates as indexed_verified_candidates,
)
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
    if stage not in {"facts", "planning", "generation", "verification"}:
        raise ValueError("Invalid compilation stage")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("Invalid artifact page")
    with kb_read_lock(Path(kb_dir) / ".openkb"):
        store = SourceStore(kb_dir)
        source = store.version(version_id)
        parsed = ParseStore(kb_dir).load(parse_id)
        if source.source_id != source_id or parsed.input_key != source.input_key:
            raise ValueError("Source and parsing identities do not match")
        indexed = indexed_artifact_page(
            store, source_id, version_id, parse_id, stage, offset=offset, limit=limit
        )
        if indexed is not None:
            rows, total = indexed
            verified = (
                indexed_verified_candidates(store, source_id, version_id, parse_id, rows) or {}
            )
            return _artifact_page(
                store,
                rows,
                total,
                offset,
                limit,
                source_id,
                version_id,
                parse_id,
                verified,
            )

        # Old JSON indexes remain readable while a source has not yet been
        # migrated. New sources never materialize this historical collection.
        index = saved_compilation_artifact_index(store, source_id, version_id, parse_id)
        adopted_pages = {
            row["page_key"]
            for row in index
            if row.get("adopted") and isinstance(row.get("page_key"), str)
        }
        selected, total = [], 0
        for row in sorted(
            index,
            key=lambda row: stage == "generation" and row["stage"] == "verification",
        ):
            found = row["stage"]
            if found != stage and not (stage == "generation" and found == "verification"):
                continue
            if (
                found == "generation"
                and not row.get("adopted")
                and row.get("page_key") in adopted_pages
            ):
                continue
            if offset <= total < offset + limit:
                selected.append(row)
            total += 1
        return _artifact_page(
            store,
            selected,
            total,
            offset,
            limit,
            source_id,
            version_id,
            parse_id,
            _verified_indexed_candidates(index),
        )


def _artifact_page(
    store, rows, total, offset, limit, source_id, version_id, parse_id, verified_candidates
):
    """Hydrate just the compact rows already selected by the index query."""

    matches = []
    for row in rows:
        record = load_compilation_artifact(store, row, source_id, version_id, parse_id)
        if record is None:
            continue
        value, key, found = record["value"], record["key"], row["stage"]
        # The compact index selects this row, but its durable review receipt
        # remains authoritative after hydration. A changed recovery draft cannot
        # inherit a stale supported label from its old index summary.
        selected = artifact_summary(record, storage=row["storage"]) or row
        formal_verified = _formally_verified_indexed_candidate(selected, verified_candidates)
        candidate = _document_candidate_key(record)
        draft = record["draft"] or (candidate is not None and not formal_verified)
        text = _preview(value, found, formal_verified=formal_verified)
        matches.append(
            {
                "key": key,
                "stage": found,
                "draft": draft,
                "text": text[:16_000],
                "truncated": len(text) > 16_000,
                "model": selected.get("model", row.get("model", "")),
                "evidence": _related_facts(value, (), found),
            }
        )
    return {
        "records": matches,
        "offset": offset,
        "total": total,
        "next_offset": offset + limit if offset + limit < total else None,
    }


def saved_compilation_artifact_index(store, source_id, version_id, parse_id):
    """Yield compact preview rows; never deserialize historical request bodies."""

    if indexed := indexed_artifact_summaries(store, source_id, version_id, parse_id):
        return tuple(indexed)
    root = store.owned_path(store.root / "compilation")
    latest = store.owned_path(root / "latest" / f"{version_id}.json")
    if not latest.exists():
        return ()
    value = read_object(latest)
    checkpoints = value.get("checkpoints", [])
    if not isinstance(checkpoints, list):
        raise ValueError("Invalid compilation artifact index")
    if "artifacts" not in value:
        # #52's compact index is additive.  A source compiled by an earlier
        # release must remain inspectable: scan its receipts one at a time and
        # retain only their compact summaries for this one UI request.
        artifacts = {
            row["storage"] + ":" + row["key"]: row
            for row in legacy_artifact_summaries(store, root, checkpoints)
        }
    else:
        artifacts = value["artifacts"]
    if not isinstance(artifacts, dict):
        raise ValueError("Invalid compilation artifact index")
    completed = {valid_id(key) for key in checkpoints}
    rows = []
    for name, row in artifacts.items():
        if not isinstance(name, str) or not valid_artifact_summary(row):
            raise ValueError("Invalid compilation artifact index")
        if (row["source"], row["version"], row["parse"]) != (
            source_id,
            version_id,
            parse_id,
        ):
            continue
        if row["storage"] == "draft" and row["key"] in completed:
            continue
        rows.append(row)
    # Checkpoint requests are immutable and have a stable source-store key;
    # mutable recovery rows retain their source order via the compact index.
    return tuple(sorted(rows, key=lambda row: (row["storage"] != "checkpoint", row["key"])))


def saved_compilation_issue_index(store, source_id, version_id, parse_id):
    """Return compact omission-to-source bindings without opening request bodies.

    Old artifact indexes did not carry this projection.  Their compatible
    fallback hydrates each relevant historic receipt serially, then retains
    only its range index for the caller.
    """

    indexed = indexed_artifact_summaries(
        store, source_id, version_id, parse_id, stages=("facts", "planning")
    )
    rows = (
        indexed
        if indexed is not None
        else saved_compilation_artifact_index(store, source_id, version_id, parse_id)
    )
    for row in rows:
        if row["stage"] not in {"facts", "planning"}:
            continue
        if "issue_index" not in row:
            record = load_compilation_artifact(store, row, source_id, version_id, parse_id)
            if record is None:
                continue
            row = artifact_summary(record, storage=row["storage"])
            if row is None or "issue_index" not in row:
                continue
        yield row


def load_compilation_artifact(store, summary, source_id, version_id, parse_id):
    """Hydrate one selected preview row and validate it against its full receipt."""

    root = store.owned_path(store.root / "compilation")
    key, storage = summary["key"], summary["storage"]
    suffix = "" if storage == "checkpoint" else "-" + storage
    path = store.owned_path(
        root / (f"{key}.json" if not suffix else f"recovery/{key}{suffix}.json")
    )
    record = read_object(path)
    identity = record.get("input")
    if not isinstance(identity, dict) or (
        identity.get("source"),
        identity.get("version"),
        identity.get("parse"),
    ) != (source_id, version_id, parse_id):
        return None
    value = record.get("value")
    digest = "value_digest" if storage == "checkpoint" else "digest"
    if record.get("key") != key or record.get(digest) != content_id(value):
        raise ValueError("Compilation artifact digest mismatch")
    if storage == "checkpoint":
        contract = record.get("contract")
        if contract is not None and content_id(contract) != key:
            raise ValueError("Compilation artifact contract mismatch")
        draft, adopted = False, False
    elif storage == "draft":
        if record.get("kind") != "draft" or not isinstance(value, dict):
            raise ValueError("Invalid draft artifact")
        value = value.get("output") or value.get("revision")
        adopted = isinstance(value, dict) and isinstance(value.get("page_key"), str)
        draft = not adopted or value.get("quality") != "verified"
    else:
        if (
            record.get("kind") != "plan"
            or not isinstance(value, dict)
            or not (
                isinstance(value.get("metadata"), dict)
                and value["metadata"].get("protocol") == "document-plan-v1"
                or ledger_progress_preview(value) is not None
            )
        ):
            raise ValueError("Invalid document plan artifact")
        draft, adopted = False, False
    contract = record.get("contract")
    stage = (contract or {}).get("payload", {}).get("stage")
    if isinstance(value, str) and stage == "planning":
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict):
        return None
    return {**record, "value": value, "draft": draft, "adopted_candidate": adopted}


def _verified_indexed_candidates(rows):
    accepted = {}
    for row in rows:
        verified = row.get("verified")
        if isinstance(verified, dict) and all(
            isinstance(verified.get(key), str)
            for key in ("page_key", "candidate", "checkpoint", "result")
        ):
            accepted[verified["page_key"], verified["candidate"]] = verified
    return accepted


def _formally_verified_indexed_candidate(row, accepted):
    page_key, candidate = row.get("page_key"), row.get("candidate")
    if not isinstance(page_key, str) or not isinstance(candidate, str):
        return False
    reviews = row.get("reviews", [])

    def accepted_review(fragment_candidate):
        verified = accepted.get((page_key, fragment_candidate))
        return verified is not None and any(
            isinstance(review, dict)
            and review.get("verdict") in {"supported", "advisory"}
            and review.get("candidate") == fragment_candidate
            and review.get("checkpoint") == verified["checkpoint"]
            and review.get("result") == verified["result"]
            for review in reviews
        )

    fragments = row.get("review_fragments")
    if isinstance(fragments, list):
        return bool(fragments) and all(
            isinstance(fragment, dict)
            and isinstance(fragment.get("candidate"), str)
            and accepted_review(fragment["candidate"])
            for fragment in fragments
        )
    verified = accepted.get((page_key, candidate))
    if verified is None:
        return False
    if not row.get("adopted"):
        return True
    return accepted_review(candidate)


def _verified_document_candidates(records):
    """Return formal page candidates accepted by a completed verification request."""

    accepted = {}
    for record in records:
        contract = record.get("contract") or {}
        payload = contract.get("payload") or {}
        if payload.get("stage") != "verification":
            continue
        if record["value"].get("verdict") not in {"supported", "advisory"}:
            continue
        page = payload.get("page") or {}
        candidate = payload.get("candidate") or {}
        candidate_digest = candidate.get("full_content_digest")
        if not isinstance(candidate_digest, str) and isinstance(candidate.get("content"), str):
            candidate_digest = content_id(candidate["content"])
        if isinstance(page.get("key"), str) and isinstance(candidate_digest, str):
            accepted[(page["key"], candidate_digest)] = {
                "checkpoint": record["key"],
                "result": content_id(record["value"]),
            }
    return accepted


def _document_candidate_key(record):
    """Return the formal candidate identity that a verification receipt covers."""

    if record.get("adopted_candidate"):
        value = record["value"]
        page_key, candidate = value.get("page_key"), value.get("candidate")
        if isinstance(page_key, str) and isinstance(candidate, str):
            return page_key, candidate
        return None
    contract = record.get("contract") or {}
    payload = contract.get("payload") or {}
    if payload.get("stage") != "generation":
        return None
    page = payload.get("page") or {}
    content = record["value"].get("content")
    if not isinstance(page.get("key"), str) or not isinstance(content, str):
        return None
    return page["key"], content_id(content)


def _formally_verified_document_candidate(record, accepted):
    """Trust a review label only when its durable checkpoint binds this candidate."""

    candidate = _document_candidate_key(record)
    if candidate is None or candidate not in accepted:
        return False
    if not record.get("adopted_candidate"):
        return True
    receipt = record["value"].get("review_receipt")
    reviews = receipt.get("reviews") if isinstance(receipt, dict) else None
    if not isinstance(reviews, list):
        return False
    verified = accepted[candidate]
    return any(
        isinstance(review, dict)
        and review.get("verdict") in {"supported", "advisory"}
        and review.get("candidate") == candidate[1]
        and review.get("checkpoint") == verified["checkpoint"]
        and review.get("result") == verified["result"]
        for review in reviews
    )


def _preview(value, stage, *, formal_verified=False):
    if progress := ledger_progress_preview(value):
        return (
            f"累计进度：已接受 {value['completed_windows']}/{value['window_count']} 个目标；"
            f"已登记 {progress['counts']['pages']} 个页面、"
            f"{progress['counts']['open_unresolved']} 个开放未决项。\n\n"
            + _preview(progress, "planning")
        )
    if stage == "generation":
        receipt = value.get("review_receipt") or value.get("_verification") or {}
        notes = value.get("review_notes", receipt.get("advisories", []))
        kinds = {note.get("kind") for note in notes if isinstance(note, dict)}
        if formal_verified and (receipt.get("verdict") == "advisory" or "uncertainty" in kinds):
            verdict = "已完成事实核对，存在待复核内容"
        elif formal_verified and "coverage" in kinds:
            verdict = "已完成事实核对，有次要遗漏"
        elif formal_verified:
            verdict = "已通过原文校验"
        else:
            verdict = "尚未通过校验"
        if value.get("source_details"):
            verdict += "；次要细节保留在原文"
        return verdict + "\n\n" + str(value.get("title", "")) + "\n\n" + str(value["content"])
    if stage == "verification":
        verdict = {
            "supported": "原文支持",
            "advisory": "原文支持，但有待复核内容",
            "unsupported": "未通过原文证据复核",
        }.get(value.get("verdict"), "校验结论无效")
        parts = ["结论：" + verdict]
        if isinstance(value.get("reason"), str) and value["reason"]:
            parts.append("理由：" + value["reason"])
        issues = value.get("issues")
        if isinstance(issues, list) and issues:
            parts.append("问题：\n" + "\n".join("- " + str(issue) for issue in issues))
        return "\n\n".join(parts)
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
    elif "page_changes" in value or "pages" in value:
        overview = value.get("overview", {})
        parts = []
        metadata = value.get("metadata", {})
        if isinstance(metadata, dict) and metadata.get("status"):
            parts.append("计划状态：" + str(metadata["status"]))
        if isinstance(overview, dict) and overview.get("text"):
            parts.append("文档概览：" + str(overview["text"]))
        for page in value.get("page_changes", value.get("pages", [])):
            if not isinstance(page, dict):
                continue
            parts.append(
                f"{page.get('title', '')}  →  {page.get('name', '')}\n覆盖范围："
                + str(page.get("subject_ranges", []))
                + f"\n状态：{page.get('state', 'planned')} / {page.get('quality', 'planned')}"
            )
        for row in value.get("source_only", []):
            if isinstance(row, dict):
                parts.append("仅保留原文：" + str(row.get("reason", "")))
        for row in value.get("unresolved", []):
            if isinstance(row, dict):
                parts.append("待解决：" + str(row.get("reason", row.get("key", ""))))
        for row in value.get("resolutions", []):
            if isinstance(row, dict):
                parts.append("已解决：" + str(row.get("unresolved_key", "")))
        return "\n\n".join(parts) or "尚无可读取的文档计划。"
    else:
        for topic in value["topics"]:
            parts.append(
                f"{topic.get('title', '')}  →  {topic.get('name', '')}\n涵盖主题："
                + "、".join(topic.get("members", []))
            )
    return "\n\n".join(parts)


def saved_compilation_records(store, source_id, version_id, parse_id, *, drafts=False):
    """Iterate identity- and digest-checked records under the caller's KB read lock."""

    from openkb.agent.compilation_index import checkpoint_keys as indexed_checkpoint_keys

    root = store.owned_path(store.root / "compilation")
    latest = store.owned_path(root / "latest" / f"{version_id}.json")
    keys = indexed_checkpoint_keys(store, version_id)
    if keys is None:
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
        adopted_candidate = False
        if draft:
            if record.get("kind") != "draft":
                raise ValueError("Invalid draft artifact")
            # A verified response supersedes its older pending draft.
            if key in keys:
                continue
            value = (
                value.get("output") or value.get("revision") if isinstance(value, dict) else None
            )
            adopted_candidate = isinstance(value, dict) and isinstance(value.get("page_key"), str)
            if adopted_candidate:
                draft = value.get("quality") != "verified"
        contract = record.get("contract")
        if contract is not None and content_id(contract) != key:
            raise ValueError("Compilation artifact contract mismatch")
        stage = (contract or {}).get("payload", {}).get("stage")
        if isinstance(value, str) and stage == "planning":
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                continue
        if not isinstance(value, dict):
            continue
        yield {
            **record,
            "value": value,
            "draft": draft,
            "adopted_candidate": adopted_candidate,
        }


def saved_document_plans(store, source_id, version_id, parse_id):
    """Yield durable private DocumentPlan recovery records for inspection."""

    root = store.owned_path(store.root / "compilation" / "recovery")
    for path in sorted(root.glob("*-plan.json")):
        record = read_object(store.owned_path(path))
        identity = record.get("input")
        if not isinstance(identity, dict) or (
            identity.get("source"),
            identity.get("version"),
            identity.get("parse"),
        ) != (source_id, version_id, parse_id):
            continue
        value = record.get("value")
        key = path.name.removesuffix("-plan.json")
        if (
            record.get("key") != key
            or record.get("kind") != "plan"
            or record.get("digest") != content_id(value)
            or not isinstance(value, dict)
            or not isinstance(value.get("metadata"), dict)
            or value["metadata"].get("protocol") != "document-plan-v1"
        ):
            raise ValueError("Invalid document plan artifact")
        yield {**record, "value": value, "draft": False}


def artifact_stage(record):
    value = record["value"]
    stage = (record.get("contract") or {}).get("payload", {}).get("stage")
    metadata = value.get("metadata")
    if ledger_progress_preview(value) is not None:
        return "planning"
    if isinstance(metadata, dict) and metadata.get("protocol") == "document-plan-v1":
        return "planning"
    if stage is not None and stage not in {"facts", "planning", "generation", "verification"}:
        return None
    if stage == "planning" and ("page_changes" in value or "pages" in value):
        return "planning"
    if "units" in value:
        return "facts"
    if "topics" in value and all(
        isinstance(row, dict) and {"name", "kind", "members"} <= row.keys()
        for row in value["topics"]
    ):
        return "planning"
    if stage == "verification" and "verdict" in value:
        return "verification"
    if "content" in value:
        return "generation"
    return None


def _related_facts(value, records, stage):
    if stage in {"generation", "verification"}:
        return ""
    if stage == "planning" and (
        ledger_progress_preview(value) is not None or "page_changes" in value or "pages" in value
    ):
        return "正式文档计划直接绑定原始证据范围。"
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
