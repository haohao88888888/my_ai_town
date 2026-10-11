"""Read-only HTTP integration tests against an isolated 15-resident world."""

from __future__ import annotations

import json
import ctypes
import hashlib
from os import environ  # pylint: disable=no-name-in-module
import subprocess
import time
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


pytestmark = pytest.mark.live_bridge
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "protocol.schema.json"
ResponseRecorder = Callable[[str, requests.Response, int], None]


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


@pytest.mark.parametrize("mode", ("roots", "photo", "threads", "profile", "profile-failure"))
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
    if mode.startswith("profile"):
        (owned / "profile-normal.json").write_bytes(b"original-profile")
        (owned / "profile-locked.json").write_bytes(b"locked-profile")
        (owned / "profile-blocked.json").mkdir()
        (owned / "profile-blocked.json/proof.bin").write_bytes(b"publication-blocker")
    if mode == "threads":
        for index in range(4):
            for offset in range(8):
                directory = owned / f"thread-{index}-{offset}"
                directory.mkdir()
                (directory / "proof.bin").write_bytes(f"thread-{index}-{offset}".encode())
    original_files = {p.relative_to(owned).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in owned.rglob("*") if p.is_file()}
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "proof.bin").write_bytes(b"foreign-evidence")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"mode": mode, "sandbox": sandbox.as_posix(),
                               "foreign": foreign.as_posix()}), encoding="utf-8")
    driver = tmp_path / "driver.gd"
    driver.write_text(_ROOT_PHOTO_DRIVER, encoding="utf-8")
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
                                 "--", str(spec), "--gametest-isolated-test"],
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=60, check=False,
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
                  "profile-failure": "native journal lock after actual profile publication"}[mode],
    })
    request.node.user_properties.extend([
        ("rootPhotoRunId", evidence.run_id),
        ("rootPhotoEvidencePath", evidence.jsonl_path.as_posix()),
        ("rootPhotoOutputPath", (tmp_path / "output.log").as_posix()),
        ("rootPhotoManifestPath", journal.as_posix()),
    ])
    for check in checks:
        evidence.record("root_photo_assertion", name=check["id"],
                        status="passed" if check["passed"] else "failed", details=check)
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
    journal_ok = (mode in ("roots", "profile-failure") or (
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
                  and "AI_TOWN_RENDERER" not in output and bool(checks))
    evidence.record("process_check", name="actual-engine-exit",
                    status="passed" if process_ok else "failed")
    failures = (sum(not c["passed"] for c in checks) + int(not isolated)
                + int(bool(mismatches)) + int(not process_ok) + int(not journal_ok))
    evidence.finish(status="failed" if failures else "passed", total=len(checks) + 4,
                    passed=len(checks) + 4 - failures, failed=failures, skipped=0, other=0,
                    duration_ms=round((time.monotonic() - started) * 1000, 3))
    assert process_ok, output
    assert not failures, checks
