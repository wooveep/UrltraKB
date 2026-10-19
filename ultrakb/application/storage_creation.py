"""Prepare a complete new store privately, then install its authoritative directory."""

import os
import sqlite3
import tempfile
from pathlib import Path

from ultrakb.config import load_config, register_kb, save_config, validate_runtime_config
from ultrakb.lifecycle import begin_creation, creation_lifecycle
from ultrakb.locks import _fsync_directory, atomic_write_text, sync_file_and_parents
from ultrakb.mutation import RecoveryRequired
from ultrakb.schema import AGENTS_MD, INDEX_SEED
from ultrakb.storage.database import create_database, require_sqlite
from ultrakb.storage.knowledge_store import KnowledgeStore
from ultrakb.storage.wiki_projection import ProjectionStore


def _environment(root, template_dir, *, seed_environment, api_key, openai_api_base):
    if (root / ".env").exists():
        return {}
    values = {"LITELLM_LOCAL_MODEL_COST_MAP": "true"} if seed_environment else {}
    template = template_dir / ".env" if template_dir else None
    if template is not None and template.exists():
        for raw in template.read_text("utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if not key.strip().startswith("ULTRAKB_"):
                values[key.strip()] = value.strip()
    if api_key:
        values["LLM_API_KEY"] = api_key
    if openai_api_base:
        values["OPENAI_API_BASE"] = openai_api_base
    return values


def initialize_kb(
    kb_dir: Path,
    *,
    model: str | None = None,
    api_key: str | None = None,
    openai_api_base: str | None = None,
    language: str | None = None,
    template_dir: Path | None = None,
    seed_environment: bool = True,
    require_empty: bool = False,
) -> dict:
    """Create v2 only; an existing directory is never interpreted as an import."""
    require_sqlite()
    requested = kb_dir.expanduser().absolute()
    if requested.is_symlink():
        raise ValueError("Create a knowledge base at its real directory")
    root = requested.resolve()
    with creation_lifecycle(root):
        state = root / ".ultrakb"
        if state.exists() or state.is_symlink() or (root / "wiki").exists():
            raise FileExistsError(f"Knowledge base already initialized or unsupported: {root}")
        if root.exists() and require_empty and any(root.iterdir()):
            raise ValueError("请选择空目录创建知识库；已有知识库请使用“打开”。")
        template_config = template_dir / "config.yaml" if template_dir else None
        config = (
            load_config(template_config)
            if model is None and template_config is not None and template_config.is_file()
            else {}
        )
        if model is not None:
            config["model"] = model
        if language is not None:
            config["language"] = language
        validate_runtime_config(config, allow_inherited=True)
        environment = _environment(
            root,
            template_dir,
            seed_environment=seed_environment,
            api_key=api_key,
            openai_api_base=openai_api_base,
        )
        ancestor = root.parent
        while not ancestor.exists():
            ancestor = ancestor.parent
        root.mkdir(parents=True, exist_ok=True)
        sync_file_and_parents(root, ancestor=ancestor)
        unfinished = []
        with tempfile.TemporaryDirectory(
            prefix=".ultrakb-create-", dir=root.parent, ignore_cleanup_errors=True
        ) as name:
            prepared = Path(name)
            identity = create_database(prepared)
            save_config(prepared / ".ultrakb/config.yaml", config)
            store = KnowledgeStore(prepared, identity)
            store.publish(
                store.prepare_revision(
                    {
                        "AGENTS.md": AGENTS_MD.encode(),
                        "index.md": INDEX_SEED.encode(),
                        "log.md": b"# Operations Log\n\n",
                    },
                    expected=None,
                )
            )
            begin_creation(root)
            os.replace(prepared / ".ultrakb", state)
            try:
                _fsync_directory(root)
            except OSError:
                unfinished.append("confirm knowledge-base installation durability")

        written = {}
        if environment:
            try:
                target = root / ".env"
                target.touch(mode=0o600, exist_ok=False)
                atomic_write_text(
                    target, "".join(f"{key}={value}\n" for key, value in environment.items())
                )
                written = environment
            except OSError:
                unfinished.append("save local connection settings")
        try:
            (root / "raw").mkdir(exist_ok=True)
            projected = ProjectionStore(root, identity).sync()
            if projected.status not in {"synced", "obsolete"}:
                unfinished.append("synchronize wiki")
        except (OSError, sqlite3.Error, RecoveryRequired):
            unfinished.append("synchronize wiki")
        try:
            register_kb(root)
        except (OSError, ValueError, TypeError, AttributeError, RecoveryRequired):
            unfinished.append("register knowledge base")
        return {
            "kb_dir": str(root),
            "created": True,
            "env_written": {
                "api_key": "LLM_API_KEY" in written,
                "openai_api_base": "OPENAI_API_BASE" in written,
            },
            "message": "Knowledge base initialized."
            + (" Pending: " + ", ".join(unfinished) if unfinished else ""),
            "unfinished": tuple(unfinished),
        }
