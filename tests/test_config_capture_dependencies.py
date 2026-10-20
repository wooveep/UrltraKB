"""A fresh application process captures the SDK's actual environment once."""

import os
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.integration


def test_sdk_dotenv_is_initialized_before_execution_settings_are_captured(tmp_path):
    from ultrakb.config import save_config
    from ultrakb.storage.database import create_database

    kb_dir = tmp_path / "kb"
    create_database(kb_dir)
    save_config(kb_dir / ".ultrakb/config.yaml", {"model": "openai/gpt-4o-mini"})
    sdk_env = tmp_path / "sdk.env"
    sdk_env.write_text("OPENAI_API_KEY=fixture-sdk-environment\n")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.endswith("_API_KEY") and key != "ULTRAKB_MODEL"
    }
    environment.pop("PYTHON_DOTENV_DISABLED", None)
    environment["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    script = textwrap.dedent("""
        import sys
        from pathlib import Path
        import dotenv.main

        root, sdk_env = map(Path, sys.argv[1:])
        # Use the real SDK dotenv loader with a controlled discovery location.
        dotenv.main.find_dotenv = lambda *args, **kwargs: str(sdk_env)
        from ultrakb import config
        from ultrakb.application.execution_configuration import capture_storage_config
        from ultrakb.storage.database import open_database

        config.GLOBAL_CONFIG_DIR = root / 'isolated-global'
        config.GLOBAL_CONFIG_PATH = config.GLOBAL_CONFIG_DIR / 'config.yaml'
        assert 'pageindex' not in sys.modules
        with open_database(root, read_only=True) as db:
            identity = db.identity
        snapshot = capture_storage_config(root, identity)
        import pageindex
        frozen = snapshot.values()['credentials']['api_key']
        current = config.resolve_credential_bundle(root).api_key
        assert frozen == current == 'fixture-sdk-environment', (
            frozen == 'fixture-sdk-environment', current == 'fixture-sdk-environment',
            snapshot.values()['effective']['model'],
        )
        print('SDK_ENVIRONMENT_CAPTURED')
    """)
    completed = subprocess.run(
        [sys.executable, "-c", script, str(kb_dir), str(sdk_env)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "SDK_ENVIRONMENT_CAPTURED" in completed.stdout
