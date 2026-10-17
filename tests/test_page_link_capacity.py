"""Page generation uses bounded hints while resolving the frozen full catalogue."""

import json

import pytest
from litellm import ServiceUnavailableError


@pytest.fixture(autouse=True)
def model_send():
    """Run the real SDK, request capacity and task budget against a local protocol server."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace

    from ultrakb.config import LlmCredentialBundle
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings
    from ultrakb.model_tokenizer import TokenMeasure

    requests, errors = [], []
    service = SimpleNamespace(respond=None, requests=requests)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            try:
                assert self.path == "/v1/chat/completions"
                assert service.respond is not None, "Unexpected model send"
                result = service.respond(payload["messages"])
                raw, finish = result if isinstance(result, tuple) else (result, "stop")
                body = {
                    "id": f"protocol-{len(requests)}",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "gpt-4o-mini",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": raw},
                            "finish_reason": finish,
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                }
                status = 200
            except RuntimeError as error:
                status, body = 503, {"error": {"message": str(error), "type": "server_error"}}
            except Exception as error:
                errors.append(error)
                status, body = 500, {"error": {"message": str(error), "type": "server_error"}}
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    executor = CompletionExecutor(
        RoleBindings(
            "openai/gpt-4o-mini",
            LlmCredentialBundle(
                api_key="local-protocol-test-key",
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
            ),
        ),
        ModelCallPolicy(max_calls=256, retries=0),
    )
    executor.compile_capacity = TokenMeasure(
        {"input_limit": 1000000, "unknown_reason": None, "measurement_unit": "utf8_byte"}
    )
    service.executor = executor
    try:
        with executor.activate():
            yield service
        assert not errors, errors
        assert executor.calls == len(requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_short_original_and_large_link_catalogue_generate_and_resolve_omitted_target(
    kb_dir, model_send
):
    from test_create_capacity import creation_inputs

    from ultrakb.agent.compiler import _KNOWN_TARGETS_USER, _format_known_targets
    from ultrakb.agent.create_generation import prepare_page_creation
    from ultrakb.agent.source_windows import CompileSourceWindows

    options, _, _ = creation_inputs(kb_dir, limit=14000)
    source = "This short original records one supported rule."
    assert len(source) == 47
    (kb_dir / "wiki/sources/manual.md").write_text(source)
    reader = CompileSourceWindows(kb_dir / "wiki", "manual", kb_dir=kb_dir)
    options["windows"] = reader
    options["originals"] = reader.prepare(
        {"name": "rules", "reading_locations": {"chars": f"0:{len(source)}"}}, long=True
    )
    targets = {f"concepts/item-{i:05d}" for i in range(35000)}
    options["known_targets_message"] = {
        "role": "user",
        "content": _KNOWN_TARGETS_USER.format(known_targets=_format_known_targets(targets)),
    }
    requests = []

    def deliver(messages):
        requests.append(messages)
        assert options["fits"](messages)
        assert "concepts/item-34999" not in json.dumps(messages)
        return json.dumps(
            {
                "description": "Supported rule.",
                "content": "See [[concepts/item-34999|the omitted valid page]].",
            }
        )

    model_send.respond = deliver
    result = await prepare_page_creation(**options).generate()
    assert result["content"] == "See [[concepts/item-34999|the omitted valid page]]."
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_page_repair_gets_bounded_link_ambiguity_and_preserves_the_complete_diagnostic(
    kb_dir, model_send
):
    from test_create_capacity import creation_inputs

    from ultrakb.agent.create_generation import prepare_page_creation
    from ultrakb.agent.link_registry import FrozenLinkRegistry

    options, _, checkpoints = creation_inputs(kb_dir, limit=50000)
    wiki = kb_dir / "wiki"
    (wiki / "entities").mkdir(exist_ok=True)
    (wiki / "concepts/cache.md").write_text("---\naliases: [Cache]\n---\n# Storage cache\n")
    (wiki / "entities/cache.md").write_text("---\naliases: [Cache]\n---\n# Cache product\n")
    options["link_registry"] = FrozenLinkRegistry.capture(
        wiki, {"concepts/cache", "entities/cache"}
    )
    sent = []

    def deliver(messages):
        sent.append(messages)
        if len(sent) == 1:
            return json.dumps({"description": "Cache.", "content": "Use [[Cache]]."})
        assert "page_link_ambiguous" in messages[-1]["content"]
        assert "concepts/cache" in messages[-1]["content"]
        assert "entities/cache" in messages[-1]["content"]
        group = json.loads(messages[-1]["content"].partition("\n")[2])
        return json.dumps({"resolutions": [{"id": group["id"], "path": "concepts/cache"}]})

    model_send.respond = deliver
    result = await prepare_page_creation(**options).generate()
    assert result["content"] == "Use [[concepts/cache]]."
    saved = json.loads(checkpoints.path.read_text())
    failures = [
        item for entry in saved["entries"].values() for item in entry.get("response_failures", [])
    ]
    assert len(failures) == 1
    diagnostic = json.loads((kb_dir / failures[0]).read_text())
    assert len(diagnostic["field_errors"]) == 1
    [problem] = diagnostic["field_errors"]
    assert problem["name"] == "Cache"
    assert problem["targets"] == ["concepts/cache", "entities/cache"]


@pytest.mark.asyncio
async def test_link_decisions_cover_every_candidate_and_resume_only_missing_groups(
    kb_dir, model_send
):
    from test_create_capacity import creation_inputs

    from ultrakb.agent.create_generation import prepare_page_creation
    from ultrakb.agent.link_registry import FrozenLinkRegistry, LinkTarget

    options, _, _ = creation_inputs(kb_dir, limit=22000)
    from ultrakb.agent.source_windows import CompileSourceWindows

    source = "This short original records one supported rule."
    (kb_dir / "wiki/sources/manual.md").write_text(source)
    reader = CompileSourceWindows(kb_dir / "wiki", "manual", kb_dir=kb_dir)
    options["windows"] = reader
    options["originals"] = reader.prepare(
        {"name": "rules", "reading_locations": {"chars": f"0:{len(source)}"}}, long=True
    )
    records = [
        LinkTarget(f"concepts/target-{i:03d}", ("Shared", f"Identity {i}: " + "context " * 100))
        for i in range(300)
    ]
    options["link_registry"] = FrozenLinkRegistry(records)
    generation_count = 0
    groups = []
    interrupt = True

    def deliver(messages):
        nonlocal generation_count
        assert options["fits"](messages)
        if "Resolve one wiki link identity" not in messages[0]["content"]:
            generation_count += 1
            return json.dumps({"description": "Supported.", "content": "See [[Shared]]."})
        data = json.loads(messages[-1]["content"].partition("\n")[2])
        if interrupt and len(groups) == 3:
            raise RuntimeError("Interrupted after three accepted identity groups")
        paths = [candidate["path"] for candidate in data["candidates"]]
        groups.append(paths)
        selected = "concepts/target-299" if "concepts/target-299" in paths else None
        return json.dumps({"resolutions": [{"id": data["id"], "path": selected}]})

    model_send.respond = deliver
    with pytest.raises(ServiceUnavailableError, match="Interrupted"):
        await prepare_page_creation(**options).generate()
    accepted_groups = list(groups)
    interrupt = False
    result = await prepare_page_creation(**options).generate()
    assert result["content"] == "See [[concepts/target-299]]."
    assert generation_count == 1
    assert len(groups) > 3
    assert groups[:3] == accepted_groups
    flattened = [path for group in groups for path in group]
    assert len(flattened) == len(set(flattened)) == 300
    assert set(flattened) == {target.path for target in records}


@pytest.mark.asyncio
async def test_update_preserves_a_full_catalogue_alias_that_was_not_in_writing_candidates(
    kb_dir, model_send
):
    from publication_inputs import update_inputs

    from ultrakb.agent.link_registry import FrozenLinkRegistry, LinkTarget
    from ultrakb.agent.update_generation import generate_page_update

    page, reader, originals, patch, _, _ = update_inputs(
        kb_dir, "# Rules\n\nOld rule.\n", "See the remote valid page."
    )
    targets = {f"concepts/target-{i:05d}" for i in range(35000)}
    patch.known_targets = targets
    registry = FrozenLinkRegistry(
        LinkTarget(path, ("Remote name",) if path.endswith("34999") else ()) for path in targets
    )

    def deliver(messages):
        assert len(json.dumps(messages).encode()) < 20000
        assert "concepts/target-34999" not in json.dumps(messages)
        return json.dumps(
            {
                "status": "updated",
                "base_sha256": patch.digest,
                "edits": [
                    {
                        "block_id": "b2",
                        "operation": "replace",
                        "content": "See [[Remote name|the remote page]].\n",
                    }
                ],
            }
        )

    model_send.respond = deliver
    result = await generate_page_update(
        patch=patch,
        originals=originals,
        windows=reader,
        page_path=page,
        model="unused",
        bundle=None,
        system_message={"role": "system", "content": "Compile source facts."},
        current_original=None,
        known_targets_message={"role": "user", "content": "Old catalogue message."},
        link_registry=registry,
        task_message={"role": "user", "content": "Update the supported rule."},
        fits=lambda messages: len(json.dumps(messages).encode()) < 20000,
    )
    assert "[[concepts/target-34999|the remote page]]" in result["content"]
    assert page.read_text() == "# Rules\n\nOld rule.\n"


def test_split_markdown_table_requests_retain_original_headers_and_every_row(kb_dir):
    from test_create_capacity import creation_inputs

    from ultrakb.agent.create_generation import prepare_page_creation
    from ultrakb.agent.source_windows import CompileSourceWindows

    options, _, _ = creation_inputs(kb_dir, limit=10000)
    header = "| Mode | Limit |\n| --- | --- |\n"
    source = header + "".join(f"| mode-{i:03d} | {i} operations |\n" for i in range(400))
    (kb_dir / "wiki/sources/manual.md").write_text(source)
    reader = CompileSourceWindows(kb_dir / "wiki", "manual", kb_dir=kb_dir)
    options["windows"] = reader
    options["originals"] = reader.prepare(
        {"name": "rules", "reading_locations": {"chars": f"0:{len(source)}"}}, long=True
    )
    prepared = prepare_page_creation(**options)
    assert len(prepared.batches) > 1
    assert (
        "".join(window["content"] for batch in prepared.batches for window in batch.windows)
        == source
    )
    for batch in prepared.batches:
        assert options["fits"](batch.messages)
        for window in batch.windows:
            if window["selection_offset"] >= len(header):
                contexts = window.get("display_context", [])
                assert any(context.get("text") == header for context in contexts)
            assert window["content"].endswith("\n")


@pytest.mark.asyncio
async def test_long_protocol_diagnostic_is_retained_locally_and_repair_request_stays_bounded(
    kb_dir, model_send
):
    from test_create_capacity import creation_inputs

    from ultrakb.agent.compiler import _llm_call_page_async

    _, _, checkpoints = creation_inputs(kb_dir)
    bad_field = "duplicated-" + "field" * 20000
    raw = '{"content":"Supported.","' + bad_field + '":1,"' + bad_field + '":2}'
    requests = []

    def deliver(messages):
        requests.append(messages)
        assert len(json.dumps(messages).encode()) < 5000
        assert "page_duplicate_field" in messages[-1]["content"]
        return '{"description":"Supported.","content":"Supported."}'

    model_send.respond = deliver
    result = await _llm_call_page_async(
        "unused",
        [{"role": "system", "content": "Return the supported page as JSON."}],
        "protocol-repair",
        initial_response=raw,
        checkpoints=checkpoints,
        checkpoint_key="protocol-repair",
        response_format={"type": "json_object"},
    )
    assert json.loads(result)["content"] == "Supported."
    assert len(requests) == 1
    saved = json.loads(checkpoints.path.read_text())["entries"]["protocol-repair"]
    [failure] = saved["response_failures"]
    diagnostic = json.loads((kb_dir / failure).read_text())
    assert bad_field in diagnostic["error_detail"]
    assert (kb_dir / diagnostic["response_file"]).read_text() == raw


@pytest.mark.asyncio
async def test_truncated_creation_splits_only_failed_contribution_and_restores_accepted_child(
    kb_dir, model_send
):
    from test_create_capacity import creation_inputs

    from ultrakb.agent.create_generation import prepare_page_creation

    options, source, checkpoints = creation_inputs(kb_dir, limit=1000000)
    requests, completed = [], []
    interrupt = True

    def deliver(messages):
        request = json.dumps(messages, sort_keys=True)
        requests.append(request)
        if '"required_covered_ranges"' not in request.replace('\\"', '"'):
            return '{"content":"cut', "length"
        if completed and interrupt:
            raise RuntimeError("Interrupted after one accepted child")
        required = next(
            message["content"].partition('"required_covered_ranges": ')[2].partition("]")[0] + "]"
            for message in messages
            if isinstance(message["content"], str)
            and '"required_covered_ranges"' in message["content"]
        )
        completed.append(request)
        return json.dumps(
            {
                "description": "Rules.",
                "content": f"Retained contribution {len(completed)}.",
                "covered_ranges": json.loads(required),
            }
        )

    model_send.respond = deliver
    with pytest.raises(ServiceUnavailableError, match="Interrupted"):
        await prepare_page_creation(**options).generate()
    accepted_request = completed[0]
    interrupt = False
    resumed = prepare_page_creation(**options)
    assert resumed.missing == len(resumed.batches) - 1
    result = await resumed.generate()
    assert requests.count(accepted_request) == 1
    assert sum("required_covered_ranges" not in request for request in requests) == 2
    assert (
        "".join(window["content"] for batch in resumed.batches for window in batch.windows)
        == source
    )
    assert "Retained contribution 1." in result["content"]
    assert "Retained contribution 2." in result["content"]
    saved = json.loads(checkpoints.path.read_text())
    assert any(key.startswith("create-split:") for key in saved["entries"])
    failures = [
        item for entry in saved["entries"].values() for item in entry.get("response_failures", [])
    ]
    assert failures
    assert any(
        (kb_dir / json.loads((kb_dir / path).read_text())["response_file"]).read_text()
        == '{"content":"cut'
        for path in failures
    )


def test_short_page_capacity_fallback_covers_full_current_original_without_repeating_it(kb_dir):
    from test_create_capacity import creation_inputs

    from ultrakb.agent.create_generation import prepare_page_creation
    from ultrakb.agent.source_windows import CompileSourceWindows

    options, source, _ = creation_inputs(kb_dir, limit=10000)
    reader = CompileSourceWindows(kb_dir / "wiki", "manual", kb_dir=kb_dir)
    current = reader.current_original_message()
    options["windows"] = reader
    options["originals"] = reader.prepare({"name": "rules"}, long=False)
    options["current_original"] = current
    prepared = prepare_page_creation(**options)
    assert len(prepared.batches) > 1
    assert (
        "".join(window["content"] for batch in prepared.batches for window in batch.windows)
        == source
    )
    assert all(current not in batch.messages for batch in prepared.batches)
    assert all(options["fits"](batch.messages) for batch in prepared.batches)


@pytest.mark.asyncio
async def test_truncated_indivisible_code_retains_exact_source_and_rejects_without_resending(
    kb_dir, model_send
):
    from test_create_capacity import creation_inputs

    from ultrakb.agent.create_generation import prepare_page_creation
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.agent.source_windows import CompileSourceWindows

    options, _, _ = creation_inputs(kb_dir, limit=1000000)
    source = "```sh\necho exact-condition\n```\n"
    (kb_dir / "wiki/sources/manual.md").write_text(source)
    reader = CompileSourceWindows(kb_dir / "wiki", "manual", kb_dir=kb_dir)
    options["windows"] = reader
    options["originals"] = reader.prepare(
        {"name": "rules", "reading_locations": {"chars": f"0:{len(source)}"}}, long=True
    )
    sends = []

    def truncated(messages):
        sends.append(messages)
        return '{"content":"cut', "length"

    model_send.respond = truncated
    with pytest.raises(
        PageResponseError, match="page_create_indivisible_output.*sources/manual.md"
    ):
        await prepare_page_creation(**options).generate()
    assert len(sends) == 2
    with pytest.raises(
        PageResponseError, match="page_create_indivisible_output.*sources/manual.md"
    ):
        prepare_page_creation(**options)
    assert len(sends) == 2
    assert (kb_dir / "wiki/sources/manual.md").read_text() == source


@pytest.mark.asyncio
async def test_large_figure_identity_catalogue_stays_local_while_selected_range_is_validated(
    kb_dir, model_send
):
    from ultrakb.agent.compiler import _llm_call_page_async
    from ultrakb.agent.page_figures import PageFigures

    identities = [f"figure-{number:05d}" for number in range(35000)]
    figures = PageFigures(
        kb_dir / "wiki",
        (
            {
                "id": "range-1",
                "source_path": "sources/manual.md",
                "location": {"start": 0, "end": 47},
                "label": "Original images",
                "figures": identities,
            },
        ),
        (),
    )

    def deliver(messages):
        assert len(json.dumps(messages).encode()) < 5000
        assert any('"figure_count": 35000' in message["content"] for message in messages)
        assert "figure-34999" not in json.dumps(messages)
        return json.dumps(
            {
                "description": "Images.",
                "content": "Original images retained.",
                "source_ranges": ["range-1"],
            }
        )

    model_send.respond = deliver
    result = await _llm_call_page_async(
        "unused",
        [{"role": "system", "content": "Return the page as JSON."}],
        "figure-catalogue",
        figure_context=figures,
        create_delivery=(),
        response_format={"type": "json_object"},
    )
    assert json.loads(result)["source_ranges"] == ["range-1"]
    assert figures.ranges[0]["figures"] == identities


@pytest.mark.asyncio
@pytest.mark.parametrize("matched", [True, False])
async def test_unknown_link_name_scans_every_frozen_catalogue_partition_before_resolving(
    kb_dir, model_send, matched
):
    from test_create_capacity import creation_inputs

    from ultrakb.agent.create_generation import prepare_page_creation
    from ultrakb.agent.link_registry import FrozenLinkRegistry, LinkTarget
    from ultrakb.agent.source_windows import CompileSourceWindows

    options, _, _ = creation_inputs(kb_dir, limit=14000)
    source = "This short original records one supported rule."
    (kb_dir / "wiki/sources/manual.md").write_text(source)
    reader = CompileSourceWindows(kb_dir / "wiki", "manual", kb_dir=kb_dir)
    options["windows"] = reader
    options["originals"] = reader.prepare(
        {"name": "rules", "reading_locations": {"chars": f"0:{len(source)}"}}, long=True
    )
    records = [LinkTarget(f"concepts/target-{number:03d}") for number in range(400)]
    options["link_registry"] = FrozenLinkRegistry(records)
    visited = []

    def deliver(messages):
        assert options["fits"](messages)
        if "Resolve one wiki link identity" not in messages[0]["content"]:
            assert "concepts/target-399" not in json.dumps(messages)
            return json.dumps(
                {
                    "description": "Supported.",
                    "content": "See [[an unregistered synonym|the topic]].",
                }
            )
        decision = json.loads(messages[-1]["content"].partition("\n")[2])
        paths = [candidate["path"] for candidate in decision["candidates"]]
        visited.extend(paths)
        selected = matched and "concepts/target-399" in paths
        return json.dumps(
            {
                "resolutions": [
                    {
                        "id": decision["id"],
                        "status": "matched" if selected else "no_match",
                        "paths": ["concepts/target-399"] if selected else [],
                    }
                ]
            }
        )

    model_send.respond = deliver
    result = await prepare_page_creation(**options).generate()
    expected = "See [[concepts/target-399|the topic]]." if matched else "See the topic."
    assert result["content"] == expected
    assert len(visited) == len(set(visited)) == 400
    assert set(visited) == {target.path for target in records}


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt_after_correction", [False, True])
async def test_invalid_link_decision_gets_one_bounded_repair_and_retains_raw_response(
    kb_dir, monkeypatch, model_send, interrupt_after_correction
):
    from test_create_capacity import creation_inputs

    from ultrakb.agent.create_generation import prepare_page_creation
    from ultrakb.agent.link_registry import FrozenLinkRegistry, LinkTarget

    options, _, checkpoints = creation_inputs(kb_dir, limit=1000000)
    options["link_registry"] = FrozenLinkRegistry([LinkTarget("concepts/target")])
    requests = []

    def deliver(messages):
        requests.append(messages)
        if "Resolve one wiki link identity" not in messages[0]["content"]:
            return '{"description":"Supported.","content":"See [[an unregistered synonym]]."}'
        decision = json.loads(messages[1]["content"].partition("\n")[2])
        raw = (
            '{"resolutions":[]}'
            if len(requests) == 2
            else json.dumps(
                {
                    "resolutions": [
                        {"id": decision["id"], "status": "matched", "paths": ["concepts/target"]}
                    ]
                }
            )
        )
        return raw

    model_send.respond = deliver
    if interrupt_after_correction:
        from ultrakb import locks

        write = locks.atomic_write_text
        interrupted = False

        def interrupt_after_receipt(path, content, **kwargs):
            nonlocal interrupted
            write(path, content, **kwargs)
            if path != checkpoints.path or interrupted:
                return
            entries = json.loads(content)["entries"]
            for key, entry in entries.items():
                if ":links:" not in key or not entry.get("latest_response"):
                    continue
                receipt = json.loads((kb_dir / entry["latest_response"]).read_text())
                if receipt["error_code"] == "response_received" and receipt["attempt"] == 2:
                    interrupted = True
                    raise RuntimeError("Interrupted after receiving the corrected group")

        monkeypatch.setattr(locks, "atomic_write_text", interrupt_after_receipt)
        with pytest.raises(RuntimeError, match="Interrupted after receiving"):
            await prepare_page_creation(**options).generate()
    result = await prepare_page_creation(**options).generate()
    assert result["content"] == "See [[concepts/target]]."
    assert len(requests) == 3
    await prepare_page_creation(**options).generate()
    assert len(requests) == 3
    entries = json.loads(checkpoints.path.read_text())["entries"]
    failures = [
        item
        for key, entry in entries.items()
        if ":links:" in key
        for item in entry.get("response_failures", [])
    ]
    assert len(failures) == 1
    record = json.loads((kb_dir / failures[0]).read_text())
    assert (kb_dir / record["response_file"]).read_text() == '{"resolutions":[]}'


@pytest.mark.parametrize(
    "metadata", ["aliases: {unexpected: value}", "aliases: 42", "aliases: [ok, 42]"]
)
def test_link_catalogue_rejects_invalid_alias_metadata_without_changing_page(kb_dir, metadata):
    from ultrakb.agent.link_registry import FrozenLinkRegistry
    from ultrakb.locks import atomic_write_text

    path = kb_dir / "wiki/concepts/bad-aliases.md"
    content = f"---\n{metadata}\n---\n# Identity\n\nOriginal page.\n"
    atomic_write_text(path, content)
    with pytest.raises(ValueError, match="aliases"):
        FrozenLinkRegistry.capture(kb_dir / "wiki", {"concepts/bad-aliases"})
    assert path.read_text() == content
