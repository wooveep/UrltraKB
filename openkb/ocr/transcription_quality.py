"""Recognize a narrow repeated-output failure without inventing replacement text."""


def repetitive_transcription(text):
    lines = text.splitlines()
    for width in range(1, 9):
        repeats = 1
        for start in range(width, len(lines), width):
            part = lines[start : start + width]
            repeats = repeats + 1 if part == lines[start - width : start] else 1
            if repeats >= 32 and repeats * sum(len(line) + 1 for line in part) >= 1024:
                return True
    return False
