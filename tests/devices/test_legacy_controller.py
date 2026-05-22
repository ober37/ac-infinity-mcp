"""Tests for legacy controller behavior (devType 11 and 18)."""

import pytest

from ac_infinity_mcp.client import ACInfinityClient
from ac_infinity_mcp.controller import ControllerType, build_write_payload, detect_controller_type
from tests.fixtures.legacy_device_fixtures import LEGACY_HISTORY_RECORD
from tests.fixtures.mock_mode_settings_legacy import (
    MOCK_MODE_SETTINGS_LEGACY_PORT1,
    MOCK_MODE_SETTINGS_LEGACY_PORT1_FLAT,
)

# ============ detect_controller_type ============

def test_detect_controller_type_devtype_11():
    assert detect_controller_type({"devType": 11}) == ControllerType.LEGACY


def test_detect_controller_type_devtype_18():
    assert detect_controller_type({"devType": 18}) == ControllerType.LEGACY


def test_detect_controller_type_defaults_to_legacy():
    assert detect_controller_type({}) == ControllerType.LEGACY


def test_detect_controller_type_new_framework_flag_overrides_legacy_devtype():
    result = detect_controller_type({"devType": 11, "newFrameworkDevice": True})
    assert result == ControllerType.NEW_FRAMEWORK


def test_detect_controller_type_legacy_11_fixture(legacy_11_device):
    assert detect_controller_type(legacy_11_device) == ControllerType.LEGACY


def test_detect_controller_type_legacy_18_fixture(legacy_18_device):
    assert detect_controller_type(legacy_18_device) == ControllerType.LEGACY


# ============ build_write_payload — legacy path ============

def test_build_write_payload_legacy_merges_updates():
    result = build_write_payload(
        MOCK_MODE_SETTINGS_LEGACY_PORT1, {"onSpead": 8}, ControllerType.LEGACY
    )
    assert result["onSpead"] == 8


def test_build_write_payload_legacy_preserves_untouched_fields():
    result = build_write_payload(
        MOCK_MODE_SETTINGS_LEGACY_PORT1, {"onSpead": 3}, ControllerType.LEGACY
    )
    assert result["externalPort"] == 1
    assert result["offSpead"] == 0
    assert result["devHt"] == 90


def test_build_write_payload_legacy_strips_modeSetid(quirk_11):
    """Quirk 11: modeSetid in payload causes 403 for legacy controllers."""
    result = build_write_payload(
        MOCK_MODE_SETTINGS_LEGACY_PORT1, {}, ControllerType.LEGACY
    )
    assert "modeSetid" not in result


def test_build_write_payload_legacy_modeType_2_when_speed_nonzero(quirk_12):
    """Quirk 12: modeType must be 2 when onSpead > 0 or change does not persist."""
    result = build_write_payload(
        MOCK_MODE_SETTINGS_LEGACY_PORT1, {"onSpead": 5}, ControllerType.LEGACY
    )
    assert result["modeType"] == 2


def test_build_write_payload_legacy_modeType_not_forced_when_speed_zero():
    current = {**MOCK_MODE_SETTINGS_LEGACY_PORT1, "modeType": 0}
    result = build_write_payload(current, {"onSpead": 0}, ControllerType.LEGACY)
    assert result["modeType"] == 0


def test_build_write_payload_legacy_excludes_devSetting(quirk_13):
    """Quirk 13: devSetting is a nested dict — cannot be form-encoded."""
    result = build_write_payload(
        MOCK_MODE_SETTINGS_LEGACY_PORT1, {}, ControllerType.LEGACY
    )
    assert "devSetting" not in result


def test_build_write_payload_legacy_excludes_fieldSet():
    result = build_write_payload(
        MOCK_MODE_SETTINGS_LEGACY_PORT1, {}, ControllerType.LEGACY
    )
    assert "fieldSet" not in result


def test_build_write_payload_legacy_flat_field_count():
    """Payload should have ~138 flat fields (140 flat minus modeSetid and fieldSet exclusions)."""
    result = build_write_payload(
        MOCK_MODE_SETTINGS_LEGACY_PORT1, {}, ControllerType.LEGACY
    )
    # modeSetid stripped, fieldSet stripped, devSetting stripped; ipcSetting (None) stays
    assert len(result) == len(MOCK_MODE_SETTINGS_LEGACY_PORT1_FLAT) - 1  # minus modeSetid


def test_build_write_payload_legacy_new_field_in_updates_is_added():
    result = build_write_payload(
        MOCK_MODE_SETTINGS_LEGACY_PORT1,
        {"customField": 99},
        ControllerType.LEGACY,
    )
    assert result["customField"] == 99


def test_build_write_payload_converts_booleans_to_int():
    """Bool fields (isUpdateVpdNums, restore) must be 0/1 not True/False for form encoding."""
    settings = {**MOCK_MODE_SETTINGS_LEGACY_PORT1, "isUpdateVpdNums": False, "restore": True}
    result = build_write_payload(settings, {}, ControllerType.LEGACY)
    assert result["isUpdateVpdNums"] == 0
    assert result["restore"] == 1
    assert not isinstance(result["isUpdateVpdNums"], bool)
    assert not isinstance(result["restore"], bool)


def test_build_write_payload_from_flat_fixture():
    """Using the pre-filtered flat fixture as input mirrors the actual client call path."""
    result = build_write_payload(
        MOCK_MODE_SETTINGS_LEGACY_PORT1_FLAT, {"onSpead": 7}, ControllerType.LEGACY
    )
    assert result["onSpead"] == 7
    assert "modeSetid" not in result


# ============ parse_history_record on the legacy fixture (P2-F020) ============


def test_parse_legacy_history_record_decodes_ports():
    """LEGACY_HISTORY_RECORD encodes port1=speed5 and port2=speed7 in portSpead."""
    client = ACInfinityClient("test@example.com", "pw")
    result = client.parse_history_record(LEGACY_HISTORY_RECORD)
    assert len(result["ports"]) == 4
    assert result["ports"][0]["speed"] == 5
    assert result["ports"][0]["on"] is True
    assert result["ports"][1]["speed"] == 7
    assert result["ports"][1]["on"] is True
    # Ports 3 and 4 are idle (nibbles 0)
    assert result["ports"][2]["on"] is False
    assert result["ports"][3]["on"] is False


# ============ Quirk markers (informational — no runtime effect) ============

@pytest.fixture
def quirk_11():
    """Marker: test verifies Quirk 11 — never send modeSetid for legacy."""


@pytest.fixture
def quirk_12():
    """Marker: test verifies Quirk 12 — modeType=2 when onSpead > 0."""


@pytest.fixture
def quirk_13():
    """Marker: test verifies Quirk 13 — read-before-write, all flat fields required."""
