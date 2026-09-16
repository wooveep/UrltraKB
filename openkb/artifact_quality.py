"""Durable checks bound to the saved artifact bytes, independent of task history."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from openkb.artifact_citations import ArtifactCitations
from openkb.file_state import contained_paths, file_versions
from openkb.locks import atomic_write_json, atomic_write_text
from openkb.sources import content_id, read_object

RULES = "artifact-evidence-v1"


def quality_path(kb_dir: Path, artifact: Path) -> Path:
    relative = artifact.relative_to(kb_dir).as_posix()
    path = kb_dir / ".openkb/artifact-quality" / f"{content_id(relative)}.json"
    contained_paths(kb_dir, [path])
    return path


def unknown_quality(*issues: str) -> dict:
    return {
        "status": "unknown",
        "checks": {"format": "unknown", "citations": "unknown", "semantics": "not_checked"},
        "issues": list(issues),
        "unchecked": ["semantic_support", "required_fact_coverage"],
    }


def _load_record(kb_dir: Path, artifact: Path) -> dict:
    from openkb.evidence import Evidence

    record = read_object(quality_path(kb_dir, artifact))
    digest = record.pop("digest", None)
    if (
        record.get("schema_version") != 1
        or record.get("rules") != RULES
        or record.get("artifact") != artifact.relative_to(kb_dir).as_posix()
        or content_id(record) != digest
        or not isinstance(record.get("files"), dict)
        or not isinstance(record.get("references"), list)
        or not isinstance(record.get("checks"), dict)
        or set(record["checks"]) != {"format", "citations", "semantics"}
        or any(
            value not in {"passed", "failed", "unknown", "not_checked"}
            for value in record["checks"].values()
        )
        or record.get("status") not in {"checked", "issues", "stale", "unknown"}
        or any(
            not isinstance(record.get(key), list)
            or any(not isinstance(item, str) for item in record[key])
            for key in ("issues", "inspected", "unchecked")
        )
    ):
        raise ValueError("Invalid artifact quality record")
    for relative, expected in record["files"].items():
        if not isinstance(relative, str) or not isinstance(expected, str) or len(expected) != 64:
            raise ValueError("Invalid artifact file binding")
        contained_paths(kb_dir, [kb_dir / relative])
    if any(relative not in record["files"] for relative in record["inspected"]):
        raise ValueError("Invalid artifact inspection scope")
    for item in record["references"]:
        if not isinstance(item, dict) or not isinstance(item.get("target"), str):
            raise ValueError("Invalid artifact reference")
        if not isinstance(item.get("target_sha256"), str) or len(item["target_sha256"]) != 64:
            raise ValueError("Invalid artifact reference content binding")
        reference = Evidence(**item["reference"])
        expected = (
            f"sources/snapshots/{reference.version_id}-{reference.parse_id}.md"
            f"#block-{reference.block_id}"
        )
        if item.get("asset"):
            if not isinstance(item["asset"], str) or len(item["asset"]) != 64:
                raise ValueError("Invalid artifact image binding")
            if not item["target"].startswith("sources/images/"):
                raise ValueError("Invalid artifact image destination")
            contained_paths(kb_dir, [kb_dir / "wiki" / item["target"]])
        elif item["target"] != expected:
            raise ValueError("Invalid artifact evidence destination")
    return record


def read_quality(kb_dir: Path, artifact: Path) -> dict:
    path = quality_path(kb_dir, artifact)
    if not path.exists():
        return unknown_quality("quality_record_missing")
    try:
        record = _load_record(kb_dir, artifact)
        issues = []
        current_files = artifact.rglob("*") if artifact.is_dir() else [artifact]
        if any(
            path.is_file() and path.relative_to(kb_dir).as_posix() not in record["files"]
            for path in current_files
        ):
            issues.append("artifact_files_added")
        for relative, expected in record["files"].items():
            path = kb_dir / relative
            contained_paths(kb_dir, [path])
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                issues.append(f"file_changed_or_missing: {relative}")
        from openkb.evidence import Evidence, ParseStore

        for item in record.get("references", []):
            try:
                target = kb_dir / "wiki" / unquote(urlsplit(item["target"]).path)
                contained_paths(kb_dir, [target])
                if hashlib.sha256(target.read_bytes()).hexdigest() != item["target_sha256"]:
                    issues.append("source_target_changed")
                view = ParseStore(kb_dir).read(Evidence(**item["reference"]), max_chars=16000)
                if item.get("asset"):
                    image = kb_dir / "wiki" / item["target"]
                    contained_paths(kb_dir, [image])
                    if (
                        item["asset"] not in view.assets
                        or hashlib.sha256(image.read_bytes()).hexdigest() != item["asset"]
                    ):
                        issues.append("source_image_changed_or_missing")
            except (ValueError, OSError, TypeError, KeyError):
                issues.append("source_binding_unavailable")
        if issues:
            return {
                **record,
                "status": "stale",
                "checks": unknown_quality()["checks"],
                "issues": record["issues"] + issues,
            }
        if not record["references"] and record["checks"]["citations"] == "passed":
            record["checks"]["citations"] = "not_checked"
            if "source_citations" not in record["unchecked"]:
                record["unchecked"].append("source_citations")
        return record
    except (ValueError, OSError, TypeError, KeyError):
        return unknown_quality("quality_record_unreadable")


def save_quality(kb_dir: Path, artifact: Path, files: list[Path], validation) -> dict:
    """Called within the same mutation as the output files, after model execution."""
    issues: list[str] = []
    inspected = []
    citations = ArtifactCitations(kb_dir)
    for path in sorted(set(files)):
        contained_paths(kb_dir, [path])
        if path.suffix.lower() not in {".md", ".markdown", ".html", ".htm"}:
            continue
        relative = path.relative_to(kb_dir).as_posix()
        original = path.read_text(encoding="utf-8")
        rendered = (
            citations.html(path, original)
            if path.suffix.lower() in {".html", ".htm"}
            else citations.markdown(path, original)
        )
        if rendered != original:
            atomic_write_text(path, rendered)
        inspected.append(relative)
    issues.extend(citations.issues)
    record = {
        "schema_version": 1,
        "rules": RULES,
        "artifact": artifact.relative_to(kb_dir).as_posix(),
        "files": file_versions(kb_dir, files),
        "checks": {
            "format": "unknown"
            if validation is None
            else "failed"
            if validation.errors
            else "passed",
            "citations": "failed" if issues else "passed" if citations.used else "not_checked",
            "semantics": "not_checked",
        },
        "inspected": inspected,
        "issues": issues,
        "unchecked": ["semantic_support", "required_fact_coverage"]
        + ([] if citations.used else ["source_citations"]),
        "references": citations.used,
        "status": "issues" if issues else "checked" if inspected else "unknown",
    }
    atomic_write_json(quality_path(kb_dir, artifact), {**record, "digest": content_id(record)})
    return record


def add_generated_diff(kb_dir: Path, artifact: Path, diff: Path, record: dict) -> None:
    """Bind the application's archive comparison without inventing a historical check."""
    if "files" not in record:
        return
    record = {**record, "files": {**record["files"], **file_versions(kb_dir, [diff])}}
    record.pop("digest", None)
    atomic_write_json(quality_path(kb_dir, artifact), {**record, "digest": content_id(record)})


