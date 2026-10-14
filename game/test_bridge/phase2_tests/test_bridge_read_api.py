"""Read-only HTTP integration tests against an isolated 15-resident world."""

from __future__ import annotations

import json
import ctypes
import hashlib
from os import environ  # pylint: disable=no-name-in-module
import subprocess
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable

import pytest
import requests
from jsonschema import Draft202012Validator, FormatChecker

# Match the existing suite's namespace-package workaround for Pylint 2.14.5.
from game.test_bridge.evidence_store import EvidenceStore  # pylint: disable=import-error
from game.test_bridge.phase2_tests.test_evidence_import import (  # pylint: disable=import-error
    _audit_retention_journal,
)
from game.test_bridge.phase2_tests.test_bridge_command_api import (  # pylint: disable=import-error
    prepare_native_case_files, prepare_host_flow_driver, record_host_flow_trace,
)


pytestmark = pytest.mark.live_bridge
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "protocol.schema.json"
ResponseRecorder = Callable[[str, requests.Response, int], None]


@pytest.fixture(name="local_model_protocol")
def fixture_local_model_protocol(tmp_path: Path):
    """Real loopback transport, deterministic model fixture, never a real LLM."""
    exchanges = []

    class Handler(BaseHTTPRequestHandler):
        """Answer only the existing Provider health protocol; retain raw bodies."""
        def do_POST(self):  # pylint: disable=invalid-name
            """Return the configured health JSON over an actual HTTP exchange."""
            self.connection.settimeout(3)
            exchange = {"path": self.path, "contentLength": self.headers.get("Content-Length"),
                        "source": self.headers.get("X-Test-Source", "godot"), "status": "reading"}
            exchanges.append(exchange)
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            response = {"choices": [{"message": {"role": "assistant",
                         "content": '{"ok":true}'}, "finish_reason": "stop"}]}
            decoded = json.loads(body)
            health_only = (decoded.get("model") == "closeout-fixture"
                           and decoded.get("messages", [])[-1].get("content")
                           == 'Return exactly {"ok":true}.')
            status = 200 if self.path == "/v1/chat/completions" and health_only else 422
            exchange.update({"body": body.decode("utf-8"),
                             "status": status, "response": response})
            payload = json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            """Do not print headers/credentials or noisy server access messages."""

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/v1"
        with requests.Session() as probe:
            probe.trust_env = False
            response = probe.post(endpoint + "/chat/completions", timeout=3,
                                  headers={"X-Test-Source": "python-listener-probe"},
                                  json={"model": "closeout-fixture", "messages": [
                                      {"content": 'Return exactly {"ok":true}.'}]})
            assert response.status_code == 200
        yield endpoint
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
        (tmp_path / "model-protocol.json").write_text(
            json.dumps({"scope": "deterministic protocol fixture, not a real LLM",
                        "exchanges": exchanges}, ensure_ascii=False, indent=2), encoding="utf-8")
        assert not worker.is_alive()


@pytest.fixture(scope="module", name="response_validator")
def fixture_response_validator() -> Draft202012Validator:
    """Create one validator for every top-level Bridge response."""
    with SCHEMA_PATH.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _request_json(
    session: requests.Session,
    recorder: ResponseRecorder,
    base_url: str,
    path: str,
    expected_status: int,
) -> dict:
    response = session.get(f"{base_url}{path}", timeout=3.0)
    recorder(f"GET {path}", response, expected_status)
    assert response.status_code == expected_status, response.text
    assert response.headers["Content-Type"].startswith("application/json")
    return response.json()


def test_health_and_world_match_protocol_schema(
    http_session: requests.Session,
    bridge_base_url: str,
    response_validator: Draft202012Validator,
    record_bridge_response: ResponseRecorder,
) -> None:
    """Health and world endpoints must match the schema and fixture facts."""
    health = _request_json(http_session, record_bridge_response, bridge_base_url, "/health", 200)
    world = _request_json(
        http_session, record_bridge_response, bridge_base_url, "/world/state", 200,
    )

    response_validator.validate(health)
    response_validator.validate(world)
    assert health["data"] == {
        "bridgeReady": True,
        "readOnly": True,
        "worldReady": True,
    }
    assert world["data"]["residentCount"] == len(world["data"]["residents"])
    assert world["data"]["residentCount"] == 15


def test_event_cursor_is_read_only_and_matches_world_metadata(
    http_session: requests.Session, bridge_base_url: str,
    response_validator: Draft202012Validator, record_bridge_response: ResponseRecorder,
) -> None:
    """Cursor reads neither generate events nor mutate a quiescent isolated world."""
    before = _request_json(http_session, record_bridge_response, bridge_base_url,
                           "/world/state", 200)["data"]
    cursors = [_request_json(http_session, record_bridge_response, bridge_base_url,
                             "/events/cursor", 200) for _ in range(3)]
    after = _request_json(http_session, record_bridge_response, bridge_base_url,
                          "/world/state", 200)["data"]
    for cursor in cursors:
        response_validator.validate(cursor)
        assert set(cursor["data"]) == {"sessionId", "worldGeneration", "stateVersion",
                                       "capturedAt", "streamId", "lastEmittedEventSequence"}
        for key in ("sessionId", "worldGeneration", "stateVersion"):
            assert cursor["data"][key] == before[key] == after[key]
    assert len({cursor["data"]["streamId"] for cursor in cursors}) == 1
    assert {cursor["data"]["lastEmittedEventSequence"] for cursor in cursors} == {0}
    assert before["residentCount"] == after["residentCount"] == 15
    assert before["simulationSpeed"] == after["simulationSpeed"]


def test_world_resident_ids_are_unique_and_detail_is_public_only(
    http_session: requests.Session,
    bridge_base_url: str,
    response_validator: Draft202012Validator,
    record_bridge_response: ResponseRecorder,
) -> None:
    """Resident IDs stay unique and details expose only the public allowlist."""
    world = _request_json(
        http_session, record_bridge_response, bridge_base_url, "/world/state", 200,
    )
    residents = world["data"]["residents"]
    resident_ids = [resident["residentId"] for resident in residents]
    assert len(resident_ids) == len(set(resident_ids))

    detail = _request_json(
        http_session,
        record_bridge_response,
        bridge_base_url,
        f"/residents/{resident_ids[0]}",
        200,
    )
    response_validator.validate(detail)
    public_resident = detail["data"]["resident"]
    assert set(public_resident) == {
        "residentId",
        "name",
        "currentPlace",
        "spaceId",
        "position",
        "doing",
        "isPresent",
    }
    assert "memory" not in public_resident
    assert "model" not in public_resident


def test_repeated_reads_do_not_change_state_version(
    http_session: requests.Session,
    bridge_base_url: str,
    record_bridge_response: ResponseRecorder,
) -> None:
    """Ten read requests must not mutate revision or world generation."""
    versions = []
    generations = []
    for _ in range(10):
        body = _request_json(
            http_session, record_bridge_response, bridge_base_url, "/world/state", 200,
        )
        versions.append(body["data"]["stateVersion"])
        generations.append(body["data"]["worldGeneration"])

    assert len(set(versions)) == 1
    assert len(set(generations)) == 1


