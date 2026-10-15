"""Shared accounting for compressed objects and the metadata needed to locate them."""

import time


class BudgetWait(ValueError):
    pass


class DiscoveryMeter:
    def __init__(self, group, check_stop):
        self.group = group
        self.check_stop = check_stop
        self.started = time.monotonic()
        self.decompressed = 0

    @property
    def remaining(self):
        return (
            self.group.budget.max_decompressed_bytes
            - self.group.decompressed_bytes
            - self.decompressed
        )

    def consume(self, count):
        self.decompressed += count
        self.check()

    def check(self):
        self.check_stop()
        if self.remaining < 0:
            raise BudgetWait("Decompression budget exhausted")
        if (
            self.group.discovery_seconds + time.monotonic() - self.started
            >= self.group.budget.max_discovery_seconds
        ):
            raise BudgetWait("Discovery time budget exhausted")

    def updated_group(self, payload_bytes=0, recovered=False):
        return self.group.model_copy(
            update={
                "sources": self.group.sources + int(recovered),
                "object_bytes": self.group.object_bytes + (payload_bytes if recovered else 0),
                "decompressed_bytes": self.group.decompressed_bytes + self.decompressed,
                "discovery_seconds": self.group.discovery_seconds + time.monotonic() - self.started,
            }
        )
