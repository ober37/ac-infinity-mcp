"""Unit tests for server.py async tools and helper functions."""

import asyncio
import copy
import json
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from ac_infinity_mcp.schema import (
    _ADVANCE_MODE_TYPE,
    ACInfinityAdvanceConflictError,
    ACInfinityAPIError,
    ACInfinityAuthError,
    ACInfinityDeviceError,
)
from ac_infinity_mcp.server import (
    _check_advance_mode,
    _days_to_switchtime,
    _decode_mode,
    _decode_rule,
    _empty_port_advisory,
    _filter_readings_by_time,
    _find_governing_automation,
    _find_governing_port_group,
    _format_probe_clause,
    _format_probes,
    _format_schedule_time,
    _format_sensor_clause,
    _format_window_dt,
    _get_device,
    _get_port_label,
    _group_automations,
    _invalidate_device_cache,
    _is_port_empty,
    _is_port_not_powered,
    _parse_duration_seconds,
    _parse_schedule_time,
    _resolve_temp_trigger,
    _sanitize_api_string,
    _short_date,
    _utc_hour_to_local,
    _validate_automation_id,
    _validate_rule_inputs,
    add_automation_rule,
    apply_grow_stage_template,
    apply_sampling,
    average_readings,
    break_out_of_automation,
    check_vpd_drift,
    create_advance_automation,
    delete_advance_automation,
    delete_automation_rule,
    detect_environment_trends,
    disable_advance_automation,
    discover_devices,
    enable_advance_automation,
    environment_alert_interpretation,
    get_advance_automation,
    get_all_device_readings,
    get_device_reading,
    get_environment_health,
    get_historical_readings,
    get_port_activity_report,
    get_port_settings,
    get_port_status,
    list_advance_automations,
    mcp_server,
    new_grower_setup,
    set_humidity_automation,
    set_port_mode,
    set_port_off,
    set_port_on,
    set_port_speed,
    set_temperature_automation,
    set_vpd_automation,
    update_automation_rule,
    vpd_troubleshooting,
)
from tests.conftest import MOCK_DEVICE_LEGACY
from tests.fixtures.advance_automation_fixtures import (
    MOCK_ADVANCE_AUTOMATIONS_LIST,
    MOCK_ADVANCE_AUTOMATIONS_SINGLE,
    MOCK_RULE_HUMIDITY_SETPOINT,
    MOCK_RULE_TEMPERATURE_TRIGGER,
    MOCK_RULE_VPD,
)


def _make_history_record(ts: str, temp_c: float = 24.0, humidity: float = 55.0,
                         vpd: float = 1.5, ports=None) -> dict:
    return {
        "timestamp": ts,
        "temperature_c": temp_c,
        "temperature_f": round(temp_c * 9 / 5 + 32, 1),
        "humidity": humidity,
        "vpd": vpd,
        "ports": ports or [],
    }


@pytest.fixture(autouse=True)
def reset_device_cache():
    """Reset the TTL cache before each test so tests are independent."""
    import ac_infinity_mcp.server as srv
    srv._device_cache = None
    srv._device_cache_expires_at = 0.0
    yield
    srv._device_cache = None
    srv._device_cache_expires_at = 0.0


# ============ Smoke / symbol checks ============

def test_mcp_server_name():
    assert mcp_server.name == "ac-infinity-mcp"


# ============ discover_devices ============

async def test_discover_devices_success(mock_client):
    result = await discover_devices()
    data = json.loads(result)
    assert "devices" in data
    assert len(data["devices"]) == 1
    assert data["devices"][0]["device_id"] == "C58ZA"


async def test_discover_devices_empty(mock_client):
    mock_client.get_devices.return_value = []
    result = await discover_devices()
    data = json.loads(result)
    assert data["devices"] == []
    # The "No devices found" message is part of the documented contract;
    # regression removing it would have been invisible before (P2-F024).
    assert data["message"] == "No devices found"


async def test_discover_devices_api_error(mock_client):
    mock_client.get_devices.side_effect = ACInfinityAPIError("API error 500: server fault")
    result = await discover_devices()
    data = json.loads(result)
    assert data["error"] == "AC Infinity API error"
    assert "detail" in data


async def test_discover_devices_auth_error(mock_client):
    mock_client.get_devices.side_effect = ACInfinityAuthError("Not authenticated")
    result = await discover_devices()
    data = json.loads(result)
    assert "Authentication failed" in data["error"]
    assert "detail" in data
    # Verify no actual credential values appear in the response
    assert "test@example.com" not in result
    assert "testpassword123" not in result


async def test_discover_devices_online_offline_status(mock_client):
    mock_client.get_devices.return_value = [
        {"devCode": "A1", "devName": "Device A", "online": True},
        {"devCode": "B2", "devName": "Device B", "online": False},
    ]
    result = await discover_devices()
    data = json.loads(result)
    by_id = {d["device_id"]: d for d in data["devices"]}
    assert by_id["A1"]["status"] == "online"
    assert by_id["B2"]["status"] == "offline"


async def test_discover_devices_client_not_initialized():
    with patch("ac_infinity_mcp.server._aci_client", None):
        result = await discover_devices()
    data = json.loads(result)
    assert "error" in data


async def test_discover_devices_includes_device_metadata(mock_client):
    """discover_devices must expose firmware_version, hardware_version, port_count, device_type."""
    result = await discover_devices()
    data = json.loads(result)
    device = data["devices"][0]
    assert device["device_type"] == 11
    assert device["port_count"] == 8
    assert device["firmware_version"] == "3.5.28"
    assert device["hardware_version"] == "1.0"


async def test_discover_devices_metadata_absent_fields_are_none(mock_client):
    """Fields absent from the API response come through as None, not KeyError."""
    mock_client.get_devices.return_value = [
        {"devCode": "X1", "devName": "Minimal", "online": True},
    ]
    result = await discover_devices()
    data = json.loads(result)
    device = data["devices"][0]
    assert device["device_type"] is None
    assert device["port_count"] is None
    assert device["firmware_version"] is None
    assert device["hardware_version"] is None


async def test_discover_devices_human_summary_single(mock_client):
    """1 device → prose human_summary with name, id, status."""
    result = await discover_devices()
    data = json.loads(result)
    assert "human_summary" in data
    summary = data["human_summary"]
    assert "1 device found" in summary
    assert "C58ZA" in summary


async def test_discover_devices_human_summary_two_devices(mock_client):
    """2 devices → prose human_summary (below table threshold)."""
    mock_client.get_devices.return_value = [
        {"devCode": "A1", "devName": "Tent A", "online": True},
        {"devCode": "B2", "devName": "Tent B", "online": False},
    ]
    result = await discover_devices()
    data = json.loads(result)
    assert "human_summary" in data
    summary = data["human_summary"]
    assert "2 devices found" in summary
    assert "A1" in summary
    assert "B2" in summary
    assert "|" not in summary  # prose, not a table


async def test_discover_devices_human_summary_table_three_devices(mock_client):
    """≥3 devices → markdown table in human_summary."""
    mock_client.get_devices.return_value = [
        {"devCode": "A1", "devName": "Tent A", "online": True},
        {"devCode": "B2", "devName": "Tent B", "online": False},
        {"devCode": "C3", "devName": "Tent C", "online": True},
    ]
    result = await discover_devices()
    data = json.loads(result)
    assert "human_summary" in data
    summary = data["human_summary"]
    assert "| Device | ID | Status |" in summary
    assert "Tent A" in summary
    assert "A1" in summary
    assert "C3" in summary


# ============ appEmail PII filtering (P2-F003) ============
#
# docs/API.md warns that device list responses include the authenticated user's
# email address in the appEmail field. The read tools must filter it out, and
# logging must never emit it at any level. These tests pin both contracts.

_PII_EMAIL = "leaked-pii@example.com"


def _device_with_pii() -> dict:
    """A legacy fixture device with appEmail populated, as the real API sends."""
    from tests.conftest import MOCK_DEVICE_LEGACY
    return {**MOCK_DEVICE_LEGACY, "appEmail": _PII_EMAIL}


@pytest.mark.parametrize(
    "tool_name,args",
    [
        ("discover_devices", ()),
        ("get_device_reading", ("C58ZA",)),
        ("get_all_device_readings", ()),
        ("get_port_status", ("C58ZA", 1)),
        ("get_port_settings", ("C58ZA", 1)),
    ],
)
async def test_read_tools_do_not_echo_appEmail(mock_client, caplog, tool_name, args):
    """Read tools must not include the user's appEmail in their JSON output or logs."""
    import logging

    import ac_infinity_mcp.server as server_module
    tool = getattr(server_module, tool_name)
    mock_client.get_devices.return_value = [_device_with_pii()]
    mock_client.get_mode_settings.return_value = {
        "atType": 1, "modeType": 0, "onSpead": 0, "offSpead": 0,
    }

    with caplog.at_level(logging.DEBUG, logger="ac_infinity_mcp"):
        result = await tool(*args)

    assert _PII_EMAIL not in result, f"{tool_name} leaked appEmail in its response"
    for record in caplog.records:
        assert _PII_EMAIL not in record.getMessage(), (
            f"{tool_name} leaked appEmail in a log record at level {record.levelname}"
        )


# ============ Credential-redacting log filter (P3-F006, P3-F019) ============


@pytest.mark.parametrize("raw,expected", [
    ("token=abc123def456", "token=<redacted>"),
    ("appPasswordl=hunter2", "appPasswordl=<redacted>"),
    ("appEmail=user@example.com", "appEmail=<redacted>"),
    ("{'appPassword': 'shouldnotleak'}", "{'appPassword': '<redacted>'}"),
    ('{"token": "abc-123_XYZ.456"}', '{"token": "<redacted>"}'),
    ("AC_INFINITY_PASSWORD=verysecret", "AC_INFINITY_PASSWORD=<redacted>"),
    # P1-C2-F001: userId in URL query string (HTTPError __str__ leak vector)
    (
        "500 Server Error for url: http://server/api?userId=SECRETTOKEN123",
        "500 Server Error for url: http://server/api?userId=<redacted>",
    ),
    # P3-C2-F004: password with embedded space — value pattern stops at structural
    # terminators (comma, newline, brace), NOT at whitespace
    ("appPasswordl=hunter pwd2,trailing", "appPasswordl=<redacted>,trailing"),
    # P1-C3-F002: URL query with trailing params — `&` is a terminator so the
    # trailing params survive redaction
    (
        "GET http://api/v1?userId=TOK&page=1&size=20",
        "GET http://api/v1?userId=<redacted>&page=1&size=20",
    ),
])
def test_credential_redaction_redacts_known_fields(raw, expected):
    """The redactor must scrub credential field values across multiple shapes."""
    from ac_infinity_mcp.server import _redact_credentials
    assert _redact_credentials(raw) == expected


def test_credential_redaction_leaves_clean_messages_alone():
    from ac_infinity_mcp.server import _redact_credentials
    clean = "Fetched 3 devices for user"
    assert _redact_credentials(clean) == clean


def test_credential_redaction_scrubs_exception_traceback():
    """P1-C2-F002 / P3-C2-F001: exc_info=True logs go through formatException;
    the formatter must scrub credentials from the traceback text too."""
    import io
    import logging

    from ac_infinity_mcp.server import _CredentialRedactingFormatter

    fmt = _CredentialRedactingFormatter()
    try:
        raise ValueError("login failed for appPasswordl=topsecret123")
    except ValueError:
        import sys
        record = logging.LogRecord(
            name="x", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="oops: %s", args=(sys.exc_info()[1],),
            exc_info=sys.exc_info(),
        )
        formatted = fmt.format(record)

    assert "topsecret123" not in formatted, (
        f"credential leaked through exc_info traceback:\n{formatted}"
    )
    assert "<redacted>" in formatted

    # also verify the bare _redact_credentials path covers the traceback text
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(fmt)
    handler.emit(record)
    assert "topsecret123" not in buf.getvalue()


@pytest.mark.parametrize("exc_class,exc_msg,tool_call", [
    # P3-C2-F003: typed exception text constructed from upstream API msg used to
    # land verbatim in the LLM-facing "detail" field. Detail now routes to logs.
    (ACInfinityAPIError, "Reflected appEmail=victim@example.com from upstream", "discover_devices"),
    (ACInfinityAuthError, "Token rejected: appPasswordl=hunter2", "discover_devices"),
])
async def test_typed_exception_text_does_not_leak_to_mcp_response(
    mock_client, exc_class, exc_msg, tool_call
):
    """Upstream-constructed exception messages must not appear in the MCP JSON response."""
    import ac_infinity_mcp.server as server_module
    tool = getattr(server_module, tool_call)
    mock_client.get_devices.side_effect = exc_class(exc_msg)

    result = await tool()

    # The exception message should NOT appear in the JSON response
    assert "victim@example.com" not in result
    assert "hunter2" not in result
    assert "appEmail=" not in result
    assert "appPasswordl=" not in result
    # And the response should route the caller to logs
    data = json.loads(result)
    assert data["detail"] == "see server logs"


@pytest.mark.parametrize("tool_name,args,fail_target", [
    ("set_port_speed", ("C58ZA", 1, 5), "set_port_mode"),
    ("set_port_on", ("C58ZA", 1), "set_port_mode"),
    ("set_port_off", ("C58ZA", 1), "set_port_mode"),
    ("set_vpd_automation", ("C58ZA", 1, 1.2), "set_port_mode"),
    ("set_temperature_automation", ("C58ZA", 1, 20.0, 28.0), "set_port_mode"),
    ("set_humidity_automation", ("C58ZA", 1, 50.0, 70.0), "set_port_mode"),
    ("set_port_mode", ("C58ZA", 1, "ON"), "set_port_mode"),
])
@pytest.mark.parametrize("exc_class,exc_msg", [
    # P3-C3-F001: write tools used to return {"error": str(e)} for the typed
    # exception triplet — leaking upstream API messages (which embed the
    # uncontrolled API response `msg` field) into the LLM-facing JSON.
    (ACInfinityAPIError, "API error 500: Reflected appEmail=leak@example.com"),
    (ACInfinityAuthError, "Token rejected by API (code 401): appPasswordl=hunter2"),
])
async def test_write_tools_do_not_leak_auth_or_api_exception_text(
    mock_client, tool_name, args, fail_target, exc_class, exc_msg,
):
    """Write tools must scrub ACInfinityAuthError/APIError text from the response (P3-C3-F001).

    ACInfinityDeviceError is intentionally NOT in this parametrize set — its
    messages (loadType=4/128, modeType=15) are self-constructed and actionable;
    the LLM uses them to switch to the right tool. See test_set_port_speed_*
    for the device-error path that pins those hints reach the LLM.
    """
    import ac_infinity_mcp.server as server_module
    tool = getattr(server_module, tool_name)
    getattr(mock_client, fail_target).side_effect = exc_class(exc_msg)

    result = await tool(*args)

    assert "leak@example.com" not in result, f"{tool_name} leaked appEmail"
    assert "hunter2" not in result, f"{tool_name} leaked password"
    assert "appEmail=" not in result
    assert "appPasswordl=" not in result
    # The response must route the caller to logs for both error classes.
    data = json.loads(result)
    assert data.get("detail") == "see server logs"


@pytest.mark.parametrize("raw,expected_level,expected_warn", [
    # Valid inputs pass through with no warning
    ("DEBUG", "DEBUG", False),
    ("INFO", "INFO", False),
    ("WARNING", "WARNING", False),
    ("ERROR", "ERROR", False),
    ("CRITICAL", "CRITICAL", False),
    # Case-insensitivity
    ("debug", "DEBUG", False),
    ("Warning", "WARNING", False),
    # Invalid → INFO with warn flag (P2-C2-F003)
    ("BOGUS", "INFO", True),
    # Empty / None fall back to INFO default — operator didn't try anything, no warn
    ("", "INFO", False),
    (None, "INFO", False),
    ("trace", "INFO", True),
    ("verbose", "INFO", True),
])
def test_resolve_log_level(raw, expected_level, expected_warn):
    """Pin the LOG_LEVEL validation contract directly (P2-C2-F003)."""
    from ac_infinity_mcp.server import _resolve_log_level
    level, warn = _resolve_log_level(raw)
    assert level == expected_level
    assert warn == expected_warn


def test_credential_redactor_installed_on_root_handlers():
    """P2-C2-F006: pin that the formatter is actually attached, not just constructible."""
    import logging

    from ac_infinity_mcp.server import _CredentialRedactingFormatter
    handlers = logging.getLogger().handlers
    assert handlers, "root logger has no handlers — install loop never ran"
    assert any(
        isinstance(h.formatter, _CredentialRedactingFormatter) for h in handlers
    ), "no root handler has the credential redactor attached"


def test_parse_device_data_drops_appEmail():
    """parse_device_data must not propagate appEmail to its returned dict (P2-F003)."""
    from ac_infinity_mcp.client import ACInfinityClient
    client = ACInfinityClient("test@example.com", "pw")
    parsed = client.parse_device_data(_device_with_pii())
    assert _PII_EMAIL not in json.dumps(parsed)
    assert "appEmail" not in parsed


@pytest.mark.parametrize("tool_name,args", [
    # P2-C2-F005: extend PII filter coverage to the rest of the read-side tools
    ("get_historical_readings", ("C58ZA", "2024-04-25", "2024-04-25")),
    ("check_vpd_drift", ("C58ZA", "veg")),
    ("get_environment_health", ("C58ZA", "veg")),
])
async def test_more_read_tools_do_not_echo_appEmail(mock_client, caplog, tool_name, args):
    """Extends the appEmail filter coverage to historical/analytics tools."""
    import logging

    import ac_infinity_mcp.server as server_module
    tool = getattr(server_module, tool_name)
    mock_client.get_devices.return_value = [_device_with_pii()]
    # Stub historical-data fetch so the tool runs end-to-end.
    mock_client.get_historical_data.return_value = []

    with caplog.at_level(logging.DEBUG, logger="ac_infinity_mcp"):
        result = await tool(*args)

    assert _PII_EMAIL not in result, f"{tool_name} leaked appEmail in its response"
    for record in caplog.records:
        assert _PII_EMAIL not in record.getMessage()


# ============ Edge-input device_id and port handling (P2-F014) ============
#
# LLMs occasionally hallucinate inputs like "" (empty), "  " (whitespace),
# or very long strings. Tools must return graceful structured errors rather
# than crashing or returning success-shaped responses with empty results.

@pytest.mark.parametrize("bad_device_id", ["", "   ", "X" * 1000])
async def test_tools_handle_edge_device_ids(mock_client, bad_device_id):
    """Empty / whitespace / oversized device_id returns a structured error."""
    for tool_name, args in [
        ("get_device_reading", (bad_device_id,)),
        ("get_port_status", (bad_device_id, 1)),
        ("get_port_settings", (bad_device_id, 1)),
    ]:
        import ac_infinity_mcp.server as server_module
        tool = getattr(server_module, tool_name)
        result = await tool(*args)
        data = json.loads(result)
        assert "error" in data, f"{tool_name}({bad_device_id!r}) should error, got {data}"
        # Bad device_id should produce a "not found" style error, not a traceback
        assert "Traceback" not in result
        assert "/Users/" not in result  # no local filesystem leakage


async def test_set_port_speed_negative_speed(mock_client):
    """Negative speed inputs should produce a structured validation error."""
    from ac_infinity_mcp.server import set_port_speed
    result = await set_port_speed("C58ZA", 1, -1)
    data = json.loads(result)
    assert "error" in data
    # Should not have attempted any client call
    mock_client.set_port_mode.assert_not_called()


# ============ get_device_reading ============

async def test_get_device_reading_success(mock_client):
    result = await get_device_reading("C58ZA")
    data = json.loads(result)
    assert data["device_id"] == "C58ZA"
    assert "temperature" in data
    assert "unit" in data
    assert "temperature_c" not in data
    assert "humidity" in data
    assert "vpd" in data


async def test_get_device_reading_human_summary(mock_client):
    """human_summary contains temp, humidity, and VPD for quick grower read."""
    result = await get_device_reading("C58ZA")
    data = json.loads(result)
    assert "human_summary" in data
    summary = data["human_summary"]
    assert "RH" in summary
    assert "kPa" in summary
    assert "Reading from" in summary


async def test_get_device_reading_no_load_field_in_ports(mock_client):
    """Regression guard: 'load' key must be absent from ports in get_device_reading output."""
    mock_client.parse_device_data.return_value = {
        "device_name": "Test", "temperature_c": 23.5, "temperature_f": 74.3,
        "humidity": 60.0, "vpd": 1.24, "timestamp": None, "zone_id": None,
        "temp_unit_raw": None, "external_sensors": [],
        "ports": [{"port": 1, "name": "Intake Fan", "speed": 5}],
    }
    result = await get_device_reading("C58ZA")
    data = json.loads(result)
    assert "load" not in data["ports"][0]


async def test_get_device_reading_plug_status_propagates(mock_client):
    """plug_status passes through get_device_reading unchanged when parser emits it."""
    mock_client.parse_device_data.return_value = {
        "device_name": "Test", "temperature_c": 23.5, "temperature_f": 74.3,
        "humidity": 60.0, "vpd": 1.24, "timestamp": None, "zone_id": None,
        "temp_unit_raw": None, "external_sensors": [],
        "ports": [
            {"port": 1, "name": "Inline Fan", "speed": 5},
            {"port": 2, "name": "Humidifier", "speed": 0, "plug_status": "not powered"},
        ],
    }
    result = await get_device_reading("C58ZA")
    data = json.loads(result)
    assert "plug_status" not in data["ports"][0]
    assert data["ports"][1]["plug_status"] == "not powered"


async def test_get_device_reading_device_not_found(mock_client):
    result = await get_device_reading("NOTEXIST")
    data = json.loads(result)
    assert "error" in data
    assert "NOTEXIST" in data["error"]


async def test_get_device_reading_api_error(mock_client):
    mock_client.get_devices.side_effect = ACInfinityAPIError("API error 500: server fault")
    result = await get_device_reading("C58ZA")
    data = json.loads(result)
    assert data["error"] == "AC Infinity API error"
    assert "detail" in data


async def test_get_device_reading_auth_error(mock_client):
    mock_client.get_devices.side_effect = ACInfinityAuthError("Not authenticated")
    result = await get_device_reading("C58ZA")
    data = json.loads(result)
    assert "Authentication failed" in data["error"]
    assert "detail" in data


# ============ get_all_device_readings ============

async def test_get_all_device_readings_success(mock_client):
    second = {**MOCK_DEVICE_LEGACY, "devCode": "D2"}
    mock_client.get_devices.return_value = [MOCK_DEVICE_LEGACY, second]
    result = await get_all_device_readings()
    data = json.loads(result)
    assert "readings" in data
    assert len(data["readings"]) == 2


async def test_get_all_device_readings_api_error(mock_client):
    mock_client.get_devices.side_effect = ACInfinityAPIError("API error 500: server fault")
    result = await get_all_device_readings()
    data = json.loads(result)
    assert data["error"] == "AC Infinity API error"
    assert "detail" in data


async def test_get_all_device_readings_auth_error(mock_client):
    mock_client.get_devices.side_effect = ACInfinityAuthError("Not authenticated")
    result = await get_all_device_readings()
    data = json.loads(result)
    assert "Authentication failed" in data["error"]
    assert "detail" in data


async def test_get_all_device_readings_parse_error_isolated(mock_client):
    good_device = MOCK_DEVICE_LEGACY
    bad_device = {**MOCK_DEVICE_LEGACY, "devCode": "BAD"}
    mock_client.get_devices.return_value = [good_device, bad_device]

    def side_effect(device):
        if device.get("devCode") == "BAD":
            raise ValueError("simulated parse failure")
        return mock_client.parse_device_data.return_value

    mock_client.parse_device_data.side_effect = side_effect
    result = await get_all_device_readings()
    data = json.loads(result)
    readings = {r["device_id"]: r for r in data["readings"]}
    assert "error" in readings["BAD"]
    assert "error" not in readings["C58ZA"]


async def test_get_all_device_readings_human_summary_prose(mock_client):
    """2 devices → prose human_summary (below table threshold)."""
    second = {**MOCK_DEVICE_LEGACY, "devCode": "D2"}
    mock_client.get_devices.return_value = [MOCK_DEVICE_LEGACY, second]
    result = await get_all_device_readings()
    data = json.loads(result)
    assert "human_summary" in data
    assert "|" not in data["human_summary"]  # prose, not a table
    assert "kPa" in data["human_summary"]


async def test_get_all_device_readings_human_summary_table(mock_client):
    """≥3 devices → markdown table in human_summary."""
    d2 = {**MOCK_DEVICE_LEGACY, "devCode": "D2"}
    d3 = {**MOCK_DEVICE_LEGACY, "devCode": "D3"}
    mock_client.get_devices.return_value = [MOCK_DEVICE_LEGACY, d2, d3]
    result = await get_all_device_readings()
    data = json.loads(result)
    assert "human_summary" in data
    summary = data["human_summary"]
    assert "| Device | Temp | Humidity | VPD |" in summary
    assert "kPa" in summary


# ============ _format_sensor_clause + external-sensor prose (#255, #264, #265) ============


def _parsed_sensor(sensor_type_label, value, unit, sensor_type=11, access_port=1):
    """A parsed external-sensor dict, matching client.parse_device_data's shape."""
    return {
        "sensor_id": f"{access_port}.{sensor_type}",
        "sensor_type": sensor_type,
        "sensor_type_label": sensor_type_label,
        "value": value,
        "unit": unit,
    }


def test_format_sensor_clause_empty_is_blank():
    """No external sensors → '' (keeps human_summary byte-identical to pre-sensor output)."""
    assert _format_sensor_clause([]) == ""


def test_format_sensor_clause_multi_sensor():
    clause = _format_sensor_clause([
        _parsed_sensor("CO2", 793, "ppm"),
        _parsed_sensor("pH", 6.5, "", sensor_type=13),
        _parsed_sensor("Light", 100.0, "%", sensor_type=12),
    ])
    assert clause == "External sensors — CO2: 793 ppm, pH: 6.5, Light: 100.0%"


@pytest.mark.parametrize("label,value,sensor_type", [
    ("pH", 6.5, 13),
    ("Water Level", 3, 20),
    ("Unrecognized (type 77)", 1234, 77),
])
def test_format_sensor_clause_empty_unit_no_trailing_space(label, value, sensor_type):
    """Unitless / unrecognized sensors render with no trailing space or orphan separator."""
    clause = _format_sensor_clause(
        [_parsed_sensor(label, value, "", sensor_type=sensor_type)]
    )
    assert clause == f"External sensors — {label}: {value}"
    assert not clause.endswith(" ")


@pytest.mark.parametrize("label,value,unit,expected_part", [
    ("Light", 100.0, "%", "Light: 100.0%"),
    ("Water Temp", 24.5, "°C", "Water Temp: 24.5°C"),
    ("Water Temp", 68, "°F", "Water Temp: 68°F"),
    ("CO2", 793, "ppm", "CO2: 793 ppm"),
    ("EC", 2.1, "µS/cm", "EC: 2.1 µS/cm"),
])
def test_format_sensor_clause_unit_spacing(label, value, unit, expected_part):
    """% and degree units attach to the number; ppm/µS/cm take a leading space."""
    clause = _format_sensor_clause([_parsed_sensor(label, value, unit)])
    assert clause == f"External sensors — {expected_part}"


def test_format_sensor_clause_no_pipe():
    """The clause must never contain '|' — it would break the ≥3-device table heuristic."""
    clause = _format_sensor_clause([
        _parsed_sensor("EC", 2.1, "µS/cm", sensor_type=14),
        _parsed_sensor("TDS", 500, "ppm", sensor_type=16),
    ])
    assert "|" not in clause


async def test_get_device_reading_human_summary_with_sensors(mock_client):
    """External sensors appear in the get_device_reading human_summary prose."""
    mock_client.parse_device_data.return_value["external_sensors"] = [
        _parsed_sensor("CO2", 793, "ppm"),
        _parsed_sensor("pH", 6.5, "", sensor_type=13),
    ]
    summary = json.loads(await get_device_reading("C58ZA"))["human_summary"]
    assert "External sensors — CO2: 793 ppm, pH: 6.5" in summary
    assert "|" not in summary
    assert "Reading from" in summary


async def test_get_device_reading_human_summary_without_sensors_unchanged(mock_client):
    """No external sensors → summary keeps the exact '…kPa. Reading from …' shape."""
    mock_client.parse_device_data.return_value["external_sensors"] = []
    summary = json.loads(await get_device_reading("C58ZA"))["human_summary"]
    assert "External sensors" not in summary
    assert "kPa. Reading from" in summary  # no stray clause or doubled space inserted


async def test_get_all_device_readings_prose_with_sensors(mock_client):
    """1–2-device prose includes each device's external-sensor clause; no pipe."""
    d1 = {**MOCK_DEVICE_LEGACY, "devCode": "C58ZA"}
    d2 = {**MOCK_DEVICE_LEGACY, "devCode": "D2"}
    mock_client.get_devices.return_value = [d1, d2]

    def side_effect(device):
        base = copy.deepcopy(mock_client.parse_device_data.return_value)
        base["device_name"] = device.get("devCode")
        base["external_sensors"] = (
            [_parsed_sensor("CO2", 793, "ppm")]
            if device.get("devCode") == "C58ZA"
            else []
        )
        return base

    mock_client.parse_device_data.side_effect = side_effect
    summary = json.loads(await get_all_device_readings())["human_summary"]
    assert "External sensors — CO2: 793 ppm" in summary
    assert "|" not in summary


# ============ get_historical_readings ============

async def test_get_historical_readings_success(mock_client):
    base_ts = 1714000000
    raw_records = [
        {
            "createTime": base_ts + i * 3600,
            "temperature": 2400,
            "fTemperature": 7520,
            "humidity": 5500,
            "vpdNums": 150,
            "portSpead": 0,
            "portStatus": 0,
            "devPortCount": 2,
        }
        for i in range(5)
    ]
    mock_client.get_historical_data.return_value = raw_records
    mock_client.parse_history_record.side_effect = lambda r, port_names=None: {
        "timestamp": f"2024-04-25T{(r['createTime'] - base_ts) // 3600:02d}:00:00Z",
        "temperature_c": 24.0,
        "temperature_f": 75.2,
        "humidity": 55.0,
        "vpd": 1.5,
        "ports": [],
    }
    result = await get_historical_readings("C58ZA", "2024-04-25", "2024-04-25", "raw")
    data = json.loads(result)
    assert "readings" in data
    assert len(data["readings"]) == 5
    assert "statistics" in data


async def test_get_historical_readings_invalid_date_format(mock_client):
    result = await get_historical_readings("C58ZA", "not-a-date", "2024-04-25")
    data = json.loads(result)
    assert "error" in data
    assert "YYYY-MM-DD" in data["error"]


async def test_get_historical_readings_start_after_end(mock_client):
    result = await get_historical_readings("C58ZA", "2024-04-26", "2024-04-25")
    data = json.loads(result)
    assert "error" in data
    assert "start_date" in data["error"]


async def test_get_historical_readings_invalid_interval(mock_client):
    result = await get_historical_readings("C58ZA", "2024-04-25", "2024-04-25", "2x")
    data = json.loads(result)
    assert "error" in data
    assert "sample_interval" in data["error"].lower() or "2x" in data["error"]


@pytest.mark.parametrize("bad_value", ["bad", "25:00", "12:60", "1200", "noon", ""])
async def test_get_historical_readings_invalid_time_start(mock_client, bad_value):
    """Invalid time_start returns structured error instead of silent empty result (P1-F006)."""
    result = await get_historical_readings(
        "C58ZA", "2024-04-25", "2024-04-25", "1h", time_start=bad_value
    )
    data = json.loads(result)
    assert "error" in data
    assert "time_start" in data["error"]


async def test_get_historical_readings_invalid_time_end(mock_client):
    result = await get_historical_readings(
        "C58ZA", "2024-04-25", "2024-04-25", "1h", time_end="bogus"
    )
    data = json.loads(result)
    assert "error" in data
    assert "time_end" in data["error"]


async def test_get_historical_readings_no_device(mock_client):
    result = await get_historical_readings("NOTEXIST", "2024-04-25", "2024-04-25")
    data = json.loads(result)
    assert "error" in data


async def test_get_historical_readings_no_records(mock_client):
    mock_client.get_historical_data.return_value = []
    result = await get_historical_readings("C58ZA", "2024-04-25", "2024-04-25")
    data = json.loads(result)
    assert "error" in data
    assert "No readings" in data["error"]


async def test_get_historical_readings_surfaces_dropped_count(mock_client):
    """P2-C2-F004: dropped_readings and drop_reason must appear in the tool response.

    The helper-level drop count is tested separately; this test pins that the
    server wiring exposes both fields in the JSON output.
    """
    base_ts = 1714000000
    # parse_history_record is called once per raw record; return a mix of
    # well-formed and bad-timestamp readings so the time filter drops two.
    mock_client.get_historical_data.return_value = [{"createTime": base_ts}] * 3
    mock_client.parse_history_record.side_effect = [
        {"timestamp": "2024-04-25T10:00:00Z", "temperature_c": 24.0,
         "temperature_f": 75.2, "humidity": 60.0, "vpd": 1.2, "ports": []},
        {"timestamp": "NOT_VALID", "temperature_c": 25.0,
         "temperature_f": 77.0, "humidity": 61.0, "vpd": 1.3, "ports": []},
        {"timestamp": "", "temperature_c": 26.0,
         "temperature_f": 78.8, "humidity": 62.0, "vpd": 1.4, "ports": []},
    ]
    result = await get_historical_readings(
        "C58ZA", "2024-04-25", "2024-04-25", "raw", time_start="00:00",
    )
    data = json.loads(result)
    assert data["dropped_readings"] == 2
    assert data["drop_reason"] == "malformed timestamp"


async def test_get_historical_readings_sampling_1h(mock_client):
    base_ts = 1714000000
    # 3 records within the same 1h bucket
    raw_records = [
        {
            "createTime": base_ts + i * 600,
            "temperature": 2400, "fTemperature": 7520,
            "humidity": 5500, "vpdNums": 150,
            "portSpead": 0, "portStatus": 0, "devPortCount": 2,
        }
        for i in range(3)
    ]
    mock_client.get_historical_data.return_value = raw_records
    mock_client.parse_history_record.side_effect = lambda r, port_names=None: {
        "timestamp": f"2024-04-25T00:{(r['createTime'] - base_ts) // 60:02d}:00Z",
        "temperature_c": 24.0,
        "temperature_f": 75.2,
        "humidity": 55.0,
        "vpd": 1.5,
        "ports": [],
    }
    result = await get_historical_readings("C58ZA", "2024-04-25", "2024-04-25", "1h")
    data = json.loads(result)
    assert len(data["readings"]) == 1


async def test_get_historical_readings_statistics_computed(mock_client):
    base_ts = 1714000000
    raw_records = [
        {"createTime": base_ts + i * 3600, "temperature": 2400, "fTemperature": 7520,
         "humidity": 5500, "vpdNums": 150, "portSpead": 0, "portStatus": 0, "devPortCount": 2}
        for i in range(3)
    ]
    mock_client.get_historical_data.return_value = raw_records
    mock_client.parse_history_record.side_effect = lambda r, port_names=None: {
        "timestamp": f"2024-04-25T{(r['createTime'] - base_ts) // 3600:02d}:00:00Z",
        "temperature_c": 24.0, "temperature_f": 75.2,
        "humidity": 55.0, "vpd": 1.5, "ports": [],
    }
    result = await get_historical_readings("C58ZA", "2024-04-25", "2024-04-25", "raw")
    data = json.loads(result)
    stats = data["statistics"]
    assert "temperature" in stats
    assert stats["temperature"]["avg"] == 24.0  # °C unit: parse_history_record returns 24.0°C
    assert "vpd" in stats


# ============ check_vpd_drift ============

async def test_check_vpd_drift_ok(mock_client):
    mock_client.parse_device_data.return_value = {
        **mock_client.parse_device_data.return_value,
        "vpd": 1.24,
    }
    result = await check_vpd_drift("C58ZA", "veg")
    data = json.loads(result)
    assert data["status"] == "OK"
    assert data["alert"] is None
    assert data["deviation"] == 0.0


async def test_check_vpd_drift_low(mock_client):
    mock_client.parse_device_data.return_value = {
        **mock_client.parse_device_data.return_value,
        "vpd": 0.5,
    }
    result = await check_vpd_drift("C58ZA", "veg")
    data = json.loads(result)
    assert data["status"] == "LOW"
    assert "below target" in data["alert"]
    assert data["deviation"] == round(0.5 - 1.0, 2)  # -0.5: below lower bound


async def test_check_vpd_drift_high(mock_client):
    mock_client.parse_device_data.return_value = {
        **mock_client.parse_device_data.return_value,
        "vpd": 2.5,
    }
    result = await check_vpd_drift("C58ZA", "veg")
    data = json.loads(result)
    assert data["status"] == "HIGH"
    assert "exceeds target" in data["alert"]
    assert data["deviation"] == round(2.5 - 1.5, 2)  # 1.0: above upper bound


async def test_check_vpd_drift_unknown_stage_returns_error(mock_client):
    """Unknown stage must return an error, not silently fall back to veg."""
    result = await check_vpd_drift("C58ZA", "bloom")
    data = json.loads(result)
    assert "error" in data
    assert "bloom" in data["error"]
    assert "Unknown stage" in data["error"]


async def test_check_vpd_drift_device_not_found(mock_client):
    result = await check_vpd_drift("NOTEXIST", "veg")
    data = json.loads(result)
    assert "error" in data


async def test_check_vpd_drift_human_summary_ok(mock_client):
    """OK status → human_summary says VPD is on target."""
    mock_client.parse_device_data.return_value = {
        **mock_client.parse_device_data.return_value, "vpd": 1.24,
    }
    result = await check_vpd_drift("C58ZA", "veg")
    data = json.loads(result)
    assert "human_summary" in data
    assert "on target" in data["human_summary"]
    assert "kPa" in data["human_summary"]


async def test_check_vpd_drift_human_summary_high(mock_client):
    """HIGH status → human_summary equals the alert text."""
    mock_client.parse_device_data.return_value = {
        **mock_client.parse_device_data.return_value, "vpd": 2.5,
    }
    result = await check_vpd_drift("C58ZA", "veg")
    data = json.loads(result)
    assert "human_summary" in data
    assert data["human_summary"] == data["alert"]


# ============ _parse_duration_seconds ============

@pytest.mark.parametrize("interval,expected", [
    ("1m", 60),
    ("5m", 300),
    ("15m", 900),
    ("30m", 1800),
    ("1h", 3600),
    ("2h", 7200),
    ("6h", 21600),
    ("12h", 43200),
    ("1d", 86400),
    ("daily", 86400),
])
def test_parse_duration_seconds_valid_values(interval, expected):
    assert _parse_duration_seconds(interval) == expected


@pytest.mark.parametrize("interval", ["2x", "abc", "", "1y", "h1"])
def test_parse_duration_seconds_invalid_raises(interval):
    with pytest.raises(ValueError):
        _parse_duration_seconds(interval)


# ============ _filter_readings_by_time ============

_READINGS = [
    _make_history_record("2024-04-25T08:00:00Z"),
    _make_history_record("2024-04-25T12:00:00Z"),
    _make_history_record("2024-04-25T16:00:00Z"),
    _make_history_record("2024-04-25T20:00:00Z"),
]


def test_filter_readings_by_time_no_filter():
    result, dropped = _filter_readings_by_time(_READINGS)
    assert len(result) == 4
    assert dropped == 0


def test_filter_readings_by_time_start_only():
    result, dropped = _filter_readings_by_time(_READINGS, time_start="12:00")
    assert len(result) == 3
    assert result[0]["timestamp"] == "2024-04-25T12:00:00Z"
    assert dropped == 0


def test_filter_readings_by_time_end_only():
    result, dropped = _filter_readings_by_time(_READINGS, time_end="16:00")
    assert len(result) == 3
    assert result[-1]["timestamp"] == "2024-04-25T16:00:00Z"
    assert dropped == 0


def test_filter_readings_by_time_both():
    result, _ = _filter_readings_by_time(_READINGS, time_start="12:00", time_end="16:00")
    assert len(result) == 2


def test_filter_readings_bad_timestamp_drops_and_counts():
    """Malformed timestamps are dropped and surfaced via the drop count (P3-F017).

    Asserts which record survives (P2-C2-F010) — a regression that swapped the
    include condition (keeping bad records, dropping good) would still satisfy
    the count alone.
    """
    readings = [
        _make_history_record("2024-04-25T12:00:00Z"),
        {"timestamp": "NOT_A_TIMESTAMP", "temperature_c": 24.0},
        {"timestamp": "", "temperature_c": 25.0},
    ]
    result, dropped = _filter_readings_by_time(readings, time_start="10:00")
    assert len(result) == 1
    assert dropped == 2
    assert result[0]["timestamp"] == "2024-04-25T12:00:00Z"


@pytest.mark.parametrize(
    "time_start,time_end,timestamp,should_match",
    [
        # Standard overnight 22:00-06:00: OR of two halves
        ("22:00", "06:00", "2024-04-25T05:00:00Z", True),    # in lower half
        ("22:00", "06:00", "2024-04-25T22:30:00Z", True),    # in upper half
        ("22:00", "06:00", "2024-04-25T12:00:00Z", False),   # midday out
        # Boundary inclusivity in overnight window
        ("22:00", "06:00", "2024-04-25T22:00:00Z", True),    # exact start
        ("22:00", "06:00", "2024-04-25T06:00:00Z", True),    # exact end
        # Equal times (same-day branch): only that exact minute matches
        ("12:00", "12:00", "2024-04-25T12:00:00Z", True),
        ("12:00", "12:00", "2024-04-25T11:59:00Z", False),
        ("12:00", "12:00", "2024-04-25T12:01:00Z", False),
        # Near-full-day same-day window
        ("00:00", "23:59", "2024-04-25T12:00:00Z", True),
        ("00:00", "23:59", "2024-04-25T23:59:00Z", True),
    ],
)
def test_filter_readings_window_boundaries(time_start, time_end, timestamp, should_match):
    """Overnight + same-day window edge cases including equal-times (P2-C2-F008)."""
    readings = [_make_history_record(timestamp)]
    result, _ = _filter_readings_by_time(readings, time_start=time_start, time_end=time_end)
    if should_match:
        assert len(result) == 1
    else:
        assert len(result) == 0


# ============ apply_sampling ============

def test_apply_sampling_raw_passthrough():
    readings = [{"timestamp": "2026-01-01T00:00:00Z", "temperature_c": 24.0}]
    assert apply_sampling(readings, "raw") == readings


def test_apply_sampling_1h_averaging():
    readings = [
        _make_history_record("2024-04-25T10:00:00Z", temp_c=24.0),
        _make_history_record("2024-04-25T10:30:00Z", temp_c=26.0),
        _make_history_record("2024-04-25T10:45:00Z", temp_c=25.0),
        _make_history_record("2024-04-25T11:00:00Z", temp_c=24.0),
    ]
    result = apply_sampling(readings, "1h")
    assert len(result) == 2


def test_apply_sampling_daily_alias():
    readings = [_make_history_record("2024-04-25T12:00:00Z")]
    r1 = apply_sampling(readings, "daily")
    r2 = apply_sampling(readings, "1d")
    assert len(r1) == len(r2)


# ============ average_readings ============

def test_average_readings_empty():
    assert average_readings([]) == {}


def test_average_readings_single():
    reading = _make_history_record("2024-04-25T10:00:00Z", temp_c=24.0, humidity=55.0, vpd=1.5)
    result = average_readings([reading])
    assert result["temperature_c"] == 24.0
    assert result["humidity"] == 55.0
    assert result["vpd"] == 1.5


def test_average_readings_multiple():
    readings = [
        _make_history_record("2024-04-25T10:00:00Z", temp_c=20.0),
        _make_history_record("2024-04-25T10:30:00Z", temp_c=30.0),
    ]
    result = average_readings(readings)
    assert result["temperature_c"] == 25.0


def test_average_readings_with_ports():
    readings = [
        {
            "timestamp": "2024-04-25T10:00:00Z",
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 55.0, "vpd": 1.5,
            "ports": [{"port": 1, "name": "Fan", "speed": 4, "on": True}],
        },
        {
            "timestamp": "2024-04-25T10:30:00Z",
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 55.0, "vpd": 1.5,
            "ports": [{"port": 1, "name": "Fan", "speed": 6, "on": True}],
        },
    ]
    result = average_readings(readings)
    assert len(result["ports"]) == 1
    assert result["ports"][0]["speed"] == 5.0


# ============ get_environment_health ============

async def test_get_environment_health_happy_path(mock_client):
    result = await get_environment_health("C58ZA", "veg")
    data = json.loads(result)
    assert "score" in data
    assert "grade" in data
    assert 0 <= data["score"] <= 100
    assert data["grade"] in ("A", "B", "C", "D", "F")
    assert "top_recommendation" in data
    assert data["device_id"] == "C58ZA"
    assert data["stage"] == "veg"
    assert data["temperature_c"] == pytest.approx(23.5)
    assert data["temperature_f"] == pytest.approx(74.3)
    assert data["humidity_pct"] == pytest.approx(60.0)
    assert data["vpd_kpa"] == pytest.approx(1.24)
    assert "human_summary" in data
    assert "74.3°F" in data["human_summary"]
    assert "23.5°C" in data["human_summary"]
    assert "60%" in data["human_summary"]
    assert "1.24 kPa" in data["human_summary"]
    assert "temperature" not in data or "temperature_c" in data  # old ambiguous field removed
    assert "unit" not in data


async def test_get_environment_health_bad_stage(mock_client):
    result = await get_environment_health("C58ZA", "bloom")
    data = json.loads(result)
    assert "error" in data
    assert "bloom" in data["error"]
    assert "Unknown stage" in data["error"]


async def test_get_environment_health_unknown_device(mock_client):
    result = await get_environment_health("NOTEXIST", "veg")
    data = json.loads(result)
    assert "error" in data
    assert "NOTEXIST" in data["error"]


async def test_get_environment_health_temp_out_of_range(mock_client):
    mock_client.parse_device_data.return_value = {
        **mock_client.parse_device_data.return_value,
        "temperature_c": 35.0,
        "vpd": 1.24,
        "humidity": 60.0,
    }
    result = await get_environment_health("C58ZA", "veg")
    data = json.loads(result)
    assert "temperature" in data["top_recommendation"].lower() or data["temp_score"] < 100


async def test_get_environment_health_vpd_low_recommendation(mock_client):
    mock_client.parse_device_data.return_value = {
        **mock_client.parse_device_data.return_value,
        "vpd": 0.3,
        "temperature_c": 24.0,
        "humidity": 60.0,
    }
    result = await get_environment_health("C58ZA", "veg")
    data = json.loads(result)
    assert "VPD is low" in data["top_recommendation"]


# ============ detect_environment_trends ============

def _make_hourly_readings(n: int = 7) -> list[dict]:
    """Generate n hourly readings for trend tests."""
    from datetime import datetime, timedelta
    base = datetime(2024, 4, 18, 0, 0, 0)
    return [
        {
            "timestamp": (base + timedelta(hours=i)).isoformat() + "Z",
            "temperature_c": 24.0,
            "humidity": 55.0,
            "vpd": 1.4,
            "ports": [],
        }
        for i in range(n)
    ]


async def test_detect_environment_trends_happy_path(mock_client):
    readings = _make_hourly_readings(7)
    mock_client.get_historical_data.return_value = [{}] * 7
    mock_client.parse_history_record.side_effect = lambda r, port_names=None: readings[0]

    hist_payload = json.dumps({
        "device_id": "C58ZA",
        "readings": readings,
        "statistics": {},
    })

    with patch("ac_infinity_mcp.server.get_historical_readings",
               return_value=hist_payload):
        result = await detect_environment_trends("C58ZA", 7)

    data = json.loads(result)
    assert data["device_id"] == "C58ZA"
    assert data["days_analyzed"] == 7
    assert len(data["trends"]) == 3
    for trend in data["trends"]:
        assert "metric" in trend
        assert "slope" in trend
        assert "direction" in trend
        assert "alert" in trend


async def test_detect_environment_trends_days_zero(mock_client):
    result = await detect_environment_trends("C58ZA", 0)
    data = json.loads(result)
    assert "error" in data
    assert "days must be between 1 and 30" in data["error"]


async def test_detect_environment_trends_days_thirty_one(mock_client):
    result = await detect_environment_trends("C58ZA", 31)
    data = json.loads(result)
    assert "error" in data
    assert "days must be between 1 and 30" in data["error"]


async def test_detect_environment_trends_historical_error_propagated(mock_client):
    # detect_environment_trends now bypasses get_historical_readings; device-not-found
    # is detected by get_devices returning an empty list for the device_id.
    mock_client.get_devices.return_value = []
    result = await detect_environment_trends("NOTEXIST", 7)
    data = json.loads(result)
    assert "error" in data


async def test_detect_environment_trends_single_reading_flat(mock_client):
    # detect_environment_trends now calls the client directly (no get_historical_readings).
    single = _make_hourly_readings(1)[0]
    mock_client.get_historical_data.return_value = [{}]
    mock_client.parse_history_record.side_effect = lambda r, port_names=None: single
    result = await detect_environment_trends("C58ZA", 1)
    data = json.loads(result)
    assert data["readings_used"] == 1
    for trend in data["trends"]:
        assert trend["slope"] == 0.0
        assert trend["direction"] == "flat"


async def test_detect_environment_trends_human_summary_table(mock_client):
    """human_summary is a markdown table with all three metrics."""
    readings = _make_hourly_readings(7)
    mock_client.get_historical_data.return_value = [{}] * 7
    mock_client.parse_history_record.side_effect = lambda r, port_names=None: readings[0]
    result = await detect_environment_trends("C58ZA", 7)
    data = json.loads(result)
    assert "human_summary" in data
    summary = data["human_summary"]
    assert "| Metric | Direction | Slope | 7-Day Projection |" in summary
    assert "Temperature" in summary
    assert "Humidity" in summary


# ============ get_port_activity_report ============

def _make_port_readings(n: int, speed: int, on: bool) -> list[dict]:
    from datetime import datetime, timedelta
    base = datetime(2024, 4, 18, 0, 0, 0)
    return [
        {
            "timestamp": (base + timedelta(hours=i)).isoformat() + "Z",
            "temperature_c": 24.0,
            "humidity": 55.0,
            "vpd": 1.4,
            "ports": [{"port": 1, "name": "Inline Fan", "speed": speed, "on": on}],
        }
        for i in range(n)
    ]


async def test_get_port_activity_report_happy_path(mock_client):
    # get_port_activity_report now calls get_devices() + get_historical_data() directly.
    readings = _make_port_readings(24, speed=5, on=True)
    mock_client.get_historical_data.return_value = [{}] * 24
    mock_client.parse_history_record.side_effect = (
        lambda r, port_names=None: readings[0]
    )
    result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    assert data["device_id"] == "C58ZA"
    assert data["days_analyzed"] == 1
    assert len(data["ports"]) == 1
    port = data["ports"][0]
    assert "on_hours" in port
    assert "uptime_pct" in port
    assert "transitions" in port


async def test_get_port_activity_report_days_zero(mock_client):
    result = await get_port_activity_report("C58ZA", 0)
    data = json.loads(result)
    assert "error" in data
    assert "days must be between 1 and 30" in data["error"]


async def test_get_port_activity_report_days_thirty_one(mock_client):
    result = await get_port_activity_report("C58ZA", 31)
    data = json.loads(result)
    assert "error" in data
    assert "days must be between 1 and 30" in data["error"]


async def test_get_port_activity_report_no_ports(mock_client):
    no_port_reading = {
        "timestamp": "2024-04-18T00:00:00Z",
        "temperature_c": 24.0,
        "humidity": 55.0,
        "vpd": 1.4,
        "ports": [],
    }
    mock_client.get_historical_data.return_value = [{}]
    mock_client.parse_history_record.side_effect = (
        lambda r, port_names=None: no_port_reading
    )
    result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    assert data["ports"] == []
    assert "verify that your devices" in data["human_summary"]


async def test_get_port_activity_report_port_always_off(mock_client):
    readings = _make_port_readings(24, speed=0, on=False)
    mock_client.get_historical_data.return_value = [{}] * 24
    mock_client.parse_history_record.side_effect = (
        lambda r, port_names=None: readings[0]
    )
    result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    port = data["ports"][0]
    assert port["uptime_pct"] == 0.0
    assert port["on_hours"] == 0.0
    assert port["avg_speed_when_running"] == 0.0
    assert port["peak_hour_local"] is None


async def test_get_port_activity_report_port_always_on(mock_client):
    readings = _make_port_readings(24, speed=5, on=True)
    mock_client.get_historical_data.return_value = [{}] * 24
    mock_client.parse_history_record.side_effect = (
        lambda r, port_names=None: readings[0]
    )
    result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    port = data["ports"][0]
    assert port["uptime_pct"] == 100.0
    assert port["avg_speed_when_running"] == 5.0


async def test_get_port_activity_report_cumulative_on_hours_multi_day(mock_client):
    """100% uptime across 7 days → on_hours = 168.0, not 24.0."""
    readings = _make_port_readings(24, speed=5, on=True)
    mock_client.get_historical_data.return_value = [{}] * 24
    mock_client.parse_history_record.side_effect = (
        lambda r, port_names=None: readings[0]
    )
    result = await get_port_activity_report("C58ZA", 7)
    data = json.loads(result)
    port = data["ports"][0]
    assert port["on_hours"] == pytest.approx(168.0)
    assert port["off_hours"] == pytest.approx(0.0)
    assert port["uptime_pct"] == 100.0


# ============ get_port_activity_report — ghost port filter (#86) ============

def _make_port_readings_named(n: int, speed: int, on: bool, name: str, port: int = 1) -> list[dict]:
    """Like _make_port_readings but with a configurable port name and port number."""
    from datetime import datetime, timedelta
    base = datetime(2024, 4, 18, 0, 0, 0)
    return [
        {
            "timestamp": (base + timedelta(hours=i)).isoformat() + "Z",
            "temperature_c": 24.0,
            "humidity": 55.0,
            "vpd": 1.4,
            "ports": [{"port": port, "name": name, "speed": speed, "on": on}],
        }
        for i in range(n)
    ]


async def test_get_port_activity_report_has_new_fields(mock_client):
    """Response includes ports_excluded_count, human_summary, and window fields."""
    readings = _make_port_readings(24, speed=5, on=True)
    mock_client.get_historical_data.return_value = [{}] * 24
    mock_client.parse_history_record.side_effect = (
        lambda r, port_names=None: readings[0]
    )
    result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    assert "ports_excluded_count" in data
    assert "human_summary" in data
    assert "window_start_local" in data
    assert "window_end_local" in data
    assert isinstance(data["ports_excluded_count"], int)
    assert isinstance(data["human_summary"], str)
    assert isinstance(data["window_start_local"], str)
    assert isinstance(data["window_end_local"], str)


async def test_get_port_activity_report_rule_a_ghost_excluded(mock_client):
    """Rule A: constant 100% uptime + portsLoad=0 → port excluded."""
    # Port 1 "Port 1": always on, 0 transitions, portsLoad=0 in device info
    mock_client.get_devices.return_value = [{
        "devCode": "C58ZA",
        "devId": "9999999999",
        "deviceInfo": {
            "ports": [{"port": 1, "portsLoad": 0}],
        },
    }]
    readings = _make_port_readings_named(24, speed=5, on=True, name="Port 1", port=1)
    mock_client.get_historical_data.return_value = [{}] * 24
    mock_client.parse_history_record.side_effect = (
        lambda r, port_names=None: readings[0]
    )
    result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    assert data["ports"] == []
    assert data["ports_excluded_count"] == 1
    # When all ports are filtered, human_summary reports no active activity with exclusion count
    assert "No active port activity" in data["human_summary"]
    assert "1 port excluded" in data["human_summary"]
    assert "verify that your devices" not in data["human_summary"]


async def test_get_port_activity_report_rule_a_not_excluded_with_load(mock_client):
    """Rule A does NOT exclude a port that has portsLoad > 0."""
    # mock_client already returns MOCK_DEVICE_LEGACY which has port 1 portsLoad=1
    readings = _make_port_readings_named(24, speed=5, on=True, name="Port 1", port=1)
    mock_client.get_historical_data.return_value = [{}] * 24
    mock_client.parse_history_record.side_effect = (
        lambda r, port_names=None: readings[0]
    )
    result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    # Port 1 has portsLoad=1 in MOCK_DEVICE_LEGACY → Rule A does not fire
    assert len(data["ports"]) == 1
    assert data["ports_excluded_count"] == 0


async def test_get_port_activity_report_all_ports_excluded(mock_client):
    """All ports excluded → empty ports list with informative human_summary."""
    # Two auto-named ports with < 1 hour/day activity over 3 days
    # 2 on out of 72 total → on_hours/days = (2/72 * 24 * 3) / 3 = 0.67 < 1.0
    from datetime import datetime, timedelta
    base = datetime(2024, 4, 18, 0, 0, 0)
    readings = []
    for i in range(72):
        on = i < 2  # first 2 readings on, rest off
        readings.append({
            "timestamp": (base + timedelta(hours=i)).isoformat() + "Z",
            "temperature_c": 24.0,
            "humidity": 55.0,
            "vpd": 1.4,
            "ports": [
                {"port": 2, "name": "Port 2", "speed": 5 if on else 0, "on": on},
                {"port": 3, "name": "Port 3", "speed": 5 if on else 0, "on": on},
            ],
        })
    mock_client.get_devices.return_value = [{
        "devCode": "C58ZA",
        "devId": "9999999999",
        "deviceInfo": {
            "ports": [
                {"port": 2, "portsLoad": 0},
                {"port": 3, "portsLoad": 0},
            ],
        },
    }]
    mock_client.get_historical_data.return_value = [{}] * 72
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = readings[idx]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 3)
    data = json.loads(result)
    assert data["ports"] == []
    assert data["ports_excluded_count"] == 2
    # human_summary should describe 0 active ports and include the exclusion count
    assert "No active port activity" in data["human_summary"]
    assert "2 ports excluded" in data["human_summary"]
    assert "verify that your devices" not in data["human_summary"]
    assert "window_start_local" in data
    assert "window_end_local" in data


async def test_get_port_activity_report_partial_exclusion(mock_client):
    """One active port kept, one auto-named low-activity port excluded."""
    from datetime import datetime, timedelta
    base = datetime(2024, 4, 18, 0, 0, 0)
    readings = []
    for i in range(72):
        # Port 1 "Inline Fan": on for all 72 readings (high activity)
        # Port 2 "Port 2": on for first 2 readings only (low activity)
        p2_on = i < 2
        readings.append({
            "timestamp": (base + timedelta(hours=i)).isoformat() + "Z",
            "temperature_c": 24.0,
            "humidity": 55.0,
            "vpd": 1.4,
            "ports": [
                {"port": 1, "name": "Inline Fan", "speed": 5, "on": True},
                {"port": 2, "name": "Port 2", "speed": 5 if p2_on else 0, "on": p2_on},
            ],
        })
    # Port 1 has load > 0, port 2 has load = 0 (not relevant — Rule B fires first for Port 2)
    mock_client.get_devices.return_value = [{
        "devCode": "C58ZA",
        "devId": "9999999999",
        "deviceInfo": {
            "ports": [
                {"port": 1, "portsLoad": 5},
                {"port": 2, "portsLoad": 0},
            ],
        },
    }]
    mock_client.get_historical_data.return_value = [{}] * 72
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = readings[idx]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 3)
    data = json.loads(result)
    assert len(data["ports"]) == 1
    assert data["ports"][0]["name"] == "Inline Fan"
    assert data["ports_excluded_count"] == 1
    assert "1 port excluded" in data["human_summary"]


# ============ get_port_activity_report — Rule E (#101) ============

async def test_get_port_activity_report_rule_e_stale_speed_phantom(mock_client):
    """Rule E: named port 'Filter' (port 4), speed=5, transitions=1, portsLoad=0, sub-threshold
    runtime → port excluded (ports==[], ports_excluded_count==1).

    Reproduces Issue #101: the history API records the previously-configured speed even after
    the port is set to OFF, producing phantom records that pass Rules A–D.

    3 on-readings out of 72 total over 3 days → on_hours = 3/72*24*3 = 3.0h; 3.0/3 = 1.0 h/day.
    Wait — we need strictly < 1.0 h/day. Use 2 on-readings:
    2/72*24*3 = 2.0h; 2.0/3 = 0.667 h/day < 1.0 → Rule E fires.
    """
    from datetime import datetime, timedelta
    base = datetime(2024, 4, 18, 0, 0, 0)
    total = 72  # 3 days × 24 readings/day
    readings = []
    for i in range(total):
        on = i < 2  # first 2 readings on, rest off
        readings.append({
            "timestamp": (base + timedelta(hours=i)).isoformat() + "Z",
            "temperature_c": 24.0,
            "humidity": 55.0,
            "vpd": 1.4,
            "ports": [{"port": 4, "name": "Filter", "speed": 5 if on else 0, "on": on}],
        })
    mock_client.get_devices.return_value = [{
        "devCode": "C58ZA",
        "devId": "9999999999",
        "deviceInfo": {
            "ports": [{"port": 4, "name": "Filter", "portsLoad": 0, "loadType": 0}],
        },
    }]
    mock_client.get_historical_data.return_value = [{}] * total
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = readings[idx]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 3)
    data = json.loads(result)
    assert data["ports"] == [], "Rule E must exclude the stale-speed phantom port"
    assert data["ports_excluded_count"] == 1
    assert "1 port excluded" in data["human_summary"]
    assert "verify that your devices" not in data["human_summary"]


# ============ get_port_activity_report — data_quality (#85) ============

def _make_toggle_port_readings(n: int, port_num: int = 2, name: str = "Heater") -> list[dict]:
    """All readings have speed=1/on=True (the 0xF toggle-nibble artifact)."""
    from datetime import datetime, timedelta
    base = datetime(2024, 4, 18, 0, 0, 0)
    return [
        {
            "timestamp": (base + timedelta(hours=i)).isoformat() + "Z",
            "temperature_c": 22.0,
            "humidity": 55.0,
            "vpd": 1.2,
            "ports": [{"port": port_num, "name": name, "speed": 1, "on": True}],
        }
        for i in range(n)
    ]


def _make_toggle_device(port_num: int = 2, load_type: int = 4, ports_load: int = 5) -> dict:
    """Device fixture with a toggle-hardware port (loadType 4 or 128)."""
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["deviceInfo"]["ports"] = [
        {"port": 1, "portName": "Exhaust Fan", "portsLoad": 5, "loadType": 0, "speak": 5},
        {"port": port_num, "portName": "Heater", "portsLoad": ports_load, "loadType": load_type,
         "speak": ports_load},
    ]
    return device


async def test_get_port_activity_report_data_quality_not_in_output(mock_client):
    """Port dicts do not expose the internal data_quality field."""
    readings = _make_port_readings(24, speed=5, on=True)
    mock_client.get_historical_data.return_value = [{}] * 24
    mock_client.parse_history_record.side_effect = lambda r, port_names=None: readings[0]
    result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    assert len(data["ports"]) == 1
    assert "data_quality" not in data["ports"][0]


async def test_get_port_activity_report_data_quality_caveat_human_summary(mock_client):
    """Toggle-hardware port (loadType=4, portsLoad>0) → caveat appears in human_summary."""
    mock_client.get_devices.return_value = [_make_toggle_device()]
    readings = _make_toggle_port_readings(72)
    mock_client.get_historical_data.return_value = [{}] * 72
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = readings[idx % len(readings)]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 3)
    data = json.loads(result)

    # The Heater should be present without data_quality exposed
    heater_ports = [p for p in data["ports"] if p["name"] == "Heater"]
    assert len(heater_ports) == 1, "Heater (portsLoad>0) must not be filtered"
    assert "data_quality" not in heater_ports[0]

    # human_summary must mention the ▎ caveat, not quote uptime
    summary = data["human_summary"]
    assert "▎" in summary
    assert "Currently ON: Heater (Port 2)" in summary  # portsLoad=5 → speak=5 → ON
    assert "Heater (Port 2)" in summary


async def test_get_port_activity_report_reliable_ports_shown_normally(mock_client):
    """Reliable ports still appear with uptime in human_summary alongside caveat port."""
    mock_client.get_devices.return_value = [_make_toggle_device()]

    # Build combined readings with both ports in each record
    from datetime import datetime, timedelta
    base = datetime(2024, 4, 18, 0, 0, 0)
    combined = [
        {
            "timestamp": (base + timedelta(hours=i)).isoformat() + "Z",
            "temperature_c": 22.0,
            "humidity": 55.0,
            "vpd": 1.2,
            "ports": [
                {"port": 1, "name": "Exhaust Fan", "speed": 5, "on": True},
                {"port": 2, "name": "Heater", "speed": 1, "on": True},
            ],
        }
        for i in range(72)
    ]
    mock_client.get_historical_data.return_value = [{}] * 72
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = combined[idx % len(combined)]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 3)
    data = json.loads(result)

    summary = data["human_summary"]
    # Reliable fan should appear with uptime
    assert "Exhaust Fan (Port 1)" in summary
    assert "uptime" in summary
    # Caveat heater should appear with new grouped format
    assert "Currently ON: Heater (Port 2)" in summary


async def test_get_port_activity_report_data_quality_currently_off(mock_client):
    """Toggle hardware (loadType=4) with portsLoad=0 survives Rule D with caveat 'currently OFF'."""
    mock_client.get_devices.return_value = [_make_toggle_device(ports_load=0)]
    readings = _make_toggle_port_readings(72)
    mock_client.get_historical_data.return_value = [{}] * 72
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = readings[idx % len(readings)]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 3)
    data = json.loads(result)
    # Toggle hardware survives Rule D exemption — appears with ▎ caveat showing "Currently OFF"
    heater_ports = [p for p in data["ports"] if p["name"] == "Heater"]
    assert len(heater_ports) == 1, "Toggle hardware must NOT be filtered even when portsLoad=0"
    assert "data_quality" not in heater_ports[0]
    assert "▎" in data["human_summary"]
    assert "Currently OFF:" in data["human_summary"]


async def test_get_port_activity_report_all_caveat_human_summary(mock_client):
    """When every port is a toggle-caveat port, summary omits the uptime line."""
    # Only a heater port — the exhaust fan port is removed from this device fixture
    device = _make_toggle_device()
    device["deviceInfo"]["ports"] = [
        {"port": 2, "portName": "Heater", "portsLoad": 5, "loadType": 4},
    ]
    mock_client.get_devices.return_value = [device]
    readings = _make_toggle_port_readings(72)
    mock_client.get_historical_data.return_value = [{}] * 72
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = readings[idx % len(readings)]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 3)
    data = json.loads(result)
    summary = data["human_summary"]
    # Summary should not have orphaned "." from empty port_lines
    assert " ." not in summary
    # Heater has portsLoad=5 but speak field is absent (defaults to 0 → OFF)
    assert "Currently OFF: Heater (Port 2)" in summary


async def test_get_port_activity_report_ports_excluded_count_unchanged_with_caveat(mock_client):
    """ports_excluded_count reflects filtered ports only, not caveat ports."""
    # Device: port 1 (fan, reliable), port 2 (heater, toggle/caveat), port 3 (auto-named, filtered)
    device = _make_toggle_device()
    device["deviceInfo"]["ports"] = [
        {"port": 1, "portName": "Exhaust Fan", "portsLoad": 5, "loadType": 0},
        {"port": 2, "portName": "Heater", "portsLoad": 5, "loadType": 4},
        {"port": 3, "portName": "Port 3", "portsLoad": 0, "loadType": 0},
    ]
    mock_client.get_devices.return_value = [device]
    from datetime import datetime, timedelta
    base = datetime(2024, 4, 18, 0, 0, 0)
    combined = [
        {
            "timestamp": (base + timedelta(hours=i)).isoformat() + "Z",
            "temperature_c": 22.0,
            "humidity": 55.0,
            "vpd": 1.2,
            "ports": [
                {"port": 1, "name": "Exhaust Fan", "speed": 5, "on": True},
                {"port": 2, "name": "Heater", "speed": 1, "on": True},
                {"port": 3, "name": "Port 3", "speed": 1, "on": True},
            ],
        }
        for i in range(72)
    ]
    mock_client.get_historical_data.return_value = [{}] * 72
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = combined[idx % len(combined)]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 3)
    data = json.loads(result)
    # Port 3 (Port N, avg speed=1<=1, portsLoad=0) → Rule D filters it → excluded_count=1
    # Port 2 (Heater, toggle, portsLoad=5) → ▎ caveat in human_summary, no data_quality in JSON
    # Port 1 (Exhaust Fan, speed=5, portsLoad=5) → reliable → in ports
    assert data["ports_excluded_count"] == 1
    port_names = [p["name"] for p in data["ports"]]
    assert "Exhaust Fan" in port_names
    assert "Heater" in port_names
    heater = next(p for p in data["ports"] if p["name"] == "Heater")
    assert "data_quality" not in heater
    assert "▎" in data["human_summary"]


def _make_devtype18_device(ports: list[dict]) -> dict:
    """Device fixture with devType=18 (8T4TC — always reports portsLoad=0)."""
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["devType"] = 18
    device["deviceInfo"]["ports"] = ports
    return device


def _make_devtype18_port_readings(
    port_num: int, name: str, speed: int, on_count: int, total: int
) -> list[dict]:
    """Build readings for a single named port: on_count on-readings then off."""
    from datetime import datetime, timedelta
    base = datetime(2024, 4, 18, 0, 0, 0)
    readings = []
    for i in range(on_count):
        readings.append({
            "timestamp": (base + timedelta(hours=i)).isoformat() + "Z",
            "temperature_c": 22.0, "humidity": 55.0, "vpd": 1.2,
            "ports": [{"port": port_num, "name": name, "speed": speed, "on": True}],
        })
    for i in range(total - on_count):
        readings.append({
            "timestamp": (base + timedelta(hours=on_count + i)).isoformat() + "Z",
            "temperature_c": 22.0, "humidity": 55.0, "vpd": 1.2,
            "ports": [{"port": port_num, "name": name, "speed": 0, "on": False}],
        })
    return readings


def _make_devtype22_device(ports: list[dict]) -> dict:
    """Device fixture with devType=22 (Q0KT4 — always reports portsLoad=None/0)."""
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["devType"] = 22
    device["deviceInfo"]["ports"] = ports
    return device


async def test_get_port_activity_report_devtype18_no_load_signal_port_in_output(mock_client):
    """devType=18: named port with short runtime appears in output without data_quality field."""
    # 4 on out of 288 total over 3 days → 0.333 h/day; Rule E would filter on devType=11
    device = _make_devtype18_device([
        {"port": 3, "portName": "Left Fan", "portsLoad": 0, "loadType": 0},
    ])
    mock_client.get_devices.return_value = [device]
    readings = _make_devtype18_port_readings(3, "Left Fan", speed=5, on_count=4, total=288)
    mock_client.get_historical_data.return_value = [{}] * 288
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = readings[idx % len(readings)]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 3)
    data = json.loads(result)

    left_fan = next((p for p in data["ports"] if p["name"] == "Left Fan"), None)
    assert left_fan is not None, "Left Fan must appear in output for devType=18"
    assert "data_quality" not in left_fan, "data_quality must not be exposed in port JSON"
    assert data["ports_excluded_count"] == 0, "No ports should be excluded via load-based rules"


async def test_get_port_activity_report_devtype18_no_load_signal_in_human_summary(mock_client):
    """devType=18: Left Fan appears in port_lines and device-level note in human_summary."""
    device = _make_devtype18_device([
        {"port": 3, "portName": "Left Fan", "portsLoad": 0, "loadType": 0},
    ])
    mock_client.get_devices.return_value = [device]
    readings = _make_devtype18_port_readings(3, "Left Fan", speed=5, on_count=4, total=288)
    mock_client.get_historical_data.return_value = [{}] * 288
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = readings[idx % len(readings)]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 3)
    data = json.loads(result)
    summary = data["human_summary"]

    assert "Left Fan (Port 3)" in summary, "Port uptime line must appear in human_summary"
    assert "uptime" in summary, "Runtime data must appear in human_summary"
    assert "does not report power draw" not in summary  # note must not appear for devType=18


async def test_get_port_activity_report_devtype18_toggle_still_api_constant_speed(mock_client):
    """devType=18 toggle port: ▎ caveat in summary, no data_quality in JSON."""
    device = _make_devtype18_device([
        {"port": 2, "portName": "Heater", "portsLoad": 0, "loadType": 4},
    ])
    mock_client.get_devices.return_value = [device]
    readings = _make_toggle_port_readings(72)
    mock_client.get_historical_data.return_value = [{}] * 72
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = readings[idx % len(readings)]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 3)
    data = json.loads(result)

    heater = next((p for p in data["ports"] if p["name"] == "Heater"), None)
    assert heater is not None
    assert "data_quality" not in heater
    assert "▎" in data["human_summary"]
    assert "Currently OFF: Heater (Port 2)" in data["human_summary"]
    # Note only appears for devType=22 after #151 — devType=18 suppressed
    assert "does not report power draw" not in data["human_summary"]


async def test_get_port_activity_report_devtype18_note_absent_with_active_ports(mock_client):
    """devType=18 with active ports: 'does not report power draw' note must not appear (#151)."""
    device = _make_devtype18_device([
        {"port": 3, "portName": "Left Fan", "portsLoad": 0, "loadType": 0},
    ])
    mock_client.get_devices.return_value = [device]
    readings = _make_devtype18_port_readings(3, "Left Fan", speed=5, on_count=12, total=288)
    mock_client.get_historical_data.return_value = [{}] * 288
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = readings[idx % len(readings)]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 3)
    data = json.loads(result)

    assert "does not report power draw" not in data["human_summary"]
    assert len(data["ports"]) > 0  # vacuity guard


async def test_get_port_activity_report_devtype11_rule_e_still_filters(mock_client):
    """devType=11 (non-18): Rule E still filters a named port with stale speed."""
    # devType=11 is the MOCK_DEVICE_LEGACY default; Rule E must apply normally
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)  # devType=11
    device["deviceInfo"]["ports"] = [
        {"port": 3, "portName": "Left Fan", "portsLoad": 0, "loadType": 0},
    ]
    mock_client.get_devices.return_value = [device]
    readings = _make_devtype18_port_readings(3, "Left Fan", speed=5, on_count=4, total=288)
    mock_client.get_historical_data.return_value = [{}] * 288
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = readings[idx % len(readings)]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 3)
    data = json.loads(result)

    assert len(data["ports"]) == 0, "devType=11 must still filter via Rule E"
    assert data["ports_excluded_count"] == 1
    assert "does not report power draw" not in data["human_summary"]


async def test_get_port_activity_report_caveat_line_speak_based_on_off(mock_client):
    """Caveat line ON/OFF uses speak field, not portsLoad — correct for devType=18/22."""
    # Toggle port on devType=18: portsLoad=0 (Quirk 24) but speak=1 means device is running
    device = _make_devtype18_device([
        {"port": 2, "portName": "Heater", "portsLoad": 0, "loadType": 4, "speak": 1},
    ])
    mock_client.get_devices.return_value = [device]
    readings = _make_toggle_port_readings(24)
    mock_client.get_historical_data.return_value = [{}] * 24
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = readings[idx % len(readings)]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    assert "Currently ON: Heater (Port 2)" in data["human_summary"], (
        "speak=1 must yield 'Currently ON' grouped caveat"
    )

    # Invalidate cache so the next call fetches the updated mock (speak=0 device).
    # Within a single test function the autouse fixture doesn't run between sub-cases.
    import ac_infinity_mcp.server as _srv
    _srv._invalidate_device_cache()

    # speak=0 → Currently OFF (portsLoad still 0; speak is the authoritative signal)
    device_off = _make_devtype18_device([
        {"port": 2, "portName": "Heater", "portsLoad": 0, "loadType": 4, "speak": 0},
    ])
    mock_client.get_devices.return_value = [device_off]
    idx = 0
    result_off = await get_port_activity_report("C58ZA", 1)
    data_off = json.loads(result_off)
    assert "Currently OFF: Heater (Port 2)" in data_off["human_summary"], (
        "speak=0 must yield 'Currently OFF' grouped caveat"
    )


async def test_get_port_activity_report_devtype22_ports_not_excluded(mock_client):
    """devType=22: named ports with portsLoad=None→0 are not ghost-filtered (Issue #128)."""
    device = _make_devtype22_device([
        {"port": 1, "portName": "R1 Clone Heat Pad", "portsLoad": None,
         "loadType": 132, "speak": 1},
        {"port": 2, "portName": "R1 Clone Lights", "portsLoad": None,
         "loadType": 129, "speak": 1},
    ])
    mock_client.get_devices.return_value = [device]
    readings = _make_devtype18_port_readings(2, "R1 Clone Lights", speed=1, on_count=12, total=24)
    mock_client.get_historical_data.return_value = [{}] * 24
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = readings[idx % len(readings)]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)

    assert data["ports_excluded_count"] == 0, "devType=22 ports must not be ghost-filtered"
    assert "does not report power draw" in data["human_summary"], (
        "Device-level no_load_signal note must appear for devType=22"
    )


# ============ get_port_activity_report — #142 grouped caveat, #143 Rule G ============


def _make_two_port_toggle_readings(
    port2_name: str = "Heater",
    port3_name: str = "Humidifier",
    total: int = 24,
) -> list[dict]:
    """Build readings for two toggle ports: 100% uptime, speed=1 — triggers api_constant_speed."""
    from datetime import datetime, timedelta
    base = datetime(2024, 4, 18, 0, 0, 0)
    return [
        {
            "timestamp": (base + timedelta(hours=i)).isoformat() + "Z",
            "temperature_c": 22.0, "humidity": 55.0, "vpd": 1.2,
            "ports": [
                {"port": 2, "name": port2_name, "speed": 1, "on": True},
                {"port": 3, "name": port3_name, "speed": 1, "on": True},
            ],
        }
        for i in range(total)
    ]


async def test_get_port_activity_report_caveat_on_off_grouped_devtype22(mock_client):
    """#142: Two caveat ports — one ON, one OFF — produce grouped ▎ lines."""
    device = _make_devtype22_device([
        {"port": 2, "portName": "Heater", "portsLoad": None, "loadType": 4, "speak": 1},
        {"port": 3, "portName": "Humidifier", "portsLoad": None, "loadType": 4, "speak": 0},
    ])
    mock_client.get_devices.return_value = [device]
    readings = _make_two_port_toggle_readings("Heater", "Humidifier")
    mock_client.get_historical_data.return_value = [{}] * 24
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = readings[idx % len(readings)]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    summary = data["human_summary"]

    assert "Currently ON: Heater (Port 2)" in summary, (
        "#142: speak=1 port must appear in ON group"
    )
    assert "Currently OFF: Humidifier (Port 3)" in summary, (
        "#142: speak=0 port must appear in OFF group"
    )
    assert "Activity data not supported" not in summary, (
        "#142: old per-port format must be gone"
    )


async def test_caveat_all_on_format(mock_client):
    """#142: All caveat ports ON → 'Currently ON:' present, 'Currently OFF:' absent."""
    device = _make_devtype22_device([
        {"port": 2, "portName": "Heater", "portsLoad": None, "loadType": 4, "speak": 1},
        {"port": 3, "portName": "Humidifier", "portsLoad": None, "loadType": 4, "speak": 1},
    ])
    mock_client.get_devices.return_value = [device]
    readings = _make_two_port_toggle_readings("Heater", "Humidifier")
    mock_client.get_historical_data.return_value = [{}] * 24
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = readings[idx % len(readings)]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    summary = data["human_summary"]

    assert "Currently ON:" in summary, "#142: all-ON caveat must show 'Currently ON:'"
    assert "Currently OFF:" not in summary, "#142: no OFF group when all ports are ON"


async def test_caveat_all_off_format(mock_client):
    """#142: All caveat ports OFF → 'Currently OFF:' present, 'Currently ON:' absent."""
    device = _make_devtype22_device([
        {"port": 2, "portName": "Heater", "portsLoad": None, "loadType": 4, "speak": 0},
        {"port": 3, "portName": "Humidifier", "portsLoad": None, "loadType": 4, "speak": 0},
    ])
    mock_client.get_devices.return_value = [device]
    readings = _make_two_port_toggle_readings("Heater", "Humidifier")
    mock_client.get_historical_data.return_value = [{}] * 24
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = readings[idx % len(readings)]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    summary = data["human_summary"]

    assert "Currently OFF:" in summary, "#142: all-OFF caveat must show 'Currently OFF:'"
    assert "Currently ON:" not in summary, "#142: no ON group when all ports are OFF"


async def test_note_mentions_on_off_reliable_devtype22(mock_client):
    """#142/#143: devType=22 Note mentions ON/OFF state as the only reliable indicator."""
    device = _make_devtype22_device([
        {"port": 2, "portName": "R1 Clone Lights", "portsLoad": None, "loadType": 129, "speak": 1},
    ])
    mock_client.get_devices.return_value = [device]
    readings = _make_devtype18_port_readings(2, "R1 Clone Lights", speed=1, on_count=12, total=24)
    mock_client.get_historical_data.return_value = [{}] * 24
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = readings[idx % len(readings)]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    assert "ON/OFF state is the only reliable activity indicator" in data["human_summary"], (
        "#142: devType=22 Note must mention ON/OFF reliability"
    )


async def test_note_mentions_on_off_reliable_devtype18(mock_client):
    """#151: devType=18 Note is suppressed after fix — only devType=22 emits the Note."""
    device = _make_devtype18_device([
        {"port": 3, "portName": "Left Fan", "portsLoad": 0, "loadType": 0},
    ])
    mock_client.get_devices.return_value = [device]
    readings = _make_devtype18_port_readings(3, "Left Fan", speed=5, on_count=4, total=288)
    mock_client.get_historical_data.return_value = [{}] * 288
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = readings[idx % len(readings)]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 3)
    data = json.loads(result)
    assert "ON/OFF state is the only reliable activity indicator" not in data["human_summary"], (
        "#151: devType=18 Note must not appear — only devType=22 emits the Note"
    )


async def test_preamble_no_active_when_zero_load_devtype(mock_client):
    """#142/#143: devType=22 preamble says 'N ports' not 'N active ports'."""
    device = _make_devtype22_device([
        {"port": 2, "portName": "R1 Clone Lights", "portsLoad": None, "loadType": 129, "speak": 1},
    ])
    mock_client.get_devices.return_value = [device]
    readings = _make_devtype18_port_readings(2, "R1 Clone Lights", speed=1, on_count=12, total=24)
    mock_client.get_historical_data.return_value = [{}] * 24
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = readings[idx % len(readings)]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    assert "active ports" not in data["human_summary"], (
        "#142: devType=22 preamble must not say 'active ports'"
    )


async def test_exclusion_reason_no_activity_detected(mock_client):
    """#143: devType=18 excluded ports show 'no activity detected' with port name."""
    # Humidifier on devType=18: speed=1, 1/48 on-readings → on_hours=0.5/day → Rule G excludes
    device = _make_devtype18_device([
        {"port": 1, "portName": "Humidifier", "portsLoad": 0, "loadType": 0},
        {"port": 2, "portName": "Exhaust Fan", "portsLoad": 0, "loadType": 0},
    ])
    mock_client.get_devices.return_value = [device]
    from datetime import datetime, timedelta
    base = datetime(2024, 4, 18, 0, 0, 0)
    # Port 1 (Humidifier): 1 on-reading at speed=1, then 47 off → on_hours = 1/48*24 = 0.5 < 1.0
    # Port 2 (Exhaust Fan): speed=5, 12 on + 12 off → on_hours=12.0 ≥ 1.0 → kept
    all_readings = []
    for i in range(48):
        ports = [
            {"port": 1, "name": "Humidifier", "speed": 1 if i == 0 else 0, "on": i == 0},
            {"port": 2, "name": "Exhaust Fan", "speed": 5 if i < 12 else 0, "on": i < 12},
        ]
        all_readings.append({
            "timestamp": (base + timedelta(hours=i)).isoformat() + "Z",
            "temperature_c": 22.0, "humidity": 55.0, "vpd": 1.2,
            "ports": ports,
        })
    mock_client.get_historical_data.return_value = [{}] * 48
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = all_readings[idx % len(all_readings)]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    summary = data["human_summary"]

    assert "no activity detected" in summary, (
        "#143: exclusion reason must say 'no activity detected'"
    )
    assert "Humidifier (Port 1)" in summary, (
        "#143: excluded port name must appear in exclusion text"
    )
    assert data["ports_excluded_count"] == 1, "#143: Humidifier must be excluded by Rule G"


async def test_get_port_activity_report_devtype18_custom_toggle_ghost_excluded(mock_client):
    """#143: devType=18 custom Humidifier, avg_speed=1.0, low activity → excluded by Rule G.

    on_count=1 / total=96 over 3 days: on_hours = 1/96 * 24 * 3 = 0.75; 0.75/3 = 0.25 < 1.0.
    avg_speed_when_running = 1.0 (single on-reading at speed=1).
    Rule G fires → ports_excluded_count == 1, 'no activity detected' in summary.
    """
    device = _make_devtype18_device([
        {"port": 1, "portName": "Humidifier", "portsLoad": 0, "loadType": 0},
    ])
    mock_client.get_devices.return_value = [device]
    readings = _make_devtype18_port_readings(1, "Humidifier", speed=1, on_count=1, total=96)
    mock_client.get_historical_data.return_value = [{}] * 96
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = readings[idx % len(readings)]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 3)
    data = json.loads(result)

    assert data["ports_excluded_count"] == 1, "#143: Humidifier must be excluded by Rule G"
    assert "no activity detected" in data["human_summary"], (
        "#143: exclusion text must say 'no activity detected' for devType=18"
    )
    assert "Humidifier (Port 1)" in data["human_summary"], (
        "#143: excluded port name must appear in exclusion sentence"
    )


@pytest.mark.asyncio
async def test_get_port_activity_report_devtype18_excluded_default_name_no_redundancy(mock_client):
    """devType=18 excluded port with API-default name shows 'Port N', not 'Port N (Port N)'."""
    device = _make_devtype18_device([
        {"port": 1, "portName": "Port 1", "portsLoad": 0, "loadType": 0},
    ])
    mock_client.get_devices.return_value = [device]
    readings = _make_devtype18_port_readings(1, "Port 1", speed=1, on_count=1, total=96)
    mock_client.get_historical_data.return_value = [{}] * 96
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = readings[idx % len(readings)]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 3)
    data = json.loads(result)
    assert data["ports_excluded_count"] == 1
    assert "no activity detected" in data["human_summary"]
    assert "Port 1 (Port 1)" not in data["human_summary"]


async def test_get_port_activity_report_devtype18_custom_variable_speed_kept(mock_client):
    """#143: devType=18 custom-named port with avg_speed=5.0 (non-toggle) is kept by Rule G.

    Same low-activity setup but speed=5.0 → avg_speed_when_running != 1.0 → Rule G does not fire.
    Port appears in port_lines, ports_excluded_count == 0.
    """
    device = _make_devtype18_device([
        {"port": 1, "portName": "Humidifier", "portsLoad": 0, "loadType": 0},
    ])
    mock_client.get_devices.return_value = [device]
    readings = _make_devtype18_port_readings(1, "Humidifier", speed=5, on_count=1, total=96)
    mock_client.get_historical_data.return_value = [{}] * 96
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = readings[idx % len(readings)]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 3)
    data = json.loads(result)

    assert data["ports_excluded_count"] == 0, (
        "#143: variable-speed port must not be excluded by Rule G"
    )
    port_names_in_output = [p["name"] for p in data["ports"]]
    assert "Humidifier" in port_names_in_output, "#143: Humidifier must appear in port output"


async def test_get_port_activity_report_get_devices_api_error_degrades_gracefully(mock_client):
    """get_devices failure → ACInfinityAPIError is caught by the error handler."""
    # In the new implementation, get_devices failure propagates as an API error.
    mock_client.get_devices.side_effect = ACInfinityAPIError("API error 500: server fault")
    result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    # ACInfinityAPIError is caught → structured error response
    assert data["error"] == "AC Infinity API error"
    assert "detail" in data


# ============ get_port_activity_report — window disclosure (#112) ============

_UTC = UTC
_CDT = ZoneInfo("America/Chicago")
# Frozen at 2025-05-24 15:35 UTC = 10:35 AM CDT; rolling 24h window starts at May 23 10:35 AM CDT
_FROZEN_MAY24 = datetime(2025, 5, 24, 15, 35, 0, tzinfo=_UTC)


def test_utc_hour_to_local_datetime_format():
    """_utc_hour_to_local produces date-bearing local time string."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    # 2024-05-23 20:00 UTC = 3:00 PM CDT (UTC-5 in summer)
    result = _utc_hour_to_local(datetime(2024, 5, 23, 20, 0, 0), ZoneInfo("America/Chicago"))
    assert result == "3:00 PM CDT (peak on May 23)"


def test_format_window_dt_midnight():
    """_format_window_dt: hour=0 formats as '12:xx AM'."""
    dt = datetime(2025, 5, 24, 0, 0, 0, tzinfo=_UTC)
    assert _format_window_dt(dt) == "May 24, 12:00 AM UTC"


def test_format_window_dt_noon():
    """_format_window_dt: hour=12 formats as '12:xx PM'."""
    dt = datetime(2025, 5, 24, 12, 0, 0, tzinfo=_UTC)
    assert _format_window_dt(dt) == "May 24, 12:00 PM UTC"


def test_short_date_single_digit_day():
    """_short_date: single-digit day has no leading zero."""
    dt = datetime(2025, 5, 3, 12, 0, 0, tzinfo=_UTC)
    assert _short_date(dt) == "May 3"


async def test_get_port_activity_report_window_fields_multi_day(mock_client):
    """window_start_local/window_end_local reflect the rolling 24h window in CDT."""
    readings = _make_port_readings(5, speed=5, on=True)
    mock_client.get_historical_data.return_value = [{}] * 5
    mock_client.parse_history_record.side_effect = lambda r, port_names=None: readings[0]
    with patch("ac_infinity_mcp.server._utcnow", return_value=_FROZEN_MAY24):
        result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    assert data["window_start_local"] == "May 23, 10:35 AM CDT"
    assert data["window_end_local"] == "May 24, 10:35 AM CDT"


async def test_get_port_activity_report_window_fields_utc_fallback(mock_client):
    """When zoneId is absent, window fields use UTC abbreviation."""
    mock_client.get_devices.return_value = [{
        "devCode": "C58ZA",
        "devId": "1424979258063367506",
        "deviceInfo": {"ports": []},
    }]
    readings = _make_port_readings(5, speed=5, on=True)
    mock_client.get_historical_data.return_value = [{}] * 5
    mock_client.parse_history_record.side_effect = lambda r, port_names=None: readings[0]
    with patch("ac_infinity_mcp.server._utcnow", return_value=_FROZEN_MAY24):
        result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    assert "UTC" in data["window_start_local"]
    assert "UTC" in data["window_end_local"]


async def test_get_port_activity_report_human_summary_includes_date_range(mock_client):
    """human_summary preamble includes the rolling-window date range."""
    readings = _make_port_readings(5, speed=5, on=True)
    mock_client.get_historical_data.return_value = [{}] * 5
    mock_client.parse_history_record.side_effect = lambda r, port_names=None: readings[0]
    with patch("ac_infinity_mcp.server._utcnow", return_value=_FROZEN_MAY24):
        result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    assert "May 23 – May 24" in data["human_summary"]


async def test_get_port_activity_report_peak_hour_local_includes_date_when_multi_day(mock_client):
    """peak_hour_local includes date prefix when window spans multiple calendar days."""
    # 3 on-readings at UTC 16:xx on May 23, 1 at 10:xx, 1 at 08:xx on May 24.
    # Peak slot: 2025-05-23T16:00 UTC → 11:00 AM CDT (peak on May 23)
    peak_readings = [
        {
            "timestamp": f"2025-05-23T16:0{i}:00Z",
            "temperature_c": 24.0, "humidity": 55.0, "vpd": 1.4,
            "ports": [{"port": 1, "name": "Inline Fan", "speed": 5, "on": True}],
        }
        for i in range(3)
    ] + [
        {
            "timestamp": "2025-05-23T10:00:00Z",
            "temperature_c": 24.0, "humidity": 55.0, "vpd": 1.4,
            "ports": [{"port": 1, "name": "Inline Fan", "speed": 5, "on": True}],
        },
        {
            "timestamp": "2025-05-24T08:00:00Z",
            "temperature_c": 24.0, "humidity": 55.0, "vpd": 1.4,
            "ports": [{"port": 1, "name": "Inline Fan", "speed": 5, "on": True}],
        },
    ]
    mock_client.get_historical_data.return_value = [{}] * len(peak_readings)
    idx = 0

    def _side(r, port_names=None):
        nonlocal idx
        v = peak_readings[idx]
        idx += 1
        return v

    mock_client.parse_history_record.side_effect = _side
    with patch("ac_infinity_mcp.server._utcnow", return_value=_FROZEN_MAY24):
        result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    assert data["ports"][0]["peak_hour_local"] == "11:00 AM CDT (peak on May 23)"


async def test_get_port_activity_report_dst_boundary(mock_client):
    """Window straddling DST spring-forward shows CST for start and CDT for end."""
    # March 9 2025: 2:00 AM CST springs forward to 3:00 AM CDT = 8:00 AM UTC.
    # window_start = March 8, 2:00 AM CST; window_end = March 9, 3:00 AM CDT.
    frozen_dst = datetime(2025, 3, 9, 8, 0, 0, tzinfo=_UTC)
    readings = _make_port_readings(5, speed=5, on=True)
    mock_client.get_historical_data.return_value = [{}] * 5
    mock_client.parse_history_record.side_effect = lambda r, port_names=None: readings[0]
    with patch("ac_infinity_mcp.server._utcnow", return_value=frozen_dst):
        result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    assert "CST" in data["window_start_local"]
    assert "CDT" in data["window_end_local"]
    assert "March 8" in data["window_start_local"]
    assert "March 9" in data["window_end_local"]


async def test_get_port_activity_report_excluded_count_capped_at_devportcount(mock_client):
    """devPortCount=2 caps excluded count when history has 3 unique ports."""
    # 3 unique auto-named ports (all Rule B filtered: low runtime, auto-named pattern)
    # Without cap: excluded = max(0, 3 - 0) = 3
    # With cap:    excluded = max(0, min(3, 2) - 0) = 2
    from datetime import datetime, timedelta
    base = datetime(2024, 4, 18, 0, 0, 0)
    readings = []
    for i in range(3):
        on = i == 0  # only 1st reading on → < 1h/day → Rule B filters all
        readings.append({
            "timestamp": (base + timedelta(hours=i)).isoformat() + "Z",
            "temperature_c": 24.0, "humidity": 55.0, "vpd": 1.4,
            "ports": [
                {"port": 1, "name": "Port 1", "speed": 5 if on else 0, "on": on},
                {"port": 2, "name": "Port 2", "speed": 5 if on else 0, "on": on},
                {"port": 3, "name": "Port 3", "speed": 5 if on else 0, "on": on},
            ],
        })
    mock_client.get_devices.return_value = [{
        "devCode": "C58ZA",
        "devId": "9999999999",
        "devPortCount": 2,
        "deviceInfo": {"ports": [
            {"port": 1, "portsLoad": 0},
            {"port": 2, "portsLoad": 0},
            {"port": 3, "portsLoad": 0},
        ]},
    }]
    mock_client.get_historical_data.return_value = [{}] * 3
    idx = 0

    def _side(r, port_names=None):
        nonlocal idx
        v = readings[idx]
        idx += 1
        return v

    mock_client.parse_history_record.side_effect = _side
    result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    assert data["ports"] == []
    assert data["ports_excluded_count"] == 2  # capped from 3 to devPortCount=2


@pytest.mark.parametrize("dev_port_count", [None, 0])
async def test_get_port_activity_report_ports_excluded_count_devportcount_fallback(
    dev_port_count, mock_client
):
    """devPortCount absent or zero falls back to unique_port_count — no cap applied."""
    # 3 unique auto-named ports (all Rule B filtered), devPortCount=None/0 → excluded==3
    from datetime import datetime, timedelta
    base = datetime(2024, 4, 18, 0, 0, 0)
    readings = []
    for i in range(3):
        on = i == 0
        readings.append({
            "timestamp": (base + timedelta(hours=i)).isoformat() + "Z",
            "temperature_c": 24.0, "humidity": 55.0, "vpd": 1.4,
            "ports": [
                {"port": 1, "name": "Port 1", "speed": 5 if on else 0, "on": on},
                {"port": 2, "name": "Port 2", "speed": 5 if on else 0, "on": on},
                {"port": 3, "name": "Port 3", "speed": 5 if on else 0, "on": on},
            ],
        })
    device_dict: dict = {
        "devCode": "C58ZA",
        "devId": "9999999999",
        "deviceInfo": {"ports": [
            {"port": 1, "portsLoad": 0},
            {"port": 2, "portsLoad": 0},
            {"port": 3, "portsLoad": 0},
        ]},
    }
    if dev_port_count is not None:
        device_dict["devPortCount"] = dev_port_count
    mock_client.get_devices.return_value = [device_dict]
    mock_client.get_historical_data.return_value = [{}] * 3
    idx = 0

    def _side(r, port_names=None):
        nonlocal idx
        v = readings[idx]
        idx += 1
        return v

    mock_client.parse_history_record.side_effect = _side
    result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    assert data["ports_excluded_count"] == 3  # no cap applied — fallback to unique_port_count


async def test_get_port_activity_report_peak_hour_local_includes_date(mock_client):
    """peak_hour_local includes date context, e.g. '3:00 PM CDT (peak on May 23)'."""
    import re
    from datetime import datetime, timedelta
    # 3 readings at UTC 20:00 on 2024-05-23 → local CDT 15:00 = 3 PM
    base = datetime(2024, 5, 23, 20, 0, 0)
    readings = [
        {
            "timestamp": (base + timedelta(minutes=i)).isoformat() + "Z",
            "temperature_c": 24.0, "humidity": 55.0, "vpd": 1.4,
            "ports": [{"port": 1, "name": "Fan", "speed": 5, "on": True}],
        }
        for i in range(3)
    ]
    mock_client.get_devices.return_value = [{
        "devCode": "C58ZA",
        "devId": "9999999999",
        "zoneId": "America/Chicago",
        "devPortCount": 1,
        "deviceInfo": {"ports": [{"port": 1, "portName": "Fan", "portsLoad": 5, "loadType": 0}]},
    }]
    mock_client.get_historical_data.return_value = [{}] * 3
    idx = 0

    def _side(r, port_names=None):
        nonlocal idx
        v = readings[idx]
        idx += 1
        return v

    mock_client.parse_history_record.side_effect = _side
    result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    assert len(data["ports"]) == 1
    phl = data["ports"][0]["peak_hour_local"]
    assert phl is not None
    assert re.match(r"\d+:\d{2} (AM|PM) \w+ \(peak on \w+ \d+\)", phl), f"Unexpected format: {phl}"
    assert "peak_hour_utc" not in data["ports"][0]


async def test_get_port_activity_report_peak_hour_utc_not_in_json_output(mock_client):
    """peak_hour_utc (internal datetime) must not appear in the JSON output."""
    from datetime import datetime, timedelta
    base = datetime(2024, 4, 18, 0, 0, 0)
    readings = [
        {
            "timestamp": (base + timedelta(hours=i)).isoformat() + "Z",
            "temperature_c": 24.0, "humidity": 55.0, "vpd": 1.4,
            "ports": [{"port": 1, "name": "Fan", "speed": 5, "on": True}],
        }
        for i in range(5)
    ]
    mock_client.get_historical_data.return_value = [{}] * 5
    idx = 0

    def _side(r, port_names=None):
        nonlocal idx
        v = readings[idx]
        idx += 1
        return v

    mock_client.parse_history_record.side_effect = _side
    result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    for port in data["ports"]:
        assert "peak_hour_utc" not in port


# ============ set_port_speed ============

MOCK_SET_PORT_MODE_DRY = {
    "payload": {"onSpead": 5, "modeType": 2, "devId": 12345},
    "dry_run": True,
    "controller_type": "legacy",
    "sent": False,
    "prior_at_type": 2,
}

MOCK_SET_PORT_MODE_LIVE = {
    "payload": {"onSpead": 5, "modeType": 2, "devId": 12345},
    "dry_run": False,
    "controller_type": "legacy",
    "sent": True,
    "prior_at_type": 2,
}


async def test_set_port_speed_dry_run(mock_client):
    mock_client.set_port_mode.return_value = MOCK_SET_PORT_MODE_DRY
    result = await set_port_speed("C58ZA", 2, 5, dry_run=True)
    data = json.loads(result)
    assert data["dry_run"] is True
    assert data["sent"] is False
    assert data["speed"] == 5
    assert data["port"] == 2
    assert data["device_id"] == "C58ZA"
    assert "payload" in data
    assert data["controller_type"] == "legacy"


async def test_set_port_speed_live(mock_client):
    mock_client.set_port_mode.return_value = MOCK_SET_PORT_MODE_LIVE
    result = await set_port_speed("C58ZA", 2, 5, dry_run=False)
    data = json.loads(result)
    assert data["sent"] is True
    assert data["dry_run"] is False
    assert "payload" not in data


async def test_set_port_speed_device_not_found(mock_client):
    result = await set_port_speed("INVALID", 1, 5)
    data = json.loads(result)
    assert "error" in data
    assert "INVALID" in data["error"]


async def test_set_port_speed_speed_zero(mock_client):
    result = await set_port_speed("C58ZA", 1, 0)
    data = json.loads(result)
    assert "error" in data
    assert "speed" in data["error"]


async def test_set_port_speed_speed_eleven(mock_client):
    result = await set_port_speed("C58ZA", 1, 11)
    data = json.loads(result)
    assert "error" in data
    assert "speed" in data["error"]


async def test_set_port_speed_speed_one_valid(mock_client):
    mock_client.set_port_mode.return_value = {**MOCK_SET_PORT_MODE_DRY, "payload": {"onSpead": 1}}
    result = await set_port_speed("C58ZA", 1, 1)
    data = json.loads(result)
    assert "error" not in data
    assert data["speed"] == 1


async def test_set_port_speed_speed_ten_valid(mock_client):
    mock_client.set_port_mode.return_value = {**MOCK_SET_PORT_MODE_DRY, "payload": {"onSpead": 10}}
    result = await set_port_speed("C58ZA", 1, 10)
    data = json.loads(result)
    assert "error" not in data
    assert data["speed"] == 10


async def test_set_port_speed_port_zero(mock_client):
    result = await set_port_speed("C58ZA", 0, 5)
    data = json.loads(result)
    assert "error" in data
    assert "port" in data["error"]


async def test_set_port_speed_api_error(mock_client):
    mock_client.set_port_mode.side_effect = ACInfinityAPIError("API error 500")
    result = await set_port_speed("C58ZA", 1, 5)
    data = json.loads(result)
    assert "error" in data


async def test_set_port_speed_auth_error(mock_client):
    mock_client.set_port_mode.side_effect = ACInfinityAuthError("Not authenticated")
    result = await set_port_speed("C58ZA", 1, 5)
    data = json.loads(result)
    assert "error" in data


async def test_set_port_speed_device_error(mock_client):
    mock_client.set_port_mode.side_effect = ACInfinityDeviceError("device_data missing devId")
    result = await set_port_speed("C58ZA", 1, 5)
    data = json.loads(result)
    assert "error" in data


async def test_set_port_speed_uses_asyncio_to_thread(mock_client):
    """Confirm set_port_mode is called via asyncio.to_thread, not directly."""
    mock_client.set_port_mode.return_value = MOCK_SET_PORT_MODE_DRY
    with patch("asyncio.to_thread", wraps=asyncio.to_thread) as mock_thread:
        await set_port_speed("C58ZA", 1, 5)
    # asyncio.to_thread should have been called at least twice:
    # once for get_devices and once for set_port_mode
    assert mock_thread.call_count >= 2


async def test_set_port_speed_off_mode_warning_modeType_0(mock_client):
    """modeType=0 (uninitialised OFF) in prior state triggers a warning field."""
    mock_result = {**MOCK_SET_PORT_MODE_DRY, "prior_at_type": 0}
    mock_client.set_port_mode.return_value = mock_result
    result = await set_port_speed("C58ZA", 2, 5, dry_run=True)
    data = json.loads(result)
    assert "warning" in data
    assert "OFF mode" in data["warning"]
    assert "set_port_mode" not in data["warning"]
    assert "Call" not in data["warning"]
    assert "ON mode" in data["warning"]
    assert "Exhaust Fan (Port 2)" in data["warning"]


async def test_set_port_speed_off_mode_warning_modeType_1(mock_client):
    """modeType=1 (explicit OFF) in prior state triggers a warning field."""
    mock_result = {**MOCK_SET_PORT_MODE_DRY, "prior_at_type": 1}
    mock_client.set_port_mode.return_value = mock_result
    result = await set_port_speed("C58ZA", 2, 5, dry_run=True)
    data = json.loads(result)
    assert "warning" in data
    assert "OFF mode" in data["warning"]
    assert "set_port_mode" not in data["warning"]
    assert "Call" not in data["warning"]
    assert "ON mode" in data["warning"]
    assert "Exhaust Fan (Port 2)" in data["warning"]


async def test_set_port_speed_no_warning_when_on_mode(mock_client):
    """modeType=2 (ON) — no warning is included in the response."""
    mock_client.set_port_mode.return_value = {**MOCK_SET_PORT_MODE_DRY, "prior_at_type": 2}
    result = await set_port_speed("C58ZA", 2, 5, dry_run=True)
    data = json.loads(result)
    assert "warning" not in data


@pytest.mark.asyncio
async def test_set_port_speed_action_uses_port_name(mock_client):
    """action field uses port name + number for a named port."""
    mock_client.set_port_mode.return_value = MOCK_SET_PORT_MODE_DRY
    result = await set_port_speed("C58ZA", 2, 5)
    data = json.loads(result)
    assert data["action"] == "set Exhaust Fan (Port 2) speed to 5"


@pytest.mark.asyncio
async def test_set_port_speed_action_unnamed_port_fallback(mock_client):
    """action field falls back to 'Port N' when port has no custom name."""
    mock_client.set_port_mode.return_value = MOCK_SET_PORT_MODE_DRY
    result = await set_port_speed("C58ZA", 3, 5)
    data = json.loads(result)
    assert data["action"] == "set Port 3 speed to 5"


@pytest.mark.asyncio
async def test_set_port_on_action_uses_port_name(mock_client):
    """set_port_on action field uses port name + number for a named port."""
    mock_client.set_port_mode.return_value = MOCK_SET_PORT_ON_DRY
    result = await set_port_on("C58ZA", 1)
    data = json.loads(result)
    assert data["action"] == "turn Intake Fan (Port 1) on"


@pytest.mark.asyncio
async def test_set_port_off_action_uses_port_name(mock_client):
    """set_port_off action field uses port name + number for a named port."""
    mock_client.set_port_mode.return_value = MOCK_SET_PORT_OFF_DRY
    result = await set_port_off("C58ZA", 2)
    data = json.loads(result)
    assert data["action"] == "turn Exhaust Fan (Port 2) off"


@pytest.mark.asyncio
async def test_set_port_mode_action_uses_port_name(mock_client):
    """set_port_mode action field uses port name + number for a named port."""
    mock_client.set_port_mode.return_value = MOCK_SET_PORT_MODE_DRY
    result = await set_port_mode("C58ZA", 1, "ON")
    data = json.loads(result)
    assert data["action"] == "set Intake Fan (Port 1) mode to ON"


@pytest.mark.asyncio
async def test_set_vpd_automation_action_uses_port_name(mock_client):
    """set_vpd_automation action field includes port name + number."""
    mock_client.set_port_mode.return_value = MOCK_VPD_DRY
    result = await set_vpd_automation("C58ZA", 1, 1.4)
    data = json.loads(result)
    assert "Intake Fan (Port 1)" in data["action"]


@pytest.mark.asyncio
async def test_set_temperature_automation_action_uses_port_name(mock_client):
    """set_temperature_automation action field includes port name + number."""
    mock_client.set_port_mode.return_value = MOCK_TEMP_DRY
    result = await set_temperature_automation("C58ZA", 1, 20.0, 28.0)
    data = json.loads(result)
    assert "Intake Fan (Port 1)" in data["action"]


@pytest.mark.asyncio
async def test_set_humidity_automation_action_uses_port_name(mock_client):
    """set_humidity_automation action field includes port name + number."""
    mock_client.set_port_mode.return_value = MOCK_HUMI_DRY
    result = await set_humidity_automation("C58ZA", 1, 50.0, 70.0)
    data = json.loads(result)
    assert "Intake Fan (Port 1)" in data["action"]


@pytest.mark.asyncio
async def test_set_port_on_action_unnamed_port_fallback(mock_client):
    """action field falls back to 'Port N' when port has no custom name."""
    mock_client.set_port_mode.return_value = MOCK_SET_PORT_ON_DRY
    result = await set_port_on("C58ZA", 3)
    data = json.loads(result)
    assert data["action"] == "turn Port 3 on"


@pytest.mark.asyncio
async def test_set_port_off_action_unnamed_port_fallback(mock_client):
    """action field falls back to 'Port N' when port has no custom name."""
    mock_client.set_port_mode.return_value = MOCK_SET_PORT_OFF_DRY
    result = await set_port_off("C58ZA", 3)
    data = json.loads(result)
    assert data["action"] == "turn Port 3 off"


@pytest.mark.asyncio
async def test_set_port_mode_action_unnamed_port_fallback(mock_client):
    """action field falls back to 'Port N' when port has no custom name."""
    mock_client.set_port_mode.return_value = MOCK_SET_PORT_MODE_DRY
    result = await set_port_mode("C58ZA", 3, "ON")
    data = json.loads(result)
    assert data["action"] == "set Port 3 mode to ON"


@pytest.mark.asyncio
async def test_set_vpd_automation_action_unnamed_port_fallback(mock_client):
    """action field falls back to 'Port N' when port has no custom name."""
    mock_client.set_port_mode.return_value = MOCK_VPD_DRY
    result = await set_vpd_automation("C58ZA", 3, 1.4)
    data = json.loads(result)
    assert "Port 3" in data["action"]


@pytest.mark.asyncio
async def test_set_temperature_automation_action_unnamed_port_fallback(mock_client):
    """action field falls back to 'Port N' when port has no custom name."""
    mock_client.set_port_mode.return_value = MOCK_TEMP_DRY
    result = await set_temperature_automation("C58ZA", 3, 20.0, 28.0)
    data = json.loads(result)
    assert "Port 3" in data["action"]


@pytest.mark.asyncio
async def test_set_humidity_automation_action_unnamed_port_fallback(mock_client):
    """action field falls back to 'Port N' when port has no custom name."""
    mock_client.set_port_mode.return_value = MOCK_HUMI_DRY
    result = await set_humidity_automation("C58ZA", 3, 50.0, 70.0)
    data = json.loads(result)
    assert "Port 3" in data["action"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "port_num,port_name_val,expected_action",
    [
        (7, "Port 7", "turn Port 7 on"),           # default name → no "(Port 7)" suffix
        (7, "Exhaust", "turn Exhaust (Port 7) on"),  # custom name → include suffix
        (8, "Port 7", "turn Port 7 (Port 8) on"),   # cross-port: "Port 7" is custom for port 8
    ],
)
async def test_set_port_on_action_port_name_cases(
    mock_client, port_num, port_name_val, expected_action
):
    """set_port_on action field handles default, custom, and cross-port names."""
    import copy
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["deviceInfo"]["ports"].append(
        {"port": port_num, "portName": port_name_val, "speak": 0, "portsLoad": 0,
         "loadState": 0, "curMode": 1, "remainTime": None}
    )
    mock_client.get_devices.return_value = [device]
    mock_client.set_port_mode.return_value = {
        "payload": {"onSpead": 10, "modeType": 2, "devId": 12345},
        "dry_run": True,
        "controller_type": "legacy",
        "sent": False,
    }
    result = await set_port_on("C58ZA", port_num)
    data = json.loads(result)
    assert data["action"] == expected_action


# ============ set_port_on ============

MOCK_SET_PORT_ON_DRY = {
    "payload": {"onSpead": 10, "modeType": 2, "devId": 12345},
    "dry_run": True,
    "controller_type": "legacy",
    "sent": False,
}


async def test_set_port_on_dry_run(mock_client):
    mock_client.set_port_mode.return_value = MOCK_SET_PORT_ON_DRY
    result = await set_port_on("C58ZA", 1, dry_run=True)
    data = json.loads(result)
    assert data["dry_run"] is True
    assert data["sent"] is False
    assert data["payload"]["onSpead"] == 10
    assert data["device_id"] == "C58ZA"
    assert data["port"] == 1


async def test_set_port_on_device_not_found(mock_client):
    result = await set_port_on("INVALID", 1)
    data = json.loads(result)
    assert "error" in data
    assert "INVALID" in data["error"]


async def test_set_port_on_port_zero(mock_client):
    result = await set_port_on("C58ZA", 0)
    data = json.loads(result)
    assert "error" in data
    assert "port" in data["error"]


async def test_set_port_on_api_error(mock_client):
    mock_client.set_port_mode.side_effect = ACInfinityAPIError("API error 403")
    result = await set_port_on("C58ZA", 1)
    data = json.loads(result)
    assert "error" in data


async def test_set_port_on_auth_error(mock_client):
    mock_client.set_port_mode.side_effect = ACInfinityAuthError("Not authenticated")
    result = await set_port_on("C58ZA", 1)
    data = json.loads(result)
    assert "error" in data


async def test_set_port_on_device_error_non_advance(mock_client):
    """Base ACInfinityDeviceError (not the advance subclass) returns a plain error string."""
    mock_client.set_port_mode.side_effect = ACInfinityDeviceError("loadType=4 device")
    result = await set_port_on("C58ZA", 1)
    data = json.loads(result)
    assert "error" in data
    assert "loadType=4" in data["error"]


# ============ set_port_off ============

MOCK_SET_PORT_OFF_DRY = {
    "payload": {"onSpead": 0, "modeType": 0, "devId": 12345},
    "dry_run": True,
    "controller_type": "legacy",
    "sent": False,
}


async def test_set_port_off_dry_run(mock_client):
    mock_client.set_port_mode.return_value = MOCK_SET_PORT_OFF_DRY
    result = await set_port_off("C58ZA", 1, dry_run=True)
    data = json.loads(result)
    assert data["dry_run"] is True
    assert data["sent"] is False
    assert data["payload"]["onSpead"] == 0
    assert data["device_id"] == "C58ZA"


async def test_set_port_off_device_not_found(mock_client):
    result = await set_port_off("INVALID", 1)
    data = json.loads(result)
    assert "error" in data
    assert "INVALID" in data["error"]


async def test_set_port_off_port_zero(mock_client):
    result = await set_port_off("C58ZA", 0)
    data = json.loads(result)
    assert "error" in data
    assert "port" in data["error"]


async def test_set_port_off_api_error(mock_client):
    mock_client.set_port_mode.side_effect = ACInfinityAPIError("API error 403")
    result = await set_port_off("C58ZA", 1)
    data = json.loads(result)
    assert "error" in data


async def test_set_port_off_auth_error(mock_client):
    mock_client.set_port_mode.side_effect = ACInfinityAuthError("Not authenticated")
    result = await set_port_off("C58ZA", 1)
    data = json.loads(result)
    assert "error" in data


async def test_set_port_off_device_error_non_advance(mock_client):
    """Base ACInfinityDeviceError (not advance subclass) returns plain error."""
    mock_client.set_port_mode.side_effect = ACInfinityDeviceError("device guard triggered")
    result = await set_port_off("C58ZA", 1)
    data = json.loads(result)
    assert "error" in data
    assert "device guard" in data["error"]


@pytest.mark.parametrize("tool_name,args", [
    ("set_port_on", ("C58ZA", 1)),
    ("set_port_off", ("C58ZA", 1)),
])
async def test_set_port_on_off_does_not_pass_require_variable_speed(mock_client, tool_name, args):
    """set_port_on/off must NOT set require_variable_speed=True — that's only for set_port_speed.

    If they did, the loadType guard would reject on/off devices (loadType=4 or 128)
    and prevent the user from turning them on/off (P2-F025).
    """
    import ac_infinity_mcp.server as server_module
    tool = getattr(server_module, tool_name)
    mock_client.set_port_mode.return_value = {
        "payload": {}, "dry_run": True, "controller_type": "legacy", "sent": False,
    }
    await tool(*args)
    kwargs = mock_client.set_port_mode.call_args.kwargs
    assert kwargs.get("require_variable_speed", False) is False


# ============ Guard rails — Phase 8 ============


async def test_set_port_speed_rejects_load_type_4(mock_client):
    """set_port_speed rejects on/off devices (loadType=4) — guard fires in client layer."""
    mock_client.set_port_mode.side_effect = ACInfinityDeviceError(
        "Port 1 is an on/off device (loadType=4) — use set_port_on or set_port_off."
    )
    result = await set_port_speed("C58ZA", 1, 5)
    data = json.loads(result)
    assert "error" in data
    assert "loadType=4" in data["error"]


async def test_set_port_speed_rejects_load_type_128(mock_client):
    """set_port_speed rejects dimmer-type devices (loadType=128) — guard fires in client layer."""
    mock_client.set_port_mode.side_effect = ACInfinityDeviceError(
        "Port 1 is an on/off device (loadType=128) — use set_port_on or set_port_off."
    )
    result = await set_port_speed("C58ZA", 1, 5)
    data = json.loads(result)
    assert "error" in data
    assert "loadType=128" in data["error"]


async def test_set_port_speed_allows_variable_speed_port(mock_client):
    """set_port_speed must succeed for variable-speed ports (loadType=0 or 1)."""
    mock_client.set_port_mode.return_value = MOCK_SET_PORT_MODE_DRY
    result = await set_port_speed("C58ZA", 1, 5)
    data = json.loads(result)
    assert "error" not in data


async def test_set_port_on_not_affected_by_load_type_guard(mock_client):
    """set_port_on must NOT trigger the loadType guard — correct tool for on/off devices."""
    mock_client.set_port_mode.return_value = {
        "payload": {"onSpead": 10}, "dry_run": True, "controller_type": "legacy", "sent": False
    }
    result = await set_port_on("C58ZA", 1, dry_run=True)
    data = json.loads(result)
    assert "error" not in data
    assert data["dry_run"] is True


async def test_set_port_off_not_affected_by_load_type_guard(mock_client):
    """set_port_off must NOT trigger the loadType guard — correct tool for on/off devices."""
    mock_client.set_port_mode.return_value = {
        "payload": {"onSpead": 0}, "dry_run": True, "controller_type": "legacy", "sent": False
    }
    result = await set_port_off("C58ZA", 1, dry_run=True)
    data = json.loads(result)
    assert "error" not in data
    assert data["dry_run"] is True


async def test_set_port_speed_returns_conflict_for_modeType_15(mock_client):
    """ACInfinityAdvanceConflictError from modeType=15 guard returns structured conflict.

    Port 4 is used because MOCK_ADVANCE_AUTOMATIONS_LIST includes a port_group with
    grouptDevType=8 (bitmask bit 3 = Port 4), so the bitmask lookup yields Sub-path A
    and offers break_out_of_automation.
    """
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError(
        "Port 4 on device 12345 is in smart automation mode (modeType=15)"
    )
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await set_port_speed("C58ZA", 4, 5)
    data = json.loads(result)
    assert data["conflict"] == "ADVANCE_AUTOMATION"
    assert "summary" in data
    assert "automation" in data["summary"].lower() and "controller" in data["summary"].lower()
    assert data["target_port"] == "Port 4"
    assert "options" in data
    assert "1_break_out" in data["options"]
    assert "1_re_disable_to_clear" not in data["options"]
    assert "human_summary" in data
    assert "error" not in data


async def test_set_port_on_returns_conflict_for_modeType_15(mock_client):
    """ACInfinityAdvanceConflictError from modeType=15 guard applies to set_port_on."""
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError(
        "Port 1 on device 12345 is in smart automation mode (modeType=15)"
    )
    result = await set_port_on("C58ZA", 1)
    data = json.loads(result)
    assert data["conflict"] == "ADVANCE_AUTOMATION"
    assert "summary" in data
    assert "error" not in data


async def test_set_port_off_returns_conflict_for_modeType_15(mock_client):
    """ACInfinityAdvanceConflictError from modeType=15 guard applies to set_port_off."""
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError(
        "Port 1 on device 12345 is in smart automation mode (modeType=15)"
    )
    result = await set_port_off("C58ZA", 1)
    data = json.loads(result)
    assert data["conflict"] == "ADVANCE_AUTOMATION"
    assert "summary" in data
    assert "error" not in data

async def test_set_port_speed_passes_require_variable_speed_to_client(mock_client):
    """set_port_speed passes require_variable_speed=True; client layer enforces the guard."""
    mock_client.set_port_mode.return_value = MOCK_SET_PORT_MODE_DRY
    await set_port_speed("C58ZA", 1, 5)
    call_kwargs = mock_client.set_port_mode.call_args
    assert call_kwargs.kwargs.get("require_variable_speed") is True


# ============ Generic except Exception coverage ============

async def test_get_device_reading_generic_exception(mock_client):
    mock_client.get_devices.side_effect = RuntimeError("unexpected crash")
    result = await get_device_reading("C58ZA")
    data = json.loads(result)
    assert "error" in data


async def test_get_all_device_readings_generic_exception(mock_client):
    mock_client.get_devices.side_effect = RuntimeError("unexpected crash")
    result = await get_all_device_readings()
    data = json.loads(result)
    assert "error" in data


async def test_set_port_speed_generic_exception(mock_client):
    mock_client.set_port_mode.side_effect = RuntimeError("unexpected crash")
    result = await set_port_speed("C58ZA", 1, 5)
    data = json.loads(result)
    assert "error" in data


async def test_set_port_on_generic_exception(mock_client):
    mock_client.set_port_mode.side_effect = RuntimeError("unexpected crash")
    result = await set_port_on("C58ZA", 1)
    data = json.loads(result)
    assert "error" in data


async def test_set_port_off_generic_exception(mock_client):
    mock_client.set_port_mode.side_effect = RuntimeError("unexpected crash")
    result = await set_port_off("C58ZA", 1)
    data = json.loads(result)
    assert "error" in data


# ============ get_historical_readings — error handlers + missing branches ============

async def test_get_historical_readings_auth_error(mock_client):
    mock_client.get_devices.side_effect = ACInfinityAuthError("token expired")
    result = await get_historical_readings("C58ZA", "2024-04-25", "2024-04-25")
    data = json.loads(result)
    assert "Authentication failed" in data["error"]


async def test_get_historical_readings_api_error(mock_client):
    mock_client.get_devices.side_effect = ACInfinityAPIError("API error 503")
    result = await get_historical_readings("C58ZA", "2024-04-25", "2024-04-25")
    data = json.loads(result)
    assert data["error"] == "AC Infinity API error"


async def test_get_historical_readings_generic_exception(mock_client):
    mock_client.get_devices.side_effect = RuntimeError("unexpected crash")
    result = await get_historical_readings("C58ZA", "2024-04-25", "2024-04-25")
    data = json.loads(result)
    assert "error" in data


async def test_get_historical_readings_empty_after_sampling(mock_client):
    base_ts = 1714000000
    raw_records = [{"createTime": base_ts}]
    mock_client.get_historical_data.return_value = raw_records
    # Return a record with a bad timestamp so apply_sampling skips it and sampled is empty
    mock_client.parse_history_record.side_effect = lambda r, port_names=None: {
        "timestamp": "NOT_A_VALID_TIMESTAMP",
        "temperature_c": 24.0, "temperature_f": 75.2,
        "humidity": 55.0, "vpd": 1.5, "ports": [],
    }
    result = await get_historical_readings("C58ZA", "2024-04-25", "2024-04-25", "1h")
    data = json.loads(result)
    assert "error" in data["statistics"]


async def test_get_historical_readings_with_time_filter(mock_client):
    base_ts = 1714000000
    raw_records = [{"createTime": base_ts + i * 3600} for i in range(4)]
    mock_client.get_historical_data.return_value = raw_records
    mock_client.parse_history_record.side_effect = lambda r, port_names=None: {
        "timestamp": f"2024-04-25T{(r['createTime'] - base_ts) // 3600 + 8:02d}:00:00Z",
        "temperature_c": 24.0, "temperature_f": 75.2,
        "humidity": 55.0, "vpd": 1.5, "ports": [],
    }
    result = await get_historical_readings(
        "C58ZA", "2024-04-25", "2024-04-25", "raw",
        time_start="10:00", time_end="12:00",
    )
    data = json.loads(result)
    assert len(data["readings"]) <= 4


async def test_get_historical_readings_port_stats_computed(mock_client):
    base_ts = 1714000000
    raw_records = [{"createTime": base_ts + i * 3600} for i in range(3)]
    mock_client.get_historical_data.return_value = raw_records
    mock_client.parse_history_record.side_effect = lambda r, port_names=None: {
        "timestamp": f"2024-04-25T{(r['createTime'] - base_ts) // 3600:02d}:00:00Z",
        "temperature_c": 24.0, "temperature_f": 75.2,
        "humidity": 55.0, "vpd": 1.5,
        "ports": [{"port": 1, "name": "Fan", "speed": 5, "on": True}],
    }
    result = await get_historical_readings("C58ZA", "2024-04-25", "2024-04-25", "raw")
    data = json.loads(result)
    stats = data["statistics"]
    assert "port_statistics" in stats
    assert "Fan" in stats["port_statistics"]


# ============ check_vpd_drift — error handlers ============

async def test_check_vpd_drift_auth_error(mock_client):
    with patch("ac_infinity_mcp.server.get_device_reading",
               side_effect=ACInfinityAuthError("token expired")):
        result = await check_vpd_drift("C58ZA", "veg")
    data = json.loads(result)
    assert "Authentication failed" in data["error"]


async def test_check_vpd_drift_api_error(mock_client):
    with patch("ac_infinity_mcp.server.get_device_reading",
               side_effect=ACInfinityAPIError("API error 500")):
        result = await check_vpd_drift("C58ZA", "veg")
    data = json.loads(result)
    assert data["error"] == "AC Infinity API error"


async def test_check_vpd_drift_generic_exception(mock_client):
    with patch("ac_infinity_mcp.server.get_device_reading",
               side_effect=RuntimeError("unexpected crash")):
        result = await check_vpd_drift("C58ZA", "veg")
    data = json.loads(result)
    assert "error" in data


# ============ get_environment_health — error handlers ============

async def test_get_environment_health_auth_error(mock_client):
    # get_environment_health calls get_devices() directly (no get_device_reading tool chain).
    mock_client.get_devices.side_effect = ACInfinityAuthError("token expired")
    result = await get_environment_health("C58ZA", "veg")
    data = json.loads(result)
    assert "Authentication failed" in data["error"]


async def test_get_environment_health_api_error(mock_client):
    # get_environment_health calls get_devices() directly (no get_device_reading tool chain).
    mock_client.get_devices.side_effect = ACInfinityAPIError("API error 500")
    result = await get_environment_health("C58ZA", "veg")
    data = json.loads(result)
    assert data["error"] == "AC Infinity API error"


async def test_get_environment_health_generic_exception(mock_client):
    # get_environment_health calls get_devices() directly (no get_device_reading tool chain).
    mock_client.get_devices.side_effect = RuntimeError("unexpected crash")
    result = await get_environment_health("C58ZA", "veg")
    data = json.loads(result)
    assert "error" in data


# ============ detect_environment_trends — error handlers ============

async def test_detect_environment_trends_auth_error(mock_client):
    # detect_environment_trends calls get_devices() directly (no get_historical_readings).
    mock_client.get_devices.side_effect = ACInfinityAuthError("token expired")
    result = await detect_environment_trends("C58ZA", 7)
    data = json.loads(result)
    assert "Authentication failed" in data["error"]


async def test_detect_environment_trends_api_error(mock_client):
    # detect_environment_trends calls get_devices() directly (no get_historical_readings).
    mock_client.get_devices.side_effect = ACInfinityAPIError("API error 500")
    result = await detect_environment_trends("C58ZA", 7)
    data = json.loads(result)
    assert data["error"] == "AC Infinity API error"


async def test_detect_environment_trends_generic_exception(mock_client):
    # detect_environment_trends calls get_devices() directly (no get_historical_readings).
    mock_client.get_devices.side_effect = RuntimeError("unexpected crash")
    result = await detect_environment_trends("C58ZA", 7)
    data = json.loads(result)
    assert "error" in data


# ============ get_port_activity_report — error propagation + error handlers ============

async def test_get_port_activity_report_error_propagated(mock_client):
    # get_port_activity_report now calls get_devices() directly; "no device found" is
    # triggered by returning an empty list.
    mock_client.get_devices.return_value = []
    result = await get_port_activity_report("C58ZA", 7)
    data = json.loads(result)
    assert "error" in data


async def test_get_port_activity_report_auth_error(mock_client):
    # get_port_activity_report calls get_devices() directly (no get_historical_readings).
    mock_client.get_devices.side_effect = ACInfinityAuthError("token expired")
    result = await get_port_activity_report("C58ZA", 7)
    data = json.loads(result)
    assert "Authentication failed" in data["error"]


async def test_get_port_activity_report_api_error(mock_client):
    # get_port_activity_report calls get_devices() directly (no get_historical_readings).
    mock_client.get_devices.side_effect = ACInfinityAPIError("API error 500")
    result = await get_port_activity_report("C58ZA", 7)
    data = json.loads(result)
    assert data["error"] == "AC Infinity API error"


async def test_get_port_activity_report_generic_exception(mock_client):
    # get_port_activity_report calls get_devices() directly (no get_historical_readings).
    mock_client.get_devices.side_effect = RuntimeError("unexpected crash")
    result = await get_port_activity_report("C58ZA", 7)
    data = json.loads(result)
    assert "error" in data


# ============ apply_sampling — bad timestamp coverage ============

def test_apply_sampling_bad_timestamp_skipped():
    readings = [
        _make_history_record("NOT_A_TIMESTAMP", temp_c=24.0),
        _make_history_record("2024-04-25T10:00:00Z", temp_c=24.0),
    ]
    result = apply_sampling(readings, "1h")
    assert len(result) == 1


# ============ _decode_mode / _format_schedule_time helpers ============

@pytest.mark.parametrize("mode_int,expected", [
    (1, "OFF"), (2, "ON"), (3, "AUTO"),
    (4, "TIMER_TO_ON"), (5, "TIMER_TO_OFF"),
    (6, "CYCLE"), (7, "SCHEDULE"), (8, "VPD"),
])
def test_decode_mode_known_values(mode_int, expected):
    assert _decode_mode(mode_int) == expected


def test_decode_mode_none_returns_unknown():
    assert _decode_mode(None) == "UNKNOWN"


def test_decode_mode_unrecognised_int():
    assert _decode_mode(99) == "UNKNOWN(99)"


@pytest.mark.parametrize("minutes,expected", [
    (0, "00:00"),
    (60, "01:00"),
    (480, "08:00"),
    (1200, "20:00"),
    (1439, "23:59"),
])
def test_format_schedule_time_valid(minutes, expected):
    assert _format_schedule_time(minutes) == expected


def test_format_schedule_time_disabled():
    assert _format_schedule_time(65535) is None


def test_format_schedule_time_none():
    assert _format_schedule_time(None) is None


@pytest.mark.parametrize("s", ["00:00", "06:30", "08:00", "12:00", "20:00", "23:59"])
def test_schedule_time_roundtrip(s):
    """_format_schedule_time and _parse_schedule_time must be inverses (P2-F017).

    Independent tests for each direction don't catch a regression that makes
    one rounder or stricter than the other. Roundtrip pins them together.
    """
    assert _format_schedule_time(_parse_schedule_time(s)) == s


@pytest.mark.parametrize("invalid_minutes", [1440, 1500, 65534, -1, -100])
def test_format_schedule_time_out_of_range_returns_none(invalid_minutes):
    """Out-of-range minutes (>= 1440 except sentinel 65535, or negative) → None (P2-F018).

    A corrupt or unset field is indistinguishable from disabled — surfacing
    None is safer than synthesizing nonsense like "25:00".
    """
    assert _format_schedule_time(invalid_minutes) is None


# ============ _sanitize_api_string helper ============

def test_sanitize_api_string_normal_string_unchanged():
    assert _sanitize_api_string("Moderate Airflow", 64) == "Moderate Airflow"


def test_sanitize_api_string_strips_control_chars():
    assert _sanitize_api_string("Fan\x00Name", 64) == "FanName"


def test_sanitize_api_string_strips_format_control_chars():
    assert _sanitize_api_string("Fan​Name", 64) == "FanName"  # U+200B zero-width space (Cf)


def test_sanitize_api_string_preserves_non_ascii_printable():
    assert _sanitize_api_string("排気ファン", 64) == "排気ファン"


def test_sanitize_api_string_truncates_to_max_len():
    assert _sanitize_api_string("A" * 100, 10) == "A" * 10


def test_sanitize_api_string_empty_string_returns_unnamed():
    assert _sanitize_api_string("", 64) == "(unnamed)"


def test_sanitize_api_string_none_returns_unnamed():
    assert _sanitize_api_string(None, 64) == "(unnamed)"


def test_sanitize_api_string_all_control_chars_returns_unnamed():
    assert _sanitize_api_string("\x00\x01\x02", 64) == "(unnamed)"


# ============ get_port_status ============

async def test_get_port_status_success(mock_client):
    result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert data["device_id"] == "C58ZA"
    assert data["port"] == 1
    assert data["port_name"] == "Intake Fan"
    assert data["power_level"] == 5
    assert "plug_status" not in data
    assert data["mode"] == "AUTO"        # curMode=3
    assert "remain_time_seconds" not in data


async def test_get_port_status_mode_on(mock_client):
    """Port 2 in conftest has curMode=2 → ON."""
    result = await get_port_status("C58ZA", 2)
    data = json.loads(result)
    assert data["mode"] == "ON"


async def test_get_port_status_remain_time_none_absent_from_output(mock_client):
    """remainTime=None → remain_time_seconds absent (not a zero value)."""
    mock_client.get_devices.return_value = [{
        **MOCK_DEVICE_LEGACY,
        "deviceInfo": {
            **MOCK_DEVICE_LEGACY["deviceInfo"],
            "ports": [
                {"port": 1, "portName": "Fan", "speak": 3,
                 "portsLoad": 1, "loadState": 1, "curMode": 3, "remainTime": None},
            ],
        },
    }]
    result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert "remain_time_seconds" not in data


async def test_get_port_status_remain_time_positive_included(mock_client):
    """remainTime=300 → remain_time_seconds=300 present in output."""
    mock_client.get_devices.return_value = [{
        **MOCK_DEVICE_LEGACY,
        "deviceInfo": {
            **MOCK_DEVICE_LEGACY["deviceInfo"],
            "ports": [
                {"port": 1, "portName": "Fan", "speak": 5,
                 "portsLoad": 1, "loadState": 1, "curMode": 4, "remainTime": 300},
            ],
        },
    }]
    result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert data["remain_time_seconds"] == 300


async def test_get_port_status_load_not_detected(mock_client):
    """loadState=0 → plug_status='not powered'."""
    mock_client.get_devices.return_value = [{
        **MOCK_DEVICE_LEGACY,
        "deviceInfo": {
            **MOCK_DEVICE_LEGACY["deviceInfo"],
            "ports": [
                {"port": 1, "portName": "Port 1", "speak": 0,
                 "portsLoad": 0, "loadState": 0, "curMode": 1, "remainTime": 0},
            ],
        },
    }]
    result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert data["plug_status"] == "not powered"
    assert data["mode"] == "OFF"


@pytest.mark.parametrize("load_state,expect_plug_status", [
    (1, False),    # loadState=1 (powered) → plug_status absent
    (0, True),     # loadState=0 (not powered, speed=0) → plug_status present
    (None, True),  # loadState absent → defaults to 0 → plug_status present
])
async def test_get_port_status_plug_status_conditional(mock_client, load_state, expect_plug_status):
    """plug_status appears only when loadState is falsy."""
    port_data: dict = {"port": 1, "portName": "Port 1", "speak": 0,
                       "portsLoad": 0, "curMode": 1, "remainTime": 0}
    if load_state is not None:
        port_data["loadState"] = load_state
    mock_client.get_devices.return_value = [{
        **MOCK_DEVICE_LEGACY,
        "deviceInfo": {**MOCK_DEVICE_LEGACY["deviceInfo"], "ports": [port_data]},
    }]
    result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert ("plug_status" in data) == expect_plug_status
    if expect_plug_status:
        assert data["plug_status"] == "not powered"


async def test_get_port_status_custom_named_off_no_plug_status(mock_client):
    """Custom-named port with loadState=0 and speak=0 → plug_status absent (TC-003).

    A grower-named port implies a device is intentionally connected; OFF state
    cannot be distinguished from 'nothing plugged in' by loadState alone.
    """
    mock_client.get_devices.return_value = [{
        **MOCK_DEVICE_LEGACY,
        "deviceInfo": {
            **MOCK_DEVICE_LEGACY["deviceInfo"],
            "ports": [
                {"port": 1, "portName": "Heater", "speak": 0,
                 "portsLoad": 0, "loadState": 0, "curMode": 1, "remainTime": 0},
            ],
        },
    }]
    result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert "plug_status" not in data
    assert data["mode"] == "OFF"


async def test_get_port_status_default_named_off_has_plug_status(mock_client):
    """Default-named port with loadState=0 and speak=0 → plug_status present."""
    mock_client.get_devices.return_value = [{
        **MOCK_DEVICE_LEGACY,
        "deviceInfo": {
            **MOCK_DEVICE_LEGACY["deviceInfo"],
            "ports": [
                {"port": 3, "portName": "Port 3", "speak": 0,
                 "portsLoad": 0, "loadState": 0, "curMode": 1, "remainTime": 0},
            ],
        },
    }]
    result = await get_port_status("C58ZA", 3)
    data = json.loads(result)
    assert data["plug_status"] == "not powered"
    assert data["mode"] == "OFF"


async def test_get_port_status_running_port_no_plug_status(mock_client):
    """speak>0 suppresses plug_status even when loadState=0 (fan running, no load signal)."""
    mock_client.get_devices.return_value = [{
        **MOCK_DEVICE_LEGACY,
        "deviceInfo": {
            **MOCK_DEVICE_LEGACY["deviceInfo"],
            "ports": [
                {"port": 1, "portName": "Filter", "speak": 5,
                 "portsLoad": 0, "loadState": 0, "curMode": 15, "remainTime": 0},
            ],
        },
    }]
    result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert "plug_status" not in data
    assert data["power_level"] == 5


async def test_get_port_status_loadstate1_speak0_no_plug_status(mock_client):
    """loadState=1 (current flowing) suppresses plug_status even when speed=0."""
    mock_client.get_devices.return_value = [{
        **MOCK_DEVICE_LEGACY,
        "deviceInfo": {
            **MOCK_DEVICE_LEGACY["deviceInfo"],
            "ports": [
                {"port": 1, "portName": "Fan", "speak": 0,
                 "portsLoad": 1, "loadState": 1, "curMode": 1, "remainTime": 0},
            ],
        },
    }]
    result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert "plug_status" not in data
    assert data["power_level"] == 0


async def test_get_port_status_device_not_found(mock_client):
    result = await get_port_status("NOTEXIST", 1)
    data = json.loads(result)
    assert "error" in data
    assert "NOTEXIST" in data["error"]


async def test_get_port_status_port_not_found(mock_client):
    result = await get_port_status("C58ZA", 99)
    data = json.loads(result)
    assert "error" in data
    assert "99" in data["error"]


async def test_get_port_status_port_zero(mock_client):
    result = await get_port_status("C58ZA", 0)
    data = json.loads(result)
    assert "error" in data
    assert "port" in data["error"]


async def test_get_port_status_human_summary_present(mock_client):
    """human_summary describes port name, mode, and speed in one sentence."""
    result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert "human_summary" in data
    summary = data["human_summary"]
    assert "Intake Fan" in summary or "Port 1" in summary
    assert "AUTO" in summary or "speed" in summary


async def test_get_port_status_auth_error(mock_client):
    mock_client.get_devices.side_effect = ACInfinityAuthError("token expired")
    result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert "Authentication failed" in data["error"]
    assert "detail" in data


async def test_get_port_status_api_error(mock_client):
    mock_client.get_devices.side_effect = ACInfinityAPIError("API error 503")
    result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert data["error"] == "AC Infinity API error"
    assert "detail" in data


async def test_get_port_status_generic_exception(mock_client):
    mock_client.get_devices.side_effect = RuntimeError("unexpected crash")
    result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert "error" in data


async def test_get_port_status_advance_mode_via_is_open_automation(mock_client):
    """isOpenAutomation=1 returns mode: Automation with name from automation lookup."""
    mock_client.get_devices.return_value = [{
        **MOCK_DEVICE_LEGACY,
        "deviceInfo": {
            **MOCK_DEVICE_LEGACY["deviceInfo"],
            "ports": [
                {"port": 1, "portName": "Left Fan", "speak": 2, "portsLoad": 1,
                 "loadState": 1, "curMode": 1, "remainTime": 0, "isOpenAutomation": 1},
            ],
        },
    }]
    result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert data["mode"] == "Automation"
    assert data["power_level"] == 2
    # Port 1 is not covered by any automation in the default fixture bitmasks:
    # grouptDevType 48=ports5+6, 8=port4, 4=port3 — none cover port 1.
    assert "automation_name" not in data
    mock_client.get_mode_settings.assert_not_called()
    mock_client.get_advance_automations.assert_called_once_with("1424979258063367506")


async def test_get_port_status_genuine_off_no_secondary_call(mock_client):
    """curMode=1 (OFF) with speak=0 is genuine OFF — secondary call not made."""
    mock_client.get_devices.return_value = [{
        **MOCK_DEVICE_LEGACY,
        "deviceInfo": {
            **MOCK_DEVICE_LEGACY["deviceInfo"],
            "ports": [
                {"port": 1, "portName": "Filter", "speak": 0, "portsLoad": 1,
                 "loadState": 0, "curMode": 1, "remainTime": 0},
            ],
        },
    }]
    result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert data["mode"] == "OFF"
    mock_client.get_mode_settings.assert_not_called()


async def test_get_port_status_advance_heuristic_curmode1_speak_nonzero(mock_client):
    """curMode=1 with speak>0 triggers secondary call; modeType=15 → Automation."""
    mock_client.get_devices.return_value = [{
        **MOCK_DEVICE_LEGACY,
        "deviceInfo": {
            **MOCK_DEVICE_LEGACY["deviceInfo"],
            "ports": [
                {"port": 1, "portName": "Right Fan", "speak": 2, "portsLoad": 1,
                 "loadState": 1, "curMode": 1, "remainTime": 0},
            ],
        },
    }]
    mock_client.get_mode_settings.return_value = {"modeType": 15}
    result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert data["mode"] == "Automation"
    mock_client.get_mode_settings.assert_called_once()
    mock_client.get_advance_automations.assert_called_once_with("1424979258063367506")


async def test_get_port_status_advance_heuristic_secondary_call_returns_non_advance(mock_client):
    """curMode=1 with speak>0 triggers secondary call; modeType!=15 → OFF (fallback)."""
    mock_client.get_devices.return_value = [{
        **MOCK_DEVICE_LEGACY,
        "deviceInfo": {
            **MOCK_DEVICE_LEGACY["deviceInfo"],
            "ports": [
                {"port": 1, "portName": "Fan", "speak": 3, "portsLoad": 1,
                 "loadState": 1, "curMode": 1, "remainTime": 0},
            ],
        },
    }]
    mock_client.get_mode_settings.return_value = {"modeType": 2}
    result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert data["mode"] == "OFF"
    mock_client.get_advance_automations.assert_not_called()


async def test_get_port_status_advance_automation_name_resolved(mock_client):
    """isOpenAutomation=1 on port 4 resolves automation_name from MOCK_ADVANCE_AUTOMATIONS_LIST."""
    # MOCK_ADVANCE_AUTOMATIONS_LIST has advId=2179295, advName="Moderate Airflow",
    # grouptDevType=8 (bit 3 = Port 4), isOn=1, runState=1 — covers port 4.
    mock_client.get_devices.return_value = [{
        **MOCK_DEVICE_LEGACY,
        "deviceInfo": {
            **MOCK_DEVICE_LEGACY["deviceInfo"],
            "ports": [
                {"port": 4, "portName": "Humidifier", "speak": 3, "portsLoad": 1,
                 "loadState": 1, "curMode": 1, "remainTime": 0, "isOpenAutomation": 1},
            ],
        },
    }]
    result = await get_port_status("C58ZA", 4)
    data = json.loads(result)
    assert data["mode"] == "Automation"
    assert data["automation_name"] == "Moderate Airflow"


async def test_get_port_status_advance_automation_lookup_fails_graceful(mock_client):
    """get_advance_automations raises RuntimeError → mode is Automation, no automation_name."""
    mock_client.get_advance_automations.side_effect = RuntimeError("timeout")
    mock_client.get_devices.return_value = [{
        **MOCK_DEVICE_LEGACY,
        "deviceInfo": {
            **MOCK_DEVICE_LEGACY["deviceInfo"],
            "ports": [
                {"port": 1, "portName": "Left Fan", "speak": 2, "portsLoad": 1,
                 "loadState": 1, "curMode": 1, "remainTime": 0, "isOpenAutomation": 1},
            ],
        },
    }]
    result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert data["mode"] == "Automation"
    assert "automation_name" not in data


async def test_get_port_status_advance_automation_api_error_graceful(mock_client):
    """get_advance_automations raises ACInfinityAPIError → swallowed, mode=Automation, no name."""
    mock_client.get_advance_automations.side_effect = ACInfinityAPIError("503 Service Unavailable")
    mock_client.get_devices.return_value = [{
        **MOCK_DEVICE_LEGACY,
        "deviceInfo": {
            **MOCK_DEVICE_LEGACY["deviceInfo"],
            "ports": [
                {"port": 1, "portName": "Left Fan", "speak": 2, "portsLoad": 1,
                 "loadState": 1, "curMode": 1, "remainTime": 0, "isOpenAutomation": 1},
            ],
        },
    }]
    result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert data["mode"] == "Automation"
    assert "automation_name" not in data
    assert "error" not in data


async def test_get_port_status_advance_automation_auth_error_propagates(mock_client):
    """get_advance_automations raises ACInfinityAuthError → auth error JSON returned."""
    mock_client.get_advance_automations.side_effect = ACInfinityAuthError("token expired")
    mock_client.get_devices.return_value = [{
        **MOCK_DEVICE_LEGACY,
        "deviceInfo": {
            **MOCK_DEVICE_LEGACY["deviceInfo"],
            "ports": [
                {"port": 1, "portName": "Left Fan", "speak": 2, "portsLoad": 1,
                 "loadState": 1, "curMode": 1, "remainTime": 0, "isOpenAutomation": 1},
            ],
        },
    }]
    result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert "Authentication failed" in data["error"]
    assert "detail" in data


async def test_get_port_status_advance_automation_disabled_no_name(mock_client):
    """Disabled automation (isOn=0, runState=0) for port 3 → mode Automation, no automation_name."""
    # MOCK_ADVANCE_AUTOMATIONS_LIST has "Pollenation Airflow" with grouptDevType=4
    # covering port 3, BUT isOn=0, runState=0 — disabled, so not governing.
    mock_client.get_devices.return_value = [{
        **MOCK_DEVICE_LEGACY,
        "deviceInfo": {
            **MOCK_DEVICE_LEGACY["deviceInfo"],
            "ports": [
                {"port": 3, "portName": "Filter", "speak": 2, "portsLoad": 1,
                 "loadState": 1, "curMode": 1, "remainTime": 0, "isOpenAutomation": 1},
            ],
        },
    }]
    result = await get_port_status("C58ZA", 3)
    data = json.loads(result)
    assert data["mode"] == "Automation"
    assert "automation_name" not in data


async def test_get_port_status_advance_automation_empty_list(mock_client):
    """get_advance_automations returns [] → mode is Automation, no automation_name."""
    mock_client.get_advance_automations.return_value = []
    mock_client.get_devices.return_value = [{
        **MOCK_DEVICE_LEGACY,
        "deviceInfo": {
            **MOCK_DEVICE_LEGACY["deviceInfo"],
            "ports": [
                {"port": 1, "portName": "Left Fan", "speak": 2, "portsLoad": 1,
                 "loadState": 1, "curMode": 1, "remainTime": 0, "isOpenAutomation": 1},
            ],
        },
    }]
    result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert data["mode"] == "Automation"
    assert "automation_name" not in data


async def test_get_port_status_advance_missing_dev_id(mock_client):
    """ADVANCE port with devId absent → mode is Automation, no automation lookup attempted."""
    mock_client.get_devices.return_value = [{
        **MOCK_DEVICE_LEGACY,
        "devId": None,
        "deviceInfo": {
            **MOCK_DEVICE_LEGACY["deviceInfo"],
            "ports": [
                {"port": 1, "portName": "Left Fan", "speak": 2, "portsLoad": 1,
                 "loadState": 1, "curMode": 1, "remainTime": 0, "isOpenAutomation": 1},
            ],
        },
    }]
    result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert data["mode"] == "Automation"
    assert "automation_name" not in data
    mock_client.get_advance_automations.assert_not_called()


async def test_get_port_status_curmode_not_in_mode_labels_secondary_call(mock_client):
    """curMode not in _MODE_LABELS (e.g. None) triggers secondary call to verify ADVANCE."""
    mock_client.get_devices.return_value = [{
        **MOCK_DEVICE_LEGACY,
        "deviceInfo": {
            **MOCK_DEVICE_LEGACY["deviceInfo"],
            "ports": [
                {"port": 1, "portName": "Fan", "speak": 0, "portsLoad": 1,
                 "loadState": 1, "curMode": None, "remainTime": 0},
            ],
        },
    }]
    mock_client.get_mode_settings.return_value = {"modeType": 0}
    result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert data["mode"] == "UNKNOWN"
    mock_client.get_mode_settings.assert_called_once()


async def test_get_port_status_check_advance_mode_exception_falls_back(mock_client):
    """If get_mode_settings raises in _check_advance_mode, falls back to decoded mode."""
    mock_client.get_devices.return_value = [{
        **MOCK_DEVICE_LEGACY,
        "deviceInfo": {
            **MOCK_DEVICE_LEGACY["deviceInfo"],
            "ports": [
                {"port": 1, "portName": "Fan", "speak": 2, "portsLoad": 1,
                 "loadState": 1, "curMode": 1, "remainTime": 0},
            ],
        },
    }]
    mock_client.get_mode_settings.side_effect = RuntimeError("network error")
    result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert data["mode"] == "OFF"  # fallback to decoded curMode=1


@pytest.mark.asyncio
async def test_check_advance_mode_disabled_automation_returns_fallback(mock_client):
    """_check_advance_mode with isOpenAutomation=0 returns fallback, not ADVANCE."""
    mock_client.get_mode_settings.return_value = {"modeType": 15, "isOpenAutomation": 0}
    result = await _check_advance_mode(dev_id="11001", port=1, fallback="OFF")
    assert result == "OFF"


@pytest.mark.asyncio
async def test_check_advance_mode_active_automation_returns_advance(mock_client):
    """_check_advance_mode with isOpenAutomation=1 returns ADVANCE."""
    mock_client.get_mode_settings.return_value = {"modeType": 15, "isOpenAutomation": 1}
    result = await _check_advance_mode(dev_id="11001", port=1, fallback="OFF")
    assert result == "ADVANCE"


# ============ get_port_settings ============

MOCK_MODE_SETTINGS_BASIC: dict = {
    "atType": 1,
    "onSpead": 5,
    "targetVpdSwitch": 0,
    "targetVpd": 0,
    "activeLt": 0,
    "activeHt": 0,
    "devLt": 0,
    "devHt": 90,
    "activeLh": 0,
    "activeHh": 0,
    "devLh": 0,
    "devHh": 100,
    "schedStartTime": 65535,
    "schedEndtTime": 65535,
    "activeCycleOn": 300,
    "activeCycleOff": 60,
    "acitveTimerOn": 0,
    "acitveTimerOff": 0,
}


async def test_get_port_settings_success_basic(mock_client):
    mock_client.get_mode_settings.return_value = MOCK_MODE_SETTINGS_BASIC
    result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert data["device_id"] == "C58ZA"
    assert data["port"] == 1
    assert data["mode"] == "OFF"       # atType=1
    assert data["speed_target"] == 5
    assert data["vpd_target_kpa"] is None
    assert data["temp_range"] is None
    assert data["humidity_range_pct"] is None
    assert data["schedule_window"] is None
    assert data["cycle_on_seconds"] == 300
    assert data["cycle_off_seconds"] == 60
    # timer fields are omitted when 0 (not configured)
    assert "timer_on_seconds" not in data
    assert "timer_off_seconds" not in data


async def test_get_port_settings_timer_fields_present_when_nonzero(mock_client):
    """Non-zero timer values are included in the response (only omitted when 0)."""
    settings = {**MOCK_MODE_SETTINGS_BASIC, "acitveTimerOn": 3600, "acitveTimerOff": 7200}
    mock_client.get_mode_settings.return_value = settings
    result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert data["timer_on_seconds"] == 3600
    assert data["timer_off_seconds"] == 7200


async def test_get_port_settings_vpd_target_active(mock_client):
    """targetVpdSwitch=1 → vpd_target_kpa populated (targetVpd / 10)."""
    settings = {**MOCK_MODE_SETTINGS_BASIC, "targetVpdSwitch": 1, "targetVpd": 14}
    mock_client.get_mode_settings.return_value = settings
    result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert data["vpd_target_kpa"] == 1.4


@pytest.mark.parametrize("raw_target_vpd", [-1, -1_000_000, 1000, 99999, "garbage", None])
async def test_get_port_settings_vpd_target_out_of_range_is_none(mock_client, raw_target_vpd):
    """Corrupted/out-of-range targetVpd from upstream parses to null, not nonsense (P3-F020)."""
    settings = {**MOCK_MODE_SETTINGS_BASIC, "targetVpdSwitch": 1, "targetVpd": raw_target_vpd}
    mock_client.get_mode_settings.return_value = settings
    result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert data["vpd_target_kpa"] is None


async def test_get_port_settings_temp_range_active(mock_client):
    """activeLt=1 and activeHt=1 → temp_range populated with preferred unit (°C for unit=1)."""
    settings = {**MOCK_MODE_SETTINGS_BASIC, "activeLt": 1, "activeHt": 1,
                "devLt": 20, "devHt": 28}
    mock_client.get_mode_settings.return_value = settings
    result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert data["temp_range"] == {"min": 20.0, "max": 28.0, "unit": "°C"}


async def test_get_port_settings_humidity_range_active(mock_client):
    """activeLh=1 → humidity_range_pct populated."""
    settings = {**MOCK_MODE_SETTINGS_BASIC, "activeLh": 1, "devLh": 40, "devHh": 70}
    mock_client.get_mode_settings.return_value = settings
    result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert data["humidity_range_pct"] == {"min_pct": 40, "max_pct": 70}


async def test_get_port_settings_schedule_window_active(mock_client):
    """schedStartTime != 65535 → schedule_window populated with HH:MM strings and timezone."""
    settings = {**MOCK_MODE_SETTINGS_BASIC, "schedStartTime": 480, "schedEndtTime": 1200}
    mock_client.get_mode_settings.return_value = settings
    result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert data["schedule_window"]["start"] == "08:00"
    assert data["schedule_window"]["end"] == "20:00"
    assert "timezone" in data["schedule_window"]


@pytest.mark.parametrize("start,end", [
    (480, 65535),    # start set, end disabled — partial = no window
    (65535, 1200),   # start disabled, end set — partial = no window
    (65535, 65535),  # both disabled — no window
])
async def test_get_port_settings_schedule_window_partial_is_none(mock_client, start, end):
    """Half-configured schedule must return schedule_window=None, not a partial dict (P2-F015)."""
    settings = {**MOCK_MODE_SETTINGS_BASIC, "schedStartTime": start, "schedEndtTime": end}
    mock_client.get_mode_settings.return_value = settings
    result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert data["schedule_window"] is None


async def test_get_port_settings_mode_auto(mock_client):
    settings = {**MOCK_MODE_SETTINGS_BASIC, "atType": 3}
    mock_client.get_mode_settings.return_value = settings
    result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert data["mode"] == "AUTO"


async def test_get_port_settings_device_not_found(mock_client):
    result = await get_port_settings("NOTEXIST", 1)
    data = json.loads(result)
    assert "error" in data
    assert "NOTEXIST" in data["error"]


async def test_get_port_settings_missing_dev_id(mock_client):
    """Device missing devId returns a clear error."""
    device_no_id = {k: v for k, v in MOCK_DEVICE_LEGACY.items() if k != "devId"}
    mock_client.get_devices.return_value = [device_no_id]
    result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert "error" in data
    assert "devId" in data["error"]


async def test_get_port_settings_port_zero(mock_client):
    result = await get_port_settings("C58ZA", 0)
    data = json.loads(result)
    assert "error" in data
    assert "port" in data["error"]


async def test_get_port_settings_auth_error(mock_client):
    mock_client.get_devices.side_effect = ACInfinityAuthError("token expired")
    result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert "Authentication failed" in data["error"]
    assert "detail" in data


async def test_get_port_settings_api_error(mock_client):
    mock_client.get_mode_settings.side_effect = ACInfinityAPIError("API error 503")
    result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert data["error"] == "AC Infinity API error"
    assert "detail" in data


async def test_get_port_settings_generic_exception(mock_client):
    mock_client.get_mode_settings.side_effect = RuntimeError("unexpected crash")
    result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert "error" in data


async def test_get_port_settings_advance_mode_returns_early(mock_client):
    """modeType=15 in settings returns ADVANCE mode enriched with automation info.

    Uses port=4 because MOCK_ADVANCE_AUTOMATIONS_LIST has grouptDevType=8 (Port 4),
    so the bitmask lookup resolves Moderate Airflow as the governing automation.
    """
    import copy
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    # Add port 4 to device so speak value is accessible.
    device["deviceInfo"]["ports"].append(
        {"port": 4, "portName": "Inline Fan", "speak": 3, "portsLoad": 1,
         "loadState": 1, "curMode": 15, "remainTime": 0}
    )
    mock_client.get_devices.return_value = [device]
    mock_client.get_mode_settings.return_value = {
        **MOCK_MODE_SETTINGS_BASIC,
        "modeType": 15,
        "onSpead": 2,
    }
    # Conftest default: get_advance_automations returns MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await get_port_settings("C58ZA", 4)
    data = json.loads(result)
    assert data["mode"] == "ADVANCE"
    assert data["advance_automation"] is True
    assert data["speed_target"] is None
    assert data["automation_name"] == "Moderate Airflow"
    assert data["automation_id"] == 1342758
    assert data["current_speed"] == 3  # from port 4 speak=3
    assert data["vpd_target_kpa"] is None
    assert data["temp_range"] is None
    assert data["humidity_range_pct"] is None
    assert data["schedule_window"] is None
    assert data["cycle_on_seconds"] is None
    assert data["cycle_off_seconds"] is None
    assert data["timer_on_seconds"] is None
    assert data["timer_off_seconds"] is None
    assert mock_client.get_advance_automations.call_count == 1


async def test_get_port_settings_empty_port_stale_note(mock_client):
    """Empty port with stale humidity settings: human_summary and advisory are staleness-aware."""
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["deviceInfo"]["ports"].append(
        {"port": 3, "portName": "Port 3", "speak": 0, "portsLoad": 0,
         "loadState": 0, "curMode": 3, "remainTime": 0}
    )
    mock_client.get_devices.return_value = [device]
    settings = {**MOCK_MODE_SETTINGS_BASIC, "activeLh": 1, "devLh": 60, "devHh": 100}
    mock_client.get_mode_settings.return_value = settings
    result = await get_port_settings("C58ZA", 3)
    data = json.loads(result)
    assert "stale" in data["human_summary"]
    assert "different port" in data["advisory"]
    assert "Humidity automation" not in data["human_summary"]
    assert data["humidity_range_pct"] == {"min_pct": 60, "max_pct": 100}  # raw data preserved


async def test_get_port_settings_empty_port_cycle_stale_note(mock_client):
    """Empty port with stale cycle settings: human_summary overridden, raw fields preserved."""
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["deviceInfo"]["ports"].append(
        {"port": 3, "portName": "Port 3", "speak": 0, "portsLoad": 0,
         "loadState": 0, "curMode": 1, "remainTime": 0}
    )
    mock_client.get_devices.return_value = [device]
    mock_client.get_mode_settings.return_value = MOCK_MODE_SETTINGS_BASIC  # has cycle_on=300
    result = await get_port_settings("C58ZA", 3)
    data = json.loads(result)
    assert "stale" in data["human_summary"]
    assert "OFF mode" not in data["human_summary"]
    assert data["cycle_on_seconds"] == 300  # raw data preserved


async def test_get_port_settings_connected_port_no_stale_note(mock_client):
    """Port 1 (Intake Fan) has portResistance=7500 — primary signal False, no staleness advisory."""
    mock_client.get_mode_settings.return_value = MOCK_MODE_SETTINGS_BASIC
    result = await get_port_settings("C58ZA", 1)  # port 1 = "Intake Fan", portResistance=7500
    data = json.loads(result)
    assert "advisory" not in data
    assert "stale" not in data.get("human_summary", "")


async def test_get_port_settings_advance_empty_port_human_summary_unchanged(mock_client):
    """ADVANCE mode + empty port: human_summary is NOT overridden with stale message."""
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["deviceInfo"]["ports"].append(
        {"port": 3, "portName": "Port 3", "speak": 0, "portsLoad": 0,
         "loadState": 0, "curMode": 15, "remainTime": 0}
    )
    mock_client.get_devices.return_value = [device]
    mock_client.get_mode_settings.return_value = {
        **MOCK_MODE_SETTINGS_BASIC, "modeType": 15
    }
    mock_client.get_advance_automations.return_value = []
    result = await get_port_settings("C58ZA", 3)
    data = json.loads(result)
    assert data["mode"] == "ADVANCE"
    # human_summary should still describe the automation state, not the stale message
    assert "stale" not in data["human_summary"]
    assert (
        "automations are disabled" in data["human_summary"]
        or "automation" in data["human_summary"].lower()
    )
    # advisory should contain the staleness message
    assert "stale" in data["advisory"]


async def test_get_port_settings_advance_governing_found_empty_port_human_summary_unchanged(
    mock_client,
):
    """ADVANCE mode + governing automation found + empty port: human_summary preserved."""
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["deviceInfo"]["ports"].append(
        {"port": 4, "portName": "Port 4", "speak": 3, "portsLoad": 0,
         "loadState": 0, "curMode": 15, "remainTime": 0}
    )
    mock_client.get_devices.return_value = [device]
    mock_client.get_mode_settings.return_value = {
        **MOCK_MODE_SETTINGS_BASIC, "modeType": 15
    }
    # MOCK_ADVANCE_AUTOMATIONS_LIST has grouptDevType=8 (port 4 bitmask)
    result = await get_port_settings("C58ZA", 4)
    data = json.loads(result)
    assert data["mode"] == "ADVANCE"
    # human_summary preserved — should reference the governing automation
    assert "stale" not in data["human_summary"]
    assert data["automation_name"] is not None
    # advisory should contain the staleness message
    assert "stale" in data["advisory"]


async def test_get_port_settings_advance_degraded_empty_port_note_concatenated(mock_client):
    """ADVANCE mode + degraded secondary call + empty port: advisory concatenates both messages."""
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["deviceInfo"]["ports"].append(
        {"port": 3, "portName": "Port 3", "speak": 0, "portsLoad": 0,
         "loadState": 0, "curMode": 15, "remainTime": 0}
    )
    mock_client.get_devices.return_value = [device]
    mock_client.get_mode_settings.return_value = {
        **MOCK_MODE_SETTINGS_BASIC, "modeType": 15
    }
    mock_client.get_advance_automations.side_effect = ACInfinityAPIError("fail")
    result = await get_port_settings("C58ZA", 3)
    data = json.loads(result)
    assert data["mode"] == "ADVANCE"
    advisory = data["advisory"]
    # Both degraded and stale messages must be present
    assert "automation" in advisory.lower()  # from degraded: "Could not fetch automation details"
    assert "stale" in advisory               # from empty-port stale advisory
    # human_summary should NOT be overridden with stale message
    assert "stale" not in data["human_summary"]


async def test_get_port_settings_portresistance_custom_name_stale_note(mock_client):
    """portResistance=65535 + custom-named port → staleness advisory fires (core #183 fix).

    Previously, custom names caused _is_port_empty to return False (assumed connected),
    so a removed device named "Humidifier" would silently show stale automation settings.
    """
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["deviceInfo"]["ports"].append(
        {"port": 3, "portName": "Humidifier", "speak": 0, "portsLoad": 0,
         "loadState": 0, "curMode": 3, "remainTime": 0, "portResistance": 65535}
    )
    mock_client.get_devices.return_value = [device]
    settings = {**MOCK_MODE_SETTINGS_BASIC, "activeLh": 1, "devLh": 60, "devHh": 100}
    mock_client.get_mode_settings.return_value = settings
    result = await get_port_settings("C58ZA", 3)
    data = json.loads(result)
    assert "stale" in data["human_summary"]
    assert "different port" in data["advisory"]
    assert "Humidity automation" not in data["human_summary"]
    assert data["humidity_range_pct"] == {"min_pct": 60, "max_pct": 100}  # raw data preserved


# ============ _parse_schedule_time ============

def test_parse_schedule_time_valid():
    assert _parse_schedule_time("08:00") == 480
    assert _parse_schedule_time("00:00") == 0
    assert _parse_schedule_time("23:59") == 1439
    assert _parse_schedule_time("06:30") == 390


def test_parse_schedule_time_none_returns_disabled():
    assert _parse_schedule_time(None) == 65535


def test_parse_schedule_time_invalid_raises():
    with pytest.raises(ValueError, match="Invalid schedule time"):
        _parse_schedule_time("25:00")
    with pytest.raises(ValueError, match="Invalid schedule time"):
        _parse_schedule_time("not-a-time")


# ============ set_vpd_automation ============

MOCK_VPD_DRY = {
    "payload": {"atType": 8, "targetVpd": 14, "vpdSettingMode": 1, "targetVpdSwitch": 1},
    "dry_run": True,
    "controller_type": "legacy",
    "sent": False,
}
MOCK_VPD_LIVE = {
    "payload": {},
    "dry_run": False,
    "controller_type": "legacy",
    "sent": True,
}


async def test_set_vpd_automation_dry_run(mock_client):
    mock_client.set_port_mode.return_value = MOCK_VPD_DRY
    result = await set_vpd_automation("C58ZA", 1, 1.4, dry_run=True)
    data = json.loads(result)
    assert data["dry_run"] is True
    assert data["sent"] is False
    assert data["target_vpd_kpa"] == 1.4
    assert "payload" in data
    assert data["controller_type"] == "legacy"


async def test_set_vpd_automation_live(mock_client):
    mock_client.set_port_mode.return_value = MOCK_VPD_LIVE
    result = await set_vpd_automation("C58ZA", 1, 1.4, dry_run=False)
    data = json.loads(result)
    assert data["sent"] is True
    assert "payload" not in data


async def test_set_vpd_automation_payload_encoding(mock_client):
    """targetVpd must be stored as kPa × 10 (not × 100)."""
    mock_client.set_port_mode.return_value = MOCK_VPD_DRY
    await set_vpd_automation("C58ZA", 1, 1.4)
    call_updates = mock_client.set_port_mode.call_args[0][2]
    assert call_updates["atType"] == 8
    assert call_updates["targetVpd"] == 14   # 1.4 × 10
    assert call_updates["vpdSettingMode"] == 1
    assert call_updates["targetVpdSwitch"] == 1


async def test_set_vpd_automation_no_bankers_rounding(mock_client):
    """1.25 kPa must encode as 13, not 12 (Python banker's rounding would give 12)."""
    mock_client.set_port_mode.return_value = MOCK_VPD_DRY
    await set_vpd_automation("C58ZA", 1, 1.25)
    call_updates = mock_client.set_port_mode.call_args[0][2]
    assert call_updates["targetVpd"] == 13   # int(12.5 + 0.5) = 13, not round(12.5) = 12


async def test_set_vpd_automation_target_too_low(mock_client):
    result = await set_vpd_automation("C58ZA", 1, 0.0)
    data = json.loads(result)
    assert "error" in data
    assert "0.1" in data["error"]


async def test_set_vpd_automation_target_too_high(mock_client):
    result = await set_vpd_automation("C58ZA", 1, 3.1)
    data = json.loads(result)
    assert "error" in data
    # P2-C2-F009: pin that the bounds-check fired, not some downstream error
    assert "3.0" in data["error"] or "3.1" in data["error"]


async def test_set_vpd_automation_boundary_min_valid(mock_client):
    mock_client.set_port_mode.return_value = MOCK_VPD_DRY
    result = await set_vpd_automation("C58ZA", 1, 0.1)
    data = json.loads(result)
    assert "error" not in data


async def test_set_vpd_automation_boundary_max_valid(mock_client):
    mock_client.set_port_mode.return_value = MOCK_VPD_DRY
    result = await set_vpd_automation("C58ZA", 1, 3.0)
    data = json.loads(result)
    assert "error" not in data


async def test_set_vpd_automation_device_not_found(mock_client):
    result = await set_vpd_automation("INVALID", 1, 1.4)
    data = json.loads(result)
    assert "error" in data
    assert "INVALID" in data["error"]


async def test_set_vpd_automation_port_zero(mock_client):
    result = await set_vpd_automation("C58ZA", 0, 1.4)
    data = json.loads(result)
    assert "error" in data
    assert "port" in data["error"]


async def test_set_vpd_automation_api_error(mock_client):
    mock_client.set_port_mode.side_effect = ACInfinityAPIError("server error")
    result = await set_vpd_automation("C58ZA", 1, 1.4)
    data = json.loads(result)
    assert "error" in data


async def test_set_vpd_automation_auth_error(mock_client):
    mock_client.set_port_mode.side_effect = ACInfinityAuthError("token expired")
    result = await set_vpd_automation("C58ZA", 1, 1.4)
    data = json.loads(result)
    assert "error" in data


async def test_set_vpd_automation_generic_exception(mock_client):
    mock_client.set_port_mode.side_effect = RuntimeError("crash")
    result = await set_vpd_automation("C58ZA", 1, 1.4)
    data = json.loads(result)
    assert "error" in data


async def test_set_vpd_automation_advance_conflict(mock_client):
    """ACInfinityAdvanceConflictError → structured conflict response, not a generic error."""
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("modeType=15")
    result = await set_vpd_automation("C58ZA", 1, 1.4)
    data = json.loads(result)
    assert data["conflict"] == "ADVANCE_AUTOMATION"
    assert "summary" in data
    assert "error" not in data


async def test_set_vpd_automation_device_error_non_advance(mock_client):
    """Base ACInfinityDeviceError → plain error response."""
    mock_client.set_port_mode.side_effect = ACInfinityDeviceError("loadType guard")
    result = await set_vpd_automation("C58ZA", 1, 1.4)
    data = json.loads(result)
    assert "error" in data
    assert "loadType guard" in data["error"]


# ============ set_temperature_automation ============

MOCK_TEMP_DRY = {
    "payload": {"atType": 3, "devLt": 20, "devHt": 28, "activeLt": 1, "activeHt": 1},
    "dry_run": True,
    "controller_type": "legacy",
    "sent": False,
}


async def test_set_temperature_automation_dry_run(mock_client):
    mock_client.set_port_mode.return_value = MOCK_TEMP_DRY
    result = await set_temperature_automation("C58ZA", 1, 20.0, 28.0, dry_run=True)
    data = json.loads(result)
    assert data["dry_run"] is True
    assert data["min_temp"] == 20.0
    assert data["max_temp"] == 28.0
    assert "payload" in data


async def test_set_temperature_automation_payload_encoding(mock_client):
    """devLt/devHt are raw Celsius integers — no × 100 scaling."""
    mock_client.set_port_mode.return_value = MOCK_TEMP_DRY
    await set_temperature_automation("C58ZA", 1, 20.0, 28.0)
    call_updates = mock_client.set_port_mode.call_args[0][2]
    assert call_updates["atType"] == 3
    assert call_updates["devLt"] == 20    # raw °C, not 2000
    assert call_updates["devHt"] == 28
    assert call_updates["activeLt"] == 1
    assert call_updates["activeHt"] == 1


async def test_set_temperature_automation_min_ge_max(mock_client):
    result = await set_temperature_automation("C58ZA", 1, 28.0, 20.0)
    data = json.loads(result)
    assert "error" in data
    assert "min_temp" in data["error"]


async def test_set_temperature_automation_equal_min_max(mock_client):
    result = await set_temperature_automation("C58ZA", 1, 25.0, 25.0)
    data = json.loads(result)
    assert "error" in data


async def test_set_temperature_automation_out_of_range(mock_client):
    result = await set_temperature_automation("C58ZA", 1, -1.0, 30.0)
    data = json.loads(result)
    assert "error" in data
    assert "0" in data["error"] and "50" in data["error"]  # range bounds in error (P2-C2-F009)


async def test_set_temperature_automation_max_out_of_range(mock_client):
    result = await set_temperature_automation("C58ZA", 1, 20.0, 51.0)
    data = json.loads(result)
    assert "error" in data
    assert "0" in data["error"] and "50" in data["error"]  # range bounds in error (P2-C2-F009)


async def test_set_temperature_automation_device_not_found(mock_client):
    result = await set_temperature_automation("INVALID", 1, 20.0, 28.0)
    data = json.loads(result)
    assert "error" in data


async def test_set_temperature_automation_port_zero(mock_client):
    result = await set_temperature_automation("C58ZA", 0, 20.0, 28.0)
    data = json.loads(result)
    assert "error" in data
    assert "port" in data["error"]

async def test_set_temperature_automation_api_error(mock_client):
    mock_client.set_port_mode.side_effect = ACInfinityAPIError("err")
    result = await set_temperature_automation("C58ZA", 1, 20.0, 28.0)
    assert "error" in json.loads(result)


async def test_set_temperature_automation_generic_exception(mock_client):
    mock_client.set_port_mode.side_effect = RuntimeError("crash")
    result = await set_temperature_automation("C58ZA", 1, 20.0, 28.0)
    assert "error" in json.loads(result)


async def test_set_temperature_automation_advance_conflict(mock_client):
    """ACInfinityAdvanceConflictError → structured conflict response."""
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("modeType=15")
    result = await set_temperature_automation("C58ZA", 1, 20.0, 28.0)
    data = json.loads(result)
    assert data["conflict"] == "ADVANCE_AUTOMATION"
    assert "error" not in data


async def test_set_temperature_automation_device_error_non_advance(mock_client):
    """Base ACInfinityDeviceError → plain error response."""
    mock_client.set_port_mode.side_effect = ACInfinityDeviceError("loadType guard")
    result = await set_temperature_automation("C58ZA", 1, 20.0, 28.0)
    data = json.loads(result)
    assert "error" in data
    assert "loadType guard" in data["error"]


@pytest.mark.parametrize(
    "min_c,max_c,expected_devLt,expected_devHt",
    [
        # Half-integer boundaries — banker's rounding (round()) would silently
        # disagree with the docstring's documented round-half-up at every .5
        # input. int(x + 0.5) is round-half-up.
        (0.5, 1.5, 1, 2),
        (1.5, 2.5, 2, 3),
        (20.5, 24.5, 21, 25),
        (49.0, 49.5, 49, 50),  # near-max °C boundary; 49.5 rounds half-up to 50
        # Non-half fractions should still round in the conventional direction
        (20.4, 24.6, 20, 25),
        (20.6, 24.4, 21, 24),
    ],
)
async def test_set_temperature_automation_no_bankers_rounding(
    mock_client, min_c, max_c, expected_devLt, expected_devHt,
):
    """Half-integer inputs round half-up, matching the docstring contract (P1-F002)."""
    mock_client.set_port_mode.return_value = MOCK_TEMP_DRY
    await set_temperature_automation("C58ZA", 1, min_c, max_c)
    updates = mock_client.set_port_mode.call_args[0][2]
    assert updates["devLt"] == expected_devLt
    assert updates["devHt"] == expected_devHt


# ============ set_humidity_automation ============

MOCK_HUMI_DRY = {
    "payload": {"atType": 3, "devLh": 50, "devHh": 70, "activeLh": 1, "activeHh": 1},
    "dry_run": True,
    "controller_type": "legacy",
    "sent": False,
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call_fn,call_args,mock_return",
    [
        (set_port_off, ("C58ZA", 7), MOCK_SET_PORT_OFF_DRY),
        (set_port_speed, ("C58ZA", 7, 3), MOCK_SET_PORT_MODE_DRY),
        (set_port_mode, ("C58ZA", 7, "ON"), MOCK_SET_PORT_MODE_DRY),
        (set_vpd_automation, ("C58ZA", 7, 1.4), MOCK_VPD_DRY),
        (set_temperature_automation, ("C58ZA", 7, 20.0, 28.0), MOCK_TEMP_DRY),
        (set_humidity_automation, ("C58ZA", 7, 50.0, 70.0), MOCK_HUMI_DRY),
    ],
    ids=["off", "speed", "mode", "vpd", "temp", "humi"],
)
async def test_write_tool_action_default_name_no_redundancy(
    mock_client, call_fn, call_args, mock_return
):
    """All write tools omit '(Port N)' suffix when portName equals the API default 'Port N'."""
    import copy
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["deviceInfo"]["ports"].append(
        {"port": 7, "portName": "Port 7", "speak": 0, "portsLoad": 0,
         "loadState": 0, "curMode": 1, "remainTime": None}
    )
    mock_client.get_devices.return_value = [device]
    mock_client.set_port_mode.return_value = mock_return
    result = await call_fn(*call_args)
    data = json.loads(result)
    assert "(Port 7)" not in data.get("action", "")


async def test_set_humidity_automation_dry_run(mock_client):
    mock_client.set_port_mode.return_value = MOCK_HUMI_DRY
    result = await set_humidity_automation("C58ZA", 1, 50.0, 70.0, dry_run=True)
    data = json.loads(result)
    assert data["dry_run"] is True
    assert data["min_rh"] == 50.0
    assert data["max_rh"] == 70.0
    assert "payload" in data


async def test_set_humidity_automation_payload_encoding(mock_client):
    """devLh/devHh are raw % integers — no × 100 scaling."""
    mock_client.set_port_mode.return_value = MOCK_HUMI_DRY
    await set_humidity_automation("C58ZA", 1, 50.0, 70.0)
    call_updates = mock_client.set_port_mode.call_args[0][2]
    assert call_updates["atType"] == 3
    assert call_updates["devLh"] == 50    # raw %, not 5000
    assert call_updates["devHh"] == 70
    assert call_updates["activeLh"] == 1
    assert call_updates["activeHh"] == 1


async def test_set_humidity_automation_min_ge_max(mock_client):
    result = await set_humidity_automation("C58ZA", 1, 70.0, 50.0)
    data = json.loads(result)
    assert "error" in data
    assert "min_rh" in data["error"]


async def test_set_humidity_automation_out_of_range(mock_client):
    result = await set_humidity_automation("C58ZA", 1, -1.0, 70.0)
    data = json.loads(result)
    assert "error" in data
    assert "between 0 and 100" in data["error"]  # P2-C2-F009


async def test_set_humidity_automation_max_out_of_range(mock_client):
    result = await set_humidity_automation("C58ZA", 1, 50.0, 101.0)
    data = json.loads(result)
    assert "error" in data
    assert "between 0 and 100" in data["error"]  # P2-C2-F009


async def test_set_humidity_automation_device_not_found(mock_client):
    result = await set_humidity_automation("INVALID", 1, 50.0, 70.0)
    data = json.loads(result)
    assert "error" in data


async def test_set_humidity_automation_port_zero(mock_client):
    result = await set_humidity_automation("C58ZA", 0, 50.0, 70.0)
    data = json.loads(result)
    assert "error" in data
    assert "port" in data["error"]

async def test_set_humidity_automation_api_error(mock_client):
    mock_client.set_port_mode.side_effect = ACInfinityAPIError("err")
    result = await set_humidity_automation("C58ZA", 1, 50.0, 70.0)
    assert "error" in json.loads(result)


async def test_set_humidity_automation_generic_exception(mock_client):
    mock_client.set_port_mode.side_effect = RuntimeError("crash")
    result = await set_humidity_automation("C58ZA", 1, 50.0, 70.0)
    assert "error" in json.loads(result)


async def test_set_humidity_automation_advance_conflict(mock_client):
    """ACInfinityAdvanceConflictError → structured conflict response."""
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("modeType=15")
    result = await set_humidity_automation("C58ZA", 1, 50.0, 70.0)
    data = json.loads(result)
    assert data["conflict"] == "ADVANCE_AUTOMATION"
    assert "error" not in data


async def test_set_humidity_automation_device_error_non_advance(mock_client):
    """Base ACInfinityDeviceError → plain error response."""
    mock_client.set_port_mode.side_effect = ACInfinityDeviceError("loadType guard")
    result = await set_humidity_automation("C58ZA", 1, 50.0, 70.0)
    data = json.loads(result)
    assert "error" in data
    assert "loadType guard" in data["error"]


@pytest.mark.parametrize(
    "min_rh,max_rh,expected_devLh,expected_devHh",
    [
        # Half-percent boundaries — banker's rounding (round()) would silently
        # disagree with the docstring's documented round-half-up at every .5
        # input. int(x + 0.5) is round-half-up.
        (0.5, 1.5, 1, 2),
        (50.5, 70.5, 51, 71),
        (99.5, 100.0, 100, 100),
        # Non-half fractions still round in the conventional direction
        (50.4, 70.6, 50, 71),
        (50.6, 70.4, 51, 70),
    ],
)
async def test_set_humidity_automation_no_bankers_rounding(
    mock_client, min_rh, max_rh, expected_devLh, expected_devHh,
):
    """Half-percent inputs round half-up, matching the docstring contract (P1-F002)."""
    mock_client.set_port_mode.return_value = MOCK_HUMI_DRY
    await set_humidity_automation("C58ZA", 1, min_rh, max_rh)
    updates = mock_client.set_port_mode.call_args[0][2]
    assert updates["devLh"] == expected_devLh
    assert updates["devHh"] == expected_devHh


# ============ set_port_mode ============

MOCK_MODE_DRY = {
    "payload": {"atType": 1},
    "dry_run": True,
    "controller_type": "legacy",
    "sent": False,
}
MOCK_MODE_LIVE = {
    "payload": {},
    "dry_run": False,
    "controller_type": "legacy",
    "sent": True,
}


async def test_set_port_mode_off_dry_run(mock_client):
    mock_client.set_port_mode.return_value = MOCK_MODE_DRY
    result = await set_port_mode("C58ZA", 1, "OFF")
    data = json.loads(result)
    assert data["mode"] == "OFF"
    assert data["dry_run"] is True
    assert "payload" in data


async def test_set_port_mode_on(mock_client):
    mock_client.set_port_mode.return_value = {**MOCK_MODE_DRY, "payload": {"atType": 2}}
    result = await set_port_mode("C58ZA", 1, "ON")
    data = json.loads(result)
    assert data["mode"] == "ON"
    call_updates = mock_client.set_port_mode.call_args[0][2]
    assert call_updates["atType"] == 2
    # ON must set a default nonzero speed so the port actually runs (P1-F003).
    # Without onSpead, a port whose prior onSpead was 0 would stay at speed 0.
    assert call_updates["onSpead"] == 10


async def test_set_port_mode_auto(mock_client):
    mock_client.set_port_mode.return_value = MOCK_MODE_DRY
    result = await set_port_mode("C58ZA", 1, "AUTO")
    data = json.loads(result)
    assert data["mode"] == "AUTO"
    call_updates = mock_client.set_port_mode.call_args[0][2]
    assert call_updates["atType"] == 3


async def test_set_port_mode_vpd(mock_client):
    mock_client.set_port_mode.return_value = MOCK_MODE_DRY
    result = await set_port_mode("C58ZA", 1, "VPD")
    data = json.loads(result)
    assert data["mode"] == "VPD"
    call_updates = mock_client.set_port_mode.call_args[0][2]
    assert call_updates["atType"] == 8


async def test_set_port_mode_case_insensitive(mock_client):
    mock_client.set_port_mode.return_value = MOCK_MODE_DRY
    result = await set_port_mode("C58ZA", 1, "off")
    data = json.loads(result)
    assert data["mode"] == "OFF"


async def test_set_port_mode_live(mock_client):
    mock_client.set_port_mode.return_value = MOCK_MODE_LIVE
    result = await set_port_mode("C58ZA", 1, "OFF", dry_run=False)
    data = json.loads(result)
    assert data["sent"] is True
    assert "payload" not in data


async def test_set_port_mode_invalid_mode(mock_client):
    result = await set_port_mode("C58ZA", 1, "INVALID")
    data = json.loads(result)
    assert "error" in data
    assert "INVALID" in data["error"]


async def test_set_port_mode_cycle_with_params(mock_client):
    mock_client.set_port_mode.return_value = MOCK_MODE_DRY
    result = await set_port_mode(
        "C58ZA", 1, "CYCLE", cycle_on_seconds=300, cycle_off_seconds=60
    )
    data = json.loads(result)
    assert data["mode"] == "CYCLE"
    call_updates = mock_client.set_port_mode.call_args[0][2]
    assert call_updates["atType"] == 6
    assert call_updates["activeCycleOn"] == 300
    assert call_updates["activeCycleOff"] == 60


async def test_set_port_mode_cycle_missing_on_param(mock_client):
    result = await set_port_mode("C58ZA", 1, "CYCLE", cycle_off_seconds=60)
    data = json.loads(result)
    assert "error" in data
    assert "cycle_on_seconds" in data["error"]


async def test_set_port_mode_cycle_missing_both_params(mock_client):
    result = await set_port_mode("C58ZA", 1, "CYCLE")
    data = json.loads(result)
    assert "error" in data


async def test_set_port_mode_cycle_zero_seconds(mock_client):
    result = await set_port_mode("C58ZA", 1, "CYCLE", cycle_on_seconds=0, cycle_off_seconds=60)
    data = json.loads(result)
    assert "error" in data


async def test_set_port_mode_schedule_with_params(mock_client):
    mock_client.set_port_mode.return_value = MOCK_MODE_DRY
    result = await set_port_mode(
        "C58ZA", 1, "SCHEDULE", schedule_start="08:00", schedule_end="20:00"
    )
    data = json.loads(result)
    assert data["mode"] == "SCHEDULE"
    call_updates = mock_client.set_port_mode.call_args[0][2]
    assert call_updates["atType"] == 7
    assert call_updates["schedStartTime"] == 480   # 8*60
    assert call_updates["schedEndtTime"] == 1200   # 20*60


async def test_set_port_mode_schedule_missing_params(mock_client):
    result = await set_port_mode("C58ZA", 1, "SCHEDULE", schedule_start="08:00")
    data = json.loads(result)
    assert "error" in data
    assert "schedule_end" in data["error"]


async def test_set_port_mode_schedule_invalid_time_format(mock_client):
    result = await set_port_mode(
        "C58ZA", 1, "SCHEDULE", schedule_start="bad", schedule_end="20:00"
    )
    data = json.loads(result)
    assert "error" in data


async def test_set_port_mode_timer_to_off_with_duration(mock_client):
    mock_client.set_port_mode.return_value = MOCK_MODE_DRY
    result = await set_port_mode(
        "C58ZA", 1, "TIMER_TO_OFF", timer_duration_seconds=3600
    )
    data = json.loads(result)
    assert data["mode"] == "TIMER_TO_OFF"
    call_updates = mock_client.set_port_mode.call_args[0][2]
    assert call_updates["atType"] == 5
    assert call_updates["acitveTimerOff"] == 3600


async def test_set_port_mode_timer_to_on_with_duration(mock_client):
    mock_client.set_port_mode.return_value = MOCK_MODE_DRY
    result = await set_port_mode(
        "C58ZA", 1, "TIMER_TO_ON", timer_duration_seconds=1800
    )
    data = json.loads(result)
    assert data["mode"] == "TIMER_TO_ON"
    call_updates = mock_client.set_port_mode.call_args[0][2]
    assert call_updates["atType"] == 4
    assert call_updates["acitveTimerOn"] == 1800


async def test_set_port_mode_timer_missing_duration(mock_client):
    result = await set_port_mode("C58ZA", 1, "TIMER_TO_OFF")
    data = json.loads(result)
    assert "error" in data
    assert "timer_duration_seconds" in data["error"]


async def test_set_port_mode_timer_zero_duration(mock_client):
    result = await set_port_mode("C58ZA", 1, "TIMER_TO_OFF", timer_duration_seconds=0)
    data = json.loads(result)
    assert "error" in data


async def test_set_port_mode_device_not_found(mock_client):
    result = await set_port_mode("INVALID", 1, "OFF")
    data = json.loads(result)
    assert "error" in data
    assert "INVALID" in data["error"]


async def test_set_port_mode_port_zero(mock_client):
    result = await set_port_mode("C58ZA", 0, "OFF")
    data = json.loads(result)
    assert "error" in data
    assert "port" in data["error"]

async def test_set_port_mode_device_error(mock_client):
    mock_client.set_port_mode.side_effect = ACInfinityDeviceError("smart mode")
    result = await set_port_mode("C58ZA", 1, "OFF")
    assert "error" in json.loads(result)


async def test_set_port_mode_auth_error(mock_client):
    mock_client.set_port_mode.side_effect = ACInfinityAuthError("expired")
    result = await set_port_mode("C58ZA", 1, "OFF")
    assert "error" in json.loads(result)


async def test_set_port_mode_generic_exception(mock_client):
    mock_client.set_port_mode.side_effect = RuntimeError("crash")
    result = await set_port_mode("C58ZA", 1, "OFF")
    assert "error" in json.loads(result)


async def test_set_port_mode_advance_conflict(mock_client):
    """ACInfinityAdvanceConflictError → structured conflict response."""
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("modeType=15")
    result = await set_port_mode("C58ZA", 1, "OFF")
    data = json.loads(result)
    assert data["conflict"] == "ADVANCE_AUTOMATION"
    assert "error" not in data


async def test_set_port_mode_device_error_non_advance(mock_client):
    """Base ACInfinityDeviceError → plain error response."""
    mock_client.set_port_mode.side_effect = ACInfinityDeviceError("loadType guard triggered")
    result = await set_port_mode("C58ZA", 1, "OFF")
    data = json.loads(result)
    assert "error" in data
    assert "loadType guard" in data["error"]


# ============ apply_grow_stage_template ============


def _stage_dry_response(payload: dict | None = None) -> dict:
    """Return a fake set_port_mode dry-run result with the given payload."""
    return {
        "payload": payload or {},
        "dry_run": True,
        "controller_type": "legacy",
        "sent": False,
    }


_STAGE_LIVE = {"payload": {}, "dry_run": False, "controller_type": "legacy", "sent": True}


async def test_apply_grow_stage_template_dry_run(mock_client):
    mock_client.set_port_mode.return_value = _stage_dry_response()
    result = await apply_grow_stage_template("C58ZA", 1, "veg", dry_run=True)
    data = json.loads(result)
    assert "error" not in data
    assert data["stage"] == "veg"
    assert data["dry_run"] is True
    assert data["sent"] is False
    assert data["controller_type"] == "legacy"
    assert data["vpd"]["target_kpa"] == 1.25
    assert data["temperature"]["min"] == 20.0
    assert data["temperature"]["max"] == 28.0
    assert "unit" in data["temperature"]
    assert data["humidity"]["min_rh"] == 50.0
    assert data["humidity"]["max_rh"] == 70.0
    assert "payload" in data
    # Single atomic write with atType=8 (VPD mode active)
    assert mock_client.set_port_mode.call_count == 1
    updates = mock_client.set_port_mode.call_args.args[2]
    assert updates["atType"] == 8
    assert updates["vpdSettingMode"] == 1
    assert updates["targetVpd"] == 13  # veg midpoint 1.25 kPa × 10, round-half-up
    assert updates["targetVpdSwitch"] == 1
    # Thresholds stored on the controller (inactive in VPD mode; available on switch to AUTO)
    assert updates["devLt"] == 20
    assert updates["devHt"] == 28
    assert updates["devLh"] == 50
    assert updates["devHh"] == 70
    assert updates["activeLt"] == 1
    assert updates["activeHt"] == 1
    assert updates["activeLh"] == 1
    assert updates["activeHh"] == 1


async def test_apply_grow_stage_template_live(mock_client):
    mock_client.set_port_mode.return_value = _STAGE_LIVE
    result = await apply_grow_stage_template("C58ZA", 1, "veg", dry_run=False)
    data = json.loads(result)
    assert "error" not in data
    assert data["dry_run"] is False
    assert data["sent"] is True
    assert "payload" not in data
    assert mock_client.set_port_mode.call_count == 1


@pytest.mark.parametrize(
    "stage,expected_vpd,expected_target_x10,temp_min,temp_max,humi_min,humi_max",
    [
        ("clones",       1.00, 10, 22.0, 26.0, 70.0, 80.0),
        ("seedling",     1.00, 10, 22.0, 26.0, 65.0, 75.0),
        ("veg",          1.25, 13, 20.0, 28.0, 50.0, 70.0),
        ("early_flower", 1.40, 14, 20.0, 26.0, 40.0, 60.0),
        ("mid_flower",   1.60, 16, 18.0, 25.0, 35.0, 55.0),
        ("late_flower",  1.50, 15, 18.0, 24.0, 30.0, 50.0),
    ],
)
async def test_apply_grow_stage_template_all_stages(
    mock_client, stage, expected_vpd, expected_target_x10,
    temp_min, temp_max, humi_min, humi_max,
):
    """Each stage produces a single write with the correct encoded targetVpd (P2-F001)."""
    mock_client.set_port_mode.return_value = _stage_dry_response()
    result = await apply_grow_stage_template("C58ZA", 1, stage, dry_run=True)
    data = json.loads(result)
    assert "error" not in data
    assert data["stage"] == stage
    assert data["vpd"]["target_kpa"] == expected_vpd
    assert data["temperature"]["min"] == temp_min
    assert data["temperature"]["max"] == temp_max
    assert "unit" in data["temperature"]
    assert data["humidity"]["min_rh"] == humi_min
    assert data["humidity"]["max_rh"] == humi_max
    updates = mock_client.set_port_mode.call_args.args[2]
    assert updates["atType"] == 8
    assert updates["targetVpd"] == expected_target_x10
    assert updates["devLt"] == int(temp_min + 0.5)
    assert updates["devHt"] == int(temp_max + 0.5)
    assert updates["devLh"] == int(humi_min + 0.5)
    assert updates["devHh"] == int(humi_max + 0.5)


async def test_apply_grow_stage_template_invalid_stage(mock_client):
    result = await apply_grow_stage_template("C58ZA", 1, "bloom")
    data = json.loads(result)
    assert "error" in data
    assert "bloom" in data["error"]
    assert "veg" in data["error"]
    mock_client.set_port_mode.assert_not_called()


@pytest.mark.parametrize("stage", ["VEG", "Veg", "VEG ", "vEg"])
async def test_apply_grow_stage_template_stage_is_case_sensitive(mock_client, stage):
    """Stage names are case-sensitive — "VEG" returns an error, not VEG defaults.

    Documenting and pinning this contract (P2-F019). If we ever decide to
    normalize input, this test changes intent and the contract is explicit.
    """
    result = await apply_grow_stage_template("C58ZA", 1, stage)
    data = json.loads(result)
    assert "error" in data
    mock_client.set_port_mode.assert_not_called()


async def test_apply_grow_stage_template_port_zero(mock_client):
    result = await apply_grow_stage_template("C58ZA", 0, "veg")
    assert "error" in json.loads(result)
    mock_client.set_port_mode.assert_not_called()


async def test_apply_grow_stage_template_device_not_found(mock_client):
    mock_client.get_devices.return_value = []
    result = await apply_grow_stage_template("NOTFOUND", 1, "veg")
    data = json.loads(result)
    assert "error" in data
    assert "NOTFOUND" in data["error"]

async def test_apply_grow_stage_template_ai_plus_dry_run(mock_client):
    mock_client.set_port_mode.return_value = _stage_dry_response()
    result = await apply_grow_stage_template("C58ZA", 1, "veg", dry_run=True)
    data = json.loads(result)
    assert "error" not in data
    assert data["sent"] is False


async def test_apply_grow_stage_template_api_error_on_write(mock_client):
    """API errors during write return a generic message (P3-C2-F003)."""
    mock_client.set_port_mode.side_effect = ACInfinityAPIError("Data saving failed")
    result = await apply_grow_stage_template("C58ZA", 1, "veg", dry_run=False)
    data = json.loads(result)
    assert data["error"] == "AC Infinity API error"
    assert data["detail"] == "see server logs"
    # Raw upstream text must not leak
    assert "Data saving failed" not in result


async def test_apply_grow_stage_template_auth_error(mock_client):
    """Auth errors from the write call return a friendly auth-error message."""
    mock_client.set_port_mode.side_effect = ACInfinityAuthError("token expired")
    result = await apply_grow_stage_template("C58ZA", 1, "veg", dry_run=False)
    data = json.loads(result)
    assert "Authentication failed" in data["error"]
    # Raw exception text must not leak (P1-C2-F003)
    assert "token expired" not in result
    assert data["detail"] == "see server logs"


async def test_apply_grow_stage_template_get_devices_exception(mock_client):
    """API errors during get_devices return a generic error, not str(e) (P1-C2-F003)."""
    mock_client.get_devices.side_effect = ACInfinityAPIError("upstream said: foo bar")
    result = await apply_grow_stage_template("C58ZA", 1, "veg", dry_run=True)
    data = json.loads(result)
    assert data["error"] == "AC Infinity API error"
    assert data["detail"] == "see server logs"
    # Raw upstream text must not leak
    assert "upstream said: foo bar" not in result
    assert mock_client.set_port_mode.call_count == 0


async def test_apply_grow_stage_template_get_devices_auth_error(mock_client):
    """Auth error during get_devices returns the auth-failure path (not generic)."""
    mock_client.get_devices.side_effect = ACInfinityAuthError("login rejected")
    result = await apply_grow_stage_template("C58ZA", 1, "veg", dry_run=True)
    data = json.loads(result)
    assert "Authentication failed" in data["error"]
    assert "login rejected" not in result
    assert mock_client.set_port_mode.call_count == 0


async def test_apply_grow_stage_template_get_devices_unexpected(mock_client):
    """Unexpected RuntimeError during get_devices returns generic message (not str(e))."""
    mock_client.get_devices.side_effect = RuntimeError(
        "trace contains appPasswordl=should-not-leak"
    )
    result = await apply_grow_stage_template("C58ZA", 1, "veg", dry_run=True)
    data = json.loads(result)
    assert data["error"] == "Unexpected error"
    assert data["detail"] == "see server logs"
    assert "should-not-leak" not in result
    assert "appPasswordl=" not in result


async def test_apply_grow_stage_template_advance_conflict(mock_client):
    """ACInfinityAdvanceConflictError from write → structured conflict, not opaque error."""
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("modeType=15")
    result = await apply_grow_stage_template("C58ZA", 1, "veg", dry_run=False)
    data = json.loads(result)
    assert data["conflict"] == "ADVANCE_AUTOMATION"
    assert "summary" in data
    assert "error" not in data


async def test_apply_grow_stage_template_device_error_non_advance(mock_client):
    """Base ACInfinityDeviceError from write → plain error response."""
    mock_client.set_port_mode.side_effect = ACInfinityDeviceError("loadType guard")
    result = await apply_grow_stage_template("C58ZA", 1, "veg", dry_run=False)
    data = json.loads(result)
    assert "error" in data
    assert "loadType guard" in data["error"]


async def test_apply_grow_stage_template_write_generic_exception(mock_client):
    """RuntimeError from write → generic error response (not str(e) leak)."""
    mock_client.set_port_mode.side_effect = RuntimeError("unexpected write crash")
    result = await apply_grow_stage_template("C58ZA", 1, "veg", dry_run=False)
    data = json.loads(result)
    assert data["error"] == "Unexpected error"
    assert "unexpected write crash" not in result


# ============ MCP Prompts ============


def test_vpd_troubleshooting_prompt():
    result = vpd_troubleshooting()
    assert isinstance(result, str)
    assert len(result) > 200
    assert "VPD" in result
    assert "HIGH" in result
    assert "LOW" in result
    assert "VPD mode" in result
    assert "grow stage template" in result
    # must not contain Python call syntax or dry_run
    assert "set_vpd_automation(" not in result
    assert "dry_run" not in result


def test_new_grower_setup_prompt():
    result = new_grower_setup()
    assert isinstance(result, str)
    assert len(result) > 200
    assert "AC Infinity devices" in result
    assert "grow stage template" in result
    assert "health" in result
    # must not contain Python call syntax or dry_run
    assert "discover_devices(" not in result
    assert "apply_grow_stage_template(" not in result
    assert "dry_run" not in result


def test_environment_alert_interpretation_prompt():
    result = environment_alert_interpretation()
    assert isinstance(result, str)
    assert len(result) > 200
    assert "check_vpd_drift" in result
    assert "get_environment_health" in result
    assert "OK" in result
    assert "HIGH" in result
    assert "LOW" in result
    assert "90" in result  # grade A threshold


# ============ parse_history_record — leaf_temp_c ============

def test_parse_history_record_includes_leaf_temp():
    """leafTemp=215 (tenths of a degree) → leaf_temp_c=21.5."""
    from ac_infinity_mcp.client import ACInfinityClient
    client = ACInfinityClient("test@example.com", "pw")
    record = {
        "createTime": 1714000000,
        "temperature": 2400,
        "fTemperature": 7520,
        "humidity": 6000,
        "vpdNums": 130,
        "portSpead": 0,
        "portStatus": 0,
        "devPortCount": 2,
        "leafTemp": 215,
    }
    parsed = client.parse_history_record(record)
    assert parsed["leaf_temp_c"] == 21.5


def test_parse_history_record_leaf_temp_zero():
    """leafTemp=0 → leaf_temp_c=0.0."""
    from ac_infinity_mcp.client import ACInfinityClient
    client = ACInfinityClient("test@example.com", "pw")
    record = {
        "createTime": 1714000000,
        "temperature": 2400,
        "fTemperature": 7520,
        "humidity": 6000,
        "vpdNums": 130,
        "portSpead": 0,
        "portStatus": 0,
        "devPortCount": 2,
        "leafTemp": 0,
    }
    parsed = client.parse_history_record(record)
    assert parsed["leaf_temp_c"] == 0.0


def test_parse_history_record_leaf_temp_absent():
    """Absent leafTemp key → leaf_temp_c=0.0 (not a KeyError)."""
    from ac_infinity_mcp.client import ACInfinityClient
    client = ACInfinityClient("test@example.com", "pw")
    record = {
        "createTime": 1714000000,
        "temperature": 2400,
        "fTemperature": 7520,
        "humidity": 6000,
        "vpdNums": 130,
        "portSpead": 0,
        "portStatus": 0,
        "devPortCount": 2,
    }
    parsed = client.parse_history_record(record)
    assert parsed["leaf_temp_c"] == 0.0


# ============ parse_device_data — external sensor type labels and precision ============

def _device_with_sensors(sensors: list[dict]) -> dict:
    """Build a minimal device dict with the given sensors list."""
    return {
        "devCode": "C58ZA",
        "devName": "Test Device",
        "devType": 11,
        "online": True,
        "deviceInfo": {
            "temperature": 2400,
            "temperatureF": 7520,
            "humidity": 6000,
            "vpdnums": 130,
            "ports": [],
            "sensors": sensors,
        },
    }


def test_external_sensor_type_label_co2():
    """sensorType=11 → sensor_type_label='CO2', unit='ppm'."""
    from ac_infinity_mcp.client import ACInfinityClient
    client = ACInfinityClient("test@example.com", "pw")
    device = _device_with_sensors([
        {"accessPort": 1, "sensorType": 11, "sensorData": 1100, "sensorPrecision": 1},
    ])
    parsed = client.parse_device_data(device)
    assert parsed["external_sensors"][0]["sensor_type_label"] == "CO2"
    assert parsed["external_sensors"][0]["unit"] == "ppm"
    assert parsed["external_sensors"][0]["sensor_type"] == 11


def test_external_sensor_type_label_soil_moisture():
    """sensorType=10 → sensor_type_label='Soil Moisture', unit='%'."""
    from ac_infinity_mcp.client import ACInfinityClient
    client = ACInfinityClient("test@example.com", "pw")
    device = _device_with_sensors([
        {"accessPort": 1, "sensorType": 10, "sensorData": 455, "sensorPrecision": 2},
    ])
    parsed = client.parse_device_data(device)
    assert parsed["external_sensors"][0]["sensor_type_label"] == "Soil Moisture"
    assert parsed["external_sensors"][0]["unit"] == "%"


def test_external_sensor_type_label_unknown():
    """Unrecognized sensorType with non-zero data → label includes type number, no unit."""
    from ac_infinity_mcp.client import ACInfinityClient
    client = ACInfinityClient("test@example.com", "pw")
    device = _device_with_sensors([
        {"accessPort": 1, "sensorType": 99, "sensorData": 100, "sensorPrecision": 1},
    ])
    parsed = client.parse_device_data(device)
    assert parsed["external_sensors"][0]["sensor_type_label"] == "Unrecognized (type 99)"
    assert parsed["external_sensors"][0]["unit"] == ""


def test_external_sensor_precision_1_passthrough():
    """sensorPrecision=1 → raw passthrough, returned as int (CO2 793 ppm stays 793)."""
    from ac_infinity_mcp.client import ACInfinityClient
    client = ACInfinityClient("test@example.com", "pw")
    device = _device_with_sensors([
        {"accessPort": 1, "sensorType": 11, "sensorData": 793, "sensorPrecision": 1},
    ])
    parsed = client.parse_device_data(device)
    value = parsed["external_sensors"][0]["value"]
    assert value == 793
    assert isinstance(value, int)  # precision <= 1 must not introduce a float


def test_external_sensor_precision_2_divides_by_ten():
    """sensorPrecision=2 → sensorData / 10**(2-1) = data / 10 (pH 65 → 6.5)."""
    from ac_infinity_mcp.client import ACInfinityClient
    client = ACInfinityClient("test@example.com", "pw")
    device = _device_with_sensors([
        {"accessPort": 1, "sensorType": 13, "sensorData": 65, "sensorPrecision": 2},
    ])
    parsed = client.parse_device_data(device)
    assert parsed["external_sensors"][0]["value"] == pytest.approx(6.5)


def test_external_sensor_precision_zero_passthrough():
    """sensorPrecision=0 → raw passthrough (NOT data/100). Also guards ZeroDivisionError."""
    from ac_infinity_mcp.client import ACInfinityClient
    client = ACInfinityClient("test@example.com", "pw")
    device = _device_with_sensors([
        {"accessPort": 1, "sensorType": 11, "sensorData": 500, "sensorPrecision": 0},
    ])
    parsed = client.parse_device_data(device)
    assert parsed["external_sensors"][0]["value"] == 500


def test_external_sensor_precision_absent_passthrough():
    """Missing sensorPrecision → defaults to precision 1 → raw passthrough (NOT data/100)."""
    from ac_infinity_mcp.client import ACInfinityClient
    client = ACInfinityClient("test@example.com", "pw")
    device = _device_with_sensors([
        {"accessPort": 1, "sensorType": 11, "sensorData": 200},
    ])
    parsed = client.parse_device_data(device)
    assert parsed["external_sensors"][0]["value"] == 200


def test_external_sensor_light_type_12_is_percentage():
    """Light (sensorType 12) is a 0-100% reading: precision 2, sensorData 1000 → 100.0.

    The old literal-divisor formula gave 1000 / 2 = 500 — an impossible light %.
    Ground-truthed against the HA ac_infinity integration (type 12 = power_factor / %).
    """
    from ac_infinity_mcp.client import ACInfinityClient
    client = ACInfinityClient("test@example.com", "pw")
    device = _device_with_sensors([
        {"accessPort": 1, "sensorType": 12, "sensorData": 1000, "sensorPrecision": 2},
    ])
    parsed = client.parse_device_data(device)
    sensor = parsed["external_sensors"][0]
    assert sensor["sensor_type_label"] == "Light"
    assert sensor["unit"] == "%"
    assert sensor["value"] == pytest.approx(100.0)
    assert 0 <= sensor["value"] <= 100


# ============ Advance Automation helper unit tests ============

def test_validate_automation_id_valid():
    assert _validate_automation_id("1342758") == 1342758
    assert _validate_automation_id("1") == 1


def test_validate_automation_id_invalid():
    assert _validate_automation_id("abc") is None
    assert _validate_automation_id("1.5") is None
    assert _validate_automation_id("-1") is None
    assert _validate_automation_id("") is None
    # Whitespace inputs
    assert _validate_automation_id(" ") is None
    assert _validate_automation_id(" 123") is None
    assert _validate_automation_id("123 ") is None
    # Leading zeros / zero itself
    assert _validate_automation_id("0") is None
    assert _validate_automation_id("01342758") is None


def test_group_automations_groups_by_name():
    """Two entries with same advName → one automation with both adv_ids."""
    grouped = _group_automations(MOCK_ADVANCE_AUTOMATIONS_LIST)
    names = [g["name"] for g in grouped]
    assert "Moderate Airflow" in names
    assert "Pollenation Airflow" in names
    # Should be 2 groups, not 3 entries
    assert len(grouped) == 2

    moderate = next(g for g in grouped if g["name"] == "Moderate Airflow")
    assert moderate["automation_id"] == 1342758  # first entry's advId
    assert set(moderate["adv_ids"]) == {1342758, 2179295}
    assert moderate["enabled"] is True
    assert moderate["run_state"] is True
    assert len(moderate["port_groups"]) == 2


def test_group_automations_empty():
    assert _group_automations([]) == []


# ============ list_advance_automations ============

async def test_list_advance_automations_groups_by_name(mock_client):
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await list_advance_automations("C58ZA")
    data = json.loads(result)
    assert "automations" in data
    # 3 raw entries → 2 grouped automations
    assert len(data["automations"]) == 2
    names = {a["name"] for a in data["automations"]}
    assert "Moderate Airflow" in names
    assert "Pollenation Airflow" in names


async def test_list_advance_automations_empty(mock_client):
    mock_client.get_advance_automations.return_value = []
    result = await list_advance_automations("C58ZA")
    data = json.loads(result)
    assert data["automations"] == []
    assert data["device_id"] == "C58ZA"


async def test_list_advance_automations_device_not_found(mock_client):
    mock_client.get_devices.return_value = []
    result = await list_advance_automations("NOTFOUND")
    data = json.loads(result)
    assert "error" in data


async def test_list_advance_automations_api_error(mock_client, caplog):
    import logging
    mock_client.get_advance_automations.side_effect = ACInfinityAPIError("fail")
    with caplog.at_level(logging.ERROR, logger="ac_infinity_mcp.server"):
        result = await list_advance_automations("C58ZA")
    data = json.loads(result)
    assert data["error"] == "API error"
    assert "detail" in data
    assert any(r.levelname == "ERROR" and "api" in r.message.lower() for r in caplog.records)


async def test_list_advance_automations_auth_error(mock_client, caplog):
    import logging
    mock_client.get_advance_automations.side_effect = ACInfinityAuthError("test")
    with caplog.at_level(logging.WARNING, logger="ac_infinity_mcp.server"):
        result = await list_advance_automations("C58ZA")
    data = json.loads(result)
    assert any(
        r.levelname == "WARNING" and "auth" in r.message.lower()
        for r in caplog.records
    )
    assert "Authentication failed — check AC_INFINITY_EMAIL" in data["error"]


# ============ get_advance_automation ============

async def test_get_advance_automation_found(mock_client):
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await get_advance_automation("C58ZA", "1342758")
    data = json.loads(result)
    assert data["automation_id"] == 1342758
    assert data["name"] == "Moderate Airflow"
    assert data["enabled"] is True
    assert "human_summary" in data
    assert isinstance(data["human_summary"], str)
    assert len(data["human_summary"]) > 0


async def test_get_advance_automation_single_group_human_summary(mock_client):
    """Single port-group → human_summary includes speed and schedule info."""
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_SINGLE
    result = await get_advance_automation("C58ZA", "999001")
    data = json.loads(result)
    assert "Pollenation Airflow" in data["human_summary"]
    assert "speed 3" in data["human_summary"]
    # beginTime=540 → "09:00", endTime=1020 → "17:00"
    assert "09:00" in data["human_summary"]
    assert "17:00" in data["human_summary"]


async def test_get_advance_automation_not_found(mock_client):
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await get_advance_automation("C58ZA", "9999999")
    data = json.loads(result)
    assert "error" in data
    assert "not found" in data["error"]


async def test_get_advance_automation_invalid_id(mock_client):
    result = await get_advance_automation("C58ZA", "not-an-id")
    data = json.loads(result)
    assert "error" in data
    assert "Invalid automation_id" in data["error"]


async def test_get_advance_automation_api_error(mock_client, caplog):
    import logging
    mock_client.get_advance_automations.side_effect = ACInfinityAPIError("fail")
    with caplog.at_level(logging.ERROR, logger="ac_infinity_mcp.server"):
        result = await get_advance_automation("C58ZA", "1342758")
    data = json.loads(result)
    assert data["error"] == "API error"
    assert any(r.levelname == "ERROR" and "api" in r.message.lower() for r in caplog.records)


async def test_get_advance_automation_auth_error(mock_client, caplog):
    import logging
    mock_client.get_advance_automations.side_effect = ACInfinityAuthError("test")
    with caplog.at_level(logging.WARNING, logger="ac_infinity_mcp.server"):
        result = await get_advance_automation("C58ZA", "1342758")
    data = json.loads(result)
    assert any(
        r.levelname == "WARNING" and "auth" in r.message.lower()
        for r in caplog.records
    )
    assert "Authentication failed — check AC_INFINITY_EMAIL" in data["error"]


# ============ enable_advance_automation ============

async def test_enable_advance_automation_dry_run(mock_client):
    """Automation is disabled → dry run returns sent=False."""
    import copy
    automations = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_LIST)
    # Set Pollenation Airflow (999001) as disabled
    for e in automations:
        if e["advId"] == 999001:
            e["isOn"] = 0
    mock_client.get_advance_automations.return_value = automations
    result = await enable_advance_automation("C58ZA", "999001", dry_run=True)
    data = json.loads(result)
    assert data["dry_run"] is True
    assert data["sent"] is False
    assert data["action"] == "enable"
    mock_client.enable_advance_automation.assert_not_called()


async def test_enable_advance_automation_already_enabled(mock_client):
    """Automation is already enabled → info response, no HTTP call."""
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    # Moderate Airflow (1342758) is enabled (isOn=1)
    result = await enable_advance_automation("C58ZA", "1342758", dry_run=False)
    data = json.loads(result)
    assert "info" in data
    assert "already enabled" in data["info"]
    mock_client.enable_advance_automation.assert_not_called()


async def test_enable_advance_automation_invalid_id(mock_client):
    result = await enable_advance_automation("C58ZA", "bad-id")
    data = json.loads(result)
    assert "error" in data


async def test_enable_advance_automation_api_error(mock_client, caplog):
    import logging
    mock_client.get_advance_automations.side_effect = ACInfinityAPIError("fail")
    with caplog.at_level(logging.ERROR, logger="ac_infinity_mcp.server"):
        result = await enable_advance_automation("C58ZA", "1342758", dry_run=False)
    data = json.loads(result)
    assert data["error"] == "API error"
    assert any(r.levelname == "ERROR" and "api" in r.message.lower() for r in caplog.records)


async def test_enable_advance_automation_auth_error(mock_client, caplog):
    import logging
    mock_client.get_advance_automations.side_effect = ACInfinityAuthError("test")
    with caplog.at_level(logging.WARNING, logger="ac_infinity_mcp.server"):
        result = await enable_advance_automation("C58ZA", "1342758", dry_run=False)
    data = json.loads(result)
    assert any(
        r.levelname == "WARNING" and "auth" in r.message.lower()
        for r in caplog.records
    )
    assert "Authentication failed — check AC_INFINITY_EMAIL" in data["error"]


# ============ disable_advance_automation ============

async def test_disable_advance_automation_dry_run(mock_client):
    """dry_run=True returns governed_ports, human_summary, and to_restore."""
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await disable_advance_automation("C58ZA", "1342758", dry_run=True)
    data = json.loads(result)
    assert data["dry_run"] is True
    assert data["sent"] is False
    assert data["action"] == "disable"
    assert "revert_behavior_confirmed" not in data
    assert "human_summary" in data
    assert "return to automated control right away" in data["human_summary"]
    assert "to_restore" in data
    assert data["to_restore"] == "Ask me to re-enable 'Moderate Airflow'."
    assert "adv_ids_to_toggle" not in data
    # "Moderate Airflow" governs ports 4, 5, 6 (grouptDevType 8 and 48).
    # MOCK_DEVICE_LEGACY only has ports 1–2, so all three fall back to "Port N".
    assert isinstance(data["governed_ports"], list)
    assert len(data["governed_ports"]) == 3
    for entry in data["governed_ports"]:
        assert "port" in entry and isinstance(entry["port"], int)
        assert "port_name" in entry and isinstance(entry["port_name"], str)
        assert entry["port_name"] == f"Port {entry['port']}"
    assert [e["port"] for e in data["governed_ports"]] == [4, 5, 6]
    mock_client.disable_advance_automation.assert_not_called()


async def test_disable_advance_automation_dry_run_governed_ports(mock_client):
    """governed_ports contains named port entries when device has matching port names."""
    import copy
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["deviceInfo"]["ports"].extend([
        {"port": 4, "portName": "Clip Fan"},
        {"port": 5, "portName": "Left Outlet"},
        {"port": 6, "portName": "Right Outlet"},
    ])
    mock_client.get_devices.return_value = [device]
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await disable_advance_automation("C58ZA", "1342758", dry_run=True)
    data = json.loads(result)
    assert data["governed_ports"] == [
        {"port": 4, "port_name": "Clip Fan (Port 4)"},
        {"port": 5, "port_name": "Left Outlet (Port 5)"},
        {"port": 6, "port_name": "Right Outlet (Port 6)"},
    ]
    mock_client.disable_advance_automation.assert_not_called()


async def test_disable_advance_automation_live_governed_ports(mock_client):
    """Live path also includes governed_ports with named entries."""
    import copy
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["deviceInfo"]["ports"].extend([
        {"port": 4, "portName": "Clip Fan"},
        {"port": 5, "portName": "Left Outlet"},
        {"port": 6, "portName": "Right Outlet"},
    ])
    mock_client.get_devices.return_value = [device]
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await disable_advance_automation("C58ZA", "1342758", dry_run=False)
    data = json.loads(result)
    assert data["sent"] is True
    assert data["governed_ports"] == [
        {"port": 4, "port_name": "Clip Fan (Port 4)"},
        {"port": 5, "port_name": "Left Outlet (Port 5)"},
        {"port": 6, "port_name": "Right Outlet (Port 6)"},
    ]


async def test_disable_advance_automation_dry_run_zero_bitmask(mock_client):
    """grouptDevType=0 for all port groups → governed_ports is empty list."""
    import copy
    automations = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_LIST)
    for e in automations:
        e["grouptDevType"] = 0
    mock_client.get_advance_automations.return_value = automations
    result = await disable_advance_automation("C58ZA", "1342758", dry_run=True)
    data = json.loads(result)
    assert data["governed_ports"] == []
    mock_client.disable_advance_automation.assert_not_called()


async def test_disable_advance_automation_dry_run_port_name_sanitized(mock_client):
    """portName with control characters is sanitized before appearing in governed_ports."""
    import copy
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["deviceInfo"]["ports"].append({"port": 4, "portName": "Clip\x00Fan"})
    mock_client.get_devices.return_value = [device]
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await disable_advance_automation("C58ZA", "1342758", dry_run=True)
    data = json.loads(result)
    port4 = next(e for e in data["governed_ports"] if e["port"] == 4)
    assert "\x00" not in port4["port_name"]
    assert "(Port 4)" in port4["port_name"]


async def test_disable_advance_automation_already_disabled(mock_client):
    """Automation is already disabled → info response, no governed_ports."""
    import copy
    automations = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_LIST)
    for e in automations:
        e["isOn"] = 0  # disable all
    mock_client.get_advance_automations.return_value = automations
    result = await disable_advance_automation("C58ZA", "1342758", dry_run=False)
    data = json.loads(result)
    assert "info" in data
    assert "already disabled" in data["info"]
    assert "governed_ports" not in data
    mock_client.disable_advance_automation.assert_not_called()


async def test_disable_advance_automation_invalid_id(mock_client):
    result = await disable_advance_automation("C58ZA", "xyz")
    data = json.loads(result)
    assert "error" in data


async def test_disable_advance_automation_api_error(mock_client, caplog):
    import logging
    mock_client.get_advance_automations.side_effect = ACInfinityAPIError("fail")
    with caplog.at_level(logging.ERROR, logger="ac_infinity_mcp.server"):
        result = await disable_advance_automation("C58ZA", "1342758", dry_run=False)
    data = json.loads(result)
    assert data["error"] == "API error"
    assert any(r.levelname == "ERROR" and "api" in r.message.lower() for r in caplog.records)


async def test_disable_advance_automation_auth_error(mock_client, caplog):
    import logging
    mock_client.get_advance_automations.side_effect = ACInfinityAuthError("test")
    with caplog.at_level(logging.WARNING, logger="ac_infinity_mcp.server"):
        result = await disable_advance_automation("C58ZA", "1342758", dry_run=False)
    data = json.loads(result)
    assert any(
        r.levelname == "WARNING" and "auth" in r.message.lower()
        for r in caplog.records
    )
    assert "Authentication failed — check AC_INFINITY_EMAIL" in data["error"]


@pytest.mark.asyncio
async def test_disable_advance_automation_governed_ports_default_name_no_redundancy(mock_client):
    """disable_advance_automation governed_ports uses plain 'Port N' for default-named ports."""
    import copy
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["deviceInfo"]["ports"].append(
        {"port": 4, "portName": "Port 4", "speak": 0, "portsLoad": 0,
         "loadState": 0, "curMode": 15, "remainTime": None}
    )
    mock_client.get_devices.return_value = [device]
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await disable_advance_automation("C58ZA", "1342758", dry_run=True)
    data = json.loads(result)
    assert data["dry_run"] is True
    port4_entry = next((e for e in data["governed_ports"] if e["port"] == 4), None)
    assert port4_entry is not None
    assert port4_entry["port_name"] == "Port 4"


# ============ create_advance_automation ============

async def test_create_advance_automation_dry_run(mock_client):
    result = await create_advance_automation(
        "C58ZA", "Night Cycle", on_speed=3, port=1, dry_run=True
    )
    data = json.loads(result)
    assert data["dry_run"] is True
    assert data["sent"] is False
    assert data["action"] == "create"
    assert data["name"] == "Night Cycle"
    assert data["port"] == 1
    assert data["port_name"] == "Intake Fan"
    assert "note" in data
    # #287: no window given → continuous 24/7 by default (not a 00:00–23:59 schedule).
    assert data["begin_time"] == "continuous"
    assert data["end_time"] == "continuous"
    assert data["schedule_summary"] == "Runs continuously (24/7)"
    mock_client.create_advance_automation.assert_not_called()


async def test_create_advance_automation_explicit_window_still_scheduled(mock_client):
    """#287: an explicit window is honored as a normal schedule, not overridden to continuous."""
    result = await create_advance_automation(
        "C58ZA", "Night Cycle", on_speed=3, port=1, begin_time=360, end_time=720, dry_run=True
    )
    data = json.loads(result)
    assert data["begin_time"] == "06:00"
    assert data["end_time"] == "12:00"
    assert "continuous" not in data["schedule_summary"].lower()


# ============ #287 continuous-default + #288 target-capability gating ============


def _device_with_modetye(modetye_by_port: dict) -> dict:
    """Deep copy of the legacy mock device with per-port modeTye set (#288 capability)."""
    d = copy.deepcopy(MOCK_DEVICE_LEGACY)
    for p in d["deviceInfo"]["ports"]:
        if p["port"] in modetye_by_port:
            p["modeTye"] = modetye_by_port[p["port"]]
    return d


def test_ports_without_target_support_helper():
    from ac_infinity_mcp.server import _ports_without_target_support
    dev = _device_with_modetye({1: 0, 2: 15})
    assert _ports_without_target_support(dev, [1]) == [1]      # modeTye 0 → no target
    assert _ports_without_target_support(dev, [2]) == []       # modeTye 15 → target ok
    assert _ports_without_target_support(dev, [1, 2]) == [1]   # only the incapable one


def test_ports_without_target_support_missing_field_allows():
    """A port that doesn't report modeTye is treated as capable (never false-blocked)."""
    from ac_infinity_mcp.server import _ports_without_target_support
    dev = copy.deepcopy(MOCK_DEVICE_LEGACY)  # no modeTye on any port
    assert _ports_without_target_support(dev, [1, 2]) == []


# ---- #287: no window → continuous 24/7 ----


async def test_create_no_window_defaults_continuous_live(mock_client):
    mock_client.create_advance_automation.return_value = {"advId": 9001}
    await create_advance_automation("C58ZA", "AllDay", on_speed=4, port=1, dry_run=False)
    _, payload = mock_client.create_advance_automation.call_args[0]
    assert payload["switchTime"] == 255      # continuous toggle, not a 00:00–23:59 schedule


async def test_create_explicit_window_is_scheduled_live(mock_client):
    mock_client.create_advance_automation.return_value = {"advId": 9002}
    await create_advance_automation(
        "C58ZA", "Sched", on_speed=4, port=1, begin_time=360, end_time=720, dry_run=False
    )
    _, payload = mock_client.create_advance_automation.call_args[0]
    assert payload["switchTime"] == 127
    assert payload["beginTime"] == 360


# ============ #300 — portType device-identity resolution ============
# Isolated inline programs (never mutate the shared MOCK_ADVANCE_AUTOMATIONS_LIST) so the
# portType assertions can't be coupled to other tests' fixtures.


async def test_create_resolves_port_type_from_existing_rule(mock_client):
    """create resolves portType from an existing getGroups rule covering the target port."""
    mock_client.get_advance_automations.return_value = [
        {"advName": "Other", "grouptDevType": 1, "portType": 1, "advId": 7001},  # Port 1, outlet
    ]
    mock_client.create_advance_automation.return_value = {"advId": 9101}
    await create_advance_automation("C58ZA", "Heat", on_speed=10, port=1, dry_run=False)
    _, payload = mock_client.create_advance_automation.call_args[0]
    assert payload["portType"] == 1


async def test_create_defaults_port_type_zero_when_no_covering_rule(mock_client):
    """No existing rule covers the target port → portType defaults to 0."""
    mock_client.get_advance_automations.return_value = [
        {"advName": "Other", "grouptDevType": 2, "portType": 1, "advId": 7002},  # Port 2 only
    ]
    mock_client.create_advance_automation.return_value = {"advId": 9102}
    await create_advance_automation("C58ZA", "Fan", on_speed=5, port=1, dry_run=False)
    _, payload = mock_client.create_advance_automation.call_args[0]
    assert payload["portType"] == 0


async def test_create_port_type_fetch_failure_falls_back_and_notes(mock_client, caplog):
    """A getGroups read failure must NOT block create: portType falls back to 0, the write
    still fires (sent=True), a warning is logged, and the response carries a grower note."""
    import logging
    mock_client.get_advance_automations.side_effect = ACInfinityAPIError("503 fail")
    mock_client.create_advance_automation.return_value = {"advId": 9103}
    with caplog.at_level(logging.WARNING, logger="ac_infinity_mcp"):
        result = await create_advance_automation(
            "C58ZA", "Heat", on_speed=10, port=1, dry_run=False
        )
    data = json.loads(result)
    assert data["sent"] is True
    assert "note" in data
    _, payload = mock_client.create_advance_automation.call_args[0]
    assert payload["portType"] == 0
    assert any("portType" in r.message for r in caplog.records)


async def test_create_dry_run_does_not_read_getgroups(mock_client):
    """create's dry-run does not surface portType, so it performs no getGroups read — a
    getGroups failure can never affect the preview."""
    await create_advance_automation("C58ZA", "Heat", on_speed=10, port=1, dry_run=True)
    mock_client.get_advance_automations.assert_not_called()


def _isolated_program(grp: int, port_type: int) -> list[dict]:
    return [{
        "advName": "Prog", "grouptDevType": grp, "portType": port_type, "advId": 8001,
        "groupNums": 3, "sortType": 3, "subNumber": 0, "subNumberSort": 0, "runState": 1,
    }]


async def test_add_rule_resolves_port_type_from_existing_rule(mock_client):
    """add resolves portType from the getGroups data it already fetched (no extra read)."""
    mock_client.get_advance_automations.return_value = _isolated_program(2, 1)  # Port 2, outlet
    mock_client.create_advance_automation.return_value = {"advId": 9201}
    await add_automation_rule("C58ZA", "Prog", [2], "on", max_level=5, dry_run=False)
    _, payload = mock_client.create_advance_automation.call_args[0]
    assert payload["portType"] == 1


async def test_add_rule_port_type_zero_when_port_uncovered(mock_client):
    """add on a port not covered by any existing rule → portType 0."""
    mock_client.get_advance_automations.return_value = _isolated_program(2, 1)  # covers Port 2
    mock_client.create_advance_automation.return_value = {"advId": 9202}
    await add_automation_rule("C58ZA", "Prog", [1], "on", max_level=5, dry_run=False)  # Port 1
    _, payload = mock_client.create_advance_automation.call_args[0]
    assert payload["portType"] == 0


async def test_update_mode_change_preserves_port_type(mock_client):
    """#300 regression: a mode-change rebuild preserves the live rule's portType (it's in no
    signature-key set, and body is a deepcopy of the live entry). Fails if portType is ever
    added to a signature-key set."""
    rule = {
        **copy.deepcopy(MOCK_RULE_TEMPERATURE_TRIGGER), "advName": "Prog",
        "grouptDevType": 2, "portType": 1, "advId": 8100,
        "groupNums": 3, "sortType": 3, "subNumber": 0, "subNumberSort": 0, "runState": 1,
    }
    mock_client.get_advance_automations.return_value = [rule]
    mock_client.update_advance_automation.return_value = {"code": 200}
    await update_automation_rule("C58ZA", "Prog", [2], mode="on", max_level=4, dry_run=False)
    sent = mock_client.update_advance_automation.call_args.args[1]
    assert sent["portType"] == 1


async def test_add_rule_no_schedule_defaults_continuous(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule("C58ZA", "Seedling", [1], "on", max_level=5, dry_run=True)
    assert "runs continuously" in json.loads(result)["rule"]["control"].lower()


async def test_add_rule_explicit_window_not_continuous(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [1], "on", max_level=5, begin_time=360, end_time=720, dry_run=True
    )
    assert "continuous" not in json.loads(result)["rule"]["control"].lower()


# ---- #288: target gated by port modeTye capability, across all surfaces ----


async def test_create_target_on_incapable_port_rejected(mock_client):
    mock_client.get_devices.return_value = [_device_with_modetye({1: 0})]
    result = await create_advance_automation(
        "C58ZA", "Hold", on_speed=5, port=1, mode="vpd", control_style="target",
        vpd_target=1.0, dry_run=True,
    )
    assert "doesn't support target" in json.loads(result)["error"]
    mock_client.create_advance_automation.assert_not_called()


async def test_create_target_on_capable_port_allowed(mock_client):
    mock_client.get_devices.return_value = [_device_with_modetye({1: 15})]
    result = await create_advance_automation(
        "C58ZA", "Hold", on_speed=5, port=1, mode="vpd", control_style="target",
        vpd_target=1.0, dry_run=True,
    )
    assert "error" not in json.loads(result)


# ---- #291: temperature target is unsupported (renders as thresholds) ----


async def test_create_temp_target_rejected_unsupported(mock_client):
    """A temperature setpoint is rejected even on a target-capable port (#291)."""
    mock_client.get_devices.return_value = [_device_with_modetye({1: 15})]
    result = await create_advance_automation(
        "C58ZA", "Hold", on_speed=5, port=1, mode="auto", control_style="target",
        temp_target_f=75, dry_run=True,
    )
    assert "temperature setpoint isn't supported" in json.loads(result)["error"]
    mock_client.create_advance_automation.assert_not_called()


async def test_add_temp_target_rejected_unsupported(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [1], "auto", control_style="target", temp_target_f=75, dry_run=True,
    )
    assert "temperature setpoint isn't supported" in json.loads(result)["error"]
    mock_client.create_advance_automation.assert_not_called()


async def test_humidity_target_still_allowed(mock_client):
    """Humidity target remains supported (only temperature target is rejected)."""
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [1], "auto", control_style="target", humidity_target=55,
        dry_run=True,
    )
    assert "error" not in json.loads(result)


async def test_add_target_on_incapable_port_rejected(mock_client):
    mock_client.get_devices.return_value = [_device_with_modetye({1: 0})]
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [1], "vpd", control_style="target", vpd_target=1.0, dry_run=True,
    )
    assert "doesn't support target" in json.loads(result)["error"]
    mock_client.create_advance_automation.assert_not_called()


async def test_update_to_target_on_incapable_port_rejected(mock_client):
    mock_client.get_devices.return_value = [_device_with_modetye({1: 0})]
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=540, end_time=180,
        mode="vpd", control_style="target", vpd_target=1.0, dry_run=True,
    )
    assert "doesn't support target" in json.loads(result)["error"]
    mock_client.update_advance_automation.assert_not_called()


async def test_set_vpd_automation_on_incapable_port_rejected(mock_client):
    mock_client.get_devices.return_value = [_device_with_modetye({1: 0})]
    result = await set_vpd_automation("C58ZA", 1, 1.0, dry_run=True)
    assert "doesn't support target" in json.loads(result)["error"]
    mock_client.set_port_mode.assert_not_called()


async def test_apply_grow_stage_template_on_incapable_port_rejected(mock_client):
    """The grow-stage template writes a VPD target → gated on a legacy port too."""
    mock_client.get_devices.return_value = [_device_with_modetye({1: 0})]
    result = await apply_grow_stage_template("C58ZA", 1, "veg", dry_run=True)
    assert "doesn't support target" in json.loads(result)["error"]
    mock_client.set_port_mode.assert_not_called()


async def test_add_humidity_target_on_incapable_port_rejected(mock_client):
    """Gate is sensor-agnostic: an auto humidity TARGET is blocked on a legacy port too."""
    mock_client.get_devices.return_value = [_device_with_modetye({1: 0})]
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [1], "auto", control_style="target", humidity_target=65,
        dry_run=True,
    )
    assert "doesn't support target" in json.loads(result)["error"]
    mock_client.create_advance_automation.assert_not_called()


async def test_update_same_mode_target_on_incapable_port_rejected(mock_client):
    """The gate fires via effective-style INFERENCE too: a same-mode edit (no mode/style) on a
    rule already in target mode, on an incapable port, is rejected — not just explicit style."""
    mock_client.get_devices.return_value = [_device_with_modetye({1: 0})]
    mock_client.get_advance_automations.return_value = _seedling_program()  # rule[0] = VPD target
    result = await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=540, end_time=180, vpd_target=1.1, dry_run=True,
    )
    assert "doesn't support target" in json.loads(result)["error"]
    mock_client.update_advance_automation.assert_not_called()


async def test_update_to_target_on_capable_port_not_blocked(mock_client):
    """No false-block: a target edit on a capable port (modeTye=15) passes the gate."""
    mock_client.get_devices.return_value = [_device_with_modetye({1: 15})]
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=540, end_time=180,
        mode="vpd", control_style="target", vpd_target=1.0, dry_run=True,
    )
    assert "doesn't support target" not in result


def test_ports_without_target_support_malformed_device_fails_open():
    from ac_infinity_mcp.server import _ports_without_target_support
    # deviceInfo.ports not a list of dicts → exception path → fail open (no block).
    assert _ports_without_target_support({"deviceInfo": {"ports": "oops"}}, [1]) == []


async def test_create_partial_window_is_scheduled_not_continuous(mock_client):
    """#287: only one of begin/end given → treated as an explicit schedule, not continuous."""
    result = await create_advance_automation(
        "C58ZA", "Partial", on_speed=3, port=1, begin_time=300, dry_run=True
    )
    data = json.loads(result)
    assert data["begin_time"] == "05:00"
    assert data["end_time"] == "23:59"
    assert "continuous" not in data["schedule_summary"].lower()


async def test_create_advance_automation_dry_run_port_no_name(mock_client):
    """Port with no portName falls back to 'Port N' in dry_run response, not '(unnamed)'."""
    import copy
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["deviceInfo"]["ports"][0].pop("portName", None)
    mock_client.get_devices.return_value = [device]
    result = await create_advance_automation(
        "C58ZA", "Night Cycle", on_speed=3, port=1, dry_run=True
    )
    data = json.loads(result)
    assert data["port_name"] == "Port 1"


async def test_create_advance_automation_invalid_speed(mock_client):
    result = await create_advance_automation("C58ZA", "Test", on_speed=11, port=1, dry_run=True)
    data = json.loads(result)
    assert "error" in data
    assert "on_speed" in data["error"]


async def test_create_advance_automation_speed_zero(mock_client):
    result = await create_advance_automation("C58ZA", "Test", on_speed=0, port=1, dry_run=True)
    data = json.loads(result)
    assert "error" in data


async def test_create_advance_automation_empty_name(mock_client):
    result = await create_advance_automation("C58ZA", "", on_speed=5, port=1, dry_run=True)
    data = json.loads(result)
    assert "error" in data


async def test_create_advance_automation_control_char_name_stripped(mock_client):
    """Control chars in name are stripped before validation."""
    result = await create_advance_automation(
        "C58ZA", "Valid\x00Name", on_speed=5, port=1, dry_run=True
    )
    data = json.loads(result)
    assert data["name"] == "ValidName"


# ============ delete_advance_automation ============

async def test_delete_advance_automation_dry_run(mock_client):
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await delete_advance_automation("C58ZA", "999001", dry_run=True)
    data = json.loads(result)
    assert data["dry_run"] is True
    assert data["sent"] is False
    assert data["action"] == "delete"
    mock_client.delete_advance_automation.assert_not_called()


async def test_delete_advance_automation_not_found(mock_client):
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await delete_advance_automation("C58ZA", "7777777", dry_run=True)
    data = json.loads(result)
    assert "error" in data
    assert "not found" in data["error"]


async def test_delete_advance_automation_invalid_id(mock_client):
    result = await delete_advance_automation("C58ZA", "bad-id")
    data = json.loads(result)
    assert "error" in data


async def test_delete_advance_automation_api_error(mock_client, caplog):
    import logging
    mock_client.get_advance_automations.side_effect = ACInfinityAPIError("fail")
    with caplog.at_level(logging.ERROR, logger="ac_infinity_mcp.server"):
        result = await delete_advance_automation("C58ZA", "1342758", dry_run=False)
    data = json.loads(result)
    assert data["error"] == "API error"
    assert any(r.levelname == "ERROR" and "api" in r.message.lower() for r in caplog.records)


async def test_delete_advance_automation_auth_error(mock_client, caplog):
    import logging
    mock_client.get_advance_automations.side_effect = ACInfinityAuthError("test")
    with caplog.at_level(logging.WARNING, logger="ac_infinity_mcp.server"):
        result = await delete_advance_automation("C58ZA", "1342758", dry_run=False)
    data = json.loads(result)
    assert any(
        r.levelname == "WARNING" and "auth" in r.message.lower()
        for r in caplog.records
    )
    assert "Authentication failed — check AC_INFINITY_EMAIL" in data["error"]


# ============ break_out_of_automation ============

async def test_break_out_not_advance_port(mock_client):
    """Port not under automation (modeType != 15) → idempotent info response."""
    mock_client.get_mode_settings.return_value = {"modeType": 3, "onSpead": 5}
    result = await break_out_of_automation("C58ZA", port=1, dry_run=True)
    data = json.loads(result)
    assert "info" in data
    assert "not currently under automation" in data["info"]
    mock_client.get_advance_automations.assert_not_called()


@pytest.mark.asyncio
async def test_break_out_not_advance_port_default_name_no_redundancy(mock_client):
    """break_out_of_automation info message uses plain 'Port N' for default-named ports."""
    import copy
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    # Append port 7 with default name — MOCK_DEVICE_LEGACY only has ports 1 and 2
    device["deviceInfo"]["ports"].append(
        {"port": 7, "portName": "Port 7", "speak": 0, "portsLoad": 0,
         "loadState": 0, "curMode": 1, "remainTime": None}
    )
    mock_client.get_devices.return_value = [device]
    mock_client.get_mode_settings.return_value = {"modeType": 3, "onSpead": 0}
    result = await break_out_of_automation("C58ZA", port=7, dry_run=True)
    data = json.loads(result)
    assert "info" in data
    # Positive: correct message format
    assert "Port 7 is not currently under automation control" in data["info"]
    # Negative: no double-Port stuttering
    assert "Port Port" not in data["info"]
    assert "(Port" not in data["info"]


@pytest.mark.asyncio
async def test_break_out_co_port_sequence_default_name_no_redundancy(mock_client):
    """break_out_of_automation sequence steps use 'Port N' for default-named co-ports."""
    import copy
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    # Override port 2's portName to default "Port 2" (MOCK_DEVICE_LEGACY has "Exhaust Fan")
    for p in device["deviceInfo"]["ports"]:
        if p["port"] == 2:
            p["portName"] = "Port 2"
    mock_client.get_devices.return_value = [device]
    mock_client.get_mode_settings.return_value = {"modeType": _ADVANCE_MODE_TYPE, "onSpead": 5}
    _auto = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_LIST[0])
    _auto["grouptDevType"] = 3  # ports 1+2 (0b00000011)
    mock_client.get_advance_automations.return_value = [_auto]
    result = await break_out_of_automation("C58ZA", port=1, dry_run=True)
    data = json.loads(result)
    assert data["dry_run"] is True
    # Port 2 should appear as a co-port lock step
    lock_steps = [s for s in data["sequence"] if "lock" in s["action"]]
    assert len(lock_steps) >= 1
    port2_step = next((s for s in lock_steps if "Port 2" in s["action"]), None)
    assert port2_step is not None, "Expected a lock step for Port 2"
    # Default name: no '(Port 2)' suffix in the action string
    assert "(Port 2)" not in port2_step["action"]


async def test_break_out_dry_run(mock_client):
    """Port under automation → dry run returns plan, zero HTTP writes."""
    import copy
    mock_client.get_mode_settings.return_value = {"modeType": _ADVANCE_MODE_TYPE, "onSpead": 2}
    _auto = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_LIST[0])
    _auto["grouptDevType"] = 1  # port 1 only (0b00000001)
    mock_client.get_advance_automations.return_value = [_auto]
    result = await break_out_of_automation("C58ZA", port=1, dry_run=True)
    data = json.loads(result)
    assert "release" in data["action"]
    assert data["dry_run"] is True
    assert "sequence" in data
    assert "automation_name" in data
    assert "estimated_duration_seconds" in data
    assert isinstance(data["estimated_duration_seconds"], (int, float))
    assert data["estimated_duration_seconds"] > 0
    assert "revert_behavior_confirmed" not in data
    assert "human_summary" in data
    assert "return to automated control right away" in data["human_summary"]
    assert "co_ports_to_lock" in data
    assert isinstance(data["co_ports_to_lock"], list)
    # No writes on dry run
    mock_client.disable_advance_automation.assert_not_called()
    mock_client.set_port_mode.assert_not_called()


async def test_break_out_confirm_name_required(mock_client):
    """dry_run=False without confirm_automation_name → error."""
    import copy
    mock_client.get_mode_settings.return_value = {"modeType": _ADVANCE_MODE_TYPE, "onSpead": 2}
    _auto = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_LIST[0])
    _auto["grouptDevType"] = 1  # port 1 only
    mock_client.get_advance_automations.return_value = [_auto]
    result = await break_out_of_automation(
        "C58ZA", port=1, dry_run=False, confirm_automation_name=None
    )
    data = json.loads(result)
    assert "error" in data
    assert "confirm" in data["error"].lower()


async def test_break_out_confirm_name_mismatch(mock_client):
    """Wrong confirm_automation_name → error."""
    import copy
    mock_client.get_mode_settings.return_value = {"modeType": _ADVANCE_MODE_TYPE, "onSpead": 2}
    _auto = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_LIST[0])
    _auto["grouptDevType"] = 1  # port 1 only
    mock_client.get_advance_automations.return_value = [_auto]
    result = await break_out_of_automation(
        "C58ZA", port=1, dry_run=False,
        confirm_automation_name="Wrong Name"
    )
    data = json.loads(result)
    assert "error" in data
    assert "match" in data["error"]


async def test_break_out_confirm_name_case_insensitive(mock_client):
    """Case-insensitive match for confirm_automation_name."""
    import copy
    mock_client.get_mode_settings.return_value = {"modeType": _ADVANCE_MODE_TYPE, "onSpead": 2}
    _auto = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_LIST[0])
    _auto["grouptDevType"] = 1  # port 1 only — no co-ports
    mock_client.get_advance_automations.return_value = [_auto]
    mock_client.disable_advance_automation.return_value = {"code": 200}
    mock_client.set_port_mode.return_value = {
        "dry_run": False, "sent": True, "controller_type": "legacy", "payload": {}
    }
    result = await break_out_of_automation(
        "C58ZA", port=1, dry_run=False,
        confirm_automation_name="MODERATE AIRFLOW"  # uppercase should match
    )
    data = json.loads(result)
    assert "release" in (data.get("action") or "")
    assert data.get("sent") is True
    # Disable called exactly once (single toggle — not once per adv_id)
    assert mock_client.disable_advance_automation.call_count == 1


async def test_break_out_live_human_summary(mock_client):
    """Live break_out response includes human_summary saying port is released."""
    import copy
    mock_client.get_mode_settings.return_value = {"modeType": _ADVANCE_MODE_TYPE, "onSpead": 2}
    _auto = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_LIST[0])
    _auto["grouptDevType"] = 1  # port 1 only — no co-ports
    mock_client.get_advance_automations.return_value = [_auto]
    mock_client.disable_advance_automation.return_value = {"code": 200}
    mock_client.set_port_mode.return_value = {
        "dry_run": False, "sent": True, "controller_type": "legacy", "payload": {}
    }
    result = await break_out_of_automation(
        "C58ZA", port=1, dry_run=False,
        confirm_automation_name="Moderate Airflow",
    )
    data = json.loads(result)
    assert data.get("sent") is True
    assert "human_summary" in data
    assert "manually" in data["human_summary"]


async def test_break_out_of_automation_api_error(mock_client, caplog):
    import logging
    # get_mode_settings must return ADVANCE mode (modeType=15) so the function
    # proceeds to call get_advance_automations before hitting the outer error handler.
    mock_client.get_mode_settings.return_value = {"modeType": 15}
    mock_client.get_advance_automations.side_effect = ACInfinityAPIError("fail")
    with caplog.at_level(logging.ERROR, logger="ac_infinity_mcp.server"):
        result = await break_out_of_automation("C58ZA", port=1, dry_run=False)
    data = json.loads(result)
    assert data["error"] == "API error"
    assert any(r.levelname == "ERROR" and "api" in r.message.lower() for r in caplog.records)


async def test_break_out_of_automation_auth_error(mock_client, caplog):
    import logging
    # get_mode_settings must return ADVANCE mode (modeType=15) so the function
    # proceeds to call get_advance_automations before hitting the outer error handler.
    mock_client.get_mode_settings.return_value = {"modeType": 15}
    mock_client.get_advance_automations.side_effect = ACInfinityAuthError("test")
    with caplog.at_level(logging.WARNING, logger="ac_infinity_mcp.server"):
        result = await break_out_of_automation("C58ZA", port=1, dry_run=False)
    data = json.loads(result)
    assert any(
        r.levelname == "WARNING" and "auth" in r.message.lower()
        for r in caplog.records
    )
    assert "Authentication failed — check AC_INFINITY_EMAIL" in data["error"]


# ============ break_out_of_automation gather tests ============

async def test_break_out_bitmask_replaces_gather(mock_client):
    """Bitmask decode replaces gather: get_mode_settings called once (idempotency only)."""
    import copy
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["deviceInfo"]["ports"].append(
        {"port": 3, "portName": "CO2 Fan", "speak": 3, "portsLoad": 1,
         "loadState": 1, "curMode": 3, "remainTime": 0}
    )
    device["deviceInfo"]["ports"].append(
        {"port": 4, "portName": "Humidifier", "speak": 5, "portsLoad": 1,
         "loadState": 1, "curMode": 3, "remainTime": 0}
    )
    mock_client.get_devices.return_value = [device]
    mock_client.get_mode_settings.return_value = {"modeType": _ADVANCE_MODE_TYPE, "onSpead": 5}
    # Automation covers ports 1-4 (grouptDevType=15 = 0b00001111)
    _auto = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_LIST[0])
    _auto["grouptDevType"] = 15
    mock_client.get_advance_automations.return_value = [_auto]
    result = await break_out_of_automation("C58ZA", port=1, dry_run=True)
    data = json.loads(result)
    assert data["dry_run"] is True
    # Only one get_mode_settings call: the Step 0 idempotency check for port 1
    assert mock_client.get_mode_settings.call_count == 1
    assert mock_client.get_mode_settings.call_args.args[1] == 1
    # Co-ports are ports 2, 3, 4 (bitmask decode, not gather)
    co_port_nums = {c["port"] for c in data["co_ports_to_lock"]}
    assert co_port_nums == {2, 3, 4}


async def test_break_out_gather_single_port_device(mock_client):
    """Single-port device: automation covers only port 1, co_ports is empty."""
    import copy
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    # Keep only port 1
    device["deviceInfo"]["ports"] = [
        {"port": 1, "portName": "Intake Fan", "speak": 5, "portsLoad": 1,
         "loadState": 1, "curMode": 3, "remainTime": 0}
    ]
    mock_client.get_devices.return_value = [device]
    mock_client.get_mode_settings.return_value = {"modeType": _ADVANCE_MODE_TYPE, "onSpead": 5}
    _auto = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_LIST[0])
    _auto["grouptDevType"] = 1  # port 1 only
    mock_client.get_advance_automations.return_value = [_auto]
    result = await break_out_of_automation("C58ZA", port=1, dry_run=True)
    data = json.loads(result)
    assert data["dry_run"] is True
    # Only the idempotency check for the target port is called
    assert mock_client.get_mode_settings.call_count == 1
    assert mock_client.get_mode_settings.call_args.args[1] == 1
    # No co-ports to lock
    assert data["co_ports_to_lock"] == []


async def test_break_out_ghost_state_empty_automations(mock_client):
    """modeType=15 but get_advance_automations returns [] → ghost state no-op, not error."""
    mock_client.get_mode_settings.return_value = {"modeType": _ADVANCE_MODE_TYPE, "onSpead": 2}
    mock_client.get_advance_automations.return_value = []
    result = await break_out_of_automation("C58ZA", port=1, dry_run=True)
    data = json.loads(result)
    assert "info" in data
    assert "not currently under active automation control" in data["info"]
    mock_client.disable_advance_automation.assert_not_called()


async def test_break_out_ghost_state_active_no_coverage(mock_client):
    """modeType=15 but active automations don't cover port 1 → ghost state no-op."""
    import copy
    mock_client.get_mode_settings.return_value = {"modeType": _ADVANCE_MODE_TYPE, "onSpead": 2}
    # Automation is active but covers only ports 5+6 — not port 1
    _auto = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_LIST[0])
    _auto["grouptDevType"] = 48  # ports 5+6
    mock_client.get_advance_automations.return_value = [_auto]
    result = await break_out_of_automation("C58ZA", port=1, dry_run=True)
    data = json.loads(result)
    assert "info" in data
    assert "not currently under active automation control" in data["info"]
    mock_client.disable_advance_automation.assert_not_called()


async def test_break_out_empty_port_excluded(mock_client):
    """portResistance==65535 co-port is excluded from co_ports_to_lock."""
    import copy
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    # Add ports 5 and 6; port 5 is disconnected (portResistance=65535)
    device["deviceInfo"]["ports"].append(
        {"port": 5, "portName": "Left Fan", "speak": 3, "portsLoad": 0,
         "portResistance": 65535, "loadState": 0, "curMode": 0, "remainTime": 0}
    )
    device["deviceInfo"]["ports"].append(
        {"port": 6, "portName": "Right Fan", "speak": 3, "portsLoad": 1,
         "portResistance": 0, "loadState": 1, "curMode": 3, "remainTime": 0}
    )
    mock_client.get_devices.return_value = [device]
    mock_client.get_mode_settings.return_value = {"modeType": _ADVANCE_MODE_TYPE, "onSpead": 3}
    # Automation covers ports 1+5+6 (grouptDevType = 1+16+32 = 49)
    _auto = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_LIST[0])
    _auto["grouptDevType"] = 49
    mock_client.get_advance_automations.return_value = [_auto]
    result = await break_out_of_automation("C58ZA", port=1, dry_run=True)
    data = json.loads(result)
    assert data["dry_run"] is True
    co_port_nums = {c["port"] for c in data["co_ports_to_lock"]}
    # Port 5 excluded (portResistance=65535); port 6 included
    assert co_port_nums == {6}
    assert len(data["co_ports_to_lock"]) == 1


async def test_break_out_cross_automation_isolation(mock_client):
    """Only ports in the governing automation are locked; other automation's ports untouched."""
    import copy
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    # Add ports 3, 4, 5 so device has ports 1-5
    for p_num, p_name in [(3, "CO2 Fan"), (4, "Heater"), (5, "Left Fan")]:
        device["deviceInfo"]["ports"].append(
            {"port": p_num, "portName": p_name, "speak": 3, "portsLoad": 1,
             "loadState": 1, "curMode": 3, "remainTime": 0}
        )
    mock_client.get_devices.return_value = [device]
    mock_client.get_mode_settings.return_value = {"modeType": _ADVANCE_MODE_TYPE, "onSpead": 3}
    # Automation A covers ports 1+3+5 (grouptDevType=21=0b010101), listed first
    auto_a = {
        "advId": 1001, "advName": "Night Cycle", "isOn": 1, "onSpeed": 5,
        "offSpeed": 0, "grouptDevType": 21, "advKey": "1-0", "runState": 1,
        "beginTime": 255, "endTime": 255, "onTimeSwitch": 0,
    }
    # Automation B covers ports 2+4 (grouptDevType=10=0b001010)
    auto_b = {
        "advId": 1002, "advName": "Day Cycle", "isOn": 1, "onSpeed": 3,
        "offSpeed": 0, "grouptDevType": 10, "advKey": "2-0", "runState": 1,
        "beginTime": 255, "endTime": 255, "onTimeSwitch": 0,
    }
    mock_client.get_advance_automations.return_value = [auto_a, auto_b]
    result = await break_out_of_automation("C58ZA", port=1, dry_run=True)
    data = json.loads(result)
    assert data["dry_run"] is True
    assert data.get("automation_name") == "Night Cycle"
    co_port_nums = {c["port"] for c in data["co_ports_to_lock"]}
    # Only Night Cycle's co-ports (3 and 5), not Day Cycle's (2 and 4)
    assert co_port_nums == {3, 5}


# ============ dry_run_never_writes parametrize ============

@pytest.mark.parametrize("tool_fn,kwargs", [
    (enable_advance_automation,
     {"device_id": "C58ZA", "automation_id": "999001", "dry_run": True}),
    (disable_advance_automation,
     {"device_id": "C58ZA", "automation_id": "1342758", "dry_run": True}),
    (create_advance_automation,
     {"device_id": "C58ZA", "name": "Test", "port": 1, "on_speed": 5, "dry_run": True}),
    (delete_advance_automation,
     {"device_id": "C58ZA", "automation_id": "999001", "dry_run": True}),
    (break_out_of_automation,
     {"device_id": "C58ZA", "port": 1, "dry_run": True}),
])
async def test_dry_run_never_writes(tool_fn, kwargs, mock_client):
    """All write tools with dry_run=True must make zero HTTP write method calls."""
    import copy
    automations = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_LIST)
    # Ensure Pollenation Airflow is disabled (for enable test)
    for e in automations:
        if e["advId"] == 999001:
            e["isOn"] = 0

    mock_client.get_advance_automations.return_value = automations
    mock_client.get_mode_settings.return_value = {
        "modeType": _ADVANCE_MODE_TYPE, "onSpead": 2
    }

    await tool_fn(**kwargs)

    mock_client.enable_advance_automation.assert_not_called()
    mock_client.disable_advance_automation.assert_not_called()
    mock_client.create_advance_automation.assert_not_called()
    mock_client.delete_advance_automation.assert_not_called()
    mock_client.set_port_mode.assert_not_called()


# ============ Live-path tests (Fix 5) ============

async def test_enable_advance_automation_live_calls_once(mock_client):
    """Live enable sends exactly one toggle regardless of adv_ids count."""
    import copy
    automations = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_LIST)
    for e in automations:
        if e["advName"] == "Moderate Airflow":
            e["isOn"] = 0  # currently disabled
    mock_client.get_advance_automations.return_value = automations
    result = await enable_advance_automation("C58ZA", "1342758", dry_run=False)
    data = json.loads(result)
    assert data.get("sent") is True
    assert mock_client.enable_advance_automation.call_count == 1
    # Must pass adv_ids[0] (first entry for "Moderate Airflow") not automation_id itself
    mock_client.enable_advance_automation.assert_called_once_with(
        mock_client.get_devices.return_value[0]["devId"],
        1342758,  # adv_ids[0] for "Moderate Airflow"
    )


async def test_delete_advance_automation_live_disables_first(mock_client):
    """Enabled multi-rule automation: disable once, then delete the whole program in a single
    isflag=1 call. #302 — the old code looped one delete per adv_id; the second delByid on the
    already-deleted program surfaced a false 'API error' even though the delete succeeded.
    'Moderate Airflow' (id 1342758) has two adv_ids (1342758, 2179295)."""
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await delete_advance_automation("C58ZA", "1342758", dry_run=False)
    data = json.loads(result)
    assert "error" not in data
    assert data.get("sent") is True
    assert data.get("was_enabled") is True
    assert data.get("automation_name") == "Moderate Airflow"
    assert mock_client.disable_advance_automation.call_count == 1
    # #302: exactly ONE whole-program delete — not one per adv_id.
    assert mock_client.delete_advance_automation.call_count == 1
    assert mock_client.delete_advance_automation.call_args.args[1] == 1342758  # adv_ids[0]


async def test_delete_single_rule_disabled_automation(mock_client):
    """A disabled single-rule automation deletes in one call with no disable-first toggle.
    'Pollenation Airflow' (999001) is a single-entry, isOn=0 automation."""
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await delete_advance_automation("C58ZA", "999001", dry_run=False)
    data = json.loads(result)
    assert "error" not in data
    assert data["sent"] is True
    assert data["was_enabled"] is False
    assert mock_client.disable_advance_automation.call_count == 0  # already disabled
    assert mock_client.delete_advance_automation.call_count == 1


async def test_get_advance_automation_no_schedule_sentinel(mock_client):
    """beginTime=255 (v2.0 no-schedule) → begin_time is None in response."""
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    # "Moderate Airflow" has beginTime=255, endTime=255
    result = await get_advance_automation("C58ZA", "1342758")
    data = json.loads(result)
    assert data.get("schedule", {}).get("begin_time") is None
    assert data.get("schedule", {}).get("end_time") is None


# ============ _sanitize_api_string (Fix 4) ============

def test_sanitize_api_string_strips_cc_cf_categories():
    """Cc characters (ASCII control chars) and Cf (format chars) are stripped via unicodedata."""
    from ac_infinity_mcp.server import _sanitize_api_string
    assert _sanitize_api_string("Hello\x00World") == "HelloWorld"
    assert _sanitize_api_string("Test\x1fName") == "TestName"


def test_sanitize_api_string_strips_format_chars():
    """Cf characters (Unicode format chars like soft-hyphen) are stripped."""
    from ac_infinity_mcp.server import _sanitize_api_string
    # Soft hyphen (U+00AD) is Cf category
    assert _sanitize_api_string("He­llo") == "Hello"


def test_sanitize_api_string_preserves_cjk():
    """CJK and other non-ASCII printable characters are preserved."""
    from ac_infinity_mcp.server import _sanitize_api_string
    assert _sanitize_api_string("日本語テスト") == "日本語テスト"
    assert _sanitize_api_string("한국어") == "한국어"
    assert _sanitize_api_string("中文名称") == "中文名称"


def test_sanitize_api_string_empty_fallback():
    """Empty result after stripping returns '(unnamed)'."""
    from ac_infinity_mcp.server import _sanitize_api_string
    assert _sanitize_api_string("\x00\x01\x02") == "(unnamed)"
    assert _sanitize_api_string("") == "(unnamed)"
    assert _sanitize_api_string(None) == "(unnamed)"


# ============ _format_schedule_time v2.0 sentinel (Fix 2) ============

def test_format_schedule_time_255_sentinel():
    """255 (v2.0 no-schedule) → None, same as 65535."""
    assert _format_schedule_time(255) is None


# ============ Quality Cycle fixes ============

def test_group_automations_none_advname_groups_together():
    """Multiple entries with advName=None group under '(unnamed)' as one automation."""
    entries = [
        {"advId": 1, "advName": None, "isOn": 1, "runState": 0, "onSpeed": 2, "offSpeed": 0,
         "grouptDevType": 8, "beginTime": 255, "endTime": 255},
        {"advId": 2, "advName": None, "isOn": 0, "runState": 0, "onSpeed": 1, "offSpeed": 0,
         "grouptDevType": 4, "beginTime": 255, "endTime": 255},
    ]
    grouped = _group_automations(entries)
    assert len(grouped) == 1
    assert grouped[0]["name"] == "(unnamed)"
    assert grouped[0]["automation_id"] == 1  # first entry's advId


async def test_build_advance_conflict_response_degraded(mock_client):
    """get_advance_automations raises → conflict response with null automation_name."""
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    mock_client.get_advance_automations.side_effect = ACInfinityAPIError("fail")
    result = await set_port_off("C58ZA", port=1, dry_run=False)
    data = json.loads(result)
    assert data.get("conflict") == "ADVANCE_AUTOMATION"
    assert data.get("automation_name") is None
    assert data.get("active_automations") == []
    assert "None" not in data["options"]["1_find_and_disable"]["instruction"]
    assert "automations" in data["options"]["1_find_and_disable"]["instruction"].lower()
    assert "1_break_out" not in data["options"]
    assert "suggested_reply" in data


async def test_build_advance_conflict_response_auth_error(mock_client):
    """get_advance_automations raises ACInfinityAuthError → auth error JSON returned directly."""
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    mock_client.get_advance_automations.side_effect = ACInfinityAuthError("test")
    result = await set_port_speed("C58ZA", port=1, speed=5, dry_run=False)
    data = json.loads(result)
    assert data.get("error") == (
        "Authentication failed — check AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD "
        "(note: AC Infinity passwords are limited to 25 characters; longer ones are "
        "truncated and login will fail)"
    )
    # #262: the auth-failure message must surface the 25-character password limit so a
    # grower (who never sees the server log) has a diagnostic path from Claude's reply alone.
    assert "25 characters" in data["error"]
    assert data.get("detail") == "see server logs"
    assert "conflict" not in data
    assert "options" not in data


async def test_conflict_response_summary_is_controller_level(mock_client):
    """Conflict summary mentions automation and controller — controller-level framing."""
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await set_port_off("C58ZA", port=1, dry_run=False)
    data = json.loads(result)
    assert "automation" in data["summary"].lower()
    assert "controller" in data["summary"].lower()
    assert "suggested_reply" in data


async def test_conflict_response_option_1_is_break_out(mock_client):
    """Option 1 uses break_out_of_automation tool with available=True.

    Port 4 is used because MOCK_ADVANCE_AUTOMATIONS_LIST has grouptDevType=8 (Port 4),
    so the bitmask lookup yields Sub-path A and 1_break_out is offered.
    """
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await set_port_off("C58ZA", port=4, dry_run=False)
    data = json.loads(result)
    assert "1_break_out" in data["options"]
    assert data["options"]["1_break_out"]["_tool"] == "break_out_of_automation"
    assert data["options"]["1_break_out"]["available"] is True
    assert "suggested_reply" in data


@pytest.mark.parametrize(
    "is_on,run_state_val",
    [
        (1, 1),  # enabled=True, run_state=True → available True (normal case)
        (0, 1),  # enabled=False, run_state=True → available True (Issue #84 bug case)
    ],
)
@pytest.mark.asyncio
async def test_conflict_response_option_1_available_includes_run_state(
    mock_client, is_on, run_state_val
):
    """opt1.available is True whenever governing automation has enabled OR run_state.

    Issue #84: The selection logic uses ``enabled or run_state`` to find the governing
    automation, but the original code set ``available`` using only ``enabled``.  A
    mid-toggle transient state (isOn=0, runState=1) would therefore select a governing
    automation but then mark opt1 as unavailable — preventing break_out_of_automation
    from being offered even though it would work.

    Note: the all-disabled boundary guard
    (test_conflict_response_all_automations_disabled_uses_all_disabled_path) still relies
    on both isOn=0 and runState=0 — that test must continue to pass unchanged.
    """
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    automations = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_LIST)
    # Mutate the first entry (Moderate Airflow group lead) to the desired state.
    automations[0]["isOn"] = is_on
    automations[0]["runState"] = run_state_val
    # Ensure the second entry for the same automation group also reflects the state so
    # _group_automations picks up the right enabled/run_state from entries[0].
    automations[1]["isOn"] = is_on
    automations[1]["runState"] = run_state_val
    mock_client.get_advance_automations.return_value = automations
    # Port 4 is in MOCK_ADVANCE_AUTOMATIONS_LIST (grouptDevType=8, bit 3 = Port 4),
    # so the bitmask lookup yields Sub-path A and offers 1_break_out.
    result = await set_port_speed("C58ZA", port=4, speed=3, dry_run=False)
    data = json.loads(result)
    assert data.get("conflict") == "ADVANCE_AUTOMATION"
    assert "1_break_out" in data["options"], (
        f"Expected 1_break_out in options for isOn={is_on}, runState={run_state_val}"
    )
    assert data["options"]["1_break_out"]["available"] is True, (
        f"opt1.available should be True for isOn={is_on}, runState={run_state_val}"
    )


async def test_conflict_response_active_automations_is_list_of_objects(mock_client):
    """active_automations is a list of dicts with 'name' and 'automation_id' keys."""
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await set_port_off("C58ZA", port=1, dry_run=False)
    data = json.loads(result)
    assert isinstance(data["active_automations"], list)
    for item in data["active_automations"]:
        assert "name" in item
        assert "automation_id" in item
    assert "suggested_reply" in data


async def test_conflict_response_human_summary_present(mock_client):
    """human_summary field is present, non-empty string."""
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await set_port_off("C58ZA", port=1, dry_run=False)
    data = json.loads(result)
    assert "human_summary" in data
    assert isinstance(data["human_summary"], str)
    assert len(data["human_summary"]) > 0
    assert "suggested_reply" in data


async def test_conflict_response_empty_automations_list(mock_client):
    """get_advance_automations returns [] → conflict type correct, active_automations empty."""
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    mock_client.get_advance_automations.return_value = []
    result = await set_port_off("C58ZA", port=1, dry_run=False)
    data = json.loads(result)
    assert data["conflict"] == "ADVANCE_AUTOMATION"
    assert data["automation_name"] is None
    assert data["active_automations"] == []
    assert "suggested_reply" in data


async def test_conflict_response_all_automations_disabled_uses_all_disabled_path(mock_client):
    """All automations disabled (isOn=0, runState=0) → governing=None, all-disabled-path summary."""
    import copy
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    disabled_automations = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_SINGLE)
    # MOCK_ADVANCE_AUTOMATIONS_SINGLE has isOn=0, runState=0
    mock_client.get_advance_automations.return_value = disabled_automations
    result = await set_port_off("C58ZA", port=1, dry_run=False)
    data = json.loads(result)
    assert data["automation_name"] is None
    assert data["active_automations"] == []
    assert "automations" in data["options"]["1_re_disable_to_clear"]["instruction"].lower()
    assert "suggested_reply" in data


@pytest.mark.asyncio
async def test_conflict_instructions_no_dry_run(mock_client):
    """No instruction field in any conflict path contains the string 'dry_run'."""
    import copy

    # Normal path
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await set_port_off("C58ZA", port=1, dry_run=False)
    data = json.loads(result)
    for opt in data.get("options", {}).values():
        assert "dry_run" not in opt.get("instruction", "")
    assert "dry_run" not in data.get("switching_guidance", "")

    # All-disabled path
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    disabled = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_SINGLE)
    mock_client.get_advance_automations.return_value = disabled
    result2 = await set_port_off("C58ZA", port=1, dry_run=False)
    data2 = json.loads(result2)
    for opt in data2.get("options", {}).values():
        assert "dry_run" not in opt.get("instruction", "")

    # Degraded path
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    mock_client.get_advance_automations.side_effect = ACInfinityAPIError("fail")
    result3 = await set_port_off("C58ZA", port=1, dry_run=False)
    data3 = json.loads(result3)
    for opt in data3.get("options", {}).values():
        assert "dry_run" not in opt.get("instruction", "")


@pytest.mark.asyncio
async def test_conflict_instructions_no_function_syntax(mock_client):
    """No instruction or switching_guidance field exposes Python function call syntax."""
    # Normal path — check device_id=, automation_id= absent from all instructions
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await set_port_off("C58ZA", port=1, dry_run=False)
    data = json.loads(result)
    for opt in data.get("options", {}).values():
        assert "device_id=" not in opt.get("instruction", "")
        assert "automation_id=" not in opt.get("instruction", "")
    assert "disable_advance_automation" not in data.get("switching_guidance", "")
    assert "create_advance_automation" not in data.get("switching_guidance", "")

    # Degraded path
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    mock_client.get_advance_automations.side_effect = ACInfinityAPIError("fail")
    result2 = await set_port_off("C58ZA", port=1, dry_run=False)
    data2 = json.loads(result2)
    for opt in data2.get("options", {}).values():
        assert "device_id=" not in opt.get("instruction", "")
        assert "automation_id=" not in opt.get("instruction", "")


@pytest.mark.asyncio
async def test_conflict_normal_path_instructions_contain_natural_language(mock_client):
    """Normal path opt1 and opt2 instruction fields use natural-language text.

    Verified format: "Ask me to release <port> from the '<automation>' automation ..."
    and "Ask me to disable the '<automation>' automation — ..."
    No Python function call syntax; no dry_run; no internal parameter names.

    Port 4 is used because MOCK_ADVANCE_AUTOMATIONS_LIST has grouptDevType=8 (bit 3 = Port 4),
    so the bitmask lookup yields Sub-path A and 1_break_out is offered.
    """
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await set_port_off("C58ZA", port=4, dry_run=False)
    data = json.loads(result)
    opt1 = data["options"]["1_break_out"]
    opt2 = data["options"]["2_disable_automation"]
    # opt1 must instruct release from the named automation in plain language
    assert "ask me" in opt1["instruction"].lower()
    assert "release" in opt1["instruction"].lower()
    assert "automation" in opt1["instruction"].lower()
    # opt2 must instruct disabling the named automation in plain language
    assert "ask me" in opt2["instruction"].lower()
    assert "disable" in opt2["instruction"].lower()
    assert "automation" in opt2["instruction"].lower()


@pytest.mark.asyncio
async def test_conflict_switching_guidance_no_function_names(mock_client):
    """switching_guidance field must not contain raw tool function names."""
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await set_port_off("C58ZA", port=1, dry_run=False)
    data = json.loads(result)
    sg = data.get("switching_guidance", "")
    assert "switching_guidance" in data
    assert "disable_advance_automation" not in sg
    assert "create_advance_automation" not in sg
    assert "ask me" in sg.lower()


# ============ Issue #107 — Option 0 (update_speed) in conflict response ============


@pytest.mark.asyncio
async def test_set_port_speed_conflict_includes_option_0_update_speed(mock_client):
    """set_port_speed conflict response includes option '0_update_speed' in normal path.

    Port 4 is used because MOCK_ADVANCE_AUTOMATIONS_LIST has grouptDevType=8 (bit 3 = Port 4),
    so the bitmask lookup yields Sub-path A where 0_update_speed is offered.
    """
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await set_port_speed("C58ZA", 4, 7)
    data = json.loads(result)
    assert data["conflict"] == "ADVANCE_AUTOMATION"
    # Option 0 must be present when called from set_port_speed (requested_speed=7)
    assert "0_update_speed" in data["options"]
    opt0 = data["options"]["0_update_speed"]
    assert opt0["available"] is True
    # Must mention the requested speed (7) in description
    assert "7" in opt0["description"]
    # Must mention the current auto speed — port 4 is governed by entry index 1
    # (grouptDevType=8 = bit 3 = Port 4, onSpeed=1).
    current_speed = str(
        MOCK_ADVANCE_AUTOMATIONS_LIST[1]["onSpeed"]  # entry with grouptDevType=8 (Port 4)
    )
    assert current_speed in opt0["description"] or "?" in opt0["description"]
    # instruction must be natural language
    assert "Ask me" in opt0["instruction"]
    assert "7" in opt0["instruction"]
    assert "dry_run" not in opt0["instruction"]
    assert "device_id=" not in opt0["instruction"]


@pytest.mark.asyncio
async def test_set_port_on_conflict_has_no_option_0_update_speed(mock_client):
    """set_port_on conflict response does NOT include option '0_update_speed'."""
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await set_port_on("C58ZA", 1)
    data = json.loads(result)
    assert data["conflict"] == "ADVANCE_AUTOMATION"
    # set_port_on passes requested_speed=None → option 0 must be absent
    assert "0_update_speed" not in data["options"]


@pytest.mark.asyncio
async def test_set_port_off_conflict_has_no_option_0_update_speed(mock_client):
    """set_port_off conflict response does NOT include option '0_update_speed'."""
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await set_port_off("C58ZA", 1)
    data = json.loads(result)
    assert data["conflict"] == "ADVANCE_AUTOMATION"
    # set_port_off passes requested_speed=None → option 0 must be absent
    assert "0_update_speed" not in data["options"]


@pytest.mark.asyncio
async def test_conflict_option_0_not_present_in_degraded_path(mock_client):
    """Option '0_update_speed' must NOT appear in the degraded path (API error)."""
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    mock_client.get_advance_automations.side_effect = ACInfinityAPIError("fail")
    result = await set_port_speed("C58ZA", 1, 7)
    data = json.loads(result)
    assert data["conflict"] == "ADVANCE_AUTOMATION"
    # Degraded path: automation lookup failed → option 0 not available
    assert "0_update_speed" not in data["options"]


@pytest.mark.asyncio
async def test_conflict_option_0_not_present_in_all_disabled_path(mock_client):
    """Option '0_update_speed' must NOT appear in the all-disabled path."""
    import copy
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    disabled_automations = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_SINGLE)
    # MOCK_ADVANCE_AUTOMATIONS_SINGLE has isOn=0, runState=0 → all-disabled path
    mock_client.get_advance_automations.return_value = disabled_automations
    result = await set_port_speed("C58ZA", 1, 7)
    data = json.loads(result)
    assert data["conflict"] == "ADVANCE_AUTOMATION"
    # All-disabled path: no governing automation → option 0 not available
    assert "0_update_speed" not in data["options"]


# ============ Issue #133 — Pre-write guard from device_data (server level) ============


@pytest.mark.asyncio
async def test_set_port_speed_conflict_fires_before_get_mode_settings_on_advance_port(mock_client):
    """ACInfinityAdvanceConflictError from pre-write guard returns structured conflict.

    The conflict comes from isOpenAutomation=1 in device_data BEFORE get_mode_settings
    is called — tests that the server's _build_advance_conflict_response is still reached.

    Port 4 is used because MOCK_ADVANCE_AUTOMATIONS_LIST has grouptDevType=8 (bit 3 = Port 4),
    so the bitmask lookup yields Sub-path A where 0_update_speed is offered.
    """
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError(
        "Port 4 on device 12345 is in smart automation mode (isOpenAutomation=1 in devInfoListAll)"
    )
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await set_port_speed("C58ZA", 4, 5)
    data = json.loads(result)
    assert data["conflict"] == "ADVANCE_AUTOMATION"
    assert "0_update_speed" in data["options"]  # speed=5 was passed
    assert data["target_port"] == "Port 4"


@pytest.mark.asyncio
async def test_set_port_speed_conflict_999999_defense_in_depth(mock_client):
    """ACInfinityAdvanceConflictError from 999999 fallback returns structured conflict.

    Covers the defense-in-depth path where the pre-write guard misses the conflict
    and the write API returns code 999999 (ADVANCE conflict sentinel).

    Port 4 is used because MOCK_ADVANCE_AUTOMATIONS_LIST has grouptDevType=8 (bit 3 = Port 4),
    so the bitmask lookup yields Sub-path A where 0_update_speed is offered.
    """
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError(
        "Port 4 on device 12345 rejected write with code 999999 — port is under Advance "
        "Automation control."
    )
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await set_port_speed("C58ZA", 4, 3)
    data = json.loads(result)
    assert data["conflict"] == "ADVANCE_AUTOMATION"
    # speed=3 was the requested speed — option 0 should appear
    assert "0_update_speed" in data["options"]


async def test_enable_advance_automation_not_found(mock_client):
    """Valid automation_id format but ID not in device's automation list → error."""
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await enable_advance_automation("C58ZA", "9999999", dry_run=False)
    data = json.loads(result)
    assert "error" in data
    assert "9999999" in data["error"] or "not found" in data["error"]


async def test_disable_advance_automation_live_calls_once(mock_client):
    """Live disable sends exactly one toggle using adv_ids[0] and includes governed_ports."""
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await disable_advance_automation("C58ZA", "1342758", dry_run=False)
    data = json.loads(result)
    assert data.get("sent") is True
    assert data["to_restore"] == "Ask me to re-enable 'Moderate Airflow'."
    assert isinstance(data["governed_ports"], list)
    assert mock_client.disable_advance_automation.call_count == 1
    mock_client.disable_advance_automation.assert_called_once_with(
        mock_client.get_devices.return_value[0]["devId"],
        1342758,  # adv_ids[0] for "Moderate Airflow"
    )


async def test_disable_advance_automation_live_human_summary(mock_client):
    """Live disable response includes human_summary confirming immediate restore."""
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await disable_advance_automation("C58ZA", "1342758", dry_run=False)
    data = json.loads(result)
    assert "human_summary" in data
    assert "restore automation control immediately" in data["human_summary"]


async def test_break_out_no_enabled_automation(mock_client):
    """All automations disabled → ghost-state no-op info response."""
    import copy
    automations = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_LIST)
    for e in automations:
        e["isOn"] = 0
        e["runState"] = 0
        e["grouptDevType"] = 1  # covers port 1 so _find_governing_automation returns None
                                # only because of the enabled check, not bitmask mismatch
    mock_client.get_mode_settings.return_value = {"modeType": _ADVANCE_MODE_TYPE, "onSpead": 2}
    mock_client.get_advance_automations.return_value = automations
    result = await break_out_of_automation("C58ZA", port=1, dry_run=True)
    data = json.loads(result)
    assert "info" in data
    assert "not currently under active automation control" in data["info"]
    mock_client.disable_advance_automation.assert_not_called()


async def test_break_out_selects_run_state_only_automation(mock_client):
    """Port is ADVANCE; isOn=0 but runState=1 (mid-toggle) → run_state-only fallback selects."""
    import copy
    _auto = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_LIST[0])
    _auto["isOn"] = 0
    _auto["runState"] = 1
    _auto["grouptDevType"] = 1  # covers port 1
    mock_client.get_mode_settings.return_value = {"modeType": _ADVANCE_MODE_TYPE, "onSpead": 2}
    mock_client.get_advance_automations.return_value = [_auto]
    result = await break_out_of_automation("C58ZA", port=1, dry_run=True)
    data = json.loads(result)
    assert "release" in (data.get("action") or "")
    assert "sequence" in data
    assert data.get("automation_name") == "Moderate Airflow"


async def test_break_out_disable_fails_rolls_back(mock_client):
    """Disable step fails → rollback re-enable attempted, structured error returned."""
    import copy
    mock_client.get_mode_settings.return_value = {"modeType": _ADVANCE_MODE_TYPE, "onSpead": 2}
    _auto = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_LIST[0])
    _auto["grouptDevType"] = 1  # port 1 only
    mock_client.get_advance_automations.return_value = [_auto]
    mock_client.disable_advance_automation.side_effect = RuntimeError("network error")
    mock_client.enable_advance_automation.return_value = {"code": 200}
    result = await break_out_of_automation(
        "C58ZA", port=1, dry_run=False,
        confirm_automation_name="Moderate Airflow",
    )
    data = json.loads(result)
    assert "error" in data
    assert "failed_step" in data
    mock_client.set_port_mode.assert_not_called()


async def test_break_out_lock_port_fails_rollback(mock_client):
    """Co-port lock step fails → rollback attempted, structured error with rollback fields."""
    import copy
    mock_client.get_mode_settings.return_value = {"modeType": _ADVANCE_MODE_TYPE, "onSpead": 2}
    # grouptDevType=3 → ports 1+2 so port 2 is a co-port and the lock will fail
    _auto = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_LIST[0])
    _auto["grouptDevType"] = 3
    mock_client.get_advance_automations.return_value = [_auto]
    mock_client.disable_advance_automation.return_value = {"code": 200}
    mock_client.set_port_mode.side_effect = RuntimeError("port lock failed")
    mock_client.enable_advance_automation.return_value = {"code": 200}  # rollback succeeds
    result = await break_out_of_automation(
        "C58ZA", port=1, dry_run=False,
        confirm_automation_name="Moderate Airflow",
    )
    data = json.loads(result)
    assert "error" in data
    assert "failed_step" in data
    assert data.get("rollback_attempted") is True
    assert "recovery_steps" in data
    assert len(data["recovery_steps"]) > 0


async def test_create_advance_automation_wraparound_window_permitted(mock_client):
    """begin_time > end_time (wrap-around, e.g. lights-on 09:00→03:00) is PERMITTED —
    required for the two-window pattern; consistent with add_automation_rule and the app."""
    result = await create_advance_automation(
        "C58ZA", "Test", on_speed=5, port=1, begin_time=1200, end_time=60, dry_run=True
    )
    data = json.loads(result)
    assert "error" not in data
    assert data.get("dry_run") is True


async def test_break_out_confirm_name_too_long(mock_client):
    """confirm_automation_name > 256 chars → structured error, no writes."""
    import copy
    mock_client.get_mode_settings.return_value = {"modeType": _ADVANCE_MODE_TYPE, "onSpead": 2}
    _auto = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_LIST[0])
    _auto["grouptDevType"] = 1  # port 1 only
    mock_client.get_advance_automations.return_value = [_auto]
    result = await break_out_of_automation(
        "C58ZA", port=1, dry_run=False,
        confirm_automation_name="A" * 257,
    )
    data = json.loads(result)
    assert "error" in data
    assert "too long" in data["error"]
    mock_client.disable_advance_automation.assert_not_called()


async def test_get_advance_automation_single_group_no_schedule(mock_client):
    """No onTimeSwitch field → continuous mode → human_summary contains 'continuously'."""
    single_no_schedule = [
        {
            "advId": 88001,
            "advName": "Night Fan",
            "isOn": 1,
            "onSpeed": 4,
            "offSpeed": 0,
            "grouptDevType": 8,
            "advKey": "1-0",
            "runState": 1,
            "beginTime": 255,
            "endTime": 255,
        }
    ]
    mock_client.get_advance_automations.return_value = single_no_schedule
    result = await get_advance_automation("C58ZA", "88001")
    data = json.loads(result)
    assert "human_summary" in data
    assert "continuously" in data["human_summary"].lower()
    assert "speed 4" in data["human_summary"]


async def test_get_advance_automation_continuous_mode_schedule_dict(mock_client):
    """onTimeSwitch=0 with sentinel times (255) → mode='continuous', both times None."""
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await get_advance_automation("C58ZA", "1342758")
    data = json.loads(result)
    assert data["schedule"]["mode"] == "continuous"
    assert data["schedule"]["begin_time"] is None
    assert data["schedule"]["end_time"] is None
    assert "schedule_note" not in data["schedule"]


async def test_get_advance_automation_continuous_rule_window_not_clock_range(mock_client):
    """Per-rule read-back: a switchTime=255 (continuous) rule's window reads 'runs
    continuously', never a clock range — so control and window agree on the read surface."""
    continuous_rule = [
        {**copy.deepcopy(MOCK_RULE_HUMIDITY_SETPOINT), "advName": "AllDay",
         "advId": 88001, "grouptDevType": 1, "beginTime": 540, "endTime": 1020,
         "switchTime": 255, "runState": 1},
    ]
    mock_client.get_advance_automations.return_value = continuous_rule
    result = await get_advance_automation("C58ZA", "88001")
    data = json.loads(result)
    rule_window = data["rules"][0]["window"]
    assert rule_window == "runs continuously"
    assert "–" not in rule_window
    assert "runs continuously" in data["rules"][0]["control"]


async def test_get_advance_automation_scheduled_mode_schedule_dict(mock_client):
    """Scheduled mode (onTimeSwitch=0) with valid times → mode='scheduled', times formatted."""
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_SINGLE
    result = await get_advance_automation("C58ZA", "999001")
    data = json.loads(result)
    assert data["schedule"]["mode"] == "scheduled"
    assert data["schedule"]["begin_time"] == "09:00"
    assert data["schedule"]["end_time"] == "17:00"
    assert "schedule_note" not in data["schedule"]


async def test_get_advance_automation_continuous_24_7_toggle_overrides_schedule(mock_client):
    """onTimeSwitch=1 means 'Continuous 24H/7D' toggle is ON — continuous even with real times."""
    toggle_on_with_times = [
        {
            "advId": 77001,
            "advName": "Ventilation",
            "isOn": 1,
            "onSpeed": 5,
            "offSpeed": 0,
            "grouptDevType": 8,
            "advKey": "1-0",
            "runState": 1,
            "beginTime": 540,
            "endTime": 1020,
            "onTimeSwitch": 1,
        }
    ]
    mock_client.get_advance_automations.return_value = toggle_on_with_times
    result = await get_advance_automation("C58ZA", "77001")
    data = json.loads(result)
    assert data["schedule"]["mode"] == "continuous"
    assert data["schedule"]["begin_time"] is None
    assert data["schedule"]["end_time"] is None
    assert "schedule_note" not in data["schedule"]


async def test_get_advance_automation_unknown_on_time_switch_treated_as_continuous(mock_client):
    """Unknown onTimeSwitch value (>1) falls through to continuous mode — unknown values safe."""
    unknown_mode = [
        {
            "advId": 55001,
            "advName": "Fan",
            "isOn": 1,
            "onSpeed": 3,
            "offSpeed": 0,
            "grouptDevType": 8,
            "advKey": "1-0",
            "runState": 1,
            "beginTime": 540,
            "endTime": 1020,
            "onTimeSwitch": 2,
        }
    ]
    mock_client.get_advance_automations.return_value = unknown_mode
    result = await get_advance_automation("C58ZA", "55001")
    data = json.loads(result)
    assert data["schedule"]["mode"] == "continuous"
    assert data["schedule"]["begin_time"] is None
    assert data["schedule"]["end_time"] is None


# ============ Issue #68 — suggested_reply in conflict response ============

async def test_build_advance_conflict_suggested_reply_normal(mock_client):
    """suggested_reply on Sub-path A (port in bitmask) contains automation name.

    Port 4 is used because MOCK_ADVANCE_AUTOMATIONS_LIST has grouptDevType=8 (Port 4),
    so the bitmask lookup yields Sub-path A and suggested_reply names the automation.
    """
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await set_port_off("C58ZA", port=4, dry_run=False)
    data = json.loads(result)
    assert "suggested_reply" in data
    assert "Moderate Airflow" in data["suggested_reply"]
    assert isinstance(data["suggested_reply"], str)
    assert len(data["suggested_reply"]) > 0


async def test_build_advance_conflict_suggested_reply_degraded(mock_client):
    """suggested_reply on degraded path is a non-empty string."""
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    mock_client.get_advance_automations.side_effect = ACInfinityAPIError("fail")
    result = await set_port_off("C58ZA", port=1, dry_run=False)
    data = json.loads(result)
    assert "suggested_reply" in data
    assert isinstance(data["suggested_reply"], str) and len(data["suggested_reply"]) > 0


# ============ Issue #60 — get_port_settings ADVANCE enrichment ============

async def test_get_port_settings_advance_enrichment_governing_found(mock_client):
    """modeType=15 + active automation → response includes automation_name and id.

    Port 5 is used because MOCK_ADVANCE_AUTOMATIONS_LIST has grouptDevType=48
    (bitmask 0b110000 = bits 4 and 5 = Ports 5 and 6), so the bitmask lookup
    resolves Moderate Airflow as the governing automation for port 5.
    """
    import copy
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["deviceInfo"]["ports"].append(
        {"port": 5, "portName": "Exhaust 2", "speak": 2, "portsLoad": 1,
         "loadState": 1, "curMode": 15, "remainTime": 0}
    )
    mock_client.get_devices.return_value = [device]
    mock_client.get_mode_settings.return_value = {**MOCK_MODE_SETTINGS_BASIC, "modeType": 15}
    result = await get_port_settings("C58ZA", 5)
    data = json.loads(result)
    assert data["mode"] == "ADVANCE"
    assert data["advance_automation"] is True
    assert data["automation_name"] == "Moderate Airflow"
    assert data["automation_id"] == 1342758
    assert data["speed_target"] is None
    assert data["current_speed"] == 2  # port 5 speak=2
    assert "automation_on_speed" in data
    assert data["automation_running"] is True
    assert data["automation_configured"] is True
    assert "human_summary" in data


async def test_get_port_settings_advance_enrichment_no_governing(mock_client):
    """modeType=15 but all automations disabled → automation_name/id are None."""
    import copy
    disabled = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_LIST)
    for e in disabled:
        e["isOn"] = 0
        e["runState"] = 0
    mock_client.get_advance_automations.return_value = disabled
    mock_client.get_mode_settings.return_value = {**MOCK_MODE_SETTINGS_BASIC, "modeType": 15}
    result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert data["mode"] == "ADVANCE"
    assert data["automation_name"] is None
    assert data["automation_id"] is None
    assert data["speed_target"] is None
    assert data["automation_running"] is False
    assert data["automation_configured"] is True


async def test_get_port_settings_advance_secondary_call_fails_degrades(mock_client):
    """Secondary get_advance_automations failure → graceful degrade with note."""
    mock_client.get_mode_settings.return_value = {**MOCK_MODE_SETTINGS_BASIC, "modeType": 15}
    mock_client.get_advance_automations.side_effect = ACInfinityAPIError("fail")
    result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert data["mode"] == "ADVANCE"
    assert data["advance_automation"] is True
    assert data["automation_name"] is None
    assert "advisory" in data
    assert data.get("automation_configured") is None
    assert data.get("automation_running") is None


async def test_get_port_settings_advance_isOpenAutomation_zero_falls_through(mock_client):
    """modeType=15 but isOpenAutomation=0 → normal parse path (automation disabled)."""
    mock_client.get_mode_settings.return_value = {
        **MOCK_MODE_SETTINGS_BASIC, "modeType": 15, "isOpenAutomation": 0, "atType": 1,
    }
    result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert data["mode"] == "OFF"  # atType=1 → "OFF"
    assert "advance_automation" not in data
    mock_client.get_advance_automations.assert_not_called()


async def test_conflict_response_normal_suggested_reply_discloses_consequence(mock_client):
    """Sub-path A suggested_reply discloses that releasing affects all ports on the automation.

    Port 4 is used (grouptDevType=8 = Port 4) so the bitmask lookup yields Sub-path A.
    """
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await set_port_off("C58ZA", port=4, dry_run=False)
    data = json.loads(result)
    suggested = data["suggested_reply"]
    assert any(word in suggested.lower() for word in ["all", "other", "ports"])


async def test_conflict_response_all_disabled_suggested_reply_force_release(mock_client):
    """All-disabled path suggested_reply mentions stuck port and force-release."""
    import copy
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    disabled_automations = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_SINGLE)
    mock_client.get_advance_automations.return_value = disabled_automations
    result = await set_port_off("C58ZA", port=1, dry_run=False)
    data = json.loads(result)
    suggested = data["suggested_reply"]
    assert any(word in suggested.lower() for word in ["stuck", "force", "re-applying", "release"])


async def test_get_port_settings_advance_human_summary_present(mock_client):
    """ADVANCE mode response includes non-empty human_summary."""
    mock_client.get_mode_settings.return_value = {**MOCK_MODE_SETTINGS_BASIC, "modeType": 15}
    result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert data["mode"] == "ADVANCE"
    assert "human_summary" in data
    assert isinstance(data["human_summary"], str)
    assert len(data["human_summary"]) > 0


async def test_get_port_settings_advance_isOpenAutomation_absent_defaults_to_active(mock_client):
    """modeType=15 with absent isOpenAutomation → safe-fail: enters ADVANCE branch."""
    mock_client.get_mode_settings.return_value = {**MOCK_MODE_SETTINGS_BASIC, "modeType": 15}
    result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert data["mode"] == "ADVANCE"
    assert data["advance_automation"] is True


async def test_get_port_settings_advance_secondary_call_auth_fails_propagates(mock_client):
    """Secondary call raises ACInfinityAuthError → propagates, returns auth error."""
    mock_client.get_mode_settings.return_value = {**MOCK_MODE_SETTINGS_BASIC, "modeType": 15}
    mock_client.get_advance_automations.side_effect = ACInfinityAuthError("bad creds")
    result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert "error" in data
    assert "Authentication failed" in data["error"]


async def test_get_port_settings_advance_current_speed_from_speak(mock_client):
    """current_speed is drawn from port's speak field in devInfoListAll."""
    import copy
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["deviceInfo"]["ports"][0]["speak"] = 9
    mock_client.get_devices.return_value = [device]
    mock_client.get_mode_settings.return_value = {**MOCK_MODE_SETTINGS_BASIC, "modeType": 15}
    result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert data["current_speed"] == 9


# ============ Issue #61 — get_advance_automation port resolution + device_type ============

async def test_get_advance_automation_device_type_labels(mock_client):
    """port_groups device_type resolves bitmask to port name labels."""
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await get_advance_automation("C58ZA", "1342758")
    data = json.loads(result)
    # Moderate Airflow: first entry grouptDevType=48, second=8
    assert "device_type" in data["port_groups"][0]
    assert "grp_dev_type" not in data["port_groups"][0]
    # bitmask 48 = bits 4,5 → ports 5,6 (not in MOCK_DEVICE_LEGACY → "Port N" fallback)
    assert data["port_groups"][0]["device_type"] == "Port 5 (Port 5), Port 6 (Port 6)"
    # bitmask 8 = bit 3 → port 4
    assert data["port_groups"][1]["device_type"] == "Port 4 (Port 4)"


@pytest.mark.asyncio
async def test_get_advance_automation_device_type_uses_port_names_when_named(mock_client):
    """port_groups device_type shows named port labels when device has named ports."""
    import copy
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    # bitmask 48 = 0b00110000: bit 4 → port 5, bit 5 → port 6
    device["deviceInfo"]["ports"].extend([
        {"port": 5, "portName": "Scrubber"},
        {"port": 6, "portName": "Clip Fan"},
    ])
    mock_client.get_devices.return_value = [device]
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await get_advance_automation("C58ZA", "1342758")
    data = json.loads(result)
    # Moderate Airflow port_groups[0] governs ports 5+6 (bitmask 48)
    assert data["port_groups"][0]["device_type"] == "Scrubber (Port 5), Clip Fan (Port 6)"


async def test_get_advance_automation_no_advance_ports(mock_client):
    """Bitmask decode populates governed_ports from automation port_groups.

    MOCK_ADVANCE_AUTOMATIONS_LIST "Moderate Airflow" has two port_groups:
      - grouptDevType=48 → bits 4,5 → Ports 5 and 6
      - grouptDevType=8  → bit 3  → Port 4
    MOCK_DEVICE_LEGACY only has ports 1 and 2, so ports 4/5/6 fall back to
    'Port N' labels.  port_resolution is 'resolved'.
    """
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await get_advance_automation("C58ZA", "1342758")
    data = json.loads(result)
    assert data["port_resolution"] == "resolved"
    port_nums = [gp["port"] for gp in data["governed_ports"]]
    assert sorted(port_nums) == [4, 5, 6]


async def test_get_advance_automation_port_resolution_single_automation(mock_client):
    """Single-group automation: governed_ports decoded from bitmask.

    MOCK_ADVANCE_AUTOMATIONS_SINGLE has grouptDevType=4 (bit 2 = Port 3).
    MOCK_DEVICE_LEGACY has no port 3, so the label falls back to 'Port 3'.
    port_resolution is 'resolved'.
    """
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_SINGLE
    result = await get_advance_automation("C58ZA", "999001")
    data = json.loads(result)
    assert data["port_resolution"] == "resolved"
    assert len(data["governed_ports"]) == 1
    assert data["governed_ports"][0]["port"] == 3
    assert data["governed_ports"][0]["port_name"] == "Port 3"


async def test_get_advance_automation_governed_ports_missing_port_name(mock_client):
    """Port in bitmask with no portName falls back to 'Port N' not '(unnamed)'.

    MOCK_ADVANCE_AUTOMATIONS_LIST "Moderate Airflow" has grouptDevType=8 (Port 4).
    We add port 4 to the device without a portName — the fallback label is 'Port 4'.
    """
    import copy
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    # Add port 4 without portName to test the fallback label path.
    device["deviceInfo"]["ports"].append(
        {"port": 4, "speak": 0, "portsLoad": 0, "loadState": 0, "curMode": 15}
    )
    mock_client.get_devices.return_value = [device]
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await get_advance_automation("C58ZA", "1342758")
    data = json.loads(result)
    assert data["port_resolution"] == "resolved"
    port_nums = [gp["port"] for gp in data["governed_ports"]]
    assert 4 in port_nums
    # Port 4 has no portName → label is 'Port 4'
    port4_entry = next(gp for gp in data["governed_ports"] if gp["port"] == 4)
    assert port4_entry["port_name"] == "Port 4"


async def test_get_advance_automation_port_resolution_multiple_automations_bitmask(mock_client):
    """Two active automations → governed_ports decoded from each automation's bitmask.

    Auto A (advId=1) has grouptDevType=4 (Port 3); Auto B (advId=2) has grouptDevType=8
    (Port 4).  Each automation correctly reports only its own port.
    port_resolution='resolved' in both cases.
    """
    two_active = [
        {
            "advId": 1, "advName": "Auto A", "isOn": 1, "onSpeed": 5, "offSpeed": 0,
            "grouptDevType": 4, "advKey": "1-0", "runState": 1, "beginTime": 255, "endTime": 255,
        },
        {
            "advId": 2, "advName": "Auto B", "isOn": 1, "onSpeed": 3, "offSpeed": 0,
            "grouptDevType": 8, "advKey": "2-0", "runState": 1, "beginTime": 255, "endTime": 255,
        },
    ]
    mock_client.get_advance_automations.return_value = two_active
    result_a = await get_advance_automation("C58ZA", "1")
    data_a = json.loads(result_a)
    assert data_a["port_resolution"] == "resolved"
    assert len(data_a["governed_ports"]) == 1
    assert data_a["governed_ports"][0]["port"] == 3

    result_b = await get_advance_automation("C58ZA", "2")
    data_b = json.loads(result_b)
    assert data_b["port_resolution"] == "resolved"
    assert len(data_b["governed_ports"]) == 1
    assert data_b["governed_ports"][0]["port"] == 4


async def test_get_advance_automation_port_resolution_error(mock_client):
    """Malformed deviceInfo.ports → port_name_map empty (swallowed), governed_ports still decoded.

    When device ports is not iterable as dicts, port_name_map stays empty and
    port_names fall back to 'Port N'. governed_ports are still resolved from
    the automation bitmasks (which come from the automation record, not device.ports),
    so port_resolution stays 'resolved'.
    """
    import copy
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["deviceInfo"]["ports"] = "not-a-list"
    mock_client.get_devices.return_value = [device]
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await get_advance_automation("C58ZA", "1342758")
    data = json.loads(result)
    # governed_ports still decoded from bitmask (not from device.ports)
    assert data["port_resolution"] == "resolved"
    port_nums = [gp["port"] for gp in data["governed_ports"]]
    assert sorted(port_nums) == [4, 5, 6]


async def test_get_advance_automation_found_has_port_resolution_fields(mock_client):
    """get_advance_automation response always includes governed_ports and port_resolution."""
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await get_advance_automation("C58ZA", "1342758")
    data = json.loads(result)
    assert "governed_ports" in data
    assert "port_resolution" in data
    assert "device_type" in data["port_groups"][0]


async def test_get_advance_automation_human_summary_multi_group_no_raw_terms(mock_client):
    """Multi-group automation: human_summary uses plain language, not 'port_groups'."""
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await get_advance_automation("C58ZA", "1342758")
    data = json.loads(result)
    assert "port_groups" not in data["human_summary"]
    assert "Moderate Airflow" in data["human_summary"]


# ============ Issues #149 #150 #152 — bitmask helpers and conflict fix ============


def test_find_governing_automation_returns_automation_when_port_in_bitmask():
    """_find_governing_automation returns the enabled automation whose bitmask covers the port."""
    automations = _group_automations([
        {"advId": 10, "advName": "Auto A", "isOn": 1, "runState": 1, "onSpeed": 5,
         "offSpeed": 0, "grouptDevType": 8, "beginTime": 255, "endTime": 255},
    ])
    # grouptDevType=8 = bit 3 = Port 4
    result = _find_governing_automation(automations, 4)
    assert result is not None
    assert result["name"] == "Auto A"


def test_find_governing_automation_returns_none_when_port_not_in_bitmask():
    """_find_governing_automation returns None when port is not in any automation bitmask."""
    automations = _group_automations([
        {"advId": 10, "advName": "Auto A", "isOn": 1, "runState": 1, "onSpeed": 5,
         "offSpeed": 0, "grouptDevType": 8, "beginTime": 255, "endTime": 255},
    ])
    # grouptDevType=8 covers Port 4 only; Port 1 (bit 0) is not covered
    result = _find_governing_automation(automations, 1)
    assert result is None


def test_find_governing_automation_returns_none_when_automation_disabled():
    """_find_governing_automation returns None when port IS in bitmask but automation disabled."""
    automations = _group_automations([
        {"advId": 10, "advName": "Auto A", "isOn": 0, "runState": 0, "onSpeed": 5,
         "offSpeed": 0, "grouptDevType": 8, "beginTime": 255, "endTime": 255},
    ])
    result = _find_governing_automation(automations, 4)
    assert result is None


def test_find_governing_automation_returns_first_match_when_multiple_cover_port():
    """_find_governing_automation returns the first automation whose bitmask covers the port."""
    automations = _group_automations([
        {"advId": 1, "advName": "First", "isOn": 1, "runState": 1, "onSpeed": 3,
         "offSpeed": 0, "grouptDevType": 4, "beginTime": 255, "endTime": 255},
        {"advId": 2, "advName": "Second", "isOn": 1, "runState": 1, "onSpeed": 7,
         "offSpeed": 0, "grouptDevType": 4, "beginTime": 255, "endTime": 255},
    ])
    # Both cover Port 3 (bit 2 = 4); first match wins
    result = _find_governing_automation(automations, 3)
    assert result is not None
    assert result["name"] == "First"


def test_find_governing_port_group_returns_group_when_port_in_bitmask():
    """_find_governing_port_group returns the port_group entry covering the port."""
    automation = {
        "port_groups": [
            {"adv_id": 1, "on_speed": 5, "grp_dev_type": 8},   # Port 4
            {"adv_id": 2, "on_speed": 2, "grp_dev_type": 48},  # Ports 5 and 6
        ]
    }
    pg = _find_governing_port_group(automation, 4)
    assert pg is not None
    assert pg["on_speed"] == 5


def test_find_governing_port_group_returns_none_when_port_not_in_bitmask():
    """_find_governing_port_group returns None when port has no matching bitmask entry."""
    automation = {
        "port_groups": [
            {"adv_id": 1, "on_speed": 5, "grp_dev_type": 8},   # Port 4 only
        ]
    }
    pg = _find_governing_port_group(automation, 1)
    assert pg is None


async def test_conflict_response_sub_path_a_break_out_offered(mock_client):
    """Sub-path A (port in bitmask): 1_break_out offered and speed from matched port_group.

    Port 4 maps to grouptDevType=8 (advId=2179295, on_speed=1 in MOCK_ADVANCE_AUTOMATIONS_LIST).
    The conflict response should read speed=1, not speed=2 from port_groups[0].
    """
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await set_port_off("C58ZA", port=4, dry_run=False)
    data = json.loads(result)
    assert data.get("conflict") == "ADVANCE_AUTOMATION"
    assert "1_break_out" in data["options"]
    assert data["options"]["1_break_out"]["_tool"] == "break_out_of_automation"
    assert data["options"]["1_break_out"]["available"] is True
    # Speed must come from port_groups[1] (grp_dev_type=8, on_speed=1), not port_groups[0]
    assert "1" in data["human_summary"]


async def test_conflict_response_sub_path_a_speed_from_matched_port_group(mock_client):
    """Sub-path A reads current_auto_speed from the bitmask-matched port_group, not port_groups[0].

    Port 4 is in port_groups[1] (grouptDevType=8, on_speed=1).
    Port_groups[0] has on_speed=2 (for ports 5&6, grouptDevType=48).
    The human_summary must show speed 1, not speed 2.
    """
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await set_port_off("C58ZA", port=4, dry_run=False)
    data = json.loads(result)
    # human_summary contains "target speed 1" from port_groups[1], not "target speed 2"
    assert "target speed 1" in data["human_summary"]
    assert "target speed 2" not in data["human_summary"]


async def test_conflict_response_sub_path_b_controller_wide_lock(mock_client):
    """Sub-path B (port NOT in bitmask): controller-wide lock language, no 1_break_out.

    Port 1 (bit 0) is not covered by MOCK_ADVANCE_AUTOMATIONS_LIST (bits 3,4,5).
    The response should describe a controller-wide lock and NOT offer 1_break_out.
    """
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await set_port_off("C58ZA", port=1, dry_run=False)
    data = json.loads(result)
    assert data.get("conflict") == "ADVANCE_AUTOMATION"
    assert "1_break_out" not in data["options"]
    assert "1_disable_automation" in data["options"]
    assert "controller" in data["human_summary"].lower()
    assert "Moderate Airflow" in data["human_summary"]
    # active_automations is still populated even in Sub-path B
    assert isinstance(data["active_automations"], list)
    assert len(data["active_automations"]) > 0


async def test_get_advance_automation_bitmask_multi_automation(mock_client):
    """Multi-automation scenario: governed_ports decoded from bitmasks, not isOpenAutomation.

    MOCK_ADVANCE_AUTOMATIONS_LIST "Moderate Airflow" covers ports 4, 5, 6.
    port_resolution must be 'resolved'.
    """
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await get_advance_automation("C58ZA", "1342758")
    data = json.loads(result)
    assert data["port_resolution"] == "resolved"
    port_nums = sorted(gp["port"] for gp in data["governed_ports"])
    assert port_nums == [4, 5, 6]


async def test_get_advance_automation_bitmask_decode_fallback_port_name(mock_client):
    """Port in bitmask but not in deviceInfo.ports → falls back to 'Port N'."""
    # MOCK_DEVICE_LEGACY has only ports 1 and 2; Moderate Airflow covers 4, 5, 6.
    # All three should fall back to 'Port N'.
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_LIST
    result = await get_advance_automation("C58ZA", "1342758")
    data = json.loads(result)
    assert data["port_resolution"] == "resolved"
    for gp in data["governed_ports"]:
        pnum = gp["port"]
        assert gp["port_name"] == f"Port {pnum}"


@pytest.mark.asyncio
async def test_get_advance_automation_governed_ports_default_name_no_redundancy(mock_client):
    """governed_ports uses plain 'Port N' for API-default-named ports, not 'Port N (Port N)'."""
    import copy
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["deviceInfo"]["ports"].append(
        {"port": 3, "portName": "Port 3", "speak": 0, "portsLoad": 0,
         "loadState": 0, "curMode": 1, "remainTime": None}
    )
    mock_client.get_devices.return_value = [device]
    mock_client.get_advance_automations.return_value = MOCK_ADVANCE_AUTOMATIONS_SINGLE
    result = await get_advance_automation("C58ZA", "999001")
    data = json.loads(result)
    assert data["port_resolution"] == "resolved"
    assert len(data["governed_ports"]) == 1
    assert data["governed_ports"][0]["port"] == 3
    assert data["governed_ports"][0]["port_name"] == "Port 3"


async def test_get_port_settings_advance_speed_from_matched_port_group(mock_client):
    """get_port_settings ADVANCE enrichment: automation_on_speed from bitmask-matched group.

    Port 4 (bit 3) maps to grouptDevType=8 (on_speed=1 in MOCK_ADVANCE_AUTOMATIONS_LIST).
    automation_on_speed must be 1, not 2 (which is port_groups[0].on_speed for ports 5&6).
    """
    import copy
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["deviceInfo"]["ports"].append(
        {"port": 4, "portName": "Inline Fan", "speak": 1, "portsLoad": 1,
         "loadState": 1, "curMode": 15, "remainTime": 0}
    )
    mock_client.get_devices.return_value = [device]
    mock_client.get_mode_settings.return_value = {**MOCK_MODE_SETTINGS_BASIC, "modeType": 15}
    result = await get_port_settings("C58ZA", 4)
    data = json.loads(result)
    assert data["mode"] == "ADVANCE"
    assert data["automation_name"] == "Moderate Airflow"
    # Port 4 maps to port_groups[1] (grouptDevType=8, on_speed=1) not port_groups[0] (on_speed=2)
    assert data["automation_on_speed"] == 1


# ============ Issue #62 — create_advance_automation port parameter ============

async def test_create_advance_automation_port_dry_run(mock_client):
    """dry_run=True with valid port → response includes port, port_name, and note."""
    result = await create_advance_automation(
        "C58ZA", "Night Cycle", on_speed=3, port=2, dry_run=True
    )
    data = json.loads(result)
    assert data["dry_run"] is True
    assert data["sent"] is False
    assert data["port"] == 2
    assert data["port_name"] == "Exhaust Fan"
    assert "note" in data
    assert "Preview only" in data["note"]
    assert data["begin_time"] == "continuous"   # #287: no window → continuous default
    assert data["end_time"] == "continuous"


async def test_create_advance_automation_port_zero_error(mock_client):
    """dry_run=True with port=0 → port validation error."""
    result = await create_advance_automation(
        "C58ZA", "Night Cycle", on_speed=3, port=0, dry_run=True
    )
    data = json.loads(result)
    assert "error" in data
    assert "port" in data["error"]


async def test_create_advance_automation_port_not_found_error(mock_client):
    """dry_run=True with port in 1–8 range but not on device → enriched error."""
    result = await create_advance_automation(
        "C58ZA", "Night Cycle", on_speed=3, port=5, dry_run=True
    )
    data = json.loads(result)
    assert "error" in data
    assert "not found" in data["error"]
    assert "available_ports" in data
    assert "suggested_reply" in data


async def test_create_advance_automation_port_not_found_suggested_reply_content(mock_client):
    """port not on device → suggested_reply references the missing port number."""
    result = await create_advance_automation(
        "C58ZA", "Night Cycle", on_speed=3, port=5, dry_run=True
    )
    data = json.loads(result)
    assert "Port 5" in data["suggested_reply"]


async def test_create_advance_automation_port_not_found_available_ports_contents(mock_client):
    """port not on device → available_ports lists ports 1-2 from MOCK_DEVICE_LEGACY."""
    result = await create_advance_automation(
        "C58ZA", "Night Cycle", on_speed=3, port=5, dry_run=True
    )
    data = json.loads(result)
    ports = data["available_ports"]
    assert isinstance(ports, list)
    assert ports[0]["port"] == 1
    assert ports[0]["name"] == "Intake Fan"
    assert ports[1]["port"] == 2
    assert ports[1]["name"] == "Exhaust Fan"


async def test_create_advance_automation_port_not_found_sanitized_port_name(mock_client):
    """portName with control char → stripped in available_ports."""
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["deviceInfo"]["ports"][0]["portName"] = "Bad\x00Name"
    mock_client.get_devices.return_value = [device]
    result = await create_advance_automation(
        "C58ZA", "Night Cycle", on_speed=3, port=5, dry_run=True
    )
    data = json.loads(result)
    assert data["available_ports"][0]["name"] == "BadName"


async def test_create_advance_automation_port_not_found_all_control_char_portname(mock_client):
    """portName that is entirely control chars → sanitizer returns '(unnamed)'."""
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["deviceInfo"]["ports"][0]["portName"] = "\x00\x01"
    mock_client.get_devices.return_value = [device]
    result = await create_advance_automation(
        "C58ZA", "Night Cycle", on_speed=3, port=5, dry_run=True
    )
    data = json.loads(result)
    assert data["available_ports"][0]["name"] == "(unnamed)"


async def test_create_advance_automation_port_not_found_no_portname_fallback(mock_client):
    """portName absent → available_ports uses 'Port N' fallback."""
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["deviceInfo"]["ports"][0].pop("portName", None)
    mock_client.get_devices.return_value = [device]
    result = await create_advance_automation(
        "C58ZA", "Night Cycle", on_speed=3, port=5, dry_run=True
    )
    data = json.loads(result)
    assert data["available_ports"][0]["name"] == "Port 1"


async def test_create_advance_automation_port_not_found_empty_ports_list(mock_client):
    """Device with no ports in deviceInfo → available_ports is empty list."""
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["deviceInfo"]["ports"] = []
    mock_client.get_devices.return_value = [device]
    result = await create_advance_automation(
        "C58ZA", "Night Cycle", on_speed=3, port=5, dry_run=True
    )
    data = json.loads(result)
    assert data["available_ports"] == []


# ============ Issue #71 — create_advance_automation live creation ============


async def test_create_advance_automation_live_port4(mock_client):
    """dry_run=False, port=4 → grouptDevType=8 in payload, sent=True, automation_id as string."""
    import copy
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["deviceInfo"]["ports"].append({"port": 4, "portName": "Clip Fan"})
    mock_client.get_devices.return_value = [device]
    mock_client.create_advance_automation.return_value = {"advId": 2302819}
    result = await create_advance_automation(
        "C58ZA", "Test Auto", on_speed=5, port=4, dry_run=False
    )
    data = json.loads(result)
    assert data["sent"] is True
    assert data["automation_id"] == "2302819"
    assert mock_client.create_advance_automation.call_count == 1
    _, payload = mock_client.create_advance_automation.call_args[0]
    assert payload["grouptDevType"] == 8
    assert payload["advName"] == "Test Auto"
    assert payload["onSpeed"] == 5
    # New program: isFlag=1, server assigns the slot (subNumber=0). Issue #284.
    assert payload["isFlag"] == 1
    assert payload["subNumber"] == 0


async def test_create_advance_automation_live_port1(mock_client):
    """port=1 → grouptDevType=1 (2^0)."""
    mock_client.create_advance_automation.return_value = {"advId": 1111}
    result = await create_advance_automation(
        "C58ZA", "Test Auto", on_speed=3, port=1, dry_run=False
    )
    data = json.loads(result)
    assert data["sent"] is True
    assert mock_client.create_advance_automation.call_count == 1
    _, payload = mock_client.create_advance_automation.call_args[0]
    assert payload["grouptDevType"] == 1


async def test_create_advance_automation_live_port8(mock_client):
    """port=8 → grouptDevType=128 (2^7)."""
    import copy
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["deviceInfo"]["ports"].append({"port": 8, "portName": "Port 8"})
    mock_client.get_devices.return_value = [device]
    mock_client.create_advance_automation.return_value = {"advId": 9999}
    result = await create_advance_automation(
        "C58ZA", "Test Auto", on_speed=7, port=8, dry_run=False
    )
    data = json.loads(result)
    assert data["sent"] is True
    _, payload = mock_client.create_advance_automation.call_args[0]
    assert payload["grouptDevType"] == 128


async def test_create_advance_automation_live_port_too_high(mock_client):
    """port=9 → error before any API call (at most 8 ports), with suggested_reply."""
    result = await create_advance_automation(
        "C58ZA", "Test Auto", on_speed=5, port=9, dry_run=False
    )
    data = json.loads(result)
    assert "error" in data
    assert "8 ports" in data["error"]
    assert "suggested_reply" in data
    mock_client.get_devices.assert_not_called()
    mock_client.create_advance_automation.assert_not_called()


async def test_create_advance_automation_live_port_zero_error(mock_client):
    """port=0, dry_run=False → port error before any API call."""
    result = await create_advance_automation(
        "C58ZA", "Test Auto", on_speed=5, port=0, dry_run=False
    )
    data = json.loads(result)
    assert "error" in data
    assert "port" in data["error"]
    mock_client.get_devices.assert_not_called()


async def test_create_advance_automation_live_no_schedule(mock_client):
    """begin_time=255, end_time=255 → Always active; payload uses 0/1439 full-day range."""
    mock_client.create_advance_automation.return_value = {"advId": 5555}
    result = await create_advance_automation(
        "C58ZA", "Always On", on_speed=4, port=1,
        begin_time=255, end_time=255, dry_run=False
    )
    data = json.loads(result)
    assert data["sent"] is True
    assert data["schedule_summary"] == "Always active"
    assert data["begin_time"] is None
    assert data["end_time"] is None
    _, payload = mock_client.create_advance_automation.call_args[0]
    # Sentinel 255 maps to valid full-day range; raw 255 is rejected by the API.
    assert payload["beginTime"] == 0
    assert payload["endTime"] == 1439
    assert payload["switchTime"] == 127


async def test_create_advance_automation_live_adv_id_mapping(mock_client):
    """Server returns advId=2302819 → automation_id='2302819' (string)."""
    mock_client.create_advance_automation.return_value = {"advId": 2302819}
    result = await create_advance_automation(
        "C58ZA", "Test", on_speed=5, port=1, dry_run=False
    )
    data = json.loads(result)
    assert data["automation_id"] == "2302819"
    assert isinstance(data["automation_id"], str)


async def test_create_advance_automation_min_speed_from_port_settings(mock_client):
    """min_speed in response comes from port's offSpead setting, not off_speed param."""
    mock_client.get_mode_settings.return_value = {"offSpead": 3}
    mock_client.create_advance_automation.return_value = {"advId": 9999}
    result = await create_advance_automation(
        "C58ZA", "Test", on_speed=7, off_speed=5, port=1, dry_run=False
    )
    data = json.loads(result)
    assert data["min_speed"] == 3
    assert data["sent"] is True


async def test_create_advance_automation_dry_run_includes_min_speed(mock_client):
    """Dry run response includes min_speed from port settings."""
    mock_client.get_mode_settings.return_value = {"offSpead": 2}
    result = await create_advance_automation(
        "C58ZA", "Test", on_speed=7, port=1, dry_run=True
    )
    data = json.loads(result)
    assert data["dry_run"] is True
    assert data["min_speed"] == 2


async def test_create_advance_automation_live_missing_adv_id(mock_client):
    """Server returns no advId → structured error, not None in output."""
    mock_client.create_advance_automation.return_value = {}
    result = await create_advance_automation(
        "C58ZA", "Test", on_speed=5, port=1, dry_run=False
    )
    data = json.loads(result)
    assert "error" in data
    assert "detail" in data
    assert data.get("automation_id") is None


async def test_create_advance_automation_live_api_error(mock_client, caplog):
    """ACInfinityAPIError → {"error": "API error", "detail": "see server logs"}."""
    import logging
    mock_client.create_advance_automation.side_effect = ACInfinityAPIError("boom")
    with caplog.at_level(logging.ERROR, logger="ac_infinity_mcp.server"):
        result = await create_advance_automation(
            "C58ZA", "Test", on_speed=5, port=1, dry_run=False
        )
    data = json.loads(result)
    assert data["error"] == "API error"
    assert data["detail"] == "see server logs"
    assert any(r.levelname == "ERROR" and "api" in r.message.lower() for r in caplog.records)


async def test_create_advance_automation_live_auth_error(mock_client, caplog):
    """ACInfinityAuthError → auth error JSON + warning log."""
    import logging
    mock_client.create_advance_automation.side_effect = ACInfinityAuthError("auth")
    with caplog.at_level(logging.WARNING, logger="ac_infinity_mcp.server"):
        result = await create_advance_automation(
            "C58ZA", "Test", on_speed=5, port=1, dry_run=False
        )
    data = json.loads(result)
    assert "Authentication failed — check AC_INFINITY_EMAIL" in data["error"]
    assert data["detail"] == "see server logs"
    assert any(r.levelname == "WARNING" and "auth" in r.message.lower() for r in caplog.records)


async def test_create_advance_automation_dry_run_note_grower_facing(mock_client):
    """dry_run=True → note contains 'Preview only', NOT 'AC Infinity app'."""
    result = await create_advance_automation(
        "C58ZA", "Test", on_speed=5, port=1, dry_run=True
    )
    data = json.loads(result)
    assert "Preview only" in data["note"]
    assert "AC Infinity app" not in data["note"]
    mock_client.create_advance_automation.assert_not_called()


async def test_create_advance_automation_dry_run_schedule_summary(mock_client):
    """begin_time=540, end_time=1020 → schedule_summary='Active 9:00 AM – 5:00 PM'."""
    result = await create_advance_automation(
        "C58ZA", "Test", on_speed=5, port=1,
        begin_time=540, end_time=1020, dry_run=True
    )
    data = json.loads(result)
    assert data["schedule_summary"] == "Active 9:00 AM – 5:00 PM"
    mock_client.create_advance_automation.assert_not_called()


async def test_create_advance_automation_off_speed_is_min_level(mock_client):
    """off_speed is the minimum fan level (Rev-4): it maps to offSpeed on the payload."""
    mock_client.create_advance_automation.return_value = {"advId": 1234}
    result = await create_advance_automation(
        "C58ZA", "Test", on_speed=7, off_speed=5, port=1, dry_run=False
    )
    data = json.loads(result)
    assert data["sent"] is True
    _, payload = mock_client.create_advance_automation.call_args[0]
    assert payload["offSpeed"] == 5
    assert payload["switchTime"] == 255   # #287: no window → continuous (switchTime 255)


async def test_create_advance_automation_mixed_255_sentinel_rejected(mock_client):
    """begin_time=255 but end_time=600 → error (must both be 255 or both be 0-1439)."""
    result = await create_advance_automation(
        "C58ZA", "Test", on_speed=5, port=1,
        begin_time=255, end_time=600, dry_run=True
    )
    data = json.loads(result)
    assert "error" in data
    mock_client.create_advance_automation.assert_not_called()


async def test_create_advance_automation_off_speed_out_of_range(mock_client):
    """off_speed=11 → validation error before any API call."""
    result = await create_advance_automation(
        "C58ZA", "Test", on_speed=5, off_speed=11, port=1, dry_run=True
    )
    data = json.loads(result)
    assert "error" in data
    assert "off_speed" in data["error"]
    mock_client.get_devices.assert_not_called()


async def test_create_advance_automation_off_speed_negative(mock_client):
    """off_speed=-1 → validation error before any API call."""
    result = await create_advance_automation(
        "C58ZA", "Test", on_speed=5, off_speed=-1, port=1, dry_run=True
    )
    data = json.loads(result)
    assert "error" in data
    assert "off_speed" in data["error"]
    mock_client.get_devices.assert_not_called()


async def test_create_advance_automation_begin_time_out_of_range(mock_client):
    """begin_time=1500 → validation error before any API call."""
    result = await create_advance_automation(
        "C58ZA", "Test", on_speed=5, port=1,
        begin_time=1500, end_time=1020, dry_run=True
    )
    data = json.loads(result)
    assert "error" in data
    assert "begin_time" in data["error"]
    mock_client.get_devices.assert_not_called()


async def test_create_advance_automation_end_time_out_of_range(mock_client):
    """end_time=1500 → validation error before any API call."""
    result = await create_advance_automation(
        "C58ZA", "Test", on_speed=5, port=1,
        begin_time=0, end_time=1500, dry_run=True
    )
    data = json.loads(result)
    assert "error" in data
    assert "end_time" in data["error"]
    mock_client.get_devices.assert_not_called()


async def test_create_advance_automation_device_not_found(mock_client):
    """device_id not in devices list → structured error."""
    result = await create_advance_automation(
        "UNKNOWN_DEVICE", "Test", on_speed=5, port=1, dry_run=False
    )
    data = json.loads(result)
    assert "error" in data
    assert "UNKNOWN_DEVICE" in data["error"]
    mock_client.create_advance_automation.assert_not_called()


async def test_create_advance_automation_device_missing_dev_id(mock_client):
    """Device found but devId is absent → structured error."""
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device.pop("devId", None)
    mock_client.get_devices.return_value = [device]
    result = await create_advance_automation(
        "C58ZA", "Test", on_speed=5, port=1, dry_run=False
    )
    data = json.loads(result)
    assert "error" in data
    assert "devId" in data["error"] or "missing" in data["error"]
    mock_client.create_advance_automation.assert_not_called()


async def test_create_advance_automation_all_control_char_name(mock_client):
    """Name containing only control chars sanitises to '(unnamed)' → rejected."""
    result = await create_advance_automation(
        "C58ZA", "\x00\x01\x02", on_speed=5, port=1, dry_run=True
    )
    data = json.loads(result)
    assert "error" in data
    assert "empty" in data["error"]
    mock_client.get_devices.assert_not_called()


async def test_create_advance_automation_device_error(mock_client):
    """ACInfinityDeviceError from get_devices → error with str(e)."""
    mock_client.get_devices.side_effect = ACInfinityDeviceError("device offline")
    result = await create_advance_automation(
        "C58ZA", "Test", on_speed=5, port=1, dry_run=False
    )
    data = json.loads(result)
    assert "device offline" in data["error"]


async def test_create_advance_automation_unexpected_exception(mock_client):
    """Bare Exception from get_devices → generic error with detail."""
    mock_client.get_devices.side_effect = RuntimeError("unexpected boom")
    result = await create_advance_automation(
        "C58ZA", "Test", on_speed=5, port=1, dry_run=False
    )
    data = json.loads(result)
    assert data["error"] == "Unexpected error"
    assert data["detail"] == "see server logs"


async def test_create_advance_automation_live_missing_adv_id_automation_is_active(mock_client):
    """No advId in response → error clarifies automation was created and is active."""
    mock_client.create_advance_automation.return_value = {}
    result = await create_advance_automation(
        "C58ZA", "Night Mode", on_speed=5, port=1, dry_run=False
    )
    data = json.loads(result)
    assert "error" in data
    assert "Night Mode" in data["error"]
    assert "active" in data["error"]
    assert "detail" in data


async def test_create_advance_automation_live_automation_id_note_present(mock_client):
    """Live success response includes automation_id_note to guide Claude away from surfacing ID."""
    mock_client.create_advance_automation.return_value = {"advId": 9999}
    result = await create_advance_automation(
        "C58ZA", "Test", on_speed=5, port=1, dry_run=False
    )
    data = json.loads(result)
    assert data["sent"] is True
    assert "automation_id_note" in data
    assert "name" in data["automation_id_note"]


# ============ get_port_activity_report — #112 #136 #139 fixes ============

async def test_activity_report_devtype22_note_emitted_when_all_api_constant_speed(mock_client):
    """#136: Note appears for devType=22 even when all ports have api_constant_speed.

    Old code: no_load_signal_ports=[] when all ports are toggle → Note suppressed (bug).
    New code: dev_type in _ZERO_LOAD_DEV_TYPES and result → Note always emitted.
    """
    device = _make_devtype22_device([
        {"port": 2, "portName": "Heater", "portsLoad": None, "loadType": 4, "speak": 1},
        {"port": 3, "portName": "Humidifer", "portsLoad": None, "loadType": 4, "speak": 0},
    ])
    mock_client.get_devices.return_value = [device]
    # Toggle readings: 100% uptime, speed=1 throughout — triggers api_constant_speed on both ports.
    readings = _make_toggle_port_readings(24, port_num=2, name="Heater")
    mock_client.get_historical_data.return_value = [{}] * 24
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        # Alternate between port 2 and port 3 readings to give both identical constant-speed data
        port = 2 if idx % 2 == 0 else 3
        name = "Heater" if port == 2 else "Humidifer"
        ts_base = readings[idx // 2 % len(readings)]["timestamp"]
        idx += 1
        return {
            "timestamp": ts_base,
            "temperature_c": 22.0, "humidity": 55.0, "vpd": 1.2,
            "ports": [{"port": port, "name": name, "speed": 1, "on": True}],
        }

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)

    assert "does not report power draw" in data["human_summary"], (
        "#136: Note must appear for devType=22 even when all ports are api_constant_speed"
    )


async def test_activity_report_zero_load_note_suppressed_when_no_result(mock_client):
    """#136: Note is NOT emitted for devType=22 when result is empty (all ports filtered)."""
    # devType=22, auto-named port always off → Rule B filters it → result=[]
    device = _make_devtype22_device([
        {"port": 1, "portName": "Port 1", "portsLoad": None, "loadType": 0, "speak": 0},
    ])
    mock_client.get_devices.return_value = [device]
    from datetime import datetime, timedelta
    base = datetime(2024, 4, 18, 0, 0, 0)
    # Auto-named port "Port 1", always off — Rule B filters via low-activity check
    readings_off = [
        {
            "timestamp": (base + timedelta(hours=i)).isoformat() + "Z",
            "temperature_c": 22.0, "humidity": 55.0, "vpd": 1.2,
            "ports": [{"port": 1, "name": "Port 1", "speed": 0, "on": False}],
        }
        for i in range(24)
    ]
    mock_client.get_historical_data.return_value = [{}] * 24
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = readings_off[idx % len(readings_off)]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)

    assert len(data["ports"]) == 0
    assert "does not report power draw" not in data["human_summary"], (
        "Note must not appear when result is empty (no active ports)"
    )


def _make_multi_port_readings(port_configs: list[dict], total: int) -> list[dict]:
    """Build multi-port readings. port_configs: [{port, name, on_hours, speed}].

    Each port is on only during its first `on_hours` readings (single-block), all same timestamp.
    """
    from datetime import datetime, timedelta
    base = datetime(2024, 4, 18, 0, 0, 0)
    result = []
    for i in range(total):
        ports = []
        for cfg in port_configs:
            on = i < cfg.get("on_readings", 0)
            ports.append({
                "port": cfg["port"],
                "name": cfg["name"],
                "speed": cfg.get("speed", 5) if on else 0,
                "on": on,
            })
        result.append({
            "timestamp": (base + timedelta(hours=i)).isoformat() + "Z",
            "temperature_c": 22.0, "humidity": 55.0, "vpd": 1.2,
            "ports": ports,
        })
    return result


async def test_activity_report_rule_f_excludes_phantom_clones_end_to_end(mock_client):
    """#139: Rule F excludes phantom-clone custom-named ports on devType=11 (C58ZA scenario).

    4-port fixture:
      Port 1 'Port 1' (auto-named, always off) → Rule B
      Port 2 'Heater' (low activity, same signature as Port 3) → Rule F
      Port 3 'Humidifer' (low activity, same signature as Port 2) → Rule F
      Port 4 'Filter' (real activity) → kept
    Expected: 1 active port, ports_excluded_count=3.
    """
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)  # devType=11
    device["devPortCount"] = 4
    device["deviceInfo"]["ports"] = [
        {"port": 1, "portName": "Port 1", "portsLoad": 0, "loadType": 0, "speak": 0},
        {"port": 2, "portName": "Heater", "portsLoad": 1, "loadType": 4, "speak": 1},
        {"port": 3, "portName": "Humidifer", "portsLoad": 1, "loadType": 4, "speak": 0},
        {"port": 4, "portName": "Filter", "portsLoad": 5, "loadType": 0, "speak": 5},
    ]
    mock_client.get_devices.return_value = [device]
    # 50 readings: Heater/Humidifer on only for reading 0 (< 1h/day sub-threshold)
    # Filter on for all 50 readings (real activity).
    port_configs = [
        {"port": 1, "name": "Port 1", "speed": 0, "on_readings": 0},
        {"port": 2, "name": "Heater", "speed": 1, "on_readings": 1},
        {"port": 3, "name": "Humidifer", "speed": 1, "on_readings": 1},
        {"port": 4, "name": "Filter", "speed": 5, "on_readings": 50},
    ]
    readings = _make_multi_port_readings(port_configs, total=50)
    mock_client.get_historical_data.return_value = [{}] * 50
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = readings[idx % len(readings)]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)

    port_names = [p["name"] for p in data["ports"]]
    assert "Filter" in port_names, "#139: Filter (real activity) must appear"
    assert "Heater" not in port_names, "#139: Heater (phantom clone) must be excluded"
    assert "Humidifer" not in port_names, "#139: Humidifer (phantom clone) must be excluded"
    assert data["ports_excluded_count"] == 3, (
        "#139: 3 ports excluded (Port 1 by Rule B, Heater/Humidifer by Rule F)"
    )


async def test_activity_report_transitions_debounced_end_to_end(mock_client):
    """#112: Boundary nibble (single-reading blip) is not counted as a transition.

    Sequence with a 1-reading ON nibble between sustained OFF runs: raw count=4, debounced=2.
    """
    from datetime import datetime, timedelta
    base = datetime(2024, 4, 18, 0, 0, 0)
    # ON nibble at index 3 (1 reading), then sustained ON at indices 5-7 (3 readings),
    # then OFF at indices 8-9 (2 readings). All other readings are OFF.
    # Debounced: 2 transitions (OFF→ON sustained, ON→OFF sustained); nibble not counted.
    on_sequence = [False, False, False, True, False, True, True, True, False, False]
    readings = [
        {
            "timestamp": (base + timedelta(hours=i)).isoformat() + "Z",
            "temperature_c": 22.0, "humidity": 55.0, "vpd": 1.2,
            "ports": [{"port": 1, "name": "Fan", "speed": 5 if on else 0, "on": on}],
        }
        for i, on in enumerate(on_sequence)
    ]
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)  # devType=11
    device["deviceInfo"]["ports"] = [
        {"port": 1, "portName": "Fan", "portsLoad": 5, "loadType": 0, "speak": 5},
    ]
    mock_client.get_devices.return_value = [device]
    mock_client.get_historical_data.return_value = [{}] * 10
    idx = 0

    def _side_effect(r, port_names=None):
        nonlocal idx
        val = readings[idx % len(readings)]
        idx += 1
        return val

    mock_client.parse_history_record.side_effect = _side_effect
    result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)

    assert len(data["ports"]) == 1
    assert data["ports"][0]["transitions"] == 2, (
        "#112: Boundary nibble (1-reading blip) must not count as a transition; "
        f"expected 2, got {data['ports'][0]['transitions']}"
    )


# ============ _is_port_empty helper (issue #165) ============

def _make_port_data(
    port: int,
    name: str | None = None,
    ports_load: int = 0,
    port_resistance: int | None = None,
) -> dict:
    """Build a minimal port dict for _is_port_empty tests.

    Omitting ``port_resistance`` (the default) exercises the fallback path.
    Pass ``port_resistance=65535`` or a real value to exercise the primary path.
    """
    p: dict = {"port": port, "portsLoad": ports_load}
    if name is not None:
        p["portName"] = name
    if port_resistance is not None:
        p["portResistance"] = port_resistance
    return p


def _make_device(dev_type: int = 11) -> dict:
    return {"devType": dev_type, "deviceInfo": {"ports": []}}


def test_is_port_empty_default_name_zero_load():
    """Default-named port with portsLoad=0 → empty."""
    port_data = _make_port_data(7, name="Port 7", ports_load=0)
    device = _make_device(dev_type=11)
    assert _is_port_empty(port_data, 7, device) is True


def test_is_port_empty_default_name_nonzero_load():
    """Default-named port but portsLoad > 0 on standard device → NOT empty."""
    port_data = _make_port_data(2, name="Port 2", ports_load=5)
    device = _make_device(dev_type=11)
    assert _is_port_empty(port_data, 2, device) is False


def test_is_port_empty_custom_name_zero_load():
    """Custom-named port, even with portsLoad=0 → NOT empty (assumed connected)."""
    port_data = _make_port_data(1, name="Inline Fan", ports_load=0)
    device = _make_device(dev_type=11)
    assert _is_port_empty(port_data, 1, device) is False


def test_is_port_empty_custom_name_nonzero_load():
    """Custom-named port with load → NOT empty."""
    port_data = _make_port_data(3, name="Exhaust", ports_load=10)
    device = _make_device(dev_type=11)
    assert _is_port_empty(port_data, 3, device) is False


def test_is_port_empty_devtype18_default_name():
    """devType=18 (Willie's Tent): default-named port always → empty (portsLoad always 0)."""
    port_data = _make_port_data(7, name="Port 7", ports_load=0)
    device = _make_device(dev_type=18)
    assert _is_port_empty(port_data, 7, device) is True


def test_is_port_empty_devtype18_custom_name():
    """devType=18: custom-named port → NOT empty even though portsLoad is 0."""
    port_data = _make_port_data(4, name="Filter", ports_load=0)
    device = _make_device(dev_type=18)
    assert _is_port_empty(port_data, 4, device) is False


def test_is_port_empty_devtype22_default_name():
    """devType=22 (Q0KT4): default-named port with zero load → empty."""
    port_data = _make_port_data(3, name="Port 3", ports_load=0)
    device = _make_device(dev_type=22)
    assert _is_port_empty(port_data, 3, device) is True


def test_is_port_empty_devtype22_custom_name():
    """devType=22: custom-named port → NOT empty."""
    port_data = _make_port_data(2, name="Light", ports_load=0)
    device = _make_device(dev_type=22)
    assert _is_port_empty(port_data, 2, device) is False


def test_is_port_empty_none_port_data():
    """None port_data → False (safe default)."""
    device = _make_device()
    assert _is_port_empty(None, 5, device) is False


def test_is_port_empty_none_device():
    """None device → False (safe default)."""
    port_data = _make_port_data(5, name="Port 5", ports_load=0)
    assert _is_port_empty(port_data, 5, None) is False


def test_is_port_empty_no_portname_key():
    """Port dict without 'portName' key treated as default-named (None/absent)."""
    port_data = {"port": 6, "portsLoad": 0}  # portName key absent
    device = _make_device(dev_type=11)
    assert _is_port_empty(port_data, 6, device) is True


# ---------- Primary portResistance signal (Quirk 27) ----------


def test_is_port_empty_resistance_65535_custom_name_returns_true():
    """portResistance=65535 + custom name → True (the core #183 fix)."""
    port_data = _make_port_data(1, name="Humidifier", ports_load=0, port_resistance=65535)
    device = _make_device(dev_type=11)
    assert _is_port_empty(port_data, 1, device) is True


def test_is_port_empty_resistance_65535_default_name_returns_true():
    """portResistance=65535 + default name → True (primary signal, no fallback needed)."""
    port_data = _make_port_data(7, name="Port 7", ports_load=0, port_resistance=65535)
    device = _make_device(dev_type=11)
    assert _is_port_empty(port_data, 7, device) is True


def test_is_port_empty_resistance_non_65535_custom_name_returns_false():
    """portResistance=7500 (fan) + custom name → False (primary wins over name heuristic)."""
    port_data = _make_port_data(4, name="Filter", ports_load=0, port_resistance=7500)
    device = _make_device(dev_type=18)
    assert _is_port_empty(port_data, 4, device) is False


def test_is_port_empty_resistance_non_65535_default_name_zero_load_returns_false():
    """portResistance=7500 + default name + portsLoad=0 → False (primary signal wins)."""
    port_data = _make_port_data(7, name="Port 7", ports_load=0, port_resistance=7500)
    device = _make_device(dev_type=11)
    assert _is_port_empty(port_data, 7, device) is False


def test_is_port_empty_resistance_65535_devtype18_custom_name_returns_true():
    """portResistance=65535 + devType=18 + custom name → True (primary wins)."""
    port_data = _make_port_data(4, name="Filter", ports_load=0, port_resistance=65535)
    device = _make_device(dev_type=18)
    assert _is_port_empty(port_data, 4, device) is True


def test_is_port_empty_resistance_malformed_returns_false():
    """Malformed portResistance string → False (safe default, treat as connected)."""
    port_data = _make_port_data(3, name="Port 3", ports_load=0, port_resistance=None)
    port_data["portResistance"] = "N/A"
    device = _make_device(dev_type=11)
    assert _is_port_empty(port_data, 3, device) is False


def test_is_port_empty_resistance_zero_returns_false():
    """portResistance=0 is a real (shorted) reading, not open-circuit → False."""
    port_data = _make_port_data(2, name="Port 2", ports_load=0, port_resistance=0)
    device = _make_device(dev_type=11)
    assert _is_port_empty(port_data, 2, device) is False


# ---------- Fallback path (portResistance absent — old firmware) ----------


def test_is_port_empty_fallback_custom_name_no_resistance():
    """portResistance absent + custom name → fallback path → False (assumed connected).

    Old-firmware devices that omit portResistance still treat custom names as connected.
    """
    port_data = _make_port_data(1, name="Humidifier", ports_load=0)  # no port_resistance key
    device = _make_device(dev_type=11)
    assert _is_port_empty(port_data, 1, device) is False


def test_is_port_empty_fallback_default_name_zero_load():
    """portResistance absent + default name + portsLoad=0 → fallback path → True (empty).

    Old-firmware devices that omit portResistance still use the dual-signal heuristic.
    """
    port_data = _make_port_data(7, name="Port 7", ports_load=0)  # no port_resistance key
    device = _make_device(dev_type=11)
    assert _is_port_empty(port_data, 7, device) is True


def test_empty_port_advisory_message():
    """Merged function produces the canonical message text."""
    msg = _empty_port_advisory("Humidifier (Port 3)")
    assert "Humidifier (Port 3)" in msg
    assert "doesn't appear to have anything connected" in msg
    assert "If you meant a different port" in msg


def test_empty_port_advisory_does_not_accept_port_int():
    """Confirm signature is (port_label: str), not (port: int, port_label: str)."""
    import inspect
    sig = inspect.signature(_empty_port_advisory)
    assert list(sig.parameters.keys()) == ["port_label"]


def test_empty_port_advisory_text_no_dry_run():
    """Advisory text must not contain 'dry_run' or expose internal params."""
    msg = _empty_port_advisory("Port 7")
    assert "dry_run" not in msg
    assert "Port 7" in msg
    assert "connected" in msg


# ============ _get_device() helper (issue #201) ============

@pytest.mark.asyncio
async def test_get_device_found():
    import ac_infinity_mcp.server as srv
    mock_device = {"devCode": "ABC123", "devName": "Controller"}
    mock_client = MagicMock()
    mock_client.get_devices.return_value = [mock_device]
    srv.setup(mock_client)
    try:
        device, err = await _get_device("ABC123")
        assert device == mock_device
        assert err is None
    finally:
        srv._aci_client = None
        srv._invalidate_device_cache()


@pytest.mark.asyncio
async def test_get_device_not_found():
    import ac_infinity_mcp.server as srv
    mock_client = MagicMock()
    mock_client.get_devices.return_value = []
    srv.setup(mock_client)
    try:
        device, err = await _get_device("MISSING")
        assert device is None
        assert err is not None
        payload = json.loads(err)
        assert "error" in payload
        assert "MISSING" in payload["error"]
    finally:
        srv._aci_client = None
        srv._invalidate_device_cache()


@pytest.mark.asyncio
async def test_get_device_not_found_returns_json_string():
    """err must be a JSON string (tool handlers return it directly)."""
    import ac_infinity_mcp.server as srv
    mock_client = MagicMock()
    mock_client.get_devices.return_value = []
    srv.setup(mock_client)
    try:
        _, err = await _get_device("X")
        assert isinstance(err, str)
        json.loads(err)  # must not raise
    finally:
        srv._aci_client = None
        srv._invalidate_device_cache()


# ============ _get_port_label() helper (issue #201) ============

@pytest.mark.parametrize("port_name,port,expected_label", [
    ("Humidifier",  3, "Humidifier (Port 3)"),
    ("Port 3",      3, "Port 3"),
    ("CO2 Sensor",  1, "CO2 Sensor (Port 1)"),
    ("Port 8",      8, "Port 8"),
])
def test_get_port_label(port_name, port, expected_label):
    device = {
        "deviceInfo": {
            "ports": [{"port": port, "portName": port_name}]
        }
    }
    name, label, port_data = _get_port_label(device, port)
    assert name == port_name
    assert label == expected_label
    assert port_data is not None
    assert port_data.get("portName") == port_name


def test_get_port_label_missing_port_data():
    """Falls back gracefully when port not in ports list."""
    device = {"deviceInfo": {"ports": []}}
    name, label, port_data = _get_port_label(device, 5)
    assert label == "Port 5"
    assert port_data is None


# ============ Write-tool empty-port warning (issue #165) ============

def _make_device_with_empty_port(port: int, dev_type: int = 11) -> dict:
    """Legacy device with one default-named, zero-load port (appears empty)."""
    dev = copy.deepcopy(MOCK_DEVICE_LEGACY)
    dev["devType"] = dev_type
    dev["deviceInfo"]["ports"] = [
        {"port": port, "portName": f"Port {port}", "portsLoad": 0,
         "speak": 0, "loadState": 0, "curMode": 2, "remainTime": 0},
    ]
    return dev


def _make_device_with_connected_port(port: int, dev_type: int = 11) -> dict:
    """Legacy device with one custom-named port (assumed connected)."""
    dev = copy.deepcopy(MOCK_DEVICE_LEGACY)
    dev["devType"] = dev_type
    dev["deviceInfo"]["ports"] = [
        {"port": port, "portName": "Filter", "portsLoad": 5,
         "speak": 5, "loadState": 1, "curMode": 2, "remainTime": 0},
    ]
    return dev


MOCK_WRITE_DRY = {
    "payload": {"onSpead": 10},
    "dry_run": True,
    "controller_type": "legacy",
    "sent": False,
    "prior_at_type": 2,
}

MOCK_WRITE_DRY_SPEED = {
    "payload": {"onSpead": 5},
    "dry_run": True,
    "controller_type": "legacy",
    "sent": False,
    "prior_at_type": 2,
}


async def test_set_port_on_empty_port_warning(mock_client):
    """set_port_on: default-named zero-load port gets advisory."""
    mock_client.set_port_mode.return_value = MOCK_WRITE_DRY
    mock_client.get_devices.return_value = [_make_device_with_empty_port(7)]
    result = await set_port_on("C58ZA", 7)
    data = json.loads(result)
    assert "advisory" in data
    assert "Port 7" in data["advisory"]
    assert "connected" in data["advisory"]


async def test_set_port_on_connected_port_no_warning(mock_client):
    """set_port_on: custom-named port does NOT get advisory."""
    mock_client.set_port_mode.return_value = MOCK_WRITE_DRY
    mock_client.get_devices.return_value = [_make_device_with_connected_port(4)]
    result = await set_port_on("C58ZA", 4)
    data = json.loads(result)
    assert "advisory" not in data


async def test_set_port_off_empty_port_warning(mock_client):
    """set_port_off: default-named zero-load port gets advisory."""
    mock_client.set_port_mode.return_value = MOCK_WRITE_DRY
    mock_client.get_devices.return_value = [_make_device_with_empty_port(7)]
    result = await set_port_off("C58ZA", 7)
    data = json.loads(result)
    assert "advisory" in data
    assert "connected" in data["advisory"]


async def test_set_port_off_connected_port_no_warning(mock_client):
    """set_port_off: custom-named connected port has no advisory."""
    mock_client.set_port_mode.return_value = MOCK_WRITE_DRY
    mock_client.get_devices.return_value = [_make_device_with_connected_port(4)]
    result = await set_port_off("C58ZA", 4)
    data = json.loads(result)
    assert "advisory" not in data


async def test_set_port_speed_empty_port_warning(mock_client):
    """set_port_speed: default-named zero-load port gets advisory."""
    mock_client.set_port_mode.return_value = MOCK_WRITE_DRY_SPEED
    mock_client.get_devices.return_value = [_make_device_with_empty_port(7)]
    result = await set_port_speed("C58ZA", 7, 5)
    data = json.loads(result)
    assert "advisory" in data
    assert "connected" in data["advisory"]


async def test_set_port_speed_connected_port_no_warning(mock_client):
    """set_port_speed: custom-named port has no empty-port advisory."""
    mock_client.set_port_mode.return_value = MOCK_WRITE_DRY_SPEED
    mock_client.get_devices.return_value = [_make_device_with_connected_port(4)]
    result = await set_port_speed("C58ZA", 4, 5)
    data = json.loads(result)
    assert "advisory" not in data


async def test_set_port_speed_off_mode_and_empty_port_both_warned(mock_client):
    """set_port_speed: OFF-mode warning and empty-port advisory are separate keys."""
    off_mode_dry = {**MOCK_WRITE_DRY_SPEED, "prior_at_type": 1}  # atType=1 = OFF
    mock_client.set_port_mode.return_value = off_mode_dry
    mock_client.get_devices.return_value = [_make_device_with_empty_port(7)]
    result = await set_port_speed("C58ZA", 7, 5)
    data = json.loads(result)
    assert "warning" in data
    assert "OFF mode" in data["warning"]
    assert "advisory" in data
    assert "connected" in data["advisory"]


async def test_set_vpd_automation_empty_port_warning(mock_client):
    """set_vpd_automation: empty port gets advisory."""
    mock_client.set_port_mode.return_value = MOCK_WRITE_DRY
    mock_client.get_devices.return_value = [_make_device_with_empty_port(7)]
    result = await set_vpd_automation("C58ZA", 7, 1.2)
    data = json.loads(result)
    assert "advisory" in data
    assert "connected" in data["advisory"]


async def test_set_vpd_automation_connected_port_no_warning(mock_client):
    """set_vpd_automation: connected port has no advisory."""
    mock_client.set_port_mode.return_value = MOCK_WRITE_DRY
    mock_client.get_devices.return_value = [_make_device_with_connected_port(4)]
    result = await set_vpd_automation("C58ZA", 4, 1.2)
    data = json.loads(result)
    assert "advisory" not in data


async def test_set_temperature_automation_empty_port_warning(mock_client):
    """set_temperature_automation: empty port gets advisory."""
    mock_client.set_port_mode.return_value = MOCK_WRITE_DRY
    mock_client.get_devices.return_value = [_make_device_with_empty_port(7, dev_type=11)]
    # devType=11 device uses °C (unit=1 in deviceInfo)
    dev = _make_device_with_empty_port(7, dev_type=11)
    dev["deviceInfo"]["unit"] = 1
    mock_client.get_devices.return_value = [dev]
    result = await set_temperature_automation("C58ZA", 7, 20.0, 28.0)
    data = json.loads(result)
    assert "advisory" in data
    assert "connected" in data["advisory"]


async def test_set_temperature_automation_connected_port_no_warning(mock_client):
    """set_temperature_automation: connected port has no advisory."""
    mock_client.set_port_mode.return_value = MOCK_WRITE_DRY
    dev = _make_device_with_connected_port(4)
    dev["deviceInfo"]["unit"] = 1
    mock_client.get_devices.return_value = [dev]
    result = await set_temperature_automation("C58ZA", 4, 20.0, 28.0)
    data = json.loads(result)
    assert "advisory" not in data


async def test_set_humidity_automation_empty_port_warning(mock_client):
    """set_humidity_automation: empty port gets advisory."""
    mock_client.set_port_mode.return_value = MOCK_WRITE_DRY
    mock_client.get_devices.return_value = [_make_device_with_empty_port(7)]
    result = await set_humidity_automation("C58ZA", 7, 50.0, 70.0)
    data = json.loads(result)
    assert "advisory" in data
    assert "connected" in data["advisory"]


async def test_set_humidity_automation_connected_port_no_warning(mock_client):
    """set_humidity_automation: connected port has no advisory."""
    mock_client.set_port_mode.return_value = MOCK_WRITE_DRY
    mock_client.get_devices.return_value = [_make_device_with_connected_port(4)]
    result = await set_humidity_automation("C58ZA", 4, 50.0, 70.0)
    data = json.loads(result)
    assert "advisory" not in data


async def test_set_port_mode_empty_port_warning(mock_client):
    """set_port_mode: empty port gets advisory."""
    mock_client.set_port_mode.return_value = MOCK_WRITE_DRY
    mock_client.get_devices.return_value = [_make_device_with_empty_port(7)]
    result = await set_port_mode("C58ZA", 7, "ON")
    data = json.loads(result)
    assert "advisory" in data
    assert "connected" in data["advisory"]


async def test_set_port_mode_connected_port_no_warning(mock_client):
    """set_port_mode: connected port has no advisory."""
    mock_client.set_port_mode.return_value = MOCK_WRITE_DRY
    mock_client.get_devices.return_value = [_make_device_with_connected_port(4)]
    result = await set_port_mode("C58ZA", 4, "ON")
    data = json.loads(result)
    assert "advisory" not in data


# ============ Read-tool empty-port advisory (issue #165) ============

async def test_get_port_status_empty_port_note(mock_client):
    """get_port_status: default-named zero-load port gets advisory."""
    dev = _make_device_with_empty_port(7)
    mock_client.get_devices.return_value = [dev]
    result = await get_port_status("C58ZA", 7)
    data = json.loads(result)
    assert "advisory" in data
    assert "Port 7" in data["advisory"]
    assert "connected" in data["advisory"]


async def test_get_port_status_connected_port_no_note(mock_client):
    """get_port_status: custom-named port has no advisory."""
    dev = _make_device_with_connected_port(4)
    mock_client.get_devices.return_value = [dev]
    result = await get_port_status("C58ZA", 4)
    data = json.loads(result)
    assert "advisory" not in data


async def test_get_port_status_portresistance_65535_custom_name_note(mock_client):
    """portResistance=65535 + custom-named port → advisory fires on get_port_status (#183)."""
    dev = copy.deepcopy(MOCK_DEVICE_LEGACY)
    dev["deviceInfo"]["ports"] = [
        {"port": 1, "portName": "Humidifier", "speak": 0, "portsLoad": 0,
         "loadState": 0, "curMode": 2, "remainTime": 0, "portResistance": 65535},
    ]
    mock_client.get_devices.return_value = [dev]
    result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert "advisory" in data
    assert "connected" in data["advisory"]


async def test_get_port_settings_empty_port_note(mock_client):
    """get_port_settings: default-named zero-load port gets advisory on non-ADVANCE path."""
    dev = _make_device_with_empty_port(7)
    mock_client.get_devices.return_value = [dev]
    mock_client.get_mode_settings.return_value = {
        "modeType": 2, "onSpead": 0, "isOpenAutomation": 0,
    }
    result = await get_port_settings("C58ZA", 7)
    data = json.loads(result)
    assert "advisory" in data
    assert "different port" in data["advisory"]
    assert "Port 7" in data["human_summary"]
    assert "connected" in data["human_summary"]


async def test_get_port_settings_connected_port_no_note(mock_client):
    """get_port_settings: custom-named port has no advisory."""
    dev = _make_device_with_connected_port(4)
    mock_client.get_devices.return_value = [dev]
    mock_client.get_mode_settings.return_value = {
        "modeType": 2, "onSpead": 5, "isOpenAutomation": 0,
    }
    result = await get_port_settings("C58ZA", 4)
    data = json.loads(result)
    assert "advisory" not in data


async def test_get_port_settings_empty_port_note_advance_path(mock_client):
    """get_port_settings: ADVANCE path also gets advisory when port is empty."""
    dev = _make_device_with_empty_port(7)
    mock_client.get_devices.return_value = [dev]
    mock_client.get_mode_settings.return_value = {
        "modeType": 15, "isOpenAutomation": 1,
    }
    mock_client.get_advance_automations.return_value = []
    result = await get_port_settings("C58ZA", 7)
    data = json.loads(result)
    assert "advisory" in data
    assert "connected" in data["advisory"]


# ============ devType=18 empty-port advisory integration (issue #165) ============

async def test_set_port_on_devtype18_default_name_warns(mock_client):
    """devType=18 (Willie's Tent): default-named port triggers advisory."""
    mock_client.set_port_mode.return_value = MOCK_WRITE_DRY
    dev = copy.deepcopy(MOCK_DEVICE_LEGACY)
    dev["devType"] = 18
    dev["deviceInfo"]["ports"] = [
        {"port": 7, "portName": "Port 7", "portsLoad": 0,
         "speak": 0, "loadState": 0, "curMode": 2, "remainTime": 0},
    ]
    mock_client.get_devices.return_value = [dev]
    result = await set_port_on("C58ZA", 7)
    data = json.loads(result)
    assert "advisory" in data


async def test_set_port_on_devtype18_custom_name_no_warning(mock_client):
    """devType=18: custom-named port (e.g. Filter) does NOT trigger advisory.

    Exercises the fallback path (portResistance absent): custom name → assumed connected.
    """
    mock_client.set_port_mode.return_value = MOCK_WRITE_DRY
    dev = copy.deepcopy(MOCK_DEVICE_LEGACY)
    dev["devType"] = 18
    dev["deviceInfo"]["ports"] = [
        {"port": 4, "portName": "Filter", "portsLoad": 0,
         "speak": 5, "loadState": 1, "curMode": 2, "remainTime": 0},
    ]
    mock_client.get_devices.return_value = [dev]
    result = await set_port_on("C58ZA", 4)
    data = json.loads(result)
    assert "advisory" not in data


async def test_set_port_on_portresistance_65535_custom_name_warns(mock_client):
    """portResistance=65535 + custom-named port → advisory fires (core #183 — write-tool level)."""
    mock_client.set_port_mode.return_value = MOCK_WRITE_DRY
    dev = copy.deepcopy(MOCK_DEVICE_LEGACY)
    dev["deviceInfo"]["ports"] = [
        {"port": 4, "portName": "Humidifier", "portsLoad": 0,
         "speak": 0, "loadState": 0, "curMode": 2, "remainTime": 0, "portResistance": 65535},
    ]
    mock_client.get_devices.return_value = [dev]
    result = await set_port_on("C58ZA", 4)
    data = json.loads(result)
    assert "advisory" in data
    assert "connected" in data["advisory"]


# ============ _is_port_not_powered helper (issue #178) ============


def test_is_port_not_powered_none_port_data():
    """None port_data → False (safe default)."""
    device = _make_device(dev_type=11)
    assert _is_port_not_powered(None, device) is False


def test_is_port_not_powered_none_device():
    """None device → False (safe default)."""
    port_data = _make_port_data(1, name="Humidifier", ports_load=0)
    assert _is_port_not_powered(port_data, None) is False


def test_is_port_not_powered_devtype18_skipped():
    """devType=18 always reports portsLoad=0 → helper returns False (signal unreliable)."""
    port_data = _make_port_data(4, name="Filter", ports_load=0)
    device = _make_device(dev_type=18)
    assert _is_port_not_powered(port_data, device) is False


def test_is_port_not_powered_devtype22_skipped():
    """devType=22 always reports portsLoad=0 → helper returns False (signal unreliable)."""
    port_data = _make_port_data(3, name="Port 3", ports_load=0)
    device = _make_device(dev_type=22)
    assert _is_port_not_powered(port_data, device) is False


def test_is_port_not_powered_zero_load_default_name():
    """Standard device, default-named port, portsLoad=0 → True."""
    port_data = _make_port_data(7, name="Port 7", ports_load=0)
    device = _make_device(dev_type=11)
    assert _is_port_not_powered(port_data, device) is True


def test_is_port_not_powered_zero_load_custom_name():
    """Standard device, custom-named port, portsLoad=0 → True.

    Unlike _is_port_empty, custom names do NOT skip the check — a named port
    can still be off (the issue #178 use case: 'Humidifier' with portsLoad=0).
    """
    port_data = _make_port_data(1, name="Humidifier", ports_load=0)
    device = _make_device(dev_type=11)
    assert _is_port_not_powered(port_data, device) is True


def test_is_port_not_powered_nonzero_load():
    """Standard device, portsLoad=5 → False (port is drawing power)."""
    port_data = _make_port_data(2, name="Exhaust", ports_load=5)
    device = _make_device(dev_type=11)
    assert _is_port_not_powered(port_data, device) is False


def test_is_port_not_powered_missing_ports_load_key():
    """Missing portsLoad key coalesces to 0 → True (treat absent as not powered)."""
    port_data = {"port": 3}  # no portsLoad key
    device = _make_device(dev_type=11)
    assert _is_port_not_powered(port_data, device) is True


# ============ _build_advance_conflict_response not-powered note (issue #178) ============


async def test_advance_conflict_not_powered_note_nospeed_path(mock_client):
    """Sub-path A, no speed, portsLoad=0 → 'not currently drawing power' note in
    suggested_reply and human_summary.

    Port 4 is in MOCK_ADVANCE_AUTOMATIONS_LIST (grouptDevType=8, bit 3 = Port 4),
    so the bitmask lookup yields Sub-path A ('Moderate Airflow').
    Port 4 is given portsLoad=0 and a custom name 'Filter' to trigger the note.
    """
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    mock_client.get_advance_automations.return_value = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_LIST)
    dev = copy.deepcopy(MOCK_DEVICE_LEGACY)
    dev["deviceInfo"]["ports"].append(
        {"port": 4, "portName": "Filter", "speak": 0, "portsLoad": 0,
         "loadState": 0, "curMode": 15, "remainTime": 0}
    )
    mock_client.get_devices.return_value = [dev]
    result = await set_port_off("C58ZA", port=4, dry_run=False)
    data = json.loads(result)
    assert data.get("conflict") == "ADVANCE_AUTOMATION"
    assert "not currently drawing power" in data["suggested_reply"]
    assert "not currently drawing power" in data["human_summary"]


async def test_advance_conflict_not_powered_note_speed_path(mock_client):
    """Sub-path A, requested_speed provided, portsLoad=0 → note injected before
    'What would you prefer?' and suggested_reply still ends with that phrase.

    Port 4 in MOCK_ADVANCE_AUTOMATIONS_LIST (grouptDevType=8).
    """
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    mock_client.get_advance_automations.return_value = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_LIST)
    dev = copy.deepcopy(MOCK_DEVICE_LEGACY)
    dev["deviceInfo"]["ports"].append(
        {"port": 4, "portName": "Filter", "speak": 0, "portsLoad": 0,
         "loadState": 0, "curMode": 15, "remainTime": 0}
    )
    mock_client.get_devices.return_value = [dev]
    result = await set_port_speed("C58ZA", port=4, speed=5, dry_run=False)
    data = json.loads(result)
    assert data.get("conflict") == "ADVANCE_AUTOMATION"
    assert "not currently drawing power" in data["suggested_reply"]
    assert data["suggested_reply"].endswith("What would you prefer?")
    assert "not currently drawing power" in data["human_summary"]


async def test_advance_conflict_not_powered_note_absent_on_subpath_b(mock_client):
    """Sub-path B regression: portsLoad=0 on a port not covered by any bitmask
    must NOT inject the 'not powered' note.

    Port 1 has no coverage in MOCK_ADVANCE_AUTOMATIONS_LIST (port bitmasks are
    grouptDevType=48 and grouptDevType=8 and grouptDevType=4 — none covers port 1),
    so the response follows Sub-path B (controller-wide lock).  The note is only
    valid when we know WHICH automation governs the port; on Sub-path B we don't.
    """
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("advance")
    mock_client.get_advance_automations.return_value = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_LIST)
    dev = copy.deepcopy(MOCK_DEVICE_LEGACY)
    # Mutate port 1 to portsLoad=0 to confirm the note is NOT injected on Sub-path B.
    dev["deviceInfo"]["ports"][0]["portsLoad"] = 0
    mock_client.get_devices.return_value = [dev]
    result = await set_port_off("C58ZA", port=1, dry_run=False)
    data = json.loads(result)
    assert data.get("conflict") == "ADVANCE_AUTOMATION"
    assert "1_break_out" not in data["options"]  # confirms Sub-path B
    assert "not currently drawing power" not in data["suggested_reply"]
    assert "not currently drawing power" not in data["human_summary"]


# ============ _get_device TTL cache ============


async def test_device_cache_hit_skips_second_fetch(mock_client):
    """Second call within TTL must not hit the API again."""
    await _get_device("C58ZA")
    await _get_device("C58ZA")
    assert mock_client.get_devices.call_count == 1


async def test_device_cache_miss_after_ttl_expiry(mock_client, monkeypatch):
    """After TTL expires, the next call must re-fetch from the API."""
    import ac_infinity_mcp.server as srv

    call_times = [0.0]

    def advancing_monotonic():
        t = call_times[0]
        call_times[0] += 1.0
        return t

    monkeypatch.setattr(time, "monotonic", advancing_monotonic)
    monkeypatch.setattr(srv, "_DEVICE_CACHE_TTL", 0.5)  # short TTL so expiry is easy to trigger

    await _get_device("C58ZA")      # t=0 → cache miss, fetches, expires_at=0.5
    await _get_device("C58ZA")      # t=1 → past expiry, fetches again
    assert mock_client.get_devices.call_count == 2


async def test_invalidate_device_cache_forces_fresh_fetch(mock_client):
    """_invalidate_device_cache() must cause the next _get_device call to re-fetch."""
    await _get_device("C58ZA")      # warms cache
    _invalidate_device_cache()
    await _get_device("C58ZA")      # cache is cold → must fetch again
    assert mock_client.get_devices.call_count == 2


async def test_device_cache_not_found_returns_error_json(mock_client):
    """Cache hit for a missing device_id must return (None, error_json) without re-fetching."""
    mock_client.get_devices.return_value = [copy.deepcopy({"devCode": "OTHER", "devName": "Other"})]
    device, error = await _get_device("NOTHERE")
    # A second attempt should still use the cached list (no extra fetch)
    device2, error2 = await _get_device("NOTHERE")
    assert device is None
    assert error is not None
    error_data = json.loads(error)
    assert "not found" in error_data["error"].lower()
    assert mock_client.get_devices.call_count == 1


# ============ #232: set_port_off/on atType fix ============


async def test_set_port_off_sends_atType_1_and_zero_speed(mock_client):
    """set_port_off must include atType=1 in the updates dict to switch mode to OFF.

    Sending only onSpead=0 without atType=1 leaves the port in ON mode (atType=2)
    at speed zero — the device stays on. Explicit atType=1 forces the OFF state.
    """
    mock_client.set_port_mode.return_value = MOCK_SET_PORT_OFF_DRY
    await set_port_off("C58ZA", 1, dry_run=True)
    updates = mock_client.set_port_mode.call_args[0][2]
    assert updates["atType"] == 1
    assert updates["onSpead"] == 0


async def test_set_port_on_sends_atType_2(mock_client):
    """set_port_on must include atType=2 in the updates dict to switch mode to ON.

    After a set_port_off call, current_settings has atType=1. A subsequent set_port_on
    with only onSpead=10 would merge atType=1 and send the device ON at speed zero.
    Explicit atType=2 closes this gap.
    """
    mock_client.set_port_mode.return_value = MOCK_SET_PORT_ON_DRY
    await set_port_on("C58ZA", 1, dry_run=True)
    updates = mock_client.set_port_mode.call_args[0][2]
    assert updates["atType"] == 2
    assert updates["onSpead"] == 10


# ============ #190: break_out co-port filter devType=18 ============


@pytest.mark.parametrize("port_resistance,expect_locked", [
    (None, False),    # portResistance absent (devType=18 old firmware) → excluded
    (7500, True),     # portResistance present and non-65535 → included
])
async def test_break_out_co_port_filter_devtype18(mock_client, port_resistance, expect_locked):
    """Co-port filter must use _is_port_empty, not portResistance==65535.

    devType=18 (Willie's Tent) omits portResistance on disconnected ports.
    The old portResistance==65535 check passes None==65535 as False, so empty
    ports were not filtered and received lock writes the API rejects.
    _is_port_empty handles the absent-portResistance case via the fallback heuristic
    (default port name + portsLoad==0).
    """
    import copy as _copy
    device = _copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["devType"] = 18

    # Port 5 is the target port (covered by grouptDevType=16 = bit 4 = port 5).
    # Port 1 is a co-port (covered by grouptDevType=17 = bits 0+4 = ports 1+5).
    port_1_data: dict = {
        "port": 1,
        "portName": "Port 1",   # default name → fallback heuristic applies
        "speak": 0,
        "portsLoad": 0,
        "loadState": 0,
        "curMode": 1,
        "remainTime": 0,
    }
    if port_resistance is not None:
        port_1_data["portResistance"] = port_resistance

    port_5_data: dict = {
        "port": 5,
        "portName": "Heater",
        "speak": 3,
        "portsLoad": 1,
        "loadState": 1,
        "curMode": 2,
        "remainTime": 0,
        "portResistance": 15800,
    }
    device["deviceInfo"]["ports"] = [port_1_data, port_5_data]
    mock_client.get_devices.return_value = [device]

    # Automation covers ports 1 and 5 (bitmask 1+16=17)
    auto = _copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_LIST[0])
    auto["grouptDevType"] = 17
    mock_client.get_advance_automations.return_value = [auto]
    mock_client.get_mode_settings.return_value = {"modeType": _ADVANCE_MODE_TYPE, "onSpead": 3}

    result = await break_out_of_automation("C58ZA", port=5, dry_run=True)
    data = json.loads(result)
    assert "error" not in data, f"Unexpected error: {data.get('error')}"

    co_ports = data.get("co_ports_to_lock", [])
    port_1_locked = any(cp.get("port") == 1 for cp in co_ports)
    assert port_1_locked is expect_locked, (
        f"port_resistance={port_resistance!r}: expected port 1 locked={expect_locked}, "
        f"got co_ports_to_lock={co_ports}"
    )


# ============ #191: ghost-ADVANCE no-op (all automations disabled) ============
# Covered by test_break_out_no_enabled_automation (line 6932) which was updated in this PR.


# ============ #245: atType mode table is the verified numbering, not MifsHub's ============


def test_mode_labels_match_verified_attype_numbering():
    """Lock _MODE_LABELS to the numbering corroborated by the HA ac_infinity integration,
    Brysshmurda's client, and the devType=20 captures. The original #245 issue body (from a
    single community source) proposed a mis-indexed table (0=OFF, 7=VPD, 8=SCHEDULE, 15=ADVANCE);
    this guards against anyone "correcting" our right table to that wrong one.
    See .claude/internal/CONTROLLER_89_AIPLUS_RESEARCH.md.
    """
    import ac_infinity_mcp.server as srv

    assert srv._MODE_LABELS == {
        1: "OFF",
        2: "ON",
        3: "AUTO",
        4: "TIMER_TO_ON",
        5: "TIMER_TO_OFF",
        6: "CYCLE",
        7: "SCHEDULE",
        8: "VPD",
    }


def test_advance_mode_type_excluded_from_writable_modes():
    """atType=15 (ADVANCE) must never be a writable mode — writing it returns API 999999.
    It is intentionally absent from _MODE_LABELS / _MODE_AT_TYPES.
    """
    import ac_infinity_mcp.server as srv

    assert 15 not in srv._MODE_LABELS
    assert "ADVANCE" not in srv._MODE_AT_TYPES
    # And there is no spurious atType=0 ("OFF" is 1, not 0).
    assert 0 not in srv._MODE_LABELS


def test_decode_mode_roundtrip_and_unknown():
    import ac_infinity_mcp.server as srv

    assert srv._decode_mode(1) == "OFF"
    assert srv._decode_mode(2) == "ON"
    assert srv._decode_mode(8) == "VPD"
    assert srv._decode_mode(None) == "UNKNOWN"
    assert srv._decode_mode(15) == "UNKNOWN(15)"  # ADVANCE not a decodable label
    # Reverse map is consistent with the forward table.
    assert srv._MODE_AT_TYPES == {v: k for k, v in srv._MODE_LABELS.items()}


# ============ #249 — shared (isShare=1) controllers are read-only for writes ============

_SHARED_DEVICE = {**MOCK_DEVICE_LEGACY, "isShare": 1}


async def test_get_device_for_write_blocks_shared(mock_client):
    """A controller shared from another account (isShare=1) is rejected for writes with a
    grower-readable read-only message that leaks no internal field name or id."""
    mock_client.get_devices.return_value = [copy.deepcopy(_SHARED_DEVICE)]
    device, err = await _get_device("C58ZA", for_write=True)
    assert device is None
    data = json.loads(err)
    assert "shared" in data["error"] and "view-only" in data["error"]
    assert "isShare" not in err and "devId" not in err  # no internal leakage


async def test_get_device_read_allows_shared(mock_client):
    """The same shared controller stays viewable for read tools (for_write=False)."""
    mock_client.get_devices.return_value = [copy.deepcopy(_SHARED_DEVICE)]
    device, err = await _get_device("C58ZA")  # default for_write=False
    assert err is None
    assert device is not None and device["devCode"] == "C58ZA"


async def test_get_device_for_write_allows_when_isShare_absent(mock_client):
    """Owned controllers carry no isShare field → writable (the common path)."""
    mock_client.get_devices.return_value = [copy.deepcopy(MOCK_DEVICE_LEGACY)]
    device, err = await _get_device("C58ZA", for_write=True)
    assert err is None and device is not None


async def test_get_device_for_write_allows_when_isShare_zero(mock_client):
    """isShare=0 (explicitly not shared) → writable."""
    mock_client.get_devices.return_value = [{**MOCK_DEVICE_LEGACY, "isShare": 0}]
    device, err = await _get_device("C58ZA", for_write=True)
    assert err is None and device is not None


# Every write tool: (label, callable(dry_run), client write method that must NOT be called).
_WRITE_TOOL_CASES = [
    ("set_port_speed", lambda dr: set_port_speed("C58ZA", 1, 5, dry_run=dr), "set_port_mode"),
    ("set_port_on", lambda dr: set_port_on("C58ZA", 1, dry_run=dr), "set_port_mode"),
    ("set_port_off", lambda dr: set_port_off("C58ZA", 1, dry_run=dr), "set_port_mode"),
    (
        "set_vpd_automation",
        lambda dr: set_vpd_automation("C58ZA", 1, 1.2, dry_run=dr),
        "set_port_mode",
    ),
    (
        "set_temperature_automation",
        lambda dr: set_temperature_automation("C58ZA", 1, 20.0, 28.0, dry_run=dr),
        "set_port_mode",
    ),
    (
        "set_humidity_automation",
        lambda dr: set_humidity_automation("C58ZA", 1, 40.0, 60.0, dry_run=dr),
        "set_port_mode",
    ),
    ("set_port_mode", lambda dr: set_port_mode("C58ZA", 1, "ON", dry_run=dr), "set_port_mode"),
    (
        "apply_grow_stage_template",
        lambda dr: apply_grow_stage_template("C58ZA", 1, "veg", dry_run=dr),
        "set_port_mode",
    ),
    (
        "enable_advance_automation",
        lambda dr: enable_advance_automation("C58ZA", "1234567", dry_run=dr),
        "enable_advance_automation",
    ),
    (
        "disable_advance_automation",
        lambda dr: disable_advance_automation("C58ZA", "1234567", dry_run=dr),
        "disable_advance_automation",
    ),
    (
        "create_advance_automation",
        lambda dr: create_advance_automation("C58ZA", "Night", 5, 1, dry_run=dr),
        "create_advance_automation",
    ),
    (
        "delete_advance_automation",
        lambda dr: delete_advance_automation("C58ZA", "1234567", dry_run=dr),
        "delete_advance_automation",
    ),
    (
        "break_out_of_automation",
        lambda dr: break_out_of_automation("C58ZA", 1, dry_run=dr),
        "disable_advance_automation",
    ),
    (
        "add_automation_rule",
        lambda dr: add_automation_rule("C58ZA", "Seedling", [1], "on", dry_run=dr),
        "create_advance_automation",
    ),
    (
        "update_automation_rule",
        lambda dr: update_automation_rule("C58ZA", "Seedling", [1], max_level=3, dry_run=dr),
        "update_advance_automation",
    ),
    (
        "delete_automation_rule",
        lambda dr: delete_automation_rule("C58ZA", "Seedling", [1], dry_run=dr),
        "delete_advance_automation",
    ),
]


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize(
    "label,call,write_method", _WRITE_TOOL_CASES, ids=[c[0] for c in _WRITE_TOOL_CASES]
)
async def test_write_tool_blocks_shared_device(mock_client, label, call, write_method, dry_run):
    """Every write tool refuses a shared controller (both preview and live) and never calls
    its client write method — proving each passes for_write=True to _get_device."""
    mock_client.get_devices.return_value = [copy.deepcopy(_SHARED_DEVICE)]
    result = await call(dry_run)
    data = json.loads(result)
    assert "shared" in data.get("error", ""), f"{label} did not return the read-only message"
    getattr(mock_client, write_method).assert_not_called()


async def test_break_out_blocks_shared_before_lock_and_disable(mock_client):
    """break_out_of_automation rejects a shared device before acquiring the per-device lock
    or calling disable — no partial sequence on a controller we cannot write."""
    mock_client.get_devices.return_value = [copy.deepcopy(_SHARED_DEVICE)]
    result = await break_out_of_automation("C58ZA", 1, dry_run=False)
    data = json.loads(result)
    assert "shared" in data["error"]
    mock_client.disable_advance_automation.assert_not_called()
    mock_client.set_port_mode.assert_not_called()


def test_all_write_tools_covered_by_shared_guard():
    """Completeness guard: the parametrized shared-device test must cover EVERY registered
    write tool (a tool whose signature has a dry_run parameter). A newly-added write tool
    not in _WRITE_TOOL_CASES fails this test, forcing a conscious decision about the
    isShare read-only guard rather than silently skipping it."""
    import inspect

    import ac_infinity_mcp.server as srv

    covered = {c[0] for c in _WRITE_TOOL_CASES}
    write_tools = set()
    for tool in asyncio.run(srv.mcp_server.list_tools()):
        fn = getattr(srv, tool.name, None)
        if fn is not None and "dry_run" in inspect.signature(fn).parameters:
            write_tools.add(tool.name)
    assert write_tools, "expected to discover write tools via the dry_run signature"
    assert write_tools == covered, (
        f"uncovered write tools: {write_tools - covered}; stale cases: {covered - write_tools}"
    )


# ============ Issue #284 — automation rule CRUD tools (Rev-4 compositional) ============


def _seedling_program():
    """Seedling program (capture-aligned): two port-1 rules (different windows) +
    one port-2 auto-trigger rule. Full per-mode field sets carried via deep-copied
    fixtures so the read-before-write overlay has every field to preserve."""
    # All three rules share one program SLOT (groupNums=1, sortType=6) with sequential
    # subNumber 0/1/2 — the real shape an appended rule must join (Issue #284).
    return [
        {**copy.deepcopy(MOCK_RULE_VPD), "advName": "Seedling", "grouptDevType": 1,
         "beginTime": 540, "endTime": 180, "runState": 1, "advId": 5001,
         "groupNums": 1, "sortType": 6, "subNumber": 0, "subNumberSort": 0},
        {**copy.deepcopy(MOCK_RULE_HUMIDITY_SETPOINT), "advName": "Seedling",
         "grouptDevType": 1, "beginTime": 180, "endTime": 540, "runState": 0, "advId": 5002,
         "groupNums": 1, "sortType": 6, "subNumber": 1, "subNumberSort": 1},
        {**copy.deepcopy(MOCK_RULE_TEMPERATURE_TRIGGER), "advName": "Seedling",
         "grouptDevType": 2, "beginTime": 540, "endTime": 180, "runState": 1, "advId": 5003,
         "groupNums": 1, "sortType": 6, "subNumber": 2, "subNumberSort": 2},
    ]


# ---- _days_to_switchtime (dedicated pure-helper unit test) ----


@pytest.mark.parametrize("days,continuous,expected", [
    (None, False, 127),          # default: every day scheduled
    ("all", False, 127),
    ("weekdays", False, 31),     # Mon–Fri
    ("weekends", False, 96),     # Sat | Sun
    (["mon"], False, 1),         # Monday-only → bit0
    (["mon", "thu"], False, 9),  # non-contiguous OR → bit0 | bit3
    (["mon", "tue", "wed", "thu", "fri"], False, 31),
    (["mon", "tue", "wed", "thu", "fri", "sat", "sun"], False, 127),
    ("mon", False, 1),
    (None, True, 255),           # continuous → bit7 set
    (["mon"], True, 255),        # continuous overrides days
    ("weekdays", True, 255),
])
def test_days_to_switchtime(days, continuous, expected):
    assert _days_to_switchtime(days, continuous) == expected


# ---- add_automation_rule ----


async def test_add_automation_rule_dry_run_auto_target(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [1], "auto", control_style="target", humidity_target=65,
        min_level=2, max_level=8, begin_time=180, end_time=540, dry_run=True,
    )
    data = json.loads(result)
    assert data["dry_run"] is True
    assert data["sent"] is False
    assert "humidity: hold at 65%" in data["rule"]["control"]
    assert data["rule"]["ports"] == "Intake Fan (Port 1)"
    assert data["rule"]["_mode"] == "auto"
    mock_client.create_advance_automation.assert_not_called()


async def test_add_automation_rule_dry_run_auto_trigger_combined(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [1], "auto", control_style="trigger",
        temp_high_f=85, humidity_low=50, min_level=2, max_level=8,
        days="weekdays", temp_buffer=3, dry_run=True,
    )
    data = json.loads(result)
    ctrl = data["rule"]["control"]
    assert "temperature: on above 85°F" in ctrl
    assert "humidity: on below 50%" in ctrl
    assert "speed 2–8" in ctrl
    assert "Mon–Fri" in ctrl
    assert "temperature buffer 3°F" in ctrl
    mock_client.create_advance_automation.assert_not_called()


async def test_add_automation_rule_wrap_around_window_allowed(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [1], "vpd", control_style="target", vpd_target=0.9,
        begin_time=540, end_time=180, dry_run=True,
    )
    data = json.loads(result)
    assert "error" not in data
    assert "VPD: hold at 0.9 kPa" in data["rule"]["control"]


async def test_add_automation_rule_program_not_found(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule("C58ZA", "Nonexistent", [1], "on", dry_run=True)
    data = json.loads(result)
    assert "error" in data
    assert "Seedling" in data["existing_programs"]


async def test_add_continuous_rule_window_not_clock_range(mock_client):
    """A continuous rule's window field must NOT be a clock range — it agrees with control."""
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [1], "auto", control_style="target", humidity_target=65,
        continuous=True, begin_time=540, end_time=1020, dry_run=True,
    )
    data = json.loads(result)
    window = data["rule"]["window"]
    assert window == "runs continuously"
    assert "–" not in window  # no clock range; control + window agree
    assert "runs continuously" in data["rule"]["control"]


async def test_add_automation_rule_live_appends_to_slot(mock_client):
    """Append (Issue #284): isFlag=0 + the target program's SLOT (groupNums/sortType) +
    subNumber = existing max + 1, so the rule joins the program rather than spawning a new one."""
    program = _seedling_program()  # slot (1, 6), subNumbers 0/1/2
    mock_client.get_advance_automations.return_value = program
    result = await add_automation_rule("C58ZA", "Seedling", [1], "on", max_level=4, dry_run=False)
    data = json.loads(result)
    assert data["sent"] is True
    mock_client.create_advance_automation.assert_called_once()
    sent = mock_client.create_advance_automation.call_args.args[1]
    assert sent["isFlag"] == 0
    assert sent["groupNums"] == 1
    assert sent["sortType"] == 6
    assert sent["subNumber"] == 3  # max(0,1,2) + 1
    assert sent["subNumberSort"] == 3


async def test_multi_rule_flow_create_then_append(mock_client):
    """End-to-end slot lifecycle: create a NEW program (isFlag=1, subNumber=0), then add a
    second rule (isFlag=0) carrying the created program's slot + subNumber=1. Issue #284."""
    import copy as _copy

    device = _copy.deepcopy(MOCK_DEVICE_LEGACY)
    device["deviceInfo"]["ports"].append({"port": 4, "portName": "Clip Fan"})
    mock_client.get_devices.return_value = [device]
    mock_client.create_advance_automation.return_value = {"advId": 7001}

    # 1) create a new program.
    await create_advance_automation("C58ZA", "Veg Fans", on_speed=5, port=4, dry_run=False)
    first_payload = mock_client.create_advance_automation.call_args.args[1]
    assert first_payload["isFlag"] == 1
    assert first_payload["subNumber"] == 0

    # 2) the server now reads back the created program as one rule in slot (3, 4),
    #    subNumber=0; appending must join that slot at subNumber=1.
    mock_client.get_advance_automations.return_value = [{
        **_copy.deepcopy(MOCK_RULE_VPD), "advName": "Veg Fans", "advId": 7001,
        "grouptDevType": 8, "groupNums": 3, "sortType": 4, "subNumber": 0, "subNumberSort": 0,
    }]
    mock_client.create_advance_automation.reset_mock()
    await add_automation_rule("C58ZA", "Veg Fans", [4], "on", max_level=6, dry_run=False)
    second_payload = mock_client.create_advance_automation.call_args.args[1]
    assert second_payload["isFlag"] == 0
    assert second_payload["groupNums"] == 3
    assert second_payload["sortType"] == 4
    assert second_payload["subNumber"] == 1


async def test_add_automation_rule_more_than_one_program_same_name_rejected(mock_client):
    """A name mapping to >1 distinct (groupNums, sortType) slot is ambiguous → friendly
    disambiguation error, no write."""
    program = _seedling_program()
    program[2]["groupNums"] = 2  # second distinct slot under the same advName
    mock_client.get_advance_automations.return_value = program
    result = await add_automation_rule("C58ZA", "Seedling", [1], "on", max_level=4, dry_run=False)
    data = json.loads(result)
    assert "error" in data
    assert "More than one program" in data["error"]
    mock_client.create_advance_automation.assert_not_called()


async def test_add_automation_rule_cross_mode_param_rejected(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [1], "on", cycle_on_minutes=30, dry_run=True,
    )
    assert "error" in json.loads(result)
    mock_client.create_advance_automation.assert_not_called()


async def test_add_automation_rule_bad_control_style_rejected(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [2], "auto", control_style="sideways", temp_high_f=82, dry_run=True,
    )
    assert "control_style must be one of" in json.loads(result)["error"]
    mock_client.create_advance_automation.assert_not_called()


async def test_add_automation_rule_auto_missing_control_style_rejected(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [2], "auto", temp_high_f=82, dry_run=True,
    )
    assert "control_style" in json.loads(result)["error"]
    mock_client.create_advance_automation.assert_not_called()


async def test_add_automation_rule_port_not_on_device(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule("C58ZA", "Seedling", [7], "on", dry_run=True)
    data = json.loads(result)
    assert "error" in data
    assert "available_ports" in data


async def test_add_automation_rule_overlap_friendly_no_upstream_echo(mock_client):
    """An 'Adv exist!' upstream failure maps to a self-authored friendly message; the raw
    upstream string never appears in the tool JSON (R6)."""
    mock_client.get_advance_automations.return_value = _seedling_program()
    mock_client.create_advance_automation.side_effect = ACInfinityAPIError("Adv exist!")
    result = await add_automation_rule("C58ZA", "Seedling", [1], "on", max_level=4, dry_run=False)
    data = json.loads(result)
    assert "A rule already covers" in data["error"]
    assert "Adv exist" not in result


# ---- validation rejects: per-guard, write assert_not_called ----


async def test_add_rule_buffer_xor_transition_rejected(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [2], "auto", control_style="trigger", temp_high_f=80,
        temp_buffer=3, temp_transition=2, dry_run=True,
    )
    assert "buffer or a transition" in json.loads(result)["error"]
    mock_client.create_advance_automation.assert_not_called()


async def test_add_rule_humidity_buffer_xor_transition_rejected(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [2], "auto", control_style="trigger", humidity_high=70,
        humidity_buffer=5, humidity_transition=4, dry_run=True,
    )
    assert "buffer or a transition" in json.loads(result)["error"]
    mock_client.create_advance_automation.assert_not_called()


async def test_add_rule_vpd_buffer_xor_transition_rejected(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [2], "vpd", control_style="target", vpd_target=1.2,
        vpd_buffer=0.3, vpd_transition=0.4, dry_run=True,
    )
    assert "buffer or a transition" in json.loads(result)["error"]
    mock_client.create_advance_automation.assert_not_called()


async def test_add_rule_vpd_buffer_exposed_and_encoded(mock_client):
    """vpd_buffer is now a real tool param: it reaches vpdBuff on the live write."""
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [1], "vpd", control_style="target", vpd_target=1.2,
        vpd_buffer=0.3, begin_time=180, end_time=540, dry_run=False,
    )
    data = json.loads(result)
    assert data["sent"] is True
    assert "VPD buffer 0.3 kPa" in data["rule"]["control"]
    sent = mock_client.create_advance_automation.call_args.args[1]
    assert sent["vpdBuff"] == 3


async def test_add_rule_target_trigger_mutual_exclusion_rejected(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [2], "auto", control_style="trigger",
        humidity_target=60, humidity_high=70, dry_run=True,
    )
    assert "pick one" in json.loads(result)["error"]
    mock_client.create_advance_automation.assert_not_called()


async def test_add_rule_min_gt_max_rejected(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [1], "on", min_level=8, max_level=3, dry_run=True,
    )
    assert "less than or equal" in json.loads(result)["error"]
    mock_client.create_advance_automation.assert_not_called()


async def test_add_rule_bad_days_token_rejected(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [1], "on", days=["funday"], dry_run=True,
    )
    assert "days must be" in json.loads(result)["error"]
    mock_client.create_advance_automation.assert_not_called()


# ---- update_automation_rule: selector 0/1/many ----


async def test_update_rule_disambiguation_no_advid(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await update_automation_rule("C58ZA", "Seedling", [1], max_level=5, dry_run=True)
    data = json.loads(result)
    assert len(data["matching_rules"]) == 2
    blob = json.dumps(data)
    assert "advId" not in blob and "adv_id" not in blob
    mock_client.update_advance_automation.assert_not_called()


async def test_update_rule_one_match_via_window(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=180, end_time=540, max_level=4, dry_run=True,
    )
    data = json.loads(result)
    assert "error" not in data
    assert data["rule"]["speed"] == 4
    assert "humidity: hold at 65%" in data["rule"]["control"]  # unchanged mode preserved


async def test_update_rule_zero_match(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=999, end_time=1000, max_level=4, dry_run=True,
    )
    data = json.loads(result)
    assert "error" in data
    assert "existing_rules" in data


async def test_update_rule_no_op_rejected(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=180, end_time=540, dry_run=True,
    )
    data = json.loads(result)
    assert "Nothing to change" in data["error"]
    mock_client.update_advance_automation.assert_not_called()


async def test_update_rule_read_before_write_preserves_structural_field(mock_client):
    """A field NOT in the overlay (switchTime) survives the update round-trip (deep copy)."""
    program = _seedling_program()
    program[1]["switchTime"] = 99
    mock_client.get_advance_automations.return_value = program
    await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=180, end_time=540, max_level=4, dry_run=False,
    )
    sent = mock_client.update_advance_automation.call_args.args[1]
    assert sent["switchTime"] == 99      # preserved (no days/continuous supplied)
    assert sent["onSpeed"] == 4          # change applied
    assert sent["advId"] == 5002         # re-resolved at write time


async def test_update_rule_more_than_one_program_same_name_rejected(mock_client):
    """G1: a name mapping to >1 distinct slot is ambiguous for update too — refuse, no write."""
    program = _seedling_program()
    program[2]["groupNums"] = 2  # second distinct slot under the same advName
    mock_client.get_advance_automations.return_value = program
    result = await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=540, end_time=180, max_level=5, dry_run=False,
    )
    data = json.loads(result)
    assert "More than one program" in data["error"]
    mock_client.update_advance_automation.assert_not_called()


async def test_update_rule_vpd_transition_same_mode_roundtrip(mock_client):
    """Same-mode update of vpd_transition writes vpdTrans (kPa × 10), mode unchanged."""
    mock_client.get_advance_automations.return_value = _seedling_program()
    await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=540, end_time=180,
        vpd_transition=0.3, dry_run=False,
    )
    sent = mock_client.update_advance_automation.call_args.args[1]
    assert sent["vpdTrans"] == 3
    assert sent["advId"] == 5001         # the VPD rule, not the humidity-setpoint one


async def test_update_rule_min_level_same_mode_roundtrip(mock_client):
    """Same-mode update of min_level writes offSpeed, leaving onSpeed untouched."""
    program = _seedling_program()
    program[0]["onSpeed"] = 7
    mock_client.get_advance_automations.return_value = program
    await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=540, end_time=180, min_level=2, dry_run=False,
    )
    sent = mock_client.update_advance_automation.call_args.args[1]
    assert sent["offSpeed"] == 2
    assert sent["onSpeed"] == 7          # unchanged


async def test_update_rule_mode_change_to_on_decodes_as_on(mock_client):
    program = _seedling_program()
    mock_client.get_advance_automations.return_value = program
    await update_automation_rule("C58ZA", "Seedling", [2], mode="on", max_level=3, dry_run=False)
    sent = mock_client.update_advance_automation.call_args.args[1]
    assert sent["currentMode"] == 1
    assert _decode_rule(sent)["mode"] == "on"


async def test_update_rule_mode_change_auto_trigger_to_target_rebuilds(mock_client):
    """Auto-trigger → auto-target: the rebuild parks triggers at rails and writes the
    target, so the decode reads the new target (not the stale trigger)."""
    program = _seedling_program()
    mock_client.get_advance_automations.return_value = program
    await update_automation_rule(
        "C58ZA", "Seedling", [2], mode="auto", control_style="target",
        humidity_target=70, dry_run=False,
    )
    sent = mock_client.update_advance_automation.call_args.args[1]
    assert sent["currentMode"] == 4
    assert sent["settingMode"] == 1
    # The stale temp trigger (autoLowTempF=76) is parked back at its rail by the rebuild.
    assert sent["autoLowTempF"] == 32
    assert sent["targetHumi"] == 70
    decoded = _decode_rule(sent)
    assert decoded["mode"] == "auto"
    assert "humidity: hold at 70%" in decoded["control"]


async def test_update_rule_mode_change_to_vpd_target(mock_client):
    program = _seedling_program()
    mock_client.get_advance_automations.return_value = program
    await update_automation_rule(
        "C58ZA", "Seedling", [2], mode="vpd", control_style="target",
        vpd_target=1.2, dry_run=False,
    )
    sent = mock_client.update_advance_automation.call_args.args[1]
    assert sent["currentMode"] == 6
    assert sent["targetVpd"] == 12
    assert sent["highVpd"] == 12       # encoder rebuild mirrors the setpoint into highVpd too
    assert "VPD: hold at 1.2 kPa" in _decode_rule(sent)["control"]


async def test_update_rule_cross_mode_param_rejected(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=180, end_time=540,
        mode="on", cycle_on_minutes=30, dry_run=True,
    )
    assert "error" in json.loads(result)
    mock_client.update_advance_automation.assert_not_called()


async def test_update_rule_dry_run_no_write(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=180, end_time=540, max_level=4, dry_run=True,
    )
    mock_client.update_advance_automation.assert_not_called()


async def test_update_rule_days_overlay_sets_switchtime(mock_client):
    program = _seedling_program()
    mock_client.get_advance_automations.return_value = program
    await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=180, end_time=540, days="weekdays", dry_run=False,
    )
    sent = mock_client.update_advance_automation.call_args.args[1]
    assert sent["switchTime"] == 31


async def test_update_rule_continuous_overlay_sets_switchtime_255(mock_client):
    program = _seedling_program()
    mock_client.get_advance_automations.return_value = program
    await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=180, end_time=540, continuous=True, dry_run=False,
    )
    sent = mock_client.update_advance_automation.call_args.args[1]
    assert sent["switchTime"] == 255


async def test_update_rule_continuous_window_not_clock_range(mock_client):
    """Setting continuous on an update makes the previewed window read 'runs continuously'."""
    program = _seedling_program()
    mock_client.get_advance_automations.return_value = program
    result = await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=180, end_time=540, continuous=True, dry_run=True,
    )
    data = json.loads(result)
    assert data["rule"]["window"] == "runs continuously"
    assert "–" not in data["rule"]["window"]


# ---- fix #3: mode-change rebuilds buffer/transition (clear stale / apply new) ----


async def test_update_rule_mode_change_clears_stale_buffer(mock_client):
    """Auto-trigger rule carrying a stale temp buffer → mode-change to VPD rebuilds the
    signature; the stale temperatureFBuff is cleared (not carried over)."""
    program = _seedling_program()
    program[2]["temperatureFBuff"] = 7  # stale buffer on the port-2 auto-trigger rule
    mock_client.get_advance_automations.return_value = program
    await update_automation_rule(
        "C58ZA", "Seedling", [2], mode="vpd", control_style="target",
        vpd_target=1.2, dry_run=False,
    )
    sent = mock_client.update_advance_automation.call_args.args[1]
    assert sent["currentMode"] == 6
    assert sent["temperatureFBuff"] == 0  # stale value cleared by the rebuild
    assert sent["humidityBuff"] == 0      # sibling buffer family also cleared, not just temp


async def test_update_rule_mode_change_applies_new_buffer(mock_client):
    """A mode-change that supplies a new buffer writes it into the rebuilt signature."""
    program = _seedling_program()
    mock_client.get_advance_automations.return_value = program
    await update_automation_rule(
        "C58ZA", "Seedling", [2], mode="auto", control_style="trigger",
        temp_high_f=82, temp_buffer=4, dry_run=False,
    )
    sent = mock_client.update_advance_automation.call_args.args[1]
    assert sent["currentMode"] == 4
    assert sent["temperatureFBuff"] == 4
    assert sent["temperatureFTrans"] == 0


# ---- M1: turn OFF continuous via update ----


async def test_update_rule_continuous_false_clears_bit_preserving_days(mock_client):
    """continuous=False clears the 24/7 bit while preserving the day pattern (255 → 127)."""
    program = _seedling_program()
    program[0]["switchTime"] = 255  # currently runs continuously
    mock_client.get_advance_automations.return_value = program
    await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=540, end_time=180,
        continuous=False, dry_run=False,
    )
    sent = mock_client.update_advance_automation.call_args.args[1]
    assert sent["switchTime"] == 127     # bit7 cleared, day bits preserved


async def test_update_rule_continuous_false_alone_is_a_change(mock_client):
    """continuous=False with no other field must NOT be rejected as 'nothing to change'."""
    program = _seedling_program()
    program[0]["switchTime"] = 255
    mock_client.get_advance_automations.return_value = program
    result = await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=540, end_time=180,
        continuous=False, dry_run=True,
    )
    data = json.loads(result)
    assert "Nothing to change" not in data.get("error", "")


# ---- M2: one-sided speed update must not invert min/max ----


async def test_update_rule_one_sided_min_level_inversion_rejected(mock_client):
    """Setting min_level above the live max (onSpeed) with no max_level is rejected."""
    program = _seedling_program()
    program[0]["onSpeed"] = 5
    mock_client.get_advance_automations.return_value = program
    result = await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=540, end_time=180,
        min_level=8, dry_run=False,
    )
    data = json.loads(result)
    assert "minimum speed can't be higher than the maximum" in data["error"]
    mock_client.update_advance_automation.assert_not_called()


async def test_update_rule_one_sided_max_level_inversion_rejected(mock_client):
    """The reverse direction: lowering max_level below the live min (offSpeed) is rejected."""
    program = _seedling_program()
    program[0]["offSpeed"] = 6
    mock_client.get_advance_automations.return_value = program
    result = await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=540, end_time=180,
        max_level=3, dry_run=False,
    )
    data = json.loads(result)
    assert "minimum speed can't be higher than the maximum" in data["error"]
    mock_client.update_advance_automation.assert_not_called()


async def test_update_rule_inverted_live_speeds_dont_block_unrelated_edit(mock_client):
    """A rule whose live speeds are already inverted (external write) is still editable when
    the edit doesn't touch speed — the inversion guard only fires when a level is supplied."""
    program = _seedling_program()
    program[0]["offSpeed"] = 9
    program[0]["onSpeed"] = 4   # already inverted, not by this tool
    mock_client.get_advance_automations.return_value = program
    result = await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=540, end_time=180,
        vpd_transition=0.3, dry_run=False,
    )
    data = json.loads(result)
    assert "error" not in data
    mock_client.update_advance_automation.assert_called_once()


# ---- M3: trigger thresholds on their inactive rail are rejected (lossy round-trip) ----


@pytest.mark.parametrize("mode,style,kwargs", [
    ("auto", "trigger", {"humidity_high": 100}),
    ("auto", "trigger", {"humidity_low": 0}),
    ("auto", "trigger", {"temp_low_f": 32}),
    ("auto", "trigger", {"temp_high_f": 194}),
    ("vpd", "trigger", {"vpd_high": 9.9}),
    ("vpd", "trigger", {"vpd_low": 0.0}),
])
async def test_add_rule_trigger_on_rail_rejected(mock_client, mode, style, kwargs):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [1], mode, control_style=style, dry_run=True, **kwargs,
    )
    assert "to trigger" in json.loads(result)["error"]
    mock_client.create_advance_automation.assert_not_called()


async def test_add_rule_trigger_just_inside_rail_allowed(mock_client):
    """One unit inside the rail is accepted (proves the guard isn't off-by-one)."""
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [1], "auto", control_style="trigger",
        humidity_high=99, dry_run=True,
    )
    data = json.loads(result)
    assert "error" not in data
    assert "humidity: on above 99%" in data["rule"]["control"]


async def test_update_rule_trigger_on_rail_rejected(mock_client):
    """The rail guard is enforced on the update path too (not just add)."""
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await update_automation_rule(
        "C58ZA", "Seedling", [2], begin_time=540, end_time=180,
        mode="auto", control_style="trigger", humidity_high=100, dry_run=True,
    )
    assert "to trigger" in json.loads(result)["error"]
    mock_client.update_advance_automation.assert_not_called()


@pytest.mark.parametrize("kwargs", [
    {"humidity_target": 0},   # _RAIL_TARGET_HUMI (temp_target_f is rejected outright, #291)
])
async def test_add_rule_target_on_rail_rejected(mock_client, kwargs):
    """A target sitting on its inactive rail decodes back as 'no rule set' → rejected."""
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [1], "auto", control_style="target", dry_run=True, **kwargs,
    )
    assert "to hold" in json.loads(result)["error"]
    mock_client.create_advance_automation.assert_not_called()


async def test_add_rule_target_just_inside_rail_allowed(mock_client):
    """One unit above the target rail is accepted and round-trips."""
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [1], "auto", control_style="target",
        humidity_target=1, dry_run=True,
    )
    data = json.loads(result)
    assert "error" not in data
    assert "humidity: hold at 1%" in data["rule"]["control"]


async def test_add_rule_invalid_string_day_token_rejected(mock_client):
    """A bad scalar-string day (not a list) is rejected (covers the string-branch _err)."""
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [1], "on", days="funday", dry_run=True,
    )
    assert "days must be" in json.loads(result)["error"]
    mock_client.create_advance_automation.assert_not_called()


async def test_update_rule_continuous_false_with_mode_change_preserves_clear(mock_client):
    """continuous=False alongside a mode change still clears bit7 (rebuild doesn't clobber it)."""
    program = _seedling_program()
    program[0]["switchTime"] = 255
    mock_client.get_advance_automations.return_value = program
    await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=540, end_time=180,
        mode="on", continuous=False, dry_run=False,
    )
    sent = mock_client.update_advance_automation.call_args.args[1]
    assert sent["currentMode"] == 1     # mode change applied
    assert sent["switchTime"] == 127    # continuous bit still cleared after rebuild


# ---- days list: empty rejected, mixed valid/invalid rejected ----


async def test_add_rule_empty_days_list_rejected(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [1], "on", days=[], dry_run=True,
    )
    assert "days can't be empty" in json.loads(result)["error"]
    mock_client.create_advance_automation.assert_not_called()


async def test_add_rule_mixed_valid_invalid_days_rejected(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [1], "on", days=["mon", "funday"], dry_run=True,
    )
    assert "days must be" in json.loads(result)["error"]
    mock_client.create_advance_automation.assert_not_called()


# ---- fix #4 / #285: same-mode style mismatch rejected (both directions) ----


async def test_update_rule_same_mode_threshold_on_target_rule_rejected(mock_client):
    """Same-mode edit: supplying a trigger threshold on a humidity-TARGET rule (without
    restating mode) is rejected with the mutually-exclusive message (closes #285)."""
    program = _seedling_program()
    mock_client.get_advance_automations.return_value = program
    result = await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=180, end_time=540,
        humidity_high=70, dry_run=True,
    )
    err = json.loads(result)["error"]
    assert "target" in err and "threshold" in err
    mock_client.update_advance_automation.assert_not_called()


async def test_update_rule_same_mode_target_on_trigger_rule_rejected(mock_client):
    """Same-mode edit: supplying a target on a temperature-TRIGGER rule is rejected."""
    program = _seedling_program()
    mock_client.get_advance_automations.return_value = program
    result = await update_automation_rule(
        "C58ZA", "Seedling", [2], humidity_target=60, dry_run=True,
    )
    err = json.loads(result)["error"]
    assert "target" in err and "threshold" in err
    mock_client.update_advance_automation.assert_not_called()


async def test_update_rule_same_mode_vpd_target_on_trigger_rejected(mock_client):
    """Same-mode VPD edit: supplying vpd_target on a VPD-trigger rule is rejected."""
    program = _seedling_program()
    # Make the port-2 rule a VPD-trigger rule.
    program[2] = {**copy.deepcopy(MOCK_RULE_VPD), "advName": "Seedling",
                  "grouptDevType": 2, "beginTime": 540, "endTime": 180, "advId": 5003,
                  "groupNums": 1, "sortType": 6, "subNumber": 2, "subNumberSort": 2,
                  "settingMode": 0, "setSelect": 0, "highVpd": 15, "highVpdSwitch": 1,
                  "lowVpd": 8, "lowVpdSwitch": 1, "targetVpd": 0}
    mock_client.get_advance_automations.return_value = program
    result = await update_automation_rule(
        "C58ZA", "Seedling", [2], vpd_target=1.0, dry_run=True,
    )
    err = json.loads(result)["error"]
    assert "cannot take a target" in err
    mock_client.update_advance_automation.assert_not_called()


# ---- same-mode overlay branches (each carries the new value to the sent body) ----


@pytest.mark.parametrize("port,window,kwargs,field,expected", [
    # port-2 auto-trigger rule (temp on_below 76)
    (2, (540, 180), dict(temp_low_f=58), "autoLowTempF", 58),
    (2, (540, 180), dict(humidity_high=72), "autoHighHumi", 72),
    (2, (540, 180), dict(temp_buffer=3), "temperatureFBuff", 3),
    (2, (540, 180), dict(temp_transition=2), "temperatureFTrans", 2),
    (2, (540, 180), dict(humidity_buffer=5), "humidityBuff", 5),
    (2, (540, 180), dict(humidity_transition=4), "humidityTrans", 4),
    # port-1 humidity-target rule (temp target is unsupported, #291 — use humidity target)
    (1, (180, 540), dict(humidity_target=70), "targetHumi", 70),
])
async def test_update_rule_same_mode_overlay_carries_value(
    mock_client, port, window, kwargs, field, expected
):
    program = _seedling_program()
    mock_client.get_advance_automations.return_value = program
    await update_automation_rule(
        "C58ZA", "Seedling", [port], begin_time=window[0], end_time=window[1],
        dry_run=False, **kwargs,
    )
    sent = mock_client.update_advance_automation.call_args.args[1]
    assert sent[field] == expected


async def test_update_rule_same_mode_vpd_trigger_overlay_carries_value(mock_client):
    """Same-mode VPD-trigger edit: vpd_high/vpd_low overlay onto the live body."""
    program = _seedling_program()
    program[2] = {**copy.deepcopy(MOCK_RULE_VPD), "advName": "Seedling",
                  "grouptDevType": 2, "beginTime": 540, "endTime": 180, "advId": 5003,
                  "groupNums": 1, "sortType": 6, "subNumber": 2, "subNumberSort": 2,
                  "settingMode": 0, "setSelect": 0, "highVpd": 15, "highVpdSwitch": 1,
                  "lowVpd": 8, "lowVpdSwitch": 1, "targetVpd": 0}
    mock_client.get_advance_automations.return_value = program
    await update_automation_rule(
        "C58ZA", "Seedling", [2], begin_time=540, end_time=180,
        vpd_high=1.6, vpd_low=0.7, dry_run=False,
    )
    sent = mock_client.update_advance_automation.call_args.args[1]
    assert sent["highVpd"] == 16
    assert sent["lowVpd"] == 7


async def test_update_rule_vpd_buffer_exposed_same_mode(mock_client):
    """vpd_buffer is a real update param: it overlays onto vpdBuff for a same-mode VPD edit."""
    program = _seedling_program()
    mock_client.get_advance_automations.return_value = program
    # port-1 begin=540/end=180 is the VPD-target rule (advId 5001).
    await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=540, end_time=180,
        vpd_buffer=0.3, dry_run=False,
    )
    sent = mock_client.update_advance_automation.call_args.args[1]
    assert sent["vpdBuff"] == 3


# ---- fix #7: On-mode renders a single speed, not a range ----


async def test_add_on_rule_renders_single_speed_not_range(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [1], "on", min_level=2, max_level=7, dry_run=True,
    )
    ctrl = json.loads(result)["rule"]["control"]
    assert "speed 7" in ctrl
    assert "speed 2–7" not in ctrl
    assert "–7" not in ctrl


# ---- delete_automation_rule ----


async def test_delete_rule_dry_run_no_write(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await delete_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=180, end_time=540, dry_run=True,
    )
    data = json.loads(result)
    assert data["dry_run"] is True
    assert "humidity: hold at 65%" in data["rule"]["control"]
    mock_client.delete_advance_automation.assert_not_called()


async def test_delete_rule_disambiguation_no_advid(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await delete_automation_rule("C58ZA", "Seedling", [1], dry_run=True)
    data = json.loads(result)
    assert "matching_rules" in data
    assert "advId" not in json.dumps(data)
    mock_client.delete_advance_automation.assert_not_called()


async def test_delete_rule_live_deletes_single_advid(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await delete_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=180, end_time=540, dry_run=False,
    )
    data = json.loads(result)
    assert data["sent"] is True
    mock_client.delete_advance_automation.assert_called_once()
    assert mock_client.delete_advance_automation.call_args.args[1] == 5002
    # Must delete ONLY this rule (isflag=0), NOT the whole program slot (isflag=1).
    assert mock_client.delete_advance_automation.call_args.kwargs.get("whole_program") is False


async def test_delete_rule_wedged_friendly_no_upstream_echo(mock_client):
    """A wedged-delete (upstream code 100001) maps to a self-authored friendly message."""
    mock_client.get_advance_automations.return_value = _seedling_program()
    mock_client.delete_advance_automation.side_effect = ACInfinityAPIError(
        "deleteGroups API error 100001: busy"
    )
    result = await delete_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=180, end_time=540, dry_run=False,
    )
    data = json.loads(result)
    assert "may or may not have applied" in data["error"]
    assert "list the program's rules" in data["error"]   # steer to read-back, not blind retry
    assert "100001" not in result


async def test_delete_rule_more_than_one_program_same_name_rejected(mock_client):
    """G1: a name mapping to >1 distinct slot is ambiguous for delete too — refuse, no write."""
    program = _seedling_program()
    program[2]["groupNums"] = 2  # second distinct slot under the same advName
    mock_client.get_advance_automations.return_value = program
    result = await delete_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=180, end_time=540, dry_run=False,
    )
    data = json.loads(result)
    assert "More than one program" in data["error"]
    mock_client.delete_advance_automation.assert_not_called()


# ---- two-window no false conflict ----


async def test_add_second_window_same_port_no_conflict(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [1], "on", max_level=3, begin_time=0, end_time=120, dry_run=True,
    )
    data = json.loads(result)
    assert "conflict" not in data and "error" not in data
    assert data["dry_run"] is True


# ---- get_advance_automation per-rule read parity ----


async def test_get_advance_automation_rules_array_parity(mock_client):
    program = _seedling_program()
    mock_client.get_advance_automations.return_value = program
    result = await get_advance_automation("C58ZA", "5001")
    data = json.loads(result)
    assert "rules" in data
    controls = " || ".join(r["control"] for r in data["rules"])
    assert "VPD: hold at 0.9 kPa" in controls
    assert "humidity: hold at 65%" in controls
    assert any("America/Chicago" in r["window"] for r in data["rules"])
    assert all("_mode" in r for r in data["rules"])


# ============ Issue #284 — _validate_rule_inputs range / required-param coverage ============


def _vri(mode, *, require_full=True, **kwargs):
    return _validate_rule_inputs(mode, require_full=require_full, **kwargs)


_VRI_REJECT_CASES = [
    ("banana", {}, True, "mode must be one of"),
    # level range / ordering
    ("on", {"min_level": 11}, True, "min_level must be 0–10"),
    ("on", {"max_level": 11}, True, "max_level must be 0–10"),
    ("on", {"min_level": 8, "max_level": 3}, True, "less than or equal"),
    # control_style enum
    ("auto", {"control_style": "sideways", "temp_high_f": 80}, True,
     "control_style must be one of"),
    # auto required control_style
    ("auto", {"temp_high_f": 80}, True, "control_style"),
    # auto temp range / upper bound
    ("auto", {"control_style": "trigger", "temp_high_f": 213}, True, "32–212"),
    ("auto", {"control_style": "trigger", "temp_low_f": 31}, True, "32–212"),
    # auto temp ordering
    ("auto", {"control_style": "trigger", "temp_low_f": 90, "temp_high_f": 80}, True,
     "temp_low_f must be less than"),
    # auto humidity range
    ("auto", {"control_style": "trigger", "humidity_high": 101}, True, "0–100"),
    ("auto", {"control_style": "trigger", "humidity_low": 80, "humidity_high": 70}, True,
     "humidity_low must be less than"),
    # auto buffer XOR transition
    ("auto", {"control_style": "trigger", "temp_high_f": 80, "temp_buffer": 3,
              "temp_transition": 2}, True, "buffer or a transition"),
    # auto target/trigger mutual exclusion (humidity; temp target is rejected outright, #291)
    ("auto", {"control_style": "trigger", "humidity_target": 60, "humidity_high": 80}, True,
     "pick one"),
    # temperature target is unsupported (#291)
    ("auto", {"control_style": "target", "temp_target_f": 72}, True, "isn't supported"),
    # auto trigger with no threshold
    ("auto", {"control_style": "trigger"}, True, "at least one"),
    # auto target with no target
    ("auto", {"control_style": "target"}, True, "needs a temperature or humidity target"),
    # vpd range / upper bound
    ("vpd", {"control_style": "target", "vpd_target": 10.0}, True, "0.0–9.9"),
    ("vpd", {"control_style": "trigger", "vpd_high": -0.1}, True, "0.0–9.9"),
    # vpd ordering
    ("vpd", {"control_style": "trigger", "vpd_low": 2.0, "vpd_high": 1.0}, True,
     "vpd_low must be less than"),
    # vpd target/trigger exclusion
    ("vpd", {"control_style": "trigger", "vpd_target": 1.0, "vpd_high": 1.5}, True, "pick one"),
    # vpd required
    ("vpd", {"control_style": "target"}, True, "needs vpd_target"),
    # cycle range / required
    ("cycle", {"cycle_on_minutes": 1440, "cycle_off_minutes": 5}, True, "cycle on-minutes"),
    ("cycle", {"cycle_on_minutes": 30}, True, "needs both an on-minutes"),
    # cross-mode param rejection
    ("on", {"cycle_on_minutes": 30}, True, "does not apply to a on rule"),
    # bad days token
    ("on", {"days": ["funday"]}, True, "days must be"),
]


@pytest.mark.parametrize(
    "mode,kwargs,require_full,err_sub", _VRI_REJECT_CASES,
    ids=[f"{c[0]}-{c[3][:18]}" for c in _VRI_REJECT_CASES],
)
def test_validate_rule_inputs_reject(mode, kwargs, require_full, err_sub):
    out, err = _vri(mode, require_full=require_full, **kwargs)
    assert out is None, f"expected reject for {mode} {kwargs}"
    assert err is not None
    assert err_sub in json.loads(err)["error"]


_VRI_PASS_CASES = [
    ("off", {}, True),
    ("on", {"min_level": 0, "max_level": 10}, True),
    ("cycle", {"cycle_on_minutes": 30, "cycle_off_minutes": 15}, True),
    ("auto", {"control_style": "trigger", "temp_high_f": 80, "humidity_low": 50}, True),
    ("auto", {"control_style": "target", "humidity_target": 65}, True),
    ("vpd", {"control_style": "target", "vpd_target": 1.2}, True),
    ("vpd", {"control_style": "trigger", "vpd_high": 1.5, "vpd_low": 0.8}, True),
    # boundary: min == max allowed
    ("on", {"min_level": 5, "max_level": 5}, True),
    ("on", {"min_level": 0, "max_level": 0}, True),
    ("on", {"min_level": 10, "max_level": 10}, True),
    # update path: partial params allowed
    ("auto", {"temp_high_f": 80}, False),
    ("vpd", {}, False),
    ("on", {"days": "weekdays"}, True),
]


@pytest.mark.parametrize("mode,kwargs,require_full", _VRI_PASS_CASES)
def test_validate_rule_inputs_pass(mode, kwargs, require_full):
    out, err = _vri(mode, require_full=require_full, **kwargs)
    assert err is None, f"unexpected reject: {err}"
    assert out is not None


# ---- end-to-end: out-of-range rejected at tool boundary, no write ----


async def test_add_rule_out_of_range_level_no_write(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await add_automation_rule(
        "C58ZA", "Seedling", [1], "on", max_level=99, dry_run=False,
    )
    assert "max_level must be 0–10" in json.loads(result)["error"]
    mock_client.create_advance_automation.assert_not_called()


async def test_update_rule_out_of_range_vpd_no_write(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=540, end_time=180,
        mode="vpd", control_style="target", vpd_target=42.0, dry_run=False,
    )
    assert "0.0–9.9" in json.loads(result)["error"]
    mock_client.update_advance_automation.assert_not_called()


async def test_delete_rule_out_of_range_begin_time_no_write(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    result = await delete_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=999, end_time=998, dry_run=False,
    )
    assert "error" in json.loads(result)
    mock_client.delete_advance_automation.assert_not_called()


# ============ Issue #284 — same-mode in-place overlay edits ============


async def test_update_rule_same_mode_auto_trigger_thresholds(mock_client):
    """auto-trigger rule, no mode change: temp/humidity thresholds overlay + activate switches."""
    program = _seedling_program()
    mock_client.get_advance_automations.return_value = program
    await update_automation_rule(
        "C58ZA", "Seedling", [2], begin_time=540, end_time=180,
        temp_high_f=85, humidity_low=45, new_begin_time=600, new_end_time=240, dry_run=False,
    )
    sent = mock_client.update_advance_automation.call_args.args[1]
    assert sent["autoHighTempF"] == 85
    assert sent["autoHighTempSwitch"] == 1
    assert sent["autoLowHumi"] == 45
    assert sent["autoLowHumiSwitch"] == 1
    assert sent["currentMode"] == 4
    assert sent["beginTime"] == 600
    assert sent["endTime"] == 240


async def test_update_rule_same_mode_cycle_targets(mock_client):
    program = _seedling_program()
    program[2] = {**program[2], "currentMode": 3, "cycleOn": 10, "cycleOff": 5}
    mock_client.get_advance_automations.return_value = program
    await update_automation_rule(
        "C58ZA", "Seedling", [2], begin_time=540, end_time=180,
        cycle_on_minutes=45, cycle_off_minutes=20, dry_run=False,
    )
    sent = mock_client.update_advance_automation.call_args.args[1]
    # cycleOn/cycleOff stored in SECONDS (minutes × 60).
    assert sent["cycleOn"] == 2700
    assert sent["cycleOff"] == 1200
    assert sent["currentMode"] == 3


async def test_update_rule_same_mode_auto_target_humidity(mock_client):
    program = _seedling_program()
    mock_client.get_advance_automations.return_value = program
    await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=180, end_time=540,
        humidity_target=72, dry_run=False,
    )
    sent = mock_client.update_advance_automation.call_args.args[1]
    assert sent["targetHumi"] == 72
    assert "humidity: hold at 72%" in _decode_rule(sent)["control"]


async def test_update_rule_same_mode_vpd_target(mock_client):
    program = _seedling_program()
    mock_client.get_advance_automations.return_value = program
    await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=540, end_time=180, vpd_target=1.3, dry_run=False,
    )
    sent = mock_client.update_advance_automation.call_args.args[1]
    assert sent["targetVpd"] == 13
    assert sent["highVpd"] == 13       # mirror must track the setpoint (no targetVpd != highVpd)
    assert sent["highVpdSwitch"] == 1
    assert sent["lowVpdSwitch"] == 0
    assert sent["currentMode"] == 6
    assert "VPD: hold at 1.3 kPa" in _decode_rule(sent)["control"]


# ============ Issue #284 — auth / API / write-failure error paths ============


async def test_add_rule_auth_error(mock_client):
    mock_client.get_advance_automations.side_effect = ACInfinityAuthError("token expired xyz")
    result = await add_automation_rule("C58ZA", "Seedling", [1], "on", dry_run=False)
    data = json.loads(result)
    assert "Authentication failed — check AC_INFINITY_EMAIL" in data["error"]
    assert "token expired xyz" not in result
    mock_client.create_advance_automation.assert_not_called()


async def test_add_rule_api_error(mock_client):
    mock_client.get_advance_automations.side_effect = ACInfinityAPIError("503 boom internal")
    result = await add_automation_rule("C58ZA", "Seedling", [1], "on", dry_run=False)
    data = json.loads(result)
    assert data["error"] == "API error"
    assert "503 boom internal" not in result


async def test_add_rule_write_method_failure(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    mock_client.create_advance_automation.side_effect = ACInfinityAPIError("write failed deep")
    result = await add_automation_rule("C58ZA", "Seedling", [1], "on", max_level=4, dry_run=False)
    data = json.loads(result)
    assert data["error"] == "API error"
    assert "write failed deep" not in result


async def test_update_rule_auth_error(mock_client):
    mock_client.get_advance_automations.side_effect = ACInfinityAuthError("token expired xyz")
    result = await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=180, end_time=540, max_level=4, dry_run=False,
    )
    data = json.loads(result)
    assert "Authentication failed — check AC_INFINITY_EMAIL" in data["error"]
    assert "token expired xyz" not in result
    mock_client.update_advance_automation.assert_not_called()


async def test_update_rule_api_error(mock_client):
    mock_client.get_advance_automations.side_effect = ACInfinityAPIError("503 boom internal")
    result = await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=180, end_time=540, max_level=4, dry_run=False,
    )
    assert json.loads(result)["error"] == "API error"
    assert "503 boom internal" not in result


async def test_update_rule_write_method_failure(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    mock_client.update_advance_automation.side_effect = ACInfinityAPIError("write failed deep")
    result = await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=180, end_time=540, max_level=4, dry_run=False,
    )
    assert json.loads(result)["error"] == "API error"
    assert "write failed deep" not in result


async def test_delete_rule_auth_error(mock_client):
    mock_client.get_advance_automations.side_effect = ACInfinityAuthError("token expired xyz")
    result = await delete_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=180, end_time=540, dry_run=False,
    )
    data = json.loads(result)
    assert "Authentication failed — check AC_INFINITY_EMAIL" in data["error"]
    assert "token expired xyz" not in result
    mock_client.delete_advance_automation.assert_not_called()


async def test_delete_rule_api_error(mock_client):
    mock_client.get_advance_automations.side_effect = ACInfinityAPIError("503 boom internal")
    result = await delete_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=180, end_time=540, dry_run=False,
    )
    assert json.loads(result)["error"] == "API error"
    assert "503 boom internal" not in result


async def test_delete_rule_write_method_failure(mock_client):
    mock_client.get_advance_automations.return_value = _seedling_program()
    mock_client.delete_advance_automation.side_effect = ACInfinityAPIError("write failed deep")
    result = await delete_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=180, end_time=540, dry_run=False,
    )
    assert json.loads(result)["error"] == "API error"
    assert "write failed deep" not in result


# ============ Issue #284 — stale-advId write-time re-resolve guard ============


async def test_update_rule_stale_advid_guard_blocks_write(mock_client):
    program = _seedling_program()
    program_after = [e for e in _seedling_program() if e["advId"] != 5002]
    mock_client.get_advance_automations.side_effect = [program, program_after]
    result = await update_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=180, end_time=540, max_level=4, dry_run=False,
    )
    assert "changed or was removed" in json.loads(result)["error"]
    mock_client.update_advance_automation.assert_not_called()


async def test_delete_rule_stale_advid_guard_blocks_write(mock_client):
    program = _seedling_program()
    program_after = [e for e in _seedling_program() if e["advId"] != 5002]
    mock_client.get_advance_automations.side_effect = [program, program_after]
    result = await delete_automation_rule(
        "C58ZA", "Seedling", [1], begin_time=180, end_time=540, dry_run=False,
    )
    assert "changed or was removed" in json.loads(result)["error"]
    mock_client.delete_advance_automation.assert_not_called()


# ============ Issue #284 — create_advance_automation compositional surface ============


async def test_create_advance_automation_on_byte_identity_preserved(mock_client):
    """Legacy On-mode create still emits onSpeed=on_speed / offSpeed=0 (byte path)."""
    result = await create_advance_automation("C58ZA", "Night", 5, 1, dry_run=False)
    data = json.loads(result)
    assert data["sent"] is True
    sent = mock_client.create_advance_automation.call_args.args[1]
    assert sent["currentMode"] == 1
    assert sent["onSpeed"] == 5
    assert sent["offSpeed"] == 0


async def test_create_advance_automation_auto_target(mock_client):
    result = await create_advance_automation(
        "C58ZA", "Auto", 8, 1, mode="auto", control_style="target",
        humidity_target=60, dry_run=False,
    )
    assert json.loads(result)["sent"] is True
    sent = mock_client.create_advance_automation.call_args.args[1]
    assert sent["currentMode"] == 4
    assert sent["settingMode"] == 1
    assert sent["targetHumi"] == 60
    assert sent["onSpeed"] == 8


async def test_create_advance_automation_dry_run_no_write(mock_client):
    result = await create_advance_automation(
        "C58ZA", "Auto", 8, 1, mode="vpd", control_style="target", vpd_target=1.1, dry_run=True,
    )
    assert json.loads(result)["dry_run"] is True
    mock_client.create_advance_automation.assert_not_called()


# ============ _format_probes / _format_probe_clause ============
#
# These were previously executed zero times across the whole suite — only ever
# called with [] — so the F/C rendering they exist for was entirely unverified.

_PROBE_C = {"sensor_port": 2, "temperature_c": 19.1, "humidity_pct": 81.6, "vpd_kpa": 0.39}


def test_format_probes_empty():
    assert _format_probes([], "F") == []
    assert _format_probes([], "C") == []


def test_format_probes_fahrenheit_device():
    """19.1 C renders as 66.4 F on a Fahrenheit device."""
    out = _format_probes([_PROBE_C], "F")
    assert out == [{
        "sensor_port": 2, "temperature": 66.4, "unit": "°F",
        "humidity": 81.6, "vpd": 0.39,
    }]


def test_format_probes_celsius_device():
    """The stored Celsius value passes through unconverted on a Celsius device."""
    out = _format_probes([_PROBE_C], "C")
    assert out == [{
        "sensor_port": 2, "temperature": 19.1, "unit": "°C",
        "humidity": 81.6, "vpd": 0.39,
    }]


def test_format_probes_multiple_preserves_order():
    probes = [
        {"sensor_port": 2, "temperature_c": 19.1, "humidity_pct": 81.6, "vpd_kpa": 0.39},
        {"sensor_port": 4, "temperature_c": 21.0, "humidity_pct": 55.0, "vpd_kpa": 1.10},
    ]
    out = _format_probes(probes, "C")
    assert [p["sensor_port"] for p in out] == [2, 4]
    assert out[1]["temperature"] == 21.0


def test_format_probe_clause_empty_is_blank():
    """No probes must leave the summary byte-identical to the pre-probe output."""
    assert _format_probe_clause([]) == ""


def test_format_probe_clause_single():
    clause = _format_probe_clause(_format_probes([_PROBE_C], "F"))
    assert clause == "Probe Sensor (Sensor Port 2): 66.4°F, 81.6% RH, VPD 0.39 kPa"


def test_format_probe_clause_multiple():
    probes = _format_probes([
        {"sensor_port": 2, "temperature_c": 19.1, "humidity_pct": 81.6, "vpd_kpa": 0.39},
        {"sensor_port": 4, "temperature_c": 21.0, "humidity_pct": 55.0, "vpd_kpa": 1.10},
    ], "C")
    clause = _format_probe_clause(probes)
    assert "Sensor Port 2" in clause and "Sensor Port 4" in clause
    assert clause.count("Probe Sensor") == 2


# ============ probes surfaced through the tool layer ============

_SERVER_PROBE = {"sensor_port": 2, "temperature_c": 19.1, "humidity_pct": 81.6, "vpd_kpa": 0.39}


async def test_get_device_reading_includes_probes(mock_client):
    """probes reaches the tool JSON, unit-converted like every sibling reading."""
    mock_client.parse_device_data.return_value["probes"] = [dict(_SERVER_PROBE)]
    data = json.loads(await get_device_reading("C58ZA"))
    assert data["probes"] == [{
        "sensor_port": 2, "temperature": 19.1, "unit": "°C",
        "humidity": 81.6, "vpd": 0.39,
    }]


async def test_get_device_reading_probe_in_human_summary(mock_client):
    """A grower asking how it looks should HEAR the probe, not just find it in JSON."""
    mock_client.parse_device_data.return_value["probes"] = [dict(_SERVER_PROBE)]
    data = json.loads(await get_device_reading("C58ZA"))
    assert "Probe Sensor (Sensor Port 2): 19.1°C, 81.6% RH, VPD 0.39 kPa" in data["human_summary"]


async def test_get_device_reading_summary_unchanged_without_probes(mock_client):
    """No probe attached must leave the summary byte-identical to before."""
    data = json.loads(await get_device_reading("C58ZA"))
    assert data["probes"] == []
    assert "Probe Sensor" not in data["human_summary"]


async def test_get_all_device_readings_includes_probes(mock_client):
    mock_client.parse_device_data.return_value["probes"] = [dict(_SERVER_PROBE)]
    data = json.loads(await get_all_device_readings())
    assert data["readings"][0]["probes"][0]["sensor_port"] == 2
    assert "Probe Sensor (Sensor Port 2)" in data["human_summary"]


async def test_get_all_device_readings_summary_unchanged_without_probes(mock_client):
    data = json.loads(await get_all_device_readings())
    assert data["readings"][0]["probes"] == []
    assert "Probe Sensor" not in data["human_summary"]


# ============ AI+ holds (#316) ============
#
# #308 enabled AI+ writes generally but deliberately held two tools back. Both
# write field combinations whose persistence on AI+ is unproven, and AI+
# accepts mode-irrelevant fields with code 200 and silently discards them
# (Quirk 36) — so reporting sent=true would be misleading rather than merely
# incomplete. These tests pin the hold so it cannot be dropped by accident.

_AI_PLUS_DEVICE_FOR_HOLD = {
    **copy.deepcopy(MOCK_DEVICE_LEGACY),
    "devCode": "D89XA",
    "devType": 20,
    "newFrameworkDevice": True,
}


async def test_apply_grow_stage_template_held_on_ai_plus(mock_client):
    """Live apply_grow_stage_template must refuse on AI+ rather than half-apply."""
    mock_client.get_devices.return_value = [_AI_PLUS_DEVICE_FOR_HOLD]
    result = await apply_grow_stage_template("D89XA", 1, "veg", dry_run=False)
    data = json.loads(result)
    assert "error" in data
    assert "AI+" in data["error"]
    assert data["controller_type"] == "new_framework"
    assert data["tracking_issue"] == 316
    mock_client.set_port_mode.assert_not_called()


async def test_apply_grow_stage_template_preview_still_works_on_ai_plus(mock_client):
    """The hold is live-write only — dry_run previews stay available."""
    mock_client.get_devices.return_value = [_AI_PLUS_DEVICE_FOR_HOLD]
    mock_client.set_port_mode.return_value = {
        "payload": {}, "dry_run": True, "controller_type": "new_framework", "sent": False,
    }
    result = await apply_grow_stage_template("D89XA", 1, "veg", dry_run=True)
    data = json.loads(result)
    assert "error" not in data
    mock_client.set_port_mode.assert_called_once()


async def test_apply_grow_stage_template_not_held_on_legacy(mock_client):
    """Legacy controllers are unaffected by the AI+ hold."""
    mock_client.set_port_mode.return_value = {
        "payload": {}, "dry_run": False, "controller_type": "legacy", "sent": True,
    }
    result = await apply_grow_stage_template("C58ZA", 1, "veg", dry_run=False)
    data = json.loads(result)
    assert "tracking_issue" not in data
    mock_client.set_port_mode.assert_called_once()


async def test_break_out_of_automation_held_on_ai_plus(mock_client):
    """break_out_of_automation must refuse on AI+ before any co-port write."""
    mock_client.get_devices.return_value = [_AI_PLUS_DEVICE_FOR_HOLD]
    result = await break_out_of_automation(
        "D89XA", 1, dry_run=False, confirm_automation_name="Test Automation"
    )
    data = json.loads(result)
    assert "error" in data
    assert "AI+" in data["error"]
    assert data["tracking_issue"] == 316
    mock_client.set_port_mode.assert_not_called()
# ============ Temperature trigger resolution (Quirk 37) ============
#
# The API stores each temperature trigger twice: devLt/devHt in °C and
# devLtf/devHtf in °F. Which pair is real depends on the controller. Captured
# from live hardware:
#
#   devType 11 (JNFZA, °F display)  port 1: devLt=10  devHt=27  devLtf=50 devHtf=80
#   devType 20 (SPEGQ, °F display)  port 2: devLt=0   devHt=0   devLtf=32 devHtf=80
#
# Both pairs agree on legacy; the AI+ leaves °C at zero. Reading °C
# unconditionally reported every AI+ trigger as 32.0–32.0°F, and the
# human_summary stated it as fact ("Fan speeds up above 32.0°F").


def test_resolve_temp_trigger_ai_plus_reads_the_f_pair():
    """AI+ leaves devLt/devHt at 0; the real trigger is in devLtf/devHtf.

    Live capture: SPEGQ port 2 (Exhaust) has a genuine 80°F high trigger.
    """
    settings = {"devLt": 0, "devHt": 0, "devLtf": 32, "devHtf": 80}
    assert _resolve_temp_trigger(settings, "F") == (32.0, 80.0)


def test_resolve_temp_trigger_legacy_prefers_stored_f_over_converting_c():
    """Both pairs are populated on legacy; the display-unit pair is exact.

    Live capture: JNFZA port 1 stores 27 °C and 80 °F for the same trigger.
    Converting the °C value would report 80.6 °F — off by more than the
    grower's own setting.
    """
    settings = {"devLt": 10, "devHt": 27, "devLtf": 50, "devHtf": 80}
    assert _resolve_temp_trigger(settings, "F") == (50.0, 80.0)


def test_resolve_temp_trigger_celsius_device_prefers_stored_c():
    """On a °C device the °C pair is what the grower set, so use it directly."""
    settings = {"devLt": 10, "devHt": 27, "devLtf": 50, "devHtf": 80}
    assert _resolve_temp_trigger(settings, "C") == (10.0, 27.0)


def test_resolve_temp_trigger_celsius_device_falls_back_when_c_unset():
    """An AI+ set to °C has no usable °C pair — convert from °F rather than 0."""
    settings = {"devLt": 0, "devHt": 0, "devLtf": 32, "devHtf": 80}
    assert _resolve_temp_trigger(settings, "C") == (0.0, 26.7)


def test_resolve_temp_trigger_unset_ai_plus_range():
    """(32, 32) °F is the unset default and must not fall through to the °C pair."""
    settings = {"devLt": 0, "devHt": 0, "devLtf": 32, "devHtf": 32}
    assert _resolve_temp_trigger(settings, "F") == (32.0, 32.0)


def test_resolve_temp_trigger_full_range_ai_plus():
    """The widest AI+ range (32–194 °F) survives intact."""
    settings = {"devLt": 0, "devHt": 0, "devLtf": 32, "devHtf": 194}
    assert _resolve_temp_trigger(settings, "F") == (32.0, 194.0)


def test_resolve_temp_trigger_f_pair_absent_converts_from_c():
    """Firmware that omits the °F pair entirely still reports a real range."""
    settings = {"devLt": 10, "devHt": 27}
    assert _resolve_temp_trigger(settings, "F") == (50.0, 80.6)


def test_resolve_temp_trigger_both_pairs_absent_is_safe():
    """Neither pair present: return the unset default rather than raising."""
    assert _resolve_temp_trigger({}, "F") == (32.0, 32.0)


async def test_get_port_settings_reports_real_ai_plus_temp_range(mock_client):
    """End-to-end: the AI+ Exhaust port must report 80 °F, not 32 °F.

    This is the whole bug. Before the fix this returned 32.0-32.0 and the
    human_summary asserted the fan sped up above 32 °F.
    """
    # The AI+ in question displays °F; the shared legacy fixture is °C.
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device.setdefault("deviceInfo", {})["unit"] = 0
    mock_client.get_devices.return_value = [device]
    mock_client.get_mode_settings.return_value = {
        **MOCK_MODE_SETTINGS_BASIC,
        "atType": 3,
        "devLt": 0, "devHt": 0,
        "devLtf": 32, "devHtf": 80,
        "activeLt": 0, "activeHt": 1,
    }
    result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert data["temp_range"] == {"min": 32.0, "max": 80.0, "unit": "°F"}
    assert "80.0" in data["human_summary"]
    assert "32.0–32.0" not in data["human_summary"]


def test_resolve_temp_trigger_tolerates_string_typed_values():
    """The API is inconsistently typed elsewhere; coerce rather than assume."""
    settings = {"devLt": "0", "devHt": "0", "devLtf": "32", "devHtf": "80"}
    assert _resolve_temp_trigger(settings, "F") == (32.0, 80.0)


def test_resolve_temp_trigger_unparseable_pair_falls_back():
    """A garbage °F pair must not take precedence over a usable °C pair."""
    settings = {"devLt": 10, "devHt": 27, "devLtf": "n/a", "devHtf": None}
    assert _resolve_temp_trigger(settings, "F") == (50.0, 80.6)


# ============ human_summary must describe the active mode ============
#
# The summary used to be a first-match chain over stored thresholds
# (temp -> vpd -> humidity) that never consulted atType. Thresholds persist
# across mode changes, so this reported settings a port was not using, and
# dropped whichever family came second. Three failures observed live on a
# devType-20 controller:
#
#   port 3  OFF  + stale 82°F range   -> "Fan speeds up above 82.0°F"  (port is off)
#   port 2  AUTO + temp AND humidity  -> temperature only, humidity dropped
#   port 4  AUTO + humidity + stale
#           VPD target                -> "VPD automation", but humidity governs

_F_DEVICE = {"deviceInfo": {"unit": 0}}          # °F display


def _settings(**over):
    base = {
        **MOCK_MODE_SETTINGS_BASIC,
        "devLt": 0, "devHt": 0, "devLtf": 32, "devHtf": 32,
        "activeLt": 0, "activeHt": 0,
        "devLh": 0, "devHh": 100, "activeLh": 0, "activeHh": 0,
        "targetVpd": 0, "targetVpdSwitch": 0,
    }
    base.update(over)
    return base


async def _summary(mock_client, settings):
    device = copy.deepcopy(MOCK_DEVICE_LEGACY)
    device.setdefault("deviceInfo", {})["unit"] = 0
    mock_client.get_devices.return_value = [device]
    mock_client.get_mode_settings.return_value = settings
    return json.loads(await get_port_settings("C58ZA", 1))["human_summary"]


async def test_summary_off_port_does_not_claim_temperature_automation(mock_client):
    """The live port-3 case: OFF, but carrying an active 82°F trigger."""
    s = await _summary(mock_client, _settings(
        atType=1, devLtf=82, devHtf=82, activeLt=1))
    assert "Fan speeds up" not in s
    assert "Temperature automation" not in s
    assert "OFF mode" in s
    assert "not active" in s


async def test_summary_auto_reports_both_temperature_and_humidity(mock_client):
    """The live port-2 case: both families active; neither may be dropped."""
    s = await _summary(mock_client, _settings(
        atType=3, devHtf=80, activeHt=1, devLh=55, devHh=55, activeLh=1))
    assert "Temperature automation" in s
    assert "80.0" in s
    assert "Humidity automation" in s
    assert "55" in s


async def test_summary_auto_with_stale_vpd_reports_the_governing_family(mock_client):
    """The live port-4 case: AUTO + humidity floor, with a leftover VPD target.

    atType=3 means the humidity trigger governs. The stored targetVpd is from a
    previous VPD-mode configuration and must not be described as running.
    """
    s = await _summary(mock_client, _settings(
        atType=3, devLh=48, devHh=100, activeLh=1,
        targetVpd=12, targetVpdSwitch=1, vpdSettingMode=1))
    assert "Humidity automation: 48–100%." in s
    assert "VPD" not in s


async def test_summary_vpd_mode_reports_the_vpd_target(mock_client):
    """atType=8 is the mode where a VPD target actually governs."""
    s = await _summary(mock_client, _settings(
        atType=8, targetVpd=12, targetVpdSwitch=1, vpdSettingMode=1))
    assert s == "VPD automation: target 1.2 kPa."


async def test_summary_schedule_mode_flags_stored_settings_as_inactive(mock_client):
    """Stored thresholds are still worth surfacing — but as stored, not as live."""
    s = await _summary(mock_client, _settings(
        atType=7, devHtf=80, activeHt=1))
    assert "SCHEDULE mode" in s
    assert "not active" in s
    assert "Fan speeds up" not in s


async def test_summary_clean_off_port_says_only_the_mode(mock_client):
    """No stored config: no dangling caveat about settings that do not exist."""
    s = await _summary(mock_client, _settings(atType=1))
    assert s == "Port is in OFF mode."
