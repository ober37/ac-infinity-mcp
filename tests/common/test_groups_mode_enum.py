"""Groups `currentMode` is class-specific — encode/decode matrices for #326 / #328.

The Groups endpoint numbers the same five modes differently on legacy controllers
(devType 11/18) and new-framework ones (devType 20/22). Two of the overlaps are
actively dangerous: `1`/`2` are on/off INVERTED, and `6` is VPD on legacy but CYCLE
on new-framework. Writing the legacy number to a new-framework controller is what
energized a grow light in #326.

Every assertion here is on the **exact** wire integer or the **exact** control
string. Substring assertions pass straight through the `_decode_modifiers` bug that
rendered an ON rule as a speed range, and a same-class round-trip is a tautology —
it passes with any self-consistent table, including the wrong one we shipped.
"""

import json
from pathlib import Path

import pytest

from ac_infinity_mcp.automation import _decode_modifiers, _decode_rule
from ac_infinity_mcp.client import build_add_groups_payload, build_groups_payload
from ac_infinity_mcp.controller import (
    ControllerType,
    detect_controller_type,
    groups_mode_code,
    groups_mode_name,
)

LEGACY = ControllerType.LEGACY
NEW = ControllerType.NEW_FRAMEWORK

MODES = ("on", "off", "cycle", "auto", "vpd")

# The two tables, spelled out literally. Deriving them from the source under test
# would assert nothing; these numbers came from Groups payloads captured off real
# hardware (legacy: C58ZA/8T4TC; new-framework: Q0KT4).
WIRE = {
    LEGACY: {"on": 1, "off": 2, "cycle": 3, "auto": 4, "vpd": 6},
    NEW: {"on": 2, "off": 1, "cycle": 6, "auto": 3, "vpd": 8},
}


def _payload(mode, ctype, **over):
    kwargs = dict(
        dev_id="X", ports=[1], clean_name="R", begin_time=540, end_time=1020,
        mode=mode, on_speed=5, controller_type=ctype,
    )
    kwargs.update(over)
    return build_groups_payload(**kwargs)


# ============ Encoder: exact wire integer, both classes ============


@pytest.mark.parametrize("ctype", [LEGACY, NEW])
@pytest.mark.parametrize("mode", MODES)
def test_encoder_writes_class_specific_wire_code(mode, ctype):
    assert _payload(mode, ctype)["currentMode"] == WIRE[ctype][mode]


@pytest.mark.parametrize("ctype", [LEGACY, NEW])
@pytest.mark.parametrize("mode", MODES)
def test_groups_mode_code_matches_wire_table(mode, ctype):
    assert groups_mode_code(ctype, mode) == WIRE[ctype][mode]


@pytest.mark.parametrize("ctype", [LEGACY, NEW])
@pytest.mark.parametrize("mode", MODES)
def test_add_groups_encoder_writes_class_specific_wire_code(mode, ctype):
    payload = build_add_groups_payload(
        dev_id="X", port=1, clean_name="R", on_speed=5, begin_time=540, end_time=1020,
        controller_type=ctype,
    )
    # build_add_groups_payload is the On-mode shim; it always writes the class's ON code.
    assert payload["currentMode"] == WIRE[ctype]["on"]


# ============ Decoder: exact control string, both classes ============


@pytest.mark.parametrize("ctype", [LEGACY, NEW])
@pytest.mark.parametrize("mode", MODES)
def test_decoder_reads_class_specific_wire_code(mode, ctype):
    assert _decode_rule({"currentMode": WIRE[ctype][mode]}, controller_type=ctype)["mode"] == mode


@pytest.mark.parametrize("ctype", [LEGACY, NEW])
@pytest.mark.parametrize("mode", MODES)
def test_table_inverse_property(mode, ctype):
    """NAMES[CODES[mode]] == mode, for every class and mode — the two directions
    are derived from one literal, so they cannot drift apart."""
    assert groups_mode_name(ctype, groups_mode_code(ctype, mode)) == mode


# ============ The two dangerous collisions, each its own named test ============


