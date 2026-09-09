"""Long document compilation through the shared document operation."""

import json

import litellm
import yaml

from openkb.application.documents import import_document


def test_compilation_thinking_mode_reaches_provider_and_invalidates_cached_facts(
    kb_dir, tmp_path, model_service
):
    from openkb.application.settings import apply_kb_config_patch, read_kb_config
    from openkb.application.settings_data import KbConfigPatchRequest

    original = tmp_path / "thinking.md"
    original.write_text("Required version 7.", encoding="utf-8")
    previous_calls = 0
    versions = []
    for mode in ("disabled", "enabled"):
        apply_kb_config_patch(
            kb_dir,
            KbConfigPatchRequest(kb=str(kb_dir), config={"compilation_thinking": mode}),
        )
        assert read_kb_config(kb_dir).compilation_thinking == mode
        result = import_document(kb_dir, original)
        assert result.knowledge_compilation == "completed", result
        versions.append((result.input_version, result.parse_id))
        new_calls = model_service[previous_calls:]
        assert {json.loads(call["messages"][-1]["content"])["stage"] for call in new_calls} == {
            "facts",
            "planning",
            "generation",
        }
        assert all(call.get("thinking") == {"type": mode} for call in new_calls)
        previous_calls = len(model_service)
    assert versions[0] == versions[1]


def test_all_sections_generate_from_original_evidence_with_bounded_requests(
    kb_dir, tmp_path, model_service
):
    facts = [
        "Required version: 7.",
        "Only start when pressure is below 37 kPa.",
        "Command: start --timeout 42. Never retry authentication failures.",
    ]
    original = tmp_path / "manual.md"
    original.write_text(
        "\n\n".join(
            f"# Section {i}\n\n" + ("Background explanation. " * 800) + "\n\n" + fact
            for i, fact in enumerate(facts, 1)
        )
    )
    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["processing"].update(
        context_tokens=4096,
        output_tokens=1024,
        max_requests=80,
        max_tokens=200000,
        stage_timeout=30,
        document_timeout=60,
    )
    config_path.write_text(yaml.safe_dump(config))
    observed = {"facts": set(), "generation": set()}

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "facts":
            units = []
            for unit in payload["units"]:
                matches = [fact for fact in facts if fact in unit["text"]]
                observed["facts"].update(matches)
                units.append(
                    {
                        "id": unit["id"],
                        "facts": [
                            {"topic": "Operation", "statement": fact, "quote": fact}
                            for fact in matches
                        ],
                        "empty_reason": "Background without operational facts"
                        if not matches
                        else "",
                    }
                )
            return {"units": units}
        if payload["stage"] == "planning":
            return {
                "topics": [
                    {
                        "name": "operation",
                        "title": "Operation",
                        "kind": "concept",
                        "members": payload["topics"],
                    }
                ]
            }
        assert payload["stage"] == "generation"
        excerpts = "\n".join(item["text"] for item in payload["evidence"])
        selected = [fact for fact in facts if fact in excerpts]
        observed["generation"].update(selected)
        return {
            "content": "# Operation\n" + "\n".join(selected),
            "covered": [fact["id"] for fact in payload["facts"]],
        }

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed", result
    assert observed == {"facts": set(facts), "generation": set(facts)}
    pages = list((kb_dir / "wiki/concepts").glob("*.md"))
    assert len(pages) == 1
    content = pages[0].read_text()
    assert all(fact in content for fact in facts)
    assert "source-evidence" in content and result.parse_id in content
    for request in model_service:
        measured = litellm.token_counter(model=request["model"], messages=request["messages"])
        assert measured + request["max_tokens"] <= 4096


def test_changed_generation_language_reuses_facts_but_does_not_skip_completed_version(
    kb_dir, tmp_path, model_service
):
    original = tmp_path / "language.md"
    original.write_text("Required version 7.")
    first = import_document(kb_dir, original)
    assert first.knowledge_compilation == "completed"
    calls_before = len(model_service)
    path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(path.read_text())
    config["language"] = "zh"
    path.write_text(yaml.safe_dump(config))
    changed = import_document(kb_dir, original)
    assert changed.status == "added", changed
    stages = [
        json.loads(call["messages"][-1]["content"])["stage"]
        for call in model_service[calls_before:]
    ]
    assert "generation" in stages
    assert "facts" not in stages


