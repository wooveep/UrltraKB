"""Retry completed length stops with strictly smaller, lossless evidence batches."""

from openkb.log import logger
from openkb.processing import InputTooLarge, OutputTruncated, processing_checkpoint
from openkb.sources import content_id


def halves(batch):
    if len(batch) < 2:
        return []
    middle = len(batch) // 2
    return [batch[:middle], batch[middle:]]


def retry_batches(batch, operation, *, stage, on_event, split=halves):
    """Recover capacity failures; never replay an uncertain transport."""
    pending = [batch]
    while pending:
        processing_checkpoint(stage)
        current = pending.pop()
        try:
            result = operation(current)
        except (OutputTruncated, InputTooLarge) as exc:
            parts = split(current)
            if not parts:
                raise  # Smallest meaningful unit also exceeded the model ceiling.
            logger.info("%s [%s]; splitting batch of %s", exc.reason, stage, len(current))
            on_event(
                {
                    "stage": stage,
                    "operation": "split_batch",
                    "items": len(current),
                    "batches": len(parts),
                    "reason": exc.reason,
                }
            )
            pending.extend(reversed(parts))
        else:
            yield current, result


def split_span(value):
    """Partition original character positions, retaining asset and context associations."""
    text = value["text"]
    if len(text) < 2:
        return []
    middle = len(text) // 2
    boundary = text.rfind("\n", 0, middle)
    if boundary > middle // 2:
        middle = boundary + 1
    start = value["reference"]["start"]
    parts = []
    for left, right in ((0, middle), (middle, len(text))):
        reference = {**value["reference"], "start": start + left, "end": start + right}
        part = {**value, "reference": reference, "text": text[left:right]}
        neighbors = list(value.get("neighbors", []))
        for begin, end, relation in (
            (max(0, left - 128), left, "previous_span"),
            (right, min(len(text), right + 128), "following_span"),
        ):
            if end > begin:
                neighbors.append(
                    {
                        "reference": {**reference, "start": start + begin, "end": start + end},
                        "text": text[begin:end],
                        "location": value["location"],
                        "context": value.get("context", ""),
                        "relation": relation,
                    }
                )
        part["neighbors"] = neighbors
        if "span" in value:  # Extraction units have their own immutable identity.
            part["span"] = {**value["span"], "start": start + left, "end": start + right}
            part.pop("id", None)
            part = {"id": content_id(part), **part}
        parts.append(part)
    return parts


def split_units(batch):
    return halves(batch) or [[part] for part in split_span(batch[0])]


def split_generation(batch):
    if len(batch) > 1:
        return halves(batch)
    fact, evidence = batch[0]
    # A fact is indivisible: retain its quote and identity while narrowing the
    # reread window, exactly as the normal generation window planner does.
    return [[(fact, part)] for part in split_span(evidence)]
