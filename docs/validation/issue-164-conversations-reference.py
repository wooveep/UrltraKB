"""Archived pre-activation reference; run against a56b0f59 with REPO OUTPUT_DIR arguments."""

# ruff: noqa: E402

import hashlib
import json
import logging
import platform
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

REPOSITORY = Path(sys.argv[1]).expanduser().resolve()
sys.path.insert(0, str(REPOSITORY))
sys.path.insert(0, str(REPOSITORY / "tests"))

from stream_transport import StreamTransport

from scripts.verify_storage_conversations import encoded, response
from ultrakb.agent.chat_session import ChatSession
from ultrakb.application.execution import ExecutionContext
from ultrakb.application.knowledge_bases import initialize_kb
from ultrakb.config import save_config
from ultrakb.context.conversation import ConversationMemory
from ultrakb.context.projection import open_runtime
from ultrakb.conversation_turns import ConversationTurn, exchange_digest
from ultrakb.locks import atomic_write_text, kb_ingest_lock, session_lock

logging.disable(logging.INFO)


def normalized(value, identities):
    if isinstance(value, dict):
        return {key: normalized(item, identities) for key, item in value.items()}
    if isinstance(value, list):
        return [normalized(item, identities) for item in value]
    return identities.get(value, value) if isinstance(value, str) else value


def run_case(directory, mode, turns):
    root = directory / mode
    initialize_kb(root, model="openai/gpt-4o-mini", language="en")
    save_config(
        root / ".ultrakb/config.yaml",
        {
            "model": "openai/gpt-4o-mini",
            "language": "en",
            "model_policy": {"max_calls": 5000, "retries": 0},
            "model_capacity": {"max_input_tokens": 65536, "tokenizer_model": "gpt-4o-mini"},
        },
    )
    session = ChatSession.new(root, "openai/gpt-4o-mini", "en", identity="calibration")
    context = ExecutionContext()
    calls = []

    def append(ordinal):
        user = f"Preference {ordinal}. " + "Public user content. " * 50
        assistant = f"ASSISTANT_ONLY_{ordinal}: " + "Display answer. " * 40
        session.turns.append(
            ConversationTurn(
                f"turn-{ordinal}",
                ordinal + 1,
                user,
                assistant,
                exchange_digest(user, assistant),
                f"attempt-{ordinal}",
            )
        )
        session.save()

    with StreamTransport(response) as service:
        atomic_write_text(
            root / ".env", f"LLM_API_KEY=loopback-only\nOPENAI_API_BASE={service.url}\n"
        )
        with session_lock(root, session.id), kb_ingest_lock(root / ".ultrakb"), context.begin(root):

            def memory():
                return ConversationMemory(session, context.executor)

            def catch_up():
                context.executor.budget.reset_question()
                previous, sent = memory().state().indexed_turns, len(service.requests)
                value = memory().catch_up()
                assert value.indexed_turns > previous
                calls.append(
                    {
                        "computed_turns": value.indexed_turns,
                        "indexed_turns": value.indexed_turns,
                        "sends": len(service.requests) - sent,
                        "diagnostics": value.diagnostics,
                    }
                )

            began = perf_counter()
            if mode == "batch_backlog":
                for ordinal in range(turns):
                    append(ordinal)
                while memory().state().pending_turns:
                    catch_up()
            elif mode == "continuous":
                for ordinal in range(turns):
                    append(ordinal)
                    catch_up()
            else:
                for ordinal in range(turns - 3):
                    append(ordinal)
                    catch_up()
                for ordinal in range(turns - 3, turns):
                    append(ordinal)
                catch_up()
            elapsed = perf_counter() - began
            before = len(service.requests)
            assert memory().catch_up().pending_turns == 0
            assert len(service.requests) == before
            with open_runtime(root) as db:
                tree, _, diagnostics = memory()._restore(db)
            assert not diagnostics
            snapshot = tree.snapshot_state()
            retrieval = tree.export_retrieval_tree(content_roles=("user",))
            assert len(snapshot["turns"]) == turns
            identities = {
                turn.turn_id: f"turn-{ordinal}" for ordinal, turn in enumerate(session.turns)
            }
            snapshot, retrieval = (
                normalized(snapshot, identities),
                normalized(retrieval, identities),
            )
            atomic_write_text(
                Path(sys.argv[2]).expanduser().resolve() / f"legacy-{mode}-snapshot.json",
                json.dumps({"snapshot": snapshot, "retrieval": retrieval}, indent=2),
            )
            assert "ASSISTANT_ONLY" not in encoded(retrieval).decode()
            sends = len(service.requests)
    return {
        "mode": mode,
        "turns": turns,
        "wall_seconds": elapsed,
        "actual_loopback_model_sends": sends,
        "catch_up_calls": len(calls),
        "calls": calls,
        "normalized_full_snapshot_sha256": hashlib.sha256(encoded(snapshot)).hexdigest(),
        "normalized_retrieval_sha256": hashlib.sha256(encoded(retrieval)).hexdigest(),
        "identity_normalization": (
            "Only complete original turn-id string values map to turn-ordinal; "
            "all other SDK fields and content remain unchanged."
        ),
    }


def main():
    parent = Path(sys.argv[2]).expanduser().resolve()
    from ultrakb import config

    isolated = parent / "conversation-reference-settings"
    config.GLOBAL_CONFIG_DIR = isolated
    config.GLOBAL_CONFIG_PATH = isolated / "global.yaml"
    config.GLOBAL_CONFIG_LOCK_PATH = isolated / "global.lock"
    reference = json.loads(
        (REPOSITORY / "docs/validation/issue-164-conversations-benchmark.json").read_text()
    )
    cases = []
    with tempfile.TemporaryDirectory(
        prefix="legacy-conversation-reference-", dir=parent
    ) as directory:
        for mode in ("batch_backlog", "continuous", "single_tail_batch"):
            case = run_case(Path(directory), mode, 100)
            expected = next(item for item in reference["cases"] if item["mode"] == mode)
            case["main_store_model_sends"] = expected["actual_loopback_model_sends"]
            case["same_model_sends"] = (
                case["actual_loopback_model_sends"] == case["main_store_model_sends"]
            )
            case["same_accepted_prefixes_and_sends_per_call"] = [
                (item["computed_turns"], item["sends"]) for item in case["calls"]
            ] == [(item["computed_turns"], item["sends"]) for item in expected["calls"]]
            case["same_full_sdk_state_after_turn_identity_normalization"] = (
                case["normalized_full_snapshot_sha256"] == expected["full_snapshot_sha256"]
            )
            case["same_retrieval_after_turn_identity_normalization"] = (
                case["normalized_retrieval_sha256"] == expected["retrieval_sha256"]
            )
            cases.append(case)
            print(
                json.dumps({key: value for key, value in case.items() if key != "calls"}),
                flush=True,
            )
    report = {
        "schema_version": 1,
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "legacy_reference_commit": "a56b0f59",
        "comparison_scope": (
            "Identical fixture turn IDs, text, SDK, response rules and per-invocation "
            "stage allowance; normalization is identity for these fixed IDs."
        ),
        "main_store_reference_commit": "9520f731",
        "environment": {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version},
        "cases": cases,
    }
    output = parent / "conversation-legacy-reference.json"
    atomic_write_text(output, json.dumps(report, indent=2) + "\n")
    assert all(
        case[key]
        for case in cases
        for key in (
            "same_model_sends",
            "same_accepted_prefixes_and_sends_per_call",
            "same_full_sdk_state_after_turn_identity_normalization",
            "same_retrieval_after_turn_identity_normalization",
        )
    )


if __name__ == "__main__":
    main()
