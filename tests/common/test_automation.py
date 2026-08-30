"""Unit tests for automation.py pure helpers and _build_advance_conflict_response."""

import copy
import json
from unittest.mock import MagicMock

from ac_infinity_mcp.automation import (
    _build_advance_conflict_response,
    _decode_rule,
    _find_governing_automation,
    _find_governing_port_group,
    _group_automations,
    _is_port_not_powered,
    _sanitize_api_string,
)
from ac_infinity_mcp.schema import ACInfinityAPIError, ACInfinityAuthError
from tests.fixtures.advance_automation_fixtures import (
    MOCK_RULE_HUMIDITY_SETPOINT,
    MOCK_RULE_TEMPERATURE_TRIGGER,
    MOCK_RULE_VPD,
    MOCK_TWO_WINDOW_PROGRAM,
)

# ============ _sanitize_api_string ============


def test_sanitize_normal_string_unchanged():
    assert _sanitize_api_string("Night Cycle") == "Night Cycle"


def test_sanitize_strips_cc_control_chars():
    assert _sanitize_api_string("hello\x00world") == "helloworld"


def test_sanitize_strips_cf_format_chars():
    # U+200B is zero-width space (Cf category)
    assert _sanitize_api_string("hello​world") == "helloworld"


def test_sanitize_preserves_non_ascii_printable():
    # Japanese characters are printable — must be preserved
    assert _sanitize_api_string("排気ファン") == "排気ファン"


def test_sanitize_truncates_to_max_len():
    long_str = "a" * 80
    result = _sanitize_api_string(long_str, max_len=64)
    assert len(result) == 64


def test_sanitize_none_returns_unnamed():
    assert _sanitize_api_string(None) == "(unnamed)"


def test_sanitize_empty_string_returns_unnamed():
    assert _sanitize_api_string("") == "(unnamed)"


def test_sanitize_all_control_chars_returns_unnamed():
    assert _sanitize_api_string("\x00\x01\x02") == "(unnamed)"


# ============ _group_automations ============


def test_group_automations_empty_list():
    assert _group_automations([]) == []


def test_group_automations_single_entry():
    raw = [{"advId": 100, "advName": "Night Cycle", "isOn": 1,
            "onSpeed": 5, "grouptDevType": 1, "runState": 1,
            "beginTime": 0, "endTime": 1439, "onTimeSwitch": 0}]
    result = _group_automations(raw)
    assert len(result) == 1
    g = result[0]
    assert g["automation_id"] == 100
    assert g["name"] == "Night Cycle"
    assert g["enabled"] is True
    assert g["adv_ids"] == [100]
    assert len(g["port_groups"]) == 1
    assert g["port_groups"][0]["on_speed"] == 5
    assert g["port_groups"][0]["grp_dev_type"] == 1


def test_group_automations_same_name_merged():
    raw = [
        {"advId": 100, "advName": "Cycle A", "isOn": 1, "onSpeed": 3,
         "grouptDevType": 1, "runState": 1, "beginTime": 0, "endTime": 1439, "onTimeSwitch": 0},
        {"advId": 200, "advName": "Cycle A", "isOn": 1, "onSpeed": 7,
         "grouptDevType": 2, "runState": 1, "beginTime": 0, "endTime": 1439, "onTimeSwitch": 0},
    ]
    result = _group_automations(raw)
    assert len(result) == 1
    g = result[0]
    assert g["automation_id"] == 100  # first entry's advId is canonical
    assert set(g["adv_ids"]) == {100, 200}
    assert len(g["port_groups"]) == 2


def test_group_automations_different_names_separate_groups_insertion_order():
    raw = [
        {"advId": 1, "advName": "Alpha", "isOn": 1, "onSpeed": 2,
         "grouptDevType": 1, "runState": 0, "beginTime": 0, "endTime": 1439, "onTimeSwitch": 0},
        {"advId": 2, "advName": "Beta", "isOn": 0, "onSpeed": 4,
         "grouptDevType": 2, "runState": 0, "beginTime": 0, "endTime": 1439, "onTimeSwitch": 0},
    ]
    result = _group_automations(raw)
    assert len(result) == 2
    # Insertion order preserved
    assert result[0]["name"] == "Alpha"
    assert result[1]["name"] == "Beta"


