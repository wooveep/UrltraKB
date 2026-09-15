"""Share optional image recognition by original image identity, never by OCR text."""

from dataclasses import asdict

from openkb.evidence import BlockDraft
from openkb.locks import atomic_write_json
from openkb.ocr.assembly import assembly_profile
from openkb.sources import content_id, read_object, valid_id


class ImageCache:
    def __init__(self, store, settings, image, page, options):
        self.store = store
        self.identity = {
            "image": valid_id(image),
            "page": page,
            "recognition": settings.profile(),
            "assembly": assembly_profile(settings.backend),
            "retry": (options.get("retries") or {}).get(page),
        }
        self.path = store.owned_path(store.root / "image-ocr" / f"{content_id(self.identity)}.json")

    def load(self):
        try:
            record = read_object(self.path)
            result = record.get("result")
            if (
                record.get("input") != self.identity
                or not isinstance(result, dict)
                or content_id(result) != record.get("digest")
                or not isinstance(result.get("blocks"), list)
                or (result.get("reason") is not None and not isinstance(result["reason"], str))
            ):
                return None
            blocks = [
                BlockDraft(**{**block, "assets": tuple(block["assets"])})
                for block in result["blocks"]
            ]
            for block in blocks:
                for asset in block.assets:
                    self.store.asset(asset)
            return blocks, result["reason"]
        except (ValueError, KeyError, TypeError, FileNotFoundError):
            return None

    def save(self, blocks, reason):
        # Never turn a transient/unknown remote request into a completed result.
        # Partial but useful recognitions retain their quality notice too.
        if not isinstance(blocks, list) or not all(isinstance(b, BlockDraft) for b in blocks):
            return
        if reason is not None and (not isinstance(reason, str) or not blocks):
            return
        result = {"blocks": [asdict(block) for block in blocks], "reason": reason}
        atomic_write_json(
            self.path, {"input": self.identity, "result": result, "digest": content_id(result)}
        )
