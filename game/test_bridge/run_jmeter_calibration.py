"""Updated 2026-10-22: isolated readonly calibration and frozen formal measurements.

No environment mutation, recursive deletion, store/Provider access or retries.
Raw XML JTL retains responses for offline validation using the original Schema.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from collections import Counter
import hashlib
import json
import math
import platform
import runpy
import shutil
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import requests
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

BRIDGE = Path(__file__).resolve().parent
GAME = BRIDGE.parent
REPO = GAME.parent
REPORTER = BRIDGE / "jmeter_report.py"
# ThreadGroup lifetime starts before the first HTTP sample (including JSR223 JIT).
# Keep the measurement anchored to the first sample; insufficient coverage still fails.
SCHEDULER_STARTUP_ALLOWANCE_SECONDS = 15
TAIL_GUARD_SECONDS = 2
PREFLIGHT_BUDGET_SECONDS = 30
# User confirmed these BEFORE formal measurement; never tune after seeing results.
FORMAL_CRITERIA = {
    "approvedOn": "2026-10-21", "scope": "local project targets, NOT production SLA",
    "baselineClients": [1, 5], "boundaryClients": [8, 10, 20],
    "baselineAllFailuresMaximum": 0, "successfulP95MsMaximum": 50,
    "successfulP99MsMaximum": 100, "successfulRpsTargetFractionMinimum": .95,
    "warmupSeconds": 180, "measurementSeconds": 300, "repetitionsPerPoint": 3,
    "stabilityClients": 5, "stabilityMeasurementSeconds": 1800,
    "loadShape": "burst", "sourceMustBeClean": True,
    "boundaryFailuresRemainFailedRequests": True,
    "recognizedBoundaryTransports": ["ConnectException/Connection refused",
                                    "SocketException/Connection reset"],
}
DRIVER = r'''extends "res://tests/support/TownWorldTestCase.gd"
const BRIDGE_SCRIPT := preload("res://test_bridge/GameTestBridge.gd")
var world: RefCounted
var bridge: Node
var spec: Dictionary
var previous_usec := 0
var frames := 0
var window_usec := 0
var max_delta_usec := 0
var next_log_usec := 0
var stopping := false
func _initialize() -> void:
	call_deferred("run")
func context() -> Dictionary:
	return {"world": world, "runtime": null, "sessionId": "jmeter-readonly-fixture", "flowHost": null}
func write_json(name: String, value: Dictionary) -> void:
	var final_path := String(spec["root"]).path_join(name)
	var file := FileAccess.open(final_path + ".pending", FileAccess.WRITE)
	if file == null:
		printerr("JMETER_ARTIFACT_WRITE_FAILED " + name)
		quit(2)
		return
	file.store_string(JSON.stringify(value))
	file = null
	if DirAccess.rename_absolute(final_path + ".pending", final_path) != OK:
		printerr("JMETER_ARTIFACT_PUBLISH_FAILED " + name)
		quit(2)
func frame() -> void:
	if stopping:
		return
	var now := Time.get_ticks_usec()
	var delta_usec := now - previous_usec
	previous_usec = now
	world.advance(float(delta_usec) / 1000000.0)
	frames += 1
	max_delta_usec = maxi(max_delta_usec, delta_usec)
	if now >= next_log_usec:
		var file := FileAccess.open(String(spec["root"]).path_join("world-telemetry.jsonl"), FileAccess.READ_WRITE)
		file.seek_end()
		file.store_line(JSON.stringify({"ticksUsec": now, "wallUnixSeconds": Time.get_unix_time_from_system(),
			"frames": frames, "windowUsec": now - window_usec, "maxDeltaUsec": max_delta_usec,
			"time": world.get_time(), "stateVersion": world.get_world_revision(),
			"simulationSpeed": world.get_simulation_speed(), "pid": OS.get_process_id()}))
		frames = 0
		window_usec = now
		max_delta_usec = 0
		next_log_usec = now + 1000000
	if FileAccess.file_exists(String(spec["root"]).path_join("stop-request.json")):
		finish()
func finish() -> void:
	if stopping:
		return
	stopping = true
	process_frame.disconnect(frame)
	write_json("final-world.json", {"time": world.get_time(), "stateVersion": world.get_world_revision(), "pid": OS.get_process_id()})
	bridge.stop()
	bridge.free()
	world.stop()
	world = null
	await _finish_suite("JMETER_READONLY_STOPPED")
func run() -> void:
	spec = JSON.parse_string(FileAccess.get_file_as_string(OS.get_cmdline_user_args()[0]))
	var data := _build_data()
	var opening := _load_opening(data)
	world = WORLD.new()
	var started: Dictionary = world.start(data, opening)
	_expect_equal(started.get("ok"), true, "real World start")
	_expect_equal(world.get_simulation_speed(), 1, "real 1x clock")
	bridge = BRIDGE_SCRIPT.new()
	root.add_child(bridge)
	var bound: Dictionary = bridge.start(context, int(spec["port"]), false)
	_expect_equal(bound.get("ok"), true, "own loopback read-only listener")
	if not _failures.is_empty():
		bridge.free()
		await _finish_suite("JMETER_READONLY_START")
		return
	write_json("ready.json", {"pid": OS.get_process_id(), "port": spec["port"], "time": world.get_time()})
	var telemetry := FileAccess.open(String(spec["root"]).path_join("world-telemetry.jsonl"), FileAccess.WRITE)
	telemetry = null
	previous_usec = Time.get_ticks_usec()
	window_usec = previous_usec
	next_log_usec = previous_usec + 1000000
	process_frame.connect(frame)
	await create_timer(float(spec["maxSeconds"]), true, false, true).timeout
	await finish()
'''


def fresh_json(path: Path, value: object) -> None:
    """Only new isolated artifacts; never silently overwrite evidence."""
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def sha(path: Path) -> str:
    """Hash original bytes without editing them."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_identity() -> dict:
    """Fingerprint actual source, including uncommitted pressure assets."""
    files = subprocess.check_output(["git", "ls-files", "game"], cwd=REPO, text=True).splitlines()
    names = [n for n in files if Path(n).suffix in {".gd", ".tscn", ".gdshader", ".json"}
             and "/fixtures/" not in n]
    names += ["game/tests/fixtures/town_world_opening.json"]
    return {"head": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
            "status": subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=REPO, text=True),
            "hashes": {n: sha(REPO / n) for n in names},
            "runnerSha256": sha(Path(__file__)), "jmxSha256": sha(BRIDGE / "jmeter-readonly.jmx"),
            "reporterSha256": sha(REPORTER)}


