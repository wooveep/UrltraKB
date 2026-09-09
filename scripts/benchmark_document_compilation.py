"""Bounded synthetic compilation/resumption probe; never a semantic model evaluation.

The deterministic model adapter extracts only manifest facts. This measures full
structural coverage, request bounds, original rereading and checkpoint reuse.
No network, credentials, OCR inference or model fees are used.
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from benchmark_document_parsing import main


def run_case(plan):
    def offline(event, args):
        # Windows asyncio creates its wakeup socket through a loopback connect.
        # No external endpoint is needed by this deterministic model adapter.
        if (
            event == "socket.connect"
            and isinstance(args[1], tuple)
            and args[1][0] in {"127.0.0.1", "::1"}
        ):
            return
        if event in {"socket.connect", "socket.getaddrinfo"}:
            raise RuntimeError("Compilation benchmark forbids network access")

    sys.addaudithook(offline)
    import os

    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    import litellm

    from openkb import config
    from openkb.application.documents import import_document
    from openkb.application.execution import ExecutionContext
    from openkb.application.knowledge_bases import initialize_kb
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.application.source_actions import continue_source
    from openkb.application.source_history import source_status
    from openkb.cancellation import OperationCancelled
    from openkb.evidence import ParseStore
    from openkb.implementation import module_revision
    from openkb.sources import SourceStore
    from openkb.state import HashRegistry

    root = Path(plan["output"])
    config.GLOBAL_CONFIG_DIR = root / "global"
    config.GLOBAL_CONFIG_PATH = root / "global/global.yaml"
    kb = root / "kb"
    initialize_kb(
        kb,
        seed_environment=False,
        model="openai/offline-benchmark",
        api_key="synthetic-offline",
        openai_api_base="http://127.0.0.1:9/v1",
    )
    budgets = plan["processing"]
    apply_kb_config_patch(kb, KbConfigPatchRequest(kb=str(kb), config={"processing": budgets}))
    original = Path(plan["corpus"]) / plan["document"]["file"]
    assert HashRegistry.hash_file(original) == plan["document"]["sha256"]
    expressions = [
        re.compile(r"\s+".join(re.escape(word) for word in fact["text"].split()))
        for fact in plan["document"]["facts"]
    ]
    units, reread, requests, events = [], set(), [], []

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        stage = payload["stage"]
        if stage == "facts":
            output = {"units": []}
            for unit in payload["units"]:
                units.append(unit["reference"])
                found = [
                    match.group()
                    for expression in expressions
                    if (match := expression.search(unit["text"]))
                ]
                output["units"].append(
                    {
                        "id": unit["id"],
                        "facts": [
                            {"topic": "Operation", "statement": text, "quote": text}
                            for text in found
                        ],
                        "empty_reason": "Synthetic repetition without a new ground-truth fact",
                    }
                )
        elif stage == "planning":
            output = {
                "topics": [
                    {
                        "name": "operation",
                        "title": "Operation",
                        "kind": "concept",
                        "members": payload["topics"],
                    }
                ]
            }
        else:
            assert stage == "generation"
            text = "\n".join(item["text"] for item in payload["evidence"])
            found = []
            for index, expression in enumerate(expressions):
                if match := expression.search(text):
                    reread.add(index)
                    found.append(match.group())
            output = {
                "content": "# Operation\n" + "\n".join(found),
                "covered": [fact["id"] for fact in payload["facts"]],
            }
        raw = json.dumps(output)
        prompt = litellm.token_counter(model=kwargs["model"], messages=kwargs["messages"])
        generated = litellm.token_counter(model=kwargs["model"], text=raw)
        assert prompt + kwargs["max_tokens"] <= budgets["context_tokens"]
        requests.append(
            {
                "stage": stage,
                "input_tokens": prompt,
                "output_tokens": generated,
                "output_reserve": kwargs["max_tokens"],
            }
        )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=raw),
                    finish_reason="stop" if generated <= kwargs["max_tokens"] else "length",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=generated),
        )

    litellm.completion = completion
    started = time.monotonic()

    def observe(event):
        events.append({**event, "elapsed_seconds": time.monotonic() - started})
        if event.get("stage") == "planning":
            raise OperationCancelled()

    stopped = import_document(kb, original, context=ExecutionContext(on_event=observe))
    before_resume = len(units)
    result = (
        continue_source(
            kb,
            stopped.source_id,
            version_id=stopped.input_version,
            context=ExecutionContext(on_event=events.append),
        )
        if stopped.status == "stopped"
        else stopped
    )
    assert len(units) == before_resume, "Continuation repeated completed fact requests"
    parsed = ParseStore(kb).load(result.parse_id) if result.parse_id else None
    covered = {}
    for unit in units:
        covered.setdefault(unit["block_id"], []).append((unit["start"], unit["end"]))
    complete_blocks = 0
    if parsed:
        for block in parsed.blocks:
            cursor = 0
            for start, end in sorted(covered.get(block.id, [])):
                assert start == cursor
                cursor = end
            complete_blocks += cursor == block.chars
    page = kb / "wiki/concepts/operation.md"
    knowledge = page.read_text(encoding="utf-8") if page.exists() else ""
    report = {
        "mode": "controlled_adapter_not_semantic_evaluation",
        "document": asdict(result),
        "stopped_before_plan": asdict(stopped),
        "requests": requests,
        "events": events,
        "complete_blocks": complete_blocks,
        "total_blocks": len(parsed.blocks) if parsed else 0,
        "fact_coverage": sum(bool(expression.search(knowledge)) for expression in expressions),
        "original_reread_fact_coverage": len(reread),
        "fact_total": len(expressions),
        "facts_reused": len(units) == before_resume,
        "network": "python_audit_denied_except_loopback",
        "fee": 0,
        "compiler_profile": module_revision("openkb.agent.evidence_compiler"),
        "elapsed_seconds": time.monotonic() - started,
        "history": source_status(kb, result.source_id) if result.source_id else None,
        "original_retained": HashRegistry.hash_file(
            SourceStore(kb).original(SourceStore(kb).version(result.input_version))
        )
        == plan["document"]["sha256"]
        if result.input_version
        else False,
    }
    (root / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    raise SystemExit(main(run_case))
