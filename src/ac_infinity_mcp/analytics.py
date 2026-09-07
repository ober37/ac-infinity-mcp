"""Analytics: health scoring, trend detection, activity reporting.

Pure functions — no API calls. All data comes from client.py responses.
"""

from __future__ import annotations

import logging
import re
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# Lives in schema.py, not here: client.py enforces it on the write path, and a
# client -> analytics import would point the low-level layer at the high-level one.
from ac_infinity_mcp.schema import TOGGLE_LOAD_TYPES as _TOGGLE_LOAD_TYPES

logger = logging.getLogger(__name__)

_DURATION_RE = re.compile(r"^(\d+)(m|h|d)$", re.IGNORECASE)
_DURATION_UNITS = {"m": 60, "h": 3600, "d": 86400}

# h/day: zero-load ports below this are treated as ghost candidates
_GHOST_LOAD_ZERO_THRESHOLD: float = 1.0

# devType values where portsLoad is always 0 regardless of actual current draw (Quirk 24)
_ZERO_LOAD_DEV_TYPES: frozenset[int] = frozenset({18, 22})

STAGE_TARGETS: dict[str, dict[str, tuple[float, float]]] = {
    "clones":       {"temp_c": (22.0, 26.0), "humidity": (70.0, 80.0), "vpd": (0.8, 1.2)},
    "seedling":     {"temp_c": (22.0, 26.0), "humidity": (65.0, 75.0), "vpd": (0.8, 1.2)},
    "veg":          {"temp_c": (20.0, 28.0), "humidity": (50.0, 70.0), "vpd": (1.0, 1.5)},
    "early_flower": {"temp_c": (20.0, 26.0), "humidity": (40.0, 60.0), "vpd": (1.0, 1.8)},
    "mid_flower":   {"temp_c": (18.0, 25.0), "humidity": (35.0, 55.0), "vpd": (1.2, 2.0)},
    "late_flower":  {"temp_c": (18.0, 24.0), "humidity": (30.0, 50.0), "vpd": (1.2, 1.8)},
}

_DEFAULT_STAGE = "veg"

# Minimum number of consecutive readings a state must persist to count as a real transition.
# Single-reading blips at automation window boundaries are API artifacts (Quirk 22).
_MIN_DWELL_READINGS: int = 2


@dataclass
class HealthScore:
    score: float
    grade: str
    vpd_score: float
    temp_score: float
    humidity_score: float
    top_recommendation: str
    temperature_c: float = 0.0
    temperature_f: float = 0.0
    humidity_pct: float = 0.0
    vpd_kpa: float = 0.0


@dataclass
class TrendReport:
    metric: str
    slope: float          # change per hour
    direction: str        # "rising", "falling", "flat"
    seven_day_projection: float
    alert: bool


@dataclass
class ActivityReport:
    port: int
    name: str
    on_hours: float
    off_hours: float
    transitions: int
    avg_speed_when_running: float
    uptime_pct: float
    peak_hour_utc: datetime | None = None  # full naive UTC datetime of peak slot
    data_quality: str | None = None  # "api_constant_speed" when toggle-device history is unreliable


def _range_score(value: float, low: float, high: float) -> float:
    """Score 0-100 based on how well value sits within [low, high]."""
    if low <= value <= high:
        return 100.0
    margin = (high - low) / 2.0
    if margin == 0:
        return 0.0
    deviation = min(abs(value - low), abs(value - high))
    return round(max(0.0, 100.0 - min(deviation / margin, 1.0) * 100.0), 1)


def _grade(score: float) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"


