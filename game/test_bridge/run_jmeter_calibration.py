"""Updated 2026-10-21: isolated readonly calibration, never formal acceptance.

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
import shutil
import socket
import subprocess
import time
from pathlib import Path
import xml.etree.ElementTree as ET

import requests
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

BRIDGE = Path(__file__).resolve().parent
GAME = BRIDGE.parent
REPO = GAME.parent
# ThreadGroup lifetime starts before the first HTTP sample (including JSR223 JIT).
# Keep the measurement anchored to the first sample; insufficient coverage still fails.
SCHEDULER_STARTUP_ALLOWANCE_SECONDS = 15
TAIL_GUARD_SECONDS = 2
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
            "runnerSha256": sha(Path(__file__)), "jmxSha256": sha(BRIDGE / "jmeter-readonly.jmx")}


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


def request_json(port: int, path: str, validator) -> dict:
    """Bounded, non-retrying read of the owned loopback bridge."""
    with requests.Session() as session:
        session.trust_env = False  # Do not inherit proxies or ambient .netrc credentials.
        response = session.get(
            f"http://127.0.0.1:{port}{path}", timeout=5, allow_redirects=False)
        if response.status_code != 200:
            raise ValueError(f"Owned readonly preflight returned HTTP {response.status_code}")
        body = response.json()
    validator.validate(body)
    return body


def percentile(values: list[int], ratio: float):
    """Nearest-rank quantile; short probes are not statistical guarantees."""
    return sorted(values)[max(0, math.ceil(len(values) * ratio) - 1)] if values else None


def audit_jtl(  # pylint: disable=too-many-locals,too-many-branches
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
    thread_indexes = Counter()
    actual_mix = Counter(s.get("lb") for s in chosen)
    allowed_ids = {r["residentId"] for r in baseline["residents"]}
    for index, sample in enumerate(samples):
        issues = set()
        try:
            if sample.get("s") != "true" or sample.get("rc") != "200":
                issues.add(f"http-or-live-assertion:{sample.get('rc')}")
            thread = sample.get("tn")
            thread_number = int(thread.rsplit("-", 1)[1]) - 1
            count = thread_indexes[thread]
            expected_label = "GET resident" if (count + thread_number) % 5 == 4 else "GET world"
            thread_indexes[thread] += 1
            if sample.get("lb") != expected_label or sample.findtext("method") != "GET":
                issues.add("load-sequence-or-method-mismatch")
            body = json.loads(sample.findtext("responseData"))
            validator.validate(body)
            data = body["data"]
            if (not body["ok"] or data["sessionId"] != baseline["sessionId"]
                    or data["worldGeneration"] != baseline["worldGeneration"]):
                raise ValueError("identity mismatch")
            url = sample.findtext("java.net.URL") or ""
            if url.endswith("/world/state"):
                if (data["residentCount"] != 15 or data["simulationSpeed"] != 1
                        or {r["residentId"] for r in data["residents"]} != allowed_ids):
                    raise ValueError("world changed population or speed")
            elif "/residents/" in url:
                if data["resident"]["residentId"] != url.rsplit("/", 1)[1]:
                    raise ValueError("resident request/response mismatch")
            else:
                raise ValueError("unexpected sampler path")
        except (ValueError, TypeError, KeyError, ValidationError) as error:
            # Calibration keeps every original failure; no sample is silently exempted.
            issues.add(f"offline-contract:{type(error).__name__}")
        if issues:
            failed_indexes.append(index)
            all_errors.update(issues)
            if begin <= int(sample.get("ts")) < end:
                measured_failed_indexes.append(index)
                errors.update(issues)
    elapsed = [int(s.get("t")) for s in chosen]
    return {"scope": "calibration, NOT formal acceptance",
            "sampleTimestampConvention": "JMeter start timestamp",
            "windowStartEpochMs": begin, "windowEndEpochMs": end, "windowSeconds": measure,
            "allSamples": len(samples), "measuredSamples": len(chosen),
            "actualMix": dict(actual_mix),
            "observedThreadCount": len(thread_indexes),
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
    """Own one bounded calibration and retain every intermediate artifact."""
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
    for name, source in (
        ("runner-source.py", Path(__file__)),
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
    fresh_json(root / "spec.json", {"root": root.as_posix(), "port": port,
                                   "maxSeconds": args.warmup + args.measure + 92})
    fresh_json(root / "environment.json", {
        "platform": platform.platform(), "machine": platform.machine(), **hardware(),
        "processor": platform.processor(), "godot": str(godot), "java": str(java),
        "jmeterJar": str(jar), "godotSha256": sha(godot), "javaSha256": sha(java),
        "jmeterJarSha256": sha(jar), "clients": args.clients, "warmupSeconds": args.warmup,
        "measureSeconds": args.measure, "driverSha256": sha(root / "driver.gd"),
        "schedulerStartupAllowanceSeconds": SCHEDULER_STARTUP_ALLOWANCE_SECONDS,
        "tailGuardSeconds": TAIL_GUARD_SECONDS,
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
            health = request_json(port, "/health", validator)
            if health["data"] != {"bridgeReady": True, "worldReady": True, "readOnly": True}:
                raise RuntimeError("Read-only readiness mismatch")
            before = request_json(port, "/world/state", validator)
            baseline = before["data"]
            if baseline["residentCount"] != 15 or baseline["simulationSpeed"] != 1:
                raise RuntimeError("Not the planned 15-resident/1x fixture")
            fresh_json(root / "baseline.json", before)
            resident_ids = sorted(r["residentId"] for r in baseline["residents"])
            for resident in resident_ids:
                request_json(port, "/residents/" + resident, validator)
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
                deadline = time.monotonic() + args.warmup + args.measure + 60
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
            report["jmeterExitCode"] = client.returncode
            report["loadShape"] = args.load_shape
            report["sourceUnchanged"] = source_identity() == identity
            report["actualJvmIdentity"] = json.loads(
                (root / "jvm-identity.json").read_text(encoding="utf-8"))
            fresh_json(root / "calibration-summary.json", report)
            if not report["sourceUnchanged"]:
                raise RuntimeError("Source changed during calibration")
            if (report["allFailedSamples"] or not report["windowCovered"]
                    or set(report["actualMix"]) != {"GET world", "GET resident"}
                    or report["observedThreadCount"] != args.clients):
                raise RuntimeError("Calibration failures or an incomplete observation window")
    except BaseException as error:
        fresh_json(root / "failure.json", {"type": type(error).__name__, "message": str(error)})
        raise
    finally:
        exits = cleanup_owned(root, engine, client, probes)
    output = (root / "engine-output.log").read_text(encoding="utf-8", errors="replace")
    if engine.returncode != 0 or any(marker in output for marker in (
            "SCRIPT ERROR", "AI_TOWN_RENDERER", "were leaked")):
        raise RuntimeError("Engine shutdown/isolation verification failed; retain all output")
    if (exits["actualJvmExitCode"] != 0 or exits["cleanupErrors"]
            or (root / "forced-jvm-stop.json").exists()):
        raise RuntimeError("Actual JVM did not exit cleanly; not accepted as calibration success")
    fresh_json(root / "final-validation.json", {
        "engineExitCode": engine.returncode, "sourceUnchanged": report["sourceUnchanged"],
        "scope": "calibration only", "rawJtlSha256": report["jtlSha256"]})
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True)
    parser.add_argument("--godot", required=True)
    parser.add_argument("--java", required=True)
    parser.add_argument("--jmeter-jar", required=True)
    parser.add_argument("--clients", type=int, choices=(1, 5, 8, 10, 20), default=1)
    parser.add_argument("--load-shape", choices=("burst", "phased"), default="burst",
                        help="phased is a separate diagnostic; never replaces the original burst")
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--measure", type=int, default=40)
    options = parser.parse_args()
    if not 1 <= options.warmup <= 300 or not 5 <= options.measure <= 600:
        parser.error("Calibration needs warmup 1..300 and measurement 5..600 seconds")
    run(options)
