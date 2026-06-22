"""Automation helpers: grouping, conflict detection, and conflict response building.

Pure functions except for _build_advance_conflict_response (async, calls the API).
All data comes from client.py responses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import unicodedata

from ac_infinity_mcp.analytics import _ZERO_LOAD_DEV_TYPES
from ac_infinity_mcp.client import ACInfinityClient
from ac_infinity_mcp.schema import _AUTH_ERROR_MSG, ACInfinityAuthError

logger = logging.getLogger(__name__)


def _sanitize_api_string(value: str | None, max_len: int = 64) -> str:
    """Strip Unicode control/format characters, truncate to max_len codepoints.

    Preserves non-ASCII printable characters (Japanese, Korean, Chinese) — the
    AC Infinity app supports non-English names. Strips only Cc (control) and Cf
    (format) Unicode categories. Empty result after stripping returns "(unnamed)".
    """
    if not value:
        return "(unnamed)"
    cleaned = "".join(
        ch for ch in value if unicodedata.category(ch) not in ("Cc", "Cf")
    )
    cleaned = cleaned[:max_len]
    return cleaned if cleaned else "(unnamed)"


def _group_automations(raw_entries: list[dict]) -> list[dict]:
    """Group flat getGroups entries by advName into user-visible automations.

    One user-visible automation = multiple entries sharing the same advName
    (one per port-speed group). The first entry's advId is the canonical ID
    used for enable/disable/delete operations (the API toggles all same-name
    entries together when called on any one of them).

    Returns a list of grouped automation dicts.
    """
    # Preserve insertion order so the list is stable across calls.
    groups: dict[str, list[dict]] = {}
    for entry in raw_entries:
        name = entry.get("advName") or ""
        groups.setdefault(name, []).append(entry)

    result = []
    for name, entries in groups.items():
        clean_name = _sanitize_api_string(name, 64)
        result.append({
            "automation_id": entries[0].get("advId"),
            "name": clean_name,
            "enabled": bool(entries[0].get("isOn", 0)),
            "adv_ids": [e.get("advId") for e in entries if e.get("advId") is not None],
            "port_groups": [
                {
                    "adv_id": e.get("advId"),
                    "on_speed": e.get("onSpeed", 0),
                    "grp_dev_type": e.get("grouptDevType", 0),
                }
                for e in entries
            ],
            "run_state": bool(entries[0].get("runState", 0)),
            "begin_time": entries[0].get("beginTime"),
            "end_time": entries[0].get("endTime"),
            "on_time_switch": entries[0].get("onTimeSwitch", 0),
        })
    return result


def _find_governing_automation(automations: list[dict], port: int) -> dict | None:
    """Return the first enabled/running automation whose bitmask covers ``port``, or None.

    Uses the ``grp_dev_type`` bitmask stored in each port_group entry by
    ``_group_automations``.  Port N maps to bit (N-1): a bitmask of 8 (0b1000)
    covers Port 4.  Only automations with ``enabled=True`` or ``run_state=True``
    are considered.
    """
    for auto in automations:
        if not (auto.get("enabled") or auto.get("run_state")):
            continue
        for pg in auto.get("port_groups", []):
            bitmask = int(pg.get("grp_dev_type") or 0)
            if bitmask & (1 << (port - 1)):
                return auto
    return None


def _find_governing_port_group(automation: dict, port: int) -> dict | None:
    """Return the port_group entry whose bitmask covers ``port``, or None.

    Iterates ``automation["port_groups"]`` and returns the first entry where
    ``grp_dev_type`` has the bit for ``port`` set.
    """
    for pg in automation.get("port_groups", []):
        bitmask = int(pg.get("grp_dev_type") or 0)
        if bitmask & (1 << (port - 1)):
            return pg
    return None


def _is_port_not_powered(port_data: dict | None, device: dict | None) -> bool:
    """Return True when a port is not currently drawing power on a legacy device.

    Fires for both custom-named and default-named ports.  Unlike ``_is_port_empty``,
    this helper does NOT skip custom-named ports — a named port can still be off.

    Returns False for devTypes 18 and 22 (``_ZERO_LOAD_DEV_TYPES``) because those
    controllers always report ``portsLoad=0`` regardless of actual state; the signal
    is meaningless there.  Returns False when either arg is None.
    """
    if port_data is None or device is None:
        return False
    if device.get("devType") in _ZERO_LOAD_DEV_TYPES:
        return False
    return (port_data.get("portsLoad") or 0) == 0


async def _build_advance_conflict_response(
    client: ACInfinityClient,
    device_id: str, dev_id: object, port: int, port_name: str,
    *, device: dict | None = None, requested_speed: int | None = None,
) -> str:
    """Build a structured ADVANCE_AUTOMATION conflict response for write tools.

    Six outcomes depending on the secondary automation lookup result:

    - **Auth-error path** (secondary lookup raises ``ACInfinityAuthError``): returns
      auth error JSON immediately; credential expiry must be resolved before conflict UX.
    - **Sub-path A — port in bitmask** (governing automation found whose bitmask covers
      the requested port): option key ``"1_break_out"`` pointing to
      ``break_out_of_automation``; option key ``"2_disable_automation"`` pointing to
      ``disable_advance_automation``.  Speed is read from the matched port_group.
      ``suggested_reply`` discloses that releasing affects ALL ports on the automation.
    - **Sub-path B — port not in bitmask** (active automations exist but none has a
      bitmask covering the requested port — controller-wide lock): controller-wide lock
      message language; ``"1_break_out"`` is NOT offered because the port is not
      explicitly governed by any automation's port group.
    - **All-disabled path** (API succeeded, automations non-empty, none active):
      option key ``"1_re_disable_to_clear"`` pointing to ``disable_advance_automation``.
      ``suggested_reply`` explains the port is stuck and offers force-release.
    - **Degraded path** (API call failed or automation list empty):
      option key ``"1_find_and_disable"`` pointing to ``list_advance_automations``.
      ``suggested_reply`` avoids exposing tool names — conversational only.

    Args:
        client: The ACInfinityClient instance to use for the automation lookup.
        device_id: Human-readable device code (e.g. ``"C58ZA"``).
        dev_id: Numeric device ID for the automation lookup API call.
        port: 1-based port number.
        port_name: Human-readable port name (e.g. ``"Filter"``).
        device: The full device dict from the device-lookup call in the caller.
            When provided and the port is not drawing power (``portsLoad == 0``),
            a "not currently powered" note is appended to ``suggested_reply`` and
            ``human_summary`` in Sub-path A only.  Ignored for all other sub-paths.
        requested_speed: The speed the caller tried to set (from set_port_speed).
            When not None, adds a ``"0_update_speed"`` option in the normal path.
            Pass ``None`` from set_port_on / set_port_off (no speed option applies).
    """
    api_call_failed = False
    automations: list[dict] = []
    active_automations: list[dict] = []
    governing = None
    try:
        raw = await asyncio.to_thread(client.get_advance_automations, str(dev_id))
        automations = _group_automations(raw)
        governing = _find_governing_automation(automations, port)
        active_automations = [
            {"name": a["name"], "automation_id": a["automation_id"]}
            for a in automations if a.get("enabled") or a.get("run_state")
        ]
    except ACInfinityAuthError:
        logger.warning(
            "Auth error in _build_advance_conflict_response (device=%s)", device_id
        )
        return json.dumps({
            "error": _AUTH_ERROR_MSG,
            "detail": "see server logs",
        })
    except Exception as exc:
        logger.warning(
            "Could not fetch automations for conflict response (device=%s): %s",
            device_id,
            type(exc).__name__,
        )
        api_call_failed = True

    has_active = any(a.get("enabled") or a.get("run_state") for a in automations)

    port_display = f"{port_name} (Port {port})" if port_name != f"Port {port}" else port_name

    if governing is not None:
        # SUB-PATH A — an enabled/running automation whose bitmask covers this port
        auto_name = governing["name"]
        auto_id = governing["automation_id"]
        governing_pg = _find_governing_port_group(governing, port)
        current_auto_speed = governing_pg.get("on_speed") if governing_pg is not None else "?"
        summary = (
            f"While '{auto_name}' automation is running, all ports on this controller"
            " are locked from manual control."
            " Your change requires resolving this conflict first."
        )
        human_summary = (
            f"'{auto_name}' is actively controlling this port at target speed {current_auto_speed}."
            " To make manual adjustments, you need to resolve this automation conflict first."
        )
        if requested_speed is not None:
            suggested_reply = (
                f"'{auto_name}' automation is controlling this port right now"
                f" (target speed: {current_auto_speed})."
                f" The easiest fix is to update the automation to run at speed {requested_speed}"
                " instead — the automation stays active, just at the new speed."
                f" Alternatively, I can release {port_display} from the automation"
                f" so you can control it manually — but that will also release all other ports"
                f" currently on '{auto_name}'."
                " What would you prefer?"
            )
        else:
            suggested_reply = (
                f"'{auto_name}' automation is controlling this port right now"
                f" (target speed: {current_auto_speed}). I can release this port from the"
                f" automation — but note this will also release all other ports currently on"
                f" '{auto_name}'. Alternatively, I could update the automation's speed settings"
                " instead. What would you prefer?"
            )
        opt1: dict = {
            "description": (
                f"Release {port_display} from '{auto_name}' to regain manual control."
            ),
            "_tool": "break_out_of_automation",
            "instruction": (
                f"Ask me to release {port_display} from the '{auto_name}'"
                " automation so you can control it manually."
            ),
            "available": governing.get("enabled", False) or governing.get("run_state", False),
        }
        opt2: dict = {
            "description": (
                f"Disable '{auto_name}' entirely — releases all ports on this automation."
            ),
            "_tool": "disable_advance_automation",
            "instruction": (
                f"Ask me to disable the '{auto_name}' automation to release all ports"
                " on this controller from automation control."
            ),
            "available": True,
        }
        opt1_key = "1_break_out"

        # Option 0 — only when the caller provided a target speed (set_port_speed path).
        # set_port_on / set_port_off pass requested_speed=None → no speed option.
        options_dict: dict = {}
        if requested_speed is not None:
            options_dict["0_update_speed"] = {
                "description": (
                    f"Change the '{auto_name}' automation's target speed from"
                    f" {current_auto_speed} to {requested_speed},"
                    " keeping the automation active."
                ),
                "instruction": (
                    f"Ask me to update the '{auto_name}' automation to run at"
                    f" speed {requested_speed} instead."
                ),
                "available": True,
            }
        options_dict[opt1_key] = opt1
        options_dict["2_disable_automation"] = opt2
        options_dict["3_fork_automation"] = {
            "available": False,
            "status": "not_yet_implemented",
        }

        # Append "not powered" note when the port is not drawing power (Sub-path A only).
        ports_list = (device or {}).get("deviceInfo", {}).get("ports", [])
        port_data_local = next((p for p in ports_list if p.get("port") == port), None)
        if _is_port_not_powered(port_data_local, device):
            power_note_speed = (
                f" Note: {port_display} is not currently drawing power"
                " — verify it is plugged in and switched on before making speed changes."
            )
            power_note_nospeed = (
                f" Note: {port_display} is not currently drawing power"
                " — verify it is plugged in and switched on before proceeding."
            )
            human_summary += f" Note: {port_display} is not currently drawing power."
            if requested_speed is not None:
                suggested_reply = (
                    suggested_reply.removesuffix(" What would you prefer?")
                    + power_note_speed
                    + " What would you prefer?"
                )
            else:
                suggested_reply = suggested_reply.replace(
                    " Alternatively,", power_note_nospeed + " Alternatively,", 1
                )

    elif not api_call_failed and has_active:
        # SUB-PATH B — active automations exist, but none has a bitmask covering this port.
        # The controller is locked at the API level; this port is not in any automation's
        # port group, so break_out_of_automation is not applicable.
        auto_name = None
        auto_id = None
        _b_name = active_automations[0]["name"] if active_automations else "an active automation"
        summary = (
            f"The '{_b_name}' automation is locking this controller from manual control."
            " Your change requires resolving this conflict first."
        )
        human_summary = (
            f"The '{_b_name}' ADVANCE automation is locking this controller."
            " Manual control of all ports is blocked until the automation is paused."
        )
        suggested_reply = (
            f"The '{_b_name}' automation has locked this controller, preventing manual port"
            " changes. I can disable it to release the lock. Want me to do that?"
        )
        opt1 = {
            "description": "Disable the active automation to release this controller.",
            "_tool": "disable_advance_automation",
            "instruction": (
                f"Ask me to list your automations for this controller to identify '{_b_name}',"
                " then ask me to disable it to release the controller lock."
            ),
            "available": True,
        }
        opt2 = {
            "available": False,
            "status": (
                "This port is not directly controlled by any active automation — use option 1 to"
                " disable the automation locking the controller."
            ),
        }
        opt1_key = "1_disable_automation"
        options_dict = {
            opt1_key: opt1,
            "2_disable_automation": opt2,
            "3_fork_automation": {
                "available": False,
                "status": "not_yet_implemented",
            },
        }
    elif not api_call_failed and len(automations) > 0:
        # ALL-DISABLED PATH — API succeeded but all automations have enabled=False / run_state=False
        auto_name = None
        auto_id = None
        summary = (
            "An Advance Automation is blocking this port. All configured automations are"
            " currently disabled, but the port hasn't fully released from automation mode."
        )
        human_summary = (
            "This port is in automation mode, but all automations are disabled."
            " The port hasn't fully released. Ask me to list your automations for details."
        )
        suggested_reply = (
            "Your automations for this port are all turned off, but the port is still stuck"
            " in automation mode — it hasn't fully released. I can force-release it by"
            " re-applying the disable command. Want me to do that?"
        )
        opt1 = {
            "description": "Force-release this port by re-applying the disable command.",
            "_tool": "disable_advance_automation",
            "instruction": (
                "Ask me to list your automations so I can find the one holding this port,"
                " then ask me to disable it to force-release the port."
            ),
            "available": True,
        }
        opt2 = {
            "available": False,
            "status": "All automations already disabled — use option 1 to force-release the port.",
        }
        opt1_key = "1_re_disable_to_clear"
        options_dict = {
            opt1_key: opt1,
            "2_disable_automation": opt2,
            "3_fork_automation": {
                "available": False,
                "status": "not_yet_implemented",
            },
        }
    else:
        # DEGRADED PATH — API call failed OR automation list is empty
        auto_name = None
        auto_id = None
        summary = (
            "An Advance Automation is running on this controller, locking all ports from"
            " manual control. Your change requires resolving this conflict first."
        )
        human_summary = (
            "An active automation is blocking manual port control on this controller."
            " Ask me to list your automations to see what's set up."
        )
        suggested_reply = (
            "An active automation is blocking this port."
            " Let me look up the active automations to resolve this — shall I get started?"
        )
        opt1 = {
            "description": "Find and disable the active automation, then apply your manual change.",
            "_tool": "list_advance_automations",
            "instruction": (
                "Ask me to list your automations for this controller so I can identify"
                " which one is active, then ask me to disable it."
            ),
            "available": True,
        }
        opt2 = {
            "available": False,
            "status": "Use option 1 first to identify the automation.",
        }
        opt1_key = "1_find_and_disable"
        options_dict = {
            opt1_key: opt1,
            "2_disable_automation": opt2,
            "3_fork_automation": {
                "available": False,
                "status": "not_yet_implemented",
            },
        }

    return json.dumps({
        "conflict": "ADVANCE_AUTOMATION",
        "summary": summary,
        "human_summary": human_summary,
        "suggested_reply": suggested_reply,
        "target_port": port_display,
        "automation_name": auto_name,
        "automation_id": auto_id,
        "active_automations": active_automations,
        "co_governed_ports": [],
        "switching_guidance": (
            "To regain manual control: ask me to disable any active automations on this"
            " controller, then apply your change. To add this port to an automation instead,"
            " ask me to create or update an automation."
        ),
        "options": options_dict,
    }, indent=2)
