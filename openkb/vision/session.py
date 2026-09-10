"""On-demand visual observations remain text, with original-image and evidence provenance."""

from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import io
import json
import re
import time
from dataclasses import asdict
from pathlib import Path

from PIL import Image

from openkb.evidence import Evidence
from openkb.locks import atomic_write_json, kb_ingest_lock
from openkb.model_outputs import write_model_output
from openkb.processing import external_request_usage, processing_checkpoint
from openkb.sources import SourceStore, content_id, read_object
from openkb.vision.connection import capability_verified, resolve_connection
from openkb.vision.transport import request_image


class VisionSession:
    def __init__(self, kb_dir: Path):
        self.root = kb_dir
        self.connection = resolve_connection(kb_dir)
        from openkb.config_state import active_values

        captured = active_values(kb_dir)
        self.verified = (
            captured.get("image_capability", False)
            if captured
            else capability_verified(self.connection)
        )
        self.started = time.monotonic()
        self.requests = self.tokens = 0
        self.lock = asyncio.Lock()

    async def analyze(
        self, image_path: str, question: str, region: list[int] | None = None
    ) -> dict:
        async with self.lock:
            return await self._analyze(image_path, question, region)

    async def _analyze(self, image_path, question, region):
        settings = self.connection.settings
        if not settings.enabled:
            return {"status": "image_understanding_disabled"}
        processing_checkpoint()
        if error := self.connection.readiness():
            return {"status": error}
        if not self.verified:
            return {"status": "image_connection_untested"}
        if not question.strip() or len(question) > 8000:
            return {"status": "image_question_invalid"}
        from openkb.agent.tools import read_wiki_image

        limits = settings.limits
        result = read_wiki_image(image_path, str(self.root / "wiki"), max_bytes=limits.image_bytes)
        if result["type"] != "image":
            return {"status": "image_unavailable", "reason": result["text"]}
        original = base64.b64decode(result["image_url"].split(",", 1)[1])
        try:
            with Image.open(io.BytesIO(original)) as image:
                if image.width * image.height > 32_000_000:
                    return {"status": "image_pixel_budget_exceeded"}
                if region is not None:
                    if (
                        len(region) != 4
                        or not all(type(v) is int for v in region)
                        or not (
                            0 <= region[0] < region[2] <= image.width
                            and 0 <= region[1] < region[3] <= image.height
                        )
                    ):
                        return {"status": "image_region_invalid"}
                    image = image.crop(tuple(region))
                stream = io.BytesIO()
                image.convert("RGB").save(stream, format="PNG")
                png = stream.getvalue()
                if len(png) > limits.image_bytes:
                    return {"status": "image_byte_budget_exceeded"}
        except (OSError, ValueError):
            return {"status": "image_invalid"}
        store = SourceStore(self.root)
        original_id = hashlib.sha256(original).hexdigest()
        image_id = hashlib.sha256(png).hexdigest()
        provenance = image_provenance(self.root, original_id)
        identity = {
            "kind": "visual_observation",
            "original_image": original_id,
            "image": image_id,
            "path": image_path,
            "region": region,
            "question": question,
            "connection": self.connection.identity,
            "output_tokens": limits.output_tokens,
            "policy": "enabled-v1",
            "source_evidence": provenance,
        }
        cache = store.owned_path(
            self.root / ".openkb/visual-observations" / f"{content_id(identity)}.json"
        )
        if cache.exists():
            cached = read_object(cache)
            if cached.get("identity") == identity and cached.get("status") == "completed":
                return {key: value for key, value in cached.items() if key != "original_bytes"}
        remaining = limits.seconds - (time.monotonic() - self.started)
        if (
            remaining <= 0
            or self.requests >= limits.max_requests
            or self.tokens + limits.output_tokens > limits.max_tokens
        ):
            return {"status": "image_budget_exhausted"}
        self.requests += 1
        self.tokens += limits.output_tokens
        with external_request_usage(limits.output_tokens, "image_understanding") as usage:
            result = await request_image(
                self.connection, png, question, seconds=min(remaining, limits.request_seconds)
            )
            if "tokens" in result:
                usage["tokens"] = result["tokens"]
        self.tokens += max(0, result.get("tokens", 0) - limits.output_tokens)
        processing_checkpoint()
        if self.tokens > limits.max_tokens:
            result = {**result, "status": "image_budget_exhausted"}
        observation = {
            **result,
            "identity": identity,
            "source_evidence": provenance,
            "interpretation": "Visual model observation; not a quotation or OCR transcription.",
        }
        if result["status"] == "completed":
            saved = {**observation, "original_bytes": base64.b64encode(original).decode("ascii")}
            if not write_model_output(
                self.root, cache, json.dumps(saved, ensure_ascii=False), artifact=False
            ):
                with kb_ingest_lock(self.root / ".openkb"):
                    atomic_write_json(cache, saved)
        return observation


def image_provenance(root: Path, image_id: str) -> list[dict]:
    """Read immutable evidence carried by materialized notes under the task lease."""
    provenance = []
    for source in sorted((root / "wiki/sources").glob("*.md")):
        processing_checkpoint()
        if source.is_symlink() or source.stat().st_size > 8_000_000:
            continue
        parts = re.split(r"<!-- source-evidence: (.+?) -->", source.read_text(encoding="utf-8"))
        for position in range(1, len(parts), 2):
            if image_id not in parts[position + 1]:
                continue
            try:
                reference = Evidence(**ast.literal_eval(parts[position]))
                provenance.append(asdict(reference))
            except (ValueError, SyntaxError, TypeError):
                continue
    return provenance


def image_tools(kb_dir: Path):
    """Expose the same textual visual interface to knowledge agents of any modality."""
    from agents import function_tool

    session = VisionSession(kb_dir)
    if not session.connection.settings.enabled:
        return (
            [],
            "Image understanding is disabled. Work from text and identify uninterpreted images.",
        )

    @function_tool
    async def get_image(image_path: str, question: str, region: list[int] | None = None) -> str:
        """Ask the configured visual model about one needed figure and receive a cited observation.

        Args:
            image_path: Existing wiki-relative or source-note-relative image path.
            question: The specific visual question needed for this task.
            region: Optional crop in original pixels: left, top, right, bottom.
        """
        import json

        return json.dumps(await session.analyze(image_path, question, region), ensure_ascii=False)

    return [get_image], (
        "When visual information is needed, use get_image with the image path and question. "
        "Its text is a visual observation, not a source quotation or OCR transcription. "
        "Cite the original image and source_evidence; preserve uncertainty and unavailable regions."
    )
