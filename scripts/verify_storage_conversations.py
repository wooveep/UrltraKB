"""Profile real SDK loopback conversation catch-up and retained checkpoint costs."""

import argparse
import cProfile
import hashlib
import json
import platform
import pstats
import sqlite3
import statistics
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter, thread_time

from ctree import CTree

from ultrakb.application.conversation_memory import ConversationMemory
from ultrakb.application.execution import ExecutionContext
from ultrakb.config import save_config
from ultrakb.locks import atomic_write_text
from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.database import create_database
from ultrakb.storage.execution_store import ExecutionStore
from ultrakb.storage.maintenance import Maintenance
from ultrakb.storage.root_retirement import RootRetirement
from ultrakb.storage.session_store import SessionStore


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def response(request, _):
    text = request["messages"][-1]["content"]
    assert "ASSISTANT_ONLY" not in text
    payload = json.loads(text.rsplit("\n", 1)[1])
    if "child_count" in payload:
        middle, end = payload["child_count"] // 2, payload["child_count"]
        value = {
            "groups": [
                {"title": "Earlier preferences", "start": 0, "end": middle},
                {"title": "Recent preferences", "start": middle, "end": end},
            ]
        }
    elif "new_exchange" in payload:
        value = {"belongs_to_current": True}
    else:
        value = {"title": "Public preferences"}
    return {"content": json.dumps(value)}


def storage_metrics(root, identity, memory):
    roots = memory.store.memory_roots(memory.session_id, memory.policy_key)
    with storage_stage(root, identity, read_only=True) as db:
        checkpoints = db.connection.execute(
            "SELECT c.checkpoint_id, b.byte_count FROM ukb_conversation_checkpoints c "
            "JOIN ukb_blobs b ON b.digest = c.state_digest"
        ).fetchall()
        originals = db.connection.execute(
            "SELECT count(*), coalesce(sum(b.byte_count), 0) FROM ukb_turns t "
            "JOIN ukb_blobs b ON b.digest = t.payload_digest"
        ).fetchone()
        trees = db.connection.execute(
            "SELECT count(*) FROM ukb_tree_builds WHERE protocol = 'chatindex'"
        ).fetchone()[0]
        projected = db.connection.execute(
            "SELECT t.tree_id, coalesce(sum(b.byte_count), 0) FROM ukb_tree_builds t "
            "LEFT JOIN ukb_blob_refs r ON r.owner_kind = 'tree-build' AND r.owner_id = t.tree_id "
            "LEFT JOIN ukb_blobs b ON b.digest = r.digest WHERE t.protocol = 'chatindex' "
            "GROUP BY t.tree_id"
        ).fetchall()
    retained = {roots.computed.checkpoint_id, roots.projected.checkpoint_id}
    preview = RootRetirement(root, identity).preview(max_items=1000)
    garbage = Maintenance(root, identity).preview(max_items=1000)
    return {
        "unique_original_turn_count": originals[0],
        "unique_original_turn_payload_bytes": originals[1],
        "accepted_checkpoint_count": len(checkpoints),
        "encoded_checkpoint_bytes": sum(size for _, size in checkpoints),
        "retained_checkpoint_bytes": sum(size for key, size in checkpoints if key in retained),
        "unbound_checkpoint_bytes": sum(size for key, size in checkpoints if key not in retained),
        "retirable_checkpoints": preview.candidate_checkpoints,
        "projection_count": trees,
        "projection_reference_bytes": sum(size for _, size in projected),
        "retained_projection_reference_bytes": sum(
            size for key, size in projected if key == roots.tree_id
        ),
        "unbound_projection_reference_bytes": sum(
            size for key, size in projected if key != roots.tree_id
        ),
        "collectable_trees": garbage.candidate_trees,
        "interpretation": (
            "Reference payload bytes may overlap; SQL tree rows "
            "and database/WAL allocation are separate costs."
        ),
    }


def sdk_profile(profile):
    result = {}
    for (filename, _, name), (_, calls, own, cumulative, _) in pstats.Stats(profile).stats.items():
        if filename.replace("\\", "/").endswith("/ctree/ctree.py") and name in {
            "snapshot_state",
            "restore_state",
            "export_retrieval_tree",
        }:
            result[name] = {
                "calls": calls,
                "own_wall_seconds": own,
                "cumulative_wall_seconds": cumulative,
            }
    return result


def final_sdk_cpu(tree):
    snapshot_times, restore_times = [], []
    for _ in range(20):
        began = thread_time()
        snapshot = tree.snapshot_state()
        snapshot_times.append(thread_time() - began)
        began = thread_time()
        restored = CTree.restore_state(snapshot)
        restore_times.append(thread_time() - began)
        assert restored.snapshot_state() == snapshot
    return {
        "samples": 20,
        "snapshot_median_cpu_seconds": statistics.median(snapshot_times),
        "snapshot_maximum_cpu_seconds": max(snapshot_times),
        "restore_median_cpu_seconds": statistics.median(restore_times),
        "restore_maximum_cpu_seconds": max(restore_times),
        "scope": "Unprofiled pure SDK calls on the final prefix, measured with thread_time.",
    }


