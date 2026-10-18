"""New-format execution captures settings without a whole-task writer lease."""


def test_prepared_capture_keeps_dependencies_fixed_while_fresh_settings_can_change(tmp_path):
    from ultrakb.application.execution_configuration import capture_storage_config
    from ultrakb.config import resolve_effective_config, save_config
    from ultrakb.storage.database import create_database

    root = tmp_path / "kb"
    identity = create_database(root)
    save_config(root / ".ultrakb/config.yaml", {"model": "gpt-4o-mini", "language": "en"})
    captured = capture_storage_config(root, identity)
    save_config(root / ".ultrakb/config.yaml", {"model": "gpt-4o-mini", "language": "zh"})
    with captured.activate():
        fresh = capture_storage_config(root, identity)
        assert resolve_effective_config(root)[0]["language"] == "en"
    assert captured.values()["effective"]["language"] == "en"
    assert fresh.values()["effective"]["language"] == "zh"
    assert "tokenizer_asset" in captured.values()
    assert captured.values()["model_table"]


def test_capture_refuses_pending_settings_without_repair_and_explicit_preparation_recovers(
    tmp_path,
):
    import pytest

    from ultrakb.application.execution_configuration import (
        capture_storage_config,
        configuration_lock_path,
        prepare_storage_config,
    )
    from ultrakb.config import save_config
    from ultrakb.locks import file_write_lock
    from ultrakb.mutation import RecoveryRequired, snapshot_paths
    from ultrakb.storage.database import create_database

    root = tmp_path / "kb"
    identity = create_database(root)
    path = root / ".ultrakb/config.yaml"
    save_config(path, {"model": "gpt-4o-mini", "language": "en"})
    with file_write_lock(configuration_lock_path(root)):
        interrupted = snapshot_paths(root, [path, root / ".env"], operation="settings")
        save_config(path, {"model": "gpt-4o-mini", "language": "zh"})
    with pytest.raises(RecoveryRequired, match="explicitly"):
        capture_storage_config(root, identity)
    assert "zh" in path.read_text()
    assert interrupted.journal_path.exists()
    prepared = prepare_storage_config(root, identity)
    assert prepared.values()["effective"]["language"] == "en"
    assert not interrupted.journal_path.exists()


