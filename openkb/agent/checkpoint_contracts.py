"""Temporary request contracts, without retaining every prompt in worker memory."""

import threading
from pathlib import Path
from tempfile import TemporaryDirectory

from openkb.locks import atomic_write_json
from openkb.sources import content_id, read_object, valid_id


class PendingContracts:
    """Spool pending inputs until their immutable checkpoint owns the contract.

    These files are process-local scratch space, not recovery records. Recovery
    continues to use the validated checkpoints in the knowledge base.
    """

    def __init__(self):
        self._directory: TemporaryDirectory | None = None
        self._lock = threading.RLock()

    def save(self, key, contract):
        with self._lock:
            if self._directory is None:
                self._directory = TemporaryDirectory(prefix="openkb-checkpoint-contracts-")
            atomic_write_json(Path(self._directory.name) / f"{valid_id(key)}.json", contract)

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
