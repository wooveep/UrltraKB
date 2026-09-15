# Conversation feedback and review recovery

The desktop conversation page renders each question and verified answer in its own
rounded native card. Pending answers show public lifecycle stages and elapsed time
separately from the Markdown transcript. Model narration, private reasoning and
unverified drafts are not rendered as answers. Review progress counts completed
answer units; it is not an estimated model percentage.

## Reported Windows failure

The confirmed task `af27ad04891a4a4db1e1a205045aef11`, session
`20260915-135457-f13`, ended with `InvalidReview` after approximately 403 seconds.
Its historical log has no review payload, so the precise rejected field cannot be
reconstructed from that task alone.

A controlled repeat of the same partition question used a private copy of the KB.
One captured review bound a quotation to observation `o5`, although that quotation
occurred in `o7`. The existing strict check rejected it with `quote_mismatch`; the
next reviewer response repaired the binding and passed. Offline replay of these
redacted responses reproduces both outcomes without further model calls. Captures
remain outside the source release under the ignored `build/chat-diagnostic/` folder.
The complete repeated question exceeded the diagnostic harness's 600-second limit;
this is not evidence that the complete real question passed after this change.

## Changes

- Each independent review batch now has one protocol-recovery attempt. Previously,
  every batch shared a single retry, so a later transient invalid response could
  fail the entire answer. Split batches inherit any spent recovery attempt, and all
  calls retain the task's request, token and time budgets.
- Protocol recovery keeps the draft and evidence unchanged. Semantic correction
  still requires located issues and a subsequent complete review.
- Failed answers retain a fixed diagnostic category such as
  `answer_verification_invalid:quote_mismatch`. Quotes and model explanations are
  excluded from these public error codes. The desktop explains the category and
  preserves the question, including when loading failed task history.
- Public stages cover reading sources, drafting, reviewing, repair and saving.
  The independent activity widget displays elapsed time even between model events.
- Native message cards retain selection, Markdown, citations and long-answer
  scrolling. Appending history or updating progress preserves existing readers.

## Regression coverage

`tests/test_answer_progress.py` exercises the public conversation operation through
controlled HTTP model responses. The independent-batch recovery test failed on the
previous implementation, then passed with per-batch recovery. Existing review tests
continue to reject repeatedly invalid bindings and incorrect typed metadata.

`tests/test_conversation_feedback.py` checks visible stages without thought/draft
leakage, elapsed time, terminal errors, rounded cards, long/narrow layouts, citation
signals and history reuse. The stream-cleanup test also covers intervening status
events before an answer delta. Light and dark native previews were inspected.

Windows source, frozen-program and distribution verification results accompany the
matching release materials. The installed GUI must exit before replacing its files.

## Follow-up: missing procedural content

The user identified the acceptance requirement: the applicable system-disk procedure
must include automatic partitioning and deletion of swap. The original also assigns
the freed space to `/home`. Multiple-disk manual partitioning and management-only
installation remain separate scenarios; the answer must not silently merge them.

Repeated drafts overgeneralized branch-specific steps into a common procedure. The
generation and review instructions now explicitly retain procedural branches and
required removal actions, including the scope inherited from answer headings.

Another repeat failed with `answer_citation_invalid` before semantic review. The
citation-repair request previously omitted the actual rejected targets. It now supplies
the precise list as diagnostic data. For a review quotation bound to the wrong
observation, feedback now lists every observation containing that exact quotation.
These are locators only: the unchanged evidence, original provenance, applicable
scenario and resulting answer must still pass normal validation. No quotation is
normalized or automatically reassigned to a different source.

`tests/test_answer_binding_recovery.py` reproduces both missing-diagnostic paths
through the public conversation operation. Both failed before these changes and
passed afterwards; the focused answer/query suite passed 101 tests. Actual-model
results are recorded separately in release verification artifacts so a passing
transport test is not mistaken for semantic acceptance of the real question.
