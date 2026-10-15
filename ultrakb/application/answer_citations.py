"""Bind answer links to captured source versions and actual returned originals."""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt

from ultrakb.application.query_views import read_selected_file, resolve_read_path
from ultrakb.file_state import contained_paths
from ultrakb.locks import kb_read_lock
from ultrakb.source_context import original_paths

_WIKI = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")


def _doc_name(path):
    return Path(path).name.removesuffix(".content.json").removesuffix(".json").removesuffix(".md")


def citation_targets(markdown):
    values = []
    for token in MarkdownIt("commonmark").enable("table").parse(markdown):
        for child in token.children or ():
            if child.type == "text":
                values.extend(match[1] for match in _WIKI.finditer(child.content))
            elif child.type == "link_open":
                values.append(child.attrGet("href") or "")
    result = []
    for value in values:
        url = urlsplit(value)
        path = unquote(url.path)
        if url.scheme not in {"", "ultrakb"} or url.netloc or not path:
            continue
        path = path.removeprefix("./")
        if (
            Path(path).is_absolute()
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            continue
        result.append((value, path))
    return tuple(dict.fromkeys(result))


def _source_identity(root, view, path):
    from ultrakb.source_catalog import read_source, read_source_revision
    from ultrakb.state import HashRegistry
    from ultrakb.unit_publication import list_source_units

    revision = view.source_revisions_by_path.get(path)
    doc_name = _doc_name(path)
    if revision:
        source = read_source(root, read_source_revision(root, revision).source_id)
        unit = next(
            (
                unit
                for unit in list_source_units(root, source.source_id)
                if unit.doc_name == doc_name
            ),
            None,
        )
        return "source", source.source_id, source.name, revision, unit.unit_id if unit else None
    matches = [
        (identity, entry)
        for identity, entry in HashRegistry(root / ".ultrakb/hashes.json").all_entries().items()
        if (entry.get("doc_name") or Path(entry.get("name", "")).stem) == doc_name
    ]
    if len(matches) == 1:
        identity, entry = matches[0]
        return "legacy", identity, entry.get("name", doc_name), None, None
    return "reference", path, doc_name, None, None


def capture_answer_citations(answer, selection, returned_reads):
    """Only the final Markdown selects citations; activity logs and focus are not inputs."""
    root, references = selection.kb_dir, []
    for target, candidate in citation_targets(answer):
        found = False
        for view in selection.views:
            try:
                page = resolve_read_path(view, candidate)
                paths = original_paths(view, page)
            except (OSError, ValueError):
                continue
            for path in paths:
                kind, identity, name, revision, unit = _source_identity(root, view, path)
                # Alias formats of one processing unit may name the same original.
                matches = [
                    read
                    for read in returned_reads
                    if read.get("digest") == view.files.get(read.get("path"))
                    and (
                        read.get("path") == path
                        or (
                            revision
                            and read.get("source_revision_id") == revision
                            and _doc_name(read.get("path", "")) == _doc_name(path)
                        )
                    )
                ]
                if not page.startswith("sources/") and not matches:
                    continue
                base = view.native_file_roots.get(path, view.scope.wiki_dir)
                file = base / path
                contained_paths(root, [file])
                read_selected_file(view, path)
                references.append(
                    {
                        "target": target,
                        "path": path,
                        "kind": kind,
                        "identity": identity,
                        "name": name,
                        "source_revision_id": revision,
                        "unit_id": unit,
                        "view_id": view.view_id,
                        "knowledge_revision_id": view.knowledge_revision_id,
                        "stored_path": str(file.relative_to(root)),
                        "stored_root": str(base.relative_to(root)),
                        "digest": view.files[path],
                        "returned": bool(matches),
                        "locations": [read.get("location", {}) for read in matches],
                    }
                )
                found = True
            if not found and not page.startswith("sources/"):
                base = view.native_file_roots.get(page, view.scope.wiki_dir)
                references.append(
                    {
                        "target": target,
                        "path": page,
                        "kind": "reference",
                        "identity": page,
                        "name": page,
                        "returned": False,
                        "view_id": view.view_id,
                        "stored_path": str((base / page).relative_to(root)),
                        "stored_root": str(base.relative_to(root)),
                        "digest": view.files[page],
                    }
                )
                found = True
        if not found:
            references.append(
                {
                    "target": target,
                    "path": candidate,
                    "kind": "reference",
                    "identity": candidate,
                    "name": candidate,
                    "returned": False,
                    "stored_path": None,
                    "digest": None,
                    "view_id": selection.views[0].view_id if selection.views else "legacy",
                }
            )
    return {"version": 1, "references": references}


def validate_citation_snapshot(value):
    if value is None:
        return
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or not isinstance(value.get("references"), list)
    ):
        raise ValueError("Invalid answer citation snapshot")
    for reference in value["references"]:
        if not isinstance(reference, dict) or any(
            not isinstance(reference.get(key), str)
            for key in ("target", "path", "kind", "identity", "name")
        ):
            raise ValueError("Invalid answer source reference")
        if (
            reference["kind"] not in {"source", "legacy", "reference"}
            or type(reference.get("returned")) is not bool
        ):
            raise ValueError("Invalid answer source provenance")


