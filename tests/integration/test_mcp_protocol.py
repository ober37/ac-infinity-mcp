"""MCP wire protocol integration tests — run in CI without real credentials."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from ac_infinity_mcp import server as srv
from ac_infinity_mcp.server import mcp_server

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXPECTED_TOOLS = {
    "discover_devices",
    "get_device_reading",
    "get_all_device_readings",
    "get_historical_readings",
    "check_vpd_drift",
    "get_environment_health",
    "detect_environment_trends",
    "get_port_activity_report",
    "get_port_status",
    "get_port_settings",
    "set_port_speed",
    "set_port_on",
    "set_port_off",
    "set_vpd_automation",
    "set_temperature_automation",
    "set_humidity_automation",
    "set_port_mode",
    "apply_grow_stage_template",
}

SCHEMA_CASES: list[tuple[str, list[str], list[str]]] = [
    ("discover_devices", [], []),
    ("get_device_reading", ["device_id"], []),
    ("get_all_device_readings", [], []),
    (
        "get_historical_readings",
        ["device_id", "start_date", "end_date"],
        ["sample_interval", "time_start", "time_end"],
    ),
    ("check_vpd_drift", ["device_id"], ["stage"]),
    ("get_environment_health", ["device_id"], ["stage"]),
    ("detect_environment_trends", ["device_id"], ["days"]),
    ("get_port_activity_report", ["device_id"], ["days"]),
    ("get_port_status", ["device_id", "port"], []),
    ("get_port_settings", ["device_id", "port"], []),
    ("set_port_speed", ["device_id", "port", "speed"], ["dry_run"]),
    ("set_port_on", ["device_id", "port"], ["dry_run"]),
    ("set_port_off", ["device_id", "port"], ["dry_run"]),
    ("set_vpd_automation", ["device_id", "port", "target_vpd"], ["dry_run"]),
    ("set_temperature_automation", ["device_id", "port", "min_c", "max_c"], ["dry_run"]),
    ("set_humidity_automation", ["device_id", "port", "min_rh", "max_rh"], ["dry_run"]),
    (
        "set_port_mode",
        ["device_id", "port", "mode"],
        [
            "dry_run", "cycle_on_seconds", "cycle_off_seconds",
            "schedule_start", "schedule_end", "timer_duration_seconds",
        ],
    ),
    ("apply_grow_stage_template", ["device_id", "port", "stage"], ["dry_run"]),
]

# Env without AC Infinity credentials — built at import time so subprocess
# calls don't inherit any credentials set by autouse mock_env_vars fixture.
_CLEAN_ENV: dict[str, str] = {
    k: v
    for k, v in os.environ.items()
    if k not in ("AC_INFINITY_EMAIL", "AC_INFINITY_PASSWORD")
}

# Cycle 2 P2-C2-F001: the earlier one-liner form chained a compound `with`
# statement after semicolons, which Python parses as SyntaxError before
# srv.main() runs. The subprocess used to exit 1 from SyntaxError (matching
# the rc==1 assertion) and the "authenticate" word appeared in the parser's
# echo of the source line — so the test passed for the wrong reason.
# Use a proper multi-line script via subprocess input or `exec()` so the
# `with` block is valid Python and srv.main() actually executes.
_BAD_CREDS_SCRIPT = (
    "exec(\"\"\"\n"
    "from unittest.mock import patch\n"
    "import ac_infinity_mcp.server as srv\n"
    "with patch('ac_infinity_mcp.server.ACInfinityClient') as M:\n"
    "    M.return_value.authenticate.return_value = False\n"
    "    srv.main()\n"
    "\"\"\")"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_tool_schema(name: str) -> dict[str, Any]:
    tool = mcp_server._tool_manager.get_tool(name)  # type: ignore[attr-defined]
    assert tool is not None, f"Tool '{name}' not found"
    return tool.parameters  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Section 1: Tool Registration (synchronous, no protocol)
# ---------------------------------------------------------------------------


def test_all_18_tools_registered() -> None:
    registered = {t.name for t in mcp_server._tool_manager.list_tools()}  # type: ignore[attr-defined]
    assert registered == EXPECTED_TOOLS


def test_tool_count_is_exactly_18() -> None:
    assert len(mcp_server._tool_manager.list_tools()) == 18  # type: ignore[attr-defined]


def test_mcp_server_name_is_ac_infinity() -> None:
    assert mcp_server.name == "ac-infinity-mcp"


# ---------------------------------------------------------------------------
# Section 2: Parameter Schemas (synchronous, introspect _tool_manager)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,expected_required,expected_optional", SCHEMA_CASES)
def test_tool_schema_required_and_optional_params(
    name: str, expected_required: list[str], expected_optional: list[str]
) -> None:
    schema = _get_tool_schema(name)
    actual_required = set(schema.get("required", []))
    assert actual_required == set(expected_required), f"{name}: required params mismatch"
    props = schema.get("properties", {})
    for opt in expected_optional:
        assert opt in props, f"{name}: optional param '{opt}' missing from properties"
        assert opt not in actual_required, f"{name}: '{opt}' should not be in required"


def test_tool_schema_numeric_param_types() -> None:
    speed_schema = _get_tool_schema("set_port_speed")
    props = speed_schema["properties"]
    assert props["port"]["type"] == "integer"
    assert props["speed"]["type"] == "integer"
    assert props["dry_run"]["type"] == "boolean"

    trends_schema = _get_tool_schema("detect_environment_trends")
    assert trends_schema["properties"]["days"]["type"] == "integer"

    activity_schema = _get_tool_schema("get_port_activity_report")
    assert activity_schema["properties"]["days"]["type"] == "integer"


def test_get_historical_readings_defaults() -> None:
    schema = _get_tool_schema("get_historical_readings")
    props = schema["properties"]
    assert props["sample_interval"].get("default") == "1h"
    assert "time_start" in props
    assert "time_end" in props


# ---------------------------------------------------------------------------
# Section 3: Wire Protocol (in-process memory transport)
#
# Context manager is inlined in each test to avoid anyio cancel-scope teardown
# errors that occur when the scope exits in a different asyncio Task than it
# was entered in (a known pytest-asyncio + anyio interaction).
# ---------------------------------------------------------------------------


async def test_protocol_list_tools_returns_all_18(mock_client: MagicMock) -> None:
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        async with create_connected_server_and_client_session(srv.mcp_server) as session:
            result = await session.list_tools()
            names = {t.name for t in result.tools}
            assert len(result.tools) == 18
            assert names == EXPECTED_TOOLS


async def test_protocol_list_tools_input_schema_present(mock_client: MagicMock) -> None:
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        async with create_connected_server_and_client_session(srv.mcp_server) as session:
            result = await session.list_tools()
            for tool in result.tools:
                assert isinstance(tool.inputSchema, dict), f"{tool.name}: inputSchema not a dict"
                assert "properties" in tool.inputSchema, (
                    f"{tool.name}: no 'properties' in inputSchema"
                )


async def test_protocol_call_discover_devices_happy_path(mock_client: MagicMock) -> None:
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        async with create_connected_server_and_client_session(srv.mcp_server) as session:
            result = await session.call_tool("discover_devices", {})
    assert result.isError is False
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    data = json.loads(result.content[0].text)
    assert "devices" in data
    assert data["devices"][0]["device_id"] == "C58ZA"


async def test_protocol_call_discover_devices_returns_text_content(
    mock_client: MagicMock,
) -> None:
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        async with create_connected_server_and_client_session(srv.mcp_server) as session:
            result = await session.call_tool("discover_devices", {})
    assert result.content[0].type == "text"


async def test_protocol_call_get_device_reading_happy_path(mock_client: MagicMock) -> None:
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        async with create_connected_server_and_client_session(srv.mcp_server) as session:
            result = await session.call_tool("get_device_reading", {"device_id": "C58ZA"})
    assert result.isError is False
    data = json.loads(result.content[0].text)
    assert "temperature_c" in data
    assert "humidity" in data
    assert "vpd" in data
    assert "error" not in data


async def test_protocol_call_get_all_device_readings_happy_path(
    mock_client: MagicMock,
) -> None:
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        async with create_connected_server_and_client_session(srv.mcp_server) as session:
            result = await session.call_tool("get_all_device_readings", {})
    assert result.isError is False
    data = json.loads(result.content[0].text)
    assert "readings" in data
    assert len(data["readings"]) >= 1


async def test_protocol_call_check_vpd_drift_happy_path(mock_client: MagicMock) -> None:
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        async with create_connected_server_and_client_session(srv.mcp_server) as session:
            result = await session.call_tool(
                "check_vpd_drift", {"device_id": "C58ZA", "stage": "veg"}
            )
    assert result.isError is False
    data = json.loads(result.content[0].text)
    assert "current_vpd" in data
    assert "target_range" in data
    assert data["status"] in {"OK", "LOW", "HIGH"}
    assert data["stage"] == "veg"


async def test_protocol_call_check_vpd_drift_invalid_stage_returns_error(
    mock_client: MagicMock,
) -> None:
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        async with create_connected_server_and_client_session(srv.mcp_server) as session:
            result = await session.call_tool(
                "check_vpd_drift", {"device_id": "C58ZA", "stage": "bloom"}
            )
    assert result.isError is False  # tool-level error, not MCP-level
    data = json.loads(result.content[0].text)
    assert "error" in data
    assert "bloom" in data["error"]  # unknown stage echoed back
    assert "veg" in data["error"]    # valid stages listed in message


async def test_protocol_call_set_port_speed_dry_run_happy_path(
    mock_client: MagicMock,
) -> None:
    mock_client.set_port_mode.return_value = {
        "dry_run": True,
        "sent": False,
        "controller_type": "legacy",
        "payload": {"onSpead": 5},
    }
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        async with create_connected_server_and_client_session(srv.mcp_server) as session:
            result = await session.call_tool(
                "set_port_speed",
                {"device_id": "C58ZA", "port": 1, "speed": 5, "dry_run": True},
            )
    assert result.isError is False
    data = json.loads(result.content[0].text)
    assert data["dry_run"] is True
    assert data["sent"] is False
    assert "payload" in data


async def test_protocol_call_set_port_on_dry_run_happy_path(
    mock_client: MagicMock,
) -> None:
    mock_client.set_port_mode.return_value = {
        "dry_run": True,
        "sent": False,
        "controller_type": "legacy",
        "payload": {"onSpead": 10},
    }
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        async with create_connected_server_and_client_session(srv.mcp_server) as session:
            result = await session.call_tool(
                "set_port_on", {"device_id": "C58ZA", "port": 1, "dry_run": True}
            )
    assert result.isError is False
    data = json.loads(result.content[0].text)
    assert "on" in data["action"]
    assert data["dry_run"] is True
    assert data["sent"] is False


async def test_protocol_call_set_port_off_dry_run_happy_path(
    mock_client: MagicMock,
) -> None:
    mock_client.set_port_mode.return_value = {
        "dry_run": True,
        "sent": False,
        "controller_type": "legacy",
        "payload": {"onSpead": 0},
    }
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        async with create_connected_server_and_client_session(srv.mcp_server) as session:
            result = await session.call_tool(
                "set_port_off", {"device_id": "C58ZA", "port": 1, "dry_run": True}
            )
    assert result.isError is False
    data = json.loads(result.content[0].text)
    assert "off" in data["action"]
    assert data["dry_run"] is True
    assert data["sent"] is False


async def test_protocol_missing_client_guard() -> None:
    """With aci_client=None, tools return a JSON error — not an MCP-level error frame.

    The error message is generic by design (P3-F021) — the specific
    "client not initialized" RuntimeError text only appears in server logs,
    not in the LLM-facing response.
    """
    with patch("ac_infinity_mcp.server.aci_client", None):
        async with create_connected_server_and_client_session(srv.mcp_server) as session:
            result = await session.call_tool("discover_devices", {})
    assert result.isError is False
    data = json.loads(result.content[0].text)
    assert "error" in data
    assert data["error"] == "Unexpected error"
    assert data.get("detail") == "see server logs"


async def test_protocol_call_tool_with_wrong_argument_type(mock_client: MagicMock) -> None:
    """FastMCP/Pydantic rejects non-integer port — expect MCP error or JSON error."""
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        async with create_connected_server_and_client_session(srv.mcp_server) as session:
            result = await session.call_tool(
                "set_port_speed", {"device_id": "C58ZA", "port": "not_an_int", "speed": 5}
            )
    is_mcp_error = result.isError is True
    is_json_error = (
        not is_mcp_error
        and len(result.content) > 0
        and "error" in json.loads(result.content[0].text)
    )
    assert is_mcp_error or is_json_error


# ---------------------------------------------------------------------------
# Section 4: main() Startup Path (subprocess)
# ---------------------------------------------------------------------------


def test_main_exits_1_missing_email() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "ac_infinity_mcp.server"],
        env=_CLEAN_ENV,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "AC_INFINITY_EMAIL" in result.stderr


def test_main_exits_1_missing_password() -> None:
    env = {**_CLEAN_ENV, "AC_INFINITY_EMAIL": "test@example.com"}
    result = subprocess.run(
        [sys.executable, "-m", "ac_infinity_mcp.server"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "Missing" in result.stderr or "AC_INFINITY" in result.stderr


def test_main_exits_1_bad_credentials() -> None:
    env = {**_CLEAN_ENV, "AC_INFINITY_EMAIL": "x@example.com", "AC_INFINITY_PASSWORD": "badpass"}
    result = subprocess.run(
        [sys.executable, "-c", _BAD_CREDS_SCRIPT],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    # Defensive checks added during Cycle 2 (P2-C2-F001): pin that we exited
    # via the auth-failure path, not via a SyntaxError that happened to also
    # exit 1. The auth-failure log line in main() contains "Failed to
    # authenticate"; SyntaxError exits would not.
    assert result.returncode == 1
    assert "SyntaxError" not in result.stderr, (
        f"_BAD_CREDS_SCRIPT failed to parse — fix the script:\n{result.stderr}"
    )
    assert "Failed to authenticate" in result.stderr


def test_main_stderr_contains_no_credentials() -> None:
    env = {
        **_CLEAN_ENV,
        "AC_INFINITY_EMAIL": "secret@example.com",
        "AC_INFINITY_PASSWORD": "supersecret999",
    }
    result = subprocess.run(
        [sys.executable, "-c", _BAD_CREDS_SCRIPT],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert "secret@example.com" not in result.stderr
    assert "supersecret999" not in result.stderr
