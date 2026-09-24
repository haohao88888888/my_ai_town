"""Shared pytest fixtures for the isolated GameTestBridge process."""

from __future__ import annotations

import collections
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
import requests

from game.test_bridge.evidence_store import EvidenceStore  # pylint: disable=import-error


DEFAULT_BRIDGE_URL = "http://127.0.0.1:18865"
DEFAULT_GODOT_BIN = Path(r"D:\Godot\Godot_v4.7.2-stable_win64_console.exe")
DEFAULT_EVIDENCE_ROOT = Path(__file__).resolve().parents[1] / "artifacts"
_EVIDENCE_CONTEXT: dict[str, Any] = {
    "store": None,
    "started_at": 0.0,
    "cases": {},
}


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register bridge location and isolated-process command-line options."""
    group = parser.getgroup("gametest-bridge")
    group.addoption(
        "--bridge-url",
        dest="bridge_url",
        default=DEFAULT_BRIDGE_URL,
        help="Base URL for the isolated GameTestBridge fixture.",
    )
    group.addoption(
        "--external-bridge",
        dest="external_bridge",
        action="store_true",
        default=False,
        help="Use an already-running bridge instead of starting the isolated Godot fixture.",
    )
    group.addoption(
        "--godot-bin",
        dest="godot_bin",
        default="",
        help="Path to the Godot console executable used to start the isolated fixture.",
    )
    group.addoption(
        "--evidence-root",
        dest="evidence_root",
        default=str(DEFAULT_EVIDENCE_ROOT),
        help="Directory for append-only Phase 2 JSONL and SQLite evidence.",
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    """Open one evidence run before test collection and execution."""
    cases: dict[str, dict[str, Any]] = _EVIDENCE_CONTEXT["cases"]
    cases.clear()
    _EVIDENCE_CONTEXT["started_at"] = time.monotonic()
    _EVIDENCE_CONTEXT["store"] = EvidenceStore(
        Path(str(session.config.getoption("evidence_root"))),
        "pytest",
        metadata={
            "rootdir": str(session.config.rootpath),
            "bridgeUrl": str(session.config.getoption("bridge_url")),
            "externalBridge": bool(session.config.getoption("external_bridge")),
        },
    )


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Accumulate one final result per collected case without double counting phases."""
    cases: dict[str, dict[str, Any]] = _EVIDENCE_CONTEXT["cases"]
    current = cases.setdefault(
        report.nodeid,
        {"status": "other", "duration_ms": 0.0, "error_type": None, "message": None},
    )
    current["duration_ms"] = float(current["duration_ms"]) + report.duration * 1000.0

    if report.failed:
        current["status"] = "failed"
        current["error_type"] = f"pytest_{report.when}_failure"
        current["message"] = report.longreprtext
    elif report.skipped and current["status"] != "failed":
        current["status"] = "skipped"
    elif report.when == "call" and current["status"] not in {"failed", "skipped"}:
        current["status"] = "passed"


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Persist case outcomes and the run summary even when tests fail."""
    store: EvidenceStore | None = _EVIDENCE_CONTEXT["store"]
    if store is None:
        return

    cases: dict[str, dict[str, Any]] = _EVIDENCE_CONTEXT["cases"]
    for nodeid, result in sorted(cases.items()):
        store.record(
            "test_case",
            name=nodeid,
            status=str(result["status"]),
            duration_ms=round(float(result["duration_ms"]), 3),
            error_type=result["error_type"],
            message=result["message"],
            details={"framework": "pytest"},
        )

    counts = {
        status: sum(1 for result in cases.values() if result["status"] == status)
        for status in ("passed", "failed", "skipped", "other")
    }
    total = session.testscollected
    counts["other"] += max(0, total - sum(counts.values()))
    store.finish(
        status="passed" if exitstatus == pytest.ExitCode.OK else "failed",
        total=total,
        passed=counts["passed"],
        failed=counts["failed"],
        skipped=counts["skipped"],
        other=counts["other"],
        duration_ms=round(
            (time.monotonic() - float(_EVIDENCE_CONTEXT["started_at"])) * 1000.0,
            3,
        ),
    )
    _EVIDENCE_CONTEXT["store"] = None


def _resolve_godot_bin(config: pytest.Config) -> Path:
    configured = str(config.getoption("godot_bin")).strip()
    if configured:
        candidate = Path(configured)
    elif DEFAULT_GODOT_BIN.is_file():
        candidate = DEFAULT_GODOT_BIN
    else:
        discovered = shutil.which("godot") or shutil.which("godot4")
        if not discovered:
            pytest.fail(
                "Godot executable not found. Pass --godot-bin or use --external-bridge.",
                pytrace=False,
            )
        candidate = Path(discovered)

    if not candidate.is_file():
        pytest.fail(f"Godot executable does not exist: {candidate}", pytrace=False)
    return candidate


def _capture_output(process: subprocess.Popen[str], lines: collections.deque[str]) -> None:
    if process.stdout is None:
        return
    for line in process.stdout:
        lines.append(line.rstrip())


def _wait_until_ready(
    process: subprocess.Popen[str] | None,
    base_url: str,
    lines: collections.deque[str],
    timeout_seconds: float = 45.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error = "bridge did not answer"
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            log_tail = "\n".join(lines)
            pytest.fail(
                f"Isolated Godot fixture exited with code {process.returncode}.\n{log_tail}",
                pytrace=False,
            )
        try:
            response = requests.get(f"{base_url}/world/state", timeout=1.0)
            if response.status_code == 200:
                return
            last_error = f"HTTP {response.status_code}: {response.text[:200]}"
        except requests.RequestException as exc:
            last_error = str(exc)
        time.sleep(0.2)

    log_tail = "\n".join(lines)
    pytest.fail(
        f"GameTestBridge was not ready within {timeout_seconds:.0f}s: {last_error}\n{log_tail}",
        pytrace=False,
    )


@contextmanager
def _isolated_bridge(
    pytestconfig: pytest.Config,
    *,
    command_mode: bool,
) -> Iterator[str]:
    """Run one module-scoped isolated bridge in read-only or command mode."""
    base_url = str(pytestconfig.getoption("bridge_url")).rstrip("/")
    if pytestconfig.getoption("external_bridge"):
        _wait_until_ready(None, base_url, collections.deque(maxlen=1))
        yield base_url
        return

    godot_bin = _resolve_godot_bin(pytestconfig)
    game_dir = Path(__file__).resolve().parents[2]
    command = [
        str(godot_bin),
        "--headless",
        "--path",
        str(game_dir),
        "--script",
        "res://tests/game_test_bridge_test.gd",
        "--",
        "--gametest-isolated-test",
        "--bridge-smoke-server",
        "--bridge-smoke-long",
    ]
    if command_mode:
        command.append("--bridge-smoke-commands")
    # Popen intentionally spans the fixture yield and is closed in finally.
    process = subprocess.Popen(  # pylint: disable=consider-using-with
        command,
        cwd=game_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lines: collections.deque[str] = collections.deque(maxlen=200)
    reader = threading.Thread(target=_capture_output, args=(process, lines), daemon=True)
    reader.start()

    try:
        _wait_until_ready(process, base_url, lines)
        yield base_url
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@pytest.fixture(scope="module")
def bridge_base_url(pytestconfig: pytest.Config) -> Iterator[str]:
    """Start the read-only isolated bridge for one test module."""
    with _isolated_bridge(pytestconfig, command_mode=False) as base_url:
        yield base_url


@pytest.fixture(scope="module")
def command_bridge_base_url(pytestconfig: pytest.Config) -> Iterator[str]:
    """Start an isolated bridge with explicitly enabled write commands."""
    with _isolated_bridge(pytestconfig, command_mode=True) as base_url:
        yield base_url


@pytest.fixture(name="record_bridge_response")
def fixture_record_bridge_response() -> Callable[[str, requests.Response, int], None]:
    """Persist Bridge correlation fields beside the pytest case result."""

    def record(name: str, response: requests.Response, expected_status: int) -> None:
        store: EvidenceStore | None = _EVIDENCE_CONTEXT["store"]
        if store is None:
            return
        try:
            body = response.json()
        except ValueError:
            body = {}
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        store.record(
            "bridge_response",
            name=name,
            status="passed" if response.status_code == expected_status else "failed",
            error_type=error.get("code"),
            message=error.get("message"),
            request_id=body.get("requestId"),
            command_id=data.get("commandId"),
            session_id=data.get("sessionId"),
            world_generation=data.get("worldGeneration"),
            state_version=data.get("stateVersion"),
            details={
                "expectedHttpStatus": expected_status,
                "actualHttpStatus": response.status_code,
            },
        )

    return record


@pytest.fixture(scope="session")
def http_session() -> requests.Session:
    """Provide one reusable HTTP session for the read-only contract tests."""
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})
    try:
        yield session
    finally:
        session.close()
