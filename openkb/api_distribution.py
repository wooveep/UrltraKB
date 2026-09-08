"""Public source/license access, independent of private knowledge-base routes."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.datastructures import MutableHeaders

from openkb.distribution import DistributionError, load_distribution

router = APIRouter(prefix="/api/v1/distribution", tags=["Source and licenses"])


@router.get("")
def distribution_summary(request: Request):
    try:
        summary = load_distribution().summary()
    except DistributionError as exc:
        raise HTTPException(503, "Matching release materials are unavailable or invalid") from exc
    for file in summary["files"]:
        file["download"] = request.url_for("distribution_file", name=file["name"]).path
    return summary


@router.get("/files/{name}", name="distribution_file")
def distribution_file(name: str):
    try:
        stream = load_distribution().open_file(name)
    except KeyError as exc:
        raise HTTPException(404, "Release file not found") from exc
    except DistributionError as exc:
        raise HTTPException(503, "Matching release file is unavailable or invalid") from exc
    return _ReleaseDownload(stream, name)


class _ReleaseDownload(StreamingResponse):
    """Close the verified snapshot even if a disconnect precedes body iteration."""

    def __init__(self, stream, name):
        self.stream = stream
        size = stream.seek(0, 2)
        stream.seek(0)
        super().__init__(
            iter(lambda: stream.read(1024 * 1024), b""),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "Content-Length": str(size),
            },
        )

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.stream.close()


def install_distribution_routes(app):
    app.include_router(router)
    app.add_middleware(_SourceLink)


class _SourceLink:
    """Advertise without buffering streams or changing ASGI task/context lifetimes."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def with_link(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                link = (
                    f"<{scope.get('root_path', '')}/api/v1/distribution>; "
                    'rel="describedby"; title="UrltraKB source and licenses"'
                )
                previous = headers.get("Link")
                headers["Link"] = f"{previous}, {link}" if previous else link
            await send(message)

        await self.app(scope, receive, with_link if scope["type"] == "http" else send)
