"""Unit tests for analytics pure functions: health score, trends, activity."""

import pytest

from ac_infinity_mcp.analytics import (
    _GHOST_LOAD_ZERO_THRESHOLD,
    _ZERO_LOAD_DEV_TYPES,
    STAGE_TARGETS,
    HealthScore,
    TrendReport,
    _count_debounced_transitions,
    _grade,
    build_activity_report,
    calculate_health_score,
    detect_trends,
)
from ac_infinity_mcp.schema import calculate_vpd


def _reading(temp_c=24.0, humidity=60.0, vpd=1.24):
    return {
        "temperature_c": temp_c,
        "temperature_f": round(temp_c * 9 / 5 + 32, 1),
        "humidity": humidity,
        "vpd": vpd,
    }


def _ts(hour: int, minute: int = 0, day: int = 25) -> str:
    return f"2024-04-{day:02d}T{hour:02d}:{minute:02d}:00Z"


def _history_reading(hour: int, temp_c: float, humidity: float, vpd: float,
                     ports=None, day: int = 25) -> dict:
    return {
        "timestamp": _ts(hour, day=day),
        "temperature_c": temp_c,
        "temperature_f": round(temp_c * 9 / 5 + 32, 1),
        "humidity": humidity,
        "vpd": vpd,
        "ports": ports or [],
    }


# ============ calculate_health_score ============

def test_calculate_health_score_all_in_range():
    # veg: temp 20-28, humidity 50-70, vpd 1.0-1.5
    result = calculate_health_score(_reading(temp_c=24.0, humidity=60.0, vpd=1.24), "veg")
    assert isinstance(result, HealthScore)
    assert result.score >= 90.0
    assert result.grade == "A"


def test_calculate_health_score_vpd_low():
    result = calculate_health_score(_reading(vpd=0.1), "veg")
    assert result.vpd_score < 100.0


def test_calculate_health_score_vpd_high():
    result = calculate_health_score(_reading(vpd=3.0), "veg")
    assert result.vpd_score < 100.0


def test_calculate_health_score_temp_out_of_range():
    # veg temp max is 28°C
    result = calculate_health_score(_reading(temp_c=40.0), "veg")
    assert result.temp_score < 100.0


def test_calculate_health_score_humidity_low():
    # veg humidity min is 50%
    result = calculate_health_score(_reading(humidity=20.0), "veg")
    assert result.humidity_score < 100.0


@pytest.mark.parametrize("vpd,temp,hum,expected_grade", [
    # Dial each metric to produce known composite scores
    # all perfect → 100 → A
    (1.24, 24.0, 60.0, "A"),
])
def test_calculate_health_score_all_in_range_is_A(vpd, temp, hum, expected_grade):
    result = calculate_health_score(_reading(temp_c=temp, humidity=hum, vpd=vpd), "veg")
    assert result.grade == expected_grade


def test_calculate_health_score_grade_mapping():
    """Verify grade boundaries A≥90, B≥80, C≥70, D≥60, F<60."""
    # We test grade logic indirectly via the _grade helper by checking boundary combos.
    # vpd_score=0, temp=100, hum=100 → 0*0.4 + 100*0.3 + 100*0.3 = 60 → "D"
    result = calculate_health_score(_reading(vpd=0.0), "veg")
    assert result.score == pytest.approx(60.0, abs=5.0)

    # vpd in-range, temp/hum also in-range → "A"
    result_a = calculate_health_score(_reading(temp_c=24.0, humidity=60.0, vpd=1.24), "veg")
    assert result_a.grade == "A"


@pytest.mark.parametrize("score,expected", [
    # At-boundary (inclusive lower bound)
    (100.0, "A"),
    (90.0, "A"),
    (89.99, "B"),
    (80.0, "B"),
    (79.99, "C"),
    (70.0, "C"),
    (69.99, "D"),
    (60.0, "D"),
    (59.99, "F"),
    (0.0, "F"),
    # Mid-band
    (95.0, "A"),
    (85.0, "B"),
    (75.0, "C"),
    (65.0, "D"),
    (30.0, "F"),
])
def test_grade_boundaries(score, expected):
    """Pin grade boundaries so a regression to old thresholds (Ph15-D002) fails fast (P2-F006)."""
    assert _grade(score) == expected


def test_calculate_health_score_recommendation_all_ok():
    result = calculate_health_score(_reading(temp_c=24.0, humidity=60.0, vpd=1.24), "veg")
    assert "No action" in result.top_recommendation


def test_calculate_health_score_recommendation_vpd_low():
    result = calculate_health_score(_reading(vpd=0.1), "veg")
    # vpd is the worst metric
    assert "VPD" in result.top_recommendation or "vpd" in result.top_recommendation.lower()
    assert "raise VPD" in result.top_recommendation or "low" in result.top_recommendation.lower()


def test_calculate_health_score_recommendation_vpd_high():
    result = calculate_health_score(_reading(vpd=3.0), "veg")
    assert "VPD" in result.top_recommendation or "vpd" in result.top_recommendation.lower()
    assert "lower VPD" in result.top_recommendation or "high" in result.top_recommendation.lower()


def test_calculate_health_score_unknown_stage_defaults():
    result = calculate_health_score(_reading(), "unknown_stage_xyz")
    assert isinstance(result, HealthScore)
    assert 0 <= result.score <= 100


@pytest.mark.parametrize("stage", list(STAGE_TARGETS.keys()))
def test_calculate_health_score_all_stages_accepted(stage):
    result = calculate_health_score(_reading(), stage)
    assert isinstance(result, HealthScore)


def test_calculate_health_score_vpd_weighted_40pct():
    """vpd_score=0, temp=100, hum=100 → 0*0.4 + 100*0.3 + 100*0.3 = 60.0"""
    # Force vpd far out of range so vpd_score → 0; keep temp+hum in range
    veg = STAGE_TARGETS["veg"]
    temp_c = (veg["temp_c"][0] + veg["temp_c"][1]) / 2  # centre of range
    humidity = (veg["humidity"][0] + veg["humidity"][1]) / 2  # centre of range
    # vpd=0 is way below veg low (1.0), penalty should max out to 0
    result = calculate_health_score(_reading(temp_c=temp_c, humidity=humidity, vpd=0.0), "veg")
    assert result.vpd_score == 0.0
    assert result.temp_score == 100.0
    assert result.humidity_score == 100.0
    assert result.score == pytest.approx(60.0, abs=0.1)


# ============ detect_trends ============

def test_detect_trends_empty_readings():
    result = detect_trends([], days=7)
    assert result == []


def test_detect_trends_single_reading_insufficient():
    readings = [_history_reading(12, 24.0, 60.0, 1.24)]
    result = detect_trends(readings, days=7)
    assert len(result) == 3
    for r in result:
        assert r.slope == 0.0
        assert r.direction == "flat"


def test_detect_trends_flat():
    readings = [_history_reading(h, 24.0, 60.0, 1.24) for h in range(10)]
    result = detect_trends(readings, days=7)
    for r in result:
        assert r.direction == "flat"
        assert abs(r.slope) < 0.01


def test_detect_trends_rising_temperature():
    # Temperature rises 1°C per hour across 10 hours
    readings = [_history_reading(h, 20.0 + h, 60.0, 1.24) for h in range(10)]
    result = detect_trends(readings, days=7)
    temp_report = next(r for r in result if r.metric == "temperature_c")
    assert temp_report.direction == "rising"
    assert temp_report.slope > 0


def test_detect_trends_falling_humidity():
    # Humidity falls 2% per hour
    readings = [_history_reading(h, 24.0, 80.0 - h * 2, 1.24) for h in range(10)]
    result = detect_trends(readings, days=7)
    hum_report = next(r for r in result if r.metric == "humidity")
    assert hum_report.direction == "falling"
    assert hum_report.slope < 0


def test_detect_trends_seven_day_projection():
    # Rising temperature: last value 29°C, slope ~1°C/hr
    readings = [_history_reading(h, 20.0 + h, 60.0, 1.24) for h in range(10)]
    result = detect_trends(readings, days=7)
    temp_report = next(r for r in result if r.metric == "temperature_c")
    last_temp = 29.0
    # Projection should be > last value since trend is rising
    assert temp_report.seven_day_projection > last_temp


def test_detect_trends_alert_temp_large_drift():
    # Temperature rises 1°C per hour for 7 days → total = 168°C change >> 3°C threshold
    readings = [_history_reading(h % 24, 20.0 + h * 0.5, 60.0, 1.24, day=25 + h // 24)
                for h in range(48)]
    result = detect_trends(readings, days=2)
    temp_report = next(r for r in result if r.metric == "temperature_c")
    assert temp_report.alert is True


def test_detect_trends_alert_not_triggered_small_drift():
    # Tiny variation (0.01°C per hour) — should not trigger alert
    readings = [_history_reading(h, 24.0 + h * 0.01, 60.0, 1.24) for h in range(10)]
    result = detect_trends(readings, days=7)
    for r in result:
        assert r.alert is False


def test_detect_trends_missing_values_skipped():
    readings = [
        {"timestamp": _ts(0), "temperature_c": None, "humidity": 60.0, "vpd": 1.24},
        {"timestamp": _ts(1), "temperature_c": 24.0, "humidity": None, "vpd": 1.24},
        {"timestamp": _ts(2), "temperature_c": 25.0, "humidity": 61.0, "vpd": None},
    ]
    result = detect_trends(readings, days=1)
    assert len(result) == 3
    for r in result:
        assert isinstance(r, TrendReport)


def test_detect_trends_returns_three_metrics():
    readings = [_history_reading(h, 24.0, 60.0, 1.24) for h in range(5)]
    result = detect_trends(readings, days=7)
    metrics = [r.metric for r in result]
    assert "temperature_c" in metrics
    assert "humidity" in metrics
    assert "vpd" in metrics
    assert len(result) == 3


# ============ build_activity_report ============

def _port(port_num: int, name: str, speed: int, on: bool) -> dict:
    return {"port": port_num, "name": name, "speed": speed, "on": on}


def test_build_activity_report_empty():
    assert build_activity_report([]) == []


def test_build_activity_report_always_on():
    readings = [
        {
            "timestamp": _ts(h),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, "Fan", 5, True)],
        }
        for h in range(10)
    ]
    result = build_activity_report(readings, port_loads={1: 5})
    assert len(result) == 1
    assert result[0].uptime_pct == 100.0
    assert result[0].off_hours == 0.0


