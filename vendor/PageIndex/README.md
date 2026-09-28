# PageIndex — local indexing for UrltraKB

This is UrltraKB's local-source adaptation of
[VectifyAI/PageIndex](https://github.com/VectifyAI/PageIndex), based on tag
`v0.3.0.dev3`. The upstream MIT license and attribution remain in [LICENSE](LICENSE).
See [UPSTREAM.json](UPSTREAM.json) for the original file hashes and local changes.

This package builds hierarchical document indexes, stores collections in SQLite,
and provides retrieval tools. Document parsing and storage happen locally. Model
requests use the LLM provider configured by the caller, which may be a local or
remote provider.

The managed-service backend, service-key constructor, remote SDK compatibility
methods and hosted-service examples have been removed. `PageIndexClient` and
`LocalClient` both use local storage; there is no automatic backend switch.

## Install from this repository

From the **UrltraKB root**:

```bash
uv sync --frozen --extra desktop --extra api --extra dev
```

For pip, install both editable projects together:

```bash
pip install -e ./vendor/PageIndex -e ".[desktop,api,dev]"
```

## Local collection API

Configure the chosen model provider before indexing. For example, an OpenAI
model uses `OPENAI_API_KEY`; a local provider can use its corresponding LiteLLM
model name and endpoint.

```python
from pageindex import LocalClient

client = LocalClient(model="gpt-5.4", storage_path="./local-index")
papers = client.collection("papers")
doc_id = papers.add("paper.pdf")
tree = papers.get_document_structure(doc_id)
pages = papers.get_page_content(doc_id, "1-3")
answer = papers.query("What are the main findings?", doc_ids=[doc_id])
```

Use `client.register_parser(...)` for custom parsers and `IndexConfig` for
indexing options. Existing local examples are in [examples](examples):

- `local_demo.py`: indexing, storage and queries.
- `demo_query_modes.py`: retrieval modes.
- `agentic_vectorless_rag_demo.py`: agent tools over local documents.

## Standalone tree generation

From this directory, using the root environment:

```bash
../../.venv/bin/python run_pageindex.py --pdf_path /path/to/document.pdf
```

PDF parsing uses the local parser. There is no hosted OCR fallback.
The local indexing, parsing, storage and retrieval implementations are preserved
from the upstream tag.

## Build and validate

From the UrltraKB root:

```bash
uv build vendor/PageIndex --wheel --out-dir dist
(cd vendor/PageIndex && OPENAI_API_KEY=pageindex-test-only ../../.venv/bin/python -m pytest tests)
```

The dummy key in the test command satisfies provider validation. Tests replace
model requests with fixtures and do not need a live provider account. Two PDF
tests are skipped when the upstream sample fixtures are absent.

See [README.urltrakb.md](README.urltrakb.md) for source ownership and upgrades.