@pytest.mark.parametrize(
    ("path", "status", "error_code"),
    [
        ("/residents/resident_missing_01", 404, "RESIDENT_NOT_FOUND"),
        ("/missing", 404, "ROUTE_NOT_FOUND"),
        ("/world/state?unexpected=true", 400, "INVALID_PATH"),
    ],
)
# Pytest injects four fixtures plus three parameter values by design.
# pylint: disable=too-many-arguments
def test_read_errors_are_structured_and_schema_valid(
    http_session: requests.Session,
    bridge_base_url: str,
    response_validator: Draft202012Validator,
    record_bridge_response: ResponseRecorder,
    path: str,
    status: int,
    error_code: str,
) -> None:
    """Missing resources and invalid paths return distinct structured errors."""
    body = _request_json(http_session, record_bridge_response, bridge_base_url, path, status)
    response_validator.validate(body)
    assert body["ok"] is False
    assert body["data"] is None
    assert body["error"]["code"] == error_code


def test_isolated_fixture_is_read_only(
    http_session: requests.Session,
    bridge_base_url: str,
    record_bridge_response: ResponseRecorder,
) -> None:
    """The default isolated fixture must reject all write commands."""
    response = http_session.post(
        f"{bridge_base_url}/commands/set-speed",
        json={},
        timeout=3.0,
    )
    record_bridge_response("POST /commands/set-speed [read-only]", response, 405)
    assert response.status_code == 405
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_non_loopback_host_is_rejected(
    http_session: requests.Session,
    bridge_base_url: str,
    response_validator: Draft202012Validator,
    record_bridge_response: ResponseRecorder,
) -> None:
    """A non-loopback Host header must fail the DNS-rebinding guard."""
    response = http_session.get(
        f"{bridge_base_url}/health",
        headers={"Host": "example.invalid"},
        timeout=3.0,
    )
    record_bridge_response("GET /health [invalid Host]", response, 403)
    assert response.status_code == 403
    body = response.json()
    response_validator.validate(body)
    assert body["error"]["code"] == "HOST_NOT_ALLOWED"


