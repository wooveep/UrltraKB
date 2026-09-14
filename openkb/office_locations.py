"""Validate native Office coordinates without treating converted lines as originals."""

import re

TITLE_PLACEHOLDERS = {"TITLE", "CENTER_TITLE", "VERTICAL_TITLE"}


def _cell(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}", value):
        raise ValueError("Invalid original cell address")
    from openpyxl.utils.cell import coordinate_to_tuple

    row, column = coordinate_to_tuple(value)
    if row > 1048576 or column > 16384:
        raise ValueError("Original cell address out of bounds")
    return row, column


def validate_spreadsheet_location(location):
    if (
        not isinstance(location.get("sheet"), str)
        or not location["sheet"]
        or type(location.get("sheet_index")) is not int
        or location["sheet_index"] < 1
    ):
        raise ValueError("Invalid original worksheet identity")
    if "cell_address" in location:
        row, column = _cell(location["cell_address"])
    if "cell_range" in location:
        region = location["cell_range"]
        if not isinstance(region, str) or region.count(":") != 1 or "cell_address" not in location:
            raise ValueError("Invalid original cell range")
        first, last = map(_cell, region.split(":"))
        if not first[0] <= row <= last[0] or not first[1] <= column <= last[1]:
            raise ValueError("Original cell does not belong to its range")


def validate_presentation_location(location):
    if type(location.get("slide")) is not int or location["slide"] < 1:
        raise ValueError("Invalid original slide number")
    if "object_id" in location and (
        type(location["object_id"]) is not int or location["object_id"] < 1
    ):
        raise ValueError("Invalid original slide object")
    if {"title_object_id", "title_placeholder_count"} & location.keys():
        count, title = location.get("title_placeholder_count"), location.get("title_object_id")
        if (
            not {"title_object_id", "title_placeholder_count"} <= location.keys()
            or type(count) is not int
            or count < 0
            or (count == 1 and (type(title) is not int or title < 1))
            or (count != 1 and title is not None)
        ):
            raise ValueError("Invalid native title placeholder identity")
    if "placeholder_type" in location:
        role = location["placeholder_type"]
        roles = {
            "BITMAP",
            "BODY",
            "CENTER_TITLE",
            "CHART",
            "DATE",
            "FOOTER",
            "HEADER",
            "MEDIA_CLIP",
            "OBJECT",
            "ORG_CHART",
            "PICTURE",
            "SLIDE_IMAGE",
            "SLIDE_NUMBER",
            "SUBTITLE",
            "TABLE",
            "TITLE",
            "VERTICAL_BODY",
            "VERTICAL_OBJECT",
            "VERTICAL_TITLE",
        }
        if "object_id" not in location or (
            role is not None and (not isinstance(role, str) or role not in roles)
        ):
            raise ValueError("Invalid native placeholder type")
        if "title_placeholder_count" in location:
            is_title = role in TITLE_PLACEHOLDERS
            count, title = location["title_placeholder_count"], location["title_object_id"]
            if (is_title and count == 0) or (
                count == 1 and (location["object_id"] == title) != is_title
            ):
                raise ValueError("Native placeholder type conflicts with title identity")
    if "bbox" in location and location.get("coordinate_unit") != "emu":
        raise ValueError("Slide object coordinates require explicit EMU units")
    if "coordinate_unit" in location and location["coordinate_unit"] != "emu":
        raise ValueError("Invalid slide coordinate unit")
    groups = location.get("group_ids", [])
    if (
        not isinstance(groups, list)
        or any(type(v) is not int or v < 1 for v in groups)
        or len(set(groups)) != len(groups)
        or location.get("object_id") in groups
    ):
        raise ValueError("Invalid original slide group hierarchy")
