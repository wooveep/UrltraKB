"""Per-question image occurrences and bounded, verified pixel deliveries."""

import hashlib
import io
import json

from agents import ToolOutputImage, ToolOutputText
from PIL import Image

from openkb.agent.tools import read_wiki_image
from openkb.application.query_views import QueryView, read_selected_file
from openkb.source_reading import ReaderFailure


class ImageCoverage:
    def __init__(self, view: QueryView):
        self.view = view
        self.occurrences: dict[str, dict] = {}
        self.deliveries = 0
        self.pixels = 0
        self.context_tokens = 0

    def listed(self, source: dict, units: list[dict]) -> dict:
        listed = []
        for unit in units:
            for ordinal, image in enumerate(unit.get("images", [])):
                identity = hashlib.sha256(
                    json.dumps(
                        [
                            self.view.view_id,
                            source["content_digest"],
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
                        "reading_action": {"tool": "get_image", "arguments": {"image_path": key}},
                    },
                )
                listed.append(dict(record))
        return {
            "occurrences": listed[:6],
            "omitted": max(0, len(listed) - 6),
            "coverage": "pixels not read until explicitly delivered",
            "reading_action": source["reading_action"],
        }

    def read(self, image_path: str) -> list[ToolOutputText | ToolOutputImage]:
        record = self.occurrences.get(image_path)
        if record is None:
            matches = [r for r in self.occurrences.values() if r["path"] == image_path]
            if len(matches) > 1:
                raise ReaderFailure(
                    "ambiguous_occurrence",
                    "Use the occurrence action returned by the source reader.",
                    matches[0]["reading_action"],
                )
            record = (
                matches[0]
                if matches
                else {"path": image_path, "occurrence_id": None, "status": "unread"}
            )
        path = record["path"]
        path = path if path in self.view.files else f"sources/{path}"
        try:
            data = read_selected_file(self.view, path)
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
                pixels = width * height
                tokens = 85 + 170 * ((width + 511) // 512) * ((height + 511) // 512)
                if (
                    self.deliveries >= 6
                    or self.pixels + pixels > 16_000_000
                    or len(data) > 8_000_000
                ):
                    raise ReaderFailure(
                        "image_capacity", "Image delivery capacity reached; narrow the question."
                    )
            result = read_wiki_image(
                path, str(self.view.scope.wiki_dir), expected_digest=self.view.files[path]
            )
            if result["type"] != "image":
                raise ReaderFailure("read_failed", "Image pixels could not be read.")
            output = ToolOutputImage(image_url=result["image_url"])
        except Exception:
            record["status"] = "failed"
            raise
        self.deliveries += 1
        self.pixels += pixels
        self.context_tokens += tokens
        record.update(
            status="pixels_delivered",
            content_digest=self.view.files[path],
            width=width,
            height=height,
        )
        return [ToolOutputText(text=json.dumps(record, ensure_ascii=False)), output]
