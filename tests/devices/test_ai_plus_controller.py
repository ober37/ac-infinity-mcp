"""Tests for AI+ controller behavior (devType 20+, newFrameworkDevice=True)."""

from ac_infinity_mcp.client import ACInfinityClient
from ac_infinity_mcp.controller import (
    ControllerType,
    build_ai_plus_manual_write_payload,
    build_write_payload,
    detect_controller_type,
)
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


# ============ build_ai_plus_manual_write_payload — AI+ manual writes ============
#
# AI+ rejects the legacy merged read-before-write payload. Two things are needed
# together: the static zeroed payload below, AND the iOS app headers set in
# client._set_port_mode_inner. Confirmed on live devType=20 hardware:
#   static payload + default okhttp headers -> 100001
#   merged payload + iOS headers            -> 999999
#   static payload + iOS headers            -> 200


def test_ai_plus_manual_payload_field_count():
    """75 fields, matching the community Charles Proxy capture of the iOS app."""
    result = build_ai_plus_manual_write_payload("20001", 1, {"atType": 2, "onSpead": 10})
    assert len(result) == 75


def test_ai_plus_manual_payload_carries_dev_id_and_port():
    result = build_ai_plus_manual_write_payload("20001", 6, {"onSpead": 5})
    assert result["devId"] == "20001"
    assert result["externalPort"] == 6


def test_ai_plus_manual_payload_speed_only_sets_on_mode():
    """Regression: a bare speed update must not leave atType at the template's OFF default.

    set_port_speed sends {"onSpead": speed} with no atType. Seeding atType from
    the static template (1 = OFF) produced atType=1 alongside modeType=2 (ON) —
    a self-contradictory payload that puts the port in OFF mode while the tool
    reports success, silently stopping a running fan.
    """
    result = build_ai_plus_manual_write_payload("20001", 2, {"onSpead": 8})
    assert result["onSpead"] == 8
    assert result["atType"] == 2, "speed>0 must select ON mode, not the template default"
    assert result["modeType"] == 2


def test_ai_plus_manual_payload_on():
    result = build_ai_plus_manual_write_payload("20001", 1, {"atType": 2, "onSpead": 10})
    assert (result["atType"], result["modeType"], result["onSpead"]) == (2, 2, 10)


def test_ai_plus_manual_payload_off():
    result = build_ai_plus_manual_write_payload("20001", 1, {"onSpead": 0, "atType": 1})
    assert (result["atType"], result["modeType"], result["onSpead"]) == (1, 0, 0)


def test_ai_plus_manual_payload_at_type_and_mode_type_never_contradict():
    """atType and modeType must always agree about ON vs OFF, for every speed."""
    for speed in range(0, 11):
        result = build_ai_plus_manual_write_payload("20001", 1, {"onSpead": speed})
        assert (result["atType"] == 2) == (result["modeType"] == 2), (
            f"contradiction at onSpead={speed}"
        )


def test_ai_plus_manual_payload_mirrors_on_self_spead_by_default():
    result = build_ai_plus_manual_write_payload("20001", 1, {"onSpead": 7})
    assert result["onSelfSpead"] == 7


def test_ai_plus_manual_payload_respects_explicit_on_self_spead():
    """An explicitly supplied onSelfSpead must not be silently overwritten."""
    result = build_ai_plus_manual_write_payload("20001", 1, {"onSpead": 10, "onSelfSpead": 6})
    assert result["onSelfSpead"] == 6


def test_ai_plus_manual_payload_omits_mode_set_id():
    """Quirk 11: modeSetid must never be sent."""
    result = build_ai_plus_manual_write_payload("20001", 1, {"onSpead": 5})
    assert "modeSetid" not in result


def test_ai_plus_manual_payload_zeroes_automation_fields():
    """The static template deliberately carries no real automation/threshold state.

    This is why automation writes stay refused on AI+ — sending this payload for
    a VPD/temp/humidity target would zero the port's other automation settings.
    """
    result = build_ai_plus_manual_write_payload("20001", 1, {"onSpead": 5})
    for field in ("targetVpd", "targetTemp", "targetHumi", "activeHt", "activeLh"):
        assert result[field] == 0
    assert result["isOpenAutomation"] == 0
