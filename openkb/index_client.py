"""One construction path for every OpenKB document indexing/read client."""

from pathlib import Path

from pageindex import IndexConfig, LocalClient

from openkb.condb_storage import ConDBPageIndexStorage
from openkb.index_location import IndexLocation


class _ReadOnlyLLM:
    def complete(self, *args, **kwargs):
        raise PermissionError("Reading a sealed document index cannot call a model")

    async def acomplete(self, *args, **kwargs):
        return self.complete(*args, **kwargs)


def create_index_client(
    *, location=None, storage_path=None, storage=None, model=None, index_config=None
):
    if location is None:
        location = (
            storage.location
            if isinstance(storage, ConDBPageIndexStorage)
            else IndexLocation.package(Path(storage_path or ".openkb"))
        )
    if storage is not None and (
        not isinstance(storage, ConDBPageIndexStorage) or storage.location != location
    ):
        raise ValueError("OpenKB requires ConDB storage at the declared index location")
    if index_config is None:
        index_config = IndexConfig()
    if isinstance(index_config, dict):
        index_config = IndexConfig(**index_config)
    if index_config.llm_client is None:
        if location.read_only:
            runtime = _ReadOnlyLLM()
        else:
            from openkb.indexer import _build_index_config

            runtime = _build_index_config({"model": model or index_config.model}).llm_client
        index_config = index_config.model_copy(update={"llm_client": runtime})
    index_config = index_config.model_copy(update={"require_llm_client": True})
    owned = storage or ConDBPageIndexStorage(location)
    try:
        client = LocalClient(
            model=model,
            storage_path=str(location.database.parent),
            storage=owned,
            files_path=str(location.inputs_root),
            read_only=location.read_only,
            index_config=index_config,
        )
        from openkb.block_package import FrozenBlockParser
        from openkb.office.slide_package import FrozenSlideParser

        client.register_parser(FrozenBlockParser())
        client.register_parser(FrozenSlideParser())
        return client
    except BaseException:
        owned.close()
        raise
