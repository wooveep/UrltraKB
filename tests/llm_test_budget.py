"""Shared, restart-safe CNY admission ledger for real DeepSeek test requests.

Admissions retain peak-price costs even outside peak hours, so the complete
suite's allowance does not depend on when it starts. Reservations assume no
provider cache hits. A failed or
interrupted request retains its reservation until a real usage receipt arrives.
Only counts, test identifiers, request fingerprints and prices belong in this
file, never prompts or credentials. Integer nanoyuan avoid rounding past the limit.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import portalocker

_SCALE = 1_000_000_000
_PEAK_HIT = 40
_PEAK_MISS = 2000
_PEAK_OUTPUT = 8000


class BudgetExceeded(RuntimeError):
    """The requested send would exceed this full run's two-yuan limit."""


class TestRunBudget:
    __test__ = False

    def __init__(self, path: Path, *, limit_cny: Decimal = Decimal("2")):
        if not limit_cny.is_finite() or not 0 < limit_cny <= 2:
            raise ValueError("A full test run must have a finite budget in (0, 2] CNY")
        self.path = path.resolve()
        self.limit = int(limit_cny * _SCALE)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as state:
            if state["limit_nanoyuan"] != self.limit:
                raise ValueError("An existing test-run budget cannot be changed")

    @contextmanager
    def _transaction(self):
        with self._lock, portalocker.Lock(str(self.path) + ".lock", timeout=30):
            state = (
                json.loads(self.path.read_text())
                if self.path.exists()
                else {
                    "limit_nanoyuan": self.limit,
                    "peak_guard": True,
                    "halted": False,
                    "entries": [],
                }
            )
            try:
                yield state
            finally:
                descriptor, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".cost-")
                try:
                    with os.fdopen(descriptor, "w") as stream:
                        json.dump(state, stream, ensure_ascii=False, indent=2)
                        stream.write("\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, self.path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)

    @staticmethod
    def _tokens(value):
        if type(value) is not int or value < 0:
            raise ValueError("Token counts must be nonnegative integers")
        return value

    @staticmethod
    def _exposure(state):
        return sum(
            max(
                entry.get("actual_nanoyuan", entry["reserved_nanoyuan"]),
                entry.get("peak_nanoyuan", 0) if state.get("peak_guard") else 0,
            )
            for entry in state["entries"]
        )

    def reserve(self, *, input_tokens: int, output_tokens: int, fingerprint="") -> int:
        input_tokens, output_tokens = self._tokens(input_tokens), self._tokens(output_tokens)
        reserved = input_tokens * _PEAK_MISS + output_tokens * _PEAK_OUTPUT
        with self._transaction() as state:
            if state.get("peak_guard") is not True:
                raise BudgetExceeded(
                    "A legacy ledger cannot admit paid sends; use a new peak-tariff test-run ledger"
                )
            if state["halted"] or self._exposure(state) + reserved > self.limit:
                raise BudgetExceeded("Full test-run LLM budget of 2.00 CNY exhausted before send")
            identifier = len(state["entries"])
            state["entries"].append(
                {
                    "id": identifier,
                    "fingerprint": fingerprint,
                    # Parameter IDs can contain fixture text; keep only the
                    # test function name when attributing actual provider cost.
                    "test": os.environ.get("PYTEST_CURRENT_TEST", "").split("[")[0].split(" (")[0],
                    "reserved_input_tokens": input_tokens,
                    "reserved_output_tokens": output_tokens,
                    "reserved_nanoyuan": reserved,
                    "state": "reserved",
                }
            )
            return identifier

    def settle(self, identifier: int, usage: dict, *, peak: bool) -> None:
        prompt = self._tokens(usage["prompt_tokens"])
        output = self._tokens(usage["completion_tokens"])
        details = usage.get("prompt_tokens_details") or {}
        hit = self._tokens(usage.get("prompt_cache_hit_tokens", details.get("cached_tokens", 0)))
        miss = self._tokens(usage.get("prompt_cache_miss_tokens", prompt - hit))
        if hit + miss != prompt:
            raise ValueError("Provider cache counts do not match its prompt token total")
        peak_price = hit * _PEAK_HIT + miss * _PEAK_MISS + output * _PEAK_OUTPUT
        price = peak_price if peak else peak_price // 2
        exceeded = False
        with self._transaction() as state:
            entry = state["entries"][identifier]
            if "actual_nanoyuan" in entry:
                return
            entry.update(
                {
                    "actual_nanoyuan": price,
                    "peak_nanoyuan": peak_price,
                    "state": "settled",
                    "period": "peak" if peak else "idle",
                    "input_cache_hit_tokens": hit,
                    "input_cache_miss_tokens": miss,
                    "output_tokens": output,
                }
            )
            admitted_price = peak_price if state.get("peak_guard") else price
            if admitted_price > entry["reserved_nanoyuan"] or self._exposure(state) > self.limit:
                state["halted"] = exceeded = True
        if exceeded:
            raise BudgetExceeded("Real provider usage exceeded the pre-send reservation")

    def mark_sent(self, identifier: int, fingerprint: str) -> None:
        """Consume a reservation once at the actual outgoing transport boundary."""
        with self._transaction() as state:
            if state.get("peak_guard") is not True:
                raise BudgetExceeded(
                    "A legacy ledger cannot admit paid sends; use a new peak-tariff test-run ledger"
                )
            entry = state["entries"][identifier]
            if entry["state"] != "reserved" or entry["fingerprint"] != fingerprint:
                raise BudgetExceeded("A model transport must consume its own unused reservation")
            entry.update(state="sent", sent=True)

    def reject_before_send(self, identifier: int, reason: str) -> None:
        with self._transaction() as state:
            entry = state["entries"][identifier]
            if entry.get("sent") or "actual_nanoyuan" in entry:
                raise ValueError("An actual model send cannot be refunded without a receipt")
            entry.update(state="not_sent", reason=reason, actual_nanoyuan=0)

    def uncertain(self, identifier: int, reason: str) -> None:
        with self._transaction() as state:
            entry = state["entries"][identifier]
            if "actual_nanoyuan" not in entry:
                entry.update(state="unknown", reason=reason)

    def report(self) -> dict:
        with self._transaction() as state:
            entries = state["entries"]
            return {
                "model": "deepseek-flash",
                "limit_cny": str(Decimal(self.limit) / _SCALE),
                "actual_cny": str(
                    Decimal(sum(e.get("actual_nanoyuan", 0) for e in entries)) / _SCALE
                ),
                "exposure_cny": str(Decimal(self._exposure(state)) / _SCALE),
                "admission_basis": "peak_tariff" if state.get("peak_guard") else "request_tariff",
                "all_peak_cny": str(
                    Decimal(
                        sum(
                            e.get("input_cache_hit_tokens", 0) * _PEAK_HIT
                            + e.get("input_cache_miss_tokens", 0) * _PEAK_MISS
                            + e.get("output_tokens", 0) * _PEAK_OUTPUT
                            for e in entries
                        )
                    )
                    / _SCALE
                ),
                "requests": len(entries),
                "provider_requests": sum(
                    e.get("sent", False) or e["state"] == "settled" for e in entries
                ),
                "rejected_before_send": sum(e["state"] == "not_sent" for e in entries),
                "unknown_requests": sum("actual_nanoyuan" not in e for e in entries),
                "halted": state["halted"],
                "usage": {
                    key: sum(e.get(key, 0) for e in entries)
                    for key in (
                        "input_cache_hit_tokens",
                        "input_cache_miss_tokens",
                        "output_tokens",
                    )
                },
            }

    @property
    def actual_cny(self) -> Decimal:
        return Decimal(self.report()["actual_cny"])

    @property
    def exposure_cny(self) -> Decimal:
        return Decimal(self.report()["exposure_cny"])
