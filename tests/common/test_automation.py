"""Unit tests for automation.py pure helpers and _build_advance_conflict_response."""

import json
from unittest.mock import MagicMock

from ac_infinity_mcp.automation import (
    _build_advance_conflict_response,
    _find_governing_automation,
    _find_governing_port_group,
    _group_automations,
    _is_port_not_powered,
    _sanitize_api_string,
)
from ac_infinity_mcp.schema import ACInfinityAPIError, ACInfinityAuthError

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