def test_generation_reads_conditions_omitted_from_the_fact_statement(
    kb_dir, tmp_path, model_service
):
    text = "Pressure: 37 kPa. Only on version 7; never retry authentication failures."
    original = tmp_path / "conditions.md"
    original.write_text(text)

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "facts":
            return {
                "units": [
                    {
                        "id": unit["id"],
                        "facts": [
                            {
                                "topic": "Pressure",
                                "statement": "Pressure is 37 kPa",
                                "quote": "37 kPa",
                            }
                        ],
                        "empty_reason": "",
                    }
                    for unit in payload["units"]
                ]
            }
        if payload["stage"] == "planning":
            return {
                "topics": [
                    {
                        "name": "pressure",
                        "title": "Pressure",
                        "kind": "concept",
                        "members": payload["topics"],
                    }
                ]
            }
        return {
            "content": "\n".join(item["text"] for item in payload["evidence"]),
            "covered": [item["id"] for item in payload["facts"]],
        }

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed"
    assert text in (kb_dir / "wiki/concepts/pressure.md").read_text()


def test_continuation_keeps_facts_but_reads_current_wiki_before_generation(
    kb_dir, tmp_path, model_service
):
    from openkb.application.execution import ExecutionContext
    from openkb.application.source_actions import continue_source
    from openkb.application.source_history import source_status
    from openkb.cancellation import OperationCancelled

    original = tmp_path / "manual.md"
    original.write_text("Required version: 7.")
    current_page = kb_dir / "wiki/concepts/operation.md"
    current_page.write_text("# Operation\nInitial manual guidance.\n")
    fact_requests = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "facts":
            fact_requests.append(payload)
            return {
                "units": [
                    {
                        "id": unit["id"],
                        "facts": [
                            {
                                "topic": "Operation",
                                "statement": "Required version: 7.",
                                "quote": "Required version: 7.",
                            }
                        ],
                        "empty_reason": "",
                    }
                    for unit in payload["units"]
                ]
            }
        if payload["stage"] == "planning":
            return {
                "topics": [
                    {
                        "name": "operation",
                        "title": "Operation",
                        "kind": "concept",
                        "members": payload["topics"],
                    }
                ]
            }
        return {
            "content": payload.get("existing", "") + "\nRequired version: 7.",
            "covered": [fact["id"] for fact in payload["facts"]],
        }

    model_service.respond = respond

    def stop(event):
        if event.get("stage") == "planning":
            raise OperationCancelled()

    first = import_document(kb_dir, original, context=ExecutionContext(on_event=stop))
    assert first.status == "stopped" and len(fact_requests) == 1
    original.unlink()
    current_page.write_text("# Operation\nEmergency override requires approval.\n")

    def stop_generated(event):
        if event.get("stage") == "generated":
            raise OperationCancelled()

    generated = continue_source(
        kb_dir,
        first.source_id,
        version_id=first.input_version,
        context=ExecutionContext(on_event=stop_generated),
    )
    assert generated.status == "stopped"
    assert current_page.read_text() == "# Operation\nEmergency override requires approval.\n"
    continued = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert continued.reason == "needs_acceptance", continued
    assert len(fact_requests) == 1
    from openkb.application.source_actions import review_source_proposal

    review = review_source_proposal(kb_dir, continued.resume)
    assert "Emergency override requires approval." in review["diffs"]["concepts/operation.md"]
    finished = continue_source(
        kb_dir,
        first.source_id,
        version_id=first.input_version,
        proposal_id=continued.resume,
        accept_pages=review["protected"],
    )
    assert finished.knowledge_compilation == "completed"
    assert "Emergency override requires approval." in current_page.read_text()
    usage = source_status(kb_dir, first.source_id)["cumulative_usage"]
    assert usage["observable_attempts"] == 3


