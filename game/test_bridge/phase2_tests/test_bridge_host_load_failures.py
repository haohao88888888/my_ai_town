"""Real Bridge/Host rejection of isolated corrupt and future-version saves."""

from __future__ import annotations

import hashlib
import json
from os import environ  # pylint: disable=no-name-in-module
from pathlib import Path
import subprocess
import time

import pytest

from game.test_bridge.evidence_store import EvidenceStore  # pylint: disable=import-error
from game.test_bridge.phase2_tests.test_bridge_command_api import (  # pylint: disable=import-error
    HOST_FLOW_DRIVER, _record_host_request, prepare_host_flow_driver,
)
from game.test_bridge.phase2_tests.test_bridge_read_api import (  # pylint: disable=import-error
    _PRODUCTION_DRIVER, fixture_local_model_protocol,
)
from game.test_bridge.phase2_tests.test_evidence_import import (  # pylint: disable=import-error
    _audit_retention_journal,
)

pytestmark = pytest.mark.live_bridge
# Register the already reviewed real loopback health fixture in this module.
local_model_protocol = fixture_local_model_protocol

_FAILURE_BODY = r'''
\tcurrent_scene = runtime
\tflow.call("_bind_current_scene")
\tcheck("actual-host-source-bound", flow.get("_town_runtime") == runtime and flow.get("_session_ui_service") != null)
\tvar bridge: Node = flow.get_node_or_null("GameTestBridge")
\tcheck("owned-bridge-listening", is_instance_valid(bridge) and bridge.get("_server").is_listening() and bridge.get("_port") == spec["bridgePort"])
\tif not is_instance_valid(bridge) or not bridge.get("_server").is_listening():
\t\treturn
\tvar healthy := await http("GET", "/health")
\tcheck("actual-host-command-ready", healthy.get("status") == 200 and healthy.get("body", {}).get("data", {}).get("readOnly") == false and healthy.get("body", {}).get("data", {}).get("worldReady") == true, healthy)
\tvar store: RefCounted = flow.get("_startup_save_store")
\tvar published: Dictionary = store.call("list_published", "town-3")
\tcheck("only-one-published-no-valid-fallback", published.get("ok") == true and published.get("manifests", []).size() == 1 and published["manifests"][0].get("save_revision") == 1, published)
\tif published.get("manifests", []).size() != 1:
\t\treturn
\tvar manifest: Dictionary = published["manifests"][0]
\tsnapshot("original-world-manifest.json", manifest)
\tvar original_discovery: Dictionary = flow.call("_discover_startup_slot", "town-3")
\tcheck("real-catalog-healthy-before-injection", original_discovery.get("ok") == true and original_discovery.get("slotState") == "healthy", original_discovery)
\tvar original_path: String
\tif spec["failureMode"] == "world-corrupt":
\t\toriginal_path = store.call("_resolve_reference", manifest["components"]["world"]["snapshot_ref"])
\telse:
\t\tvar agent_store: RefCounted = flow.get("_startup_agent_save_store")
\t\toriginal_path = String(agent_store.call("_snapshot_root", source.call("get_agent_save_context"))).path_join("snapshot.json")
\tcheck("injection-path-is-owned", original_path.begins_with(path("") + "/") and FileAccess.file_exists(original_path), original_path)
\tif not original_path.begins_with(path("") + "/") or not FileAccess.file_exists(original_path):
\t\treturn
\tvar original := FileAccess.get_file_as_bytes(original_path)
\tvar original_copy := FileAccess.open(String(spec["sandbox"]).path_join("sample-original.bin"), FileAccess.WRITE)
\toriginal_copy.store_buffer(original)
\toriginal_copy = null
\tvar damaged := original.duplicate()
\tif spec["failureMode"] == "world-corrupt":
\t\tdamaged.append(32)
\telse:
\t\tvar future: Dictionary = JSON.parse_string(original.get_string_from_utf8())
\t\tfuture["format_version"] = 999
\t\tdamaged = JSON.stringify(future).to_utf8_buffer()
\tvar mutation := FileAccess.open(original_path, FileAccess.WRITE)
\tmutation.store_buffer(damaged)
\tmutation = null
\tvar damaged_copy := FileAccess.open(String(spec["sandbox"]).path_join("sample-injected.bin"), FileAccess.WRITE)
\tdamaged_copy.store_buffer(damaged)
\tdamaged_copy = null
\tsnapshot("injection.json", {"mode": spec["failureMode"], "targetPath": original_path,
\t\t"originalSha256": FileAccess.get_sha256(String(spec["sandbox"]).path_join("sample-original.bin")),
\t\t"injectedSha256": FileAccess.get_sha256(original_path), "saveRevision": 1,
\t\t"expectedError": "SESSION_SAVE_REFERENCE_HASH_MISMATCH" if spec["failureMode"] == "world-corrupt" else "SAVE_VERSION_NEWER_THAN_SUPPORTED"})
\tcheck("sample-actually-changed", original != damaged and FileAccess.get_file_as_bytes(original_path) == damaged)
\tvar before_inventory := {}
\tcheck("native-before-inventory", FS._inventory(path(""), before_inventory) == OK)
\tsnapshot("before-files.json", before_inventory)
\tvar before := projection(runtime.call("get_world_runtime"), source, resident)
\tbefore["activeAgentIds"] = source.call("get_agent_save_participant").call("get_active_resident_ids")
\tbefore["allAgentPayloads"] = capture_agents(source)
\tsnapshot("before-rejection.json", before)
\tvar generation := await http("GET", "/world/state")
\tvar cursor_before := await http("GET", "/events/cursor")
\tvar source_id := runtime.get_instance_id()
\tvar catalog := flow.call("_startup_catalog_snapshot") as Dictionary
\tsnapshot("damaged-catalog.json", catalog)
\tvar damaged_slot := flow.call("_startup_slot_by_id", catalog, "town-3") as Dictionary
\tvar damage_code := "SESSION_SAVE_REFERENCE_HASH_MISMATCH" if spec["failureMode"] == "world-corrupt" else "SAVE_VERSION_NEWER_THAN_SUPPORTED"
\tvar damaged_state := "corrupt" if spec["failureMode"] == "world-corrupt" else "read_only"
\tcheck("real-catalog-classifies-injected-damage", damaged_slot.get("state") == damaged_state and damaged_slot.get("errorCode") == damage_code and damaged_slot.get("continueAvailable") == false, damaged_slot)
\tvar discovery := flow.call("_discover_startup_slot", "town-3") as Dictionary
\tsnapshot("damaged-discovery.json", discovery)
\tcheck("damaged-slot-not-healthy", discovery.get("ok") == false or discovery.get("slotState") != "healthy", discovery)
\tvar payload := command_input(generation["body"]["data"], "host-reject-load")
\tvar rejected := await http("POST", "/commands/load", payload)
\tvar wanted := "SESSION_LOAD_RECOVERY_REQUIRED" if spec["failureMode"] == "world-corrupt" else "SAVE_VERSION_NEWER_THAN_SUPPORTED"
\tcheck("real-http-host-load-rejected", rejected.get("status") == 409 and rejected.get("body", {}).get("ok") == false and rejected.get("body", {}).get("error", {}).get("code") == wanted, {"receipt": rejected, "wanted": wanted})
\tfor _frame in range(12):
\t\tpump_host_events()
\t\tawait process_frame
\tvar after := projection(runtime.call("get_world_runtime"), source, resident)
\tafter["activeAgentIds"] = source.call("get_agent_save_participant").call("get_active_resident_ids")
\tafter["allAgentPayloads"] = capture_agents(source)
\tsnapshot("after-rejection.json", after)
\tcheck("rejected-load-keeps-world-tasks-logs-memory-agents", same(before, after))
\tcheck("rejected-load-keeps-nonempty-projections", same(after["task"], expected["task"]) and same(after["logs"], expected["logs"]) and same(after["memory"], expected["memory"]))
\tcheck("rejected-load-keeps-actual-scene", current_scene == runtime and flow.get("_town_runtime") == runtime and runtime.get_instance_id() == source_id and host_scenes.all(func(scene): return scene["instanceId"] == source_id), host_scenes)
\tcheck("no-load-operation-or-departure-save", flow.get("_gametest_load_operation").is_empty() and flow.get("_in_session_load_pending") == false and not flow.get("_session_ui_service").call("has_active_create_save_async"))
\tvar after_world := await http("GET", "/world/state")
\tvar cursor_after := await http("GET", "/events/cursor")
\tcheck("rejected-load-keeps-bridge-generation-and-state", same_observation(generation["body"]["data"], after_world["body"]["data"]))
\tcheck("rejected-load-emits-no-completion", same_observation(cursor_before["body"]["data"], cursor_after["body"]["data"]), {"before": cursor_before, "after": cursor_after})
\tvar after_inventory := {}
\tcheck("native-after-inventory", FS._inventory(path(""), after_inventory) == OK)
\tsnapshot("after-files.json", after_inventory)
\tcheck("rejection-does-not-rewrite-publish-or-clean-save-files", same(before_inventory, after_inventory))
\tcheck("injected-sample-remains-byte-identical", FileAccess.get_file_as_bytes(original_path) == damaged)
\tvar persisted := store.call("list_published", "town-3") as Dictionary
\tcheck("no-extra-departure-revision", persisted.get("manifests", []).size() == 1 and persisted["manifests"][0].get("save_revision") == 1, persisted)
\tsnapshot("host-http.json", {"requests": host_http, "events": host_events, "scenes": host_scenes,
\t\t"cursorStart": cursor_before, "cursorEnd": cursor_after})
\tcheck("native-source-close", source.call("close_session").get("ok") == true)
\tflow.call("_unmount_town_overlays")
\tflow.call("_release_internal_session_refs")
\tcurrent_scene = null
\truntime.free()
\tfor _frame in range(6):
\t\tawait process_frame
func same_observation(before: Dictionary, after: Dictionary) -> bool:
\t# capturedAt is an acquisition timestamp, not game state; raw responses remain intact.
\tvar first := before.duplicate(true)
\tvar second := after.duplicate(true)
\tfirst.erase("capturedAt")
\tsecond.erase("capturedAt")
\treturn same(first, second)
func capture_agents(source: Node) -> Dictionary:
\tvar captured := source.call("get_agent_save_participant").call("prepare_save_candidate") as Dictionary
\tcheck("actual-all-agent-payloads-captured", captured.get("ok") == true and captured.get("residentPayloads", {}).size() == 15)
\tvar result := {"context": captured.get("context"), "residentIds": captured.get("residentIds"), "payloads": {}}
\tfor id: String in captured.get("residentPayloads", {}):
\t\tvar entry: Dictionary = captured["residentPayloads"][id]
\t\tresult["payloads"][id] = {"residentName": entry["resident_name"], "base64": Marshalls.raw_to_base64(entry["payload"])}
\treturn result
'''


