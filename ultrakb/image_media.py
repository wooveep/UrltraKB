"""The supported media boundary shared by fixed knowledge readers and imports."""

from pathlib import PurePosixPath


def knowledge_image_media_type(path: str) -> str:
    media_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }
    try:
        return media_types[PurePosixPath(path).suffix.lower()]
    except KeyError as error:
        raise ValueError("Unsupported knowledge image format") from error