# ============ _decode_rule (Issue #284) ============


def test_decode_rule_off_mode():
    assert _decode_rule({"currentMode": 2}) == {
        "mode": "off", "control": "off", "direction": None,
    }


def test_decode_rule_on_mode():
    decoded = _decode_rule({"currentMode": 1, "onSpeed": 7, "offSpeed": 0})
    assert decoded["mode"] == "on"
    assert decoded["direction"] is None
    assert "runs at set speed" in decoded["control"]


def test_decode_rule_cycle_mode():
    # cycleOn/cycleOff are SECONDS on the device; decoder shows minutes = seconds/60.
    decoded = _decode_rule({"currentMode": 3, "cycleOn": 3600, "cycleOff": 7200,
                            "onSpeed": 5, "offSpeed": 0})
    assert decoded["mode"] == "cycle"
    assert "cycle 60 min on / 120 min off" in decoded["control"]


def test_decode_rule_vpd_target_div10():
    """Live VPD-target rule (targetVpd=9, currentMode=6) decodes to 0.9 kPa despite the
    rail VPD-trigger family (highVpd=99 / switch=1)."""
    decoded = _decode_rule(copy.deepcopy(MOCK_RULE_VPD))
    assert decoded["mode"] == "vpd"
    assert "VPD: hold at 0.9 kPa" in decoded["control"]
    assert decoded["direction"] is None


def test_decode_rule_auto_target_humidity_wins_over_rail_triggers():
    """currentMode=4 + settingMode=1 + targetHumi>0 decodes as a humidity hold; the
    rail-parked trigger families (switches=1) are correctly ignored (rail-sentinel rule)."""
    decoded = _decode_rule(copy.deepcopy(MOCK_RULE_HUMIDITY_SETPOINT))
    assert decoded["mode"] == "auto"
    assert "humidity: hold at 65%" in decoded["control"]
    # The rail-parked temperature trigger must NOT produce a clause.
    assert "temperature: on" not in decoded["control"]
    assert decoded["direction"] is None


def test_decode_rule_auto_trigger_on_below_rail_aware():
    """autoLowTempF=76 + switch=1 is active; autoHighTempF=194 (rail) is NOT. Decodes to a
    single on-below temperature trigger clause."""
    decoded = _decode_rule(copy.deepcopy(MOCK_RULE_TEMPERATURE_TRIGGER))
    assert decoded["mode"] == "auto"
    assert decoded["direction"] == "on_below"
    assert "temperature: on below 76°F" in decoded["control"]


def test_decode_rule_auto_trigger_on_above_single_sensor():
    """Mirror of the on_below test: high active, low parked at its rail → on_above."""
    entry = copy.deepcopy(MOCK_RULE_TEMPERATURE_TRIGGER)
    entry["autoHighTempF"] = 85
    entry["autoHighTempSwitch"] = 1
    entry["autoLowTempF"] = 32        # park the low trigger at its rail (inactive)
    decoded = _decode_rule(entry)
    assert decoded["direction"] == "on_above"
    assert "temperature: on above 85°F" in decoded["control"]


def test_decode_rule_auto_no_rule_set_fallback():
    """currentMode=4 with every trigger parked at its rail → graceful 'no rule set'."""
    entry = copy.deepcopy(MOCK_RULE_TEMPERATURE_TRIGGER)
    entry["settingMode"] = 0
    entry["autoHighTempF"] = 194
    entry["autoLowTempF"] = 32
    entry["autoHighHumi"] = 100
    entry["autoLowHumi"] = 0
    decoded = _decode_rule(entry)
    assert decoded["mode"] == "auto"
    assert "auto (no rule set)" in decoded["control"]


def test_decode_rule_vpd_no_rule_set_fallback():
    """currentMode=6 trigger style with both VPD rails parked → graceful 'no rule set'."""
    entry = copy.deepcopy(MOCK_RULE_VPD)
    entry["settingMode"] = 0
    entry["highVpd"] = 99
    entry["lowVpd"] = 0
    decoded = _decode_rule(entry)
    assert decoded["mode"] == "vpd"
    assert "VPD (no rule set)" in decoded["control"]


