"""Shared, validated configuration reads and merge patches."""

from __future__ import annotations

import logging
import os
from contextlib import nullcontext
from pathlib import Path

import yaml
from pydantic import ValidationError

from openkb import config as _config_module
from openkb.application.settings_data import (
    _KB_CONFIG_WRITABLE_KEYS,
    GlobalConfigPatchRequest,
    GlobalConfigResponse,
    GlobalConfigValues,
    KbConfigPatchRequest,
    KbConfigResponse,
    SettingsView,
    _KbConfigWritable,
)
from openkb.config import (
    DEFAULT_CONFIG,
    _atomic_yaml_dump,
    _load_global_config_unlocked,
    _with_global_config_lock,
    load_global_config,
    resolve_credential_bundle,
    resolve_effective_config,
    resolve_entity_types,
    save_config,
)
from openkb.locks import atomic_write_text, kb_ingest_lock, kb_read_lock
from openkb.mutation import mutation_scope
from openkb.ocr.config import parsing_settings
from openkb.ocr.credentials import OCR_API_KEY_ENV, resolve_ocr_credential

logger = logging.getLogger(__name__)
_SECRET_ENV_FIELDS = {"api_key": "LLM_API_KEY", "ocr_api_key": OCR_API_KEY_ENV}
_CREDENTIAL_FIELDS = (*_SECRET_ENV_FIELDS, "openai_api_base")


def _has_line_separator(value: str) -> bool:
    """Whether ``value`` contains ANY character ``str.splitlines()`` treats as a
    line boundary — the exact splitter :func:`_merge_patch_env` uses to parse
    ``.env``.

    This is a strict superset of ``\\r``/``\\n``: ``str.splitlines()`` also
    breaks on ``\\v \\f \\x1c \\x1d \\x1e \\x85 \\u2028 \\u2029``. Guarding only
    CR/LF would let one of those smuggle a second ``KEY=VALUE`` line past the
    guard and into ``.env`` (the parser splits on it even though the naive guard
    doesn't see it). ``value != "".join(value.splitlines())`` is true iff at
    least one such separator is present.
    """
    return value != "".join(value.splitlines())


def _reject_credential_newlines(
    request: KbConfigPatchRequest | GlobalConfigPatchRequest,
) -> None:
    """Reject a line separator in a credential value BEFORE it reaches ``.env``.

    ``.env`` is line-oriented (``KEY=VALUE\\n``), so an ``api_key`` or
    ``openai_api_base`` containing any character the ``.env`` parser splits on
    (see :func:`_has_line_separator`) would inject extra KEY=VALUE lines. Raise a
    400 naming the offending field (the value is never logged). Merge-patch
    semantics: only fields the client actually sent are checked
    (``model_fields_set``); an explicit ``null`` (clear) carries no value.
    """
    fields_set = request.model_fields_set
    for field in _SECRET_ENV_FIELDS:
        secret = getattr(request, field)
        if (
            field in fields_set
            and secret is not None
            and _has_line_separator(secret.get_secret_value())
        ):
            raise ValueError(f"{field} must not contain newline characters")
    if (
        "openai_api_base" in fields_set
        and request.openai_api_base is not None
        and _has_line_separator(request.openai_api_base)
    ):
        raise ValueError("openai_api_base must not contain newline characters")