def test_on_off_are_inverted_between_classes():
    """This is the inversion that energized a grow light (#326).

    Legacy on=1/off=2; new-framework on=2/off=1. Writing the legacy `2` for "off"
    to a new-framework controller tells it ON.
    """
    assert _payload("off", LEGACY)["currentMode"] == 2
    assert _payload("off", NEW)["currentMode"] == 1
    assert _payload("on", LEGACY)["currentMode"] == 1
    assert _payload("on", NEW)["currentMode"] == 2

    assert _decode_rule({"currentMode": 2}, controller_type=LEGACY)["mode"] == "off"
    assert _decode_rule({"currentMode": 2}, controller_type=NEW)["mode"] == "on"
    assert _decode_rule({"currentMode": 1}, controller_type=LEGACY)["mode"] == "on"
    assert _decode_rule({"currentMode": 1}, controller_type=NEW)["mode"] == "off"


def test_wire_code_6_collides_vpd_and_cycle():
    """`6` is VPD on legacy and CYCLE on new-framework — the same integer, two modes."""
    assert groups_mode_name(LEGACY, 6) == "vpd"
    assert groups_mode_name(NEW, 6) == "cycle"
    assert _payload("vpd", LEGACY)["currentMode"] == 6
    assert _payload("cycle", NEW)["currentMode"] == 6


@pytest.mark.parametrize("mode", MODES)
def test_cross_class_round_trip_is_wrong_where_the_tables_differ(mode):
    """Decoding a new-framework payload with the legacy table must NOT reproduce the
    mode wherever the two tables disagree. A same-class round-trip passes even with
    one shared (wrong) table — this is the assertion that proves the gate is
    load-bearing.
    """
    encoded = _payload(mode, NEW)["currentMode"]
    misread = groups_mode_name(LEGACY, encoded)
    if WIRE[LEGACY][mode] == encoded:
        pytest.skip(f"{mode} happens to share a code across classes")
    assert misread != mode


def test_off_encoded_for_new_framework_never_reads_as_on_anywhere():
    """The specific #326 safety property: an "off" rule written for a new-framework
    controller does not read as "on" under either table."""
    code = _payload("off", NEW)["currentMode"]
    assert groups_mode_name(NEW, code) == "off"
    # Legacy reads that same 1 as "on" — which is exactly why the class must be resolved
    # from the device and never assumed. Pinning the misread keeps the hazard visible.
    assert groups_mode_name(LEGACY, code) == "on"


# ============ _decode_modifiers keys on the resolved mode, not the raw int ============


def test_decode_modifiers_on_mode_renders_single_speed():
    """ON renders `speed N`, never a `0 (off)–N` range."""
    entry = {"onSpeed": 7, "offSpeed": 0}
    assert _decode_modifiers(entry, "on")[0] == "speed 7"


def test_decode_modifiers_non_on_mode_renders_range():
    entry = {"onSpeed": 7, "offSpeed": 0}
    assert _decode_modifiers(entry, "cycle")[0] == "speed 0 (off)–7"


def test_new_framework_on_rule_control_string_is_single_speed():
    """currentMode=2 on new-framework is ON — the control string must say `speed 7`,
    not the `speed 0 (off)–7` range the old integer-keyed check produced."""
    decoded = _decode_rule({"currentMode": 2, "onSpeed": 7, "offSpeed": 0}, controller_type=NEW)
    assert decoded["mode"] == "on"
    assert decoded["control"] == "runs at set speed; speed 7"


def test_legacy_code_2_on_new_framework_does_not_render_as_off():
    """The read-side of #328: the same entry decoded under both tables."""
    entry = {"currentMode": 2, "onSpeed": 7, "offSpeed": 0}
    assert _decode_rule(entry, controller_type=LEGACY)["mode"] == "off"
    assert _decode_rule(entry, controller_type=NEW)["mode"] == "on"


# ============ Unknown / malformed codes decode gracefully, per class ============

_UNRECOGNIZED = "a rule type I don't recognize yet — check this one in the AC Infinity app"


# The unknown sets differ by class, and getting this wrong is not academic: `4` is AUTO on
# legacy and four of the golden rules depend on it, while `8` is VPD only on new-framework.
_UNKNOWN = {
    LEGACY: [0, 5, 7, 8, 9, None, "3", -1, 99, 1.5, [], {}],
    NEW: [0, 4, 5, 7, 9, None, "3", -1, 99, 1.5, [], {}],
}


