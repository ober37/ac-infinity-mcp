"""Tests for AI+ controller behavior (devType 20+, newFrameworkDevice=True)."""

from unittest.mock import patch

import ac_infinity_mcp.client as client_mod
from ac_infinity_mcp.client import ACInfinityClient
from ac_infinity_mcp.controller import ControllerType, build_write_payload, detect_controller_type
from tests.fixtures.ai_plus_device_fixtures import AI_PLUS_HISTORY_RECORD
from tests.fixtures.mock_mode_settings_ai_plus import (
    MOCK_MODE_SETTINGS_AI_PLUS_PORT1,
    MOCK_MODE_SETTINGS_AI_PLUS_PORT1_FLAT,
)

# ============ detect_controller_type ============

def test_detect_controller_type_devtype_20():
    assert detect_controller_type({"devType": 20}) == ControllerType.NEW_FRAMEWORK


def test_detect_controller_type_new_framework_flag():
    assert detect_controller_type({"newFrameworkDevice": True}) == ControllerType.NEW_FRAMEWORK


def test_detect_controller_type_devtype_25():
    assert detect_controller_type({"devType": 25}) == ControllerType.NEW_FRAMEWORK


def test_detect_controller_type_ai_plus_fixture(ai_plus_device):
    assert detect_controller_type(ai_plus_device) == ControllerType.NEW_FRAMEWORK


# ============ build_write_payload — AI+ path ============

def test_build_write_payload_ai_plus_merges_updates():
    result = build_write_payload(
        MOCK_MODE_SETTINGS_AI_PLUS_PORT1, {"onSpead": 5}, ControllerType.NEW_FRAMEWORK
    )
    assert result["onSpead"] == 5


def test_build_write_payload_ai_plus_strips_modeSetid():
    """Quirk 11: strip modeSetid for AI+ as well — same behavior as legacy."""
    result = build_write_payload(
        MOCK_MODE_SETTINGS_AI_PLUS_PORT1, {}, ControllerType.NEW_FRAMEWORK
    )
    assert "modeSetid" not in result


def test_build_write_payload_ai_plus_modeType_2_when_speed_nonzero():
    """Quirk 12 applies to AI+ as well."""
    result = build_write_payload(
        MOCK_MODE_SETTINGS_AI_PLUS_PORT1, {"onSpead": 7}, ControllerType.NEW_FRAMEWORK
    )
    assert result["modeType"] == 2


def test_build_write_payload_ai_plus_excludes_devSetting():
    result = build_write_payload(
        MOCK_MODE_SETTINGS_AI_PLUS_PORT1, {}, ControllerType.NEW_FRAMEWORK
    )
    assert "devSetting" not in result


def test_build_write_payload_ai_plus_excludes_fieldSet():
    result = build_write_payload(
        MOCK_MODE_SETTINGS_AI_PLUS_PORT1, {}, ControllerType.NEW_FRAMEWORK
    )
    assert "fieldSet" not in result


def test_build_write_payload_ai_plus_flat_field_count():
    result = build_write_payload(
        MOCK_MODE_SETTINGS_AI_PLUS_PORT1, {}, ControllerType.NEW_FRAMEWORK
    )
    assert len(result) == len(MOCK_MODE_SETTINGS_AI_PLUS_PORT1_FLAT) - 1  # minus modeSetid


def test_build_write_payload_ai_plus_preserves_surplus_null():
    """AI+ has surplus=None (not 0 like legacy) — verify it passes through."""
    result = build_write_payload(
        MOCK_MODE_SETTINGS_AI_PLUS_PORT1, {}, ControllerType.NEW_FRAMEWORK
    )
    assert result["surplus"] is None


# ============ parse_history_record on the AI+ fixture (P2-F020) ============


def test_parse_ai_plus_history_record_decodes_ports():
    """AI_PLUS_HISTORY_RECORD has 2 ports with port1=speed5 active, port2 idle."""
    client = ACInfinityClient("test@example.com", "pw")
    result = client.parse_history_record(AI_PLUS_HISTORY_RECORD)
    assert len(result["ports"]) == 2
    assert result["ports"][0]["speed"] == 5
    assert result["ports"][0]["on"] is True
    assert result["ports"][1]["speed"] == 0
    assert result["ports"][1]["on"] is False
    assert result["temperature_c"] == 23.5


# ============ AI+ write path — iOS app headers ============
#
# The ENTIRE AI+ write fix is the request headers. With the default
# okhttp/3.10.0 header set the API returns 100001 even given a correct payload;
# with the iOS app headers the ordinary merged read-before-write payload
# succeeds for manual control AND automation targets alike.
#
# Verified on live devType=20 hardware (connected port):
#   merged payload + okhttp headers -> 100001
#   merged payload + iOS headers    -> 200 (manual on/off/speed)
#   merged payload + iOS headers    -> 200 (humidity trigger changed)
#   merged payload + iOS headers    -> 200 (VPD target changed)

AI_PLUS_HEADER_KEYS = ("phoneType", "appVersion", "minversion")


def _capture_write_headers(client, device, updates):
    """Run a live-write and return the headers the client would send."""
    captured = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"code": 200, "msg": "success."}

    def _fake_post(url, data=None, headers=None, timeout=None):
        captured.update(headers or {})
        return _Resp()

    settings = dict(MOCK_MODE_SETTINGS_AI_PLUS_PORT1)
    with patch.object(client.session, "post", _fake_post), \
         patch.object(client, "_enforce_write_rate_limit"), \
         patch.object(client, "get_mode_settings", return_value=settings):
        client.set_port_mode(device, port=1, updates=updates, dry_run=False)
    return captured


def test_ai_plus_write_adds_ios_headers(ai_plus_device):
    """AI+ writes must carry the iOS app headers, or the API returns 100001."""
    c = ACInfinityClient("test@example.com", "pw")
    c.token = "tok"
    headers = _capture_write_headers(c, ai_plus_device, {"onSpead": 5})
    for key in AI_PLUS_HEADER_KEYS:
        assert key in headers, f"AI+ write missing required header {key}"
    assert "Alamofire" in headers["User-Agent"]
    assert "okhttp" not in headers["User-Agent"]


def test_legacy_write_keeps_okhttp_headers(legacy_11_device):
    """Legacy controllers must be untouched — no iOS headers, okhttp preserved."""
    c = ACInfinityClient("test@example.com", "pw")
    c.token = "tok"
    headers = _capture_write_headers(c, legacy_11_device, {"onSpead": 5})
    for key in AI_PLUS_HEADER_KEYS:
        assert key not in headers, f"legacy write should not carry {key}"
    assert headers["User-Agent"] == "okhttp/3.10.0"


def test_ai_plus_automation_write_is_not_refused(ai_plus_device):
    """Automation-target writes on AI+ must send, not return ai_plus_write_unsupported."""
    c = ACInfinityClient("test@example.com", "pw")
    c.token = "tok"

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"code": 200, "msg": "success."}

    with patch.object(client_mod.ACInfinityClient, "get_mode_settings",
                      return_value=dict(MOCK_MODE_SETTINGS_AI_PLUS_PORT1)), \
         patch.object(c.session, "post", lambda *a, **k: _Resp()), \
         patch.object(c, "_enforce_write_rate_limit"):
        result = c.set_port_mode(
            ai_plus_device, port=1,
            updates={"atType": 8, "targetVpd": 12, "targetVpdSwitch": 1},
            dry_run=False,
        )
    assert result["sent"] is True
    assert "ai_plus_write_unsupported" not in result
