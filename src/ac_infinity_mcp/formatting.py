from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ac_infinity_mcp.automation import _sanitize_api_string


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
