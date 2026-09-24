"""Tests for importing external tool reports into shared evidence."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from game.test_bridge.evidence_import import (  # pylint: disable=import-error
    import_ci_summary,
    import_jenkins_summary,
    import_locust_final_json,
    import_locust_report,
    import_newman_report,
)


def _latest_run(root: Path, run_id: str) -> tuple:
    with sqlite3.connect(root / "phase2_evidence.sqlite3") as connection:
        return connection.execute(
            """
            SELECT tool, status, total, passed, failed, skipped, other
            FROM runs WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()


def test_newman_assertions_and_correlations_are_imported(tmp_path: Path) -> None:
    """Newman assertions and Bridge IDs remain traceable in both formats."""
    response = {
        "requestId": "request-7",
        "data": {
            "sessionId": "session-7",
            "worldGeneration": "world-7",
            "stateVersion": 7,
        },
    }
    report = {
        "run": {
            "timings": {"started": 1000, "completed": 1042},
            "executions": [
                {
                    "item": {"name": "World state"},
                    "response": {
                        "code": 200,
                        "responseTime": 8,
                        "stream": {
                            "type": "Buffer",
                            "data": list(json.dumps(response).encode("utf-8")),
                        },
                    },
                    "assertions": [
                        {"assertion": "HTTP 200"},
                        {"assertion": "15 residents"},
                    ],
                }
            ],
        }
    }
    report_path = tmp_path / "newman.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    run_id = import_newman_report(report_path, tmp_path)

    assert _latest_run(tmp_path, run_id) == ("newman", "passed", 2, 2, 0, 0, 0)
    events = [
        json.loads(line)
        for line in (tmp_path / f"{run_id}.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assertion = next(event for event in events if event["eventType"] == "assertion")
    assert assertion["requestId"] == "request-7"
    assert assertion["sessionId"] == "session-7"
    assert assertion["worldGeneration"] == "world-7"
    assert assertion["stateVersion"] == 7


def test_locust_aggregate_and_failures_are_imported(tmp_path: Path) -> None:
    """Locust counts, percentiles, and errors remain queryable."""
    fieldnames = [
        "Type",
        "Name",
        "Request Count",
        "Failure Count",
        "Average Response Time",
        "Max Response Time",
        "Requests/s",
        "50%",
        "95%",
        "99%",
    ]
    stats_path = tmp_path / "locust_stats.csv"
    with stats_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(
            {
                "Type": "GET",
                "Name": "GET /health",
                "Request Count": "9",
                "Failure Count": "1",
                "Average Response Time": "6",
                "Max Response Time": "20",
                "Requests/s": "3.5",
                "50%": "5",
                "95%": "9",
                "99%": "12",
            }
        )
        writer.writerow(
            {
                "Type": "",
                "Name": "Aggregated",
                "Request Count": "9",
                "Failure Count": "1",
                "Average Response Time": "6",
                "Max Response Time": "20",
                "Requests/s": "3.5",
                "50%": "5",
                "95%": "9",
                "99%": "12",
            }
        )
    failures_path = tmp_path / "locust_failures.csv"
    with failures_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Method", "Name", "Error", "Occurrences"])
        writer.writeheader()
        writer.writerow(
            {
                "Method": "GET",
                "Name": "GET /health",
                "Error": "expected 200",
                "Occurrences": "1",
            }
        )

    run_id = import_locust_report(stats_path, failures_path, evidence_root=tmp_path)

    assert _latest_run(tmp_path, run_id) == ("locust", "failed", 9, 8, 1, 0, 0)
    with sqlite3.connect(tmp_path / "phase2_evidence.sqlite3") as connection:
        p95 = connection.execute(
            """
            SELECT json_extract(details_json, '$.p95Ms')
            FROM evidence_events
            WHERE run_id = ? AND event_type = 'load_metric' AND name = 'Aggregated'
            """,
            (run_id,),
        ).fetchone()[0]
        error_count = connection.execute(
            """
            SELECT count(*) FROM evidence_events
            WHERE run_id = ? AND event_type = 'load_error'
            """,
            (run_id,),
        ).fetchone()[0]
    assert p95 == 9.0
    assert error_count == 1


def test_locust_final_snapshot_is_preferred_over_sampled_csv(tmp_path: Path) -> None:
    """The quitting-event snapshot must preserve the exact terminal counters."""
    report = {
        "users": 5,
        "scenario": "mixed-command",
        "durationMs": 10000.0,
        "stats": [
            {
                "method": None,
                "name": "Aggregated",
                "num_requests": 341,
                "num_failures": 0,
                "avg_response_time": 6.0,
                "total_rps": 35.98,
                "response_time_percentile_0.5": 6,
                "response_time_percentile_0.95": 10,
                "response_time_percentile_0.99": 10,
                "max_response_time": 19,
            }
        ],
        "failures": [],
        "exceptions": [],
        "businessOutcomes": {
            "command_completed": 18,
            "expected_state_version_conflict": 3,
            "idempotent_replay_verified": 18,
        },
    }
    report_path = tmp_path / "locust-final.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    run_id = import_locust_final_json(report_path, tmp_path)

    assert _latest_run(tmp_path, run_id) == ("locust", "passed", 341, 341, 0, 0, 0)
    with sqlite3.connect(tmp_path / "phase2_evidence.sqlite3") as connection:
        metadata = connection.execute(
            """
            SELECT json_extract(metadata_json, '$.users'),
                   json_extract(metadata_json, '$.scenario')
            FROM runs WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        outcomes = dict(
            connection.execute(
                """
                SELECT name, json_extract(details_json, '$.count')
                FROM evidence_events
                WHERE run_id = ? AND event_type = 'business_outcome'
                """,
                (run_id,),
            ).fetchall()
        )
    assert metadata == (5, "mixed-command")
    assert outcomes == report["businessOutcomes"]


def test_jenkins_stages_and_source_fingerprint_are_imported(tmp_path: Path) -> None:
    """CI stage outcomes remain tied to the exact tested source fingerprint."""

    report = {
        "runId": "jenkins-build-7",
        "buildNumber": "7",
        "executionMode": "jenkins",
        "status": "failed",
        "durationMs": 1250.5,
        "sourceFingerprint": {
            "head": "abc123",
            "trackedDiffSha256": "tracked-hash",
            "untrackedSha256": "untracked-hash",
        },
        "toolVersions": {"python": "3.11.16", "jenkins": "2.568.3"},
        "disk": {"artifactsBytes": 1024},
        "stages": [
            {
                "name": "preflight",
                "status": "passed",
                "durationMs": 250.0,
                "exitCode": 0,
                "reportPaths": [],
            },
            {
                "name": "pytest",
                "status": "failed",
                "durationMs": 1000.5,
                "exitCode": 1,
                "errorType": "StageFailure",
                "message": "pytest exited with code 1",
                "reportPaths": ["pytest-junit.xml"],
            },
        ],
    }
    report_path = tmp_path / "jenkins-summary.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    run_id = import_jenkins_summary(report_path, tmp_path)

    assert _latest_run(tmp_path, run_id) == ("jenkins", "failed", 2, 1, 1, 0, 0)
    with sqlite3.connect(tmp_path / "phase2_evidence.sqlite3") as connection:
        fingerprint = connection.execute(
            """
            SELECT json_extract(metadata_json, '$.sourceFingerprint.trackedDiffSha256')
            FROM runs WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()[0]
        failed_stage = connection.execute(
            """
            SELECT name, json_extract(details_json, '$.exitCode')
            FROM evidence_events
            WHERE run_id = ? AND event_type = 'ci_stage' AND status = 'failed'
            """,
            (run_id,),
        ).fetchone()
    assert fingerprint == "tracked-hash"
    assert failed_stage == ("pytest", 1)

    report["runId"] = "actions-run-8"
    report["executionMode"] = "github-actions"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    actions_run_id = import_ci_summary(report_path, tmp_path)
    assert _latest_run(tmp_path, actions_run_id) == ("ci", "failed", 2, 1, 1, 0, 0)
