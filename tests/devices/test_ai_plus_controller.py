"""Tests for AI+ controller behavior (devType 20+, newFrameworkDevice=True)."""

from unittest.mock import patch

import pytest

from ac_infinity_mcp.analytics import _TOGGLE_LOAD_TYPES
from ac_infinity_mcp.client import ACInfinityClient
from ac_infinity_mcp.controller import ControllerType, build_write_payload, detect_controller_type
from ac_infinity_mcp.schema import ACInfinityAdvanceConflictError, ACInfinityDeviceError
from tests.fixtures.ai_plus_device_fixtures import AI_PLUS_HISTORY_RECORD
from tests.fixtures.mock_mode_settings_ai_plus import (
    MOCK_MODE_SETTINGS_AI_PLUS_PORT1,
    MOCK_MODE_SETTINGS_AI_PLUS_PORT1_FLAT,
)
from tests.fixtures.mock_mode_settings_legacy import MOCK_MODE_SETTINGS_LEGACY_PORT1

# ============ detect_controller_type ============

def test_detect_controller_type_devtype_20():
    assert detect_controller_type({"devType": 20}) == ControllerType.NEW_FRAMEWORK


def test_detect_controller_type_new_framework_flag():
    assert detect_controller_type({"newFrameworkDevice": True}) == ControllerType.NEW_FRAMEWORK


def test_detect_controller_type_devtype_25():
    assert detect_controller_type({"devType": 25}) == ControllerType.NEW_FRAMEWORK


def test_detect_controller_type_ai_plus_fixture(ai_plus_device):
    assert detect_controller_type(ai_plus_device) == ControllerType.NEW_FRAMEWORK


# detect_controller_type is total: it gates _ai_plus_write_held in the server layer,
# and some of those gates sit outside the try/except wrapping their tool body, so a
# TypeError here would escape as an unhandled exception instead of a readable error.

def test_detect_controller_type_numeric_string_devtype():
    """A stringified devType still classifies — "20" is an AI+, not a legacy fallback."""
    assert detect_controller_type({"devType": "20"}) == ControllerType.NEW_FRAMEWORK
    assert detect_controller_type({"devType": "11"}) == ControllerType.LEGACY


def test_detect_controller_type_none_devtype_does_not_raise():
    """devType: None is LEGACY (the same answer an absent devType gives), never a raise."""
    assert detect_controller_type({"devType": None}) == ControllerType.LEGACY


def test_detect_controller_type_unparseable_devtype_does_not_raise():
    assert detect_controller_type({"devType": "unknown"}) == ControllerType.LEGACY
    assert detect_controller_type({"devType": []}) == ControllerType.LEGACY


def test_detect_controller_type_flag_wins_over_unreadable_devtype():
    """newFrameworkDevice is checked first, so it survives a garbage devType."""
    assert detect_controller_type(
        {"devType": None, "newFrameworkDevice": True}
    ) == ControllerType.NEW_FRAMEWORK


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


# ============ AI+ write path — the minversion gate ============
#
# The ENTIRE AI+ write fix is one request header: `minversion: "3.5"`. With the
# stock okhttp header set the API returns 100001 even given a correct payload;
# with minversion present the ordinary merged read-before-write payload succeeds
# for manual control and automation targets alike.
#
# Ablated against live devType-20 hardware, no-op write to an idle port. 8 of the
# 16 header subsets were run; the three minversion-plus-one pairs were not. Full
# matrix and the reasoning for why that gap is harmless: docs/API.md Quirk 14.
#
#   okhttp headers only ................................ 100001
#   appVersion alone ................................... 100001
#   iOS User-Agent + phoneType + appVersion, no
#     minversion ....................................... 100001
#   minversion="3.5" only .............................. 200
#   minversion + any two of the other three ............ 200
#   all four ........................................... 200
#
# Despite the name it is not a version comparison: "3.4", "3.6", "3", "3.50",
# "3.5.0", "" and "99.9" all return 100001. The server matches the literal
# string, so these tests pin the exact value rather than merely its presence —
# a typo here would ship green and break every AI+ write in the field.

AI_PLUS_MINVERSION = "3.5"

LEGACY_EXPECTED_HEADERS = {
    "token": "tok",
    "Host": "www.acinfinityserver.com",
    "User-Agent": "okhttp/3.10.0",
    "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
}

# Every AI+ test below overrides modeType to 0. The fixture ships modeType=15,
# which is what a real AI+ port reports whenever its atType is OFF, ON or AUTO
# (Quirk 35 — modeType echoes atType) — but leaning on the
# fixture's incidental isOpenAutomation=0 to slip past the ADVANCE guard would
# make these tests fail for reasons unrelated to what they assert if that field
# ever changed. Mirrors the override in tests/common/test_client.py.
AI_PLUS_SETTINGS = {**MOCK_MODE_SETTINGS_AI_PLUS_PORT1, "modeType": 0}


