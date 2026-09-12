"""Optional local PageIndex enhancement of immutable, complete source positions."""

from __future__ import annotations

from dataclasses import asdict
from importlib.metadata import version as package_version

from openkb.evidence import Evidence, ParseStore
from openkb.implementation import module_revision
from openkb.locks import atomic_write_json
from openkb.processing import (
    processing_checkpoint,
    validate_usage,
)
from openkb.sources import SourceStore, content_id, read_object


def _location_rows(source, parsed):
    return [
        {
            "block_id": block.id,
            "kind": block.kind,
            "location": block.location,
            "reference": asdict(Evidence(source.source_id, source.id, parsed.id, block.id)),
            "order": block.order,
        }
        for block in parsed.blocks
    ]


def read_navigation(kb_dir, source, *, offset=0, limit=100, identity=None):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Invalid navigation window")
    store = SourceStore(kb_dir)
    root = store.owned_path(store.root / "navigation")
    if identity is None:
        from openkb.state import HashRegistry

        published = HashRegistry(kb_dir / ".openkb/hashes.json").get(source.source_id)
        if published and published.get("source_version") == source.id:
            identity = published.get("navigation_id")
        if identity is None:
            pointer = store.owned_path(root / "latest" / f"{source.id}.json")
            if not pointer.exists():
                return None
            selected = read_object(pointer)
            if set(selected) != {"navigation"}:
                raise ValueError("Invalid navigation pointer")
            identity = selected["navigation"]
    from openkb.sources import valid_id

    record = read_object(store.owned_path(root / f"{valid_id(identity)}.json"))
    if (
        set(record) - {"nodes", "schema"}
        != {"source_id", "version", "parse", "profile", "status", "reason", "positions", "usage"}
        or content_id(record) != identity
        or record.get("version") != source.id
        or record.get("source_id") != source.source_id
        or not isinstance(record.get("status"), str)
        or record["status"] not in {"basic", "enhanced", "degraded"}
        or (record.get("reason") is not None and not isinstance(record["reason"], str))
    ):
        raise ValueError("Navigation identity mismatch")
    valid_id(record["profile"])
    parsed = ParseStore(kb_dir).load(valid_id(record["parse"]))
    if parsed.input_key != source.input_key:
        raise ValueError("Navigation parsing identity mismatch")
    rows = record["positions"]
    expected = _location_rows(source, parsed)
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise ValueError("Invalid navigation coverage")
    for row, original in zip(rows, expected):
        if (
            not isinstance(row, dict)
            or {key: row.get(key) for key in original} != original
            or set(row) - original.keys() - {"title", "summary", "parents"}
            or any(key in row and not isinstance(row[key], str) for key in ("title", "summary"))
            or not isinstance(row.get("parents", []), list)
            or any(
                type(parent) is not int or not 1 <= parent <= len(rows)
                for parent in row.get("parents", [])
            )
        ):
            raise ValueError("Invalid navigation position")
    if "nodes" in record:
        from openkb.navigation_tree import validate_nodes

        if type(record.get("schema")) is not int or record["schema"] != 2:
            raise ValueError("Invalid navigation schema")
        validate_nodes(record["nodes"], len(rows))
    validate_usage(record["usage"])
    return {
        **record,
        "id": identity,
        "positions": rows[offset : offset + limit],
        "total_positions": len(rows),
        "capabilities": navigation_capabilities(record),
        "next_offset": offset + limit if offset + limit < len(rows) else None,
    }


def build_navigation(kb_dir, source, parsed, settings, *, bundle=None):
    """Rebuild a saved parse and atomically switch only a matching published view."""
    from openkb.compilation_report import collect_compile_report
    from openkb.locks import kb_ingest_lock
    from openkb.mutation import mutation_scope
    from openkb.navigation_usage import NavigationRun
    from openkb.processing import processing_scope
    from openkb.state import HashRegistry

    store = SourceStore(kb_dir)
    with kb_ingest_lock(kb_dir / ".openkb"):
        registry_path = kb_dir / ".openkb/hashes.json"
        registry = HashRegistry(registry_path)
        published = registry.get(source.source_id)
        if published and (
            published.get("source_version") != source.id or published.get("parse_id") != parsed.id
        ):
            raise ValueError(
                "Rebuild parsing differs from published knowledge; compile and publish first"
            )
        with collect_compile_report() as report, processing_scope(settings) as budget:
            run = NavigationRun(store, source, parsed, content_id(settings.get("navigation")))
            budget.on_observation = run.observe
            try:
                result = prepare_navigation(
                    kb_dir, source, parsed, settings, bundle=bundle, reserve_compilation=False
                )
                if published:
                    with mutation_scope(
                        kb_dir, [registry_path], operation="publish source navigation"
                    ):
                        registry.add(source.source_id, {**published, "navigation_id": result["id"]})
            finally:
                run.finish(budget)
        return {**result, "usage": report.usage}


