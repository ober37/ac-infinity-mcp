"""Unit tests for server.py async tools and helper functions."""

import asyncio
import json
from unittest.mock import patch

import pytest

from ac_infinity_mcp.schema import (
    ACInfinityAdvanceConflictError,
    ACInfinityAPIError,
    ACInfinityAuthError,
    ACInfinityDeviceError,
)
from ac_infinity_mcp.server import (
    _decode_mode,
    _filter_readings_by_time,
    _format_schedule_time,
    _parse_duration_seconds,
    _parse_schedule_time,
    _sanitize_api_string,
    apply_grow_stage_template,
    apply_sampling,
    average_readings,
    check_vpd_drift,
    detect_environment_trends,
    discover_devices,
    environment_alert_interpretation,
    get_all_device_readings,
    get_device_reading,
    get_environment_health,
    get_historical_readings,
    get_port_activity_report,
    get_port_settings,
    get_port_status,
    mcp_server,
    new_grower_setup,
    set_humidity_automation,
    set_port_mode,
    set_port_off,
    set_port_on,
    set_port_speed,
    set_temperature_automation,
    set_vpd_automation,
    vpd_troubleshooting,
)
from tests.conftest import MOCK_DEVICE_LEGACY


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


# ============ Smoke / symbol checks ============

def test_mcp_server_name():
    assert mcp_server.name == "ac-infinity-mcp"


# ============ discover_devices ============

