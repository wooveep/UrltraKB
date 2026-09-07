"""Exercise the exact renderer candidate through bundle-relative paths."""

import json
import os
import subprocess

from openkb.desktop_prototype.samples import SAMPLES
from openkb.desktop_prototype.svg_adapter import adapt_diagram
from openkb.portable_prototype.paths import assets, helper_environment


def invoke(executable, arguments, request, cwd):
    result = subprocess.run(
        [str(executable), *map(str, arguments)],
        input=json.dumps(request, ensure_ascii=False),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=60,
        cwd=cwd,
        env=helper_environment(),
    )
    response = json.loads(result.stdout)
    if result.returncode or not response.get("ok"):
        raise RuntimeError(response.get("error") or result.stderr)
    return response


def run_renderers(output, emit):
    root = assets() / "renderers"
    node = root / ("node.exe" if os.name == "nt" else "node")
    helper = root / ("renderer.exe" if os.name == "nt" else "renderer")
    fonts = [str(root / "fonts" / f"NotoSansCJKsc-{w}.otf") for w in ("Regular", "Bold")]
    records = []
    for sample in SAMPLES:
        for dark in (False, True):
            target = output / (sample["id"] + ("-dark" if dark else "-light"))
            target.mkdir(parents=True, exist_ok=True)
            original = sample["markdown"]
            (target / "source.md").write_text(original, encoding="utf-8")
            try:
                if sample["kind"] == "mermaid":
                    source = original.removeprefix("```mermaid\n").removesuffix("\n```")
                    raw = invoke(
                        helper,
                        [],
                        {
                            "kind": "mermaid",
                            "source": source,
                            "fonts": fonts,
                            "id": sample["id"],
                            "dark": dark,
                            "svg_only": True,
                        },
                        root,
                    )
                    svg, _ = adapt_diagram(raw["svg"], dark)
                else:
                    source = original.strip()[2:-2].strip()
                    svg = invoke(
                        node,
                        [root / "mathjax_render.mjs"],
                        {"source": source, "dark": dark, "display": sample.get("display", True)},
                        root,
                    )["svg"]
                result = invoke(
                    helper,
                    [],
                    {
                        "kind": "svg",
                        "source": svg,
                        "fonts": fonts,
                        "dark": dark,
                        "scale": 2,
                        "svg_path": str(target / "image.svg"),
                        "png_path": str(target / "image.png"),
                    },
                    root,
                )
                assert result["font_faces"] == 2
                record = {
                    "id": sample["id"],
                    "dark": dark,
                    "ok": True,
                    "png": str(target / "image.png"),
                }
            except Exception as exc:
                record = {"id": sample["id"], "dark": dark, "ok": False, "error": str(exc)}
            record["expected"] = record["ok"] != bool(sample.get("expected_error"))
            records.append(record)
            emit("render", record)
    assert all(r["expected"] for r in records), "bundled renderer failed a corpus expectation"
    return {
        "attempts": len(records),
        "expected_errors": sum(not r["ok"] for r in records),
        "node_path": str(node),
        "helper_path": str(helper),
        "records": records,
    }
