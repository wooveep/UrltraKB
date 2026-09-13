# Evidence checks for answers

Source query tools capture one published source/version/parse/index snapshot.
`search_source_text` searches original block text for a case-insensitive literal,
without consulting generated summaries. It returns the total matching block count,
bounded original text and row/header context, exact citations and a pagination
cursor. A match count describes that literal only; it does not establish aliases,
synonyms or actual network behavior. Follow incomplete context through the returned
source node cursor before using a row.

Answers that cite sources or read knowledge-base content receive independent model
review after the terminal answer finishes. The reviewer receives the question,
the observed tool outputs and every answer text unit. Each unit needs its own
verdict; supported units require exact quotations from identified observations.
The application checks coverage of all unit IDs, quoted text, response shape and
located rejection feedback. Citation targets must also have been observed. These
checks do not prove semantic entailment: model review can still make mistakes,
so real acceptance tests must compare claims and omissions with the original.

Review covers optional explanations, parenthetical aliases, table conditions,
matching-row completeness and exact figure associations. An image's page association
does not establish that the asset depicts the whole page. OCR and visual observations
keep their distinct meanings; missing image understanding remains unknown.

Streamed application answers have one shared replacement allowance for truncation,
unobserved citation targets or a semantic rejection. The replacement retains the
evidence, has no tools and must pass review again. Review instructions, rejected
drafts and review results never become completed conversation history. Invalid or
truncated reviews cannot authorize completion. Legacy terminal paths also reject
unsupported answers. Review requests use the configured verification model options
and share task limits, cancellation cleanup and usage accounting with answer requests.

The import policy remains: “导入文档，异常的情况，可以丢弃，知识可以缺失，任务不能随意中止与判断失败。”
Local omissions do not discard independent usable content. Original evidence and
the full coverage denominator remain available; a partial publication is not proof
of a complete answer or a complete analysis of every image.