class _Resp:
    """Stand-in addDevMode response; defaults to the success body."""

    status_code = 200

    def __init__(self, code=200, msg="success."):
        self._code, self._msg = code, msg

    def raise_for_status(self):
        pass

    def json(self):
        return {"code": self._code, "msg": self._msg}


def _capture_write(client, device, updates, settings=None, **kwargs):
    """Run a live write and capture both the headers and the body actually sent."""
    captured = {"headers": None, "data": None, "calls": 0}

    def _fake_post(url, data=None, headers=None, timeout=None):
        captured["headers"] = dict(headers or {})
        captured["data"] = data
        captured["calls"] += 1
        return _Resp()

    settings = dict(settings if settings is not None else AI_PLUS_SETTINGS)
    with patch.object(client.session, "post", _fake_post), \
         patch.object(client, "_enforce_write_rate_limit"), \
         patch.object(client, "get_mode_settings", autospec=True, return_value=settings):
        captured["result"] = client.set_port_mode(
            device, port=1, updates=updates, dry_run=False, **kwargs
        )
    return captured


@pytest.mark.parametrize("dev_type", [20, 22])
def test_ai_plus_write_sends_exact_minversion(ai_plus_device, dev_type):
    """AI+ writes must carry minversion with the exact value the server accepts."""
    ai_plus_device["devType"] = dev_type
    c = ACInfinityClient("test@example.com", "pw")
    c.token = "tok"
    cap = _capture_write(c, ai_plus_device, {"onSpead": 5})
    assert cap["headers"]["minversion"] == AI_PLUS_MINVERSION


@pytest.mark.parametrize("dev_type", [20, 22])
def test_ai_plus_write_sends_no_other_app_identity_headers(ai_plus_device, dev_type):
    """minversion is the whole fix — the ablated headers must not creep back.

    Each header we declare is surface for the kind of server-side tightening that
    broke the v2 endpoints in #298, and all three were proven unnecessary.
    """
    ai_plus_device["devType"] = dev_type
    c = ACInfinityClient("test@example.com", "pw")
    c.token = "tok"
    cap = _capture_write(c, ai_plus_device, {"onSpead": 5})
    assert cap["headers"] == {**LEGACY_EXPECTED_HEADERS, "minversion": AI_PLUS_MINVERSION}
    assert "Alamofire" not in cap["headers"]["User-Agent"]


def test_ai_plus_write_sends_the_merged_payload(ai_plus_device):
    """The body must be the merged read-before-write payload, not a template.

    This is the assertion that defends the central safety claim of the
    header-only design: stored settings survive a write because the payload
    carries them.
    """
    c = ACInfinityClient("test@example.com", "pw")
    c.token = "tok"
    cap = _capture_write(c, ai_plus_device, {"onSpead": 5})
    expected = build_write_payload(
        dict(AI_PLUS_SETTINGS), {"onSpead": 5}, ControllerType.NEW_FRAMEWORK
    )
    assert cap["data"] == expected
    # Spot-check that unrelated stored state rode along untouched.
    assert cap["data"]["devHtf"] == AI_PLUS_SETTINGS["devHtf"]
    assert cap["data"]["schedStartTime"] == AI_PLUS_SETTINGS["schedStartTime"]


def test_legacy_write_headers_are_untouched(legacy_11_device):
    """Legacy controllers carry exactly the four stock headers — no minversion."""
    c = ACInfinityClient("test@example.com", "pw")
    c.token = "tok"
    cap = _capture_write(
        c, legacy_11_device, {"onSpead": 5},
        settings={**MOCK_MODE_SETTINGS_LEGACY_PORT1, "modeType": 0},
    )
    assert cap["headers"] == LEGACY_EXPECTED_HEADERS


def test_ai_plus_dry_run_sends_nothing(ai_plus_device):
    """dry_run=True must issue zero addDevMode calls."""
    c = ACInfinityClient("test@example.com", "pw")
    c.token = "tok"
    calls = []

    with patch.object(c.session, "post", lambda *a, **k: calls.append(k) or _Resp()), \
         patch.object(c, "get_mode_settings", autospec=True,
                      return_value=dict(AI_PLUS_SETTINGS)):
        result = c.set_port_mode(ai_plus_device, port=1, updates={"onSpead": 5}, dry_run=True)

    assert calls == []
    assert result["sent"] is False
    assert result["dry_run"] is True