def _merge_patch_env(env_path: Path, updates: dict[str, str | None]) -> None:
    """Securely merge-patch a ``.env`` credential file (shared KB/global writer).

    Parses existing ``KEY=VALUE`` lines, tolerating a leading ``export `` so the
    key normalizes: ``export LLM_API_KEY=x`` is stored under ``LLM_API_KEY``,
    exactly what python-dotenv strips on read-back. Without this an ``export ``-
    prefixed line would be keyed ``"export LLM_API_KEY"``, so a later
    ``pop("LLM_API_KEY")`` (RFC 7386 clear) would MISS it and leave a live
    credential on disk. Applies ``updates`` (a ``None`` value REMOVES the key,
    else sets it), then writes atomically.

    The file is tightened to 0o600 BEFORE the atomic write so the LLM key is
    never world-readable — not even briefly: atomic_write_text copies the
    target's *current* mode onto its private temp file before the rename (see
    locks._target_mode), so touch(0o600)+chmod(0o600) makes os.replace land a
    0o600 ``.env`` with no widen-then-chmod gap. Callers MUST hold the relevant
    write lock (the per-KB mutation lock or the global-config lock): this is a
    read-modify-write. Credential VALUES are never logged.
    """
    env_lines: dict[str, str] = {}
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # Strip a leading ``export `` (shell-style .env) BEFORE partition so
            # the key normalizes and a later removal matches; python-dotenv
            # strips it too when it reads the file back.
            if line.startswith("export ") or line.startswith("export\t"):
                line = line[len("export") :].lstrip()
            if "=" not in line:
                continue
            k, _eq, v = line.partition("=")
            env_lines[k.strip()] = v
    for key, value in updates.items():
        if value is None:
            env_lines.pop(key, None)
        else:
            env_lines[key] = value
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.touch(mode=0o600, exist_ok=True)
    env_path.chmod(0o600)
    atomic_write_text(env_path, "".join(f"{k}={v}\n" for k, v in env_lines.items()))


def _read_kb_config(kb_dir: Path) -> KbConfigResponse:
    """Build the config response for a KB.

    Reports the EFFECTIVE scalar values (DEFAULT -> global.yaml -> KB config.yaml
    via resolve_effective_config), plus per-field ``sources`` and the raw
    ``global_values`` so the UI can render 继承(全局/默认) vs 本库覆盖. Credentials
    (openai_api_base plaintext + has_api_key presence flag) are bundle-resolved
    and unchanged; the API key value is NEVER exposed.
    """
    effective, sources = resolve_effective_config(kb_dir)
    bundle = resolve_credential_bundle(kb_dir)
    global_config = load_global_config()
    parsing = parsing_settings(effective.get("parsing"))
    return KbConfigResponse(
        model=effective["model"],
        parsing=parsing,
        processing=effective.get("processing"),
        navigation=effective.get("navigation") or {},
        compilation_thinking=effective.get("compilation_thinking"),
        verification_thinking=effective.get("verification_thinking"),
        language=effective["language"],
        pageindex_threshold=effective["pageindex_threshold"],
        # Cleaned effective list (what the compiler will use), not the raw stored
        # value — resolve_entity_types defaults + dedupes + ensures "other".
        entity_types=resolve_entity_types(effective, warn=False),
        openai_api_base=bundle.base_url,
        has_api_key=bundle.api_key is not None,
        has_ocr_api_key=bool(resolve_ocr_credential(kb_dir, parsing.ocr.cloud).api_key),
        sources=sources,
        global_values=GlobalConfigValues(
            model=global_config.get("model"),
            parsing=global_config.get("parsing"),
            processing=global_config.get("processing"),
            navigation=global_config.get("navigation"),
            compilation_thinking=global_config.get("compilation_thinking"),
            verification_thinking=global_config.get("verification_thinking"),
            language=global_config.get("language"),
            pageindex_threshold=global_config.get("pageindex_threshold"),
            entity_types=global_config.get("entity_types"),
        ),
    )


