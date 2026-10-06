"""Smoke-test installed SDK identities, provider imports and packaged resources."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from importlib import metadata, resources
from pathlib import Path

from local_vendors import VENDORS, verify_installed_vendors


def verify_install(*, wheel: bool = False) -> dict:
    packages = verify_installed_vendors()
    for _, _, module, _, _ in VENDORS:
        importlib.import_module(module)
    import yaml
    from jinja2 import Environment, PackageLoader
    from litellm import get_llm_provider

    import openkb

    paths = {**packages, "openkb": Path(openkb.__file__).resolve().parent}
    if wheel and any(
        not path.is_relative_to(Path(sys.prefix).resolve()) for path in paths.values()
    ):
        raise ValueError("Wheel verification imported source outside the installed environment")
    config = yaml.safe_load(resources.files("contextdb").joinpath("config/config.yaml").read_text())
    if not isinstance(config, dict):
        raise ValueError("ConDB configuration resource is invalid")
    Environment(loader=PackageLoader("contextdb", "prompts")).get_template("beam.jinja")
    prices = json.loads(
        resources.files("litellm")
        .joinpath("model_prices_and_context_window_backup.json")
        .read_text()
    )
    if "gpt-4o-mini" not in prices:
        raise ValueError("LiteLLM model resource is incomplete")
    if get_llm_provider(model="openai/gpt-4o-mini", api_key="fixture-key")[1] != "openai":
        raise ValueError("OpenAI-compatible provider routing lost")
    # Resolving subscription credentials starts a device login. Check the real
    # provider modules without instantiating authenticators or requesting tokens.
    for provider in ("chatgpt", "github_copilot"):
        for interface in ("chat", "responses"):
            importlib.import_module(f"litellm.llms.{provider}.{interface}.transformation")
    return {
        name: {"version": metadata.version(name), "path": str(path)} for name, path in paths.items()
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", action="store_true", help="Reject checkout imports")
    options = parser.parse_args()
    print(json.dumps(verify_install(wheel=options.wheel), indent=2))
