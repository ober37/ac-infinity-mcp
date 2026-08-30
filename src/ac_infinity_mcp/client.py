import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ac_infinity_mcp.analytics import _TOGGLE_LOAD_TYPES
from ac_infinity_mcp.controller import ControllerType, build_write_payload, detect_controller_type
from ac_infinity_mcp.schema import (
    ACInfinityAdvanceConflictError,
    ACInfinityAPIError,
    ACInfinityAuthError,
    ACInfinityDeviceError,
)

logger = logging.getLogger(__name__)

# portResistance == 65535 is the hardware open-circuit sentinel (Quirk 27):
# nothing is connected to the port. Duplicated from ports.py rather than imported
# to keep client.py free of a dependency on the server-side helpers.
_PORT_EMPTY_RESISTANCE = 65535


def _port_resistance(device_data: dict, port: int) -> int | None:
    """Return a port's portResistance from a devInfoListAll device dict, or None."""
    for p in (device_data.get("deviceInfo") or {}).get("ports", []):
        if p.get("port") == port:
            raw = p.get("portResistance")
            if raw is None:
                return None
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None
    return None

# ---------------------------------------------------------------------------
# App-identity headers
#
# This client declares two different app identities to the same host, each
# captured from a different AC Infinity app build. They are deliberately not
# unified: each was observed being accepted on the endpoints it is used for, and
# we have no capture proving either is accepted everywhere.
#
#   _LOGIN_UA           -> /user/appUserLogin only. Phase-1 capture, app 1.8.2.
#   okhttp/3.10.0       -> v1 reads and all writes. Upstream default, unchanged.
#   _V2_APP_VERSION     -> v2 automation endpoints (_v2_headers), app 2.0.4.
#   _AI_PLUS_MINVERSION -> AI+ (devType >= 20) writes. NOT an app identity — an
#                          opaque server-side gate. See Quirk 14 and below.
#
# docs/SECURITY-RISKS.md documents the UA-spoofing posture and locks these
# values with exact-value regression tests. The AI+ write path deliberately
# spoofs no User-Agent at all, so it adds no row to that table.
# ---------------------------------------------------------------------------
_LOGIN_UA = "ACController/1.8.2 (com.acinfinity.humiture; build:489; iOS 16.5.1)"

_V2_APP_VERSION = "2.0.4"

# AI+ (devType >= 20) port writes are gated on a single header: `minversion`,
# which must be the exact string "3.5". Nothing else is required — not a spoofed
# User-Agent, not `appVersion`, not `phoneType`. Ablated one header at a time
# against live devType-20 hardware with a no-op write (see Quirk 14 for the full
# matrix); `minversion: "3.5"` alone against the stock okhttp header set returns
# 200, and every other combination that omits it returns 100001.
#
# Despite the name it is NOT a minimum-version comparison. "3.4", "3.6", "3",
# "3.50", "3.5.0", "" and "99.9" all fail — a higher value fails just as a lower
# one does, so the server is matching the literal string, not parsing a version.
# Treat it as an opaque magic constant, not a number to bump.
_AI_PLUS_MINVERSION = "3.5"

# Maps AC Infinity sensorType → (grower-readable label, unit). The unit is a
# property of the sensorType itself: AC Infinity assigns a distinct type per unit
# variant (EC µS/cm=14 vs mS/cm=15, TDS ppm=16 vs ppt=17, water-temp °F=18 vs °C=19),
# so the type alone is unambiguous — the raw `sensorUnit` field is only an F/C flag and
# is intentionally not surfaced. Confirmed against the HA `ac_infinity` integration.
# Water-temp polarity (18=°F, 19=°C) follows HA const.py (even=°F/odd=°C across all its
# temperature types) and the API's own waterTempHighValueF/waterTempHighValue convention;
# unverified against live hydro hardware. Unit "" means the quantity is genuinely unitless.
_SENSOR_TYPE_INFO: dict[int, tuple[str, str]] = {
    10: ("Soil Moisture", "%"),
    11: ("CO2", "ppm"),
    12: ("Light", "%"),
    13: ("pH", ""),
    14: ("EC", "µS/cm"),
    15: ("EC", "mS/cm"),
    16: ("TDS", "ppm"),
    17: ("TDS", "ppt"),
    18: ("Water Temp", "°F"),
    19: ("Water Temp", "°C"),
    20: ("Water Level", ""),
}


def _sensor_label(sensor_type: int | None) -> str:
    """Return grower-readable label for sensor type, or a safe fallback."""
    if sensor_type in _SENSOR_TYPE_INFO:
        return _SENSOR_TYPE_INFO[sensor_type][0]
    if sensor_type is not None:
        try:
            return f"Unrecognized (type {int(sensor_type)})"
        except (ValueError, TypeError):
            return "Unknown"
    return "Unknown"


def _sensor_unit(sensor_type: int | None) -> str:
    """Return the unit for a sensor type (derived from the type, not the API
    ``sensorUnit`` field). Empty string for unitless quantities (pH, water level)
    and for unrecognized/None types."""
    if sensor_type in _SENSOR_TYPE_INFO:
        return _SENSOR_TYPE_INFO[sensor_type][1]
    return ""


def _should_include_sensor(s: dict) -> bool:
    """Return True if this sensor entry is non-phantom and should be included."""
    sensor_type = s.get("sensorType")
    if sensor_type is None:
        return False
    if sensor_type in _SENSOR_TYPE_INFO:
        return True  # recognized external sensor type (10–20): always include
    try:
        if int(sensor_type) < 10:
            return False  # types 1–9 are internal/built-in readings, not external hardware
    except (ValueError, TypeError):
        return False
    return (s.get("sensorData") or 0) != 0  # unrecognized high type: include if non-zero


# Real AC Infinity sensorPrecision values are 1-3; anything well above this is a
# malformed response, not a 4+ decimal sensor. Cap generously to passthrough.
_MAX_SENSOR_PRECISION: int = 6


def _sensor_value(s: dict) -> float | int:
    """Scale a raw sensor reading to its real-world value.

    AC Infinity encodes ``sensorPrecision`` as a decimal-place exponent, not a
    literal divisor: the real value is ``sensorData / 10**(precision - 1)``.
    Precision <= 1 (or absent) means the raw integer is already the value, and
    is returned as-is to avoid spurious floats (e.g. CO2 ``500`` stays ``500``,
    not ``500.0``). Mirrors the AC Infinity app and the HA ``ac_infinity``
    integration so a 0-100% light reading (sensorType 12) is not mis-scaled.

    Real precision values are 1-3. An implausibly large value (a malformed API
    response) would otherwise yield a silent near-zero reading that looks like a
    dead sensor; such values are logged and treated as raw passthrough instead.
    """
    data = s.get("sensorData") or 0
    precision = s.get("sensorPrecision")
    if precision is None:
        precision = 1
    if precision > _MAX_SENSOR_PRECISION:
        logger.warning(
            "Implausible sensorPrecision %d (sensorType=%s); real values are 1-3. "
            "Treating the reading as raw to avoid a silent near-zero value.",
            precision,
            s.get("sensorType"),
        )
        precision = 1
    return data / (10 ** (precision - 1)) if precision > 1 else data


# The controller's own onboard sensor and the AC-SPC24 plug-in probe report through
# the same `sensors` array, distinguished by a published sensorType enum rather than
# by any arithmetic pattern. Confirmed against the HA `ac_infinity` integration
# (dalinicus/homeassistant-acinfinity, const.py):
#   0 PROBE_TEMPERATURE_F   1 PROBE_TEMPERATURE_C   2 PROBE_HUMIDITY   3 PROBE_VPD
#   4 CONTROLLER_TEMP_F     5 CONTROLLER_TEMP_C     6 CONTROLLER_HUM   7 CONTROLLER_VPD
# HA attaches 0-3 to a child device it names "UIS Controller Sensor Probe (AC-SPC24)"
# and 4-7 to the controller itself.
_ONBOARD_SENSOR_TYPES = frozenset({4, 5, 6, 7})  # already surfaced as the top-level reading
_PROBE_TEMP_TYPES = (0, 1)                        # 0 = raw °F, 1 = raw °C
_PROBE_HUMIDITY_TYPE = 2
_PROBE_VPD_TYPE = 3


def _extract_probes(sensors: list[dict] | None) -> list[dict]:
    """Extract readings from plug-in AC-SPC24 probes (sensorType 0-3).

    The controller's own sensor (types 4-7) is excluded by type: it is already
    reported as the top-level temperature/humidity/vpd, so surfacing it here
    would double-report it. Identifying it by type rather than by comparing
    values means a genuine probe reading identically to the onboard sensor still
    appears, and a probe present without any onboard group still appears.

    ``_should_include_sensor`` deliberately keeps all ``sensorType < 10`` entries
    out of ``external_sensors`` (Quirk 20) — correct, but it left real probe data
    with nowhere to go. This is that missing path, not a change to that rule.

    Temperature is normalised to Celsius here so the conversion happens once, in
    one direction, matching every other temperature in this module. The per-entry
    ``sensorUnit`` flag decides the raw scale (>0 means already Celsius); when it
    is absent the sensorType itself disambiguates (0 = °F, 1 = °C).
    """
    by_port: dict[int, dict[int, dict]] = {}
    for s in sensors or []:
        raw_type = s.get("sensorType")
        raw_port = s.get("accessPort")
        if raw_type is None or raw_port is None:
            continue
        try:
            s_type = int(raw_type)
            port = int(raw_port)
        except (TypeError, ValueError):
            # Malformed entry — skip it rather than letting an uncoerced key
            # propagate into the grouping and take out the whole device reading.
            continue
        if s_type >= 10 or s_type in _ONBOARD_SENSOR_TYPES:
            continue
        if s.get("sensorData") is None:
            continue  # absent, not zero — let the completeness check drop the group
        by_port.setdefault(port, {})[s_type] = s

    probes: list[dict] = []
    for port, entries in sorted(by_port.items()):
        temp = next((entries[t] for t in _PROBE_TEMP_TYPES if t in entries), None)
        if temp is None or _PROBE_HUMIDITY_TYPE not in entries or _PROBE_VPD_TYPE not in entries:
            logger.info(
                "Unrecognized sensorType<10 group on accessPort %s: %s — skipped",
                port, sorted(entries),
            )
            continue

        humidity_entry = entries[_PROBE_HUMIDITY_TYPE]
        vpd_entry = entries[_PROBE_VPD_TYPE]
        if not any(e.get("sensorData") for e in (temp, humidity_entry, vpd_entry)):
            continue  # unpopulated slot — the Quirk 20 phantom class

        raw = _sensor_value(temp)
        unit_flag = temp.get("sensorUnit")
        is_celsius = (
            unit_flag > 0 if isinstance(unit_flag, int)
            else temp.get("sensorType") in (1, "1")
        )
        probes.append({
            "sensor_port": port,
            "temperature_c": round(raw if is_celsius else (raw - 32) * 5 / 9, 1),
            "humidity_pct": round(_sensor_value(humidity_entry), 1),
            "vpd_kpa": round(_sensor_value(vpd_entry), 2),
        })
    return probes


