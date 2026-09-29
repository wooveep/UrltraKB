# Document classification and execution

PDFs of 10 physical pages or fewer are short by default. A short document normally
uses the full-document compiler; a long document uses PageIndex and the segmented
compiler. Blank pages count. Printed page labels do not change the classification.

Set `pdf_short_max_pages` in global or KB settings to change the inclusive limit.
Zero means every nonempty PDF is long. Existing explicit `pageindex_threshold: N`
continues to mean segmented from page N (`N - 1` is the short limit); zero or
negative legacy thresholds still force indexing. Within a layer the new key wins;
an explicit KB policy wins over a global policy, including a global new key.
Existing values are never guessed to be defaults. New KBs inherit this policy.

Settings merge patches leave absent keys unchanged. `null` removes that override;
Nested capacity patches preserve unspecified fields in the same layer.
removing a new key can reveal an existing legacy key in the same layer. In raw
YAML an explicit new-key `null` inherits from the next layer. Boolean page limits
are invalid and are not silently treated as zero or one. Execution snapshots keep
the selected policy and its source fixed for the running task.

`model_capacity` optionally declares `max_input_tokens`, or
`context_window_tokens` together with `output_reserve_tokens`. For example:

```yaml
model_capacity:
  context_window_tokens: 32768
  output_reserve_tokens: 4096
  tokenizer_model: gpt-4o
```

`tokenizer_model` explicitly declares the measurement model when an endpoint uses
a private alias. Automatic limits come only from exact entries in the installed,
pinned LiteLLM catalog. Automatic measurement is restricted to known OpenAI chat
models with a recognized tiktoken encoding. Custom endpoints require explicit
capacity and measurement information; other uncertain cases stay unknown.
Knowing a context window alone does not imply a zero output reserve.

Only the first summary request is estimated, including its system/schema prompt.
Known insufficiency switches the execution to segmented while the length class
remains **short**. Unknown capacity still attempts the full request. This is not
a guarantee that every later model request fits. No pre-summary or truncation is
introduced; existing PageIndex and compiler content algorithms are used.

`openkb --kb-dir /path/to/kb settings` (or `settings --global`) reports effective
values, their origins, and capacity diagnostics without API keys. The settings
API and desktop settings expose the same fields. Source reads, document lists,
and import outcomes report saved classification and execution separately.
Failed or pending imports expose `target_processing`. When an older successful
body is displayed, `processing` continues to describe that body; its replacement's
decision is kept separate. A first failed compile can therefore still report the
retained classification, estimate, and unknown-capacity reason.
Recompilation reuses the retained normalization, index, and execution decision;
changing settings does not silently reclassify old input. Historical reads show
the decision belonging to the selected knowledge revision.
