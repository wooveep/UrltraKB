# pageindex/client.py
from __future__ import annotations
from pathlib import Path

from .collection import Collection
from .config import IndexConfig
from .parser.protocol import DocumentParser


def _normalize_retrieve_model(model: str) -> str:
    """Preserve supported Agents SDK prefixes and route other provider paths via LiteLLM."""
    passthrough_prefixes = ("litellm/", "openai/")
    if not model or "/" not in model:
        return model
    if model.startswith(passthrough_prefixes):
        return model
    return f"litellm/{model}"


class PageIndexClient:
    """Local document indexing and retrieval with project-owned storage.

    Model requests use the caller's configured LLM provider. Indexes, source
    files and collections are stored locally; there is no managed service mode.
    Use keyword arguments to configure model, retrieve_model, storage_path,
    storage or index_config. The former positional service key is unsupported.
    """

    def __init__(self, *, model: str = None, retrieve_model: str = None,
                 storage_path: str = None, storage=None,
                 index_config: IndexConfig | dict = None, files_path: str = None,
                 read_only: bool = False):
        self._init_local(model, retrieve_model, storage_path, storage, index_config, files_path=files_path, read_only=read_only)

    def _init_local(self, model: str = None, retrieve_model: str = None,
                    storage_path: str = None, storage=None,
                    index_config: IndexConfig | dict = None, *, files_path: str = None,
                 read_only: bool = False):
        # Build IndexConfig: merge model/retrieve_model with index_config
        overrides = {}
        if model:
            overrides["model"] = model
        if retrieve_model:
            overrides["retrieve_model"] = retrieve_model
        if isinstance(index_config, IndexConfig):
            opt = index_config.model_copy(update=overrides)
        elif isinstance(index_config, dict):
            merged = {**index_config, **overrides}  # explicit model/retrieve_model win
            opt = IndexConfig(**merged)
        else:
            opt = IndexConfig(**overrides) if overrides else IndexConfig()


        storage_path = Path(storage_path or ".pageindex").resolve()
        if read_only:
            if storage is None:
                raise ValueError("Read-only clients require explicit read-only storage")
            if not storage_path.is_dir():
                raise FileNotFoundError(storage_path)
        else:
            storage_path.mkdir(parents=True, exist_ok=True)

        from .storage.sqlite import SQLiteStorage
        from .backend.local import LocalBackend
        storage_engine = storage or SQLiteStorage(str(storage_path / "pageindex.db"))
        self._backend = LocalBackend(
            storage=storage_engine,
            files_dir=str(Path(files_path).resolve() if files_path else storage_path / "files"),
            read_only=read_only,
            model=opt.model,
            retrieve_model=_normalize_retrieve_model(opt.retrieve_model or opt.model),
            index_config=opt,
        )

    @staticmethod
    def _validate_llm_provider(model: str) -> None:
        """Validate the model string and require an API key for providers that
        need one. Local / keyless providers (ollama, lm_studio, …) are skipped so
        a keyless LiteLLM model isn't rejected at construction time."""
        try:
            import litellm
            _, provider, _, _ = litellm.get_llm_provider(model=model)
        except Exception:
            return

        # LiteLLM providers that run locally / self-hosted and need no API key
        # by default (litellm itself falls back to a placeholder key for these
        # rather than erroring — see e.g. hosted_vllm's transformation.py).
        # This list is necessarily a manual allowlist (litellm.validate_environment
        # isn't reliable enough to derive it from); extend it as litellm adds
        # more local-inference providers.
        keyless = {
            "ollama", "ollama_chat", "lm_studio", "hosted_vllm", "vllm",
            "xinference", "llamafile", "triton", "oobabooga",
            "openai_like", "custom_openai", "custom", "docker_model_runner",
            "petals",
        }
        if provider in keyless:
            return

        key = litellm.get_api_key(llm_provider=provider, dynamic_api_key=None)
        if not key:
            import os
            common_var = f"{provider.upper()}_API_KEY"
            if not os.getenv(common_var):
                from .errors import PageIndexError
                raise PageIndexError(
                    f"API key not configured for provider '{provider}' (model: {model}). "
                    f"Set the {common_var} environment variable."
                )

    def collection(self, name: str = "default") -> Collection:
        """Get or create a collection. Defaults to 'default'."""
        self._backend.get_or_create_collection(name)
        return Collection(name=name, backend=self._backend)

    def list_collections(self) -> list[str]:
        return self._backend.list_collections()

    def delete_collection(self, name: str) -> None:
        self._backend.delete_collection(name)

    def register_parser(self, parser: DocumentParser) -> None:
        """Register a custom document parser."""
        self._backend.register_parser(parser)

    def close(self):
        self._backend._storage.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class LocalClient(PageIndexClient):
    """Local mode — indexes and queries documents on your machine.

    Args:
        model: LLM model for indexing (default: gpt-4o-2024-11-20)
        retrieve_model: LLM model for agent QA (default: same as model)
        storage_path: Directory for SQLite DB and files (default: ./.pageindex)
        storage: Custom StorageEngine instance (default: SQLiteStorage)
        index_config: Advanced indexing parameters. Pass an IndexConfig instance
            or a dict. All fields have sensible defaults — most users don't need this.

    Example::

        # Simple — defaults are fine
        client = LocalClient(model="gpt-5.4")

        # Advanced — tune indexing parameters
        from pageindex.config import IndexConfig
        client = LocalClient(
            model="gpt-5.4",
            index_config=IndexConfig(toc_check_page_num=30),
        )
    """

    def __init__(self, model: str = None, retrieve_model: str = None,
                 storage_path: str = None, storage=None,
                 index_config: IndexConfig | dict = None, *, files_path: str = None,
                 read_only: bool = False):
        self._init_local(model, retrieve_model, storage_path, storage, index_config, files_path=files_path, read_only=read_only)
