"""Identical in-flight batches keep independent source occurrences and bounded retries."""

import json
import re
import threading
import time

import litellm
import pytest
import yaml

from openkb.application.documents import import_document
from tests.http_model_fixture import evidence_response
from tests.test_adaptive_processing import response


@pytest.mark.parametrize("fail_first", [False, True])
def test_repeated_batches_share_one_executor_and_recover_its_failure(
    kb_dir, tmp_path, monkeypatch, fail_first
):
    path = kb_dir / ".openkb/config.yaml"
    settings = yaml.safe_load(path.read_text())
    settings["processing"].update(context_tokens=4096, output_tokens=1024, concurrency=8)
    path.write_text(yaml.safe_dump(settings))
    source = tmp_path / "repeated.md"
    source.write_text("\n\n".join(["Pressure must remain below 37 kPa."] * 120))
    guard = threading.Lock()
    active, calls, peaks = {}, [], {}
    failed = False

    def completion(**options):
        nonlocal failed
        payload = json.loads(options["messages"][-1]["content"])
        value = evidence_response(payload)
        if payload["stage"] != "facts":
            return response(value)
        # Compare visible source text and context, independent of local wire IDs.
        signature = json.dumps(
            [
                [unit["text"], [(row["relation"], row["text"]) for row in unit["neighbors"]]]
                for unit in payload["units"]
            ]
        )
        with guard:
            active[signature] = active.get(signature, 0) + 1
            peaks[signature] = max(peaks.get(signature, 0), active[signature])
            calls.append(signature)
            reject = fail_first and not failed
            failed |= reject
        try:
            time.sleep(0.1)
            return response({"invalid": "executor failed validation"} if reject else value)
        finally:
            with guard:
                active[signature] -= 1

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert peaks and set(peaks.values()) == {1}
    assert failed == fail_first
    cited = {
        json.loads(ref)["block_id"]
        for page in (kb_dir / "wiki/concepts").glob("*.md")
        for ref in re.findall(r"<!-- source-evidence: (.*?) -->", page.read_text())
    }
    assert len(cited) == 120
    assert len(calls) < 10  # Shared middle batches do not each dispatch a duplicate.
    assert any(row["event"] == "hit" for row in result.usage["measurement"]["analyses"])