def dependency_records(kb_dir: Path) -> dict[str, tuple[Path, dict]]:
    """Existing active and archived artifacts retain their explicit historical bindings.

    Edited artifacts still retain their recorded evidence, although their checks are
    stale. An absent artifact is no longer a root. Legacy artifacts gain no identities.
    """
    result = {}
    directory = kb_dir / ".openkb/artifact-quality"
    contained_paths(kb_dir, [directory])
    for path in sorted(directory.glob("*.json")):
        contained_paths(kb_dir, [path])
        record = read_object(path)
        relative = record.get("artifact")
        if not isinstance(relative, str):
            raise ValueError("Artifact quality history needs repair before source cleanup")
        artifact = kb_dir / relative
        contained_paths(kb_dir, [artifact])
        if path != quality_path(kb_dir, artifact):
            raise ValueError("Artifact quality identity mismatch")
        if artifact.exists():
            try:
                result[relative] = (path, _load_record(kb_dir, artifact))
            except (ValueError, OSError, TypeError, KeyError) as exc:
                raise ValueError(
                    "Artifact quality history needs repair before source cleanup"
                ) from exc
    return result


def relocate_quality(kb_dir: Path, source: Path, target: Path, record: dict) -> None:
    """Rebase a preserved copy and its record together inside the caller's mutation."""
    if "files" not in record:
        return  # Historical unknown never becomes a fabricated check.
    from openkb.artifact_citations import markdown_document, markdown_links
    from openkb.artifact_html import rewrite_html

    files = {}
    inspected = []
    references = {item["target"] for item in record["references"]}
    for relative, digest in record["files"].items():
        old = kb_dir / relative
        new = target / old.relative_to(source) if old.is_relative_to(source) else old
        if old == source:
            new = target / source.name if target.is_dir() else target
        contained_paths(kb_dir, [new])
        if not new.is_file():
            files[new.relative_to(kb_dir).as_posix()] = digest
            continue

        def rebase(value):
            parts = urlsplit(value)
            if parts.scheme or parts.netloc:
                return value
            resolved = (old.parent / unquote(parts.path)).resolve()
            if not resolved.is_relative_to(kb_dir / "wiki"):
                return value
            canonical = resolved.relative_to(kb_dir / "wiki").as_posix()
            canonical += "#" + unquote(parts.fragment) if parts.fragment else ""
            if canonical not in references:
                return value
            return quote(os.path.relpath(resolved, new.parent), safe="/.-") + (
                "#" + parts.fragment if parts.fragment else ""
            )

        if relative in record["inspected"] and new != old:
            text = new.read_text(encoding="utf-8")
            rendered = (
                rewrite_html(text, rebase)
                if new.suffix.lower() in {".html", ".htm"}
                else markdown_document(
                    text,
                    lambda value: markdown_links(value, rebase),
                    lambda value: rewrite_html(value, rebase),
                )
            )
            if rendered != text:
                atomic_write_text(new, rendered)
        key = new.relative_to(kb_dir).as_posix()
        files[key] = hashlib.sha256(new.read_bytes()).hexdigest()
        if relative in record["inspected"]:
            inspected.append(key)
    value = {
        **record,
        "artifact": target.relative_to(kb_dir).as_posix(),
        "files": files,
        "inspected": inspected,
    }
    value.pop("digest", None)
    atomic_write_json(quality_path(kb_dir, target), {**value, "digest": content_id(value)})