def _apply_kb_config_patch(kb_dir: Path, request: KbConfigPatchRequest) -> None:
    """Apply a JSON Merge Patch (RFC 7386) to a KB's ``config.yaml`` and ``.env``.

    The caller MUST hold the per-KB mutation lock: this is a read-modify-write
    over both files, so two concurrent patches without the lock would silently
    drop one's fields. Merge-patch semantics rely on ``model_fields_set`` so an
    ABSENT field is left unchanged while an explicit ``null`` CLEARS it — a plain
    ``is None`` check cannot tell the two apart. An unknown ``config`` key is a
    400 (not a silent no-op). Credential values are never logged.
    """
    fields_set = request.model_fields_set
    _reject_credential_newlines(request)

    if request.config is not None:
        unknown = set(request.config) - _KB_CONFIG_WRITABLE_KEYS
        if unknown:
            raise ValueError(f"Unknown config field(s): {', '.join(sorted(unknown))}")
        # Validate VALUE types before touching disk: a wrong-typed value is a
        # client error (400), and persisting it would 500 every future GET.
        try:
            validated = _KbConfigWritable.model_validate(request.config)
        except ValidationError as exc:
            first = exc.errors()[0]
            field = ".".join(str(p) for p in first.get("loc", ())) or "config"
            raise ValueError(f"Invalid type for config field '{field}': {first['msg']}") from exc
        config_path = kb_dir / ".openkb" / "config.yaml"
        # RAW read of the KB's own config.yaml — NOT load_config(), which
        # merges in DEFAULT_CONFIG. Merging defaults here would materialize
        # every default key (model/language/pageindex_threshold/...) into
        # config.yaml on the very next save_config() below, permanently
        # KB-pinning them to their default values and breaking
        # resolve_effective_config's global/default inheritance for every
        # scalar the client didn't ask to change (see resolve_effective_config
        # in config.py, which does the same raw read for its null-inherit gate).
        if config_path.exists():
            with config_path.open("r", encoding="utf-8") as fh:
                config = yaml.safe_load(fh) or {}
        else:
            config = {}
        if not isinstance(config, dict):
            raise ValueError("Knowledge-base config must be a mapping")
        # Persist the VALIDATED/coerced values (e.g. "20"→20), not the raw dict:
        # a coercible-but-wrong-typed value (numeric string, bool) would otherwise
        # land on disk verbatim and crash downstream int comparisons. exclude_unset
        # keeps merge-patch semantics — only the keys the client sent get touched.
        # RFC 7386: an explicit null REMOVES the key (revert to inherited),
        # while an absent field is left unchanged. model_fields_set (via
        # exclude_unset) distinguishes the two; a None value = explicit null.
        dumped = validated.model_dump(exclude_unset=True)
        for key, value in dumped.items():
            if value is None:
                config.pop(key, None)
            else:
                config[key] = value
        save_config(config_path, config)

    if fields_set.intersection(_CREDENTIAL_FIELDS):
        # Reject newline/CR injection in credential VALUES before touching disk:
        # a value with \n/\r would inject extra KEY=VALUE lines into .env (400,
        # never persisted). The shared writer does the secure atomic 0o600 write.
        _reject_credential_newlines(request)
        updates = _credential_updates(request)
        _merge_patch_env(kb_dir / ".env", updates)
        logger.info(
            "kb/config credential rotation: kb=%s fields=%s",
            request.kb,
            sorted(fields_set.intersection(_CREDENTIAL_FIELDS)),
        )


def _read_global_config() -> GlobalConfigResponse:
    """The global default scalars, DEFAULT_CONFIG-filled where global.yaml is
    silent, plus the global-default credentials read from the global ``.env``.

    Credentials come from ``GLOBAL_CONFIG_DIR / ".env"`` (the lowest-precedence
    source in ``resolve_credential_bundle``): ``openai_api_base`` is reported
    plaintext, ``has_api_key`` is a presence flag only — the key value is NEVER
    exposed. An empty string in the ``.env`` counts as unset.
    """
    from dotenv import dotenv_values

    gc = load_global_config()
    # GLOBAL_CONFIG_DIR is read through the module object (not imported by name)
    # because tests monkeypatch openkb.config.GLOBAL_CONFIG_DIR per test.
    env_path = _config_module.GLOBAL_CONFIG_DIR / ".env"
    env_values: dict[str, str | None] = {}
    if env_path.exists():
        env_values = dict(dotenv_values(str(env_path)))
    parsing = parsing_settings(gc.get("parsing"))
    return GlobalConfigResponse(
        model=gc.get("model", DEFAULT_CONFIG["model"]),
        parsing=parsing,
        processing=gc.get("processing"),
        navigation=gc.get("navigation") or {},
        compilation_thinking=gc.get("compilation_thinking"),
        verification_thinking=gc.get("verification_thinking"),
        language=gc.get("language", DEFAULT_CONFIG["language"]),
        pageindex_threshold=gc.get("pageindex_threshold", DEFAULT_CONFIG["pageindex_threshold"]),
        # Effective global vocabulary (cleaned; defaults to DEFAULT_ENTITY_TYPES).
        entity_types=resolve_entity_types(gc, warn=False),
        # Report the EFFECTIVE root (env > global.yaml kb_root > default) via the
        # module object so a test-monkeypatched GLOBAL_CONFIG_DIR is honored.
        kb_root=str(_config_module.kb_root_dir()),
        kb_root_env_pinned=bool(os.environ.get("OPENKB_KB_ROOT")),
        openai_api_base=(env_values.get("OPENAI_API_BASE") or None),
        has_api_key=bool(env_values.get("LLM_API_KEY")),
        has_ocr_api_key=bool(resolve_ocr_credential(cloud=parsing.ocr.cloud).api_key),
    )


