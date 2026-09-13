"""Optional local PageIndex enhancement of immutable, complete source positions."""

from __future__ import annotations

from dataclasses import asdict
from importlib.metadata import version as package_version

from openkb.evidence import Evidence, ParseStore
from openkb.implementation import module_revision
from openkb.processing import (
    processing_checkpoint,
    validate_usage,
)
from openkb.sources import SourceStore, content_id


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
    from openkb.navigation_tree import validate_nodes
    from openkb.pageindex_store import PageIndexUnavailable, load_nodes, saved_indexes
    from openkb.sources import valid_id
    from openkb.state import HashRegistry

    if identity is None:
        published = HashRegistry(kb_dir / ".openkb/hashes.json").get(source.source_id)
        if published and published.get("source_version") == source.id:
            identity = published.get("navigation_id")
    records = saved_indexes(kb_dir, identity=identity, version=source.id)
    if not records:
        if identity is not None:
            raise PageIndexUnavailable("PageIndex published source index is unavailable")
        return None
    identity, record = next(iter(records.items()))
    if (
        set(record)
        != {
            "schema",
            "source_id",
            "version",
            "parse",
            "profile",
            "status",
            "reason",
            "usage",
            "pageindex",
        }
        or type(record["schema"]) is not int
        or record["schema"] != 1
        or content_id(record) != identity
        or record["version"] != source.id
        or record["source_id"] != source.source_id
        or not isinstance(record["status"], str)
        or record["status"] not in {"basic", "enhanced", "degraded"}
        or (record["reason"] is not None and not isinstance(record["reason"], str))
    ):
        raise ValueError("PageIndex navigation identity mismatch")
    valid_id(record["profile"])
    parsed = ParseStore(kb_dir).load(valid_id(record["parse"]))
    if parsed.input_key != source.input_key:
        raise ValueError("PageIndex navigation parsing identity mismatch")
    rows = _location_rows(source, parsed)
    record["nodes"] = load_nodes(kb_dir, record["pageindex"])
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
                "verification_options": compilation_model_options(settings, verification=True),
                "adapter": module_revision(__name__),
                "tree": module_revision("openkb.navigation_tree"),
                "enhancement": module_revision("openkb.navigation_enhancement"),
                "verification": module_revision("openkb.navigation_verification"),
                "structure": module_revision("openkb.navigation_structure"),
                "wire": module_revision("openkb.agent.evidence_wire"),
                "model_json": module_revision("openkb.agent.model_json"),
                "analysis": module_revision("openkb.agent.request_analysis"),
                "shared": module_revision("openkb.agent.shared_analysis"),
                "dispatch": module_revision("openkb.execution_receipt"),
                "budget": module_revision("openkb.processing"),
                "pageindex": package_version("pageindex"),
                "pageindex_store": module_revision("openkb.pageindex_store"),
                "pageindex_bindings": module_revision("openkb.pageindex_bindings"),
                "endpoint": content_id(getattr(bundle, "base_url", None)),
                "headers": content_id(getattr(bundle, "extra_headers", None)),
            }
        )
        from openkb.pageindex_store import indexed_reader, saved_indexes

        try:
            candidates = saved_indexes(kb_dir, version=source.id, parse=parsed.id, profile=profile)
            if candidates:
                identity = next(iter(candidates))
                saved = read_navigation(kb_dir, source, identity=identity, limit=200)
                indexed_reader(kb_dir, source, parsed, saved)
                saved["positions"] = _location_rows(source, parsed)
                return saved
        except (FileNotFoundError, KeyError, ValueError):
            pass  # Explicit processing may replace a damaged database generation.
        record = {
            "schema": 1,
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
        from openkb.pageindex_store import save_index

        identity = save_index(kb_dir, source, parsed, record)
        saved = read_navigation(kb_dir, source, identity=identity)
        saved["positions"] = record["positions"]
        return saved


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
