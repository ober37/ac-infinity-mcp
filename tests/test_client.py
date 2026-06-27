"""Unit tests for ac_infinity_mcp.client module-level helpers (Issue #284 compositional)."""

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


# ============ Legacy On-mode shim (byte-identity must hold) ============


def test_build_add_groups_payload_required_fields():
    payload = build_add_groups_payload(**_REPRESENTATIVE_KWARGS)
    assert payload["advName"] == "Night Cycle"
    assert payload["onSpeed"] == 7
    assert payload["beginTime"] == 1320
    assert payload["endTime"] == 1439
    assert payload["grouptDevType"] == 4  # Port 3 → 2^(3-1)


def test_build_add_groups_payload_field_count():
    payload = build_add_groups_payload(**_REPRESENTATIVE_KWARGS)
    assert len(payload) >= 45


def test_build_add_groups_payload_returns_dict():
    assert isinstance(build_add_groups_payload(**_REPRESENTATIVE_KWARGS), dict)


def test_build_add_groups_payload_port_bitmask():
    for port in range(1, 9):
        payload = build_add_groups_payload(
            dev_id="X", port=port, clean_name="Test", on_speed=5, begin_time=0, end_time=1439,
        )
        assert payload["grouptDevType"] == 2 ** (port - 1)


def test_build_add_groups_payload_schedule_always_active_sentinel():
    payload = build_add_groups_payload(
        dev_id="X", port=1, clean_name="Always On", on_speed=5, begin_time=255, end_time=255,
    )
    assert payload["beginTime"] == 0
    assert payload["endTime"] == 1439


def test_build_add_groups_payload_devid_not_in_payload():
    assert "devId" not in build_add_groups_payload(**_REPRESENTATIVE_KWARGS)


def test_build_add_groups_payload_switchtime_is_127():
    assert build_add_groups_payload(**_REPRESENTATIVE_KWARGS)["switchTime"] == 127


@pytest.mark.parametrize("port", [1, 4, 8])
def test_build_groups_payload_byte_identical_to_shim_on_mode(port):
    """build_groups_payload(ports=[P], mode="on", on_speed=N) is byte-identical to the
    legacy single-port On-mode builder — pins the single→list bitmask invariant."""
    legacy = build_add_groups_payload(
        dev_id="X", port=port, clean_name="Test", on_speed=5, begin_time=0, end_time=1439,
    )
    new = build_groups_payload(
        dev_id="X", ports=[port], clean_name="Test", begin_time=0, end_time=1439,
        mode="on", on_speed=5, adv_id=None,
    )
    assert new == legacy
    assert new["grouptDevType"] == 2 ** (port - 1)


def test_build_groups_payload_multi_port_bitmask():
    payload = build_groups_payload(
        dev_id="X", ports=[5, 6], clean_name="Fans", begin_time=0, end_time=1439,
        mode="on", on_speed=3,
    )
    assert payload["grouptDevType"] == 48  # 2^4 + 2^5


def test_build_groups_payload_adv_id_only_when_set():
    no_id = build_groups_payload(
        dev_id="X", ports=[1], clean_name="T", begin_time=0, end_time=1439,
        mode="on", on_speed=1,
    )
    assert "advId" not in no_id
    with_id = build_groups_payload(
        dev_id="X", ports=[1], clean_name="T", begin_time=0, end_time=1439,
        mode="on", on_speed=1, adv_id=12345,
    )
    assert with_id["advId"] == 12345


def test_build_groups_payload_no_caller_param_spread():
    """An unknown caller key never reaches the payload (explicit per-field assignment)."""
    payload = build_groups_payload(
        dev_id="X", ports=[1], clean_name="T", begin_time=0, end_time=1439, mode="on",
        max_level=4,
    )
    assert "max_level" not in payload
    assert "mode" not in payload


# ============ Golden signatures per mode/sub-mode (capture program "0624") ============
#
# Each golden asserts the EXACT captured byte signature for the field set that defines the
# mode. Substitute the user's value/direction where noted.