class MemoryCounters(ctypes.Structure):  # pylint: disable=too-few-public-methods
    """Native Windows PROCESS_MEMORY_COUNTERS_EX."""
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
        (n, ctypes.c_size_t) for n in (
        "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
        "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
        "PagefileUsage", "PeakPagefileUsage", "PrivateUsage")]


class ProcessProbe:
    """Read counters for the exact owned executable PID, not a console launcher."""
    def __init__(self, pid: int):
        self.pid = pid
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.GetProcessTimes.argtypes = (
            [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4)
        self.kernel.K32GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
        self.kernel.GetProcessHandleCount.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.OpenProcess(0x0400 | 0x0010, False, pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def sample(self) -> dict:
        """Return cumulative CPU time and native memory/handle counters."""
        counters = MemoryCounters(ctypes.sizeof(MemoryCounters))
        times = [wintypes.FILETIME() for _ in range(4)]
        handles = wintypes.DWORD()
        if not self.kernel.GetProcessTimes(self.handle, *(ctypes.byref(t) for t in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not self.kernel.K32GetProcessMemoryInfo(
                self.handle, ctypes.byref(counters), ctypes.sizeof(counters)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not self.kernel.GetProcessHandleCount(self.handle, ctypes.byref(handles)):
            raise ctypes.WinError(ctypes.get_last_error())
        seconds = sum((t.dwHighDateTime << 32) + t.dwLowDateTime for t in times[2:]) / 10_000_000
        return {"pid": self.pid, "cpuSeconds": seconds, "workingSetBytes": counters.WorkingSetSize,
                "privateBytes": counters.PrivateUsage, "handles": handles.value}

    def close(self) -> None:
        """Release only the observer's handle, not the target process."""
        self.kernel.CloseHandle(self.handle)

    def exit_code(self) -> int:
        """Query the still-owned handle; 259 means the process is active."""
        value = wintypes.DWORD()
        if not self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        return value.value

    def stop_owned(self) -> None:
        """Terminate only the JVM proven by its fresh self-reported identity."""
        if self.exit_code() != 259:
            return
        handle = self.kernel.OpenProcess(0x0001, False, self.pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self.kernel.TerminateProcess(handle, 2):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.kernel.CloseHandle(handle)


def hardware() -> dict:
    """Read machine capacity without WMI, environment edits or new tools."""
    installed_kib = ctypes.c_ulonglong()
    success = ctypes.windll.kernel32.GetPhysicallyInstalledSystemMemory(
        ctypes.byref(installed_kib))
    return {"logicalCpuCount": int(ctypes.windll.kernel32.GetActiveProcessorCount(0xFFFF)),
            "installedRamBytes": installed_kib.value * 1024 if success else None}


def request_json(port: int, path: str, validator, deadline: float | None = None) -> dict:
    """Bounded, non-retrying read of the owned loopback bridge."""
    remaining = deadline - time.monotonic() if deadline is not None else 10
    if remaining <= 0:
        raise TimeoutError("Owned readonly preflight total deadline exceeded")
    with requests.Session() as session:
        session.trust_env = False  # Do not inherit proxies or ambient .netrc credentials.
        response = session.get(
            f"http://127.0.0.1:{port}{path}", timeout=min(5, remaining / 2),
            allow_redirects=False)
        if response.status_code != 200:
            raise ValueError(f"Owned readonly preflight returned HTTP {response.status_code}")
        body = response.json()
    validator.validate(body)
    if deadline is not None and time.monotonic() > deadline:
        raise TimeoutError("Owned readonly preflight completed after its total deadline")
    return body


def percentile(values: list[int], ratio: float):
    """Nearest-rank quantile; short probes are not statistical guarantees."""
    return sorted(values)[max(0, math.ceil(len(values) * ratio) - 1)] if values else None


def transport_assertion_valid(sample, code: str) -> bool:
    """Only the archived JMX's HTTP-status assertion may explain a transport failure."""
    expected = ("java.lang.AssertionError: unexpected HTTP status: " + code
                + ". Expression: (prev.getResponseCode() == 200)")
    assertions = sample.findall("assertionResult")
    return len(assertions) == 1 and all(
        a.findtext("name") == "Live identity and readonly contract"
        and a.findtext("failure") == "true" and a.findtext("error") == "false"
        and a.findtext("failureMessage") == expected for a in assertions)


def inspect_sample(sample, context):  # pylint: disable=too-many-locals,too-many-branches
    """Validate request routing even when no HTTP response exists; never fake Schema."""
    issues, fatal = set(), set()
    thread = sample.get("tn") or ""
    ordinal = context["threads"][thread]
    context["threads"][thread] += 1
    url = None
    try:
        number = int(thread.rsplit("-", 1)[1]) - 1
        if number < 0:
            raise ValueError("negative thread index")
        resident = (ordinal + number) % 5 == 4
        expected = ("/residents/" + context["ids"][(ordinal // 5 + number) % len(context["ids"])]
                    if resident else "/world/state")
        url = urlsplit(sample.findtext("java.net.URL") or "")
        routing_checks = (sample.tag == "httpSample", sample.findtext("method") == "GET",
                          sample.get("lb") == ("GET resident" if resident else "GET world"),
                          url.scheme == "http", url.hostname == "127.0.0.1",
                          url.port == context["port"], url.username is None,
                          url.path == expected, not url.query, not url.fragment)
        if not all(routing_checks):
            fatal.add("load-sequence-method-or-url-mismatch")
    except (ValueError, TypeError, IndexError) as error:
        fatal.add(f"load-routing:{type(error).__name__}")
    code, message = sample.get("rc") or "", sample.get("rm") or ""
    raw = sample.findtext("responseData") or ""
    no_http = code.startswith("Non HTTP response code:")
    recognized_transport = (
        (code == "Non HTTP response code: org.apache.http.conn.HttpHostConnectException"
         and "Connection refused" in message and "java.net.ConnectException" in raw)
        or (code == "Non HTTP response code: java.net.SocketException"
            and "Connection reset" in message and "java.net.SocketException" in raw))
    if no_http:
        # No body can be Schema-validated. This is still a FAILED request.
        issues.add(f"transport:{code}")
        if not recognized_transport or sample.get("s") != "false":
            fatal.add("unclassified-or-inconsistent-transport-failure")
        # A known network refusal cannot hide an absent/broken Groovy assertion.
        if not transport_assertion_valid(sample, code):
            fatal.add("transport-live-assertion-missing-or-unexpected")
        return issues | fatal, fatal, recognized_transport, True
    if code != "200" or sample.get("s") != "true":
        fatal.add(f"http-or-live-assertion:{code}")
    assertions = sample.findall("assertionResult")
    if (len(assertions) != 1 or any(a.findtext("failure") != "false"
                                 or a.findtext("error") != "false" for a in assertions)):
        fatal.add("live-assertion-missing-or-failed")
    try:
        body = json.loads(raw)
        context["validator"].validate(body)
        data = body["data"]
        identity_checks = (body["ok"], body["error"] is None,
                           data["sessionId"] == context["baseline"]["sessionId"],
                           data["worldGeneration"] == context["baseline"]["worldGeneration"],
                           data["stateVersion"] >= context["versions"].get(thread, 0),
                           body["requestId"] not in context["requestIds"])
        if not all(identity_checks):
            raise ValueError("identity/version/request ID mismatch")
        context["versions"][thread] = data["stateVersion"]
        context["requestIds"].add(body["requestId"])
        if sample.get("lb") == "GET world":
            if (data["residentCount"] != 15 or data["simulationSpeed"] != 1
                    or sorted(r["residentId"] for r in data["residents"]) != context["ids"]):
                raise ValueError("world population or speed mismatch")
        elif url is None or data["resident"]["residentId"] != url.path.rsplit("/", 1)[1]:
            raise ValueError("resident request/response mismatch")
    except (ValueError, TypeError, KeyError, ValidationError) as error:
        fatal.add(f"offline-contract:{type(error).__name__}")
    return issues | fatal, fatal, False, False


def audit_jtl(  # pylint: disable=too-many-locals
    path: Path, baseline: dict, warmup: int, measure: int, validator,
) -> dict:
    """Offline full schema/identity validation, fixed window anchored to first sample."""
    xml = ET.parse(path).getroot()
    samples = list(xml)
    if not samples:
        raise ValueError("No JMeter samples")
    first = min(int(s.get("ts")) for s in samples)
    begin, end = first + warmup * 1000, first + (warmup + measure) * 1000
    chosen = [s for s in samples if begin <= int(s.get("ts")) < end]
    errors = Counter()
    all_errors = Counter()
    failed_indexes = []
    measured_failed_indexes = []
    context = {"baseline": baseline, "validator": validator, "threads": Counter(),
               "ids": sorted(r["residentId"] for r in baseline["residents"]),
               "versions": {}, "requestIds": set(),
               "port": json.loads((path.parent / "spec.json").read_text(encoding="utf-8"))["port"]}
    contract_invalid, transport_indexes, schema_unavailable = [], [], []
    actual_mix = Counter(s.get("lb") for s in chosen)
    for index, sample in enumerate(samples):
        issues, fatal, transport, no_http = inspect_sample(sample, context)
        if fatal:
            contract_invalid.append(index)
        if transport:
            transport_indexes.append(index)
        if no_http:
            schema_unavailable.append(index)
        if issues:
            failed_indexes.append(index)
            all_errors.update(issues)
            if begin <= int(sample.get("ts")) < end:
                measured_failed_indexes.append(index)
                errors.update(issues)
    elapsed = [int(s.get("t")) for s in chosen]
    failed_set = set(failed_indexes)
    successful = [int(s.get("t")) for i, s in enumerate(samples)
                  if i not in failed_set and begin <= int(s.get("ts")) < end]
    return {"scope": "audited samples only; no acceptance implied",
            "sampleTimestampConvention": "JMeter start timestamp",
            "windowStartEpochMs": begin, "windowEndEpochMs": end, "windowSeconds": measure,
            "allSamples": len(samples), "measuredSamples": len(chosen),
            "actualMix": dict(actual_mix),
            "observedThreadCount": len(context["threads"]),
            "observedThreadNames": sorted(context["threads"]),
            "allContractInvalidSampleIndexes": contract_invalid,
            "recognizedTransportFailureSampleIndexes": transport_indexes,
            "schemaUnverifiableSampleIndexes": schema_unavailable,
            "successfulMeasuredSamples": len(successful),
            "successfulRps": len(successful) / measure,
            "successfulResponseLatency": {"population": "fully verified measurement responses only",
                "p50Ms": percentile(successful, .5), "p95Ms": percentile(successful, .95),
                "p99Ms": percentile(successful, .99), "maxMs": max(successful, default=None)},
            "attemptLatencyPopulation": "ALL measurement attempts, including failures",
            "worldFraction": actual_mix["GET world"] / len(chosen) if chosen else None,
            "measuredFailedSamples": len(measured_failed_indexes),
            "measuredErrorRate": len(measured_failed_indexes) / len(chosen) if chosen else None,
            "allFailedSamples": len(failed_indexes), "allErrors": dict(all_errors),
            "failedJtlSampleIndexes": failed_indexes,
            "measuredFailedJtlSampleIndexes": measured_failed_indexes,
            "windowCovered": max(int(s.get("ts")) for s in samples) >= end,
            "actualRps": len(chosen) / measure, "errors": dict(errors),
            "p50Ms": percentile(elapsed, .5), "p95Ms": percentile(elapsed, .95),
            "p99Ms": percentile(elapsed, .99),
            "maxMs": max(elapsed, default=None), "jtlSha256": sha(path)}


def assess_measurement(report: dict, clients: int) -> dict:
    """Pure policy: valid boundary observation is not a successful load target."""
    latency = report["successfulResponseLatency"]
    expected_threads = sorted(f"Readonly clients 1-{n}" for n in range(1, clients + 1))
    mix = report["actualMix"]
    valid = all((report["windowCovered"], report["sourceUnchanged"],
                 not report["allContractInvalidSampleIndexes"],
                 report["allFailedSamples"] == len(
                     report["recognizedTransportFailureSampleIndexes"]),
                 report["observedThreadNames"] == expected_threads,
                 set(mix) == {"GET world", "GET resident"},
                 abs(mix.get("GET world", 0) - .8 * report["measuredSamples"]) <= clients))
    baseline = clients in FORMAL_CRITERIA["baselineClients"]
    target = all((valid, report["allFailedSamples"] == 0,
                  latency["p95Ms"] is not None
                  and latency["p95Ms"] <= FORMAL_CRITERIA["successfulP95MsMaximum"],
                  latency["p99Ms"] is not None
                  and latency["p99Ms"] <= FORMAL_CRITERIA["successfulP99MsMaximum"],
                  report["successfulRps"] >= clients
                  * FORMAL_CRITERIA["successfulRpsTargetFractionMinimum"])) if baseline else None
    return {"protocolAndWindowValid": valid, "baselineTargetApplicable": baseline,
            "loadTargetPassed": target if baseline else (
                False if report["allFailedSamples"] else None),
            "zeroFailureObserved": report["allFailedSamples"] == 0,
            "status": ("baseline-target-met" if target else "baseline-target-failed") if baseline
                else "boundary-observed-with-failed-requests" if report["allFailedSamples"]
                else "boundary-observed-without-failed-requests",
            "scope": "preparation only unless the complete formal/stability run validates"}


def verify_stability_prerequisites(args, identity: dict) -> None:
    """Require three distinct, completed five-client formal baseline roots on this SHA."""
    roots = [Path(value).resolve(strict=True) for value in args.baseline_runs]
    if len(roots) != 3 or len(set(roots)) != 3:
        raise ValueError("Stability requires three distinct completed five-client formal runs")
    digests = set()
    for root in roots:
        if not root.is_relative_to((BRIDGE / "artifacts").resolve()):
            raise ValueError("Baseline proof must be an approved isolated artifact")
        before = json.loads((root / "source-before.json").read_text(encoding="utf-8"))
        final = json.loads((root / "final-validation.json").read_text(encoding="utf-8"))
        env = json.loads((root / "environment.json").read_text(encoding="utf-8"))
        if not (before == identity and final["mode"] == "formal"
                and env["clients"] == 5 and env["warmupSeconds"] == 180
                and env["measureSeconds"] == 300 and all(final["gates"].values())
                and final["rawJtlSha256"] == sha(root / "samples.jtl")):
            raise ValueError("Stability baseline identity/completion/threshold proof mismatch")
        digests.add(final["rawJtlSha256"])
    if len(digests) != 3:
        raise ValueError("Reused baseline artifact bytes are not three independent runs")


def probe_recovery(  # pylint: disable=too-many-locals
    root: Path, port: int, baseline: dict, validator, engine_probe,
) -> dict:
    """Ten real low-rate requests to the same World after JVM exit; never retry."""
    residents = sorted(r["residentId"] for r in baseline["residents"])
    started = time.monotonic()
    deadline = started + 20
    records = []
    request_ids = set()
    version = baseline["stateVersion"]
    with requests.Session() as session, (root / "recovery-requests.jsonl").open(
            "x", encoding="utf-8") as output:
        session.trust_env = False
        for index in range(10):
            time.sleep(max(0, started + index - time.monotonic()))
            path = "/residents/" + residents[index // 5] if index % 5 == 4 else "/world/state"
            entry = {"probeId": f"recovery-{index + 1:02d}", "method": "GET", "path": path,
                     "sentEpochSeconds": time.time(), "gameEventSequence": None,
                     "gameEventSequenceReason": "HTTP readonly probe; no WS subscription"}
            request_started = time.monotonic()
            try:
                if request_started >= deadline:
                    raise RuntimeError("Bounded recovery deadline reached")
                response = session.get(f"http://127.0.0.1:{port}{path}",
                                       timeout=min(3, deadline - request_started),
                                       allow_redirects=False)
                entry.update(status=response.status_code, responseHeaders=dict(response.headers),
                             rawResponse=response.text, requestUrl=response.request.url)
                response.raise_for_status()
                body = response.json()
                validator.validate(body)
                data = body["data"]
                identity_checks = (response.status_code == 200, body["ok"],
                                   data["sessionId"] == baseline["sessionId"],
                                   data["worldGeneration"] == baseline["worldGeneration"],
                                   data["stateVersion"] >= version,
                                   body["requestId"] not in request_ids)
                if not all(identity_checks):
                    raise ValueError("Recovery status/identity/version mismatch")
                version = data["stateVersion"]
                request_ids.add(body["requestId"])
                if path == "/world/state":
                    if (data["residentCount"] != 15 or data["simulationSpeed"] != 1
                            or sorted(r["residentId"] for r in data["residents"]) != residents):
                        raise ValueError("Recovery World population/speed mismatch")
                elif data["resident"]["residentId"] != path.rsplit("/", 1)[1]:
                    raise ValueError("Recovery resident request mismatch")
                entry.update(requestId=body["requestId"], worldGeneration=data["worldGeneration"],
                             stateVersion=data["stateVersion"], actualGodot=engine_probe.sample(),
                             passed=True)
            except Exception as error:  # pylint: disable=broad-except
                # Preserve failed transport/Schema/IO evidence before propagating.
                entry.update(passed=False, errorType=type(error).__name__, errorMessage=str(error))
                raise
            finally:
                entry.update(receivedEpochSeconds=time.time(),
                             elapsedMs=(time.monotonic() - request_started) * 1000)
                output.write(json.dumps(entry, ensure_ascii=False) + "\n")
                output.flush()
                records.append(entry)
    result = {"scope": "post-load recovery only, NOT acceptance of preceding load",
              "attempts": len(records), "failures": sum(not r["passed"] for r in records),
              "targetRequestsPerSecond": 1, "elapsedSeconds": time.monotonic() - started,
              "worldGeneration": baseline["worldGeneration"], "finalStateVersion": version,
              "requestsSha256": sha(root / "recovery-requests.jsonl")}
    fresh_json(root / "recovery-summary.json", result)
    return result


def cleanup_owned(  # pylint: disable=too-many-branches
    root: Path, engine, client, probes: list,
) -> dict:
    """Attempt every independent owned-resource cleanup even if another fails."""
    failures = []

    def attempt(name, action):
        try:
            return action()
        except Exception as error:  # pylint: disable=broad-except
            # Cleanup faults must not bypass the other resource cleanups.
            failures.append({"resource": name, "type": type(error).__name__,
                             "message": str(error)})
            return None

    def stop_jvm():
        deadline = time.monotonic() + 10
        while probes[1].exit_code() == 259 and time.monotonic() < deadline:
            time.sleep(.05)
        if probes[1].exit_code() == 259:
            probes[1].stop_owned()
            fresh_json(root / "forced-jvm-stop.json", {"pid": probes[1].pid})

    def terminate(process):
        process.terminate()
        process.wait(timeout=10)

    try:
        if len(probes) > 1:
            attempt("jvm", stop_jvm)
        if client is not None and client.poll() is None:
            attempt("owned-java-executable", lambda: terminate(client))
        if engine is not None and engine.poll() is None:
            attempt("engine-stop-signal", lambda: fresh_json(root / "stop-request.json", {
                "reason": "owned calibration ended", "epochSeconds": time.time()}))
            try:
                engine.wait(timeout=10)
            except subprocess.TimeoutExpired:
                attempt("owned-engine", lambda: terminate(engine))
                attempt("forced-stop-record", lambda: fresh_json(
                    root / "forced-engine-stop.json", {"pid": engine.pid}))
            except OSError as error:
                failures.append({"resource": "engine-wait", "message": str(error)})
                attempt("owned-engine", lambda: terminate(engine))
        result = {
            "actualGodotPid": engine.pid if engine else None,
            "actualGodotExitCode": engine.poll() if engine else None,
            "ownedJavaPid": client.pid if client else None,
            "ownedJavaExitCode": client.poll() if client else None,
            "actualJvmPid": probes[1].pid if len(probes) > 1 else None,
            "actualJvmExitCode": attempt("jvm-exit-query", probes[1].exit_code)
                if len(probes) > 1 else None,
        }
        attempt("process-exits", lambda: fresh_json(root / "process-exits.json", result))
    finally:
        for probe in probes:
            attempt("observer-handle", probe.close)
        fresh_json(root / "cleanup-result.json", {"errors": failures})
    result["cleanupErrors"] = failures
    return result


def run(args) -> None:  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    """Own one bounded isolated run and retain every intermediate artifact."""
    if platform.system() != "Windows":
        raise ValueError("This resource probe is Windows-only")
    candidate = Path(args.artifacts).absolute()
    for parent in [candidate, *candidate.parents]:
        if parent.exists() and (parent.is_symlink() or parent.lstat().st_file_attributes & 0x400):
            raise ValueError("Linked artifact roots are not permitted")
    root = candidate.resolve()
    allowed = (BRIDGE / "artifacts").resolve()
    if not root.is_relative_to(allowed) or root == allowed:
        raise ValueError("Use a fresh descendant of the approved artifact root")
    root.mkdir(exist_ok=False)
    godot, java, jar = [Path(p).resolve(strict=True) for p in (
        args.godot, args.java, args.jmeter_jar)]
    if godot.name.endswith("_console.exe"):
        raise ValueError("Use the existing actual Godot executable, not its launcher")
    if not (java.parent / "server/jvm.dll").is_file():
        raise ValueError("Use the installed JVM bin/java.exe, not a Conda launcher")
    identity = source_identity()
    fresh_json(root / "source-before.json", identity)
    if args.mode != "calibration":
        if identity["status"] or identity["head"] != args.frozen_sha:
            raise ValueError("Formal/stability measurement requires the exact clean frozen SHA")
        fresh_json(root / "fixed-criteria.json", FORMAL_CRITERIA)
        if args.mode == "stability":
            verify_stability_prerequisites(args, identity)
    for name, source in (
        ("runner-source.py", Path(__file__)),
        ("reporter-source.py", REPORTER),
        ("plan-source.jmx", BRIDGE / "jmeter-readonly.jmx"),
        ("response-schema.json", BRIDGE / "protocol.schema.json"),
    ):
        with (root / name).open("xb") as copied:
            copied.write(source.read_bytes())
    with (root / "tracked-source.patch").open("xb") as patch:
        patch.write(subprocess.check_output(["git", "diff", "HEAD", "--binary"], cwd=REPO))
    (root / "driver.gd").write_text(DRIVER, encoding="utf-8")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    # Reserved budgets: readiness40 + whole preflight30 + JVM identity40 + scheduler17 +
    # request tail5 + post-state5 + offline audit20 + recovery20 + source-check10.
    # Normal shutdown has its separate 10-second graceful budget.
    total_budget = args.warmup + args.measure + 157 + PREFLIGHT_BUDGET_SECONDS
    fresh_json(root / "spec.json", {"root": root.as_posix(), "port": port,
                                   "maxSeconds": total_budget})
    fresh_json(root / "environment.json", {
        "platform": platform.platform(), "machine": platform.machine(), **hardware(),
        "processor": platform.processor(), "godot": str(godot), "java": str(java),
        "jmeterJar": str(jar), "godotSha256": sha(godot), "javaSha256": sha(java),
        "jmeterJarSha256": sha(jar), "clients": args.clients, "warmupSeconds": args.warmup,
        "measureSeconds": args.measure, "driverSha256": sha(root / "driver.gd"),
        "schedulerStartupAllowanceSeconds": SCHEDULER_STARTUP_ALLOWANCE_SECONDS,
        "tailGuardSeconds": TAIL_GUARD_SECONDS,
        "preflightTotalBudgetSeconds": PREFLIGHT_BUDGET_SECONDS,
        "mode": args.mode, "engineLifetimeMaximumSeconds": total_budget,
        "jvmLifetimeMaximumSeconds": args.warmup + args.measure + 62,
        "loadShape": args.load_shape,
        "phaseOffsetRule": "first request: floor(threadIndex * 1000 / clients) ms"
            if args.load_shape == "phased" else "none; original per-thread pacing",
        "loadModel": "closed-loop; target 1 req/s/client; actual RPS reported",
        "blindSpots": ["No production renderer or model decisions",
                       "No network bandwidth, GC or per-connection access log",
                       "No full machine resource exclusion"]})
    schema = json.loads((root / "response-schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    engine = client = None
    probes = []
    try:
        with (root / "engine-output.log").open("x", encoding="utf-8") as output:
            command = [str(godot), "--headless", "--path", str(GAME), "--log-file",
                       str(root / "engine.log"), "--script", str(root / "driver.gd"),
                       "--", str(root / "spec.json"), "--gametest-isolated-test"]
            # Popen spans sampling; finally below owns its shutdown.
            engine = subprocess.Popen(  # pylint: disable=consider-using-with
                command, cwd=GAME, stdout=output, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW)
            deadline = time.monotonic() + 40
            while not (root / "ready.json").exists():
                if engine.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Owned engine did not become ready; see retained logs")
                time.sleep(.1)
            ready = json.loads((root / "ready.json").read_text(encoding="utf-8"))
            if ready["pid"] != engine.pid or ready["port"] != port:
                raise RuntimeError("Actual engine/listener ownership mismatch")
            preflight_deadline = time.monotonic() + PREFLIGHT_BUDGET_SECONDS
            health = request_json(port, "/health", validator, preflight_deadline)
            if health["data"] != {"bridgeReady": True, "worldReady": True, "readOnly": True}:
                raise RuntimeError("Read-only readiness mismatch")
            before = request_json(port, "/world/state", validator, preflight_deadline)
            baseline = before["data"]
            if baseline["residentCount"] != 15 or baseline["simulationSpeed"] != 1:
                raise RuntimeError("Not the planned 15-resident/1x fixture")
            fresh_json(root / "baseline.json", before)
            resident_ids = sorted(r["residentId"] for r in baseline["residents"])
            for resident in resident_ids:
                request_json(port, "/residents/" + resident, validator, preflight_deadline)
            scheduled_seconds = (args.warmup + args.measure + SCHEDULER_STARTUP_ALLOWANCE_SECONDS
                                 + TAIL_GUARD_SECONDS)
            args_jmeter = [
                str(java), "-Xms256m", "-Xmx512m", "-jar", str(jar), "-n", "-t",
                str(BRIDGE / "jmeter-readonly.jmx"), "-l", str(root / "samples.jtl"),
                "-j", str(root / "jmeter.log"), f"-Jport={port}", f"-Jclients={args.clients}",
                "-Jramp=1", f"-Jduration={scheduled_seconds}",
                f"-JloadShape={args.load_shape}",
                "-JownedJvmIdentityPath=" + str(root / "jvm-identity.json"),
                "-JresidentIds=" + ",".join(resident_ids), "-JsessionId=" + baseline["sessionId"],
                "-JworldGeneration=" + baseline["worldGeneration"], "-Jhttpclient4.retrycount=0",
                "-Jjmeter.save.saveservice.output_format=xml",
                "-Jjmeter.save.saveservice.response_data=true",
                "-Jjmeter.save.saveservice.samplerData=true",
                "-Jjmeter.save.saveservice.assertion_results=all",
                "-Jjmeter.save.saveservice.url=true",
                "-Jjmeter.save.saveservice.print_field_names=true",
                "-Jsampleresult.timestamp.start=true"]
            fresh_json(root / "commands.json", {"engine": command, "jmeter": args_jmeter})
            with (root / "jmeter-output.log").open("x", encoding="utf-8") as joutput, (
                root / "resources.jsonl").open("x", encoding="utf-8") as resources:
                client = subprocess.Popen(  # pylint: disable=consider-using-with
                    args_jmeter, cwd=root, stdout=joutput, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                probes.append(ProcessProbe(engine.pid))
                probes.append(ProcessProbe(client.pid))
                jvm_verified = False
                identity_deadline = time.monotonic() + 40
                deadline = time.monotonic() + args.warmup + args.measure + 62
                while client.poll() is None:
                    if engine.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("Engine exit or bounded JMeter deadline exceeded")
                    identity_path = root / "jvm-identity.json"
                    if not jvm_verified and identity_path.exists():
                        jvm = json.loads(identity_path.read_text(encoding="utf-8"))
                        if int(jvm["pid"]) != client.pid:
                            raise RuntimeError("JVM self-report differs from owned native Java PID")
                        jvm_verified = True
                    if not jvm_verified and time.monotonic() > identity_deadline:
                        raise RuntimeError("Bounded JVM identity discovery failed")
                    samples = {name: probe.sample() for name, probe in zip(
                        ("actualGodot", "actualJmeterJvm"), probes)}
                    samples.update(epochSeconds=time.time(),
                                   freeDiskBytes=shutil.disk_usage(root).free)
                    resources.write(json.dumps(samples) + "\n")
                    resources.flush()
                    if samples["freeDiskBytes"] < 5 * 1024 ** 3:
                        raise RuntimeError("Safety stop: free disk below 5 GiB")
                    time.sleep(1)
                if client.returncode != 0:
                    raise RuntimeError(f"JMeter exited {client.returncode}; keep all evidence")
                if not jvm_verified:
                    raise RuntimeError("JVM identity absent; no successful calibration claim")
            after = request_json(port, "/world/state", validator)
            fresh_json(root / "after-world.json", after)
            if (after["data"]["worldGeneration"] != baseline["worldGeneration"]
                    or after["data"]["residentCount"] != 15):
                raise RuntimeError("Post-run identity/population pollution")
            if after["data"]["time"] == baseline["time"]:
                raise RuntimeError("World clock did not advance")
            report = audit_jtl(root / "samples.jtl", baseline, args.warmup, args.measure, validator)
            report["scope"] = "calibration, NOT formal acceptance" if args.mode == "calibration" \
                else args.mode + " readonly local measurement; target decision is separate"
            report["jmeterExitCode"] = client.returncode
            report["loadShape"] = args.load_shape
            report["sourceUnchanged"] = source_identity() == identity
            report["actualJvmIdentity"] = json.loads(
                (root / "jvm-identity.json").read_text(encoding="utf-8"))
            # Preserve the load report even if the independent recovery probe fails.
            summary_name = "calibration-summary.json" if args.mode == "calibration" \
                else "measurement-summary.json"
            fresh_json(root / summary_name, report)
            recovery = probe_recovery(root, port, baseline, validator, probes[0])
            after_recovery = source_identity()
            fresh_json(root / "source-after-recovery.json", after_recovery)
            if after_recovery != identity:
                raise RuntimeError("Source changed during post-load recovery")
            if not report["sourceUnchanged"]:
                raise RuntimeError("Source changed during calibration")
            decision = assess_measurement(report, args.clients)
            fresh_json(root / "target-decision.json", decision)
            if args.mode == "calibration" and (report["allFailedSamples"]
                                                or not decision["protocolAndWindowValid"]):
                raise RuntimeError("Calibration failures or an incomplete observation window")
    except BaseException as error:
        fresh_json(root / "failure.json", {"type": type(error).__name__, "message": str(error)})
        raise
    finally:
        exits = cleanup_owned(root, engine, client, probes)
        # Always archive shutdown checks, including on rejected calibration or IO errors.
        output_path = root / "engine-output.log"
        output = output_path.read_text(encoding="utf-8", errors="replace") \
            if output_path.exists() else ""
        after_shutdown = source_identity()
        fresh_json(root / "source-after-shutdown.json", after_shutdown)
        shutdown = {"actualEngineExitedZero": engine is not None and engine.returncode == 0,
                    "actualJvmExitedZero": exits["actualJvmExitCode"] == 0,
                    "noCleanupErrors": not exits["cleanupErrors"],
                    "noForcedStops": not any((root / name).exists() for name in (
                        "forced-jvm-stop.json", "forced-engine-stop.json")),
                    "noScriptRendererOrLeakMarkers": not any(marker in output for marker in (
                        "SCRIPT ERROR", "AI_TOWN_RENDERER", "were leaked")),
                    "sourceUnchanged": after_shutdown == identity}
        fresh_json(root / "shutdown-validation.json", shutdown)
    gates = {"measurementValid": decision["protocolAndWindowValid"] and all(shutdown.values()),
             "recoveryPassed": recovery["attempts"] == 10 and recovery["failures"] == 0,
             "loadTargetPassed": decision["loadTargetPassed"]}
    dashboard = None
    if args.html_report:
        try:
            # Pylint 2.14 cannot inspect Python 3.11's frozen runpy module.
            reporter = runpy.run_path(str(REPORTER))  # pylint: disable=no-member
            dashboard = reporter["build_report"](root, report, java, jar)
        except BaseException as error:
            fresh_json(root / "report-failure.json", {
                "type": type(error).__name__, "message": str(error)})
            raise
    gates["reportVerified"] = not args.html_report or dashboard is not None
    after_report = source_identity()
    fresh_json(root / "source-after-report.json", after_report)
    gates["measurementValid"] = gates["measurementValid"] and after_report == identity
    fresh_json(root / "final-validation.json", {
        "engineExitCode": engine.returncode, "sourceUnchanged": report["sourceUnchanged"],
        "scope": "calibration only" if args.mode == "calibration" else decision["status"],
        "mode": args.mode, "gates": gates, "rawJtlSha256": report["jtlSha256"],
        "reportRequested": args.html_report, "dashboard": dashboard,
        "recoveryRequestsSha256": recovery["requestsSha256"]})
    if (not gates["measurementValid"] or not gates["recoveryPassed"]
            or (decision["baselineTargetApplicable"] and args.mode != "calibration"
                and not gates["loadTargetPassed"])):
        fresh_json(root / "failure.json", {
            "type": "FormalGateFailure", "gates": gates, "mode": args.mode})
        raise RuntimeError("Completed measurement/target gate failed; all evidence retained")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True)
    parser.add_argument("--godot", required=True)
    parser.add_argument("--java", required=True)
    parser.add_argument("--jmeter-jar", required=True)
    parser.add_argument("--clients", type=int, choices=(1, 5, 8, 10, 20), default=1)
    parser.add_argument("--mode", choices=("calibration", "formal", "stability"),
                        default="calibration")
    parser.add_argument("--frozen-sha")
    parser.add_argument("--html-report", action="store_true",
                        help="optional for calibration; mandatory for formal/stability")
    parser.add_argument("--baseline-runs", nargs=3, default=[])
    parser.add_argument("--load-shape", choices=("burst", "phased"), default="burst",
                        help="phased is a separate diagnostic; never replaces the original burst")
    parser.add_argument("--warmup", type=int)
    parser.add_argument("--measure", type=int)
    options = parser.parse_args()
    required_durations = (180, 1800 if options.mode == "stability" else 300)
    if options.mode == "calibration":
        options.warmup = 20 if options.warmup is None else options.warmup
        options.measure = 40 if options.measure is None else options.measure
    else:
        options.html_report = True
        if (options.load_shape != "burst" or not options.frozen_sha
                or options.warmup not in (None, required_durations[0])
                or options.measure not in (None, required_durations[1])):
            parser.error("Formal/stability needs frozen SHA, burst and fixed 180/300 or 180/1800")
        options.warmup, options.measure = required_durations
        if options.mode == "stability" and (options.clients != 5 or not options.baseline_runs):
            parser.error("Stability needs five clients and three completed formal baseline roots")
    if options.mode == "calibration" and (
            not 1 <= options.warmup <= 300 or not 5 <= options.measure <= 600):
        parser.error("Calibration needs warmup 1..300 and measurement 5..600 seconds")
    run(options)
