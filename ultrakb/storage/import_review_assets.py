"""Recognize retained image relocation without relaxing human content protection."""

import posixpath
from pathlib import PurePosixPath

from ultrakb.image_media import knowledge_image_media_type
from ultrakb.markdown_images import image_references

from .blobs import read_bytes


def only_image_relocations(db, path, before, after):
    """Compare exact text after resolving local image URLs in each fixed manifest."""
    if path not in before or path not in after:
        return False

    def resolved(files):
        text = read_bytes(db, files[path]).decode("utf-8")
        parts: list[object] = []
        cursor = 0
        for reference in image_references(text):
            if reference.local_path is None:
                continue
            logical = posixpath.normpath(str(PurePosixPath(path).parent / reference.local_path))
            digest = files.get(logical)
            if digest is not None:
                try:
                    media_type = knowledge_image_media_type(logical)
                except ValueError:
                    continue
                parts.extend(
                    (
                        text[cursor : reference.start],
                        (digest, media_type, reference.relocated("", preserve_suffix=True)),
                    )
                )
                cursor = reference.end
        parts.append(text[cursor:])
        # Resolved references are tuples; unresolved URLs remain literal text.
        # No user-authored URL can collide with a resolved-content marker.
        return tuple(parts)

    return resolved(before) == resolved(after)