def prepare_navigation(kb_dir, source, parsed, settings, *, bundle=None, reserve_compilation=True):
    """Build and save immutable compiler input without publishing a retrieval pointer."""
    from openkb.config import compilation_model_options
    from openkb.execution_measurement import measure_span
    from openkb.navigation_tree import basic_tree

    with measure_span("indexing"):
        processing_checkpoint("indexing")
        store = SourceStore(kb_dir)
        profile = content_id(
            {
                "options": settings.get("navigation"),
                "limits": settings.get("processing")
                if (settings.get("navigation") or {}).get("enabled", True)
                else None,
                "model": settings.get("model"),
                "model_options": compilation_model_options(settings),
                "adapter": module_revision(__name__),
                "tree": module_revision("openkb.navigation_tree"),
                "enhancement": module_revision("openkb.navigation_enhancement"),
                "structure": module_revision("openkb.navigation_structure"),
                "wire": module_revision("openkb.agent.evidence_wire"),
                "analysis": module_revision("openkb.agent.request_analysis"),
                "shared": module_revision("openkb.agent.shared_analysis"),
                "dispatch": module_revision("openkb.execution_receipt"),
                "budget": module_revision("openkb.processing"),
                "pageindex": package_version("pageindex"),
                "endpoint": content_id(getattr(bundle, "base_url", None)),
                "headers": content_id(getattr(bundle, "extra_headers", None)),
            }
        )
        cache = store.owned_path(
            store.root
            / "navigation"
            / "prepared"
            / f"{content_id([source.id, parsed.id, profile])}.json"
        )
        if cache.exists():
            try:
                identity = read_object(cache)["navigation"]
                saved = read_navigation(kb_dir, source, identity=identity, limit=200)
                # Compiler receives the full range map even when public reads paginate.
                saved["positions"] = _location_rows(source, parsed)
                return saved
            except (FileNotFoundError, KeyError, ValueError):
                # This lookup is disposable. Rebuild from validated immutable parsing.
                pass
        record = {
            "schema": 2,
            "source_id": source.source_id,
            "version": source.id,
            "parse": parsed.id,
            "profile": profile,
            "status": "basic",
            "reason": None,
            "positions": _location_rows(source, parsed),
            "usage": {},
            "nodes": basic_tree(kb_dir, source, parsed),
        }
        from openkb.navigation_enhancement import enhance_ranges
        from openkb.navigation_usage import NavigationRun
        from openkb.processing import processing_scope

        with processing_scope(settings) as budget:
            run = (
                NavigationRun(store, source, parsed, profile, included_in_compilation=True)
                if reserve_compilation
                else None
            )
            previous_observer = budget.on_observation
            if run is not None:

                def observe(value):
                    previous_observer(value)
                    run.observe(value)

                budget.on_observation = observe
            try:
                enhance_ranges(
                    kb_dir,
                    source,
                    parsed,
                    record,
                    settings,
                    bundle,
                    reserve_compilation=reserve_compilation,
                )
            finally:
                if run is not None:
                    record["usage"] = run.finish(budget)
                budget.on_observation = previous_observer
        identity = content_id(record)
        atomic_write_json(store.owned_path(store.root / "navigation" / f"{identity}.json"), record)
        atomic_write_json(cache, {"navigation": identity})
        return {**record, "id": identity}


def navigation_capabilities(record):
    nodes = record.get("nodes", [])
    return {
        "original_ranges": "complete",
        "structure": {
            "native": sum(node["structure_origin"] == "native" for node in nodes),
            "inferred": sum(node["structure_origin"] == "inferred" for node in nodes),
            "basic": sum(node["structure_origin"] == "basic" for node in nodes),
        },
        "summaries": {
            "model": sum(node["summary_origin"] == "model" for node in nodes),
            "preview": sum(node["summary_origin"] == "preview" for node in nodes),
            "unavailable": sum(node["summary_origin"] == "unavailable" for node in nodes),
        },
    }