def test_build_activity_report_always_off():
    readings = [
        {
            "timestamp": _ts(h),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, "Fan", 0, False)],
        }
        for h in range(10)
    ]
    result = build_activity_report(readings)
    assert len(result) == 1
    assert result[0].uptime_pct == 0.0
    assert result[0].on_hours == 0.0


def test_build_activity_report_transitions():
    # All 4 transitions are sustained (each state held ≥ 2 readings) — no debounce drop.
    states = [False, False, True, True, False, False, True, True, False, False]
    readings = [
        {
            "timestamp": _ts(i),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, "Fan", 5 if on else 0, on)],
        }
        for i, on in enumerate(states)
    ]
    result = build_activity_report(readings)
    assert result[0].transitions == 4


def test_build_activity_report_avg_speed():
    readings = [
        {
            "timestamp": _ts(h),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, "Fan", 5, True)],
        }
        for h in range(4)
    ]
    result = build_activity_report(readings, port_loads={1: 5})
    assert result[0].avg_speed_when_running == 5.0


def test_build_activity_report_peak_hour_utc():
    readings = []
    # Hour 14 has 3 on-readings; others have 1
    for h in [10, 14, 14, 14, 18]:
        readings.append({
            "timestamp": _ts(h),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, "Fan", 5, True)],
        })
    result = build_activity_report(readings, port_loads={1: 5})
    assert result[0].peak_hour_utc is not None
    assert result[0].peak_hour_utc.hour == 14
    assert result[0].peak_hour_utc.day == 25


def test_build_activity_report_multiple_ports():
    # All 4 ports have non-zero activity above the ghost threshold so they
    # remain in the result regardless of ghost-port rules (incl. Rule F).
    readings = [
        {
            "timestamp": _ts(h),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [
                _port(1, "Fan 1", 5, True),
                _port(2, "Fan 2", 7, True),
                _port(3, "Light", 3, True),
                _port(4, "Heater", 1, True),
            ],
        }
        for h in range(5)
    ]
    result = build_activity_report(readings, port_loads={1: 5, 2: 5, 3: 3, 4: 1})
    assert len(result) == 4
    assert [r.port for r in result] == [1, 2, 3, 4]


def test_build_activity_report_uptime_pct_range():
    # 5 on, 5 off → 50%
    states = [True, True, True, True, True, False, False, False, False, False]
    readings = [
        {
            "timestamp": _ts(i),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, "Fan", 5 if on else 0, on)],
        }
        for i, on in enumerate(states)
    ]
    result = build_activity_report(readings)
    assert 0 <= result[0].uptime_pct <= 100
    assert result[0].uptime_pct == 50.0


# ============ calculate_vpd ============

def test_calculate_vpd_known_value():
    vpd = calculate_vpd(25.0, 60.0)
    assert 1.2 <= vpd <= 1.4


def test_calculate_vpd_saturated():
    assert calculate_vpd(25.0, 100.0) == 0.0


def test_calculate_vpd_zero_humidity():
    vpd = calculate_vpd(25.0, 0.0)
    assert vpd > 3.0


# ============ Residual coverage — degenerate / defensive paths ============

def test_health_score_temp_low_recommendation():
    """worst_metric == 'temp' with low temp triggers the 'raise temperature' branch."""
    reading = _reading(temp_c=10.0, humidity=60.0, vpd=1.24)
    result = calculate_health_score(reading, "veg")
    assert "Temperature is low" in result.top_recommendation


def test_health_score_grade_F_for_terrible_environment():
    """A reading way outside targets must produce an F grade (score < 60)."""
    reading = _reading(temp_c=45.0, humidity=10.0, vpd=4.0)
    result = calculate_health_score(reading, "veg")
    assert result.grade == "F"
    assert result.score < 60


def test_health_score_degenerate_range_returns_zero():
    """A degenerate target range (low == high) yields 0.0 outside the band."""
    from ac_infinity_mcp.analytics import _range_score
    # margin == 0 path — value outside the equal low/high
    assert _range_score(value=10.0, low=5.0, high=5.0) == 0.0


def test_detect_trends_skips_invalid_timestamps():
    """Records with bad timestamps are skipped in trend computation."""
    readings = [
        {"timestamp": "BAD_TS", "temperature_c": 24.0, "humidity": 55.0, "vpd": 1.4},
        {"timestamp": _ts(0), "temperature_c": 24.0, "humidity": 55.0, "vpd": 1.4},
        {"timestamp": _ts(1), "temperature_c": 25.0, "humidity": 56.0, "vpd": 1.5},
    ]
    trends = detect_trends(readings, days=1)
    assert len(trends) == 3  # one per metric (temp_c, humidity, vpd)


def test_activity_report_skips_records_with_bad_timestamp_hour():
    """Records with unparseable timestamps still feed port stats but no hour is recorded."""
    readings = [
        {"timestamp": "BAD_TS", "ports": [_port(1, "Fan", 5, True)]},
        {"timestamp": _ts(8), "ports": [_port(1, "Fan", 5, True)]},
    ]
    result = build_activity_report(readings)
    assert len(result) == 1
    assert result[0].port == 1


def test_activity_report_skips_port_with_no_number():
    """Ports missing a 'port' key are skipped."""
    readings = [
        {
            "timestamp": _ts(8),
            "ports": [
                {"name": "Headless", "speed": 5, "on": True},  # no port_num
                _port(1, "Fan", 5, True),
            ],
        }
    ]
    result = build_activity_report(readings)
    assert len(result) == 1
    assert result[0].port == 1


def test_activity_report_skips_port_with_zero_total_readings():
    """A port with no on_flags entries is skipped from the report."""
    # Edge case: empty readings list yields no reports
    result = build_activity_report([])
    assert result == []


# ============ build_activity_report — days param and peak_hour_utc fixes (#57 #58) ============

def _port_readings_for_days(on_count: int, off_count: int) -> list[dict]:
    """Generate a flat list of on/off port readings with sequential timestamps."""
    readings = []
    for i in range(on_count):
        readings.append({
            "timestamp": _ts(i % 24, day=25 + i // 24),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, "Fan", 5, True)],
        })
    for i in range(off_count):
        readings.append({
            "timestamp": _ts(i % 24, day=25 + (on_count + i) // 24),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, "Fan", 0, False)],
        })
    return readings


def test_build_activity_report_days_param_scales_on_hours():
    """50% uptime over 3 days → on_hours = 36.0 (not 12.0)."""
    # 3 days = 72 hours total; 50% uptime → 36 on-hours
    # Use equal on/off counts to achieve exactly 50%
    on_count = 12
    off_count = 12
    readings = _port_readings_for_days(on_count, off_count)
    result = build_activity_report(readings, days=3)
    assert len(result) == 1
    assert result[0].on_hours == pytest.approx(36.0)
    assert result[0].uptime_pct == 50.0


def test_build_activity_report_peak_hour_detected_correctly():
    """peak_hour_utc returns a naive UTC datetime for the slot with the most ON readings."""
    readings = []
    # Hour 14 has 3 on-readings; hour 10 and 18 each have 1
    for h in [10, 14, 14, 14, 18]:
        readings.append({
            "timestamp": _ts(h),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, "Fan", 5, True)],
        })
    result = build_activity_report(readings, days=1)
    assert len(result) == 1
    assert result[0].peak_hour_utc is not None
    assert result[0].peak_hour_utc.hour == 14
    assert result[0].peak_hour_utc.day == 25


def test_build_activity_report_peak_hour_multi_day_same_clock_different_days():
    """Slot with highest count wins even when same clock-hour spans multiple days."""
    # Apr 25 14:00 ×2, Apr 26 14:00 ×2, Apr 25 16:00 ×3
    # Old hour-only bucketing: hour 14 = 4 readings (wins incorrectly)
    # New slot bucketing: (Apr25,14)=2, (Apr26,14)=2, (Apr25,16)=3 → Apr 25 16:00 wins
    readings = []
    for _ in range(2):
        readings.append({
            "timestamp": _ts(14, day=25),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, "Fan", 5, True)],
        })
    for _ in range(2):
        readings.append({
            "timestamp": _ts(14, day=26),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, "Fan", 5, True)],
        })
    for _ in range(3):
        readings.append({
            "timestamp": _ts(16, day=25),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, "Fan", 5, True)],
        })
    result = build_activity_report(readings, days=2, port_loads={1: 5})
    assert len(result) == 1
    assert result[0].peak_hour_utc is not None
    assert result[0].peak_hour_utc.hour == 16
    assert result[0].peak_hour_utc.day == 25


def test_build_activity_report_peak_hour_utc_aware_input_stripped():
    """Timestamps with explicit +00:00 offset are stored as naive datetimes."""
    readings = [
        {
            "timestamp": "2024-04-25T14:00:00+00:00",
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, "Fan", 5, True)],
        }
    ]
    result = build_activity_report(readings, days=1, port_loads={1: 5})
    assert result[0].peak_hour_utc is not None
    assert result[0].peak_hour_utc.tzinfo is None
    assert result[0].peak_hour_utc.hour == 14


