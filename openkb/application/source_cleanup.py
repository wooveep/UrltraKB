"""Preview-bound cleanup of unreachable source history, under the shared KB lease.

Current sources, Wiki citations, pending proposals and OCR recovery records are
roots. Content-addressed files are followed transitively; unknown 64-hex tokens
conservatively retain matching objects. Usage history is retained as accounting,
even when an explicitly cleaned old input is no longer available.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from openkb.application.execution import ExecutionContext
from openkb.evidence import ParseStore
from openkb.knowledge_commit import wiki_version
from openkb.locks import kb_ingest_lock, kb_read_lock
from openkb.mutation import mutation_scope
from openkb.processing import processing_checkpoint
from openkb.sources import SourceStore, content_id, read_object, valid_id
from openkb.state import HashRegistry

_IDENTITY = re.compile(rb"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")


@dataclass(frozen=True)
class HistoryCleanup:
    id: str
    files: tuple[str, ...]
    bytes: int
    versions: tuple[str, ...]
    parses: tuple[str, ...]


def _references(path: Path) -> set[str]:
    result: set[str] = set()
    with path.open("rb") as stream:
        tail = b""
        while block := stream.read(65536):
            processing_checkpoint()
            data = tail + block
            result.update(match.decode("ascii") for match in _IDENTITY.findall(data))
            tail = data[-66:]
    return result


def _compilation_references(path: Path) -> set[str]:
    """Whole-topic receipts must not pin obsolete copies of other sources.

    Reusing these receipts requires the other-source body to match the current
    Wiki. That current body is already a root. The receipt's own contribution
    and immutable input must remain reachable, including all its citations.
    Unknown record shapes keep the conservative complete-file scan.
    """
    record = read_object(path)
    value, identity = record.get("value"), record.get("input")
    if (
        isinstance(value, dict)
        and set(value) == {"content", "title"}
        and isinstance(value["content"], str)
        and isinstance(identity, dict)
        and re.fullmatch(r"[0-9a-f]{32}", str(identity.get("source", "")))
        and content_id(value) == record.get("value_digest")
    ):
        source = identity["source"]
        opening, closing = f"<!-- openkb-source:{source} -->", f"<!-- /openkb-source:{source} -->"
        body = value["content"]
        if body.count(opening) == body.count(closing) == 1:
            start, end = body.index(opening), body.index(closing)
            if start < end:
                owned = json.dumps(identity) + body[start : end + len(closing)]
                return {match.decode("ascii") for match in _IDENTITY.findall(owned.encode())}
    contract = record.get("contract")
    if (
        isinstance(contract, dict)
        and isinstance(contract.get("payload"), dict)
        and content_id(contract) == path.stem
        and content_id(value) == record.get("value_digest")
    ):
        contract["payload"].pop("existing", None)
        contract["payload"].pop("existing_pages", None)
        return {match.decode("ascii") for match in _IDENTITY.findall(json.dumps(record).encode())}
    return _references(path)


def _preview(kb_dir: Path) -> HistoryCleanup:
    store = SourceStore(kb_dir)
    nodes: dict[str, Path] = {}
    groups: dict[str, set[str]] = {}
    knowledge = store.owned_path(kb_dir / ".openkb/knowledge")
    for kind, directory, pattern in (
        ("versions", store.root / "versions", "*.json"),
        ("parses", store.root / "parses", "*.json"),
        ("proposals", knowledge / "proposals", "*.json"),
        ("blobs", store.root / "blobs", "*/*"),
        ("compilation", store.root / "compilation", "*.json"),
        ("navigation", store.root / "navigation", "*.json"),
        ("analysis", store.root / "analysis/records", "*.json"),
        ("analysis_inputs", store.root / "analysis/inputs", "*.json"),
        ("analysis_bindings", store.root / "analysis/bindings", "*/*.json"),
    ):
        groups[kind] = set()
        for path in store.owned_path(directory).glob(pattern):
            path = store.owned_path(path)
            identity = valid_id(path.stem)
            if identity in nodes:
                raise ValueError("Ambiguous stored content identity")
            nodes[identity] = path
            groups[kind].add(identity)
    roots = set()
    bindings: dict[str, set[str]] = {}
    for identity in groups["analysis_bindings"]:
        path = nodes[identity]
        try:
            record = read_object(path)
            version = record["source"]["version"]
            valid_id(version)
            if content_id(record) != identity:
                raise ValueError("Invalid analysis binding")
            bindings.setdefault(version, set()).add(identity)
        except (ValueError, KeyError, TypeError):
            roots.add(identity)  # Unknown history remains available for diagnosis.
    current_versions = set()
    for source in store.list_sources():
        roots.add(source.id)
        current_versions.add(source.id)
        if parsed := ParseStore(kb_dir).selected(source):
            roots.add(parsed.id)
        progress = store.owned_path(store.root / "compilation/latest" / f"{source.id}.json")
        if progress.exists():
            roots.update(_references(progress))
        navigation = store.owned_path(store.root / "navigation/latest" / f"{source.id}.json")
        if navigation.exists():
            roots.update(_references(navigation))
    wiki = wiki_version(kb_dir)
    # Generated snapshots are leaves reached by actual citations, not independent
    # roots. Only byte-identical generated files qualify; user edits stay roots.
    from openkb.navigation_tree import snapshot_markdown

    snapshots = {}
    for relative in wiki:
        match = re.fullmatch(r"sources/snapshots/([0-9a-f]{64})-([0-9a-f]{64})\.md", relative)
        if match:
            path = store.owned_path(kb_dir / "wiki" / relative)
            try:
                source = store.version(match[1])
                parsed = ParseStore(kb_dir).load(match[2])
                expected = snapshot_markdown(kb_dir, source, parsed)
                if path.read_text(encoding="utf-8") == expected:
                    snapshots[relative] = (match[1], match[2])
            except (FileNotFoundError, ValueError):
                pass  # Unknown or modified content must keep its references.
    protected_paths = [kb_dir / "wiki" / path for path in wiki if path not in snapshots]
    protected_paths += [path for path in (kb_dir / ".openkb/hashes.json",) if path.exists()]
    baseline_path = knowledge / "baselines.json"
    if baseline_path.exists():
        baselines = read_object(baseline_path)
        for relative, digest in baselines.items():
            if relative not in snapshots:
                roots.add(digest)
    # A retained pending proposal must keep its exact before/after images. Old
    # completed proposals are history, not a second chain of publication roots.
    for latest in store.owned_path(store.root / "runs").glob("*/latest.json"):
        record = read_object(store.owned_path(latest))
        attempt = valid_id(record["attempt"], source=True)
        result_path = store.owned_path(latest.parent / f"{attempt}.json")
        result = read_object(result_path)
        if result.get("input_version") in current_versions:
            protected_paths.append(result_path)
    # These records support job recovery and cumulative OCR accounting. Their
    # associated versions are deliberately retained, even without Wiki citations.
    for name in ("cloud-jobs", "local-ocr-runs", "local-ocr-results", "ocr-reprocessing"):
        protected_paths += list(store.owned_path(store.root / name).rglob("*.json"))
    protected_paths += list((kb_dir / ".openkb/visual-observations").glob("*.json"))
    protected_paths += list((kb_dir / ".openkb/chats").glob("*.json"))
    for path in protected_paths:
        roots.update(_references(store.owned_path(path)))
    retained = set()
    queue = list(roots)
    while queue:
        identity = queue.pop()
        if identity in retained or identity not in nodes:
            continue
        retained.add(identity)
        if identity in groups["analysis"]:
            try:
                record = read_object(nodes[identity])
                # Producer metadata records historical expense; it must not pin
                # an obsolete producer version when another source uses analysis.
                current = {key: value for key, value in record.items() if key != "producer"}
                for message in current.get("input", {}).get("payload", {}).get("messages", []):
                    if message.get("role") == "user":
                        body = json.loads(message["content"])
                        # Old catalogue/body windows are comparison dependencies,
                        # not a published citation or current source binding.
                        body.pop("existing", None)
                        body.pop("existing_pages", None)
                        message["content"] = json.dumps(body)
                references = {
                    match.decode("ascii")
                    for match in _IDENTITY.findall(json.dumps(current).encode())
                }
            except (ValueError, TypeError, AttributeError, KeyError):
                references = _references(nodes[identity])
        else:
            references = (
                _compilation_references(nodes[identity])
                if identity in groups["compilation"]
                else _references(nodes[identity])
            )
        references.update(bindings.get(identity, set()))
        queue.extend(references - retained)
    unused = nodes.keys() - retained
    removable = {nodes[identity] for identity in unused}
    for relative, (version, parse) in snapshots.items():
        if version in unused or parse in unused:
            removable.add(store.owned_path(kb_dir / "wiki" / relative))
    for path in store.owned_path(store.root / "navigation/prepared").glob("*.json"):
        record = read_object(store.owned_path(path))
        if record.get("navigation") in unused:
            removable.add(path)
    for lookup_kind, key in (("lookup", "parse_id"), ("selected", "parse_id")):
        for path in store.owned_path(store.root / "parses" / lookup_kind).glob("*.json"):
            record = read_object(store.owned_path(path))
            if record.get(key) in unused or (lookup_kind == "selected" and path.stem in unused):
                removable.add(path)
    for path in store.owned_path(store.root / "parses/confirmations").glob("*.json"):
        record = read_object(store.owned_path(path))
        if record.get("version") in unused or record.get("parse") in unused:
            removable.add(path)
    for path in store.owned_path(knowledge / "accepted").glob("*.json"):
        if path.stem in unused:
            removable.add(store.owned_path(path))
    for kind in ("compilation", "navigation"):
        for path in store.owned_path(store.root / kind / "latest").glob("*.json"):
            if path.stem in unused:
                removable.add(store.owned_path(path))
    # Any state change after the preview invalidates authorization, including a
    # citation in an unrelated page or an intake that starts sharing a blob.
    state = {}
    for directory in (store.root, knowledge, kb_dir / ".openkb/chats"):
        for path in directory.rglob("*"):
            path = store.owned_path(path)
            if path.is_file():
                processing_checkpoint()
                state[path.relative_to(kb_dir).as_posix()] = HashRegistry.hash_file(path)
    files = tuple(sorted(path.relative_to(kb_dir).as_posix() for path in removable))
    return HistoryCleanup(
        content_id({"state": state, "wiki": wiki, "files": files}),
        files,
        sum(path.stat().st_size for path in removable),
        tuple(sorted(unused & groups["versions"])),
        tuple(sorted(unused & groups["parses"])),
    )


def preview_history_cleanup(kb_dir: Path) -> HistoryCleanup:
    kb_dir = kb_dir.resolve()
    with kb_read_lock(kb_dir / ".openkb"):
        return _preview(kb_dir)


def cleanup_history(kb_dir: Path, preview_id: str, *, context: ExecutionContext | None = None):
    valid_id(preview_id)
    kb_dir = kb_dir.resolve()
    context = context or ExecutionContext()
    with kb_ingest_lock(kb_dir / ".openkb", cancelled=context.cancelled, on_wait=context.waiting):
        with context.begin(kb_dir):
            preview = _preview(kb_dir)
            if preview.id != preview_id:
                raise ValueError("History or citations changed; review a new cleanup preview")
            paths = [SourceStore(kb_dir).owned_path(kb_dir / path) for path in preview.files]
            baseline_path = kb_dir / ".openkb/knowledge/baselines.json"
            with mutation_scope(
                kb_dir, [*paths, baseline_path], operation="clean unreferenced source history"
            ):
                for path in paths:
                    context.check_stop()
                    path.unlink()
                if baseline_path.exists():
                    from openkb.locks import atomic_write_json

                    baselines = read_object(baseline_path)
                    deleted = {
                        path.removeprefix("wiki/")
                        for path in preview.files
                        if path.startswith("wiki/")
                    }
                    atomic_write_json(
                        baseline_path,
                        {key: value for key, value in baselines.items() if key not in deleted},
                    )
            return preview
