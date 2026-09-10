"""Optional Windows text recognition using the installed, local OS language packs."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from functools import lru_cache
from pathlib import Path

from openkb.evidence import BlockDraft, ParseStore
from openkb.inputs import processing_directory
from openkb.processing import ProcessingIncomplete, processing_checkpoint
from openkb.sources import content_id

# Windows.Media.Ocr returns words and their rectangles; no network provider or
# optional Python runtime is used. API: https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine
_WORKER = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime]
function Await($operation, [Type]$resultType) {
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and
        $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    $task = $method.MakeGenericMethod($resultType).Invoke($null, @($operation))
    $task.Wait()
    return $task.Result
}
$plan = Get-Content -LiteralPath $env:OPENKB_WINDOWS_OCR_PLAN -Raw -Encoding UTF8 | ConvertFrom-Json
$languages = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages)
$language = $languages | Where-Object { $_.LanguageTag -like 'zh-Hans*' } | Select-Object -First 1
if ($null -eq $language) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
} else {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
}
if ($null -eq $engine -and $languages.Length -gt 0) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($languages[0])
}
if ($null -eq $engine) { throw 'Windows OCR language is unavailable' }
$dll = Get-Item -LiteralPath "$env:SystemRoot\System32\Windows.Media.Ocr.dll"
$info = @{
    language = $engine.RecognizerLanguage.LanguageTag
    engine_version = $dll.VersionInfo.FileVersion
    max_dimension = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension
}
if ($plan.action -eq 'info') { $result = $info } else {
    $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime]
    $operation = [Windows.Storage.StorageFile]::GetFileFromPathAsync($plan.input)
    $file = Await $operation ([Windows.Storage.StorageFile])
    $operation = $file.OpenAsync([Windows.Storage.FileAccessMode]::Read)
    $stream = Await $operation ([Windows.Storage.Streams.IRandomAccessStream])
    try {
        $operation = [Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)
        $decoder = Await $operation ([Windows.Graphics.Imaging.BitmapDecoder])
        $operation = $decoder.GetSoftwareBitmapAsync()
        $bitmap = Await $operation ([Windows.Graphics.Imaging.SoftwareBitmap])
        try {
            $recognized = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
            $lines = @()
            foreach ($line in $recognized.Lines) {
                $words = @()
                foreach ($word in $line.Words) {
                    $rect = $word.BoundingRect
                    $words += @{text=$word.Text; box=@($rect.X,$rect.Y,$rect.Width,$rect.Height)}
                }
                $lines += @{text=$line.Text; words=$words}
            }
            $result = @{
                info=$info; width=$bitmap.PixelWidth; height=$bitmap.PixelHeight; lines=$lines
            }
        } finally { $bitmap.Dispose() }
    } finally { $stream.Dispose() }
}
$text = ConvertTo-Json -InputObject $result -Depth 8 -Compress
$utf8 = New-Object Text.UTF8Encoding($false)
[IO.File]::WriteAllText($env:OPENKB_WINDOWS_OCR_OUTPUT, $text, $utf8)
"""


