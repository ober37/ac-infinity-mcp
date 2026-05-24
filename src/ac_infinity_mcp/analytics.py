"""Analytics: health scoring, trend detection, activity reporting.

Pure functions — no API calls. All data comes from client.py responses.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# h/day: zero-load ports below this are treated as ghost candidates
_GHOST_LOAD_ZERO_THRESHOLD: float = 1.0

STAGE_TARGETS: dict[str, dict[str, tuple[float, float]]] = {
    "clones":       {"temp_c": (22.0, 26.0), "humidity": (70.0, 80.0), "vpd": (0.8, 1.2)},
    "seedling":     {"temp_c": (22.0, 26.0), "humidity": (65.0, 75.0), "vpd": (0.8, 1.2)},
    "veg":          {"temp_c": (20.0, 28.0), "humidity": (50.0, 70.0), "vpd": (1.0, 1.5)},
    "early_flower": {"temp_c": (20.0, 26.0), "humidity": (40.0, 60.0), "vpd": (1.0, 1.8)},
    "mid_flower":   {"temp_c": (18.0, 25.0), "humidity": (35.0, 55.0), "vpd": (1.2, 2.0)},
    "late_flower":  {"temp_c": (18.0, 24.0), "humidity": (30.0, 50.0), "vpd": (1.2, 1.8)},
}

_DEFAULT_STAGE = "veg"


@dataclass
class HealthScore:
    score: float
    grade: str
    vpd_score: float
    temp_score: float
    humidity_score: float
    top_recommendation: str


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
    peak_hour_utc: int | None = None


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
            top_recommendation = "VPD is low — lower fan speed or increase humidity to raise VPD."
        else:
            top_recommendation = (
                "VPD is high — increase air circulation or reduce humidity to lower VPD."
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


def build_activity_report(
    readings: list[dict[str, Any]],
    days: int = 1,
    port_loads: dict[int, int] | None = None,
) -> list[ActivityReport]:
    """Build per-port runtime activity report from parsed history readings.

    Each reading is treated as one equal time slice.
    on_hours/off_hours are cumulative hours over the full ``days`` window.
    """
    days = max(days, 1)  # defense-in-depth: prevents ZeroDivisionError in Rule B
    if not port_loads:
        # normalizes {} to None — empty dict would enable Rule A with all-zero defaults
        port_loads = None
    if not readings:
        return []

    port_data: dict[int, dict[str, Any]] = {}

    for r in readings:
        ts_str = r.get("timestamp", "")
        try:
            ts_hour = datetime.fromisoformat(ts_str.rstrip("Z")).hour
        except (ValueError, AttributeError, TypeError):
            ts_hour = None

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
                    "prev_on": None,
                    "transitions": 0,
                }

            on = port.get("on", False) or (port.get("speed", 0) or 0) > 0
            speed = port.get("speed", 0) or 0

            pd = port_data[port_num]
            pd["speeds"].append(speed)
            pd["on_flags"].append(on)
            if ts_hour is not None:
                pd["hours"].append(ts_hour if on else None)

            if pd["prev_on"] is not None and on != pd["prev_on"]:
                pd["transitions"] += 1
            pd["prev_on"] = on

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

        hour_counts: dict[int, int] = {}
        for h in pd["hours"]:
            if h is not None:
                hour_counts[h] = hour_counts.get(h, 0) + 1
        peak_hour_utc = max(hour_counts, key=hour_counts.get) if hour_counts else None  # type: ignore[arg-type]

        reports.append(
            ActivityReport(
                port=port_num,
                name=pd["name"],
                on_hours=on_hours,
                off_hours=off_hours,
                transitions=pd["transitions"],
                avg_speed_when_running=avg_speed,
                uptime_pct=uptime_pct,
                peak_hour_utc=peak_hour_utc,
            )
        )

    filtered: list[ActivityReport] = []
    for rep in reports:
        # Rule A: constant-100%-uptime ghost port with no current draw (unchanged)
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
        # Rule D: named port showing only toggle-speed history with no current draw.
        # Covers unconnected ports and toggle devices (heaters, lights, humidifiers)
        # currently off — history is unreliable for these (issues #85, #98).
        if (
            port_loads is not None
            and port_loads.get(rep.port, 0) == 0
            and rep.avg_speed_when_running <= 1.0
        ):
            continue
        filtered.append(rep)
    return filtered