@dataclass(frozen=True)
class CitationGroup:
    kind: str
    identity: str
    name: str
    references: tuple[dict, ...]
    historical_unknown: bool = False


def answer_citations(answer):
    targets = {target for target, _ in citation_targets(answer.markdown)}
    snapshot = answer.citations
    validate_citation_snapshot(snapshot)
    references = (
        [reference for reference in snapshot["references"] if reference["target"] in targets]
        if snapshot
        else [
            {
                "target": target,
                "path": path,
                "kind": "reference",
                "identity": path.removesuffix(".md"),
                "name": path,
                "returned": False,
                "stored_path": None,
                "digest": None,
                "view_id": answer.view_id,
            }
            for target, path in citation_targets(answer.markdown)
        ]
    )
    groups = {}
    for reference in references:
        key = reference["kind"], reference["identity"]
        groups.setdefault(key, []).append(reference)
    return tuple(
        CitationGroup(kind, identity, values[0]["name"], tuple(values), snapshot is None)
        for (kind, identity), values in groups.items()
    )


def read_answer_reference(kb_dir: Path, reference, *, historical_unknown=False):
    root = kb_dir.resolve()
    with kb_read_lock(root / ".ultrakb"):
        raw_path = reference.get("stored_path")
        if raw_path is None and historical_unknown:
            from ultrakb.application.pages import read_page

            page = read_page(root, reference["path"].removesuffix(".md"))
            return (
                page.body,
                "旧回答未保存证据快照；这是当前可读引用，无法确认生成时版本。",
                root / "wiki",
            )
        if not raw_path:
            raise ValueError("该引用未记录可读的原文依据；请在资料页核对并恢复内容。")
        path = root / raw_path
        contained_paths(root, [path])
        base = root / reference.get("stored_root", "invalid")
        allowed = reference.get("stored_root") == "wiki" or (
            base.relative_to(root).parts[:2]
            in {(".ultrakb", "knowledge"), (".ultrakb", "retained-documents")}
        )
        if (
            not allowed
            or not path.resolve().is_relative_to(base.resolve())
            or path.suffix not in {".md", ".json"}
        ):
            raise ValueError("Invalid retained source path")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != reference["digest"]:
            raise ValueError("生成时依据已改变或缺失；不会替换为另一修订。请在资料页核对原件。")
        if path.name.endswith(".content.json"):
            from ultrakb.text_source import read_text_selection

            content = read_text_selection(json.loads(data))["content"]
        elif path.suffix == ".json":
            from ultrakb.source_pages import read_page_selection

            content = read_page_selection(json.loads(data))["content"]
        else:
            content = data.decode("utf-8")
        note = "生成时原文版本；" + (
            "有本次原文读取记录（读取不表示覆盖全文或核实所有结论）。"
            if reference["returned"]
            else "仅为正文引用，未找到本次原文读取记录。"
        )
        if not reference["path"].startswith("sources/"):
            note = "生成时保留的摘要或知识页面；生成内容不能代替原文核实。"
        if reference.get("locations"):
            note += "\n本次返回的阅读位置：" + json.dumps(
                reference["locations"], ensure_ascii=False
            )
        return content, note, path.parent
