"""Fresh SDK workers reproduce the catalog captured before adapter setup."""

import json
import os
import subprocess
import sys
from textwrap import dedent

import pytest


@pytest.mark.parametrize("adapter", ["cli", "api"])
@pytest.mark.parametrize("initially_forced", [False, True])
def test_adapter_setup_keeps_captured_table_and_worker_environment_consistent(
    kb_dir, tmp_path, adapter, initially_forced
):
    # Each process initializes the real pinned SDK table exactly once. There is
    # no model response or table metadata replacement in this regression.
    program = dedent("""\
        import importlib
        import json
        import os
        import sys
        from pathlib import Path

        from litellm.litellm_core_utils.get_model_cost_map import get_model_cost_map_source_info

        table = get_model_cost_map_source_info()
        flag = "LITELLM_LOCAL_MODEL_COST_MAP"
        if not table["is_env_forced"]:
            os.environ.pop(flag, None)
        before_adapter = os.environ.get(flag)
        if sys.argv[2] == "cli":
            importlib.import_module("ultrakb.cli")
        else:
            from ultrakb.api import create_app
            create_app()
        assert os.environ.get(flag) == before_adapter

        from ultrakb import config
        from ultrakb.config_state import capture_config
        from ultrakb.locks import kb_ingest_lock

        config.GLOBAL_CONFIG_DIR = Path(sys.argv[3])
        config.GLOBAL_CONFIG_PATH = config.GLOBAL_CONFIG_DIR / "config.yaml"
        root = Path(sys.argv[1])
        before = dict(os.environ)
        with kb_ingest_lock(root / ".ultrakb"):
            values = capture_config(root).values()
        assert dict(os.environ) == before
        assert values["model_table"] == table
        assert values["environment"][flag] == ("true" if table["is_env_forced"] else "false")
        print(json.dumps({"source": table["source"], "is_env_forced": table["is_env_forced"]}))
        """)
    environment = dict(os.environ)
    environment["LITELLM_LOCAL_MODEL_COST_MAP"] = "true" if initially_forced else "false"
    result = subprocess.run(
        [sys.executable, "-c", program, str(kb_dir), adapter, str(tmp_path / "captured-global")],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"source": "local", "is_env_forced": initially_forced}
