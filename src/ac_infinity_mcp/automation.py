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


# Rail sentinels (Issue #284): a value AT its rail means "inactive" even when the paired
# switch is 1. The app parks the unused unit/family at its rail. Mirrors client.py rails.
_RAIL_TEMP_HIGH_F = 194
_RAIL_TEMP_LOW_F = 32
_RAIL_HUMI_HIGH = 100
_RAIL_HUMI_LOW = 0
_RAIL_VPD_HIGH = 99
_RAIL_VPD_LOW = 0
_RAIL_TARGET_TEMP_F = 32
_RAIL_TARGET_HUMI = 0
_RAIL_TARGET_VPD = 0

# switchTime bit 7 (128) = continuous flag; bits 0–6 = days (bit0=Mon … bit6=Sun).
_SWITCHTIME_CONTINUOUS_BIT = 0x80
_SWITCHTIME_ALL_DAYS = 127
_SWITCHTIME_WEEKDAYS = 31  # Mon–Fri
_DAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _fmt_hhmm(minutes: int | None) -> str:
    """Format minutes-since-midnight as HH:MM (clamped to a valid clock value)."""
    m = int(minutes or 0) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def _decode_schedule(entry: dict) -> str | None:
    """Return a human-readable schedule modifier, or None when no window applies.

    ``switchTime`` bit 7 set (e.g. 255) → "runs continuously" (window suppressed). Else
    decode the day bitmask (127→"every day", 31→"Mon–Fri", otherwise named days) and append
    the HH:MM–HH:MM window.
    """
    switch_time = int(entry.get("switchTime") or 0)
    if switch_time & _SWITCHTIME_CONTINUOUS_BIT:
        return "runs continuously"

    begin = entry.get("beginTime")
    end = entry.get("endTime")
    days_mask = switch_time & 0x7F
    if days_mask == _SWITCHTIME_ALL_DAYS:
        day_str = "every day"
    elif days_mask == _SWITCHTIME_WEEKDAYS:
        day_str = "Mon–Fri"
    elif days_mask == 0:
        day_str = None
    else:
        day_str = ", ".join(_DAY_ABBR[i] for i in range(7) if days_mask & (1 << i))

    if begin is None and end is None and day_str is None:
        return None
    window = f"{_fmt_hhmm(begin)}–{_fmt_hhmm(end)}"
    return f"{day_str} {window}" if day_str else window


def _decode_modifiers(entry: dict) -> list[str]:
    """Return speed-range + buffer/transition modifier phrases for the control string."""
    mods: list[str] = []
    min_level = int(entry.get("offSpeed") or 0)
    max_level = int(entry.get("onSpeed") or 0)
    if entry.get("currentMode") == 1:
        # On mode runs at a single speed (the port's own min is used when inactive); there
        # is no user-settable min, so render just the active speed, not a range.
        mods.append(f"speed {max_level}")
    else:
        min_render = "0 (off)" if min_level == 0 else str(min_level)
        mods.append(f"speed {min_render}–{max_level}")

    # Coerce each value once (the API returns ints, but stay defensive against a
    # string-valued field so a display modifier can never raise — _decode_modifiers
    # runs for every rule via _group_automations, which feeds legacy conflict detection).
    temp_buff = int(entry.get("temperatureFBuff") or 0)
    temp_trans = int(entry.get("temperatureFTrans") or 0)
    humi_buff = int(entry.get("humidityBuff") or 0)
    humi_trans = int(entry.get("humidityTrans") or 0)
    vpd_buff = int(entry.get("vpdBuff") or 0)
    vpd_trans = int(entry.get("vpdTrans") or 0)
    if temp_buff > 0:
        mods.append(f"temperature buffer {temp_buff}°F")
    if temp_trans > 0:
        mods.append(f"temperature transition {temp_trans}°F")
    if humi_buff > 0:
        mods.append(f"humidity buffer {humi_buff}%")
    if humi_trans > 0:
        mods.append(f"humidity transition {humi_trans}%")
    if vpd_buff > 0:
        mods.append(f"VPD buffer {vpd_buff / 10:g} kPa")
    if vpd_trans > 0:
        mods.append(f"VPD transition {vpd_trans / 10:g} kPa")
    return mods


