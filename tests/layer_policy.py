"""Classify tests by their execution dependencies, without executing fixtures."""

import ast
import inspect
from functools import lru_cache

LAYERS = frozenset({"unit", "integration", "live"})
MARKERS = {
    "unit": "pure rules and request/response contracts; no external model traffic",
    "integration": "local filesystem, HTTP, persistence or process integration",
    "live": "real DeepSeek Flash responses through the accounted gateway",
    "native": "requires native desktop/document/process facilities (orthogonal tag)",
}
LOCAL_FIXTURES = frozenset(
    {"kb_dir", "tmp_path", "tmp_path_factory", "tmpdir", "tmpdir_factory", "pytester"}
)
NATIVE_FIXTURES = frozenset({"office_runtime", "prepared_cfb_helper", "qapp", "qtbot"})
MODEL_FIXTURES = frozenset(
    {
        "live_llm",
        "model_service",
        "_model",
        "model",
        "pdf_model",
        "pdf_index_model",
        "block_model",
        "source_llm",
        "narrated_model",
        "presentation_model",
        "single_slide_model",
        "native_index_llm",
        "chat_model",
    }
)
MODEL_CALLS = frozenset(
    {
        "LiveLLM",
        "compile_short_doc",
        "compile_long_doc",
        "run_query",
        "continue_conversation",
        "run_knowledge_lint",
        "generate_deck",
        "generate_skill",
    }
)
LOCAL_CALLS = frozenset(
    {
        "read_text",
        "read_bytes",
        "write_text",
        "write_bytes",
        "mkdir",
        "unlink",
        "atomic_write_text",
        "atomic_write_json",
        "TaskManager",
        "TestClient",
        "CliRunner",
        "mutation_scope",
        "kb_ingest_lock",
        "kb_read_lock",
        "ThreadPoolExecutor",
        "Process",
        "Popen",
    }
)


def is_live(item):
    return item.get_closest_marker("live") is not None


def default_selection(option, expression):
    """A generic -m filter must not accidentally opt into paid model work."""
    if option is not None:
        return option
    try:
        tree = ast.parse(expression or "False", mode="eval")
    except SyntaxError:
        return "offline"  # Pytest reports malformed/unsupported marker syntax.

    def positive_live(node, negated=False):
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return positive_live(node.operand, not negated)
        if isinstance(node, ast.Name):
            return node.id == "live" and not negated
        if isinstance(node, ast.Call):
            return positive_live(node.func, negated)
        return any(positive_live(child, negated) for child in ast.iter_child_nodes(node))

    return "all" if positive_live(tree) else "offline"


@lru_cache(maxsize=None)
def _calls(function):
    """Read only the collected callable, including class methods and wrappers."""
    import textwrap

    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(inspect.unwrap(function))))
    except (OSError, TypeError, IndentationError, SyntaxError):
        return None
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
            if isinstance(node.func.value, ast.Name):
                names.add(f"{node.func.value.id}.{node.func.attr}")
            if (
                node.func.attr == "getfixturevalue"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in MODEL_FIXTURES
            ):
                names.add("LiveLLM")
    return names


def intentional_fixtures(item):
    """Ambient settings isolation must not turn every pure rule into integration."""
    names = set(item._fixtureinfo.argnames)
    for mark in item.iter_markers("usefixtures"):
        names.update(mark.args)
    waiting = list(names)
    while waiting:
        name = waiting.pop()
        definitions = item._fixtureinfo.name2fixturedefs.get(name)
        if definitions:
            for dependency in definitions[-1].argnames:
                if dependency not in names:
                    names.add(dependency)
                    waiting.append(dependency)
    return names


def classify(item):
    """Explicit layers win; ambiguous model dependencies remain live."""
    explicit = {mark.name for mark in item.iter_markers() if mark.name in LAYERS}
    if len(explicit) > 1:
        raise ValueError(f"{item.nodeid}: conflicting test layers: {sorted(explicit)}")
    fixtures = set(item.fixturenames)
    unresolved = {
        name
        for name in fixtures & MODEL_FIXTURES
        if not item._fixtureinfo.name2fixturedefs.get(name)
    }
    if explicit:
        layer = explicit.pop()
        if layer != "live" and "live_llm" in fixtures:
            raise ValueError(
                f"{item.nodeid}: {layer} depends on live_llm; remove the real-model "
                "fixture or mark the test live"
            )
        if layer != "live" and unresolved:
            raise ValueError(
                f"{item.nodeid}: {layer} has unresolved model fixtures {sorted(unresolved)}; "
                "register the fixture provider and declare its actual execution layer"
            )
        return layer, "explicit marker"
    if unresolved:
        return "live", f"unresolved model fixtures {sorted(unresolved)}; conservative admission"
    if "live_llm" in fixtures:
        return "live", "real-model fixture dependency"
    if "real_application_model" in fixtures and "processing_only" not in fixtures:
        return "live", "application model setup; no explicit offline boundary"
    calls = _calls(item.obj)
    if calls is None:
        return "live", "uninspectable callable; conservative model admission"
    if calls & MODEL_CALLS or calls & {"Runner.run", "Runner.run_streamed"}:
        return "live", "model-producing callable"
    intentional = intentional_fixtures(item)
    if intentional & LOCAL_FIXTURES or fixtures & NATIVE_FIXTURES or calls & LOCAL_CALLS:
        return "integration", "local storage, fixture or process dependency"
    return "unit", "no model or local integration dependency"


def needs_native(item):
    if item.get_closest_marker("native") is not None:
        return True
    if set(item.fixturenames) & NATIVE_FIXTURES:
        return True
    module = inspect.getmodule(item.obj)
    try:
        text = inspect.getsource(module) if module else ""
    except (OSError, TypeError):
        return False
    return 'importorskip("PySide6")' in text or "importorskip('PySide6')" in text