_PRODUCTION_DRIVER = r'''extends SceneTree
const FS = preload("res://agent/AgentFileSystem.gd")
const JSON_HELPER = preload("res://agent/AgentJson.gd")
const AGENT = preload("res://agent/AgentSystem.gd")
const AGENT_STORE = preload("res://agent/lifecycle/AgentSaveStore.gd")
const INTERNAL = preload("res://world/presentation/session/TownInternalPlaytestCatalog.gd")
const COMPILER = preload("res://world/presentation/session/TownNewGameOpeningCompiler.gd")
const BOOTSTRAP = preload("res://world/presentation/session/TownSessionBootstrap.gd")
const PROVIDER = preload("res://world/integration/TownAgentProviderService.gd")
const GATEWAY = preload("res://world/integration/TownWorldAgentGateway.gd")
const RUNTIME = preload("res://world/presentation/town_runtime/TownRuntime.tscn")
const UI_SERVICE = preload("res://world/presentation/session/TownSessionUiService.gd")
var checks: Array = []
var spec: Dictionary
func check(id: String, passed: bool, actual: Variant = null) -> void:
	checks.append({"id": id, "passed": passed, "actual": actual})
	print("ROOT_PHOTO_CHECK " + JSON.stringify(checks.back()))
func _initialize() -> void:
	call_deferred("run")
func same(a: Variant, b: Variant) -> bool:
	return JSON_HELPER.content_sha256(a) == JSON_HELPER.content_sha256(b)
func path(name: String) -> String:
	var base := String(spec["sandbox"]).path_join("owned/host" if String(spec["mode"]).begins_with("production-host") else "owned")
	return base if name.is_empty() else base.path_join(name)
func gateway(name: String) -> Node:
	if String(spec["mode"]).begins_with("production-host"):
		return root.get_node("GameFlowHost").call("_create_session_gateway")
	var store := AGENT_STORE.new()
	store.set("_store_root", path("agent-saves"))
	var system := AGENT.new(store)
	var base := path("memory-" + name)
	DirAccess.make_dir_absolute(base)
	system.set("_runtime_memory_root_base", base)
	system.set("_memory_root", base.path_join("initial"))
	var value := GATEWAY.new()
	value.set("_agent_system", system)
	var photos: RefCounted = value.get("_photo_store")
	photos.set("_storage_root", path("photos"))
	(photos.get("_save_store") as RefCounted).set("_root", path("photo-leases"))
	return value
func service(runtime: Node, value: Node, config: Dictionary) -> RefCounted:
	var result := UI_SERVICE.new()
	var configured: Dictionary = result.configure(runtime, runtime.call("get_world_runtime"), value.call("get_agent_save_participant"), config)
	check("real-ui-service-" + runtime.name, configured.get("ok") == true, configured)
	# configure has no storage side effects; bind its actual store before any operation.
	if String(spec["mode"]).begins_with("production-host"):
		var bound: Dictionary = root.get_node("GameFlowHost").call("_gametest_bind_session_store", result)
		check("real-host-ui-store-binding", bound.get("ok") == true and result.get("_store").get("_root") == path("world-saves"), bound)
	else:
		(result.get("_store") as RefCounted).set("_root", path("world-saves"))
	return result
func tasks(world: RefCounted) -> RefCounted:
	return (world.get("work_domain") as RefCounted).get("tasks") as RefCounted
func task(world: RefCounted, id: String, resident: String, complete: bool) -> Dictionary:
	var values := tasks(world)
	var result: Dictionary = values.call("create_task", {"taskId": id, "capability": "library.accession",
		"sourceKind": "research_handoff", "sourceRef": id + "-source",
		"targets": [{"kind": "prop", "ref": "图书馆归还书台"}],
		"requestedResultKind": "accession_record", "createdAtMinute": 0, "priority": 73})
	if result.get("ok") != true:
		return result
	result = values.call("add_eligible_residents", id, [resident])
	if result.get("ok") != true:
		return result
	var data: Dictionary = result["task"]
	var occupations: Array = data.get("eligibleOccupationIds", [])
	result = values.call("accept_task", id, resident, String(occupations[0]) if not occupations.is_empty() else "", int(data["revision"]))
	if result.get("ok") != true:
		return result
	result = values.call("start_task", id, resident, int(result["task"]["revision"]))
	if result.get("ok") != true or not complete:
		return result
	return values.call("complete_task", id, resident, int(result["task"]["revision"]), "accession_record",
		{"resultRef": id + "-result", "facts": {"fixture": "phase2-production-persistence"}})
func logs(world: RefCounted) -> Array:
	var result: Array = []
	var listed: Dictionary = world.call("query_world_log_threads", {"limit": 200})
	check("real-log-query", listed.get("ok") == true)
	for row: Dictionary in listed.get("rows", []):
		var detail: Dictionary = world.call("get_world_log_thread_detail", String(row["threadId"]), {"limit": 500})
		for record: Dictionary in detail.get("records", []):
			var payload: Dictionary = record.get("payload", {})
			if String(payload.get("taskId", "")).begins_with("phase2-"):
				result.append(record.duplicate(true))
	result.sort_custom(func(a, b): return JSON.stringify(a) < JSON.stringify(b))
	return result
func memory(value: Node, resident: String) -> Dictionary:
	var response: Dictionary = value.call("get_resident_memory", resident)
	check("real-memory-query", response.get("ok") == true, response.get("errorCode"))
	return response.get("memory", {})
func memory_entry(value: Node, resident: String) -> Dictionary:
	for entry: Dictionary in memory(value, resident).get("formal_memories", []):
		if entry.get("memoryKey") == "phase2-memory":
			return entry.duplicate(true)
	return {}
func projection(world: RefCounted, value: Node, resident: String) -> Dictionary:
	return {"task": tasks(world).call("task", "phase2-state"),
		"logs": logs(world), "memory": memory_entry(value, resident),
		"worldRuntimeGeneration": world.call("get_runtime_generation"),
		"stateVersion": world.call("get_world_revision"),
		"agentContext": value.call("get_agent_save_context"),
		"worldSnapshot": world.call("create_save_snapshot"),
		"worldLogDebug": world.call("get_world_log_debug_snapshot"),
		"bridgeWorldGeneration": null, "bridgeEventSequence": null}
func snapshot(name: String, state: Dictionary) -> void:
	var file := FileAccess.open(String(spec["sandbox"]).path_join(name), FileAccess.WRITE)
	file.store_string(JSON.stringify(state))
	file = null
func run() -> void:
	spec = JSON.parse_string(FileAccess.get_file_as_string(OS.get_cmdline_user_args()[0]))
	if String(spec["mode"]).begins_with("production-host"):
		var rejected: Dictionary = root.get_node("GameFlowHost").call("gametest_configure_storage", path("before-enrollment"))
		check("host-reject-before-enrollment", rejected.get("errorCode") == "GAMETEST_STORAGE_PATH_INVALID", rejected)
	var enrolled := FS.configure_test_retention(spec["sandbox"])
	check("enrollment", enrolled == OK, int(enrolled))
	if enrolled != OK:
		quit(1)
		return
	# Let startup/import frame deltas settle before timing a new HTTPRequest.
	await process_frame
	await process_frame
	await production()
	var audio := root.get_node_or_null("TownAudioController")
	if audio != null and audio.has_method("prepare_shutdown"):
		audio.call("prepare_shutdown")
	await create_timer(0.3, true, false, true).timeout
	quit(0 if checks.all(func(item): return item["passed"]) else 1)
func production() -> void:
	var host := Node.new()
	root.add_child(host)
	if String(spec["mode"]).begins_with("production-host"):
		var flow := root.get_node("GameFlowHost")
		for candidate: Variant in [null, true, "", spec["sandbox"], spec["foreign"], "user://town_session_saves", path("../escape"), path("invalid/")]:
			var rejected: Dictionary = flow.call("gametest_configure_storage", candidate)
			check("host-root-rejected-" + str(candidate), rejected.get("ok") == false and flow.get("_startup_save_catalog") == null, rejected)
		var blocked_root := path("blocked")
		DirAccess.make_dir_recursive_absolute(blocked_root)
		var directory_probe := FileAccess.open(blocked_root.path_join("file-root"), FileAccess.WRITE)
		directory_probe.store_string("not-a-directory")
		directory_probe = null
		check("host-reject-file-root", flow.call("gametest_configure_storage", blocked_root.path_join("file-root")).get("errorCode") == "GAMETEST_STORAGE_PATH_INVALID")
		var blocked_path := blocked_root.path_join("provider-settings.json.bak")
		var backup := FileAccess.open(blocked_path, FileAccess.WRITE)
		backup.store_string("provider-backup-proof")
		backup = null
		check("host-reject-provider-recovery-evidence", flow.call("gametest_configure_storage", blocked_root).get("errorCode") == "GAMETEST_PROVIDER_RECOVERY_EVIDENCE_PRESENT" and FileAccess.get_file_as_string(blocked_path) == "provider-backup-proof")
		DirAccess.make_dir_absolute(path(""))
		var bound: Dictionary = flow.call("gametest_configure_storage", path(""))
		check("host-native-storage-configured", bound.get("ok") == true, bound)
		if bound.get("ok") != true:
			return
		check("host-storage-one-way", flow.call("gametest_configure_storage", path("")).get("errorCode") == "GAMETEST_STORAGE_ALREADY_CONFIGURED")
		check("host-startup-world-root", flow.get("_startup_save_store").get("_root") == path("world-saves"))
		check("host-startup-agent-root", flow.get("_startup_agent_save_store").get("_store_root") == path("agent-saves"))
		check("host-startup-profile-root", flow.get("_startup_save_catalog").get("_profile_path") == path("startup-profile.json"))
		var archive: RefCounted = flow.call("_resolve_formal_archive_service")
		check("host-archive-root", archive.get("_world_slots_root") == path("world-saves/slots") and archive.get("_agent_slots_root") == path("agent-saves") and archive.get("_photo_slots_root") == path("photos") and archive.get("_backup_root") == path("backups"))
		var settings: RefCounted = flow.call("_create_provider_settings_service")
		check("host-provider-settings-roots", settings.get("_store").get("_path") == path("provider-settings.json") and settings.get("_credential_store").get("_path") == path("provider-credentials.enc"))
		check("host-root-binding-has-no-storage-side-effects", not DirAccess.dir_exists_absolute(path("world-saves")) and not FileAccess.file_exists(path("startup-profile.json")))
	var provider := PROVIDER.new()
	var configured: Dictionary = provider.configure({"capabilityMode": "formal", "source": "runtime",
		"allowFake": false, "providerConfigs": {"ollama": {"endpoint": spec["modelEndpoint"],
		"api_key": "isolated-fixture-not-secret", "api_model": "closeout-fixture",
		"api_models": ["closeout-fixture"], "timeout_seconds": 5}}}, host)
	check("actual-formal-provider-config", configured.get("ok") == true and configured.get("formalReady") == true, configured)
	var health_results: Array = []
	var health_started: Dictionary = provider.request_health_check(
		[{"providerId": "ollama", "modelId": "closeout-fixture"}], func(value): health_results.append(value))
	check("actual-health-check-accepted", health_started.get("accepted") == true, health_started)
	print("PRODUCTION_DIAGNOSTIC " + JSON.stringify({"healthCallbacksAtAcceptance": health_results.size(),
		"hostChildClasses": host.get_children().map(func(child): return child.get_class()), "ticks": Time.get_ticks_msec()}))
	var deadline := Time.get_ticks_msec() + 15000
	var observed_transport := false
	while health_results.is_empty() and Time.get_ticks_msec() < deadline:
		if not observed_transport and Time.get_ticks_msec() > deadline - 13000:
			for child: Node in host.get_children():
				if child is HTTPRequest:
					print("PRODUCTION_DIAGNOSTIC " + JSON.stringify({"clientStatus": child.get_http_client_status(), "scenePaused": paused, "canProcess": child.can_process()}))
			observed_transport = true
		await process_frame
	check("actual-http-health-check-completed", not health_results.is_empty() and health_results[0].get("ok") == true, health_results)
	if health_results.is_empty() or health_results[0].get("ok") != true:
		host.free()
		return
	var data: Dictionary = JSON.parse_string(FileAccess.get_file_as_string("res://world/data/town/town_world.json"))
	var selection := INTERNAL.build_view_model("ollama", "closeout-fixture")
	var selection_data: Dictionary = selection["data"]
	selection_data["selected_resident_ids"] = selection_data["recommended_resident_ids"].slice(0, 15)
	INTERNAL.update_confirmation_payload(selection_data, "ollama", "closeout-fixture", 2)
	var draft: Dictionary = selection_data["confirmation_payload"]
	var catalog := INTERNAL.build_catalog(data, selection)
	var compiled := COMPILER.compile(draft, data, catalog)
	check("actual-compiler", compiled.get("ok") == true, compiled.get("errorCode"))
	if compiled.get("ok") != true:
		host.free()
		return
	var source := gateway("source")
	var runtime: Node = RUNTIME.instantiate()
	runtime.name = "SourceRuntime"
	runtime.set_process(false)
	runtime.set_physics_process(false)
	var bootstrap := BOOTSTRAP.new()
	var outcomes: Array = []
	var started: Dictionary = bootstrap.begin_new_game_from_catalog(draft, data, catalog, provider, source, runtime,
		{"worldStartMode": "formal", "sessionId": "phase2-production", "slotId": "town-3" if spec["mode"] == "production-host-flow" else "phase2-slot",
		"requestHost": host, "useLiveModel": true, "enablePlayerAvatar": false}, func(value): outcomes.append(value))
	check("actual-bootstrap-accepted", started.get("accepted") == true, started)
	deadline = Time.get_ticks_msec() + 15000
	while outcomes.is_empty() and Time.get_ticks_msec() < deadline:
		await process_frame
	check("actual-bootstrap-completion", not outcomes.is_empty() and outcomes[0].get("ok") == true, outcomes)
	if outcomes.is_empty() or outcomes[0].get("ok") != true:
		runtime.free()
		source.free()
		host.free()
		return
	var config: Dictionary = outcomes[0]["sessionConfig"].duplicate(true)
	check("formal-ready-from-real-health-and-bootstrap", config.get("formalReady") == true and config.get("capabilityMode") == "formal", config)
	root.add_child(runtime)
	runtime.process_mode = Node.PROCESS_MODE_DISABLED
	runtime.set_process(false)
	runtime.set_physics_process(false)
	source.call("cancel_background_departure_messages")
	check("actual-runtime-startup", runtime.call("get_startup_result").get("ok") == true)
	var world: RefCounted = runtime.call("get_world_runtime")
	var ids: Array = config["residentIdentities"]
	check("actual-population-15", ids.size() == 15 and world.call("get_resident_identity_snapshot").get("residents", []).size() == 15)
	check("controlled-runtime-clock", not runtime.is_processing() and not runtime.is_physics_processing())
	check("controlled-runtime-subtree", runtime.find_children("*", "Node", true, false).all(func(child): return not child.can_process()))
	var resident: String = ids[0]["residentId"]
	var written: Dictionary = source.call("apply_resident_memory_intervention", resident,
		{"memoryKey": "phase2-memory", "operation": "write", "playerText": "saved-memory", "expectedRevision": 0})
	check("native-memory-write", written.get("ok") == true, written)
	check("real-task-in-progress", task(world, "phase2-state", resident, false).get("ok") == true)
	check("real-task-log-complete", task(world, "phase2-log", resident, true).get("ok") == true)
	var expected := projection(world, source, resident)
	check("nonempty-three-projections", expected["task"].get("state") == "in_progress" and not expected["logs"].is_empty() and expected["memory"].get("subject") == "saved-memory", expected)
	snapshot("expected-state.json", expected)
	var ui := service(runtime, source, config)
	var accepted: Dictionary = ui.call("begin_create_save_async", {"reason": "phase2-production-roundtrip"})
	check("real-ui-async-accepted", accepted.get("ok") == true and accepted.get("pending") == true, accepted)
	var busy: Dictionary = ui.call("begin_create_save_async")
	check("real-ui-rejects-second-async", busy.get("ok") == false and busy.get("errorCode") == "SESSION_SAVE_BUSY", busy)
	var saved: Dictionary = {}
	var worker_observed := false
	var worker_info := {}
	deadline = Time.get_ticks_msec() + 30000
	while Time.get_ticks_msec() < deadline:
		saved = ui.call("poll_create_save_async")
		var job: Variant = ui.get("_async_save_job")
		if job != null:
			var worker: Thread = job.get("_thread")
			worker_observed = worker_observed or (worker != null and worker.is_started() and worker.get_id() != str(OS.get_thread_caller_id()))
			if worker != null and worker.is_started():
				worker_info = {"threadId": worker.get_id(), "mainThreadId": str(OS.get_thread_caller_id()), "pid": OS.get_process_id()}
		if not bool(saved.get("pending", false)):
			break
		await process_frame
	check("actual-async-worker-observed", worker_observed, worker_info)
	if spec["mode"] == "production-failure":
		check("native-worker-manifest-publish-failure", saved.get("ok") == false and not bool(saved.get("pending", true)) and saved.get("errorCode") == "SESSION_SAVE_AGENT_ORPHAN_ISOLATED" and saved.get("meta", {}).get("stage") == "manifest_publish" and saved.get("meta", {}).get("participantBlocker") == "AGENT_SINGLE_REVISION_CLEANUP_MISSING", saved)
		var failed_state := projection(world, source, resident)
		snapshot("failed-save-state.json", failed_state)
		check("async-failure-preserves-live-world-agent-and-memory", same(failed_state, expected))
		check("async-failure-releases-job", not ui.call("has_active_create_save_async"))
		var store: RefCounted = ui.get("_store")
		var published: Dictionary = store.call("list_published", "phase2-slot")
		check("failed-manifest-not-discoverable", published.get("ok") == true and published.get("manifests", []).is_empty(), published)
		var incomplete: Dictionary = store.call("list_incomplete", "phase2-slot")
		check("failed-worker-journal-retained", incomplete.get("ok") == true and not incomplete.get("records", []).is_empty(), incomplete)
		var lease: Dictionary = store.call("begin_slot_transaction", "phase2-slot")
		check("async-failure-lease-reacquirable", lease.get("ok") == true, lease)
		if lease.get("ok") == true:
			check("async-failure-lease-release", store.call("end_slot_transaction", lease["leaseToken"]).get("ok") == true)
		check("failed-worker-source-close", source.call("close_session").get("ok") == true)
		runtime.free()
		host.free()
		return
	check("actual-async-save-completed", saved.get("ok") == true and not bool(saved.get("pending", true)), saved)
	if saved.get("ok") != true or bool(saved.get("pending", true)):
		runtime.free()
		host.free()
		return
	var context: Dictionary = saved["context"]
	check("published-async-revision", context["save_revision"] == 1)
	check("agent-adopts-published-context", same(source.call("get_agent_save_context"), context))
	check("async-state-released", not ui.call("has_active_create_save_async"))
	if spec["mode"] == "production-host-flow":
		await bridge_host_roundtrip(runtime, source, ui, config, provider, resident, expected)
		host.free()
		return
	var changed: Dictionary = source.call("apply_resident_memory_intervention", resident,
		{"memoryKey": "phase2-memory", "operation": "edit", "playerText": "mutated-memory",
		"expectedRevision": int(memory(source, resident).get("formal_memory_revision", -1))})
	check("native-post-save-memory-mutation", changed.get("ok") == true, changed)
	check("native-post-save-task-cancel", tasks(world).call("cancel_task", "phase2-state", "post-save-mutation").get("ok") == true)
	check("native-post-save-log", task(world, "phase2-after-save", resident, true).get("ok") == true)
	var mutated := projection(world, source, resident)
	snapshot("mutated-state.json", mutated)
	check("live-state-actually-differs", mutated["task"].get("state") == "cancelled" and mutated["memory"].get("subject") == "mutated-memory" and not same(mutated["logs"], expected["logs"]))
	check("real-source-session-close", source.call("close_session").get("ok") == true)
	runtime.free()
	var restored := gateway("restored")
	var restored_config := config.duplicate(true)
	restored_config["mode"] = "continue"
	restored_config["restorePending"] = true
	restored_config["saveRevision"] = context["save_revision"]
	check("real-restore-gateway-config", restored.call("configure_session", restored_config, provider, host).get("ok") == true)
	var target: Node = RUNTIME.instantiate()
	target.name = "RestoredRuntime"
	target.set_process(false)
	target.set_physics_process(false)
	check("real-target-gateway-injection", target.call("configure_agent_gateway", restored).get("ok") == true)
	check("real-target-session-config", target.call("configure_session", restored_config).get("ok") == true)
	root.add_child(target)
	target.process_mode = Node.PROCESS_MODE_DISABLED
	target.set_process(false)
	target.set_physics_process(false)
	check("real-target-startup", target.call("get_startup_result").get("ok") == true)
	var target_world: RefCounted = target.call("get_world_runtime")
	var restore_ui := service(target, restored, restored_config)
	var continued: Dictionary = restore_ui.call("continue_revision", context["session_id"], int(context["save_revision"]), data, ids, restored)
	check("real-ui-and-gateway-restore", continued.get("ok") == true, continued)
	restored.call("cancel_background_departure_messages")
	var actual := projection(target_world, restored, resident)
	snapshot("restored-state.json", actual)
	check("restored-runtime-clock-controlled", not target.is_processing() and not target.is_physics_processing())
	check("nonempty-task-exactly-restored", same(actual["task"], expected["task"]), actual["task"])
	check("nonempty-log-exactly-restored-no-after-save-leak", same(actual["logs"], expected["logs"]), actual["logs"])
	check("nonempty-memory-exactly-restored", same(actual["memory"], expected["memory"]), actual["memory"])
	check("real-restore-all-15-agents", restored.call("get_agent_save_participant").call("get_active_resident_ids").get("resident_ids", []).size() == 15)
	check("restore-gate-released", (restore_ui.get("_gate").get("_active") as Dictionary).is_empty())
	check("real-restored-session-close", restored.call("close_session").get("ok") == true)
	target.free()
	host.free()
func bridge_host_roundtrip(_runtime: Node, _source: Node, _ui: RefCounted, _config: Dictionary, _provider: RefCounted, _resident: String, _expected: Dictionary) -> void:
	check("host-flow-driver-not-implemented", false)
'''


