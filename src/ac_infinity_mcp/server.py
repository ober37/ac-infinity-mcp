import asyncio
import calendar
import copy
import dataclasses
import json
import logging
import os
import re
import sys
import time
import weakref
from datetime import UTC, datetime, timedelta

from mcp.server.fastmcp import FastMCP

from ac_infinity_mcp.analytics import (
    _ZERO_LOAD_DEV_TYPES,
    STAGE_TARGETS,
    ActivityReport,
    _filter_readings_by_time,  # noqa: F401 — re-exported for test compatibility
    _parse_duration_seconds,  # noqa: F401 — re-exported for test compatibility
    apply_sampling,  # noqa: F401 — re-exported for test compatibility
    average_readings,  # noqa: F401 — re-exported for test compatibility
    build_activity_report,
    calculate_health_score,
    detect_trends,
)  # noqa: E402 (ruff isort: private names _ZERO_LOAD_DEV_TYPES/_filter_…/_parse_… sorted before public)
from ac_infinity_mcp.automation import (
    _RAIL_HUMI_HIGH,
    _RAIL_HUMI_LOW,
    _RAIL_TARGET_HUMI,
    _RAIL_TEMP_HIGH_F,
    _RAIL_TEMP_LOW_F,
    _RAIL_VPD_HIGH,
    _RAIL_VPD_LOW,
    _build_advance_conflict_response,
    _decode_rule,  # noqa: F401 — re-exported for test compatibility
    _find_governing_automation,
    _find_governing_port_group,
    _group_automations,
    _is_port_not_powered,  # noqa: F401 — re-exported for test compatibility
    _sanitize_api_string,
)
from ac_infinity_mcp.client import (
    ACInfinityClient,
    build_groups_payload,
    resolve_port_type,
)
from ac_infinity_mcp.controller import ControllerType, detect_controller_type
from ac_infinity_mcp.formatting import (
    _effective_tz,
    _effective_unit,
    _format_probes,
    _format_window_dt,
    _short_date,
    _to_preferred_temp,
    _unit_label,
    _utc_hour_to_local,
    _utc_iso_to_local,
    _utcnow,  # noqa: F401 — re-exported so tests can patch ac_infinity_mcp.server._utcnow
)
from ac_infinity_mcp.logging_config import (
    _FIELD_PATTERN,  # noqa: F401 — re-exported for test compatibility
    _CredentialRedactingFormatter,  # noqa: F401 — re-exported for test compatibility
    _install_credential_redactor,
    _redact_credentials,  # noqa: F401 — re-exported for test compatibility
)
from ac_infinity_mcp.ports import (
    _PORT_EMPTY_RESISTANCE,  # noqa: F401 — re-exported for test compatibility
    _empty_port_advisory,
    _is_port_empty,
)
from ac_infinity_mcp.schema import (
    _ADVANCE_MODE_TYPE,
    _AUTH_ERROR_MSG,
    ACInfinityAdvanceConflictError,
    ACInfinityAPIError,
    ACInfinityAuthError,
    ACInfinityDeviceError,
)

_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


def _resolve_log_level(raw: str | None) -> tuple[str, bool]:
    """Map a raw LOG_LEVEL env value to a valid logging level + warn flag.

    Returns (effective_level, fallback_warning_needed).

    Defensive: a malformed LOG_LEVEL would cause logging.basicConfig to raise
    ValueError at import, before any error handler can format the failure for
    the operator. Falls back to INFO and signals that a warning should be
    emitted once the logger is configured.
    """
    candidate = (raw or "INFO").upper()
    if candidate not in _VALID_LOG_LEVELS:
        return "INFO", True
    return candidate, False


_log_level_raw = os.getenv("LOG_LEVEL", "INFO").upper()
_log_level_effective, _log_level_fallback_warning = _resolve_log_level(_log_level_raw)

logging.basicConfig(level=_log_level_effective)
logger = logging.getLogger(__name__)
if _log_level_fallback_warning:
    logger.warning(
        "LOG_LEVEL=%r is not a recognized level; falling back to INFO. "
        "Valid: DEBUG, INFO, WARNING, ERROR, CRITICAL",
        _log_level_raw,
    )


_install_credential_redactor()

mcp_server = FastMCP(name="ac-infinity-mcp")

# TTL cache for get_devices — avoids redundant API fetches in interactive sessions.
# Device data stales only on physical change or port rename; 45s covers normal usage.
_DEVICE_CACHE_TTL: float = 45.0
_device_cache: list[dict] | None = None
_device_cache_expires_at: float = 0.0

_aci_client: ACInfinityClient | None = None


def _invalidate_device_cache() -> None:
    """Expire the device cache immediately (call after writes that change device structure)."""
    global _device_cache, _device_cache_expires_at
    _device_cache = None
    _device_cache_expires_at = 0.0


def setup(client: ACInfinityClient) -> None:
    """Wire the client into the server and reset state. Call once at startup or in tests."""
    global _aci_client
    _aci_client = client
    _invalidate_device_cache()


def _client() -> ACInfinityClient:
    """Return the initialized client; raises RuntimeError if setup() was not called."""
    if _aci_client is None:
        raise RuntimeError("AC Infinity client not initialized — call setup() first")
    return _aci_client


async def _get_device(
    device_id: str, *, for_write: bool = False
) -> tuple[dict | None, str | None]:
    """Fetch devices (from TTL cache if warm) and find the one matching device_id.

    Returns (device_dict, None) on success, (None, error_json) on not-found.

    ``for_write`` (set by every write tool) rejects controllers shared from another
    AC Infinity account (``isShare == 1``): the API returns "No Permission" on writes
    to them, so we surface a grower-readable read-only message instead of attempting
    the write. Read tools leave ``for_write=False`` so shared devices stay viewable.
    The guard fires before any dry-run handling, so a shared device is blocked in
    both preview and live paths.
    """
    global _device_cache, _device_cache_expires_at
    now = time.monotonic()
    if _device_cache is None or now >= _device_cache_expires_at:
        _device_cache = await asyncio.to_thread(_client().get_devices)
        _device_cache_expires_at = now + _DEVICE_CACHE_TTL
    device = next((d for d in _device_cache if d.get("devCode") == device_id), None)
    if not device:
        return None, json.dumps({"error": f"Device {device_id} not found"})
    if for_write and device.get("isShare") == 1:
        name = _sanitize_api_string(device.get("devName"), 64)  # "(unnamed)" if missing
        return None, json.dumps(
            {
                "error": (
                    f"{name} is shared with you from another AC Infinity account, so it's "
                    "view-only. You can see its readings, but only the account that owns it "
                    "can change its settings. Ask the owner if you need control."
                )
            }
        )
    return device, None


# ============ Advance Automation Helpers ============

# Per-device async locks for break_out_of_automation sequencing.
# Prevents concurrent break-out operations on the same device from interleaving
# the disable + port-lock steps (a race could partially apply state).
_device_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def _get_device_lock(device_id: str) -> asyncio.Lock:
    """Return (creating if absent) the per-device async lock."""
    lock = _device_locks.get(device_id)
    if lock is None:
        lock = asyncio.Lock()
        _device_locks[device_id] = lock
    return lock


_AUTOMATION_ID_RE = re.compile(r"^[1-9]\d{0,19}$")


def _validate_automation_id(automation_id: str) -> int | None:
    """Validate that automation_id is a pure integer string. Returns int or None."""
    if _AUTOMATION_ID_RE.match(automation_id or ""):
        return int(automation_id)
    return None


# ============ Automation Rule CRUD helpers (Issue #284) ============

_RULE_MODES = ("off", "on", "cycle", "auto", "vpd")
_CONTROL_STYLES = ("target", "trigger")

# Day token set accepted by ``days``. Aliases expand to bitmasks (bit0=Mon … bit6=Sun).
_DAY_TOKENS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}
_DAY_ALIASES = {"all", "weekdays", "weekends"}

# Sensor param ranges (upper bounds added per R7).
_TEMP_F_MIN, _TEMP_F_MAX = 32, 212
_HUMI_MIN, _HUMI_MAX = 0, 100
_VPD_MIN, _VPD_MAX = 0.0, 9.9
_TEMP_BUFFER_MAX = 180  # a sane °F delta upper bound
_LEVEL_MIN, _LEVEL_MAX = 0, 10

# °C rails the app pairs with the °F temperature rails (mirrors client.py; NOT derived).
_RAIL_TEMP_HIGH_C = 90
_RAIL_TEMP_LOW_C = 0

# Which grower params are relevant to each mode. Any supplied param not in the
# chosen mode's set is rejected (cross-mode param guard).
_AUTO_SENSOR_FIELDS = {
    "control_style", "temp_high_f", "temp_low_f", "humidity_high", "humidity_low",
    "temp_target_f", "humidity_target",
    "temp_buffer", "temp_transition", "humidity_buffer", "humidity_transition",
}
_VPD_SENSOR_FIELDS = {
    "control_style", "vpd_target", "vpd_high", "vpd_low", "vpd_buffer", "vpd_transition",
}
_MODE_PARAM_FIELDS: dict[str, set[str]] = {
    "off": set(),
    "on": set(),
    "cycle": {"cycle_on_minutes", "cycle_off_minutes"},
    "auto": set(_AUTO_SENSOR_FIELDS),
    "vpd": set(_VPD_SENSOR_FIELDS),
}


def _days_to_switchtime(days: list[str] | str | None, continuous: bool) -> int:
    """Convert a ``days`` spec + ``continuous`` flag to the switchTime bitmask.

    bit0=Mon … bit6=Sun; bit7 (128) = continuous (overrides days → 255 = 127|128).
    ``days`` may be None (→ all 7 days), the string aliases "all"/"weekdays"/"weekends",
    or a list of day-name tokens. Returns the integer switchTime value.
    """
    if continuous:
        return 255
    if days is None:
        return 127  # default: every day scheduled
    if isinstance(days, str):
        alias = days.strip().lower()
        if alias == "all":
            return 127
        if alias == "weekdays":
            return 31  # Mon–Fri
        if alias == "weekends":
            return 96  # Sat | Sun = bit5 | bit6
        # A single bare day token passed as a string.
        if alias in _DAY_TOKENS:
            return 1 << _DAY_TOKENS[alias]
        return 127
    mask = 0
    for tok in days:
        key = str(tok).strip().lower()
        if key in _DAY_TOKENS:
            mask |= 1 << _DAY_TOKENS[key]
    return mask if mask else 127


def _ports_bitmask(ports: list[int]) -> int:
    """OR of 2**(p-1) for each port — the grouptDevType of a rule governing those ports."""
    return sum(2 ** (p - 1) for p in ports)


# Per-port target/setpoint capability (#288). devInfoListAll exposes a `modeTye` field per
# port (note the API's typo): observed 15 = target-capable (UIS Pro+/AI firmware), 0 = legacy
# with no target/setpoint support. It is PER-PORT — a devType=22 controller mixes 0 and 15
# across its ports — so target must NEVER be gated by devType. A port that does not report
# the field is treated as capable, so a device that omits it is never false-blocked.
_MODETYE_NO_TARGET = 0


def _ports_without_target_support(device: dict, ports: list[int]) -> list[int]:
    """Return the subset of ``ports`` whose ``modeTye`` marks them as not target-capable."""
    info_ports: dict[int, object] = {}
    try:
        for p in device.get("deviceInfo", {}).get("ports", []):
            pn = p.get("port")
            if pn is not None:
                info_ports[int(pn)] = p.get("modeTye")
    except (TypeError, ValueError, AttributeError):
        return []
    return [pt for pt in ports if info_ports.get(pt) == _MODETYE_NO_TARGET]


def _target_capability_error(device: dict, ports: list[int]) -> str | None:
    """Friendly error JSON if any governed port lacks target/setpoint support, else None.

    Mirrors the AC Infinity app, which only offers a target/hold option on capable ports;
    on a legacy port a target write renders as garbage rail triggers (#288).
    """
    bad = _ports_without_target_support(device, ports)
    if not bad:
        return None
    labels = _ports_label(_build_port_name_map(device), bad)
    return json.dumps({
        "error": (
            f"{labels} doesn't support target/hold mode on this controller —"
            " use high/low thresholds (trigger) instead."
        ),
        "suggested_reply": (
            f"{labels} can't hold a setpoint on this controller. I can set it to turn on"
            " above or below a threshold instead — want me to do that?"
        ),
    })


def _port_label_for(port_name_map: dict[int, str], port: int) -> str:
    """Return 'Name (Port N)' or 'Port N' for a single port."""
    raw = port_name_map.get(port, f"Port {port}")
    return f"{raw} (Port {port})" if raw != f"Port {port}" else raw


def _ports_label(port_name_map: dict[int, str], ports: list[int]) -> str:
    """Comma-joined 'Name (Port N)' labels for a list of ports."""
    return ", ".join(_port_label_for(port_name_map, p) for p in sorted(ports))


# Units that read naturally attached to the number (matching the existing prose, e.g.
# "75°F"): no space between the value and the unit. Everything else (ppm, ppt, µS/cm,
# mS/cm) takes a single space ("793 ppm"). Empty unit renders with neither.
_ATTACHED_UNITS: frozenset[str] = frozenset({"%", "°C", "°F"})


def _format_sensor_clause(external_sensors: list[dict]) -> str:
    """Grower-readable prose clause for external sensor readings, e.g.
    "External sensors — CO2: 793 ppm, pH: 6.5, Light: 100.0%" (no leading space or
    trailing period — the call site owns sentence punctuation). Returns "" when there
    are no external sensors so the summary stays byte-identical to the pre-sensor
    output. The composed clause is sanitized (matching the other API-derived prose on
    these lines); the empty case short-circuits before sanitizing so it never becomes
    the "(unnamed)" placeholder."""
    if not external_sensors:
        return ""
    parts = []
    for s in external_sensors:
        label = s.get("sensor_type_label", "")
        value = s.get("value")
        unit = s.get("unit", "")
        sep = "" if (not unit or unit in _ATTACHED_UNITS) else " "
        parts.append(f"{label}: {value}{sep}{unit}")
    clause = "External sensors — " + ", ".join(parts)
    return _sanitize_api_string(clause, 512)


def _format_probe_clause(probes: list[dict]) -> str:
    """Grower-readable prose clause for plug-in probe readings, e.g.
    "Probe Sensor (Sensor Port 2): 66.3°F, 81.6% RH, VPD 0.39 kPa" (no leading space
    or trailing period — the call site owns sentence punctuation). Returns "" when
    there are no probes so the summary stays byte-identical to the pre-probe output.

    Without this a grower asking "how's it looking in there?" hears only the onboard
    reading, which is the failure mode #305 opens with — the probe is structurally
    present but conversationally invisible."""
    if not probes:
        return ""
    parts = [
        f"Probe Sensor (Sensor Port {p.get('sensor_port')}): "
        f"{p.get('temperature')}{p.get('unit')}, {p.get('humidity')}% RH, "
        f"VPD {p.get('vpd')} kPa"
        for p in probes
    ]
    return _sanitize_api_string(", ".join(parts), 512)


# switchTime bit 7 (128) = continuous flag; mirrors automation._SWITCHTIME_CONTINUOUS_BIT.
_SWITCHTIME_CONTINUOUS_BIT = 0x80


def _rule_window_str(
    begin_time: int | None,
    end_time: int | None,
    tz_label: str,
    switch_time: int | None = None,
) -> str:
    """Format a rule window as 'HH:MM–HH:MM (tz)'. Falls back gracefully for sentinels.

    When ``switch_time``'s continuous bit (0x80) is set the rule runs 24/7, so the window
    reads "runs continuously" — agreeing with the ``control`` field instead of showing a
    contradictory clock range. A degenerate zero-length window (begin==end) renders
    "always active" rather than "00:00–00:00".
    """
    if switch_time is not None and switch_time & _SWITCHTIME_CONTINUOUS_BIT:
        return "runs continuously"
    begin_s = _format_schedule_time(begin_time)
    end_s = _format_schedule_time(end_time)
    if begin_s is None or end_s is None:
        return "always active"
    if begin_time == end_time:
        return "always active"
    return f"{begin_s}–{end_s} ({tz_label})"


def _err(msg: str) -> str:
    """Shorthand for a single-error JSON response string."""
    return json.dumps({"error": msg})


def _validate_rule_inputs(
    mode: str,
    *,
    control_style: str | None = None,
    min_level: int | None = None,
    max_level: int | None = None,
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
    days: list[str] | str | None = None,
    continuous: bool = False,
    require_full: bool,
) -> tuple[dict | None, str | None]:
    """Validate compositional rule params; return (encoder_kwargs, error_json).

    ``encoder_kwargs`` is a dict of validated, non-None values suitable for direct
    splat into ``build_groups_payload`` (sensor params, min_level/max_level, switch_time,
    cycle minutes, mode/control_style). ``require_full`` is True for add (all required
    params for the mode must be present); for update missing params are allowed and simply
    omitted (the caller overlays them onto the live rule). On failure returns
    ``(None, error_json_string)``.
    """
    if mode not in _RULE_MODES:
        return None, _err(f"mode must be one of: {', '.join(_RULE_MODES)}")

    # Cross-mode param rejection: any non-None sensor/cycle param outside the mode's set.
    supplied: dict[str, object] = {
        "control_style": control_style,
        "temp_high_f": temp_high_f, "temp_low_f": temp_low_f,
        "humidity_high": humidity_high, "humidity_low": humidity_low,
        "temp_target_f": temp_target_f, "humidity_target": humidity_target,
        "vpd_target": vpd_target, "vpd_high": vpd_high, "vpd_low": vpd_low,
        "temp_buffer": temp_buffer, "temp_transition": temp_transition,
        "humidity_buffer": humidity_buffer, "humidity_transition": humidity_transition,
        "vpd_buffer": vpd_buffer, "vpd_transition": vpd_transition,
        "cycle_on_minutes": cycle_on_minutes, "cycle_off_minutes": cycle_off_minutes,
    }
    allowed = _MODE_PARAM_FIELDS[mode] | {"cycle_on_minutes", "cycle_off_minutes"} \
        if mode == "cycle" else _MODE_PARAM_FIELDS[mode]
    for field, value in supplied.items():
        if value is not None and field not in allowed:
            return None, _err(f"'{field}' does not apply to a {mode} rule and cannot be set.")

    kwargs: dict = {"mode": mode}

    # Speed range (shared across all modes that run a fan).
    if min_level is not None:
        if not _LEVEL_MIN <= min_level <= _LEVEL_MAX:
            return None, _err("min_level must be 0–10")
        kwargs["min_level"] = min_level
    if max_level is not None:
        if not _LEVEL_MIN <= max_level <= _LEVEL_MAX:
            return None, _err("max_level must be 0–10")
        kwargs["max_level"] = max_level
    if min_level is not None and max_level is not None and min_level > max_level:
        return None, _err("min_level must be less than or equal to max_level")

    # Schedule (days → switchTime). continuous overrides days.
    if days is not None and not isinstance(days, str):
        if not days:
            return None, _err(
                "days can't be empty — give day names or 'all'/'weekdays'/'weekends'"
            )
        for tok in days:
            if str(tok).strip().lower() not in _DAY_TOKENS:
                return None, _err(
                    "days must be day names (mon–sun) or 'all'/'weekdays'/'weekends'"
                )
    elif isinstance(days, str):
        if days.strip().lower() not in (_DAY_ALIASES | set(_DAY_TOKENS)):
            return None, _err(
                "days must be day names (mon–sun) or 'all'/'weekdays'/'weekends'"
            )
    if days is not None or continuous:
        kwargs["switch_time"] = _days_to_switchtime(days, continuous)

    if mode in ("on", "off"):
        return kwargs, None

    if mode == "cycle":
        if require_full and (cycle_on_minutes is None or cycle_off_minutes is None):
            return None, _err("a cycle rule needs both an on-minutes and an off-minutes value")
        for label, val in (("on", cycle_on_minutes), ("off", cycle_off_minutes)):
            if val is not None and not 0 <= val <= 1439:
                return None, _err(f"cycle {label}-minutes must be 0–1439")
        if cycle_on_minutes is not None:
            kwargs["cycle_on_minutes"] = cycle_on_minutes
        if cycle_off_minutes is not None:
            kwargs["cycle_off_minutes"] = cycle_off_minutes
        return kwargs, None

    # Auto / VPD share the control_style requirement.
    if control_style is not None and control_style not in _CONTROL_STYLES:
        return None, _err(f"control_style must be one of: {', '.join(_CONTROL_STYLES)}")
    if require_full and control_style is None:
        return None, _err(f"a {mode} rule needs control_style ('target' or 'trigger')")
    if control_style is not None:
        kwargs["control_style"] = control_style

    if mode == "auto":
        err = _validate_auto(
            kwargs, control_style=control_style, require_full=require_full,
            temp_high_f=temp_high_f, temp_low_f=temp_low_f,
            humidity_high=humidity_high, humidity_low=humidity_low,
            temp_target_f=temp_target_f, humidity_target=humidity_target,
            temp_buffer=temp_buffer, temp_transition=temp_transition,
            humidity_buffer=humidity_buffer, humidity_transition=humidity_transition,
        )
        if err:
            return None, err
        return kwargs, None

    # mode == "vpd"
    err = _validate_vpd(
        kwargs, control_style=control_style, require_full=require_full,
        vpd_target=vpd_target, vpd_high=vpd_high, vpd_low=vpd_low,
        vpd_buffer=vpd_buffer, vpd_transition=vpd_transition,
    )
    if err:
        return None, err
    return kwargs, None


def _validate_auto(
    kwargs: dict,
    *,
    control_style: str | None,
    require_full: bool,
    temp_high_f: int | None,
    temp_low_f: int | None,
    humidity_high: int | None,
    humidity_low: int | None,
    temp_target_f: int | None,
    humidity_target: int | None,
    temp_buffer: int | None,
    temp_transition: int | None,
    humidity_buffer: int | None,
    humidity_transition: int | None,
) -> str | None:
    """Validate Auto-mode sensor params, populating ``kwargs`` in place. Returns err or None."""
    # #291: a temperature setpoint ("hold temp at X") is not supported by the AC Infinity app
    # in Auto mode — it renders as thresholds, and no app-made rule ever sets one (targetTempF
    # is always the rail). The encoder path was inferred without ground truth and is wrong.
    # Reject with a redirect; humidity target and VPD target are supported and unaffected.
    if temp_target_f is not None:
        return _err(
            "Holding a temperature setpoint isn't supported — use temperature high/low"
            " thresholds (a trigger), or a VPD target, instead."
        )
    is_trigger_param = any(
        v is not None for v in (temp_high_f, temp_low_f, humidity_high, humidity_low)
    )
    is_target_param = humidity_target is not None
    # Mutual exclusion per sensor: humidity. (Temperature targets are rejected above, #291.)
    if humidity_target is not None and (humidity_high is not None or humidity_low is not None):
        return _err(
            "For humidity you asked me to both hold a target and trigger on a"
            " threshold — pick one."
        )
    if control_style == "trigger" and is_target_param:
        return _err("a trigger rule cannot take a target value — set thresholds instead")
    if control_style == "target" and is_trigger_param:
        return _err("a target rule cannot take threshold values — set a target instead")
    if require_full and control_style == "trigger" and not is_trigger_param:
        return _err("a trigger rule needs at least one temperature or humidity threshold")
    if require_full and control_style == "target" and not is_target_param:
        return _err("a target rule needs a temperature or humidity target")

    for label, val in (("temp_high_f", temp_high_f), ("temp_low_f", temp_low_f)):
        if val is not None and not _TEMP_F_MIN <= val <= _TEMP_F_MAX:
            return _err(f"{label} must be {_TEMP_F_MIN}–{_TEMP_F_MAX} °F")
    if temp_low_f is not None and temp_high_f is not None and temp_low_f >= temp_high_f:
        return _err("temp_low_f must be less than temp_high_f")
    for label, val in (("humidity_high", humidity_high), ("humidity_low", humidity_low),
                       ("humidity_target", humidity_target)):
        if val is not None and not _HUMI_MIN <= val <= _HUMI_MAX:
            return _err(f"{label} must be {_HUMI_MIN}–{_HUMI_MAX}%")
    if humidity_low is not None and humidity_high is not None and humidity_low >= humidity_high:
        return _err("humidity_low must be less than humidity_high")

    # A trigger threshold sitting on its inactive rail decodes back as "no rule set" — reject
    # it so the write is never silently lossy (a trigger above 100% RH / below freezing / at
    # the temp ceiling can never fire anyway).
    if temp_high_f is not None and temp_high_f >= _RAIL_TEMP_HIGH_F:
        return _err(f"temp_high_f must be below {_RAIL_TEMP_HIGH_F}°F to trigger")
    if temp_low_f is not None and temp_low_f <= _RAIL_TEMP_LOW_F:
        return _err(f"temp_low_f must be above {_RAIL_TEMP_LOW_F}°F to trigger")
    if humidity_high is not None and humidity_high >= _RAIL_HUMI_HIGH:
        return _err(f"humidity_high must be below {_RAIL_HUMI_HIGH}% to trigger")
    if humidity_low is not None and humidity_low <= _RAIL_HUMI_LOW:
        return _err(f"humidity_low must be above {_RAIL_HUMI_LOW}% to trigger")
    # A target sitting on its inactive rail decodes back as "no rule set" too (same lossy
    # round-trip as the triggers above). VPD targets have no rail; temp targets are rejected
    # outright (#291), so only the humidity target is checked here.
    if humidity_target is not None and humidity_target <= _RAIL_TARGET_HUMI:
        return _err(f"humidity_target must be above {_RAIL_TARGET_HUMI}% to hold")

    # Buffer XOR transition per sensor.
    if temp_buffer is not None and temp_transition is not None:
        return _err("set either a temperature buffer or a transition, not both")
    if humidity_buffer is not None and humidity_transition is not None:
        return _err("set either a humidity buffer or a transition, not both")
    for label, val, hi in (
        ("temp_buffer", temp_buffer, _TEMP_BUFFER_MAX),
        ("temp_transition", temp_transition, _TEMP_BUFFER_MAX),
        ("humidity_buffer", humidity_buffer, _HUMI_MAX),
        ("humidity_transition", humidity_transition, _HUMI_MAX),
    ):
        if val is not None and not 0 <= val <= hi:
            return _err(f"{label} must be 0–{hi}")

    for name, val in (
        ("temp_high_f", temp_high_f), ("temp_low_f", temp_low_f),
        ("humidity_high", humidity_high), ("humidity_low", humidity_low),
        ("humidity_target", humidity_target),
        ("temp_buffer", temp_buffer), ("temp_transition", temp_transition),
        ("humidity_buffer", humidity_buffer), ("humidity_transition", humidity_transition),
    ):
        if val is not None:
            kwargs[name] = val
    return None


def _validate_vpd(
    kwargs: dict,
    *,
    control_style: str | None,
    require_full: bool,
    vpd_target: float | None,
    vpd_high: float | None,
    vpd_low: float | None,
    vpd_buffer: float | None,
    vpd_transition: float | None,
) -> str | None:
    """Validate VPD-mode sensor params, populating ``kwargs`` in place. Returns err or None."""
    is_trigger_param = vpd_high is not None or vpd_low is not None
    is_target_param = vpd_target is not None
    if is_target_param and is_trigger_param:
        return _err(
            "For VPD you asked me to both hold a target and trigger on a threshold — pick one."
        )
    if control_style == "trigger" and is_target_param:
        return _err("a VPD trigger rule cannot take a target — set vpd_high / vpd_low instead")
    if control_style == "target" and is_trigger_param:
        return _err("a VPD target rule cannot take thresholds — set vpd_target instead")
    if require_full and control_style == "trigger" and not is_trigger_param:
        return _err("a VPD trigger rule needs vpd_high and/or vpd_low")
    if require_full and control_style == "target" and not is_target_param:
        return _err("a VPD target rule needs vpd_target")

    for label, val in (("vpd_target", vpd_target), ("vpd_high", vpd_high),
                       ("vpd_low", vpd_low)):
        if val is not None and not _VPD_MIN <= val <= _VPD_MAX:
            return _err(f"{label} must be {_VPD_MIN}–{_VPD_MAX} kPa")
    if vpd_low is not None and vpd_high is not None and vpd_low >= vpd_high:
        return _err("vpd_low must be less than vpd_high")
    # A VPD trigger on its inactive rail (highVpd≥99 i.e. ≥9.9 kPa, or lowVpd≤0) decodes back
    # as "no rule set" — reject the lossy write. VPD rails are stored ×10.
    if vpd_high is not None and round(vpd_high * 10) >= _RAIL_VPD_HIGH:
        return _err(f"vpd_high must be below {_RAIL_VPD_HIGH / 10:g} kPa to trigger")
    if vpd_low is not None and round(vpd_low * 10) <= _RAIL_VPD_LOW:
        return _err(f"vpd_low must be above {_RAIL_VPD_LOW / 10:g} kPa to trigger")
    if vpd_buffer is not None and vpd_transition is not None:
        return _err("set either a VPD buffer or a transition, not both")
    for label, val in (("vpd_buffer", vpd_buffer), ("vpd_transition", vpd_transition)):
        if val is not None and not _VPD_MIN <= val <= _VPD_MAX:
            return _err(f"{label} must be {_VPD_MIN}–{_VPD_MAX} kPa")

    for name, val in (
        ("vpd_target", vpd_target), ("vpd_high", vpd_high), ("vpd_low", vpd_low),
        ("vpd_buffer", vpd_buffer), ("vpd_transition", vpd_transition),
    ):
        if val is not None:
            kwargs[name] = val
    return None