# Field tuple extended (R2) so the matrix is not blind to speed-range / buffer / transition /
# switchTime — these are asserted against the captured 0624 values.
_SIG_FIELDS = (
    "currentMode", "setSelect", "settingMode",
    "autoHighTempF", "autoLowTempF", "autoHighTempC", "autoLowTempC",
    "autoHighTempSwitch", "autoLowTempSwitch",
    "autoHighHumi", "autoLowHumi", "autoHighHumiSwitch", "autoLowHumiSwitch",
    "highVpd", "lowVpd", "highVpdSwitch", "lowVpdSwitch",
    "targetTempF", "targetHumi", "targetVpd",
    "targetTSwitch", "targetHumiSwitch", "targetVpdSwitch",
    "offSpeed", "onSpeed",
    "temperatureFBuff", "humidityBuff", "vpdBuff",
    "temperatureFTrans", "humidityTrans", "vpdTrans",
    "switchTime",
)


def _sig(payload):
    return {f: payload.get(f) for f in _SIG_FIELDS}


def test_golden_auto_trigger_combined_temp_humidity():
    """Rule 1 (0624): Auto-trigger, temp 60–85 + humidity 50–70, MIN 2/MAX 8, buff 3/5."""
    p = build_groups_payload(
        dev_id="X", ports=[4], clean_name="Seedling", begin_time=540, end_time=180,
        mode="auto", control_style="trigger",
        temp_high_f=85, temp_low_f=60, humidity_high=70, humidity_low=50,
        min_level=2, max_level=8, temp_buffer=3, humidity_buffer=5, switch_time=127,
    )
    assert _sig(p) == {
        "currentMode": 4, "setSelect": 1, "settingMode": 0,
        "autoHighTempF": 85, "autoLowTempF": 60, "autoHighTempC": 90, "autoLowTempC": 0,
        "autoHighTempSwitch": 1, "autoLowTempSwitch": 1,
        "autoHighHumi": 70, "autoLowHumi": 50, "autoHighHumiSwitch": 1, "autoLowHumiSwitch": 1,
        # VPD family is zeroed in Auto mode (#288 — app does not park it at the rail here).
        "highVpd": 0, "lowVpd": 0, "highVpdSwitch": 0, "lowVpdSwitch": 0,
        "targetTempF": 32, "targetHumi": 0, "targetVpd": 0,
        "targetTSwitch": 1, "targetHumiSwitch": 1, "targetVpdSwitch": 0,
        "offSpeed": 2, "onSpeed": 8,
        "temperatureFBuff": 3, "humidityBuff": 5, "vpdBuff": 0,
        "temperatureFTrans": 0, "humidityTrans": 0, "vpdTrans": 0,
        "switchTime": 127,
    }


def test_golden_auto_target_humidity():
    """Rule 2 (0624): Auto-target humidity 65, MIN 1/MAX 10, continuous (255), trans 2/4."""
    p = build_groups_payload(
        dev_id="X", ports=[1], clean_name="Seedling", begin_time=540, end_time=1020,
        mode="auto", control_style="target", humidity_target=65,
        min_level=1, max_level=10, temp_transition=2, humidity_transition=4, switch_time=255,
    )
    assert _sig(p) == {
        "currentMode": 4, "setSelect": 0, "settingMode": 1,
        "autoHighTempF": 194, "autoLowTempF": 32, "autoHighTempC": 90, "autoLowTempC": 0,
        "autoHighTempSwitch": 1, "autoLowTempSwitch": 1,
        "autoHighHumi": 100, "autoLowHumi": 0, "autoHighHumiSwitch": 1, "autoLowHumiSwitch": 1,
        # VPD family is zeroed in Auto mode (#288).
        "highVpd": 0, "lowVpd": 0, "highVpdSwitch": 0, "lowVpdSwitch": 0,
        "targetTempF": 32, "targetHumi": 65, "targetVpd": 0,
        "targetTSwitch": 1, "targetHumiSwitch": 1, "targetVpdSwitch": 0,
        "offSpeed": 1, "onSpeed": 10,
        "temperatureFBuff": 0, "humidityBuff": 0, "vpdBuff": 0,
        "temperatureFTrans": 2, "humidityTrans": 4, "vpdTrans": 0,
        "switchTime": 255,
    }


