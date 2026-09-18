"""Temporary request contracts, without retaining every prompt in worker memory."""

import threading
import weakref
from pathlib import Path
from tempfile import gettempdir

from openkb.locks import atomic_write_json
from openkb.processing import ProcessingIncomplete
from openkb.resource_checks import CONTRACT_BYTES, check_disk, json_size, resource_operation
from openkb.runtime.input_store import InputStore
from openkb.sources import content_id, read_object, valid_id


class PendingContracts:
    """Spool pending inputs until their immutable checkpoint owns the contract.

    These files are process-local scratch space, not recovery records. Recovery
    continues to use the validated checkpoints in the knowledge base.
    """

    def __init__(self, *, max_bytes=CONTRACT_BYTES):
        from openkb.resource_budget import current_resources

        self._resources = current_resources()
        self._directory: InputStore | None = None
        self._finalizer = None
        self._lock = threading.RLock()
        self._sizes: dict[str, int] = {}
        self._max_bytes = max_bytes

    def save(self, key, contract):
        size = json_size(contract)
        from openkb.resource_budget import check_memory

        check_memory(size * 3, stage=contract["payload"]["stage"])
        with self._lock:
            stage = contract["payload"]["stage"]
            if sum(self._sizes.values()) - self._sizes.get(key, 0) + size > self._max_bytes:
                raise ProcessingIncomplete("resource_input_exceeds_budget", stage)
            check_disk(gettempdir(), size, stage=stage, operation="temporary_contract")
            if self._directory is None:
                self._directory = InputStore(
                    Path(gettempdir()).resolve() / "openkb-checkpoint-contracts"
                )
                self._finalizer = weakref.finalize(self, self._directory.cleanup)
            previous = self._sizes.get(key, 0)
            if self._resources:
                self._resources.reserve_input(id(self), key, size, stage)
            try:
                with resource_operation(stage, "temporary_contract"):
                    atomic_write_json(
                        Path(self._directory.name) / f"{valid_id(key)}.json", contract
                    )
                self._sizes[key] = size
            except BaseException:
                if self._resources:
                    if previous:
                        self._resources.reserve_input(id(self), key, previous, stage)
                    else:
                        self._resources.release_input(id(self), key)
                raise

    def read(self, key):
        with self._lock:
            if self._directory is None:
                return None
            path = Path(self._directory.name) / f"{valid_id(key)}.json"
            if not path.exists():
                return None
            contract = read_object(path)
            if (
                content_id(contract) != key
                or not isinstance(contract.get("payload"), dict)
                or not isinstance(contract["payload"].get("stage"), str)
            ):
                raise ValueError("Pending checkpoint contract identity mismatch")
            return contract

    def discard(self, key):
        with self._lock:
            if self._directory is not None:
                (Path(self._directory.name) / f"{valid_id(key)}.json").unlink(missing_ok=True)
            self._sizes.pop(key, None)
            if self._resources:
                self._resources.release_input(id(self), key)

    def close(self):
        with self._lock:
            if self._directory is not None:
                self._finalizer()
                self._finalizer = None
                self._directory = None
            for key in list(self._sizes):
                self.discard(key)