def _resolve_rule(
    raw_entries: list[dict],
    program_name: str,
    ports: list[int],
    begin_time: int | None,
    end_time: int | None,
    port_name_map: dict[int, str],
    tz_label: str,
) -> tuple[dict | None, list[dict], list[dict], str | None]:
    """Resolve a single rule within one program by name + port bitmask + window.

    Returns ``(match, disambiguation, program_rules, error_json)``:
    - ``match`` is the raw getGroups entry when exactly one matches, else None.
    - ``disambiguation`` is the user-facing rule list (no advId) when >1 match.
    - ``program_rules`` is the user-facing list of all rules in the program (for the
      0-match error).
    - ``error_json`` is set (and the others empty) when the program does not exist.

    Match key: ``advName == program_name AND grouptDevType == bitmask(ports)``, plus
    ``beginTime``/``endTime`` when the caller supplied a window (exact equality).
    """
    bitmask = _ports_bitmask(ports)
    clean_target = _sanitize_api_string(program_name, 64)

    program_entries = [
        e for e in raw_entries
        if _sanitize_api_string(e.get("advName") or "", 64) == clean_target
    ]

    def _rule_view(e: dict) -> dict:
        decoded = _decode_rule(e)
        _bm = int(e.get("grouptDevType") or 0)
        _ports = [bit + 1 for bit in range(8) if _bm & (1 << bit)]
        return {
            "ports": _ports_label(port_name_map, _ports) if _ports else "Unknown",
            "control": decoded["control"],
            "window": _rule_window_str(
                e.get("beginTime"), e.get("endTime"), tz_label, e.get("switchTime")
            ),
            "running": bool(e.get("runState", 0)),
        }

    program_rules = [_rule_view(e) for e in program_entries]

    if not program_entries:
        return None, [], [], None  # caller decides program-not-found vs empty

    # A program is a shared (groupNums, sortType) SLOT. If the name maps to more than one
    # slot, editing/deleting "a rule in it" is ambiguous — we could mutate the wrong
    # program. Refuse and ask the user to make the names unique (mirrors add_automation_rule).
    slots = {(e.get("groupNums"), e.get("sortType")) for e in program_entries}
    if len(slots) > 1:
        ambiguous = json.dumps({
            "error": (
                f"More than one program named '{clean_target}'."
                " Rename them so they're unique, then try again."
            ),
            "suggested_reply": (
                f"There's more than one program called '{clean_target}', so I can't tell"
                " which one you mean. Rename them to be unique and we'll try again."
            ),
        })
        return None, [], program_rules, ambiguous

    matches = [
        e for e in program_entries
        if int(e.get("grouptDevType") or 0) == bitmask
        and (begin_time is None or e.get("beginTime") == begin_time)
        and (end_time is None or e.get("endTime") == end_time)
    ]

    if len(matches) == 1:
        return matches[0], [], program_rules, None
    if len(matches) > 1:
        return None, [_rule_view(e) for e in matches], program_rules, None
    return None, [], program_rules, None


def _build_port_name_map(device: dict) -> dict[int, str]:
    """Map port number → base name (no '(Port N)' suffix), sanitized."""
    port_name_map: dict[int, str] = {}
    try:
        for _p in device.get("deviceInfo", {}).get("ports", []):
            _pnum = _p.get("port")
            if _pnum is None:
                continue
            _raw = _p.get("portName")
            port_name_map[int(_pnum)] = (
                _sanitize_api_string(_raw, 64) if _raw else f"Port {_pnum}"
            )
    except (TypeError, ValueError, AttributeError):
        pass
    return port_name_map


# ============ MCP Tools ============

