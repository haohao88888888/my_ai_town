"""Contract tests for append-only Phase 2 evidence persistence."""

from __future__ import annotations

import json
import hashlib
import shutil
import sqlite3
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

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


def test_pytest_failure_links_responses_in_jsonl_sqlite_and_junit(tmp_path: Path) -> None:
    """A real child pytest failure must retain all response links, not just the last."""
    child_root = tmp_path / "trace-child"
    child_root.mkdir()
    suite_root = Path(__file__).resolve().parent
    shutil.copyfile(suite_root / "conftest.py", child_root / "conftest.py")
    child_test = child_root / "test_controlled_trace.py"
    child_test.write_text(
        '''import json
import socket
import pytest
import requests

def response(payload):
    value = requests.Response()
    value.status_code = 200
    value._content = payload.encode("utf-8")
    return value

def test_controlled_failure(record_bridge_response):
    for number in (1, 2):
        payload = {"requestId": f"request-{number}", "data": {
            "commandId": f"command-{number}", "sessionId": "session-1",
            "worldGeneration": "world-1", "stateVersion": number}}
        record_bridge_response("GET /same-path", response(json.dumps(payload)), 200)
    assert False, "controlled business assertion failure"

def test_same_path_different_case(record_bridge_response):
    record_bridge_response("GET /same-path", response('{"requestId":"request-other"}'), 200)

@pytest.mark.parametrize("payload", ["null", "[]", "not-json"])
def test_malformed_capture_does_not_hide_failure(record_bridge_response, payload):
    record_bridge_response("GET /invalid-body", response(payload), 200)

def test_unhandled_timeout(http_session, monkeypatch):
    def timed_out(*args, **kwargs):
        raise requests.ReadTimeout("synthetic timeout with token=private")
    monkeypatch.setattr(http_session, "send", timed_out)
    http_session.get("http://127.0.0.1:1/world/state?token=private", timeout=0.1)

def test_unreachable_listener(http_session):
    with socket.socket() as unused_listener:
        unused_listener.bind(("127.0.0.1", 0))
        port = unused_listener.getsockname()[1]
        with pytest.raises(requests.ConnectionError):
            http_session.get(f"http://127.0.0.1:{port}/health", timeout=0.5)
''',
        encoding="utf-8",
    )
    evidence_root = child_root / "evidence"
    junit_path = child_root / "junit.xml"
    completed = subprocess.run(
        [
            sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "-c", str(suite_root.parent / "pytest.ini"), str(child_test),
            "--evidence-root", str(evidence_root), "--junitxml", str(junit_path),
            "--basetemp", str(child_root / "pytest-temp"),
        ],
        cwd=suite_root.parents[2], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30, check=False,
    )
    assert completed.returncode == 1, completed.stdout + completed.stderr
    jsonl_paths = list(evidence_root.glob("pytest-*.jsonl"))
    assert len(jsonl_paths) == 1
    events = [json.loads(line) for line in jsonl_paths[0].read_text("utf-8").splitlines()]
    failed_case = next(
        event for event in events
        if event["eventType"] == "test_case"
        and event["name"].endswith("::test_controlled_failure")
    )
    failed_id = failed_case["details"]["caseId"]
    response_ids = failed_case["details"]["responseEventIds"]
    responses = [event for event in events if event["eventId"] in response_ids]
    assert [event["requestId"] for event in responses] == ["request-1", "request-2"]
    assert [event["commandId"] for event in responses] == ["command-1", "command-2"]
    assert [event["details"]["stepIndex"] for event in responses] == [1, 2]
    assert all(event["details"]["caseId"] == failed_id for event in responses)
    assert len({event["details"]["stepId"] for event in responses}) == 2
    malformed = [event for event in events if event["name"] == "GET /invalid-body"]
    assert len(malformed) == 3
    assert all(event["requestId"] is None for event in malformed)
    assert all(event["details"]["responseDecodeError"] for event in malformed)
    _assert_persisted_failure(evidence_root, junit_path, failed_id, response_ids)
    _assert_transport_failure(events, evidence_root, junit_path)


