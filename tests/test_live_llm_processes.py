"""Independent pytest workers share genuine replies and one cost admission."""

import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from live_llm import ROOT


@pytest.mark.parametrize("stream", [False, True])
def test_independent_workers_coalesce_one_real_request(live_llm, stream):
    parameters = {
        "messages": [{"role": "user", "content": 'Return JSON {"shared_budget":true} exactly.'}],
        "response_format": {"type": "json_object"},
        "max_tokens": 64,
        "stream": stream,
    }
    script = """
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'tests'))
from live_llm import LiveLLM
from live_network import install_network_guard
from llm_test_budget import TestRunBudget
install_network_guard()
configuration = json.load(sys.stdin)
client = LiveLLM(TestRunBudget(Path(configuration['ledger'])))
async def request():
    frames = await client.remote_acompletion(**configuration['parameters'])
    text, ids, usage = [], [], None
    async for frame in frames:
        ids.append(frame.id)
        if frame.choices:
            text.append(frame.choices[0].delta.content or '')
        if getattr(frame, 'usage', None):
            usage = frame.usage.model_dump()
    await frames.aclose()
    return {'id':ids[0], 'text':''.join(text), 'usage':usage}
try:
    if configuration['parameters']['stream']:
        result = asyncio.run(request())
    else:
        reply = client.request(**configuration['parameters'])
        result = {'id':reply['id'], 'text':reply['choices'][0]['message']['content'],
                  'usage':reply['usage']}
    print(json.dumps(result))
finally:
    client.close()
"""
    stdin = json.dumps({"ledger": str(live_llm.budget.path.resolve()), "parameters": parameters})

    def worker(_):
        result = subprocess.run(
            [sys.executable, "-c", script],
            input=stdin,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout.strip())

    with ThreadPoolExecutor(max_workers=2) as workers:
        replies = list(workers.map(worker, range(2)))
    assert replies[0] == replies[1]
    assert json.loads(replies[0]["text"]) == {"shared_budget": True}
    assert replies[0]["usage"]["prompt_tokens"] > 0
    payload, _ = live_llm._payload(parameters)
    fingerprint = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    entries = json.loads(live_llm.budget.path.read_text())["entries"]
    matches = [entry for entry in entries if entry["fingerprint"] == fingerprint]
    assert len(matches) == 1 and matches[0]["state"] == "settled"
