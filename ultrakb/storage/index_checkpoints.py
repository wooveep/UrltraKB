"""PageIndex's exact dispatches and accepted stages in the shared recovery store."""

from contextlib import contextmanager

from ultrakb.agent.page_response import PageResponseError
from ultrakb.compile_checkpoints import digest

from .checkpoint_store import _json
from .compile_binding import model_invocation
from .compile_deliveries import provider_response
from .coordination import storage_stage


class StoredIndexStages:
    def __init__(self, work):
        self.work = work
        self.used = set()
        self.reused = 0

    @staticmethod
    def _dispatch_head(key):
        return key.startswith("stage:node-dispatch:") and key.count(":") == 2

    def load(self, key, *, once=False):
        if once and key in self.used:
            return None
        signature = digest(key)
        if self._dispatch_head(key):
            with storage_stage(self.work.root, self.work.identity, read_only=True) as db:
                value = self.work._record(db, key, signature).get("dispatch_head")
        else:
            value = self.work.load(key, signature)
        if value is None and key.startswith("response:"):
            with storage_stage(self.work.root, self.work.identity, read_only=True) as db:
                retained = provider_response(db, self.work, key, signature)
            if retained is not None:
                response, _ = retained
                try:
                    [choice] = response["choices"]
                    text, finish = choice["message"]["content"], choice["finish_reason"]
                    if not isinstance(text, str) or finish not in {"stop", "length"}:
                        raise ValueError("Index receipt requires complete text and finish status")
                except (ValueError, TypeError, KeyError) as error:
                    from ultrakb.mutation import RecoveryRequired

                    raise RecoveryRequired(
                        "The index response needs explicit receipt repair"
                    ) from error
                value = {
                    "text": text,
                    "finish_reason": "finished" if finish == "stop" else "max_output_reached",
                }
                self.save(key, value)
        if value is not None:
            self.used.add(key)
            self.reused += 1
        return value

    def save(self, key, result):
        signature = digest(key)
        if not self._dispatch_head(key):
            self.work.save(key, signature, result)
            self.used.add(key)
            return
        if (
            not isinstance(result, dict)
            or set(result) != {"count"}
            or type(result["count"]) is not int
        ):
            raise ValueError("Invalid index dispatch head")
        with storage_stage(self.work.root, self.work.identity) as db:
            previous, record = self.work._record_snapshot(db, key, signature)
            count = record.get("dispatch_head", {"count": 0})["count"]
            if result["count"] not in {count, count + 1}:
                raise PageResponseError(
                    "index_dispatch_changed", "Index dispatch head is not consecutive"
                )
            encoded = _json({**record, "dispatch_head": result}, bounded=True)
            with db.transaction() as connection:
                self.work._active(connection)
                self.work._write_record(connection, key, signature, previous, encoded)
        self.used.add(key)

    @contextmanager
    def request(self, key, stage):
        signature = digest(key)
        self.work.require_request(key, signature)
        invocation = self.work.claim_generation(key, signature, stage="document_index." + stage)
        with model_invocation(self.work, invocation):
            yield
