"""Per-question figure locations for readers; no model pixel delivery."""

import hashlib
import json

from ultrakb.application.query_views import QueryView
from ultrakb.image_delivery import ImageDeliveries


class ImageCoverage:
    def __init__(self, view: QueryView, *, executor=None):
        self.view = view
        self.executor = executor
        self.receipts = executor.image_deliveries if executor else ImageDeliveries()
        self.occurrences: dict[str, dict] = {}

    def listed(self, source: dict, units: list[dict]) -> dict:
        listed = []
        for unit in units:
            for ordinal, image in enumerate(unit.get("images", [])):
                ordinal = unit.get("image_ordinals", list(range(len(unit["images"]))))[ordinal]
                identity = hashlib.sha256(
                    json.dumps(
                        [
                            self.view.view_id,
                            source["content_digest"],
                            source["path"],
                            unit["page"],
                            ordinal,
                            image["path"],
                        ]
                    ).encode()
                ).hexdigest()[:24]
                key = f"occurrence:{identity}"
                record = self.occurrences.setdefault(
                    key,
                    {
                        "occurrence_id": key,
                        "path": image["path"],
                        "page": unit["page"],
                        "source_path": source["path"],
                        "source_revision_id": source["source_revision_id"],
                        "location_listed": True,
                        "status": "unread",
                        "interpreted": False,
                        "interpretation": "Original retained; not interpreted by the text model.",
                    },
                )
                self.receipts.list_location(record)
                listed.append(dict(record))
        return {
            "occurrences": listed,
            "omitted": sum(unit.get("omitted_page_images", 0) for unit in units),
            "coverage": "Original images retained for readers; image semantics remain unknown",
            "reading_action": source["reading_action"],
        }
