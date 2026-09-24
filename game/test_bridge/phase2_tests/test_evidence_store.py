"""Contract tests for append-only Phase 2 evidence persistence."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from game.test_bridge.evidence_store import EvidenceStore  # pylint: disable=import-error


def test_jsonl_and_sqlite_store_the_same_traceable_run(tmp_path: Path) -> None:
    """One normalized payload must feed both evidence formats."""
    store = EvidenceStore(
        tmp_path,
        "pytest",
        run_id="pytest-fixed-run",
        metadata={"suite": "unit"},
    )
    case_event = store.record(
        "test_case",
        name="example::case",
        status="failed",
        duration_ms=12.5,
        error_type="AssertionError",
        message="expected 15 residents",
        request_id="request-1",
        command_id=None,
        session_id="session-1",
        world_generation="world-1",
        state_version=42,
        event_sequence=None,
        details={"phase": "call"},
    )
    store.finish(
        status="failed",
        total=1,
        passed=0,
        failed=1,
        skipped=0,
        other=0,
        duration_ms=15.0,
    )

    jsonl_events = [
        json.loads(line)
        for line in store.jsonl_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [event["eventType"] for event in jsonl_events] == [
        "run_started",
        "test_case",
        "run_finished",
    ]
    assert jsonl_events[1] == case_event
    assert jsonl_events[1]["commandId"] is None
    assert jsonl_events[1]["eventSequence"] is None

    with sqlite3.connect(store.sqlite_path) as connection:
        run_row = connection.execute(
            """
            SELECT status, total, passed, failed, skipped, other, duration_ms
            FROM runs WHERE run_id = ?
            """,
            (store.run_id,),
        ).fetchone()
        event_row = connection.execute(
            """
            SELECT event_type, status, request_id, command_id, state_version,
                   details_json
            FROM evidence_events
            WHERE run_id = ? AND event_type = 'test_case'
            """,
            (store.run_id,),
        ).fetchone()

    assert run_row == ("failed", 1, 0, 1, 0, 0, 15.0)
    assert event_row == (
        "test_case",
        "failed",
        "request-1",
        None,
        42,
        '{"phase":"call"}',
    )


def test_finished_store_rejects_late_evidence(tmp_path: Path) -> None:
    """Late writes must not make JSONL and SQLite disagree."""
    store = EvidenceStore(tmp_path, "pytest", run_id="pytest-finished-run")
    store.finish(
        status="passed",
        total=0,
        passed=0,
        failed=0,
        skipped=0,
        other=0,
        duration_ms=1.0,
    )

    with pytest.raises(RuntimeError, match="run has finished"):
        store.record("test_case", name="too-late", status="passed")