def test_decode_rule_unknown_mode():
    decoded = _decode_rule({"currentMode": 99})
    assert decoded["mode"] == "unknown"


# ============ _group_automations per-rule decode (Issue #284) ============


def test_group_automations_two_window_per_rule_decode():
    """A 2-entry same-advName program with different windows + modes decodes into two
    distinct per-rule descriptions (the pattern that collapsed before #284)."""
    grouped = _group_automations(copy.deepcopy(MOCK_TWO_WINDOW_PROGRAM))
    assert len(grouped) == 1
    pgs = grouped[0]["port_groups"]
    assert len(pgs) == 2
    # First entry: VPD lights-on window 540–180, running.
    assert pgs[0]["begin_time"] == 540
    assert pgs[0]["end_time"] == 180
    assert pgs[0]["run_state"] is True
    assert pgs[0]["rule"]["mode"] == "vpd"
    assert "VPD: hold at 0.9 kPa" in pgs[0]["rule"]["control"]
    # Second entry: auto-target humidity lights-off window 180–540, not running.
    assert pgs[1]["begin_time"] == 180
    assert pgs[1]["end_time"] == 540
    assert pgs[1]["run_state"] is False
    assert pgs[1]["rule"]["mode"] == "auto"
    assert "humidity: hold at 65%" in pgs[1]["rule"]["control"]


def test_group_automations_additive_keys_do_not_disturb_program_or_old_per_element_keys():
    """Non-breaking-change guard (Python Rev-2 MINOR): program-level keys and the original
    per-element keys (adv_id, on_speed, grp_dev_type) are unchanged; new keys are additive."""
    raw = [{"advId": 100, "advName": "Night Cycle", "isOn": 1, "onSpeed": 5,
            "grouptDevType": 1, "runState": 1, "beginTime": 0, "endTime": 1439,
            "currentMode": 1, "onTimeSwitch": 0}]
    grouped = _group_automations(copy.deepcopy(raw))[0]
    # Program-level keys unchanged.
    assert grouped["automation_id"] == 100
    assert grouped["name"] == "Night Cycle"
    assert grouped["enabled"] is True
    assert grouped["adv_ids"] == [100]
    assert grouped["begin_time"] == 0
    assert grouped["end_time"] == 1439
    assert grouped["run_state"] is True
    assert grouped["on_time_switch"] == 0
    # Original per-element keys unchanged.
    pg = grouped["port_groups"][0]
    assert pg["adv_id"] == 100
    assert pg["on_speed"] == 5
    assert pg["grp_dev_type"] == 1
    # New keys present (additive only).
    assert pg["begin_time"] == 0
    assert pg["current_mode"] == 1
    assert pg["rule"]["mode"] == "on"


# ============ _find_governing_automation ============


def _make_automation(name, enabled, run_state, bitmask, auto_id=1):
    return {
        "automation_id": auto_id,
        "name": name,
        "enabled": enabled,
        "run_state": run_state,
        "adv_ids": [auto_id],
        "port_groups": [{"adv_id": auto_id, "on_speed": 5, "grp_dev_type": bitmask}],
    }


def test_find_governing_automation_matching_bitmask_enabled():
    # Port 1 → bit 0 → bitmask 1
    auto = _make_automation("Night Cycle", enabled=True, run_state=False, bitmask=1)
    result = _find_governing_automation([auto], port=1)
    assert result is auto


def test_find_governing_automation_no_match_returns_none():
    # bitmask=2 covers Port 2, not Port 1
    auto = _make_automation("Night Cycle", enabled=True, run_state=False, bitmask=2)
    result = _find_governing_automation([auto], port=1)
    assert result is None


def test_find_governing_automation_disabled_and_not_running_skipped():
    auto = _make_automation("Night Cycle", enabled=False, run_state=False, bitmask=1)
    result = _find_governing_automation([auto], port=1)
    assert result is None


def test_find_governing_automation_run_state_true_counts():
    # enabled=False but run_state=True → should still be returned
    auto = _make_automation("Night Cycle", enabled=False, run_state=True, bitmask=1)
    result = _find_governing_automation([auto], port=1)
    assert result is auto


