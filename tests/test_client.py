"""Unit tests for ac_infinity_mcp.client module-level helpers."""

import pytest

from ac_infinity_mcp.automation import _decode_rule
from ac_infinity_mcp.client import build_add_groups_payload, build_groups_payload

_REPRESENTATIVE_KWARGS = dict(
    dev_id="ABC123",
    port=3,
    clean_name="Night Cycle",
    on_speed=7,
    begin_time=1320,
    end_time=1439,
)


def test_build_add_groups_payload_required_fields():
    """All required top-level fields are present in output."""
    payload = build_add_groups_payload(**_REPRESENTATIVE_KWARGS)
    assert payload["advName"] == "Night Cycle"
    assert payload["onSpeed"] == 7
    assert payload["beginTime"] == 1320
    assert payload["endTime"] == 1439
    # Port 3 → 2^(3-1) = 4
    assert payload["grouptDevType"] == 4


def test_build_add_groups_payload_field_count():
    """Payload contains the expected ~50 fields — catches accidental omissions."""
    payload = build_add_groups_payload(**_REPRESENTATIVE_KWARGS)
    assert len(payload) >= 45


def test_build_add_groups_payload_returns_dict():
    """Return value is a plain dict."""
    payload = build_add_groups_payload(**_REPRESENTATIVE_KWARGS)
    assert isinstance(payload, dict)


def test_build_add_groups_payload_port_bitmask():
    """grouptDevType bitmask is computed correctly for each port number."""
    for port in range(1, 9):
        payload = build_add_groups_payload(
            dev_id="X",
            port=port,
            clean_name="Test",
            on_speed=5,
            begin_time=0,
            end_time=1439,
        )
        assert payload["grouptDevType"] == 2 ** (port - 1)


def test_build_add_groups_payload_schedule_always_active_sentinel():
    """Sentinel value 255 for begin_time/end_time is mapped to full-day range."""
    payload = build_add_groups_payload(
        dev_id="X",
        port=1,
        clean_name="Always On",
        on_speed=5,
        begin_time=255,
        end_time=255,
    )
    assert payload["beginTime"] == 0
    assert payload["endTime"] == 1439


def test_build_add_groups_payload_devid_not_in_payload():
    """devId is intentionally excluded — _create_advance_automation_inner injects it."""
    payload = build_add_groups_payload(**_REPRESENTATIVE_KWARGS)
    assert "devId" not in payload


def test_build_add_groups_payload_switchtime_is_127():
    """switchTime must be 127 (all 7 days); 255 causes Continuous mode in app."""
    payload = build_add_groups_payload(**_REPRESENTATIVE_KWARGS)
    assert payload["switchTime"] == 127


# ============ Issue #284 — build_groups_payload golden-payload regression ============


@pytest.mark.parametrize("port", [1, 4, 8])
def test_build_groups_payload_byte_identical_to_shim_on_mode(port):
    """build_groups_payload(ports=[P], mode="on") is byte-identical to the legacy
    single-port On-mode builder — pins the single→list bitmask invariant (Python BLOCKING-2)."""
    legacy = build_add_groups_payload(
        dev_id="X", port=port, clean_name="Test", on_speed=5,
        begin_time=0, end_time=1439,
    )
    new = build_groups_payload(
        dev_id="X", ports=[port], clean_name="Test", on_speed=5,
        begin_time=0, end_time=1439, mode="on", adv_id=None,
    )
    assert new == legacy
    assert new["grouptDevType"] == 2 ** (port - 1)


def test_build_groups_payload_multi_port_bitmask():
    """A multi-port rule ORs the per-port bits into one grouptDevType."""
    payload = build_groups_payload(
        dev_id="X", ports=[5, 6], clean_name="Fans", on_speed=3,
        begin_time=0, end_time=1439, mode="on",
    )
    assert payload["grouptDevType"] == 48  # 2^4 + 2^5


def test_build_groups_payload_adv_id_only_when_set():
    """advId appears only on the update path (adv_id not None)."""
    no_id = build_groups_payload(
        dev_id="X", ports=[1], clean_name="T", on_speed=1,
        begin_time=0, end_time=1439, mode="on",
    )
    assert "advId" not in no_id
    with_id = build_groups_payload(
        dev_id="X", ports=[1], clean_name="T", on_speed=1,
        begin_time=0, end_time=1439, mode="on", adv_id=12345,
    )
    assert with_id["advId"] == 12345


# ============ Per-mode signature (table-driven) ============


def test_build_groups_payload_cycle_signature():
    p = build_groups_payload(
        dev_id="X", ports=[4], clean_name="C", on_speed=1, begin_time=540, end_time=1020,
        mode="cycle", targets={"cycle_on_minutes": 60, "cycle_off_minutes": 120},
    )
    assert p["currentMode"] == 3
    assert p["cycleOn"] == 60
    assert p["cycleOff"] == 120


