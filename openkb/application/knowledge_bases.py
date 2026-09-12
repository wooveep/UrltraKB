"""Knowledge-base creation, reopening and inventory shared by all entry points."""

from __future__ import annotations

import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from openkb.config import DEFAULT_CONFIG, load_config, register_kb, save_config
from openkb.locks import atomic_write_json, atomic_write_text, kb_ingest_lock, kb_read_lock
from openkb.schema import AGENTS_MD, INDEX_SEED


def display_document_type(raw_type: str) -> str:
    if raw_type == "long_pdf":
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

        if not (root / ".openkb/config.yaml").is_file() or not (root / "wiki").is_dir():
            raise ValueError(f"Knowledge base is not initialized; create it explicitly: {root}")
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


@contextmanager
def _creation(kb_dir: Path, *, require_empty: bool) -> Iterator[None]:
    """Own initialization and recover a previous incomplete attempt first."""
    from openkb.mutation import mutation_scope

    state = kb_dir / ".openkb"
    if state.is_symlink():
        raise ValueError("Knowledge-base state must stay inside its directory")
    if state.is_dir() and not any(state.iterdir()):
        # Preserve the legacy refusal for an existing state directory with no
        # ownership/recovery evidence. A held creation lease has ingest.lock.
        raise FileExistsError(f"Knowledge base already initialized: {kb_dir}")
    with kb_ingest_lock(state):
        if (
            require_empty
            and not initialization_rolled_back(kb_dir)
            and any(path != state for path in kb_dir.iterdir())
        ):
            raise ValueError("请选择空目录创建知识库；已有知识库请使用“打开”。")
        # Recovery leaves these empty directories and the stable lock inode.
        # Any actual state still present belongs to an existing/partial KB.
        for path in state.iterdir():
            if path.name == "ingest.lock":
                continue
            if path.name == "initializing.json" and initialization_pending(kb_dir):
                continue
            if (
                path.name in {"journal", "staging"}
                and not path.is_symlink()
                and path.is_dir()
                and not any(path.iterdir())
            ):
                continue
            raise FileExistsError(f"Knowledge base already initialized: {kb_dir}")
        from openkb.lifecycle import begin_creation

        begin_creation(kb_dir)
        directories = [
            kb_dir / name
            for name in (
                "raw",
                "wiki",
                "wiki/sources",
                "wiki/sources/images",
                "wiki/summaries",
                "wiki/concepts",
                "wiki/entities",
            )
        ]
        files = [kb_dir / "wiki" / name for name in ("AGENTS.md", "index.md", "log.md")]
        files += [state / "config.yaml", state / "hashes.json"]
        if not (kb_dir / ".env").exists():
            files.append(kb_dir / ".env")
        targets = [path for path in directories if not path.exists()] + files
        if any(
            path.is_symlink() or not path.resolve().is_relative_to(kb_dir)
            for path in directories + files
        ):
            raise ValueError("Initialization paths must stay inside the knowledge base")
        # Record only highest missing directories, avoiding overlapping rollback
        # targets and backing up unrelated pre-existing documents.
        targets = [
            path for path in targets if not any(parent in targets for parent in path.parents)
        ]
        # This intent survives rollback. It distinguishes a creation that may be
        # explicitly retried from an established KB whose config was lost.
        intent = state / "initializing.json"
        atomic_write_json(intent, {"version": 1, "kb_dir": str(kb_dir)})
        with mutation_scope(kb_dir, [*targets, intent], operation="initialize"):
            yield
            # Intent removal commits with all seed files. A crash before commit
            # restores it; a committed creation never leaves a retry signal.
            intent.unlink(missing_ok=True)


def initialization_pending(kb_dir: Path) -> bool:
    """Whether this directory retains an explicit, unfinished creation intent."""
    root = kb_dir.expanduser().resolve()
    marker = root / ".openkb/initializing.json"
    try:
        return not marker.is_symlink() and json.loads(marker.read_text("utf-8")) == {
            "version": 1,
            "kb_dir": str(root),
        }
    except (OSError, ValueError):
        return False


def initialization_rolled_back(kb_dir: Path) -> bool:
    """A retained intent permits retry only after every persisted seed is gone."""
    if not initialization_pending(kb_dir):
        return False
    state = kb_dir / ".openkb"
    for path in state.iterdir():
        if path.name in {"ingest.lock", "initializing.json", "needs-repair.json"}:
            continue
        if (
            path.name in {"journal", "staging"}
            and not path.is_symlink()
            and path.is_dir()
            and not any(path.iterdir())
        ):
            continue
        return False
    return True


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
) -> dict[str, Any]:
    """Initialize a knowledge base at an explicit directory (REST ``/init``).

    Non-interactive counterpart to the ``init`` Click command: creates the
    raw/wiki/.openkb layout, seed files, config, and empty hash registry, then
    optionally writes LLM credentials to a KB-local ``.env``. Raises
    ``FileExistsError`` if the KB is already initialized.
    """
    kb_dir = kb_dir.expanduser().resolve()
    from openkb.lifecycle import creation_lifecycle

    with creation_lifecycle(kb_dir):
        openkb_dir = kb_dir / ".openkb"
        with _creation(kb_dir, require_empty=require_empty):
            kb_dir.mkdir(parents=True, exist_ok=True)
            (kb_dir / "raw").mkdir(exist_ok=True)
            (kb_dir / "wiki" / "sources" / "images").mkdir(parents=True, exist_ok=True)
            (kb_dir / "wiki" / "summaries").mkdir(parents=True, exist_ok=True)
            (kb_dir / "wiki" / "concepts").mkdir(parents=True, exist_ok=True)
            (kb_dir / "wiki" / "entities").mkdir(parents=True, exist_ok=True)

            atomic_write_text(kb_dir / "wiki" / "AGENTS.md", AGENTS_MD)
            atomic_write_text(kb_dir / "wiki" / "index.md", INDEX_SEED)
            atomic_write_text(kb_dir / "wiki" / "log.md", "# Operations Log\n\n")

            openkb_dir.mkdir(exist_ok=True)
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

        from openkb.application.source_history import source_status
        from openkb.sources import SourceStore

        by_identity = {row["hash"]: row for row in documents}
        for source in SourceStore(kb_dir).list_sources():
            details = source_status(kb_dir, source.source_id)
            result = details["result"]
            if source.source_id not in by_identity:
                # Removing knowledge does not erase its historical evidence.
                # Completed sources without a live registration stay in history.
                if result and result["knowledge_compilation"] == "completed":
                    continue
                row = {
                    "hash": source.source_id, "name": source.name,
                    "type": source.suffix.lstrip("."), "display_type": source.suffix.lstrip("."),
                    "pages": None,
                }
                documents.append(row)
            else:
                row = by_identity[source.source_id]
            row.update(
                source_id=source.source_id, source_version=source.id, source_origin=source.origin,
                source_intake="saved",
                knowledge_compilation=result["knowledge_compilation"] if result else "not_started",
                stage=result["stage"] if result else "source_intake",
                reason=result["reason"] if result else None,
                parse_id=result["parse_id"] if result else None,
                resume=result["resume"] if result else source.id,
                original=details["original"], cumulative_usage=details["cumulative_usage"],
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