_SCHEDULE_ALWAYS_ACTIVE: int = 255

# API response codes that mean "session expired — re-authenticate" (HTTP body code,
# distinct from HTTP 401). On a READ these trigger a transparent token refresh + retry;
# on a WRITE the code surfaces as an API error instead (no replay — double-apply guard).
# 10003 is documented by the AC Infinity community as the session-expired code; treated
# defensively (the refresh-failure cache bounds re-login to one attempt regardless).
_SESSION_EXPIRED_API_CODES: frozenset[int] = frozenset({10003})

# Some v2 endpoints signal a genuine session expiry as HTTP-200 body code 403 with a
# "Login Expired Please login again!" message rather than code 10003 (Issue #298). Because
# 403 is overloaded on writes (rate-limit "Data saving failed", field-validation errors),
# we treat a 403 as session-expiry ONLY on the read path (session_refreshable=True) AND only
# when the message carries one of these markers — never on the bare 403 code.
_SESSION_EXPIRED_MSG_MARKERS: tuple[str, ...] = ("login expired", "login again")


# ============ Rail sentinels (Issue #284) ============
# A trigger/target parked at its rail means "inactive" — the app stores the rail value
# (not the derived/real value) for the unit/family that is not in use. The encoder writes
# these rails explicitly; the decoder treats a value AT its rail as "not a clause".
_RAIL_TEMP_HIGH_F = 194
_RAIL_TEMP_HIGH_C = 90  # the °C the app pairs with the 194°F high rail (a fixed rail, NOT derived)
_RAIL_TEMP_LOW_F = 32
_RAIL_TEMP_LOW_C = 0
_RAIL_HUMI_HIGH = 100
_RAIL_HUMI_LOW = 0
_RAIL_VPD_HIGH = 99
_RAIL_VPD_LOW = 0

# currentMode values for the Groups (Advance Automation) API. Single mode pick, matches
# the app's Advance Automation screen: Off/On/Auto/VPD/Cycle = 2/1/4/6/3.
# NOTE: this is a DIFFERENT enum from the legacy per-port `atType` (getdevModeSettingList):
# atType OFF=1, ON=2, AUTO=3, TIMER=4/5, CYCLE=6, SCHEDULE=7, VPD=8. Do not conflate the two.
_MODE_ON = 1
_MODE_OFF = 2
_MODE_CYCLE = 3
_MODE_AUTO = 4
_MODE_VPD = 6


def resolve_port_type(raw_entries: list[dict[str, Any]], ports: list[int]) -> int:
    """Resolve the device-identity ``portType`` for ``ports`` from existing getGroups rules.

    ``portType`` (0 = variable-speed fan, 1 = on/off outlet/power-adaptor) is a per-port
    attribute the AC Infinity app persists ONLY inside automation rules — it is absent from
    ``devInfoListAll`` and ``getdevModeSettingList`` (Issue #300). Sending the wrong value makes
    the app render an outlet rule as a fan rule (phantom MIN/MAX speed range).

    Returns the ``portType`` of the first existing rule whose ``grouptDevType`` bitmask covers
    ANY of ``ports`` (rules group same-device-type ports, so the value is consistent). Returns
    ``0`` when no existing rule governs any target port — the value is then undiscoverable via
    the read APIs (accepted limitation: the very first automation on a fresh outlet port may
    still need an in-app fix). Missing/None ``grouptDevType``/``portType`` coerce to 0 (the
    entry is skipped or read as a fan), so a malformed entry degrades gracefully.
    """
    for port in ports:
        bit = 1 << (port - 1)
        for entry in raw_entries:
            if int(entry.get("grouptDevType") or 0) & bit:
                return int(entry.get("portType") or 0)
    return 0


def build_groups_payload(
    dev_id: str,
    ports: list[int],
    clean_name: str,
    begin_time: int,
    end_time: int,
    *,
    mode: str = "on",
    control_style: str | None = None,
    on_speed: int | None = None,
    min_level: int = 0,
    max_level: int = 10,
    temp_high_f: int | None = None,
    temp_low_f: int | None = None,
    humidity_high: int | None = None,
    humidity_low: int | None = None,
    temp_target_f: int | None = None,
    humidity_target: int | None = None,
    vpd_target: float | None = None,
    vpd_high: float | None = None,
    vpd_low: float | None = None,
    temp_buffer: int | None = None,
    temp_transition: int | None = None,
    humidity_buffer: int | None = None,
    humidity_transition: int | None = None,
    vpd_buffer: float | None = None,
    vpd_transition: float | None = None,
    cycle_on_minutes: int | None = None,
    cycle_off_minutes: int | None = None,
    switch_time: int = 127,
    sub_number: int = 0,
    is_flag: int = 1,
    group_nums: int | None = None,
    sort_type: int | None = None,
    adv_id: int | None = None,
    port_type: int = 0,
) -> dict[str, Any]:
    """Build the addGroups / updateGroupsById payload for one Advance Automation rule.

    Compositional surface (Issue #284, byte-grounded in the user's real rules + capture
    program "0624"). ``ports`` is a list of 1-based port numbers governed by this single
    rule; the grouptDevType bitmask is ``sum(2**(p-1) for p in ports)``.

    ``mode`` is the single mode pick: ``off`` (currentMode=2), ``on`` (1), ``cycle`` (3),
    ``auto`` (4), ``vpd`` (6). Auto and VPD additionally take a ``control_style``:
    ``target`` (settingMode=1) or ``trigger`` (settingMode=0).

    Speed range: ``max_level`` → ``onSpeed``, ``min_level`` → ``offSpeed``. (``on_speed``
    is a legacy alias for the On-mode max; when given and ``max_level`` is its default it
    sets onSpeed directly to preserve the original create byte-identity.)

    Sensor targets/triggers are explicitly assigned per field; inactive families are parked
    at their rail (°C parked at rail, NOT derived). Buffer → ``*Buff``, transition →
    ``*Trans`` (the two are mutually exclusive per sensor — validated upstream). ``switch_time``
    bits 0–6 = days (bit0=Mon … bit6=Sun), bit 7 = continuous.

    A *program* is a shared ``(groupNums, sortType)`` slot whose rules carry sequential
    ``subNumber``. ``addGroups`` is gated by ``is_flag`` (Issue #284, iOS-app capture):
    ``is_flag=1`` → NEW program (server assigns a fresh slot, ``subNumber=0`` — sent
    ``group_nums``/``sort_type`` are ignored); ``is_flag=0`` → APPEND (server HONORS the
    sent ``group_nums`` + ``sort_type`` = the target program's slot, and
    ``subNumber``/``subNumberSort`` = existing max + 1, so the rule joins that program).
    The defaults (``is_flag=1``, no slot, ``sub_number=0``) reproduce the original
    new-program output byte-for-byte.

    NO ``{**base, **caller}`` spread — every field is assigned explicitly so an unknown
    caller param can never reach the payload. ``adv_id`` is included only on the update path.
    """
    grp_dev_type = sum(2 ** (p - 1) for p in ports)

    # Resolve the On/Cycle speed: legacy On-mode create passes on_speed; the rule family
    # passes max_level/min_level. on_speed (when set) takes precedence for the On byte path.
    resolved_on_speed = on_speed if on_speed is not None else max_level
    resolved_off_speed = min_level

    payload: dict[str, Any] = {
        # devId NOT included here — the inner method injects it.
        # advCode NOT included — absent from addGroups live capture (unlike addAlarms).
        # isFlag (capital F) confirmed for addGroups;
        # isflag (lowercase) for updateGroupsIsOn/delByid.
        "advName": clean_name,
        "currentMode": _MODE_ON,
        "isOn": 1,
        "onSpeed": resolved_on_speed,
        # On mode has no user-settable min; port's own min setting is used.
        "offSpeed": resolved_off_speed,
        # Map "always active" sentinel to a valid full-day range.
        "beginTime": 0 if begin_time == _SCHEDULE_ALWAYS_ACTIVE else begin_time,
        "endTime": 1439 if end_time == _SCHEDULE_ALWAYS_ACTIVE else end_time,
        # groupNums/sortType define the program SLOT. On a NEW program (isFlag=1) the
        # server assigns the slot and ignores these; on APPEND (isFlag=0) the caller passes
        # the target program's slot so the rule joins it (Issue #284).
        "groupNums": 9 if group_nums is None else group_nums,
        "sortType": 9 if sort_type is None else sort_type,
        "subNumber": sub_number,
        "subNumberSort": sub_number,
        "isDel": 0,
        "isFlag": is_flag,
        "returnData": 1,
        "templateType": 0,
        "grouptDevType": grp_dev_type,
        # Issue #300: portType is a per-port device-identity attribute (0 = variable-speed
        # fan, 1 = on/off outlet/power-adaptor). It is exposed by NO read endpoint — only by
        # existing getGroups rules — so callers resolve it via resolve_port_type() and pass it
        # here. Default 0 preserves byte-identity for the golden-payload tests / On-mode shim.
        "portType": port_type,
        "portState": 0,
        "portSetHex": "",
        "portStateHex": "",
        "autoHighTempF": 110,
        "autoLowTempF": 40,
        "autoHighTempC": 90,
        "autoLowTempC": 0,
        "autoHighTempSwitch": 1,
        "autoLowTempSwitch": 1,
        "autoHighHumi": 90,
        "autoLowHumi": 40,
        "autoHighHumiSwitch": 1,
        "autoLowHumiSwitch": 1,
        "highVpd": 99,
        "lowVpd": 0,
        "highVpdSwitch": 1,
        "lowVpdSwitch": 1,
        "cycleOn": 0,
        "cycleOff": 0,
        "onTime": 0,
        "onTimeSwitch": 0,
        # bits 0-6 = days (bit0=Mon), bit 7 = continuous. 127 = all 7 days scheduled;
        # 255 (= 127 | 128) sets bit 7 → app treats the rule as Continuous 24/7.
        "switchTime": switch_time,
        "dualZoneSwitch": 1,
        "photocellSwitch": 0,
        "isOpenDoseTime": 0,
        "onDoseTime": 60,
        "offDoseTime": 1,
        "isOnMinMaxTime": 1,
        "onMinTime": 0,
        "onMaxTime": 0,
        "settingMode": 0,
        "targetTSwitch": 1,
        "targetHumiSwitch": 1,
        "targetVpdSwitch": 1,
        "targetTemp": 0,
        "targetTempF": 32,
        "targetHumi": 0,
        "targetVpd": 0,
        "insidePort": 255,
        "insideType": 15,
        "outsidePort": 255,
        "outsideType": 15,
        "runState": 0,
        "setSelect": 0,
        "humidityBuff": 0,
        "humidityTrans": 0,
        "temperatureFBuff": 0,
        "temperatureFTrans": 0,
        "switchHumidityBuff": 0,
        "switchTemperatureFBuff": 0,
        "switchVpdBuff": 0,
        "vpdBuff": 0,
        "vpdTrans": 0,
        "nameLangKey": "",
        "remarkLangKey": "",
    }

    if mode == "on":
        # Base dict is the verified On-mode signature — leave as-is.
        pass
    elif mode == "off":
        payload["currentMode"] = _MODE_OFF
    elif mode == "cycle":
        payload["currentMode"] = _MODE_CYCLE
        # cycleOn/cycleOff are stored in SECONDS on the controller (the app shows
        # minutes = seconds/60; verified live: cycleOn=30 rendered as "0 min").
        payload["cycleOn"] = int(cycle_on_minutes or 0) * 60
        payload["cycleOff"] = int(cycle_off_minutes or 0) * 60
    elif mode == "auto":
        _apply_auto(
            payload,
            control_style=control_style,
            temp_high_f=temp_high_f, temp_low_f=temp_low_f,
            humidity_high=humidity_high, humidity_low=humidity_low,
            temp_target_f=temp_target_f, humidity_target=humidity_target,
        )
    elif mode == "vpd":
        _apply_vpd(
            payload,
            control_style=control_style,
            vpd_target=vpd_target, vpd_high=vpd_high, vpd_low=vpd_low,
        )

    # Buffer / transition (per sensor). Buffer and transition are mutually exclusive per
    # sensor (validated upstream); whichever is set lands in its dedicated field.
    if temp_buffer is not None:
        payload["temperatureFBuff"] = int(temp_buffer)
    if temp_transition is not None:
        payload["temperatureFTrans"] = int(temp_transition)
    if humidity_buffer is not None:
        payload["humidityBuff"] = int(humidity_buffer)
    if humidity_transition is not None:
        payload["humidityTrans"] = int(humidity_transition)
    if vpd_buffer is not None:
        payload["vpdBuff"] = round(float(vpd_buffer) * 10)
    if vpd_transition is not None:
        payload["vpdTrans"] = round(float(vpd_transition) * 10)

    if adv_id is not None:
        payload["advId"] = adv_id
    return payload