def apply_global_config_patch(request: GlobalConfigPatchRequest) -> None:
    """Merge validated global settings and credentials as one recoverable change.

    The global lock covers reading, both writes and the durable commit marker.
    Other registry keys are preserved; omitted values stay unchanged and null
    removes an override. Failed rollback retains its journal and blocks later
    global reads/writes until controlled recovery succeeds. Secret values are
    never returned or logged.
    """
    fields_set = request.model_fields_set
    write_env = bool(fields_set.intersection(_CREDENTIAL_FIELDS))
    write_kb_root = "kb_root" in fields_set

    # Reject newline/CR injection in credential VALUES before the lock is touched
    # (a bad value is a 400 that never acquires the lock or writes disk).
    if write_env:
        _reject_credential_newlines(request)

    # A non-empty kb_root MUST be absolute — mirrors resolve_init_kb_dir / the
    # /init `path` guard. A relative root would resolve against the server's cwd
    # (unstable), so reject it with a 400 before the lock. Empty/whitespace (and
    # explicit null) still clear it (handled in the write section below).
    if write_kb_root and request.kb_root is not None and request.kb_root.strip():
        if not Path(request.kb_root).expanduser().is_absolute():
            raise ValueError("kb_root must be an absolute directory")

    # Validate scalar VALUE types before touching disk (a wrong type is a 400,
    # never persisted). Done outside the lock.
    dumped: dict[str, object] | None = None
    if request.config is not None:
        unknown = set(request.config) - _KB_CONFIG_WRITABLE_KEYS
        if unknown:
            raise ValueError(f"Unknown config field(s): {', '.join(sorted(unknown))}")
        try:
            validated = _KbConfigWritable.model_validate(request.config)
        except ValidationError as exc:
            first = exc.errors()[0]
            field = ".".join(str(p) for p in first.get("loc", ())) or "config"
            raise ValueError(f"Invalid type for config field '{field}': {first['msg']}") from exc
        dumped = validated.model_dump(exclude_unset=True)

    if dumped is None and not write_env and not write_kb_root:
        return

    with (
        _with_global_config_lock(),
        mutation_scope(
            _config_module.GLOBAL_CONFIG_DIR,
            [_config_module.GLOBAL_CONFIG_PATH, _config_module.GLOBAL_CONFIG_DIR / ".env"],
            operation="global-settings",
            lock_path=_config_module.GLOBAL_CONFIG_DIR / "global.lock",
        ),
    ):
        if dumped is not None or write_kb_root:
            gc = _load_global_config_unlocked()
            if dumped is not None:
                for key, value in dumped.items():
                    if value is None:
                        gc.pop(key, None)  # RFC 7386 removal -> back to DEFAULT_CONFIG
                    else:
                        gc[key] = value
            if write_kb_root:
                # kb_root is a plain global.yaml key (like known_kbs), NOT a
                # scalar in `config` and NOT a credential — it is merged directly
                # into the loaded dict, never routed to the .env. RFC 7386:
                # explicit null (or an empty/whitespace string) removes it
                # (revert to the default root), a non-empty absolute string sets
                # it. Env OPENKB_KB_ROOT still overrides it at runtime.
                if request.kb_root is None or not request.kb_root.strip():
                    gc.pop("kb_root", None)
                else:
                    gc["kb_root"] = request.kb_root
            # GLOBAL_CONFIG_PATH is read through the module object (not imported
            # by name) because tests monkeypatch openkb.config.GLOBAL_CONFIG_PATH
            # per test; a `from openkb.config import GLOBAL_CONFIG_PATH` would
            # freeze a stale Path captured at first import.
            _atomic_yaml_dump(_config_module.GLOBAL_CONFIG_PATH, gc)
        if write_env:
            _write_global_env(request, fields_set)