def _failure_driver() -> str:
    """Reuse actual TCP transport and Host binding, not a replacement Host."""
    prefix, callback = HOST_FLOW_DRIVER.split("func bridge_host_roundtrip(", 1)
    signature, setup = callback.split("\n", 1)
    setup = setup.split("\t# Actual runtime scene,", 1)[0]
    return (prefix + "func bridge_host_roundtrip(" + signature + "\n" + setup
            + _FAILURE_BODY.replace("\\t", "\t"))


def _fingerprints(game: Path) -> dict[str, str]:
    """Capture actual code, scenes and schema identities for the native route."""
    names = {path for folder in ("agent", "common", "world", "ui")
             for path in (game / folder).rglob("*")
             if path.is_file() and path.suffix in (".gd", ".tscn", ".gdshader")}
    names.update(game / path for path in (
        "test_bridge/GameTestBridge.gd", "test_bridge/protocol.schema.json",
        "test_bridge/phase2_tests/test_bridge_host_load_failures.py",
        "test_bridge/phase2_tests/test_bridge_read_api.py",
        "test_bridge/phase2_tests/test_bridge_command_api.py"))
    return {path.relative_to(game).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(names)}


def _audit_samples(sandbox: Path) -> dict:
    """Check real bytes and entire native save inventory without changing files."""
    injection = json.loads((sandbox / "injection.json").read_text(encoding="utf-8"))
    original = sandbox / "sample-original.bin"
    injected = sandbox / "sample-injected.bin"
    target = Path(injection["targetPath"])
    assert target.resolve().is_relative_to((sandbox / "owned/host").resolve())
    assert original.read_bytes() != injected.read_bytes()
    if injection["mode"] == "world-corrupt":
        assert injected.read_bytes() == original.read_bytes() + b" "
        assert json.loads(injected.read_bytes()) == json.loads(original.read_bytes())
    else:
        valid = json.loads(original.read_bytes())
        assert valid["format_version"] == 3
        valid["format_version"] = 999
        assert json.loads(injected.read_bytes()) == valid
    assert hashlib.sha256(original.read_bytes()).hexdigest() == injection["originalSha256"]
    assert hashlib.sha256(injected.read_bytes()).hexdigest() == injection["injectedSha256"]
    assert target.read_bytes() == injected.read_bytes()
    before = json.loads((sandbox / "before-files.json").read_text(encoding="utf-8"))
    after = json.loads((sandbox / "after-files.json").read_text(encoding="utf-8"))
    assert before == after
    before_state = json.loads((sandbox / "before-rejection.json").read_text(encoding="utf-8"))
    after_state = json.loads((sandbox / "after-rejection.json").read_text(encoding="utf-8"))
    assert before_state == after_state
    assert len(before_state["allAgentPayloads"]["payloads"]) == 15
    return {"injection": injection, "inventoryEntryCount": len(before),
            "originalBytes": original.stat().st_size, "injectedBytes": injected.stat().st_size}