def _apply_auto(
    payload: dict[str, Any],
    *,
    control_style: str | None,
    temp_high_f: int | None,
    temp_low_f: int | None,
    humidity_high: int | None,
    humidity_low: int | None,
    temp_target_f: int | None,
    humidity_target: int | None,
) -> None:
    """Apply the currentMode=4 (Auto) signature in place.

    Trigger sub-mode (settingMode=0, setSelect=1): each named threshold sets its value +
    switch=1; the opposite/unused threshold is parked at its rail with switch=0. Target
    sub-mode (settingMode=1, setSelect=0): rails for the trigger families with switches=1,
    real values in targetHumi / targetTempF.
    """
    payload["currentMode"] = _MODE_AUTO

    # VPD family is inert in Auto mode — the app zeroes it (value 0, switch 0) for BOTH
    # target and trigger sub-modes (verified live against app-made Auto rules). The old code
    # parked it at the 99 rail with switches=1, which the app rendered as phantom VPD high/low
    # triggers on an Auto rule (#288 root cause).
    payload["highVpd"] = 0
    payload["highVpdSwitch"] = 0
    payload["lowVpd"] = 0
    payload["lowVpdSwitch"] = 0
    payload["targetVpd"] = 0
    payload["targetVpdSwitch"] = 0

    if control_style == "target":
        # Auto-target: settingMode=1, setSelect=0. Trigger families parked at rails with
        # switches=1; the held setpoint goes in targetHumi / targetTempF. Both target
        # switches stay 1 (captured signature); the unused sensor sits at its rail.
        payload["settingMode"] = 1
        payload["setSelect"] = 0
        payload["autoHighTempF"] = _RAIL_TEMP_HIGH_F
        payload["autoHighTempC"] = _RAIL_TEMP_HIGH_C
        payload["autoLowTempF"] = _RAIL_TEMP_LOW_F
        payload["autoLowTempC"] = _RAIL_TEMP_LOW_C
        payload["autoHighTempSwitch"] = 1
        payload["autoLowTempSwitch"] = 1
        payload["autoHighHumi"] = _RAIL_HUMI_HIGH
        payload["autoLowHumi"] = _RAIL_HUMI_LOW
        payload["autoHighHumiSwitch"] = 1
        payload["autoLowHumiSwitch"] = 1
        payload["targetTempF"] = (
            int(temp_target_f) if temp_target_f is not None else _RAIL_TEMP_LOW_F
        )
        payload["targetHumi"] = int(humidity_target) if humidity_target is not None else 0
        payload["targetTSwitch"] = 1
        payload["targetHumiSwitch"] = 1
    else:
        # Auto-trigger: settingMode=0, setSelect=1. Named thresholds active; unused
        # families parked at rails with switch=0.
        payload["settingMode"] = 0
        payload["setSelect"] = 1

        if temp_high_f is not None:
            payload["autoHighTempF"] = int(temp_high_f)
            payload["autoHighTempC"] = _RAIL_TEMP_HIGH_C
            payload["autoHighTempSwitch"] = 1
        else:
            payload["autoHighTempF"] = _RAIL_TEMP_HIGH_F
            payload["autoHighTempC"] = _RAIL_TEMP_HIGH_C
            payload["autoHighTempSwitch"] = 0
        if temp_low_f is not None:
            payload["autoLowTempF"] = int(temp_low_f)
            payload["autoLowTempC"] = _RAIL_TEMP_LOW_C
            payload["autoLowTempSwitch"] = 1
        else:
            payload["autoLowTempF"] = _RAIL_TEMP_LOW_F
            payload["autoLowTempC"] = _RAIL_TEMP_LOW_C
            payload["autoLowTempSwitch"] = 0

        if humidity_high is not None:
            payload["autoHighHumi"] = int(humidity_high)
            payload["autoHighHumiSwitch"] = 1
        else:
            payload["autoHighHumi"] = _RAIL_HUMI_HIGH
            payload["autoHighHumiSwitch"] = 0
        if humidity_low is not None:
            payload["autoLowHumi"] = int(humidity_low)
            payload["autoLowHumiSwitch"] = 1
        else:
            payload["autoLowHumi"] = _RAIL_HUMI_LOW
            payload["autoLowHumiSwitch"] = 0

        # Target family parked at rails with switches=1 (captured Auto-trigger signature).
        payload["targetTempF"] = _RAIL_TEMP_LOW_F
        payload["targetTSwitch"] = 1
        payload["targetHumiSwitch"] = 1
        payload["targetHumi"] = 0


