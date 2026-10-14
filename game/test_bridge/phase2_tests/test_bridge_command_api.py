"""External write-command regression against the isolated GameTestBridge."""

from __future__ import annotations

import json
import socket
import sqlite3
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


HOST_FLOW_DRIVER = r'''extends "__BASE_DRIVER__"
var host_http: Array = []
var host_events: Array = []
var host_scenes: Array = []
var host_ws := WebSocketPeer.new()
func pump_host_events() -> void:
	host_ws.poll()
	while host_ws.get_available_packet_count() > 0:
		var raw := host_ws.get_packet().get_string_from_utf8()
		host_events.append({"raw": raw, "event": JSON.parse_string(raw), "ticksMsec": Time.get_ticks_msec()})
	if current_scene != null:
		var instance := current_scene.get_instance_id()
		if host_scenes.is_empty() or host_scenes.back()["instanceId"] != instance:
			host_scenes.append({"name": current_scene.name, "instanceId": instance, "ticksMsec": Time.get_ticks_msec()})
func http(method: String, url: String, payload: Dictionary = {}) -> Dictionary:
	var peer := StreamPeerTCP.new()
	var deadline := Time.get_ticks_msec() + 15000
	var body := JSON.stringify(payload) if method == "POST" else ""
	var wire := ("%s %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nConnection: close\r\n" % [method, url, spec["bridgePort"]])
	wire += "Content-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % [body.to_utf8_buffer().size(), body]
	var entry := {"method": method, "path": url, "payload": payload.duplicate(true), "sentAtMsec": Time.get_ticks_msec(), "rawRequest": wire}
	var bytes := PackedByteArray()
	var connect_error := peer.connect_to_host("127.0.0.1", int(spec["bridgePort"]))
	while connect_error == OK and peer.get_status() == StreamPeerTCP.STATUS_CONNECTING and Time.get_ticks_msec() < deadline:
		peer.poll()
		pump_host_events()
		await process_frame
	if connect_error == OK and peer.get_status() == StreamPeerTCP.STATUS_CONNECTED:
		peer.put_data(wire.to_utf8_buffer())
		while Time.get_ticks_msec() < deadline:
			peer.poll()
			pump_host_events()
			if peer.get_available_bytes() > 0:
				var got := peer.get_data(peer.get_available_bytes())
				if got[0] == OK:
					bytes.append_array(got[1])
			var text := bytes.get_string_from_utf8()
			var split := text.find("\r\n\r\n")
			if split >= 0:
				var length := -1
				for header: String in text.left(split).split("\r\n"):
					if header.to_lower().begins_with("content-length:"):
						length = int(header.split(":", true, 1)[1].strip_edges())
				if length >= 0 and bytes.size() >= split + 4 + length:
					break
			if peer.get_status() != StreamPeerTCP.STATUS_CONNECTED:
				break
			await process_frame
	peer.disconnect_from_host()
	var raw := bytes.get_string_from_utf8()
	var split := raw.find("\r\n\r\n")
	var result := {"status": int(raw.split(" ")[1]) if raw.begins_with("HTTP/1.1 ") else 0,
		"body": JSON.parse_string(raw.substr(split + 4)) if split >= 0 else null}
	entry["receivedAtMsec"] = Time.get_ticks_msec()
	entry["rawResponse"] = raw
	entry["response"] = result
	host_http.append(entry)
	snapshot("host-http.json", {"requests": host_http, "events": host_events, "scenes": host_scenes})
	return result
func command_input(state: Dictionary, id: String) -> Dictionary:
	return {"commandId": id, "idempotencyKey": id, "expectedSessionId": state["sessionId"],
		"expectedWorldGeneration": state["worldGeneration"], "expectedStateVersion": state["stateVersion"], "timeoutMs": 120000}
func terminal(id: String) -> Dictionary:
	var deadline := Time.get_ticks_msec() + 120000
	var result := {}
	while Time.get_ticks_msec() < deadline:
		var response := await http("GET", "/commands/" + id)
		if response.get("status") == 200 and response.get("body") is Dictionary:
			result = response["body"].get("data", {})
			if result.get("status") in ["completed", "failed"]:
				break
		await create_timer(0.05, true, false, true).timeout
	return result
func bridge_host_roundtrip(runtime: Node, source: Node, _ui: RefCounted, _config: Dictionary, _provider: RefCounted, resident: String, expected: Dictionary) -> void:
	var flow := root.get_node("GameFlowHost")
	# Fixtures are fresh current-format files, not loaded/migrated user settings.
	var provider_config := {"schemaVersion": 2, "selectedProviderId": "ollama",
		"selectedModelByProvider": {"ollama": "closeout-fixture"},
		"providers": {"ollama": {"enabled": true, "endpoint": spec["modelEndpoint"],
			"apiModels": ["closeout-fixture"], "apiKeyRef": "secure_store.llm.ollama.api_key"}}}
	var config_file := FileAccess.open(path("provider-settings.json"), FileAccess.WRITE)
	config_file.store_string(JSON.stringify(provider_config))
	config_file = null
	var credentials := flow.call("_create_provider_settings_service").get("_credential_store") as RefCounted
	var password: Dictionary = credentials.call("_encryption_password")
	check("actual-credential-password-available", password.get("ok") == true)
	if password.get("ok") != true:
		return
	var credential_file := FileAccess.open_encrypted_with_pass(path("provider-credentials.enc"), FileAccess.WRITE, password["password"])
	check("isolated-native-encrypted-credential-fixture", credential_file != null)
	if credential_file == null:
		return
	credential_file.store_string(JSON.stringify({"schemaVersion": 1, "keys": {"ollama": "isolated-fixture-not-secret"}}))
	credential_file = null
	flow.call("_initialize_startup_settings_services")
	check("actual-startup-settings-loaded", flow.get("_startup_provider_settings_service").call("runtime_configuration").get("providerConfigs", {}).get("ollama", {}).get("api_key") == "isolated-fixture-not-secret")
	flow.set("_provider_service", flow.get("_startup_provider_service"))
	flow.set("_gateway", source)
	flow.set("_active_session_config", runtime.get("session_config").duplicate(true))
	# Actual runtime scene, only the test clock is fixed before ready/restore.
	var original_scene := FileAccess.get_file_as_string("res://world/presentation/town_runtime/TownRuntime.tscn")
	var root_line := "[node name=\"TownRuntime\" type=\"Node2D\"]"
	check("actual-runtime-fixture-only-clock-property", original_scene.count(root_line) == 1)
	var clock_scene_path := String(spec["sandbox"]).path_join("clock-runtime.tscn")
	var clock_file := FileAccess.open(clock_scene_path, FileAccess.WRITE)
	clock_file.store_string(original_scene.replace(root_line, root_line + "\nprocess_mode = 4"))
	clock_file = null
	var fixed_scene := load(clock_scene_path) as PackedScene
	check("actual-runtime-clock-template-loaded", fixed_scene != null)
	flow.set("_town_runtime_scene", fixed_scene)
	current_scene = runtime
	flow.call("_bind_current_scene")
	check("real-host-runtime-bound", flow.get("_town_runtime") == runtime and flow.get("_session_ui_service") != null)
	var bridge: Node = flow.get_node_or_null("GameTestBridge")
	check("owned-bridge-listener", is_instance_valid(bridge) and bridge.get("_server").is_listening() and bridge.get("_port") == spec["bridgePort"])
	if not is_instance_valid(bridge) or not bridge.get("_server").is_listening():
		return
	var ws_start := host_ws.connect_to_url("ws://127.0.0.1:%d/events" % spec["bridgePort"])
	var deadline := Time.get_ticks_msec() + 5000
	while host_ws.get_ready_state() == WebSocketPeer.STATE_CONNECTING and Time.get_ticks_msec() < deadline:
		pump_host_events()
		await process_frame
	check("real-websocket-subscribed", ws_start == OK and host_ws.get_ready_state() == WebSocketPeer.STATE_OPEN)
	var health := await http("GET", "/health")
	check("real-host-http-ready", health.get("status") == 200 and health.get("body", {}).get("data", {}).get("readOnly") == false and health.get("body", {}).get("data", {}).get("worldReady") == true, health)
	var initial := await http("GET", "/world/state")
	check("real-host-http-population", initial.get("status") == 200 and initial.get("body", {}).get("data", {}).get("residentCount") == 15)
	if initial.get("status") != 200:
		return
	var before: Dictionary = initial["body"]["data"]
	var save_payload := command_input(before, "host-http-save")
	var accepted := await http("POST", "/commands/save", save_payload)
	check("real-http-save-accepted", accepted.get("status") == 202, accepted)
	var saved := await terminal("host-http-save")
	check("real-http-save-completed", saved.get("status") == "completed", saved)
	check("real-http-save-published-revision-2", saved.get("result", {}).get("saveRevision") == 2, saved)
	var unchanged := await http("POST", "/commands/save", save_payload)
	check("real-http-save-idempotent", unchanged.get("status") == 200 and unchanged.get("body", {}).get("data", {}) == saved)
	var before_load := await http("GET", "/world/state")
	var load_state: Dictionary = before_load["body"]["data"]
	var start_cursor := await http("GET", "/events/cursor")
	var previous_instance := runtime.get_instance_id()
	var load_payload := command_input(load_state, "host-http-load")
	var loading := await http("POST", "/commands/load", load_payload)
	check("real-http-load-accepted", loading.get("status") == 202, loading)
	var loaded := await terminal("host-http-load")
	check("real-http-load-completed", loaded.get("status") == "completed", loaded)
	var target: Node = flow.get("_town_runtime")
	check("actual-new-runtime-and-scene", is_instance_valid(target) and target.get_instance_id() != previous_instance and current_scene == target, host_scenes)
	if not is_instance_valid(target) or target.get_instance_id() == previous_instance:
		snapshot("host-failure-state.json", {"flowResult": flow.get("_last_result"), "load": loaded, "scenes": host_scenes})
		return
	var gateway: Node = flow.get("_gateway")
	gateway.call("cancel_background_departure_messages")
	var actual := projection(target.call("get_world_runtime"), gateway, resident)
	snapshot("host-restored-state.json", actual)
	check("host-nonempty-task-restored", same(actual["task"], expected["task"]))
	check("host-full-log-records-restored", same(actual["logs"], expected["logs"]))
	check("host-nonempty-memory-restored", same(actual["memory"], expected["memory"]))
	check("host-restore-15-active-agents", gateway.call("get_agent_save_participant").call("get_active_resident_ids").get("resident_ids", []).size() == 15)
	var store: RefCounted = flow.get("_session_ui_service").get("_store")
	var published: Dictionary = store.call("list_published", "town-3")
	var manifest: Dictionary = published.get("manifests", [])[0] if not published.get("manifests", []).is_empty() else {}
	snapshot("host-published-manifest.json", manifest)
	check("departure-published-actual-revision-3", manifest.get("save_revision") == 3 and actual["agentContext"].get("save_revision") == 3, manifest)
	check("load-receipt-matches-actual-restored-revision", loaded.get("result", {}).get("saveRevision") == actual["agentContext"].get("save_revision"), {"receipt": loaded, "agentContext": actual["agentContext"]})
	var after := await http("GET", "/world/state")
	check("http-world-generation-changed", after.get("status") == 200 and after.get("body", {}).get("data", {}).get("worldGeneration") != load_state["worldGeneration"])
	var replay := await http("POST", "/commands/load", load_payload)
	check("real-http-load-idempotent", replay.get("status") == 200 and replay.get("body", {}).get("data", {}) == loaded)
	var old_payload := command_input(load_state, "host-old-generation")
	old_payload["timeoutMs"] = 3000
	old_payload["speed"] = 2
	var old_request := await http("POST", "/commands/set-speed", old_payload)
	check("real-host-old-generation-rejected", old_request.get("status") == 409 and old_request.get("body", {}).get("error", {}).get("code") == "WORLD_GENERATION_CONFLICT", old_request)
	var end_cursor := await http("GET", "/events/cursor")
	for _frame in range(6):
		pump_host_events()
		await process_frame
	check("real-host-command-completion-events", ["host-http-save", "host-http-load"].all(func(id): return host_events.any(func(event): return event.get("event", {}).get("commandId") == id and event.get("event", {}).get("eventType") == "command-completed")), host_events)
	snapshot("host-http.json", {"requests": host_http, "events": host_events, "scenes": host_scenes, "cursorStart": start_cursor, "cursorEnd": end_cursor})
	check("host-actual-session-close", gateway.call("close_session").get("ok") == true)
	host_ws.close()
	for _frame in range(6):
		host_ws.poll()
		await process_frame
	flow.call("_unmount_town_overlays")
	flow.call("_release_internal_session_refs")
	flow.set("_town_runtime_scene", null)
	current_scene = null
	target.free()
	for _frame in range(6):
		await process_frame
'''