def _assert_persisted_failure(
    evidence_root: Path, junit_path: Path, failed_id: str, response_ids: list[str],
) -> None:
    """Compare the failed case's SQL and JUnit links with its JSONL event IDs."""
    with sqlite3.connect(evidence_root / "phase2_evidence.sqlite3") as connection:
        sql_response_ids = [row[0] for row in connection.execute(
            "SELECT event_id FROM evidence_events WHERE event_type='bridge_response' "
            "AND json_extract(details_json, '$.caseId')=? ORDER BY sequence",
            (failed_id,),
        )]
        run_counts = connection.execute(
            "SELECT status,total,passed,failed,skipped FROM runs",
        ).fetchone()
    assert sql_response_ids == response_ids
    assert run_counts == ("failed", 7, 5, 2, 0)
    failed_xml = next(
        case for case in ET.parse(junit_path).iter("testcase")
        if case.get("name") == "test_controlled_failure"
    )
    properties = {item.get("name"): item.get("value")
                  for item in failed_xml.findall("properties/property")}
    assert properties["caseId"] == failed_id
    assert json.loads(properties["responseEventIds"]) == response_ids


def _assert_transport_failure(events: list[dict], evidence_root: Path, junit_path: Path) -> None:
    """Timeouts and an unreachable local listener retain links with null server IDs."""
    attempts = [event for event in events if event["eventType"] == "bridge_transport_error"]
    assert len(attempts) == 2
    assert attempts[0]["errorType"] == "ReadTimeout"
    # A bound non-listening socket may time out on Windows or refuse on other hosts.
    assert attempts[1]["errorType"] in {"ConnectionError", "ConnectTimeout"}
    assert all(event["durationMs"] >= 0 for event in attempts)
    for attempt in attempts:
        assert all(attempt[field] is None for field in (
            "requestId", "commandId", "sessionId", "worldGeneration",
            "stateVersion", "eventSequence",
        ))
        assert "private" not in json.dumps(attempt)
        case = next(event for event in events if event["eventType"] == "test_case"
                    and event["name"] == attempt["details"]["caseId"])
        assert case["details"]["responseEventIds"] == []
        assert case["details"]["transportEventIds"] == [attempt["eventId"]]
        assert case["details"]["evidenceEventIds"] == [attempt["eventId"]]
        expected_status = "failed" if attempt["errorType"] == "ReadTimeout" else "passed"
        assert case["status"] == expected_status
    with sqlite3.connect(evidence_root / "phase2_evidence.sqlite3") as connection:
        rows = connection.execute(
            "SELECT event_id, request_id, command_id FROM evidence_events "
            "WHERE event_type='bridge_transport_error' ORDER BY sequence",
        ).fetchall()
    assert rows == [(event["eventId"], None, None) for event in attempts]
    timeout_xml = next(case for case in ET.parse(junit_path).iter("testcase")
                       if case.get("name") == "test_unhandled_timeout")
    properties = {item.get("name"): item.get("value")
                  for item in timeout_xml.findall("properties/property")}
    assert timeout_xml.find("failure") is not None
    assert json.loads(properties["transportEventIds"]) == [attempts[0]["eventId"]]


def test_real_failure_retains_snapshot_and_websocket_timeline(
    tmp_path: Path, godot_executable: Path,
) -> None:
    """A deliberate child assertion failure must reference real isolated-game evidence.

    This is a reporter acceptance experiment, not a newly discovered product bug.
    No fake HTTP responses, injected game events or real save files are used.
    """
    child_root = tmp_path / "real-event-child"
    child_root.mkdir()
    suite_root = Path(__file__).resolve().parent
    shutil.copyfile(suite_root / "conftest.py", child_root / "conftest.py")
    child_test = child_root / "test_real_failure.py"
    child_test.write_text(
        '''import pytest
from game.test_bridge.phase2_tests.test_bridge_command_api import _world, _command, _json_request

pytestmark = [pytest.mark.live_bridge, pytest.mark.command_bridge]

def test_controlled_real_failure(http_session, command_bridge_base_url,
                                record_bridge_response, bridge_event_trace):
    before = _world(http_session, record_bridge_response, command_bridge_base_url)
    command = {**_command(before), "speed": 2 if before["simulationSpeed"] != 2 else 3}
    response = _json_request(http_session, record_bridge_response, "POST",
                             command_bridge_base_url, "/commands/set-speed", 200, payload=command)
    event = bridge_event_trace.wait_for_command(command["commandId"])
    assert event["details"]["gameEvent"]["result"] == response["data"]["result"]
    assert response["data"]["result"]["simulationSpeed"] == 4, (
        "intentional reporter acceptance failure")

def test_other_case_is_not_the_failed_case(http_session, command_bridge_base_url,
                                          record_bridge_response):
    after = _world(http_session, record_bridge_response, command_bridge_base_url)
    assert after["residentCount"] == 15
''', encoding="utf-8",
    )
    evidence_root = child_root / "evidence"
    junit_path = child_root / "junit.xml"
    completed = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-c", str(suite_root.parent / "pytest.ini"), str(child_test),
         "--godot-bin", str(godot_executable),
         "--evidence-root", str(evidence_root), "--junitxml", str(junit_path),
         "--basetemp", str(child_root / "fresh-pytest-temp")],
        cwd=suite_root.parents[2], capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=90, check=False,
    )
    (child_root / "child-output.log").write_text(completed.stdout + completed.stderr,
                                                encoding="utf-8")
    assert completed.returncode == 1, completed.stdout + completed.stderr
    jsonl_paths = list(evidence_root.glob("pytest-*.jsonl"))
    assert len(jsonl_paths) == 1
    events = [json.loads(line) for line in jsonl_paths[0].read_text("utf-8").splitlines()]
    _assert_real_failure_trace(events, evidence_root, junit_path)