def calculate_health_score(reading: dict[str, Any], stage: str) -> HealthScore:
    """Calculate composite 0-100 environment health score.

    Weights: VPD 40%, temperature 30%, humidity 30%.
    """
    targets = STAGE_TARGETS.get(stage, STAGE_TARGETS[_DEFAULT_STAGE])
    vpd_low, vpd_high = targets["vpd"]
    temp_low, temp_high = targets["temp_c"]
    hum_low, hum_high = targets["humidity"]

    vpd_score = _range_score(float(reading.get("vpd", 0)), vpd_low, vpd_high)
    temp_score = _range_score(float(reading.get("temperature_c", 0)), temp_low, temp_high)
    humidity_score = _range_score(float(reading.get("humidity", 0)), hum_low, hum_high)

    score = round(vpd_score * 0.4 + temp_score * 0.3 + humidity_score * 0.3, 1)
    grade = _grade(score)

    worst_metric = min(
        [("vpd", vpd_score), ("temp", temp_score), ("humidity", humidity_score)],
        key=lambda x: x[1],
    )[0]

    vpd = float(reading.get("vpd", 0))
    temp_c = float(reading.get("temperature_c", 0))
    humidity = float(reading.get("humidity", 0))

    if vpd_score == 100.0 and temp_score == 100.0 and humidity_score == 100.0:
        top_recommendation = "All metrics within target range. No action needed."
    elif worst_metric == "vpd":
        if vpd < vpd_low:
            top_recommendation = "VPD is low — lower humidity or raise temperature to increase VPD."
        else:
            top_recommendation = (
                "VPD is high — raise humidity or lower temperature to reduce VPD."
            )
    elif worst_metric == "temp":
        if temp_c < temp_low:
            top_recommendation = "Temperature is low — raise grow room temperature."
        else:
            top_recommendation = "Temperature is high — lower grow room temperature."
    else:
        if humidity < hum_low:
            top_recommendation = "Humidity is low — add a misting cycle or humidifier."
        else:
            top_recommendation = "Humidity is high — add a dehumidifier or increase airflow."

    return HealthScore(
        score=score,
        grade=grade,
        vpd_score=vpd_score,
        temp_score=temp_score,
        humidity_score=humidity_score,
        top_recommendation=top_recommendation,
        temperature_c=temp_c,
        temperature_f=float(reading.get("temperature_f", 0)),
        humidity_pct=humidity,
        vpd_kpa=vpd,
    )


def detect_trends(readings: list[dict[str, Any]], days: int) -> list[TrendReport]:
    """Detect linear trends across temperature, humidity, and VPD.

    Uses statistics.linear_regression (Python 3.11+ stdlib).
    Slope is expressed as change-per-hour.
    Alert thresholds: temp >3°C total change, humidity >15%, vpd >0.5 kPa.
    """
    if not readings:
        return []

    metrics = ["temperature_c", "humidity", "vpd"]
    reports: list[TrendReport] = []

    for metric in metrics:
        points: list[tuple[float, float]] = []
        for r in readings:
            ts_str = r.get("timestamp", "")
            val = r.get(metric)
            if val is None or not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str.rstrip("Z")).timestamp()
                points.append((ts, float(val)))
            except (ValueError, TypeError):
                continue

        if len(points) < 2:
            last_val = points[0][1] if points else 0.0
            reports.append(
                TrendReport(
                    metric=metric,
                    slope=0.0,
                    direction="flat",
                    seven_day_projection=round(last_val, 2),
                    alert=False,
                )
            )
            continue

        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        lr = statistics.linear_regression(xs, ys)
        slope_per_second = lr.slope

        slope_per_hour = slope_per_second * 3600
        last_y = ys[-1]
        projection = round(last_y + slope_per_second * 7 * 86400, 2)

        if slope_per_hour > 0.05:
            direction = "rising"
        elif slope_per_hour < -0.05:
            direction = "falling"
        else:
            direction = "flat"

        total_change = abs(slope_per_second * days * 86400)
        if metric == "temperature_c":
            alert = total_change > 3.0
        elif metric == "humidity":
            alert = total_change > 15.0
        else:  # vpd
            alert = total_change > 0.5

        reports.append(
            TrendReport(
                metric=metric,
                slope=round(slope_per_hour, 4),
                direction=direction,
                seven_day_projection=projection,
                alert=alert,
            )
        )

    return reports


def _count_debounced_transitions(on_flags: list[bool]) -> int:
    """Count state transitions that persist for at least _MIN_DWELL_READINGS consecutive readings.

    Single-reading state changes (API boundary nibbles at automation window edges) are
    not counted — only sustained transitions (new state held for ≥ 2 readings) are recorded.
    """
    if not on_flags:
        return 0
    count = 0
    prev_state = on_flags[0]
    i = 1
    while i < len(on_flags):
        if on_flags[i] != prev_state:
            j = i + 1
            while j < len(on_flags) and on_flags[j] == on_flags[i]:
                j += 1
            if (j - i) >= _MIN_DWELL_READINGS:
                count += 1
                prev_state = on_flags[i]
            i = j
        else:
            i += 1
    return count