# ============ _find_governing_port_group ============


def test_find_governing_port_group_matching_bitmask():
    auto = _make_automation("Night Cycle", enabled=True, run_state=True, bitmask=1)
    pg = _find_governing_port_group(auto, port=1)
    assert pg is not None
    assert pg["grp_dev_type"] == 1


def test_find_governing_port_group_no_match_returns_none():
    # bitmask=2 covers Port 2 only
    auto = _make_automation("Night Cycle", enabled=True, run_state=True, bitmask=2)
    pg = _find_governing_port_group(auto, port=1)
    assert pg is None


# ============ _is_port_not_powered ============


def _make_port_data(ports_load):
    return {"portsLoad": ports_load}


def _make_device(dev_type):
    return {"devType": dev_type}


def test_is_port_not_powered_zero_load_normal_type():
    assert _is_port_not_powered(_make_port_data(0), _make_device(11)) is True


def test_is_port_not_powered_zero_load_dev_type_18():
    # devType 18 always reports portsLoad=0 — signal is meaningless
    assert _is_port_not_powered(_make_port_data(0), _make_device(18)) is False


def test_is_port_not_powered_zero_load_dev_type_22():
    # devType 22 always reports portsLoad=0 — signal is meaningless
    assert _is_port_not_powered(_make_port_data(0), _make_device(22)) is False


def test_is_port_not_powered_none_port_data():
    assert _is_port_not_powered(None, _make_device(11)) is False


def test_is_port_not_powered_none_device():
    assert _is_port_not_powered(_make_port_data(0), None) is False


def test_is_port_not_powered_nonzero_load():
    assert _is_port_not_powered(_make_port_data(5), _make_device(11)) is False


def test_is_port_not_powered_missing_ports_load_key():
    # .get("portsLoad") returns None → treated as 0 → evaluates to False for (None or 0) == 0
    assert _is_port_not_powered({}, _make_device(11)) is True


# ============ _build_advance_conflict_response (async) ============


def _make_raw_entry(adv_id, adv_name, is_on, run_state, bitmask, on_speed=5):
    return {
        "advId": adv_id,
        "advName": adv_name,
        "isOn": is_on,
        "onSpeed": on_speed,
        "grouptDevType": bitmask,
        "runState": run_state,
        "beginTime": 255,
        "endTime": 255,
        "onTimeSwitch": 0,
    }


def _make_mock_client(raw_entries=None, side_effect=None):
    client = MagicMock()
    if side_effect is not None:
        client.get_advance_automations.side_effect = side_effect
    else:
        client.get_advance_automations.return_value = raw_entries or []
    return client


async def test_build_conflict_sub_path_a_has_expected_options():
    """Port covered by bitmask → 1_break_out + 2_disable_automation in options."""
    raw = [_make_raw_entry(101, "Night Cycle", is_on=1, run_state=1, bitmask=1)]
    client = _make_mock_client(raw)
    result = await _build_advance_conflict_response(client, "C58ZA", 123456, 1, "Filter")
    data = json.loads(result)
    assert data["conflict"] == "ADVANCE_AUTOMATION"
    assert "1_break_out" in data["options"]
    assert "2_disable_automation" in data["options"]
    assert data["automation_name"] == "Night Cycle"


async def test_build_conflict_sub_path_a_human_summary_explains_learning():
    """#250: the governed-port conflict explains WHY manual override is blocked — to
    protect the pattern the controller is learning — so a grower understands it is not
    arbitrary obstruction (and won't try to force repeated manual overrides)."""
    raw = [_make_raw_entry(101, "Night Cycle", is_on=1, run_state=1, bitmask=1)]
    client = _make_mock_client(raw)
    result = await _build_advance_conflict_response(client, "C58ZA", 123456, 1, "Filter")
    data = json.loads(result)
    assert "pattern the controller is learning" in data["human_summary"]


async def test_build_conflict_sub_path_a_with_requested_speed_has_update_option():
    """requested_speed provided → 0_update_speed option present."""
    raw = [_make_raw_entry(101, "Night Cycle", is_on=1, run_state=1, bitmask=1)]
    client = _make_mock_client(raw)
    result = await _build_advance_conflict_response(
        client, "C58ZA", 123456, 1, "Filter", requested_speed=5
    )
    data = json.loads(result)
    assert "0_update_speed" in data["options"]
    assert "1_break_out" in data["options"]