def test_one_large_topic_is_generated_in_bounded_parts_without_partial_publication(
    kb_dir, tmp_path, model_service
):
    facts = [
        f"Setting {number}: value {number + 37}; " + "required before startup " * 12
        for number in range(20)
    ]
    original = tmp_path / "settings.md"
    original.write_text("\n\n".join(facts))
    path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(path.read_text())
    config["processing"].update(context_tokens=4096, max_requests=80, max_tokens=200000)
    path.write_text(yaml.safe_dump(config))
    parts = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "facts":
            return {
                "units": [
                    {
                        "id": unit["id"],
                        "facts": [
                            {"topic": "Settings", "statement": unit["text"], "quote": unit["text"]}
                        ],
                        "empty_reason": "",
                    }
                    for unit in payload["units"]
                ]
            }
        if payload["stage"] == "planning":
            return {
                "topics": [
                    {
                        "name": "settings",
                        "title": "Settings",
                        "kind": "concept",
                        "members": payload["topics"],
                    }
                ]
            }
        parts.append(payload)
        assert not (kb_dir / "wiki/concepts/settings.md").exists()
        return {
            "content": "\n\n".join(item["text"] for item in payload["evidence"]),
            "covered": [item["id"] for item in payload["facts"]],
        }

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed", result
    assert len(parts) > 1
    content = (kb_dir / "wiki/concepts/settings.md").read_text()
    assert all(fact in content for fact in facts)


def test_long_unbroken_block_is_automatically_split_again_for_generation(
    kb_dir, tmp_path, model_service
):
    from tests.http_model_fixture import evidence_response

    original = tmp_path / "long-command.md"
    text = "start " + "--argument=37 " * 1400 + " --timeout=42"
    original.write_text(text)
    path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(path.read_text())
    config["processing"].update(context_tokens=4096, max_requests=150, max_tokens=500000)
    path.write_text(yaml.safe_dump(config))
    scopes = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "generation":
            scopes.extend(item["reference"] for item in payload["evidence"])
        return evidence_response(payload)

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed", result
    covered = set()
    for scope in scopes:
        covered.update(range(scope["start"], scope["end"]))
    assert covered == set(range(len(text)))
    for request in model_service:
        measured = litellm.token_counter(model=request["model"], messages=request["messages"])
        assert measured + request["max_tokens"] <= 4096


def test_named_entity_and_concept_share_valid_links_and_preserve_entity_vocabulary(
    kb_dir, tmp_path, model_service
):
    original = tmp_path / "entities.md"
    original.write_text("AtlasDB supports atomic commits.")

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "facts":
            return {
                "units": [
                    {
                        "id": unit["id"],
                        "facts": [
                            {
                                "topic": "AtlasDB",
                                "statement": "AtlasDB supports atomic commits",
                                "quote": "AtlasDB",
                            },
                            {
                                "topic": "Atomic commits",
                                "statement": "AtlasDB supports atomic commits",
                                "quote": "atomic commits",
                            },
                        ],
                        "empty_reason": "",
                    }
                    for unit in payload["units"]
                ]
            }
        if payload["stage"] == "planning":
            assert "product" in payload["entity_types"]
            return {
                "topics": [
                    {
                        "name": "atlasdb",
                        "title": "AtlasDB",
                        "kind": "entity",
                        "type": "product",
                        "members": ["AtlasDB"],
                    },
                    {
                        "name": "atomic-commits",
                        "title": "Atomic commits",
                        "kind": "concept",
                        "members": ["Atomic commits"],
                    },
                ]
            }
        return {
            "content": "# "
            + payload["title"]
            + "\n[[entities/atlasdb]] supports [[concepts/atomic-commits]]. [[concepts/phantom]]",
            "covered": [fact["id"] for fact in payload["facts"]],
        }

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed", result
    entity = (kb_dir / "wiki/entities/atlasdb.md").read_text()
    concept = (kb_dir / "wiki/concepts/atomic-commits.md").read_text()
    assert yaml.safe_load(entity.split("---")[1])["type"] == "Product"
    assert "[[concepts/atomic-commits]]" in entity and "[[entities/atlasdb]]" in concept
    assert "[[concepts/phantom]]" not in entity + concept
    summary = next((kb_dir / "wiki/summaries").glob("*.md")).read_text()
    assert "entities/atlasdb" in summary