def test_memory_model_wait_allows_settings_admission_and_gc_without_changing_the_recipe(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from test_storage_sessions import TopicResponses

    from ultrakb.application.conversation_memory import ConversationMemory
    from ultrakb.application.execution_configuration import (
        capture_storage_config,
        patch_storage_config,
    )
    from ultrakb.application.settings_data import KbConfigPatchRequest
    from ultrakb.config import resolve_effective_config, save_config
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.database import create_database
    from ultrakb.storage.maintenance import Maintenance
    from ultrakb.storage.session_store import SessionStore

    root = tmp_path / "kb"
    identity = create_database(root)
    save_config(root / ".ultrakb/config.yaml", {"model": "gpt-4o-mini", "language": "en"})
    captured = capture_storage_config(root, identity)
    store = SessionStore(root, identity)
    store.create("one", model="test/model", language="en")
    store.append_exchange("one", "first", "Preference", "DISPLAY_ONLY")
    entered, resume = Event(), Event()

    class Provider(TopicResponses):
        def complete(self, messages, **options):
            assert resolve_effective_config(root)[0]["language"] == "en"
            entered.set()
            assert resume.wait(5)
            assert resolve_effective_config(root)[0]["language"] == "en"
            return super().complete(messages, **options)

    def catch_up():
        with captured.activate():
            return ConversationMemory(
                store, "one", policy_key="policy", model="test/model", llm=Provider()
            ).catch_up()

    with ThreadPoolExecutor(max_workers=1) as worker:
        pending = worker.submit(catch_up)
        try:
            assert entered.wait(5)
            patched = patch_storage_config(
                root, identity, KbConfigPatchRequest(kb=str(root), config={"language": "zh"})
            )
            assert patched.language == "zh"
            external = tmp_path / "source.txt"
            external.write_text("new source during memory analysis")
            admitted = CatalogStore(root, identity).admit(external, source_key="new")
            assert (
                CatalogStore(root, identity).read_original(admitted.revision_id).startswith(b"new")
            )
            assert Maintenance(root, identity).collect().status != "busy"
        finally:
            resume.set()
        assert pending.result(timeout=5).indexed_turns == 1
    assert captured.values()["effective"]["language"] == "en"
    assert capture_storage_config(root, identity).values()["effective"]["language"] == "zh"


def test_new_settings_lock_preserves_the_pair_when_credential_write_fails(tmp_path, monkeypatch):
    import pytest

    from ultrakb.application import settings
    from ultrakb.application.execution_configuration import (
        capture_storage_config,
        patch_storage_config,
    )
    from ultrakb.application.settings_data import KbConfigPatchRequest
    from ultrakb.config import save_config
    from ultrakb.storage.database import create_database

    root = tmp_path / "kb"
    identity = create_database(root)
    path = root / ".ultrakb/config.yaml"
    save_config(path, {"model": "gpt-4o-mini", "language": "en"})
    (root / ".env").write_text("LLM_API_KEY=fixture-original\n")
    before = path.read_bytes(), (root / ".env").read_bytes()

    def failed_credential_write(*args):
        raise OSError("injected credential persistence failure")

    monkeypatch.setattr(settings, "_merge_patch_env", failed_credential_write)
    with pytest.raises(OSError, match="injected"):
        patch_storage_config(
            root,
            identity,
            KbConfigPatchRequest(kb=str(root), config={"language": "zh"}, api_key="fixture-new"),
        )
    assert (path.read_bytes(), (root / ".env").read_bytes()) == before
    assert capture_storage_config(root, identity).values()["effective"]["language"] == "en"


def test_new_settings_gate_refuses_unknown_schema_before_capture_lock_creation(tmp_path):
    import pytest

    from ultrakb.application.execution_configuration import (
        capture_storage_config,
        configuration_lock_path,
    )
    from ultrakb.config import save_config
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.database import StorageFormatError, create_database

    root = tmp_path / "kb"
    identity = create_database(root)
    save_config(root / ".ultrakb/config.yaml", {"model": "gpt-4o-mini"})
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute("UPDATE ukb_meta SET schema_version = schema_version + 1")
    with pytest.raises(StorageFormatError):
        capture_storage_config(root, identity)
    assert not configuration_lock_path(root).exists()


def test_database_identity_is_required_even_inside_an_exclusive_lifecycle(tmp_path):
    import asyncio

    import pytest

    from ultrakb.application.execution_configuration import capture_storage_config
    from ultrakb.config import save_config
    from ultrakb.lifecycle import async_read_lifecycle, exclusive_lifecycle
    from ultrakb.storage.database import DatabaseIdentity, StorageFormatError, create_database

    root = tmp_path / "kb"
    identity = create_database(root)
    wrong = DatabaseIdentity("wrong-kb", identity.generation)
    save_config(root / ".ultrakb/config.yaml", {"model": "gpt-4o-mini"})
    with exclusive_lifecycle(root), pytest.raises(StorageFormatError):
        capture_storage_config(root, wrong)

    async def enter():
        with exclusive_lifecycle(root):
            async with async_read_lifecycle(root, database_identity=wrong):
                pass

    with pytest.raises(StorageFormatError):
        asyncio.run(enter())


def test_explicit_preparation_initializes_sdk_credentials_before_capacity_changes(tmp_path):
    import os
    import subprocess
    import sys
    import textwrap

    sdk_env = tmp_path / "sdk.env"
    sdk_env.write_text("OPENAI_API_BASE=https://private.fixture.invalid/v1\n")
    environment = dict(os.environ)
    for key in ("OPENAI_API_BASE", "OPENAI_BASE_URL", "PYTHON_DOTENV_DISABLED"):
        environment.pop(key, None)
    environment["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    script = textwrap.dedent("""
        import sys
        from pathlib import Path
        import dotenv.main
        root, sdk_env = map(Path, sys.argv[1:])
        dotenv.main.find_dotenv = lambda *args, **kwargs: str(sdk_env)
        from ultrakb import config
        from ultrakb.storage.database import create_database
        from ultrakb.application.execution_configuration import prepare_storage_config
        config.GLOBAL_CONFIG_DIR = root.parent / 'isolated-global'
        config.GLOBAL_CONFIG_PATH = config.GLOBAL_CONFIG_DIR / 'global.yaml'
        assert 'pageindex' not in sys.modules
        identity = create_database(root)
        model = 'deepseek/deepseek-flash'
        config.save_config(root / '.ultrakb/config.yaml', {
            'model': model, 'model_capacity': {
                'max_input_tokens': 65536, 'applies_to_model': model,
                'tokenizer_model': 'local:/missing/utf8-bound-v1-33f2bfa809d90e0d.json',
            },
        })
        snapshot = prepare_storage_config(root, identity)
        values = snapshot.values()
        assert values['credentials']['base_url'] == 'https://private.fixture.invalid/v1'
        assert values['effective']['model_capacity'] == {}
        assert config.load_config(root / '.ultrakb/config.yaml')['model_capacity'] == {}
        print('PREPARATION_USED_CAPTURED_ENDPOINT')
    """)
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "kb"), str(sdk_env)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "PREPARATION_USED_CAPTURED_ENDPOINT" in completed.stdout
