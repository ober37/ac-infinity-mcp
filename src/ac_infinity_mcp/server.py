import asyncio
import calendar
import dataclasses
import json
import logging
import os
import re
import sys
import unicodedata
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server.fastmcp import FastMCP

from ac_infinity_mcp.analytics import (
    _ZERO_LOAD_DEV_TYPES,
    STAGE_TARGETS,
    ActivityReport,
    build_activity_report,
    calculate_health_score,
    detect_trends,
)  # noqa: E402 (ruff isort: _ZERO_LOAD_DEV_TYPES sorted before ActivityReport below)
from ac_infinity_mcp.client import ACInfinityClient
from ac_infinity_mcp.schema import (
    _ADVANCE_MODE_TYPE,
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
    emitted once the logger is configured. P3-F007 (Cycle 1); extracted into
    a function for direct testability in Cycle 2 (P2-C2-F003).
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


# Credential markers redacted in formatted log output. Tuple of (field_name,
# value_pattern) — the field-name alternation matches the marker token, and the
# value pattern is intentionally permissive to handle:
#   field=value        (positional log args, e.g. "token=abc123")
#   'field': 'value'   (dict repr from logger.debug("%s", payload_dict))
#   "field": "value"   (json.dumps output)
#   field=value with spaces in the value (greedy until a structural terminator)
#   url?userId=value   (query-string credentials in HTTPError __str__)
_FIELD_PATTERN = re.compile(
    r"(appPasswordl|appPassword|AC_INFINITY_PASSWORD|appEmail|token|appId|userId)"
    r"(['\"]?\s*[:=]\s*)"
    # Value: either quoted (any chars until matching quote) or unquoted (any
    # chars until a structural terminator). The terminator set covers JSON
    # delimiters (newline, comma, closing brace/bracket) AND URL/query
    # separators (`&`, `;`) so URL-query credentials don't swallow trailing
    # params — `?userId=tok&other=val` redacts only the token, not `&other=val`
    # (Cycle 3 P1-C3-F002).
    #
    # A naked whitespace inside a value (e.g. password with embedded space) is
    # preserved as part of the value so we never under-redact a Cycle 2
    # P3-C2-F004-class leak. The remaining edge case — two adjacent credential
    # markers in space-separated positional form on the same log line
    # (P1-C3-F001) — does not occur in any production log site in this server
    # and is documented as an accepted trade-off.
    r"(?:(['\"])([^'\"]*)\3|([^\n,}\];&]+))",
    re.IGNORECASE,
)


def _redact_credentials(text: str) -> str:
    """Redact credential-field values from any text. Idempotent."""
    if not text:
        return text

    def _sub(match: re.Match[str]) -> str:
        field = match.group(1)
        sep = match.group(2)
        quote = match.group(3)
        if quote is not None:
            return f"{field}{sep}{quote}<redacted>{quote}"
        return f"{field}{sep}<redacted>"

    return _FIELD_PATTERN.sub(_sub, text)


class _CredentialRedactingFormatter(logging.Formatter):
    """Formatter that scrubs credential markers from both the message line AND
    any exception text (the traceback emitted by ``exc_info=True``).

    Switched from a logging.Filter to a Formatter subclass during Cycle 2:
    Filter only sees ``record.msg`` and cannot scrub the post-formatExc text,
    which is what every ``logger.error(..., exc_info=True)`` site emits. The
    formatter wraps both surfaces. Defense in depth: every existing logger.*
    call site is audited clean; the formatter prevents future leaks.

    Tracks the lineage of Cycle 1 P3-F006 / P3-F019 and Cycle 2
    P1-C2-F001 / P1-C2-F002 / P3-C2-F001 / P3-C2-F002 / P3-C2-F004.
    """

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        return _redact_credentials(formatted)

    def formatException(self, ei: object) -> str:  # type: ignore[override]
        return _redact_credentials(super().formatException(ei))  # type: ignore[arg-type]


def _install_credential_redactor(target_logger: logging.Logger | None = None) -> None:
    """Attach the credential-redacting formatter to every handler on the root logger.

    Filters on logger objects (vs handlers) skip records propagated up from
    child loggers — Python's logging design. Attaching at the handler layer
    means every record emitted to a sink (stderr, file) passes through the
    redactor regardless of origin logger. Also called from tests after they
    add their own handlers.
    """
    target = target_logger or logging.getLogger()
    for handler in target.handlers:
        # Preserve any existing format string the operator may have configured.
        existing_fmt = handler.formatter._fmt if handler.formatter else None  # type: ignore[union-attr]
        handler.setFormatter(_CredentialRedactingFormatter(existing_fmt))


_install_credential_redactor()

mcp_server = FastMCP(name="ac-infinity-mcp")

# Initialized at startup via main()
aci_client: ACInfinityClient | None = None


def _client() -> ACInfinityClient:
    """Return the initialized client; raises RuntimeError if main() was not called."""
    if aci_client is None:
        raise RuntimeError("AC Infinity client not initialized — call main() first")
    return aci_client


# ============ Advance Automation Helpers ============

# Per-device async locks for break_out_of_automation sequencing.
# Prevents concurrent break-out operations on the same device from interleaving
# the disable + port-lock steps (a race could partially apply state).
_device_locks: dict[str, asyncio.Lock] = {}


def _get_device_lock(device_id: str) -> asyncio.Lock:
    """Return (creating if absent) the per-device async lock."""
    if device_id not in _device_locks:
        _device_locks[device_id] = asyncio.Lock()
    return _device_locks[device_id]


_AUTOMATION_ID_RE = re.compile(r"^[1-9]\d{0,19}$")


def _validate_automation_id(automation_id: str) -> int | None:
    """Validate that automation_id is a pure integer string. Returns int or None."""
    if _AUTOMATION_ID_RE.match(automation_id or ""):
        return int(automation_id)
    return None


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


# ============ MCP Tools ============

@mcp_server.tool()
async def discover_devices() -> str:
    """
    Discover all AC Infinity devices from the cloud API.
    Returns device IDs, names, and online status.
    Use this to find device_ids for use in other tools.

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
            "error": "Authentication failed — check AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD",
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
              "external_sensors": []
            }

        Temperature and timestamp use the device's own unit preference and timezone
        (from ``deviceInfo.unit`` and ``zoneId`` in the API response). Devices
        without a configured timezone fall back to UTC.
        ``external_sensors`` excludes phantom entries (API-reported sensor slots
        with no physical hardware connected — see API Quirk 20).
        ``plug_status`` is only present on a port entry when no current is detected,
        the port is not running (speed 0 and no load), **and the port still has its
        default name** (``"Port N"``). Custom-named ports are assumed to have a device
        intentionally connected — ``loadState=0`` alone cannot distinguish "nothing
        plugged in" from "device is off" for on/off devices. This matches the signal
        used in ``get_port_status``.
        On failure returns ``{"error": "...", "detail": "..."}``.
    """
    try:
        devices = await asyncio.to_thread(_client().get_devices)

        device = next((d for d in devices if d.get("devCode") == device_id), None)
        if not device:
            return json.dumps({"error": f"Device {device_id} not found"})

        parsed = _client().parse_device_data(device)
        tz = _effective_tz(parsed.get("zone_id"))
        unit = _effective_unit(parsed.get("temp_unit_raw"))

        _temp_val = _to_preferred_temp(parsed.get("temperature_c", 0.0), unit)
        _unit_lbl = _unit_label(unit)
        _humid = parsed.get("humidity")
        _vpd = parsed.get("vpd")
        _ts = _utc_iso_to_local(parsed.get("timestamp"), tz)
        _safe_name = _sanitize_api_string(parsed.get("device_name"), 64) or "Device"
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
            "human_summary": (
                f"{_safe_name}: {_temp_val}{_unit_lbl}, {_humid}% RH, VPD {_vpd} kPa. "
                f"Reading from {_ts}."
            ),
        }

        return json.dumps(output, indent=2)

    except ACInfinityAuthError as e:
        logger.warning("Auth error in get_device_reading: %s", e)
        return json.dumps({
            "error": "Authentication failed — check AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD",
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

        devices = await asyncio.to_thread(_client().get_devices)

        device = next((d for d in devices if d.get("devCode") == device_id), None)
        if not device:
            return json.dumps({"error": f"Device {device_id} not found"})

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
            "error": "Authentication failed — check AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD",
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
            "error": "Authentication failed — check AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD",
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
        with no physical hardware connected — see API Quirk 20).
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
            _all_parts = [
                f"{_sanitize_api_string(r.get('device_name'), 64) or 'Unknown'}: "
                f"{r.get('temperature')}{r.get('unit')}, {r.get('humidity')}% RH, "
                f"VPD {r.get('vpd')} kPa"
                for r in _ok
            ]
            _all_summary = ". ".join(_all_parts) + "."
        else:
            _all_summary = "No readings available."

        return json.dumps({"readings": readings, "human_summary": _all_summary}, indent=2)

    except ACInfinityAuthError as e:
        logger.warning("Auth error in get_all_device_readings: %s", e)
        return json.dumps({
            "error": "Authentication failed — check AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD",
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

        devices = await asyncio.to_thread(_client().get_devices)
        device = next((d for d in devices if d.get("devCode") == device_id), None)
        if not device:
            return json.dumps({"error": f"Device {device_id} not found"})

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
            "error": "Authentication failed — check AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD",
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

        devices = await asyncio.to_thread(_client().get_devices)
        device = next((d for d in devices if d.get("devCode") == device_id), None)
        if not device:
            return json.dumps({"error": f"Device {device_id} not found"})

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
            "error": "Authentication failed — check AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD",
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
        humidifiers — loadType 4 or 128 on standard devices, or pattern-detected on
        devType=18/22 where loadType is unreliable) produces a ▎-prefixed caveat line;
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

        devices = await asyncio.to_thread(_client().get_devices)
        device = next((d for d in devices if d.get("devCode") == device_id), None)
        if not device:
            return json.dumps({"error": f"Device {device_id} not found"})

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
            "error": "Authentication failed — check AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD",
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


def _format_schedule_time(minutes: int | None) -> str | None:
    """Convert minutes-since-midnight to HH:MM string. Returns None when disabled.

    65535 is the API's disabled-sentinel. Any other out-of-range value
    (>= 1440 minutes = past 24h, or negative) is treated as None rather than
    silently producing nonsense like "25:00" — a corrupt or unset field is
    indistinguishable from disabled in this context.
    """
    if minutes is None or minutes == 65535 or minutes == 255:
        return None
    if not (0 <= minutes < 1440):
        return None
    h, m = divmod(minutes, 60)
    return f"{h:02d}:{m:02d}"


def _format_schedule_summary(begin: int, end: int) -> str:
    """Return a grower-readable schedule description in 12-hour format."""
    if begin in (255, 65535):
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


# ============ Timezone and Unit Helpers ============


def _effective_tz(zone_id: str | None) -> ZoneInfo:
    """Return a ZoneInfo for the given IANA zone string, or UTC on any error."""
    if zone_id:
        try:
            return ZoneInfo(_sanitize_api_string(zone_id, 64))
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            pass
    return ZoneInfo("UTC")


def _effective_unit(unit_raw: int | None) -> str:
    """Return 'C' for Celsius devices (unit=1) or 'F' for all others."""
    return "C" if unit_raw == 1 else "F"


def _to_preferred_temp(c: float, unit: str) -> float:
    """Convert a Celsius value to the preferred unit, rounded to 1 decimal place."""
    return round(c * 9 / 5 + 32, 1) if unit == "F" else round(c, 1)


def _unit_label(unit: str) -> str:
    """Return the display label for the given unit code."""
    return "°F" if unit == "F" else "°C"


def _utc_iso_to_local(utc_iso: str | None, tz: ZoneInfo) -> str | None:
    """Convert a UTC ISO 8601 string to a local timezone string, or None if input is None."""
    if not utc_iso:
        return None
    dt = datetime.fromisoformat(utc_iso.rstrip("Z")).replace(tzinfo=UTC)
    return dt.astimezone(tz).strftime("%Y-%m-%dT%H:%M:%S %Z")


def _utcnow() -> datetime:
    """Returns current UTC time as a tz-aware datetime. Wrappable for testing."""
    return datetime.now(UTC)


def _format_window_dt(dt: datetime) -> str:
    """Format a tz-aware datetime to a human-readable local string, e.g. 'May 23, 10:35 AM CDT'.

    Caller must always pass a tz-aware datetime — asserted at entry.
    Uses literal 'AM'/'PM' strings to avoid strftime('%p') locale variation.
    """
    assert dt.tzinfo is not None, "_format_window_dt requires a tz-aware datetime"
    period = "AM" if dt.hour < 12 else "PM"
    display_hour = dt.hour % 12 or 12
    tz_name = dt.strftime("%Z")
    return f"{dt.strftime('%B')} {dt.day}, {display_hour}:{dt.strftime('%M')} {period} {tz_name}"


def _short_date(dt: datetime) -> str:
    """Return a short local date string like 'May 23'. Cross-platform (no %-d)."""
    return f"{dt.strftime('%B')} {dt.day}"


def _utc_hour_to_local(utc_dt: datetime, tz: ZoneInfo) -> str:
    """Convert a naive UTC datetime to a local time string like '3:00 PM CDT (peak on May 23)'.

    Includes the calendar date to disambiguate peak hours across multi-day report windows.
    Uses astimezone for full DST-aware conversion — sub-hour UTC offsets (UTC+5:30) are
    handled correctly (replaces the prior floor-of-whole-hours approximation, Quirk 23).
    Uses literal 'AM'/'PM' strings to avoid strftime('%p') locale variation.
    """
    local_dt = utc_dt.replace(tzinfo=UTC).astimezone(tz)
    display_hour = local_dt.hour % 12 or 12
    period = "AM" if local_dt.hour < 12 else "PM"
    tz_name = local_dt.strftime("%Z")
    date_str = f"{local_dt.strftime('%b')} {local_dt.day}"
    return f"{display_hour}:00 {period} {tz_name} (peak on {date_str})"


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


def _is_port_empty(port_data: dict | None, port: int, device: dict | None) -> bool:
    """Return True when a port appears to have nothing connected.

    Detection signal (from issue #165):
    - Port name matches the API default pattern ``"Port N"`` (i.e. not custom-named), AND
    - ``portsLoad == 0``, OR the device ``devType`` is in ``_ZERO_LOAD_DEV_TYPES`` (18, 22).

    devType 18 (8T4TC / UIS 69 Pro+) always has ``portsLoad=0`` for all ports regardless
    of whether anything is actually connected, so we rely on the default-name-only signal
    for those devices. devType 22 (Q0KT4) also has unreliable portsLoad.

    Custom-named ports are assumed connected — if the grower named a port, something is
    plugged in. Returns False when ``port_data`` is None (port not found) or when the
    device dict is not available.
    """
    if port_data is None or device is None:
        return False

    port_name = port_data.get("portName")
    # Custom-named = not default "Port N" — assume connected.
    if port_name and port_name != f"Port {port}":
        return False

    # Port name is default or absent — check load signal.
    ports_load = port_data.get("portsLoad", 0) or 0
    dev_type = device.get("devType")
    if ports_load == 0 or dev_type in _ZERO_LOAD_DEV_TYPES:
        return True

    return False


def _empty_port_warning(port: int, port_label: str) -> str:
    """Return the grower-friendly advisory warning text for an empty port.

    Used by write tools (``warning`` field). The message is advisory — it does not
    block the action. ``port_label`` is the formatted label, e.g. ``"Port 7"`` or
    ``"Inline Fan (Port 3)"``.
    """
    return (
        f"{port_label} doesn't appear to have anything connected. "
        "If you meant a different port, let me know which one."
    )


def _empty_port_note(port: int, port_label: str) -> str:
    """Return the grower-friendly note text for an empty port.

    Used by read tools (``note`` field).
    """
    return (
        f"{port_label} doesn't appear to have anything connected. "
        "If you meant a different port, let me know which one."
    )


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


async def _build_advance_conflict_response(
    device_id: str, dev_id: object, port: int, port_name: str,
    requested_speed: int | None = None,
) -> str:
    """Build a structured ADVANCE_AUTOMATION conflict response for write tools.

    Five paths depending on the secondary automation lookup result:

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
        device_id: Human-readable device code (e.g. ``"C58ZA"``).
        dev_id: Numeric device ID for the automation lookup API call.
        port: 1-based port number.
        port_name: Human-readable port name (e.g. ``"Filter"``).
        requested_speed: The speed the caller tried to set (from set_port_speed).
            When not None, adds a ``"0_update_speed"`` option in the normal path.
            Pass ``None`` from set_port_on / set_port_off (no speed option applies).
    """
    api_call_failed = False
    automations: list[dict] = []
    active_automations: list[dict] = []
    governing = None
    try:
        raw = await asyncio.to_thread(_client().get_advance_automations, str(dev_id))
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
            "error": "Authentication failed — check AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD",
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
              "port_name": "Humidifier",
              "power_level": 0,
              "mode": "OFF",
              "plug_status": "not powered"
            }

        ``mode`` is one of: OFF, ON, AUTO, TIMER_TO_ON, TIMER_TO_OFF, CYCLE, SCHEDULE, VPD,
        Automation. ``plug_status`` is only present when no current is detected on the port (the
        port is not powered or nothing is connected). It is omitted when the port is running.
        ``remain_time_seconds`` is only present when a countdown timer is active (value > 0);
        it is omitted when there is no active timer.
        When ``mode`` is ``Automation``, the port is governed by a named Advance Automation
        program in the AC Infinity app. ``automation_name`` is present only when the port is
        under automation control and the governing automation name was successfully resolved;
        absent otherwise.

        When the port appears to have nothing connected (default-named ``"Port N"`` with zero load,
        or a devType=18/22 controller), the response also includes a ``note`` field alerting the
        grower (e.g. ``"Port 7 doesn't appear to have anything connected."``).

        On failure returns ``{"error": "...", "detail": "..."}``.

        Note on ADVANCE detection: ``isOpenAutomation==1`` in devInfoListAll is the primary
        signal. For AI+ devices (no curMode field) and older firmware without isOpenAutomation,
        a secondary call to getdevModeSettingList is made to check modeType.
    """
    try:
        if port < 1:
            return json.dumps({"error": "port must be a positive integer"})

        devices = await asyncio.to_thread(_client().get_devices)
        device = next((d for d in devices if d.get("devCode") == device_id), None)
        if not device:
            return json.dumps({"error": f"Device {device_id} not found"})

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
        if not port_data.get("loadState", 0) and not _ps_power:
            result["plug_status"] = "not powered"
        if _is_port_empty(port_data, port, device):
            _port_label_s = _ps_raw_name
            result["note"] = _empty_port_note(port, _port_label_s)
        result["human_summary"] = _ps_summary
        return json.dumps(result, indent=2)

    except ACInfinityAuthError as e:
        logger.warning("Auth error in get_port_status: %s", e)
        return json.dumps({
            "error": "Authentication failed — check AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD",
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

        When the port appears to have nothing connected (default-named ``"Port N"`` with zero load,
        or a devType=18/22 controller), the response also includes a ``note`` field alerting the
        grower. On failure returns ``{"error": "...", "detail": "..."}``.
    """
    try:
        if port < 1:
            return json.dumps({"error": "port must be a positive integer"})

        devices = await asyncio.to_thread(_client().get_devices)
        device = next((d for d in devices if d.get("devCode") == device_id), None)
        if not device:
            return json.dumps({"error": f"Device {device_id} not found"})

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
                resp["note"] = (
                    "Could not fetch automation details."
                    " Ask me to list your automations for details."
                )
            if _is_port_empty(port_data, port, device):
                _ps_port_label = (
                    port_data.get("portName", f"Port {port}") if port_data else f"Port {port}"
                )
                empty_note = _empty_port_note(port, _ps_port_label)
                if "note" in resp:
                    resp["note"] = resp["note"] + " " + empty_note
                else:
                    resp["note"] = empty_note
            return json.dumps(resp, indent=2)

        vpd_target = None
        if settings.get("targetVpdSwitch"):
            raw = settings.get("targetVpd", 0)
            # Clamp out-of-range / corrupted values. Realistic VPD targets are
            # 0–3 kPa; anything outside [0, 50] (i.e. 0..500 raw) suggests a
            # corrupt or unset field rather than a plant-bearable target. Return
            # None instead of feeding nonsense to the LLM (P3-F020).
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
            min_c_raw = settings.get("devLt", 0)
            max_c_raw = settings.get("devHt", 0)
            temp_range = {
                "min": _to_preferred_temp(float(min_c_raw), _unit),
                "max": _to_preferred_temp(float(max_c_raw), _unit),
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
        if temp_range:
            _t_min = temp_range["min"]
            _t_max = temp_range["max"]
            human_summary = (
                f"Temperature automation: {_t_min}–{_t_max}{_unit_lbl}. "
                f"Fan speeds up above {_t_max}{_unit_lbl} and slows below {_t_min}{_unit_lbl}."
            )
        elif vpd_target is not None:
            human_summary = f"VPD automation: target {vpd_target} kPa."
        elif humi_range:
            human_summary = (
                f"Humidity automation: {humi_range['min_pct']}–{humi_range['max_pct']}%."
            )
        else:
            human_summary = f"Port is in {mode_str} mode."

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
            _gps_port_label = (
                port_data.get("portName", f"Port {port}") if port_data else f"Port {port}"
            )
            non_adv_resp["note"] = _empty_port_note(port, _gps_port_label)
        return json.dumps(non_adv_resp, indent=2)

    except ACInfinityAuthError as e:
        logger.warning("Auth error in get_port_settings: %s", e)
        return json.dumps({
            "error": "Authentication failed — check AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD",
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

        devices = await asyncio.to_thread(_client().get_devices)
        device = next((d for d in devices if d.get("devCode") == device_id), None)
        if not device:
            return json.dumps({"error": f"Device {device_id} not found"})

        write_result = await asyncio.to_thread(
            _client().set_port_mode, device, port, {"onSpead": speed}, dry_run,
            require_variable_speed=True,
        )

        if write_result.get("ai_plus_write_unsupported"):
            return _ai_plus_unsupported_error(device_id, port, write_result["controller_type"])

        ports_list = device.get("deviceInfo", {}).get("ports", [])
        port_data = next((p for p in ports_list if p.get("port") == port), None)
        has_custom_name = bool(
            port_data
            and port_data.get("portName")
            and port_data.get("portName") != f"Port {port}"
        )
        port_name = _get_port_name_from_device(device, port)
        port_label = f"{port_name} (Port {port})" if has_custom_name else port_name

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

        prior_mode_type = write_result.get("prior_mode_type")
        if prior_mode_type in (0, 1):
            response["warning"] = (
                f"{port_label} is currently in OFF mode — speed was stored but the port "
                "will not run until the mode is changed to ON. "
                "To activate it, ask me to switch this port to ON mode."
            )

        if _is_port_empty(port_data, port, device):
            empty_warn = _empty_port_warning(port, port_label)
            if "warning" in response:
                response["warning"] = response["warning"] + " " + empty_warn
            else:
                response["warning"] = empty_warn

        return json.dumps(response, indent=2)

    except ACInfinityAuthError as e:
        logger.warning("Auth error in set_port_speed (device=%s port=%s): %s", device_id, port, e)
        return json.dumps({
            "error": "Authentication failed — check AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD",
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error("API error in set_port_speed (device=%s port=%s): %s", device_id, port, e)
        return json.dumps({"error": "AC Infinity API error", "detail": "see server logs"})
    except ACInfinityAdvanceConflictError:
        port_name = _get_port_name_from_device(device, port)
        dev_id = device.get("devId") if device else None
        return await _build_advance_conflict_response(
            device_id, dev_id, port, port_name, requested_speed=speed
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

        When the port appears to have nothing connected (default-named ``"Port N"``
        with zero load, or a devType=18/22 device), the response also includes a
        ``warning`` field alerting the grower.

        On failure returns ``{"error": "..."}``.
    """
    try:
        if port < 1:
            return json.dumps({"error": "port must be a positive integer"})

        devices = await asyncio.to_thread(_client().get_devices)
        device = next((d for d in devices if d.get("devCode") == device_id), None)
        if not device:
            return json.dumps({"error": f"Device {device_id} not found"})

        write_result = await asyncio.to_thread(
            _client().set_port_mode, device, port, {"onSpead": 10}, dry_run
        )

        if write_result.get("ai_plus_write_unsupported"):
            return _ai_plus_unsupported_error(device_id, port, write_result["controller_type"])

        ports_list = device.get("deviceInfo", {}).get("ports", [])
        port_data = next((p for p in ports_list if p.get("port") == port), None)
        has_custom_name = bool(
            port_data
            and port_data.get("portName")
            and port_data.get("portName") != f"Port {port}"
        )
        port_name = _get_port_name_from_device(device, port)
        port_label = f"{port_name} (Port {port})" if has_custom_name else port_name

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
            response["warning"] = _empty_port_warning(port, port_label)

        return json.dumps(response, indent=2)

    except ACInfinityAuthError as e:
        logger.warning("Auth error in set_port_on (device=%s port=%s): %s", device_id, port, e)
        return json.dumps({
            "error": "Authentication failed — check AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD",
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error("API error in set_port_on (device=%s port=%s): %s", device_id, port, e)
        return json.dumps({"error": "AC Infinity API error", "detail": "see server logs"})
    except ACInfinityAdvanceConflictError:
        port_name = _get_port_name_from_device(device, port)
        dev_id = device.get("devId") if device else None
        return await _build_advance_conflict_response(device_id, dev_id, port, port_name)
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
    """Zero a port's speed (onSpead=0).

    Sends onSpead=0 only — the port's active automation mode (atType) is left
    unchanged. If the port is in AUTO or VPD mode, the controller's automation
    logic may re-engage the port when its trigger condition is next met. To
    keep the port off until manually re-enabled, switch the mode to OFF first
    via ``set_port_mode(device_id, port, mode="OFF")``.

    Uses read-before-write. Defaults to dry_run=True — set dry_run=False to
    write to the device.

    Args:
        device_id: Device code from discover_devices (e.g. "C58ZA").
        port: 1-based port number.
        dry_run: If True (default), returns the payload that would be sent
            without writing.

    Returns:
        JSON with action, device_id, port, dry_run, controller_type, sent,
        and payload (when dry_run=True).

        When the port appears to have nothing connected (default-named ``"Port N"``
        with zero load, or a devType=18/22 device), the response also includes a
        ``warning`` field alerting the grower.

        On failure returns ``{"error": "..."}``.
    """
    try:
        if port < 1:
            return json.dumps({"error": "port must be a positive integer"})

        devices = await asyncio.to_thread(_client().get_devices)
        device = next((d for d in devices if d.get("devCode") == device_id), None)
        if not device:
            return json.dumps({"error": f"Device {device_id} not found"})

        write_result = await asyncio.to_thread(
            _client().set_port_mode, device, port, {"onSpead": 0}, dry_run
        )

        if write_result.get("ai_plus_write_unsupported"):
            return _ai_plus_unsupported_error(device_id, port, write_result["controller_type"])

        ports_list = device.get("deviceInfo", {}).get("ports", [])
        port_data = next((p for p in ports_list if p.get("port") == port), None)
        has_custom_name = bool(
            port_data
            and port_data.get("portName")
            and port_data.get("portName") != f"Port {port}"
        )
        port_name = _get_port_name_from_device(device, port)
        port_label = f"{port_name} (Port {port})" if has_custom_name else port_name

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
            response["warning"] = _empty_port_warning(port, port_label)

        return json.dumps(response, indent=2)

    except ACInfinityAuthError as e:
        logger.warning("Auth error in set_port_off (device=%s port=%s): %s", device_id, port, e)
        return json.dumps({
            "error": "Authentication failed — check AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD",
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error("API error in set_port_off (device=%s port=%s): %s", device_id, port, e)
        return json.dumps({"error": "AC Infinity API error", "detail": "see server logs"})
    except ACInfinityAdvanceConflictError:
        port_name = _get_port_name_from_device(device, port)
        dev_id = device.get("devId") if device else None
        return await _build_advance_conflict_response(device_id, dev_id, port, port_name)
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


def _ai_plus_unsupported_error(device_id: str, port: int, controller_type: str) -> str:
    # dry_run is always False here: the AI+ guard in client._set_port_mode_inner fires
    # only on the live-write path (dry_run returns early before the guard is reached).
    return json.dumps({
        "error": (
            "AI+ controllers live write path is not yet implemented. "
            "Preview mode (showing what would happen) is fully supported for this device type "
            "— ask me to preview the action first."
        ),
        "device_id": device_id,
        "port": port,
        "dry_run": False,
        "controller_type": controller_type,
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

        devices = await asyncio.to_thread(_client().get_devices)
        device = next((d for d in devices if d.get("devCode") == device_id), None)
        if not device:
            return json.dumps({"error": f"Device {device_id} not found"})

        updates = {
            "atType": 8,  # VPD mode
            "vpdSettingMode": 1,
            "targetVpd": int(target_vpd * 10 + 0.5),  # ×10; int(x+0.5) avoids banker's rounding
            "targetVpdSwitch": 1,
        }
        write_result = await asyncio.to_thread(
            _client().set_port_mode, device, port, updates, dry_run
        )

        if write_result.get("ai_plus_write_unsupported"):
            return _ai_plus_unsupported_error(device_id, port, write_result["controller_type"])

        ports_list = device.get("deviceInfo", {}).get("ports", [])
        port_data = next((p for p in ports_list if p.get("port") == port), None)
        has_custom_name = bool(
            port_data
            and port_data.get("portName")
            and port_data.get("portName") != f"Port {port}"
        )
        port_name = _get_port_name_from_device(device, port)
        port_label = f"{port_name} (Port {port})" if has_custom_name else port_name

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
            response["warning"] = _empty_port_warning(port, port_label)
        return json.dumps(response, indent=2)

    except ACInfinityAuthError as e:
        logger.warning(
            "Auth error in set_vpd_automation (device=%s port=%s): %s",
            device_id, port, e,
        )
        return json.dumps({
            "error": "Authentication failed — check AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD",
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
        return await _build_advance_conflict_response(device_id, dev_id, port, port_name)
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

        devices = await asyncio.to_thread(_client().get_devices)
        device = next((d for d in devices if d.get("devCode") == device_id), None)
        if not device:
            return json.dumps({"error": f"Device {device_id} not found"})

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

        if write_result.get("ai_plus_write_unsupported"):
            return _ai_plus_unsupported_error(device_id, port, write_result["controller_type"])

        ports_list = device.get("deviceInfo", {}).get("ports", [])
        port_data = next((p for p in ports_list if p.get("port") == port), None)
        has_custom_name = bool(
            port_data
            and port_data.get("portName")
            and port_data.get("portName") != f"Port {port}"
        )
        port_name = _get_port_name_from_device(device, port)
        port_label = f"{port_name} (Port {port})" if has_custom_name else port_name

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
            response["warning"] = _empty_port_warning(port, port_label)
        return json.dumps(response, indent=2)

    except ACInfinityAuthError as e:
        logger.warning(
            "Auth error in set_temperature_automation (device=%s port=%s): %s",
            device_id, port, e,
        )
        return json.dumps({
            "error": "Authentication failed — check AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD",
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
        return await _build_advance_conflict_response(device_id, dev_id, port, port_name)
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

        devices = await asyncio.to_thread(_client().get_devices)
        device = next((d for d in devices if d.get("devCode") == device_id), None)
        if not device:
            return json.dumps({"error": f"Device {device_id} not found"})

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

        if write_result.get("ai_plus_write_unsupported"):
            return _ai_plus_unsupported_error(device_id, port, write_result["controller_type"])

        ports_list = device.get("deviceInfo", {}).get("ports", [])
        port_data = next((p for p in ports_list if p.get("port") == port), None)
        has_custom_name = bool(
            port_data
            and port_data.get("portName")
            and port_data.get("portName") != f"Port {port}"
        )
        port_name = _get_port_name_from_device(device, port)
        port_label = f"{port_name} (Port {port})" if has_custom_name else port_name

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
            response["warning"] = _empty_port_warning(port, port_label)
        return json.dumps(response, indent=2)

    except ACInfinityAuthError as e:
        logger.warning(
            "Auth error in set_humidity_automation (device=%s port=%s): %s",
            device_id, port, e,
        )
        return json.dumps({
            "error": "Authentication failed — check AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD",
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
        return await _build_advance_conflict_response(device_id, dev_id, port, port_name)
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

        devices = await asyncio.to_thread(_client().get_devices)
        device = next((d for d in devices if d.get("devCode") == device_id), None)
        if not device:
            return json.dumps({"error": f"Device {device_id} not found"})

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

        if write_result.get("ai_plus_write_unsupported"):
            return _ai_plus_unsupported_error(device_id, port, write_result["controller_type"])

        ports_list = device.get("deviceInfo", {}).get("ports", [])
        port_data = next((p for p in ports_list if p.get("port") == port), None)
        has_custom_name = bool(
            port_data
            and port_data.get("portName")
            and port_data.get("portName") != f"Port {port}"
        )
        port_name = _get_port_name_from_device(device, port)
        port_label = f"{port_name} (Port {port})" if has_custom_name else port_name

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
            response["warning"] = _empty_port_warning(port, port_label)
        return json.dumps(response, indent=2)

    except ACInfinityAuthError as e:
        logger.warning("Auth error in set_port_mode (device=%s port=%s): %s", device_id, port, e)
        return json.dumps({
            "error": "Authentication failed — check AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD",
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error("API error in set_port_mode (device=%s port=%s): %s", device_id, port, e)
        return json.dumps({"error": "AC Infinity API error", "detail": "see server logs"})
    except ACInfinityAdvanceConflictError:
        port_name = _get_port_name_from_device(device, port)
        dev_id = device.get("devId") if device else None
        return await _build_advance_conflict_response(device_id, dev_id, port, port_name)
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
        devices = await asyncio.to_thread(_client().get_devices)
    except ACInfinityAuthError as e:
        logger.warning(
            "Auth error fetching devices in apply_grow_stage_template (device=%s): %s",
            device_id, e,
        )
        return json.dumps({
            "error": "Authentication failed — check AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD",
            "detail": "see server logs",
        })
    except ACInfinityAPIError as e:
        logger.error(
            "API error fetching devices in apply_grow_stage_template (device=%s): %s",
            device_id, e,
        )
        return json.dumps({"error": "AC Infinity API error", "detail": "see server logs"})
    except Exception as e:
        # P1-C2-F003 / P3-C2-F010: previously returned str(e) here, which echoed
        # arbitrary exception text into the LLM-facing response. Match the
        # generic pattern used elsewhere.
        logger.error(
            "Unexpected error fetching devices in apply_grow_stage_template (device=%s): %s",
            device_id, e, exc_info=True,
        )
        return json.dumps({"error": "Unexpected error", "detail": "see server logs"})

    device = next((d for d in devices if d.get("devCode") == device_id), None)
    if not device:
        return json.dumps({"error": f"Device {device_id} not found"})

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
            "error": "Authentication failed — check AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD",
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
        return await _build_advance_conflict_response(device_id, dev_id, port, port_name)
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

    if write_result.get("ai_plus_write_unsupported"):
        return _ai_plus_unsupported_error(device_id, port, write_result["controller_type"])

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
        devices = await asyncio.to_thread(_client().get_devices)
        device = next((d for d in devices if d.get("devCode") == device_id), None)
        if not device:
            return json.dumps({"error": f"Device {device_id} not found"})

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

    except ACInfinityAuthError:
        return json.dumps({"error": "Authentication failed", "detail": "see server logs"})
    except ACInfinityAPIError:
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

        devices = await asyncio.to_thread(_client().get_devices)
        device = next((d for d in devices if d.get("devCode") == device_id), None)
        if not device:
            return json.dumps({"error": f"Device {device_id} not found"})

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
            "human_summary": human_summary,
        }, indent=2)

    except ACInfinityAuthError:
        return json.dumps({"error": "Authentication failed", "detail": "see server logs"})
    except ACInfinityAPIError:
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

        devices = await asyncio.to_thread(_client().get_devices)
        device = next((d for d in devices if d.get("devCode") == device_id), None)
        if not device:
            return json.dumps({"error": f"Device {device_id} not found"})

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

    except ACInfinityAuthError:
        return json.dumps({"error": "Authentication failed", "detail": "see server logs"})
    except ACInfinityAPIError:
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

        devices = await asyncio.to_thread(_client().get_devices)
        device = next((d for d in devices if d.get("devCode") == device_id), None)
        if not device:
            return json.dumps({"error": f"Device {device_id} not found"})

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

    except ACInfinityAuthError:
        return json.dumps({"error": "Authentication failed", "detail": "see server logs"})
    except ACInfinityAPIError:
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
    begin_time: int = 0,
    end_time: int = 1439,
    dry_run: bool = True,
) -> str:
    """Create a new Advance Automation on a device.

    Defaults to dry_run=True for safety. Set dry_run=False to send the automation
    to the device. The port bitmask (grouptDevType) is computed automatically from
    the port number (Port N → 2^(N-1)).

    Args:
        device_id: The AC Infinity device code (from discover_devices).
        name: Automation name (max 64 chars, control chars stripped).
        on_speed: Fan speed when automation is active (1–10).
        port: 1-based port number the automation should control (1–8).
        off_speed: Not used — On mode relies on the port's own minimum speed setting.
            Parameter accepted for compatibility but not sent to the device.
        begin_time: Schedule start in minutes since midnight (0–1439, or 255=always active).
            Default: 0 (midnight). Use 255 for "always active" (runs 00:00–23:59 every day).
        end_time: Schedule end in minutes since midnight (0–1439, or 255=always active).
            Default: 1439 (23:59). Use 255 for "always active".
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
        if not (0 <= begin_time <= 1439 or begin_time == 255):
            return json.dumps({"error": "begin_time must be 0–1439 or 255 (no schedule)"})
        if not (0 <= end_time <= 1439 or end_time == 255):
            return json.dumps({"error": "end_time must be 0–1439 or 255 (no schedule)"})
        if begin_time != 255 and end_time != 255 and begin_time > end_time:
            return json.dumps(
                {"error": "begin_time must be <= end_time (or both 255 for no schedule)"}
            )
        if (begin_time == 255) != (end_time == 255):
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

        devices = await asyncio.to_thread(_client().get_devices)
        device = next((d for d in devices if d.get("devCode") == device_id), None)
        if not device:
            return json.dumps({"error": f"Device {device_id} not found"})

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

        port_settings = await asyncio.to_thread(_client().get_mode_settings, str(dev_id), port)
        min_speed = int(port_settings.get("offSpead", 0))

        schedule_summary = _format_schedule_summary(begin_time, end_time)

        if dry_run:
            return json.dumps({
                "action": "create",
                "name": clean_name,
                "port": port,
                "port_name": port_name,
                "on_speed": on_speed,
                "min_speed": min_speed,
                "begin_time": _format_schedule_time(begin_time),
                "end_time": _format_schedule_time(end_time),
                "schedule_summary": schedule_summary,
                "dry_run": True,
                "sent": False,
                "note": (
                    "Preview only — nothing sent to your device yet."
                    " Confirm to create this automation."
                ),
            })

        # Live path: compute port bitmask and build full payload
        grp_dev_type = 2 ** (port - 1)
        payload: dict = {
            # devId NOT included here — client._create_advance_automation_inner injects it.
            # advCode NOT included — absent from addGroups live capture (unlike addAlarms).
            # isFlag (capital F) confirmed for addGroups;
            # isflag (lowercase) for updateGroupsIsOn/delByid.
            "advName": clean_name,
            "currentMode": 1,
            "isOn": 1,
            "onSpeed": on_speed,
            # On mode has no user-settable min; port's own min setting is used.
            "offSpeed": 0,
            # Map "always active" sentinel (255) to a valid full-day range.
            "beginTime": 0 if begin_time == 255 else begin_time,
            "endTime": 1439 if end_time == 255 else end_time,
            "groupNums": 9,
            "sortType": 9,
            "subNumber": 0,
            "subNumberSort": 0,
            "isDel": 0,
            "isFlag": 1,
            "returnData": 1,
            "templateType": 0,
            "grouptDevType": grp_dev_type,
            "portType": 0,
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
            # 127 = binary 01111111 = all 7 days bitmask. 255 has bit 7 set which
            # causes the app to ignore the schedule and treat it as Continuous.
            "switchTime": 127,
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

        return json.dumps({
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
        })

    except ACInfinityAuthError:
        return json.dumps({"error": "Authentication failed", "detail": "see server logs"})
    except ACInfinityAPIError:
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

        devices = await asyncio.to_thread(_client().get_devices)
        device = next((d for d in devices if d.get("devCode") == device_id), None)
        if not device:
            return json.dumps({"error": f"Device {device_id} not found"})

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
        # Then delete each adv_id individually (each entry must be explicitly deleted).
        if was_enabled:
            await asyncio.to_thread(
                _client().disable_advance_automation, str(dev_id), found["adv_ids"][0]
            )

        for adv_id in found["adv_ids"]:
            await asyncio.to_thread(
                _client().delete_advance_automation, str(dev_id), adv_id
            )

        return json.dumps({
            "action": "delete",
            "automation_name": name,
            "automation_id": found["automation_id"],
            "was_enabled": was_enabled,
            "dry_run": False,
            "sent": True,
        })

    except ACInfinityAuthError:
        return json.dumps({"error": "Authentication failed", "detail": "see server logs"})
    except ACInfinityAPIError:
        return json.dumps({"error": "API error", "detail": "see server logs"})
    except ACInfinityDeviceError as e:
        logger.warning("Device error in delete_advance_automation (%s): %s", device_id, e)
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("Unexpected error in delete_advance_automation: %s", e, exc_info=True)
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
    3. Identifies all other ports currently in ADVANCE mode on this device (co-ports).
       Note: This locks *all* ADVANCE-mode ports on the device, not only those belonging
       to the governing automation. On devices with multiple active automations all
       ADVANCE-mode ports will be locked to manual control.
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
        if port < 1:
            return json.dumps({"error": "port must be a positive integer"})

        devices = await asyncio.to_thread(_client().get_devices)
        device = next((d for d in devices if d.get("devCode") == device_id), None)
        if not device:
            return json.dumps({"error": f"Device {device_id} not found"})

        dev_id = device.get("devId")
        if not dev_id:
            return json.dumps({"error": f"Device {device_id} is missing devId"})

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

        # Find the first enabled+running automation; fall back to first enabled-only,
        # then to first run_state-only (mid-toggle transient where isOn=0 but runState=1).
        automation = (
            next((g for g in grouped if g["enabled"] and g["run_state"]), None)
            or next((g for g in grouped if g["enabled"]), None)
            or next((g for g in grouped if g["run_state"]), None)
        )

        if automation is None:
            return json.dumps({
                "error": (
                    "Could not identify governing automation. "
                    "No enabled or actively running automations found on this device."
                ),
            })

        auto_name = automation["name"]
        auto_id = automation["automation_id"]
        adv_ids = automation["adv_ids"]

        # Step 2: Identify co-governed ports — all ports currently under automation
        # control except the target port.
        co_ports: list[dict] = []
        for p_data in ports_data:
            p_num = p_data.get("port")
            if p_num is None or p_num == port:
                continue
            p_settings = await asyncio.to_thread(_client().get_mode_settings, dev_id, p_num)
            if p_settings.get("modeType") == _ADVANCE_MODE_TYPE:
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
        _human_co = f" {_co_str} will be locked to current speeds." if _co_str else ""
        _bo_human_summary = (
            f"This will disable the '{auto_name}' automation and free {_target_label} for "
            f"manual control.{_human_co} You can re-enable the automation at any time — "
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
                    "(device=%s auto=%s): %s", device_id, auto_name, disable_exc,
                )
                return json.dumps({
                    "error": "Failed to disable automation",
                    "failed_step": "disable_automation",
                    "detail": "see server logs",
                })

            # Step B: Lock co-governed ports to their current speeds.
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

        return json.dumps({
            "action": f"release {_target_label} from '{auto_name}' automation",
            "dry_run": False,
            "automation_name": auto_name,
            "automation_id": auto_id,
            "co_ports_locked": co_ports_locked,
            "target_port": port,
            "target_port_freed": True,
            "human_summary": (
                f"Released {_target_label} from the '{auto_name}' automation. "
                "You can now control it manually."
            ),
            "sent": True,
        }, indent=2)

    except ACInfinityAuthError:
        return json.dumps({"error": "Authentication failed", "detail": "see server logs"})
    except ACInfinityAPIError:
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
Call `get_device_reading(device_id)` and look at the `vpd` field (kPa).
Or call `check_vpd_drift(device_id, stage)` to compare against a growth stage target.

**Step 2 — Diagnose HIGH VPD (above target range)**
High VPD means the air is too dry. The plant is losing water faster than it can absorb it.
Signs: wilting, leaf curl, slow growth.

Fixes (choose one or both):
- **Lower temperature** — call `set_temperature_automation(device_id, port, min_c, max_c)`
  to drop the max threshold 1–2°C.
- **Raise humidity** — call `set_humidity_automation(device_id, port, min_rh, max_rh)`
  to increase the lower humidity bound.
- **Use VPD mode** — call `set_vpd_automation(device_id, port, target_vpd)` to let the
  controller manage VPD directly. Start with the midpoint of your stage range.

**Step 3 — Diagnose LOW VPD (below target range)**
Low VPD means the air is too humid. Stomata close, CO2 uptake drops, mould risk rises.
Signs: soft growth, mould, bud rot risk in flower.

Fixes (choose one or both):
- **Raise temperature** — increase the min_c threshold in `set_temperature_automation`.
- **Lower humidity** — decrease the max_rh in `set_humidity_automation`.
- Increase airflow with `set_port_speed(device_id, port, speed)`.

**Target ranges by stage**
| Stage | VPD (kPa) | Temp (°F) |
|---|---|---|
| clones / seedling | 0.8–1.2 | 72–79 |
| veg | 1.0–1.5 | 68–82 |
| early flower | 1.0–1.8 | 68–79 |
| mid flower | 1.2–2.0 | 64–77 |
| late flower | 1.2–1.8 | 64–75 |

**One-click solution:** `apply_grow_stage_template(device_id, port, stage)` sets VPD,
temperature, and humidity automation in one call. Use `dry_run=True` first to preview.
"""


@mcp_server.prompt()
def new_grower_setup() -> str:
    """Onboarding guide: from first connection to automated grow environment."""
    return """\
## New Grower Setup Guide

Welcome! Here is how to connect your AC Infinity controller and get your environment
dialled in with automation in four steps.

**Step 1 — Discover your devices**
```
discover_devices()
```
Returns all controllers on your account. Copy the `device_id` (e.g. `"C58ZA"`) — you
need it for every other tool.

**Step 2 — Check current readings**
```
get_device_reading(device_id)
```
Shows live temperature, humidity, VPD, and the current speed of each port. Verify the
numbers match your physical environment before making any changes.

**Step 3 — Apply a grow stage template (dry_run first)**
```
apply_grow_stage_template(device_id, port=1, stage="veg", dry_run=True)
```
Preview the automation settings — VPD target, temperature range, humidity range —
without writing anything. When the numbers look right:
```
apply_grow_stage_template(device_id, port=1, stage="veg", dry_run=False)
```
Available stages: `clones`, `seedling`, `veg`, `early_flower`, `mid_flower`, `late_flower`.

**Step 4 — Check your environment health score**
```
get_environment_health(device_id, stage="veg")
```
Returns a 0–100 score and letter grade (A–F) with a per-metric breakdown and the top
recommendation. Run this after applying automation to confirm the environment is responding.

**Tip:** Use `check_vpd_drift(device_id, stage)` any time you want a quick status check
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

`check_vpd_drift(device_id, stage)` returns a `status` field:

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

`get_environment_health(device_id, stage)` returns a composite score:

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

| Situation | Tool to call |
|---|---|
| VPD HIGH or LOW | `set_vpd_automation`, `set_temperature_automation`, `set_humidity_automation` |
| Health score C or below | Follow `top_recommendation`; use `apply_grow_stage_template` |
| Unsure where to start | `vpd_troubleshooting` prompt |
| First time setup | `new_grower_setup` prompt |
"""


# ============ Helpers ============

_DURATION_RE = re.compile(r"^(\d+)(m|h|d)$", re.IGNORECASE)
_DURATION_UNITS = {"m": 60, "h": 3600, "d": 86400}


def _parse_duration_seconds(interval: str) -> int:
    """Parse a duration string into a bucket size in seconds.

    Accepts e.g. "1m", "5m", "15m", "30m", "1h", "2h", "6h", "12h", "1d".
    "daily" is accepted as an alias for "1d".
    Raises ValueError for unrecognised formats.
    """
    if interval in ("daily", "1d"):
        return 86400
    m = _DURATION_RE.fullmatch(interval)
    if not m:
        raise ValueError(
            f"Invalid sample_interval {interval!r}. "
            "Use 'raw' for unsampled data, or a duration like '1m', '5m', '15m', "
            "'30m', '1h', '2h', '6h', '12h', '1d'."
        )
    value, unit = int(m.group(1)), m.group(2).lower()
    return value * _DURATION_UNITS[unit]


def _filter_readings_by_time(
    readings: list, time_start: str | None = None, time_end: str | None = None
) -> tuple[list, int]:
    """Filter readings to only include those within a UTC time window (HH:MM format).

    Returns:
        (filtered_readings, dropped_count) where dropped_count is the number of
        readings whose timestamps could not be parsed (and were therefore excluded
        from the result). The caller is expected to surface a non-zero drop count
        in the response so the user knows data was dropped.

    Overnight windows: when time_start > time_end (e.g. "22:00"-"06:00"), the
    filter is the OR of [time_start, 24:00) and [00:00, time_end] — i.e. the
    window crosses midnight. Same-day windows use the inclusive intersection.
    """
    if not time_start and not time_end:
        return readings, 0

    overnight = (
        time_start is not None and time_end is not None and time_start > time_end
    )
    filtered = []
    dropped = 0
    for reading in readings:
        timestamp_str = reading.get("timestamp", "")
        try:
            # P1-C2-F005: handle both UTC-naive (..."T...Z") and aware (...+HH:MM)
            # timestamps. The historical-data parser always emits the naive-Z
            # form today, but a future fixture or hand-crafted payload could
            # carry a non-UTC offset — converting via astimezone preserves the
            # instant, where the old .replace(tzinfo=UTC) silently corrupted it.
            ts_dt = datetime.fromisoformat(timestamp_str.rstrip("Z"))
            if ts_dt.tzinfo is None:
                ts_dt = ts_dt.replace(tzinfo=UTC)
            else:
                ts_dt = ts_dt.astimezone(UTC)
            reading_time = ts_dt.strftime("%H:%M")
        except (ValueError, AttributeError, TypeError) as e:
            logger.warning("Could not parse timestamp %s: %s", timestamp_str, e)
            dropped += 1
            continue

        if time_start and time_end:
            if overnight:
                include = reading_time >= time_start or reading_time <= time_end
            else:
                include = time_start <= reading_time <= time_end
        elif time_start:
            include = reading_time >= time_start
        else:  # time_end only
            include = reading_time <= time_end  # type: ignore[operator]

        if include:
            filtered.append(reading)

    return filtered, dropped


def apply_sampling(readings: list, interval: str) -> list:
    """Bucket readings by the given duration interval and average each bucket.

    "raw" returns all records unchanged.
    Any duration string (e.g. "1m", "15m", "1h", "6h", "1d") averages readings
    into fixed-width time buckets of that size; each bucket is represented by
    a single averaged record whose timestamp is the bucket-start time (UTC).
    """
    if interval == "raw":
        return readings

    bucket_secs = _parse_duration_seconds(interval)
    sampled: dict = {}

    for reading in readings:
        timestamp_str = reading.get("timestamp", "")
        try:
            ts_dt = datetime.fromisoformat(timestamp_str.rstrip("Z"))
            unix_ts = int(ts_dt.replace(tzinfo=UTC).timestamp())
        except (ValueError, AttributeError, TypeError) as e:
            # Narrow exception set: only timestamp-parse failures (bad string,
            # None, unexpected type) should drop a reading. Anything else
            # should propagate — silently swallowing every Exception masks
            # real bugs in the parser layer (P1-F014).
            logger.debug("apply_sampling skipping bad timestamp %r: %s", timestamp_str, e)
            continue
        bucket_key = (unix_ts // bucket_secs) * bucket_secs
        sampled.setdefault(bucket_key, []).append(reading)

    result = []
    for bucket_key in sorted(sampled.keys()):
        avg = average_readings(sampled[bucket_key])
        avg["timestamp"] = (
            datetime.fromtimestamp(bucket_key, UTC).replace(tzinfo=None).isoformat() + "Z"
        )
        result.append(avg)
    return result


def average_readings(readings: list) -> dict:
    """Compute average of multiple readings."""
    if not readings:
        return {}

    temps_c = [r.get("temperature_c", 0) for r in readings]
    temps_f = [r.get("temperature_f", 0) for r in readings]
    humidities = [r.get("humidity", 0) for r in readings]
    vpds = [r.get("vpd", 0) for r in readings]

    ports_by_number: dict = {}
    for reading in readings:
        for port in reading.get("ports", []):
            port_num = port.get("port")
            if port_num not in ports_by_number:
                ports_by_number[port_num] = {
                    "port": port_num,
                    "name": port.get("name", f"Port {port_num}"),
                    "speeds": [],
                    "on_count": 0,
                }
            ports_by_number[port_num]["speeds"].append(port.get("speed", 0))
            if port.get("on"):
                ports_by_number[port_num]["on_count"] += 1

    averaged_ports = [
        {
            "port": port_num,
            "name": data["name"],
            "speed": round(sum(data["speeds"]) / len(data["speeds"]), 2),
            "on": data["on_count"] > 0,
        }
        for port_num, data in sorted(ports_by_number.items())
    ]

    return {
        "timestamp": readings[0].get("timestamp"),
        "temperature_c": round(sum(temps_c) / len(temps_c), 2) if temps_c else None,
        "temperature_f": round(sum(temps_f) / len(temps_f), 2) if temps_f else None,
        "humidity": round(sum(humidities) / len(humidities), 2) if humidities else None,
        "vpd": round(sum(vpds) / len(vpds), 2) if vpds else None,
        "ports": averaged_ports,
    }


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

    global aci_client
    aci_client = ACInfinityClient(email, password)
    if not aci_client.authenticate():
        logger.error("Failed to authenticate with AC Infinity")
        sys.exit(1)

    async def _run() -> None:
        logger.info("AC Infinity MCP Server ready (stdio)")
        await mcp_server.run_stdio_async()

    asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    main()
