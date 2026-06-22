"""Regression tests against real Controller 89 AI+ (devType=20) API captures.

These assert the field shapes that distinguish the 89 AI+ from the controllers we can
test directly, and lock in the corrected findings from the source-triangulation research
(see .claude/internal/CONTROLLER_89_AIPLUS_RESEARCH.md). They guard against:
  - reading the wrong mode-field spelling per endpoint (#242),
  - re-introducing the refuted loadId-as-empty-port theory (#243),
  - drift in the devType=20 detection threshold (#244).
"""

import pytest

from ac_infinity_mcp.controller import ControllerType, detect_controller_type
from ac_infinity_mcp.ports import _is_port_empty
from tests.fixtures.captures import CAPTURE_DATES, load_89_aiplus_capture

_EMPTY_PORT_SENTINEL = 65535  # portResistance / open-circuit sentinel (Quirk 26/27)


@pytest.fixture(params=CAPTURE_DATES)
def capture(request):
    """Each test runs once per pinned capture date."""
    return load_89_aiplus_capture(request.param)


@pytest.fixture
def device(capture):
    return capture["devices"][0]


def _ports(device):
    return device["portShapes"]


# ============ controller type detection (#244) ============


def test_detected_as_new_framework(device):
    assert detect_controller_type(device) == ControllerType.NEW_FRAMEWORK


def test_devtype_and_port_count(device):
    assert device["devType"] == 20  # Controller 89 AI+
    assert device["totalPortCount"] == 8
    ports = _ports(device)
    assert len(ports) == 8
    assert [p["port"] for p in ports] == list(range(1, 9))


# ============ mode-field spelling is endpoint-specific (#242) ============


def test_device_list_payload_uses_modeTye_typo(device):
    """The device-list/portValuesInList read uses AC's misspelling `modeTye`."""
    for ps in _ports(device):
        pv = ps["portValuesInList"]
        assert "modeTye" in pv, f"port {ps['port']} missing modeTye in device-list payload"
        # 89 AI+ ports sit under the Advance-Automation wrapper (modeType 15).
        assert pv["modeTye"] == 15


def test_mode_settings_payload_uses_correct_modeType(device):
    """The getdevModeSettingList read uses the correctly-spelled `modeType`."""
    for ps in _ports(device):
        data = ps["getdevModeSettingList"]["data"]
        assert data.get("modeType") == 15
        # atType is the active sub-mode; only 1 (OFF) / 2 (ON) appear in these captures.
        assert data.get("atType") in (1, 2)


# ============ empty-port signal is portResistance, NOT loadId (#243) ============


def test_empty_ports_use_portresistance_sentinel(device):
    """Empty/offline ports report portResistance == 65535; connected ports < 65535."""
    for ps in _ports(device):
        pv = ps["portValuesInList"]
        if pv["online"] == 0:
            assert pv["portResistance"] == _EMPTY_PORT_SENTINEL, (
                f"port {ps['port']} offline but portResistance != sentinel"
            )
        else:
            assert pv["portResistance"] < _EMPTY_PORT_SENTINEL, (
                f"port {ps['port']} online but portResistance == sentinel"
            )


def test_is_port_empty_classifies_aiplus_ports_correctly(device):
    """The production `_is_port_empty` (Quirk 26/27 portResistance==65535) already classifies
    devType=20 ports correctly — no AI-controller-specific logic needed. Online ports are
    reported connected; offline ports empty. This is the #243 verification: existing
    detection covers AI controllers; the original loadId-sentinel theory was unnecessary.
    """
    for ps in _ports(device):
        pv = ps["portValuesInList"]
        expected_empty = pv["online"] == 0
        assert _is_port_empty(pv, ps["port"], device) is expected_empty, (
            f"port {ps['port']} (online={pv['online']}, "
            f"portResistance={pv['portResistance']}) misclassified by _is_port_empty"
        )


def test_loadId_is_not_an_empty_port_sentinel(device):
    """Regression guard: loadId=65535 does NOT mean 'empty'. Connected, online ports
    report loadId=65535, so loadId must never be used for empty-port detection.
    """
    connected = [ps for ps in _ports(device) if ps["portValuesInList"]["online"] == 1]
    assert connected, "expected at least one connected port in the capture"
    assert any(ps["portValuesInList"]["loadId"] == _EMPTY_PORT_SENTINEL for ps in connected), (
        "expected a connected port with loadId=65535 — proving loadId is not an empty sentinel"
    )


def test_loadType_is_zero_on_all_ports(device):
    """89 AI+ reports loadType=0 for every port — not a usable device-routing signal."""
    assert all(ps["portValuesInList"]["loadType"] == 0 for ps in _ports(device))
