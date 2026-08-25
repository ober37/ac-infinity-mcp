"""Controller-type detection and write payload building.

Implements the read-before-write pattern for both legacy and AI+ controllers.
Both devType 11/18 (legacy) and devType 22 (AI+) use the same 142-field response
structure from getdevModeSettingList. The write payload is the flat scalar subset
with modeSetid stripped (Quirk 11) and modeType enforced (Quirk 12).
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class ControllerType(Enum):
    LEGACY = "legacy"          # devType 11 (69 Pro), 18 (69 Pro+)
    NEW_FRAMEWORK = "new_framework"  # devType 20+ (89 AI+)


def detect_controller_type(device_data: dict[str, Any]) -> ControllerType:
    """Detect controller type from device data.

    Legacy: devType in {11, 18} or newFrameworkDevice == False
    New framework: devType >= 20 or newFrameworkDevice == True
    """
    dev_type = device_data.get("devType", 0)
    new_framework = device_data.get("newFrameworkDevice", False)

    if new_framework or dev_type >= 20:
        return ControllerType.NEW_FRAMEWORK
    return ControllerType.LEGACY


def build_write_payload(
    current_settings: dict[str, Any],
    updates: dict[str, Any],
    controller_type: ControllerType,
) -> dict[str, Any]:
    """Build the write payload for a port mode/speed change.

    Both legacy and AI+ use read-before-write: start from the flat scalar fields
    of the getdevModeSettingList response, strip modeSetid (Quirk 11), overlay
    updates, then enforce modeType=2 when onSpead > 0 (Quirk 12).

    Non-scalar fields (devSetting dict, fieldSet list, ipcSetting) are excluded
    because the write endpoint uses form-encoding and cannot represent nested objects.

    Args:
        current_settings: Full mode settings dict from get_mode_settings() — the
            142-field response. Nested values (devSetting, fieldSet) are filtered out.
        updates: Fields to change, e.g. {"onSpead": 5}.
        controller_type: LEGACY or NEW_FRAMEWORK (same logic for both).

    Returns:
        Complete flat payload dict ready to POST to /dev/addDevMode (~138 fields).
    """
    # Keep only flat scalar values — form-encoding cannot represent dicts or lists.
    # Convert Python booleans to int (0/1) — the API form-encodes as integers, not
    # "True"/"False" strings, which cause 999999 rejections.
    payload: dict[str, Any] = {
        k: int(v) if isinstance(v, bool) else v
        for k, v in current_settings.items()
        if not isinstance(v, (dict, list))
    }

    payload.update(updates)

    # Quirk 11: modeSetid causes a 403 for legacy controllers; strip it for all types
    payload.pop("modeSetid", None)

    # Quirk 12: modeType must be 2 when onSpead > 0
    if payload.get("onSpead", 0) > 0:
        payload["modeType"] = 2

    logger.debug(
        "Built %s write payload (%d fields)",
        controller_type.value,
        len(payload),
    )
    return payload


# AI+ (devType 20+, "new framework") controllers reject the read-before-write
# merged payload above for manual control — addDevMode returns 100001. Community
# reverse-engineering via Charles Proxy capture against live UIS 89 AI+ hardware
# (github.com/keithah/homebridge-acinfinity, API_REFERENCE.md) found the AI+
# firmware instead expects every automation/threshold field zeroed, with only
# devId, externalPort, onSpead, onSelfSpead, atType and modeType carrying real
# values. Note the payload is necessary but NOT sufficient: it must be sent with
# the iOS app headers set in client._set_port_mode_inner, or the API still
# returns 100001 (verified by A/B test on live devType=20 hardware).
#
# This template must NOT be used for automation-target writes: it carries zeros
# for every threshold field, so sending it for a VPD/temperature/humidity target
# would write those zeros. A correct AI+ automation payload is not known.
_AI_PLUS_MANUAL_WRITE_TEMPLATE: dict[str, Any] = {
    "acitveTimerOff": 0, "acitveTimerOn": 0, "activeCycleOff": 0, "activeCycleOn": 0,
    "activeHh": 0, "activeHt": 0, "activeHtVpd": 0, "activeHtVpdNums": 0,
    "activeLh": 0, "activeLt": 0, "activeLtVpd": 0, "activeLtVpdNums": 0,
    "atType": 1,
    "co2FanHighSwitch": 0, "co2FanHighValue": 0, "co2LowSwitch": 0, "co2LowValue": 0,
    "devHh": 0, "devHt": 0, "devHtf": 32,
    "devLh": 0, "devLt": 0, "devLtf": 32,
    "devMacAddr": "",
    "ecOrTds": 0, "ecTdsLowSwitchEc": 0, "ecTdsLowSwitchTds": 0,
    "ecTdsLowValueEcMs": 1, "ecTdsLowValueEcUs": 0, "ecTdsLowValueTdsPpm": 0,
    "ecTdsLowValueTdsPpt": 1, "ecUnit": 0,
    "hTrend": 0, "humidity": 0,
    "isOpenAutomation": 0,
    "masterPort": 0,
    "modeType": 0,
    "moistureLowSwitch": 0, "moistureLowValue": 0,
    "offSpead": 0, "onSelfSpead": 0, "onSpead": 0, "onlyUpdateSpeed": 0,
    "phHighSwitch": 0, "phHighValue": 0, "phLowSwitch": 0, "phLowValue": 0,
    "schedEndtTime": 65535, "schedStartTime": 65535,
    "settingMode": 0, "speak": 0, "surplus": 0, "tTrend": 0,
    "targetHumi": 0, "targetHumiSwitch": 0, "targetTSwitch": 0, "targetTemp": 0,
    "targetTempF": 32, "targetVpd": 0, "targetVpdSwitch": 0,
    "tdsUnit": 0,
    "temperature": 0, "temperatureF": 0,
    "trend": 0, "unit": 0,
    "vpdSettingMode": 0,
    "waterLevelLowSwitch": 0,
    "waterTempHighSwitch": 0, "waterTempHighValue": 0, "waterTempHighValueF": 32,
    "waterTempLowSwitch": 0, "waterTempLowValue": 0, "waterTempLowValueF": 32,
}


def build_ai_plus_manual_write_payload(
    dev_id: str, port: int, updates: dict[str, Any]
) -> dict[str, Any]:
    """Build the payload for a manual on/off/speed change on an AI+ controller.

    See ``_AI_PLUS_MANUAL_WRITE_TEMPLATE`` for why this differs from
    ``build_write_payload`` and why it is manual-control only.

    Both ``modeType`` and ``atType`` are derived from the final ``onSpead``
    rather than taken from ``updates`` or the template default. Deriving atType
    matters: ``set_port_speed`` sends ``{"onSpead": speed}`` with no atType, so
    leaving the template's default (1 = OFF) produced atType=OFF alongside
    modeType=ON — a self-contradictory payload that puts the port in OFF mode
    while the call reports success, silently stopping a running fan.
    """
    payload = dict(_AI_PLUS_MANUAL_WRITE_TEMPLATE)
    payload["devId"] = dev_id
    payload["externalPort"] = port
    payload.update(updates)
    on_spead = int(payload.get("onSpead", 0) or 0)
    payload["modeType"] = 2 if on_spead > 0 else 0
    payload["atType"] = 2 if on_spead > 0 else 1
    if "onSelfSpead" not in updates:
        payload["onSelfSpead"] = on_spead
    return payload
