"""MCP wire protocol integration tests — run in CI without real credentials."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import responses as responses_lib
from mcp.shared.memory import create_connected_server_and_client_session

from ac_infinity_mcp import server as srv
from ac_infinity_mcp.client import ACInfinityClient
from ac_infinity_mcp.schema import ACInfinityAuthError
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
    # Phase 17 Part 2 — Advance Automation management
    "list_advance_automations",
    "get_advance_automation",
    "enable_advance_automation",
    "disable_advance_automation",
    "create_advance_automation",
    "delete_advance_automation",
    "break_out_of_automation",
    # Issue #284 — Advance Automation full rule CRUD
    "add_automation_rule",
    "update_automation_rule",
    "delete_automation_rule",
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
    ("set_temperature_automation", ["device_id", "port", "min_temp", "max_temp"], ["dry_run"]),
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
    # Phase 17 Part 2 — Advance Automation
    ("list_advance_automations", ["device_id"], []),
    ("get_advance_automation", ["device_id", "automation_id"], []),
    (
        "enable_advance_automation",
        ["device_id", "automation_id"],
        ["dry_run"],
    ),
    (
        "disable_advance_automation",
        ["device_id", "automation_id"],
        ["dry_run"],
    ),
    (
        "create_advance_automation",
        ["device_id", "name", "port", "on_speed"],
        [
            "off_speed", "begin_time", "end_time", "mode", "control_style",
            "temp_high_f", "temp_low_f", "humidity_high", "humidity_low",
            "temp_target_f", "humidity_target", "vpd_target", "vpd_high", "vpd_low",
            "cycle_on_minutes", "cycle_off_minutes", "dry_run",
        ],
    ),
    (
        "delete_advance_automation",
        ["device_id", "automation_id"],
        ["dry_run"],
    ),
    (
        "break_out_of_automation",
        ["device_id", "port"],
        ["dry_run", "confirm_automation_name"],
    ),
    # Issue #284 — Advance Automation full rule CRUD
    (
        "add_automation_rule",
        ["device_id", "program_name", "ports", "mode"],
        [
            "control_style", "min_level", "max_level",
            "temp_high_f", "temp_low_f", "humidity_high", "humidity_low",
            "temp_target_f", "humidity_target", "vpd_target", "vpd_high", "vpd_low",
            "temp_buffer", "temp_transition", "humidity_buffer", "humidity_transition",
            "vpd_buffer", "vpd_transition",
            "cycle_on_minutes", "cycle_off_minutes", "begin_time", "end_time",
            "days", "continuous", "dry_run",
        ],
    ),
    (
        "update_automation_rule",
        ["device_id", "program_name", "ports"],
        [
            "begin_time", "end_time", "mode", "control_style", "min_level", "max_level",
            "temp_high_f", "temp_low_f", "humidity_high", "humidity_low",
            "temp_target_f", "humidity_target", "vpd_target", "vpd_high", "vpd_low",
            "temp_buffer", "temp_transition", "humidity_buffer", "humidity_transition",
            "vpd_buffer", "vpd_transition",
            "cycle_on_minutes", "cycle_off_minutes", "new_begin_time", "new_end_time",
            "days", "continuous", "dry_run",
        ],
    ),
    (
        "delete_automation_rule",
        ["device_id", "program_name", "ports"],
        ["begin_time", "end_time", "dry_run"],
    ),
]

# Env without AC Infinity credentials — built at import time so subprocess
# calls don't inherit any credentials set by autouse mock_env_vars fixture.
_CLEAN_ENV: dict[str, str] = {
    k: v
    for k, v in os.environ.items()
    if k not in ("AC_INFINITY_EMAIL", "AC_INFINITY_PASSWORD")
}


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


def test_all_28_tools_registered() -> None:
    registered = {t.name for t in mcp_server._tool_manager.list_tools()}  # type: ignore[attr-defined]
    assert registered == EXPECTED_TOOLS


def test_tool_count_is_exactly_28() -> None:
    assert len(mcp_server._tool_manager.list_tools()) == 28  # type: ignore[attr-defined]


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


async def test_protocol_list_tools_returns_all_28(mock_client: MagicMock) -> None:
    async with create_connected_server_and_client_session(srv.mcp_server) as session:
        result = await session.list_tools()
        names = {t.name for t in result.tools}
        assert len(result.tools) == 28
        assert names == EXPECTED_TOOLS


async def test_protocol_list_tools_input_schema_present(mock_client: MagicMock) -> None:
    async with create_connected_server_and_client_session(srv.mcp_server) as session:
        result = await session.list_tools()
        for tool in result.tools:
            assert isinstance(tool.inputSchema, dict), f"{tool.name}: inputSchema not a dict"
            assert "properties" in tool.inputSchema, (
                f"{tool.name}: no 'properties' in inputSchema"
            )


async def test_protocol_call_discover_devices_happy_path(mock_client: MagicMock) -> None:
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
    async with create_connected_server_and_client_session(srv.mcp_server) as session:
        result = await session.call_tool("discover_devices", {})
    assert result.content[0].type == "text"


async def test_protocol_call_get_device_reading_happy_path(mock_client: MagicMock) -> None:
    async with create_connected_server_and_client_session(srv.mcp_server) as session:
        result = await session.call_tool("get_device_reading", {"device_id": "C58ZA"})
    assert result.isError is False
    data = json.loads(result.content[0].text)
    assert "temperature" in data
    assert "unit" in data
    assert "humidity" in data
    assert "vpd" in data
    assert "error" not in data


async def test_protocol_call_get_all_device_readings_happy_path(
    mock_client: MagicMock,
) -> None:
    async with create_connected_server_and_client_session(srv.mcp_server) as session:
        result = await session.call_tool("get_all_device_readings", {})
    assert result.isError is False
    data = json.loads(result.content[0].text)
    assert "readings" in data
    assert len(data["readings"]) >= 1


async def test_protocol_call_check_vpd_drift_happy_path(mock_client: MagicMock) -> None:
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
    """With _aci_client=None, tools return a JSON error — not an MCP-level error frame.

    The error message is generic by design (P3-F021) — the specific
    "client not initialized" RuntimeError text only appears in server logs,
    not in the LLM-facing response.
    """
    with patch("ac_infinity_mcp.server._aci_client", None):
        async with create_connected_server_and_client_session(srv.mcp_server) as session:
            result = await session.call_tool("discover_devices", {})
    assert result.isError is False
    data = json.loads(result.content[0].text)
    assert "error" in data
    assert data["error"] == "Unexpected error"
    assert data.get("detail") == "see server logs"


async def test_protocol_call_tool_with_wrong_argument_type(mock_client: MagicMock) -> None:
    """FastMCP/Pydantic rejects non-integer port — expect MCP error or JSON error."""
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


@responses_lib.activate
async def test_first_tool_call_returns_auth_error_on_bad_credentials() -> None:
    """Bad credentials surface as an auth-error tool response, not a process crash."""
    responses_lib.add(
        responses_lib.POST,
        "https://www.acinfinityserver.com/api/user/appUserLogin",
        json={"code": 400, "msg": "Email or password is wrong"},
        status=200,
    )
    real_client = ACInfinityClient("bad@example.com", "wrongpass")
    srv.setup(real_client)
    try:
        async with create_connected_server_and_client_session(srv.mcp_server) as session:
            result = await session.call_tool("discover_devices", {})
        data = json.loads(result.content[0].text)
        assert "error" in data
        assert "Authentication" in data["error"]
        assert data.get("detail") == "see server logs"
        login_calls = [c for c in responses_lib.calls if "appUserLogin" in c.request.url]
        assert len(login_calls) == 1, "lazy-auth preamble must have fired (not inner guard)"
    finally:
        srv._aci_client = None
        srv._invalidate_device_cache()


def test_main_starts_with_placeholder_credentials() -> None:
    """Server responds to MCP initialize with placeholder creds — the Glama build check."""
    env = {
        **_CLEAN_ENV,
        "AC_INFINITY_EMAIL": "test@test.com",
        "AC_INFINITY_PASSWORD": "placeholder",
    }
    initialize_msg = (
        '{"jsonrpc":"2.0","id":1,"method":"initialize",'
        '"params":{"protocolVersion":"2024-11-05","capabilities":{},'
        '"clientInfo":{"name":"test","version":"0.1"}}}\n'
    )
    result = subprocess.run(
        [sys.executable, "-m", "ac_infinity_mcp.server"],
        env=env,
        input=initialize_msg,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert '"result"' in result.stdout
    assert '"protocolVersion"' in result.stdout


@responses_lib.activate
def test_main_stderr_contains_no_credentials(caplog: pytest.LogCaptureFixture) -> None:
    """Credentials must never appear in log output even when auth fails."""
    responses_lib.add(
        responses_lib.POST,
        "https://www.acinfinityserver.com/api/user/appUserLogin",
        json={"code": 400, "msg": "Email or password is wrong"},
        status=200,
    )
    import logging
    real_client = ACInfinityClient("secret@example.com", "supersecret999")
    with caplog.at_level(logging.DEBUG):
        # Trigger auth failure by calling the client directly
        with pytest.raises(ACInfinityAuthError):
            real_client.get_devices()
    assert "secret@example.com" not in caplog.text
    assert "supersecret999" not in caplog.text


async def test_main_no_api_calls_on_introspection() -> None:
    """MCP tools/list must not trigger any AC Infinity API calls."""
    real_client = ACInfinityClient("test@test.com", "placeholder")
    srv.setup(real_client)
    try:
        with patch.object(real_client, "_authenticate_inner") as mock_auth:
            async with create_connected_server_and_client_session(srv.mcp_server) as session:
                result = await session.list_tools()
            assert len(result.tools) == 28
            mock_auth.assert_not_called()
    finally:
        srv._aci_client = None
        srv._invalidate_device_cache()
