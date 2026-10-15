"""A subprocess must reach the same accounted real client as its parent."""

import json
import os
import subprocess
import sys


def test_spawned_sdk_worker_reuses_the_same_real_request_and_budget(live_llm):
    messages = [{"role": "user", "content": "State this fact: Orion has 17 credits."}]
    original = live_llm.request(messages=messages, max_tokens=64)
    admitted = live_llm.budget.report()["requests"]
    code = """
import json, os
from openai import OpenAI
values = json.loads(input())
with OpenAI(api_key=values['key'], base_url=values['base'], max_retries=0) as client:
    result = client.chat.completions.create(model='deepseek-flash', messages=values['messages'],
        max_tokens=64, extra_body={'thinking': {'type':'disabled'}})
print(json.dumps({'content': result.choices[0].message.content, 'model':result.model}))
"""
    child = subprocess.run(
        [sys.executable, "-c", code],
        input=json.dumps(
            {
                "key": live_llm.credentials.api_key,
                "base": live_llm.credentials.base_url,
                "messages": messages,
            }
        ),
        capture_output=True,
        text=True,
        timeout=45,
        env={**os.environ, "OPENAI_AGENTS_DISABLE_TRACING": "1"},
    )
    assert child.returncode == 0, child.stderr
    result = json.loads(child.stdout)
    assert result["content"] == original["choices"][0]["message"]["content"]
    assert result["model"] == "deepseek-flash" and "17" in result["content"]
    assert live_llm.budget.report()["requests"] == admitted