def _assert_real_failure_trace(  # pylint: disable=too-many-locals
    events: list[dict], evidence_root: Path, junit_path: Path,
) -> None:
    """Verify the actual request/event/snapshot links in JSONL, SQL and failing JUnit."""
    index = {event["eventId"]: event for event in events}
    failures = [event for event in events if event["eventType"] == "assertion_failure"]
    call_failures = [event for event in failures if event["details"]["phase"] == "call"]
    assert len(call_failures) == 1
    failure = call_failures[0]
    case_id = failure["details"]["caseId"]
    assert case_id.endswith("::test_controlled_real_failure")
    snapshot = index[failure["details"]["snapshotEvidenceId"]]
    assert snapshot["eventType"] == "failure_snapshot"
    assert snapshot["status"] == "captured"
    assert snapshot["requestId"]
    assert snapshot["details"]["response"]["data"]["residentCount"] == 15
    observed = [index[event_id] for event_id in failure["details"]["gameEventEvidenceIds"]]
    terminal = next(event for event in observed if event["name"] == "command-completed")
    schema_path = Path(__file__).resolve().parents[1] / "protocol.schema.json"
    schema = json.loads(schema_path.read_text("utf-8"))
    validator = Draft202012Validator({
        "$schema": schema["$schema"], "$ref": "#/$defs/event", "$defs": schema["$defs"],
    }, format_checker=FormatChecker())
    assert terminal["details"]["schemaErrors"] == sorted(
        error.message for error in validator.iter_errors(terminal["details"]["gameEvent"])
    )
    # Reporter acceptance must preserve, not suppress, any actual protocol failures.
    protocol_failures = [event for event in failures if event["details"]["phase"] == "teardown"]
    assert len(protocol_failures) == (1 if terminal["details"]["schemaErrors"] else 0)
    if protocol_failures:
        assert "Event protocol errors" in protocol_failures[0]["message"]
        assert terminal["eventId"] in protocol_failures[0]["details"]["protocolErrorEvidenceIds"]
    assert terminal["worldGeneration"] == snapshot["worldGeneration"]
    assert terminal["stateVersion"] == snapshot["stateVersion"]
    assert terminal["eventSequence"] == terminal["details"]["gameEvent"]["eventSequence"] > 0
    assert terminal["eventId"] != terminal["details"]["gameEventId"]
    assert terminal["testEvidenceId"] == terminal["eventId"]
    assert terminal["details"]["missingTrackingFields"] == []
    requests_for_event = [index[event_id] for event_id in
                          terminal["details"]["relatedRequestEvidenceIds"]]
    assert len(requests_for_event) == 1
    request_event = requests_for_event[0]
    assert request_event["name"] == "POST /commands/set-speed"
    assert request_event["commandId"] == terminal["commandId"]
    assert (request_event["details"]["requestParameters"]["speed"]
            == terminal["details"]["gameEvent"]["result"]["simulationSpeed"])
    responses = [index[event_id] for event_id in failure["details"]["responseEvidenceIds"]]
    receipt = next(event for event in responses
                   if event["details"]["requestEvidenceId"] == request_event["eventId"])
    assert receipt["requestId"]
    assert receipt["commandId"] == terminal["commandId"]
    assert receipt["stateVersion"] == terminal["stateVersion"]
    assert receipt["details"]["responseEnvelope"]["requestId"] == receipt["requestId"]
    assert receipt["details"]["stepId"] == request_event["details"]["stepId"]
    assert failure["details"]["traceGaps"] == []
    linked_ids = (failure["details"]["requestEvidenceIds"]
                  + failure["details"]["responseEvidenceIds"]
                  + failure["details"]["gameEventEvidenceIds"])
    assert all(index[event_id]["details"]["caseId"] == case_id for event_id in linked_ids)
    case = next(event for event in events if event["eventType"] == "test_case"
                and event["name"] == case_id)
    assert case["status"] == "failed"
    assert case["details"]["failureEventIds"] == [event["eventId"] for event in failures]
    _assert_real_failure_formats(case, terminal, evidence_root, junit_path)


