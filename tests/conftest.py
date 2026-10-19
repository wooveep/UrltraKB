import pytest

pytest_plugins = (
    "live_llm",
    "model_service_fixtures",
    "test_layers",
    "compile_work_fixtures",
    "index_stage_fixtures",
    "pytester",
)


@pytest.fixture(autouse=True)
def _isolate_offline_application_config(request, tmp_path, monkeypatch):
    """Keep test discovery, credentials and subprocesses out of real user settings."""
    if request.node.get_closest_marker("live") is not None:
        return
    from ultrakb import config

    private = tmp_path / "application-config"
    monkeypatch.setenv("ULTRAKB_CONFIG_DIR", str(private))
    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", private)
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", private / "global.yaml")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_LOCK_PATH", private / "global.lock")


@pytest.fixture(autouse=True)
def _reset_extra_headers():
    """Keep the process-wide LLM extra-headers / timeout / parallel-tool-calls
    stashes from leaking across tests."""
    from ultrakb.config import set_extra_headers, set_parallel_tool_calls, set_timeout

    yield
    set_extra_headers({})
    set_timeout(None)
    set_parallel_tool_calls(None, False)


@pytest.fixture
def kb_dir(tmp_path):
    """Create a minimal knowledge base directory structure for testing."""
    # Raw documents
    (tmp_path / "raw").mkdir()

    # Wiki sub-directories
    (tmp_path / "wiki" / "sources" / "images").mkdir(parents=True)
    (tmp_path / "wiki" / "summaries").mkdir(parents=True)
    (tmp_path / "wiki" / "concepts").mkdir(parents=True)
    (tmp_path / "wiki" / "explorations").mkdir(parents=True)
    (tmp_path / "wiki" / "reports").mkdir(parents=True)

    from ultrakb.config import save_config
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore

    identity = create_database(tmp_path)
    save_config(
        tmp_path / ".ultrakb/config.yaml",
        {
            "version": "0.1.0",
            "model": "gpt-4o-mini",
            "model_capacity": {"max_input_tokens": 65536, "tokenizer_model": "gpt-4o-mini"},
        },
    )
    store = KnowledgeStore(tmp_path, identity)
    store.publish(store.prepare_revision({}, expected=None))

    return tmp_path


@pytest.fixture
def sample_tree():
    """Return a sample PageIndex tree structure dict for testing."""
    return {
        "doc_name": "Sample Document",
        "doc_description": "A sample document used for unit testing.",
        "structure": [
            {
                "title": "Introduction",
                "node_id": "node-1",
                "start_index": 0,
                "end_index": 120,
                "summary": "Overview of the document topic.",
                "text": "This document introduces the core concepts of the system.",
                "nodes": [
                    {
                        "title": "Background",
                        "node_id": "node-1-1",
                        "start_index": 0,
                        "end_index": 60,
                        "summary": "Historical context.",
                        "text": "Background information on the subject.",
                        "nodes": [],
                    },
                    {
                        "title": "Motivation",
                        "node_id": "node-1-2",
                        "start_index": 61,
                        "end_index": 120,
                        "summary": "Why this work matters.",
                        "text": "Explanation of the motivation behind this work.",
                        "nodes": [],
                    },
                ],
            },
            {
                "title": "Conclusion",
                "node_id": "node-2",
                "start_index": 121,
                "end_index": 200,
                "summary": "Summary of findings.",
                "text": "The system performs well under the described conditions.",
                "nodes": [],
            },
        ],
    }