def test_build_activity_report_peak_hour_none_when_never_ran():
    """All-off readings → peak_hour_utc is None (not 0)."""
    readings = [
        {
            "timestamp": _ts(h),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, "Fan", 0, False)],
        }
        for h in range(10)
    ]
    result = build_activity_report(readings, days=1)
    assert len(result) == 1
    assert result[0].peak_hour_utc is None


def test_build_activity_report_on_off_hours_complement():
    """on_hours + off_hours == days * 24; on_hours magnitude is correct (70% of 72h)."""
    readings = _port_readings_for_days(on_count=7, off_count=3)
    days = 3
    result = build_activity_report(readings, days=days)
    assert len(result) == 1
    assert result[0].on_hours == pytest.approx(50.4)  # 7/10 * 24 * 3
    total = result[0].on_hours + result[0].off_hours
    assert total == pytest.approx(days * 24)


def test_build_activity_report_single_day_unchanged():
    """days=1 (default) matches the original per-day behavior (regression guard)."""
    # 100% uptime → on_hours should be 24.0 for a single day
    readings = [
        {
            "timestamp": _ts(h),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, "Fan", 5, True)],
        }
        for h in range(24)
    ]
    result = build_activity_report(readings, days=1)
    assert len(result) == 1
    assert result[0].on_hours == pytest.approx(24.0)
    assert result[0].off_hours == pytest.approx(0.0)
    assert result[0].uptime_pct == 100.0


# ---- Issue #86: ghost port filter tests ----

def test_build_activity_report_rule_a_excludes_ghost_constant() -> None:
    """Rule A: port with 0 transitions, 100% uptime, portsLoad=0 is excluded."""
    readings = [
        {
            "timestamp": _ts(h),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, "Port 1", 5, True)],
        }
        for h in range(24)
    ]
    result = build_activity_report(readings, days=1, port_loads={1: 0})
    assert len(result) == 0


@pytest.mark.parametrize(
    "on_pattern,port_loads,expected_excluded",
    [
        # All on: 0 transitions, 100% uptime, load=0 → excluded by Rule A
        ([True] * 24, {1: 0}, True),
        # One off at end: 1 transition, <100% uptime, load=0 → NOT excluded (has transition →
        # Rule A disabled; named "Exhaust Fan" → Rule B doesn't match; 23/24 h/day ≥ 1.0 → Rule C
        # doesn't fire either)
        ([True] * 23 + [False], {1: 0}, False),
        # All on, load > 0 → NOT excluded
        ([True] * 24, {1: 5}, False),
        # All on, port_loads=None → Rule A disabled → NOT excluded
        ([True] * 24, None, False),
        # All on, port_loads={} → normalized to None → NOT excluded
        ([True] * 24, {}, False),
    ],
)
def test_build_activity_report_rule_a_boundary(
    on_pattern: list[bool],
    port_loads: "dict[int, int] | None",
    expected_excluded: bool,
) -> None:
    """Rule A boundary: all four conditions must be met for exclusion.

    Uses port name 'Exhaust Fan' (not 'Port N') to isolate Rule A from Rule B/C.
    """
    readings = [
        {
            "timestamp": _ts(i % 24, day=25 + i // 24),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, "Exhaust Fan", 5 if on else 0, on)],
        }
        for i, on in enumerate(on_pattern)
    ]
    result = build_activity_report(readings, days=1, port_loads=port_loads)
    if expected_excluded:
        assert len(result) == 0
    else:
        assert len(result) == 1


def test_build_activity_report_rule_b_excludes_low_activity_auto_named() -> None:
    """Rule B: auto-named 'Port N' with < 1 hour/day average is excluded."""
    # 2 on readings out of 72 total, days=3: on_hours/days = (2/72*24*3)/3 = 0.67 < 1.0
    readings = (
        [
            {
                "timestamp": _ts(i % 24, day=25 + i // 24),
                "temperature_c": 24.0, "temperature_f": 75.2,
                "humidity": 60.0, "vpd": 1.24,
                "ports": [_port(1, "Port 1", 5, True)],
            }
            for i in range(2)
        ]
        + [
            {
                "timestamp": _ts(i % 24, day=25 + (i + 2) // 24),
                "temperature_c": 24.0, "temperature_f": 75.2,
                "humidity": 60.0, "vpd": 1.24,
                "ports": [_port(1, "Port 1", 0, False)],
            }
            for i in range(70)
        ]
    )
    result = build_activity_report(readings, days=3)
    assert len(result) == 0


def test_build_activity_report_rule_b_does_not_exclude_user_named() -> None:
    """Rule B must not exclude a user-named port (name != 'Port N' pattern)."""
    # Same low-activity scenario but with a custom name → should NOT be excluded
    readings = (
        [
            {
                "timestamp": _ts(i % 24, day=25 + i // 24),
                "temperature_c": 24.0, "temperature_f": 75.2,
                "humidity": 60.0, "vpd": 1.24,
                "ports": [_port(1, "Humidifier", 5, True)],
            }
            for i in range(2)
        ]
        + [
            {
                "timestamp": _ts(i % 24, day=25 + (i + 2) // 24),
                "temperature_c": 24.0, "temperature_f": 75.2,
                "humidity": 60.0, "vpd": 1.24,
                "ports": [_port(1, "Humidifier", 0, False)],
            }
            for i in range(70)
        ]
    )
    result = build_activity_report(readings, days=3)
    assert len(result) == 1
    assert result[0].name == "Humidifier"


def test_build_activity_report_empty_port_loads_normalized() -> None:
    """port_loads={} is normalized to None so Rule A is disabled (no false exclusions)."""
    readings = [
        {
            "timestamp": _ts(h),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, "Port 1", 5, True)],
        }
        for h in range(24)
    ]
    # port_loads={} means we don't have load data — Rule A must be disabled
    result = build_activity_report(readings, days=1, port_loads={})
    # Port 1 with 100% uptime and 0 transitions but port_loads={} → NOT excluded
    assert len(result) == 1


# ============ Rule C tests (#88) — named ports with zero load ============

def _named_port_readings(name: str, on_count: int, off_count: int, days: int = 3) -> list[dict]:
    """Generate readings for a named ghost port — speed=1 mirrors the toggle-nibble artifact."""
    readings = []
    for i in range(on_count):
        readings.append({
            "timestamp": _ts(i % 24, day=25 + i // 24),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, name, 1, True)],
        })
    for i in range(off_count):
        readings.append({
            "timestamp": _ts(i % 24, day=25 + (on_count + i) // 24),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, name, 0, False)],
        })
    return readings


def test_rule_c_excludes_named_ghost_live_example() -> None:
    """Ghost port (toggle-speed=1, portsLoad=0, 0.67 h/day) is excluded by Rule D.

    on_count=2 out of 72 readings → 0.67 h/day; speed=1 (toggle-nibble artifact) → Rule D fires.
    """
    readings = _named_port_readings("Humidifier", on_count=2, off_count=70, days=3)
    result = build_activity_report(readings, days=3, port_loads={1: 0})
    assert len(result) == 0, "ghost port with toggle-speed and zero load must be excluded"


def test_rule_c_excludes_named_ghost_very_low_runtime() -> None:
    """Ghost port (toggle-speed=1, portsLoad=0, 0.48 h/day) is excluded by Rule D."""
    readings = _named_port_readings("Humidifier", on_count=1, off_count=49, days=3)
    result = build_activity_report(readings, days=3, port_loads={1: 0})
    assert len(result) == 0


def test_rule_c_does_not_exclude_named_port_with_load() -> None:
    """Rule C must not fire when portsLoad > 0, even with sub-threshold runtime."""
    readings = _named_port_readings("Humidifier", on_count=2, off_count=70, days=3)
    result = build_activity_report(readings, days=3, port_loads={1: 5})
    assert len(result) == 1
    assert result[0].name == "Humidifier"


def test_rule_c_does_not_exclude_named_port_with_sufficient_runtime() -> None:
    """Rule C must not fire when on_hours/days >= 1.0, even with zero load."""
    # All-on, days=1: on_hours = 24 h, per day = 24 >> 1.0 threshold
    readings = [
        {
            "timestamp": _ts(h),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, "Humidifier", 5, True)],
        }
        for h in range(24)
    ]
    # Rule A would exclude (100% uptime, 0 transitions, load=0) → check Rule C independently
    # with transitions=1 to defeat Rule A
    readings_with_transition = readings + [{
        "timestamp": _ts(0, day=26),
        "temperature_c": 24.0, "temperature_f": 75.2,
        "humidity": 60.0, "vpd": 1.24,
        "ports": [_port(1, "Humidifier", 0, False)],
    }]
    result2 = build_activity_report(readings_with_transition, days=1, port_loads={1: 0})
    # on_hours = 24/25 * 24 * 1 = 23.04 h/day >> 1.0 → Rule C does not fire
    assert len(result2) == 1
    assert result2[0].name == "Humidifier"


def test_rule_c_does_not_fire_when_port_loads_is_none() -> None:
    """Rule C is disabled when port_loads is None (supplementary call failed)."""
    readings = _named_port_readings("Humidifier", on_count=2, off_count=70, days=3)
    result = build_activity_report(readings, days=3, port_loads=None)
    assert len(result) == 1
    assert result[0].name == "Humidifier"


def test_rule_c_does_not_exclude_named_port_at_days_1_borderline() -> None:
    """Regression guard: named port at 1.63 h/day for days=1 is kept (>= 1.0 threshold).

    Accepted gap: 1.63 h/day is above the 1.0 h/day threshold so Rule C does not fire.
    Growers with very-low-duty devices should use get_port_status to confirm device state.
    """
    # on_hours = 1.63, days = 1: per day = 1.63 ≥ 1.0 → NOT excluded
    # Simulate: total=24, on_count such that on_hours ≈ 1.63
    # on_hours = on_count/24 * 24 * 1 = on_count → use on_count=2 → 2.0 h/day ≥ 1.0
    readings = [
        {
            "timestamp": _ts(h),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, "Humidifier", 5 if h < 2 else 0, h < 2)],
        }
        for h in range(24)
    ]
    result = build_activity_report(readings, days=1, port_loads={1: 0})
    # on_hours = 2.0; transitions = 1 (defeats Rule A); 2.0/1 = 2.0 ≥ 1.0 → kept
    assert len(result) == 1
    assert result[0].name == "Humidifier"


# ============ Rule B enhancement tests (#89) — portsLoad guard ============

def test_rule_b_enhanced_excludes_autonamed_port_with_zero_load_at_days_1() -> None:
    """Rule B enhanced: auto-named port with portsLoad=0 is excluded even if on_hours/days ≥ 1.0."""
    # on_count=2, days=1: on_hours = 2.0 h/day ≥ 1.0 → OLD Rule B would NOT exclude
    # NEW Rule B: portsLoad=0 → excluded regardless
    readings = [
        {
            "timestamp": _ts(h),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, "Port 7", 5 if h < 2 else 0, h < 2)],
        }
        for h in range(24)
    ]
    result = build_activity_report(readings, days=1, port_loads={1: 0})
    assert len(result) == 0, "Enhanced Rule B must exclude auto-named port with zero load"