def test_ai_plus_write_enforces_rate_limit(ai_plus_device):
    """The 1.5s write spacing must still apply to the newly-enabled device class."""
    c = ACInfinityClient("test@example.com", "pw")
    c.token = "tok"

    with patch.object(c.session, "post", lambda *a, **k: _Resp()), \
         patch.object(c, "_enforce_write_rate_limit") as limiter, \
         patch.object(c, "get_mode_settings", autospec=True,
                      return_value=dict(AI_PLUS_SETTINGS)):
        c.set_port_mode(ai_plus_device, port=1, updates={"onSpead": 5}, dry_run=False)

    assert limiter.call_count == 1


def test_ai_plus_automation_write_is_not_refused(ai_plus_device):
    """Automation-target writes on AI+ must send, not be refused at the client layer."""
    c = ACInfinityClient("test@example.com", "pw")
    c.token = "tok"
    cap = _capture_write(
        c, ai_plus_device, {"atType": 8, "targetVpd": 12, "targetVpdSwitch": 1}
    )
    assert cap["result"]["sent"] is True
    assert cap["data"]["atType"] == 8
    assert cap["data"]["targetVpd"] == 12


# ============ Toggle-hardware guard ============


@pytest.mark.parametrize("load_type", sorted(_TOGGLE_LOAD_TYPES))
def test_speed_write_refused_on_toggle_hardware(ai_plus_device, load_type):
    """Every value in _TOGGLE_LOAD_TYPES must block a speed write before the POST.

    129 and 132 are both live on a devType-22 controller (clone lights, rack
    lights, heat pads). Before they were in the set, a speed write to one of them
    reached the API, came back 999999, and was reported to the grower as an
    Advance Automation conflict that did not exist.
    """
    c = ACInfinityClient("test@example.com", "pw")
    c.token = "tok"
    settings = {**AI_PLUS_SETTINGS, "loadType": load_type}

    with patch.object(c.session, "post") as post, \
         patch.object(c, "_enforce_write_rate_limit"), \
         patch.object(c, "get_mode_settings", autospec=True, return_value=settings):
        with pytest.raises(ACInfinityDeviceError, match="on/off device"):
            c.set_port_mode(ai_plus_device, port=1, updates={"onSpead": 5},
                            dry_run=False, require_variable_speed=True)
    post.assert_not_called()


def test_toggle_guard_does_not_block_on_off_writes(ai_plus_device):
    """set_port_on/off must still work on toggle hardware — the guard is speed-only."""
    c = ACInfinityClient("test@example.com", "pw")
    c.token = "tok"
    cap = _capture_write(
        c, ai_plus_device, {"atType": 2},
        settings={**AI_PLUS_SETTINGS, "loadType": 129},
    )
    assert cap["result"]["sent"] is True


# ============ AI+ error codes ============


def _post_returning(code, msg):
    return lambda *a, **k: _Resp(code=code, msg=msg)


def test_ai_plus_100001_names_the_minversion_gate(ai_plus_device):
    """100001 on AI+ means the header gate moved — say so, not a bare API error."""
    c = ACInfinityClient("test@example.com", "pw")
    c.token = "tok"

    with patch.object(c.session, "post", _post_returning(100001, "Something went wrong")), \
         patch.object(c, "_enforce_write_rate_limit"), \
         patch.object(c, "get_mode_settings", autospec=True,
                      return_value=dict(AI_PLUS_SETTINGS)):
        with pytest.raises(ACInfinityDeviceError) as exc:
            c.set_port_mode(ai_plus_device, port=1, updates={"onSpead": 5}, dry_run=False)

    assert "100001" in str(exc.value)
    assert AI_PLUS_MINVERSION in str(exc.value)


def test_ai_plus_999999_on_speed_write_reports_hardware_not_automation(ai_plus_device):
    """999999 is overloaded; on a speed write the on/off reading is the useful one.

    devType-20 reports loadType 0 on every port (Quirk 34), so the pre-write
    guard cannot catch toggle hardware there — this path is how a grower finds
    out, and telling them to release a nonexistent automation is a wrong answer.
    """
    c = ACInfinityClient("test@example.com", "pw")
    c.token = "tok"

    with patch.object(c.session, "post", _post_returning(999999, "error")), \
         patch.object(c, "_enforce_write_rate_limit"), \
         patch.object(c, "get_mode_settings", autospec=True,
                      return_value={**AI_PLUS_SETTINGS, "loadType": 0}):
        with pytest.raises(ACInfinityDeviceError) as exc:
            c.set_port_mode(ai_plus_device, port=1, updates={"onSpead": 5},
                            dry_run=False, require_variable_speed=True)

    assert "set_port_on" in str(exc.value)


def test_ai_plus_999999_on_mode_write_still_reports_automation_conflict(ai_plus_device):
    """Outside a speed write, 999999 keeps its ADVANCE-conflict meaning."""
    c = ACInfinityClient("test@example.com", "pw")
    c.token = "tok"

    with patch.object(c.session, "post", _post_returning(999999, "error")), \
         patch.object(c, "_enforce_write_rate_limit"), \
         patch.object(c, "get_mode_settings", autospec=True,
                      return_value=dict(AI_PLUS_SETTINGS)):
        with pytest.raises(ACInfinityAdvanceConflictError):
            c.set_port_mode(ai_plus_device, port=1, updates={"atType": 2}, dry_run=False)