def test_golden_vpd_target():
    """VPD-target (matches the app's Clone Transplant signature, #288): the setpoint is
    mirrored into targetVpd AND highVpd (highVpdSwitch=1); lowVpd off; and all the auto
    temp/humidity + temp/humidity-target families are zeroed (inert in VPD mode)."""
    p = build_groups_payload(
        dev_id="X", ports=[1], clean_name="V", begin_time=0, end_time=1439,
        mode="vpd", control_style="target", vpd_target=1.2,
    )
    assert p["currentMode"] == 6
    assert p["settingMode"] == 1
    assert p["setSelect"] == 0
    assert p["targetVpd"] == 12
    assert p["targetVpdSwitch"] == 1
    assert p["highVpd"] == 12          # mirrors the setpoint (app behavior), not the 99 rail
    assert p["highVpdSwitch"] == 1
    assert p["lowVpd"] == 0
    assert p["lowVpdSwitch"] == 0      # low off (was wrongly 1 before #288 fix)
    # Auto + temp/humidity-target families are inert/zeroed in VPD mode.
    assert p["autoHighHumiSwitch"] == 0 and p["autoLowHumiSwitch"] == 0
    assert p["autoHighTempSwitch"] == 0 and p["autoLowTempSwitch"] == 0
    assert p["targetHumiSwitch"] == 0 and p["targetTSwitch"] == 0


def test_golden_vpd_trigger():
    """VPD-trigger captured: highVpd=15 (1.5), lowVpd=8 (0.8), both switches=1."""
    p = build_groups_payload(
        dev_id="X", ports=[1], clean_name="V", begin_time=0, end_time=1439,
        mode="vpd", control_style="trigger", vpd_high=1.5, vpd_low=0.8,
    )
    assert p["currentMode"] == 6
    assert p["settingMode"] == 0
    assert p["setSelect"] == 0
    assert p["highVpd"] == 15
    assert p["highVpdSwitch"] == 1
    assert p["lowVpd"] == 8
    assert p["lowVpdSwitch"] == 1


def test_golden_off():
    """Off rule: currentMode=2; the port is forced off."""
    p = build_groups_payload(
        dev_id="X", ports=[1], clean_name="O", begin_time=0, end_time=1439, mode="off",
    )
    assert p["currentMode"] == 2


def test_golden_cycle():
    p = build_groups_payload(
        dev_id="X", ports=[4], clean_name="C", begin_time=540, end_time=1020,
        mode="cycle", cycle_on_minutes=60, cycle_off_minutes=120,
    )
    assert p["currentMode"] == 3
    # cycleOn/cycleOff are stored in SECONDS (minutes × 60); verified live.
    assert p["cycleOn"] == 3600
    assert p["cycleOff"] == 7200


# ============ Auto-trigger directional / single-sensor sub-cases ============


@pytest.mark.parametrize("kwargs,expect", [
    # temp-only, high only
    (dict(temp_high_f=85), {"autoHighTempF": 85, "autoHighTempSwitch": 1,
                            "autoLowTempF": 32, "autoLowTempSwitch": 0}),
    # temp-only, low only
    (dict(temp_low_f=60), {"autoLowTempF": 60, "autoLowTempSwitch": 1,
                           "autoHighTempF": 194, "autoHighTempSwitch": 0}),
    # humidity-only, high only
    (dict(humidity_high=70), {"autoHighHumi": 70, "autoHighHumiSwitch": 1,
                              "autoLowHumi": 0, "autoLowHumiSwitch": 0}),
    # humidity-only, low only
    (dict(humidity_low=50), {"autoLowHumi": 50, "autoLowHumiSwitch": 1,
                             "autoHighHumi": 100, "autoHighHumiSwitch": 0}),
])
def test_auto_trigger_single_sensor_directions(kwargs, expect):
    p = build_groups_payload(
        dev_id="X", ports=[1], clean_name="T", begin_time=0, end_time=1439,
        mode="auto", control_style="trigger", **kwargs,
    )
    for k, v in expect.items():
        assert p[k] == v, f"{k}={p[k]} expected {v}"


def test_auto_target_temp_passthrough():
    """temp_target_f is pass-through (device-gated; no frozen golden) — it lands in targetTempF."""
    p = build_groups_payload(
        dev_id="X", ports=[1], clean_name="T", begin_time=0, end_time=1439,
        mode="auto", control_style="target", temp_target_f=72,
    )
    assert p["targetTempF"] == 72
    assert p["settingMode"] == 1