def test_build_groups_payload_humidity_setpoint_signature():
    p = build_groups_payload(
        dev_id="X", ports=[1], clean_name="H", on_speed=2, begin_time=180, end_time=540,
        mode="humidity", targets={"target": 65},
    )
    assert p["currentMode"] == 4
    assert p["settingMode"] == 1
    assert p["targetHumi"] == 65
    assert p["autoLowHumiSwitch"] == 0
    assert p["autoHighHumiSwitch"] == 0


def test_build_groups_payload_humidity_trigger_signature():
    p = build_groups_payload(
        dev_id="X", ports=[1], clean_name="H", on_speed=2, begin_time=0, end_time=1439,
        mode="humidity", targets={"low": 40, "high": 80, "direction": "both"},
    )
    assert p["currentMode"] == 4
    assert p["settingMode"] == 0
    assert p["autoLowHumi"] == 40
    assert p["autoHighHumi"] == 80
    assert p["autoLowHumiSwitch"] == 1
    assert p["autoHighHumiSwitch"] == 1


def test_build_groups_payload_temperature_signature():
    p = build_groups_payload(
        dev_id="X", ports=[2], clean_name="T", on_speed=10, begin_time=0, end_time=1439,
        mode="temperature", targets={"low_f": 60, "high_f": 82, "direction": "both"},
    )
    assert p["currentMode"] == 4
    assert p["setSelect"] == 1
    assert p["autoLowTempF"] == 60
    assert p["autoHighTempF"] == 82
    assert p["autoLowTempSwitch"] == 1
    assert p["autoHighTempSwitch"] == 1
    # °C derived, not separately supplied.
    assert p["autoLowTempC"] == round((60 - 32) * 5 / 9)
    assert p["autoHighTempC"] == round((82 - 32) * 5 / 9)


def test_build_groups_payload_vpd_signature():
    """VPD: target_kpa=0.9 → targetVpd=9, highVpd=9, highVpdSwitch=1, lowVpd=0, lowVpdSwitch=0."""
    p = build_groups_payload(
        dev_id="X", ports=[1], clean_name="V", on_speed=2, begin_time=540, end_time=180,
        mode="vpd", targets={"target_kpa": 0.9},
    )
    assert p["currentMode"] == 6
    assert p["settingMode"] == 1
    assert p["targetVpd"] == 9
    assert p["highVpd"] == 9
    assert p["highVpdSwitch"] == 1
    assert p["lowVpd"] == 0
    assert p["lowVpdSwitch"] == 0


# ============ Single-direction switch selection ============


@pytest.mark.parametrize("direction,low_sw,high_sw", [
    ("on_below", 1, 0),
    ("on_above", 0, 1),
    ("both", 1, 1),
])
def test_temperature_direction_switch_selection(direction, low_sw, high_sw):
    p = build_groups_payload(
        dev_id="X", ports=[2], clean_name="T", on_speed=5, begin_time=0, end_time=1439,
        mode="temperature", targets={"low_f": 60, "high_f": 82, "direction": direction},
    )
    assert p["autoLowTempSwitch"] == low_sw
    assert p["autoHighTempSwitch"] == high_sw


@pytest.mark.parametrize("direction,low_sw,high_sw", [
    ("on_below", 1, 0),
    ("on_above", 0, 1),
    ("both", 1, 1),
])
def test_humidity_trigger_direction_switch_selection(direction, low_sw, high_sw):
    p = build_groups_payload(
        dev_id="X", ports=[1], clean_name="H", on_speed=5, begin_time=0, end_time=1439,
        mode="humidity", targets={"low": 40, "high": 80, "direction": direction},
    )
    assert p["autoLowHumiSwitch"] == low_sw
    assert p["autoHighHumiSwitch"] == high_sw


# ============ Round-trip: build → _decode_rule (QA BLOCKING-2) ============


@pytest.mark.parametrize("mode,targets,speed,expected_control,expected_direction", [
    ("on", {}, 7, "runs at speed 7", None),
    ("cycle", {"cycle_on_minutes": 30, "cycle_off_minutes": 90}, 1,
     "cycle 30 min on / 90 min off", None),
    ("vpd", {"target_kpa": 0.9}, 2, "hold VPD at 0.9 kPa", None),
    ("humidity", {"target": 65}, 2, "hold humidity at 65%", None),
    ("humidity", {"low": 40, "high": 80, "direction": "on_below"}, 3,
     "run when humidity drops below 40%", "on_below"),
    ("humidity", {"low": 40, "high": 80, "direction": "on_above"}, 3,
     "run when humidity rises above 80%", "on_above"),
    ("temperature", {"low_f": 60, "high_f": 82, "direction": "on_above"}, 10,
     "run when temp rises above 82°F", "on_above"),
    ("temperature", {"low_f": 60, "high_f": 82, "direction": "on_below"}, 10,
     "run when temp drops below 60°F", "on_below"),
])
def test_build_then_decode_round_trip(mode, targets, speed, expected_control, expected_direction):
    payload = build_groups_payload(
        dev_id="X", ports=[1], clean_name="RT", on_speed=speed,
        begin_time=0, end_time=1439, mode=mode, targets=targets,
    )
    decoded = _decode_rule(payload)
    assert decoded["mode"] == mode
    assert decoded["control"] == expected_control
    assert decoded["direction"] == expected_direction
