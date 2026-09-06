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


# Groups (Advance Automation) `currentMode` codes, keyed by controller class.
#
# The two classes use DIFFERENT numbering for the same five modes, and the overlap is
# actively dangerous: value 6 is VPD on legacy and CYCLE on new-framework, and 1/2 are
# on/off INVERTED between them. A single table cannot serve both — see Issues #326
# (writes: `mode="off"` energized a grow light) and #328 (reads: every new-framework
# automation decoded wrong).
#
# Every value below is observed on live hardware, none inferred:
#   legacy 11/18 — 1 on, 3 cycle, 4 auto, 6 vpd across 31 app-created rules whose
#     payloads are internally consistent (cycle rules carry cycle timings, auto rules
#     carry temperature triggers, VPD rules carry VPD targets); 2 off confirmed
#     2026-09-05 by an app-created Off Mode rule on a devType 11.
#   new-framework 20/22 — 2 on and 6 cycle from app-created rules on a devType 22;
#     1 off and 2 on write-tested on a devType 20; 3 auto and 8 vpd from app-created
#     rules on a devType 20.
#
# New-framework numbering matches the legacy per-port `atType` enum. That is a
# coincidence of the firmware, not a shared definition — do not merge the two.
_GROUPS_MODE_CODES: dict[ControllerType, dict[str, int]] = {
    ControllerType.LEGACY: {"on": 1, "off": 2, "cycle": 3, "auto": 4, "vpd": 6},
    ControllerType.NEW_FRAMEWORK: {"on": 2, "off": 1, "cycle": 6, "auto": 3, "vpd": 8},
}

# Reverse maps are DERIVED, never hand-written. Two hand-maintained twin tables is
# exactly how #326/#328 arose.
_GROUPS_MODE_NAMES: dict[ControllerType, dict[int, str]] = {
    ctype: {code: name for name, code in modes.items()}
    for ctype, modes in _GROUPS_MODE_CODES.items()
}


def groups_mode_code(controller_type: ControllerType, mode: str) -> int:
    """Return the Groups `currentMode` wire value for ``mode`` on this controller class.

    Raises:
        ValueError: on a mode this class cannot express. Unreachable from the tool
            surface — `_validate_rule_inputs` rejects unknown modes first — but the
            encoder must never fall through to a default that energizes a port.
    """
    try:
        return _GROUPS_MODE_CODES[controller_type][mode]
    except KeyError:  # pragma: no cover — guarded upstream by _validate_rule_inputs
        raise ValueError(
            f"no Groups currentMode for mode={mode!r} on {controller_type.value}"
        ) from None


def groups_mode_name(controller_type: ControllerType, code: object) -> str | None:
    """Return the mode name for a Groups `currentMode` wire value, or None if unknown.

    None means "this class does not define that code" — the caller renders it as an
    unrecognised rule rather than guessing.

    Matching is strict: only a real `int` resolves. The API sends this field as an integer,
    and pre-#328 the decoder compared it with `==` against int literals — so a *string*
    already read as unrecognised, and this keeps that. It also tightens two cases that the
    old `==` accepted by accident: `2.0 == 2` and `True == 1` both matched, so a float or a
    bool used to decode as a real mode. Failing closed is the right bias on the field that
    decides whether a grower is told their equipment is running.
    """
    if type(code) is not int:
        return None
    return _GROUPS_MODE_NAMES[controller_type].get(code)


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