# ============ Buffer / transition per sensor ============


@pytest.mark.parametrize("kwargs,field,val", [
    (dict(temp_buffer=3), "temperatureFBuff", 3),
    (dict(temp_transition=2), "temperatureFTrans", 2),
    (dict(humidity_buffer=5), "humidityBuff", 5),
    (dict(humidity_transition=4), "humidityTrans", 4),
])
def test_auto_buffer_transition_fields(kwargs, field, val):
    p = build_groups_payload(
        dev_id="X", ports=[1], clean_name="T", begin_time=0, end_time=1439,
        mode="auto", control_style="trigger", temp_high_f=80, **kwargs,
    )
    assert p[field] == val


def test_vpd_buffer_golden():
    """vpd_buffer lands in vpdBuff as kPa*10 (now that it's an exposed param)."""
    p = build_groups_payload(
        dev_id="X", ports=[1], clean_name="V", begin_time=0, end_time=1439,
        mode="vpd", control_style="target", vpd_target=1.2, vpd_buffer=0.3,
    )
    assert p["vpdBuff"] == 3
    assert p["vpdTrans"] == 0


def test_vpd_transition_golden():
    p = build_groups_payload(
        dev_id="X", ports=[1], clean_name="V", begin_time=0, end_time=1439,
        mode="vpd", control_style="trigger", vpd_high=1.5, vpd_transition=0.4,
    )
    assert p["vpdTrans"] == 4
    assert p["vpdBuff"] == 0


@pytest.mark.parametrize("kwargs,frag", [
    (dict(mode="auto", control_style="trigger", temp_high_f=80, temp_transition=2),
     "temperature transition 2°F"),
    (dict(mode="auto", control_style="trigger", humidity_high=70, humidity_buffer=5),
     "humidity buffer 5%"),
    (dict(mode="auto", control_style="trigger", humidity_high=70, humidity_transition=4),
     "humidity transition 4%"),
    (dict(mode="vpd", control_style="target", vpd_target=1.2, vpd_buffer=0.3),
     "VPD buffer 0.3 kPa"),
    (dict(mode="vpd", control_style="trigger", vpd_high=1.5, vpd_transition=0.4),
     "VPD transition 0.4 kPa"),
])
def test_buffer_transition_control_string_round_trip(kwargs, frag):
    """Buffer/transition modifiers render in the decoded control string for every sensor."""
    p = build_groups_payload(
        dev_id="X", ports=[1], clean_name="BT", begin_time=0, end_time=1439, **kwargs,
    )
    assert frag in _decode_rule(p)["control"]


def test_vpd_buffer_round_trip():
    p = build_groups_payload(
        dev_id="X", ports=[1], clean_name="V", begin_time=0, end_time=1439,
        mode="vpd", control_style="target", vpd_target=1.2, vpd_buffer=0.3,
    )
    decoded = _decode_rule(p)
    assert decoded["mode"] == "vpd"
    assert "VPD: hold at 1.2 kPa" in decoded["control"]
    assert "VPD buffer 0.3 kPa" in decoded["control"]


def test_vpd_trigger_on_below_only_round_trip():
    """A VPD on-below-only trigger reports the 'on below X kPa' clause + on_below direction."""
    p = build_groups_payload(
        dev_id="X", ports=[1], clean_name="V", begin_time=0, end_time=1439,
        mode="vpd", control_style="trigger", vpd_low=0.8,
    )
    decoded = _decode_rule(p)
    assert decoded["mode"] == "vpd"
    assert "VPD: on below 0.8 kPa" in decoded["control"]
    assert decoded["direction"] == "on_below"


# ============ Round-trip: build → _decode_rule per permutation ============