@pytest.mark.parametrize("ctype", [LEGACY, NEW])
def test_unknown_codes_decode_gracefully(ctype):
    """Nothing raises, and every unhandled code reads as grower-actionable text."""
    for code in _UNKNOWN[ctype]:
        decoded = _decode_rule({"currentMode": code}, controller_type=ctype)
        assert decoded["mode"] == "unknown", f"{code!r} decoded as {decoded['mode']}"
        assert decoded["control"] == _UNRECOGNIZED
        assert decoded["direction"] is None


def test_unknown_sets_are_class_qualified():
    """Pin the exact known sets. `4` is auto on legacy but unknown on new-framework; `8`
    is VPD on new-framework but unknown on legacy; `6` is known to both and means
    different things."""
    assert {c for c in range(-1, 12) if groups_mode_name(LEGACY, c)} == {1, 2, 3, 4, 6}
    assert {c for c in range(-1, 12) if groups_mode_name(NEW, c)} == {1, 2, 3, 6, 8}


@pytest.mark.parametrize("ctype", [LEGACY, NEW])
def test_every_known_code_decodes_to_a_known_mode(ctype):
    """Completeness pin: no table entry maps to a mode the decoder cannot render."""
    for mode in MODES:
        code = groups_mode_code(ctype, mode)
        assert _decode_rule({"currentMode": code}, controller_type=ctype)["mode"] == mode


@pytest.mark.parametrize("code", ["1", "3", 1.0, True, None, [], {}])
def test_only_real_ints_resolve(code):
    """Pre-#328 the decoder used `==` against int literals, so a string, float or bool has
    always read as unrecognised. `True == 1` in Python — a bool must not decode as ON.
    Coercing here would be a silent behaviour change on the field that decides whether a
    grower is told their equipment is running."""
    assert groups_mode_name(LEGACY, code) is None
    assert groups_mode_name(NEW, code) is None


# ============ The class argument is required — no silent legacy default ============


def test_build_groups_payload_requires_controller_type():
    with pytest.raises(TypeError):
        build_groups_payload(
            dev_id="X", ports=[1], clean_name="R", begin_time=0, end_time=1439,
            mode="off", on_speed=5,
        )


def test_build_add_groups_payload_requires_controller_type():
    with pytest.raises(TypeError):
        build_add_groups_payload(
            dev_id="X", port=1, clean_name="R", on_speed=5, begin_time=0, end_time=1439,
        )


def test_decode_rule_requires_controller_type():
    with pytest.raises(TypeError):
        _decode_rule({"currentMode": 1})


# ============ detect_controller_type feeds the right table ============


@pytest.mark.parametrize("dev_type,expected", [(11, LEGACY), (18, LEGACY), (20, NEW), (22, NEW)])
def test_devtype_selects_the_table(dev_type, expected):
    assert detect_controller_type({"devType": dev_type}) is expected
    assert groups_mode_code(detect_controller_type({"devType": dev_type}), "off") == \
        WIRE[expected]["off"]


# ============ Legacy golden file — the change must be a no-op on legacy ============

_GOLDEN = Path(__file__).resolve().parents[1] / "fixtures/captures/getgroups-legacy-2026-09-06.json"


def test_legacy_decode_is_byte_identical_to_pre_change_capture():
    """31 real entries off C58ZA (devType 11) and 8T4TC (devType 18), captured on `main`
    before the enum change. Legacy behaviour must not move by a single character.
    """
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    checked = 0
    for code, dev in golden["devices"].items():
        ctype = detect_controller_type({"devType": dev["devType"]})
        assert ctype is LEGACY, f"{code} is not a legacy controller"
        for entry, expected in zip(dev["entries"], dev["decoded"], strict=True):
            assert _decode_rule(entry, controller_type=LEGACY) == expected
            checked += 1
    assert checked == 31, f"golden capture changed size: {checked}"


def test_golden_capture_covers_every_legacy_mode():
    """A regression baseline that only exercised one mode would pass vacuously."""
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    seen = {d["mode"] for dev in golden["devices"].values() for d in dev["decoded"]}
    assert set(MODES) <= seen, f"golden capture is missing modes: {set(MODES) - seen}"


