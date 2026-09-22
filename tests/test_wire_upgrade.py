"""Formal document-protocol revisions are content-addressed cache boundaries."""


def _revision_with(monkeypatch, code):
    from types import SimpleNamespace

    from openkb import implementation

    monkeypatch.setattr(
        implementation,
        "find_spec",
        lambda name: SimpleNamespace(loader=SimpleNamespace(get_code=lambda name: code)),
    )
    implementation.module_revision.cache_clear()
    return implementation


def test_document_protocol_revision_is_independent_of_compiled_file_path(monkeypatch):
    from importlib.util import find_spec

    from openkb import implementation

    name = "openkb.agent.document_protocol"
    loader = find_spec(name).loader
    current = implementation.module_revision(name)
    code = compile(loader.get_source(name), "C:/_MEI_TEST/document_protocol.pyc", "exec")
    patched = _revision_with(monkeypatch, code)
    try:
        assert patched.module_revision(name) == current
    finally:
        patched.module_revision.cache_clear()


def test_document_protocol_revision_changes_when_its_rules_change(monkeypatch):
    from importlib.util import find_spec

    from openkb import implementation

    name = "openkb.agent.document_protocol"
    loader = find_spec(name).loader
    current = implementation.module_revision(name)
    source = loader.get_source(name) + '\n_NEW_SEMANTIC_RULE = "Changed rules"\n'
    code = compile(source, "C:/_MEI_TEST/document_protocol.pyc", "exec")
    patched = _revision_with(monkeypatch, code)
    try:
        assert patched.module_revision(name) != current
    finally:
        patched.module_revision.cache_clear()
