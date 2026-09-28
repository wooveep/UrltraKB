"""Opt-in Step 3 acceptance against a real provider, with private audited artifacts.

Run with ``python -m tests.online_step3 --previous-run DIR --config-kb KB --output NEW``.
Use ``--source FILE`` instead of --previous-run to prepare an independent Step 2 input.
The normal path runs Step 3 once. --resume explicitly recovers an existing output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
from collections import Counter
from pathlib import Path

from openkb.locks import atomic_write_json, atomic_write_text, kb_ingest_lock
from openkb.processing import ProcessingIncomplete
from tests.online_audit import Audit  # re-export for private replay tools
from tests.online_audit import serial as serial
from tests.online_model import load_online_model


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): digest(path) for path in root.rglob("*") if path.is_file()}


def run(args):
    from openkb.agent.document_page_preparation import PreparationContext, prepare_planned_pages
    from openkb.agent.document_plan import to_dict
    from openkb.agent.document_planning_runtime import preparation_max_chars
    from openkb.agent.evidence_checkpoints import CompilationCheckpoints, publication_settings
    from openkb.application.execution import ExecutionContext
    from openkb.compilation_report import collect_compile_report
    from openkb.evidence import ParseStore
    from openkb.inputs import prepared_input
    from openkb.knowledge_commit import KnowledgeWorkspace
    from openkb.pageindex_store import indexed_reader
    from openkb.processing import RequestLimits, processing_scope
    from openkb.schema import AGENTS_MD, INDEX_SEED
    from openkb.sources import SourceStore

    output = args.output.resolve()
    if args.resume:
        if not (output / "input.json").is_file():
            raise ValueError("--resume requires an existing acceptance run")
        audit_dir = output / ("resume-" + str(time.time_ns()))
        audit_dir.mkdir(mode=0o700)
    else:
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
        audit_dir = output
    profile = load_online_model(config_kb=args.config_kb)
    settings = publication_settings(profile.settings, profile.bundle)
    protected = manifest(profile.config_kb)
    repo = Path(__file__).resolve().parents[1]
    tracked = subprocess.check_output(["git", "-C", str(repo), "ls-files", "-z"], text=True).split(
        "\0"
    )
    project_before = {
        name: digest(repo / name) for name in tracked if name and (repo / name).is_file()
    }
    git_status = subprocess.check_output(["git", "-C", str(repo), "status", "--short"], text=True)
    parsed_run = getattr(args, "parsed_run", None)
    previous = args.previous_run or parsed_run
    previous = previous.resolve() if previous else None
    prior = manifest(previous) if previous else None
    kb = output / "kb"
    audit = Audit(audit_dir, profile.bundle.api_key)
    audit.write(
        "isolation-before.json",
        {"config_kb": protected, "previous_run": prior, "project": project_before},
    )
    audit.write(
        "setup.json",
        {
            "online_model": profile.description(),
            "processing": settings["processing"],
            "previous_run": previous,
            "parsed_run": parsed_run,
            "git_status": git_status,
        },
    )
    if not args.resume:
        if previous:
            shutil.copytree(previous / "kb", kb)
        else:
            for name in (
                ".openkb",
                "wiki/concepts",
                "wiki/entities",
                "wiki/summaries",
                "wiki/sources",
            ):
                (kb / name).mkdir(parents=True, exist_ok=True)
            atomic_write_text(kb / "wiki/AGENTS.md", AGENTS_MD)
            atomic_write_text(kb / "wiki/index.md", INDEX_SEED)
            atomic_write_text(kb / "wiki/log.md", "# Operations Log\n")
            atomic_write_json(kb / ".openkb/hashes.json", {})
        # The isolated context resolves the same explicit KB configuration as the profile.
        for name in (".openkb/config.yaml", ".env"):
            if (profile.config_kb / name).is_file():
                shutil.copyfile(profile.config_kb / name, kb / name)
    wiki_before = manifest(kb / "wiki")
    result, prepared, error = None, [], None
    source = parsed = navigation = None
    setup_started = time.monotonic()
    try:
        with (
            audit.capture(),
            kb_ingest_lock(kb / ".openkb"),
            ExecutionContext().begin(kb),
            collect_compile_report() as report,
        ):
            # Resolve target exports inside the bounded audit, including their aliases.
            from openkb.agent.evidence_compiler import compile_evidence
            from openkb.navigation import prepare_navigation
            from openkb.parsing import parse_document

            if args.resume or previous:
                artifact_root = output if args.resume else previous
                source = SourceStore(kb).version(
                    json.loads((artifact_root / "step1-source.json").read_text())["id"]
                )
                parsed = ParseStore(kb).load(
                    json.loads((artifact_root / "step2-parsed.json").read_text())["id"]
                )
                if not parsed_run:
                    navigation = json.loads((artifact_root / "step2-navigation.json").read_text())
            else:
                with prepared_input(args.source.resolve()) as ready:
                    source = SourceStore(kb).intake(ready)
                audit.write("step1-source.json", source)
                with processing_scope(settings):
                    parsed = parse_document(kb, source, options=settings.get("parsing"))
            if not args.resume:
                audit.write("step1-source.json", source)
                audit.write("step2-parsed.json", parsed)
            if navigation is None:
                with processing_scope(settings):
                    navigation = prepare_navigation(
                        kb,
                        source,
                        parsed,
                        settings,
                        bundle=profile.bundle,
                        reserve_compilation=False,
                    )
            for name, value in (
                ("step1-source.json", source),
                ("step2-parsed.json", parsed),
                ("step2-navigation.json", navigation),
            ):
                if not args.resume:
                    audit.write(name, value)
            setup_elapsed = time.monotonic() - setup_started
            audit.write("step2-compile-report.json", report)
            audit.write(
                "step2-summary.json",
                {
                    "reused": bool(previous or args.resume) and not bool(parsed_run),
                    "reused_parse": bool(previous or args.resume),
                    "elapsed_seconds": setup_elapsed,
                    "calls": dict(audit.calls),
                    "requests": len(audit.requests),
                },
            )
            audit.write(
                "input.json",
                {
                    "previous_run": previous,
                    "source_file": args.source,
                    "online_model": profile.description(),
                    "processing": settings["processing"],
                    "git_head": subprocess.check_output(
                        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
                    ).strip(),
                    "git_status": git_status,
                    "project_files_before": project_before,
                    "source_sha256": digest(SourceStore(kb).original(source)),
                    "source_id": source.source_id,
                    "version_id": source.id,
                    "parse_id": parsed.id,
                    "navigation_id": navigation["id"],
                    "blocks": len(parsed.blocks),
                    "entrypoint": "compile_evidence(plan_only=True)",
                    "resume_plan": args.resume,
                },
            )
            audit.phase = "step3"
            if comparison := getattr(args, "compare_pages_from", None):
                from tests.online_step3_comparison import compare_pages

                compare_pages(comparison, audit, profile, settings, kb, source, parsed, navigation)
            with processing_scope(settings):
                with KnowledgeWorkspace(kb, source, parsed, settings) as workspace:
                    result = compile_evidence(
                        kb,
                        workspace.path,
                        source,
                        parsed,
                        source.source_id,
                        settings,
                        bundle=profile.bundle,
                        navigation=navigation,
                        plan_only=True,
                        resume_plan=args.resume,
                    )
                audit.write("planning-result.json", result)
                if result.plan:
                    audit.write("step3-plan.json", to_dict(result.plan))
                    shutil.copyfile(
                        result.overview_ref, audit_dir / "overview.md"
                    ) if result.overview_ref else None
                    shutil.copyfile(
                        result.plan.metadata["plan_preview"], audit_dir / "plan-preview.md"
                    )
                    shutil.copyfile(result.report_ref, audit_dir / "program-report.json")
                    with audit.stage("prepare_pages"):
                        reader = indexed_reader(kb, source, parsed, navigation)
                        limits = RequestLimits.from_config(settings)
                        with CompilationCheckpoints(
                            kb, source, parsed, settings, profile.bundle
                        ) as checkpoints:
                            prepared = prepare_planned_pages(
                                result.plan,
                                PreparationContext(
                                    source,
                                    parsed,
                                    navigation,
                                    reader,
                                    settings,
                                    checkpoints,
                                    bundle=profile.bundle,
                                    retry_skipped=args.resume,
                                ),
                            )
                        audit.write("step3-plan.json", to_dict(result.plan))
                        shutil.copyfile(result.report_ref, audit_dir / "program-report.json")
                        shutil.copyfile(
                            result.plan.metadata["plan_preview"], audit_dir / "plan-preview.md"
                        )
                    audit.write("prepared-evidence.json", prepared)
                    from copy import deepcopy

                    from openkb.planning_coverage import planning_range_views

                    prepared_plan = deepcopy(result.plan)
                    prepared_plan.pages = [row.page for row in prepared]
                    ranges = planning_range_views(prepared_plan, parsed)
                    ranges["selection"] = result.plan.metadata["planning_ranges"]["selection"]
                    audit.write("prepared-ranges.json", ranges)
                    audit.write(
                        "prepare-budget.json",
                        {
                            "input_capacity": limits.input_capacity,
                            "max_chars": preparation_max_chars(limits),
                        },
                    )
            audit.write("compile-report.json", report)
    except (Exception, ProcessingIncomplete) as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        audit.write(
            "isolation-after.json",
            {
                "config_kb": manifest(profile.config_kb),
                "previous_run": manifest(previous) if previous else None,
                "project": {
                    name: digest(repo / name) for name in project_before if (repo / name).is_file()
                },
            },
        )
        invariants = {
            "config_kb_unchanged": protected == manifest(profile.config_kb),
            "previous_run_unchanged": prior == manifest(previous) if previous else True,
            "wiki_unchanged": wiki_before == manifest(kb / "wiki"),
            "project_files_unchanged": all(
                (repo / name).is_file() and digest(repo / name) == checksum
                for name, checksum in project_before.items()
            ),
            "no_step3_upstream_rebuild": not any(
                audit.calls["step3:" + name] for name in ("parse_document", "prepare_navigation")
            ),
            "no_generation_or_publication": not any(
                audit.calls[phase + ":" + name]
                for phase in ("step2", "step3")
                for name in ("generate_document_page", "publish_proposal")
            ),
        }
        summary = {
            "outcome": result.outcome if result else None,
            "error": error,
            "elapsed_seconds": time.monotonic() - audit.started,
            "requests": dict(Counter(row["phase"] for row in audit.requests)),
            "pages": len(result.plan.pages) if result and result.plan else 0,
            "deferred": len(result.plan.metadata.get("deferred_suggestions", []))
            if result and result.plan
            else 0,
            "prepared_states": dict(Counter(row.page.state for row in prepared)),
            "skip_reasons": dict(Counter(row.reason for row in prepared if row.reason)),
            "invariants": invariants,
        }
        audit.write("summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        if not all(invariants.values()):
            raise RuntimeError("Online acceptance changed protected input artifacts")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--previous-run", type=Path)
    inputs.add_argument("--parsed-run", type=Path, help="Reuse saved source/parse and run indexing")
    inputs.add_argument("--source", type=Path)
    parser.add_argument("--config-kb", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--compare-pages-from",
        type=Path,
        help="Compare saved versus current pages rules with frozen request inputs before Step 3",
    )
    args = parser.parse_args()
    if args.compare_pages_from and (args.resume or not args.previous_run):
        parser.error("--compare-pages-from requires --previous-run and a new output")
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    run(args)


if __name__ == "__main__":
    main()