def _apply_vpd(
    payload: dict[str, Any],
    *,
    control_style: str | None,
    vpd_target: float | None,
    vpd_high: float | None,
    vpd_low: float | None,
) -> None:
    """Apply the currentMode=6 (VPD) signature in place.

    The auto temp/humidity families and the temp/humidity target families are INERT in VPD
    mode — the app zeroes them (values at 0/32 rails, all switches 0). The old code left them
    at the base defaults (90/110 with switches=1), which the app rendered as phantom triggers
    on a VPD rule (#288 — verified live against the app's Clone Transplant VPD-target rule).

    Target (settingMode=1): the app mirrors the setpoint into both targetVpd and highVpd
    (highVpdSwitch=1) and leaves lowVpd=0 with lowVpdSwitch=0. Trigger (settingMode=0):
    highVpd/lowVpd = kpa*10 with switch=1; the unused direction parked at its rail/switch=0.
    """
    payload["currentMode"] = _MODE_VPD

    # Zero the auto + temp/humidity-target families (inert in VPD mode; see docstring / #288).
    payload["autoHighHumi"] = 0
    payload["autoLowHumi"] = 0
    payload["autoHighHumiSwitch"] = 0
    payload["autoLowHumiSwitch"] = 0
    payload["autoHighTempF"] = _RAIL_TEMP_LOW_F
    payload["autoLowTempF"] = _RAIL_TEMP_LOW_F
    payload["autoHighTempC"] = 0
    payload["autoLowTempC"] = 0
    payload["autoHighTempSwitch"] = 0
    payload["autoLowTempSwitch"] = 0
    payload["targetHumi"] = 0
    payload["targetHumiSwitch"] = 0
    payload["targetTempF"] = _RAIL_TEMP_LOW_F
    payload["targetTSwitch"] = 0

    if control_style == "trigger":
        payload["settingMode"] = 0
        payload["setSelect"] = 0
        payload["targetVpd"] = 0
        payload["targetVpdSwitch"] = 0
        if vpd_high is not None:
            payload["highVpd"] = round(float(vpd_high) * 10)
            payload["highVpdSwitch"] = 1
        else:
            payload["highVpd"] = _RAIL_VPD_HIGH
            payload["highVpdSwitch"] = 0
        if vpd_low is not None:
            payload["lowVpd"] = round(float(vpd_low) * 10)
            payload["lowVpdSwitch"] = 1
        else:
            payload["lowVpd"] = _RAIL_VPD_LOW
            payload["lowVpdSwitch"] = 0
    else:
        # VPD-target: mirror the setpoint into targetVpd + highVpd; lowVpd off (captured sig).
        payload["settingMode"] = 1
        payload["setSelect"] = 0
        tgt = round(float(vpd_target) * 10) if vpd_target is not None else 0
        payload["targetVpd"] = tgt
        payload["targetVpdSwitch"] = 1
        payload["highVpd"] = tgt
        payload["highVpdSwitch"] = 1
        payload["lowVpd"] = 0
        payload["lowVpdSwitch"] = 0


def build_add_groups_payload(
    dev_id: str,
    port: int,
    clean_name: str,
    on_speed: int,
    begin_time: int,
    end_time: int,
) -> dict[str, Any]:
    """Build the addGroups API payload for create_advance_automation (On mode, one port).

    Thin shim over ``build_groups_payload`` for the original single-port On-mode create
    path; output is byte-identical to the pre-refactor builder (golden-payload regression).
    """
    return build_groups_payload(
        dev_id=dev_id,
        ports=[port],
        clean_name=clean_name,
        begin_time=begin_time,
        end_time=end_time,
        mode="on",
        on_speed=on_speed,
    )


