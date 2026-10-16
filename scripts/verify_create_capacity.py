"""Replay retained creation inputs on scratch copies, with network and real models denied.

The generated responses are synthetic protocol fixtures, never knowledge results.
Usage: python scripts/verify_create_capacity.py --kb PATH --model-task ID --output PATH
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import socket
from pathlib import Path
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb", type=Path, required=True)
    parser.add_argument("--model-task", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    kb, output = args.kb.resolve(), args.output.resolve()
    if output.is_relative_to(kb):
        raise ValueError("Offline evidence must be outside the actual knowledge base")
    output.mkdir(parents=True, exist_ok=True)
    logging.disable(logging.INFO)
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "true"
    denied = []

    def deny(*a, **kw):
        denied.append("network_or_real_model")
        raise AssertionError("Offline creation replay denies network and real models")

    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = deny
    import litellm

    litellm.completion = litellm.acompletion = deny
    from ultrakb.agent import compiler
    from ultrakb.agent.compile_capacity import CompileCapacity
    from ultrakb.agent.create_delivery import create_repairs
    from ultrakb.agent.create_generation import prepare_page_creation
    from ultrakb.agent.document_summary import (
        SYSTEM_TEMPLATE,
        build_document_summary,
        summary_capacity,
    )
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.compile_checkpoints import CompileCheckpoints
    from ultrakb.config import LlmCredentialBundle, resolve_effective_config
    from ultrakb.execution_capacity import request_input
    from ultrakb.lint import list_existing_wiki_targets
    from ultrakb.llm_execution import (
        CompletionExecutor,
        ModelCallPolicy,
        ModelRequest,
        RoleBindings,
    )
    from ultrakb.model_tokenizer import TokenMeasure
    from ultrakb.schema import get_agents_md

    compiler._llm_call = deny
    config = resolve_effective_config(kb)[0]
    model_task = json.loads((kb / ".ultrakb/model-tasks" / f"{args.model_task}.json").read_text())
    manifest = next((kb / ".ultrakb/compile-checkpoints" / args.model_task).glob("*/manifest.json"))
    record = json.loads(manifest.read_text())
    revision = record["conditions"]["revisions"][0]
    unit = revision["unit"]
    index = (
        kb
        / ".ultrakb/index-checkpoints"
        / unit["source_id"]
        / revision["revision"]["unit_revision_id"]
    )
    protected = [
        kb / ".ultrakb/config.yaml",
        kb / ".ultrakb/model-tasks" / f"{args.model_task}.json",
    ]
    protected += [p for d in (manifest.parent, index) for p in d.rglob("*") if p.is_file()]
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    scratch = output / "scratch"
    if scratch.exists():
        raise ValueError("Use a fresh output directory for each replay")
    shutil.copytree(index / "data", scratch)
    shutil.copytree(kb / ".ultrakb/catalog", scratch / ".ultrakb/catalog")
    for name in ("AGENTS.md", "index.md"):
        shutil.copyfile(kb / "wiki" / name, scratch / "wiki" / name)
    directory = scratch / ".ultrakb/compile-checkpoints" / args.model_task / manifest.parent.name
    shutil.copytree(manifest.parent, directory)
    task = SimpleNamespace(task_id=args.model_task, parent_task_id=None)
    checkpoints = CompileCheckpoints(scratch, task, record["identity"], record["conditions"])
    tree = json.loads((index / "checkpoint.json").read_text())["tree"]
    capacity = json.loads(revision["revision"]["processing_fingerprint"])["capacity"]
    measure = TokenMeasure(capacity)
    executor = CompletionExecutor(
        RoleBindings(
            config["model"],
            LlmCredentialBundle(base_url="https://api.deepseek.com"),
        ),
        ModelCallPolicy(max_calls=model_task["policy"]["max_calls"]),
    )
    executor.compile_capacity = measure
    executor.budget.restore(model_task["used"], model_task["stages"])
    fits = CompileCapacity(executor, measure)
    reader = CompileSourceWindows(
        scratch / "wiki", unit["doc_name"], navigation=tree["structure"], kb_dir=scratch
    )
    plan = json.loads(checkpoints.load("plan", record["entries"]["plan"]["request_digest"]))
    known = list_existing_wiki_targets(reader.wiki) | {
        f"{kind}/{p['name']}" for kind in ("concepts", "entities") for p in plan[kind]["create"]
    }
    known_message = {
        "role": "user",
        "content": compiler._cached_text(
            compiler._KNOWN_TARGETS_USER.format(known_targets=compiler._format_known_targets(known))
        ),
    }
    system = {
        "role": "system",
        "content": compiler._SYSTEM_TEMPLATE.format(
            schema_md=get_agents_md(reader.wiki), language=config.get("language", "en")
        ),
    }
    calls, reports = [], []

    async def synthetic(model, messages, stage, **kwargs):
        assert stage.startswith(("concept: ", "entity: ")), stage
        request = ModelRequest(
            compiler._prepare_messages(model, messages),
            operation="compile",
            stage=stage,
            generation_options={"response_format": {"type": "json_object"}},
        )
        sdk = executor._kwargs("compile", request)
        assert request_input(sdk) == fits.payload(messages)
        calls.append({"stage": stage, "units": measure.payload_tokens(request_input(sdk))})
        coverage = []
        ranges = []
        for message in messages:
            content = message["content"]
            if not isinstance(content, str):
                continue
            if match := re.search(r'"required_covered_ranges": (\[[^\]]*\])', content):
                coverage = json.loads(match[1])
            if content.startswith("Figure source ranges:\n"):
                ranges = [r["id"] for r in json.loads(content.split("\n", 1)[1])["ranges"]]
        value = {
            "description": "Synthetic offline protocol fixture.",
            "type": "other",
            "content": "Synthetic protocol fixture; not a generated knowledge result.",
        }
        if coverage:
            value["covered_ranges"] = coverage
        if ranges:
            value["source_ranges"] = ranges
        return json.dumps(value)

    compiler._llm_call_async = synthetic

    async def replay():
        with executor.activate():
            summary = await build_document_summary(
                reader,
                system_message={
                    "role": "system",
                    "content": SYSTEM_TEMPLATE.format(language=config.get("language", "en")),
                },
                generate=deny,
                fits=summary_capacity(
                    config,
                    custom_endpoint=True,
                    legacy_format=True,
                    base_url="https://api.deepseek.com",
                ),
                checkpoints=checkpoints,
            )
            assert len(summary.ranges) == 5
            creations = []
            for kind in ("concepts", "entities"):
                for item in plan[kind]["create"]:
                    originals = reader.prepare(item, long=True)
                    template = (
                        compiler._CONCEPT_PAGE_USER
                        if kind == "concepts"
                        else compiler._ENTITY_PAGE_USER
                    )
                    options = dict(
                        windows=reader,
                        originals=originals,
                        model=config["model"],
                        bundle=None,
                        system_message=system,
                        current_original=None,
                        known_targets_message=known_message,
                        task_message={
                            "role": "user",
                            "content": template.format(
                                title=item.get("title", item["name"]),
                                doc_name=reader.doc_name,
                                type=item.get("type", "other"),
                                update_instruction="",
                            ).replace("__ENTITY_TYPES__", ", ".join(compiler._ENTITY_TYPE_LIST)),
                        },
                        page=f"{kind}/{item['name']}.md",
                        checkpoints=checkpoints,
                    )
                    prepared = prepare_page_creation(**options)
                    repairs = [
                        measure.payload_tokens(fits.payload([*b.messages, c]))
                        for b in prepared.batches
                        for c in create_repairs(b.coverage)
                    ]
                    assert max(repairs) <= measure.input_limit
                    assert "".join(
                        w["content"] for b in prepared.batches for w in b.windows
                    ) == "".join(w["content"] for w in originals.windows)
                    reports.append(
                        {
                            "page": options["page"],
                            "batches": len(prepared.batches),
                            "request_units": [
                                measure.payload_tokens(fits.payload(b.messages))
                                for b in prepared.batches
                            ],
                            "max_repair_units": max(repairs),
                            "coverage_exact": True,
                        }
                    )
                    creations.append((options, prepared))
            # Interrupt the largest page after its first accepted part, then rebuild the plan.
            options, largest = max(creations, key=lambda c: len(c[1].batches))
            assert len(largest.batches) > 1
            normal = compiler._llm_call_async
            count = 0

            async def interrupted(*a, **kw):
                nonlocal count
                count += 1
                if count == 2:
                    raise RuntimeError("Synthetic interruption")
                return await normal(*a, **kw)

            compiler._llm_call_async = interrupted
            try:
                await largest.generate()
            except RuntimeError as error:
                assert str(error) == "Synthetic interruption"
            else:
                raise AssertionError("Interruption scenario was not reached")
            compiler._llm_call_async = normal
            resumed = prepare_page_creation(**options)
            assert resumed.missing == len(resumed.batches) - 1
            before_calls = len(calls)
            await resumed.generate()
            assert len(calls) - before_calls == len(resumed.batches) - 1
            for opt, prepared in creations:
                if opt is options:
                    continue
                await prepared.generate()
            assert all(prepare_page_creation(**opt).missing == 0 for opt, _ in creations)
            assert (
                executor.calls == model_task["used"]
            )  # Synthetic responses made no transport sends.
        return {
            "summary_cache_hits": 5,
            "accepted_plan_reused": True,
            "pages": reports,
            "interrupted_page": options["page"],
            "resume_verified": True,
        }

    result = asyncio.run(replay())
    changed = [
        p for p, h in before.items() if hashlib.sha256(Path(p).read_bytes()).hexdigest() != h
    ]
    assert changed == [] and denied == []
    result.update(
        protected_files=len(before),
        actual_files_changed=changed,
        network_attempts=denied,
        real_model_calls=0,
        actual_import_runs=0,
        synthetic_responses=len(calls),
        budget_used=model_task["used"],
        budget_limit=model_task["policy"]["max_calls"],
        local_input_limit=measure.input_limit,
        unit=measure.unit,
    )
    (output / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
