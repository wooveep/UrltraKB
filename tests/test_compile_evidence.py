"""Long-document pages receive original source facts before generation."""

import json

import pytest

from openkb.agent.compile_evidence import CompileEvidence, LongDocumentEvidence
from openkb.agent.page_response import PageResponseError


def test_frozen_selection_preserves_locator_and_never_reads_another_source(kb_dir):
    path = kb_dir / "wiki/sources/api.json"
    path.write_text(
        json.dumps([{"page": 1, "unit_kind": "page", "content": "/api/login user pwd"}])
    )
    reader = LongDocumentEvidence(kb_dir / "wiki", "api")
    path.write_text(json.dumps([{"page": 1, "unit_kind": "page", "content": "replacement"}]))
    chosen = reader.select({"evidence_units": [1]})
    assert "user pwd" in chosen.text and "replacement" not in chosen.text
    assert "sha256=" in chosen.locator and "physical pages=1" in chosen.locator
    with pytest.raises(PageResponseError, match="page_invalid_evidence"):
        reader.select({"evidence_units": [2]})


@pytest.mark.parametrize("template", ["```json\n%s\n```", "Request body: `%s`"])
def test_structurally_valid_but_invented_field_names_are_rejected(template):
    evidence = CompileEvidence("physical page 4", '/api/login {"user":"...", "pwd":"..."}')
    evidence.validate(template % '{"user":"example", "pwd":"example"}')
    with pytest.raises(PageResponseError, match="page_unsupported_fields"):
        evidence.validate(template % '{"username":"example", "password":"example"}')


def test_update_preserves_unchanged_examples_but_checks_new_examples():
    previous = 'Earlier source:\n```json\n{"old_api_field":"example"}\n```'
    evidence = CompileEvidence("physical page 4", "new_api_field", previous)
    evidence.validate(previous + '\n```json\n{"new_api_field":"example"}\n```')
    with pytest.raises(PageResponseError, match="page_unsupported_fields"):
        evidence.validate(previous + '\n```json\n{"invented_field":"example"}\n```')


@pytest.mark.parametrize(
    "example",
    ["`https://example.invalid:8443/api`", "```text\nhttps://example.invalid:8443/api\n```"],
)
def test_url_example_is_not_a_structured_field(example):
    evidence = CompileEvidence("physical page 4", "地址为 http s://example.invalid:8443/api")
    evidence.validate(example)


@pytest.mark.parametrize(
    "example",
    [
        '```json\n{"https": true}\n```',
        "```yaml\nservice:\n  https: true\n```",
        "```bash\ncurl https://example.invalid -d '{\"https\": true}'\n```",
    ],
)
def test_url_spelling_is_still_checked_when_used_as_a_data_key(example):
    with pytest.raises(PageResponseError, match="https"):
        CompileEvidence("page 4", "service user pwd").validate(example)


def test_curl_checks_only_literal_request_body():
    CompileEvidence("page 4", "user pwd").validate(
        "```bash\ncurl https://example.invalid -H 'Content-Type: application/json' "
        '--data-raw=\'{"user":"admin","pwd":"x"}\'\n```'
    )


def test_malformed_structured_example_has_actionable_diagnostic():
    with pytest.raises(PageResponseError, match="Malformed JSON"):
        CompileEvidence("page 4", "user").validate('```json\n{"user":\n```')


def test_retained_docx_responses_keep_original_urls_without_repair():
    from pathlib import Path

    samples = json.loads((Path(__file__).parent / "fixtures/docx-url-evidence.json").read_text())
    for sample in samples:
        assert "http s://" in sample["evidence"] and "https://" in sample["content"]
        CompileEvidence(sample["locator"], sample["evidence"]).validate(sample["content"])


@pytest.mark.asyncio
async def test_compile_long_page_reads_originals_and_repairs_only_bad_fields(kb_dir, monkeypatch):
    from openkb.agent.compiler import _compile_concepts
    from openkb.compilation_report import collect_compile_report

    wiki = kb_dir / "wiki"
    (wiki / "sources/api.json").write_text(
        json.dumps(
            [
                {
                    "page": 1,
                    "unit_kind": "page",
                    "content": "http s://host/api/login: user and pwd required.",
                }
            ]
        )
    )
    plan = {"concepts": {"create": [{"name": "login", "evidence_units": [1]}]}}
    plan_calls, page_calls = [], []

    def planned(*args, **kwargs):
        plan_calls.append(args)
        return json.dumps(plan)

    async def generate(model, messages, step, **kwargs):
        page_calls.append(messages)
        assert "user and pwd required" in str(messages)
        assert "physical pages=1" in str(messages)
        keys = ("username", "password") if len(page_calls) == 1 else ("user", "pwd")
        return json.dumps(
            {
                "content": "`https://host/api/login`\n```json\n"
                + json.dumps(dict.fromkeys(keys, "example"))
                + "\n```"
            }
        )

    monkeypatch.setattr("openkb.agent.compiler._llm_call", planned)
    monkeypatch.setattr("openkb.agent.compiler._llm_call_async", generate)
    with collect_compile_report() as report:
        await _compile_concepts(
            wiki, kb_dir, "test", {}, {}, "Login summary", "api", 1, doc_type="pageindex"
        )
        report.require_complete()
    result = (wiki / "concepts/login.md").read_text()
    assert '"user"' in result and '"username"' not in result
    assert "https://host/api/login" in result
    assert len(plan_calls) == 1 and len(page_calls) == 2
    assert "page_response_repaired" in report.quality


@pytest.mark.parametrize("units", [[0], [True], [1, 1], list(range(1, 8)), "1-2"])
def test_plan_rejects_invalid_evidence_ranges(units):
    from openkb.agent.compile_plan import normalize_plan

    with pytest.raises(ValueError, match="evidence_units"):
        normalize_plan(
            {"concepts": {"create": [{"name": "login", "evidence_units": units}]}},
            existing={"concepts": set(), "entities": set()},
            entity_types=frozenset({"other"}),
            sanitize=lambda name: name,
        )
