"""Real, local core calls on disposable KBs; does not evaluate a live language model."""

import os
import threading
from pathlib import Path

from openkb.portable_prototype.paths import assets


def run_core(output, tag, emit):
    # Only this isolated worker changes cwd. The GUI remains in its launch directory.
    output.mkdir(parents=True, exist_ok=True)
    os.chdir(output)
    os.environ["TIKTOKEN_CACHE_DIR"] = str(assets() / "tiktoken-cache")
    # Interactive prototype runs must not register scratch KBs in the user's
    # real profile. Clean-container acceptance explicitly uses the unmodified path.
    if profile := os.environ.get("OPENKB_PROBE_PROFILE"):
        import openkb.config as config_module

        config_module.GLOBAL_CONFIG_DIR = Path(profile)
        config_module.GLOBAL_CONFIG_PATH = Path(profile) / "global.yaml"
    from agents import set_tracing_disabled

    set_tracing_disabled(True)
    from openkb.cli import get_kb_list, initialize_kb
    from openkb.config import GLOBAL_CONFIG_PATH, resolve_effective_config
    from openkb.converter import convert_document
    from openkb.locks import atomic_write_text, kb_ingest_lock
    from openkb.state import HashRegistry

    kb = output / f"知识库 {tag}"
    initialize_kb(kb, model=f"probe-model-{tag}")
    facts = {
        "kb": str(kb),
        "model": resolve_effective_config(kb)[0]["model"],
        "global_config_path": str(GLOBAL_CONFIG_PATH),
        "conversions": [],
    }
    source_root = assets() / "fixtures"
    files = list(source_root.glob("*")) if tag == "A" else [source_root / "中文 笔记.md"]
    for path in files:
        if path.suffix == ".png":
            continue
        result = convert_document(path, kb)
        if result.is_long_doc:
            assert path.name == "长文.pdf"
            row = {"name": path.name, "branch": "long-document", "pages": 20}
        else:
            text = result.source_path.read_text(encoding="utf-8")
            assert "PortableProbe" in text, (path.name, text[:150])
            with kb_ingest_lock(kb / ".openkb"):
                HashRegistry(kb / ".openkb" / "hashes.json").add(
                    result.file_hash,
                    {"doc_name": result.doc_name, "name": path.name, "type": path.suffix},
                )
            assert convert_document(path, kb).skipped
            row = {
                "name": path.name,
                "branch": "converted",
                "bytes": len(text.encode()),
                "deduplicated": True,
            }
        facts["conversions"].append(row)
        emit("converted", {"tag": tag, **row})
    facts["inventory_count"] = len(get_kb_list(kb)["documents"])
    if tag == "B":
        return facts

    from openkb.watcher import start_watch

    seen = []
    ready = threading.Event()

    def changed(paths):
        seen.extend(paths)
        ready.set()

    observer = start_watch(kb / "raw", changed, debounce=0.1)
    try:
        atomic_write_text(kb / "raw" / "原子 保存.md", "PortableProbe atomic rename")
        atomic_observed = ready.wait(0.5)
        ready.clear()
        # Simulate a user copying a new raw file; this is input, not a wiki mutation.
        (kb / "raw" / "监听 样本.md").write_text("PortableProbe watch", encoding="utf-8")
        assert ready.wait(5), "packaged watcher did not observe the write"
        assert any(Path(path).name == "监听 样本.md" for path in seen)
    finally:
        observer.stop()
        observer.join(5)
    assert not observer.is_alive()
    facts["watcher"] = {
        "observed": seen,
        "stopped": True,
        "atomic_rename_observed": atomic_observed,
    }

    from openkb.agent.skills import scan_local_skills
    from openkb.prompts import load_prompt
    from openkb.visualize import build_graph, render_html

    with kb_ingest_lock(kb / ".openkb"):
        atomic_write_text(
            kb / "wiki" / "concepts" / "甲.md", "---\ntype: Concept\n---\n# 甲\n[[concepts/乙]]"
        )
        atomic_write_text(
            kb / "wiki" / "concepts" / "乙.md", "---\ntype: Concept\n---\n# 乙\nPortableProbe"
        )
    graph = build_graph(kb / "wiki")
    assert len(graph["nodes"]) == 2 and len(graph["edges"]) == 1
    html = render_html(graph)
    (output / "graph.html").write_text(html, encoding="utf-8")
    assert "__GRAPH_DATA__" not in html and len(html) > 1000
    skills = scan_local_skills(kb)
    names = {s["name"] for s in skills}
    assert {"openkb-deck-neon", "openkb-deck-editorial", "openkb-html-critic"} <= names, names
    assert len(load_prompt("skill_create")) > 100
    facts["resources"] = {"graph_nodes": 2, "graph_edges": 1, "builtin_skills": sorted(names)}

    import onnxruntime
    import tiktoken
    from magika import Magika

    kind = Magika().identify_path(source_root / "中文 演示.pptx").output.ct_label
    assert kind == "pptx", kind
    tokens = tiktoken.get_encoding("o200k_base").encode("PortableProbe 中文")
    assert tokens
    facts["native_dependencies"] = {
        "onnxruntime": onnxruntime.__version__,
        "magika_inference": kind,
        "tiktoken_count": len(tokens),
    }
    import trafilatura

    extracted = trafilatura.extract(
        (source_root / "中文 页面.html").read_text(encoding="utf-8"),
        output_format="markdown",
        include_links=True,
    )
    assert extracted and "PortableProbe" in extracted
    facts["html_extraction"] = {"characters": len(extracted), "network_fetch_evaluated": False}
    return facts


def prepare_recovery(output):
    from openkb.locks import atomic_write_text, kb_ingest_lock
    from openkb.mutation import snapshot_paths

    target = output / "恢复知识库" / "wiki" / "concepts" / "恢复.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    with kb_ingest_lock(output / "恢复知识库" / ".openkb"):
        atomic_write_text(target, "PortableProbe committed")
        snapshot_paths(output / "恢复知识库", [target], operation="prototype-intentional-crash")
        atomic_write_text(target, "UNCOMMITTED")
        os._exit(23)


def recover(output):
    from openkb.locks import kb_ingest_lock

    kb = output / "恢复知识库"
    with kb_ingest_lock(kb / ".openkb"):
        value = (kb / "wiki" / "concepts" / "恢复.md").read_text(encoding="utf-8")
    assert value == "PortableProbe committed", value
    return {"recovered_after_exit_23": True, "text": value}


def stop_at_boundary(output, stop, emit):
    from openkb.locks import atomic_write_text, kb_ingest_lock

    completed = []
    for index in range(20):
        if stop.is_set():
            break
        with kb_ingest_lock(output / "停止知识库" / ".openkb"):
            path = output / "停止知识库" / f"{index}.md"
            atomic_write_text(path, f"PortableProbe item {index}")
            completed.append(index)
        emit("boundary", {"completed": list(completed)})
        stop.wait(0.2)
    assert 0 < len(completed) < 20, completed
    return {"completed": completed, "safe_stop": stop.is_set()}