# ============ Tool boundary — a devType-20 device end-to-end ============
#
# Every one of the assertions above calls the encoder/decoder directly. That is exactly
# how the original bug survived: a threading site that forgets to pass the device's class
# still passes a direct-call suite. These drive the MCP tools with a devType-20 fixture,
# so a missed site shows up as a wrong mode in the tool's own JSON.

import copy  # noqa: E402

from ac_infinity_mcp.server import (  # noqa: E402
    create_advance_automation,
    get_advance_automation,
    list_advance_automations,
    update_automation_rule,
)
from tests.fixtures.ai_plus_device_fixtures import AI_PLUS_DEVICE  # noqa: E402

AI_PLUS_CODE = AI_PLUS_DEVICE["devCode"]


def _ai_plus_entry(**over):
    """One new-framework Groups entry. currentMode defaults to 2 = ON on this class."""
    entry = {
        "advId": 9001, "advName": "Lights", "isOn": 1, "runState": 1,
        "currentMode": 2, "onSpeed": 7, "offSpeed": 0, "grouptDevType": 1,
        "advKey": "1-0", "beginTime": 255, "endTime": 255, "onTimeSwitch": 0,
        "groupNums": 1, "sortType": 6, "subNumber": 0, "subNumberSort": 0,
        # switchTime bit 7 set = the 24/7 toggle, consistent with the 255 begin/end
        # sentinels above (Quirk 21).
        "switchTime": 255, "sensorModeDataNum": 0,
    }
    entry.update(over)
    return entry


@pytest.fixture
def ai_plus_client(mock_client):
    """mock_client re-pointed at the devType-20 fixture."""
    mock_client.get_devices.return_value = [copy.deepcopy(AI_PLUS_DEVICE)]
    mock_client.get_advance_automations.return_value = [_ai_plus_entry()]
    return mock_client


async def test_get_advance_automation_decodes_new_framework_on(ai_plus_client):
    """currentMode=2 on devType 20 is ON. Under the legacy table this tool reported
    the grower's running light as "off" (#328)."""
    data = json.loads(await get_advance_automation(AI_PLUS_CODE, "9001"))
    rule = data["rules"][0]
    assert rule["_mode"] == "on"
    assert rule["control"].startswith("runs at set speed; speed 7")


async def test_get_advance_automation_decodes_new_framework_cycle_not_vpd(ai_plus_client):
    """`6` is CYCLE here, VPD on legacy — the collision, seen through the tool."""
    ai_plus_client.get_advance_automations.return_value = [
        _ai_plus_entry(currentMode=6, cycleOn=600, cycleOff=1200)
    ]
    data = json.loads(await get_advance_automation(AI_PLUS_CODE, "9001"))
    rule = data["rules"][0]
    assert rule["_mode"] == "cycle"
    assert "cycle 10 min on / 20 min off" in rule["control"]
    assert "VPD" not in rule["control"]


async def test_list_advance_automations_reaches_the_tool_for_new_framework(ai_plus_client):
    """list_ groups through _group_automations, which is a separate threading site from
    the decode path get_ uses."""
    data = json.loads(await list_advance_automations(AI_PLUS_CODE))
    assert [a["name"] for a in data["automations"]] == ["Lights"]


async def test_get_advance_automation_decodes_new_framework_off(ai_plus_client):
    """currentMode=1 is OFF on this class. Legacy reads the same 1 as ON — the read-side
    mirror of the #326 hazard."""
    ai_plus_client.get_advance_automations.return_value = [_ai_plus_entry(currentMode=1)]
    data = json.loads(await get_advance_automation(AI_PLUS_CODE, "9001"))
    assert data["rules"][0]["_mode"] == "off"


async def test_create_advance_automation_dry_run_previews_new_framework_off_code(ai_plus_client):
    """The #326 write itself: an "off" automation created on a devType-20 controller must
    put 1 on the wire, not the legacy 2 (which that controller reads as ON)."""
    data = json.loads(await create_advance_automation(
        AI_PLUS_CODE, "Lights Off", on_speed=1, port=1, mode="off", dry_run=True,
    ))
    assert data["payload"]["currentMode"] == 1


