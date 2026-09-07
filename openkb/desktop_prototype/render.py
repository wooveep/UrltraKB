"""THROWAWAY renderer runner. Writes only disposable prototype artifacts, never a KB."""

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from bootstrap import ROOT, node_path
from samples import SAMPLES
from svg_adapter import adapt_diagram

HELPER = (
    ROOT
    / "rust-helper"
    / "target"
    / "debug"
    / ("openkb-render-prototype.exe" if os.name == "nt" else "openkb-render-prototype")
)
FONTS = [
    ROOT / ".runtime" / "fonts" / f"NotoSansCJKsc-{weight}.otf" for weight in ("Regular", "Bold")
]


def parse_markup(markdown):
    match = re.fullmatch(r"\s*```mermaid\s*\n(.*?)\n```\s*", markdown, re.S)
    if match:
        return "mermaid", match.group(1), True
    for opening, closing, display in ((r"\[", r"\]", True), (r"\(", r"\)", False)):
        text = markdown.strip()
        if text.startswith(opening) and text.endswith(closing):
            return "math", text[len(opening) : -len(closing)].strip(), display
    raise ValueError("请使用小写 mermaid 围栏、\\(…\\) 或 \\[…\\]；原文已保留。")


def invoke(argv, request, timeout=60):
    env = dict(os.environ)
    env["TZ"] = "UTC"
    # Runtime calls use already installed local resources. No proxy/network loader is configured.
    result = subprocess.run(
        argv,
        input=json.dumps(request, ensure_ascii=False),
        text=True,
        encoding="utf-8",
        capture_output=True,
        cwd=ROOT,
        env=env,
        timeout=timeout,
    )
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError((result.stderr or result.stdout or "renderer returned no JSON")[-3000:])
    if result.returncode or not response.get("ok"):
        raise RuntimeError(response.get("error", result.stderr or "renderer failed"))
    response["stderr"] = result.stderr[-4000:]
    return response


def render_sample(sample, dark=False, scale=1.0):
    started = time.monotonic()
    digest = hashlib.sha256((sample["markdown"] + str(dark) + str(scale)).encode()).hexdigest()[:12]
    target = ROOT / "artifacts" / f"{sample['id']}-{'dark' if dark else 'light'}-{scale:g}-{digest}"
    target.mkdir(parents=True, exist_ok=True)
    result = {
        "id": sample["id"],
        "title": sample["title"],
        "family": sample["family"],
        "markdown": sample["markdown"],
        "expected": sample["expected"],
        "dark": dark,
        "scale": scale,
        "expected_error": bool(sample.get("expected_error")),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "node": "24.20.0",
        "mathjax": "4.1.3",
        "merman": "0.7.0",
        "resvg": "0.46.0",
        "semantic_review": "pending",
        "directory": str(target),
    }
    (target / "source.md").write_text(sample["markdown"], encoding="utf-8")
    try:
        kind, source, display = parse_markup(sample["markdown"])
        result.update(kind=kind, display=display)
        if kind == "math":
            math_result = invoke(
                [str(node_path()), str(ROOT / "mathjax_render.mjs")],
                {"source": source, "display": display, "dark": dark},
            )
            source = math_result["svg"]
            result["depth_px"] = math_result["depth_px"]
            result["mathjax_stderr"] = math_result["stderr"]
        else:
            generated = invoke(
                [str(HELPER)],
                {
                    "kind": "mermaid",
                    "source": source,
                    "id": sample["id"],
                    "dark": dark,
                    "svg_only": True,
                    "fonts": [str(p) for p in FONTS],
                },
            )
            (target / "upstream-safe.svg").write_text(generated["svg"], encoding="utf-8")
            source, result["svg_adaptations"] = adapt_diagram(generated["svg"], dark)
        native = invoke(
            [str(HELPER)],
            {
                "kind": "svg",
                "source": source,
                "id": sample["id"],
                "dark": dark,
                "scale": scale,
                "fonts": [str(p) for p in FONTS],
                "svg_path": str(target / "image.svg"),
                "png_path": str(target / "image.png"),
            },
        )
        result.update(native)
        tree = ET.parse(result["svg_path"])
        result["svg_text"] = [
            "".join(e.itertext()) for e in tree.iter() if e.tag.rsplit("}", 1)[-1] == "text"
        ]
        result["foreign_objects"] = sum(
            e.tag.rsplit("}", 1)[-1] == "foreignObject" for e in tree.iter()
        )
        if result["font_faces"] != 2 or result["foreign_objects"]:
            raise RuntimeError("字体或 safe SVG 结构不符合原型约定")
    except Exception as exc:
        result.update(ok=False, error=str(exc))
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    result["technical_expectation_met"] = bool(result["ok"]) != result["expected_error"]
    (target / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", default="flow-cn")
    parser.add_argument("--source-file")
    parser.add_argument("--dark", action="store_true")
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--both-themes", action="store_true")
    args = parser.parse_args()
    if args.all:
        results = []
        for dark in [False, True] if args.both_themes else [args.dark]:
            for sample in SAMPLES:
                r = render_sample(sample, dark, args.scale)
                results.append(r)
                print(
                    f"{r['id']} {'dark' if dark else 'light'} ok={r['ok']} "
                    f"expected={r['technical_expectation_met']} {r.get('error', '')}",
                    flush=True,
                )
        theme_name = "both" if args.both_themes else "dark" if args.dark else "light"
        summary = ROOT / "artifacts" / f"batch-{theme_name}-{args.scale:g}.json"
        summary.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"SUMMARY {summary}", flush=True)
        return
    sample = dict(next(s for s in SAMPLES if s["id"] == args.sample))
    if args.source_file:
        sample["markdown"] = Path(args.source_file).read_text(encoding="utf-8")
    print(json.dumps(render_sample(sample, args.dark, args.scale), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