def _assert_real_failure_formats(
    case: dict, terminal: dict, evidence_root: Path, junit_path: Path,
) -> None:
    """JUnit can split call and teardown failures into two testcases; preserve both."""
    failed_xml = [item for item in ET.parse(junit_path).iter("testcase")
                  if item.get("name") == "test_controlled_real_failure"]
    assert any(item.find("failure") is not None for item in failed_xml)
    junit_failure_ids = set()
    junit_game_ids = set()
    for xml_case in failed_xml:
        properties = {item.get("name"): item.get("value")
                      for item in xml_case.findall("properties/property")}
        assert properties["caseId"] == case["name"]
        junit_failure_ids.update(json.loads(properties["failureEventIds"]))
        junit_game_ids.update(json.loads(properties["gameEventEvidenceIds"]))
    assert junit_failure_ids == set(case["details"]["failureEventIds"])
    assert junit_game_ids == set(case["details"]["gameEventEvidenceIds"])
    with sqlite3.connect(evidence_root / "phase2_evidence.sqlite3") as connection:
        row = connection.execute(
            "SELECT event_id,command_id,event_sequence,details_json FROM evidence_events "
            "WHERE event_id=?", (terminal["eventId"],),
        ).fetchone()
        counts = connection.execute(
            "SELECT status,total,passed,failed,skipped FROM runs",
        ).fetchone()
    assert row[:3] == (terminal["eventId"], terminal["commandId"], terminal["eventSequence"])
    assert json.loads(row[3])["gameEventId"] == terminal["details"]["gameEventId"]
    assert counts == ("failed", 2, 1, 1, 0)