def run_case(directory, mode, turns, transport):
    root = directory / mode
    identity = create_database(root)
    save_config(
        root / ".ultrakb/config.yaml",
        {
            "model": "openai/gpt-4o-mini",
            "language": "en",
            "model_policy": {"max_calls": 5000, "retries": 0},
            "model_capacity": {"max_input_tokens": 65536, "tokenizer_model": "gpt-4o-mini"},
        },
    )
    store = SessionStore(root, identity)
    store.create("calibration", model="openai/gpt-4o-mini", language="en")
    context = ExecutionContext(storage_identity=identity)
    profile = cProfile.Profile()
    calls = []

    def append(ordinal):
        store.append_exchange(
            "calibration",
            f"turn-{ordinal}",
            f"Preference {ordinal}. " + "Public user content. " * 50,
            f"ASSISTANT_ONLY_{ordinal}: " + "Display answer. " * 40,
        )

    with transport(response) as service:
        atomic_write_text(
            root / ".env", f"LLM_API_KEY=loopback-only\nOPENAI_API_BASE={service.url}\n"
        )
        with context.begin(root):
            memory = ConversationMemory.from_executor(store, "calibration", context.executor)

            def catch_up():
                # Use the existing per-question renewal; the logical task budget
                # and the SDK's per-stage six-send allowance remain unchanged.
                context.executor.budget.reset_question()
                previous = memory.state().computed_turns
                sent = len(service.requests)
                value = memory.catch_up()
                assert value.computed_turns > previous
                calls.append(
                    {
                        "computed_turns": value.computed_turns,
                        "indexed_turns": value.indexed_turns,
                        "sends": len(service.requests) - sent,
                        "diagnostics": value.diagnostics,
                    }
                )
                assert value.computed_turns == value.indexed_turns
                return value

            began, cpu_began = perf_counter(), thread_time()
            profile.enable()
            try:
                if mode == "batch_backlog":
                    for ordinal in range(turns):
                        append(ordinal)
                    while memory.state().pending_turns:
                        catch_up()
                elif mode == "continuous":
                    for ordinal in range(turns):
                        append(ordinal)
                        catch_up()
                else:
                    # A bounded final backlog on a long accepted prefix gives a
                    # like-for-like comparison without a stage-budget interruption.
                    for ordinal in range(turns - 3):
                        append(ordinal)
                        catch_up()
                    for ordinal in range(turns - 3, turns):
                        append(ordinal)
                    assert catch_up().computed_turns == turns
            finally:
                profile.disable()
            elapsed, cpu = perf_counter() - began, thread_time() - cpu_began
            before = len(service.requests)
            assert memory.catch_up().pending_turns == 0
            assert len(service.requests) == before
            restored = store.restore_checkpoint("calibration", memory.policy_key)
            snapshot = restored.snapshot_state()
            assert restored.turns == list(store.load("calibration").turns)
            assert CTree.restore_state(snapshot).snapshot_state() == snapshot
            retrieval = restored.export_retrieval_tree(content_roles=("user",))
            assert "ASSISTANT_ONLY" not in encoded(retrieval).decode()
            assert len(snapshot["turns"]) == turns
            metrics = storage_metrics(root, identity, memory)
            pure_cpu = final_sdk_cpu(restored)
            assert metrics["accepted_checkpoint_count"] == turns
            assert metrics["projection_count"] == len(calls)
            assert all(call["sends"] <= 6 for call in calls)
            actual_sends = len(service.requests)
    profiled = sdk_profile(profile)
    assert all(value["cumulative_wall_seconds"] <= elapsed * 1.01 for value in profiled.values())
    assert (
        ExecutionStore(root, identity).status(context.storage_execution.execution_id) == "completed"
    )
    return {
        "mode": mode,
        "turns": turns,
        "wall_seconds": elapsed,
        "main_thread_cpu_seconds": cpu,
        "sdk_profile": profiled,
        "final_prefix_sdk_cpu": pure_cpu,
        "actual_loopback_model_sends": actual_sends,
        "catch_up_calls": len(calls),
        "single_tail_batch_turns": 3 if mode == "single_tail_batch" else None,
        "full_final_snapshot_bytes": len(encoded(snapshot)),
        "final_tree_state_without_turns_bytes": len(
            encoded({k: v for k, v in snapshot.items() if k != "turns"})
        ),
        "full_snapshot_sha256": hashlib.sha256(encoded(snapshot)).hexdigest(),
        "retrieval_sha256": hashlib.sha256(encoded(retrieval)).hexdigest(),
        "retained_full_turns_equal": True,
        "assistant_excluded_from_analysis_and_retrieval": True,
        "storage": metrics,
        "calls": calls,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--turns", type=int, default=100)
    args = parser.parse_args()
    if not 3 <= args.turns <= 1000:
        parser.error("--turns must be 3..1000")
    # Reuse the repository's provider-boundary HTTP fixture. No SDK or application
    # method is replaced; this calibration requires a complete source checkout.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from stream_transport import StreamTransport

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="conversation-", dir=args.output.parent) as temporary:
        cases = [
            run_case(Path(temporary), mode, args.turns, StreamTransport)
            for mode in ("batch_backlog", "continuous", "single_tail_batch")
        ]
    assert len({case["full_snapshot_sha256"] for case in cases}) == 1
    assert len({case["retrieval_sha256"] for case in cases}) == 1
    assert cases[1]["actual_loopback_model_sends"] == cases[2]["actual_loopback_model_sends"]
    result = {
        "schema_version": 1,
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
        },
        "cases": cases,
        "interpretation": (
            "Profiled single-host SDK loopback; no external model calls. "
            "Stage-budget continuations remain visible; "
            "no claim of linear long-session cost."
        ),
    }
    atomic_write_text(args.output, json.dumps(result, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "turns": args.turns}))


if __name__ == "__main__":
    main()
