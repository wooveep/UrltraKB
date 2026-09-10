# Authenticated document-provider validation

On 2026-09-10, the shared document operation was exercised with the official
PaddleOCR jobs service and `deepseek/deepseek-v4-flash` on Debian 13 x64 and
Windows 11 x64 (build 26100), both using CPython 3.12.13. Two-page OCR, retained
evidence, bounded stopping, continuation and publication completed on both
systems. Earlier prompt-only runs failed technical semantic acceptance: generated
prose broadened a startup restriction to shutdown. The follow-up below adds an
independent evidence review and records both failed and successful evaluations;
the historical failures remain part of the evidence.

Only synthetic documents and isolated knowledge bases were used. Credentials
were supplied through private temporary files and environment variables, not
committed configuration, source, manifests or diagnostic output. The authorized
OCR ceiling was 100 cumulative pages. Actual submissions totaled **6 pages**;
known-job and compilation continuations did not resubmit those pages.

## Inputs and verifiable claims

The PDF contains two raster-only pages, 612 × 792 points, rendered at 150 DPI.
Its SHA-256 is
`9a8a50eb258902b5e252810e08e9bed779445f09eab82176d756c182b8e39fe4`.
A native Markdown control carries the same claims. The first page is headed
“Pump controller version 7 - startup procedure”; the second is headed
“Pump controller version 7 - shutdown procedure”.

| Page | Required claim |
| --- | --- |
| 1 | The startup procedure applies only to controller version 7. |
| 1 | Before startup, keep the isolation valve closed. |
| 1 | The normal pressure limit is 37 kPa. |
| 1 | Run `pumpctl start --timeout 42 --mode safe`. |
| 1 | If authentication fails, do not retry automatically. |
| 2 | Wait 15 seconds after stopping the pump. |
| 2 | Open the drain valve only after pressure falls below 5 kPa. |
| 2 | For controller version 6, this **startup** procedure is unsupported. |
| 2 | Record the shutdown timestamp in the maintenance log. |

The startup restriction is deliberately placed under the shutdown heading.
Acceptable generation preserves the literal operation and identifies the layout
ambiguity where relevant; it must not turn it into a shutdown restriction. Both
prompt-only compilations contained the nine claims somewhere, but also included
contradictory or unsupported prose. Counting claim presence alone would falsely
pass this sample.

## Bounds and provider contracts

The final compilation profile used context 16384, output reserve 8192, request
90 seconds, stage 240 seconds, document 270 seconds, cleanup 10 seconds,
`max_attempts: 1`, `max_requests: 12`, `max_tokens: 80000`, and concurrency 1.
Each explicit continuation started a new bounded operation; prior source usage
remained in history. The independent supervisor allowed 600 seconds, 1 GiB worker
memory and 128 MiB output, with 10 seconds for cleanup. Every worker was reaped.
These are experiment limits, not recommended defaults or measured model capacity.

OCR selected `PaddleOCR-VL-1.6` at the official jobs endpoint, with 180 seconds
per run, 20 seconds per request, 100 requests, two pages, 2 MB per submitted page
and 32 MB downloaded data. Polling changed from once per second to once per three
seconds after the initial run exhausted its request allowance. The initial LLM
context cap of 32768 was reduced to 16384. No time, token, page or request cap was
increased to obtain completion.

