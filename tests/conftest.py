from unittest.mock import MagicMock

import pytest

MOCK_DEVICE_LEGACY: dict = {
    "devCode": "C58ZA",
    "devName": "Test 69 Pro",
    "devType": 11,
    "devId": 12345,
    "online": True,
    "newFrameworkDevice": False,
    "deviceInfo": {
        "temperature": 2350,
        "temperatureF": 7430,
        "humidity": 6000,
        "vpdnums": 124,
        "ports": [
            {"port": 1, "portName": "Intake Fan", "speak": 5, "portsLoad": 1,
             "loadState": 1, "curMode": 3, "remainTime": 0},
            {"port": 2, "portName": "Exhaust Fan", "speak": 7, "portsLoad": 1,
             "loadState": 1, "curMode": 2, "remainTime": 0},
        ],
    },
}

MOCK_DEVICE_AI_PLUS: dict = {
    "devCode": "D89XA",
    "devName": "Test 89 AI+",
    "devType": 20,
    "devId": 67890,
    "online": True,
    "newFrameworkDevice": True,
    "deviceInfo": {
        "temperature": 2400,
        "temperatureF": 7520,
        "humidity": 5500,
        "vpdnums": 150,
        "ports": [{"port": 1, "portName": "Port 1", "speak": 0, "portsLoad": 0,
                   "loadState": 0, "curMode": 1, "remainTime": None}],
    },
}


# NOTE: the autouse fixture that injects test credentials lives in
# tests/common/conftest.py and tests/devices/conftest.py — explicitly NOT here.
# Putting it at the tests/ root would apply to tests/integration/test_live.py,
# which depends on the REAL AC_INFINITY_EMAIL / AC_INFINITY_PASSWORD captured
# from the developer's environment. See P2-F011 in REVIEW_FINDINGS.md.


@pytest.fixture
def mock_client():
    """MagicMock of ACInfinityClient with sensible defaults."""
    client = MagicMock()
    client.get_devices.return_value = [MOCK_DEVICE_LEGACY]
    client.parse_device_data.return_value = {
        "timestamp": "2026-01-01T00:00:00Z",
        "device_id": "C58ZA",
        "device_name": "Test 69 Pro",
        "temperature_c": 23.5,
        "temperature_f": 74.3,
        "humidity": 60.0,
        "vpd": 1.24,
        "ports": [],
        "external_sensors": [],
    }
    return client