def _run(plan: dict, root: Path, *, seconds: float = 30) -> dict:
    plan_path, output = root / "plan.json", root / "result.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    environment = {
        **os.environ,
        "OPENKB_WINDOWS_OCR_PLAN": str(plan_path),
        "OPENKB_WINDOWS_OCR_OUTPUT": str(output),
    }
    started = time.monotonic()
    process = subprocess.Popen(
        [
            str(executable),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            base64.b64encode(_WORKER.encode("utf-16-le")).decode("ascii"),
        ],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        while process.poll() is None:
            processing_checkpoint("ocr")
            if time.monotonic() - started > seconds:
                raise ProcessingIncomplete("windows_ocr_timeout", "ocr")
            time.sleep(0.03)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
    if process.returncode or not output.is_file() or output.stat().st_size > 16_000_000:
        raise ValueError("windows_ocr_unavailable")
    result = json.loads(output.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("windows_ocr_result_invalid")
    return result


@lru_cache(maxsize=1)
def runtime_profile() -> dict:
    with processing_directory(prefix="openkb-windows-ocr-info-") as temporary:
        info = _run({"action": "info"}, Path(temporary), seconds=10)
    if (
        set(info) != {"language", "engine_version", "max_dimension"}
        or not all(isinstance(info[k], str) and info[k] for k in ("language", "engine_version"))
        or type(info["max_dimension"]) is not int
        or info["max_dimension"] <= 0
    ):
        raise ValueError("windows_ocr_runtime_invalid")
    return {
        "backend": "windows-media-ocr-v1",
        "worker": content_id(_WORKER),
        "render_dpi": 144,
        **info,
    }


class WindowsOcr:
    def __init__(self, store, source, *, retries=None):
        self.store, self.source = store, source
        self.profile = runtime_profile()
        self.retries = retries or {}

    def close(self):
        pass

    def page(self, document, page, *, input_id=None):
        profile = {"ocr": self.profile, "physical_page": page}
        if page in self.retries:
            profile["attempt"] = self.retries[page]
        if input_id is not None:
            profile["embedded_input"] = input_id
        parses = ParseStore(self.store.kb_dir)
        cached = parses.find(self.source, profile)
        if cached is not None:
            blocks = [
                BlockDraft(
                    self.store.asset(b.blob).read_text(encoding="utf-8"),
                    b.kind,
                    b.location,
                    b.assets,
                    b.context,
                )
                for b in cached.blocks
            ]
            reason = next(
                (q["reason"] for q in cached.quality if q["status"] == "needs_review"), None
            )
            return blocks, reason
        rect = document[page - 1].rect
        scale = self.profile["render_dpi"] / 72
        if (
            rect.width * rect.height * scale * scale > 32_000_000
            or max(rect.width, rect.height) * scale > self.profile["max_dimension"]
        ):
            return [], "windows_ocr_image_too_large"
        pixmap = document[page - 1].get_pixmap(dpi=self.profile["render_dpi"])
        content = pixmap.tobytes("png")
        image = self.store.put_bytes(content)
        with processing_directory(prefix="openkb-windows-ocr-") as temporary:
            root = Path(temporary)
            input_path = root / "input.png"
            input_path.write_bytes(content)
            try:
                value = _run({"action": "recognize", "input": str(input_path)}, root)
                blocks = self._blocks(value, page, rect, pixmap.width, pixmap.height, image)
            except (ValueError, OSError):
                return [], "windows_ocr_result_unavailable"
            except ProcessingIncomplete as exc:
                if exc.reason != "windows_ocr_timeout":
                    raise
                return [], exc.reason
        reason = (
            None
            if any(b.kind != "image" for b in blocks)
            else "windows_ocr_no_text_requires_review"
        )
        parses.save(
            self.source,
            profile,
            blocks,
            quality=[
                {
                    "page": page,
                    "status": "needs_review" if reason else "verified",
                    "reason": reason or "windows_ocr_text_checked",
                }
            ],
        )
        return blocks, reason

    def _blocks(self, value, page, rect, width, height, image):
        import math

        if (
            value.get("info")
            != {k: self.profile[k] for k in ("language", "engine_version", "max_dimension")}
            or value.get("width") != width
            or value.get("height") != height
        ):
            raise ValueError("windows_ocr_result_identity_mismatch")
        lines = value.get("lines")
        if not isinstance(lines, list) or len(lines) > 10000:
            raise ValueError("windows_ocr_result_invalid")
        blocks = [
            BlockDraft(
                f"![Original page](asset:{image})",
                "image",
                {"kind": "pdf", "page": page, "bbox": list(rect)},
                (image,),
                "Original visual content retained; OCR below records text only.",
            )
        ]
        for line in lines:
            if (
                not isinstance(line, dict)
                or not isinstance(line.get("text"), str)
                or not isinstance(line.get("words"), list)
                or not line["words"]
            ):
                raise ValueError("windows_ocr_line_invalid")
            boxes = []
            for word in line["words"]:
                box = word.get("box") if isinstance(word, dict) else None
                if (
                    not isinstance(box, list)
                    or len(box) != 4
                    or not all(type(v) in {int, float} and math.isfinite(v) and v >= 0 for v in box)
                    or box[0] + box[2] > width + 1
                    or box[1] + box[3] > height + 1
                ):
                    raise ValueError("windows_ocr_word_invalid")
                boxes.append(box)
            bbox = [
                min(b[0] for b in boxes) / width * rect.width,
                min(b[1] for b in boxes) / height * rect.height,
                max(b[0] + b[2] for b in boxes) / width * rect.width,
                max(b[1] + b[3] for b in boxes) / height * rect.height,
            ]
            blocks.append(
                BlockDraft(
                    line["text"],
                    "paragraph",
                    {"kind": "pdf", "page": page, "bbox": bbox},
                    (image,),
                    "Local Windows OCR text; original image retained.",
                )
            )
        return blocks