async def test_discover_devices_success(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await discover_devices()
    data = json.loads(result)
    assert "devices" in data
    assert len(data["devices"]) == 1
    assert data["devices"][0]["device_id"] == "C58ZA"


async def test_discover_devices_empty(mock_client):
    mock_client.get_devices.return_value = []
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await discover_devices()
    data = json.loads(result)
    assert data["devices"] == []
    # The "No devices found" message is part of the documented contract;
    # regression removing it would have been invisible before (P2-F024).
    assert data["message"] == "No devices found"


async def test_discover_devices_api_error(mock_client):
    mock_client.get_devices.side_effect = ACInfinityAPIError("API error 500: server fault")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await discover_devices()
    data = json.loads(result)
    assert data["error"] == "AC Infinity API error"
    assert "detail" in data


async def test_discover_devices_auth_error(mock_client):
    mock_client.get_devices.side_effect = ACInfinityAuthError("Not authenticated")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await discover_devices()
    data = json.loads(result)
    by_id = {d["device_id"]: d for d in data["devices"]}
    assert by_id["A1"]["status"] == "online"
    assert by_id["B2"]["status"] == "offline"


async def test_discover_devices_client_not_initialized():
    with patch("ac_infinity_mcp.server.aci_client", None):
        result = await discover_devices()
    data = json.loads(result)
    assert "error" in data


async def test_discover_devices_includes_device_metadata(mock_client):
    """discover_devices must expose firmware_version, hardware_version, port_count, device_type."""
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await discover_devices()
    data = json.loads(result)
    device = data["devices"][0]
    assert device["device_type"] is None
    assert device["port_count"] is None
    assert device["firmware_version"] is None
    assert device["hardware_version"] is None


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
        with patch("ac_infinity_mcp.server.aci_client", mock_client):
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

    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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

    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
        with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
        with patch("ac_infinity_mcp.server.aci_client", mock_client):
            result = await tool(*args)
        data = json.loads(result)
        assert "error" in data, f"{tool_name}({bad_device_id!r}) should error, got {data}"
        # Bad device_id should produce a "not found" style error, not a traceback
        assert "Traceback" not in result
        assert "/Users/" not in result  # no local filesystem leakage


async def test_set_port_speed_negative_speed(mock_client):
    """Negative speed inputs should produce a structured validation error."""
    from ac_infinity_mcp.server import set_port_speed
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_speed("C58ZA", 1, -1)
    data = json.loads(result)
    assert "error" in data
    # Should not have attempted any client call
    mock_client.set_port_mode.assert_not_called()


# ============ get_device_reading ============

async def test_get_device_reading_success(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_device_reading("C58ZA")
    data = json.loads(result)
    assert data["device_id"] == "C58ZA"
    assert "temperature_c" in data
    assert "humidity" in data
    assert "vpd" in data


async def test_get_device_reading_device_not_found(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_device_reading("NOTEXIST")
    data = json.loads(result)
    assert "error" in data
    assert "NOTEXIST" in data["error"]


async def test_get_device_reading_api_error(mock_client):
    mock_client.get_devices.side_effect = ACInfinityAPIError("API error 500: server fault")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_device_reading("C58ZA")
    data = json.loads(result)
    assert data["error"] == "AC Infinity API error"
    assert "detail" in data


async def test_get_device_reading_auth_error(mock_client):
    mock_client.get_devices.side_effect = ACInfinityAuthError("Not authenticated")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_device_reading("C58ZA")
    data = json.loads(result)
    assert "Authentication failed" in data["error"]
    assert "detail" in data


# ============ get_all_device_readings ============

async def test_get_all_device_readings_success(mock_client):
    second = {**MOCK_DEVICE_LEGACY, "devCode": "D2"}
    mock_client.get_devices.return_value = [MOCK_DEVICE_LEGACY, second]
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_all_device_readings()
    data = json.loads(result)
    assert "readings" in data
    assert len(data["readings"]) == 2


async def test_get_all_device_readings_api_error(mock_client):
    mock_client.get_devices.side_effect = ACInfinityAPIError("API error 500: server fault")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_all_device_readings()
    data = json.loads(result)
    assert data["error"] == "AC Infinity API error"
    assert "detail" in data


async def test_get_all_device_readings_auth_error(mock_client):
    mock_client.get_devices.side_effect = ACInfinityAuthError("Not authenticated")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_all_device_readings()
    data = json.loads(result)
    readings = {r["device_id"]: r for r in data["readings"]}
    assert "error" in readings["BAD"]
    assert "error" not in readings["C58ZA"]


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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_historical_readings("C58ZA", "2024-04-25", "2024-04-25", "raw")
    data = json.loads(result)
    assert "readings" in data
    assert len(data["readings"]) == 5
    assert "statistics" in data


async def test_get_historical_readings_invalid_date_format(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_historical_readings("C58ZA", "not-a-date", "2024-04-25")
    data = json.loads(result)
    assert "error" in data
    assert "YYYY-MM-DD" in data["error"]


async def test_get_historical_readings_start_after_end(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_historical_readings("C58ZA", "2024-04-26", "2024-04-25")
    data = json.loads(result)
    assert "error" in data
    assert "start_date" in data["error"]


async def test_get_historical_readings_invalid_interval(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_historical_readings("C58ZA", "2024-04-25", "2024-04-25", "2x")
    data = json.loads(result)
    assert "error" in data
    assert "sample_interval" in data["error"].lower() or "2x" in data["error"]


@pytest.mark.parametrize("bad_value", ["bad", "25:00", "12:60", "1200", "noon", ""])
async def test_get_historical_readings_invalid_time_start(mock_client, bad_value):
    """Invalid time_start returns structured error instead of silent empty result (P1-F006)."""
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_historical_readings(
            "C58ZA", "2024-04-25", "2024-04-25", "1h", time_start=bad_value
        )
    data = json.loads(result)
    assert "error" in data
    assert "time_start" in data["error"]


async def test_get_historical_readings_invalid_time_end(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_historical_readings(
            "C58ZA", "2024-04-25", "2024-04-25", "1h", time_end="bogus"
        )
    data = json.loads(result)
    assert "error" in data
    assert "time_end" in data["error"]


async def test_get_historical_readings_no_device(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_historical_readings("NOTEXIST", "2024-04-25", "2024-04-25")
    data = json.loads(result)
    assert "error" in data


async def test_get_historical_readings_no_records(mock_client):
    mock_client.get_historical_data.return_value = []
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_historical_readings("C58ZA", "2024-04-25", "2024-04-25", "raw")
    data = json.loads(result)
    stats = data["statistics"]
    assert "temperature_c" in stats
    assert stats["temperature_c"]["avg"] == 24.0
    assert "vpd" in stats


# ============ check_vpd_drift ============

async def test_check_vpd_drift_ok(mock_client):
    mock_client.parse_device_data.return_value = {
        **mock_client.parse_device_data.return_value,
        "vpd": 1.24,
    }
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await check_vpd_drift("C58ZA", "veg")
    data = json.loads(result)
    assert data["status"] == "HIGH"
    assert "exceeds target" in data["alert"]
    assert data["deviation"] == round(2.5 - 1.5, 2)  # 1.0: above upper bound


async def test_check_vpd_drift_unknown_stage_returns_error(mock_client):
    """Unknown stage must return an error, not silently fall back to veg."""
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await check_vpd_drift("C58ZA", "bloom")
    data = json.loads(result)
    assert "error" in data
    assert "bloom" in data["error"]
    assert "Unknown stage" in data["error"]


async def test_check_vpd_drift_device_not_found(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await check_vpd_drift("NOTEXIST", "veg")
    data = json.loads(result)
    assert "error" in data


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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_environment_health("C58ZA", "veg")
    data = json.loads(result)
    assert "score" in data
    assert "grade" in data
    assert 0 <= data["score"] <= 100
    assert data["grade"] in ("A", "B", "C", "D", "F")
    assert "top_recommendation" in data
    assert data["device_id"] == "C58ZA"
    assert data["stage"] == "veg"


async def test_get_environment_health_bad_stage(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_environment_health("C58ZA", "bloom")
    data = json.loads(result)
    assert "error" in data
    assert "bloom" in data["error"]
    assert "Unknown stage" in data["error"]


async def test_get_environment_health_unknown_device(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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

    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await detect_environment_trends("C58ZA", 0)
    data = json.loads(result)
    assert "error" in data
    assert "days must be between 1 and 30" in data["error"]


async def test_detect_environment_trends_days_thirty_one(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await detect_environment_trends("C58ZA", 31)
    data = json.loads(result)
    assert "error" in data
    assert "days must be between 1 and 30" in data["error"]


async def test_detect_environment_trends_historical_error_propagated(mock_client):
    error_payload = json.dumps({"error": "Device NOTEXIST not found"})
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        with patch("ac_infinity_mcp.server.get_historical_readings",
                   return_value=error_payload):
            result = await detect_environment_trends("NOTEXIST", 7)
    data = json.loads(result)
    assert "error" in data


async def test_detect_environment_trends_single_reading_flat(mock_client):
    single = [_make_hourly_readings(1)[0]]
    hist_payload = json.dumps({
        "device_id": "C58ZA",
        "readings": single,
        "statistics": {},
    })
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        with patch("ac_infinity_mcp.server.get_historical_readings",
                   return_value=hist_payload):
            result = await detect_environment_trends("C58ZA", 1)
    data = json.loads(result)
    assert data["readings_used"] == 1
    for trend in data["trends"]:
        assert trend["slope"] == 0.0
        assert trend["direction"] == "flat"


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
    readings = _make_port_readings(24, speed=5, on=True)
    hist_payload = json.dumps({
        "device_id": "C58ZA",
        "readings": readings,
        "statistics": {},
    })
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        with patch("ac_infinity_mcp.server.get_historical_readings",
                   return_value=hist_payload):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_activity_report("C58ZA", 0)
    data = json.loads(result)
    assert "error" in data
    assert "days must be between 1 and 30" in data["error"]


async def test_get_port_activity_report_days_thirty_one(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_activity_report("C58ZA", 31)
    data = json.loads(result)
    assert "error" in data
    assert "days must be between 1 and 30" in data["error"]


async def test_get_port_activity_report_no_ports(mock_client):
    readings = [
        {
            "timestamp": "2024-04-18T00:00:00Z",
            "temperature_c": 24.0,
            "humidity": 55.0,
            "vpd": 1.4,
            "ports": [],
        }
    ]
    hist_payload = json.dumps({
        "device_id": "C58ZA",
        "readings": readings,
        "statistics": {},
    })
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        with patch("ac_infinity_mcp.server.get_historical_readings",
                   return_value=hist_payload):
            result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    assert data["ports"] == []


async def test_get_port_activity_report_port_always_off(mock_client):
    readings = _make_port_readings(24, speed=0, on=False)
    hist_payload = json.dumps({
        "device_id": "C58ZA",
        "readings": readings,
        "statistics": {},
    })
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        with patch("ac_infinity_mcp.server.get_historical_readings",
                   return_value=hist_payload):
            result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    port = data["ports"][0]
    assert port["uptime_pct"] == 0.0
    assert port["on_hours"] == 0.0
    assert port["avg_speed_when_running"] == 0.0


async def test_get_port_activity_report_port_always_on(mock_client):
    readings = _make_port_readings(24, speed=5, on=True)
    hist_payload = json.dumps({
        "device_id": "C58ZA",
        "readings": readings,
        "statistics": {},
    })
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        with patch("ac_infinity_mcp.server.get_historical_readings",
                   return_value=hist_payload):
            result = await get_port_activity_report("C58ZA", 1)
    data = json.loads(result)
    port = data["ports"][0]
    assert port["uptime_pct"] == 100.0
    assert port["avg_speed_when_running"] == 5.0


# ============ set_port_speed ============

MOCK_SET_PORT_MODE_DRY = {
    "payload": {"onSpead": 5, "modeType": 2, "devId": 12345},
    "dry_run": True,
    "controller_type": "legacy",
    "sent": False,
}

MOCK_SET_PORT_MODE_LIVE = {
    "payload": {"onSpead": 5, "modeType": 2, "devId": 12345},
    "dry_run": False,
    "controller_type": "legacy",
    "sent": True,
}


async def test_set_port_speed_dry_run(mock_client):
    mock_client.set_port_mode.return_value = MOCK_SET_PORT_MODE_DRY
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_speed("C58ZA", 2, 5, dry_run=False)
    data = json.loads(result)
    assert data["sent"] is True
    assert data["dry_run"] is False
    assert "payload" not in data


async def test_set_port_speed_device_not_found(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_speed("INVALID", 1, 5)
    data = json.loads(result)
    assert "error" in data
    assert "INVALID" in data["error"]


async def test_set_port_speed_speed_zero(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_speed("C58ZA", 1, 0)
    data = json.loads(result)
    assert "error" in data
    assert "speed" in data["error"]


async def test_set_port_speed_speed_eleven(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_speed("C58ZA", 1, 11)
    data = json.loads(result)
    assert "error" in data
    assert "speed" in data["error"]


async def test_set_port_speed_speed_one_valid(mock_client):
    mock_client.set_port_mode.return_value = {**MOCK_SET_PORT_MODE_DRY, "payload": {"onSpead": 1}}
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_speed("C58ZA", 1, 1)
    data = json.loads(result)
    assert "error" not in data
    assert data["speed"] == 1


async def test_set_port_speed_speed_ten_valid(mock_client):
    mock_client.set_port_mode.return_value = {**MOCK_SET_PORT_MODE_DRY, "payload": {"onSpead": 10}}
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_speed("C58ZA", 1, 10)
    data = json.loads(result)
    assert "error" not in data
    assert data["speed"] == 10


async def test_set_port_speed_port_zero(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_speed("C58ZA", 0, 5)
    data = json.loads(result)
    assert "error" in data
    assert "port" in data["error"]


async def test_set_port_speed_api_error(mock_client):
    mock_client.set_port_mode.side_effect = ACInfinityAPIError("API error 500")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_speed("C58ZA", 1, 5)
    data = json.loads(result)
    assert "error" in data


async def test_set_port_speed_auth_error(mock_client):
    mock_client.set_port_mode.side_effect = ACInfinityAuthError("Not authenticated")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_speed("C58ZA", 1, 5)
    data = json.loads(result)
    assert "error" in data


async def test_set_port_speed_device_error(mock_client):
    mock_client.set_port_mode.side_effect = ACInfinityDeviceError("device_data missing devId")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_speed("C58ZA", 1, 5)
    data = json.loads(result)
    assert "error" in data


async def test_set_port_speed_uses_asyncio_to_thread(mock_client):
    """Confirm set_port_mode is called via asyncio.to_thread, not directly."""
    mock_client.set_port_mode.return_value = MOCK_SET_PORT_MODE_DRY
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        with patch("asyncio.to_thread", wraps=asyncio.to_thread) as mock_thread:
            await set_port_speed("C58ZA", 1, 5)
    # asyncio.to_thread should have been called at least twice:
    # once for get_devices and once for set_port_mode
    assert mock_thread.call_count >= 2


# ============ set_port_on ============

MOCK_SET_PORT_ON_DRY = {
    "payload": {"onSpead": 10, "modeType": 2, "devId": 12345},
    "dry_run": True,
    "controller_type": "legacy",
    "sent": False,
}


async def test_set_port_on_dry_run(mock_client):
    mock_client.set_port_mode.return_value = MOCK_SET_PORT_ON_DRY
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_on("C58ZA", 1, dry_run=True)
    data = json.loads(result)
    assert data["dry_run"] is True
    assert data["sent"] is False
    assert data["payload"]["onSpead"] == 10
    assert data["device_id"] == "C58ZA"
    assert data["port"] == 1


async def test_set_port_on_device_not_found(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_on("INVALID", 1)
    data = json.loads(result)
    assert "error" in data
    assert "INVALID" in data["error"]


async def test_set_port_on_port_zero(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_on("C58ZA", 0)
    data = json.loads(result)
    assert "error" in data
    assert "port" in data["error"]


async def test_set_port_on_api_error(mock_client):
    mock_client.set_port_mode.side_effect = ACInfinityAPIError("API error 403")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_on("C58ZA", 1)
    data = json.loads(result)
    assert "error" in data


async def test_set_port_on_auth_error(mock_client):
    mock_client.set_port_mode.side_effect = ACInfinityAuthError("Not authenticated")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_on("C58ZA", 1)
    data = json.loads(result)
    assert "error" in data


async def test_set_port_on_device_error_non_advance(mock_client):
    """Base ACInfinityDeviceError (not the advance subclass) returns a plain error string."""
    mock_client.set_port_mode.side_effect = ACInfinityDeviceError("loadType=4 device")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_off("C58ZA", 1, dry_run=True)
    data = json.loads(result)
    assert data["dry_run"] is True
    assert data["sent"] is False
    assert data["payload"]["onSpead"] == 0
    assert data["device_id"] == "C58ZA"


async def test_set_port_off_device_not_found(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_off("INVALID", 1)
    data = json.loads(result)
    assert "error" in data
    assert "INVALID" in data["error"]


async def test_set_port_off_port_zero(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_off("C58ZA", 0)
    data = json.loads(result)
    assert "error" in data
    assert "port" in data["error"]


async def test_set_port_off_api_error(mock_client):
    mock_client.set_port_mode.side_effect = ACInfinityAPIError("API error 403")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_off("C58ZA", 1)
    data = json.loads(result)
    assert "error" in data


async def test_set_port_off_auth_error(mock_client):
    mock_client.set_port_mode.side_effect = ACInfinityAuthError("Not authenticated")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_off("C58ZA", 1)
    data = json.loads(result)
    assert "error" in data


async def test_set_port_off_device_error_non_advance(mock_client):
    """Base ACInfinityDeviceError (not advance subclass) returns plain error."""
    mock_client.set_port_mode.side_effect = ACInfinityDeviceError("device guard triggered")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        await tool(*args)
    kwargs = mock_client.set_port_mode.call_args.kwargs
    assert kwargs.get("require_variable_speed", False) is False


# ============ Guard rails — Phase 8 ============

MOCK_AI_PLUS_UNSUPPORTED = {
    "payload": {"onSpead": 5, "modeType": 2},
    "dry_run": False,
    "controller_type": "new_framework",
    "sent": False,
    "ai_plus_write_unsupported": True,
}


async def test_set_port_speed_rejects_load_type_4(mock_client):
    """set_port_speed rejects on/off devices (loadType=4) — guard fires in client layer."""
    mock_client.set_port_mode.side_effect = ACInfinityDeviceError(
        "Port 1 is an on/off device (loadType=4) — use set_port_on or set_port_off."
    )
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_speed("C58ZA", 1, 5)
    data = json.loads(result)
    assert "error" in data
    assert "loadType=4" in data["error"]


async def test_set_port_speed_rejects_load_type_128(mock_client):
    """set_port_speed rejects dimmer-type devices (loadType=128) — guard fires in client layer."""
    mock_client.set_port_mode.side_effect = ACInfinityDeviceError(
        "Port 1 is an on/off device (loadType=128) — use set_port_on or set_port_off."
    )
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_speed("C58ZA", 1, 5)
    data = json.loads(result)
    assert "error" in data
    assert "loadType=128" in data["error"]


async def test_set_port_speed_allows_variable_speed_port(mock_client):
    """set_port_speed must succeed for variable-speed ports (loadType=0 or 1)."""
    mock_client.set_port_mode.return_value = MOCK_SET_PORT_MODE_DRY
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_speed("C58ZA", 1, 5)
    data = json.loads(result)
    assert "error" not in data


async def test_set_port_on_not_affected_by_load_type_guard(mock_client):
    """set_port_on must NOT trigger the loadType guard — correct tool for on/off devices."""
    mock_client.set_port_mode.return_value = {
        "payload": {"onSpead": 10}, "dry_run": True, "controller_type": "legacy", "sent": False
    }
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_on("C58ZA", 1, dry_run=True)
    data = json.loads(result)
    assert "error" not in data
    assert data["dry_run"] is True


async def test_set_port_off_not_affected_by_load_type_guard(mock_client):
    """set_port_off must NOT trigger the loadType guard — correct tool for on/off devices."""
    mock_client.set_port_mode.return_value = {
        "payload": {"onSpead": 0}, "dry_run": True, "controller_type": "legacy", "sent": False
    }
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_off("C58ZA", 1, dry_run=True)
    data = json.loads(result)
    assert "error" not in data
    assert data["dry_run"] is True


async def test_set_port_speed_returns_conflict_for_modeType_15(mock_client):
    """ACInfinityAdvanceConflictError from modeType=15 guard returns structured conflict."""
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError(
        "Port 1 on device 12345 is in smart automation mode (modeType=15)"
    )
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_speed("C58ZA", 1, 5)
    data = json.loads(result)
    assert data["conflict"] == "ADVANCE_AUTOMATION"
    assert "summary" in data
    assert "Intake Fan (Port 1)" in data["summary"]
    assert data["target_port"] == "Intake Fan (Port 1)"
    assert "options" in data
    assert "error" not in data


async def test_set_port_on_returns_conflict_for_modeType_15(mock_client):
    """ACInfinityAdvanceConflictError from modeType=15 guard applies to set_port_on."""
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError(
        "Port 1 on device 12345 is in smart automation mode (modeType=15)"
    )
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_off("C58ZA", 1)
    data = json.loads(result)
    assert data["conflict"] == "ADVANCE_AUTOMATION"
    assert "summary" in data
    assert "error" not in data


async def test_set_port_speed_ai_plus_live_write_returns_not_implemented(mock_client):
    """AI+ dry_run=False returns a clear documented error, not a crash."""
    mock_client.set_port_mode.return_value = MOCK_AI_PLUS_UNSUPPORTED
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_speed("C58ZA", 1, 5, dry_run=False)
    data = json.loads(result)
    assert "error" in data
    assert "AI+" in data["error"] or "devType=22" in data["error"]
    assert data["controller_type"] == "new_framework"


async def test_set_port_on_ai_plus_live_write_returns_not_implemented(mock_client):
    """AI+ set_port_on dry_run=False returns documented error."""
    mock_client.set_port_mode.return_value = MOCK_AI_PLUS_UNSUPPORTED
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_on("C58ZA", 1, dry_run=False)
    data = json.loads(result)
    assert "error" in data
    assert data["controller_type"] == "new_framework"


async def test_set_port_off_ai_plus_live_write_returns_not_implemented(mock_client):
    """AI+ set_port_off dry_run=False returns documented error."""
    mock_client.set_port_mode.return_value = MOCK_AI_PLUS_UNSUPPORTED
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_off("C58ZA", 1, dry_run=False)
    data = json.loads(result)
    assert "error" in data
    assert data["controller_type"] == "new_framework"


async def test_set_port_speed_passes_require_variable_speed_to_client(mock_client):
    """set_port_speed passes require_variable_speed=True; client layer enforces the guard."""
    mock_client.set_port_mode.return_value = MOCK_SET_PORT_MODE_DRY
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        await set_port_speed("C58ZA", 1, 5)
    call_kwargs = mock_client.set_port_mode.call_args
    assert call_kwargs.kwargs.get("require_variable_speed") is True


# ============ Generic except Exception coverage ============

async def test_get_device_reading_generic_exception(mock_client):
    mock_client.get_devices.side_effect = RuntimeError("unexpected crash")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_device_reading("C58ZA")
    data = json.loads(result)
    assert "error" in data


async def test_get_all_device_readings_generic_exception(mock_client):
    mock_client.get_devices.side_effect = RuntimeError("unexpected crash")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_all_device_readings()
    data = json.loads(result)
    assert "error" in data


async def test_set_port_speed_generic_exception(mock_client):
    mock_client.set_port_mode.side_effect = RuntimeError("unexpected crash")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_speed("C58ZA", 1, 5)
    data = json.loads(result)
    assert "error" in data


async def test_set_port_on_generic_exception(mock_client):
    mock_client.set_port_mode.side_effect = RuntimeError("unexpected crash")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_on("C58ZA", 1)
    data = json.loads(result)
    assert "error" in data


async def test_set_port_off_generic_exception(mock_client):
    mock_client.set_port_mode.side_effect = RuntimeError("unexpected crash")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_off("C58ZA", 1)
    data = json.loads(result)
    assert "error" in data


# ============ get_historical_readings — error handlers + missing branches ============

async def test_get_historical_readings_auth_error(mock_client):
    mock_client.get_devices.side_effect = ACInfinityAuthError("token expired")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_historical_readings("C58ZA", "2024-04-25", "2024-04-25")
    data = json.loads(result)
    assert "Authentication failed" in data["error"]


async def test_get_historical_readings_api_error(mock_client):
    mock_client.get_devices.side_effect = ACInfinityAPIError("API error 503")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_historical_readings("C58ZA", "2024-04-25", "2024-04-25")
    data = json.loads(result)
    assert data["error"] == "AC Infinity API error"


async def test_get_historical_readings_generic_exception(mock_client):
    mock_client.get_devices.side_effect = RuntimeError("unexpected crash")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.get_device_reading",
               side_effect=ACInfinityAuthError("token expired")):
        result = await get_environment_health("C58ZA", "veg")
    data = json.loads(result)
    assert "Authentication failed" in data["error"]


async def test_get_environment_health_api_error(mock_client):
    with patch("ac_infinity_mcp.server.get_device_reading",
               side_effect=ACInfinityAPIError("API error 500")):
        result = await get_environment_health("C58ZA", "veg")
    data = json.loads(result)
    assert data["error"] == "AC Infinity API error"


async def test_get_environment_health_generic_exception(mock_client):
    with patch("ac_infinity_mcp.server.get_device_reading",
               side_effect=RuntimeError("unexpected crash")):
        result = await get_environment_health("C58ZA", "veg")
    data = json.loads(result)
    assert "error" in data


# ============ detect_environment_trends — error handlers ============

async def test_detect_environment_trends_auth_error(mock_client):
    with patch("ac_infinity_mcp.server.get_historical_readings",
               side_effect=ACInfinityAuthError("token expired")):
        result = await detect_environment_trends("C58ZA", 7)
    data = json.loads(result)
    assert "Authentication failed" in data["error"]


async def test_detect_environment_trends_api_error(mock_client):
    with patch("ac_infinity_mcp.server.get_historical_readings",
               side_effect=ACInfinityAPIError("API error 500")):
        result = await detect_environment_trends("C58ZA", 7)
    data = json.loads(result)
    assert data["error"] == "AC Infinity API error"


async def test_detect_environment_trends_generic_exception(mock_client):
    with patch("ac_infinity_mcp.server.get_historical_readings",
               side_effect=RuntimeError("unexpected crash")):
        result = await detect_environment_trends("C58ZA", 7)
    data = json.loads(result)
    assert "error" in data


# ============ get_port_activity_report — error propagation + error handlers ============

async def test_get_port_activity_report_error_propagated(mock_client):
    error_payload = json.dumps({"error": "No readings found for device C58ZA"})
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        with patch("ac_infinity_mcp.server.get_historical_readings",
                   return_value=error_payload):
            result = await get_port_activity_report("C58ZA", 7)
    data = json.loads(result)
    assert "error" in data


async def test_get_port_activity_report_auth_error(mock_client):
    with patch("ac_infinity_mcp.server.get_historical_readings",
               side_effect=ACInfinityAuthError("token expired")):
        result = await get_port_activity_report("C58ZA", 7)
    data = json.loads(result)
    assert "Authentication failed" in data["error"]


async def test_get_port_activity_report_api_error(mock_client):
    with patch("ac_infinity_mcp.server.get_historical_readings",
               side_effect=ACInfinityAPIError("API error 500")):
        result = await get_port_activity_report("C58ZA", 7)
    data = json.loads(result)
    assert data["error"] == "AC Infinity API error"


async def test_get_port_activity_report_generic_exception(mock_client):
    with patch("ac_infinity_mcp.server.get_historical_readings",
               side_effect=RuntimeError("unexpected crash")):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert data["device_id"] == "C58ZA"
    assert data["port"] == 1
    assert data["port_name"] == "Intake Fan"
    assert data["power_level"] == 5
    assert data["load_detected"] is True
    assert data["mode"] == "AUTO"        # curMode=3
    assert data["remain_time_seconds"] == 0


async def test_get_port_status_mode_on(mock_client):
    """Port 2 in conftest has curMode=2 → ON."""
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_status("C58ZA", 2)
    data = json.loads(result)
    assert data["mode"] == "ON"


async def test_get_port_status_remain_time_none_defaults_to_zero(mock_client):
    """remainTime=None in fixture → remain_time_seconds=0 in output."""
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert data["remain_time_seconds"] == 0


async def test_get_port_status_load_not_detected(mock_client):
    """loadState=0 → load_detected=False."""
    mock_client.get_devices.return_value = [{
        **MOCK_DEVICE_LEGACY,
        "deviceInfo": {
            **MOCK_DEVICE_LEGACY["deviceInfo"],
            "ports": [
                {"port": 1, "portName": "Empty Port", "speak": 0,
                 "portsLoad": 0, "loadState": 0, "curMode": 1, "remainTime": 0},
            ],
        },
    }]
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert data["load_detected"] is False
    assert data["mode"] == "OFF"


async def test_get_port_status_device_not_found(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_status("NOTEXIST", 1)
    data = json.loads(result)
    assert "error" in data
    assert "NOTEXIST" in data["error"]


async def test_get_port_status_port_not_found(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_status("C58ZA", 99)
    data = json.loads(result)
    assert "error" in data
    assert "99" in data["error"]


async def test_get_port_status_port_zero(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_status("C58ZA", 0)
    data = json.loads(result)
    assert "error" in data
    assert "port" in data["error"]


async def test_get_port_status_auth_error(mock_client):
    mock_client.get_devices.side_effect = ACInfinityAuthError("token expired")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert "Authentication failed" in data["error"]
    assert "detail" in data


async def test_get_port_status_api_error(mock_client):
    mock_client.get_devices.side_effect = ACInfinityAPIError("API error 503")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert data["error"] == "AC Infinity API error"
    assert "detail" in data


async def test_get_port_status_generic_exception(mock_client):
    mock_client.get_devices.side_effect = RuntimeError("unexpected crash")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert "error" in data


async def test_get_port_status_advance_mode_via_is_open_automation(mock_client):
    """isOpenAutomation=1 in port data returns mode: ADVANCE without secondary call."""
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert data["mode"] == "ADVANCE"
    assert data["power_level"] == 2
    mock_client.get_mode_settings.assert_not_called()


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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert data["mode"] == "OFF"
    mock_client.get_mode_settings.assert_not_called()


async def test_get_port_status_advance_heuristic_curmode1_speak_nonzero(mock_client):
    """curMode=1 with speak>0 triggers secondary call; modeType=15 → ADVANCE."""
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert data["mode"] == "ADVANCE"
    mock_client.get_mode_settings.assert_called_once()


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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert data["mode"] == "OFF"


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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_status("C58ZA", 1)
    data = json.loads(result)
    assert data["mode"] == "OFF"  # fallback to decoded curMode=1


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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert data["device_id"] == "C58ZA"
    assert data["port"] == 1
    assert data["mode"] == "OFF"       # atType=1
    assert data["speed_target"] == 5
    assert data["vpd_target_kpa"] is None
    assert data["temp_range_c"] is None
    assert data["humidity_range_pct"] is None
    assert data["schedule_window"] is None
    assert data["cycle_on_seconds"] == 300
    assert data["cycle_off_seconds"] == 60
    assert data["timer_on_seconds"] == 0
    assert data["timer_off_seconds"] == 0


async def test_get_port_settings_vpd_target_active(mock_client):
    """targetVpdSwitch=1 → vpd_target_kpa populated (targetVpd / 10)."""
    settings = {**MOCK_MODE_SETTINGS_BASIC, "targetVpdSwitch": 1, "targetVpd": 14}
    mock_client.get_mode_settings.return_value = settings
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert data["vpd_target_kpa"] == 1.4


@pytest.mark.parametrize("raw_target_vpd", [-1, -1_000_000, 1000, 99999, "garbage", None])
async def test_get_port_settings_vpd_target_out_of_range_is_none(mock_client, raw_target_vpd):
    """Corrupted/out-of-range targetVpd from upstream parses to null, not nonsense (P3-F020)."""
    settings = {**MOCK_MODE_SETTINGS_BASIC, "targetVpdSwitch": 1, "targetVpd": raw_target_vpd}
    mock_client.get_mode_settings.return_value = settings
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert data["vpd_target_kpa"] is None


async def test_get_port_settings_temp_range_active(mock_client):
    """activeLt=1 and activeHt=1 → temp_range_c populated (raw Celsius, no scaling)."""
    settings = {**MOCK_MODE_SETTINGS_BASIC, "activeLt": 1, "activeHt": 1,
                "devLt": 20, "devHt": 28}
    mock_client.get_mode_settings.return_value = settings
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert data["temp_range_c"] == {"min_c": 20, "max_c": 28}


async def test_get_port_settings_humidity_range_active(mock_client):
    """activeLh=1 → humidity_range_pct populated."""
    settings = {**MOCK_MODE_SETTINGS_BASIC, "activeLh": 1, "devLh": 40, "devHh": 70}
    mock_client.get_mode_settings.return_value = settings
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert data["humidity_range_pct"] == {"min_pct": 40, "max_pct": 70}


async def test_get_port_settings_schedule_window_active(mock_client):
    """schedStartTime != 65535 → schedule_window populated with HH:MM strings."""
    settings = {**MOCK_MODE_SETTINGS_BASIC, "schedStartTime": 480, "schedEndtTime": 1200}
    mock_client.get_mode_settings.return_value = settings
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert data["schedule_window"] == {"start": "08:00", "end": "20:00"}


@pytest.mark.parametrize("start,end", [
    (480, 65535),    # start set, end disabled — partial = no window
    (65535, 1200),   # start disabled, end set — partial = no window
    (65535, 65535),  # both disabled — no window
])
async def test_get_port_settings_schedule_window_partial_is_none(mock_client, start, end):
    """Half-configured schedule must return schedule_window=None, not a partial dict (P2-F015)."""
    settings = {**MOCK_MODE_SETTINGS_BASIC, "schedStartTime": start, "schedEndtTime": end}
    mock_client.get_mode_settings.return_value = settings
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert data["schedule_window"] is None


async def test_get_port_settings_mode_auto(mock_client):
    settings = {**MOCK_MODE_SETTINGS_BASIC, "atType": 3}
    mock_client.get_mode_settings.return_value = settings
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert data["mode"] == "AUTO"


async def test_get_port_settings_device_not_found(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_settings("NOTEXIST", 1)
    data = json.loads(result)
    assert "error" in data
    assert "NOTEXIST" in data["error"]


async def test_get_port_settings_missing_dev_id(mock_client):
    """Device missing devId returns a clear error."""
    device_no_id = {k: v for k, v in MOCK_DEVICE_LEGACY.items() if k != "devId"}
    mock_client.get_devices.return_value = [device_no_id]
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert "error" in data
    assert "devId" in data["error"]


async def test_get_port_settings_port_zero(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_settings("C58ZA", 0)
    data = json.loads(result)
    assert "error" in data
    assert "port" in data["error"]


async def test_get_port_settings_auth_error(mock_client):
    mock_client.get_devices.side_effect = ACInfinityAuthError("token expired")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert "Authentication failed" in data["error"]
    assert "detail" in data


async def test_get_port_settings_api_error(mock_client):
    mock_client.get_mode_settings.side_effect = ACInfinityAPIError("API error 503")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert data["error"] == "AC Infinity API error"
    assert "detail" in data


async def test_get_port_settings_generic_exception(mock_client):
    mock_client.get_mode_settings.side_effect = RuntimeError("unexpected crash")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert "error" in data


async def test_get_port_settings_advance_mode_returns_early(mock_client):
    """modeType=15 in settings returns ADVANCE mode with all automation targets null."""
    mock_client.get_mode_settings.return_value = {
        **MOCK_MODE_SETTINGS_BASIC,
        "modeType": 15,
        "onSpead": 2,
    }
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await get_port_settings("C58ZA", 1)
    data = json.loads(result)
    assert data["mode"] == "ADVANCE"
    assert data["advance_automation"] is True
    assert data["speed_target"] == 2
    assert data["vpd_target_kpa"] is None
    assert data["temp_range_c"] is None
    assert data["humidity_range_pct"] is None
    assert data["schedule_window"] is None
    assert data["cycle_on_seconds"] is None
    assert data["cycle_off_seconds"] is None
    assert data["timer_on_seconds"] is None
    assert data["timer_off_seconds"] is None


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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_vpd_automation("C58ZA", 1, 1.4, dry_run=True)
    data = json.loads(result)
    assert data["dry_run"] is True
    assert data["sent"] is False
    assert data["target_vpd_kpa"] == 1.4
    assert "payload" in data
    assert data["controller_type"] == "legacy"


async def test_set_vpd_automation_live(mock_client):
    mock_client.set_port_mode.return_value = MOCK_VPD_LIVE
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_vpd_automation("C58ZA", 1, 1.4, dry_run=False)
    data = json.loads(result)
    assert data["sent"] is True
    assert "payload" not in data


async def test_set_vpd_automation_payload_encoding(mock_client):
    """targetVpd must be stored as kPa × 10 (not × 100)."""
    mock_client.set_port_mode.return_value = MOCK_VPD_DRY
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        await set_vpd_automation("C58ZA", 1, 1.4)
    call_updates = mock_client.set_port_mode.call_args[0][2]
    assert call_updates["atType"] == 8
    assert call_updates["targetVpd"] == 14   # 1.4 × 10
    assert call_updates["vpdSettingMode"] == 1
    assert call_updates["targetVpdSwitch"] == 1


async def test_set_vpd_automation_no_bankers_rounding(mock_client):
    """1.25 kPa must encode as 13, not 12 (Python banker's rounding would give 12)."""
    mock_client.set_port_mode.return_value = MOCK_VPD_DRY
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        await set_vpd_automation("C58ZA", 1, 1.25)
    call_updates = mock_client.set_port_mode.call_args[0][2]
    assert call_updates["targetVpd"] == 13   # int(12.5 + 0.5) = 13, not round(12.5) = 12


async def test_set_vpd_automation_target_too_low(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_vpd_automation("C58ZA", 1, 0.0)
    data = json.loads(result)
    assert "error" in data
    assert "0.1" in data["error"]


async def test_set_vpd_automation_target_too_high(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_vpd_automation("C58ZA", 1, 3.1)
    data = json.loads(result)
    assert "error" in data
    # P2-C2-F009: pin that the bounds-check fired, not some downstream error
    assert "3.0" in data["error"] or "3.1" in data["error"]


async def test_set_vpd_automation_boundary_min_valid(mock_client):
    mock_client.set_port_mode.return_value = MOCK_VPD_DRY
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_vpd_automation("C58ZA", 1, 0.1)
    data = json.loads(result)
    assert "error" not in data


async def test_set_vpd_automation_boundary_max_valid(mock_client):
    mock_client.set_port_mode.return_value = MOCK_VPD_DRY
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_vpd_automation("C58ZA", 1, 3.0)
    data = json.loads(result)
    assert "error" not in data


async def test_set_vpd_automation_device_not_found(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_vpd_automation("INVALID", 1, 1.4)
    data = json.loads(result)
    assert "error" in data
    assert "INVALID" in data["error"]


async def test_set_vpd_automation_port_zero(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_vpd_automation("C58ZA", 0, 1.4)
    data = json.loads(result)
    assert "error" in data
    assert "port" in data["error"]


async def test_set_vpd_automation_ai_plus_returns_not_implemented(mock_client):
    mock_client.set_port_mode.return_value = {
        "payload": {}, "dry_run": False,
        "controller_type": "new_framework", "sent": False,
        "ai_plus_write_unsupported": True,
    }
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_vpd_automation("C58ZA", 1, 1.4, dry_run=False)
    data = json.loads(result)
    assert "error" in data
    assert "AI+" in data["error"]
    assert data["controller_type"] == "new_framework"


async def test_set_vpd_automation_api_error(mock_client):
    mock_client.set_port_mode.side_effect = ACInfinityAPIError("server error")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_vpd_automation("C58ZA", 1, 1.4)
    data = json.loads(result)
    assert "error" in data


async def test_set_vpd_automation_auth_error(mock_client):
    mock_client.set_port_mode.side_effect = ACInfinityAuthError("token expired")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_vpd_automation("C58ZA", 1, 1.4)
    data = json.loads(result)
    assert "error" in data


async def test_set_vpd_automation_generic_exception(mock_client):
    mock_client.set_port_mode.side_effect = RuntimeError("crash")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_vpd_automation("C58ZA", 1, 1.4)
    data = json.loads(result)
    assert "error" in data


async def test_set_vpd_automation_advance_conflict(mock_client):
    """ACInfinityAdvanceConflictError → structured conflict response, not a generic error."""
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("modeType=15")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_vpd_automation("C58ZA", 1, 1.4)
    data = json.loads(result)
    assert data["conflict"] == "ADVANCE_AUTOMATION"
    assert "summary" in data
    assert "error" not in data


async def test_set_vpd_automation_device_error_non_advance(mock_client):
    """Base ACInfinityDeviceError → plain error response."""
    mock_client.set_port_mode.side_effect = ACInfinityDeviceError("loadType guard")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_temperature_automation("C58ZA", 1, 20.0, 28.0, dry_run=True)
    data = json.loads(result)
    assert data["dry_run"] is True
    assert data["min_c"] == 20.0
    assert data["max_c"] == 28.0
    assert "payload" in data


async def test_set_temperature_automation_payload_encoding(mock_client):
    """devLt/devHt are raw Celsius integers — no × 100 scaling."""
    mock_client.set_port_mode.return_value = MOCK_TEMP_DRY
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        await set_temperature_automation("C58ZA", 1, 20.0, 28.0)
    call_updates = mock_client.set_port_mode.call_args[0][2]
    assert call_updates["atType"] == 3
    assert call_updates["devLt"] == 20    # raw °C, not 2000
    assert call_updates["devHt"] == 28
    assert call_updates["activeLt"] == 1
    assert call_updates["activeHt"] == 1


async def test_set_temperature_automation_min_ge_max(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_temperature_automation("C58ZA", 1, 28.0, 20.0)
    data = json.loads(result)
    assert "error" in data
    assert "min_c" in data["error"]


async def test_set_temperature_automation_equal_min_max(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_temperature_automation("C58ZA", 1, 25.0, 25.0)
    data = json.loads(result)
    assert "error" in data


async def test_set_temperature_automation_out_of_range(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_temperature_automation("C58ZA", 1, -1.0, 30.0)
    data = json.loads(result)
    assert "error" in data
    assert "between 0 and 50" in data["error"]  # P2-C2-F009


async def test_set_temperature_automation_max_out_of_range(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_temperature_automation("C58ZA", 1, 20.0, 51.0)
    data = json.loads(result)
    assert "error" in data
    assert "between 0 and 50" in data["error"]  # P2-C2-F009


async def test_set_temperature_automation_device_not_found(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_temperature_automation("INVALID", 1, 20.0, 28.0)
    data = json.loads(result)
    assert "error" in data


async def test_set_temperature_automation_port_zero(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_temperature_automation("C58ZA", 0, 20.0, 28.0)
    data = json.loads(result)
    assert "error" in data
    assert "port" in data["error"]


async def test_set_temperature_automation_ai_plus_returns_not_implemented(mock_client):
    mock_client.set_port_mode.return_value = {
        "payload": {}, "dry_run": False,
        "controller_type": "new_framework", "sent": False,
        "ai_plus_write_unsupported": True,
    }
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_temperature_automation("C58ZA", 1, 20.0, 28.0, dry_run=False)
    data = json.loads(result)
    assert "error" in data
    assert "AI+" in data["error"]


async def test_set_temperature_automation_api_error(mock_client):
    mock_client.set_port_mode.side_effect = ACInfinityAPIError("err")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_temperature_automation("C58ZA", 1, 20.0, 28.0)
    assert "error" in json.loads(result)


async def test_set_temperature_automation_generic_exception(mock_client):
    mock_client.set_port_mode.side_effect = RuntimeError("crash")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_temperature_automation("C58ZA", 1, 20.0, 28.0)
    assert "error" in json.loads(result)


async def test_set_temperature_automation_advance_conflict(mock_client):
    """ACInfinityAdvanceConflictError → structured conflict response."""
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("modeType=15")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_temperature_automation("C58ZA", 1, 20.0, 28.0)
    data = json.loads(result)
    assert data["conflict"] == "ADVANCE_AUTOMATION"
    assert "error" not in data


async def test_set_temperature_automation_device_error_non_advance(mock_client):
    """Base ACInfinityDeviceError → plain error response."""
    mock_client.set_port_mode.side_effect = ACInfinityDeviceError("loadType guard")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
        (49.5, 50.0, 50, 50),
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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


async def test_set_humidity_automation_dry_run(mock_client):
    mock_client.set_port_mode.return_value = MOCK_HUMI_DRY
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_humidity_automation("C58ZA", 1, 50.0, 70.0, dry_run=True)
    data = json.loads(result)
    assert data["dry_run"] is True
    assert data["min_rh"] == 50.0
    assert data["max_rh"] == 70.0
    assert "payload" in data


async def test_set_humidity_automation_payload_encoding(mock_client):
    """devLh/devHh are raw % integers — no × 100 scaling."""
    mock_client.set_port_mode.return_value = MOCK_HUMI_DRY
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        await set_humidity_automation("C58ZA", 1, 50.0, 70.0)
    call_updates = mock_client.set_port_mode.call_args[0][2]
    assert call_updates["atType"] == 3
    assert call_updates["devLh"] == 50    # raw %, not 5000
    assert call_updates["devHh"] == 70
    assert call_updates["activeLh"] == 1
    assert call_updates["activeHh"] == 1


async def test_set_humidity_automation_min_ge_max(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_humidity_automation("C58ZA", 1, 70.0, 50.0)
    data = json.loads(result)
    assert "error" in data
    assert "min_rh" in data["error"]


async def test_set_humidity_automation_out_of_range(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_humidity_automation("C58ZA", 1, -1.0, 70.0)
    data = json.loads(result)
    assert "error" in data
    assert "between 0 and 100" in data["error"]  # P2-C2-F009


async def test_set_humidity_automation_max_out_of_range(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_humidity_automation("C58ZA", 1, 50.0, 101.0)
    data = json.loads(result)
    assert "error" in data
    assert "between 0 and 100" in data["error"]  # P2-C2-F009


async def test_set_humidity_automation_device_not_found(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_humidity_automation("INVALID", 1, 50.0, 70.0)
    data = json.loads(result)
    assert "error" in data


async def test_set_humidity_automation_port_zero(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_humidity_automation("C58ZA", 0, 50.0, 70.0)
    data = json.loads(result)
    assert "error" in data
    assert "port" in data["error"]


async def test_set_humidity_automation_ai_plus_returns_not_implemented(mock_client):
    mock_client.set_port_mode.return_value = {
        "payload": {}, "dry_run": False,
        "controller_type": "new_framework", "sent": False,
        "ai_plus_write_unsupported": True,
    }
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_humidity_automation("C58ZA", 1, 50.0, 70.0, dry_run=False)
    data = json.loads(result)
    assert "error" in data
    assert "AI+" in data["error"]


async def test_set_humidity_automation_api_error(mock_client):
    mock_client.set_port_mode.side_effect = ACInfinityAPIError("err")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_humidity_automation("C58ZA", 1, 50.0, 70.0)
    assert "error" in json.loads(result)


async def test_set_humidity_automation_generic_exception(mock_client):
    mock_client.set_port_mode.side_effect = RuntimeError("crash")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_humidity_automation("C58ZA", 1, 50.0, 70.0)
    assert "error" in json.loads(result)


async def test_set_humidity_automation_advance_conflict(mock_client):
    """ACInfinityAdvanceConflictError → structured conflict response."""
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("modeType=15")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_humidity_automation("C58ZA", 1, 50.0, 70.0)
    data = json.loads(result)
    assert data["conflict"] == "ADVANCE_AUTOMATION"
    assert "error" not in data


async def test_set_humidity_automation_device_error_non_advance(mock_client):
    """Base ACInfinityDeviceError → plain error response."""
    mock_client.set_port_mode.side_effect = ACInfinityDeviceError("loadType guard")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_mode("C58ZA", 1, "OFF")
    data = json.loads(result)
    assert data["mode"] == "OFF"
    assert data["dry_run"] is True
    assert "payload" in data


async def test_set_port_mode_on(mock_client):
    mock_client.set_port_mode.return_value = {**MOCK_MODE_DRY, "payload": {"atType": 2}}
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_mode("C58ZA", 1, "AUTO")
    data = json.loads(result)
    assert data["mode"] == "AUTO"
    call_updates = mock_client.set_port_mode.call_args[0][2]
    assert call_updates["atType"] == 3


async def test_set_port_mode_vpd(mock_client):
    mock_client.set_port_mode.return_value = MOCK_MODE_DRY
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_mode("C58ZA", 1, "VPD")
    data = json.loads(result)
    assert data["mode"] == "VPD"
    call_updates = mock_client.set_port_mode.call_args[0][2]
    assert call_updates["atType"] == 8


async def test_set_port_mode_case_insensitive(mock_client):
    mock_client.set_port_mode.return_value = MOCK_MODE_DRY
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_mode("C58ZA", 1, "off")
    data = json.loads(result)
    assert data["mode"] == "OFF"


async def test_set_port_mode_live(mock_client):
    mock_client.set_port_mode.return_value = MOCK_MODE_LIVE
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_mode("C58ZA", 1, "OFF", dry_run=False)
    data = json.loads(result)
    assert data["sent"] is True
    assert "payload" not in data


async def test_set_port_mode_invalid_mode(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_mode("C58ZA", 1, "INVALID")
    data = json.loads(result)
    assert "error" in data
    assert "INVALID" in data["error"]


async def test_set_port_mode_cycle_with_params(mock_client):
    mock_client.set_port_mode.return_value = MOCK_MODE_DRY
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_mode("C58ZA", 1, "CYCLE", cycle_off_seconds=60)
    data = json.loads(result)
    assert "error" in data
    assert "cycle_on_seconds" in data["error"]


async def test_set_port_mode_cycle_missing_both_params(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_mode("C58ZA", 1, "CYCLE")
    data = json.loads(result)
    assert "error" in data


async def test_set_port_mode_cycle_zero_seconds(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_mode("C58ZA", 1, "CYCLE", cycle_on_seconds=0, cycle_off_seconds=60)
    data = json.loads(result)
    assert "error" in data


async def test_set_port_mode_schedule_with_params(mock_client):
    mock_client.set_port_mode.return_value = MOCK_MODE_DRY
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_mode("C58ZA", 1, "SCHEDULE", schedule_start="08:00")
    data = json.loads(result)
    assert "error" in data
    assert "schedule_end" in data["error"]


async def test_set_port_mode_schedule_invalid_time_format(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_mode(
            "C58ZA", 1, "SCHEDULE", schedule_start="bad", schedule_end="20:00"
        )
    data = json.loads(result)
    assert "error" in data


async def test_set_port_mode_timer_to_off_with_duration(mock_client):
    mock_client.set_port_mode.return_value = MOCK_MODE_DRY
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_mode(
            "C58ZA", 1, "TIMER_TO_ON", timer_duration_seconds=1800
        )
    data = json.loads(result)
    assert data["mode"] == "TIMER_TO_ON"
    call_updates = mock_client.set_port_mode.call_args[0][2]
    assert call_updates["atType"] == 4
    assert call_updates["acitveTimerOn"] == 1800


async def test_set_port_mode_timer_missing_duration(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_mode("C58ZA", 1, "TIMER_TO_OFF")
    data = json.loads(result)
    assert "error" in data
    assert "timer_duration_seconds" in data["error"]


async def test_set_port_mode_timer_zero_duration(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_mode("C58ZA", 1, "TIMER_TO_OFF", timer_duration_seconds=0)
    data = json.loads(result)
    assert "error" in data


async def test_set_port_mode_device_not_found(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_mode("INVALID", 1, "OFF")
    data = json.loads(result)
    assert "error" in data
    assert "INVALID" in data["error"]


async def test_set_port_mode_port_zero(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_mode("C58ZA", 0, "OFF")
    data = json.loads(result)
    assert "error" in data
    assert "port" in data["error"]


async def test_set_port_mode_ai_plus_returns_not_implemented(mock_client):
    mock_client.set_port_mode.return_value = {
        "payload": {}, "dry_run": False,
        "controller_type": "new_framework", "sent": False,
        "ai_plus_write_unsupported": True,
    }
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_mode("C58ZA", 1, "OFF", dry_run=False)
    data = json.loads(result)
    assert "error" in data
    assert "AI+" in data["error"]


async def test_set_port_mode_device_error(mock_client):
    mock_client.set_port_mode.side_effect = ACInfinityDeviceError("smart mode")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_mode("C58ZA", 1, "OFF")
    assert "error" in json.loads(result)


async def test_set_port_mode_auth_error(mock_client):
    mock_client.set_port_mode.side_effect = ACInfinityAuthError("expired")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_mode("C58ZA", 1, "OFF")
    assert "error" in json.loads(result)


async def test_set_port_mode_generic_exception(mock_client):
    mock_client.set_port_mode.side_effect = RuntimeError("crash")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_mode("C58ZA", 1, "OFF")
    assert "error" in json.loads(result)


async def test_set_port_mode_advance_conflict(mock_client):
    """ACInfinityAdvanceConflictError → structured conflict response."""
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("modeType=15")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await set_port_mode("C58ZA", 1, "OFF")
    data = json.loads(result)
    assert data["conflict"] == "ADVANCE_AUTOMATION"
    assert "error" not in data


async def test_set_port_mode_device_error_non_advance(mock_client):
    """Base ACInfinityDeviceError → plain error response."""
    mock_client.set_port_mode.side_effect = ACInfinityDeviceError("loadType guard triggered")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await apply_grow_stage_template("C58ZA", 1, "veg", dry_run=True)
    data = json.loads(result)
    assert "error" not in data
    assert data["stage"] == "veg"
    assert data["dry_run"] is True
    assert data["sent"] is False
    assert data["controller_type"] == "legacy"
    assert data["vpd"]["target_kpa"] == 1.25
    assert data["temperature"]["min_c"] == 20.0
    assert data["temperature"]["max_c"] == 28.0
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await apply_grow_stage_template("C58ZA", 1, stage, dry_run=True)
    data = json.loads(result)
    assert "error" not in data
    assert data["stage"] == stage
    assert data["vpd"]["target_kpa"] == expected_vpd
    assert data["temperature"]["min_c"] == temp_min
    assert data["temperature"]["max_c"] == temp_max
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await apply_grow_stage_template("C58ZA", 1, stage)
    data = json.loads(result)
    assert "error" in data
    mock_client.set_port_mode.assert_not_called()


async def test_apply_grow_stage_template_port_zero(mock_client):
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await apply_grow_stage_template("C58ZA", 0, "veg")
    assert "error" in json.loads(result)
    mock_client.set_port_mode.assert_not_called()


async def test_apply_grow_stage_template_device_not_found(mock_client):
    mock_client.get_devices.return_value = []
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await apply_grow_stage_template("NOTFOUND", 1, "veg")
    data = json.loads(result)
    assert "error" in data
    assert "NOTFOUND" in data["error"]


async def test_apply_grow_stage_template_ai_plus_live(mock_client):
    mock_client.set_port_mode.return_value = {
        "payload": {}, "dry_run": False,
        "controller_type": "new_framework", "sent": False,
        "ai_plus_write_unsupported": True,
    }
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await apply_grow_stage_template("C58ZA", 1, "veg", dry_run=False)
    data = json.loads(result)
    assert "error" in data
    assert "AI+" in data["error"]
    assert mock_client.set_port_mode.call_count == 1


async def test_apply_grow_stage_template_ai_plus_dry_run(mock_client):
    mock_client.set_port_mode.return_value = _stage_dry_response()
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await apply_grow_stage_template("C58ZA", 1, "veg", dry_run=True)
    data = json.loads(result)
    assert "error" not in data
    assert data["sent"] is False


async def test_apply_grow_stage_template_api_error_on_write(mock_client):
    """API errors during write return a generic message (P3-C2-F003)."""
    mock_client.set_port_mode.side_effect = ACInfinityAPIError("Data saving failed")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await apply_grow_stage_template("C58ZA", 1, "veg", dry_run=False)
    data = json.loads(result)
    assert data["error"] == "AC Infinity API error"
    assert data["detail"] == "see server logs"
    # Raw upstream text must not leak
    assert "Data saving failed" not in result


async def test_apply_grow_stage_template_auth_error(mock_client):
    """Auth errors from the write call return a friendly auth-error message."""
    mock_client.set_port_mode.side_effect = ACInfinityAuthError("token expired")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await apply_grow_stage_template("C58ZA", 1, "veg", dry_run=False)
    data = json.loads(result)
    assert "Authentication failed" in data["error"]
    # Raw exception text must not leak (P1-C2-F003)
    assert "token expired" not in result
    assert data["detail"] == "see server logs"


async def test_apply_grow_stage_template_get_devices_exception(mock_client):
    """API errors during get_devices return a generic error, not str(e) (P1-C2-F003)."""
    mock_client.get_devices.side_effect = ACInfinityAPIError("upstream said: foo bar")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await apply_grow_stage_template("C58ZA", 1, "veg", dry_run=True)
    data = json.loads(result)
    assert data["error"] == "Unexpected error"
    assert data["detail"] == "see server logs"
    assert "should-not-leak" not in result
    assert "appPasswordl=" not in result


async def test_apply_grow_stage_template_advance_conflict(mock_client):
    """ACInfinityAdvanceConflictError from write → structured conflict, not opaque error."""
    mock_client.set_port_mode.side_effect = ACInfinityAdvanceConflictError("modeType=15")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await apply_grow_stage_template("C58ZA", 1, "veg", dry_run=False)
    data = json.loads(result)
    assert data["conflict"] == "ADVANCE_AUTOMATION"
    assert "summary" in data
    assert "error" not in data


async def test_apply_grow_stage_template_device_error_non_advance(mock_client):
    """Base ACInfinityDeviceError from write → plain error response."""
    mock_client.set_port_mode.side_effect = ACInfinityDeviceError("loadType guard")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
        result = await apply_grow_stage_template("C58ZA", 1, "veg", dry_run=False)
    data = json.loads(result)
    assert "error" in data
    assert "loadType guard" in data["error"]


async def test_apply_grow_stage_template_write_generic_exception(mock_client):
    """RuntimeError from write → generic error response (not str(e) leak)."""
    mock_client.set_port_mode.side_effect = RuntimeError("unexpected write crash")
    with patch("ac_infinity_mcp.server.aci_client", mock_client):
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
    assert "set_vpd_automation" in result
    assert "HIGH" in result
    assert "LOW" in result
    assert "apply_grow_stage_template" in result


def test_new_grower_setup_prompt():
    result = new_grower_setup()
    assert isinstance(result, str)
    assert len(result) > 200
    assert "discover_devices" in result
    assert "apply_grow_stage_template" in result
    assert "get_environment_health" in result
    assert "dry_run" in result


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
    """sensorType=11 → sensor_type_label='co2'."""
    from ac_infinity_mcp.client import ACInfinityClient
    client = ACInfinityClient("test@example.com", "pw")
    device = _device_with_sensors([
        {"accessPort": 1, "sensorType": 11, "sensorData": 1100, "sensorPrecision": 1},
    ])
    parsed = client.parse_device_data(device)
    assert parsed["external_sensors"][0]["sensor_type_label"] == "co2"
    assert parsed["external_sensors"][0]["sensor_type"] == 11


def test_external_sensor_type_label_soil_moisture():
    """sensorType=10 → sensor_type_label='soil_moisture'."""
    from ac_infinity_mcp.client import ACInfinityClient
    client = ACInfinityClient("test@example.com", "pw")
    device = _device_with_sensors([
        {"accessPort": 1, "sensorType": 10, "sensorData": 500, "sensorPrecision": 10},
    ])
    parsed = client.parse_device_data(device)
    assert parsed["external_sensors"][0]["sensor_type_label"] == "soil_moisture"


def test_external_sensor_type_label_unknown():
    """Unknown sensorType → sensor_type_label='unknown'."""
    from ac_infinity_mcp.client import ACInfinityClient
    client = ACInfinityClient("test@example.com", "pw")
    device = _device_with_sensors([
        {"accessPort": 1, "sensorType": 99, "sensorData": 100, "sensorPrecision": 100},
    ])
    parsed = client.parse_device_data(device)
    assert parsed["external_sensors"][0]["sensor_type_label"] == "unknown"


def test_external_sensor_precision_used():
    """value = sensorData / sensorPrecision (1150 / 1000 = 1.15)."""
    from ac_infinity_mcp.client import ACInfinityClient
    client = ACInfinityClient("test@example.com", "pw")
    device = _device_with_sensors([
        {"accessPort": 1, "sensorType": 11, "sensorData": 1150, "sensorPrecision": 1000},
    ])
    parsed = client.parse_device_data(device)
    assert parsed["external_sensors"][0]["value"] == pytest.approx(1.15)


def test_external_sensor_precision_zero_falls_back_to_100():
    """sensorPrecision=0 → fallback divisor of 100 (guard against ZeroDivisionError)."""
    from ac_infinity_mcp.client import ACInfinityClient
    client = ACInfinityClient("test@example.com", "pw")
    device = _device_with_sensors([
        {"accessPort": 1, "sensorType": 11, "sensorData": 500, "sensorPrecision": 0},
    ])
    parsed = client.parse_device_data(device)
    assert parsed["external_sensors"][0]["value"] == pytest.approx(5.0)


def test_external_sensor_precision_absent_falls_back_to_100():
    """Missing sensorPrecision key → fallback divisor of 100."""
    from ac_infinity_mcp.client import ACInfinityClient
    client = ACInfinityClient("test@example.com", "pw")
    device = _device_with_sensors([
        {"accessPort": 1, "sensorType": 11, "sensorData": 200},
    ])
    parsed = client.parse_device_data(device)
    assert parsed["external_sensors"][0]["value"] == pytest.approx(2.0)