def prepare_native_case_files(owned: Path, mode: str) -> None:
    """Build only owned explicit blockers/worker inputs; never delete them."""
    if mode.startswith("profile"):
        (owned / "profile-normal.json").write_bytes(b"original-profile")
        (owned / "profile-locked.json").write_bytes(b"locked-profile")
        (owned / "profile-blocked.json").mkdir()
        (owned / "profile-blocked.json/proof.bin").write_bytes(b"publication-blocker")
    if mode == "production-failure":
        blocker = owned / "world-saves/slots/phase2-slot/manifests/00000000000000000001.json"
        blocker.mkdir(parents=True)
        (blocker / "proof.bin").write_bytes(b"native-manifest-publication-blocker")
    if mode == "threads":
        for index in range(4):
            for offset in range(8):
                directory = owned / f"thread-{index}-{offset}"
                directory.mkdir()
                (directory / "proof.bin").write_bytes(f"thread-{index}-{offset}".encode())


def prepare_host_flow_driver(tmp_path: Path, production_driver: str) -> list[str]:
    """Generate only isolated driver/config artifacts using already installed tools."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    spec = json.loads((tmp_path / "spec.json").read_text(encoding="utf-8"))
    spec["bridgePort"] = port
    (tmp_path / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    base = tmp_path / "base-driver.gd"
    base.write_text(production_driver, encoding="utf-8")
    (tmp_path / "driver.gd").write_text(
        HOST_FLOW_DRIVER.replace("__BASE_DRIVER__", base.as_posix()), encoding="utf-8")
    return ["--gametest-bridge", "--gametest-commands", f"--gametest-port={port}"]


def _record_host_request(evidence, entry: dict, case_id: str, index: int) -> tuple:
    """Record one genuine exchange, not synthesized server data."""
    body = entry["response"]["body"]
    if not isinstance(body, dict):
        return [f"request {index}: no parsed server response"], None, None
    errors = [f"response {index}: {error.message}"
              for error in RESPONSE_VALIDATOR.iter_errors(body)]
    if json.loads(entry["rawResponse"].split("\r\n\r\n", 1)[1]) != body:
        errors.append(f"response {index}: raw/parsed mismatch")
    payload = entry["payload"]
    command_id = payload.get("commandId") or (
        entry["path"].removeprefix("/commands/")
        if entry["method"] == "GET" and entry["path"].startswith("/commands/") else None)
    request_event = evidence.record(
        "http_request", name=f"{entry['method']} {entry['path']}", status="captured",
        request_id=body.get("requestId"), command_id=command_id,
        session_id=payload.get("expectedSessionId"),
        world_generation=payload.get("expectedWorldGeneration"),
        state_version=payload.get("expectedStateVersion"),
        details={"caseId": case_id, "stepId": f"{case_id}::native-http-{index}",
                 "wireSource": "native-real-tcp", **entry})
    evidence.record("http_response", name=f"{entry['method']} {entry['path']}",
                    status="captured", request_id=body.get("requestId"), command_id=command_id,
                    details={"requestEvidenceId": request_event["eventId"], "response": body,
                             "actualHttpStatus": entry["response"]["status"]})
    return errors, command_id, request_event["eventId"]


def _link_host_assertions(evidence, assertions: list[dict], requests_by_command: dict) -> None:
    """Link a failed or passed actual-receipt assertion to the captured wire evidence."""
    with sqlite3.connect(evidence.sqlite_path.as_uri() + "?mode=ro", uri=True) as database:
        for assertion in assertions:
            actual = assertion["details"].get("actual")
            receipt = actual.get("receipt", {}) if isinstance(actual, dict) else {}
            command_id = receipt.get("commandId")
            if not command_id:
                continue
            event_ids = [row[0] for row in database.execute(
                "select event_id from evidence_events where run_id=? and event_type='game_event'"
                " and command_id=? order by sequence", (evidence.run_id, command_id))]
            evidence.record("assertion_trace_link", name=assertion["name"], status="captured",
                            command_id=command_id, details={
                                "assertionEvidenceId": assertion["eventId"],
                                "relatedRequestEvidenceIds": requests_by_command.get(
                                    command_id, []),
                                "relatedGameEventEvidenceIds": event_ids,
                                "receipt": receipt,
                                "snapshotPaths": [p.as_posix() for p in
                                    evidence.root.parent.rglob("host-restored-state.json")],
                                "association": "actualReceipt.commandId"})


def record_host_flow_trace(evidence, trace_path: Path, case_id: str,
                           assertions: list[dict]) -> list[str]:
    """Validate actual native wire records, preserving request/event/step links."""
    if not trace_path.is_file():
        errors = ["native HTTP/WS trace was not produced"]
        evidence.record("host_flow_trace_audit", status="failed", details={"errors": errors})
        return errors
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    event_validator = Draft202012Validator({
        "$defs": json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))["$defs"],
        "$ref": "#/$defs/event"}, format_checker=FormatChecker())
    errors = []
    requests_by_command: dict[str, list[str]] = {}
    for index, entry in enumerate(trace["requests"]):
        exchange = _record_host_request(evidence, entry, case_id, index)
        errors.extend(exchange[0])
        if exchange[1]:
            requests_by_command.setdefault(exchange[1], []).append(exchange[2])
    sequences = []
    for index, observation in enumerate(trace["events"]):
        event = observation["event"]
        errors.extend(f"event {index}: {error.message}"
                      for error in event_validator.iter_errors(event))
        if json.loads(observation["raw"]) != event:
            errors.append(f"event {index}: raw/parsed mismatch")
        sequences.append(event.get("eventSequence"))
        evidence.record(
            "game_event", name=event.get("eventType"), status="observed",
            command_id=event.get("commandId"), session_id=event.get("sessionId"),
            world_generation=event.get("worldGeneration"), state_version=event.get("stateVersion"),
            event_sequence=event.get("eventSequence"), details={
                "caseId": case_id, "stepId": f"{case_id}::native-ws-{index}", "gameEvent": event,
                "observationSource": "live-websocket",
                "relatedRequestEvidenceIds": requests_by_command.get(event.get("commandId"), []),
                "association": ("commandId/case" if event.get("commandId")
                                else "case-observation-window")})
    if sequences != sorted(set(sequences)):
        errors.append("non-increasing or repeated native event sequence")
    if not {"StartupScreen", "SourceRuntime"}.issubset({s["name"] for s in trace["scenes"]}):
        errors.append("actual startup/source scene transition not observed")
    evidence.record("host_flow_trace_audit", status="failed" if errors else "passed",
                    details={"errors": errors, "requests": len(trace["requests"]),
                             "events": len(trace["events"]), "scenes": trace["scenes"],
                             "tracePath": trace_path.as_posix()})
    _link_host_assertions(evidence, assertions, requests_by_command)
    return errors


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


def test_captured_event_disorder_duplicate_and_gap_resync_read_only(  # pylint: disable=too-many-locals,too-many-statements
    http_session: requests.Session,
    command_bridge_base_url: str,
    record_bridge_response: ResponseRecorder,
    bridge_event_trace,
) -> None:
    """Controlled replay of real events; not a claim of automatic runtime recovery."""
    before_cursor = _json_request(http_session, record_bridge_response, "GET",
                                  command_bridge_base_url, "/events/cursor", 200)["data"]
    cursor_evidence_ids = [bridge_event_trace.responses[-1]["eventId"]]
    terminal = None
    for _ in range(2):
        before = _world(http_session, record_bridge_response, command_bridge_base_url)
        command = {**_command(before), "speed": 2 if before["simulationSpeed"] != 2 else 3}
        _json_request(http_session, record_bridge_response, "POST", command_bridge_base_url,
                      "/commands/set-speed", 200, payload=command)
        terminal = bridge_event_trace.wait_for_command(command["commandId"])
        bridge_event_trace.flush(0.1)
    assert terminal is not None
    after_cursor = _json_request(http_session, record_bridge_response, "GET",
                                 command_bridge_base_url, "/events/cursor", 200)["data"]
    cursor_evidence_ids.append(bridge_event_trace.responses[-1]["eventId"])
    sources = list(bridge_event_trace.game_events)
    assert len(sources) >= 4, "Two real mutations must yield command and state-change events"
    original = json.dumps(sources, sort_keys=True)
    count = len(sources)
    scenarios = {
        "ordered-control": (list(range(count)), set()),
        "reversed-order": (list(reversed(range(count))),
                           {"non-increasing-game-event-sequence"}),
        "duplicate-terminal": (list(range(count)) + [count - 1],
                               {"duplicate-game-event-id", "non-increasing-game-event-sequence"}),
        "omitted-middle-event": ([0, *range(2, count)], {"game-event-sequence-gap"}),
        "omitted-first-terminal": (list(range(1, count)), set()),
        "omitted-last-state-notification": (list(range(count - 1)), set()),
        "omitted-last-terminal-window": (list(range(count - 2)), set()),
        "omitted-all-events": ([], set()),
    }
    baseline = _world(http_session, record_bridge_response, command_bridge_base_url)
    request_count = len(bridge_event_trace.requests)
    live_count = len(bridge_event_trace.game_events)
    command_id = terminal["commandId"]
    for scenario, (order, expected_anomalies) in scenarios.items():
        replay = bridge_event_trace.replay_observations(scenario, sources, order)
        after = _world(http_session, record_bridge_response, command_bridge_base_url)
        receipt = _json_request(http_session, record_bridge_response, "GET",
                                command_bridge_base_url, f"/commands/{command_id}", 200)["data"]
        # Record facts before assertions, so any failure retains the full controlled timeline.
        checkpoint = bridge_event_trace.replay_checkpoint(scenario, replay, {
            "beforeState": baseline, "afterState": after, "actualReceipt": receipt,
            "beforeCursor": before_cursor, "afterCursor": after_cursor,
            "cursorResponseEvidenceIds": cursor_evidence_ids,
        })
        anomalies = {item for event in replay for item in event["details"]["sequenceObservations"]}
        assert anomalies == expected_anomalies, replay
        expected_commands = {event["commandId"] for event in sources
                             if event["name"] == "command-completed"}
        assert set(checkpoint["details"]["expectedTerminalCommandIds"]) == expected_commands
        expected_missing = ({sources[0]["commandId"]} if scenario == "omitted-first-terminal"
                            else {sources[-2]["commandId"]}
                            if scenario == "omitted-last-terminal-window"
                            else expected_commands if scenario == "omitted-all-events" else set())
        assert set(checkpoint["details"]["missingTerminalCommandIds"]) == expected_missing
        assert checkpoint["details"]["allFramesComplete"] is (scenario == "ordered-control")
        assert checkpoint["details"]["completenessBlindSpot"] is None
        window = checkpoint["details"]["eventWindow"]
        expected_sequences = set(range(before_cursor["lastEmittedEventSequence"] + 1,
                                       after_cursor["lastEmittedEventSequence"] + 1))
        assert set(window["missingEventSequences"]) == expected_sequences - {
            event["eventSequence"] for event in replay}
        assert not window["foreignEventIds"]
        assert [event["details"]["sourceEvidenceId"] for event in replay] == [
            sources[index]["eventId"] for index in order]
        for event, source_index in zip(replay, order):
            source = sources[source_index]
            assert event["eventType"] == "controlled_event_replay"
            assert event["details"]["observationSource"] == "controlled-replay"
            assert event["details"]["gameEvent"] == source["details"]["gameEvent"]
            assert event["details"]["wirePayloadSha256"] == source["details"]["wirePayloadSha256"]
            assert event["details"]["receiveIndex"] == source["details"]["receiveIndex"]
            assert event["eventSequence"] == source["eventSequence"]
            assert event["eventId"] != source["eventId"]
            assert event["eventId"] != source["details"]["gameEventId"]
            assert not event["details"]["schemaErrors"]
        assert (after["sessionId"], after["worldGeneration"], after["stateVersion"],
                after["simulationSpeed"], after["residentCount"]) == (
                    baseline["sessionId"], baseline["worldGeneration"], baseline["stateVersion"],
                    baseline["simulationSpeed"], 15)
        assert receipt["commandId"] == command_id
        assert receipt["status"] == "completed"
        assert receipt["result"] == terminal["details"]["gameEvent"]["result"]
        assert receipt["stateVersion"] == terminal["stateVersion"] == after["stateVersion"]
    assert json.dumps(sources, sort_keys=True) == original
    assert len(bridge_event_trace.game_events) == live_count
    resync_requests = bridge_event_trace.requests[request_count:]
    assert len(resync_requests) == 16
    assert all(event["name"].startswith("GET ") for event in resync_requests)


@pytest.mark.parametrize("disconnect_mode", ["client-close", "server-policy-close"])
def test_real_disconnect_reconnect_preserves_receipt_and_reclaims_slots(  # pylint: disable=too-many-locals,too-many-statements
    disconnect_mode: str,
    http_session: requests.Session,
    command_bridge_base_url: str,
    record_bridge_response: ResponseRecorder,
    bridge_event_trace,
) -> None:
    """Real TCP/WS gaps never justify reposting a completed command or faking replay."""
    trace = bridge_event_trace
    trace.resource_checkpoint(f"{disconnect_mode}-baseline")
    baseline = _world(http_session, record_bridge_response, command_bridge_base_url)
    command = {**_command(baseline), "speed": 2 if baseline["simulationSpeed"] != 2 else 3}
    _json_request(http_session, record_bridge_response, "POST", command_bridge_base_url,
                  "/commands/set-speed", 200, payload=command)
    previous_event = trace.wait_for_command(command["commandId"])
    trace.flush(0.1)
    baseline_sequence = trace.game_events[-1]["eventSequence"]
    for cycle in range(1, 4):
        before = _world(http_session, record_bridge_response, command_bridge_base_url)
        old_connection = trace.stream.connection_id
        trace.disconnect(disconnect_mode)
        if cycle == 1:
            trace.resource_checkpoint(f"{disconnect_mode}-disconnected")
        live_count = len(trace.game_events)
        request_start = len(trace.requests)
        offline = {**_command(before), "speed": 2 if before["simulationSpeed"] != 2 else 3}
        posted = _json_request(http_session, record_bridge_response, "POST",
                               command_bridge_base_url, "/commands/set-speed", 200,
                               payload=offline)["data"]
        queried = _poll_command(http_session, record_bridge_response, command_bridge_base_url,
                                offline["commandId"])
        offline_state = _world(http_session, record_bridge_response, command_bridge_base_url)
        trace.connection_checkpoint(f"{disconnect_mode}-{cycle}-offline", {
            "beforeState": before, "afterState": offline_state,
            "postedReceipt": posted, "queriedReceipt": queried,
            "disconnectedCommandId": offline["commandId"],
            "noObservedEventMeaning": "subscription closed; not business command failure",
        })
        assert posted == queried and queried["status"] == "completed"
        assert queried["commandId"] == offline["commandId"]
        assert offline_state["stateVersion"] == before["stateVersion"] + 1
        assert offline_state["simulationSpeed"] == offline["speed"]
        assert len(trace.game_events) == live_count
        trace.reconnect()
        trace.flush(0.1)
        assert trace.stream.connection_id != old_connection
        assert len(trace.game_events) == live_count, "Bridge must not invent history replay"
        resync_state = _world(http_session, record_bridge_response, command_bridge_base_url)
        resync_receipt = _json_request(http_session, record_bridge_response, "GET",
                                      command_bridge_base_url,
                                      f"/commands/{offline['commandId']}", 200)["data"]
        trace.connection_checkpoint(f"{disconnect_mode}-{cycle}-resync", {
            "beforeState": offline_state, "afterState": resync_state,
            "queriedReceipt": resync_receipt, "disconnectedCommandId": offline["commandId"],
            "previousConnectionId": old_connection,
        })
        assert resync_receipt == queried
        for field in ("sessionId", "worldGeneration", "stateVersion", "simulationSpeed",
                      "residentCount"):
            assert resync_state[field] == offline_state[field]
        assert resync_state["residentCount"] == 15
        recovery_requests = trace.requests[request_start:]
        assert sum(event["name"].startswith("POST ") for event in recovery_requests) == 1
        assert all(event["name"].startswith("GET ") for event in recovery_requests[1:])
        fresh = {**_command(resync_state),
                 "speed": 2 if resync_state["simulationSpeed"] != 2 else 3}
        fresh_receipt = _json_request(http_session, record_bridge_response, "POST",
                                     command_bridge_base_url, "/commands/set-speed", 200,
                                     payload=fresh)["data"]
        previous_event = trace.wait_for_command(fresh["commandId"])
        trace.flush(0.1)
        source = previous_event["details"]["gameEvent"]
        assert source["eventType"] == "command-completed"
        assert source["commandId"] == fresh_receipt["commandId"]
        assert source["stateVersion"] == fresh_receipt["stateVersion"]
        assert source["worldGeneration"] == resync_state["worldGeneration"]
        assert previous_event["details"]["connectionId"] == trace.stream.connection_id
        assert previous_event["eventSequence"] > baseline_sequence + 1
        assert previous_event["details"]["relatedRequestEvidenceIds"]
        assert previous_event["details"]["relatedResponseEvidenceIds"]
        assert previous_event["eventId"] != source["eventId"]
        assert not any(event["commandId"] == offline["commandId"] for event in trace.game_events)
        baseline_sequence = trace.game_events[-1]["eventSequence"]
        _json_request(http_session, record_bridge_response, "GET", command_bridge_base_url,
                      "/health", 200)
    # More cumulative subscriptions than MAX_CLIENTS=8, serial, NOT concurrent load.
    churn_before = _world(http_session, record_bridge_response, command_bridge_base_url)
    for _ in range(9):
        trace.disconnect(disconnect_mode)
        trace.reconnect()
        _json_request(http_session, record_bridge_response, "GET", command_bridge_base_url,
                      "/health", 200)
    churn_after = _world(http_session, record_bridge_response, command_bridge_base_url)
    trace.connection_checkpoint(f"{disconnect_mode}-serial-slot-reuse", {
        "beforeState": churn_before, "afterState": churn_after,
        "serialSubscriptionCycles": 9, "serverMaxClients": 8,
        "scopeLimit": "slot reuse and reader cleanup, NOT proof of no memory leak",
    })
    trace.resource_checkpoint(f"{disconnect_mode}-after-churn")
    for field in ("sessionId", "worldGeneration", "stateVersion", "simulationSpeed",
                  "residentCount"):
        assert churn_after[field] == churn_before[field]
    assert len(set(trace.connection_history)) == len(trace.connection_history) == 13
    source_ids = [event["details"]["gameEventId"] for event in trace.game_events]
    sequences = [event["eventSequence"] for event in trace.game_events]
    assert len(source_ids) == len(set(source_ids))
    assert all(current > previous for previous, current in zip(sequences, sequences[1:]))
    assert not trace.gaps and not trace.protocol_errors
