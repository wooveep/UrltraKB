"""Provider replies for tests of adapters, independent of evidence-review internals."""

import json
from types import SimpleNamespace


def review_response(input):
    content = input[-1]["content"]
    packet = json.loads(content[0]["text"] if isinstance(content, list) else content)
    if "answer_units" not in packet:
        return None
    read = next(r for r in packet["reads"] if r["kind"] == "text")
    # These fixtures contain one original rule. Tests of semantic rejection use
    # independent adversarial verdicts instead of this successful provider reply.
    quote = "\n".join(line.partition(": ")[2] for line in read["content"].splitlines())
    word = quote.split()[0]
    report = {
        "units": [
            {
                "unit_id": unit["unit_id"],
                "verdict": "supported",
                "reason": "",
                "proofs": [
                    {
                        "read_id": read["read_id"],
                        "quote": "",
                        "lines": [1, len(read["content"].splitlines())],
                        "subject": word,
                        "setting": word,
                        "condition": "",
                        "visual_region": None,
                    }
                ],
            }
            for unit in packet["answer_units"]
        ]
    }
    return SimpleNamespace(final_output=json.dumps(report), raw_responses=[])


def provider_review(kwargs):
    from litellm import ModelResponse

    if "final evidence reviewer" not in str(kwargs["messages"][:1]):
        return None
    response = review_response([m for m in kwargs["messages"] if m["role"] == "user"])
    return ModelResponse(
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        choices=[
            {
                "message": {"role": "assistant", "content": response.final_output},
                "finish_reason": "stop",
            }
        ],
    )


async def read_fixture(agent, path="sources/fixture.md"):
    from test_answer_evidence_regression import invoke

    return await invoke(agent, "read_file", path=path)