def build_activity_report(
    readings: list[dict[str, Any]],
    days: int = 1,
    port_loads: dict[int, int] | None = None,
    port_load_types: dict[int, int] | None = None,
    dev_type: int | None = None,
) -> list[ActivityReport]:
    """Build per-port runtime activity report from parsed history readings.

    Each reading is treated as one equal time slice.
    on_hours/off_hours are cumulative hours over the full ``days`` window.
    """
    days = max(days, 1)  # defense-in-depth: prevents ZeroDivisionError in Rule B
    if not port_loads:
        # normalizes {} to None — empty dict would enable Rule A with all-zero defaults
        port_loads = None
    if dev_type in _ZERO_LOAD_DEV_TYPES:
        # Quirk 24: this device always reports portsLoad=0; load-based ghost rules are unreliable
        port_loads = None
    if not readings:
        return []

    port_data: dict[int, dict[str, Any]] = {}

    for r in readings:
        ts_str = r.get("timestamp", "")
        try:
            ts_dt: datetime | None = datetime.fromisoformat(ts_str.rstrip("Z")).replace(tzinfo=None)
        except (ValueError, AttributeError, TypeError):
            ts_dt = None

        for port in r.get("ports", []):
            port_num = port.get("port")
            if port_num is None:
                continue
            if port_num not in port_data:
                port_data[port_num] = {
                    "name": port.get("name", f"Port {port_num}"),
                    "speeds": [],
                    "on_flags": [],
                    "hours": [],
                }

            on = port.get("on", False) or (port.get("speed", 0) or 0) > 0
            speed = port.get("speed", 0) or 0

            pd = port_data[port_num]
            pd["speeds"].append(speed)
            pd["on_flags"].append(on)
            pd["hours"].append(ts_dt if on else None)

    reports: list[ActivityReport] = []
    for port_num in sorted(port_data.keys()):
        pd = port_data[port_num]
        total = len(pd["on_flags"])
        # pragma below: port_data is only populated when on_flags is appended in the same step
        if total == 0:  # pragma: no cover
            continue

        on_count = sum(1 for f in pd["on_flags"] if f)

        on_hours = round(on_count / total * 24 * days, 2)
        off_hours = round(days * 24 - on_hours, 2)

        running_speeds = [s for s, f in zip(pd["speeds"], pd["on_flags"]) if f and s > 0]
        avg_speed = round(sum(running_speeds) / len(running_speeds), 2) if running_speeds else 0.0

        uptime_pct = round(on_count / total * 100, 1)

        slot_counts: dict[datetime, int] = {}
        for h_dt in pd["hours"]:
            if h_dt is not None:
                slot = h_dt.replace(minute=0, second=0, microsecond=0)
                slot_counts[slot] = slot_counts.get(slot, 0) + 1
        if slot_counts:
            weighted: list[datetime] = []
            for slot in sorted(slot_counts.keys()):
                weighted.extend([slot] * slot_counts[slot])
            peak_hour_utc: datetime | None = weighted[len(weighted) // 2]
        else:
            peak_hour_utc = None

        transitions = _count_debounced_transitions(pd["on_flags"])

        # Detect toggle-device history artifact: AC Infinity always emits nibble 0xF
        # (decoded speed=1) for heaters/lights/humidifiers, even when physically off.
        # Confirmed toggle hardware (_TOGGLE_LOAD_TYPES) is sufficient. For _ZERO_LOAD_DEV_TYPES,
        # loadType is also unreliable (Quirk 24), so the pattern alone is used instead —
        # a variable-speed device stuck at speed 1 is indistinguishable, but that is an
        # acceptable trade-off given the load signal is completely absent on these devices.
        all_running_are_one = bool(running_speeds) and all(s == 1 for s in running_speeds)
        is_toggle_hardware = (
            port_load_types is not None
            and port_load_types.get(port_num) in _TOGGLE_LOAD_TYPES
        )
        is_toggle_pattern = (
            transitions == 0 and uptime_pct == 100.0 and all_running_are_one
        )
        data_quality: str | None = None
        if is_toggle_pattern and (is_toggle_hardware or dev_type in _ZERO_LOAD_DEV_TYPES):
            data_quality = "api_constant_speed"
        if dev_type in _ZERO_LOAD_DEV_TYPES and data_quality is None:
            data_quality = "no_load_signal"

        reports.append(
            ActivityReport(
                port=port_num,
                name=pd["name"],
                on_hours=on_hours,
                off_hours=off_hours,
                transitions=transitions,
                avg_speed_when_running=avg_speed,
                uptime_pct=uptime_pct,
                peak_hour_utc=peak_hour_utc,
                data_quality=data_quality,
            )
        )

    # Rule F: phantom clone detection — custom-named ports that share identical activity
    # signatures AND have low activity are phantom artifacts of legacy controllers (devType=11)
    # reporting disconnected port history. Only fires when the matching group is a proper
    # subset of all ports (at least one real port remains after exclusion).
    phantom_ports: set[int] = set()
    if port_loads is not None:
        sig_groups: dict[tuple[float, int, datetime | None], list[int]] = {}
        for rep in reports:
            if re.match(r"^Port \d+$", str(rep.name)):
                continue
            sig: tuple[float, int, datetime | None] = (
                rep.uptime_pct, rep.transitions, rep.peak_hour_utc
            )
            sig_groups.setdefault(sig, []).append(rep.port)
        port_map = {r.port: r for r in reports}
        for port_nums in sig_groups.values():
            if len(port_nums) < 2:
                continue
            if not all(
                (port_map[p].on_hours / days) < _GHOST_LOAD_ZERO_THRESHOLD for p in port_nums
            ):
                continue
            port_set = set(port_nums)
            if all(r.port in port_set for r in reports):
                continue  # proper-subset guard: don't exclude every port
            phantom_ports.update(port_nums)

    filtered: list[ActivityReport] = []
    for rep in reports:
        # Ports with the api_constant_speed caveat are never ghost-filtered — the caveat
        # is the right signal for the grower, not silence. This must run before Rules A/D
        # because both rules would otherwise filter the same toggle-hardware pattern.
        if rep.data_quality == "api_constant_speed":
            filtered.append(rep)
            continue
        # Rule F: phantom clone — identical low-activity signature across custom-named ports.
        if rep.port in phantom_ports:
            continue
        # Rule G: custom-named port on _ZERO_LOAD_DEV_TYPES with toggle-hardware speed and low
        # activity. api_constant_speed ports exit before this point and are kept with caveat.
        if (
            dev_type in _ZERO_LOAD_DEV_TYPES
            and not re.match(r"^Port \d+$", str(rep.name))
            and rep.avg_speed_when_running == 1.0
            and (rep.on_hours / days) < _GHOST_LOAD_ZERO_THRESHOLD
        ):
            continue
        # Rule A: constant-100%-uptime ghost port with no current draw.
        if (
            rep.transitions == 0
            and rep.uptime_pct == 100.0
            and port_loads is not None
            and port_loads.get(rep.port, 0) == 0
        ):
            continue
        # Rule B (enhanced): auto-named port — low avg runtime OR zero load when data available
        if re.match(r"^Port \d+$", str(rep.name)):
            if (rep.on_hours / days) < _GHOST_LOAD_ZERO_THRESHOLD:
                continue
            if port_loads is not None and port_loads.get(rep.port, 0) == 0:
                continue
        # Rule C: named port with zero transitions, zero load, sub-threshold runtime
        if (
            rep.transitions == 0
            and port_loads is not None
            and port_loads.get(rep.port, 0) == 0
            and (rep.on_hours / days) < _GHOST_LOAD_ZERO_THRESHOLD
        ):
            continue
        # Rule D: non-toggle named port with speed history ≤ 1 and no current draw.
        # Confirmed toggle hardware (_TOGGLE_LOAD_TYPES) with transitions > 0 is exempt —
        # it ran and the grower should see the data. The data_quality early-exit above
        # handles the 100%-uptime constant-speed artifact for toggle hardware. Any
        # non-toggle port reaching this point with avg_speed ≤ 1.0 and zero load is a
        # ghost artifact (stale nibble 0xF recorded after the device was off).
        is_toggle = (
            port_load_types is not None
            and port_load_types.get(rep.port) in _TOGGLE_LOAD_TYPES
        )
        if (
            not is_toggle
            and port_loads is not None
            and port_loads.get(rep.port, 0) == 0
            and rep.avg_speed_when_running <= 1.0
        ):
            continue
        # Rule E: named port, non-toggle hardware, zero current load, sub-threshold runtime.
        # transitions > 0 precondition: ports with transitions==0 and load==0 are already
        # eliminated by Rule C, so Rule E is only reachable when transitions > 0.
        # The history API records a port's previously-configured speed even after OFF,
        # producing phantom records with avg_speed>1 and non-zero transitions (Issue #101).
        # Sub-threshold guard keeps ports that genuinely ran briefly but were polled while off.
        # days is clamped to min 1 upstream in this function — division is safe.
        if (
            rep.transitions > 0
            and port_loads is not None
            and port_loads.get(rep.port, 0) == 0
            and rep.avg_speed_when_running > 1.0
            and (rep.on_hours / days) < _GHOST_LOAD_ZERO_THRESHOLD
        ):
            continue
        filtered.append(rep)
    return filtered


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
            # Handle both UTC-naive (..."T...Z") and aware (...+HH:MM) timestamps.
            # The historical-data parser always emits the naive-Z form today, but
            # a future fixture or hand-crafted payload could carry a non-UTC offset
            # — converting via astimezone preserves the instant, whereas
            # .replace(tzinfo=UTC) would silently corrupt it.
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
            # real bugs in the parser layer.
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