def _decode_auto_clauses(entry: dict) -> tuple[list[str], str | None]:
    """Collect sensor-labeled clauses for a currentMode=4 (Auto) rule. Returns (clauses, dir).

    Target sub-mode (settingMode==1): collect every non-rail target. Trigger sub-mode:
    collect every active threshold (switch==1 AND value non-rail). ``direction`` is reported
    for a single-sensor trigger (back-compat with the trigger round-trip tests).
    """
    clauses: list[str] = []
    direction: str | None = None

    if entry.get("settingMode") == 1:
        temp_t = entry.get("targetTempF")
        if temp_t is not None and int(temp_t) != _RAIL_TARGET_TEMP_F:
            clauses.append(f"temperature: hold at {int(temp_t)}°F")
        humi_t = entry.get("targetHumi")
        if humi_t is not None and int(humi_t) != _RAIL_TARGET_HUMI:
            clauses.append(f"humidity: hold at {int(humi_t)}%")
        return clauses, direction

    # Trigger sub-mode.
    temp_high = (
        entry.get("autoHighTempSwitch") == 1
        and int(entry.get("autoHighTempF") or 0) < _RAIL_TEMP_HIGH_F
    )
    temp_low = (
        entry.get("autoLowTempSwitch") == 1
        and int(entry.get("autoLowTempF") or 0) > _RAIL_TEMP_LOW_F
    )
    if temp_high or temp_low:
        clause, direction = _trigger_clause(
            "temperature", "°F",
            high=temp_high, low=temp_low,
            high_value=int(entry.get("autoHighTempF") or 0),
            low_value=int(entry.get("autoLowTempF") or 0),
        )
        clauses.append(clause)

    humi_high = (
        entry.get("autoHighHumiSwitch") == 1
        and int(entry.get("autoHighHumi") or 0) < _RAIL_HUMI_HIGH
    )
    humi_low = (
        entry.get("autoLowHumiSwitch") == 1
        and int(entry.get("autoLowHumi") or 0) > _RAIL_HUMI_LOW
    )
    if humi_high or humi_low:
        clause, humi_dir = _trigger_clause(
            "humidity", "%",
            high=humi_high, low=humi_low,
            high_value=int(entry.get("autoHighHumi") or 0),
            low_value=int(entry.get("autoLowHumi") or 0),
        )
        clauses.append(clause)
        # Single-sensor trigger: surface the direction; multi-sensor leaves it None.
        direction = humi_dir if not (temp_high or temp_low) else None

    return clauses, direction


def _trigger_clause(
    sensor: str, unit: str, *, high: bool, low: bool, high_value: int, low_value: int
) -> tuple[str, str]:
    """Build one sensor-labeled trigger clause + its direction."""
    if high and low:
        return (
            f"{sensor}: on above {high_value}{unit} or below {low_value}{unit}",
            "both",
        )
    if high:
        return f"{sensor}: on above {high_value}{unit}", "on_above"
    return f"{sensor}: on below {low_value}{unit}", "on_below"


def _decode_vpd_clauses(entry: dict) -> tuple[list[str], str | None]:
    """Collect VPD clauses for a currentMode=6 rule. Returns (clauses, direction)."""
    if entry.get("settingMode") == 1:
        kpa = int(entry.get("targetVpd") or 0) / 10
        return [f"VPD: hold at {kpa:g} kPa"], None

    high = (
        entry.get("highVpdSwitch") == 1
        and int(entry.get("highVpd") or 0) < _RAIL_VPD_HIGH
    )
    low = (
        entry.get("lowVpdSwitch") == 1
        and int(entry.get("lowVpd") or 0) > _RAIL_VPD_LOW
    )
    high_kpa = int(entry.get("highVpd") or 0) / 10
    low_kpa = int(entry.get("lowVpd") or 0) / 10
    if high and low:
        return [f"VPD: on above {high_kpa:g} or below {low_kpa:g} kPa"], "both"
    if high:
        return [f"VPD: on above {high_kpa:g} kPa"], "on_above"
    if low:
        return [f"VPD: on below {low_kpa:g} kPa"], "on_below"
    return [], None


