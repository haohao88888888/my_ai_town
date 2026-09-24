"""Read-only HTTP integration tests against an isolated 15-resident world."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests
from jsonschema import Draft202012Validator, FormatChecker


pytestmark = pytest.mark.live_bridge
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "protocol.schema.json"


@pytest.fixture(scope="module", name="response_validator")
def fixture_response_validator() -> Draft202012Validator:
    """Create one validator for every top-level Bridge response."""
    with SCHEMA_PATH.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _request_json(
    session: requests.Session,
    base_url: str,
    path: str,
    expected_status: int,
) -> dict:
    response = session.get(f"{base_url}{path}", timeout=3.0)
    assert response.status_code == expected_status, response.text
    assert response.headers["Content-Type"].startswith("application/json")
    return response.json()


def test_health_and_world_match_protocol_schema(
    http_session: requests.Session,
    bridge_base_url: str,
    response_validator: Draft202012Validator,
) -> None:
    """Health and world endpoints must match the schema and fixture facts."""
    health = _request_json(http_session, bridge_base_url, "/health", 200)
    world = _request_json(http_session, bridge_base_url, "/world/state", 200)

    response_validator.validate(health)
    response_validator.validate(world)
    assert health["data"] == {
        "bridgeReady": True,
        "readOnly": True,
        "worldReady": True,
    }
    assert world["data"]["residentCount"] == len(world["data"]["residents"])
    assert world["data"]["residentCount"] == 15


def test_world_resident_ids_are_unique_and_detail_is_public_only(
    http_session: requests.Session,
    bridge_base_url: str,
    response_validator: Draft202012Validator,
) -> None:
    """Resident IDs stay unique and details expose only the public allowlist."""
    world = _request_json(http_session, bridge_base_url, "/world/state", 200)
    residents = world["data"]["residents"]
    resident_ids = [resident["residentId"] for resident in residents]
    assert len(resident_ids) == len(set(resident_ids))

    detail = _request_json(
        http_session,
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
) -> None:
    """Ten read requests must not mutate revision or world generation."""
    versions = []
    generations = []
    for _ in range(10):
        body = _request_json(http_session, bridge_base_url, "/world/state", 200)
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
# Pytest injects three fixtures plus three parameter values by design.
# pylint: disable=too-many-arguments
def test_read_errors_are_structured_and_schema_valid(
    http_session: requests.Session,
    bridge_base_url: str,
    response_validator: Draft202012Validator,
    path: str,
    status: int,
    error_code: str,
) -> None:
    """Missing resources and invalid paths return distinct structured errors."""
    body = _request_json(http_session, bridge_base_url, path, status)
    response_validator.validate(body)
    assert body["ok"] is False
    assert body["data"] is None
    assert body["error"]["code"] == error_code


def test_isolated_fixture_is_read_only(
    http_session: requests.Session,
    bridge_base_url: str,
) -> None:
    """The default isolated fixture must reject all write commands."""
    response = http_session.post(
        f"{bridge_base_url}/commands/set-speed",
        json={},
        timeout=3.0,
    )
    assert response.status_code == 405
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_non_loopback_host_is_rejected(
    http_session: requests.Session,
    bridge_base_url: str,
    response_validator: Draft202012Validator,
) -> None:
    """A non-loopback Host header must fail the DNS-rebinding guard."""
    response = http_session.get(
        f"{bridge_base_url}/health",
        headers={"Host": "example.invalid"},
        timeout=3.0,
    )
    assert response.status_code == 403
    body = response.json()
    response_validator.validate(body)
    assert body["error"]["code"] == "HOST_NOT_ALLOWED"
