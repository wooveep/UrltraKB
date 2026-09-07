"""Private multipart-upload storage, separate from published KB raw files."""

import tempfile
from pathlib import Path

import anyio
from fastapi import HTTPException, UploadFile
from starlette.responses import StreamingResponse

from openkb.inputs import SUPPORTED_EXTENSIONS


class UploadStreamingResponse(StreamingResponse):
    """Own inputs even when disconnect precedes the iterator's first instruction."""

    def __init__(self, content, *, uploads, **kwargs):
        super().__init__(content, **kwargs)
        self.uploads = uploads

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await self.body_iterator.aclose()
                finally:
                    cleanup_uploads(self.uploads)


def reserve_upload(upload: UploadFile) -> tuple[Path, str]:
    name = Path(upload.filename or "").name
    if not name:
        raise HTTPException(status_code=400, detail="Uploaded file is missing a filename.")
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported file type: {suffix}. "
                f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
            ),
        )
    directory = Path(tempfile.mkdtemp(prefix="openkb-upload-"))
    return directory / name, name


def cleanup_uploads(uploads: list[tuple[Path, str]]) -> None:
    for path, _ in uploads:
        # Only private directories allocated above are owned by this request.
        if path.parent.parent != Path(tempfile.gettempdir()) or not path.parent.name.startswith(
            "openkb-upload-"
        ):
            raise ValueError("Not an owned upload path")
        path.unlink(missing_ok=True)
        if path.parent.exists():
            path.parent.rmdir()