class ACInfinityClient:
    """Client for AC Infinity cloud API"""

    BASE_URL = "https://www.acinfinityserver.com/api"
    LOGIN_ENDPOINT = f"{BASE_URL}/user/appUserLogin"
    DEVICES_ENDPOINT = f"{BASE_URL}/user/devInfoListAll"
    HISTORY_ENDPOINT = f"{BASE_URL}/log/dataPage"
    MODE_SETTINGS_ENDPOINT = f"{BASE_URL}/dev/getdevModeSettingList"
    ADD_DEV_MODE_ENDPOINT = f"{BASE_URL}/dev/addDevMode"
    MODE_AND_SETTING_ENDPOINT = f"{BASE_URL}/dev/modeAndSetting"

    # v2.0 Automation management endpoints. The path prefix embeds the version
    # string as a literal path segment, which is an unusual but confirmed API design.
    V2_BASE_URL = "https://www.acinfinityserver.com"
    V2_GET_GROUPS_ENDPOINT = f"{V2_BASE_URL}/api/version=2.0/dev/getGroups"
    V2_ADD_GROUPS_ENDPOINT = f"{V2_BASE_URL}/api/version=2.0/dev/addGroups"
    V2_UPDATE_GROUPS_IS_ON_ENDPOINT = f"{V2_BASE_URL}/api/version=2.0/dev/updateGroupsIsOn"
    V2_UPDATE_GROUPS_BY_ID_ENDPOINT = f"{V2_BASE_URL}/api/version=2.0/dev/updateGroupsById"
    V2_DEL_BY_ID_ENDPOINT = f"{V2_BASE_URL}/api/version=2.0/dev/delByid"

    def __init__(self, email: str, password: str):
        self.email = email
        if len(password) > 25:
            logger.warning(
                "Password length %d exceeds the 25-character AC Infinity API limit "
                "(Quirk 2 in docs/API.md); using the first 25 characters only. "
                "If authentication fails, change your AC Infinity account password "
                "to 25 or fewer characters and update AC_INFINITY_PASSWORD.",
                len(password),
            )
        self.password = password[:25]  # API silently truncates to 25 chars (Quirk 2)
        self.token: str | None = None
        # Shared across asyncio.to_thread calls. urllib3 pool is thread-safe;
        # cookie jar thread safety is moot because this API uses header tokens only.
        self.session = requests.Session()
        self._last_write_time: float = 0.0
        self._write_lock = threading.Lock()
        self._auth_lock = threading.Lock()
        self._auth_error: ACInfinityAuthError | None = None

    def _raise_for_api_code(
        self,
        code: int | None,
        error_msg: str | None,
        context: str,
        *,
        session_refreshable: bool = True,
    ) -> None:
        """Map an API response code to the appropriate exception.

        ``session_refreshable`` (default ``True``, used by reads) controls whether a
        session-expiry code is raised as ACInfinityAuthError so the caller's token
        refresh-and-retry path fires. Write paths pass ``False``: a write that comes
        back with a session-expiry code must NOT be transparently replayed — the
        server may already have applied it, so a retry would double-apply state (same
        rationale as excluding Timeout from write retries). On a write, the
        session-expiry code surfaces as a plain ACInfinityAPIError instead.

        ``401`` is always treated as an auth failure (unchanged on every path).

        A ``403`` carrying a "login expired" message (Issue #298) is treated as a
        refreshable session-expiry on reads only — gated on the message marker, not the
        bare 403 code, so write-path 403s (rate-limit / field-validation) are never
        misclassified. ``(error_msg or "")`` guards against a null ``msg`` in the body.
        """
        msg_lower = (error_msg or "").lower()
        session_expired_403 = (
            code == 403
            and session_refreshable
            and any(marker in msg_lower for marker in _SESSION_EXPIRED_MSG_MARKERS)
        )
        if (
            code == 401
            or (session_refreshable and code in _SESSION_EXPIRED_API_CODES)
            or session_expired_403
        ):
            raise ACInfinityAuthError(f"Token rejected by API (code {code}): {error_msg}")
        raise ACInfinityAPIError(f"{context} API error {code}: {error_msg}")

    def _call_with_token_refresh(self, fn, *args, **kwargs):
        """Lazy-auth preamble + 401-refresh.

        On the first tool call (token is None), authenticate before calling fn().
        Subsequent calls skip the preamble. On an auth rejection mid-session
        (HTTP 401 or a session-expiry body code on a read), re-authenticate once
        and retry transparently.

        _auth_error is set on the first credential failure — both at the initial
        login and on a failed mid-session refresh — so that concurrent callers and
        subsequent callers raise immediately without re-hitting the login endpoint.
        Only genuine credential failures are cached; transient network errors are
        not, so a momentary outage cannot pin a permanent false lockout.
        """
        if not self.token:
            with self._auth_lock:
                if self._auth_error is not None:
                    raise self._auth_error  # cached failure — don't retry
                if not self.token:
                    try:
                        self._authenticate_inner()
                    except ACInfinityAuthError as exc:
                        self._auth_error = exc
                        raise
        token_at_start = self.token
        try:
            return fn(*args, **kwargs)
        except ACInfinityAuthError as original_auth_error:
            if not self.token:
                raise  # never authenticated; nothing to refresh
            with self._auth_lock:
                if self.token == token_at_start:
                    logger.info("Token rejected by API — refreshing")
                    try:
                        self._authenticate_inner()
                    except ACInfinityAuthError as exc:
                        # Genuine credential failure on refresh: cache it and drop the
                        # stale token so subsequent calls short-circuit via the preamble
                        # instead of re-hammering the login endpoint.
                        self._auth_error = exc
                        self.token = None
                        raise
                    except Exception:
                        # Transient failure during refresh (e.g. network): do NOT cache
                        # (no false permanent lockout) and surface the original auth
                        # rejection so callers see a consistent error type.
                        raise original_auth_error from None
            return fn(*args, **kwargs)

    def _enforce_write_rate_limit(self) -> None:
        """Enforce 1.5s minimum between write API calls (returns 403 if exceeded).

        Held under a lock so concurrent writers serialize correctly — without it,
        parallel tool calls can pass the elapsed-time check simultaneously and
        slam the API back-to-back. _last_write_time is updated under the same
        lock so concurrent waiters see the latest completion timestamp as soon
        as the prior write returns.
        """
        with self._write_lock:
            elapsed = time.monotonic() - self._last_write_time
            if elapsed < 1.5:
                time.sleep(1.5 - elapsed)
            # Provisionally mark the start time so concurrent waiters in this
            # method also serialize; the precise completion time is rewritten
            # by _mark_write_completed() once the POST returns.
            self._last_write_time = time.monotonic()

    def _mark_write_completed(self) -> None:
        """Update _last_write_time to reflect the actual write completion.

        Called immediately after the upstream POST returns (success or HTTP
        error). The pre-POST update inside _enforce_write_rate_limit() set
        the timestamp at the *start* of the call; rewriting it here ensures
        the next write's 1.5s gap is measured from the prior call's
        completion, not its start. Without this, an in-flight 500ms POST
        would leave only ~1.0s of gap before the next caller proceeded,
        risking a 403 rate-limit response from the upstream.
        """
        with self._write_lock:
            self._last_write_time = time.monotonic()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(
            (requests.exceptions.Timeout, requests.exceptions.ConnectionError)
        ),
        reraise=True,
    )
    def _authenticate_inner(self) -> None:
        """Single login attempt; retried by tenacity on transient network errors."""
        # NOTE: API parameter name has intentional typo — 'appPasswordl' with 'l' at end
        data = {
            "appEmail": self.email,
            "appPasswordl": self.password,
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "User-Agent": _LOGIN_UA,
        }

        resp = self.session.post(self.LOGIN_ENDPOINT, data=data, headers=headers, timeout=10)
        resp.raise_for_status()

        result = resp.json()
        if result.get("code") != 200:
            error_msg = result.get("msg", "Unknown error")
            logger.error("AC Infinity login failed: %s", error_msg)
            raise ACInfinityAuthError(f"Authentication failed: {error_msg}")

        self.token = result["data"]["appId"]
        logger.info("AC Infinity authentication successful")

    def authenticate(self) -> bool:
        """Login and get API token.

        Transient network errors (Timeout, ConnectionError) trigger a tenacity
        retry inside _authenticate_inner; only after exhaustion does this method
        fall back to returning False. Returns False on credential failure as well.
        """
        try:
            self._authenticate_inner()
            return True
        except requests.exceptions.Timeout:
            logger.error("AC Infinity authentication timeout (10s) after retries")
            return False
        except requests.exceptions.ConnectionError as e:
            logger.error("Failed to connect to AC Infinity after retries: %s", e)
            return False
        except ACInfinityAuthError:
            return False
        except Exception as e:
            logger.error("AC Infinity authentication error: %s", e)
            return False

    def get_devices(self) -> list[dict]:
        """Fetch all connected devices.

        Returns:
            List of raw device dicts from the AC Infinity API.

        Raises:
            ACInfinityAuthError: If not authenticated or refresh fails.
            ACInfinityAPIError: If the API returns a non-200, non-401 code.
            requests.exceptions.Timeout: After tenacity exhausts retries.
            requests.exceptions.ConnectionError: After tenacity exhausts retries.
        """
        return self._call_with_token_refresh(self._get_devices_inner)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(
            (requests.exceptions.Timeout, requests.exceptions.ConnectionError)
        ),
        reraise=True,
    )
    def _get_devices_inner(self) -> list[dict]:
        if not self.token:
            raise ACInfinityAuthError("Not authenticated — call authenticate() first")

        params = {"userId": self.token}
        headers = {
            "token": self.token,
            "Host": "www.acinfinityserver.com",
            "User-Agent": "okhttp/3.10.0",
        }

        resp = self.session.post(
            self.DEVICES_ENDPOINT, params=params, headers=headers, timeout=10
        )
        resp.raise_for_status()

        result = resp.json()
        if result.get("code") != 200:
            error_msg = result.get("msg", "Unknown error")
            code = result.get("code")
            logger.error("Failed to get devices: %s", error_msg)
            self._raise_for_api_code(code, error_msg, "Devices")

        devices = result.get("data", [])
        logger.info("Fetched %d devices", len(devices))
        return devices

    def get_historical_data(
        self,
        dev_id: str,
        start_timestamp: int,
        end_timestamp: int,
        page_size: int = 2000,
    ) -> list[dict]:
        """Fetch historical sensor data (with transparent 401 token refresh)."""
        return self._call_with_token_refresh(
            self._get_historical_data_inner,
            dev_id, start_timestamp, end_timestamp, page_size,
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(
            (requests.exceptions.Timeout, requests.exceptions.ConnectionError)
        ),
        reraise=True,
    )
    def _get_historical_data_inner(
        self,
        dev_id: str,
        start_timestamp: int,
        end_timestamp: int,
        page_size: int = 2000,
    ) -> list[dict]:
        """Fetch historical sensor data from AC Infinity cloud API.

        Uses POST /api/log/dataPage.  The API ignores the pageNum parameter
        and always returns the first page_size records starting at 'time'.
        To retrieve more records than page_size, we use time-cursor pagination:
        after each fetch the next request's 'time' is set to the last returned
        record's createTime + 1.

        Args:
            dev_id: Device ID (devId field from devInfoListAll — string or int)
            start_timestamp: Unix timestamp (seconds) for start of range
            end_timestamp: Unix timestamp (seconds) for end of range
            page_size: Records per request (default 2000; API caps at ~1257/day)

        Returns:
            List of raw history record dicts.

        Raises:
            ACInfinityAuthError: If not authenticated (token is None).
            ACInfinityAPIError: If any pagination chunk returns a non-200 code.
            requests.exceptions.Timeout: After tenacity exhausts retries.
            requests.exceptions.ConnectionError: After tenacity exhausts retries.
        """
        if not self.token:
            raise ACInfinityAuthError("Not authenticated — call authenticate() first")

        all_records: list[dict] = []
        current_start = start_timestamp
        chunk_num = 0

        while True:
            chunk_num += 1
            data = {
                "appId": self.token,
                "devId": dev_id,
                "time": current_start,
                "endTime": end_timestamp,
                "pageNum": 1,       # API ignores pageNum; always 1
                "pageSize": page_size,
            }
            headers = {
                "token": self.token,
                "Host": "www.acinfinityserver.com",
                "User-Agent": "okhttp/3.10.0",
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            }

            resp = self.session.post(
                self.HISTORY_ENDPOINT, data=data, headers=headers, timeout=30
            )
            resp.raise_for_status()

            result = resp.json()
            if result.get("code") != 200:
                error_msg = result.get("msg", "Unknown error")
                code = result.get("code")
                logger.error(
                    "History fetch failed (chunk %d): %s", chunk_num, error_msg
                )
                self._raise_for_api_code(code, error_msg, "History")

            rows = result.get("data", {}).get("rows", [])
            if not rows:
                break

            for row in rows:
                create_time = row.get("createTime", 0)
                if start_timestamp <= create_time <= end_timestamp:
                    all_records.append(row)

            if len(rows) < page_size:
                break

            # Advance time cursor past the last record's timestamp
            last_ts = rows[-1].get("createTime", 0)
            if last_ts <= current_start or last_ts >= end_timestamp:
                break
            current_start = last_ts + 1

        logger.info(
            "Fetched %d history records for devId=%s in %d chunk(s)",
            len(all_records),
            dev_id,
            chunk_num,
        )
        return all_records

    def get_mode_settings(self, dev_id: str | int, port: int) -> dict:
        """Fetch current mode settings (with transparent 401 token refresh)."""
        return self._call_with_token_refresh(
            self._get_mode_settings_inner, dev_id, port,
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(
            (requests.exceptions.Timeout, requests.exceptions.ConnectionError)
        ),
        reraise=True,
    )
    def _get_mode_settings_inner(self, dev_id: str | int, port: int) -> dict:
        """Fetch current mode settings for one port on a device.

        Required for read-before-write (Quirk 13). The port parameter is mandatory
        (Quirk 16) — the endpoint returns a single dict for that port, not a list.

        Args:
            dev_id: Numeric device ID (devId field from devInfoListAll — Quirk 7).
            port: 1-based port number.

        Returns:
            142-field dict from the API response data. Nested fields (devSetting,
            fieldSet, ipcSetting) are present but excluded by build_write_payload.

        Raises:
            ACInfinityAuthError: If not authenticated or token rejected (code 401).
            ACInfinityAPIError: If the API returns a non-200 code.
            requests.exceptions.Timeout: After tenacity exhausts retries.
            requests.exceptions.ConnectionError: After tenacity exhausts retries.
        """
        if not self.token:
            raise ACInfinityAuthError("Not authenticated — call authenticate() first")

        headers = {
            "token": self.token,
            "Host": "www.acinfinityserver.com",
            "User-Agent": "okhttp/3.10.0",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        }
        data = {"devId": dev_id, "port": port, "appId": self.token}

        resp = self.session.post(
            self.MODE_SETTINGS_ENDPOINT, data=data, headers=headers, timeout=10
        )
        resp.raise_for_status()

        result = resp.json()
        if result.get("code") != 200:
            error_msg = result.get("msg", "Unknown error")
            code = result.get("code")
            logger.error(
                "Failed to get mode settings (devId=%s port=%s): %s", dev_id, port, error_msg
            )
            self._raise_for_api_code(code, error_msg, "Mode settings")

        settings = result.get("data") or {}
        logger.debug("Fetched mode settings for devId=%s port=%s", dev_id, port)
        return settings

    def set_port_mode(
        self,
        device_data: dict,
        port: int,
        updates: dict,
        dry_run: bool = True,
        require_variable_speed: bool = False,
    ) -> dict:
        """Write port mode settings (with transparent 401 token refresh)."""
        return self._call_with_token_refresh(
            self._set_port_mode_inner,
            device_data, port, updates, dry_run, require_variable_speed,
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        # ConnectionError fires before the request reaches the server, so retry is
        # safe — the write hasn't been applied. Timeout is intentionally excluded:
        # a read timeout can mean the server already processed the write and the
        # response was lost, so retrying would risk double-applying state.
        retry=retry_if_exception_type(requests.exceptions.ConnectionError),
        reraise=True,
    )
    def _set_port_mode_inner(
        self,
        device_data: dict,
        port: int,
        updates: dict,
        dry_run: bool = True,
        require_variable_speed: bool = False,
    ) -> dict:
        """Write port mode settings using read-before-write.

        Reads current settings, merges updates, and optionally POSTs to addDevMode.
        Both legacy and AI+ controllers use the same read-before-write pattern since
        getdevModeSettingList returns the same 142-field structure for both.

        Args:
            device_data: Full device dict from get_devices() — used for controller
                type detection and devId lookup.
            port: 1-based port number.
            updates: Fields to change, e.g. {"onSpead": 5}.
            dry_run: If True (default), build and return the payload without sending.
            require_variable_speed: If True, raise ACInfinityDeviceError when the port's
                loadType indicates toggle (on/off) hardware — see _TOGGLE_LOAD_TYPES.
                Pass True from set_port_speed; leave False for set_port_on/set_port_off.

        Returns:
            Dict with keys:
                "payload": the complete dict that would be / was sent
                "dry_run": bool
                "controller_type": "legacy" or "new_framework"
                "sent": bool (True only when dry_run=False and HTTP succeeded)
                "prior_at_type": the port's atType before the write, or None

        Raises:
            ACInfinityAuthError: If not authenticated.
            ACInfinityAPIError: If the API returns a non-200 code (only when dry_run=False).
            ACInfinityDeviceError: If devId is missing from device_data, if the port is in
                smart automation mode (modeType=15), or if require_variable_speed=True and
                the port's loadType indicates on/off hardware.
        """
        if not self.token:
            raise ACInfinityAuthError("Not authenticated — call authenticate() first")

        dev_id = device_data.get("devId")
        if not dev_id:
            raise ACInfinityDeviceError("device_data missing devId field")

        controller_type = detect_controller_type(device_data)

        # Pre-write guard (Quirk 25): check isOpenAutomation from device_data (devInfoListAll)
        # BEFORE calling get_mode_settings. Legacy firmware (devType=11, firmware 3.2.56) may
        # return unreliable modeType from getdevModeSettingList while the port is under ADVANCE
        # control — the devInfoListAll port-level isOpenAutomation flag is authoritative.
        # Safe-fail: absent field treated as 0 (not active) — we fall through to the secondary
        # getdevModeSettingList check which has its own safe-fail of 1.
        port_list = (device_data.get("deviceInfo") or {}).get("ports", [])
        port_entry = next((p for p in port_list if p.get("port") == port), None)
        if port_entry is not None and port_entry.get("isOpenAutomation") == 1:
            raise ACInfinityAdvanceConflictError(
                f"Port {port} on device {dev_id} is in smart automation mode "
                "(isOpenAutomation=1 in devInfoListAll) — cannot override manually."
            )

        current_settings = self.get_mode_settings(dev_id, port)

        # Guard: smart automation mode cannot be overridden via the write API (returns 999999).
        # Absent isOpenAutomation defaults to 1 (assume active) in both branches — safe-fail.
        mode_type = current_settings.get("modeType")
        if controller_type == ControllerType.NEW_FRAMEWORK:
            # Quirk 35: on AI+, modeType is a per-port resting value carrying no ADVANCE
            # signal — a live devType-22 controller reports 0 on ports 1/3/5/7 and 15 on
            # ports 2/4/6/8 with no automation configured anywhere. Requiring modeType==15
            # here would therefore gate the guard on an unrelated field and, because that
            # same controller reports isOpenAutomation=0 on all 8 ports, the combined
            # condition could never fire at all. isOpenAutomation alone is authoritative.
            if current_settings.get("isOpenAutomation", 1) != 0:
                raise ACInfinityAdvanceConflictError(
                    f"Port {port} on device {dev_id} is under Advance Automation control "
                    "(isOpenAutomation != 0) — cannot override manually."
                )
        elif mode_type == 15 and current_settings.get("isOpenAutomation", 1) != 0:
            raise ACInfinityAdvanceConflictError(
                f"Port {port} on device {dev_id} is in smart automation mode (modeType=15) — "
                "cannot override manually."
            )

        # Guard: toggle (on/off) hardware rejects speed writes with 999999.
        # Only enforced when require_variable_speed=True (i.e. called from set_port_speed).
        # _TOGGLE_LOAD_TYPES is shared with analytics.py so the two layers cannot drift;
        # 129 and 132 were added after they were observed on live devType-22 lights and
        # heat pads. This guard has always run for every controller type — only the set
        # membership changed — so a legacy 132/129 port now raises here instead of POSTing
        # and surfacing the 999999 as a misleading ADVANCE conflict.
        load_type = current_settings.get("loadType", 0)
        if require_variable_speed and load_type in _TOGGLE_LOAD_TYPES:
            raise ACInfinityDeviceError(
                f"Port {port} is an on/off device (loadType={load_type}) — "
                "use set_port_on or set_port_off instead of set_port_speed."
            )

        payload = build_write_payload(current_settings, updates, controller_type)

        result: dict = {
            "payload": payload,
            "dry_run": dry_run,
            "controller_type": controller_type.value,
            "sent": False,
            # Deliberately an atType (1=OFF, 2=ON, 3=AUTO, 7=SCHEDULE, 8=VPD), not a
            # modeType — the server layer uses it to warn when a speed was stored on a
            # port left in OFF mode. Named prior_mode_type until #308; renamed because
            # modeType now carries real per-port state on AI+ (Quirk 35).
            "prior_at_type": current_settings.get("atType"),
        }

        if dry_run:
            logger.debug(
                "Dry run — payload built for devId=%s port=%s (%d fields)",
                dev_id, port, len(payload),
            )
            return result

        headers = {
            "token": self.token,
            "Host": "www.acinfinityserver.com",
            "User-Agent": "okhttp/3.10.0",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        }
        if controller_type == ControllerType.NEW_FRAMEWORK:
            # Quirk 14: AI+ rejects the stock okhttp header set with 100001 even
            # given a correct payload. This one header is the entire fix; the
            # ordinary merged read-before-write payload then succeeds for manual
            # control and automation targets alike.
            #
            # Ablated on live devType-20 hardware, no-op write to an idle port:
            #   okhttp headers only                     -> 100001
            #   minversion="3.5" only                   -> 200
            #   iOS UA + phoneType + appVersion, no
            #     minversion                            -> 100001
            #   minversion plus any subset of the above -> 200
            #
            # The three headers an earlier revision also sent (a spoofed iOS
            # User-Agent, phoneType, appVersion) are all droppable and are not
            # sent: each one we declare is surface for the kind of server-side
            # tightening that broke the v2 endpoints in #298.
            headers["minversion"] = _AI_PLUS_MINVERSION

        # Retry loop: 403 "Data saving failed" = rate limit; back off and retry.
        # Other error codes fail immediately (auth, field validation, etc.).
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            self._enforce_write_rate_limit()
            try:
                resp = self.session.post(
                    self.ADD_DEV_MODE_ENDPOINT, data=payload, headers=headers, timeout=10
                )
            finally:
                # Anchor the next rate-limit gap from the POST's completion
                # (or error) rather than its start (P1-F015).
                self._mark_write_completed()
            resp.raise_for_status()

            write_result = resp.json()
            if write_result.get("code") == 200:
                break

            error_msg = write_result.get("msg", "Unknown error")
            code = write_result.get("code")

            if code == 403 and "saving failed" in error_msg.lower() and attempt < max_attempts:
                logger.warning(
                    "Write rate-limit hit for devId=%s port=%s (attempt %d/%d), backing off 3s",
                    dev_id, port, attempt, max_attempts,
                )
                time.sleep(3)
                continue

            # Defense-in-depth: 999999 is the API's "conflict with active automation" code.
            # This fires only if the pre-write guard (isOpenAutomation check above) missed the
            # conflict — e.g. legacy firmware that does not populate isOpenAutomation in the
            # device list, or a race condition where automation activated between the guard and
            # the write. Raising ACInfinityAdvanceConflictError here ensures the server-layer
            # exception handler routes to _build_advance_conflict_response instead of the
            # generic ACInfinityAPIError path.
            if code == 999999:
                # Most 999999s are neither an automation conflict nor toggle
                # hardware: the port simply has nothing plugged into it.
                # Verified by no-op writes across 12 ports on two controllers —
                # every port with portResistance == 65535 (the Quirk 27
                # open-circuit sentinel) returned 999999, every connected port
                # returned 200. That held on a devType-11 controller with zero
                # Advance Automations configured, which rules the automation
                # reading out entirely, and connected ports covered by disabled
                # automations wrote fine.
                if _port_resistance(device_data, port) == _PORT_EMPTY_RESISTANCE:
                    logger.warning(
                        "Write returned 999999 for devId=%s port=%s — port reports "
                        "open circuit (nothing connected)", dev_id, port,
                    )
                    raise ACInfinityDeviceError(
                        f"Port {port} on device {dev_id} rejected the write "
                        "(code 999999) because nothing is connected to it — the "
                        "controller reports an open circuit. Plug a device in, or "
                        "pick the port your device is actually on."
                    )
                # 999999 is overloaded: it is also what toggle hardware returns when it
                # rejects a speed write. On a speed write we know which reading is right,
                # because the pre-write _TOGGLE_LOAD_TYPES guard already cleared this port
                # — so its loadType did not identify it as toggle hardware, and loadType is
                # unreliable on devType 18/22 (Quirk 24) and reports 0 on devType 20
                # (Quirk 34). Telling the grower to release an automation that does not
                # exist is a wrong answer; the hardware reading is the actionable one.
                if require_variable_speed:
                    logger.warning(
                        "Write returned code 999999 on a speed write for devId=%s port=%s "
                        "(loadType=%s) — reporting as on/off hardware, not ADVANCE conflict",
                        dev_id, port, load_type,
                    )
                    raise ACInfinityDeviceError(
                        f"Port {port} on device {dev_id} rejected the speed write "
                        f"(code 999999). This port reports loadType={load_type}, which is "
                        "not a known on/off value, but the rejection is what on/off "
                        "hardware returns for a speed write — use set_port_on or "
                        "set_port_off instead. If the port really is variable-speed, it "
                        "may instead be under Advance Automation control."
                    )
                logger.warning(
                    "Write returned code 999999 (ADVANCE conflict) for devId=%s port=%s",
                    dev_id, port,
                )
                raise ACInfinityAdvanceConflictError(
                    f"Port {port} on device {dev_id} rejected write with code 999999 — "
                    "port is under Advance Automation control."
                )

            # AI+ writes are gated on the minversion header (Quirk 14). If AC Infinity
            # ever stops honouring the value we send, every AI+ write starts returning
            # 100001 — a generic code whose default handling gives the grower nothing to
            # act on. Name the cause and point at the path that still works.
            if code == 100001 and controller_type == ControllerType.NEW_FRAMEWORK:
                logger.error(
                    "AI+ write rejected with 100001 for devId=%s port=%s — the minversion "
                    "gate may have changed server-side", dev_id, port,
                )
                raise ACInfinityDeviceError(
                    f"Device {dev_id} rejected the write (code 100001). AC Infinity gates "
                    "AI+ port writes on a header value that this client sends as "
                    f"{_AI_PLUS_MINVERSION!r}; the server may no longer accept it. Reads "
                    "and dry_run=True previews are unaffected. Please report this upstream."
                )

            logger.error("Write failed for devId=%s port=%s: %s", dev_id, port, error_msg)
            self._raise_for_api_code(code, error_msg, "Write", session_refreshable=False)
        else:  # pragma: no cover — defensive; current control flow always break/raise first
            # Defensive guard (P1-F017): the loop above must either break on
            # a 200 response or raise via _raise_for_api_code. If a future
            # refactor breaks that invariant (e.g. reorders the retry guard),
            # this else clause prevents the function from silently falling
            # through and reporting sent=True for a write that never succeeded.
            raise ACInfinityAPIError(
                f"Write loop exited without success or explicit failure for "
                f"devId={dev_id} port={port} — internal invariant violated"
            )

        logger.info("Wrote mode settings for devId=%s port=%s", dev_id, port)
        result["sent"] = True
        return result

    # ============ v2.0 Automation Management Methods ============

    def _v2_headers(self) -> dict[str, str]:
        """Build the additional headers required for v2.0 API endpoints.

        The `version` and `requestId` headers are intentionally omitted (Issue #298):
        the server now rejects any v2 request that carries either one — absent a valid
        `sign` request signature — with a misleading HTTP-200 body
        ``{"code": 403, "msg": "Login Expired Please login again!"}``, breaking the
        entire Advance-Automation surface (reads and writes). Both were sent in the
        Phase-17 network capture and accepted at the time; the server contract has since
        tightened. We do not compute `sign`, so the request authenticates on the `token`
        header alone (the same posture the legacy v1 endpoints rely on). The remaining
        app-identity headers are accepted by the server and left in place.
        """
        return {
            "token": self.token or "",
            "Host": "www.acinfinityserver.com",
            "User-Agent": "okhttp/3.10.0",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "phoneType": "1",
            "devType": "18",
            "appVersion": _V2_APP_VERSION,
            "languageType": "en-US",
            "languageVersion": "idongle_pro_3",
        }

    def get_advance_automations(self, dev_id: str) -> list[dict]:
        """Fetch all automation group entries for a device (with transparent 401 refresh).

        Returns a flat list of raw automation entries from the getGroups endpoint.
        One user-visible automation may map to multiple entries with different advId
        values (one per port-speed group) but the same advName.

        Args:
            dev_id: Numeric device ID string (devId field from devInfoListAll).

        Returns:
            List of raw automation entry dicts. Empty list if no automations.

        Raises:
            ACInfinityAuthError: If not authenticated or token refresh fails.
            ACInfinityAPIError: If the API returns a non-200, non-401 code.
        """
        return self._call_with_token_refresh(self._get_advance_automations_inner, dev_id)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(
            (requests.exceptions.Timeout, requests.exceptions.ConnectionError)
        ),
        reraise=True,
    )
    def _get_advance_automations_inner(self, dev_id: str) -> list[dict]:
        """POST /api/version=2.0/dev/getGroups — returns data array."""
        if not self.token:
            raise ACInfinityAuthError("Not authenticated — call authenticate() first")

        resp = self.session.post(
            self.V2_GET_GROUPS_ENDPOINT,
            data={"devId": dev_id},
            headers=self._v2_headers(),
            timeout=10,
        )
        resp.raise_for_status()

        result = resp.json()
        if result.get("code") != 200:
            error_msg = result.get("msg", "Unknown error")
            code = result.get("code")
            logger.error("Failed to get advance automations (devId=%s): %s", dev_id, error_msg)
            self._raise_for_api_code(code, error_msg, "GetGroups")

        data = result.get("data") or []
        logger.info("Fetched %d automation entries for devId=%s", len(data), dev_id)
        return data

    def enable_advance_automation(self, dev_id: str, adv_id: int) -> dict:
        """Toggle automation to enabled state (with transparent 401 refresh).

        IMPORTANT: updateGroupsIsOn TOGGLES the current isOn state server-side.
        The caller must verify the current state is disabled before calling this
        method, to ensure the toggle results in enabled state.

        Args:
            dev_id: Numeric device ID string.
            adv_id: Automation entry ID to toggle.

        Returns:
            API response dict.
        """
        return self._call_with_token_refresh(
            self._enable_advance_automation_inner, dev_id, adv_id
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        # ConnectionError fires before the request reaches the server — safe to retry.
        # Timeout excluded: server may have processed the write; retrying risks double-apply.
        retry=retry_if_exception_type(requests.exceptions.ConnectionError),
        reraise=True,
    )
    def _enable_advance_automation_inner(self, dev_id: str, adv_id: int) -> dict:
        """POST /api/version=2.0/dev/updateGroupsIsOn — toggles isOn state."""
        if not self.token:
            raise ACInfinityAuthError("Not authenticated — call authenticate() first")

        self._enforce_write_rate_limit()
        try:
            resp = self.session.post(
                self.V2_UPDATE_GROUPS_IS_ON_ENDPOINT,
                data={"advId": adv_id, "isDel": 0, "isflag": 1},
                headers=self._v2_headers(),
                timeout=10,
            )
        finally:
            self._mark_write_completed()
        resp.raise_for_status()

        result = resp.json()
        if result.get("code") != 200:
            error_msg = result.get("msg", "Unknown error")
            code = result.get("code")
            logger.error(
                "Failed to enable automation advId=%s (devId=%s): %s", adv_id, dev_id, error_msg
            )
            self._raise_for_api_code(
                code, error_msg, "EnableAutomation", session_refreshable=False
            )

        logger.info("Toggled automation advId=%s to enabled (devId=%s)", adv_id, dev_id)
        return result

    def disable_advance_automation(self, dev_id: str, adv_id: int) -> dict:
        """Toggle automation to disabled state (with transparent 401 refresh).

        IMPORTANT: updateGroupsIsOn TOGGLES the current isOn state server-side.
        The caller must verify the current state is enabled before calling this
        method, to ensure the toggle results in disabled state.

        Args:
            dev_id: Numeric device ID string.
            adv_id: Automation entry ID to toggle.

        Returns:
            API response dict.
        """
        return self._call_with_token_refresh(
            self._disable_advance_automation_inner, dev_id, adv_id
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.exceptions.ConnectionError),
        reraise=True,
    )
    def _disable_advance_automation_inner(self, dev_id: str, adv_id: int) -> dict:
        """POST /api/version=2.0/dev/updateGroupsIsOn — toggles isOn state (same body as enable)."""
        if not self.token:
            raise ACInfinityAuthError("Not authenticated — call authenticate() first")

        self._enforce_write_rate_limit()
        try:
            resp = self.session.post(
                self.V2_UPDATE_GROUPS_IS_ON_ENDPOINT,
                data={"advId": adv_id, "isDel": 0, "isflag": 1},
                headers=self._v2_headers(),
                timeout=10,
            )
        finally:
            self._mark_write_completed()
        resp.raise_for_status()

        result = resp.json()
        if result.get("code") != 200:
            error_msg = result.get("msg", "Unknown error")
            code = result.get("code")
            logger.error(
                "Failed to disable automation advId=%s (devId=%s): %s", adv_id, dev_id, error_msg
            )
            self._raise_for_api_code(
                code, error_msg, "DisableAutomation", session_refreshable=False
            )

        logger.info("Toggled automation advId=%s to disabled (devId=%s)", adv_id, dev_id)
        return result

    def create_advance_automation(self, dev_id: str, payload: dict) -> dict:
        """Create a new advance automation group (with transparent 401 refresh).

        Args:
            dev_id: Numeric device ID string.
            payload: Complete form payload for addGroups. Must include at minimum
                advName, devId, onSpeed. The caller is responsible for constructing
                the full ~50-field payload with safe defaults.

        Returns:
            Created automation object with server-assigned advId.
        """
        return self._call_with_token_refresh(
            self._create_advance_automation_inner, dev_id, payload
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.exceptions.ConnectionError),
        reraise=True,
    )
    def _create_advance_automation_inner(self, dev_id: str, payload: dict) -> dict:
        """POST /api/version=2.0/dev/addGroups — creates automation, returns created object."""
        if not self.token:
            raise ACInfinityAuthError("Not authenticated — call authenticate() first")

        form_data = {**payload, "devId": dev_id}

        self._enforce_write_rate_limit()
        try:
            resp = self.session.post(
                self.V2_ADD_GROUPS_ENDPOINT,
                data=form_data,
                headers=self._v2_headers(),
                timeout=10,
            )
        finally:
            self._mark_write_completed()
        resp.raise_for_status()

        result = resp.json()
        if result.get("code") != 200:
            error_msg = result.get("msg", "Unknown error")
            code = result.get("code")
            logger.error("Failed to create automation (devId=%s): %s", dev_id, error_msg)
            self._raise_for_api_code(
                code, error_msg, "CreateAutomation", session_refreshable=False
            )

        data = result.get("data") or {}
        logger.info("Created automation for devId=%s, advId=%s", dev_id, data.get("advId"))
        return data

    def update_advance_automation(self, dev_id: str, payload: dict) -> dict:
        """Edit an existing advance automation rule in place (with transparent 401 refresh).

        Args:
            dev_id: Numeric device ID string.
            payload: Complete form payload for updateGroupsById. Must include the target
                rule's ``advId`` (server edits in place by advId) plus the full rule body.
                The caller is responsible for constructing the complete payload from the
                rule's current getGroups body (read-before-write — Quirk 13).

        Returns:
            API response data dict.
        """
        return self._call_with_token_refresh(
            self._update_advance_automation_inner, dev_id, payload
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.exceptions.ConnectionError),
        reraise=True,
    )
    def _update_advance_automation_inner(self, dev_id: str, payload: dict) -> dict:
        """POST /api/version=2.0/dev/updateGroupsById — edits a rule in place by advId."""
        if not self.token:
            raise ACInfinityAuthError("Not authenticated — call authenticate() first")

        form_data = {**payload, "devId": dev_id}

        self._enforce_write_rate_limit()
        try:
            resp = self.session.post(
                self.V2_UPDATE_GROUPS_BY_ID_ENDPOINT,
                data=form_data,
                headers=self._v2_headers(),
                timeout=10,
            )
        finally:
            self._mark_write_completed()
        resp.raise_for_status()

        result = resp.json()
        if result.get("code") != 200:
            error_msg = result.get("msg", "Unknown error")
            code = result.get("code")
            logger.error("Failed to update automation (devId=%s): %s", dev_id, error_msg)
            self._raise_for_api_code(
                code, error_msg, "UpdateAutomation", session_refreshable=False
            )

        data = result.get("data") or {}
        logger.info("Updated automation for devId=%s, advId=%s", dev_id, payload.get("advId"))
        return data

    def delete_advance_automation(
        self, dev_id: str, adv_id: int, *, whole_program: bool = True
    ) -> dict:
        """Delete via delByid (with transparent 401 refresh).

        The ``isflag`` field on delByid selects the scope (verified live):
        ``isflag=1`` deletes the ENTIRE program (the whole groupNums/sortType slot —
        all its rules); ``isflag=0`` deletes only the single rule identified by ``adv_id``.

        Args:
            dev_id: Numeric device ID string.
            adv_id: Automation entry (rule) ID to delete.
            whole_program: True (default) → delete the whole program (isflag=1), used by
                the delete-whole-automation tool. False → delete only this one rule
                (isflag=0), used by delete_automation_rule on multi-rule programs.

        Returns:
            API response dict.
        """
        return self._call_with_token_refresh(
            self._delete_advance_automation_inner, dev_id, adv_id, whole_program
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.exceptions.ConnectionError),
        reraise=True,
    )
    def _delete_advance_automation_inner(
        self, dev_id: str, adv_id: int, whole_program: bool = True
    ) -> dict:
        """POST /api/version=2.0/dev/delByid — isflag=1 deletes the whole program slot,
        isflag=0 deletes only this rule."""
        if not self.token:
            raise ACInfinityAuthError("Not authenticated — call authenticate() first")

        self._enforce_write_rate_limit()
        try:
            resp = self.session.post(
                self.V2_DEL_BY_ID_ENDPOINT,
                data={"advId": adv_id, "isDel": 1, "isflag": 1 if whole_program else 0},
                headers=self._v2_headers(),
                timeout=10,
            )
        finally:
            self._mark_write_completed()
        resp.raise_for_status()

        result = resp.json()
        if result.get("code") != 200:
            error_msg = result.get("msg", "Unknown error")
            code = result.get("code")
            logger.error(
                "Failed to delete automation advId=%s (devId=%s): %s", adv_id, dev_id, error_msg
            )
            self._raise_for_api_code(
                code, error_msg, "DeleteAutomation", session_refreshable=False
            )

        logger.info("Deleted automation advId=%s (devId=%s)", adv_id, dev_id)
        return result

    def parse_device_data(self, device_data: dict, role: str | None = None) -> dict:
        """Extract readable values from AC Infinity device response.

        Type errors in the upstream response (a field arriving as a string
        where the parser expects an int, etc.) are converted to a typed
        ACInfinityAPIError so tool-level handlers log the structural issue
        clearly rather than re-raising raw TypeError text to the LLM (P3-F011).
        """
        try:
            info = device_data.get("deviceInfo", {})

            # API returns values * 100 — divide to get actual readings
            temp_c = info.get("temperature", 0) / 100.0
            temp_f = info.get("temperatureF", 0) / 100.0
            humidity = info.get("humidity", 0) / 100.0
            vpd = round(info.get("vpdnums", 0) / 100.0, 2)

            raw_ports = info.get("ports", [])
            ports = []
            for p in raw_ports:
                port_num = p.get("port")
                port_name = p.get("portName") or f"Port {port_num}"
                port_entry: dict = {
                    "port": port_num,
                    "name": port_name,
                    "speed": p.get("speak", 0),  # 0-10 scale from API
                }
                # Only flag default-named ports as "not powered" — a user-renamed port
                # implies a device was intentionally connected, and loadState=0 alone
                # can't distinguish "nothing plugged in" from "device is off".
                if (
                    not p.get("loadState", 0)
                    and not p.get("speak", 0)
                    and port_name == f"Port {port_num}"
                ):
                    port_entry["plug_status"] = "not powered"
                ports.append(port_entry)

            sensors = info.get("sensors")
            external = []
            if sensors:
                external = [
                    {
                        "sensor_id": f"{s.get('accessPort')}.{s.get('sensorType')}",
                        "sensor_type": s.get("sensorType"),
                        "sensor_type_label": _sensor_label(s.get("sensorType")),
                        "value": _sensor_value(s),
                        "unit": _sensor_unit(s.get("sensorType")),
                    }
                    for s in sensors
                    if _should_include_sensor(s)
                ]

            return {
                "timestamp": datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z",
                "device_id": device_data.get("devCode"),
                "device_name": device_data.get("devName", "Unknown"),
                "temperature_c": round(temp_c, 1),
                "temperature_f": round(temp_f, 1),
                "humidity": round(humidity, 1),
                "vpd": vpd,
                "ports": ports,
                "external_sensors": external,
                "probes": _extract_probes(sensors),
                "zone_id": device_data.get("zoneId"),            # IANA string or None
                "temp_unit_raw": info.get("unit"),               # 0=°F, 1=°C, or None
            }
        except (TypeError, ValueError, AttributeError) as e:
            logger.warning(
                "Malformed device data for devCode=%s: %s",
                device_data.get("devCode") if isinstance(device_data, dict) else "<non-dict>",
                e,
            )
            raise ACInfinityAPIError(
                "AC Infinity API returned malformed device data"
            ) from e

    def parse_history_record(
        self, record: dict, port_names: dict[int, str] | None = None
    ) -> dict:
        """Parse a historical data record from the AC Infinity API.

        The API encodes port data as bitmask integers rather than a ports array:
        - ``portSpead``: 4 bits (one nibble) per port, LSB = Port 1.
          Values 0-10 are fan/dimmer speeds; 0xF (15) means ON for
          on/off devices (lights, heaters, humidifiers, heat pads).
        - ``portStatus``: 1 bit per port, LSB = Port 1.  Indicates
          whether the port was actively triggered by automation.

        Args:
            record: Raw historical record from get_historical_data API call
            port_names: Optional mapping of port number -> name from live
                device info.  When provided the names are attached to
                each decoded port entry.

        Returns:
            Dict with parsed timestamp, temperature, humidity, VPD, and port data.

        Raises:
            ACInfinityAPIError: when the upstream record is malformed (wrong
                field types — e.g. portSpead as a string rather than int).
                Defense in depth so a poisoned response cannot surface raw
                TypeError text to the LLM via the tool-level handlers (P3-F011).
        """
        try:
            create_time = record.get("createTime", 0)
            timestamp = (
                datetime.fromtimestamp(int(create_time), UTC).replace(tzinfo=None).isoformat()
                + "Z"
                if create_time
                else None
            )

            # Decode port speeds from portSpead bitmask (4 bits per port). Quirk 6:
            # portStatus is the "automation-triggered" flag, NOT the on/off state.
            # The speed nibble alone is authoritative for on/off — a port can be
            # automation-armed (status bit set) with nibble=0 (idle), which used
            # to be reported as ON, overstating runtime in the activity report.
            port_spead = record.get("portSpead", 0) or 0
            port_status = record.get("portStatus", 0) or 0
            port_count = record.get("devPortCount") or 8

            ports = []
            for i in range(port_count):
                nibble = (port_spead >> (i * 4)) & 0xF
                on = nibble > 0
                automation_triggered = bool((port_status >> i) & 1)
                speed = 1 if nibble == 0xF else nibble  # 0xF = ON for toggle devices
                name = (port_names or {}).get(i + 1, f"Port {i + 1}")
                ports.append({
                    "port": i + 1,
                    "name": name,
                    "speed": speed,
                    "on": on,
                    "automation_triggered": automation_triggered,
                })

            return {
                "timestamp": timestamp,
                "temperature_c": round(record.get("temperature", 0) / 100.0, 1),
                "temperature_f": round(record.get("fTemperature", 0) / 100.0, 1),
                "humidity": round(record.get("humidity", 0) / 100.0, 1),
                "vpd": round(record.get("vpdNums", 0) / 100.0, 2),
                "leaf_temp_c": round(record.get("leafTemp", 0) / 10.0, 1),
                "ports": ports,
            }
        except (TypeError, ValueError, AttributeError) as e:
            logger.warning("Malformed history record: %s", e)
            raise ACInfinityAPIError(
                "AC Infinity API returned malformed history record"
            ) from e
