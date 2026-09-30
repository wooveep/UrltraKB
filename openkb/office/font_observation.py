"""Associate actual PDF glyph fonts with unambiguous Writer text runs."""

import unicodedata
from collections import Counter


def _wanted(portion: dict, character: str) -> str:
    name = unicodedata.name(character, "")
    if name.startswith(("CJK", "HIRAGANA", "KATAKANA", "HANGUL", "BOPOMOFO", "YI ")):
        return portion["requested_asian"] or portion["requested"]
    if unicodedata.bidirectional(character) in {"AL", "R", "AN"}:
        return portion["requested_complex"] or portion["requested"]
    return portion["requested"]


def _compact(text: str) -> str:
    return "".join(character for character in text if not character.isspace())


def _canonical(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def observe_fonts(requested: list[dict], spans: list[dict]) -> dict:
    glyphs = [
        (character, span["font"])
        for span in spans
        for character in span["text"]
        if not character.isspace()
    ]
    rendered = "".join(character for character, _ in glyphs)
    counts = Counter(_compact(portion["text"]) for portion in requested)
    matches = []
    for portion in requested:
        text = _compact(portion["text"])
        start = rendered.find(text) if text else -1
        ambiguous = bool(text) and (counts[text] > 1 or rendered.find(text, start + 1) >= 0)
        status = "unmatched" if start < 0 else "ambiguous" if ambiguous else "matched"
        matches.append((start, start + len(text), status))
    # Unique substrings are not proof if multiple runs claim the same glyphs:
    # a deleted run may occur inside an unrelated visible sentence.
    conflicts: set[int] = set()
    group: list[int] = []
    right = -1
    for start, end, index in sorted(
        (start, end, index) for index, (start, end, status) in enumerate(matches) if start >= 0
    ):
        if start >= right:
            if len(group) > 1:
                conflicts.update(group)
            group = []
        group.append(index)
        right = max(right, end)
    if len(group) > 1:
        conflicts.update(group)
    substitutions, observations = [], []
    for index, portion in enumerate(requested):
        start, end, status = matches[index]
        if index in conflicts:
            status = "ambiguous"
        observation = {
            "requested": portion["requested"],
            "requested_asian": portion["requested_asian"],
            "requested_complex": portion["requested_complex"],
            "status": status,
            "characters": end - start,
            "actual": [],
        }
        if status == "matched":
            selected = glyphs[start:end]
            observation["actual"] = sorted({font for _, font in selected})
            groups: dict[tuple[str, str], list[str]] = {}
            for character, actual in selected:
                wanted = _wanted(portion, character)
                if wanted and _canonical(wanted) not in _canonical(actual):
                    groups.setdefault((wanted, actual), []).append(character)
            for (wanted, actual), characters in groups.items():
                substitutions.append(
                    {"requested": wanted, "actual": actual, "text": "".join(characters)[:160]}
                )
        # Unmatched input can be hidden/deleted text. Do not retain that text in
        # diagnostics; report its font names and unresolved observation instead.
        observations.append(observation)
    return {"font_substitutions": substitutions, "font_observations": observations}
