"""Original-image derivations retained by parsing, never guessed from display labels."""

from __future__ import annotations

import copy


def validate_image_relations(value):
    from openkb.sources import valid_id

    invalid = ValueError("Invalid source image relations")
    if not isinstance(value, list) or not value:
        raise invalid
    for relation in value:
        if (
            not isinstance(relation, dict)
            or set(relation) - {"original_asset", "source_alt", "frames"}
            or not {"original_asset", "frames"} <= set(relation)
            or not isinstance(relation["frames"], list)
            or ("source_alt" in relation and not isinstance(relation["source_alt"], str))
        ):
            raise invalid
        valid_id(relation["original_asset"])
        numbers = set()
        for frame in relation["frames"]:
            if (
                not isinstance(frame, dict)
                or set(frame) != {"number", "asset", "ocr_assets"}
                or type(frame["number"]) is not int
                or frame["number"] < 1
                or frame["number"] in numbers
                or not isinstance(frame["ocr_assets"], list)
            ):
                raise invalid
            numbers.add(frame["number"])
            valid_id(frame["asset"])
            for asset in frame["ocr_assets"]:
                valid_id(asset)
            if len(set(frame["ocr_assets"])) != len(frame["ocr_assets"]):
                raise invalid


def with_image_relations(context, relations):
    """Retain table context while adding the exact images read in this block."""
    if not relations:
        return context
    return {
        **copy.deepcopy(context or {"source_excerpts": [], "structure": {}, "reader_status": {}}),
        "image_relations": copy.deepcopy(relations),
    }


def validate_image_bindings(context, assets):
    """A derivation cannot borrow a different block's unbound image identity."""
    for relation in (context or {}).get("image_relations", []):
        referenced = {relation["original_asset"]}
        for frame in relation["frames"]:
            referenced.add(frame["asset"])
            referenced.update(frame["ocr_assets"])
        if not referenced <= set(assets):
            raise ValueError("Source image relation is not bound to this block")


def image_origin(context, asset):
    """Project recorded origins for one asset; legacy parses stay explicitly unknown."""
    roles = set()
    extent = None
    for relation in (context or {}).get("image_relations", []):
        original = relation["original_asset"]
        if asset == original:
            roles.add("original_image")
            extent = "whole_original_image"
        for frame in relation["frames"]:
            if asset == frame["asset"]:
                roles.add("display_frame")
                extent = extent or "whole_rendered_frame"
            if asset in frame["ocr_assets"]:
                roles.add("ocr_output")
    if not roles:
        return {}
    # Full descriptions and derivations belong to the paginated context stream.
    return {
        "provenance": [{"role": role} for role in sorted(roles)],
        **({"extent": extent} if extent else {}),
    }