def _credential_updates(request: KbConfigPatchRequest | GlobalConfigPatchRequest):
    updates: dict[str, str | None] = {}
    for field, key in _SECRET_ENV_FIELDS.items():
        if field in request.model_fields_set:
            secret = getattr(request, field)
            updates[key] = None if secret is None else secret.get_secret_value()
    if "openai_api_base" in request.model_fields_set:
        updates["OPENAI_API_BASE"] = request.openai_api_base
    return updates


def _write_global_env(request: GlobalConfigPatchRequest, fields_set: set[str]) -> None:
    """Read-modify-write the global ``.env`` credential file, mirroring the
    per-KB ``.env`` write in ``apply_kb_config_patch``.

    MUST be called while holding ``_with_global_config_lock()`` (a
    read-modify-write). RFC 7386: an explicit ``null`` removes the key while an
    absent field is left unchanged (``model_fields_set`` distinguishes them).
    Newline rejection has already happened at the request-validation layer
    (:func:`_reject_credential_newlines`, before the lock). The shared writer
    does the secure atomic 0o600 write; the key VALUE is never logged.
    """
    updates = _credential_updates(request)
    # Module object, not a by-name import: tests monkeypatch GLOBAL_CONFIG_DIR.
    _merge_patch_env(_config_module.GLOBAL_CONFIG_DIR / ".env", updates)
    logger.info(
        "global/config credential rotation: fields=%s",
        sorted(fields_set.intersection(_CREDENTIAL_FIELDS)),
    )


def read_global_config() -> GlobalConfigResponse:
    with _with_global_config_lock():
        return _read_global_config()


def read_kb_config(kb_dir: Path) -> KbConfigResponse:
    """Read one consistent settings version without returning a secret value."""
    with kb_read_lock(kb_dir / ".openkb"):
        with _with_global_config_lock():
            return _read_kb_config(kb_dir)


def apply_kb_config_patch(kb_dir: Path, request: KbConfigPatchRequest) -> None:
    """Save config and credentials together, or restore both on failure."""
    if not (kb_dir / ".openkb/config.yaml").is_file():
        raise FileNotFoundError(f"Knowledge base not found: {kb_dir}")
    with kb_ingest_lock(kb_dir / ".openkb"):
        with mutation_scope(
            kb_dir, [kb_dir / ".openkb/config.yaml", kb_dir / ".env"], operation="settings"
        ):
            _apply_kb_config_patch(kb_dir, request)


def read_settings_view(kb_dir: Path | None = None) -> SettingsView:
    """Read native settings with field origins, retaining existing API responses."""
    from dotenv import dotenv_values

    with (
        kb_read_lock(kb_dir / ".openkb") if kb_dir else nullcontext(),
        _with_global_config_lock(),
    ):
        values = _read_kb_config(kb_dir) if kb_dir else _read_global_config()
        global_config = _load_global_config_unlocked()
        sources: dict[str, str] = (
            dict(values.sources)
            if isinstance(values, KbConfigResponse)
            else {
                key: "global" if global_config.get(key) is not None else "default"
                for key in _KB_CONFIG_WRITABLE_KEYS
            }
        )
        layers = []
        if kb_dir:
            layers.extend([("kb", dotenv_values(kb_dir / ".env")), ("environment", os.environ)])
        layers.append(("global", dotenv_values(_config_module.GLOBAL_CONFIG_DIR / ".env")))
        for field, key in (("api_key", "LLM_API_KEY"), ("openai_api_base", "OPENAI_API_BASE")):
            sources[field] = next((name for name, data in layers if data.get(key)), "unset")
        sources["ocr_api_key"] = resolve_ocr_credential(kb_dir, values.parsing.ocr.cloud).source
        return SettingsView(values=values, sources=sources)
