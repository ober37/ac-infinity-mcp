"""Live integration tests — skipped in CI without AC_INFINITY_EMAIL credential.

Belt-and-braces opt-in: the live marker is deselected by default in pyproject.toml.
A contributor who wants to run these must pass ``-m live`` explicitly. CI does
not pass that flag, so even if AC_INFINITY_EMAIL leaked into the workflow env
(e.g. via a future secrets edit), the suite would still skip. See P3-F009.
"""

import asyncio
import json
import os
from datetime import date, timedelta

import pytest

from ac_infinity_mcp import server as srv
from ac_infinity_mcp.client import ACInfinityClient

pytestmark = [
    pytest.mark.live,  # opt-in marker — deselected by default (see pyproject.toml)
    pytest.mark.skipif(
        not os.getenv("AC_INFINITY_EMAIL"),
        reason="AC_INFINITY_EMAIL not set — skipping live API tests",
    ),
]


# ---------------------------------------------------------------------------
# Module-scoped fixtures — authenticate once, reuse across all live tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def authed_client() -> ACInfinityClient:
    email = os.getenv("AC_INFINITY_EMAIL", "")
    password = os.getenv("AC_INFINITY_PASSWORD", "")
    client = ACInfinityClient(email, password)
    assert client.authenticate() is True, "Live authentication failed"
    srv.setup(client)
    return client


@pytest.fixture(scope="module")
def live_device_id(authed_client: ACInfinityClient) -> str:
    devices = authed_client.get_devices()
    assert devices, "No devices found on live account"
    return str(devices[0]["devCode"])


# ---------------------------------------------------------------------------
# Original 3 tests (preserved)
# ---------------------------------------------------------------------------


def test_live_authenticate(authed_client: ACInfinityClient) -> None:
    # authed_client is already authenticated — verify token is present
    assert authed_client.token is not None


def test_live_get_devices(authed_client: ACInfinityClient) -> None:
    devices = authed_client.get_devices()
    assert devices is not None
    assert len(devices) > 0


def test_live_get_device_reading(authed_client: ACInfinityClient, live_device_id: str) -> None:
    result = asyncio.run(srv.get_device_reading(live_device_id))
    data = json.loads(result)
    assert "temperature_c" in data
    assert "error" not in data


# ---------------------------------------------------------------------------
# Stream B — expanded live tests covering all 11 tools
# ---------------------------------------------------------------------------


def test_live_get_all_device_readings(authed_client: ACInfinityClient) -> None:
    result = asyncio.run(srv.get_all_device_readings())
    data = json.loads(result)
    assert "readings" in data
    assert len(data["readings"]) >= 1
    assert "error" not in data


def test_live_check_vpd_drift_veg(
    authed_client: ACInfinityClient, live_device_id: str
) -> None:
    result = asyncio.run(srv.check_vpd_drift(live_device_id, "veg"))
    data = json.loads(result)
    assert "current_vpd" in data
    assert data["status"] in {"OK", "LOW", "HIGH"}
    assert len(data["target_range"]) == 2


def test_live_get_environment_health(
    authed_client: ACInfinityClient, live_device_id: str
) -> None:
    result = asyncio.run(srv.get_environment_health(live_device_id, "veg"))
    data = json.loads(result)
    assert "score" in data
    assert 0 <= data["score"] <= 100
    assert data["grade"] in {"A", "B", "C", "D", "F"}


def test_live_get_historical_readings_1h(
    authed_client: ACInfinityClient, live_device_id: str
) -> None:
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=1)).isoformat()
    result = asyncio.run(
        srv.get_historical_readings(live_device_id, start, end, sample_interval="1h")
    )
    data = json.loads(result)
    assert "readings" in data, f"unexpected response: {data}"
    assert len(data["readings"]) >= 1
    assert "statistics" in data


def test_live_detect_environment_trends(
    authed_client: ACInfinityClient, live_device_id: str
) -> None:
    result = asyncio.run(srv.detect_environment_trends(live_device_id, days=3))
    data = json.loads(result)
    assert "trends" in data
    for trend in data["trends"]:
        assert "metric" in trend
        assert "slope" in trend
        assert "direction" in trend


def test_live_get_port_activity_report(
    authed_client: ACInfinityClient, live_device_id: str
) -> None:
    result = asyncio.run(srv.get_port_activity_report(live_device_id, days=3))
    data = json.loads(result)
    assert "ports" in data
    for port in data["ports"]:
        assert "on_hours" in port
        assert "uptime_pct" in port


def test_live_set_port_speed_dry_run(
    authed_client: ACInfinityClient, live_device_id: str
) -> None:
    result = asyncio.run(srv.set_port_speed(live_device_id, port=1, speed=5, dry_run=True))
    data = json.loads(result)
    assert data.get("dry_run") is True
    assert data.get("sent") is False


def test_live_set_port_on_dry_run(
    authed_client: ACInfinityClient, live_device_id: str
) -> None:
    result = asyncio.run(srv.set_port_on(live_device_id, port=1, dry_run=True))
    data = json.loads(result)
    assert data.get("dry_run") is True
    assert data.get("sent") is False


def test_live_set_port_off_dry_run(
    authed_client: ACInfinityClient, live_device_id: str
) -> None:
    result = asyncio.run(srv.set_port_off(live_device_id, port=1, dry_run=True))
    data = json.loads(result)
    assert data.get("dry_run") is True
    assert data.get("sent") is False


def test_live_apply_grow_stage_template_dry_run(
    authed_client: ACInfinityClient, live_device_id: str
) -> None:
    result = asyncio.run(
        srv.apply_grow_stage_template(live_device_id, port=1, stage="veg", dry_run=True)
    )
    data = json.loads(result)
    # Cycle 1 refactor (commit 4fd7497) collapsed the three sequential writes
    # into a single atomic write — response is now flat sent/payload, not
    # per-target nested. P2-C2-F002.
    assert data.get("dry_run") is True
    assert data["sent"] is False
    assert "payload" in data
    assert data["vpd"]["target_kpa"] == 1.25
    assert data["temperature"]["min_c"] == 20.0
    assert data["temperature"]["max_c"] == 28.0
    assert data["humidity"]["min_rh"] == 50.0
    assert data["humidity"]["max_rh"] == 70.0
    assert "error" not in data


def test_live_get_historical_readings_time_filter(
    authed_client: ACInfinityClient, live_device_id: str
) -> None:
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=1)).isoformat()
    result = asyncio.run(
        srv.get_historical_readings(
            live_device_id, start, end, sample_interval="1h",
            time_start="00:00", time_end="23:59",
        )
    )
    data = json.loads(result)
    assert "readings" in data, f"unexpected response: {data}"