@mcp_server.tool()
async def discover_devices() -> str:
    """
    Discover all AC Infinity devices from the cloud API.
    Returns device IDs, names, and online status.
    Use this to find device_ids for use in other tools.

    Heads up: signing in through this assistant may sign you out of the AC Infinity
    mobile app, since AC Infinity allows one active session at a time. Just log back
    into the app when you need it — your controllers and schedules are unaffected.

    Returns:
        JSON example::

            {
              "devices": [
                {"device_id": "C58ZA", "device_name": "Towlie Tent", "status": "online"},
                {"device_id": "D91XB", "device_name": "Veg Tent",    "status": "online"}
              ]
            }

        Empty account returns ``{"devices": [], "message": "No devices found"}``.
        On failure returns ``{"error": "...", "detail": "..."}``.
    """
    try:
        devices = await asyncio.to_thread(_client().get_devices)
        if not devices:
            return json.dumps({"devices": [], "message": "No devices found"})

        result = [
            {
                "device_id": d.get("devCode"),
                "device_name": d.get("devName"),
                "status": "online" if d.get("online") else "offline",
                "device_type": d.get("devType"),
                "port_count": d.get("devPortCount"),
                "firmware_version": d.get("firmwareVersion"),
                "hardware_version": d.get("hardwareVersion"),
                "zone_id": _sanitize_api_string(d.get("zoneId") or "", 64) or None,
                "temp_unit": _unit_label(
                    _effective_unit(d.get("deviceInfo", {}).get("unit"))
                ),
            }
            for d in devices
        ]

        if len(result) >= 3:
            _rows = "\n".join(
                f"| {_sanitize_api_string(d['device_name'], 64) or 'Unknown'} "
                f"| {d['device_id']} | {d['status']} |"
                for d in result
            )
            _human_summary = f"| Device | ID | Status |\n|---|---|---|\n{_rows}"
        elif len(result) == 2:
            _parts = [
                f"{_sanitize_api_string(d['device_name'], 64) or 'Unknown'}"
                f" ({d['device_id']}, {d['status']})"
                for d in result
            ]
            _human_summary = f"2 devices found: {', '.join(_parts)}."
        elif len(result) == 1:
            _d = result[0]
            _human_summary = (
                f"1 device found: "
                f"{_sanitize_api_string(_d['device_name'], 64) or 'Unknown'}"
                f" ({_d['device_id']}, {_d['status']})."
            )
        else:
            _human_summary = "No devices found."

        return json.dumps({"devices": result, "human_summary": _human_summary}, indent=2)

    except ACInfinityAuthError as e:
        logger.warning("Auth error in discover_devices: %s", e)
        return json.dumps({
            "error": _AUTH_ERROR_MSG,
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error("API error in discover_devices: %s", e)
        return json.dumps({"error": "AC Infinity API error", "detail": "see server logs"})
    except Exception as e:
        logger.error("Unexpected error in discover_devices: %s", e, exc_info=True)
        return json.dumps({
            "error": "Unexpected error",
            "detail": "see server logs",
        })


@mcp_server.tool()
async def get_device_reading(device_id: str) -> str:
    """
    Get current sensor reading for a device by its AC Infinity device_id.
    Returns temperature, humidity, VPD, and timestamp.

    Args:
        device_id: The AC Infinity device code (from discover_devices)

    Returns:
        JSON example::

            {
              "device_id": "C58ZA",
              "device_name": "Towlie Tent",
              "temperature": 75.7,
              "unit": "°F",
              "humidity": 58.2,
              "vpd": 1.31,
              "timestamp": "2026-05-20T09:32:00 CDT",
              "ports": [
                {"port": 1, "name": "Inline Fan", "speed": 5},
                {"port": 2, "name": "Port 2", "speed": 0, "plug_status": "not powered"}
              ],
              "external_sensors": [
                {"sensor_id": "9.11", "sensor_type": 11,
                 "sensor_type_label": "CO2", "value": 793, "unit": "ppm"}
              ]
            }

        Temperature and timestamp use the device's own unit preference and timezone
        (from ``deviceInfo.unit`` and ``zoneId`` in the API response). Devices
        without a configured timezone fall back to UTC.
        ``external_sensors`` excludes phantom entries (API-reported sensor slots
        with no physical hardware connected — see API Quirk 20). Each entry carries a
        Title-Case ``sensor_type_label`` and a ``unit`` derived from ``sensor_type``
        (empty string for unitless pH and Water Level). EC readings carry their probe's
        unit (µS/cm or mS/cm); 1 mS/cm = 1000 µS/cm — do not compare bare numbers across
        probes.
        ``probes`` lists readings from plug-in AC-SPC24 sensor probes (sensorType
        0-3). The controller's own onboard sensor (types 4-7) is excluded by type —
        it is already the top-level ``temperature``/``humidity``/``vpd``. Each entry
        carries ``sensor_port``, ``temperature``, ``unit``, ``humidity`` and ``vpd``,
        in the same unit as the top-level reading. Empty when no probe is attached.
        ``plug_status`` is only present on a port entry when no current is detected,
        the port is not running (speed 0 and no load), **and the port still has its
        default name** (``"Port N"``). Custom-named ports are assumed to have a device
        intentionally connected — ``loadState=0`` alone cannot distinguish "nothing
        plugged in" from "device is off" for on/off devices. This matches the signal
        used in ``get_port_status``.
        On failure returns ``{"error": "...", "detail": "..."}``.
    """
    try:
        device, err = await _get_device(device_id)
        if err:
            return err
        assert device is not None

        parsed = _client().parse_device_data(device)
        tz = _effective_tz(parsed.get("zone_id"))
        unit = _effective_unit(parsed.get("temp_unit_raw"))

        _temp_val = _to_preferred_temp(parsed.get("temperature_c", 0.0), unit)
        _unit_lbl = _unit_label(unit)
        _humid = parsed.get("humidity")
        _vpd = parsed.get("vpd")
        _ts = _utc_iso_to_local(parsed.get("timestamp"), tz)
        _safe_name = _sanitize_api_string(parsed.get("device_name"), 64) or "Device"
        _sensor_clause = _format_sensor_clause(parsed.get("external_sensors", []))
        _probes = _format_probes(parsed.get("probes", []), unit)
        _probe_clause = _format_probe_clause(_probes)
        output = {
            "device_id": device_id,
            "device_name": parsed.get("device_name"),
            "temperature": _temp_val,
            "unit": _unit_lbl,
            "humidity": _humid,
            "vpd": _vpd,
            "timestamp": _ts,
            "ports": parsed.get("ports", []),
            "external_sensors": parsed.get("external_sensors", []),
            "probes": _probes,
            "human_summary": (
                f"{_safe_name}: {_temp_val}{_unit_lbl}, {_humid}% RH, VPD {_vpd} kPa. "
                + (f"{_probe_clause}. " if _probe_clause else "")
                + (f"{_sensor_clause}. " if _sensor_clause else "")
                + f"Reading from {_ts}."
            ),
        }

        return json.dumps(output, indent=2)

    except ACInfinityAuthError as e:
        logger.warning("Auth error in get_device_reading: %s", e)
        return json.dumps({
            "error": _AUTH_ERROR_MSG,
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error("API error in get_device_reading: %s", e)
        return json.dumps({"error": "AC Infinity API error", "detail": "see server logs"})
    except Exception as e:
        logger.error("Unexpected error in get_device_reading: %s", e, exc_info=True)
        return json.dumps({
            "error": "Unexpected error",
            "detail": "see server logs",
        })


@mcp_server.tool()
async def get_historical_readings(
    device_id: str,
    start_date: str,
    end_date: str,
    sample_interval: str = "1h",
    time_start: str | None = None,
    time_end: str | None = None,
) -> str:
    """
    Query AC Infinity environment data across a date range with configurable sampling.

    Args:
        device_id: The AC Infinity device code (from discover_devices)
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        sample_interval: Bucket size for averaging readings. Use "raw" for all records
            unmodified, or a duration string like "1m", "5m", "15m", "30m", "1h",
            "2h", "6h", "12h", "1d". "daily" is accepted as an alias for "1d".
            Default: "1h" (one averaged reading per hour).
        time_start: Optional UTC time filter in HH:MM format (e.g., "16:00").
            If provided, only readings at or after this time are returned.
            Invalid HH:MM strings return a structured error.
            Note: time_start/time_end filters are in UTC. Use discover_devices
            to get the device's timezone for conversion.
        time_end: Optional UTC time filter in HH:MM format (e.g., "16:15").
            If provided, only readings at or before this time are returned.
            Invalid HH:MM strings return a structured error.

            When both bounds are set and time_start > time_end (e.g. "22:00"–"06:00"),
            the window crosses midnight: the OR of [time_start, 24:00) and
            [00:00, time_end] is returned.

    Returns:
        JSON with ``"readings"`` list and ``"statistics"`` summary. Each reading contains
        timestamp, temperature_c/f, humidity, vpd, and ports list. Statistics include
        min/avg/max per metric across the returned window. If any readings were dropped
        because their timestamps could not be parsed, the response also includes
        ``"dropped_readings"`` (count) and ``"drop_reason"``. See docs/API.md for full
        shape.

        On failure returns ``{"error": "...", "detail": "..."}``.
    """
    try:
        try:
            start = datetime.fromisoformat(f"{start_date}T00:00:00+00:00")
            end = datetime.fromisoformat(f"{end_date}T23:59:59+00:00")
        except ValueError:
            return json.dumps({"error": "Dates must be in YYYY-MM-DD format"})

        if start > end:
            return json.dumps({"error": "start_date must be before or equal to end_date"})

        if sample_interval != "raw":
            try:
                _parse_duration_seconds(sample_interval)
            except ValueError as exc:
                return json.dumps({"error": str(exc)})

        # Validate time_start / time_end as HH:MM. Without this, garbage input
        # (e.g. "bad") silently excluded every reading from the result via
        # lexicographic compare and the tool returned "No data available after
        # sampling" with no hint that the filter was at fault.
        for label, value in (("time_start", time_start), ("time_end", time_end)):
            if value is not None:
                try:
                    _parse_schedule_time(value)
                except ValueError:
                    return json.dumps({
                        "error": (
                            f"Invalid {label} {value!r}: expected 'HH:MM' (00:00–23:59)"
                        ),
                    })

        device, err = await _get_device(device_id)
        if err:
            return err
        assert device is not None

        zone_id = device.get("zoneId")
        temp_unit_raw = device.get("deviceInfo", {}).get("unit")
        tz = _effective_tz(zone_id)
        unit = _effective_unit(temp_unit_raw)

        start_ts = int(calendar.timegm(start.timetuple()))
        end_ts = int(calendar.timegm(end.replace(hour=23, minute=59, second=59).timetuple()))

        dev_id_numeric = device.get("devId")
        readings: list[dict] = []

        device_info = device.get("deviceInfo", {})
        port_names: dict = {}
        for p in device_info.get("ports", []):
            port_num = p.get("port")
            if port_num is not None:
                port_names[port_num] = p.get("portName", f"Port {port_num}")

        if dev_id_numeric:
            raw_records = await asyncio.to_thread(
                _client().get_historical_data, dev_id_numeric, start_ts, end_ts
            )
            if raw_records:
                readings = [
                    _client().parse_history_record(r, port_names=port_names)
                    for r in raw_records
                ]
                logger.info(
                    "Retrieved %d readings from cloud API for %s", len(readings), device_id
                )

        if not readings:
            return json.dumps({
                "error": (
                    f"No readings available for device {device_id} "
                    f"in range {start_date} to {end_date}"
                ),
            })

        sampled = apply_sampling(readings, sample_interval)

        dropped_readings = 0
        if time_start or time_end:
            sampled, dropped_readings = _filter_readings_by_time(
                sampled, time_start, time_end
            )

        # Convert per-reading temperature and timestamp to preferred unit/timezone.
        # temperature_c is kept in the dict for apply_sampling/average_readings to work
        # on the raw records; we project to preferred unit in the output only.
        output_readings = [
            {
                **{k: v for k, v in r.items() if k not in ("temperature_c", "temperature_f")},
                "temperature": _to_preferred_temp(r.get("temperature_c", 0.0), unit),
                "unit": _unit_label(unit),
                "timestamp": _utc_iso_to_local(r.get("timestamp"), tz),
            }
            for r in sampled
        ]

        if sampled:
            temps_c = [r.get("temperature_c", 0) for r in sampled if "temperature_c" in r]
            humidities = [r.get("humidity", 0) for r in sampled if "humidity" in r]
            vpds = [r.get("vpd", 0) for r in sampled if "vpd" in r]

            port_stats: dict = {}
            for r in sampled:
                for port in r.get("ports", []):
                    name = port.get("name", f"Port {port.get('port')}")
                    port_stats.setdefault(name, []).append(port.get("speed", 0))

            port_statistics = {
                name: {
                    "min": round(min(speeds), 2),
                    "avg": round(sum(speeds) / len(speeds), 2),
                    "max": round(max(speeds), 2),
                }
                for name, speeds in sorted(port_stats.items())
                if any(s > 0 for s in speeds)
            }

            temps_preferred = [_to_preferred_temp(tc, unit) for tc in temps_c]
            stats = {
                "readings_count": len(sampled),
                "sample_interval": sample_interval,
                "date_range": {"start": start_date, "end": end_date},
                "temperature": {
                    "min": round(min(temps_preferred), 2) if temps_preferred else None,
                    "avg": (
                        round(sum(temps_preferred) / len(temps_preferred), 2)
                        if temps_preferred else None
                    ),
                    "max": round(max(temps_preferred), 2) if temps_preferred else None,
                    "unit": _unit_label(unit),
                },
                "humidity": {
                    "min": round(min(humidities), 2) if humidities else None,
                    "avg": round(sum(humidities) / len(humidities), 2) if humidities else None,
                    "max": round(max(humidities), 2) if humidities else None,
                },
                "vpd": {
                    "min": round(min(vpds), 2) if vpds else None,
                    "avg": round(sum(vpds) / len(vpds), 2) if vpds else None,
                    "max": round(max(vpds), 2) if vpds else None,
                },
                "port_statistics": port_statistics,
            }
        else:
            stats = {"error": "No data available after sampling"}

        response: dict = {
            "device_id": device_id,
            "readings": output_readings,
            "statistics": stats,
        }
        if dropped_readings:
            response["dropped_readings"] = dropped_readings
            response["drop_reason"] = "malformed timestamp"
        return json.dumps(response, indent=2)

    except ACInfinityAuthError as e:
        logger.warning("Auth error in get_historical_readings: %s", e)
        return json.dumps({
            "error": _AUTH_ERROR_MSG,
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error("API error in get_historical_readings: %s", e)
        return json.dumps({"error": "AC Infinity API error", "detail": "see server logs"})
    except Exception as e:
        logger.error("Unexpected error in get_historical_readings: %s", e, exc_info=True)
        return json.dumps({
            "error": "Unexpected error",
            "detail": "see server logs",
        })


@mcp_server.tool()
async def check_vpd_drift(device_id: str, stage: str = "veg") -> str:
    """
    Check if current VPD is within target range for a growth stage.

    Args:
        device_id: The AC Infinity device code (from discover_devices)
        stage: Growth stage - one of: clones, seedling, veg, early_flower, mid_flower, late_flower

    Returns:
        JSON example::

            {
              "device_id": "C58ZA",
              "current_vpd": 1.58,
              "target_range": [1.0, 1.5],
              "stage": "veg",
              "status": "HIGH",
              "deviation": 0.08,
              "alert": "VPD 1.58 exceeds target 1.00–1.50. Raise humidity or lower temperature."
            }

        ``status`` is one of ``"OK"``, ``"LOW"``, or ``"HIGH"``.
        ``deviation`` is 0 when OK; positive when HIGH (kPa above upper bound);
        negative when LOW (kPa below lower bound).
        ``alert`` is ``null`` when status is ``"OK"``.
        On failure returns ``{"error": "...", "detail": "..."}``.
    """
    try:
        if stage not in STAGE_TARGETS:
            valid = ", ".join(STAGE_TARGETS)
            return json.dumps({"error": f"Unknown stage: {stage}. Valid: {valid}"})

        reading_json = await get_device_reading(device_id)
        reading = json.loads(reading_json)

        if "error" in reading:
            return json.dumps(reading)

        target_range = STAGE_TARGETS[stage]["vpd"]
        current_vpd = reading["vpd"]

        status = "OK"
        alert = None
        deviation = 0.0

        if current_vpd < target_range[0]:
            status = "LOW"
            deviation = round(current_vpd - target_range[0], 2)  # negative: below lower bound
            alert = (
                f"VPD {current_vpd:.2f} is below target "
                f"{target_range[0]:.2f}–{target_range[1]:.2f}. "
                "Lower humidity or raise temperature to increase VPD."
            )
        elif current_vpd > target_range[1]:
            status = "HIGH"
            deviation = round(current_vpd - target_range[1], 2)  # positive: above upper bound
            alert = (
                f"VPD {current_vpd:.2f} exceeds target "
                f"{target_range[0]:.2f}–{target_range[1]:.2f}. "
                "Raise humidity or lower temperature to reduce VPD."
            )

        if status == "OK":
            _vpd_summary = (
                f"VPD is on target at {current_vpd:.2f} kPa "
                f"(target {target_range[0]:.2f}–{target_range[1]:.2f} kPa for {stage})."
            )
        else:
            _vpd_summary = alert or ""  # alert is always set when status != OK

        return json.dumps({
            "device_id": device_id,
            "current_vpd": current_vpd,
            "target_range": target_range,
            "stage": stage,
            "status": status,
            "deviation": deviation,
            "alert": alert,
            "human_summary": _vpd_summary,
        }, indent=2)

    except ACInfinityAuthError as e:
        logger.warning("Auth error in check_vpd_drift: %s", e)
        return json.dumps({
            "error": _AUTH_ERROR_MSG,
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error("API error in check_vpd_drift: %s", e)
        return json.dumps({"error": "AC Infinity API error", "detail": "see server logs"})
    except Exception as e:
        logger.error("Unexpected error in check_vpd_drift: %s", e, exc_info=True)
        return json.dumps({
            "error": "Unexpected error",
            "detail": "see server logs",
        })


@mcp_server.tool()
async def get_all_device_readings() -> str:
    """
    Get current sensor readings for all AC Infinity devices.
    Useful for a full status check across all controllers.
    Returns a list of readings keyed by device_id.

    Returns:
        JSON with ``"readings"`` list — one entry per device, same shape as
        ``get_device_reading``. Devices that fail to parse individually include
        an ``"error"`` key instead of sensor fields.
        ``ports[].plug_status`` is present on not-powered port entries (same
        ``loadState == 0`` AND ``speak == 0`` condition as ``get_device_reading``,
        and only on default-named ``"Port N"`` ports); omitted otherwise.
        ``external_sensors`` excludes phantom entries (API-reported sensor slots
        with no physical hardware connected — see API Quirk 20). Each entry carries a
        Title-Case ``sensor_type_label`` and a ``unit`` derived from ``sensor_type``
        (empty string for unitless pH and Water Level). EC readings carry their probe's
        unit (µS/cm or mS/cm); 1 mS/cm = 1000 µS/cm — do not compare bare numbers across
        probes.
        ``probes`` lists readings from plug-in AC-SPC24 sensor probes (sensorType
        0-3). The controller's own onboard sensor (types 4-7) is excluded by type —
        it is already the top-level ``temperature``/``humidity``/``vpd``. Each entry
        carries ``sensor_port``, ``temperature``, ``unit``, ``humidity`` and ``vpd``,
        in the same unit as the top-level reading. Empty when no probe is attached.
        On auth/API failure returns ``{"error": "...", "detail": "..."}``.
    """
    try:
        devices = await asyncio.to_thread(_client().get_devices)

        readings = []
        for device in devices:
            device_id = device.get("devCode")
            try:
                parsed = _client().parse_device_data(device)
                tz = _effective_tz(parsed.get("zone_id"))
                unit = _effective_unit(parsed.get("temp_unit_raw"))
                readings.append({
                    "device_id": device_id,
                    "device_name": parsed.get("device_name"),
                    "temperature": _to_preferred_temp(parsed.get("temperature_c", 0.0), unit),
                    "unit": _unit_label(unit),
                    "humidity": parsed.get("humidity"),
                    "vpd": parsed.get("vpd"),
                    "timestamp": _utc_iso_to_local(parsed.get("timestamp"), tz),
                    "ports": parsed.get("ports", []),
                    "external_sensors": parsed.get("external_sensors", []),
                    "probes": _format_probes(parsed.get("probes", []), unit),
                })
            except Exception as e:
                readings.append({
                    "device_id": device_id,
                    "device_name": device.get("devName"),
                    "error": str(e),
                })

        _ok = [r for r in readings if "error" not in r]
        if len(_ok) >= 3:
            _rows = "\n".join(
                f"| {_sanitize_api_string(r.get('device_name'), 64) or 'Unknown'} "
                f"| {r.get('temperature')}{r.get('unit')} "
                f"| {r.get('humidity')}% "
                f"| {r.get('vpd')} kPa |"
                for r in _ok
            )
            _all_summary = f"| Device | Temp | Humidity | VPD |\n|---|---|---|---|\n{_rows}"
        elif _ok:
            _all_parts = []
            for r in _ok:
                _base = (
                    f"{_sanitize_api_string(r.get('device_name'), 64) or 'Unknown'}: "
                    f"{r.get('temperature')}{r.get('unit')}, {r.get('humidity')}% RH, "
                    f"VPD {r.get('vpd')} kPa"
                )
                _sc = _format_sensor_clause(r.get("external_sensors", []))
                _pc = _format_probe_clause(r.get("probes", []))
                _extra = ". ".join(x for x in (_pc, _sc) if x)
                _all_parts.append(f"{_base}. {_extra}" if _extra else _base)
            _all_summary = ". ".join(_all_parts) + "."
        else:
            _all_summary = "No readings available."

        return json.dumps({"readings": readings, "human_summary": _all_summary}, indent=2)

    except ACInfinityAuthError as e:
        logger.warning("Auth error in get_all_device_readings: %s", e)
        return json.dumps({
            "error": _AUTH_ERROR_MSG,
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error("API error in get_all_device_readings: %s", e)
        return json.dumps({"error": "AC Infinity API error", "detail": "see server logs"})
    except Exception as e:
        logger.error("Unexpected error in get_all_device_readings: %s", e, exc_info=True)
        return json.dumps({
            "error": "Unexpected error",
            "detail": "see server logs",
        })


@mcp_server.tool()
async def get_environment_health(device_id: str, stage: str = "veg") -> str:
    """
    Calculate composite environment health score (0–100) for a device.

    Args:
        device_id: The AC Infinity device code (from discover_devices)
        stage: Growth stage — one of: clones, seedling, veg,
               early_flower, mid_flower, late_flower. Default: veg.

    Returns:
        JSON with score (0–100), grade (A–F), per-metric sub-scores,
        top_recommendation, actual sensor readings (temperature_c, temperature_f,
        humidity_pct, vpd_kpa), and a human_summary one-liner.
    """
    try:
        if stage not in STAGE_TARGETS:
            valid = ", ".join(STAGE_TARGETS)
            return json.dumps({"error": f"Unknown stage: {stage}. Valid: {valid}"})

        device, err = await _get_device(device_id)
        if err:
            return err
        assert device is not None

        parsed = _client().parse_device_data(device)

        health = calculate_health_score(parsed, stage)
        result = dataclasses.asdict(health)
        result["device_id"] = device_id
        result["stage"] = stage
        result["human_summary"] = (
            f"Temperature {health.temperature_f:.1f}°F ({health.temperature_c:.1f}°C), "
            f"humidity {health.humidity_pct:.0f}%, VPD {health.vpd_kpa:.2f} kPa. "
            f"Overall health: {health.grade} ({health.score}/100)."
        )
        return json.dumps(result, indent=2)

    except ACInfinityAuthError as e:
        logger.warning("Auth error in get_environment_health: %s", e)
        return json.dumps({
            "error": _AUTH_ERROR_MSG,
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error("API error in get_environment_health: %s", e)
        return json.dumps({"error": "AC Infinity API error", "detail": "see server logs"})
    except Exception as e:
        logger.error("Unexpected error in get_environment_health: %s", e, exc_info=True)
        return json.dumps({
            "error": "Unexpected error",
            "detail": "see server logs",
        })


@mcp_server.tool()
async def detect_environment_trends(device_id: str, days: int = 7) -> str:
    """
    Detect linear trends in temperature, humidity, and VPD over a look-back window.

    Args:
        device_id: The AC Infinity device code (from discover_devices)
        days: Number of days to look back. Default: 7. Must be 1–30.

    Returns:
        JSON with per-metric trend reports: slope (change/hour), direction,
        7-day projection, and alert flag.

    Note:
        The AC Infinity history API returns a maximum of ~1257 records per day
        regardless of page_size. For longer windows the data may be sparse.
    """
    try:
        if not 1 <= days <= 30:
            return json.dumps({"error": "days must be between 1 and 30"})

        device, err = await _get_device(device_id)
        if err:
            return err
        assert device is not None

        dev_id = device.get("devId")
        temp_unit_raw = device.get("deviceInfo", {}).get("unit")
        unit = _effective_unit(temp_unit_raw)

        today = datetime.now(UTC).replace(tzinfo=None)
        start_dt = today - timedelta(days=days)
        start_ts = int(calendar.timegm(start_dt.timetuple()))
        end_ts = int(calendar.timegm(today.replace(hour=23, minute=59, second=59).timetuple()))

        port_names: dict[int, str] = {}
        for p in device.get("deviceInfo", {}).get("ports", []):
            pn = p.get("port")
            if pn is not None:
                port_names[pn] = _sanitize_api_string(p.get("portName"), 64) or f"Port {pn}"

        raw_records = await asyncio.to_thread(
            _client().get_historical_data, dev_id, start_ts, end_ts
        ) if dev_id else []
        readings = [
            _client().parse_history_record(r, port_names=port_names)
            for r in (raw_records or [])
        ]
        readings = apply_sampling(readings, "1h")

        if not readings:
            return json.dumps({"error": f"No readings available for device {device_id}"})

        trends = detect_trends(readings, days)  # reads temperature_c — analytics unchanged

        trend_output = []
        for t in trends:
            d = dataclasses.asdict(t)
            if d["metric"] == "temperature_c":
                d["metric"] = "temperature"
                d["slope"] = round(d["slope"] * 9 / 5, 4) if unit == "F" else d["slope"]
                d["seven_day_projection"] = _to_preferred_temp(d["seven_day_projection"], unit)
                d["slope_unit"] = f"{_unit_label(unit)}/hr"
                d["projection_unit"] = _unit_label(unit)
            trend_output.append(d)

        _arrows = {"flat": "→", "rising": "↑", "falling": "↓"}
        _trend_rows = []
        _alert_lines = []
        for _t in trend_output:
            _metric_label = _t["metric"].replace("_", " ").capitalize()
            _arrow = _arrows.get(_t["direction"], "")
            _dir_str = f"{_arrow} {_t['direction'].capitalize()}"
            _slope_unit = _t.get("slope_unit", "/hr")
            _slope_str = f"{_t['slope']:+.4f} {_slope_unit}"
            _proj = _t.get("seven_day_projection")
            _proj_unit = _t.get("projection_unit", "")
            _proj_str = f"{_proj} {_proj_unit}".strip() if _proj is not None else "N/A"
            _trend_rows.append(
                f"| {_metric_label} | {_dir_str} | {_slope_str} | {_proj_str} |"
            )
            if _t.get("alert"):
                _alert_lines.append(
                    f"⚠ {_metric_label} is trending {_t['direction']} — "
                    f"7-day projection: {_proj_str}."
                )
        _table = (
            "| Metric | Direction | Slope | 7-Day Projection |\n"
            "|---|---|---|---|\n"
            + "\n".join(_trend_rows)
        )
        if _alert_lines:
            _table += "\n\n" + "\n".join(_alert_lines)

        return json.dumps({
            "device_id": device_id,
            "days_analyzed": days,
            "readings_used": len(readings),
            "trends": trend_output,
            "human_summary": _table,
        }, indent=2)

    except ACInfinityAuthError as e:
        logger.warning("Auth error in detect_environment_trends: %s", e)
        return json.dumps({
            "error": _AUTH_ERROR_MSG,
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error("API error in detect_environment_trends: %s", e)
        return json.dumps({"error": "AC Infinity API error", "detail": "see server logs"})
    except Exception as e:
        logger.error("Unexpected error in detect_environment_trends: %s", e, exc_info=True)
        return json.dumps({
            "error": "Unexpected error",
            "detail": "see server logs",
        })


@mcp_server.tool()
async def get_port_activity_report(device_id: str, days: int = 7) -> str:
    """
    Build a per-port runtime activity report from historical data.

    Args:
        device_id: The AC Infinity device code (from discover_devices)
        days: Number of days to analyze. Default: 7. Must be 1–30.

    Returns:
        JSON with window_start_local and window_end_local (the exact local time range
        analyzed, e.g. 'May 23, 10:35 AM CDT' to 'May 24, 10:35 AM CDT'), per-port
        on_hours (total hours ON over the full period), off_hours, transitions,
        avg_speed_when_running, uptime_pct, and peak_hour_local (device-local time
        string with peak date, e.g. '3:00 PM CDT (peak on May 23)', or null if the
        port never ran).
        Note: data_quality is an internal classification field stripped from the JSON
        output before serialization — it is NOT present in the response JSON. Its
        effects are visible only in human_summary: toggle hardware (heaters, lights,
        humidifiers — loadType 4, 128, 129 or 132 on standard devices, or
        pattern-detected on devType=18/22 where loadType is unreliable) produces a
        ▎-prefixed caveat line;
        devType=22 (Q0KT4 Genetics Lab) produces a device-level Note about missing
        power-draw data. devType=18 (UIS 69 Pro+) does NOT emit this Note — its active
        ports produce reliable runtime data in historical records even though portsLoad
        is always 0.
        ports_excluded_count is the number of ports removed by the ghost-port filter,
        capped at devPortCount when the device's physical port count is known (prevents
        over-counting on sub-8-port devices; unknown/zero devPortCount means no cap).
        Six rules apply: Rule A (constant 100%% uptime + zero load), Rule B
        (auto-named Port N with low average runtime or zero load), Rule C (named
        port with zero transitions + zero load + < 1 h/day average runtime), Rule D
        (non-toggle named port with speed history ≤ 1 and zero load — confirmed toggle
        hardware with transitions > 0 is exempt; see Quirk 22 in docs/API.md), Rule E
        (named port, non-toggle hardware, zero current load,
        sub-threshold runtime — stale configured speed from a port previously set to
        OFF), and Rule F (phantom clone detection — custom-named ports sharing identical
        activity signatures with low average on-time are excluded as legacy controller
        artifacts; fires only when port_loads data is available; proper-subset guard
        ensures at least one port is always retained). The human_summary field already
        includes a brief note about excluded ports when ports_excluded_count > 0. Do not
        repeat the exclusion count in prose response.
        The transitions count uses debouncing (_MIN_DWELL_READINGS=2): single-reading
        state changes at automation window edges are not counted — only transitions
        where the new state persists for ≥ 2 consecutive readings are recorded.

        Ports whose timing data is unreliable appear only as ▎-prefixed caveat
        lines in human_summary grouped by current state, e.g.
        "▎ Currently ON: Heater (Port 2)." or
        "▎ Currently OFF: Humidifier (Port 3)."
        Do NOT quote on_hours or uptime_pct for these ports —
        relay the caveat lines verbatim instead.

        All ports listed under the main runtime sentences have reliable timing data
        and should be presented normally. When a device-level Note about missing load
        data appears in human_summary (devType=22 devices only), relay it once — do not
        add further caveats.

    Presentation guidance:
        - Always refer to ports as 'Name (Port N)', e.g., 'Exhaust Fan (Port 3)'.
        - When presenting on_hours to a grower, translate it from raw hours to natural
          language, e.g.: "The fan ran for 36.0 hours over the past 3 days (about 50%
          of the time)." Do NOT describe on_hours as hours per day.
        - window_start_local and window_end_local show the exact analysis window in the
          device's local timezone. Use these when explaining why a device shows activity
          from multiple calendar days (the window is a rolling 24h/N-day span, not a
          calendar-day boundary).
        - peak_hour_local is in device-local time with the peak date,
          e.g. '3:00 PM CDT (peak on May 23)'.
        - Ports with a ▎ caveat line: relay the caveat verbatim, no runtime numbers.
    """
    try:
        if not 1 <= days <= 30:
            return json.dumps({"error": "days must be between 1 and 30"})

        device, err = await _get_device(device_id)
        if err:
            return err
        assert device is not None

        dev_id = device.get("devId")
        zone_id = device.get("zoneId")
        tz = _effective_tz(zone_id)

        # port_loads for ghost-port Rule A filter; port_load_types for data_quality detection
        # port_speaks: current ON/OFF state (speak: 0=off, None=unavailable; treat both as off)
        port_loads: dict[int, int] = {}
        port_load_types: dict[int, int] = {}
        port_speaks: dict[int, bool] = {}
        port_names: dict[int, str] = {}
        for p in device.get("deviceInfo", {}).get("ports", []):
            pn = p.get("port")
            if pn is not None:
                port_loads[pn] = p.get("portsLoad") or 0
                port_load_types[pn] = p.get("loadType") or 0
                port_speaks[pn] = (p.get("speak") or 0) > 0
                port_names[pn] = _sanitize_api_string(p.get("portName"), 64) or f"Port {pn}"

        now_utc = _utcnow()
        today = now_utc.replace(tzinfo=None)
        start_ts = int(calendar.timegm((today - timedelta(days=days)).timetuple()))
        end_ts = int(calendar.timegm(today.replace(hour=23, minute=59, second=59).timetuple()))

        window_start_dt = datetime.fromtimestamp(start_ts, tz=UTC).astimezone(tz)
        window_end_dt = now_utc.astimezone(tz)
        window_start_local = _format_window_dt(window_start_dt)
        window_end_local = _format_window_dt(window_end_dt)

        raw_records = await asyncio.to_thread(
            _client().get_historical_data, dev_id, start_ts, end_ts
        ) if dev_id else []
        readings = [
            _client().parse_history_record(r, port_names=port_names)
            for r in (raw_records or [])
        ]
        # No sampling — build_activity_report needs raw granularity

        unique_port_count = len({
            p["port"]
            for r in readings
            for p in r.get("ports", [])
            if isinstance(p.get("port"), int)
        })
        # Cap at physical port count — history API can return phantom port records beyond
        # devPortCount. or-fallback is intentional: 0/None both mean "unknown, don't cap"
        # (reads device-list field, not history record).
        physical_port_count = device.get("devPortCount") or unique_port_count
        unique_port_count = min(unique_port_count, physical_port_count)

        dev_type = device.get("devType")
        result = build_activity_report(
            readings,
            days=days,
            port_loads=port_loads if port_loads else None,
            port_load_types=port_load_types if port_load_types else None,
            dev_type=dev_type,
        )
        ports_excluded_count = max(0, unique_port_count - len(result))

        date_range = f"{_short_date(window_start_dt)} – {_short_date(window_end_dt)}"

        # Build output with peak_hour_local instead of peak_hour_utc
        port_dicts = [
            {
                "port": p.port,
                "name": p.name,
                "on_hours": p.on_hours,
                "off_hours": p.off_hours,
                "transitions": p.transitions,
                "avg_speed_when_running": p.avg_speed_when_running,
                "uptime_pct": p.uptime_pct,
                "peak_hour_local": (
                    _utc_hour_to_local(p.peak_hour_utc, tz)
                    if p.peak_hour_utc is not None else None
                ),
                "data_quality": p.data_quality,
            }
            for p in result
        ]

        reliable_dicts = [
            d for d in port_dicts if d.get("data_quality") in (None, "no_load_signal")
        ]
        caveat_results = [r for r in result if r.data_quality == "api_constant_speed"]

        day_word = "day" if days == 1 else "days"
        if result:
            port_lines = "; ".join(
                (
                    (
                        f"{p['name']} (Port {p['port']})"
                        if p['name'] != f"Port {p['port']}"
                        else p['name']
                    )
                    + f" ran {p['uptime_pct']}% uptime "
                    + f"({p['on_hours']}h total)"
                    + (
                        f", typically active around {p['peak_hour_local']}"
                        if p["peak_hour_local"] else ""
                    )
                )
                for p in reliable_dicts
            )
            caveat_on = [r for r in caveat_results if port_speaks.get(r.port, False)]
            caveat_off = [r for r in caveat_results if not port_speaks.get(r.port, False)]

            def _fmt_port_list(reps: list[ActivityReport]) -> str:
                return ", ".join(
                    f"{r.name} (Port {r.port})" if r.name != f"Port {r.port}" else r.name
                    for r in reps
                )

            caveat_parts: list[str] = []
            if caveat_on:
                caveat_parts.append(f"▎ Currently ON: {_fmt_port_list(caveat_on)}.")
            if caveat_off:
                caveat_parts.append(f"▎ Currently OFF: {_fmt_port_list(caveat_off)}.")
            caveat_lines = " ".join(caveat_parts)
            port_word = "port" if ports_excluded_count == 1 else "ports"
            if ports_excluded_count > 0:
                if dev_type in _ZERO_LOAD_DEV_TYPES:
                    result_port_nums = {r.port for r in result}
                    excl_name_parts: list[str] = []
                    for p in device.get("deviceInfo", {}).get("ports", []):
                        pn = p.get("port")
                        if pn is not None and pn not in result_port_nums:
                            pname = port_names.get(pn, f"Port {pn}")
                            excl_name_parts.append(
                                f"{pname} (Port {pn})" if pname != f"Port {pn}" else pname
                            )
                    excluded_port_names = ", ".join(excl_name_parts)
                    if excluded_port_names:
                        excl = (
                            f" {ports_excluded_count} {port_word} excluded"
                            f" (no activity detected): {excluded_port_names}."
                        )
                    else:
                        excl = (
                            f" {ports_excluded_count} {port_word} excluded (no activity detected)."
                        )
                else:
                    excl = f" {ports_excluded_count} {port_word} excluded (no power detected)."
            else:
                excl = ""
            active_port_word = "port" if len(result) == 1 else "ports"
            if dev_type in _ZERO_LOAD_DEV_TYPES:
                preamble = (
                    f"Analyzed {days} {day_word} ({date_range})"
                    f" across {len(result)} {active_port_word}."
                )
            elif not reliable_dicts and caveat_results:
                preamble = (
                    f"Analyzed {days} {day_word} ({date_range})"
                    f" across {len(result)} {active_port_word}."
                )
            else:
                preamble = (
                    f"Analyzed {days} {day_word} ({date_range}) of activity across"
                    f" {len(result)} active {active_port_word}."
                )
            summary_parts = [preamble]
            if dev_type == 22:
                summary_parts.append(
                    "Note: This controller does not report power draw for individual"
                    " ports. ON/OFF state is the only reliable activity indicator —"
                    " history-based runtime data is not available for this controller type."
                )
            if port_lines:
                summary_parts.append(f"{port_lines}.")
            if caveat_lines:
                summary_parts.append(caveat_lines)
            if excl:
                summary_parts.append(excl.strip())
            human_summary = " ".join(summary_parts)
        else:
            port_word = "port" if ports_excluded_count == 1 else "ports"
            if ports_excluded_count > 0:
                if dev_type in _ZERO_LOAD_DEV_TYPES:
                    excl_empty_parts: list[str] = []
                    for p in device.get("deviceInfo", {}).get("ports", []):
                        pn = p.get("port")
                        if pn is not None:
                            pname = port_names.get(pn, f"Port {pn}")
                            excl_empty_parts.append(
                                f"{pname} (Port {pn})" if pname != f"Port {pn}" else pname
                            )
                    excl_empty_names = ", ".join(excl_empty_parts)
                    excl_detail = f": {excl_empty_names}" if excl_empty_names else ""
                    human_summary = (
                        f"No active port activity was detected over the past {days} {day_word}."
                        f" {ports_excluded_count} {port_word} excluded"
                        f" (no activity detected){excl_detail}."
                    )
                else:
                    human_summary = (
                        f"No active port activity was detected over the past {days} {day_word}."
                        f" {ports_excluded_count} {port_word} excluded (no power detected)."
                    )
            else:
                human_summary = (
                    f"No active port activity was detected over the past {days} {day_word}. "
                    "This can happen if all devices were off, unplugged, or no scheduled activity "
                    "occurred during the analysis window. If you expected activity, verify that "
                    "your devices are connected and scheduled to run in the AC Infinity app."
                )

        output_port_dicts = [
            {k: v for k, v in d.items() if k != "data_quality"}
            for d in port_dicts
        ]
        return json.dumps({
            "device_id": device_id,
            "days_analyzed": days,
            "window_start_local": window_start_local,
            "window_end_local": window_end_local,
            "readings_used": len(readings),
            "ports": output_port_dicts,
            "ports_excluded_count": ports_excluded_count,
            "human_summary": human_summary,
        }, indent=2)

    except ACInfinityAuthError as e:
        logger.warning("Auth error in get_port_activity_report: %s", e)
        return json.dumps({
            "error": _AUTH_ERROR_MSG,
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error("API error in get_port_activity_report: %s", e)
        return json.dumps({"error": "AC Infinity API error", "detail": "see server logs"})
    except Exception as e:
        logger.error("Unexpected error in get_port_activity_report: %s", e, exc_info=True)
        return json.dumps({
            "error": "Unexpected error",
            "detail": "see server logs",
        })


# ============ New Read Tools ============

# Schedule sentinel: begin_time/end_time == 255 means "no schedule window" (always active).
# Distinct from 65535 which is the API's general disabled-sentinel for other fields.
_SCHEDULE_ALWAYS_ACTIVE: int = 255

# curMode (devInfoListAll) and atType (getdevModeSettingList) use the same integer encoding
_MODE_LABELS: dict[int, str] = {
    1: "OFF", 2: "ON", 3: "AUTO",
    4: "TIMER_TO_ON", 5: "TIMER_TO_OFF",
    6: "CYCLE", 7: "SCHEDULE", 8: "VPD",
}

# _ADVANCE_MODE_TYPE = 15 is imported from schema above.
# NOT added to _MODE_LABELS — doing so would allow set_port_mode(mode="ADVANCE")
# to write atType=15 and trigger 999999 errors.


def _decode_mode(mode_int: int | None) -> str:
    if mode_int is None:
        return "UNKNOWN"
    return _MODE_LABELS.get(mode_int, f"UNKNOWN({mode_int})")


_MODE_AT_TYPES: dict[str, int] = {v: k for k, v in _MODE_LABELS.items()}


def _temp_pair(low: object, high: object) -> tuple[float, float] | None:
    """Coerce a stored (low, high) trigger pair to floats, or None if unusable.

    Values arrive as ints on every capture we have, but the API is
    inconsistently typed elsewhere (Quirk 20 sensors ship strings), so this
    coerces rather than assuming and treats anything unparseable as absent.
    """
    if low is None or high is None:
        return None
    try:
        return (float(low), float(high))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _resolve_temp_trigger(settings: dict, unit: str) -> tuple[float, float]:
    """Return the (low, high) temperature triggers already in the device's unit.

    The API stores each trigger twice — ``devLt``/``devHt`` in °C and
    ``devLtf``/``devHtf`` in °F — and which pair carries the real value depends
    on the controller (Quirk 38):

    - devType 11 (legacy): both pairs populated and mutually consistent.
    - devType 20 (AI+): ``devLt``/``devHt`` are always ``0``; only °F is real.

    Reading the °C pair unconditionally therefore renders *every* AI+
    temperature trigger as 0 °C / 32 °F, including ones the grower can watch
    working in the app.

    Prefer the pair matching the device's own display unit — that is the one the
    grower set, so it is exact rather than round-tripped through a conversion (a
    legacy port storing 27 °C / 80 °F should report 80 °F, not the 80.6 °F that
    converting the °C value would give). Fall back to the other pair when the
    preferred one is absent or still at its unset default, which is what makes
    AI+ work: its °C pair is always the unset ``(0, 0)``.
    """
    pair_c = _temp_pair(settings.get("devLt"), settings.get("devHt"))
    pair_f = _temp_pair(settings.get("devLtf"), settings.get("devHtf"))

    # The unset default, expressed in each scale: 0 °C is exactly 32 °F.
    unset_c = (0.0, 0.0)
    unset_f = (32.0, 32.0)

    if unit == "C":
        if pair_c is not None and pair_c != unset_c:
            lo, hi = pair_c
        elif pair_f is not None:
            lo = (pair_f[0] - 32) * 5 / 9
            hi = (pair_f[1] - 32) * 5 / 9
        elif pair_c is not None:
            lo, hi = pair_c
        else:
            lo, hi = unset_c
        return (round(lo, 1), round(hi, 1))

    if pair_f is not None and pair_f != unset_f:
        lo, hi = pair_f
    elif pair_c is not None and pair_c != unset_c:
        lo = pair_c[0] * 9 / 5 + 32
        hi = pair_c[1] * 9 / 5 + 32
    elif pair_f is not None:
        lo, hi = pair_f
    else:
        lo, hi = unset_f
    return (round(lo, 1), round(hi, 1))


def _format_schedule_time(minutes: int | None) -> str | None:
    """Convert minutes-since-midnight to HH:MM string. Returns None when disabled.

    65535 is the API's disabled-sentinel. Any other out-of-range value
    (>= 1440 minutes = past 24h, or negative) is treated as None rather than
    silently producing nonsense like "25:00" — a corrupt or unset field is
    indistinguishable from disabled in this context.
    """
    if minutes is None or minutes == 65535 or minutes == _SCHEDULE_ALWAYS_ACTIVE:
        return None
    if not (0 <= minutes < 1440):
        return None
    h, m = divmod(minutes, 60)
    return f"{h:02d}:{m:02d}"


def _format_schedule_summary(begin: int, end: int) -> str:
    """Return a grower-readable schedule description in 12-hour format."""
    if begin in (_SCHEDULE_ALWAYS_ACTIVE, 65535):
        return "Always active"

    def _fmt(m: int) -> str:
        h, mi = divmod(m, 60)
        suffix = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12}:{mi:02d} {suffix}"

    return f"Active {_fmt(begin)} – {_fmt(end)}"


def _parse_schedule_time(time_str: str | None) -> int:
    """Convert HH:MM string to minutes-since-midnight. Returns 65535 if None (disabled)."""
    if time_str is None:
        return 65535
    try:
        parts = time_str.split(":")
        if len(parts) != 2:
            raise ValueError
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        return h * 60 + m
    except (ValueError, AttributeError):
        raise ValueError(
            f"Invalid schedule time {time_str!r}: expected 'HH:MM' (00:00–23:59)"
        ) from None


async def _check_advance_mode(dev_id: str | None, port: int, fallback: str) -> str:
    """Secondary call to getdevModeSettingList to verify ADVANCE state.

    Used for AI+ devices (no curMode field) and firmware without isOpenAutomation.
    Falls back gracefully on any error — mode accuracy is best-effort for these cases.
    """
    if not dev_id:
        return fallback
    try:
        settings = await asyncio.to_thread(_client().get_mode_settings, dev_id, port)
        return "ADVANCE" if (
            settings.get("modeType") == _ADVANCE_MODE_TYPE and
            settings.get("isOpenAutomation", 1) != 0
        ) else fallback
    except Exception as e:
        logger.warning("Could not verify ADVANCE mode for port %s: %s", port, type(e).__name__)
        return fallback


def _get_port_name_from_device(device: dict | None, port: int) -> str:
    """Extract port name from device dict. Returns 'Port N' when device is None/not found."""
    if not device:
        return f"Port {port}"
    ports = device.get("deviceInfo", {}).get("ports", [])
    port_data = next((p for p in ports if p.get("port") == port), None)
    raw_name = port_data.get("portName") if port_data else None
    return _sanitize_api_string(raw_name, 64) if raw_name else f"Port {port}"


def _get_port_label(device: dict, port: int) -> tuple[str, str, dict | None]:
    """Return (port_name, port_label, port_data).

    port_label = 'Name (Port N)' for custom-named ports, or 'Port N' for defaults.
    port_data is the raw port dict from deviceInfo.ports, or None if not found.
    """
    port_name = _get_port_name_from_device(device, port)
    default_name = f"Port {port}"
    port_label = f"{port_name} ({default_name})" if port_name != default_name else port_name
    ports = device.get("deviceInfo", {}).get("ports", [])
    port_data = next((p for p in ports if p.get("port") == port), None)
    return port_name, port_label, port_data








@mcp_server.tool()
async def get_port_status(device_id: str, port: int) -> str:
    """
    Get the live operational status of a single port.

    Reads real-time fields from the device info response: actual current power
    level, active automation mode, and remaining timer seconds.

    Args:
        device_id: The AC Infinity device code (from discover_devices)
        port: 1-based port number

    Returns:
        JSON example (port not powered)::

            {
              "device_id": "C58ZA",
              "port": 1,
              "port_name": "Port 1",
              "power_level": 0,
              "mode": "OFF",
              "plug_status": "not powered"
            }

        ``mode`` is one of: OFF, ON, AUTO, TIMER_TO_ON, TIMER_TO_OFF, CYCLE, SCHEDULE, VPD,
        Automation. ``plug_status`` is only present when no current is detected on the port (the
        port is not powered or nothing is connected). It is omitted when the port is running.
        Only emitted for default-named ports (``"Port N"``) — custom-named ports are assumed to
        have a device intentionally connected; ``loadState=0`` alone cannot distinguish "nothing
        plugged in" from "device is off".
        ``remain_time_seconds`` is only present when a countdown timer is active (value > 0);
        it is omitted when there is no active timer.
        When ``mode`` is ``Automation``, the port is governed by a named Advance Automation
        program in the AC Infinity app. ``automation_name`` is present only when the port is
        under automation control and the governing automation name was successfully resolved;
        absent otherwise.

        When the port appears to have nothing connected (primary: ``portResistance == 65535``;
        fallback for old firmware: default-named ``"Port N"`` with zero load, or a devType=18/22
        controller), the response also includes a ``note`` field alerting the grower
        (e.g. ``"Port 7 doesn't appear to have anything connected."``).

        On failure returns ``{"error": "...", "detail": "..."}``.

        Note on ADVANCE detection: ``isOpenAutomation==1`` in devInfoListAll is the primary
        signal. For AI+ devices (no curMode field) and older firmware without isOpenAutomation,
        a secondary call to getdevModeSettingList is made to check modeType.
    """
    try:
        if port < 1:
            return json.dumps({"error": "port must be a positive integer"})

        device, err = await _get_device(device_id)
        if err:
            return err
        assert device is not None

        ports = device.get("deviceInfo", {}).get("ports", [])
        port_data = next((p for p in ports if p.get("port") == port), None)
        if port_data is None:
            return json.dumps({"error": f"Port {port} not found on device {device_id}"})

        dev_id = device.get("devId")
        cur_mode_int = port_data.get("curMode")

        if port_data.get("isOpenAutomation") == 1:
            # Primary ADVANCE signal — present in devInfoListAll; no secondary call needed.
            mode_str = "ADVANCE"
        elif cur_mode_int not in _MODE_LABELS:
            # AI+ devices return no curMode, or future firmware may introduce new codes.
            # Secondary call to getdevModeSettingList to verify.
            mode_str = await _check_advance_mode(dev_id, port, _decode_mode(cur_mode_int))
        elif cur_mode_int == 1 and port_data.get("speak", 0) > 0:
            # Heuristic fallback for firmware without isOpenAutomation: a port reporting
            # curMode=1 (OFF) while speak>0 is a contradiction — a genuinely OFF port
            # has speak=0. This catches ADVANCE ports on older firmware. Genuine OFF
            # ports (speak=0) are exempt; ADVANCE-at-speed-0 is a known gap.
            mode_str = await _check_advance_mode(dev_id, port, "OFF")
        else:
            mode_str = _decode_mode(cur_mode_int)

        automation_name: str | None = None
        if mode_str == "ADVANCE" and dev_id:
            try:
                raw_adv = await asyncio.to_thread(_client().get_advance_automations, str(dev_id))
                governing = _find_governing_automation(_group_automations(raw_adv), port)
                automation_name = governing["name"] if governing else None
            except ACInfinityAuthError:
                raise
            except Exception as exc:
                logger.warning(
                    "Could not fetch advance automations in get_port_status (device=%s): %s",
                    device_id,
                    type(exc).__name__,
                )
            mode_str = "Automation"
        elif mode_str == "ADVANCE":
            mode_str = "Automation"

        _ps_raw_name = port_data.get("portName", f"Port {port}")
        _ps_label = (
            f"{_ps_raw_name} (Port {port})" if _ps_raw_name != f"Port {port}" else f"Port {port}"
        )
        _ps_power = port_data.get("speak", 0)
        if mode_str == "Automation" and automation_name:
            _ps_summary = (
                f"{_ps_label} is running under '{automation_name}' automation "
                f"at speed {_ps_power}."
            )
        elif _ps_power == 0:
            _ps_summary = f"{_ps_label} is {mode_str} (speed 0)."
        else:
            _ps_summary = f"{_ps_label} is {mode_str} at speed {_ps_power}."

        result: dict = {
            "device_id": device_id,
            "port": port,
            "port_name": _ps_raw_name,
            "power_level": _ps_power,
            "mode": mode_str,
        }
        if automation_name is not None:
            result["automation_name"] = automation_name
        remain = port_data.get("remainTime") or 0
        if remain > 0:
            result["remain_time_seconds"] = remain
        if not port_data.get("loadState", 0) and not _ps_power and _ps_raw_name == f"Port {port}":
            result["plug_status"] = "not powered"
        if _is_port_empty(port_data, port, device):
            _port_label_s = _ps_raw_name
            result["advisory"] = _empty_port_advisory(_port_label_s)
        result["human_summary"] = _ps_summary
        return json.dumps(result, indent=2)

    except ACInfinityAuthError as e:
        logger.warning("Auth error in get_port_status: %s", e)
        return json.dumps({
            "error": _AUTH_ERROR_MSG,
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error("API error in get_port_status: %s", e)
        return json.dumps({"error": "AC Infinity API error", "detail": "see server logs"})
    except Exception as e:
        logger.error("Unexpected error in get_port_status: %s", e, exc_info=True)
        return json.dumps({
            "error": "Unexpected error",
            "detail": "see server logs",
        })


@mcp_server.tool()
async def get_port_settings(device_id: str, port: int) -> str:
    """
    Get the full automation configuration for a port.

    Calls the getdevModeSettingList endpoint and returns the active mode,
    speed target, and all configured automation targets (VPD, temperature,
    humidity, schedule, timer, cycle).

    Args:
        device_id: The AC Infinity device code (from discover_devices)
        port: 1-based port number

    Returns:
        JSON example (non-ADVANCE port)::

            {
              "device_id": "C58ZA",
              "port": 1,
              "mode": "AUTO",
              "speed_target": 5,
              "vpd_target_kpa": null,
              "temp_range": null,
              "humidity_range_pct": null,
              "schedule_window": null,
              "cycle_on_seconds": 300,
              "cycle_off_seconds": 60
            }

        When ``mode`` is ``"ADVANCE"``, ``speed_target`` is null (an automation governs
        the port), and the response includes three additional enrichment fields:

        - ``automation_running``: ``true`` if the governing automation has
          ``run_state=True``; ``false`` if an automation was found but none active;
          ``null`` when the secondary API call failed (degraded path).
        - ``automation_configured``: ``true`` if the automations list is non-empty;
          ``false`` if empty; ``null`` when degraded.
        - ``human_summary``: grower-readable description of the ADVANCE state.
          Three variants:
          - Governing found: ``"Port is running under 'Name' automation (target
            speed: N, current live speed: M). The automation is active."``
          - All disabled: ``"Port is in automation mode, but all automations are
            disabled. The port hasn't fully released. Ask me to list your
            automations for details."``
          - Degraded: ``"Port is in ADVANCE automation mode. Automation details
            could not be retrieved."``

        ``current_speed`` reflects the live fan speed from the device.
        ``automation_name``/``automation_id`` are populated from the governing
        automation (or null if none active or secondary lookup degrades).
        ``automation_on_speed`` is read from the port group of the governing
        automation whose ``grouptDevType`` bitmask covers this port (bitmask-matched);
        null when no governing automation, no matching port group, or degraded.
        ``vpd_target_kpa`` is non-null only when VPD automation is active.
        ``temp_range`` / ``humidity_range_pct`` are non-null only when those
        thresholds are enabled. ``schedule_window`` times are in device local time
        (not UTC).

        When the port appears to have nothing connected (primary: ``portResistance == 65535``;
        fallback for old firmware: default-named ``"Port N"`` with zero load, or a devType=18/22
        controller), the response includes a staleness-aware ``note`` field.
        On the non-ADVANCE path, ``human_summary`` is overridden with a staleness statement
        and ``note`` is set to a redirect hint, so the response doesn't contradict itself
        (e.g. "Humidity automation: 60–100%") for a port with nothing connected.
        On the ADVANCE path, ``human_summary`` is preserved (it already describes the
        automation state) and only ``note`` is appended.
        On failure returns ``{"error": "...", "detail": "..."}``.
    """
    try:
        if port < 1:
            return json.dumps({"error": "port must be a positive integer"})

        device, err = await _get_device(device_id)
        if err:
            return err
        assert device is not None

        dev_id = device.get("devId")
        if not dev_id:
            return json.dumps({"error": f"Device {device_id} is missing devId"})

        settings = await asyncio.to_thread(_client().get_mode_settings, dev_id, port)

        # Extract current live speed from devInfoListAll for ADVANCE enrichment.
        port_data = next(
            (p for p in device.get("deviceInfo", {}).get("ports", []) if p.get("port") == port),
            None,
        )
        current_speed = int(port_data.get("speak", 0)) if port_data else 0

        # ADVANCE detection (Quirk 19): modeType=15 AND isOpenAutomation != 0.
        # Safe-fail default: absent isOpenAutomation key treated as 1 (active).
        if (
            settings.get("modeType") == _ADVANCE_MODE_TYPE
            and settings.get("isOpenAutomation", 1) != 0
        ):
            governing = None
            degraded = False
            adv_grouped: list[dict] = []
            try:
                raw_adv = await asyncio.to_thread(_client().get_advance_automations, str(dev_id))
                adv_grouped = _group_automations(raw_adv)
                governing = _find_governing_automation(adv_grouped, port)
            except ACInfinityAuthError:
                # ACInfinityAuthError must precede Exception — auth must propagate, not degrade.
                raise
            except Exception as exc:
                logger.warning(
                    "Could not fetch advance automations in get_port_settings (device=%s): %s",
                    device_id,
                    type(exc).__name__,
                )
                degraded = True

            governing_pg = (
                _find_governing_port_group(governing, port) if governing is not None else None
            )
            resp: dict = {
                "device_id": device_id,
                "port": port,
                "mode": "ADVANCE",
                "advance_automation": True,
                "automation_name": governing["name"] if governing else None,
                "automation_id": governing["automation_id"] if governing else None,
                "automation_on_speed": (
                    governing_pg.get("on_speed") if governing_pg is not None else None
                ),
                "current_speed": current_speed,
                "speed_target": None,
                "vpd_target_kpa": None,
                "temp_range": None,
                "humidity_range_pct": None,
                "schedule_window": None,
                "cycle_on_seconds": None,
                "cycle_off_seconds": None,
                "timer_on_seconds": None,
                "timer_off_seconds": None,
            }
            resp["automation_running"] = (
                None if degraded
                else bool(governing.get("run_state", False)) if governing
                else False
            )
            resp["automation_configured"] = None if degraded else len(adv_grouped) > 0
            if degraded:
                resp["human_summary"] = (
                    "Port is in ADVANCE automation mode."
                    " Automation details could not be retrieved."
                )
            elif governing:
                _target_speed = (
                    governing_pg.get("on_speed") if governing_pg is not None else "?"
                )
                resp["human_summary"] = (
                    f"Port is running under '{governing['name']}' automation"
                    f" (target speed: {_target_speed}, current live speed: {current_speed})."
                    " The automation is active."
                )
            else:
                resp["human_summary"] = (
                    "Port is in automation mode, but all automations are disabled."
                    " The port hasn't fully released."
                    " Ask me to list your automations for details."
                )
            if degraded:
                resp["advisory"] = (
                    "Could not fetch automation details."
                    " Ask me to list your automations for details."
                )
            if _is_port_empty(port_data, port, device):
                _ps_raw_name = (
                    port_data.get("portName", f"Port {port}") if port_data else f"Port {port}"
                )
                _ps_port_label = (
                    f"{_ps_raw_name} (Port {port})"
                    if _ps_raw_name != f"Port {port}"
                    else f"Port {port}"
                )
                _adv_stale_note = (
                    f"{_ps_port_label} doesn't appear to have anything connected. "
                    "Any settings shown may be stale from a previous configuration. "
                    "If you meant a different port, let me know which one."
                )
                if "advisory" in resp:
                    resp["advisory"] = resp["advisory"] + " " + _adv_stale_note
                else:
                    resp["advisory"] = _adv_stale_note
            return json.dumps(resp, indent=2)

        vpd_target = None
        if settings.get("targetVpdSwitch"):
            raw = settings.get("targetVpd", 0)
            # Clamp out-of-range / corrupted values. Realistic VPD targets are
            # 0–3 kPa; anything outside [0, 50] (i.e. 0..500 raw) suggests a
            # corrupt or unset field rather than a plant-bearable target. Return
            # None instead of feeding nonsense to the LLM.
            try:
                vpd_target = round(int(raw) / 10, 2)
                if not (0 <= vpd_target <= 50):
                    logger.warning(
                        "targetVpd out of range (%s) — returning null", vpd_target
                    )
                    vpd_target = None
            except (TypeError, ValueError):
                logger.warning("targetVpd is non-numeric (%r) — returning null", raw)
                vpd_target = None

        _zone_id = device.get("zoneId")
        _temp_unit_raw = device.get("deviceInfo", {}).get("unit")
        _unit = _effective_unit(_temp_unit_raw)
        _unit_lbl = _unit_label(_unit)

        temp_range = None
        if settings.get("activeLt") or settings.get("activeHt"):
            _t_lo, _t_hi = _resolve_temp_trigger(settings, _unit)
            temp_range = {
                "min": _t_lo,
                "max": _t_hi,
                "unit": _unit_lbl,
            }

        humi_range = None
        if settings.get("activeLh") or settings.get("activeHh"):
            humi_range = {
                "min_pct": settings.get("devLh", 0),
                "max_pct": settings.get("devHh", 0),
            }

        sched_start = _format_schedule_time(settings.get("schedStartTime"))
        sched_end = _format_schedule_time(settings.get("schedEndtTime"))  # API typo: EndtTime
        # A half-configured schedule (only start, or only end) is not a meaningful
        # window — return None rather than {"start": "...", "end": None}, which
        # forces the caller to interpret a confusing partial state.
        schedule_window = (
            {"start": sched_start, "end": sched_end, "timezone": _zone_id or "UTC"}
            if sched_start is not None and sched_end is not None
            else None
        )

        # Build human_summary for non-ADVANCE path
        mode_str = _decode_mode(settings.get("atType"))
        _port_name_str = (
            port_data.get("portName", f"Port {port}") if port_data else f"Port {port}"
        )
        # The summary must describe what the port is DOING, which is decided by
        # atType — not by whichever stored threshold happens to be populated.
        # Thresholds persist across mode changes, so a port sitting in OFF can
        # still carry an active-looking temperature range from a previous
        # configuration. Reporting that as behaviour ("Fan speeds up above
        # 82.0°F") states something the controller is not doing.
        #
        # Only AUTO and VPD are trigger-driven. Within AUTO both the temperature
        # and humidity families can be live at once, so they are joined rather
        # than ranked — the previous first-match chain silently dropped whichever
        # came second.
        _clauses: list[str] = []
        if mode_str == "AUTO":
            if temp_range:
                _t_min, _t_max = temp_range["min"], temp_range["max"]
                _clauses.append(
                    f"Temperature automation: {_t_min}–{_t_max}{_unit_lbl}. "
                    f"Fan speeds up above {_t_max}{_unit_lbl} and slows below "
                    f"{_t_min}{_unit_lbl}."
                )
            if humi_range:
                _clauses.append(
                    f"Humidity automation: {humi_range['min_pct']}–"
                    f"{humi_range['max_pct']}%."
                )
        elif mode_str == "VPD" and vpd_target is not None:
            _clauses.append(f"VPD automation: target {vpd_target} kPa.")

        if _clauses:
            human_summary = " ".join(_clauses)
        else:
            human_summary = f"Port is in {mode_str} mode."
            # Stored thresholds a mode change left behind are still worth
            # surfacing — they are what the port would use if switched back —
            # but as stored config, not as current behaviour.
            if temp_range or humi_range or vpd_target is not None:
                human_summary += (
                    " It has stored automation settings, but they are not active "
                    f"in {mode_str} mode."
                )

        _cycle_on = settings.get("activeCycleOn") or 0
        _cycle_off = settings.get("activeCycleOff") or 0
        _timer_on = settings.get("acitveTimerOn") or 0
        _timer_off = settings.get("acitveTimerOff") or 0
        non_adv_resp: dict = {
            "device_id": device_id,
            "port": port,
            "mode": mode_str,
            "speed_target": settings.get("onSpead", 0),
            "vpd_target_kpa": vpd_target,
            "temp_range": temp_range,
            "humidity_range_pct": humi_range,
            "schedule_window": schedule_window,
            "human_summary": human_summary,
        }
        if _cycle_on or _cycle_off:
            non_adv_resp["cycle_on_seconds"] = _cycle_on
            non_adv_resp["cycle_off_seconds"] = _cycle_off
        if _timer_on or _timer_off:
            non_adv_resp["timer_on_seconds"] = _timer_on
            non_adv_resp["timer_off_seconds"] = _timer_off
        if _is_port_empty(port_data, port, device):
            _gps_raw_name = (
                port_data.get("portName", f"Port {port}") if port_data else f"Port {port}"
            )
            _gps_port_label = (
                f"{_gps_raw_name} (Port {port})"
                if _gps_raw_name != f"Port {port}"
                else f"Port {port}"
            )
            non_adv_resp["human_summary"] = (
                f"{_gps_port_label} doesn't appear to have anything connected. "
                "Any settings shown may be stale from a previous configuration."
            )
            non_adv_resp["advisory"] = "If you meant a different port, let me know which one."
        return json.dumps(non_adv_resp, indent=2)

    except ACInfinityAuthError as e:
        logger.warning("Auth error in get_port_settings: %s", e)
        return json.dumps({
            "error": _AUTH_ERROR_MSG,
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error("API error in get_port_settings: %s", e)
        return json.dumps({"error": "AC Infinity API error", "detail": "see server logs"})
    except Exception as e:
        logger.error("Unexpected error in get_port_settings: %s", e, exc_info=True)
        return json.dumps({
            "error": "Unexpected error",
            "detail": "see server logs",
        })


# ============ Write Tools ============

@mcp_server.tool()
async def set_port_speed(
    device_id: str,
    port: int,
    speed: int,
    dry_run: bool = True,
) -> str:
    """Set fan or dimmer speed on a specific port.

    Uses read-before-write: reads current mode settings then overlays the new
    speed value. Defaults to dry_run=True — set dry_run=False to write to the
    device.

    Args:
        device_id: Device code from discover_devices (e.g. "C58ZA").
        port: 1-based port number.
        speed: Target speed 1–10 (10 = full speed).
        dry_run: If True (default), returns the payload that would be sent
            without writing. Set to False to execute the change.

    Returns:
        JSON with action, device_id, port, speed, dry_run, controller_type,
        sent, and payload (when dry_run=True).

        When the port is in OFF mode (atType=0 or atType=1) at call time, the
        response also includes a ``warning`` field telling the grower to ask
        Claude to switch the port to ON mode to activate it. The speed is stored
        on the controller but the port will not run until the mode is changed.

        Example (dry_run=True)::

            {
              "action": "set Exhaust Fan (Port 2) speed to 5",
              "device_id": "C58ZA",
              "port": 2,
              "speed": 5,
              "dry_run": true,
              "controller_type": "legacy",
              "sent": false,
              "payload": { ... }
            }

        On failure returns ``{"error": "..."}``.
    """
    try:
        if port < 1:
            return json.dumps({"error": "port must be a positive integer"})
        if not 1 <= speed <= 10:
            return json.dumps({"error": "speed must be 1–10"})

        device, err = await _get_device(device_id, for_write=True)
        if err:
            return err
        assert device is not None

        write_result = await asyncio.to_thread(
            _client().set_port_mode, device, port, {"onSpead": speed}, dry_run,
            require_variable_speed=True,
        )

        port_name, port_label, port_data = _get_port_label(device, port)

        response: dict = {
            "action": f"set {port_label} speed to {speed}",
            "device_id": device_id,
            "port": port,
            "speed": speed,
            "dry_run": write_result["dry_run"],
            "controller_type": write_result["controller_type"],
            "sent": write_result["sent"],
        }
        if write_result["dry_run"]:
            response["payload"] = write_result["payload"]

        prior_at_type = write_result.get("prior_at_type")
        if prior_at_type in (0, 1):
            response["warning"] = (
                f"{port_label} is currently in OFF mode — speed was stored but the port "
                "will not run until the mode is changed to ON. "
                "To activate it, ask me to switch this port to ON mode."
            )

        if _is_port_empty(port_data, port, device):
            response["advisory"] = _empty_port_advisory(port_label)

        return json.dumps(response, indent=2)

    except ACInfinityAuthError as e:
        logger.warning("Auth error in set_port_speed (device=%s port=%s): %s", device_id, port, e)
        return json.dumps({
            "error": _AUTH_ERROR_MSG,
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error("API error in set_port_speed (device=%s port=%s): %s", device_id, port, e)
        return json.dumps({"error": "AC Infinity API error", "detail": "see server logs"})
    except ACInfinityAdvanceConflictError:
        port_name = _get_port_name_from_device(device, port)
        dev_id = device.get("devId") if device else None
        return await _build_advance_conflict_response(
            _client(), device_id, dev_id, port, port_name, device=device, requested_speed=speed
        )
    except ACInfinityDeviceError as e:
        logger.warning("Device error in set_port_speed (device=%s port=%s): %s", device_id, port, e)
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("Unexpected error in set_port_speed: %s", e, exc_info=True)
        return json.dumps({
            "error": "Unexpected error",
            "detail": "see server logs",
        })


@mcp_server.tool()
async def set_port_on(
    device_id: str,
    port: int,
    dry_run: bool = True,
) -> str:
    """Turn a port on at full speed (onSpead=10).

    Works for fan-type and on/off toggle devices. Uses read-before-write.
    Defaults to dry_run=True — set dry_run=False to write to the device.

    Args:
        device_id: Device code from discover_devices (e.g. "C58ZA").
        port: 1-based port number.
        dry_run: If True (default), returns the payload that would be sent
            without writing.

    Returns:
        JSON with action, device_id, port, dry_run, controller_type, sent,
        and payload (when dry_run=True).

        When the port appears to have nothing connected (primary: ``portResistance == 65535``;
        fallback for old firmware: default-named ``"Port N"`` with zero load, or a devType=18/22
        device), the response also includes a ``warning`` field alerting the grower.

        On failure returns ``{"error": "..."}``.
    """
    try:
        if port < 1:
            return json.dumps({"error": "port must be a positive integer"})

        device, err = await _get_device(device_id, for_write=True)
        if err:
            return err
        assert device is not None

        write_result = await asyncio.to_thread(
            _client().set_port_mode, device, port, {"atType": 2, "onSpead": 10}, dry_run
        )

        port_name, port_label, port_data = _get_port_label(device, port)

        response: dict = {
            "action": f"turn {port_label} on",
            "device_id": device_id,
            "port": port,
            "dry_run": write_result["dry_run"],
            "controller_type": write_result["controller_type"],
            "sent": write_result["sent"],
        }
        if write_result["dry_run"]:
            response["payload"] = write_result["payload"]

        if _is_port_empty(port_data, port, device):
            response["advisory"] = _empty_port_advisory(port_label)

        return json.dumps(response, indent=2)

    except ACInfinityAuthError as e:
        logger.warning("Auth error in set_port_on (device=%s port=%s): %s", device_id, port, e)
        return json.dumps({
            "error": _AUTH_ERROR_MSG,
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error("API error in set_port_on (device=%s port=%s): %s", device_id, port, e)
        return json.dumps({"error": "AC Infinity API error", "detail": "see server logs"})
    except ACInfinityAdvanceConflictError:
        port_name = _get_port_name_from_device(device, port)
        dev_id = device.get("devId") if device else None
        return await _build_advance_conflict_response(
            _client(), device_id, dev_id, port, port_name, device=device
        )
    except ACInfinityDeviceError as e:
        logger.warning("Device error in set_port_on (device=%s port=%s): %s", device_id, port, e)
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("Unexpected error in set_port_on: %s", e, exc_info=True)
        return json.dumps({
            "error": "Unexpected error",
            "detail": "see server logs",
        })


@mcp_server.tool()
async def set_port_off(
    device_id: str,
    port: int,
    dry_run: bool = True,
) -> str:
    """Sets mode to OFF (atType=1) and zeros speed (onSpead=0).

    Works for all device types including toggle hardware (heaters, lights,
    on/off outlets). Uses read-before-write. Defaults to dry_run=True — set
    dry_run=False to write to the device.

    Args:
        device_id: Device code from discover_devices (e.g. "C58ZA").
        port: 1-based port number.
        dry_run: If True (default), returns the payload that would be sent
            without writing.

    Returns:
        JSON with action, device_id, port, dry_run, controller_type, sent,
        and payload (when dry_run=True).

        When the port appears to have nothing connected (primary: ``portResistance == 65535``;
        fallback for old firmware: default-named ``"Port N"`` with zero load, or a devType=18/22
        device), the response also includes a ``warning`` field alerting the grower.

        On failure returns ``{"error": "..."}``.
    """
    try:
        if port < 1:
            return json.dumps({"error": "port must be a positive integer"})

        device, err = await _get_device(device_id, for_write=True)
        if err:
            return err
        assert device is not None

        write_result = await asyncio.to_thread(
            _client().set_port_mode, device, port, {"onSpead": 0, "atType": 1}, dry_run
        )

        port_name, port_label, port_data = _get_port_label(device, port)

        response: dict = {
            "action": f"turn {port_label} off",
            "device_id": device_id,
            "port": port,
            "dry_run": write_result["dry_run"],
            "controller_type": write_result["controller_type"],
            "sent": write_result["sent"],
        }
        if write_result["dry_run"]:
            response["payload"] = write_result["payload"]

        if _is_port_empty(port_data, port, device):
            response["advisory"] = _empty_port_advisory(port_label)

        return json.dumps(response, indent=2)

    except ACInfinityAuthError as e:
        logger.warning("Auth error in set_port_off (device=%s port=%s): %s", device_id, port, e)
        return json.dumps({
            "error": _AUTH_ERROR_MSG,
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error("API error in set_port_off (device=%s port=%s): %s", device_id, port, e)
        return json.dumps({"error": "AC Infinity API error", "detail": "see server logs"})
    except ACInfinityAdvanceConflictError:
        port_name = _get_port_name_from_device(device, port)
        dev_id = device.get("devId") if device else None
        return await _build_advance_conflict_response(
            _client(), device_id, dev_id, port, port_name, device=device
        )
    except ACInfinityDeviceError as e:
        logger.warning("Device error in set_port_off (device=%s port=%s): %s", device_id, port, e)
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("Unexpected error in set_port_off: %s", e, exc_info=True)
        return json.dumps({
            "error": "Unexpected error",
            "detail": "see server logs",
        })


# ============ Automation Write Tools ============


def _ai_plus_write_held(device: dict | None) -> bool:
    """True when a tool must refuse a live write because the device is an AI+.

    #308 enabled AI+ (devType >= 20) writes generally, but deliberately held two
    tools back pending per-tool verification on real hardware — see #316. Both
    write field combinations whose persistence is unproven on AI+, where
    ``addDevMode`` accepts mode-irrelevant fields with code 200 and silently
    discards them (Quirk 36). Reporting ``sent: true`` for settings the device
    threw away is worse than refusing.
    """
    return detect_controller_type(device or {}) == ControllerType.NEW_FRAMEWORK


def _ai_plus_held_error(tool: str, device_id: str, port: int, reason: str) -> str:
    """Refusal payload for a tool held back on AI+ controllers."""
    return json.dumps({
        "error": (
            f"{tool} is not yet enabled for AI+ controllers (devType >= 20). {reason} "
            "Preview mode is fully supported — ask me to preview the action first, or "
            "use the individual port and automation tools, which are verified on AI+."
        ),
        "device_id": device_id,
        "port": port,
        "dry_run": False,
        "controller_type": ControllerType.NEW_FRAMEWORK.value,
        "tracking_issue": 316,
    })


@mcp_server.tool()
async def set_vpd_automation(
    device_id: str,
    port: int,
    target_vpd: float,
    dry_run: bool = True,
) -> str:
    """Enable VPD automation on a port using the built-in temperature and humidity sensors.

    Switches the port to VPD mode (atType=8) and sets the VPD target.
    Uses read-before-write. Defaults to dry_run=True — set dry_run=False to
    write to the device.

    Args:
        device_id: Device code from discover_devices (e.g. "C58ZA").
        port: 1-based port number.
        target_vpd: Target VPD in kPa, range 0.1–3.0.
            Typical ranges by stage: seedling/clones 0.8–1.2, veg 1.0–1.5,
            early_flower 1.0–1.8, mid_flower 1.2–2.0, late_flower 1.2–1.8.
        dry_run: If True (default), returns the payload that would be sent
            without writing.

    This is a target/hold write, so the port must support setpoints. A port that
    does not report target capability is rejected with guidance to use high/low
    thresholds instead.

    Returns:
        JSON with action, device_id, port, target_vpd_kpa, dry_run,
        controller_type, sent, and payload (when dry_run=True).
        On failure returns ``{"error": "..."}``.
    """
    try:
        if port < 1:
            return json.dumps({"error": "port must be a positive integer"})
        if not 0.1 <= target_vpd <= 3.0:
            return json.dumps({"error": "target_vpd must be between 0.1 and 3.0 kPa"})

        device, err = await _get_device(device_id, for_write=True)
        if err:
            return err
        assert device is not None

        # #288: VPD automation here is a target/hold (vpdSettingMode=1). Gate on the port's
        # modeTye capability — a target on a legacy port renders as garbage rail triggers.
        cap_err = _target_capability_error(device, [port])
        if cap_err:
            return cap_err

        updates = {
            "atType": 8,  # VPD mode
            "vpdSettingMode": 1,
            "targetVpd": int(target_vpd * 10 + 0.5),  # ×10; int(x+0.5) avoids banker's rounding
            "targetVpdSwitch": 1,
        }
        write_result = await asyncio.to_thread(
            _client().set_port_mode, device, port, updates, dry_run
        )

        port_name, port_label, port_data = _get_port_label(device, port)

        response: dict = {
            "action": f"set {port_label} VPD automation to {target_vpd} kPa",
            "device_id": device_id,
            "port": port,
            "target_vpd_kpa": target_vpd,
            "dry_run": write_result["dry_run"],
            "controller_type": write_result["controller_type"],
            "sent": write_result["sent"],
        }
        if write_result["dry_run"]:
            response["payload"] = write_result["payload"]
        if _is_port_empty(port_data, port, device):
            response["advisory"] = _empty_port_advisory(port_label)
        return json.dumps(response, indent=2)

    except ACInfinityAuthError as e:
        logger.warning(
            "Auth error in set_vpd_automation (device=%s port=%s): %s",
            device_id, port, e,
        )
        return json.dumps({
            "error": _AUTH_ERROR_MSG,
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error(
            "API error in set_vpd_automation (device=%s port=%s): %s",
            device_id, port, e,
        )
        return json.dumps({"error": "AC Infinity API error", "detail": "see server logs"})
    except ACInfinityAdvanceConflictError:
        port_name = _get_port_name_from_device(device, port)
        dev_id = device.get("devId") if device else None
        return await _build_advance_conflict_response(
            _client(), device_id, dev_id, port, port_name, device=device
        )
    except ACInfinityDeviceError as e:
        logger.warning(
            "Device error in set_vpd_automation (device=%s port=%s): %s",
            device_id, port, e,
        )
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("Unexpected error in set_vpd_automation: %s", e, exc_info=True)
        return json.dumps({
            "error": "Unexpected error",
            "detail": "see server logs",
        })


@mcp_server.tool()
async def set_temperature_automation(
    device_id: str,
    port: int,
    min_temp: float,
    max_temp: float,
    dry_run: bool = True,
) -> str:
    """Enable temperature automation on a port using the built-in temperature sensor.

    Switches the port to AUTO mode (atType=3) and sets the temperature thresholds.
    The controller speeds up when temperature exceeds max_temp and slows down below
    min_temp. Uses read-before-write. Defaults to dry_run=True.

    Pass values in the device's preferred unit (°F or °C). Call ``discover_devices``
    first to check ``temp_unit``. Valid range: 32–122°F or 0–50°C (device API cap = 50°C).

    Args:
        device_id: Device code from discover_devices (e.g. "C58ZA").
        port: 1-based port number.
        min_temp: Minimum temperature threshold in the device's preferred unit.
            Sub-degree values are rounded to the nearest integer.
        max_temp: Maximum temperature threshold in the device's preferred unit.
            Must exceed min_temp. Sub-degree values are rounded to the nearest integer.
        dry_run: If True (default), returns the payload that would be sent
            without writing.

    Returns:
        JSON with action, device_id, port, min_temp, max_temp, unit, dry_run,
        controller_type, sent, and payload (when dry_run=True).
        On failure returns ``{"error": "..."}``.
    """
    try:
        if port < 1:
            return json.dumps({"error": "port must be a positive integer"})

        device, err = await _get_device(device_id, for_write=True)
        if err:
            return err
        assert device is not None

        temp_unit_raw = device.get("deviceInfo", {}).get("unit")
        unit = _effective_unit(temp_unit_raw)
        unit_label = _unit_label(unit)

        if unit == "F":
            if not (32.0 <= min_temp <= 122.0 and 32.0 <= max_temp <= 122.0):
                return json.dumps({
                    "error": "min_temp and max_temp must be between 32–122°F for this device"
                })
            c_lo = round((min_temp - 32) * 5 / 9)
            c_hi = round((max_temp - 32) * 5 / 9)
        else:
            if not (0.0 <= min_temp <= 50.0 and 0.0 <= max_temp <= 50.0):
                return json.dumps({
                    "error": "min_temp and max_temp must be between 0–50°C for this device"
                })
            c_lo = int(min_temp + 0.5)  # round-half-up (not banker's rounding)
            c_hi = int(max_temp + 0.5)

        if min_temp >= max_temp:
            return json.dumps({"error": "min_temp must be less than max_temp"})

        # Post-conversion collapse guard
        if c_lo >= c_hi:
            return json.dumps({
                "error": (
                    f"Temperature range too narrow — min and max round to the same °C value "
                    f"({c_lo}°C). Widen the range by at least 2°F (or 1°C)."
                )
            })

        updates = {
            "atType": 3,  # AUTO mode
            # raw °C integer — no ×100 scaling. Converted above with round() (banker's rounding)
            # which is acceptable since we control the conversion; edge cases handled by
            # the collapse guard above.
            "devLt": c_lo,
            "devHt": c_hi,
            "activeLt": 1,
            "activeHt": 1,
        }
        # When °F device, also send the F values for informational storage
        if unit == "F":
            updates["devLtf"] = round(min_temp)
            updates["devHtf"] = round(max_temp)

        write_result = await asyncio.to_thread(
            _client().set_port_mode, device, port, updates, dry_run
        )

        port_name, port_label, port_data = _get_port_label(device, port)

        response: dict = {
            "action": f"set {port_label} temperature automation {min_temp}–{max_temp}{unit_label}",
            "device_id": device_id,
            "port": port,
            "min_temp": min_temp,
            "max_temp": max_temp,
            "unit": unit_label,
            "dry_run": write_result["dry_run"],
            "controller_type": write_result["controller_type"],
            "sent": write_result["sent"],
        }
        if write_result["dry_run"]:
            response["payload"] = write_result["payload"]
        if _is_port_empty(port_data, port, device):
            response["advisory"] = _empty_port_advisory(port_label)
        return json.dumps(response, indent=2)

    except ACInfinityAuthError as e:
        logger.warning(
            "Auth error in set_temperature_automation (device=%s port=%s): %s",
            device_id, port, e,
        )
        return json.dumps({
            "error": _AUTH_ERROR_MSG,
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error(
            "API error in set_temperature_automation (device=%s port=%s): %s",
            device_id, port, e,
        )
        return json.dumps({"error": "AC Infinity API error", "detail": "see server logs"})
    except ACInfinityAdvanceConflictError:
        port_name = _get_port_name_from_device(device, port)
        dev_id = device.get("devId") if device else None
        return await _build_advance_conflict_response(
            _client(), device_id, dev_id, port, port_name, device=device
        )
    except ACInfinityDeviceError as e:
        logger.warning(
            "Device error in set_temperature_automation (device=%s port=%s): %s",
            device_id, port, e,
        )
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("Unexpected error in set_temperature_automation: %s", e, exc_info=True)
        return json.dumps({
            "error": "Unexpected error",
            "detail": "see server logs",
        })


@mcp_server.tool()
async def set_humidity_automation(
    device_id: str,
    port: int,
    min_rh: float,
    max_rh: float,
    dry_run: bool = True,
) -> str:
    """Enable humidity automation on a port using the built-in humidity sensor.

    Switches the port to AUTO mode (atType=3) and sets the humidity thresholds.
    The controller speeds up when humidity exceeds max_rh and slows down below
    min_rh. Uses read-before-write. Defaults to dry_run=True.

    Args:
        device_id: Device code from discover_devices (e.g. "C58ZA").
        port: 1-based port number.
        min_rh: Minimum relative humidity threshold (%), range 0–100. Sub-percent values
            are rounded to the nearest integer (e.g. 50.5 → 51).
        max_rh: Maximum relative humidity threshold (%), range 0–100. Must exceed min_rh.
            Sub-percent values are rounded to the nearest integer.
        dry_run: If True (default), returns the payload that would be sent
            without writing.

    Returns:
        JSON with action, device_id, port, min_rh, max_rh, dry_run,
        controller_type, sent, and payload (when dry_run=True).
        On failure returns ``{"error": "..."}``.
    """
    try:
        if port < 1:
            return json.dumps({"error": "port must be a positive integer"})
        if not (0 <= min_rh <= 100 and 0 <= max_rh <= 100):
            return json.dumps({"error": "min_rh and max_rh must be between 0 and 100"})
        if min_rh >= max_rh:
            return json.dumps({"error": "min_rh must be less than max_rh"})

        device, err = await _get_device(device_id, for_write=True)
        if err:
            return err
        assert device is not None

        updates = {
            "atType": 3,  # AUTO mode
            # raw % RH integer — no ×100 scaling. int(x + 0.5) is round-half-up;
            # see set_temperature_automation for rationale.
            "devLh": int(min_rh + 0.5),
            "devHh": int(max_rh + 0.5),
            "activeLh": 1,
            "activeHh": 1,
        }
        write_result = await asyncio.to_thread(
            _client().set_port_mode, device, port, updates, dry_run
        )

        port_name, port_label, port_data = _get_port_label(device, port)

        response: dict = {
            "action": f"set {port_label} humidity automation {min_rh}–{max_rh}%",
            "device_id": device_id,
            "port": port,
            "min_rh": min_rh,
            "max_rh": max_rh,
            "dry_run": write_result["dry_run"],
            "controller_type": write_result["controller_type"],
            "sent": write_result["sent"],
        }
        if write_result["dry_run"]:
            response["payload"] = write_result["payload"]
        if _is_port_empty(port_data, port, device):
            response["advisory"] = _empty_port_advisory(port_label)
        return json.dumps(response, indent=2)

    except ACInfinityAuthError as e:
        logger.warning(
            "Auth error in set_humidity_automation (device=%s port=%s): %s",
            device_id, port, e,
        )
        return json.dumps({
            "error": _AUTH_ERROR_MSG,
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error(
            "API error in set_humidity_automation (device=%s port=%s): %s",
            device_id, port, e,
        )
        return json.dumps({"error": "AC Infinity API error", "detail": "see server logs"})
    except ACInfinityAdvanceConflictError:
        port_name = _get_port_name_from_device(device, port)
        dev_id = device.get("devId") if device else None
        return await _build_advance_conflict_response(
            _client(), device_id, dev_id, port, port_name, device=device
        )
    except ACInfinityDeviceError as e:
        logger.warning(
            "Device error in set_humidity_automation (device=%s port=%s): %s",
            device_id, port, e,
        )
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("Unexpected error in set_humidity_automation: %s", e, exc_info=True)
        return json.dumps({
            "error": "Unexpected error",
            "detail": "see server logs",
        })


_VALID_MODES = frozenset(_MODE_AT_TYPES)
_CYCLE_MODES = frozenset({"CYCLE"})
_SCHEDULE_MODES = frozenset({"SCHEDULE"})
_TIMER_MODES = frozenset({"TIMER_TO_ON", "TIMER_TO_OFF"})


@mcp_server.tool()
async def set_port_mode(
    device_id: str,
    port: int,
    mode: str,
    dry_run: bool = True,
    cycle_on_seconds: int | None = None,
    cycle_off_seconds: int | None = None,
    schedule_start: str | None = None,
    schedule_end: str | None = None,
    timer_duration_seconds: int | None = None,
) -> str:
    """Switch a port to a specific automation mode.

    All 8 AC Infinity automation modes are supported. Mode-specific parameters
    are required for CYCLE, SCHEDULE, TIMER_TO_ON, and TIMER_TO_OFF modes.
    Uses read-before-write. Defaults to dry_run=True.

    For setting automation targets alongside the mode, prefer the dedicated tools:
    ``set_vpd_automation`` (VPD mode), ``set_temperature_automation`` and
    ``set_humidity_automation`` (AUTO mode).

    Scheduled times run on the clock set in the controller itself, not your phone or
    any other timezone. If you move the controller to a new timezone, your schedules
    won't shift automatically — open the AC Infinity app and update the controller's
    time so your schedule lands at the hour you expect.

    Args:
        device_id: Device code from discover_devices (e.g. "C58ZA").
        port: 1-based port number.
        mode: One of OFF, ON, AUTO, VPD, CYCLE, SCHEDULE, TIMER_TO_ON, TIMER_TO_OFF.
        dry_run: If True (default), returns the payload without writing.
        cycle_on_seconds: CYCLE mode — seconds the port runs per cycle. Required for CYCLE.
        cycle_off_seconds: CYCLE mode — seconds the port is off per cycle. Required for CYCLE.
        schedule_start: SCHEDULE mode — start time as "HH:MM" in device local time.
            Required for SCHEDULE.
        schedule_end: SCHEDULE mode — end time as "HH:MM" in device local time.
            Required for SCHEDULE.
        timer_duration_seconds: TIMER_TO_ON / TIMER_TO_OFF — countdown duration in seconds.
            Required for TIMER_TO_ON and TIMER_TO_OFF.

    Returns:
        JSON with action, device_id, port, mode, dry_run, controller_type, sent,
        and payload (when dry_run=True). On failure returns ``{"error": "..."}``.
    """
    try:
        if port < 1:
            return json.dumps({"error": "port must be a positive integer"})

        mode_upper = mode.upper()
        if mode_upper not in _VALID_MODES:
            valid = ", ".join(sorted(_VALID_MODES))
            return json.dumps({"error": f"Invalid mode {mode!r}. Valid modes: {valid}"})

        if mode_upper in _CYCLE_MODES:
            if cycle_on_seconds is None or cycle_off_seconds is None:
                return json.dumps({
                    "error": "CYCLE mode requires cycle_on_seconds and cycle_off_seconds"
                })
            if cycle_on_seconds < 1 or cycle_off_seconds < 1:
                return json.dumps({"error": "cycle_on_seconds and cycle_off_seconds must be >= 1"})

        if mode_upper in _SCHEDULE_MODES:
            if schedule_start is None or schedule_end is None:
                return json.dumps({
                    "error": "SCHEDULE mode requires schedule_start and schedule_end ('HH:MM')"
                })

        if mode_upper in _TIMER_MODES:
            if timer_duration_seconds is None:
                return json.dumps({
                    "error": f"{mode_upper} mode requires timer_duration_seconds"
                })
            if timer_duration_seconds < 1:
                return json.dumps({"error": "timer_duration_seconds must be >= 1"})

        device, err = await _get_device(device_id, for_write=True)
        if err:
            return err
        assert device is not None

        at_type = _MODE_AT_TYPES[mode_upper]
        updates: dict = {"atType": at_type}

        if mode_upper == "ON":
            # The bare atType=2 (ON) preserves whatever onSpead was previously set.
            # If the port was last left at onSpead=0 (e.g. via a prior set_port_off
            # or a fresh port), switching to ON mode would leave the port running
            # at speed 0 — functionally still off. Match set_port_on by setting a
            # default nonzero speed so "ON" actually turns the port on.
            updates["onSpead"] = 10
        elif mode_upper == "CYCLE":
            updates["activeCycleOn"] = cycle_on_seconds
            updates["activeCycleOff"] = cycle_off_seconds
        elif mode_upper == "SCHEDULE":
            try:
                updates["schedStartTime"] = _parse_schedule_time(schedule_start)
                updates["schedEndtTime"] = _parse_schedule_time(schedule_end)  # API typo
            except ValueError as exc:
                return json.dumps({"error": str(exc)})
        elif mode_upper == "TIMER_TO_ON":
            updates["acitveTimerOn"] = timer_duration_seconds  # API typo: acitve
        elif mode_upper == "TIMER_TO_OFF":
            updates["acitveTimerOff"] = timer_duration_seconds  # API typo: acitve

        write_result = await asyncio.to_thread(
            _client().set_port_mode, device, port, updates, dry_run
        )

        port_name, port_label, port_data = _get_port_label(device, port)

        response: dict = {
            "action": f"set {port_label} mode to {mode_upper}",
            "device_id": device_id,
            "port": port,
            "mode": mode_upper,
            "dry_run": write_result["dry_run"],
            "controller_type": write_result["controller_type"],
            "sent": write_result["sent"],
        }
        if write_result["dry_run"]:
            response["payload"] = write_result["payload"]
        if _is_port_empty(port_data, port, device):
            response["advisory"] = _empty_port_advisory(port_label)
        return json.dumps(response, indent=2)

    except ACInfinityAuthError as e:
        logger.warning("Auth error in set_port_mode (device=%s port=%s): %s", device_id, port, e)
        return json.dumps({
            "error": _AUTH_ERROR_MSG,
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error("API error in set_port_mode (device=%s port=%s): %s", device_id, port, e)
        return json.dumps({"error": "AC Infinity API error", "detail": "see server logs"})
    except ACInfinityAdvanceConflictError:
        port_name = _get_port_name_from_device(device, port)
        dev_id = device.get("devId") if device else None
        return await _build_advance_conflict_response(
            _client(), device_id, dev_id, port, port_name, device=device
        )
    except ACInfinityDeviceError as e:
        logger.warning("Device error in set_port_mode (device=%s port=%s): %s", device_id, port, e)
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("Unexpected error in set_port_mode: %s", e, exc_info=True)
        return json.dumps({
            "error": "Unexpected error",
            "detail": "see server logs",
        })


@mcp_server.tool()
async def apply_grow_stage_template(
    device_id: str,
    port: int,
    stage: str,
    dry_run: bool = True,
) -> str:
    """Apply a grow-stage automation template (VPD + temperature + humidity) in one call.

    Issues a single atomic write that puts the port in VPD mode (atType=8) with the
    stage's VPD midpoint as the active target, and simultaneously stores the stage's
    temperature and humidity thresholds on the controller for fallback when the user
    later switches modes. Defaults to dry_run=True — set dry_run=False to write.

    Stage targets (VPD midpoint used as single target):

    | Stage        | VPD (kPa) | Temp (°C) | Humidity (%) |
    |---|---|---|---|
    | clones       | 1.00      | 22–26     | 70–80        |
    | seedling     | 1.00      | 22–26     | 65–75        |
    | veg          | 1.25      | 20–28     | 50–70        |
    | early_flower | 1.40      | 20–26     | 40–60        |
    | mid_flower   | 1.60      | 18–25     | 35–55        |
    | late_flower  | 1.50      | 18–24     | 30–50        |

    Args:
        device_id: Device code from discover_devices (e.g. "C58ZA").
        port: 1-based port number.
        stage: Growth stage name. One of: clones, seedling, veg, early_flower,
            mid_flower, late_flower.
        dry_run: If True (default), returns the payload without writing.

    Returns:
        JSON with action, device_id, port, stage, dry_run, controller_type, sent,
        per-target summary (vpd/temperature/humidity), and payload (when dry_run=True).
        On failure returns ``{"error": "..."}``.
    """
    if port < 1:
        return json.dumps({"error": "port must be a positive integer"})
    if stage not in STAGE_TARGETS:
        valid = ", ".join(sorted(STAGE_TARGETS.keys()))
        return json.dumps({"error": f"Unknown stage {stage!r} — valid stages: {valid}"})

    targets = STAGE_TARGETS[stage]
    vpd_min, vpd_max = targets["vpd"]
    temp_min, temp_max = targets["temp_c"]
    humi_min, humi_max = targets["humidity"]
    # Compute the 2-dp midpoint via integer math (round-half-up at 2 dp) so the
    # displayed target reflects the stage's actual midpoint (e.g. veg → 1.25, not
    # 1.30). Encoding is round-half-up at 1 dp (×10), matching the VPD field.
    midpoint_x100 = int((vpd_min + vpd_max) * 50 + 0.5)
    target_vpd = midpoint_x100 / 100
    target_vpd_x10 = int(midpoint_x100 / 10 + 0.5)

    try:
        device, err = await _get_device(device_id, for_write=True)
    except ACInfinityAuthError as e:
        logger.warning(
            "Auth error fetching devices in apply_grow_stage_template (device=%s): %s",
            device_id, e,
        )
        return json.dumps({
            "error": _AUTH_ERROR_MSG,
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error(
            "API error fetching devices in apply_grow_stage_template (device=%s): %s",
            device_id, e,
        )
        return json.dumps({"error": "AC Infinity API error", "detail": "see server logs"})
    except Exception as e:
        logger.error(
            "Unexpected error fetching devices in apply_grow_stage_template (device=%s): %s",
            device_id, e, exc_info=True,
        )
        return json.dumps({"error": "Unexpected error", "detail": "see server logs"})
    if err:
        return err
    assert device is not None

    # #288: the grow-stage template sets a VPD target (vpdSettingMode=1). Gate on the port's
    # modeTye capability so a legacy port doesn't get a garbage rail-trigger rule.
    cap_err = _target_capability_error(device, [port])
    if cap_err:
        return cap_err

    # Held on AI+ (#316). This tool writes temp/humidity thresholds alongside
    # atType=8 specifically so they are stored as a fallback for a later switch to
    # AUTO — but on AI+ a field that is not relevant to the port's mode at write
    # time is accepted with code 200 and silently discarded (Quirk 36). The whole
    # point of those fields here is that they are NOT live, so they are exactly
    # what AI+ throws away, and the tool would report a stored fallback that does
    # not exist. It also never writes devLtf/devHtf, so on a °F AI+ the °F pair
    # stays stale regardless. Verifying this needs live writes that put real
    # trigger values on a running port; not done, so not claimed.
    if not dry_run and _ai_plus_write_held(device):
        return _ai_plus_held_error(
            "apply_grow_stage_template", device_id, port,
            "It stores temperature and humidity fallback thresholds that AI+ "
            "controllers accept and then discard, so the confirmation would be "
            "misleading.",
        )

    # Single atomic write: VPD mode active, temp/humidity thresholds stored on the
    # controller for fallback if the user later switches to AUTO mode. Earlier
    # versions issued three separate writes; the temp and humidity writes carried
    # atType=3 (AUTO), which clobbered the VPD mode set by the first write.
    updates = {
        "atType": 8,  # VPD mode active
        "vpdSettingMode": 1,
        "targetVpd": target_vpd_x10,
        "targetVpdSwitch": 1,
        "devLt": int(temp_min + 0.5),
        "devHt": int(temp_max + 0.5),
        "activeLt": 1,
        "activeHt": 1,
        "devLh": int(humi_min + 0.5),
        "devHh": int(humi_max + 0.5),
        "activeLh": 1,
        "activeHh": 1,
    }

    try:
        write_result = await asyncio.to_thread(
            _client().set_port_mode, device, port, updates, dry_run
        )
    except ACInfinityAuthError as e:
        logger.warning(
            "Auth error in apply_grow_stage_template (device=%s port=%s stage=%s): %s",
            device_id, port, stage, e,
        )
        return json.dumps({
            "error": _AUTH_ERROR_MSG,
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error(
            "API error in apply_grow_stage_template (device=%s port=%s stage=%s): %s",
            device_id, port, stage, e,
        )
        return json.dumps({"error": "AC Infinity API error", "detail": "see server logs"})
    except ACInfinityAdvanceConflictError:
        port_name = _get_port_name_from_device(device, port)
        dev_id = device.get("devId") if device else None
        return await _build_advance_conflict_response(
            _client(), device_id, dev_id, port, port_name, device=device
        )
    except ACInfinityDeviceError as e:
        logger.warning(
            "Device error in apply_grow_stage_template (device=%s port=%s stage=%s): %s",
            device_id, port, stage, e,
        )
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("Unexpected error in apply_grow_stage_template: %s", e, exc_info=True)
        return json.dumps({
            "error": "Unexpected error",
            "detail": "see server logs",
        })

    _temp_unit_raw = device.get("deviceInfo", {}).get("unit")
    _unit = _effective_unit(_temp_unit_raw)
    _unit_lbl = _unit_label(_unit)

    response: dict = {
        "action": "apply grow stage template",
        "device_id": device_id,
        "port": port,
        "stage": stage,
        "dry_run": write_result["dry_run"],
        "controller_type": write_result["controller_type"],
        "sent": write_result["sent"],
        "vpd": {"target_kpa": target_vpd},
        "temperature": {
            "min": _to_preferred_temp(temp_min, _unit),
            "max": _to_preferred_temp(temp_max, _unit),
            "unit": _unit_lbl,
        },
        "humidity": {"min_rh": humi_min, "max_rh": humi_max},
    }
    if write_result["dry_run"]:
        response["payload"] = write_result["payload"]

    return json.dumps(response, indent=2)


# ============ Advance Automation Tools ============


@mcp_server.tool()
async def list_advance_automations(device_id: str) -> str:
    """List all Advance Automations configured on a device.

    Advance Automations (also called "programs" in the AC Infinity app) are
    named schedules that can govern one or more ports simultaneously.

    Args:
        device_id: The AC Infinity device code (from discover_devices).

    Returns:
        JSON with ``"automations"`` list. Each entry includes automation_id,
        name, enabled status, and currently_running flag.
        Empty: ``{"device_id": "...", "automations": []}``.
        On failure returns ``{"error": "...", "detail": "..."}``.
    """
    try:
        device, err = await _get_device(device_id)
        if err:
            return err
        assert device is not None

        dev_id = device.get("devId")
        if not dev_id:
            return json.dumps({"error": f"Device {device_id} is missing devId"})

        raw = await asyncio.to_thread(_client().get_advance_automations, str(dev_id))
        grouped = _group_automations(raw)

        automations = [
            {
                "automation_id": g["automation_id"],
                "name": g["name"],
                "enabled": g["enabled"],
                "currently_running": g["run_state"],
            }
            for g in grouped
        ]

        return json.dumps({"device_id": device_id, "automations": automations}, indent=2)

    except ACInfinityAuthError as e:
        logger.warning("Auth error in list_advance_automations: %s", e)
        return json.dumps({
            "error": _AUTH_ERROR_MSG,
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error("API error in list_advance_automations: %s", e)
        return json.dumps({"error": "API error", "detail": "see server logs"})
    except ACInfinityDeviceError as e:
        logger.warning("Device error in list_advance_automations (%s): %s", device_id, e)
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("Unexpected error in list_advance_automations: %s", e, exc_info=True)
        return json.dumps({"error": "Unexpected error", "detail": "see server logs"})


@mcp_server.tool()
async def get_advance_automation(device_id: str, automation_id: str) -> str:
    """Get full detail for a single Advance Automation by ID.

    Args:
        device_id: The AC Infinity device code (from discover_devices).
        automation_id: The automation_id from list_advance_automations.

    Returns:
        JSON with automation detail including name, enabled status, schedule
        (with ``mode``: ``"continuous"`` or ``"scheduled"`` per Quirk 21;
        ``begin_time``/``end_time`` as ``"HH:MM"`` or ``null``; optional
        ``schedule_note`` when scheduled mode has no time window configured),
        port_groups (each entry has ``device_type`` listing the actual port names
        governed by that group, resolved from the ``grouptDevType`` bitmask —
        e.g. ``"Left Fan (Port 5), Right Fan (Port 6)"``, formatted as
        ``"Name (Port N)"`` for each bit set; ``"Unknown"`` when bitmask is 0),
        governed_ports (list of ports this automation controls, decoded from
        the automation's port_group bitmasks), port_resolution status
        ("resolved" or "error"), and
        human_summary (adapts to continuous/scheduled/no-window variants).
        On failure returns ``{"error": "..."}``.
    """
    try:
        adv_id_int = _validate_automation_id(automation_id)
        if adv_id_int is None:
            return json.dumps({"error": "Invalid automation_id format"})

        device, err = await _get_device(device_id)
        if err:
            return err
        assert device is not None

        dev_id = device.get("devId")
        if not dev_id:
            return json.dumps({"error": f"Device {device_id} is missing devId"})

        raw = await asyncio.to_thread(_client().get_advance_automations, str(dev_id))
        grouped = _group_automations(raw)

        found = next((g for g in grouped if g["automation_id"] == adv_id_int), None)
        if found is None:
            return json.dumps({"error": f"Automation {automation_id} not found"})

        name = found["name"]
        enabled = found["enabled"]
        state_str = "enabled" if enabled else "disabled"
        port_groups = found["port_groups"]

        # Build port_name_map once: port number → base name (without "(Port N)" suffix).
        # Used by both port_groups_out (device_type label) and governed_ports.
        port_name_map: dict[int, str] = {}
        try:
            for _p in device.get("deviceInfo", {}).get("ports", []):
                _pnum = _p.get("port")
                if _pnum is None:
                    continue
                _raw = _p.get("portName")
                port_name_map[int(_pnum)] = (
                    _sanitize_api_string(_raw, 64) if _raw else f"Port {_pnum}"
                )
        except (TypeError, ValueError, AttributeError):
            pass  # port_name_map stays partially built; bitmask fallback uses "Port N"

        # Transform port_groups: resolve device_type from grp_dev_type bitmask.
        # Range(8) = 8-port ceiling matching AC Infinity hardware maximum.
        port_groups_out = []
        for pg in port_groups:
            _bitmask = int(pg.get("grp_dev_type") or 0)
            _pg_names = [
                f"{port_name_map.get(_bit + 1, f'Port {_bit + 1}')} (Port {_bit + 1})"
                for _bit in range(8)
                if _bitmask & (1 << _bit)
            ]
            port_groups_out.append({
                "adv_id": pg["adv_id"],
                "on_speed": pg["on_speed"],
                "device_type": ", ".join(_pg_names) if _pg_names else "Unknown",
            })

        # Governed ports from bitmask (uses shared port_name_map).
        # grouptDevType is a bitmask: Port N → bit (N-1). This approach correctly handles
        # multiple simultaneous automations by attributing each port to the automation that
        # explicitly claims it, rather than using the isOpenAutomation flag which becomes
        # ambiguous when more than one automation is active (#149, #150, #152).
        governed_ports: list[dict] = []
        port_resolution: str = "resolved"
        try:
            governed_port_nums: set[int] = set()
            for pg in found.get("port_groups", []):
                bitmask = int(pg.get("grp_dev_type") or 0)
                for bit in range(8):
                    if bitmask & (1 << bit):
                        governed_port_nums.add(bit + 1)

            for pnum in sorted(governed_port_nums):
                raw_label = port_name_map.get(pnum, f"Port {pnum}")
                port_name_display = (
                    f"{raw_label} (Port {pnum})" if raw_label != f"Port {pnum}" else raw_label
                )
                governed_ports.append({
                    "port": pnum,
                    "port_name": port_name_display,
                })
        except (KeyError, TypeError, AttributeError, ValueError):
            governed_ports = []
            port_resolution = "error"

        # Build human-readable summary.
        # onTimeSwitch=0 means the "Continuous 24H/7D" toggle is OFF — the time window
        # applies when real begin/end times are present.
        # onTimeSwitch=1 means the toggle is ON — runs 24/7 regardless of time values.
        on_time_switch = found.get("on_time_switch", 0)
        begin_str = _format_schedule_time(found.get("begin_time"))
        end_str = _format_schedule_time(found.get("end_time"))

        # Scheduled only when toggle is OFF (0) and both formatted times are real values.
        is_scheduled = on_time_switch == 0 and bool(begin_str) and bool(end_str)
        if not is_scheduled:
            begin_str = None
            end_str = None

        _adv_zone_id = device.get("zoneId")
        _tz_label = _adv_zone_id or "unknown"
        _tz_suffix = (
            f" ({_tz_label})" if _adv_zone_id
            else " (timezone unknown — times are device-local)"
        )

        if len(port_groups) == 1:
            speed = port_groups[0]["on_speed"]
            if is_scheduled and begin_str and end_str:
                human_summary = (
                    f"'{name}' runs at speed {speed} from {begin_str} to {end_str}"
                    f"{_tz_suffix}, currently {state_str}."
                )
            else:
                human_summary = (
                    f"'{name}' runs continuously at speed {speed}, "
                    f"currently {state_str}."
                )
        else:
            port_list_str = (
                ", ".join(gp["port_name"] for gp in governed_ports)
                if governed_ports
                else "multiple ports (port list could not be read)"
            )
            schedule_suffix = (
                f" from {begin_str} to {end_str}{_tz_suffix}"
                if is_scheduled and begin_str and end_str
                else ""
            )
            speed_phrase = " at varying speeds" if governed_ports else ""
            human_summary = (
                f"'{name}' controls {port_list_str}{speed_phrase}.{schedule_suffix}"
                f" Currently {state_str}."
            )

        schedule_dict: dict[str, str | None] = {
            "mode": "scheduled" if is_scheduled else "continuous",
            "begin_time": begin_str,
            "end_time": end_str,
            "timezone": _tz_label,
        }

        # Per-rule read parity: one entry per port_group, using the same _decode_rule
        # control wording and window-with-timezone shape the write tools emit. _mode is
        # underscore-prefixed = internal/round-trip only; Claude reads control + window.
        rules_out = []
        for pg in port_groups:
            _bm = int(pg.get("grp_dev_type") or 0)
            _pg_ports = [bit + 1 for bit in range(8) if _bm & (1 << bit)]
            _rule = pg.get("rule") or {}
            rules_out.append({
                "ports": (
                    _ports_label({p: port_name_map.get(p, f"Port {p}") for p in _pg_ports},
                                 _pg_ports)
                    if _pg_ports else "Unknown"
                ),
                "control": _rule.get("control", "unknown rule type"),
                "speed": pg.get("on_speed"),
                "window": _rule_window_str(
                    pg.get("begin_time"), pg.get("end_time"), _tz_label,
                    pg.get("switch_time"),
                ),
                "running": pg.get("run_state", False),
                "_mode": _rule.get("mode", "unknown"),
            })

        return json.dumps({
            "device_id": device_id,
            "automation_id": found["automation_id"],
            "name": name,
            "enabled": enabled,
            "currently_running": found["run_state"],
            "schedule": schedule_dict,
            "port_groups": port_groups_out,
            "governed_ports": governed_ports,
            "port_resolution": port_resolution,
            "rules": rules_out,
            "human_summary": human_summary,
        }, indent=2)

    except ACInfinityAuthError as e:
        logger.warning("Auth error in get_advance_automation: %s", e)
        return json.dumps({
            "error": _AUTH_ERROR_MSG,
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error("API error in get_advance_automation: %s", e)
        return json.dumps({"error": "API error", "detail": "see server logs"})
    except ACInfinityDeviceError as e:
        logger.warning("Device error in get_advance_automation (%s): %s", device_id, e)
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("Unexpected error in get_advance_automation: %s", e, exc_info=True)
        return json.dumps({"error": "Unexpected error", "detail": "see server logs"})


@mcp_server.tool()
async def enable_advance_automation(
    device_id: str,
    automation_id: str,
    dry_run: bool = True,
) -> str:
    """Enable a previously disabled Advance Automation.

    Reads current state before toggling — no-ops if already enabled.
    Defaults to dry_run=True — set dry_run=False to execute.

    IMPORTANT: The AC Infinity API uses a toggle endpoint (updateGroupsIsOn).
    This tool reads the current enabled state first and only calls the API if
    the automation is currently disabled, ensuring the toggle results in enabled.

    Args:
        device_id: The AC Infinity device code (from discover_devices).
        automation_id: The automation_id from list_advance_automations.
        dry_run: If True (default), returns the action plan without executing.

    Returns:
        JSON with action, automation_name, automation_id, dry_run, sent.
        On failure returns ``{"error": "..."}``.
    """
    try:
        adv_id_int = _validate_automation_id(automation_id)
        if adv_id_int is None:
            return json.dumps({"error": "Invalid automation_id format"})

        device, err = await _get_device(device_id, for_write=True)
        if err:
            return err
        assert device is not None

        dev_id = device.get("devId")
        if not dev_id:
            return json.dumps({"error": f"Device {device_id} is missing devId"})

        raw = await asyncio.to_thread(_client().get_advance_automations, str(dev_id))
        grouped = _group_automations(raw)

        found = next((g for g in grouped if g["automation_id"] == adv_id_int), None)
        if found is None:
            return json.dumps({"error": f"Automation {automation_id} not found"})

        name = found["name"]

        if found["enabled"]:
            return json.dumps({
                "info": f"Automation '{name}' is already enabled. No action taken.",
                "dry_run": dry_run,
            })

        if dry_run:
            return json.dumps({
                "action": "enable",
                "automation_name": name,
                "automation_id": found["automation_id"],
                "dry_run": True,
                "sent": False,
            })

        # Live: call once with adv_ids[0]. The API's updateGroupsIsOn endpoint
        # toggles ALL entries sharing the same advName when called with ANY one
        # of their advId values — calling it N times causes N toggles (a no-op
        # for even N). One call is the correct behaviour (Fix 1).
        await asyncio.to_thread(
            _client().enable_advance_automation, str(dev_id), found["adv_ids"][0]
        )

        return json.dumps({
            "action": "enable",
            "automation_name": name,
            "automation_id": found["automation_id"],
            "dry_run": False,
            "sent": True,
        })

    except ACInfinityAuthError as e:
        logger.warning("Auth error in enable_advance_automation: %s", e)
        return json.dumps({
            "error": _AUTH_ERROR_MSG,
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error("API error in enable_advance_automation: %s", e)
        return json.dumps({"error": "API error", "detail": "see server logs"})
    except ACInfinityDeviceError as e:
        logger.warning("Device error in enable_advance_automation (%s): %s", device_id, e)
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("Unexpected error in enable_advance_automation: %s", e, exc_info=True)
        return json.dumps({"error": "Unexpected error", "detail": "see server logs"})


@mcp_server.tool()
async def disable_advance_automation(
    device_id: str,
    automation_id: str,
    dry_run: bool = True,
) -> str:
    """Disable a currently enabled Advance Automation.

    Reads current state before toggling — no-ops if already disabled.
    Defaults to dry_run=True — set dry_run=False to execute.

    Live-tested (2026-05-22): disabling sets governed ports to OFF; re-enabling
    immediately restores ADVANCE mode at automation-defined speeds — no next-trigger
    wait. Use break_out_of_automation for a controlled handoff that also locks
    co-governed ports to safe manual speeds.

    Args:
        device_id: The AC Infinity device code (from discover_devices).
        automation_id: The automation_id from list_advance_automations.
        dry_run: If True (default), returns the action plan without executing.

    Returns:
        JSON with action, automation_name, automation_id, governed_ports (list of
        ``{port, port_name}`` dicts decoded from the automation's grouptDevType bitmasks),
        human_summary, dry_run, sent, and to_restore (natural-language hint
        for re-enabling). On failure returns ``{"error": "..."}``.
    """
    try:
        adv_id_int = _validate_automation_id(automation_id)
        if adv_id_int is None:
            return json.dumps({"error": "Invalid automation_id format"})

        device, err = await _get_device(device_id, for_write=True)
        if err:
            return err
        assert device is not None

        dev_id = device.get("devId")
        if not dev_id:
            return json.dumps({"error": f"Device {device_id} is missing devId"})

        raw = await asyncio.to_thread(_client().get_advance_automations, str(dev_id))
        grouped = _group_automations(raw)

        found = next((g for g in grouped if g["automation_id"] == adv_id_int), None)
        if found is None:
            return json.dumps({"error": f"Automation {automation_id} not found"})

        name = found["name"]
        to_restore = f"Ask me to re-enable '{name}'."

        if not found["enabled"]:
            return json.dumps({
                "info": f"Automation '{name}' is already disabled. No action taken.",
                "dry_run": dry_run,
            })

        # Decode which ports this automation governs from port_group bitmasks.
        # grouptDevType is a port bitmask: Port N → 2^(N-1) (bit N-1 set).
        # Using the bitmask rather than isOpenAutomation flags avoids false positives
        # when multiple automations are simultaneously active.
        _device_ports = device.get("deviceInfo", {}).get("ports", [])
        _port_map = {p["port"]: p for p in _device_ports if p.get("port") is not None}
        _seen: set[int] = set()
        governed_ports: list[dict] = []
        for _pg in found["port_groups"]:
            _bitmask = int(_pg.get("grp_dev_type") or 0)
            for _bit in range(8):
                if _bitmask & (1 << _bit):
                    _pnum = _bit + 1
                    if _pnum not in _seen:
                        _seen.add(_pnum)
                        _p = _port_map.get(_pnum)
                        _raw_nm = _p.get("portName") if _p else None
                        _label = _sanitize_api_string(_raw_nm, 64) if _raw_nm else f"Port {_pnum}"
                        if _label != f"Port {_pnum}":
                            _label = f"{_label} (Port {_pnum})"
                        governed_ports.append({"port": _pnum, "port_name": _label})
        governed_ports.sort(key=lambda x: x["port"])

        _governed_labels = [p["port_name"] for p in governed_ports]
        _governed_str = (
            ", ".join(_governed_labels) if _governed_labels else "its governed ports"
        )
        if dry_run:
            return json.dumps({
                "action": "disable",
                "automation_name": name,
                "automation_id": found["automation_id"],
                "governed_ports": governed_ports,
                "human_summary": (
                    f"Disabling '{name}' will take {_governed_str} off automation control. "
                    "You can re-enable it at any time and all ports will return to automated "
                    "control right away."
                ),
                "dry_run": True,
                "sent": False,
                "to_restore": to_restore,
            })

        # Live: call once with adv_ids[0]. The API's updateGroupsIsOn endpoint
        # toggles ALL entries sharing the same advName on a single call —
        # calling it N times causes N toggles (a no-op for even N). (Fix 1)
        await asyncio.to_thread(
            _client().disable_advance_automation, str(dev_id), found["adv_ids"][0]
        )

        return json.dumps({
            "action": "disable",
            "automation_name": name,
            "automation_id": found["automation_id"],
            "governed_ports": governed_ports,
            "human_summary": (
                f"'{name}' has been disabled. "
                "Re-enabling it will restore automation control immediately."
            ),
            "dry_run": False,
            "sent": True,
            "to_restore": to_restore,
        })

    except ACInfinityAuthError as e:
        logger.warning("Auth error in disable_advance_automation: %s", e)
        return json.dumps({
            "error": _AUTH_ERROR_MSG,
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error("API error in disable_advance_automation: %s", e)
        return json.dumps({"error": "API error", "detail": "see server logs"})
    except ACInfinityDeviceError as e:
        logger.warning("Device error in disable_advance_automation (%s): %s", device_id, e)
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("Unexpected error in disable_advance_automation: %s", e, exc_info=True)
        return json.dumps({"error": "Unexpected error", "detail": "see server logs"})


@mcp_server.tool()
async def create_advance_automation(
    device_id: str,
    name: str,
    on_speed: int,
    port: int,
    off_speed: int = 0,
    begin_time: int | None = None,
    end_time: int | None = None,
    mode: str = "on",
    control_style: str | None = None,
    temp_high_f: int | None = None,
    temp_low_f: int | None = None,
    humidity_high: int | None = None,
    humidity_low: int | None = None,
    temp_target_f: int | None = None,
    humidity_target: int | None = None,
    vpd_target: float | None = None,
    vpd_high: float | None = None,
    vpd_low: float | None = None,
    cycle_on_minutes: int | None = None,
    cycle_off_minutes: int | None = None,
    dry_run: bool = True,
) -> str:
    """Create a new Advance Automation on a device.

    Defaults to dry_run=True for safety. Set dry_run=False to send the automation
    to the device. The port bitmask (grouptDevType) is computed automatically from
    the port number (Port N → 2^(N-1)).

    Scheduled times run on the clock set in the controller itself, not your phone or
    any other timezone. If you move the controller to a new timezone, your schedules
    won't shift automatically — open the AC Infinity app and update the controller's
    time so your schedule lands at the hour you expect.

    Args:
        device_id: The AC Infinity device code (from discover_devices).
        name: Automation name (max 64 chars, control chars stripped).
        on_speed: Fan speed when automation is active (1–10).
        port: 1-based port number the automation should control (1–8).
        off_speed: Minimum fan level when inactive (0–10). Default 0.
        begin_time: Schedule start in minutes since midnight (0–1439). Omit (with end_time)
            for a continuous 24/7 automation — the app's default toggle.
        end_time: Schedule end in minutes since midnight (0–1439). Omit (with begin_time)
            for a continuous 24/7 automation.
        mode: Behavior of the first rule — on (default), off, cycle, auto, vpd.
        control_style: "target" or "trigger" (required for auto and vpd). Inference:
            "hold/keep/maintain at X" -> target; "above/below/turn on at" -> trigger.
        temp_high_f: Turn on above this °F (auto trigger).
        temp_low_f: Turn on below this °F (auto trigger).
        humidity_high: Turn on above this % (auto trigger).
        humidity_low: Turn on below this % (auto trigger).
        temp_target_f: NOT SUPPORTED — holding a temperature setpoint isn't offered by the
            AC Infinity app and renders as thresholds; this is rejected. Use temperature
            high/low thresholds (a trigger), or a VPD target, instead.
        humidity_target: Hold this % (auto target).
        vpd_target: Hold this kPa (vpd target).
        vpd_high: Turn on above this kPa (vpd trigger).
        vpd_low: Turn on below this kPa (vpd trigger).
        cycle_on_minutes: Minutes on (for mode="cycle").
        cycle_off_minutes: Minutes off (for mode="cycle").
        dry_run: If True (default), previews the automation without sending it.
            Set to False to create the automation on the device.

    Returns:
        JSON with action, name, port, port_name, on_speed, min_speed (the port's
        configured minimum speed — used when the automation is inactive), begin_time,
        end_time, schedule_summary, dry_run, sent. Live responses also include
        automation_id (for programmatic chaining — do not surface to the user; use
        ``name`` instead). On failure returns ``{"error": "..."}``.
        When the specified port does not exist on the device, returns
        ``{"error": "Port N not found on device X", "available_ports": [{"port": N,
        "name": "..."}], "suggested_reply": "..."}``. Port names absent or empty in
        the API response fall back to "Port N"; control chars are sanitized.
    """
    try:
        # Validate original name before sanitizing so empty input produces an error
        # rather than the "(unnamed)" fallback (which is reserved for API-returned data).
        if not (name or "").strip():
            return json.dumps({"error": "name must not be empty"})
        clean_name = _sanitize_api_string(name, 64)
        # If sanitizing stripped all printable content (e.g. only control chars), reject it.
        if clean_name == "(unnamed)":
            return json.dumps({"error": "name must not be empty"})
        if not 1 <= on_speed <= 10:
            return json.dumps({"error": "on_speed must be 1–10"})
        if not 0 <= off_speed <= 10:
            return json.dumps({"error": "off_speed must be 0–10"})
        # Optional per-mode behavior for the first rule (default mode="on" = unchanged path).
        # max_level=on_speed preserves the legacy On-mode byte-identity (onSpeed=on_speed).
        rule_kwargs, _rule_err = _validate_rule_inputs(
            mode,
            control_style=control_style, min_level=off_speed, max_level=on_speed,
            temp_high_f=temp_high_f, temp_low_f=temp_low_f,
            humidity_high=humidity_high, humidity_low=humidity_low,
            temp_target_f=temp_target_f, humidity_target=humidity_target,
            vpd_target=vpd_target, vpd_high=vpd_high, vpd_low=vpd_low,
            cycle_on_minutes=cycle_on_minutes, cycle_off_minutes=cycle_off_minutes,
            require_full=mode not in ("on", "off"),
        )
        if _rule_err:
            return _rule_err
        assert rule_kwargs is not None
        # #287: with no schedule given at all, default to the continuous 24/7 toggle (what the
        # app does) rather than a 00:00–23:59 window. An explicit window is honored as-is.
        create_continuous = begin_time is None and end_time is None
        begin_time = 0 if begin_time is None else begin_time
        end_time = 1439 if end_time is None else end_time
        if not (0 <= begin_time <= 1439 or begin_time == _SCHEDULE_ALWAYS_ACTIVE):
            return json.dumps({"error": "begin_time must be 0–1439 or 255 (no schedule)"})
        if not (0 <= end_time <= 1439 or end_time == _SCHEDULE_ALWAYS_ACTIVE):
            return json.dumps({"error": "end_time must be 0–1439 or 255 (no schedule)"})
        # Wrap-around windows (begin > end, e.g. lights-on 09:00→03:00) are VALID and
        # required for the two-window pattern — the controller and add_automation_rule both
        # permit them. Do NOT reject begin > end here (kept consistent with add_automation_rule).
        if (begin_time == _SCHEDULE_ALWAYS_ACTIVE) != (end_time == _SCHEDULE_ALWAYS_ACTIVE):
            return json.dumps({
                "error": (
                    "begin_time and end_time must both be 255 (no schedule) or both be 0–1439"
                )
            })
        if port < 1:
            return json.dumps({"error": "port must be a positive integer"})
        if port > 8:
            return json.dumps({
                "error": (
                    f"Port {port} not found on device {device_id}"
                    " — devices have at most 8 ports"
                ),
                "suggested_reply": (
                    f"Port {port} doesn't exist — this controller has at most 8 ports. "
                    f"Let me look up what's connected on your device."
                ),
            })

        device, err = await _get_device(device_id, for_write=True)
        if err:
            return err
        assert device is not None

        dev_id = device.get("devId")
        if not dev_id:
            return json.dumps({"error": f"Device {device_id} is missing devId"})

        ports_list = device.get("deviceInfo", {}).get("ports", [])
        port_obj = next((p for p in ports_list if p.get("port") == port), None)
        if port_obj is None:
            available = [
                {
                    "port": p.get("port"),
                    "name": (
                        _sanitize_api_string(p.get("portName"), 64)
                        if p.get("portName")
                        else f"Port {p.get('port')}"
                    ),
                }
                for p in ports_list
                if p.get("port") is not None
            ]
            return json.dumps({
                "error": f"Port {port} not found on device {device_id}",
                "available_ports": available,
                "suggested_reply": (
                    f"Port {port} isn't in use on this device. "
                    f"Let me show you what's connected."
                ),
            })

        raw_port_nm = port_obj.get("portName")
        port_name = _sanitize_api_string(raw_port_nm, 64) if raw_port_nm else f"Port {port}"

        # #288: a target/hold rule on a port that doesn't support target mode renders as
        # garbage rail triggers. Gate on the port's modeTye capability (mirrors the app).
        if control_style == "target":
            cap_err = _target_capability_error(device, [port])
            if cap_err:
                return cap_err

        port_settings = await asyncio.to_thread(_client().get_mode_settings, str(dev_id), port)
        min_speed = int(port_settings.get("offSpead", 0))

        disp_begin: str | None
        disp_end: str | None
        if create_continuous:
            schedule_summary = "Runs continuously (24/7)"
            disp_begin = disp_end = "continuous"
        else:
            schedule_summary = _format_schedule_summary(begin_time, end_time)
            disp_begin = _format_schedule_time(begin_time)
            disp_end = _format_schedule_time(end_time)

        if dry_run:
            return json.dumps({
                "action": "create",
                "name": clean_name,
                "port": port,
                "port_name": port_name,
                "on_speed": on_speed,
                "min_speed": min_speed,
                "begin_time": disp_begin,
                "end_time": disp_end,
                "schedule_summary": schedule_summary,
                "dry_run": True,
                "sent": False,
                "note": (
                    "Preview only — nothing sent to your device yet."
                    " Confirm to create this automation."
                ),
            })

        # #300: resolve the port's device-identity portType from existing getGroups rules so an
        # outlet/power-adaptor rule (portType=1) isn't written as a fan rule (portType=0, phantom
        # MIN/MAX speed). Only the fetch is guarded — a getGroups read failure must never block
        # creation (best-effort, fall back to 0), while a resolver bug stays visible. Resolved
        # on the live path only: the dry-run preview does not surface portType, so it needs no
        # read (and gains no new failure surface).
        port_type = 0
        port_type_degraded = False
        try:
            existing_rules = await asyncio.to_thread(
                _client().get_advance_automations, str(dev_id)
            )
        except Exception as exc:
            logger.warning(
                "Could not resolve portType for create (device=%s): %s",
                device_id,
                type(exc).__name__,
            )
            port_type_degraded = True
        else:
            port_type = resolve_port_type(existing_rules, [port])

        # Live path: build full addGroups payload via client helper. mode="on" reproduces
        # the original single-port On-mode payload byte-for-byte (on_speed passed directly).
        # #287: continuous → switch_time 255 (the 24/7 toggle), else the default day schedule.
        build_extra = {k: v for k, v in rule_kwargs.items() if k not in ("min_level", "max_level")}
        if create_continuous:
            build_extra["switch_time"] = _days_to_switchtime(None, True)
        payload = build_groups_payload(
            dev_id=str(dev_id),
            ports=[port],
            clean_name=clean_name,
            begin_time=begin_time,
            end_time=end_time,
            on_speed=on_speed,
            min_level=off_speed,
            port_type=port_type,
            **build_extra,
        )

        result = await asyncio.to_thread(_client().create_advance_automation, str(dev_id), payload)
        adv_id = result.get("advId")
        if not adv_id:
            logger.error("addGroups succeeded but returned no advId for devId=%s", dev_id)
            return json.dumps({
                "error": (
                    f"Automation '{clean_name}' was created on your device and is active, "
                    "but the system could not confirm its tracking ID. "
                    "Check the AC Infinity app — it should appear there."
                ),
                "detail": "see server logs",
            })

        response = {
            "action": "create",
            "automation_id": str(adv_id),
            "automation_id_note": "internal — reference this automation by name to users",
            "name": clean_name,
            "port": port,
            "port_name": port_name,
            "on_speed": on_speed,
            "min_speed": min_speed,
            "begin_time": _format_schedule_time(begin_time),
            "end_time": _format_schedule_time(end_time),
            "schedule_summary": schedule_summary,
            "dry_run": False,
            "sent": True,
        }
        # #300: on the rare fallback branch (getGroups read failed), the device type could not
        # be verified, so the rule may have been created with a default type. Tell the grower to
        # check it — this fires only on failure, so it adds no noise on the normal path.
        if port_type_degraded:
            port_display = (
                f"{port_name} (Port {port})" if port_name != f"Port {port}" else port_name
            )
            response["note"] = (
                f"I created '{clean_name}', but couldn't fully verify {port_display}'s device"
                " type just now. Please open the AC Infinity app and check this rule — if it"
                " shows an unexpected speed range on an on/off device (like a heater or"
                " humidifier), re-create that rule in the app."
            )
        return json.dumps(response)

    except ACInfinityAuthError as e:
        logger.warning("Auth error in create_advance_automation: %s", e)
        return json.dumps({
            "error": _AUTH_ERROR_MSG,
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error("API error in create_advance_automation: %s", e)
        return json.dumps({"error": "API error", "detail": "see server logs"})
    except ACInfinityDeviceError as e:
        logger.warning("Device error in create_advance_automation (%s): %s", device_id, e)
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("Unexpected error in create_advance_automation: %s", e, exc_info=True)
        return json.dumps({"error": "Unexpected error", "detail": "see server logs"})


@mcp_server.tool()
async def delete_advance_automation(
    device_id: str,
    automation_id: str,
    dry_run: bool = True,
) -> str:
    """Delete an Advance Automation from a device.

    If the automation is currently enabled, it is disabled first before deletion.
    Defaults to dry_run=True — set dry_run=False to delete.

    Args:
        device_id: The AC Infinity device code (from discover_devices).
        automation_id: The automation_id from list_advance_automations.
        dry_run: If True (default), returns the action plan without executing.

    Returns:
        JSON with action, automation_name, automation_id, was_enabled, dry_run, sent.
        On failure returns ``{"error": "..."}``.
    """
    try:
        adv_id_int = _validate_automation_id(automation_id)
        if adv_id_int is None:
            return json.dumps({"error": "Invalid automation_id format"})

        device, err = await _get_device(device_id, for_write=True)
        if err:
            return err
        assert device is not None

        dev_id = device.get("devId")
        if not dev_id:
            return json.dumps({"error": f"Device {device_id} is missing devId"})

        raw = await asyncio.to_thread(_client().get_advance_automations, str(dev_id))
        grouped = _group_automations(raw)

        found = next((g for g in grouped if g["automation_id"] == adv_id_int), None)
        if found is None:
            return json.dumps({"error": f"Automation {automation_id} not found"})

        name = found["name"]
        was_enabled = found["enabled"]

        if dry_run:
            return json.dumps({
                "action": "delete",
                "automation_name": name,
                "automation_id": found["automation_id"],
                "was_enabled": was_enabled,
                "dry_run": True,
                "sent": False,
            })

        # If enabled, disable first with a single toggle call (Fix 1: the API
        # toggles all same-name entries on one call — N calls cause N toggles).
        if was_enabled:
            await asyncio.to_thread(
                _client().disable_advance_automation, str(dev_id), found["adv_ids"][0]
            )

        # #302: one whole-program delete removes the entire slot (all rules) in a single
        # isflag=1 call. Do NOT loop over adv_ids — after the first call the program is gone,
        # so a second delByid returns a non-200 code and surfaces a false "API error" even
        # though the delete succeeded. Pass whole_program=True explicitly to make the isflag=1
        # intent self-documenting at the call site.
        await asyncio.to_thread(
            _client().delete_advance_automation,
            str(dev_id),
            found["adv_ids"][0],
            whole_program=True,
        )

        return json.dumps({
            "action": "delete",
            "automation_name": name,
            "automation_id": found["automation_id"],
            "was_enabled": was_enabled,
            "dry_run": False,
            "sent": True,
        })

    except ACInfinityAuthError as e:
        logger.warning("Auth error in delete_advance_automation: %s", e)
        return json.dumps({
            "error": _AUTH_ERROR_MSG,
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error("API error in delete_advance_automation: %s", e)
        return json.dumps({"error": "API error", "detail": "see server logs"})
    except ACInfinityDeviceError as e:
        logger.warning("Device error in delete_advance_automation (%s): %s", device_id, e)
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("Unexpected error in delete_advance_automation: %s", e, exc_info=True)
        return json.dumps({"error": "Unexpected error", "detail": "see server logs"})


def _validate_rule_ports(
    ports: list[int], device: dict, device_id: str
) -> str | None:
    """Validate every port in ``ports`` exists on the device. Returns error_json or None."""
    if not ports:
        return json.dumps({"error": "ports must list at least one port number"})
    ports_list = device.get("deviceInfo", {}).get("ports", [])
    valid_ports = {p.get("port") for p in ports_list if p.get("port") is not None}
    for p in ports:
        if not isinstance(p, int) or p < 1 or p > 8:
            return json.dumps({"error": f"Port {p} is invalid — ports are numbered 1–8"})
        if p not in valid_ports:
            available = [
                {
                    "port": pp.get("port"),
                    "name": (
                        _sanitize_api_string(pp.get("portName"), 64)
                        if pp.get("portName")
                        else f"Port {pp.get('port')}"
                    ),
                }
                for pp in ports_list
                if pp.get("port") is not None
            ]
            return json.dumps({
                "error": f"Port {p} not found on device {device_id}",
                "available_ports": available,
                "suggested_reply": (
                    f"Port {p} isn't in use on this device. Let me show you what's connected."
                ),
            })
    return None


# Friendly write-failure messages, mapped from contained-substring detection on a locally
# held copy of the upstream string (R6 — never echo the upstream text to the client).
_OVERLAP_UPSTREAM_MARKER = "Adv exist"
# Anchored to the "{context} API error {code}: {msg}" client format so a bare "100001"
# appearing elsewhere in the message (e.g. inside a sanitized name) can't false-positive.
_WEDGED_DELETE_UPSTREAM_MARKER = "error 100001"
_OVERLAP_FRIENDLY = (
    "A rule already covers those ports during that window — pick a different time"
    " or update the existing rule."
)
_WEDGED_FRIENDLY = (
    "The controller was busy and may or may not have applied that change. Before trying"
    " again, ask me to list the program's rules so we can see whether it took — retrying"
    " blindly can apply it twice. If it didn't take and the controller stays busy, restart"
    " the controller."
)


def _map_write_failure(exc: Exception) -> str | None:
    """Return a self-authored friendly message for a recognized write failure, else None.

    Detection is by contained-substring on a locally-held copy of the upstream string; the
    upstream text itself is NEVER surfaced to the client (logged at ERROR by the caller).
    """
    local = str(exc)
    if _OVERLAP_UPSTREAM_MARKER in local:
        return _OVERLAP_FRIENDLY
    if _WEDGED_DELETE_UPSTREAM_MARKER in local:
        return _WEDGED_FRIENDLY
    return None


@mcp_server.tool()
async def add_automation_rule(
    device_id: str,
    program_name: str,
    ports: list[int],
    mode: str,
    control_style: str | None = None,
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
    begin_time: int | None = None,
    end_time: int | None = None,
    days: list[str] | str | None = None,
    continuous: bool = False,
    dry_run: bool = True,
) -> str:
    """Add one rule to an existing Advance Automation program.

    A program is a named automation (e.g. "Seedling"); a rule is one schedule window +
    behavior for one or more ports inside that program. This appends a new rule. I'll
    preview the rule before sending it.

    Behavior is chosen with ``mode``:
    - ``off``: keep the port(s) off during the window.
    - ``on``: run the port(s) between ``min_level`` and ``max_level``.
    - ``cycle``: alternate on/off using ``cycle_on_minutes`` / ``cycle_off_minutes``.
    - ``auto``: respond to temperature and/or humidity. Needs ``control_style``:
      - ``target`` — hold a setpoint (``humidity_target``). Holding a temperature
        setpoint is not supported — use temperature thresholds or a VPD target.
      - ``trigger`` — turn on at thresholds (``temp_high_f``/``temp_low_f`` and/or
        ``humidity_high``/``humidity_low``).
    - ``vpd``: respond to VPD. Needs ``control_style``: ``target`` (``vpd_target``) or
      ``trigger`` (``vpd_high``/``vpd_low``).

    Control-style inference (from how the user phrases it): "hold/keep/maintain at X" →
    target; "above/below/when it rises|drops/turn on at" → trigger.

    Example (target): to keep humidity at 65%, use mode='auto', control_style='target',
    humidity_target=65.

    Buffer vs transition (per sensor, choose at most one each): a buffer is a deadband; a
    transition ramps speed across the band. Set ``temp_buffer`` OR ``temp_transition``, etc.

    The schedule window (``begin_time``/``end_time``, minutes since midnight) may wrap past
    midnight. ``days`` accepts day names (mon–sun), "all", "weekdays", or "weekends".
    ``continuous=True`` runs 24/7 (ignores the window).

    Args:
        device_id: The AC Infinity device code (from discover_devices).
        program_name: The name of the existing program to add the rule to.
        ports: One or more 1-based port numbers this rule controls.
        mode: One of off, on, cycle, auto, vpd.
        control_style: "target" or "trigger" (required for auto and vpd).
        min_level: Minimum fan level when the rule is inactive (0–10). Default 0.
        max_level: Maximum/active fan level (0–10). Default 10.
        temp_high_f: Turn on above this °F (auto trigger).
        temp_low_f: Turn on below this °F (auto trigger).
        humidity_high: Turn on above this % (auto trigger).
        humidity_low: Turn on below this % (auto trigger).
        temp_target_f: NOT SUPPORTED — holding a temperature setpoint isn't offered by the
            AC Infinity app and renders as thresholds; this is rejected. Use temperature
            high/low thresholds (a trigger), or a VPD target, instead.
        humidity_target: Hold this % (auto target).
        vpd_target: Hold this kPa (vpd target).
        vpd_high: Turn on above this kPa (vpd trigger).
        vpd_low: Turn on below this kPa (vpd trigger).
        temp_buffer: Temperature deadband °F (auto). Mutually exclusive with temp_transition.
        temp_transition: Temperature ramp band °F (auto).
        humidity_buffer: Humidity deadband % (auto). Mutually exclusive with the transition.
        humidity_transition: Humidity ramp band % (auto).
        vpd_buffer: VPD deadband kPa (vpd). Mutually exclusive with vpd_transition.
        vpd_transition: VPD ramp band kPa (vpd).
        cycle_on_minutes: Minutes on, for a cycle rule.
        cycle_off_minutes: Minutes off, for a cycle rule.
        begin_time: Window start, minutes since midnight (0–1439). Omit for a 24/7 rule.
        end_time: Window end, minutes since midnight (0–1439). Omit for a 24/7 rule.
        days: Day names, "all", "weekdays", or "weekends". Default all days.
        continuous: Run 24/7 (ignores the window). Default False.
        dry_run: If True (default), previews the rule without sending it.

    Returns:
        JSON with action, program_name, the new rule (ports, control, window), and
        sent/preview status. On failure returns ``{"error": "..."}``.
    """
    try:
        # #287: with no schedule given at all, default to continuous 24/7 (the app's default
        # toggle) rather than a 00:00–23:59 scheduled window. An explicit window or days, or
        # continuous=True, is honored as-is.
        if begin_time is None and end_time is None and days is None and not continuous:
            continuous = True
        begin_time = 0 if begin_time is None else begin_time
        end_time = 1439 if end_time is None else end_time
        for label, val in (("begin_time", begin_time), ("end_time", end_time)):
            if not 0 <= val <= 1439:
                return json.dumps({"error": f"{label} must be 0–1439 (minutes since midnight)"})

        kwargs, err = _validate_rule_inputs(
            mode, control_style=control_style, min_level=min_level, max_level=max_level,
            temp_high_f=temp_high_f, temp_low_f=temp_low_f,
            humidity_high=humidity_high, humidity_low=humidity_low,
            temp_target_f=temp_target_f, humidity_target=humidity_target,
            vpd_target=vpd_target, vpd_high=vpd_high, vpd_low=vpd_low,
            temp_buffer=temp_buffer, temp_transition=temp_transition,
            humidity_buffer=humidity_buffer, humidity_transition=humidity_transition,
            vpd_buffer=vpd_buffer, vpd_transition=vpd_transition,
            cycle_on_minutes=cycle_on_minutes, cycle_off_minutes=cycle_off_minutes,
            days=days, continuous=continuous, require_full=True,
        )
        if err:
            return err
        assert kwargs is not None

        device, derr = await _get_device(device_id, for_write=True)
        if derr:
            return derr
        assert device is not None

        dev_id = device.get("devId")
        if not dev_id:
            return json.dumps({"error": f"Device {device_id} is missing devId"})

        perr = _validate_rule_ports(ports, device, device_id)
        if perr:
            return perr

        # #288: gate target/hold on the governed ports' modeTye capability.
        if control_style == "target":
            cap_err = _target_capability_error(device, ports)
            if cap_err:
                return cap_err

        port_name_map = _build_port_name_map(device)
        tz_label = device.get("zoneId") or "device-local time"

        raw = await asyncio.to_thread(_client().get_advance_automations, str(dev_id))
        clean_program = _sanitize_api_string(program_name, 64)
        program_entries = [
            e for e in raw
            if _sanitize_api_string(e.get("advName") or "", 64) == clean_program
        ]
        if not program_entries:
            names = sorted({
                _sanitize_api_string(e.get("advName") or "", 64) for e in raw
            })
            return json.dumps({
                "error": f"No program named '{clean_program}' on device {device_id}",
                "existing_programs": names,
                "suggested_reply": (
                    f"I don't see a program called '{clean_program}'."
                    f" Existing programs: {', '.join(names) if names else 'none'}."
                ),
            })

        # A program is a shared (groupNums, sortType) SLOT; its rules carry sequential
        # subNumber. Append = isFlag=0 + the target slot + next subNumber (existing max + 1).
        slots = {
            (e.get("groupNums"), e.get("sortType")) for e in program_entries
        }
        if len(slots) > 1:
            return json.dumps({
                "error": (
                    f"More than one program named '{clean_program}' on device {device_id}."
                    " Rename them so they're unique, then add the rule to the one you want."
                ),
                "suggested_reply": (
                    f"There's more than one program called '{clean_program}', so I can't tell"
                    " which to add to. Rename them to be unique and we'll try again."
                ),
            })
        group_nums, sort_type = next(iter(slots))
        next_sub = max((e.get("subNumber") or 0) for e in program_entries) + 1
        # #300: resolve the ports' device-identity portType from the getGroups data already
        # fetched above (no extra API call) so an outlet/power-adaptor rule isn't written as a
        # fan rule. Falls back to 0 when no existing rule governs the port (undiscoverable).
        port_type = resolve_port_type(raw, ports)
        payload = build_groups_payload(
            dev_id=str(dev_id), ports=ports, clean_name=clean_program,
            begin_time=begin_time, end_time=end_time,
            is_flag=0, group_nums=group_nums, sort_type=sort_type, sub_number=next_sub,
            port_type=port_type,
            **kwargs,
        )
        decoded = _decode_rule(payload)
        rule_view = {
            "ports": _ports_label(port_name_map, ports),
            "control": decoded["control"],
            "window": _rule_window_str(
                begin_time, end_time, tz_label, payload.get("switchTime")
            ),
            "_mode": decoded["mode"],
        }

        if dry_run:
            return json.dumps({
                "action": f"add rule to '{clean_program}'",
                "program_name": clean_program,
                "rule": rule_view,
                "dry_run": True,
                "sent": False,
                "note": "Preview only — nothing sent yet. Confirm to add this rule.",
            })

        try:
            await asyncio.to_thread(_client().create_advance_automation, str(dev_id), payload)
        except ACInfinityAPIError as e:
            friendly = _map_write_failure(e)
            if friendly is not None:
                logger.error("add_automation_rule write failed (%s): %s", device_id, e)
                return json.dumps({"error": friendly})
            raise
        return json.dumps({
            "action": f"add rule to '{clean_program}'",
            "program_name": clean_program,
            "rule": rule_view,
            "dry_run": False,
            "sent": True,
            "human_summary": (
                f"Added a rule on {_ports_label(port_name_map, ports)}"
                f" ({decoded['control']}) for"
                f" {_rule_window_str(begin_time, end_time, tz_label, payload.get('switchTime'))}."
            ),
        })

    except ACInfinityAuthError as e:
        logger.warning("Auth error in add_automation_rule: %s", e)
        return json.dumps({"error": _AUTH_ERROR_MSG, "detail": "see server logs"})
    except ACInfinityAPIError as e:
        logger.error("API error in add_automation_rule: %s", e)
        return json.dumps({"error": "API error", "detail": "see server logs"})
    except ACInfinityDeviceError as e:
        logger.warning("Device error in add_automation_rule (%s): %s", device_id, e)
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("Unexpected error in add_automation_rule: %s", e, exc_info=True)
        return json.dumps({"error": "Unexpected error", "detail": "see server logs"})


# Per-mode payload field sets — the enumerated keys the update overlay copies from a
# rebuilt signature onto the live body (R6: only the enumerated per-mode field set is
# overlaid; never a wholesale spread of caller input).
_AUTO_SIGNATURE_KEYS = (
    "currentMode", "setSelect", "settingMode",
    "autoLowTempF", "autoHighTempF", "autoLowTempC", "autoHighTempC",
    "autoLowTempSwitch", "autoHighTempSwitch",
    "autoLowHumi", "autoHighHumi", "autoLowHumiSwitch", "autoHighHumiSwitch",
    "targetHumi", "targetTempF", "targetTSwitch", "targetHumiSwitch", "targetVpdSwitch",
    "highVpd", "lowVpd", "highVpdSwitch", "lowVpdSwitch", "targetVpd",
    # Buffer/transition: included so a mode-change rebuild applies newly-supplied values
    # AND clears any stale value carried over from the prior mode (rebuilt default = 0).
    "temperatureFBuff", "temperatureFTrans", "humidityBuff", "humidityTrans",
    "vpdBuff", "vpdTrans",
)
_VPD_SIGNATURE_KEYS = (
    "currentMode", "setSelect", "settingMode",
    "targetVpd", "targetVpdSwitch", "highVpd", "lowVpd", "highVpdSwitch", "lowVpdSwitch",
    "vpdBuff", "vpdTrans", "temperatureFBuff", "temperatureFTrans",
    "humidityBuff", "humidityTrans",
)
_CYCLE_SIGNATURE_KEYS = ("currentMode", "cycleOn", "cycleOff")
_ON_OFF_SIGNATURE_KEYS = ("currentMode",)


def _signature_keys_for(mode: str) -> tuple[str, ...]:
    if mode == "auto":
        return _AUTO_SIGNATURE_KEYS
    if mode == "vpd":
        return _VPD_SIGNATURE_KEYS
    if mode == "cycle":
        return _CYCLE_SIGNATURE_KEYS
    return _ON_OFF_SIGNATURE_KEYS


def _overlay_same_mode(
    body: dict,
    mode: str,
    *,
    temp_high_f: int | None,
    temp_low_f: int | None,
    humidity_high: int | None,
    humidity_low: int | None,
    humidity_target: int | None,
    vpd_target: float | None,
    vpd_high: float | None,
    vpd_low: float | None,
    temp_buffer: int | None,
    temp_transition: int | None,
    humidity_buffer: int | None,
    humidity_transition: int | None,
    vpd_buffer: float | None,
    vpd_transition: float | None,
    cycle_on_minutes: int | None,
    cycle_off_minutes: int | None,
    control_style: str | None,
) -> None:
    """Overlay only the changed sensor/cycle fields onto a same-mode rule body in place.

    Setting a trigger threshold activates its switch (=1); the unused threshold is left
    untouched. Setting a target writes its value (the live body already carries the
    target settingMode/rail shape from the captured signature).
    """
    if mode == "cycle":
        # cycleOn/cycleOff are stored in SECONDS (minutes × 60); see build_groups_payload.
        if cycle_on_minutes is not None:
            body["cycleOn"] = cycle_on_minutes * 60
        if cycle_off_minutes is not None:
            body["cycleOff"] = cycle_off_minutes * 60
        return

    if mode == "auto":
        if temp_high_f is not None:
            body["autoHighTempF"] = temp_high_f
            body["autoHighTempC"] = _RAIL_TEMP_HIGH_C
            body["autoHighTempSwitch"] = 1
        if temp_low_f is not None:
            body["autoLowTempF"] = temp_low_f
            body["autoLowTempC"] = _RAIL_TEMP_LOW_C
            body["autoLowTempSwitch"] = 1
        if humidity_high is not None:
            body["autoHighHumi"] = humidity_high
            body["autoHighHumiSwitch"] = 1
        if humidity_low is not None:
            body["autoLowHumi"] = humidity_low
            body["autoLowHumiSwitch"] = 1
        if humidity_target is not None:
            body["targetHumi"] = humidity_target
        if temp_buffer is not None:
            body["temperatureFBuff"] = temp_buffer
        if temp_transition is not None:
            body["temperatureFTrans"] = temp_transition
        if humidity_buffer is not None:
            body["humidityBuff"] = humidity_buffer
        if humidity_transition is not None:
            body["humidityTrans"] = humidity_transition
        return

    if mode == "vpd":
        if vpd_target is not None:
            # VPD-target mirrors the setpoint into BOTH targetVpd and highVpd (the app's
            # signature; see _apply_vpd / #288). The same-mode overlay must mirror too, or the
            # rule ends up with targetVpd != highVpd — a shape no app rule ever has.
            tgt = round(vpd_target * 10)
            body["targetVpd"] = tgt
            body["highVpd"] = tgt
            body["highVpdSwitch"] = 1
            body["lowVpd"] = 0
            body["lowVpdSwitch"] = 0
        if vpd_high is not None:
            body["highVpd"] = round(vpd_high * 10)
            body["highVpdSwitch"] = 1
        if vpd_low is not None:
            body["lowVpd"] = round(vpd_low * 10)
            body["lowVpdSwitch"] = 1
        if vpd_buffer is not None:
            body["vpdBuff"] = round(vpd_buffer * 10)
        if vpd_transition is not None:
            body["vpdTrans"] = round(vpd_transition * 10)


@mcp_server.tool()
async def update_automation_rule(
    device_id: str,
    program_name: str,
    ports: list[int],
    begin_time: int | None = None,
    end_time: int | None = None,
    mode: str | None = None,
    control_style: str | None = None,
    min_level: int | None = None,
    max_level: int | None = None,
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
    new_begin_time: int | None = None,
    new_end_time: int | None = None,
    days: list[str] | str | None = None,
    continuous: bool | None = None,
    dry_run: bool = True,
) -> str:
    """Edit one existing rule inside an Advance Automation program.

    The rule is identified by ``program_name`` plus the ``ports`` it controls, and
    optionally the window (``begin_time``/``end_time``) when more than one rule on those
    ports exists. Only the fields you supply are changed; everything else is preserved.
    I'll preview the change before sending it.

    To change the rule's behavior type, set ``mode`` (off, on, cycle, auto, vpd) plus the
    matching params (and ``control_style`` for auto/vpd). To change just the schedule
    window, set ``new_begin_time`` / ``new_end_time``. To change speed range, set
    ``min_level`` / ``max_level``.

    Control-style inference: "hold/keep/maintain at X" → target; "above/below/when it
    rises|drops/turn on at" → trigger.

    Args:
        device_id: The AC Infinity device code (from discover_devices).
        program_name: The program the rule belongs to.
        ports: The port number(s) the target rule controls (used to find the rule).
        begin_time: Window-start selector to disambiguate when >1 rule matches.
        end_time: Window-end selector to disambiguate.
        mode: New behavior type (off, on, cycle, auto, vpd). Omit to keep.
        control_style: "target" or "trigger" (when changing mode to auto/vpd).
        min_level: New minimum fan level (0–10).
        max_level: New maximum/active fan level (0–10).
        temp_high_f / temp_low_f: New temperature thresholds °F (auto trigger).
        humidity_high / humidity_low: New humidity thresholds % (auto trigger).
        humidity_target: New auto humidity target setpoint.
        temp_target_f: NOT SUPPORTED (rejected) — use temperature thresholds or a VPD target.
        vpd_target / vpd_high / vpd_low: New VPD target or thresholds (kPa).
        temp_buffer / temp_transition / humidity_buffer / humidity_transition: New
            buffer/transition bands (auto; buffer XOR transition per sensor).
        vpd_buffer / vpd_transition: New VPD buffer/transition band kPa (vpd; XOR).
        cycle_on_minutes / cycle_off_minutes: New cycle on/off minutes.
        new_begin_time: New window start (0–1439).
        new_end_time: New window end (0–1439).
        days: New day spec (day names, "all", "weekdays", "weekends").
        continuous: True runs the rule 24/7 (ignores the window); False stops it running
            24/7, keeping the existing day pattern. Omit to leave the schedule unchanged.
        dry_run: If True (default), previews the change without sending it.

    Returns:
        JSON with action, program_name, the updated rule, and sent/preview status. When
        more than one rule matches, returns a disambiguation list (by window) and asks
        which to edit. On failure returns ``{"error": "..."}``.
    """
    try:
        for label, val in (("new_begin_time", new_begin_time), ("new_end_time", new_end_time)):
            if val is not None and not 0 <= val <= 1439:
                return json.dumps({"error": f"{label} must be 0–1439 (minutes since midnight)"})

        # Detect a no-op update: no change fields supplied at all.
        change_fields = [
            mode, control_style, min_level, max_level, temp_high_f, temp_low_f,
            humidity_high, humidity_low, temp_target_f, humidity_target,
            vpd_target, vpd_high, vpd_low, temp_buffer, temp_transition,
            humidity_buffer, humidity_transition, vpd_buffer, vpd_transition,
            cycle_on_minutes, cycle_off_minutes,
            new_begin_time, new_end_time, days,
        ]
        # `continuous` is bool | None here, so an explicit False ("stop running 24/7") is a
        # real change, distinct from the None default. Only None counts as "not supplied".
        if all(f is None for f in change_fields) and continuous is None:
            return json.dumps({
                "error": "Nothing to change — supply at least one field to update."
            })

        device, derr = await _get_device(device_id, for_write=True)
        if derr:
            return derr
        assert device is not None

        dev_id = device.get("devId")
        if not dev_id:
            return json.dumps({"error": f"Device {device_id} is missing devId"})

        perr = _validate_rule_ports(ports, device, device_id)
        if perr:
            return perr

        port_name_map = _build_port_name_map(device)
        tz_label = device.get("zoneId") or "device-local time"

        raw = await asyncio.to_thread(_client().get_advance_automations, str(dev_id))
        match, disambig, program_rules, ambiguous_program = _resolve_rule(
            raw, program_name, ports, begin_time, end_time, port_name_map, tz_label
        )
        clean_program = _sanitize_api_string(program_name, 64)

        if not program_rules:
            names = sorted({
                _sanitize_api_string(e.get("advName") or "", 64) for e in raw
            })
            return json.dumps({
                "error": f"No program named '{clean_program}' on device {device_id}",
                "existing_programs": names,
            })
        if ambiguous_program is not None:
            return ambiguous_program
        if len(disambig) > 0:
            return json.dumps({
                "error": "More than one rule matches — pick which window to edit.",
                "program_name": clean_program,
                "matching_rules": disambig,
                "suggested_reply": (
                    "There's more than one rule on those ports."
                    " Which window should I edit?"
                ),
            })
        if match is None:
            return json.dumps({
                "error": f"No matching rule in '{clean_program}' for those ports.",
                "program_name": clean_program,
                "existing_rules": program_rules,
            })

        # Determine effective mode for validation: explicit mode, else the rule's current mode.
        current_decoded = _decode_rule(match)
        effective_mode = mode if mode is not None else current_decoded["mode"]

        # Same-mode edit (mode/control_style both omitted): resolve the rule's effective style
        # from the live body (settingMode==1 → target, else trigger) so the target↔trigger
        # mutual-exclusion guards run against it — supplying a target on a trigger rule (or a
        # threshold on a target rule) is rejected with the friendly mutually-exclusive message.
        effective_style = control_style
        if (
            mode is None
            and control_style is None
            and effective_mode in ("auto", "vpd")
        ):
            effective_style = "target" if match.get("settingMode") == 1 else "trigger"

        # #288: gate target/hold on the governed ports' modeTye capability.
        if effective_style == "target":
            cap_err = _target_capability_error(device, ports)
            if cap_err:
                return cap_err

        kwargs, verr = _validate_rule_inputs(
            effective_mode, control_style=effective_style,
            min_level=min_level, max_level=max_level,
            temp_high_f=temp_high_f, temp_low_f=temp_low_f,
            humidity_high=humidity_high, humidity_low=humidity_low,
            temp_target_f=temp_target_f, humidity_target=humidity_target,
            vpd_target=vpd_target, vpd_high=vpd_high, vpd_low=vpd_low,
            temp_buffer=temp_buffer, temp_transition=temp_transition,
            humidity_buffer=humidity_buffer, humidity_transition=humidity_transition,
            vpd_buffer=vpd_buffer, vpd_transition=vpd_transition,
            cycle_on_minutes=cycle_on_minutes, cycle_off_minutes=cycle_off_minutes,
            days=days, continuous=bool(continuous),
            require_full=mode is not None,
        )
        if verr:
            return verr
        assert kwargs is not None

        # Turn OFF continuous: _validate only emits switch_time for days or continuous=True,
        # so an explicit continuous=False (with no days) clears the continuous bit on the
        # live schedule while preserving its day pattern (e.g. 255 → 127, not a reset to all).
        if continuous is False and days is None and "switch_time" not in kwargs:
            kwargs["switch_time"] = (
                int(match.get("switchTime") or 127) & ~_SWITCHTIME_CONTINUOUS_BIT
            )

        # Read-before-write: start from the live rule body, overlay only changed fields.
        body = copy.deepcopy(match)
        if min_level is not None:
            body["offSpeed"] = min_level
        if max_level is not None:
            body["onSpeed"] = max_level
        # One-sided speed update must not invert the rule: cross-check against the live body,
        # since _validate's min<=max check only fires when BOTH levels are supplied at once.
        # Only when a level was actually supplied — never block an unrelated edit on a rule
        # whose live speeds were already inverted by some other writer.
        if (min_level is not None or max_level is not None) and (
            int(body.get("offSpeed") or 0) > int(body.get("onSpeed") or 0)
        ):
            return json.dumps({
                "error": "The minimum speed can't be higher than the maximum speed."
            })
        if new_begin_time is not None:
            body["beginTime"] = new_begin_time
        if new_end_time is not None:
            body["endTime"] = new_end_time
        if "switch_time" in kwargs:
            body["switchTime"] = kwargs["switch_time"]

        if mode is not None:
            # Mode change: rebuild the full per-mode signature so no stale off-mode field
            # remains active, then overlay only the enumerated per-mode signature keys.
            build_kwargs = {k: v for k, v in kwargs.items() if k != "switch_time"}
            rebuilt = build_groups_payload(
                dev_id=str(dev_id), ports=ports,
                clean_name=body.get("advName") or clean_program,
                begin_time=body.get("beginTime", 0),
                end_time=body.get("endTime", 1439),
                min_level=body.get("offSpeed", 0),
                max_level=body.get("onSpeed", 0),
                **{k: v for k, v in build_kwargs.items()
                   if k not in ("mode", "min_level", "max_level")},
                mode=mode,
            )
            for key in _signature_keys_for(mode):
                body[key] = rebuilt[key]
        else:
            # Same-mode edit: overlay only the changed sensor/cycle fields in place.
            _overlay_same_mode(
                body, effective_mode,
                temp_high_f=temp_high_f, temp_low_f=temp_low_f,
                humidity_high=humidity_high, humidity_low=humidity_low,
                humidity_target=humidity_target,
                vpd_target=vpd_target, vpd_high=vpd_high, vpd_low=vpd_low,
                temp_buffer=temp_buffer, temp_transition=temp_transition,
                humidity_buffer=humidity_buffer, humidity_transition=humidity_transition,
                vpd_buffer=vpd_buffer, vpd_transition=vpd_transition,
                cycle_on_minutes=cycle_on_minutes, cycle_off_minutes=cycle_off_minutes,
                control_style=control_style,
            )

        new_decoded = _decode_rule(body)
        rule_view = {
            "ports": _ports_label(port_name_map, ports),
            "control": new_decoded["control"],
            "speed": body.get("onSpeed"),
            "window": _rule_window_str(
                body.get("beginTime"), body.get("endTime"), tz_label, body.get("switchTime")
            ),
            "_mode": new_decoded["mode"],
        }

        if dry_run:
            return json.dumps({
                "action": f"update rule in '{clean_program}'",
                "program_name": clean_program,
                "rule": rule_view,
                "dry_run": True,
                "sent": False,
                "note": "Preview only — nothing sent yet. Confirm to update this rule.",
            })

        # Stale-advId guard: re-resolve from a fresh getGroups at write time.
        raw_now = await asyncio.to_thread(_client().get_advance_automations, str(dev_id))
        match_now, _d, _r, _e = _resolve_rule(
            raw_now, program_name, ports, begin_time, end_time, port_name_map, tz_label
        )
        if match_now is None:
            return json.dumps({
                "error": (
                    f"The rule in '{clean_program}' changed or was removed before I could"
                    " update it. Ask me to list the program's rules and try again."
                ),
            })
        body["advId"] = match_now.get("advId")
        try:
            await asyncio.to_thread(_client().update_advance_automation, str(dev_id), body)
        except ACInfinityAPIError as e:
            friendly = _map_write_failure(e)
            if friendly is not None:
                logger.error("update_automation_rule write failed (%s): %s", device_id, e)
                return json.dumps({"error": friendly})
            raise
        _window_str = _rule_window_str(
            body.get("beginTime"), body.get("endTime"), tz_label, body.get("switchTime")
        )
        return json.dumps({
            "action": f"update rule in '{clean_program}'",
            "program_name": clean_program,
            "rule": rule_view,
            "dry_run": False,
            "sent": True,
            "human_summary": (
                f"Updated the rule on {_ports_label(port_name_map, ports)}"
                f" ({new_decoded['control']}) for {_window_str}."
            ),
        })

    except ACInfinityAuthError as e:
        logger.warning("Auth error in update_automation_rule: %s", e)
        return json.dumps({"error": _AUTH_ERROR_MSG, "detail": "see server logs"})
    except ACInfinityAPIError as e:
        logger.error("API error in update_automation_rule: %s", e)
        return json.dumps({"error": "API error", "detail": "see server logs"})
    except ACInfinityDeviceError as e:
        logger.warning("Device error in update_automation_rule (%s): %s", device_id, e)
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("Unexpected error in update_automation_rule: %s", e, exc_info=True)
        return json.dumps({"error": "Unexpected error", "detail": "see server logs"})


@mcp_server.tool()
async def delete_automation_rule(
    device_id: str,
    program_name: str,
    ports: list[int],
    begin_time: int | None = None,
    end_time: int | None = None,
    dry_run: bool = True,
) -> str:
    """Remove one rule from an Advance Automation program.

    The rule is identified by ``program_name`` plus the ``ports`` it controls, and
    optionally the window (``begin_time``/``end_time``) when more than one rule on those
    ports exists. This removes only that single rule — the rest of the program is left
    in place. I'll preview which rule will be removed before deleting it.

    Args:
        device_id: The AC Infinity device code (from discover_devices).
        program_name: The program the rule belongs to.
        ports: The port number(s) the target rule controls (used to find the rule).
        begin_time: Window-start selector to disambiguate when more than one rule matches.
        end_time: Window-end selector to disambiguate.
        dry_run: If True (default), previews the deletion without performing it.

    Returns:
        JSON with action, program_name, the removed rule, and sent/preview status.
        When more than one rule matches, returns a disambiguation list (by window).
        On failure returns ``{"error": "..."}``.
    """
    try:
        device, derr = await _get_device(device_id, for_write=True)
        if derr:
            return derr
        assert device is not None

        dev_id = device.get("devId")
        if not dev_id:
            return json.dumps({"error": f"Device {device_id} is missing devId"})

        perr = _validate_rule_ports(ports, device, device_id)
        if perr:
            return perr

        port_name_map = _build_port_name_map(device)
        tz_label = device.get("zoneId") or "device-local time"

        raw = await asyncio.to_thread(_client().get_advance_automations, str(dev_id))
        match, disambig, program_rules, ambiguous_program = _resolve_rule(
            raw, program_name, ports, begin_time, end_time, port_name_map, tz_label
        )
        clean_program = _sanitize_api_string(program_name, 64)

        if not program_rules:
            names = sorted({
                _sanitize_api_string(e.get("advName") or "", 64) for e in raw
            })
            return json.dumps({
                "error": f"No program named '{clean_program}' on device {device_id}",
                "existing_programs": names,
            })
        if ambiguous_program is not None:
            return ambiguous_program
        if len(disambig) > 0:
            return json.dumps({
                "error": "More than one rule matches — pick which window to remove.",
                "program_name": clean_program,
                "matching_rules": disambig,
                "suggested_reply": (
                    "There's more than one rule on those ports."
                    " Which window should I remove?"
                ),
            })
        if match is None:
            return json.dumps({
                "error": f"No matching rule in '{clean_program}' for those ports.",
                "program_name": clean_program,
                "existing_rules": program_rules,
            })

        decoded = _decode_rule(match)
        rule_view = {
            "ports": _ports_label(port_name_map, ports),
            "control": decoded["control"],
            "window": _rule_window_str(
                match.get("beginTime"), match.get("endTime"), tz_label, match.get("switchTime")
            ),
            "_mode": decoded["mode"],
        }

        if dry_run:
            return json.dumps({
                "action": f"remove rule from '{clean_program}'",
                "program_name": clean_program,
                "rule": rule_view,
                "dry_run": True,
                "sent": False,
                "note": "Preview only — nothing removed yet. Confirm to remove this rule.",
            })

        # Stale-advId guard: re-resolve from a fresh getGroups at write time.
        raw_now = await asyncio.to_thread(_client().get_advance_automations, str(dev_id))
        match_now, _d, _r, _e = _resolve_rule(
            raw_now, program_name, ports, begin_time, end_time, port_name_map, tz_label
        )
        if match_now is None:
            return json.dumps({
                "error": (
                    f"The rule in '{clean_program}' changed or was removed before I could"
                    " remove it. Ask me to list the program's rules and try again."
                ),
            })
        try:
            # whole_program=False → delByid isflag=0 removes ONLY this rule, not the
            # whole program slot (isflag=1 would nuke every rule in the program).
            await asyncio.to_thread(
                _client().delete_advance_automation, str(dev_id),
                int(match_now["advId"]), whole_program=False,
            )
        except ACInfinityAPIError as e:
            friendly = _map_write_failure(e)
            if friendly is not None:
                logger.error("delete_automation_rule write failed (%s): %s", device_id, e)
                return json.dumps({"error": friendly})
            raise
        return json.dumps({
            "action": f"remove rule from '{clean_program}'",
            "program_name": clean_program,
            "rule": rule_view,
            "dry_run": False,
            "sent": True,
            "human_summary": (
                f"Removed the rule on {_ports_label(port_name_map, ports)} from"
                f" '{clean_program}'."
            ),
        })

    except ACInfinityAuthError as e:
        logger.warning("Auth error in delete_automation_rule: %s", e)
        return json.dumps({"error": _AUTH_ERROR_MSG, "detail": "see server logs"})
    except ACInfinityAPIError as e:
        logger.error("API error in delete_automation_rule: %s", e)
        return json.dumps({"error": "API error", "detail": "see server logs"})
    except ACInfinityDeviceError as e:
        logger.warning("Device error in delete_automation_rule (%s): %s", device_id, e)
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("Unexpected error in delete_automation_rule: %s", e, exc_info=True)
        return json.dumps({"error": "Unexpected error", "detail": "see server logs"})


@mcp_server.tool()
async def break_out_of_automation(
    device_id: str,
    port: int,
    dry_run: bool = True,
    confirm_automation_name: str | None = None,
) -> str:
    """Break a port out of Advance Automation control and lock co-governed ports.

    This is the safe way to manually override a port that is currently under
    Advance Automation. It:

    1. Checks that the port is actually under automation (idempotent: no-ops if not).
    2. Finds the governing automation.
    3. Identifies all other ports in the same automation as the target port (co-ports).
       Only those ports are locked — ports in other automations or empty ports are unaffected.
    4. On dry_run=False:
       a. Disables the automation.
       b. Locks each co-port to its current manual speed (prevents unexpected speed changes).
       c. Leaves the target port free for your manual change.

    Defaults to dry_run=True. For live execution (dry_run=False), you must supply
    ``confirm_automation_name`` matching the automation name (case-insensitive) as a
    safety confirmation.

    Args:
        device_id: The AC Infinity device code (from discover_devices).
        port: The port number you want to break free (1-based).
        dry_run: If True (default), returns the execution plan without making changes.
        confirm_automation_name: Required when dry_run=False — the name of the
            automation to disable, for safety confirmation.

    Returns:
        Dry-run: JSON plan with sequence of steps, co_ports_to_lock, estimated_duration.
        Live: JSON with co_ports_locked and target_port_freed.
        Idempotent: ``{"info": "Port is not currently under automation control."}``
        On failure returns ``{"error": "..."}``.
    """
    try:
        if port < 1 or port > 8:
            return json.dumps({"error": f"port must be between 1 and 8 (got {port})"})

        device, err = await _get_device(device_id, for_write=True)
        if err:
            return err
        assert device is not None

        dev_id = device.get("devId")
        if not dev_id:
            return json.dumps({"error": f"Device {device_id} is missing devId"})

        # Held on AI+ (#316). This tool issues one live write per co-governed port
        # plus the target, and its rollback path re-enables the automation without
        # unwinding co-ports it already switched to manual — leaving those ports
        # pinned manually AND claimed by a re-enabled automation. Those call sites
        # were never among the AI+ refusal branches, so pre-#308 they returned
        # sent=False silently; enabling AI+ writes makes them real. Multi-port
        # partial-failure handling needs its own fix and its own tests.
        if not dry_run and _ai_plus_write_held(device):
            return _ai_plus_held_error(
                "break_out_of_automation", device_id, port,
                "It performs multi-port writes whose partial-failure rollback is "
                "not yet safe on this controller type.",
            )

        # Step 0: Idempotency check — is this port actually under automation?
        port_settings = await asyncio.to_thread(
            _client().get_mode_settings, dev_id, port
        )
        mode_type = port_settings.get("modeType")

        # Get port info from device data for display names.
        ports_data = device.get("deviceInfo", {}).get("ports", [])
        port_info = next((p for p in ports_data if p.get("port") == port), None)
        raw_port_name = port_info.get("portName") if port_info else None
        port_name = _sanitize_api_string(raw_port_name, 64) if raw_port_name else f"Port {port}"

        if mode_type != _ADVANCE_MODE_TYPE:
            _port_display = (
                f"{port_name} (Port {port})" if port_name != f"Port {port}" else port_name
            )
            return json.dumps({
                "info": (
                    f"{_port_display} is not currently under automation control. "
                    "No action taken."
                ),
            })

        # Step 1: Find governing automation.
        raw_automations = await asyncio.to_thread(
            _client().get_advance_automations, str(dev_id)
        )
        grouped = _group_automations(raw_automations)

        # Find the automation whose bitmask covers the target port.
        automation = _find_governing_automation(grouped, port)

        if automation is None:
            # Ghost state: no active automation covers this port — modeType=15 flag is stale
            # from a deleted or fully-disabled automation. Either way the port is not under
            # active automation control, so the correct response is a no-op info message.
            _port_display = (
                f"{port_name} (Port {port})" if port_name != f"Port {port}" else port_name
            )
            return json.dumps({
                "info": (
                    f"{_port_display} is not currently under active automation control. "
                    "No action taken."
                ),
            })

        auto_name = automation["name"]
        auto_id = automation["automation_id"]
        adv_ids = automation["adv_ids"]

        # Step 2: Identify co-governed ports from the governing automation's bitmasks.
        # Only ports in the same automation are locked — ports in other automations or
        # empty/disconnected ports (via _is_port_empty) are excluded.
        automation_port_nums: set[int] = set()
        for pg in automation.get("port_groups", []):
            bitmask = int(pg.get("grp_dev_type") or 0)
            for bit in range(8):
                if bitmask & (1 << bit):
                    automation_port_nums.add(bit + 1)
        automation_port_nums.discard(port)  # exclude the target port

        co_ports: list[dict] = []
        for p_data in ports_data:
            p_num = p_data.get("port")
            if p_num not in automation_port_nums:
                continue
            if _is_port_empty(p_data, p_num, device):  # empty/disconnected — skip
                continue
            raw_p_name = p_data.get("portName")
            p_name = _sanitize_api_string(raw_p_name, 64) if raw_p_name else f"Port {p_num}"
            current_speed = p_data.get("speak", 0)
            co_ports.append({
                "port": p_num,
                "port_name": p_name,
                "current_speed": current_speed,
            })

        # Estimate: 1.5s rate limit per write; 1 disable + len(co_ports) locks.
        n_writes = 1 + len(co_ports)
        estimated_duration = round(n_writes * 1.5, 1)

        sequence = [
            {"step": 1, "action": f"disable automation '{auto_name}'"},
        ]
        for i, cp in enumerate(co_ports, start=2):
            lock_mode = "ON" if cp["current_speed"] > 0 else "OFF"
            _cp_display = (
                f"{cp['port_name']} (Port {cp['port']})"
                if cp['port_name'] != f"Port {cp['port']}"
                else cp['port_name']
            )
            sequence.append({
                "step": i,
                "action": (
                    f"lock {_cp_display} to "
                    f"current speed {cp['current_speed']} (manual {lock_mode})"
                ),
            })
        sequence.append({
            "step": len(sequence) + 1,
            "action": "target port freed from automation — apply your change manually",
        })

        _target_label = (
            f"{port_name} (Port {port})" if port_name != f"Port {port}" else f"Port {port}"
        )
        _co_label_parts = [
            f"{cp['port_name']} (Port {cp['port']})"
            if cp["port_name"] != f"Port {cp['port']}"
            else f"Port {cp['port']}"
            for cp in co_ports
        ]
        _co_str = ", ".join(_co_label_parts) if _co_label_parts else ""
        if _co_str:
            _human_co = (
                f" The other ports in this automation — {_co_str} — "
                "will be locked to their current speeds."
            )
        else:
            _human_co = ""
        _bo_human_summary = (
            f"This will disable the '{auto_name}' automation.{_human_co} "
            f"{_target_label} will be freed for manual control. "
            "You can re-enable the automation at any time — "
            "all ports will return to automated control right away."
        )

        if dry_run:
            return json.dumps({
                "action": f"release {_target_label} from '{auto_name}' automation",
                "dry_run": True,
                "automation_name": auto_name,
                "automation_id": auto_id,
                "target_port": port,
                "target_port_name": port_name,
                "estimated_duration_seconds": estimated_duration,
                "human_summary": _bo_human_summary,
                "sequence": sequence,
                "co_ports_to_lock": [
                    {
                        "port_name": cp["port_name"],
                        "port": cp["port"],
                        "current_speed": cp["current_speed"],
                        "lock_mode": "ON" if cp["current_speed"] > 0 else "OFF",
                    }
                    for cp in co_ports
                ],
            }, indent=2)

        # Live execution.
        if confirm_automation_name is None:
            return json.dumps({
                "error": (
                    f"Please confirm which automation to disable. "
                    f"Tell me '{auto_name}' to proceed."
                ),
            })

        if len(confirm_automation_name) > 256:
            return json.dumps(
                {"error": "The automation name you provided is too long (max 256 characters)."}
            )

        if confirm_automation_name.casefold() != auto_name.casefold():
            safe_confirm = _sanitize_api_string(confirm_automation_name or "", 64)
            return json.dumps({
                "error": (
                    f"'{safe_confirm}' doesn't match the governing automation '{auto_name}'. "
                    "Please use the exact automation name."
                ),
            })

        device_lock = _get_device_lock(device_id)
        if device_lock.locked():
            return json.dumps({
                "conflict": "SEQUENCE_IN_PROGRESS",
                "device_id": device_id,
                "message": (
                    "Another break_out_of_automation is already in progress for this device."
                ),
            })

        async with device_lock:
            # Step A: Disable the automation with a single toggle call (Fix 1: the
            # API toggles all same-name entries on one call — N calls cause N toggles).
            try:
                await asyncio.to_thread(
                    _client().disable_advance_automation, str(dev_id), adv_ids[0]
                )
            except Exception as disable_exc:
                logger.error(
                    "break_out_of_automation failed at disable step "
                    "(device=%s): %s", device_id, disable_exc,
                )
                return json.dumps({
                    "error": "Failed to disable automation",
                    "failed_step": "disable_automation",
                    "detail": "see server logs",
                })

            # Step B: Wait briefly for the AC Infinity cloud to propagate the disable, then
            # re-fetch device data. Two guards in _set_port_mode_inner check ADVANCE state:
            # Guard 1 reads isOpenAutomation from devInfoListAll (the device dict we pass);
            # Guard 2 reads modeType/isOpenAutomation from getdevModeSettingList (a fresh API
            # call inside the write layer). Both return stale state immediately after the
            # disable — the sleep allows both to settle before the co-port lock writes.
            await asyncio.sleep(2.0)
            _invalidate_device_cache()
            device, err = await _get_device(device_id, for_write=True)
            if err:
                logger.error(
                    "break_out_of_automation could not re-fetch device after disable "
                    "(device=%s) — rollback: re-enabling automation", device_id,
                )
                try:
                    await asyncio.to_thread(
                        _client().enable_advance_automation, str(dev_id), adv_ids[0]
                    )
                except Exception:
                    pass
                return json.dumps({
                    "error": "Automation disabled but re-fetch failed — automation re-enabled",
                    "detail": "see server logs",
                })
            assert device is not None

            # Step C: Lock co-governed ports to their current speeds.
            co_ports_locked: list[dict] = []
            failed_port = None
            for cp in co_ports:
                cp_num = cp["port"]
                cp_speed = cp["current_speed"]
                try:
                    if cp_speed > 0:
                        lock_updates = {"atType": 2, "onSpead": cp_speed}  # ON at current speed
                        lock_mode_str = "ON"
                    else:
                        lock_updates = {"atType": 1, "onSpead": 0}  # OFF
                        lock_mode_str = "OFF"
                    await asyncio.to_thread(
                        _client().set_port_mode, device, cp_num, lock_updates, False
                    )
                    co_ports_locked.append({
                        "port_name": cp["port_name"],
                        "port": cp_num,
                        "locked_to_speed": cp_speed,
                        "locked_to_mode": lock_mode_str,
                    })
                except Exception as lock_exc:
                    logger.error(
                        "break_out_of_automation failed locking port %s "
                        "(device=%s): %s", cp_num, device_id, lock_exc,
                    )
                    failed_port = cp_num
                    break

            if failed_port is not None:
                # Attempt rollback: re-enable the automation with a single toggle call
                # (Fix 1: same API behaviour as disable — one call toggles all entries).
                rollback_succeeded = False
                try:
                    await asyncio.to_thread(
                        _client().enable_advance_automation, str(dev_id), adv_ids[0]
                    )
                    rollback_succeeded = True
                except Exception as rb_exc:
                    logger.error(
                        "break_out_of_automation rollback failed (device=%s): %s",
                        device_id, rb_exc,
                    )
                return json.dumps({
                    "error": f"Failed to lock co-port {failed_port}",
                    "failed_step": f"lock_port_{failed_port}",
                    "rollback_attempted": True,
                    "rollback_succeeded": rollback_succeeded,
                    "recovery_steps": [
                        f"Manually re-enable automation '{auto_name}' via the AC Infinity app.",
                        "To restore automation control, ask me to re-enable the automation.",
                    ],
                })

            # Step D: Lock the target port to its automation-controlled speed so the grower
            # sees no unexpected state change. The port is now under manual control; they
            # can adjust from this baseline.
            # Use the governing automation's on_speed (not ports_data["speak"]) because
            # speak stores the last manually-set speed, not the automation-controlled speed.
            governing_pg = _find_governing_port_group(automation, port)
            target_speed = governing_pg.get("on_speed", 0) if governing_pg else 0
            if target_speed > 0:
                target_lock_updates: dict = {"atType": 2, "onSpead": target_speed}
            else:
                target_lock_updates = {"atType": 1, "onSpead": 0}
            try:
                await asyncio.to_thread(
                    _client().set_port_mode, device, port, target_lock_updates, False
                )
            except Exception as tgt_exc:
                logger.warning(
                    "break_out_of_automation: could not lock target port %s to speed %s "
                    "(device=%s): %s", port, target_speed, device_id, tgt_exc,
                )
                # Non-fatal: automation is disabled and port is free from automation control
                # even if the baseline speed lock failed.

        _target_speed_note = (
            f" at speed {target_speed}" if target_speed > 0 else " (currently off)"
        )
        return json.dumps({
            "action": f"release {_target_label} from '{auto_name}' automation",
            "dry_run": False,
            "automation_name": auto_name,
            "automation_id": auto_id,
            "co_ports_locked": co_ports_locked,
            "target_port": port,
            "target_port_freed": True,
            "target_port_locked_to_speed": target_speed,
            "human_summary": (
                f"Released {_target_label} from the '{auto_name}' automation"
                f"{_target_speed_note}. You can now control it manually."
            ),
            "sent": True,
        }, indent=2)

    except ACInfinityAuthError as e:
        logger.warning("Auth error in break_out_of_automation: %s", e)
        return json.dumps({
            "error": _AUTH_ERROR_MSG,
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error("API error in break_out_of_automation: %s", e)
        return json.dumps({"error": "API error", "detail": "see server logs"})
    except ACInfinityDeviceError as e:
        logger.warning("Device error in break_out_of_automation (%s): %s", device_id, e)
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("Unexpected error in break_out_of_automation: %s", e, exc_info=True)
        return json.dumps({"error": "Unexpected error", "detail": "see server logs"})


# ============ MCP Prompts ============


@mcp_server.prompt()
def vpd_troubleshooting() -> str:
    """Step-by-step guide for diagnosing and fixing VPD issues."""
    return """\
## VPD Troubleshooting Guide

**What is VPD?**
Vapour Pressure Deficit (VPD) is the difference between the moisture in the air and
how much moisture the air can hold at saturation. It drives transpiration — too high
stresses the plant, too low causes wet conditions and disease risk.

**Step 1 — Check your current VPD**
Ask me to check the current readings for your device and look at the `vpd` field (kPa).
Or ask me to check VPD drift against your grow stage to compare against a growth stage target.

**Step 2 — Diagnose HIGH VPD (above target range)**
High VPD means the air is too dry. The plant is losing water faster than it can absorb it.
Signs: wilting, leaf curl, slow growth.

Fixes (choose one or both):
- **Lower temperature** — ask me to set temperature automation on the port
  to drop the max threshold 1–2°C.
- **Raise humidity** — ask me to set humidity automation on the port
  to increase the lower humidity bound.
- **Use VPD mode** — ask me to enable VPD mode on the port to let the
  controller manage VPD directly. Start with the midpoint of your stage range.

**Step 3 — Diagnose LOW VPD (below target range)**
Low VPD means the air is too humid. Stomata close, CO2 uptake drops, mould risk rises.
Signs: soft growth, mould, bud rot risk in flower.

Fixes (choose one or both):
- **Raise temperature** — ask me to raise the temperature minimum threshold.
- **Lower humidity** — ask me to lower the maximum humidity threshold.
- Ask me to increase the fan speed on that port.

**Target ranges by stage**
| Stage | VPD (kPa) | Temp (°F) |
|---|---|---|
| clones / seedling | 0.8–1.2 | 72–79 |
| veg | 1.0–1.5 | 68–82 |
| early flower | 1.0–1.8 | 68–79 |
| mid flower | 1.2–2.0 | 64–77 |
| late flower | 1.2–1.8 | 64–75 |

**One-click solution:** Ask me to apply a grow stage template — I'll show you the planned
settings before making any changes.
"""


@mcp_server.prompt()
def new_grower_setup() -> str:
    """Onboarding guide: from first connection to automated grow environment."""
    return """\
## New Grower Setup Guide

Welcome! Here is how to connect your AC Infinity controller and get your environment
dialled in with automation in four steps.

**Step 1 — Discover your devices**
Ask me: "Show me my AC Infinity devices." I'll list every controller on your account with
its device code, current readings, and port states. You'll need the device code
(e.g. "C58ZA") for every other request.

**Step 2 — Check current readings**
Ask me to show the current readings for your device. I'll report live temperature, humidity,
VPD, and the current speed of each port. Verify the numbers match your physical environment
before making any changes.

**Step 3 — Apply a grow stage template**
Ask me to apply a grow stage template to a port — for example, "Set Port 1 on Veg Tent to
veg stage settings." I'll show you the exact VPD target, temperature range, and humidity
range before writing anything. Confirm when you're ready and I'll apply it.
Available stages: `clones`, `seedling`, `veg`, `early_flower`, `mid_flower`, `late_flower`.

**Step 4 — Check your environment health score**
Ask me for an environment health report on your device. I'll return a 0–100 score and
letter grade (A–F) with a per-metric breakdown and the single most impactful recommendation.
Run this after applying automation to confirm the environment is responding.

**Tip:** Ask me to check VPD drift any time you want a quick status check
(OK / HIGH / LOW) without the full health report.

**Tip:** If anything looks wrong, see the `vpd_troubleshooting` prompt for step-by-step
diagnosis and fix instructions.
"""


@mcp_server.prompt()
def environment_alert_interpretation() -> str:
    """Guide to interpreting alerts from check_vpd_drift and get_environment_health."""
    return """\
## Environment Alert Interpretation Guide

### check_vpd_drift — Status Field

Asking me to check VPD drift returns a `status` field:

| Status | Meaning | Typical action |
|---|---|---|
| `OK` | VPD is within the target range for the stage | None needed |
| `HIGH` | VPD above target — air too dry | Lower temp or raise humidity; see vpd_troubleshooting |
| `LOW` | VPD below target — air too humid | Raise temp or lower humidity; increase airflow |

The response also includes `current_vpd` (kPa), `target_range` [min, max], and `deviation`
(how far outside the range). A deviation of 0 means exactly on target; a positive value
means above the upper bound; negative means below the lower bound.

---

### get_environment_health — Score and Grade

Asking me for an environment health report returns a composite score:

| Grade | Score | Interpretation |
|---|---|---|
| A | 90–100 | Excellent — environment is dialled in |
| B | 80–89 | Good — minor deviation, stable growth |
| C | 70–79 | Fair — worth investigating; one metric is off |
| D | 60–69 | Poor — environment stress likely; intervene soon |
| F | 0–59 | Critical — significant stress; act immediately |

**Score weighting:** VPD 40% + Temperature 30% + Humidity 30%.

VPD has the highest weight because it integrates both temperature and humidity into a
single stress indicator. A D or F on VPD alone can drag an otherwise healthy environment
into the C/D range.

**top_recommendation** — the single most impactful action to improve the score. Always
start here. Common recommendations:
- "Lower temperature 1–2°C to bring VPD into target range"
- "Increase humidity by 5–10% RH to reduce VPD"
- "Temperature is the primary driver of health score — adjust min/max thresholds"

**Per-metric scores** (vpd_score, temp_score, humidity_score) are each 0–100. A score
below 60 on any metric is the most likely root cause of a low overall score.

---

### Quick Action Reference

| Situation | What to ask |
|---|---|
| VPD HIGH or LOW | Ask me to set VPD automation, temperature automation, or humidity automation |
| Health score C or below | Follow `top_recommendation`; ask me to apply a grow stage template |
| Unsure where to start | See the `vpd_troubleshooting` prompt |
| First time setup | See the `new_grower_setup` prompt |
"""



def main() -> None:  # pragma: no cover
    email = os.getenv("AC_INFINITY_EMAIL")
    password = os.getenv("AC_INFINITY_PASSWORD")

    if not email or not password:
        # The server reads env vars directly; it does not auto-load .env.
        # Set via your MCP client's env config (Claude Desktop / Cline / Codex
        # config block) or export them in your shell before launching.
        logger.error(
            "Missing AC_INFINITY_EMAIL or AC_INFINITY_PASSWORD — "
            "set them in your MCP client config or shell environment"
        )
        sys.exit(1)

    setup(ACInfinityClient(email, password))

    async def _run() -> None:
        logger.info("AC Infinity MCP Server ready (stdio)")
        await mcp_server.run_stdio_async()

    asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    main()
