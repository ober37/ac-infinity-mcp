import copy
from unittest.mock import MagicMock

import pytest

from tests.fixtures.advance_automation_fixtures import MOCK_ADVANCE_AUTOMATIONS_LIST

MOCK_DEVICE_LEGACY: dict = {
    "devCode": "C58ZA",
    "devName": "Test 69 Pro",
    "devType": 11,
    # Per docs/API.md Quirk 7: devId is a string at the top level of device
    # records (large integer values that lose precision if int()-cast). The
    # value used here is a representative 19-digit ID matching the real API
    # shape; the previous int-typed fixture (12345) wouldn't surface a
    # precision-loss bug if a future caller did int(device["devId"]).
    # P2-C2-F011.
    "devId": "1424979258063367506",
    "online": True,
    "newFrameworkDevice": False,
    "devPortCount": 8,
    "firmwareVersion": "3.5.28",
    "hardwareVersion": "1.0",
    "zoneId": "America/Chicago",
    "deviceInfo": {
        "temperature": 2350,
        "temperatureF": 7430,
        "humidity": 6000,
        "vpdnums": 124,
        "unit": 1,
        "ports": [
            {"port": 1, "portName": "Intake Fan", "speak": 5, "portsLoad": 1,
             "loadState": 1, "curMode": 3, "remainTime": 0, "portResistance": 7500},
            {"port": 2, "portName": "Exhaust Fan", "speak": 7, "portsLoad": 1,
             "loadState": 1, "curMode": 2, "remainTime": 0, "portResistance": 7500},
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
# from the developer's environment. See P2-F011 in
# .claude/internal/REVIEW_FINDINGS.md (gitignored).


@pytest.fixture
def mock_client():
    """MagicMock of ACInfinityClient with sensible defaults.

    Default return values are deep-copied so a test that mutates them
    (e.g. ``mock_client.parse_device_data.return_value["vpd"] = 0.5``) does not
    leak state to other tests. Without the copy, the shared module-level dict
    would carry the mutation across test boundaries (P2-F016).

    Wires the mock into the server via setup() so tests don't need
    ``with patch("ac_infinity_mcp.server.aci_client", mock_client):``.
    """
    from ac_infinity_mcp.server import setup
    client = MagicMock()
    client.get_devices.return_value = [copy.deepcopy(MOCK_DEVICE_LEGACY)]
    client.parse_device_data.return_value = copy.deepcopy({
        "timestamp": "2026-01-01T00:00:00Z",
        "device_id": "C58ZA",
        "device_name": "Test 69 Pro",
        "temperature_c": 23.5,
        "temperature_f": 74.3,
        "humidity": 60.0,
        "vpd": 1.24,
        "ports": [],
        "external_sensors": [],
        "zone_id": "America/Chicago",
        "temp_unit_raw": 1,
    })
    # Advance Automation defaults.
    client.get_advance_automations.return_value = copy.deepcopy(MOCK_ADVANCE_AUTOMATIONS_LIST)
    client.enable_advance_automation.return_value = {"code": 200}
    client.disable_advance_automation.return_value = {"code": 200}
    client.create_advance_automation.return_value = {"advId": 2302819}
    client.delete_advance_automation.return_value = {"code": 200}
    setup(client)
    yield client
    setup(MagicMock())
