import asyncio
import dataclasses
import json
import logging
import os
import re
import sys
import unicodedata
from datetime import UTC, datetime, timedelta

from mcp.server.fastmcp import FastMCP

from ac_infinity_mcp.analytics import (
    STAGE_TARGETS,
    build_activity_report,
    calculate_health_score,
    detect_trends,
)
from ac_infinity_mcp.client import ACInfinityClient
from ac_infinity_mcp.schema import (
    ACInfinityAdvanceConflictError,
    ACInfinityAPIError,
    ACInfinityAuthError,
    ACInfinityDeviceError,
    ACIReading,
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
            }
            for d in devices
        ]

        return json.dumps({"devices": result}, indent=2)

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
              "timestamp": "2026-05-20T14:32:00Z",
              "device_id": "C58ZA",
              "device_name": "Towlie Tent",
              "temperature_c": 24.3,
              "temperature_f": 75.7,
              "humidity": 58.2,
              "vpd": 1.31,
              "ports": [
                {"port": 1, "name": "Inline Fan", "speed": 5, "load": 0},
                ...
              ],
              "external_sensors": []
            }

        On failure returns ``{"error": "...", "detail": "..."}``.
    """
    try:
        devices = await asyncio.to_thread(_client().get_devices)

        device = next((d for d in devices if d.get("devCode") == device_id), None)
        if not device:
            return json.dumps({"error": f"Device {device_id} not found"})

        parsed = _client().parse_device_data(device)
        reading = ACIReading(**parsed)

        return json.dumps(reading.to_dict(), indent=2)

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

        import calendar
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

        if sampled:
            temps_c = [r.get("temperature_c", 0) for r in sampled if "temperature_c" in r]
            temps_f = [r.get("temperature_f", 0) for r in sampled if "temperature_f" in r]
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

            stats = {
                "readings_count": len(sampled),
                "sample_interval": sample_interval,
                "date_range": {"start": start_date, "end": end_date},
                "temperature_c": {
                    "min": round(min(temps_c), 2) if temps_c else None,
                    "avg": round(sum(temps_c) / len(temps_c), 2) if temps_c else None,
                    "max": round(max(temps_c), 2) if temps_c else None,
                },
                "temperature_f": {
                    "min": round(min(temps_f), 2) if temps_f else None,
                    "avg": round(sum(temps_f) / len(temps_f), 2) if temps_f else None,
                    "max": round(max(temps_f), 2) if temps_f else None,
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
            "readings": sampled,
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

        return json.dumps({
            "device_id": device_id,
            "current_vpd": current_vpd,
            "target_range": target_range,
            "stage": stage,
            "status": status,
            "deviation": deviation,
            "alert": alert,
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
        On auth/API failure returns ``{"error": "...", "detail": "..."}``.
    """
    try:
        devices = await asyncio.to_thread(_client().get_devices)

        readings = []
        for device in devices:
            device_id = device.get("devCode")
            try:
                parsed = _client().parse_device_data(device)
                reading = ACIReading(**parsed)
                readings.append({
                    "device_id": device_id,
                    "device_name": device.get("devName"),
                    **reading.to_dict(),
                })
            except Exception as e:
                readings.append({
                    "device_id": device_id,
                    "device_name": device.get("devName"),
                    "error": str(e),
                })

        return json.dumps({"readings": readings}, indent=2)

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
        and a top actionable recommendation.
    """
    try:
        if stage not in STAGE_TARGETS:
            valid = ", ".join(STAGE_TARGETS)
            return json.dumps({"error": f"Unknown stage: {stage}. Valid: {valid}"})

        reading_json = await get_device_reading(device_id)
        reading = json.loads(reading_json)
        if "error" in reading:
            return json.dumps(reading)

        health = calculate_health_score(reading, stage)
        result = dataclasses.asdict(health)
        result["device_id"] = device_id
        result["stage"] = stage
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

        today = datetime.now(UTC).replace(tzinfo=None)
        start_date = (today - timedelta(days=days)).strftime("%Y-%m-%d")
        end_date = today.strftime("%Y-%m-%d")

        hist_json = await get_historical_readings(device_id, start_date, end_date, "1h")
        hist = json.loads(hist_json)
        if "error" in hist:
            return json.dumps(hist)

        readings = hist.get("readings", [])
        trends = detect_trends(readings, days)

        return json.dumps({
            "device_id": device_id,
            "days_analyzed": days,
            "readings_used": len(readings),
            "trends": [dataclasses.asdict(t) for t in trends],
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
        JSON with per-port on_hours, off_hours, transitions, avg_speed_when_running,
        uptime_pct, and peak_hour_utc (UTC hour 0–23).
    """
    try:
        if not 1 <= days <= 30:
            return json.dumps({"error": "days must be between 1 and 30"})

        today = datetime.now(UTC).replace(tzinfo=None)
        start_date = (today - timedelta(days=days)).strftime("%Y-%m-%d")
        end_date = today.strftime("%Y-%m-%d")

        hist_json = await get_historical_readings(device_id, start_date, end_date, "raw")
        hist = json.loads(hist_json)
        if "error" in hist:
            return json.dumps(hist)

        readings = hist.get("readings", [])
        ports = build_activity_report(readings)

        return json.dumps({
            "device_id": device_id,
            "days_analyzed": days,
            "readings_used": len(readings),
            "ports": [dataclasses.asdict(p) for p in ports],
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

# modeType=15 signals Advance Automation control. NOT added to _MODE_LABELS — doing so
# would allow set_port_mode(mode="ADVANCE") to write atType=15 and trigger 999999 errors.
_ADVANCE_MODE_TYPE: int = 15


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
    if minutes is None or minutes == 65535:
        return None
    if not (0 <= minutes < 1440):
        return None
    h, m = divmod(minutes, 60)
    return f"{h:02d}:{m:02d}"


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


def _sanitize_api_string(value: str | None, max_len: int) -> str:
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


async def _check_advance_mode(dev_id: str | None, port: int, fallback: str) -> str:
    """Secondary call to getdevModeSettingList to verify ADVANCE state.

    Used for AI+ devices (no curMode field) and firmware without isOpenAutomation.
    Falls back gracefully on any error — mode accuracy is best-effort for these cases.
    """
    if not dev_id:
        return fallback
    try:
        settings = await asyncio.to_thread(_client().get_mode_settings, dev_id, port)
        return "ADVANCE" if settings.get("modeType") == _ADVANCE_MODE_TYPE else fallback
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


def _build_advance_conflict_response(
    device_id: str, port: int, port_name: str
) -> str:
    """Build a structured ADVANCE_AUTOMATION conflict response for write tools.

    The automation management API endpoints are not yet confirmed — automation name
    and co-governed ports are not available until network capture is done (see Quirk 18
    in docs/API.md). Returns a fully structured conflict response with all fields present.
    """
    summary = (
        f"{port_name} (Port {port}) is under Advance Automation control. "
        "Your change requires resolving this conflict first."
    )
    return json.dumps({
        "conflict": "ADVANCE_AUTOMATION",
        "summary": summary,
        "target_port": f"{port_name} (Port {port})",
        "automation_name": None,
        "automation_id": None,
        "co_governed_ports": [],
        "switching_guidance": (
            "To manage automations, use the AC Infinity app or provide a network capture "
            "to enable the automation management tools."
        ),
        "options": {
            "1_break_out_manually": {
                "available": False,
                "status": "requires_automation_api_discovery",
            },
            "2_edit_automation": {
                "available": False,
                "status": "not_yet_implemented",
            },
            "3_fork_automation": {
                "available": False,
                "status": "not_yet_implemented",
            },
        },
    }, indent=2)


@mcp_server.tool()
async def get_port_status(device_id: str, port: int) -> str:
    """
    Get the live operational status of a single port.

    Reads real-time fields from the device info response: actual current power
    level, load detection, active automation mode, and remaining timer seconds.

    Args:
        device_id: The AC Infinity device code (from discover_devices)
        port: 1-based port number

    Returns:
        JSON example::

            {
              "device_id": "C58ZA",
              "port": 1,
              "port_name": "Intake Fan",
              "power_level": 5,
              "load_detected": true,
              "mode": "AUTO",
              "remain_time_seconds": 0
            }

        ``mode`` is one of: OFF, ON, AUTO, TIMER_TO_ON, TIMER_TO_OFF, CYCLE, SCHEDULE, VPD,
        ADVANCE. ``load_detected`` is true when a device is physically plugged into the port.
        When ``mode`` is ``ADVANCE``, the port is governed by a named Advance Automation program
        in the AC Infinity app. On failure returns ``{"error": "...", "detail": "..."}``.

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

        return json.dumps({
            "device_id": device_id,
            "port": port,
            "port_name": port_data.get("portName", f"Port {port}"),
            "power_level": port_data.get("speak", 0),
            "load_detected": bool(port_data.get("loadState", 0)),
            "mode": mode_str,
            "remain_time_seconds": port_data.get("remainTime") or 0,
        }, indent=2)

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
        JSON example::

            {
              "device_id": "C58ZA",
              "port": 1,
              "mode": "AUTO",
              "speed_target": 5,
              "vpd_target_kpa": null,
              "temp_range_c": null,
              "humidity_range_pct": null,
              "schedule_window": null,
              "cycle_on_seconds": 300,
              "cycle_off_seconds": 60,
              "timer_on_seconds": 0,
              "timer_off_seconds": 0
            }

        ``vpd_target_kpa`` is non-null only when VPD automation is active.
        ``temp_range_c`` / ``humidity_range_pct`` are non-null only when those
        thresholds are enabled. ``schedule_window`` times are in device local time
        (not UTC). On failure returns ``{"error": "...", "detail": "..."}``.
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

        # ADVANCE detection: modeType=15 means this port is governed by a named Advance
        # Automation. All automation-target fields reflect automation-internal config and
        # are not meaningful for individual port management — return early with ADVANCE mode.
        if settings.get("modeType") == _ADVANCE_MODE_TYPE:
            return json.dumps({
                "device_id": device_id,
                "port": port,
                "mode": "ADVANCE",
                "advance_automation": True,
                "speed_target": settings.get("onSpead", 0),
                "vpd_target_kpa": None,
                "temp_range_c": None,
                "humidity_range_pct": None,
                "schedule_window": None,
                "cycle_on_seconds": None,
                "cycle_off_seconds": None,
                "timer_on_seconds": None,
                "timer_off_seconds": None,
            }, indent=2)

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

        temp_range = None
        if settings.get("activeLt") or settings.get("activeHt"):
            temp_range = {
                "min_c": settings.get("devLt", 0),
                "max_c": settings.get("devHt", 0),
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
            {"start": sched_start, "end": sched_end}
            if sched_start is not None and sched_end is not None
            else None
        )

        return json.dumps({
            "device_id": device_id,
            "port": port,
            "mode": _decode_mode(settings.get("atType")),
            "speed_target": settings.get("onSpead", 0),
            "vpd_target_kpa": vpd_target,
            "temp_range_c": temp_range,
            "humidity_range_pct": humi_range,
            "schedule_window": schedule_window,
            "cycle_on_seconds": settings.get("activeCycleOn", 0),
            "cycle_off_seconds": settings.get("activeCycleOff", 0),
            "timer_on_seconds": settings.get("acitveTimerOn", 0),
            "timer_off_seconds": settings.get("acitveTimerOff", 0),
        }, indent=2)

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

        Example (dry_run=True)::

            {
              "action": "set port 2 speed to 5",
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
            return json.dumps({
                "error": (
                    "AI+ controllers (devType=22) live write path is not yet implemented. "
                    "dry_run=True is fully supported and returns the payload that would be sent. "
                    "See docs/API.md for details."
                ),
                "device_id": device_id,
                "port": port,
                "dry_run": False,
                "controller_type": write_result["controller_type"],
            })

        response: dict = {
            "action": f"set port {port} speed to {speed}",
            "device_id": device_id,
            "port": port,
            "speed": speed,
            "dry_run": write_result["dry_run"],
            "controller_type": write_result["controller_type"],
            "sent": write_result["sent"],
        }
        if write_result["dry_run"]:
            response["payload"] = write_result["payload"]

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
        return _build_advance_conflict_response(device_id, port, port_name)
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
        and payload (when dry_run=True). On failure returns ``{"error": "..."}``.
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
            return json.dumps({
                "error": (
                    "AI+ controllers (devType=22) live write path is not yet implemented. "
                    "dry_run=True is fully supported and returns the payload that would be sent. "
                    "See docs/API.md for details."
                ),
                "device_id": device_id,
                "port": port,
                "dry_run": False,
                "controller_type": write_result["controller_type"],
            })

        response: dict = {
            "action": f"turn port {port} on",
            "device_id": device_id,
            "port": port,
            "dry_run": write_result["dry_run"],
            "controller_type": write_result["controller_type"],
            "sent": write_result["sent"],
        }
        if write_result["dry_run"]:
            response["payload"] = write_result["payload"]

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
        return _build_advance_conflict_response(device_id, port, port_name)
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
        and payload (when dry_run=True). On failure returns ``{"error": "..."}``.
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
            return json.dumps({
                "error": (
                    "AI+ controllers (devType=22) live write path is not yet implemented. "
                    "dry_run=True is fully supported and returns the payload that would be sent. "
                    "See docs/API.md for details."
                ),
                "device_id": device_id,
                "port": port,
                "dry_run": False,
                "controller_type": write_result["controller_type"],
            })

        response: dict = {
            "action": f"turn port {port} off",
            "device_id": device_id,
            "port": port,
            "dry_run": write_result["dry_run"],
            "controller_type": write_result["controller_type"],
            "sent": write_result["sent"],
        }
        if write_result["dry_run"]:
            response["payload"] = write_result["payload"]

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
        return _build_advance_conflict_response(device_id, port, port_name)
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
            "AI+ controllers (devType=22) live write path is not yet implemented. "
            "dry_run=True is fully supported and returns the payload that would be sent. "
            "See docs/API.md for details."
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

        response: dict = {
            "action": f"set port {port} VPD automation to {target_vpd} kPa",
            "device_id": device_id,
            "port": port,
            "target_vpd_kpa": target_vpd,
            "dry_run": write_result["dry_run"],
            "controller_type": write_result["controller_type"],
            "sent": write_result["sent"],
        }
        if write_result["dry_run"]:
            response["payload"] = write_result["payload"]
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
        return _build_advance_conflict_response(device_id, port, port_name)
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
    min_c: float,
    max_c: float,
    dry_run: bool = True,
) -> str:
    """Enable temperature automation on a port using the built-in temperature sensor.

    Switches the port to AUTO mode (atType=3) and sets the temperature thresholds.
    The controller speeds up when temperature exceeds max_c and slows down below
    min_c. Uses read-before-write. Defaults to dry_run=True.

    Args:
        device_id: Device code from discover_devices (e.g. "C58ZA").
        port: 1-based port number.
        min_c: Minimum temperature threshold in °C, range 0–50. Sub-degree values
            are rounded to the nearest integer (e.g. 20.5 → 21).
        max_c: Maximum temperature threshold in °C, range 0–50. Must exceed min_c.
            Sub-degree values are rounded to the nearest integer.
        dry_run: If True (default), returns the payload that would be sent
            without writing.

    Returns:
        JSON with action, device_id, port, min_c, max_c, dry_run,
        controller_type, sent, and payload (when dry_run=True).
        On failure returns ``{"error": "..."}``.
    """
    try:
        if port < 1:
            return json.dumps({"error": "port must be a positive integer"})
        if not (0 <= min_c <= 50 and 0 <= max_c <= 50):
            return json.dumps({"error": "min_c and max_c must be between 0 and 50°C"})
        if min_c >= max_c:
            return json.dumps({"error": "min_c must be less than max_c"})

        devices = await asyncio.to_thread(_client().get_devices)
        device = next((d for d in devices if d.get("devCode") == device_id), None)
        if not device:
            return json.dumps({"error": f"Device {device_id} not found"})

        updates = {
            "atType": 3,  # AUTO mode
            # raw °C integer — no ×100 scaling. int(x + 0.5) is round-half-up;
            # round() uses banker's rounding and would silently disagree with
            # the docstring at half-integer inputs (e.g. round(20.5) == 20).
            "devLt": int(min_c + 0.5),
            "devHt": int(max_c + 0.5),
            "activeLt": 1,
            "activeHt": 1,
        }
        write_result = await asyncio.to_thread(
            _client().set_port_mode, device, port, updates, dry_run
        )

        if write_result.get("ai_plus_write_unsupported"):
            return _ai_plus_unsupported_error(device_id, port, write_result["controller_type"])

        response: dict = {
            "action": f"set port {port} temperature automation {min_c}–{max_c}°C",
            "device_id": device_id,
            "port": port,
            "min_c": min_c,
            "max_c": max_c,
            "dry_run": write_result["dry_run"],
            "controller_type": write_result["controller_type"],
            "sent": write_result["sent"],
        }
        if write_result["dry_run"]:
            response["payload"] = write_result["payload"]
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
        return _build_advance_conflict_response(device_id, port, port_name)
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

        response: dict = {
            "action": f"set port {port} humidity automation {min_rh}–{max_rh}%",
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
        return _build_advance_conflict_response(device_id, port, port_name)
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

        response: dict = {
            "action": f"set port {port} mode to {mode_upper}",
            "device_id": device_id,
            "port": port,
            "mode": mode_upper,
            "dry_run": write_result["dry_run"],
            "controller_type": write_result["controller_type"],
            "sent": write_result["sent"],
        }
        if write_result["dry_run"]:
            response["payload"] = write_result["payload"]
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
        return _build_advance_conflict_response(device_id, port, port_name)
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
        return _build_advance_conflict_response(device_id, port, port_name)
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

    response: dict = {
        "action": "apply grow stage template",
        "device_id": device_id,
        "port": port,
        "stage": stage,
        "dry_run": write_result["dry_run"],
        "controller_type": write_result["controller_type"],
        "sent": write_result["sent"],
        "vpd": {"target_kpa": target_vpd},
        "temperature": {"min_c": temp_min, "max_c": temp_max},
        "humidity": {"min_rh": humi_min, "max_rh": humi_max},
    }
    if write_result["dry_run"]:
        response["payload"] = write_result["payload"]

    return json.dumps(response, indent=2)


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