def test_read_only_agent_save_validation_without_cleanup(  # pylint: disable=too-many-locals,too-many-statements
    tmp_path: Path, pytestconfig: pytest.Config, godot_executable: Path,
) -> None:
    """Exercise real read helpers, not restore transactions or mocked save outcomes.

    Direct helper calls deliberately do not test root configuration, full snapshot
    loading, lifecycle rollback or world retention. All generated files are retained.
    """
    game_root = Path(pytestconfig.rootpath).parent
    fixture_root = game_root / "tests/fixtures/historical_saves/beta6/agent_saves"
    snapshot_root = (fixture_root / "roundtrip-slot-beta6/sessions"
                     / "roundtrip-session-beta6/revisions/1")
    manifest_path = snapshot_root / "snapshot.json"
    source_manifest = json.loads(manifest_path.read_text("utf-8"))
    entry = source_manifest["residents"][0]
    source_payload = snapshot_root / entry["file"]
    source_paths = [manifest_path, source_payload,
                    game_root / "agent/lifecycle/AgentSaveStore.gd"]
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in source_paths}
    assert before[str(source_payload)] == entry["sha256"]
    assert source_payload.stat().st_size == entry["byte_length"]
    truncated = tmp_path / "truncated-snapshot.json"
    truncated.write_text(manifest_path.read_text("utf-8")[:32], encoding="utf-8")
    non_object = tmp_path / "non-object-snapshot.json"
    non_object.write_text("[]", encoding="utf-8")
    incompatible = tmp_path / "incompatible-snapshot.json"
    future = {**source_manifest, "format_version": source_manifest["format_version"] + 1}
    incompatible.write_text(json.dumps(future, ensure_ascii=False), encoding="utf-8")
    context = {key: source_manifest[key] for key in
               ("slot_id", "session_id", "save_revision")}
    # GDScript parses JSON numeric fields as floats; context revision must be TYPE_INT.
    context_literal = json.dumps(context, ensure_ascii=False)
    script = tmp_path / "read-only-save-validation.gd"
    script.write_text(
        'extends SceneTree\n'
        'const Store = preload("res://agent/lifecycle/AgentSaveStore.gd")\n'
        'func _initialize() -> void:\n'
        '\tvar store = Store.new()\n'
        f'\tvar manifest = store._read_json({json.dumps(manifest_path.as_posix())})\n'
        f'\tvar context = {context_literal}\n'
        '\tvar entry = manifest["value"]["residents"][0]\n'
        f'\tvar root_path = {json.dumps(snapshot_root.as_posix())}\n'
        '\tvar cases = []\n'
        '\tcases.append({"id": "valid-json", "result": manifest})\n'
        f'\tcases.append({{"id": "truncated-json", "result": store._read_json('
        f'{json.dumps(truncated.as_posix())})}})\n'
        f'\tcases.append({{"id": "non-object-json", "result": store._read_json('
        f'{json.dumps(non_object.as_posix())})}})\n'
        '\tvar valid = store._read_checked_resident_payload(root_path, entry)\n'
        '\tvalid.erase("payload")\n'
        '\tcases.append({"id": "valid-payload", "result": valid})\n'
        '\tvar wrong_length = entry.duplicate(true)\n'
        '\twrong_length["byte_length"] += 1\n'
        '\tcases.append({"id": "wrong-length", "result": '
        'store._read_checked_resident_payload(root_path, wrong_length)})\n'
        '\tvar wrong_hash = entry.duplicate(true)\n'
        '\twrong_hash["sha256"] = "0".repeat(64)\n'
        '\tcases.append({"id": "wrong-hash", "result": '
        'store._read_checked_resident_payload(root_path, wrong_hash)})\n'
        f'\tvar future = store._read_json({json.dumps(incompatible.as_posix())})\n'
        '\tvar errors = store._snapshot_identity_errors(future["value"], context)\n'
        '\tcases.append({"id": "future-version", "result": '
        '{"ok": errors.is_empty(), "errors": errors}})\n'
        '\tprint("READ_ONLY_SAVE_RESULTS " + JSON.stringify(cases))\n'
        '\tquit(0)\n', encoding="utf-8",
    )
    started = time.monotonic()
    completed = subprocess.run(
        [str(godot_executable), "--headless", "--path", str(game_root),
         "--log-file", str(tmp_path / "godot-engine.log"),
         "--script", str(script)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30, check=False,
    )
    (tmp_path / "godot-output.log").write_text(completed.stdout + completed.stderr,
                                               encoding="utf-8")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "SCRIPT ERROR" not in completed.stdout + completed.stderr
    results = [line.removeprefix("READ_ONLY_SAVE_RESULTS ")
               for line in completed.stdout.splitlines()
               if line.startswith("READ_ONLY_SAVE_RESULTS ")]
    assert len(results) == 1, completed.stdout + completed.stderr
    cases = json.loads(results[0])
    expected = {
        "valid-json": (True, None), "truncated-json": (False, "存档文件损坏"),
        "non-object-json": (False, "存档文件损坏"), "valid-payload": (True, None),
        "wrong-length": (False, "byte_length 不一致"),
        "wrong-hash": (False, "SHA-256 不一致"),
        "future-version": (False, "format_version 不受支持"),
    }
    assert [item["id"] for item in cases] == list(expected)
    store = EvidenceStore(tmp_path / "evidence", "godot-read-only-save", metadata={
        "scope": "real read helpers only; NOT restore/rollback or path-guard acceptance",
        "sourceSha256": before, "historicalFixture": "beta6",
        "construction": "truncated JSON; non-object JSON; changed length/hash/version",
    })
    failures = []
    for item in cases:
        expected_ok, error_fragment = expected[item["id"]]
        result = item["result"]
        matches = result["ok"] is expected_ok and (error_fragment is None or
                    any(error_fragment in error for error in result.get("errors", [])))
        # Do not persist the full manifest or resident bytes; raw stdout remains local.
        store.record("save_validation", name=item["id"],
                     status="passed" if matches else "failed", details={
                         "expectedOk": expected_ok, "actualOk": result["ok"],
                         "errors": result.get("errors", []),
                         "scope": "read-helper-only",
                     })
        if not matches:
            failures.append(item)
    store.finish(status="failed" if failures else "passed", total=len(cases),
                 passed=len(cases) - len(failures), failed=len(failures), skipped=0,
                 other=0, duration_ms=round((time.monotonic() - started) * 1000, 3))
    assert not failures, failures
    assert before == {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in source_paths}
