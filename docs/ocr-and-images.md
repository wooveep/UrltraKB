# OCR and independent image understanding

New settings use `parsing.ocr.policy: auto`, `backend: system`, `device: auto`.
Native text and native tables are read first. OCR is considered only for uncertain
pages or images. On Windows the system engine uses installed Windows OCR language
packs. On Linux this engine reports unavailable; it does not install software or
select another engine. Original text and images remain available. An image-only
source with no readable transcription stays unfinished and identifies the missing
readable text. A local failure never submits a cloud job.

The [publication policy revised on 2026-09-12](document-processing.md#accepted-publication-policy--2026-09-12)
allows ordinary parsing/OCR errors and local omissions to coexist with successful
import and knowledge publication. Missing text, tables, images or attachments
remain visible diagnostics; users need not confirm each issue before publishing
the usable content. Knowledge publication does not imply complete transcription.
The original, available evidence and omission details remain available for later
review or reprocessing. This policy also covers native parsing errors, not only OCR.

Set `policy: off` to avoid OCR runtime initialization, hardware detection and
network submissions, including embedded documents. This creates a distinct parse
without earlier OCR transcription. Existing versions and citations are retained.
The page review action can force one page with a selected engine without changing
the saved defaults; the other pages retain their existing evidence.

Old explicit local configurations retain the native PaddleOCR CPU route. Old
cloud jobs settings retain cloud processing. The old unconfigured `local` option
maps to `system`. Existing manual runtime paths remain an advanced option.

## Install and choose a local model

The desktop **文档识别** settings expose policy, engine, installed environment and
device. Choose a package to see its fixed sources, exact bytes and destination;
**安装所选模型** starts acquisition. Saving settings and importing documents never
starts an installation. Installation does not select an engine automatically.

```sh
openkb ocr settings --kb /path/to/kb
openkb ocr configure --kb /path/to/kb --settings ocr.json
openkb ocr prepare openvino
openkb ocr install openvino
openkb ocr status
openkb ocr check INSTALLATION_ID --device auto
```

Profiles are `native` (PaddleOCR-VL-1.6 CPU), `nvidia` (the same model with the
official PaddlePaddle 3.3.1 CUDA 12.6 wheel), and `openvino` (PaddleOCR-VL-1.5).
Set `parsing.ocr.installation` to the ID reported by installation and choose
`backend: local`. CPU and GPU packages remain separate environments. The native
NVIDIA runtime supports CPU execution too; automatic fallback never changes the
model weights. Explicit CPU skips GPU enumeration. Explicit GPU cannot fall back.
The first release conservatively serializes local GPU inference across processes.
Only classified device failures permit one CPU retry, after process cleanup and
within the original time, region and token allowance.

Native NVIDIA admission checks the GPU wheel, CUDA build, visible device and
compute capability (7.x–9.x for this pinned route), then loads the complete
pipeline. Blackwell is not claimed by this CUDA 12.6 profile. OpenVINO enumerates
concrete `GPU.N` adapters and reports the selected full device name. Its layout
stage runs on CPU; vision, projection, embedding and language stages compile on
the selected Intel device. `parsing.ocr.gpu_device` can explicitly select `GPU.1` or
`gpu:1` for an installed or advanced manual runtime configuration. The sample check also accepts `--gpu-device`. No guessed VRAM capacity or
unmeasured speedup is displayed; process RSS limits are not GPU memory isolation.

Default storage is `%LOCALAPPDATA%/OpenKB/ocr` on Windows and
`${XDG_DATA_HOME:-~/.local/share}/openkb/ocr` on Linux. Installation can select a
different root. `models/`, `runtimes/` and `cache/` are separate, and KB evidence
stays in its own KB. Model content is shared across compatible installations.
Only a checked package receives an atomic ready receipt. Device availability is
checked separately with a built-in sample or during actual OCR. Interrupted
acquisition retains resumable files and failed/cancelled state. Inference verifies
model files before loading and blocks network access in native/OpenVINO workers.

For an offline transfer, acquire the complete package on a connected machine
of the target platform, copy the directory, then install it offline:

```sh
openkb ocr export-package openvino /absolute/offline-ocr
openkb ocr install openvino --offline /absolute/offline-ocr --destination /absolute/ocr
```

The published catalogs under `openkb/ocr/profiles/` contain fixed wheel URLs,
sizes and SHA-256 hashes for Windows/Linux x64, the Python 3.12.13 archive, and
complete model file manifests. `scripts/build_ocr_catalog.py` regenerates wheel
catalogs from the reviewed locks; the native model builder retains its original
revision and digest checks. These build operations use public artifacts only.

## Already deployed services and cloud jobs

Under local OCR, `execution: service` selects an explicitly configured Paddle
service. `service.protocol: pipeline` uses the official `/layout-parsing` API;
`vlm` uses a compatible `/chat/completions` VLM endpoint. Configure `service.endpoint`
and `service.model: PaddleOCR-VL-1.6`. Optional `credential_env` names a dedicated
service key; no main-model or image key is borrowed. Localhost is labeled as
processing on this machine; other hosts are remote processing.

The official service route allows independently deployed supported NVIDIA/AMD/
Intel stacks without installing drivers, containers or servers during import.
The service's hardware is not inferred from its URL. VLM-only output is retained
as transcription requiring layout review. The standard full service API does not
supply per-block EOS receipts, so its generation completeness remains explicitly
unverified rather than being fabricated. URLs in an image-data field are rejected;
this adapter currently requires the official default inline Base64 asset mode.

`backend: cloud` retains the existing Paddle jobs protocol and independent cloud
key. Stopping local waiting does not cancel the remote job; job identifiers and
uncertain submission receipts remain recoverable. Neither service protocol is
assumed to be the jobs API.

## Independent image model

Image understanding defaults off. Configure it in **图片理解**, save, and explicitly
run **测试已保存的图片连接**. The test sends a built-in red square, checks a completed
image response and the color answer, and never enables the feature. Enable it
separately when ready. Supported protocols are OpenAI-compatible, Anthropic and
Ollama's OpenAI-compatible API. Unknown endpoints require an explicit image-support
declaration and successful sample verification. A model name alone proves nothing.

```json
{
  "enabled": false,
  "connection": "independent",
  "provider": "openai-compatible",
  "model": "my-vision-model",
  "endpoint": "http://127.0.0.1:8123/v1",
  "authentication": "none",
  "supports_images": true
}
```

```sh
openkb image configure --kb /absolute/kb --settings image-settings.json
openkb image configure --kb /absolute/kb --set-key
openkb image test --kb /absolute/kb
openkb image status --kb /absolute/kb
```

The key is entered through a hidden prompt; `--clear-key` removes it. Settings
responses show presence and source, never plaintext. A full connection group and
its key have one global or KB scope. Changing its destination/model invalidates
the old key binding. An independent connection never reads another provider key.
`connection: reuse_main` explicitly uses the main model's complete connection and
still requires image capability verification. Tasks retain captured connections
and credentials; settings changes affect subsequent tasks.

Enabled agents can request a specific image or pixel crop and a specific visual
question. The visual endpoint receives the image; the knowledge model receives a
text observation with image identity, original bytes retained separately, region,
question, connection identity, completion status and source evidence references.
Observations are interpretations, not direct quotations or OCR transcription.
Caches bind all these inputs and output settings. Disabling the feature removes
the tool and prevents fresh image requests/cache injection. Existing conversation
text stays readable, and historical images are preserved outside model input.

Independent image limits cover time, requests, input/response bytes and tokens;
requests also check task cancellation and contribute to an active processing
budget. Unknown token usage retains the output reservation. Failures and truncated
outputs remain explicit and do not erase usable text evidence.

REST maps the same settings through `/api/v1/config` and KB config routes. Explicit
actions are `POST /api/v1/config/image/test`, `POST /api/v1/config/ocr/prepare`,
`POST /api/v1/config/ocr/install`, `POST /api/v1/config/ocr/check`, and
`GET /api/v1/config/ocr/installations`. Disconnecting an install requests cooperative
cancellation; its process owner finishes cleanup and records the resulting state.

## Validation and attribution

The OpenVINO source is pinned to
[zhaohb/paddleocr_vl_ov commit 598857c](https://github.com/zhaohb/paddleocr_vl_ov/commit/598857c1272c2b7224109ad4573974f7d4cc260f).
Its pyproject declares Apache-2.0. Apache-2.0 text and an attribution notice ship with the adapter. The unmodified source archive, project metadata,
notices and license-bearing source headers are retained with the installation.
OpenKB's separate adapter replaces swallowed exceptions and batch early stopping
with serial generation and an EOS receipt for every attempted block.
The 1.5 model uses the author's
[ModelScope artifacts](https://modelscope.cn/models/zhaohb/PaddleOCR-VL-1.5-ov),
revision `43f60524b332ac2fc5f06aed8f015ad3d0dc000b`. The selected unquantized/FP16
artifact set consists of **17 files, 1,882,744,547 bytes**, all actually downloaded
and SHA-256 checked during this implementation. No conversion or quantization was
performed. Model code/configuration/tokenizer and all five model stages are included.

An isolated OpenVINO 2025.4.1 / Transformers 4.54.0 / Torch 2.8.0+cpu environment
ran the full built-in CPU sample through the process supervisor, preserving two
text blocks and normal EOS receipts. A second complete online installation through
the application installer also passed its built-in CPU check. One initial sample
used 4,752,363,520 bytes peak process-tree RSS and completed supervised execution in
8.26 seconds. This is a single synthetic sample on this development host, not a
minimum memory requirement or comparative benchmark.

The user's prior Intel GPU success is accepted evidence for the upstream 1.5
route. No Intel/NVIDIA/AMD GPU performance measurement was made on this host.
Controlled-process tests establish routing, cleanup, fallback budgets and result
validation; they do not establish hardware compatibility or performance.

Hardware requirements are checked against the fixed
[PaddleOCR v3.7 documentation](https://github.com/PaddlePaddle/PaddleOCR/blob/v3.7.0/docs/version3.x/pipeline_usage/PaddleOCR-VL.en.md),
its [AMD guide](https://github.com/PaddlePaddle/PaddleOCR/blob/v3.7.0/docs/version3.x/pipeline_usage/PaddleOCR-VL-AMD-GPU.en.md),
and the [OpenVINO GPU documentation](https://docs.openvino.ai/2025/openvino-workflow/running-inference/inference-devices-and-modes/gpu-device.html).

The service check is also available at `POST /api/v1/config/ocr/check-service`
with an optional `kb` field, and through the OCR settings sample-check button.
A successful VLM-only sample reports `connected_needs_review`; it never claims
verified layout or a GPU device the service did not report. Slow streaming replies
are bounded by the full elapsed deadline and the client is closed before returning.

## Implementation verification

The full test suite was run once: 1,685 passed, 11 failed, and 1 skipped. The
failures were resolved by updating expectations for the new defaults and fixing
progress-scope cleanup when an event observer raises. All 229 tests in the seven
affected files subsequently passed. A further combined OCR, service, image,
conversation, history, accounting and progress regression passed 81 tests with
one platform-dependent skip. The task-wide request-budget test verifies that no
source image is uploaded after the main model consumes the last allowed request.
Both independent code-review axes were completed; all reported findings were
fixed and rechecked. Mypy checks all 248 application modules and Ruff lint passes.
Changed Python files pass formatting. A whole-repository format check still finds
two pre-existing files (`application/knowledge_bases.py`, `compilation_report.py`)
that were outside this change and were left untouched.

A wheel was built and inspected for the runtime catalogs, workers, notices and
visual interface. The original uncommitted worktree changes are excluded from the
implementation commit; feature tests also passed against the isolated commit tree.
GPU results are retained with actual runtime metadata; GPU cache reuse is deferred
until hardware identity can be re-attested, rather than trusting a device ordinal.
