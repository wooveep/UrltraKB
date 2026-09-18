"""Real second-step outputs hand off to compilation without parsing again."""

import json

import litellm
import pytest

from openkb.agent.evidence_units import source_units
from openkb.config import load_config, resolve_credential_bundle
from openkb.locks import kb_ingest_lock
from openkb.navigation import prepare_navigation, read_navigation
from openkb.parsing import parse_document
from openkb.processing import RequestLimits
from tests.test_native_parsing import source_version


def prepare(kb, path, options):
    source = source_version(kb, path)
    parsed = parse_document(kb, source)
    settings = load_config(kb / ".openkb/config.yaml")
    settings["processing"].update(output_tokens=4096, max_tokens=1000000)
    options = dict(options)
    settings["processing"].update(options.pop("execution", {}))
    settings["navigation"] = {"enabled": True, "summaries": False, **options}
    with kb_ingest_lock(kb / ".openkb"):
        saved = prepare_navigation(
            kb, source, parsed, settings, bundle=resolve_credential_bundle(kb)
        )
    return source, parsed, settings, saved


def test_native_headings_enter_windows_and_empty_continuation_hands_off(
    kb_dir, tmp_path, model_service
):
    path = tmp_path / "long.md"
    path.write_text("# Install\n\n" + "\n\n".join("Required version 7. " * 35 for _ in range(5)))
    received = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        received.append((body, payload))
        assert payload["stage"] == "index_structure"
        assert any(
            row["order"] == payload["target"]["start"] for row in payload["evidence"]["blocks"]
        )
        if len(received) > 1:
            assert payload["continuation"]["sections"]
            return {"sections": []}
        first = payload["evidence"]["blocks"][0]
        return {
            "sections": [
                {
                    "title": "Install",
                    "title_origin": "source",
                    "level": 1,
                    "start_block": first["id"],
                    "anchor": "Install",
                }
            ]
        }

    model_service.respond = respond
    source, parsed, settings, saved = prepare(kb_dir, path, {"window_tokens": 550})
    assert len(received) > 1
    assert saved["status"] == "enhanced", saved
    assert len(saved["windows"]) == len(received)
    assert all(row["status"] == "complete" for row in saved["windows"])
    assert all(body["max_tokens"] == 4096 for body, _ in received)
    assert saved["nodes"][-1]["end"] == len(parsed.blocks)

    path.unlink()
    restored = read_navigation(kb_dir, source, identity=saved["id"], limit=200)
    units = list(
        source_units(
            kb_dir,
            source,
            parsed,
            RequestLimits.from_config(settings),
            settings["model"],
            navigation=restored,
        )
    )
    assert any("Required version 7." in unit["text"] for unit in units)
    calls = len(received)
    with kb_ingest_lock(kb_dir / ".openkb"):
        assert (
            prepare_navigation(
                kb_dir, source, parsed, settings, bundle=resolve_credential_bundle(kb_dir)
            )["id"]
            == saved["id"]
        )
    assert len(received) == calls


def test_dense_structure_uses_the_configured_output_and_preserves_every_heading(
    kb_dir, tmp_path, model_service
):
    path = tmp_path / "dense.md"
    path.write_text("\n\n".join(f"# Section {i}\n\nValue {i}." for i in range(90)))
    output_sizes = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        assert body["max_tokens"] == 16000
        value = {
            "sections": [
                {
                    "title": row["text"][2:],
                    "title_origin": "source",
                    "level": 1,
                    "start_block": row["id"],
                    "anchor": row["text"],
                }
                for row in payload["evidence"]["blocks"]
                if row["kind"] == "heading"
            ]
        }
        output_sizes.append(litellm.token_counter(model=body["model"], text=json.dumps(value)))
        return value

    model_service.respond = respond
    _, parsed, _, saved = prepare(kb_dir, path, {"execution": {"output_tokens": 16000}})
    assert saved["status"] == "enhanced", saved
    assert len(saved["nodes"]) == 91
    assert saved["nodes"][-1]["end"] == len(parsed.blocks)
    assert max(output_sizes) > 2048


@pytest.mark.parametrize("limit", [1024, 2048])
def test_explicit_small_output_budgets_reach_transport(kb_dir, tmp_path, model_service, limit):
    path = tmp_path / "small.md"
    path.write_text("Original source material.")
    _, _, _, saved = prepare(kb_dir, path, {"execution": {"output_tokens": limit}})
    assert saved["status"] == "enhanced"
    assert all(body["max_tokens"] == limit for body in model_service)