def test_large_topic_plan_is_bounded_and_merges_one_topic_across_planning_parts(
    kb_dir, tmp_path, model_service
):
    from tests.http_model_fixture import evidence_response

    original = tmp_path / "topic-plan.md"
    original.write_text(
        "\n\n".join(f"Setting {i}: value 37, prerequisite version 7." for i in range(120))
    )
    path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(path.read_text())
    config["processing"].update(
        context_tokens=4096,
        max_requests=400,
        max_tokens=1500000,
        stage_timeout=40,
        document_timeout=90,
    )
    path.write_text(yaml.safe_dump(config))
    planned = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        response = evidence_response(payload)
        if payload["stage"] == "facts":
            for output, unit in zip(response["units"], payload["units"]):
                output["facts"][0]["topic"] = (
                    unit["text"] + " Detailed operating parameter and prerequisite"
                )
        if payload["stage"] == "planning":
            planned.extend(payload["topics"])
        return response

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed", result
    assert len(planned) == len(set(planned)) == 120
    assert len(list((kb_dir / "wiki/concepts").glob("*.md"))) == 1
    assert (
        sum(
            json.loads(call["messages"][-1]["content"])["stage"] == "planning"
            for call in model_service
        )
        > 1
    )
    for request in model_service:
        measured = litellm.token_counter(model=request["model"], messages=request["messages"])
        assert measured + request["max_tokens"] <= 4096


def test_new_source_version_retracts_its_retired_topic_without_deleting_other_sources(
    kb_dir, tmp_path, model_service
):
    from tests.http_model_fixture import evidence_response

    source = tmp_path / "features.md"
    source.write_text("Retired feature.")

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        response = evidence_response(payload)
        if payload["stage"] == "facts":
            for output, unit in zip(response["units"], payload["units"]):
                output["facts"][0]["topic"] = "retired" if "Retired" in unit["text"] else "current"
        if payload["stage"] == "planning":
            response["topics"][0].update(name=payload["topics"][0], title=payload["topics"][0])
        return response

    model_service.respond = respond
    first = import_document(kb_dir, source)
    other = tmp_path / "other-source.md"
    other.write_text("Retired feature remains available in the legacy edition.")
    second = import_document(kb_dir, other)
    assert second.knowledge_compilation == "completed"
    source.write_text("Current feature.")
    changed = import_document(kb_dir, source)
    assert changed.knowledge_compilation == "completed", changed
    retained = (kb_dir / "wiki/concepts/retired.md").read_text()
    assert first.source_id not in retained
    assert second.source_id in retained
    assert (kb_dir / "wiki/concepts/current.md").exists()


def test_nested_headings_and_adjacent_conditions_reach_generation(kb_dir, tmp_path, model_service):
    from tests.http_model_fixture import evidence_response

    source = tmp_path / "nested.md"
    source.write_text(
        "# Operations\n\n## Linux\n\nOnly on version 7.\n\nEnable cache with --timeout 42."
    )
    generated = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        response = evidence_response(payload)
        if payload["stage"] == "facts":
            for unit, output in zip(payload["units"], response["units"]):
                if "Enable cache" in unit["text"]:
                    assert unit["headings"] == ["Operations", "Linux"]
                    output["facts"][0]["topic"] = "Cache"
                else:
                    output.update(facts=[], empty_reason="Context for the cache instruction")
        if payload["stage"] == "generation":
            generated.append(payload)
        return response

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert "Only on version 7." in json.dumps(generated)


def test_generated_figure_link_points_to_the_retained_immutable_asset(
    kb_dir, tmp_path, model_service
):
    import re

    from PIL import Image

    from tests.http_model_fixture import evidence_response

    original = tmp_path / "figure.md"
    original.write_text("![Valve](valve.png)\n\nValve rated 37 kPa.")
    Image.new("RGB", (80, 80), "blue").save(tmp_path / "valve.png")

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        response = evidence_response(payload)
        if payload["stage"] == "generation":
            response["content"] = "\n\n".join(item["text"] for item in payload["evidence"])
        return response

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed", result
    page = kb_dir / "wiki/concepts/notes.md"
    links = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", page.read_text())
    assert links
    assert all((page.parent / link).is_file() for link in links)
    assert all(
        (page.parent / link).read_bytes() == (tmp_path / "valve.png").read_bytes() for link in links
    )