async def test_build_conflict_sub_path_b_port_not_in_bitmask():
    """Active automation covers Port 2, request is for Port 1 → 1_disable_automation."""
    # bitmask=2 → Port 2 only
    raw = [_make_raw_entry(101, "Night Cycle", is_on=1, run_state=1, bitmask=2)]
    client = _make_mock_client(raw)
    result = await _build_advance_conflict_response(client, "C58ZA", 123456, 1, "Filter")
    data = json.loads(result)
    assert "1_disable_automation" in data["options"]
    assert "1_break_out" not in data["options"]


async def test_build_conflict_all_disabled_path():
    """All automations disabled → 1_re_disable_to_clear option."""
    raw = [_make_raw_entry(101, "Night Cycle", is_on=0, run_state=0, bitmask=1)]
    client = _make_mock_client(raw)
    result = await _build_advance_conflict_response(client, "C58ZA", 123456, 1, "Filter")
    data = json.loads(result)
    assert "1_re_disable_to_clear" in data["options"]


async def test_build_conflict_degraded_api_error():
    """API raises ACInfinityAPIError → degraded path with 1_find_and_disable."""
    client = _make_mock_client(side_effect=ACInfinityAPIError("fail"))
    result = await _build_advance_conflict_response(client, "C58ZA", 123456, 1, "Filter")
    data = json.loads(result)
    assert data["conflict"] == "ADVANCE_AUTOMATION"
    assert "1_find_and_disable" in data["options"]


async def test_build_conflict_auth_error_returns_error():
    """API raises ACInfinityAuthError → auth error JSON, no conflict key."""
    client = _make_mock_client(side_effect=ACInfinityAuthError("auth"))
    result = await _build_advance_conflict_response(client, "C58ZA", 123456, 1, "Filter")
    data = json.loads(result)
    assert "error" in data
    assert "conflict" not in data
    assert "Authentication failed" in data["error"]


# ============ _decode_rule defensive coercion (buffer/transition) ============


def test_decode_rule_string_valued_buffer_transition_no_raise():
    """A string-valued buffer/transition field must not raise in _decode_rule —
    it runs for every rule via _group_automations (legacy conflict-detection hot path).
    The VPD /10 path would TypeError on a raw string without coercion."""
    entry = {
        "advName": "X", "advId": 1, "grouptDevType": 1, "currentMode": 6,
        "settingMode": 1, "targetVpd": 12, "onSpeed": 5, "offSpeed": 1,
        "beginTime": 540, "endTime": 1020, "switchTime": 127,
        # string-valued (defensive): must coerce, not crash
        "vpdBuff": "3", "temperatureFBuff": "2", "humidityTrans": "4",
    }
    decoded = _decode_rule(entry)  # must not raise
    assert decoded["mode"] == "vpd"
    assert "VPD buffer 0.3 kPa" in decoded["control"]
    assert "temperature buffer 2°F" in decoded["control"]
    assert "humidity transition 4%" in decoded["control"]


def test_is_port_not_powered_false_on_ai_plus() -> None:
    """devType 20 has no load signal, so it must not claim a running port is unpowered.

    portsLoad is None on every AI+ port (Quirk 24). Before devType 20 joined
    _ZERO_LOAD_DEV_TYPES, `(portsLoad or 0) == 0` was true for all of them, so an
    ADVANCE-conflict response would append "is not currently drawing power" to a
    light that was visibly running at speed 2 with a 400-ohm load.
    """
    running_light = {
        "port": 1, "portName": "Light", "speak": 2,
        "portsLoad": None, "portResistance": 400,
    }
    assert _is_port_not_powered(running_light, {"devType": 20}) is False


def test_is_port_not_powered_still_true_on_legacy_zero_load() -> None:
    """devType 11 reports portsLoad honestly, so the signal stays meaningful there."""
    idle = {"port": 1, "portName": "Ventilation", "speak": 0, "portsLoad": 0}
    assert _is_port_not_powered(idle, {"devType": 11}) is True
