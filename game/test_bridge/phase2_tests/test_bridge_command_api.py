"""External write-command regression against the isolated GameTestBridge."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Callable

import pytest
import requests
from jsonschema import Draft202012Validator, FormatChecker


pytestmark = [pytest.mark.live_bridge, pytest.mark.command_bridge]
ResponseRecorder = Callable[[str, requests.Response, int], None]
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "protocol.schema.json"
with SCHEMA_PATH.open("r", encoding="utf-8") as _schema_handle:
    RESPONSE_VALIDATOR = Draft202012Validator(
        json.load(_schema_handle),
        format_checker=FormatChecker(),
    )


def _json_request(  # pylint: disable=too-many-arguments
    session: requests.Session,
    recorder: ResponseRecorder,
    method: str,
    base_url: str,
    path: str,
    expected_status: int,
    *,
    payload: dict | None = None,
) -> dict:
    response = session.request(
        method,
        f"{base_url}{path}",
        json=payload,
        timeout=5.0,
    )
    recorder(f"{method} {path}", response, expected_status)
    assert response.status_code == expected_status, response.text
    assert response.headers["Content-Type"].startswith("application/json")
    body = response.json()
    RESPONSE_VALIDATOR.validate(body)
    return body


def _world(
    session: requests.Session,
    recorder: ResponseRecorder,
    base_url: str,
) -> dict:
    return _json_request(
        session,
        recorder,
        "GET",
        base_url,
        "/world/state",
        200,
    )["data"]


def _command(snapshot: dict, *, timeout_ms: int = 3000) -> dict:
    identifier = f"pytest-{uuid.uuid4().hex}"
    return {
        "commandId": identifier,
        "idempotencyKey": identifier,
        "expectedSessionId": snapshot["sessionId"],
        "expectedWorldGeneration": snapshot["worldGeneration"],
        "expectedStateVersion": snapshot["stateVersion"],
        "timeoutMs": timeout_ms,
    }


def _poll_command(
    session: requests.Session,
    recorder: ResponseRecorder,
    base_url: str,
    command_id: str,
    *,
    timeout_seconds: float = 10.0,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    latest: dict = {}
    while time.monotonic() < deadline:
        latest = _json_request(
            session,
            recorder,
            "GET",
            base_url,
            f"/commands/{command_id}",
            200,
        )["data"]
        if latest["status"] in {"completed", "failed"}:
            return latest
        time.sleep(0.05)
    pytest.fail(f"command {command_id} did not finish: {latest}", pytrace=False)


def test_set_speed_succeeds_and_replay_is_idempotent(
    http_session: requests.Session,
    command_bridge_base_url: str,
    record_bridge_response: ResponseRecorder,
) -> None:
    """A lost-response retry must return the original receipt without a second mutation."""
    health = _json_request(
        http_session,
        record_bridge_response,
        "GET",
        command_bridge_base_url,
        "/health",
        200,
    )
    assert health["data"]["readOnly"] is False
    before = _world(http_session, record_bridge_response, command_bridge_base_url)
    target_speed = 2 if before["simulationSpeed"] != 2 else 3
    command = {**_command(before), "speed": target_speed}

    first = _json_request(
        http_session,
        record_bridge_response,
        "POST",
        command_bridge_base_url,
        "/commands/set-speed",
        200,
        payload=command,
    )
    assert first["data"]["accepted"] is True
    assert first["data"]["status"] == "completed"
    assert first["data"]["result"] == {
        "simulationSpeed": target_speed,
        "changed": True,
    }
    assert first["data"]["stateVersion"] == before["stateVersion"] + 1

    replay = _json_request(
        http_session,
        record_bridge_response,
        "POST",
        command_bridge_base_url,
        "/commands/set-speed",
        200,
        payload=command,
    )
    assert replay["requestId"] != first["requestId"]
    assert replay["data"] == first["data"]

    queried = _json_request(
        http_session,
        record_bridge_response,
        "GET",
        command_bridge_base_url,
        f"/commands/{command['commandId']}",
        200,
    )
    assert queried["data"] == first["data"]
    after = _world(http_session, record_bridge_response, command_bridge_base_url)
    assert after["simulationSpeed"] == target_speed
    assert after["stateVersion"] == first["data"]["stateVersion"]


def test_command_identity_conflicts_do_not_add_side_effects(
    http_session: requests.Session,
    command_bridge_base_url: str,
    record_bridge_response: ResponseRecorder,
) -> None:
    """Reusing either identity for changed work must be rejected."""
    before = _world(http_session, record_bridge_response, command_bridge_base_url)
    target_speed = 2 if before["simulationSpeed"] != 2 else 3
    command = {**_command(before), "speed": target_speed}
    accepted = _json_request(
        http_session,
        record_bridge_response,
        "POST",
        command_bridge_base_url,
        "/commands/set-speed",
        200,
        payload=command,
    )

    changed_work = {**command, "speed": 1 if target_speed != 1 else 3}
    idempotency_conflict = _json_request(
        http_session,
        record_bridge_response,
        "POST",
        command_bridge_base_url,
        "/commands/set-speed",
        409,
        payload=changed_work,
    )
    assert idempotency_conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    command_id_conflict = _json_request(
        http_session,
        record_bridge_response,
        "POST",
        command_bridge_base_url,
        "/commands/set-speed",
        409,
        payload={**command, "idempotencyKey": f"other-{uuid.uuid4().hex}"},
    )
    assert command_id_conflict["error"]["code"] == "COMMAND_ID_CONFLICT"
    after = _world(http_session, record_bridge_response, command_bridge_base_url)
    assert after["simulationSpeed"] == target_speed
    assert after["stateVersion"] == accepted["data"]["stateVersion"]


def test_stale_version_and_invalid_schema_are_side_effect_free(
    http_session: requests.Session,
    command_bridge_base_url: str,
    record_bridge_response: ResponseRecorder,
) -> None:
    """State races and malformed commands must not mutate the World."""
    before = _world(http_session, record_bridge_response, command_bridge_base_url)
    target_speed = 2 if before["simulationSpeed"] != 2 else 3
    stale = {
        **_command(before),
        "expectedStateVersion": before["stateVersion"] + 1,
        "speed": target_speed,
    }
    stale_response = _json_request(
        http_session,
        record_bridge_response,
        "POST",
        command_bridge_base_url,
        "/commands/set-speed",
        409,
        payload=stale,
    )
    assert stale_response["error"]["code"] == "STATE_VERSION_CONFLICT"

    invalid = {**_command(before), "speed": 4}
    invalid_response = _json_request(
        http_session,
        record_bridge_response,
        "POST",
        command_bridge_base_url,
        "/commands/set-speed",
        400,
        payload=invalid,
    )
    assert invalid_response["error"]["code"] == "INVALID_COMMAND_SCHEMA"

    missing = _command(before)
    missing_response = _json_request(
        http_session,
        record_bridge_response,
        "POST",
        command_bridge_base_url,
        "/commands/set-speed",
        400,
        payload=missing,
    )
    assert missing_response["error"]["code"] == "INVALID_COMMAND_SCHEMA"
    after = _world(http_session, record_bridge_response, command_bridge_base_url)
    assert after["simulationSpeed"] == before["simulationSpeed"]
    assert after["stateVersion"] == before["stateVersion"]


def test_save_and_load_reach_terminal_state_and_replay_safely(  # pylint: disable=too-many-locals
    http_session: requests.Session,
    command_bridge_base_url: str,
    record_bridge_response: ResponseRecorder,
) -> None:
    """External clients must distinguish acceptance, completion, and restored generation."""
    before_save = _world(http_session, record_bridge_response, command_bridge_base_url)
    save_command = _command(before_save, timeout_ms=10000)
    save_accepted = _json_request(
        http_session,
        record_bridge_response,
        "POST",
        command_bridge_base_url,
        "/commands/save",
        202,
        payload=save_command,
    )
    assert save_accepted["data"]["status"] == "running"

    concurrent = _command(before_save, timeout_ms=10000)
    busy = _json_request(
        http_session,
        record_bridge_response,
        "POST",
        command_bridge_base_url,
        "/commands/save",
        409,
        payload=concurrent,
    )
    assert busy["error"]["code"] == "SESSION_COMMAND_BUSY"

    saved = _poll_command(
        http_session,
        record_bridge_response,
        command_bridge_base_url,
        save_command["commandId"],
    )
    assert saved["status"] == "completed"
    assert saved["result"]["saveRevision"] == 7
    save_replay = _json_request(
        http_session,
        record_bridge_response,
        "POST",
        command_bridge_base_url,
        "/commands/save",
        200,
        payload=save_command,
    )
    assert save_replay["data"] == saved

    before_load = _world(http_session, record_bridge_response, command_bridge_base_url)
    load_command = _command(before_load, timeout_ms=10000)
    load_accepted = _json_request(
        http_session,
        record_bridge_response,
        "POST",
        command_bridge_base_url,
        "/commands/load",
        202,
        payload=load_command,
    )
    assert load_accepted["data"]["status"] == "running"
    loaded = _poll_command(
        http_session,
        record_bridge_response,
        command_bridge_base_url,
        load_command["commandId"],
    )
    assert loaded["status"] == "completed"
    assert loaded["result"]["saveRevision"] == 7
    after_load = _world(http_session, record_bridge_response, command_bridge_base_url)
    assert after_load["residentCount"] == 15
    assert after_load["worldGeneration"] != before_load["worldGeneration"]

    load_replay = _json_request(
        http_session,
        record_bridge_response,
        "POST",
        command_bridge_base_url,
        "/commands/load",
        200,
        payload=load_command,
    )
    assert load_replay["data"] == loaded


def test_move_rejection_is_structured_without_a_player_runtime(
    http_session: requests.Session,
    command_bridge_base_url: str,
    record_bridge_response: ResponseRecorder,
) -> None:
    """The lightweight command fixture must reject movement rather than fake success."""
    before = _world(http_session, record_bridge_response, command_bridge_base_url)
    avatar = before["playerAvatar"]
    movement = {
        **_command(before, timeout_ms=1000),
        "spaceId": avatar["spaceId"] or "town_outdoor",
        "targetPosition": [avatar["position"][0] + 64, avatar["position"][1]],
        "tolerance": 6,
    }
    rejected = _json_request(
        http_session,
        record_bridge_response,
        "POST",
        command_bridge_base_url,
        "/commands/move-player",
        503,
        payload=movement,
    )
    assert rejected["error"]["code"] == "PLAYER_RUNTIME_NOT_READY"
    after = _world(http_session, record_bridge_response, command_bridge_base_url)
    assert after["stateVersion"] == before["stateVersion"]


def test_real_command_event_links_request_receipt_and_generation(
    http_session: requests.Session,
    command_bridge_base_url: str,
    record_bridge_response: ResponseRecorder,
    bridge_event_trace,
) -> None:
    """Observe a real speed mutation, not a mocked protocol event or save result."""
    before = _world(http_session, record_bridge_response, command_bridge_base_url)
    speed = 2 if before["simulationSpeed"] != 2 else 3
    command = {**_command(before), "speed": speed}
    receipt = _json_request(http_session, record_bridge_response, "POST",
                            command_bridge_base_url, "/commands/set-speed", 200,
                            payload=command)
    observed = bridge_event_trace.wait_for_command(command["commandId"])
    source = observed["details"]["gameEvent"]
    assert source["eventType"] == "command-completed"
    assert source["commandId"] == receipt["data"]["commandId"]
    assert source["worldGeneration"] == before["worldGeneration"]
    assert source["stateVersion"] == receipt["data"]["stateVersion"]
    assert source["result"] == receipt["data"]["result"]
    assert observed["eventSequence"] == source["eventSequence"] > 0
    assert observed["eventId"] != source["eventId"]
    assert observed["details"]["relatedRequestEvidenceIds"]
    assert observed["details"]["relatedResponseEvidenceIds"]
    assert "failure" in source
    assert source["failure"] is None


def test_idle_event_stream_does_not_resend_last_frame(
    http_session: requests.Session,
    command_bridge_base_url: str,
    record_bridge_response: ResponseRecorder,
    bridge_event_trace,
) -> None:
    """Observe the wire after a real command, without filtering repeated frames."""
    before = _world(http_session, record_bridge_response, command_bridge_base_url)
    command = {**_command(before), "speed": 2 if before["simulationSpeed"] != 2 else 3}
    _json_request(http_session, record_bridge_response, "POST", command_bridge_base_url,
                  "/commands/set-speed", 200, payload=command)
    bridge_event_trace.wait_for_command(command["commandId"])
    bridge_event_trace.flush(0.3)
    observations = bridge_event_trace.game_events
    source_ids = [event["details"]["gameEventId"] for event in observations]
    sequences = [event["eventSequence"] for event in observations]
    assert len(source_ids) >= 2, "command and state-change events must both be observed"
    assert len(source_ids) == len(set(source_ids)), observations
    assert all(current > previous for previous, current in
               zip(sequences, sequences[1:])), observations
