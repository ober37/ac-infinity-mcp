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
