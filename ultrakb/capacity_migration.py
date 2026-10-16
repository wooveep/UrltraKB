"""Idempotent migration of the exact retired connection template, under settings locks."""

import hashlib
import json
from pathlib import Path

from ultrakb.locks import atomic_write_json
from ultrakb.model_capabilities import connection_capacity


def legacy_template(capacity: dict | None, model: str) -> bool:
    if isinstance(capacity, dict):
        capacity = {
            k: v
            for k, v in capacity.items()
            if v is not None
            and k != "schema_version"
            and not (k == "measurement" and v == "tokenizer")
        }
    if not isinstance(capacity, dict) or capacity.get("max_input_tokens") != 65536:
        return False
    if model not in {"gpt-5.4", "deepseek/deepseek-flash"}:
        return False
    if capacity.get("applies_to_model") != model or set(capacity) != {
        "max_input_tokens",
        "tokenizer_model",
        "applies_to_model",
    }:
        return False
    name = capacity.get("tokenizer_model", "")
    if not isinstance(name, str) or not name.startswith("local:"):
        return False
    path = Path(name.removeprefix("local:"))
    if not path.is_absolute():
        return False
    if path.is_dir():
        path /= "tokenizer.json"
    try:
        raw = json.loads(path.read_text())
        from tokenizers import Tokenizer, models, pre_tokenizers

        expected = Tokenizer(
            models.BPE(
                vocab={c: i for i, c in enumerate(sorted(pre_tokenizers.ByteLevel.alphabet()))},
                merges=[],
            )
        )
        expected.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False, use_regex=False)
        return raw == json.loads(expected.to_str())
    except FileNotFoundError:
        # The retired factory used this content-addressed name. A moved KB
        # must be able to migrate even if the old settings asset is absent.
        return path.name == "utf8-bound-v1-33f2bfa809d90e0d.json"
    except (OSError, ValueError, TypeError):
        return False


def migrate_settings(kb_dir: Path | None, *, base_url: str | None) -> bool:
    """Caller owns the KB lease (or global settings lock). Never touch old artifacts."""
    from ultrakb import config
    from ultrakb.locks import kb_ingest_lock_held

    if kb_dir is not None and not kb_ingest_lock_held(kb_dir / ".ultrakb"):
        raise RuntimeError("Capacity migration requires the knowledge base lease")
    with config._with_global_config_lock():
        path = kb_dir / ".ultrakb/config.yaml" if kb_dir else config.GLOBAL_CONFIG_PATH
        if not path.is_file():
            return False
        import yaml

        values = yaml.safe_load(path.read_text()) or {}
        if not isinstance(values, dict):
            raise ValueError("Settings must contain a mapping")
        model = values.get("model") or (
            config.resolve_effective_config(kb_dir)[0]["model"]
            if kb_dir
            else config.DEFAULT_CONFIG["model"]
        )
        before = values.get("model_capacity")
        if not legacy_template(before, model):
            return False
        # Empty mapping deliberately blocks inheritance of another retired
        # global template when this private endpoint has no sourced profile.
        after = connection_capacity(model, base_url) or {}
        receipt = {"version": 1, "model": model, "before": before, "after": after}
        identity = hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()
        atomic_write_json(path.parent / "capacity-migrations" / (identity + ".json"), receipt)
        values["model_capacity"] = after
        config._atomic_yaml_dump(path, values)
        return True