def test_rule_b_enhanced_does_not_exclude_autonamed_port_with_load() -> None:
    """Rule B enhanced: auto-named port with portsLoad > 0 and on_hours/days ≥ 1.0 is kept."""
    readings = [
        {
            "timestamp": _ts(h),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, "Port 7", 5 if h < 2 else 0, h < 2)],
        }
        for h in range(24)
    ]
    result = build_activity_report(readings, days=1, port_loads={1: 5})
    assert len(result) == 1
    assert result[0].name == "Port 7"


def test_rule_b_enhanced_does_not_fire_when_port_loads_none() -> None:
    """Rule B portsLoad guard is disabled when port_loads is None."""
    # on_count=2, days=1 → on_hours=2.0 ≥ 1.0; port_loads=None → portsLoad guard off → kept
    readings = [
        {
            "timestamp": _ts(h),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, "Port 7", 5 if h < 2 else 0, h < 2)],
        }
        for h in range(24)
    ]
    result = build_activity_report(readings, days=1, port_loads=None)
    assert len(result) == 1
    assert result[0].name == "Port 7"


# ============ Interaction tests ============

def test_rule_c_fires_for_named_port_not_caught_by_rule_b() -> None:
    """Rule B only fires on auto-named 'Port N'; named ghost ports are caught by Rule D."""
    # speed=1 (toggle-nibble) + portsLoad=0 → Rule D fires even though name isn't "Port N"
    readings = _named_port_readings("Humidifier", on_count=1, off_count=49, days=3)
    result = build_activity_report(readings, days=3, port_loads={1: 0})
    assert len(result) == 0  # Rule D excluded it


def test_rule_a_b_c_d_e_together_multi_port_scenario() -> None:
    """Multi-port: each rule catches a distinct port; only 'Exhaust Fan' survives.

    Uses 48 readings (days=2) so that h==0 in 24-reading blocks gives 2 on-readings
    out of 48 total → on_hours = 2/48 * 24 * 2 = 2.0 h / 2 days = 1.0 h/day.
    That is NOT below threshold. Use on_count=1 out of 48 → 0.5 h/day < 1.0 ✓.
    """
    # Rule A candidate: Port 2 — 100% uptime, 0 transitions, load=0
    # Rule B candidate: Port 3 — auto-named "Port 3", < 1 h/day (low on-time guard)
    # Rule C candidate: Port 4 — named "Misting Pump", zero load, < 1 h/day
    # Rule D candidate: Port 4 "Misting Pump" catches speed=1 case; see below
    # Rule E candidate: Port 5 — named "Carbon", speed=5 stale config,
    #   transitions>0, load=0, sub-threshold
    # Survivor: Port 1 — named "Exhaust Fan", load=5, meaningful uptime
    days = 2
    total_readings = 48
    readings = []
    for i in range(total_readings):
        h = i % 24
        # Port 5 "Carbon": 1 on reading, then off — speed=5 stale config, transitions=1
        carbon_on = i == 0  # only first reading on
        carbon_speed = 5 if carbon_on else 0
        readings.append({
            "timestamp": _ts(h, day=25 + i // 24),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [
                _port(1, "Exhaust Fan", 5, True),        # survivor: named, load>0, good uptime
                _port(2, "Port 2", 5, True),                   # Rule A: 100% uptime, load=0
                _port(3, "Port 3", 5 if i == 0 else 0, i == 0),  # Rule B: 1/48*24*2/2=0.5 h/day
                _port(4, "Misting Pump", 1 if i == 0 else 0, i == 0),  # Rule D: speed=1, load=0
                _port(5, "Carbon", carbon_speed, carbon_on),   # Rule E: speed=5, load=0
            ],
        })
    port_loads = {1: 5, 2: 0, 3: 0, 4: 0, 5: 0}
    result = build_activity_report(readings, days=days, port_loads=port_loads)
    assert len(result) == 1
    assert result[0].name == "Exhaust Fan"


# ============ Parametrized threshold boundary ============

