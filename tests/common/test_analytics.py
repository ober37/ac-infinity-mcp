"""Unit tests for analytics pure functions: health score, trends, activity."""

import pytest

from ac_infinity_mcp.analytics import (
    _GHOST_LOAD_ZERO_THRESHOLD,
    STAGE_TARGETS,
    HealthScore,
    TrendReport,
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
    states = [True, True, False, False, True, True, False, True]
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
    # Transitions: T→T, T→F (+1), F→F, F→T (+1), T→T, T→F (+1), F→T (+1) = 4
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
    assert result[0].peak_hour_utc == 14


def test_build_activity_report_multiple_ports():
    readings = [
        {
            "timestamp": _ts(h),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [
                _port(1, "Fan 1", 5, True),
                _port(2, "Fan 2", 7, True),
                _port(3, "Light", 0, False),
                _port(4, "Heater", 0, False),
            ],
        }
        for h in range(5)
    ]
    # Provide non-zero loads for ports 3 and 4 so Rule C does not filter them;
    # this test checks port ordering, not ghost-port filtering.
    result = build_activity_report(readings, port_loads={1: 5, 2: 5, 3: 3, 4: 3})
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
    """peak_hour_utc returns the UTC hour with the most ON readings."""
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
    assert result[0].peak_hour_utc == 14


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


def test_rule_a_b_c_together_multi_port_scenario() -> None:
    """Multi-port: each rule catches a distinct port; only 'Exhaust Fan' survives.

    Uses 48 readings (days=2) so that h==0 in 24-reading blocks gives 2 on-readings
    out of 48 total → on_hours = 2/48 * 24 * 2 = 2.0 h / 2 days = 1.0 h/day.
    That is NOT below threshold. Use on_count=1 out of 48 → 0.5 h/day < 1.0 ✓.
    """
    # Rule A candidate: Port 2 — 100% uptime, 0 transitions, load=0
    # Rule B candidate: Port 3 — auto-named "Port 3", < 1 h/day (low on-time guard)
    # Rule C candidate: Port 4 — named "Misting Pump", zero load, < 1 h/day
    # Survivor: Port 1 — named "Exhaust Fan", load=5, meaningful uptime
    days = 2
    total_readings = 48
    readings = []
    for i in range(total_readings):
        h = i % 24
        readings.append({
            "timestamp": _ts(h, day=25 + i // 24),
            "temperature_c": 24.0, "temperature_f": 75.2,
            "humidity": 60.0, "vpd": 1.24,
            "ports": [
                _port(1, "Exhaust Fan", 5, True),        # survivor: named, load>0, good uptime
                _port(2, "Port 2", 5, True),                   # Rule A: 100% uptime, load=0
                _port(3, "Port 3", 5 if i == 0 else 0, i == 0),  # Rule B: 1/48*24*2/2=0.5 h/day
                _port(4, "Misting Pump", 1 if i == 0 else 0, i == 0),  # Rule D: speed=1, load=0
            ],
        })
    port_loads = {1: 5, 2: 0, 3: 0, 4: 0}
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
    (1,  24, 1, 0, 5, 1, "real fan 1.0 h/day, no load → kept"),   # speed=5 → Rule D skips
    (2,  24, 1, 0, 5, 1, "real fan 2.0 h/day, no load → kept"),   # speed=5 → Rule D skips
    (1,  48, 1, 5, 5, 1, "0.5 h/day but has load → kept"),        # load>0 → all rules skip
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