async def test_create_advance_automation_live_writes_new_framework_off_code(ai_plus_client):
    data = json.loads(await create_advance_automation(
        AI_PLUS_CODE, "Lights Off", on_speed=1, port=1, mode="off", dry_run=False,
    ))
    assert "error" not in data, data
    sent = ai_plus_client.create_advance_automation.call_args.args[1]
    assert sent["currentMode"] == 1


async def test_create_advance_automation_legacy_off_code_is_unchanged(mock_client):
    """The same call on a legacy controller still writes 2 — this fix must not move
    legacy behaviour."""
    data = json.loads(await create_advance_automation(
        "C58ZA", "Lights Off", on_speed=1, port=1, mode="off", dry_run=True,
    ))
    assert data["payload"]["currentMode"] == 2


# ---- Edit path: the same-mode overlay is chosen from the decoded mode ----


async def test_same_mode_edit_on_new_framework_takes_the_auto_overlay(ai_plus_client):
    """currentMode=3 is AUTO on devType 20 (CYCLE on legacy). The edit must take the auto
    overlay and emit no cycle keys — under the legacy table this wrote cycleOn/cycleOff
    onto an auto rule."""
    ai_plus_client.get_advance_automations.return_value = [
        _ai_plus_entry(currentMode=3, beginTime=540, endTime=180, advName="Lights")
    ]
    await update_automation_rule(
        AI_PLUS_CODE, "Lights", [1], begin_time=540, end_time=180,
        temp_high_f=85, dry_run=False,
    )
    sent = ai_plus_client.update_advance_automation.call_args.args[1]
    assert sent["autoHighTempF"] == 85
    assert sent["autoHighTempSwitch"] == 1
    assert sent["currentMode"] == 3
    assert sent.get("cycleOn", 0) == 0
    assert sent.get("cycleOff", 0) == 0


# ============ sensorModeData degradation — "no rule set" must not lie ============
#
# Some new-framework app-created rules keep their real configuration in `sensorModeData`,
# which this project does not decode. The legacy trigger fields then sit at their rails and
# the decoder used to report "auto (no rule set)" for a rule actively running a heater.
# A grower told that may recreate the automation on live equipment, or set a threshold that
# overlays the legacy fields while the real rule sits in `sensorModeData` — two disagreeing
# sources of truth on a heater.

_RULE_SET_ELSEWHERE = "rule set in the AC Infinity app — I can't read its details yet"


def _inactive_auto(**over):
    """An auto rule with every legacy trigger switch off — nothing decodable."""
    entry = {
        "currentMode": None, "onSpeed": 10, "offSpeed": 0,
        "autoHighTempSwitch": 0, "autoLowTempSwitch": 0,
        "autoHighHumiSwitch": 0, "autoLowHumiSwitch": 0,
        "targetTempSwitch": 0, "targetHumiSwitch": 0,
    }
    entry.update(over)
    return entry


def test_legacy_inactive_auto_still_says_no_rule_set():
    """Legacy has no sensorModeData path — the reassuring wording is correct there and
    must not move."""
    entry = _inactive_auto(currentMode=groups_mode_code(LEGACY, "auto"))
    assert "auto (no rule set)" in _decode_rule(entry, controller_type=LEGACY)["control"]


def test_new_framework_inactive_auto_with_explicit_zero_says_no_rule_set():
    """An explicit count of 0 means the rule genuinely keeps its config in the named
    fields — the reassurance is earned."""
    entry = _inactive_auto(currentMode=groups_mode_code(NEW, "auto"), sensorModeDataNum=0)
    assert "auto (no rule set)" in _decode_rule(entry, controller_type=NEW)["control"]


@pytest.mark.parametrize("count", [1, 2, 3])
def test_new_framework_inactive_auto_with_a_count_admits_the_gap(count):
    entry = _inactive_auto(currentMode=groups_mode_code(NEW, "auto"), sensorModeDataNum=count)
    control = _decode_rule(entry, controller_type=NEW)["control"]
    assert f"auto ({_RULE_SET_ELSEWHERE})" in control
    assert "no rule set" not in control


def test_absent_count_takes_the_cautious_branch():
    """`sensorModeDataNum` is undocumented and appears in no committed capture. Defaulting
    a missing key to 0 would produce exactly the false reassurance this guards against."""
    entry = _inactive_auto(currentMode=groups_mode_code(NEW, "auto"))
    assert "sensorModeDataNum" not in entry
    control = _decode_rule(entry, controller_type=NEW)["control"]
    assert f"auto ({_RULE_SET_ELSEWHERE})" in control


