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
import sys
import time
from collections import Counter
from dataclasses import asdict, is_dataclass
from pathlib import Path

from openkb.locks import atomic_write_json, atomic_write_text, kb_ingest_lock
from tests.online_model import load_online_model


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): digest(path) for path in root.rglob("*") if path.is_file()}


def serial(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    raise TypeError(type(value).__name__)


class Audit:
    """Observe the real compiler transport; do not replace any business function."""

    def __init__(self, output, credential):
        self.output, self.credential = output, credential
        self.calls = Counter()
        self.pending = {}
        self.requests = []
        self.responses = []
        self.phase = "step2"
        self.started = time.monotonic()

    def write(self, name, value):
        text = json.dumps(value, ensure_ascii=False, indent=2, default=serial) + "\n"
        if self.credential:
            text = text.replace(self.credential, "<REDACTED>")
        atomic_write_text(self.output / name, text)

    def observe(self, frame, event, result):
        filename, function = frame.f_code.co_filename, frame.f_code.co_name
        if "/openkb/" not in filename:
            return
        if event == "call" and function in {
            "parse_document",
            "prepare_navigation",
            "generate_document_page",
            "publish_proposal",
        }:
            self.calls[self.phase + ":" + function] += 1
        if not filename.endswith("/agent/compiler.py") or function != "_llm_call":
            return
        local = frame.f_locals
        if event == "call":
            messages = local["messages"]
            payload = json.loads(messages[-1]["content"])
            row = {
                "phase": self.phase,
                "stage": local["step_name"],
                "messages": list(messages),
                "inverse": dict(getattr(messages, "inverse", {})),
                "evidence_sha256": hashlib.sha256(
                    json.dumps(payload.get("evidence"), ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest(),
                "options": {
                    k: v
                    for k, v in local["kwargs"].items()
                    if k
                    in {
                        "max_tokens",
                        "temperature",
                        "thinking",
                        "reasoning_effort",
                        "response_format",
                    }
                },
            }
            self.requests.append(row)
            number = len(self.requests)
            self.pending[id(frame)] = (number, time.monotonic(), self.phase)
            self.write(f"request-{number:02d}.json", row)
            print(
                json.dumps({"request": number, "phase": self.phase, "stage": row["stage"]}),
                flush=True,
            )
        elif event == "return" and id(frame) in self.pending:
            number, started, phase = self.pending.pop(id(frame))
            response = local.get("response")
            if response is None:
                self.write(
                    f"response-{number:02d}.json",
                    {"request": number, "phase": phase, "failed": True},
                )
                return
            row = {
                "request": number,
                "phase": phase,
                "elapsed_seconds": time.monotonic() - started,
                "finish_reason": response.choices[0].finish_reason,
                "usage": getattr(response, "usage", None),
                "provider_content": response.choices[0].message.content,
                "decoded_content": str(result),
            }
            self.responses.append(row)
            self.write(f"response-{number:02d}.json", row)


def run(args):
    from openkb.agent.document_page_resolution import prepare_page
    from openkb.agent.document_plan import to_dict
    from openkb.agent.evidence_checkpoints import publication_settings
    from openkb.agent.evidence_compiler import compile_evidence
    from openkb.application.execution import ExecutionContext
    from openkb.compilation_report import collect_compile_report
    from openkb.evidence import ParseStore
    from openkb.inputs import prepared_input
    from openkb.knowledge_commit import KnowledgeWorkspace
    from openkb.navigation import prepare_navigation
    from openkb.pageindex_store import indexed_reader
    from openkb.parsing import parse_document
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
    previous = args.previous_run.resolve() if args.previous_run else None
    prior = manifest(previous) if previous else None
    kb = output / "kb"
    audit = Audit(audit_dir, profile.bundle.api_key)
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
    sys.setprofile(audit.observe)
    try:
        with (
            kb_ingest_lock(kb / ".openkb"),
            ExecutionContext().begin(kb),
            collect_compile_report() as report,
        ):
            if args.resume or previous:
                artifact_root = output if args.resume else previous
                source = SourceStore(kb).version(
                    json.loads((artifact_root / "step1-source.json").read_text())["id"]
                )
                parsed = ParseStore(kb).load(
                    json.loads((artifact_root / "step2-parsed.json").read_text())["id"]
                )
                navigation = json.loads((artifact_root / "step2-navigation.json").read_text())
            else:
                with prepared_input(args.source.resolve()) as ready:
                    source = SourceStore(kb).intake(ready)
                with processing_scope(settings):
                    parsed = parse_document(kb, source, options=settings.get("parsing"))
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
            audit.write(
                "step2-summary.json",
                {
                    "reused": bool(previous or args.resume),
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
                    reader = indexed_reader(kb, source, parsed, navigation)
                    limits = RequestLimits.from_config(settings)
                    for page in result.plan.pages:
                        prepared.append(
                            prepare_page(
                                page,
                                source,
                                parsed,
                                navigation,
                                reader,
                                max_chars=max(1000, limits.input_capacity * 2),
                            )
                        )
                    audit.write("prepared-evidence.json", prepared)
                    audit.write(
                        "prepare-budget.json",
                        {
                            "input_capacity": limits.input_capacity,
                            "max_chars": max(1000, limits.input_capacity * 2),
                        },
                    )
            audit.write("compile-report.json", report)
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        sys.setprofile(None)
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
    inputs.add_argument("--source", type=Path)
    parser.add_argument("--config-kb", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    run(args)


if __name__ == "__main__":
    main()
