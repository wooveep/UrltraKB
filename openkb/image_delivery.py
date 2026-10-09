"""Bind prepared image occurrences to the actual answer requests that carried them."""

import copy
import json
import threading
import uuid


class ImageDeliveries:
    def __init__(self) -> None:
        self._records: dict[str, tuple[dict, dict]] = {}
        self._guard = threading.RLock()

    def prepare(self, occurrence: dict) -> dict:
        with self._guard:
            receipt = {
                **occurrence,
                "delivery_id": uuid.uuid4().hex,
                "status": "delivery_pending",
                "provider_delivery": "unconfirmed",
                "attempts": [],
            }
            receipt.pop("reason_code", None)
            self._records[receipt["delivery_id"]] = occurrence, receipt
            if occurrence["status"] != "pixels_delivered":
                occurrence.pop("reason_code", None)
                occurrence.update(receipt)
            return copy.deepcopy(receipt)

    def capture(self, items) -> tuple[str, ...]:
        """Only registered tool output identities can establish an image read attempt."""
        found = set()
        for item in items if isinstance(items, list) else ():
            if not isinstance(item, dict) or item.get("type") != "function_call_output":
                continue
            output = item.get("output")
            for part in output if isinstance(output, list) else ():
                if not isinstance(part, dict) or part.get("type") != "input_text":
                    continue
                try:
                    metadata = json.loads(part["text"])
                except (KeyError, TypeError, ValueError):
                    continue
                if isinstance(metadata, dict) and isinstance(metadata.get("delivery_id"), str):
                    found.add(metadata["delivery_id"])
        with self._guard:
            return tuple(sorted(found & self._records.keys()))

    def fail_read(self, occurrence: dict, code: str) -> None:
        with self._guard:
            prepared = self.prepare(occurrence)
            _, receipt = self._records[prepared["delivery_id"]]
            receipt.update(status="failed", provider_delivery="not_sent", reason_code=code)
            if occurrence["status"] != "pixels_delivered":
                occurrence.update(copy.deepcopy(receipt))

    def settle(self, identities, observed, *, accepted: bool) -> None:
        request_ids = list(observed.sent_ids) if observed else []
        digests = observed.image_digests if observed else None
        response_status = observed.response_status if observed else None
        with self._guard:
            for identity in identities:
                occurrence, receipt = self._records[identity]
                if (
                    observed is None
                    or (not request_ids and observed.send_error is not None)
                    or (digests is not None and receipt["content_digest"] not in digests)
                ):
                    status, delivery, reason = "failed", "not_sent", "image_not_sent"
                elif response_status is not None and response_status >= 400:
                    status, delivery, reason = "failed", "rejected", "image_request_rejected"
                elif accepted and digests is not None:
                    status, delivery, reason = "pixels_delivered", "confirmed", None
                else:
                    status, delivery, reason = (
                        "delivery_unknown",
                        "unknown",
                        "image_delivery_unknown",
                    )
                attempt = {
                    "request_ids": request_ids,
                    "status": status,
                    "provider_delivery": delivery,
                }
                if reason:
                    attempt["reason_code"] = reason
                if attempt not in receipt["attempts"]:
                    receipt["attempts"].append(attempt)
                if receipt["status"] != "pixels_delivered":
                    receipt.update(status=status, provider_delivery=delivery)
                    receipt.pop("reason_code", None)
                    if reason:
                        receipt["reason_code"] = reason
                if occurrence.get("delivery_id") == identity or status == "pixels_delivered":
                    occurrence.pop("reason_code", None)
                    occurrence.update(copy.deepcopy(receipt))

    def finish(self) -> tuple[dict, ...]:
        with self._guard:
            pending = [
                key
                for key, (_, value) in self._records.items()
                if value["status"] == "delivery_pending"
            ]
            self.settle(pending, None, accepted=False)
            return self.snapshot()

    def snapshot(self) -> tuple[dict, ...]:
        with self._guard:
            return tuple(copy.deepcopy(receipt) for _, receipt in self._records.values())
