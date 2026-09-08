"""Local MathJax → SVG / Merman → SVG / resvg → PNG rendering."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from openkb.locks import atomic_write_bytes
from openkb.rendering.svg_adapter import adapt_diagram

# Bump when the renderer, adapters, fonts, or output protocol changes.
CACHE_VERSION = "mathjax4.1.3-merman0.7.0-resvg0.46.0-noto2.004-adapter2"


@dataclass(frozen=True)
class RenderedBlock:
    source: str
    kind: str
    display: bool
    image: str | None = None
    width: float = 0
    height: float = 0
    depth: float = 0
    error: str | None = None


class Renderer:
    def __init__(self, cache_dir: Path, assets_dir: Path | None = None) -> None:
        self.cache_dir = cache_dir
        self.assets = assets_dir or Path(__file__).parent / "assets"
        self._closing = threading.Event()
        self._guard = threading.Lock()
        self._processes: set[subprocess.Popen] = set()

    def close(self) -> None:
        self._closing.set()
        with self._guard:
            for process in self._processes:
                if process.poll() is None:
                    process.kill()

    def _invoke(self, command: list[str], request: dict) -> dict:
        environment = dict(os.environ)
        environment["TZ"] = "UTC"
        environment["OPENKB_RENDER_PARENT_PID"] = str(os.getpid())
        environment["OPENKB_RENDER_TIMEOUT_MS"] = "60000"
        if os.name != "nt":
            stat = Path("/proc/self/stat").read_text()
            environment["OPENKB_RENDER_PARENT_START"] = stat[stat.rfind(")") + 2 :].split()[19]
        if "LD_LIBRARY_PATH_ORIG" in environment:
            environment["LD_LIBRARY_PATH"] = environment["LD_LIBRARY_PATH_ORIG"]
        else:
            environment.pop("LD_LIBRARY_PATH", None)
        if self._closing.is_set():
            raise RuntimeError("Rendering stopped")
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=self.assets,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        with self._guard:
            self._processes.add(process)
        try:
            payload: str | None = json.dumps(request, ensure_ascii=False)
            deadline = time.monotonic() + 60
            while True:
                if self._closing.is_set() or time.monotonic() > deadline:
                    process.kill()
                    process.communicate()
                    raise RuntimeError("Rendering stopped or exceeded its time limit")
                try:
                    stdout, _ = process.communicate(payload, timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    payload = None
            try:
                value = json.loads(stdout)
            except ValueError:
                if process.returncode:
                    raise RuntimeError(
                        "Content renderer stopped before returning a result"
                    ) from None
                raise ValueError("Invalid renderer response") from None
            if not isinstance(value, dict):
                raise ValueError("Invalid renderer response")
            if not value.get("ok"):
                raise ValueError(value.get("error", "Content rendering failed"))
            if process.returncode:
                raise RuntimeError("Content renderer exited abnormally after returning a result")
            return value
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
            with self._guard:
                self._processes.discard(process)

    def render(
        self, source: str, kind: str, *, display: bool = True, dark: bool = False, scale: float = 1
    ) -> RenderedBlock:
        if kind not in {"math", "mermaid"} or not 0.5 <= scale <= 4:
            raise ValueError("Unsupported content kind or scale")
        if len(source) > 100_000:
            return RenderedBlock(source, kind, display, error="Content exceeds the rendering limit")
        digest = hashlib.sha256(
            json.dumps([CACHE_VERSION, source, kind, display, dark, scale]).encode()
        ).hexdigest()
        target = self.cache_dir / digest
        target.mkdir(parents=True, exist_ok=True)
        scratch = tempfile.TemporaryDirectory(prefix=".render-", dir=target)
        prepared = Path(scratch.name)
        extension = ".exe" if os.name == "nt" else ""
        helper = str(self.assets / f"renderer{extension}")
        fonts = [str(self.assets / "fonts" / f"NotoSansCJKsc-{w}.otf") for w in ("Regular", "Bold")]
        try:
            if kind == "math":
                value = self._invoke(
                    [
                        str(self.assets / f"node{extension}"),
                        str(self.assets / "mathjax_render.mjs"),
                    ],
                    {"source": source, "display": display, "dark": dark},
                )
                svg, depth = value["svg"], value["depth_px"]
            else:
                value = self._invoke(
                    [helper],
                    {
                        "kind": "mermaid",
                        "source": source,
                        "id": digest,
                        "dark": dark,
                        "svg_only": True,
                        "fonts": fonts,
                    },
                )
                svg, _ = adapt_diagram(value["svg"], dark)
                depth = 0
            if any(
                node.tag.rsplit("}", 1)[-1] == "foreignObject" for node in ET.fromstring(svg).iter()
            ):
                raise ValueError("Diagram contains unsupported browser-only content")
            native = self._invoke(
                [helper],
                {
                    "kind": "svg",
                    "source": svg,
                    "id": digest,
                    "dark": dark,
                    "scale": scale,
                    "fonts": fonts,
                    "svg_path": str(prepared / "image.svg"),
                    "png_path": str(prepared / "image.png"),
                },
            )
            if native["font_faces"] != 2:
                raise ValueError("Bundled fonts could not be loaded")
            # Readers may share a cached formula. Never expose a helper's
            # partially written file to another reader or a Qt image loader.
            for name in ("image.svg", "image.png"):
                atomic_write_bytes(target / name, (prepared / name).read_bytes())
            return RenderedBlock(
                source,
                kind,
                display,
                str(target / "image.png"),
                native["width"] / scale,
                native["height"] / scale,
                depth,
            )
        except (OSError, ValueError, RuntimeError, ET.ParseError, KeyError) as exc:
            return RenderedBlock(source, kind, display, error=str(exc))
        finally:
            scratch.cleanup()