# ============ AI+ ADVANCE guard (Quirk 35) ============


def test_ai_plus_advance_guard_ignores_mode_type(ai_plus_device):
    """modeType=15 is an AI+ resting value, not an ADVANCE signal — must not block."""
    c = ACInfinityClient("test@example.com", "pw")
    c.token = "tok"
    cap = _capture_write(
        c, ai_plus_device, {"onSpead": 5},
        settings={**MOCK_MODE_SETTINGS_AI_PLUS_PORT1, "modeType": 15, "isOpenAutomation": 0},
    )
    assert cap["result"]["sent"] is True


@pytest.mark.parametrize("settings_patch", [
    {"isOpenAutomation": 1},   # explicitly active
    {},                        # absent entirely — safe-fail treats as active
])
def test_ai_plus_advance_guard_fires_on_is_open_automation(ai_plus_device, settings_patch):
    """On AI+, isOpenAutomation alone gates the write: present-and-0, or refuse."""
    settings = {k: v for k, v in MOCK_MODE_SETTINGS_AI_PLUS_PORT1.items()
                if k != "isOpenAutomation"}
    settings.update(settings_patch)
    c = ACInfinityClient("test@example.com", "pw")
    c.token = "tok"

    with patch.object(c.session, "post") as post, \
         patch.object(c, "_enforce_write_rate_limit"), \
         patch.object(c, "get_mode_settings", autospec=True, return_value=settings):
        with pytest.raises(ACInfinityAdvanceConflictError):
            c.set_port_mode(ai_plus_device, port=1, updates={"onSpead": 5}, dry_run=False)
    post.assert_not_called()


def test_legacy_advance_guard_still_requires_mode_type_15(legacy_11_device):
    """Legacy behaviour is unchanged: modeType=15 AND isOpenAutomation != 0."""
    c = ACInfinityClient("test@example.com", "pw")
    c.token = "tok"
    cap = _capture_write(
        c, legacy_11_device, {"onSpead": 5},
        settings={**MOCK_MODE_SETTINGS_LEGACY_PORT1, "modeType": 0, "isOpenAutomation": 1},
    )
    assert cap["result"]["sent"] is True


# ============ 999999 scoping (legacy conflict UX must not regress) ============
#
# The speed-write reroute is scoped to NEW_FRAMEWORK. 999999 correlates strongly
# with an empty port (12 no-op writes across devType 11 and 20: every
# portResistance == 65535 port returned 999999, every connected port 200, on a
# controller with zero Advance Automations - Quirk 37). We deliberately do not
# branch on portResistance to say so: per #315 it is a frozen 15800 on devType 22
# and 65535 on legacy ports that do have equipment attached.


def test_999999_speed_write_on_legacy_keeps_advance_conflict(legacy_11_device):
    """Legacy loadType is dependable, so a cleared guard + 999999 is a real conflict.

    The AI+ reroute exists because loadType is unreliable there (Quirks 24/34).
    Applying it to legacy would replace an accurate ADVANCE-conflict response
    with on/off-hardware guidance that does not fit the device.
    """
    c = ACInfinityClient("test@example.com", "pw")
    c.token = "tok"

    with patch.object(c.session, "post", _post_returning(999999, "Operation failed")), \
         patch.object(c, "_enforce_write_rate_limit"), \
         patch.object(c, "get_mode_settings", autospec=True,
                      return_value={**MOCK_MODE_SETTINGS_LEGACY_PORT1, "loadType": 0}):
        with pytest.raises(ACInfinityAdvanceConflictError):
            c.set_port_mode(legacy_11_device, port=1, updates={"onSpead": 5},
                            dry_run=False, require_variable_speed=True)


def test_999999_speed_write_on_ai_plus_still_reroutes(ai_plus_device):
    """The counterpart: on NEW_FRAMEWORK the reroute must still fire."""
    c = ACInfinityClient("test@example.com", "pw")
    c.token = "tok"

    with patch.object(c.session, "post", _post_returning(999999, "Operation failed")), \
         patch.object(c, "_enforce_write_rate_limit"), \
         patch.object(c, "get_mode_settings", autospec=True,
                      return_value={**AI_PLUS_SETTINGS, "loadType": 0}):
        with pytest.raises(ACInfinityDeviceError) as exc:
            c.set_port_mode(ai_plus_device, port=1, updates={"onSpead": 5},
                            dry_run=False, require_variable_speed=True)

    assert "set_port_on" in str(exc.value)
    assert "on/off hardware" in str(exc.value)