def _decode_rule(entry: dict) -> dict:
    """Decode a raw getGroups entry into a grower-readable rule description.

    Returns ``{"mode": <off|on|cycle|auto|vpd>, "control": <plain string>,
    "direction": <on_below|on_above|both|None>}``. This is the single source of truth for
    read-back and the exact mirror of ``build_groups_payload``'s encoder.

    Additive multi-clause: for Auto/VPD every active (non-rail) target or trigger across all
    sensors is collected into a sensor-labeled clause, joined by "; ", then speed-range and
    schedule and buffer/transition modifiers are appended. A value AT its rail is NEVER a
    clause, so a Target rule's rail-parked triggers (switches=1) are correctly ignored.
    """
    # Groups (Advance Automation) currentMode: Off/On/Auto/VPD/Cycle = 2/1/4/6/3. This is a
    # DIFFERENT enum from the legacy per-port `atType` (getdevModeSettingList): atType OFF=1,
    # ON=2, AUTO=3, TIMER=4/5, CYCLE=6, SCHEDULE=7, VPD=8. Do not conflate the two. Any
    # currentMode not in {1,2,3,4,6} decodes gracefully to "unknown" (no KeyError/crash) so a
    # future firmware value (e.g. 5 or 7) can't break read-back.
    current_mode = entry.get("currentMode")

    if current_mode == 2:
        return {"mode": "off", "control": "off", "direction": None}

    if current_mode == 1:
        clauses: list[str] = ["runs at set speed"]
        direction: str | None = None
        mode = "on"
    elif current_mode == 3:
        # cycleOn/cycleOff are stored in SECONDS; the app shows minutes = seconds/60.
        on_min = int(entry.get("cycleOn") or 0) // 60
        off_min = int(entry.get("cycleOff") or 0) // 60
        clauses = [f"cycle {on_min} min on / {off_min} min off"]
        direction = None
        mode = "cycle"
    elif current_mode == 4:
        clauses, direction = _decode_auto_clauses(entry)
        if not clauses:
            clauses = ["auto (no rule set)"]
        mode = "auto"
    elif current_mode == 6:
        clauses, direction = _decode_vpd_clauses(entry)
        if not clauses:
            clauses = ["VPD (no rule set)"]
        mode = "vpd"
    else:
        return {"mode": "unknown", "control": "unrecognized rule", "direction": None}

    parts = list(clauses)
    parts.extend(_decode_modifiers(entry))
    schedule = _decode_schedule(entry)
    if schedule:
        parts.append(schedule)
    return {"mode": mode, "control": "; ".join(parts), "direction": direction}


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
                    # Per-rule fields (additive — program-level keys read by the 4
                    # consumers stay above; these are for the per-rule read/CRUD path).
                    "begin_time": e.get("beginTime"),
                    "end_time": e.get("endTime"),
                    "switch_time": e.get("switchTime"),
                    "run_state": bool(e.get("runState", 0)),
                    "current_mode": e.get("currentMode"),
                    "rule": _decode_rule(e),
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

    Returns False for devTypes 18, 20 and 22 (``_ZERO_LOAD_DEV_TYPES``) because those
    controllers report ``portsLoad`` as 0/None regardless of actual state; the signal
    is meaningless there.  devType 20 reports ``None`` on every port, including ones
    with a running load, so ``(portsLoad or 0) == 0`` would otherwise be true for all
    of them.  Returns False when either arg is None.
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
            " Changing its speed by hand repeatedly can throw off the pattern the controller is"
            " learning for your grow, so I've left it on automation. To make manual adjustments,"
            " you need to resolve this automation conflict first."
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