def test_truncated_empty_response_remains_a_declared_basic_fallback(
    kb_dir, tmp_path, model_service
):
    path = tmp_path / "truncated.md"
    path.write_text("Original source material.")
    model_service.finish_reason = "length"
    _, _, _, saved = prepare(kb_dir, path, {})
    assert saved["status"] == "degraded"
    assert saved["windows"][0]["status"] == "basic"
    assert saved["windows"][0]["reason"] == "output_budget_exhausted"


def test_unlocated_starts_get_one_local_attempt_and_remain_resolved_on_resume(
    kb_dir, tmp_path, model_service
):
    path = tmp_path / "unlocated.md"
    path.write_text("Only original text, without a heading.")
    stages = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        stages.append(payload["stage"])
        if payload["stage"] == "index_structure":
            return {
                "sections": [
                    {
                        "title": "Missing title",
                        "title_origin": "source",
                        "level": 1,
                        "start_block": "invented",
                        "anchor": "Missing title",
                    }
                ]
            }
        return {"locations": [{"id": c["id"], "location": None} for c in payload["candidates"]]}

    model_service.respond = respond
    source, parsed, settings, saved = prepare(kb_dir, path, {})
    assert stages == ["index_structure", "index_location"]
    assert saved["status"] == "degraded" and len(saved["nodes"]) == 1
    with kb_ingest_lock(kb_dir / ".openkb"):
        restored = prepare_navigation(
            kb_dir, source, parsed, settings, bundle=resolve_credential_bundle(kb_dir)
        )
    assert restored["id"] == saved["id"]
    assert stages == ["index_structure", "index_location"]


def test_structure_and_summary_share_final_prefix_without_a_summary_reviewer(
    kb_dir, tmp_path, model_service
):
    path = tmp_path / "summary.md"
    path.write_text("# Install\n\nRequired version 7. " * 20)
    _, _, _, saved = prepare(kb_dir, path, {"summaries": True})
    assert saved["status"] == "enhanced", saved
    requests = [json.loads(body["messages"][-1]["content"]) for body in model_service]
    assert [p["stage"] for p in requests] == ["index_structure", "index_summary"]
    assert model_service[0]["messages"][0] == model_service[1]["messages"][0]
    prefixes = [body["messages"][-1]["content"].split(',"stage":')[0] for body in model_service]
    assert prefixes[0] == prefixes[1]
    assert any(n["summary_origin"] == "model" for n in saved["nodes"])


def test_usable_contents_maps_headings_in_code_without_model_checks(
    kb_dir, tmp_path, model_service
):
    path = tmp_path / "contents.md"
    path.write_text(
        "# Contents\n\nInstall .... 1\nRepair .... 2\n\n"
        "# Install\n\nUse version 7.\n\n# Repair\n\nRestart once.\n"
    )
    _, parsed, _, saved = prepare(kb_dir, path, {})
    assert not model_service
    assert saved["status"] == "enhanced"
    assert [n["title"] for n in saved["nodes"]][1:] == ["Install", "Repair"]
    assert saved["nodes"][-1]["end"] == len(parsed.blocks)


def test_real_200000_token_assembly_keeps_only_bounded_descriptors(kb_dir, tmp_path, model_service):
    path = tmp_path / "large.txt"
    path.write_text("\n\n".join("word " * 3500 for _ in range(59)))
    source, parsed, _, saved = prepare(
        kb_dir,
        path,
        {
            "execution": {
                "context_tokens": 260000,
                "max_tokens": None,
                "request_timeout": 30,
                "stage_timeout": 120,
                "document_timeout": 180,
            }
        },
    )
    assert saved["status"] == "enhanced", saved
    assert len(saved["windows"]) == 2
    assert 190000 < saved["windows"][0]["tokens"] <= 200000
    assert len(json.dumps(saved["windows"])) < 4000
    from openkb.navigation_evidence import read_evidence_group

    group = read_evidence_group(kb_dir, source, parsed, saved["windows"][1]["evidence"])
    assert group["blocks"][-1]["id"] == parsed.blocks[-1].id


def test_compile_and_verify_reuse_the_final_evidence_prefix(kb_dir, tmp_path, model_service):
    from openkb.application.documents import import_document

    path = tmp_path / "facts.md"
    path.write_text("Use version 7.")
    result = import_document(kb_dir, path)
    assert result.knowledge_compilation == "completed"
    messages = {
        json.loads(body["messages"][-1]["content"])["stage"]: body["messages"]
        for body in model_service
    }
    assert messages["generation"][0] == messages["verification"][0]
    generation = json.loads(messages["generation"][-1]["content"])
    verification = json.loads(messages["verification"][-1]["content"])
    assert generation["evidence"] == verification["evidence"]
    assert messages["generation"][-1]["content"].startswith('{"evidence":')
    assert messages["verification"][-1]["content"].startswith('{"evidence":')