_ROOT_PHOTO_DRIVER = r'''extends SceneTree
const FS = preload("res://agent/AgentFileSystem.gd")
const STORE = preload("res://world/presentation/session/TownSessionSaveStore.gd")
const AGENT_STORE = preload("res://agent/lifecycle/AgentSaveStore.gd")
const PHOTO = preload("res://world/integration/TownConversationPhotoStore.gd")
const CATALOG = preload("res://world/presentation/session/TownStartupSaveCatalog.gd")
const JSON_HELPER = preload("res://agent/AgentJson.gd")
var checks: Array = []
var spec: Dictionary
func check(id: String, passed: bool, actual: Variant = null) -> void:
	checks.append({"id": id, "passed": passed, "actual": actual})
	print("ROOT_PHOTO_CHECK " + JSON.stringify(checks.back()))
func _initialize() -> void:
	call_deferred("run")
func run() -> void:
	spec = JSON.parse_string(FileAccess.get_file_as_string(OS.get_cmdline_user_args()[0]))
	var enrollment := FS.configure_test_retention(spec["sandbox"])
	check("enrollment", enrollment == OK, int(enrollment))
	if enrollment != OK:
		quit(1)
		return
	if spec["mode"] == "roots":
		roots()
	elif spec["mode"] == "threads":
		threads()
	elif String(spec["mode"]).begins_with("profile"):
		profiles()
	else:
		photos()
	var audio := root.get_node_or_null("TownAudioController")
	if audio != null and audio.has_method("prepare_shutdown"):
		audio.call("prepare_shutdown")
	await create_timer(0.3, true, false, true).timeout
	quit(0 if checks.all(func(item): return item["passed"]) else 1)
func roots() -> void:
	var store := STORE.new()
	var original: String = store.get("_root")
	for candidate: Variant in [null, true, 5, "", original, STORE.TEST_ROOT,
		STORE.TEST_ROOT + "/../escape", spec["sandbox"], STORE.TEST_ROOT + "/guard/",
		" " + STORE.TEST_ROOT + "/guard", STORE.TEST_ROOT + "/guard\\child",
		STORE.TEST_ROOT + "/guard//child"]:
		var result: Dictionary = store.configure_test_root(candidate)
		check("town-root-reject-" + str(candidate), result.get("ok") == false
			and store.get("_root") == original, result)
	var valid := STORE.TEST_ROOT + "/guard-" + str(OS.get_process_id())
	check("town-root-accept", store.configure_test_root(valid).get("ok") == true)
	check("town-peer-same-root", store.create_isolated_peer().get("_root") == valid)
	check("town-root-one-shot", store.configure_test_root(valid + "-again").get("errorCode") == "SESSION_SAVE_STORE_ALREADY_CONFIGURED")
	var agent := AGENT_STORE.new()
	for candidate: String in ["", AGENT_STORE.DEFAULT_STORE_ROOT, AGENT_STORE.TEST_STORE_ROOT,
		AGENT_STORE.TEST_STORE_ROOT + "/../escape", String(spec["sandbox"])]:
		check("agent-root-reject-" + candidate, agent.configure_test_root(candidate).get("ok") == false
			and agent.get("_store_root") == AGENT_STORE.DEFAULT_STORE_ROOT)
	valid = AGENT_STORE.TEST_STORE_ROOT + "/guard-" + str(OS.get_process_id())
	check("agent-root-trailing-slash-normalization", agent.configure_test_root(valid + "/").get("ok") == true and agent.get("_store_root") == valid)
	check("agent-peer-same-root", agent.create_isolated_peer().get("_store_root") == valid)
	check("agent-root-one-shot", agent.configure_test_root(valid + "-again").get("ok") == false)
	var photo := PHOTO.new()
	check("photo-root-reject", photo.configure_test_roots(PHOTO.STORAGE_ROOT, STORE.TEST_ROOT + "/guard").get("ok") == false)
	check("photo-root-accept", photo.configure_test_roots(PHOTO.TEST_STORAGE_ROOT + "/guard", STORE.TEST_ROOT + "/guard").get("ok") == true)
	check("photo-root-one-shot", photo.configure_test_roots(PHOTO.TEST_STORAGE_ROOT + "/guard2", STORE.TEST_ROOT + "/guard2").get("ok") == false)
func photo(name: String) -> RefCounted:
	var result := PHOTO.new()
	result.set("_storage_root", String(spec["sandbox"]).path_join("owned/" + name))
	(result.get("_save_store") as RefCounted).set("_root", String(spec["sandbox"]).path_join("owned/leases"))
	return result
func profiles() -> void:
	var catalog := CATALOG.new()
	var store := STORE.new()
	check("catalog-formal-configuration-contract", catalog.configure(store).get("ok") == true)
	check("formal-profile-write-denied-before-mkdir", catalog.call("_write_profile", {}).get("errorCode") == "STARTUP_SAVE_PROFILE_TEST_PATH_INVALID")
	var path := String(spec["sandbox"]).path_join("owned/profile-normal.json")
	catalog.set("_profile_path", path)
	var next := {"schema": CATALOG.PROFILE_SCHEMA, "schemaVersion": CATALOG.PROFILE_SCHEMA_VERSION,
		"lastPlayedSlotId": "after", "shownResidentMessages": {}}
	var written: Dictionary = catalog.call("_write_profile", next)
	var published: Variant = JSON.parse_string(FileAccess.get_file_as_string(path))
	var content_matches := JSON.stringify(JSON_HELPER.normalize_numbers(published)) == JSON.stringify(next)
	if spec["mode"] == "profile-failure":
		check("backup-journal-lock-propagated", written.get("ok") == false and written.get("errorCode") == "STARTUP_SAVE_PROFILE_RETENTION_FAILED", written)
		check("failure-not-reported-as-unpublished", written.get("meta", {}).get("profilePublished") == true and written.get("retryable") == false)
		check("published-bytes-not-hidden", content_matches, published)
		var backups: PackedStringArray = []
		for name: String in DirAccess.get_files_at(path.get_base_dir()):
			if name.begins_with(path.get_file() + ".bak-"):
				backups.append(name)
		check("failed-retention-backup-still-present", backups.size() == 1 and FileAccess.get_file_as_string(path.get_base_dir().path_join(backups[0])) == "original-profile")
		return
	check("native-profile-replacement", written.get("ok") == true, written)
	check("published-profile-content", content_matches, published)
	var remaining_backup := false
	for name: String in DirAccess.get_files_at(path.get_base_dir()):
		remaining_backup = remaining_backup or name.begins_with("profile-normal.json.bak-")
	check("retained-backup-releases-owned-name", not remaining_backup)
	var locked := String(spec["sandbox"]).path_join("owned/profile-locked.json")
	catalog.set("_profile_path", locked)
	var failed: Dictionary = catalog.call("_write_profile", next)
	check("native-profile-rename-failure", failed.get("ok") == false and failed.get("errorCode") == "STARTUP_SAVE_PROFILE_WRITE_FAILED", failed)
	check("failed-profile-old-bytes-preserved", FileAccess.get_file_as_string(locked) == "locked-profile")
	var remaining_temp := false
	for name: String in DirAccess.get_files_at(locked.get_base_dir()):
		remaining_temp = remaining_temp or name.begins_with("profile-locked.json.tmp-")
	check("failed-profile-temp-retained", not remaining_temp)
	var blocked := String(spec["sandbox"]).path_join("owned/profile-blocked.json")
	catalog.set("_profile_path", blocked)
	failed = catalog.call("_write_profile", next)
	check("native-profile-publication-collision", failed.get("ok") == false and failed.get("errorCode") == "STARTUP_SAVE_PROFILE_WRITE_FAILED", failed)
	check("publication-collision-directory-unchanged", FileAccess.get_file_as_string(blocked.path_join("proof.bin")) == "publication-blocker")
	remaining_temp = false
	for name: String in DirAccess.get_files_at(blocked.get_base_dir()):
		remaining_temp = remaining_temp or name.begins_with("profile-blocked.json.tmp-")
	check("publication-collision-temp-retained", not remaining_temp)
	catalog.set("_profile_path", String(spec["foreign"]).path_join("new/profile.json"))
	check("foreign-profile-write-denied", catalog.call("_write_profile", next).get("errorCode") == "STARTUP_SAVE_PROFILE_TEST_PATH_INVALID")
	check("foreign-profile-cleanup-denied", catalog.call("_cleanup_profile_file", String(spec["foreign"]).path_join("proof.bin")) == ERR_INVALID_PARAMETER)
func photos() -> void:
	var value := photo("photos")
	check("configure-real-photo-and-lease", value.configure_session("slot", "session").get("ok") == true)
	check("discard-retains-real-session", value.discard_unpublished_session().get("ok") == true)
	check("empty-slot-namespace-released", not DirAccess.dir_exists_absolute(String(spec["sandbox"]).path_join("owned/photos/slot")))
	check("discard-clears-context", value.get("_session_storage_root") == "")
	value = photo("locked-photos")
	check("configure-locked-session", value.configure_session("slot", "session").get("ok") == true)
	var before: String = value.get("_session_storage_root")
	var failed: Dictionary = value.discard_unpublished_session()
	check("native-photo-retention-failure", failed.get("ok") == false and failed.get("errorCode") == "PHOTO_STORAGE_UNAVAILABLE", failed)
	check("failed-session-context-preserved", value.get("_session_storage_root") == before and FileAccess.file_exists(before.path_join("proof.bin")))
	value = photo("other-photos")
	check("other-session-not-moved", value.call("_remove_empty_slot_root", "slot") == false)
	check("other-session-bytes", FileAccess.get_file_as_string(String(spec["sandbox"]).path_join("owned/other-photos/slot/other/proof.bin")) == "other-session")
	value = photo("archive-photos")
	check("valid-blocker-retained-on-configure", value.configure_session("slot", "session").get("ok") == true)
	check("discard-and-rebuild-blocker", value.discard_unpublished_session(true).get("ok") == true)
	var marker := String(spec["sandbox"]).path_join("owned/archive-photos/slot")
	check("blocker-valid-after-discard", FileAccess.file_exists(marker) and value.call("_archive_blocker_is_valid", marker, "slot") == true)
	value = photo("foreign-photos")
	value.set("_storage_root", spec["foreign"])
	check("foreign-config-denied-before-lease", value.configure_session("slot", "session").get("errorCode") == "PHOTO_STORAGE_TEST_PATH_INVALID")
	check("foreign-cleanup-denied", value.call("_remove_tree", String(spec["foreign"])) == ERR_INVALID_PARAMETER)
	check("foreign-directory-creation-denied", value.call("_make_directory", String(spec["foreign"]).path_join("new")) == false)
class MoveWorker extends RefCounted:
	func move_paths(paths: Array) -> Array:
		var results: Array = []
		for path: String in paths:
			results.append({"path": path, "error": int(FS.retain_for_test(path)), "pid": OS.get_process_id(), "threadId": OS.get_thread_caller_id()})
		return results
func threads() -> void:
	var workers: Array = []
	var running: Array = []
	var main_thread := OS.get_thread_caller_id()
	for index in 4:
		var worker := MoveWorker.new()
		var paths: Array = []
		for offset in 8:
			paths.append(String(spec["sandbox"]).path_join("owned/thread-%d-%d" % [index, offset]))
		var thread := Thread.new()
		check("worker-start-%d" % index, thread.start(worker.move_paths.bind(paths)) == OK)
		workers.append(worker)
		running.append(thread)
	for index in running.size():
		var results: Array = running[index].wait_to_finish()
		check("worker-count-%d" % index, results.size() == 8)
		for result: Dictionary in results:
			check("real-thread-move-" + result["path"], result["error"] == OK and result["pid"] == OS.get_process_id() and result["threadId"] != main_thread, result)
'''