Authenticated testing exposed a request-contract defect: the jobs service expects
camelCase keys in `optionalPayload`, while the application sent snake_case.
The fix uses serialization aliases for both submission and parsing cache
identity. YAML and settings fields remain snake_case. Existing job identities
remain queryable; changing the effective wire profile invalidates incompatible
parsing reuse. The wire contract matches the pinned PaddleOCR 3.7.0 client's
`_api_client/models.py` payload builder and the
[official jobs client documentation](https://www.paddleocr.ai/latest/en/version3.x/inference_deployment/serving/paddleocr_official_api/cli.html).

The requested DeepSeek model defaults to thinking mode. With an 8192-token output
cap, the initial PDF and native control exhausted output during reasoning and
were correctly left unfinished. Explicit `compilation_thinking: disabled` is
passed through all three compiler stages, using the same requested model.
This follows the provider's [thinking-mode contract](https://api-docs.deepseek.com/guides/thinking_mode).
The setting is included in checkpoint identity; absence preserves the provider
default. No automatic mode change was introduced.

## Observed runs, including failures

| Experiment | Outcome |
| --- | --- |
| Debian initial PDF, original wire options | Two OCR pages completed; default-thinking facts hit `output_budget_exhausted`. |
| Debian corrected wire profile | First run stopped on the OCR request limit. Continuation reused the known job, submitted the remaining page once, and stopped on elapsed OCR budget. Both jobs were later downloaded and validated. |
| Debian native control | Default thinking exhausted output; explicit disabled mode completed in 11 model calls and 26.54 supervisor seconds. |
| Debian first complete PDF compilation | Disabled mode completed in 11 calls and 35.58 seconds, reusing the two corrected OCR jobs. Manual review found incorrect restriction scope. |
| Windows initial PDF | Two corrected OCR jobs completed; facts without an empty reason were rejected. A later response that quoted another unit's text was rejected as `fact_evidence_invalid`. |
| Prompt-contract checks | Missing empty reasons and missing top-level `covered` arrays remained failures. Explicit schema reminders were added without weakening validation. |
| Debian final prompt evaluation | Facts were reused after a planned stop; the next operation reached its 12-call limit. One continuation reused valid generations and finished with three calls. Total: 17 calls, 38.46 supervisor seconds across these runs. Semantic review failed. |
| Windows final prompt evaluation | Facts were reused after a planned stop; the next operation reached its 12-call limit. One continuation finished the remaining generation with one call. Total: 15 calls, 35.56 supervisor seconds across these runs. Semantic review failed. |

The final PDF parses each had 15 structural blocks and both physical pages marked
`verified / ocr_layout_and_assets`. Original bytes retained the input hash.
The final model evaluations used no new OCR submissions. Maximum provider-reported
input in those evaluations was 7670 tokens on Debian and 7828 on Windows, below
the configured context after including the output reserve. Peak sampled worker
memory across all authenticated experiments stayed below 288 MB. Other processes
were active; timings are not isolated platform comparisons or long-document
throughput measurements.

## Historical semantic counterexamples

The Debian response for “Procedure Applicability for Controller Version 6” said:

> The shutdown procedure described in this document applies to **pump controller
> version 7**. The procedure is unsupported for **controller version 6**; a
> version 6 controller must not use this shutdown procedure.

Its extracted fact and quote both said **startup**. The model returned normally
with 92 output tokens, so truncation is not the explanation. This was a newly
generated contribution, not a retained old page. A separate generated topic in
the same run correctly preserved startup, creating a contradiction in the Wiki.

On Windows, “Drain Valve Opening Condition” incorrectly said that shutdown steps
did not apply to version 6 because its startup procedure was unsupported. Other
topics preserved the startup wording. General exception-handling guidance was
also added beyond the source's authentication rule.

Before independent model review, publication checks validated evidence identities, verbatim extraction
quotes, structural coverage, output completion and source/page versions. These structural checks could
not establish that generated prose was entailed by its evidence. Explicit
prompt instructions to preserve operation/version restrictions did not prevent
these failures. A structural `added` result alone was insufficient. The follow-up below tests an
explicit verification gate against retained counterexamples. Representative
technical documents and broader budget calibration remain necessary.

## Earlier usage and code regression

The earlier 64 observed DeepSeek calls all returned usage: 251676 input tokens (28544 cached,
223132 uncached) and 37419 output tokens, including reasoning. Summing each actual
call once, including failed experiments and continuations, gives an estimated
**CNY 0.505** using the request timestamps and the
[2026-08-28 published price table](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/).
Applying peak prices to every call would give CNY 1.010. These are token-based
estimates, not billing receipts. No OCR billing receipt was inspected; six actual
page submissions were observed at the official API.

Cloud serialization and thinking-mode changes passed the HTTP/shared-operation
regressions. After prompt reminders, Windows exposed a 4096-context table case
where the smallest required evidence exceeded input space. Removing optional
JSON formatting whitespace preserved every field and string and restored the
same test without raising its budget. Final checks passed 30 tests on Debian and
107 on Windows with one platform skip. The compact JSON formatting was verified
offline; semantic counterexamples above were recorded before that formatting-only
change, and no accuracy improvement is claimed from it. Ruff, mypy across 204
modules, and both Standards and Spec code reviews passed. The Spec reviewer
confirmed the real-model semantic gate remains failed.

The broader integration and full-suite history is recorded in
[document execution](document-processing.md). This two-page experiment does not
measure 100-page OCR quality, representative user-document accuracy, or complete
production budget calibration.

## Independent evidence review follow-up

The follow-up adds a publication gate that reviews the exact public title and
cleaned Markdown body against original quotes and reread evidence. Extraction
statements remain useful for generation, but are omitted from the review so that
an erroneous paraphrase cannot overrule the original quote. Heading and adjacent
block relationships remain attached to the evidence. A rejected draft gets at
most one correction with explicit feedback and must pass another review. Review,
correction and generation consume the original stage/document/request budgets.
Uncertain, malformed and still-unsupported results leave the whole source
unfinished; they do not overwrite an earlier complete Wiki.

A successful checkpoint includes a supported verdict, nonempty review reason and
a digest of the public title and final body. Recovery checks all three. The first
supported title is fixed for subsequent parts; a conflicting later title stops
the proposal. A title correction also updates an exactly matching opening body
heading before review. Reserved provenance markers are rejected after all such
normalization, preventing a title from corrupting source contribution boundaries.

Actual review exposed additional false positives and false negatives. Disabled
thinking scored 9/10 on one fixed evaluation, then 8/11 after its input format
was revised. It missed an incorrect title and an invented rationale, and rejected
a faithful body that corrected a faulty extraction statement. Explicitly enabled
review thinking passed a three-case diagnostic and an 11-case evaluation, but a
later 12-case evaluation still rejected the faulty-plan case. The final input
removes that non-evidence statement; all **12/12 fixed cases** then matched their
expected verdicts. The cases are retained in
[`tests/fixtures/evidence_verifier_cases.json`](../tests/fixtures/evidence_verifier_cases.json).
Expected labels were not sent to the provider. Every evaluation, including the
failed ones, is included in the usage totals below. This is a small diagnostic
set, not a statistical accuracy estimate.

The final real-provider profile explicitly sets `compilation_thinking: disabled`
and `verification_thinking: enabled`, using the same requested DeepSeek model.
The review override is optional; absence inherits the compiler setting. It changes
only generation/review reuse, leaving independent facts and planning reusable.
The earlier 8192 output reserve and all other time, token, concurrency and request
limits remain unchanged. Thinking output is charged like other output; it is not
an extra allowance or an automatic fallback.

A controlled 100-page native PDF regression covered all 353 nonempty blocks,
extracted and reread all three planted technical facts, reused facts after a
planned stop, and completed in 125 local model-adapter calls. The supervisor
elapsed time was 6.54 seconds and peak sampled worker memory was 240 MB. No
provider requests or OCR charges occurred in that controlled experiment; it does
not measure semantic quality or real-provider throughput.

The Windows follow-up exposed a deterministic Markdown defect: global image-link
rewriting changed an `asset:` example inside a fenced code block, and evidence
review correctly refused the altered literal. Link normalization now follows
actual Markdown inline blocks and table cells, preserving code examples and
original line endings. Real images still require retained assets. Independent
review also caught cross-paragraph backtick pairing, Unicode line-offset drift,
table-cell pairing and JSON escaping of image titles; regression tests reproduce
these failures through the shared document operation. The implementation uses
the already pinned `markdown-it-py==4.2.0`, now an explicit core dependency.

## Final follow-up observations

With the final code and unchanged profile, both platforms completed the retained
two-page sample. The first operations stopped at their request allowance and
continued from supported checkpoints. Windows also encountered a review output
limit before a later explicit continuation completed. These stops remain in the
usage history; neither platform performed more OCR.

| Platform | Explicit operations | Actual model calls | Supervisor seconds, summed | Peak sampled worker memory | Final result |
| --- | --- | --- | --- | --- | --- |
| Debian 13 x64 | `real-linux-v11` through `v13` | 26 | 187.11 | 245.6 MB | `added`, original retained |
| Windows 11 x64 | `real-windows-v7` through `v9` | 28 | 255.97 | 233.0 MB | `added`, original retained |

Inspection of all final pages found the nine seeded technical claims retained,
including the exact command, both pressure thresholds, the waiting period,
authentication exception and startup-only version restriction. The Windows
published image resolves to the retained immutable asset and its content hash.
The earlier startup-to-shutdown restriction error did not recur in these final
outputs. This does not establish complete semantic accuracy: the Debian pressure
page still links the isolation-valve prerequisite label to the drain-valve topic.
Its displayed condition is correct, but the Wiki association is misleading.
Source evidence remains readable; semantic quality of cross-topic links needs
further calibration and must not be reported as fully accepted.

Maximum provider-reported input in the final runs was 6421 tokens on Debian and
5513 on Windows; both fit the 16384 configured context with the 8192 output
reserve. Across earlier follow-up experiments, one request reported 8324 input
tokens despite fitting the local estimator, exceeding that experiment's configured
context allowance when combined with the reserve. The provider accepted it, but
this exposes a tokenizer-estimation limitation. Local fit checks must not be
presented as an exact guarantee of provider token counts; representative model
budget calibration remains outstanding.

All authenticated experiments together, including earlier failures, fixed-case
evaluations and continuations, made **360 observed DeepSeek calls**. All returned
usage: 950995 input tokens (272998 cached, 677997 uncached) and 305888 output tokens,
including reasoning. Applying the same timestamped published prices gives
**CNY 4.310 estimated**, or CNY 4.814 if every call used peak pricing. These are
estimates, not billing receipts. OCR remained **6/100 cumulative submitted pages**.
Run-local observations and the cumulative ledger retain unsuccessful attempts;
duplicate copies of evaluation results are not counted twice.

Final frozen-code checks passed **1587 tests with one skip** on Debian, and the
58-test evidence/Markdown/budget selection passed on Windows. The Windows test
initially decoded UTF-8 output using the system default encoding; the test now
reads UTF-8 explicitly. Ruff and mypy over 206 modules passed. Standards and Spec
reviews closed all implementation findings, including independent Markdown
boundary probes. Controlled adapter tests, fixed-case model evaluation and
whole-document observations are separate evidence; none substitutes for
representative long-document semantic acceptance or a frozen desktop build.