@pytest.mark.parametrize("mode,kwargs,expected_mode,must_contain,direction", [
    ("on", dict(on_speed=7), "on", ["runs at set speed", "speed"], None),
    ("off", {}, "off", ["off"], None),
    ("cycle", dict(cycle_on_minutes=30, cycle_off_minutes=90), "cycle",
     ["cycle 30 min on / 90 min off"], None),
    # Auto-target humidity
    ("auto", dict(control_style="target", humidity_target=65), "auto",
     ["humidity: hold at 65%"], None),
    # Auto-trigger combined
    ("auto", dict(control_style="trigger", temp_high_f=85, humidity_low=50), "auto",
     ["temperature: on above 85°F", "humidity: on below 50%"], None),
    # Auto-trigger single-sensor humidity (direction surfaced)
    ("auto", dict(control_style="trigger", humidity_high=70), "auto",
     ["humidity: on above 70%"], "on_above"),
    # VPD-target (float format)
    ("vpd", dict(control_style="target", vpd_target=1.2), "vpd", ["VPD: hold at 1.2 kPa"], None),
    # VPD-trigger both directions
    ("vpd", dict(control_style="trigger", vpd_high=1.5, vpd_low=0.8), "vpd",
     ["VPD: on above 1.5 or below 0.8 kPa"], "both"),
])
def test_build_then_decode_round_trip(mode, kwargs, expected_mode, must_contain, direction):
    payload = build_groups_payload(
        dev_id="X", ports=[1], clean_name="RT", begin_time=0, end_time=1439,
        mode=mode, **kwargs,
    )
    decoded = _decode_rule(payload)
    assert decoded["mode"] == expected_mode
    for frag in must_contain:
        assert frag in decoded["control"], f"'{frag}' not in '{decoded['control']}'"
    assert decoded["direction"] == direction


def test_round_trip_continuous_255():
    """switchTime=255 round-trips to 'runs continuously' (window suppressed)."""
    p = build_groups_payload(
        dev_id="X", ports=[1], clean_name="C", begin_time=540, end_time=1020,
        mode="on", on_speed=5, switch_time=255,
    )
    assert "runs continuously" in _decode_rule(p)["control"]
    assert "09:00" not in _decode_rule(p)["control"]


def test_round_trip_all_days_127():
    p = build_groups_payload(
        dev_id="X", ports=[1], clean_name="C", begin_time=540, end_time=1020,
        mode="on", on_speed=5, switch_time=127,
    )
    assert "every day 09:00–17:00" in _decode_rule(p)["control"]


def test_round_trip_weekdays_31():
    p = build_groups_payload(
        dev_id="X", ports=[1], clean_name="C", begin_time=540, end_time=1020,
        mode="on", on_speed=5, switch_time=31,
    )
    assert "Mon–Fri 09:00–17:00" in _decode_rule(p)["control"]


def test_round_trip_wrap_around_window_no_negative_duration():
    """A wrap-around window (begin > end) reports both clock times sensibly."""
    p = build_groups_payload(
        dev_id="X", ports=[1], clean_name="C", begin_time=540, end_time=180,
        mode="on", on_speed=5, switch_time=127,
    )
    assert "09:00–03:00" in _decode_rule(p)["control"]


# ============ isFlag / program-slot gating (Issue #284 append fix) ============


def test_build_groups_payload_default_is_new_program():
    """Defaults: isFlag=1 (new program), subNumber=0, slot at the new-program sentinel 9/9."""
    p = build_groups_payload(
        dev_id="X", ports=[1], clean_name="P", begin_time=0, end_time=1439,
        mode="on", on_speed=5,
    )
    assert p["isFlag"] == 1
    assert p["subNumber"] == 0
    assert p["subNumberSort"] == 0
    assert p["groupNums"] == 9
    assert p["sortType"] == 9


def test_build_groups_payload_append_honors_slot_and_subnumber():
    """isFlag=0 append carries the target program's slot + the next subNumber."""
    p = build_groups_payload(
        dev_id="X", ports=[2], clean_name="0624", begin_time=0, end_time=1439,
        mode="on", on_speed=5, is_flag=0, group_nums=1, sort_type=6, sub_number=2,
    )
    assert p["isFlag"] == 0
    assert p["groupNums"] == 1
    assert p["sortType"] == 6
    assert p["subNumber"] == 2
    assert p["subNumberSort"] == 2


# ============ Graceful decode of unhandled currentMode (robustness) ============


@pytest.mark.parametrize("mode", [5, 7])
def test_decode_unknown_current_mode_is_graceful(mode):
    """A currentMode the decoder doesn't handle returns 'unknown' — no KeyError/crash."""
    decoded = _decode_rule({"currentMode": mode})
    assert decoded["mode"] == "unknown"
    assert decoded["control"] == "unrecognized rule"
    assert decoded["direction"] is None
