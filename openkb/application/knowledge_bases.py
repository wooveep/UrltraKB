"""Knowledge-base creation, reopening and inventory shared by all entry points."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from openkb.config import DEFAULT_CONFIG, load_config, register_kb, save_config
from openkb.locks import atomic_write_json, atomic_write_text, kb_ingest_lock, kb_read_lock
from openkb.schema import AGENTS_MD, INDEX_SEED


def display_document_type(raw_type: str) -> str:
    if raw_type in {"long_pdf", "pageindex_cloud"}:
        return "pageindex"
    if raw_type in {
        "pdf",
        "docx",
        "md",
        "markdown",
        "html",
        "htm",
        "txt",
        "csv",
        "pptx",
        "xlsx",
        "xls",
    }:
        return "short"
    return raw_type


def open_kb(kb_dir: Path) -> Path:
    root = kb_dir.expanduser().resolve()
    if not (root / ".openkb/config.yaml").is_file() or not (root / "wiki").is_dir():
        raise ValueError(f"Not a knowledge base: {root}")
    with kb_ingest_lock(root / ".openkb"):
        from openkb.config import resolve_effective_config, validate_runtime_config

        validate_runtime_config(load_config(root / ".openkb/config.yaml"), allow_inherited=True)
        validate_runtime_config(resolve_effective_config(root)[0])
        register_kb(root)
    return root


def _newest_mtime_iso(paths: list[Path]) -> str | None:
    """Newest mtime among paths as an ISO-8601 string, or None when empty."""
    if not paths:
        return None
    import datetime

    newest = max(paths, key=lambda p: p.stat().st_mtime)
    local_tz = datetime.datetime.now().astimezone().tzinfo
    return datetime.datetime.fromtimestamp(
        newest.stat().st_mtime,
        tz=local_tz,
    ).isoformat()


def initialize_kb(
    kb_dir: Path,
    *,
    model: str | None = None,
    api_key: str | None = None,
    openai_api_base: str | None = None,
    language: str | None = None,
    template_dir: Path | None = None,
    seed_environment: bool = True,
) -> dict[str, Any]:
    """Initialize a knowledge base at an explicit directory (REST ``/init``).

    Non-interactive counterpart to the ``init`` Click command: creates the
    raw/wiki/.openkb layout, seed files, config, and empty hash registry, then
    optionally writes LLM credentials to a KB-local ``.env``. Raises
    ``FileExistsError`` if the KB is already initialized.
    """
    kb_dir = kb_dir.expanduser().resolve()
    openkb_dir = kb_dir / ".openkb"
    from openkb.locks import kb_ingest_lock_held

    # A caller may pre-acquire execution before waiting for global settings.
    # Its lock file is not an initialization marker. Only that exact, owned
    # directory shape is accepted; arbitrary partial/existing KBs are rejected.
    prepared_lock = (
        kb_ingest_lock_held(openkb_dir)
        and openkb_dir.is_dir()
        and {path.name for path in openkb_dir.iterdir()} == {"ingest.lock"}
    )
    if openkb_dir.exists() and not prepared_lock:
        raise FileExistsError(f"Knowledge base already initialized: {kb_dir}")

    kb_dir.mkdir(parents=True, exist_ok=True)
    (kb_dir / "raw").mkdir(exist_ok=True)
    (kb_dir / "wiki" / "sources" / "images").mkdir(parents=True, exist_ok=True)
    (kb_dir / "wiki" / "summaries").mkdir(parents=True, exist_ok=True)
    (kb_dir / "wiki" / "concepts").mkdir(parents=True, exist_ok=True)
    (kb_dir / "wiki" / "entities").mkdir(parents=True, exist_ok=True)

    atomic_write_text(kb_dir / "wiki" / "AGENTS.md", AGENTS_MD)
    atomic_write_text(kb_dir / "wiki" / "index.md", INDEX_SEED)
    atomic_write_text(kb_dir / "wiki" / "log.md", "# Operations Log\n\n")

    openkb_dir.mkdir(exist_ok=prepared_lock)
    # Seed config.yaml: an explicit model wins; otherwise inherit the
    # operator's project-root config.yaml (model/language/optional blocks)
    # so a KB created via the REST UI matches the deployed setup instead of
    # the hardcoded DEFAULT_CONFIG (gpt-5.4 / en). Defaults are the last resort.
    template_config = template_dir / "config.yaml" if template_dir else None
    if model is not None:
        config = {
            "model": model,
            "language": language or DEFAULT_CONFIG["language"],
            "pageindex_threshold": DEFAULT_CONFIG["pageindex_threshold"],
        }
        save_config(openkb_dir / "config.yaml", config)
    elif template_config is not None and template_config.exists():
        shutil.copy2(template_config, openkb_dir / "config.yaml")
    else:
        config = {
            "model": DEFAULT_CONFIG["model"],
            "language": language or DEFAULT_CONFIG["language"],
            "pageindex_threshold": DEFAULT_CONFIG["pageindex_threshold"],
        }
        save_config(openkb_dir / "config.yaml", config)
    atomic_write_json(openkb_dir / "hashes.json", {})

    # Seed KB-local .env: inherit LLM credentials from the project-root .env so
    # a new KB can run queries/compiles out of the box. REST-server variables
    # (OPENKB_API_TOKEN, OPENKB_KB_ROOT, ...) are filtered out — they scope to
    # the server, not a single KB. Explicit api_key/openai_api_base params
    # override anything inherited. Precedence: default -> template -> explicit.
    env_path = kb_dir / ".env"
    can_write_env = not env_path.exists()
    env_pairs: dict[str, str] = {}
    if can_write_env:
        if seed_environment:
            env_pairs["LITELLM_LOCAL_MODEL_COST_MAP"] = "true"
        template_env = template_dir / ".env" if template_dir else None
        if template_env is not None and template_env.exists():
            for raw in template_env.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                # Skip REST-server variables; keep LLM/provider config.
                if key.startswith("OPENKB_"):
                    continue
                env_pairs[key] = val.strip()
        if api_key:
            env_pairs["LLM_API_KEY"] = api_key
        if openai_api_base:
            env_pairs["OPENAI_API_BASE"] = openai_api_base
        if env_pairs:
            env_path.touch(mode=0o600, exist_ok=False)
            atomic_write_text(env_path, "".join(f"{k}={v}\n" for k, v in env_pairs.items()))

    register_kb(kb_dir)
    return {
        "kb_dir": str(kb_dir),
        "created": True,
        "env_written": {
            "api_key": "LLM_API_KEY" in env_pairs,
            "openai_api_base": "OPENAI_API_BASE" in env_pairs,
        },
        "message": "Knowledge base initialized.",
    }


def get_kb_list(kb_dir: Path) -> dict[str, Any]:
    """Return a structured inventory of the knowledge base (REST ``/list``)."""
    with kb_read_lock(kb_dir / ".openkb"):
        openkb_dir = kb_dir / ".openkb"
        hashes_file = openkb_dir / "hashes.json"
        hashes = json.loads(hashes_file.read_text(encoding="utf-8")) if hashes_file.exists() else {}

        documents = []
        for file_hash, meta in hashes.items():
            raw_type = meta.get("type", "unknown")
            pages = meta.get("pages")
            documents.append(
                {
                    "hash": file_hash,
                    "name": meta.get("name", "unknown"),
                    "type": raw_type,
                    "display_type": display_document_type(raw_type),
                    "pages": pages if pages not in ("", 0) else None,
                }
            )

        summaries_dir = kb_dir / "wiki" / "summaries"
        concepts_dir = kb_dir / "wiki" / "concepts"
        entities_dir = kb_dir / "wiki" / "entities"
        reports_dir = kb_dir / "wiki" / "reports"
        return {
            "documents": documents,
            "document_count": len(documents),
            "summaries": sorted(p.stem for p in summaries_dir.glob("*.md"))
            if summaries_dir.exists()
            else [],
            "concepts": sorted(p.stem for p in concepts_dir.glob("*.md"))
            if concepts_dir.exists()
            else [],
            "entities": sorted(p.stem for p in entities_dir.glob("*.md"))
            if entities_dir.exists()
            else [],
            "reports": sorted(p.name for p in reports_dir.glob("*.md"))
            if reports_dir.exists()
            else [],
        }


def get_kb_status(kb_dir: Path) -> dict[str, Any]:
    """Return structured status for the knowledge base (REST ``/status``)."""
    with kb_read_lock(kb_dir / ".openkb"):
        wiki_dir = kb_dir / "wiki"
        subdirs = ["sources", "summaries", "concepts", "reports"]
        directories = {}
        for subdir in subdirs:
            path = wiki_dir / subdir
            directories[subdir] = len(list(path.glob("*.md"))) if path.exists() else 0

        raw_dir = kb_dir / "raw"
        raw_count = len([f for f in raw_dir.iterdir() if f.is_file()]) if raw_dir.exists() else 0

        hashes_file = kb_dir / ".openkb" / "hashes.json"
        hashes = json.loads(hashes_file.read_text(encoding="utf-8")) if hashes_file.exists() else {}

        summaries = (
            list((wiki_dir / "summaries").glob("*.md")) if (wiki_dir / "summaries").exists() else []
        )
        reports = (
            list((wiki_dir / "reports").glob("*.md")) if (wiki_dir / "reports").exists() else []
        )
        return {
            "directories": directories,
            "raw_count": raw_count,
            "total_indexed": len(hashes),
            "last_compile": _newest_mtime_iso(summaries),
            "last_lint": _newest_mtime_iso(reports),
        }