@pytest.mark.parametrize("failure_mode", ("world-corrupt", "agent-future"))
def test_real_host_rejects_invalid_save(  # pylint: disable=too-many-locals,too-many-statements
    tmp_path: Path, pytestconfig: pytest.Config, godot_executable: Path,
    request: pytest.FixtureRequest, failure_mode: str,
) -> None:
    """Reject at real Host discovery before departure can overwrite the bad sample."""
    game = Path(pytestconfig.rootpath).parent
    fingerprints = _fingerprints(game)
    sandbox = tmp_path / "gametest-retention-host-failure"
    (sandbox / "owned").mkdir(parents=True)
    (sandbox / "retained").mkdir()
    journal = sandbox / "retention.jsonl"
    journal.touch()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "proof.bin").write_bytes(b"foreign-evidence")
    spec = {"mode": "production-host-flow", "failureMode": failure_mode,
            "sandbox": sandbox.as_posix(), "foreign": foreign.as_posix(),
            "modelEndpoint": request.getfixturevalue("local_model_protocol")}
    (tmp_path / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    extra_args = prepare_host_flow_driver(tmp_path, _PRODUCTION_DRIVER)
    driver = tmp_path / "driver.gd"
    driver.write_text(_failure_driver().replace(
        "__BASE_DRIVER__", (tmp_path / "base-driver.gd").as_posix()), encoding="utf-8")
    evidence = EvidenceStore(tmp_path / "evidence", "godot-host-load-failure", metadata={
        "caseId": request.node.nodeid, "failureMode": failure_mode,
        "sourceFileSha256": fingerprints,
        "driverSha256": hashlib.sha256(driver.read_bytes()).hexdigest(),
        "baseDriverSha256": hashlib.sha256((tmp_path / "base-driver.gd").read_bytes()).hexdigest(),
        "scope": "real Host/HTTP rejection, actual saved files and old live world;"
                 " deterministic loopback model health, not real LLM or GUI"})
    request.node.user_properties.extend([
        ("hostFailureRunId", evidence.run_id),
        ("hostFailureEvidencePath", evidence.jsonl_path.as_posix()),
        ("hostFailureSnapshotRoot", sandbox.as_posix()),
        ("hostFailureOutputPath", (tmp_path / "output.log").as_posix()),
        ("nativeModelProtocolPath", (tmp_path / "model-protocol.json").as_posix())])
    started = time.monotonic()
    try:
        result = subprocess.run(
            [str(godot_executable), "--headless", "--path", str(game),
             "--log-file", str(tmp_path / "engine.log"), "--script", str(driver),
             "--", str(tmp_path / "spec.json"), "--gametest-isolated-test", *extra_args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180, check=False,
            env={**environ, "AI_TOWN_PROVIDER_TEST_NO_NETWORK": "1"})
    except subprocess.TimeoutExpired as error:
        (tmp_path / "output.log").write_bytes(b"".join(
            part.encode("utf-8") if isinstance(part, str) else part or b""
            for part in (error.stdout, error.stderr)))
        evidence.record("process_check", status="failed", details={"reason": "native timeout"})
        evidence.finish(status="failed", total=1, passed=0, failed=1, skipped=0, other=0,
                        duration_ms=round((time.monotonic() - started) * 1000, 3))
        raise
    output = result.stdout + result.stderr
    (tmp_path / "output.log").write_text(output, encoding="utf-8")
    checks = [json.loads(line.removeprefix("ROOT_PHOTO_CHECK "))
              for line in result.stdout.splitlines() if line.startswith("ROOT_PHOTO_CHECK ")]
    assertions = [evidence.record("host_failure_assertion", name=check["id"],
                                 status="passed" if check["passed"] else "failed", details=check)
                  for check in checks]
    errors = []
    try:
        trace = json.loads((sandbox / "host-http.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        trace = {"requests": []}
        errors.append(f"native trace unavailable: {type(error).__name__}: {error}")
    requests_by_id = {}
    for index, entry in enumerate(trace["requests"]):
        invalid, command, evidence_id = _record_host_request(
            evidence, entry, request.node.nodeid, index)
        errors.extend(invalid)
        if command:
            requests_by_id.setdefault(command, []).append(evidence_id)
    for assertion in assertions:
        if assertion["name"] == "real-http-host-load-rejected":
            evidence.record("assertion_trace_link", name=assertion["name"], status="captured",
                            command_id="host-reject-load", details={
                                "assertionEvidenceId": assertion["eventId"],
                                "relatedRequestEvidenceIds": requests_by_id.get(
                                    "host-reject-load", []),
                                "relatedGameEventEvidenceIds": [],
                                "gameEventSequence": None,
                                "association": "rejected synchronously; cursor unchanged",
                                "snapshotRoot": sandbox.as_posix()})
    audit_ok = True
    try:
        audit = _audit_samples(sandbox)
    except (OSError, ValueError, KeyError, AssertionError) as error:
        audit_ok = False
        audit = {"errorType": type(error).__name__, "reason": str(error),
                 "snapshotRoot": sandbox.as_posix()}
    records, mismatches = _audit_retention_journal(journal)
    source_ok = (fingerprints == _fingerprints(game)
                 and (foreign / "proof.bin").read_bytes() == b"foreign-evidence")
    process_ok = (result.returncode == 0 and checks and "SCRIPT ERROR" not in output
                  and "were leaked" not in output and "AI_TOWN_RENDERER" not in output)
    evidence.record("retention_integrity", status="failed" if mismatches else "passed",
                    details={"mismatches": mismatches, "journalRecords": len(records)})
    evidence.record("sample_and_live_state_audit", status="passed" if audit_ok else "failed",
                    details=audit)
    evidence.record("source_isolation", status="passed" if source_ok else "failed")
    evidence.record("wire_schema", status="failed" if errors else "passed",
                    details={"errors": errors})
    evidence.record("process_check", status="passed" if process_ok else "failed",
                    details={"returncode": result.returncode})
    failures = (sum(not check["passed"] for check in checks) + int(not process_ok)
                + int(not source_ok) + int(bool(mismatches)) + int(bool(errors))
                + int(not audit_ok))
    total = len(checks) + 5
    evidence.finish(status="failed" if failures else "passed", total=total,
                    passed=total - failures, failed=failures, skipped=0, other=0,
                    duration_ms=round((time.monotonic() - started) * 1000, 3))
    assert process_ok, output
    assert not failures, checks