@pytest.mark.parametrize("raw", [None, "", "abc", [], {}])
def test_unparseable_count_takes_the_cautious_branch(raw):
    entry = _inactive_auto(currentMode=groups_mode_code(NEW, "auto"), sensorModeDataNum=raw)
    assert _RULE_SET_ELSEWHERE in _decode_rule(entry, controller_type=NEW)["control"]


def test_vpd_gets_the_same_treatment_as_auto():
    """The VPD twin — rev 3 of the plan specified auto only, and the two clauses sit two
    lines apart."""
    base = {"currentMode": groups_mode_code(NEW, "vpd"), "onSpeed": 10, "offSpeed": 0,
            "highVpdSwitch": 0, "lowVpdSwitch": 0, "targetVpdSwitch": 0}
    assert f"VPD ({_RULE_SET_ELSEWHERE})" in _decode_rule(
        {**base, "sensorModeDataNum": 2}, controller_type=NEW)["control"]
    assert "VPD (no rule set)" in _decode_rule(
        {**base, "sensorModeDataNum": 0}, controller_type=NEW)["control"]


def test_the_clause_composes_as_a_phrase_not_a_sentence():
    """automation.py joins clauses with "; " and the result also lands in the one-line
    disambiguation list, so this has to stay a noun phrase."""
    entry = _inactive_auto(currentMode=groups_mode_code(NEW, "auto"), sensorModeDataNum=1)
    control = _decode_rule(entry, controller_type=NEW)["control"]
    assert control == f"auto ({_RULE_SET_ELSEWHERE}); speed 0 (off)–10"


# ============ human_summary reflects the rule, not just a speed ============


async def test_human_summary_substitutes_the_control_for_a_cycle_rule(ai_plus_client):
    """#328: a cycle rule was summarised as "runs continuously at speed 7", which states
    the opposite of what the equipment does."""
    ai_plus_client.get_advance_automations.return_value = [
        _ai_plus_entry(currentMode=6, cycleOn=600, cycleOff=1200)
    ]
    data = json.loads(await get_advance_automation(AI_PLUS_CODE, "9001"))
    assert data["human_summary"] == (
        "'Lights' cycle 10 min on / 20 min off; speed 0 (off)–7; runs continuously,"
        " currently enabled."
    )


async def test_human_summary_for_an_on_rule_is_unchanged(ai_plus_client):
    """On rules keep the existing wording byte-for-byte."""
    data = json.loads(await get_advance_automation(AI_PLUS_CODE, "9001"))
    assert data["human_summary"] == "'Lights' runs continuously at speed 7, currently enabled."


async def test_human_summary_for_an_unknown_rule_falls_back_to_the_speed_wording(
    ai_plus_client,
):
    """An unrecognised code must not put "a rule type I don't recognize yet" into the
    summary sentence — the per-rule `control` already says it."""
    ai_plus_client.get_advance_automations.return_value = [_ai_plus_entry(currentMode=99)]
    data = json.loads(await get_advance_automation(AI_PLUS_CODE, "9001"))
    assert "don't recognize" not in data["human_summary"]
    assert data["human_summary"].startswith("'Lights' runs continuously at speed 7")


async def test_legacy_human_summary_for_an_auto_rule_names_the_trigger(mock_client):
    """The same improvement on legacy — driven through the tool so the threading is real."""
    mock_client.get_advance_automations.return_value = [{
        "advId": 7001, "advName": "Heat", "isOn": 1, "runState": 1,
        "currentMode": groups_mode_code(LEGACY, "auto"), "onSpeed": 10, "offSpeed": 0,
        "grouptDevType": 1, "advKey": "1-0", "beginTime": 255, "endTime": 255,
        "onTimeSwitch": 1, "switchTime": 127,
        "autoLowTempSwitch": 1, "autoLowTempF": 70,
    }]
    data = json.loads(await get_advance_automation("C58ZA", "7001"))
    assert "temperature: on below 70°F" in data["human_summary"]
    assert "currently enabled." in data["human_summary"]
