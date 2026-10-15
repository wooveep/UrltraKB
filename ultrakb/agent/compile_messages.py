"""Pure page request assembly; planning history stays outside the writing context."""

from ultrakb.text_source import clip_text_origins

_PAGE_WRITING = """Organize the supplied originals completely for this page's topic.
Retain facts, conditions and necessary explanations, with verifiable calculations,
comparisons and supported synthesis and their premises. Use the supplied original
labels and native positions; generated labels and the proposed page name help locate
the topic. Keep uncertainty local to an interpretation supported only by conjecture.
The previous page is an editable draft. The same source basis applies to the title,
description and entity type as to the body.
Organize each fact under its original object, version and applicable conditions.
Interpret table records with all supplied column headers. For a comparison retain
each object's own conditions; for a calculation show the given inputs and assumptions.
"""


def render_original_window(window: dict, selection: dict) -> dict:
    """Group only actually projected cell observations into sparse physical rows."""
    observations = []
    for start, end in window.get("source_spans", []):
        observations.extend(clip_text_origins(selection.get("origin_locators", []), start, end))
    groups: dict[str, dict[int, dict[int, dict]]] = {"worksheet_rows": {}, "csv_rows": {}}
    for origin in observations:
        location = origin.get("sheet_cell")
        csv_cell = origin.get("csv")
        if location:
            cell = location["cell"]
        elif csv_cell:
            cell = csv_cell
        else:
            continue
        row, column = cell.get("row"), cell.get("column")
        if type(row) is not int or type(column) is not int:
            continue
        rows = groups["worksheet_rows" if location else "csv_rows"]
        item = rows.setdefault(row, {}).setdefault(
            column,
            {"column": column, "observations": []}
            | ({"coordinate": cell["coordinate"]} if "coordinate" in cell else {}),
        )
        observation = {
            **cell,
            "span": origin["normalized_span"],
            "original_span": origin["original_span"],
            "kind": origin["kind"],
        }
        if (location or cell).get("value_complete") is False:
            observation["value_complete"] = False
            for key in ("value", "display", "formula", "cached"):
                observation.pop(key, None)
        if observation not in item["observations"]:
            item["observations"].append(observation)
    if not any(groups.values()):
        return window
    return {
        **{k: v for k, v in window.items() if k != "cells"},
        **{
            kind: [
                {"row": row, "cells": [cells[column] for column in sorted(cells)]}
                for row, cells in sorted(rows.items())
            ]
            for kind, rows in groups.items()
            if rows
        },
    }


def build_page_messages(
    *,
    system_message: dict,
    current_original: dict | None,
    original_context: dict,
    known_targets_message: dict,
    task_message: dict,
    update_message: dict | None = None,
) -> list[dict]:
    messages = [system_message]
    if current_original is not None:
        messages.append(current_original)
    messages.append(original_context)
    if update_message is not None:
        messages.append(update_message)
    messages.extend(
        [
            known_targets_message,
            {**task_message, "content": task_message["content"] + "\n" + _PAGE_WRITING},
        ]
    )
    return messages


def planning_messages(
    system_message: dict,
    document_message: dict,
    summary: str,
    *,
    concept_briefs: str,
    entity_briefs: str,
    entity_types: list[str] | None = None,
    long: bool = False,
) -> list[dict]:
    """Build the cached planning context; page-writing requests use originals."""
    from ultrakb.agent.compiler import _CONCEPTS_PLAN_USER, _ENTITY_TYPE_LIST, _cached_text
    from ultrakb.agent.page_figures import split_gallery
    from ultrakb.agent.source_windows import READING_PLAN_INSTRUCTION

    types = _ENTITY_TYPE_LIST if entity_types is None else entity_types
    return [
        system_message,
        document_message,
        {"role": "assistant", "content": _cached_text(split_gallery(summary)[0])},
        {
            "role": "user",
            "content": _CONCEPTS_PLAN_USER.format(
                concept_briefs=concept_briefs,
                entity_briefs=entity_briefs,
            ).replace("__ENTITY_TYPES__", ", ".join(types))
            + (READING_PLAN_INSTRUCTION if long else ""),
        },
    ]
