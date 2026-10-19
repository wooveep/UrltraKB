"""New-store settings use their own short coordination, never a model-wait lock."""

import json
from pathlib import Path

from ultrakb import config
from ultrakb.application.settings_data import KbConfigPatchRequest, KbConfigResponse
from ultrakb.config_snapshot import initialize_sdk_environment, read_snapshot
from ultrakb.config_state import ConfigSnapshot, live_configuration
from ultrakb.configuration_lock import (
    configuration_lock_path,
)
from ultrakb.configuration_lock import (
    require_prepared_configuration as _require_prepared,
)
from ultrakb.lifecycle import read_lifecycle
from ultrakb.locks import file_write_lock
from ultrakb.mutation import (
    RecoveryRequired,
    mutation_scope,
    recover_pending_journals,
)
from ultrakb.storage.database import DatabaseIdentity


def capture_storage_config(
    kb_dir: Path,
    identity: DatabaseIdentity,
    *,
    bundle: config.LlmCredentialBundle | None = None,
    **wait_options,
) -> ConfigSnapshot:
    root = kb_dir.resolve()
    with (
        read_lifecycle(root, database_identity=identity, **wait_options),
        file_write_lock(configuration_lock_path(root), **wait_options),
        config._with_global_config_lock(recover=False, **wait_options),
    ):
        _require_prepared(root)
        _require_prepared(config.GLOBAL_CONFIG_DIR)
        if not (root / ".ultrakb/config.yaml").is_file():
            raise FileNotFoundError("Prepare execution configuration before starting work")
        return read_snapshot(root, bundle=bundle, **wait_options)


def _validate_settings_journals(root: Path):
    allowed = {root / ".ultrakb/config.yaml", root / ".env"}
    for path in (root / ".ultrakb/journal").glob("*.json"):
        try:
            if path.is_symlink() or path.stat().st_size > 256 * 1024:
                raise ValueError("Invalid settings journal")
            record = json.loads(path.read_bytes())
            if (
                not isinstance(record, dict)
                or record.get("operation") != "settings"
                or record.get("publication") is not None
                or record.get("parent_journal") is not None
                or not isinstance(record.get("entries"), list)
                or any(Path(item["target"]).resolve() not in allowed for item in record["entries"])
            ):
                raise ValueError("Only the settings pair belongs to configuration preparation")
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise RecoveryRequired("Settings journal needs explicit diagnosis") from error


def prepare_storage_config(
    kb_dir: Path, identity: DatabaseIdentity, **wait_options
) -> ConfigSnapshot:
    """Explicitly recover settings and prepare retired capacity templates before work."""
    from ultrakb.capacity_migration import migrate_settings

    root = kb_dir.resolve()
    with (
        read_lifecycle(root, database_identity=identity, **wait_options),
        file_write_lock(root / ".ultrakb/ingest.lock", **wait_options),
        file_write_lock(configuration_lock_path(root), **wait_options),
        live_configuration(),
        config._with_global_config_lock(**wait_options),
    ):
        if not (root / ".ultrakb/config.yaml").is_file():
            raise FileNotFoundError("Create execution configuration before preparing settings")
        _validate_settings_journals(root)
        recover_pending_journals(root, lock_path=configuration_lock_path(root))
        initialize_sdk_environment()
        credentials = config.resolve_credential_bundle(root)
        effective, sources = config.resolve_effective_config(root)
        if sources.get("model_capacity") == "global":
            global_credentials, _ = config.resolve_connection_credentials(model=effective["model"])
            if global_credentials.base_url == credentials.base_url:
                migrate_settings(None, base_url=global_credentials.base_url)
        migrate_settings(root, base_url=credentials.base_url)
        return read_snapshot(root, **wait_options)


def patch_storage_config(
    kb_dir: Path, identity: DatabaseIdentity, request: KbConfigPatchRequest, **wait_options
) -> KbConfigResponse:
    from ultrakb.application.settings import _apply_kb_config_patch, _read_kb_config

    if not isinstance(request, KbConfigPatchRequest):
        raise ValueError("Settings changes require a validated merge-patch request")
    root = kb_dir.resolve()
    with (
        read_lifecycle(root, database_identity=identity, **wait_options),
        file_write_lock(configuration_lock_path(root), **wait_options),
        live_configuration(),
        config._with_global_config_lock(recover=False, **wait_options),
    ):
        _require_prepared(root)
        _require_prepared(config.GLOBAL_CONFIG_DIR)
        paths = [root / ".ultrakb/config.yaml", root / ".env"]
        if not paths[0].is_file():
            raise FileNotFoundError("Prepare execution configuration before changing settings")
        with mutation_scope(
            root, paths, operation="settings", lock_path=configuration_lock_path(root)
        ):
            _apply_kb_config_patch(root, request)
        return _read_kb_config(root)


def repair_storage_config(kb_dir: Path, identity: DatabaseIdentity) -> tuple[str, ...]:
    """Recover only this format's settings pair, without loading model credentials."""
    import yaml
    from dotenv.parser import parse_stream

    from ultrakb.locks import atomic_write_json, durable_unlink
    from ultrakb.mutation import repair_marker

    root = kb_dir.resolve()
    lock = configuration_lock_path(root)
    with read_lifecycle(root, database_identity=identity), file_write_lock(lock):
        try:
            _validate_settings_journals(root)
            messages = recover_pending_journals(root, repairing=True, lock_path=lock)
            value = yaml.safe_load((root / ".ultrakb/config.yaml").read_text("utf-8"))
            if value is not None and not isinstance(value, dict):
                raise ValueError("Configuration must be a mapping")
            config.validate_runtime_config(value or {}, allow_inherited=True)
            if (root / ".env").exists():
                with (root / ".env").open(encoding="utf-8") as stream:
                    if any(binding.error for binding in parse_stream(stream)):
                        raise ValueError("Knowledge-base credential file is invalid")
        except (OSError, ValueError, yaml.YAMLError, RecoveryRequired) as error:
            atomic_write_json(repair_marker(root), {"error_type": type(error).__name__})
            raise
        durable_unlink(repair_marker(root), missing_ok=True)
        return tuple(messages)
