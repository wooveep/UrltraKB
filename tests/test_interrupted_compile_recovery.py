"""Explicit retry can finish one unobserved repair without repeating paid results."""

import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from test_vendor_sdk import response

pytest_plugins = ("test_vendor_sdk",)


def reply(value):
    return 200, response({"role": "assistant", "content": json.dumps(value)})


@pytest.mark.parametrize("valid_recovery", [True, False])
def test_public_retry_recovers_unobserved_summary_repair_once(
    kb_dir, model_service, valid_recovery
):
    from openkb.application.documents import import_document
    from openkb.application.execution import ExecutionContext
    from openkb.application.repair import repair_knowledge_base
    from openkb.application.source_retry import retry_source
    from openkb.llm_usage import read_requests
    from openkb.model_task import read_task

    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture\nOPENAI_API_BASE={model_service.url}\n")
    (kb_dir / ".openkb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\nlanguage: en\nmodel_policy:\n  retries: 0\n  max_calls: 8\n"
    )
    normal = kb_dir / "normal.txt"
    normal.write_text("Normal operation requires supervision.")
    accepted = {
        "description": "Operating condition",
        "content": "# Operation\nSupervision required.",
    }
    model_service.replies.extend([reply(accepted), reply({})])
    context = ExecutionContext()
    result = import_document(kb_dir, normal, context=context)
    assert result.status == "added"
    retained = (kb_dir / "wiki/summaries/normal.md").read_bytes()
    task_id = context.model_task.record.task_id
    source = kb_dir / "interrupted.txt"
    source.write_text("Interrupted operation requires supervision.")
    requested, release = threading.Event(), threading.Event()

    def unobserved(_request):
        requested.set()
        assert release.wait(30), "The test did not release its interrupted response"
        return response({"role": "assistant", "content": json.dumps(accepted)})

    model_service.replies.extend(
        [reply({"description": "Invalid", "content": ""}), (200, unobserved)]
    )
    script = """
import sys
from pathlib import Path
from openkb.application.documents import import_document
from openkb.application.execution import ExecutionContext
import_document(Path(sys.argv[1]), Path(sys.argv[2]),
                context=ExecutionContext(resume_model_task_id=sys.argv[3]))
"""
    repo = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join(
            [
                str(repo),
                *[
                    str(repo / "vendor" / name)
                    for name in ("PageIndex", "ChatIndex", "ConDB", "LiteLLM")
                ],
            ]
        ),
    }
    with (kb_dir.parent / "interrupted-compile.log").open("w") as stream:
        child = subprocess.Popen(
            [sys.executable, "-B", "-c", script, str(kb_dir), str(source), task_id],
            cwd=repo,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            assert requested.wait(30), "The real repair request was never sent"
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=10)
        finally:
            release.set()
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=10)
    assert child.returncode == -signal.SIGKILL
    before = read_task(kb_dir, task_id)
    assert before.used == 4
    unknown = [item for item in read_requests(kb_dir) if item.state == "started"]
    assert len(unknown) == 1 and unknown[0].observation == "call"
    assert unknown[0].send_status == "sent"
    old_ledger = {p: p.read_bytes() for p in (kb_dir / ".openkb/usage/requests").glob("*.json")}
    assert repair_knowledge_base(kb_dir).repaired
    model_service.replies.extend(
        [reply(accepted), reply({})]
        if valid_recovery
        else [reply({"description": "Still invalid", "content": ""})]
    )
    resumed = retry_source(kb_dir, unknown[0].source_id)
    if not valid_recovery:
        assert resumed.status == "failed", resumed
        assert len(model_service.requests) == read_task(kb_dir, task_id).used == 5
        again = retry_source(kb_dir, unknown[0].source_id)
        assert again.status == "failed", again
        assert len(model_service.requests) == read_task(kb_dir, task_id).used == 5
        assert all(p.read_bytes() == value for p, value in old_ledger.items())
        assert (kb_dir / "wiki/summaries/normal.md").read_bytes() == retained
        assert not (kb_dir / "wiki/summaries/interrupted.md").exists()
        return
    assert resumed.status == "added", resumed
    assert len(model_service.requests) == 6
    assert read_task(kb_dir, task_id).used == 6
    assert len(list((kb_dir / ".openkb/model-tasks").glob("*.json"))) == 1
    assert resumed.model_usage["current"]["actual_sends"] == 2
    assert all(p.read_bytes() == value for p, value in old_ledger.items())
    assert (kb_dir / "wiki/summaries/normal.md").read_bytes() == retained
    assert "Supervision required." in (kb_dir / "wiki/summaries/interrupted.md").read_text()