@pytest.mark.parametrize("mode", (
    "roots", "photo", "threads", "profile", "profile-failure", "production", "production-failure",
    "production-host-storage", "production-host-flow",
))
def test_real_store_root_guards_and_photo_retention(  # pylint: disable=too-many-locals,too-many-statements
    tmp_path: Path, pytestconfig: pytest.Config, godot_executable: Path,
    request: pytest.FixtureRequest, mode: str,
) -> None:
    """Exercise actual setters/native filesystem; no formal saves or fake cleanup."""
    game = Path(pytestconfig.rootpath).parent
    source_names = ("agent/AgentFileSystem.gd", "agent/lifecycle/AgentSaveStore.gd",
                    "world/presentation/session/TownSessionSaveStore.gd",
                    "world/integration/TownConversationPhotoStore.gd",
                    "world/presentation/session/TownStartupSaveCatalog.gd", "agent/AgentJson.gd")
    model_endpoint = ""
    if mode.startswith("production"):
        model_endpoint = request.getfixturevalue("local_model_protocol")
        source_names += ("agent/AgentSystem.gd", "agent/model/ModelProviderCatalog.gd",
                             "agent/model/GenericOpenAICompatibleModelProvider.gd",
                             "agent/model/OpenAICompatibleModelProvider.gd",
                             "agent/model/ModelProvider.gd",
                         "world/integration/TownAgentProviderService.gd",
                         "world/integration/TownWorldAgentGateway.gd",
                         "world/presentation/session/TownInternalPlaytestCatalog.gd",
                         "world/presentation/session/TownNewGameOpeningCompiler.gd",
                         "world/presentation/session/TownSessionBootstrap.gd",
                         "world/presentation/session/TownSessionUiService.gd",
                         "world/presentation/session/TownSessionAsyncSaveJob.gd",
                         "world/presentation/session/TownSessionSaveCoordinator.gd",
                         "world/presentation/session/TownSessionRuntimeGate.gd",
                         "world/presentation/town_runtime/TownRuntime.gd",
                         "world/presentation/town_runtime/TownRuntime.tscn",
                         "world/runtime/TownWorldRuntime.gd", "world/data/town/town_world.json")
        if mode.startswith("production-host"):
            source_names += ("world/presentation/game_flow/GameFlowHost.gd",
                             "world/integration/TownFormalSlotArchiveService.gd",
                             "world/presentation/ui/TownProviderSettingsService.gd",
                             "world/presentation/ui/TownProviderConfigStore.gd",
                             "world/presentation/ui/TownProviderCredentialStore.gd",
                             "world/presentation/ui/TownProviderStoreFiles.gd",
                             "world/presentation/game_flow/GameFlowFormalOverwriteCompensator.gd")
        if mode == "production-host-flow":
            source_names = tuple(sorted(set(source_names) | {
                p.relative_to(game).as_posix() for directory in ("agent", "common", "world", "ui")
                for p in game.joinpath(directory).rglob("*")
                if p.suffix in (".gd", ".tscn", ".gdshader") and p.is_file()} | {
                    "test_bridge/GameTestBridge.gd", "test_bridge/protocol.schema.json",
                    "test_bridge/phase2_tests/test_bridge_command_api.py"}))
    fingerprints = {n: hashlib.sha256((game / n).read_bytes()).hexdigest() for n in source_names}
    sandbox = tmp_path / f"gametest-retention-{mode}"
    owned = sandbox / "owned"
    owned.mkdir(parents=True)
    (sandbox / "retained").mkdir()
    journal = sandbox / "retention.jsonl"
    journal.touch()
    for name, content in (("photos/slot/session/proof.bin", b"photo-proof"),
                          ("photos/slot/session/.hidden-proof", b"hidden-photo-proof"),
                          ("locked-photos/slot/session/proof.bin", b"locked-photo-proof"),
                          ("other-photos/slot/other/proof.bin", b"other-session")):
        target = owned / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    marker = owned / "archive-photos/slot"
    marker.parent.mkdir()
    marker.write_text(json.dumps({"schema": "ai-town-conversation-photo-archive-block",
                                  "schema_version": 1, "slot_id": "slot"}), encoding="utf-8")
    original_marker = hashlib.sha256(marker.read_bytes()).hexdigest()
    prepare_native_case_files(owned, mode)
    original_files = {p.relative_to(owned).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in owned.rglob("*") if p.is_file()}
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "proof.bin").write_bytes(b"foreign-evidence")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"mode": mode, "sandbox": sandbox.as_posix(),
                               "foreign": foreign.as_posix(), "modelEndpoint": model_endpoint}),
                    encoding="utf-8")
    driver = tmp_path / "driver.gd"
    driver.write_text(_PRODUCTION_DRIVER if mode.startswith("production") else _ROOT_PHOTO_DRIVER,
                      encoding="utf-8")
    extra_args = (prepare_host_flow_driver(tmp_path, _PRODUCTION_DRIVER)
                  if mode == "production-host-flow" else [])
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    locked = {"profile": owned / "profile-locked.json", "profile-failure": journal}.get(
        mode, owned / "locked-photos/slot/session/proof.bin")
    handle = kernel.CreateFileW(str(locked), 0x80000000,
                               1 if mode == "profile-failure" else 3, None, 3, 0x80, None)
    assert handle not in (None, ctypes.c_void_p(-1).value), ctypes.get_last_error()
    started = time.monotonic()
    try:
        result = subprocess.run([str(godot_executable), "--headless", "--path", str(game),
                                 "--log-file", str(tmp_path / "engine.log"),
                                 "--script", str(driver),
                                 "--", str(spec), "--gametest-isolated-test", *extra_args],
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                timeout=180 if mode.startswith("production") else 60, check=False,
                                env={**environ, "AI_TOWN_PROVIDER_TEST_NO_NETWORK": "1"})
    except subprocess.TimeoutExpired as error:
        (tmp_path / "output.log").write_bytes(b"".join(
            v.encode("utf-8") if isinstance(v, str) else v or b""
            for v in (error.stdout, error.stderr)))
        raise
    finally:
        kernel.CloseHandle(handle)
    output = result.stdout + result.stderr
    (tmp_path / "output.log").write_text(output, encoding="utf-8")
    checks = [json.loads(line.removeprefix("ROOT_PHOTO_CHECK "))
              for line in result.stdout.splitlines() if line.startswith("ROOT_PHOTO_CHECK ")]
    evidence = EvidenceStore(tmp_path / "evidence", "godot-root-photo", metadata={
        "caseId": request.node.nodeid, "mode": mode, "sourceFileSha256": fingerprints,
        "driverSha256": hashlib.sha256(driver.read_bytes()).hexdigest(),
        "scope": {"roots": "actual root setters without filesystem mutations",
                  "photo": "native photo retention with private enrolled bindings",
                  "threads": "four actual Godot threads, 32 isolated directory moves",
                  "profile": "actual profile publication and failed rename, retained cleanup",
                  "profile-failure": "native journal lock after actual profile publication",
                  "production": "actual Bootstrap/Provider health/Gateway/Runtime/UI/async save"
                                " and nonempty restore; protocol fixture, no real LLM",
                  "production-failure": "actual worker manifest collision and live-state protection"
                                        "; protocol fixture, no real LLM",
                  "production-host-storage": "actual Host storage guards/composition and Gateway/UI"
                                             " roundtrip; not full HTTP scene transition",
                  "production-host-flow": "real TCP HTTP/WS, departure save, Startup/Town scene"
                                          " transition, real formal health/restore"}[mode],
    })
    request.node.user_properties.extend([
        ("rootPhotoRunId", evidence.run_id),
        ("rootPhotoEvidencePath", evidence.jsonl_path.as_posix()),
        ("rootPhotoOutputPath", (tmp_path / "output.log").as_posix()),
        ("rootPhotoManifestPath", journal.as_posix()),
        ("nativeModelProtocolPath", (tmp_path / "model-protocol.json").as_posix()
         if mode.startswith("production") else ""),
        ("nativeSnapshotRoot", sandbox.as_posix() if mode.startswith("production") else ""),
    ])
    assertion_events = [evidence.record(
        "root_photo_assertion", name=check["id"],
        status="passed" if check["passed"] else "failed", details=check) for check in checks]
    trace_errors = (record_host_flow_trace(evidence, sandbox / "host-http.json",
                                         request.node.nodeid, assertion_events)
                    if mode == "production-host-flow" else [])
    if mode in ("roots", "profile-failure"):
        records = []
        mismatches = [] if journal.read_bytes() == b"" else [{"reason": "Unexpected journal write"}]
    else:
        records, mismatches = _audit_retention_journal(journal)
    available_hashes = {hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in owned.rglob("*") if p.is_file()}
    available_hashes.update(item["sha256"] for r in records if r["status"] == "retained"
                            for item in r["entries"].values() if item["type"] == "file")
    isolated = ((foreign / "proof.bin").read_bytes() == b"foreign-evidence"
                and not (foreign / "new").exists()
                and set(original_files.values()).issubset(available_hashes)
                and all(hashlib.sha256((game / n).read_bytes()).hexdigest() == h
                        for n, h in fingerprints.items()))
    evidence.record("isolation_check", name="source-and-foreign-unchanged",
                    status="passed" if isolated else "failed")
    evidence.record("retention_integrity", name="every-retained-file-matches-manifest",
                    status="failed" if mismatches else "passed", details={"mismatches": mismatches})
    journal_ok = (mode in ("roots", "profile-failure")
        or (mode.startswith("production") and bool(records)) or (
        mode == "photo" and any(r["status"] == "move_failed" for r in records)
        and any(item.get("sha256") == original_marker for r in records
                if r["status"] == "retained" for item in r["entries"].values()))
        or (mode == "threads" and len(records) == 64
            and [r["retentionId"] for r in records[::2]] == list(range(1, 33))
            and all(a["status"] == "prepared" and b["status"] == "retained"
                    and a["retentionId"] == b["retentionId"]
                    and a["source"] == b["source"] and a["entries"] == b["entries"]
                    for a, b in zip(records[::2], records[1::2])))
        or (mode == "profile" and len(records) == 6
            and all(a["status"] == "prepared" and b["status"] == "retained"
                    and a["entries"] == b["entries"] and a["source"] == b["source"]
                    and a["retentionId"] == b["retentionId"]
                    for a, b in zip(records[::2], records[1::2]))
            and records[0]["source"].split("/profile-normal.json.bak-")[0] == owned.as_posix()
            and records[2]["source"].split("/profile-locked.json.tmp-")[0] == owned.as_posix()
            and records[4]["source"].split("/profile-blocked.json.tmp-")[0] == owned.as_posix()))
    evidence.record("journal_contract", name="root-no-write-or-real-move-failure-and-blocker",
                    status="passed" if journal_ok else "failed")
    process_ok = (result.returncode == 0 and "SCRIPT ERROR" not in output
                  and "were leaked" not in output
                  and "AI_TOWN_RENDERER" not in output and bool(checks))
    evidence.record("process_check", name="actual-engine-exit",
                    status="passed" if process_ok else "failed")
    failures = (sum(not c["passed"] for c in checks) + int(not isolated)
                + int(bool(mismatches)) + int(not process_ok) + int(not journal_ok)
                + int(bool(trace_errors)))
    total = len(checks) + 4 + int(mode == "production-host-flow")
    evidence.finish(status="failed" if failures else "passed", total=total,
                    passed=total - failures, failed=failures, skipped=0, other=0,
                    duration_ms=round((time.monotonic() - started) * 1000, 3))
    assert process_ok, output
    assert not failures, checks