@pytest.mark.parametrize("on_count,total,days,port_load,speed,expected_count,label", [
    # speed=1 → toggle-nibble artifact (ghost/toggle device) → Rule D fires when portsLoad=0
    # speed=5 → real fan → Rule D skips; Rule C requires transitions==0 to fire
    (0,  24, 1, 0, 1, 0, "zero runtime → excluded"),              # Rule C fires (transitions=0)
    (1,  48, 1, 0, 1, 0, "toggle 0.5 h/day, no load → excluded"), # Rule D fires (speed=1, load=0)
    (1,  30, 1, 0, 1, 0, "toggle 0.8 h/day, no load → excluded"), # Rule D fires (speed=1, load=0)
    (1,  24, 1, 0, 5, 1, "real fan 1.0 h/day → kept (at Rule E boundary)"),  # strict < not <=
    (2,  24, 1, 0, 5, 1, "real fan 2.0 h/day, no load → kept"),   # speed=5 → Rule D skips
    (1,  48, 1, 5, 5, 1, "0.5 h/day but has load → kept"),        # load>0 → all rules skip
    # Rule E: named port, speed=5 stale config, transitions=1, load=0, sub-threshold runtime
    # on_count=1 out of 48, days=3 → on_hours = 1/48*24*3 = 1.5h; 1.5/3 = 0.5 h/day < 1.0
    # transitions>0 (1 on→off = 1 transition); speed=5 → Rule D skips; Rule E fires
    (1,  48, 3, 0, 5, 0, "stale speed=5, load=0, 0.5 h/day → excluded by Rule E"),
])
def test_rule_c_threshold_boundary_named_port(
    on_count: int, total: int, days: int, port_load: int, speed: int,
    expected_count: int, label: str
) -> None:
    """Ghost port filter boundary: excluded by Rule D (toggle-speed) or Rule C (zero-runtime)."""
    assert _GHOST_LOAD_ZERO_THRESHOLD == 1.0  # guard: test is calibrated to this value

    off_count = total - on_count
    readings = []
    for i in range(on_count):
        readings.append({
            "timestamp": _ts(i % 24, day=25 + i // 24),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, "Humidifier", speed, True)],
        })
    for i in range(off_count):
        readings.append({
            "timestamp": _ts(i % 24, day=25 + (on_count + i) // 24),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, "Humidifier", 0, False)],
        })

    result = build_activity_report(readings, days=days, port_loads={1: port_load})
    on_hours_per_day = (on_count / total * 24 * days) / days
    assert len(result) == expected_count, (
        f"{label}: on_hours_per_day={on_hours_per_day:.2f}, speed={speed}, port_load={port_load}: "
        f"expected {expected_count} port(s), got {len(result)}"
    )


# ============ calculate_health_score — actual reading fields (#56) ============


def test_calculate_health_score_exposes_actual_readings():
    reading = _reading(temp_c=22.0, humidity=55.0, vpd=1.1)
    result = calculate_health_score(reading, "veg")
    assert result.temperature_c == pytest.approx(22.0)
    assert result.temperature_f == pytest.approx(reading["temperature_f"])
    assert result.humidity_pct == pytest.approx(55.0)
    assert result.vpd_kpa == pytest.approx(1.1)


def test_calculate_health_score_missing_keys_default_to_zero():
    result = calculate_health_score({}, "veg")
    assert result.temperature_c == 0.0
    assert result.temperature_f == 0.0
    assert result.humidity_pct == 0.0
    assert result.vpd_kpa == 0.0


# ============ data_quality — toggle-device history artifact detection (#85) ============


def _toggle_readings(
    name: str = "Heater",
    port_num: int = 2,
    speed: int = 1,
    on: bool = True,
    count: int = 72,
) -> list[dict]:
    return [
        {
            "timestamp": _ts(i % 24, day=25 + i // 24),
            "temperature_c": 22.0, "temperature_f": 71.6,
            "humidity": 55.0, "vpd": 1.2,
            "ports": [_port(port_num, name, speed, on)],
        }
        for i in range(count)
    ]


def test_data_quality_api_constant_speed_toggle_hardware():
    """Heater with loadType=4, transitions=0, uptime=100%, all speed=1 → data_quality set."""
    readings = _toggle_readings(speed=1, on=True)
    result = build_activity_report(
        readings, days=3, port_loads={2: 5}, port_load_types={2: 4}
    )
    assert len(result) == 1
    assert result[0].data_quality == "api_constant_speed"


def test_data_quality_api_constant_speed_loadtype_128():
    """loadType=128 (toggle hardware variant) also triggers data_quality flag."""
    readings = _toggle_readings(speed=1, on=True)
    result = build_activity_report(
        readings, days=3, port_loads={2: 5}, port_load_types={2: 128}
    )
    assert len(result) == 1
    assert result[0].data_quality == "api_constant_speed"


def test_data_quality_none_with_transitions():
    """Port with transitions > 0 does not get flagged, even with toggle loadType."""
    readings = []
    speeds = [1, 1, 0, 1, 1]
    ons = [s > 0 for s in speeds]
    for i, (spd, on) in enumerate(zip(speeds, ons)):
        readings.append({
            "timestamp": _ts(i),
            "temperature_c": 22.0, "temperature_f": 71.6,
            "humidity": 55.0, "vpd": 1.2,
            "ports": [_port(2, "Heater", spd, on)],
        })
    result = build_activity_report(
        readings, days=1, port_loads={2: 5}, port_load_types={2: 4}
    )
    assert len(result) == 1
    assert result[0].data_quality is None


@pytest.mark.parametrize("description,speed,on,load_type,expected_quality", [
    ("mixed speeds → not flagged", 5, True, 4, None),         # speed != 1 → not all-one
    ("always off → not flagged", 0, False, 4, None),          # no running_speeds → bool([])=False
    ("on=True speed=0 → not flagged", 0, True, 4, None),      # speed=0 excluded from running_speeds
    ("no loadType info → not flagged", 1, True, None, None),  # port_load_types=None
    ("loadType=0 → not flagged", 1, True, 0, None),           # loadType not in {4, 128}
])
def test_data_quality_none_various_scenarios(
    description: str,
    speed: int,
    on: bool,
    load_type: int | None,
    expected_quality: str | None,
) -> None:
    """data_quality is None unless all three conditions hold for confirmed toggle hardware."""
    readings = _toggle_readings(speed=speed, on=on)
    port_load_types = {2: load_type} if load_type is not None else None
    result = build_activity_report(
        readings, days=3, port_loads={2: 5}, port_load_types=port_load_types
    )
    # Always-off ports may be filtered or show uptime=0; always-on at speed=0 is unusual
    if result:
        assert result[0].data_quality == expected_quality, description


def test_data_quality_not_100pct_uptime():
    """Port with some off-slots is not flagged even with toggle loadType."""
    readings = _toggle_readings(speed=1, on=True, count=36)
    readings += _toggle_readings(speed=0, on=False, count=36)
    result = build_activity_report(
        readings, days=3, port_loads={2: 5}, port_load_types={2: 4}
    )
    assert len(result) == 1
    assert result[0].data_quality is None
    assert result[0].uptime_pct == pytest.approx(50.0)


def test_rule_d_exempts_toggle_hardware_currently_off():
    """Toggle hardware (loadType=4) with portsLoad=0 and the 100%-uptime artifact survives via
    the data_quality early-exit and appears with the api_constant_speed caveat."""
    readings = _toggle_readings(speed=1, on=True)
    result = build_activity_report(
        readings, days=3, port_loads={2: 0}, port_load_types={2: 4}
    )
    assert len(result) == 1, "Toggle hardware must survive even when currently off"
    assert result[0].data_quality == "api_constant_speed"


def test_rule_d_keeps_toggle_hardware_with_genuine_transitions():
    """Confirmed toggle hardware (loadType=4) with transitions > 0 is kept by Rule D, not dropped.

    Rule D is exempt for confirmed toggle hardware that ran — the grower should see the data.
    The data_quality early-exit handles the 100%-uptime constant-speed artifact; any toggle
    port with transitions > 0 (an actual on→off cycle) is a genuine runner.
    Gate 5 (PR #114, Issue #101): Heater ran 2+ days then turned off; Rule D was incorrectly
    dropping it because avg_speed=1.0 (expected for toggle hardware) and portsLoad=0 (now off).
    """
    # 4 on readings then 68 off — transitions=1, uptime<100%, avg_speed=1.0
    readings = _toggle_readings(speed=1, on=True, count=4) + _toggle_readings(
        speed=0, on=False, count=68
    )
    result = build_activity_report(
        readings, days=3, port_loads={2: 0}, port_load_types={2: 4}
    )
    assert len(result) == 1, "Confirmed toggle hardware with transitions>0 must survive Rule D"
    assert result[0].data_quality is None  # ran briefly but not the constant-speed artifact


def test_rule_d_keeps_genuine_toggle_runner_currently_off():
    """Toggle hardware (loadType=4) that ran significantly and is now off is kept.

    Regression for Gate 5 T4 (PR #114 / Issue #101): Heater ran ~2.5 days in a 7-day
    window (transitions=1, on_hours≈60h, avg_speed=1.0), then portsLoad dropped to 0.
    Rule D must not silently discard it — the grower needs to see the runtime data.
    Simulated with 60 on + 108 off out of 168 total readings over 7 days.
    """
    readings = _toggle_readings(speed=1, on=True, count=60) + _toggle_readings(
        speed=0, on=False, count=108
    )
    result = build_activity_report(
        readings, days=7, port_loads={2: 0}, port_load_types={2: 4}
    )
    assert len(result) == 1, "Toggle hardware with significant runtime must survive Rule D"
    assert result[0].name == "Heater"
    assert result[0].transitions == 1
    assert result[0].data_quality is None


def test_rule_d_still_filters_non_toggle_ghost_ports():
    """Non-toggle ghost port (loadType=0, portsLoad=0, avg_speed<=1) is filtered by Rule D."""
    readings = _toggle_readings(name="Port 3", port_num=3, speed=1, on=True)
    result = build_activity_report(
        readings, days=3, port_loads={3: 0}, port_load_types={3: 0}
    )
    assert len(result) == 0, "Non-toggle ghost port must still be filtered by Rule D"


def test_data_quality_none_when_port_load_types_not_provided():
    """Without port_load_types, no port is flagged (safe default)."""
    readings = _toggle_readings(speed=1, on=True)
    result = build_activity_report(
        readings, days=3, port_loads={2: 5}, port_load_types=None
    )
    assert len(result) == 1
    assert result[0].data_quality is None


# ============ Rule E tests (#101) — stale configured speed phantom ============

def _filter_port_readings(
    port_num: int,
    name: str,
    speed: int,
    on_count: int,
    total: int,
    days: int,
) -> list[dict]:
    """Build readings for a named port: on_count on-readings (speed) then off."""
    off_count = total - on_count
    readings = []
    for i in range(on_count):
        readings.append({
            "timestamp": _ts(i % 24, day=25 + i // 24),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(port_num, name, speed, True)],
        })
    for i in range(off_count):
        readings.append({
            "timestamp": _ts(i % 24, day=25 + (on_count + i) // 24),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(port_num, name, 0, False)],
        })
    return readings


def test_rule_e_filters_stale_config_speed_phantom() -> None:
    """Exact Issue #101 reproduction: Filter (Port 4), speed=5, load=0, sub-threshold.

    4 on-readings out of 288 total over 3 days:
      on_hours = 4/288 * 24 * 3 = 1.0 h; 1.0/3 = 0.333 h/day < 1.0 threshold.
    avg_speed = 5.0 > 1.0 → Rule D skips. transitions=2 > 0 → Rule C skips.
    port_loads={4: 0} → Rule E fires.
    """
    readings = _filter_port_readings(4, "Filter", speed=5, on_count=4, total=288, days=3)
    result = build_activity_report(readings, days=3, port_loads={4: 0})
    assert len(result) == 0, "Stale-speed phantom port must be filtered by Rule E"


def test_rule_e_does_not_filter_above_threshold() -> None:
    """Port with same stale speed=5 and zero load is kept when runtime exceeds 1 h/day.

    216 on-readings out of 288 total over 3 days:
      on_hours = 216/288 * 24 * 3 = 54.0 h; 54.0/3 = 18.0 h/day >> 1.0 threshold.
    Rule E sub-threshold guard does not fire → port is kept.
    """
    readings = _filter_port_readings(4, "Filter", speed=5, on_count=216, total=288, days=3)
    result = build_activity_report(readings, days=3, port_loads={4: 0})
    assert len(result) == 1, "Port with sufficient runtime must not be filtered by Rule E"
    assert result[0].name == "Filter"


def test_rule_e_does_not_filter_port_with_load() -> None:
    """Rule E must not fire when portsLoad > 0, even with sub-threshold runtime and speed=5."""
    readings = _filter_port_readings(4, "Filter", speed=5, on_count=4, total=288, days=3)
    result = build_activity_report(readings, days=3, port_loads={4: 10})
    assert len(result) == 1, "Port with non-zero load must not be filtered by Rule E"
    assert result[0].name == "Filter"


def test_rule_e_does_not_fire_when_port_loads_none() -> None:
    """Rule E is disabled when port_loads is None (supplementary call failed)."""
    readings = _filter_port_readings(4, "Filter", speed=5, on_count=4, total=288, days=3)
    result = build_activity_report(readings, days=3, port_loads=None)
    assert len(result) == 1, "Rule E must be disabled when port_loads is None"
    assert result[0].name == "Filter"


def test_rule_d_handles_toggle_speed_before_rule_e_is_reached() -> None:
    """Rule D regression guard: avg_speed=1.0 with load=0 is caught by Rule D, not Rule E.

    Port "Misting Pump" with avg_speed=1.0 (toggle-nibble artifact), sub-threshold runtime,
    and port_loads=0 must be excluded by Rule D. Rule E's avg_speed>1.0 condition is false,
    so Rule E never evaluates this port. This test documents that avg_speed<=1.0 is owned
    by Rule D.
    """
    readings = _filter_port_readings(4, "Misting Pump", speed=1, on_count=4, total=288, days=3)
    result = build_activity_report(readings, days=3, port_loads={4: 0})
    assert len(result) == 0, "avg_speed=1.0 port with zero load must be filtered by Rule D"


# ============ Issue #120: devType=18 zero-load quirk ============


def test_zero_load_dev_types_membership() -> None:
    """_ZERO_LOAD_DEV_TYPES must contain exactly {18, 20, 22} — regression guard.

    20 was added on live evidence: an 89 AI+ reports portsLoad=None on all 8
    ports, including a light and an exhaust fan that were both running at the
    time. Treating that absence as "no current draw" made Rule D drop a
    humidifier that had cycled 74 times for 7.3h over two days.
    """
    assert _ZERO_LOAD_DEV_TYPES == frozenset({18, 20, 22})


@pytest.mark.parametrize("dev_type,expected_kept", [
    (18, True),   # devType=18: Rule E disabled, port kept
    (11, False),  # devType=11: Rule E fires, port filtered
    (None, False),  # dev_type=None (default): Rule E fires, port filtered
])
def test_dev_type_18_bypasses_rule_e_vs_non_18(dev_type, expected_kept: bool) -> None:
    """devType=18 bypasses Rule E; devType=11 and None apply Rule E normally."""
    # 4 on-readings out of 288 total over 3 days → 0.333 h/day < 1.0 threshold
    # avg_speed=5, transitions>0 → Rule D skips; Rule E fires when load check is active
    readings = _filter_port_readings(4, "Left Fan", speed=5, on_count=4, total=288, days=3)
    result = build_activity_report(
        readings, days=3, port_loads={4: 0}, dev_type=dev_type
    )
    assert (len(result) == 1) == expected_kept, (
        f"dev_type={dev_type}: expected {'kept' if expected_kept else 'filtered'}, "
        f"got {len(result)} port(s)"
    )
    if expected_kept:
        assert result[0].data_quality == "no_load_signal"


def test_dev_type_18_rule_b_runtime_threshold_still_fires() -> None:
    """devType=18: auto-named 'Port N' with < 1h/day is still filtered by Rule B."""
    # 1 on-reading / 48 total over 2 days → on_hours=1.0h total; 1.0/2 = 0.5 h/day < threshold
    readings = _filter_port_readings(3, "Port 3", speed=5, on_count=1, total=48, days=2)
    result = build_activity_report(readings, days=2, port_loads={3: 0}, dev_type=18)
    assert len(result) == 0, "Auto-named 'Port N' with sub-threshold runtime must still be filtered"


def test_dev_type_18_api_constant_speed_takes_priority() -> None:
    """devType=18 toggle hardware gets api_constant_speed, not no_load_signal."""
    readings = [
        {
            "timestamp": _ts(i % 24, day=25 + i // 24),
            "temperature_c": 22.0, "temperature_f": 71.6,
            "humidity": 55.0, "vpd": 1.2,
            "ports": [_port(2, "Heater", 1, True)],
        }
        for i in range(24)
    ]
    result = build_activity_report(
        readings, days=1,
        port_loads={2: 0},
        port_load_types={2: 4},  # toggle hardware
        dev_type=18,
    )
    assert len(result) == 1
    assert result[0].data_quality == "api_constant_speed"


def test_dev_type_18_no_load_signal_on_surviving_non_toggle_ports() -> None:
    """devType=18: non-toggle ports get no_load_signal; toggle gets api_constant_speed."""
    base_on = [
        {
            "timestamp": _ts(i % 24, day=25 + i // 24),
            "temperature_c": 22.0, "temperature_f": 71.6,
            "humidity": 55.0, "vpd": 1.2,
            "ports": [
                _port(1, "Exhaust Fan", 5, True),   # named, high uptime → kept
                _port(2, "Heater", 1, True),          # toggle hardware → api_constant_speed
            ],
        }
        for i in range(24)
    ]
    result = build_activity_report(
        base_on, days=1,
        port_loads={1: 0, 2: 0},
        port_load_types={1: 0, 2: 4},
        dev_type=18,
    )
    by_port = {r.port: r for r in result}
    assert by_port[1].data_quality == "no_load_signal"
    assert by_port[2].data_quality == "api_constant_speed"


def test_dev_type_18_toggle_pattern_no_loadtype_gets_api_constant_speed() -> None:
    """devType=18: toggle artifact detected by pattern when loadType is not 4/128.

    On devType=18 devices the API does not return loadType 4/128 for toggle ports,
    so is_toggle_hardware is False. The pattern (transitions=0, uptime=100%, speed=1)
    must be sufficient to flag api_constant_speed (Issue #126).
    """
    readings = [
        {
            "timestamp": _ts(i, day=25),
            "temperature_c": 22.0, "temperature_f": 71.6,
            "humidity": 55.0, "vpd": 1.2,
            "ports": [_port(2, "Heater", 1, True)],
        }
        for i in range(24)
    ]
    result = build_activity_report(
        readings, days=1,
        port_loads={2: 0},
        port_load_types={2: 0},  # loadType=0, NOT toggle hardware per loadType
        dev_type=18,
    )
    assert len(result) == 1
    assert result[0].data_quality == "api_constant_speed", (
        "devType=18 toggle pattern must yield api_constant_speed without loadType 4/128"
    )


def test_dev_type_18_rule_c_named_port_zero_transitions_kept() -> None:
    """devType=18: named port with transitions=0 and sub-threshold runtime is kept.

    Rule C fires when port_loads is provided and portsLoad==0 (transitions==0,
    on_hours/days < 1.0). For devType=18, port_loads is forced to None, so
    Rule C's port_loads guard fails and the port is kept with no_load_signal.
    """
    # All off = transitions=0, on_hours=0; on a non-18 device Rule C would filter this
    readings = [
        {
            "timestamp": _ts(i, day=25),
            "temperature_c": 22.0, "temperature_f": 71.6,
            "humidity": 55.0, "vpd": 1.2,
            "ports": [_port(1, "Misting Pump", 0, False)],
        }
        for i in range(24)
    ]
    result = build_activity_report(readings, days=1, port_loads={1: 0}, dev_type=18)
    # Port has zero uptime — it's all-off, so on_hours=0 and avg_speed=0
    # Rule C would filter it on non-18 (transitions=0, load=0, sub-threshold)
    # On devType=18, port_loads=None so Rule C doesn't fire
    assert len(result) == 1
    assert result[0].data_quality == "no_load_signal"


# ============ Issues #117 / #128: devType=22 zero-load quirk ============


@pytest.mark.parametrize("dev_type,expected_kept", [
    (22, True),   # devType=22: load-based rules disabled, port kept
    (11, False),  # devType=11: Rule D fires (avg_speed=1.0, load=0), port filtered
    (None, False),  # dev_type=None (default): Rule D fires, port filtered
])
def test_dev_type_22_bypasses_rules_vs_non_22(dev_type: int | None, expected_kept: bool) -> None:
    """devType=22 bypasses Rule D; devType=11 and None apply Rule D normally.

    port_load_types={N: 0} ensures is_toggle_hardware is False (0 not in _TOGGLE_LOAD_TYPES).
    Readings use 50% uptime (on_count=12/24) so the toggle pattern (100% uptime + speed=1) is
    NOT triggered — data_quality for devType=22 is no_load_signal, not api_constant_speed.
    Rule D fires on non-22 devices: avg_speed=1.0, load=0, not is_toggle → port filtered.
    """
    # 12 on-readings at speed=1 then 12 off → transitions=1, uptime=50%, avg_speed=1.0
    readings = _filter_port_readings(3, "R1 Clone Lights", speed=1, on_count=12, total=24, days=1)
    result = build_activity_report(
        readings, days=1, port_loads={3: 0}, port_load_types={3: 0}, dev_type=dev_type
    )
    assert (len(result) == 1) == expected_kept, (
        f"dev_type={dev_type}: expected {'kept' if expected_kept else 'filtered'}, "
        f"got {len(result)} port(s)"
    )
    if expected_kept:
        assert result[0].data_quality == "no_load_signal"


def test_dev_type_22_no_load_signal_on_cycling_ports() -> None:
    """devType=22: port with genuine (sustained) transitions gets no_load_signal caveat."""
    readings = []
    for i in range(24):
        # Sustained pairs: on for 2 readings, off for 2 readings — no single-reading nibbles.
        on = (i // 2) % 2 == 0
        readings.append({
            "timestamp": _ts(i, day=25),
            "temperature_c": 22.0, "temperature_f": 71.6,
            "humidity": 55.0, "vpd": 1.2,
            "ports": [_port(2, "R1 Clone Lights", 1 if on else 0, on)],
        })
    result = build_activity_report(
        readings, days=1, port_loads={2: 0}, port_load_types={2: 129}, dev_type=22
    )
    assert len(result) == 1
    assert result[0].data_quality == "no_load_signal"
    assert result[0].transitions > 0


def test_dev_type_22_api_constant_speed_on_toggle_pattern() -> None:
    """devType=22: toggle pattern detected via dev_type membership, not loadType.

    loadType=129 is NOT in _TOGGLE_LOAD_TYPES={4, 128}, so is_toggle_hardware is False.
    The api_constant_speed caveat fires via: is_toggle_pattern AND dev_type in _ZERO_LOAD_DEV_TYPES.
    This exercises the live Q0KT4 scenario where loadType=129 (heat pad/lights) is non-standard.
    """
    readings = [
        {
            "timestamp": _ts(i, day=25),
            "temperature_c": 22.0, "temperature_f": 71.6,
            "humidity": 55.0, "vpd": 1.2,
            "ports": [_port(1, "R1 Clone Heat Pad", 1, True)],
        }
        for i in range(24)
    ]
    result = build_activity_report(
        readings, days=1, port_loads={1: 0}, port_load_types={1: 129}, dev_type=22
    )
    assert len(result) == 1
    assert result[0].data_quality == "api_constant_speed", (
        "devType=22 toggle pattern must yield api_constant_speed via pattern alone "
        "(loadType=129 is non-standard; dev_type membership is the trigger)"
    )


def test_dev_type_22_rule_b_runtime_threshold_still_fires() -> None:
    """devType=22: auto-named 'Port N' with < 1h/day is still filtered by Rule B."""
    readings = _filter_port_readings(4, "Port 4", speed=1, on_count=1, total=48, days=2)
    result = build_activity_report(readings, days=2, port_loads={4: 0}, dev_type=22)
    assert len(result) == 0, "Auto-named 'Port N' with sub-threshold runtime must still be filtered"


def test_dev_type_22_user_named_all_off_port_gets_no_load_signal() -> None:
    """devType=22: user-named all-off port (0% uptime) is kept with no_load_signal.

    Rule B does not apply (not auto-named). Rule C, D, E require port_loads is not None
    (port_loads is forced to None for devType=22). Port is kept.
    """
    readings = [
        {
            "timestamp": _ts(i, day=25),
            "temperature_c": 22.0, "temperature_f": 71.6,
            "humidity": 55.0, "vpd": 1.2,
            "ports": [_port(4, "R1 Outlet 4", 0, False)],
        }
        for i in range(24)
    ]
    result = build_activity_report(readings, days=1, port_loads={4: 0}, dev_type=22)
    assert len(result) == 1
    assert result[0].data_quality == "no_load_signal"
    assert result[0].uptime_pct == 0.0


def test_dev_type_22_auto_named_all_off_filtered_by_rule_b() -> None:
    """devType=22: auto-named 'Port N' at 0% uptime is filtered by Rule B runtime arm.

    Rule B's runtime arm (on_hours/days < 1.0) fires independently of port_loads.
    Port is excluded even though load-based rules are disabled for devType=22.
    """
    readings = [
        {
            "timestamp": _ts(i, day=25),
            "temperature_c": 22.0, "temperature_f": 71.6,
            "humidity": 55.0, "vpd": 1.2,
            "ports": [_port(4, "Port 4", 0, False)],
        }
        for i in range(24)
    ]
    result = build_activity_report(readings, days=1, port_loads={4: 0}, dev_type=22)
    assert len(result) == 0, (
        "Auto-named 'Port 4' at 0% uptime must be filtered by Rule B runtime arm"
    )


def test_dev_type_22_rule_b_load_guard_disabled_sufficient_runtime() -> None:
    """devType=22: auto-named 'Port N' with >= 1h/day is kept even with load=0.

    Rule B's portsLoad guard requires port_loads is not None, which is False for devType=22.
    On devType=11, the same port is filtered by Rule B's load guard.
    """
    # 2 on-readings / 24 total → on_hours=2.0 ≥ 1.0 threshold
    readings = _filter_port_readings(3, "Port 3", speed=5, on_count=2, total=24, days=1)

    result_22 = build_activity_report(readings, days=1, port_loads={3: 0}, dev_type=22)
    assert len(result_22) == 1, (
        "devType=22: Rule B load guard disabled; port with sufficient runtime kept"
    )
    assert result_22[0].data_quality == "no_load_signal"

    result_11 = build_activity_report(readings, days=1, port_loads={3: 0}, dev_type=11)
    assert len(result_11) == 0, "devType=11: Rule B load guard fires; port with load=0 filtered"


# ============ _count_debounced_transitions ============

def test_count_debounced_transitions_empty():
    assert _count_debounced_transitions([]) == 0


def test_count_debounced_transitions_no_change():
    assert _count_debounced_transitions([True, True, True]) == 0


def test_count_debounced_transitions_single_blip_not_counted():
    # F, T (1 reading), F — the T is a single-reading blip; should not count.
    assert _count_debounced_transitions([False, True, False, False]) == 0


def test_count_debounced_transitions_sustained_changes():
    # F,F → T,T → F,F → T,T → F,F: 4 transitions all sustained.
    flags = [False, False, True, True, False, False, True, True, False, False]
    assert _count_debounced_transitions(flags) == 4


def test_count_debounced_transitions_boundary_nibble():
    # F,T (single),F,T,T,T,F,F — first T is a nibble (1 reading), second T run is sustained.
    # Transitions accepted: F→T (3-reading run), T→F (2-reading run) = 2.
    flags = [False, True, False, True, True, True, False, False]
    assert _count_debounced_transitions(flags) == 2


# ============ weighted-median peak_hour_utc ============

def test_build_activity_report_peak_hour_weighted_median_skewed():
    """Weighted median stays on the central mass when a single slot has a nibble spike.

    Scenario: 3 readings at hour 2 (API boundary nibble), 1 reading each at hours 10–14.
    max() picks hour 2 (highest count from nibble); weighted median picks hour 12 (centre).
    """
    readings = []
    # hour 2 has 3 readings (nibble artifact); hours 10-14 each have 1 reading
    for h in [2, 2, 2, 10, 11, 12, 13, 14]:
        readings.append({
            "timestamp": _ts(h),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, "Fan", 5, True)],
        })
    result = build_activity_report(readings, port_loads={1: 5})
    assert result[0].peak_hour_utc is not None
    # max() would return hour 2 (count=3); weighted median returns hour 11 (index 4 of 8)
    assert result[0].peak_hour_utc.hour == 11


def test_build_activity_report_peak_hour_uniform():
    """Uniform schedule: every hour appears once → median returns the middle slot."""
    readings = [
        {
            "timestamp": _ts(h),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [_port(1, "Fan", 5, True)],
        }
        for h in range(9, 18)  # hours 9–17 inclusive, 9 slots
    ]
    result = build_activity_report(readings, port_loads={1: 5})
    assert result[0].peak_hour_utc is not None
    assert result[0].peak_hour_utc.hour == 13  # middle of 9 slots = index 4 → hour 13


# ============ Rule F — phantom clone detection ============

def _rule_f_reading(hour: int, ports: list[dict]) -> dict:
    return {
        "timestamp": _ts(hour),
        "temperature_c": 24.0, "temperature_f": 75.2,
        "humidity": 60.0, "vpd": 1.24,
        "ports": ports,
    }


def test_rule_f_excludes_identical_low_activity_custom_ports():
    """Ports with identical uptime/transitions/peak and low activity are phantom clones."""
    # 50 readings so on_hours = 1/50*24 = 0.48 h < 1.0 threshold.
    # Port 2 'Heater' and Port 3 'Humidifer' identical: speed=1 on at reading 10, otherwise off.
    # Port 4 'Filter' has different profile (higher activity) → stays.
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    base = _dt(2024, 4, 25, 0, 0, 0)
    readings = [
        {
            "timestamp": (base + _td(hours=i)).isoformat() + "Z",
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [
                _port(2, "Heater", 1 if i == 10 else 0, i == 10),
                _port(3, "Humidifer", 1 if i == 10 else 0, i == 10),
                _port(4, "Filter", 5, True),
            ],
        }
        for i in range(50)
    ]
    result = build_activity_report(
        readings, days=1,
        port_loads={2: 1, 3: 1, 4: 5},
        port_load_types={2: 4, 3: 4, 4: 1},
    )
    port_names = [r.name for r in result]
    assert "Filter" in port_names
    assert "Heater" not in port_names
    assert "Humidifer" not in port_names


def test_rule_f_does_not_exclude_high_activity_custom_ports():
    """Even if two ports share the same pattern, high activity (≥ threshold) prevents exclusion."""
    # Both ports run 12h/day — well above the 1h/day ghost threshold.
    readings = [
        _rule_f_reading(h, [
            _port(2, "Heater", 5, h < 12),
            _port(3, "Humidifer", 5, h < 12),
        ])
        for h in range(24)
    ]
    result = build_activity_report(
        readings, days=1,
        port_loads={2: 5, 3: 5},
        port_load_types={2: 4, 3: 4},
    )
    assert len(result) == 2


def test_rule_f_proper_subset_guard_prevents_all_exclusion():
    """Rule F must not fire when the matching group is ALL ports — would leave empty result."""
    # 50 readings so on_hours < threshold, AND these are the only two custom-named ports.
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    base = _dt(2024, 4, 25, 0, 0, 0)
    readings = [
        {
            "timestamp": (base + _td(hours=i)).isoformat() + "Z",
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [
                _port(2, "Heater", 1 if i == 10 else 0, i == 10),
                _port(3, "Humidifer", 1 if i == 10 else 0, i == 10),
            ],
        }
        for i in range(50)
    ]
    result = build_activity_report(
        readings, days=1,
        port_loads={2: 1, 3: 1},
        port_load_types={2: 4, 3: 4},
    )
    # Proper-subset guard fires: {2,3} == all ports → Rule F does not exclude them.
    assert len(result) == 2


def test_rule_f_disabled_when_port_loads_none():
    """Rule F requires port_loads to be populated; if None, phantom detection is skipped."""
    readings = [
        _rule_f_reading(h, [
            _port(2, "Heater", 1 if h == 10 else 0, h == 10),
            _port(3, "Humidifer", 1 if h == 10 else 0, h == 10),
            _port(4, "Filter", 5, True),
        ])
        for h in range(24)
    ]
    result = build_activity_report(readings, days=1, port_loads=None)
    port_names = [r.name for r in result]
    assert "Heater" in port_names
    assert "Humidifer" in port_names


def test_rule_f_does_not_affect_auto_named_ports():
    """Auto-named 'Port N' ports are excluded from Rule F signature matching."""
    readings = [
        _rule_f_reading(h, [
            _port(2, "Port 2", 1 if h == 10 else 0, h == 10),
            _port(3, "Port 3", 1 if h == 10 else 0, h == 10),
            _port(4, "Filter", 5, True),
        ])
        for h in range(24)
    ]
    result = build_activity_report(
        readings, days=1,
        port_loads={2: 1, 3: 1, 4: 5},
    )
    # Port 2 and 3 might be filtered by Rule B (auto-named + low activity),
    # but NOT by Rule F — Rule F skips 'Port N' names.
    port_names = [r.name for r in result]
    assert "Filter" in port_names


# ============ Rule G — zero-load devType custom port with toggle-speed and low activity ===========


def _rule_g_readings(
    port_num: int,
    name: str,
    speed: int,
    on_count: int,
    total: int,
    days: int = 1,
) -> list[dict]:
    """Build readings for a named port: on_count on-readings (speed) then off."""
    readings = []
    for i in range(on_count):
        readings.append({
            "timestamp": _ts(i % 24, day=25 + i // 24),
            "temperature_c": 22.0, "temperature_f": 71.6,
            "humidity": 55.0, "vpd": 1.2,
            "ports": [_port(port_num, name, speed, True)],
        })
    for i in range(total - on_count):
        readings.append({
            "timestamp": _ts((on_count + i) % 24, day=25 + (on_count + i) // 24),
            "temperature_c": 22.0, "temperature_f": 71.6,
            "humidity": 55.0, "vpd": 1.2,
            "ports": [_port(port_num, name, 0, False)],
        })
    return readings


@pytest.mark.parametrize("on_count,total,days,expected_excluded", [
    # Case 1: on_hours = 1/48 * 24 * 1 = 0.5 < 1.0 → excluded
    (1, 48, 1, True),
    # Case 2: on_hours = 1/24 * 24 * 1 = 1.0 — exactly at threshold → NOT excluded
    (1, 24, 1, False),
    # Case 3: on_hours = 1/12 * 24 * 1 = 2.0 > 1.0 → NOT excluded
    (1, 12, 1, False),
    # Case 4: on_hours = 5/168 * 24 * 7 = 5.0; 5.0/7 = 0.714 < 1.0 → excluded
    (5, 168, 7, True),
    # Case 5: on_hours = 7/168 * 24 * 7 = 7.0; 7.0/7 = 1.0 exactly at threshold → NOT excluded
    (7, 168, 7, False),
])
def test_rule_g_threshold_parametrized(
    on_count: int, total: int, days: int, expected_excluded: bool
) -> None:
    """Rule G: devType=18, custom name 'Humidifier', avg_speed=1.0 — threshold boundary tests."""
    readings = _rule_g_readings(1, "Humidifier", speed=1, on_count=on_count, total=total)
    result = build_activity_report(readings, days=days, port_loads={1: 0}, dev_type=18)
    if expected_excluded:
        assert len(result) == 0, (
            f"Rule G: on_count={on_count}/{total} over {days}d should be excluded"
        )
    else:
        assert len(result) == 1, (
            f"Rule G: on_count={on_count}/{total} over {days}d should be kept"
        )


def test_rule_g_variable_speed_not_excluded() -> None:
    """Rule G does not fire when avg_speed_when_running != 1.0 (non-toggle device)."""
    # avg_speed=5.0 → condition avg_speed_when_running == 1.0 is False → Rule G skips
    readings = _rule_g_readings(1, "Humidifier", speed=5, on_count=1, total=48, days=1)
    result = build_activity_report(readings, days=1, port_loads={1: 0}, dev_type=18)
    assert len(result) == 1, "Rule G must not fire for avg_speed != 1.0"
    assert result[0].name == "Humidifier"


def test_rule_g_all_off_zero_speed_not_excluded() -> None:
    """Rule G does not fire when avg_speed_when_running == 0.0 (all readings off).

    All-off port: running_speeds=[] → avg_speed=0.0 ≠ 1.0 → Rule G condition is False.
    Rule B does not apply (custom name). Port_loads=None (devType=18). Port survives.
    """
    readings = _rule_g_readings(1, "Humidifier", speed=0, on_count=0, total=24, days=1)
    result = build_activity_report(readings, days=1, port_loads={1: 0}, dev_type=18)
    # Port is all-off but custom-named on devType=18 — no rule fires, it is kept
    assert len(result) == 1, "Rule G must not fire for avg_speed=0.0 (all-off port)"
    assert result[0].name == "Humidifier"


def test_rule_g_devtype11_not_affected() -> None:
    """Rule G condition checks dev_type in _ZERO_LOAD_DEV_TYPES; devType=11 is not affected."""
    readings = _rule_g_readings(1, "Humidifier", speed=1, on_count=1, total=48, days=1)
    result = build_activity_report(readings, days=1, port_loads={1: 0}, dev_type=11)
    # On devType=11: Rule D fires (avg_speed=1.0, port_loads={1:0}, not toggle) → excluded
    assert len(result) == 0, "devType=11 with avg_speed=1.0 and zero load is filtered by Rule D"


def test_rule_g_devtype22_custom_speed1_low_excluded() -> None:
    """Rule G: devType=22 also triggers — custom-named port, avg_speed=1.0, low activity."""
    readings = _rule_g_readings(1, "Humidifier", speed=1, on_count=1, total=48, days=1)
    result = build_activity_report(readings, days=1, port_loads={1: 0}, dev_type=22)
    assert len(result) == 0, "Rule G must fire on devType=22 (also in _ZERO_LOAD_DEV_TYPES)"


def test_rule_g_devtype22_above_threshold_not_excluded() -> None:
    """Regression guard: devType=22 custom port with sufficient activity is kept.

    on_hours = 12/24 * 24 * 1 = 12.0; 12.0/1 = 12.0 >> 1.0 → Rule G does not fire.
    """
    readings = _rule_g_readings(3, "R1 Clone Lights", speed=1, on_count=12, total=24, days=1)
    result = build_activity_report(readings, days=1, port_loads={3: 0}, dev_type=22)
    assert len(result) == 1, "Rule G must not fire for above-threshold activity on devType=22"
    assert result[0].name == "R1 Clone Lights"


def test_rule_g_auto_named_port_not_excluded() -> None:
    """Rule G does not fire for auto-named 'Port N' ports (name matches ^Port \\d+$).

    Rule B handles auto-named ports independently; Rule G targets custom names only.
    """
    readings = _rule_g_readings(2, "Port 2", speed=1, on_count=1, total=96, days=1)
    result = build_activity_report(readings, days=1, port_loads={2: 0}, dev_type=18)
    # Rule B runtime arm: on_hours = 1/96 * 24 = 0.25 < 1.0 → excluded by Rule B, not Rule G
    assert len(result) == 0, "Auto-named 'Port N' is filtered by Rule B, not Rule G"


def test_rule_g_api_constant_speed_port_in_output() -> None:
    """api_constant_speed ports exit before Rule G and appear in output with caveat.

    devType=22, 100% uptime speed=1 pattern → data_quality=api_constant_speed → early exit.
    Rule G is never evaluated for these ports.
    """
    # 24 readings, all on at speed=1 → transitions=0, uptime=100%, all_running_are_one
    # → api_constant_speed
    readings = _toggle_readings(name="R1 Clone Lights", port_num=1, speed=1, on=True, count=24)
    result = build_activity_report(
        readings, days=1,
        port_loads={1: 0},
        port_load_types={1: 129},  # non-standard loadType → is_toggle_hardware=False
        dev_type=22,
    )
    assert len(result) == 1, "api_constant_speed port must survive (not excluded by Rule G)"
    assert result[0].data_quality == "api_constant_speed", (
        "Port must exit via api_constant_speed early-exit, bypassing Rule G"
    )


def test_sampling_functions_importable_from_analytics() -> None:
    """analytics.py is the canonical home for these functions."""
    from ac_infinity_mcp.analytics import (
        _filter_readings_by_time,
        _parse_duration_seconds,
        apply_sampling,
        average_readings,
    )
    assert callable(apply_sampling)
    assert callable(average_readings)
    assert callable(_filter_readings_by_time)
    assert callable(_parse_duration_seconds)


def test_sampling_shim_still_works_from_server() -> None:
    """Re-export shim keeps legacy imports green during transition."""
    from ac_infinity_mcp.server import apply_sampling
    assert callable(apply_sampling)


# ============ Rule D must not fire where the load signal is absent ============
#
# Live devType-20 capture: a humidifier on port 4 cycled 74 times for 7.35h over
# two days on a 48% RH floor, and was excluded from the activity report as
# "no power detected". Three quirks stack to cause it:
#
#   Quirk 24/34  portsLoad is None and loadType is 0 on every AI+ port, so
#                "zero load" is vacuously true and is_toggle can never be true
#   Quirk 22     toggle hardware reports speed 1 while running
#
# leaving avg_speed <= 1.0 as Rule D's only real criterion.


def test_rule_d_keeps_cycling_toggle_port_on_zero_load_device() -> None:
    """A humidifier that genuinely cycled must survive Rule D on devType 20."""
    readings = _filter_port_readings(4, "Humidity", speed=1, on_count=12, total=24, days=1)
    kept = build_activity_report(
        readings, days=1, port_loads={4: 0}, port_load_types={4: 0}, dev_type=20
    )
    assert len(kept) == 1, "cycling port was ghost-filtered on a zero-load device"
    assert kept[0].port == 4


def test_rule_d_still_fires_on_devices_with_a_real_load_signal() -> None:
    """devType 11 reports portsLoad honestly, so zero load remains real evidence."""
    readings = _filter_port_readings(4, "Humidity", speed=1, on_count=12, total=24, days=1)
    kept = build_activity_report(
        readings, days=1, port_loads={4: 0}, port_load_types={4: 0}, dev_type=11
    )
    assert kept == [], "Rule D must still filter ghosts where load data is trustworthy"
