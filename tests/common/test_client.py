"""Unit tests for ACInfinityClient — data parsing and HTTP methods."""

from unittest.mock import patch

import pytest
import requests
import responses as responses_lib

from ac_infinity_mcp.client import ACInfinityClient
from ac_infinity_mcp.schema import (
    ACInfinityAdvanceConflictError,
    ACInfinityAPIError,
    ACInfinityAuthError,
    ACInfinityDeviceError,
)
from tests.fixtures.mock_api_responses import (
    AUTH_FAILURE,
    AUTH_SUCCESS,
    DEVICES_API_ERROR,
    DEVICES_EMPTY,
    DEVICES_SUCCESS,
    HISTORY_EMPTY,
    HISTORY_PAGE_1,
)
from tests.fixtures.mock_mode_settings_ai_plus import MOCK_MODE_SETTINGS_AI_PLUS_PORT1
from tests.fixtures.mock_mode_settings_legacy import MOCK_MODE_SETTINGS_LEGACY_PORT1

LOGIN_URL = "https://www.acinfinityserver.com/api/user/appUserLogin"
DEVICES_URL = "https://www.acinfinityserver.com/api/user/devInfoListAll"
HISTORY_URL = "https://www.acinfinityserver.com/api/log/dataPage"
MODE_SETTINGS_URL = "https://www.acinfinityserver.com/api/dev/getdevModeSettingList"
ADD_DEV_MODE_URL = "https://www.acinfinityserver.com/api/dev/addDevMode"


@pytest.fixture
def client():
    return ACInfinityClient("test@example.com", "password123")


def test_base_url_is_https():
    """docs/API.md Quirk 8: HTTPS confirmed 2026-05-29 (TLSv1.3, DigiCert). Guards the
    scheme invariant so a regression to plain HTTP fails CI (P2-F007).
    """
    assert ACInfinityClient.BASE_URL.startswith("https://")
    # Confirm derived endpoints inherit the scheme
    for endpoint in (
        ACInfinityClient.LOGIN_ENDPOINT,
        ACInfinityClient.DEVICES_ENDPOINT,
        ACInfinityClient.HISTORY_ENDPOINT,
        ACInfinityClient.MODE_SETTINGS_ENDPOINT,
        ACInfinityClient.ADD_DEV_MODE_ENDPOINT,
        ACInfinityClient.MODE_AND_SETTING_ENDPOINT,
    ):
        assert endpoint.startswith("https://")


@pytest.fixture
def authed_client():
    c = ACInfinityClient("test@example.com", "password123")
    c.token = "tok_test_abc123"
    return c


# ============ parse_device_data ============

MOCK_DEVICE = {
    "devCode": "C58ZA",
    "devName": "Test Controller",
    "deviceInfo": {
        "temperature": 2350,
        "temperatureF": 7430,
        "humidity": 6000,
        "vpdnums": 124,
        "ports": [
            {"port": 1, "portName": "Intake Fan", "speak": 5, "portsLoad": 1, "loadState": 1},
            {"port": 2, "portName": "Exhaust Fan", "speak": 7, "portsLoad": 1, "loadState": 1},
        ],
    },
}


def test_parse_device_data_divide_by_100(client):
    result = client.parse_device_data(MOCK_DEVICE)
    assert result["temperature_c"] == 23.5
    assert result["temperature_f"] == 74.3
    assert result["humidity"] == 60.0
    assert result["vpd"] == 1.24


def test_parse_device_data_device_id(client):
    result = client.parse_device_data(MOCK_DEVICE)
    assert result["device_id"] == "C58ZA"
    assert result["device_name"] == "Test Controller"


def test_parse_device_data_ports(client):
    result = client.parse_device_data(MOCK_DEVICE)
    assert len(result["ports"]) == 2
    assert result["ports"][0]["name"] == "Intake Fan"
    assert result["ports"][0]["speed"] == 5
    assert result["ports"][1]["name"] == "Exhaust Fan"
    assert result["ports"][1]["speed"] == 7
    assert "load" not in result["ports"][0]
    assert "load" not in result["ports"][1]
    # Running ports (speak>0) never get plug_status regardless of loadState
    assert "plug_status" not in result["ports"][0]
    assert "plug_status" not in result["ports"][1]


def _port_device(load_state, speak=0, port_name="Port 1"):
    """Build a minimal device dict with one port for plug_status edge-case tests."""
    port: dict = {"port": 1, "portName": port_name, "speak": speak, "portsLoad": 0}
    if load_state is not None:
        port["loadState"] = load_state
    return {
        "devCode": "C58ZA",
        "devName": "Test",
        "deviceInfo": {
            "temperature": 2350, "temperatureF": 7430,
            "humidity": 6000, "vpdnums": 124,
            "ports": [port],
        },
    }


@pytest.mark.parametrize("load_state,speak,port_name,expect_plug_status", [
    (0, 0, "Port 1", True),        # default-named, no load, not running → plug_status
    (1, 0, "Port 1", False),       # default-named, connected but idle → no plug_status
    (0, 5, "Port 1", False),       # default-named, loadState=0 but running → no plug_status
    (1, 5, "Port 1", False),       # default-named, connected and running → no plug_status
    (None, 0, "Port 1", True),     # default-named, None loadState treated as 0 → plug_status
    (2, 0, "Port 1", False),       # default-named, any nonzero loadState → no plug_status
    (0, 0, "Humidifier", False),   # custom-named, no load → no plug_status (named = intentional)
    (None, 0, "Heater", False),    # custom-named, None loadState → no plug_status
])
def test_parse_device_data_port_plug_status(client, load_state, speak, port_name, expect_plug_status):  # noqa: E501
    device = _port_device(load_state, speak, port_name)
    result = client.parse_device_data(device)
    if expect_plug_status:
        assert result["ports"][0].get("plug_status") == "not powered"
    else:
        assert "plug_status" not in result["ports"][0]


def test_parse_device_data_no_sensors(client):
    result = client.parse_device_data(MOCK_DEVICE)
    assert result["external_sensors"] == []


def test_parse_device_data_with_external_sensors(client):
    device = {
        "devCode": "C58ZA",
        "devName": "Test",
        "deviceInfo": {
            "temperature": 2400,
            "temperatureF": 7520,
            "humidity": 5500,
            "vpdnums": 150,
            "ports": [],
            "sensors": [
                {"accessPort": 1, "sensorType": 11, "sensorData": 85000},
            ],
        },
    }
    result = client.parse_device_data(device)
    assert len(result["external_sensors"]) == 1
    assert result["external_sensors"][0]["sensor_id"] == "1.11"
    assert result["external_sensors"][0]["value"] == pytest.approx(850.0, abs=0.1)


# ============ parse_device_data — phantom sensor filtering ============


def _sensor_entry(sensor_type, sensor_data=0, precision=100, access_port=1):
    return {
        "sensorType": sensor_type,
        "sensorData": sensor_data,
        "sensorPrecision": precision,
        "accessPort": access_port,
    }


def _device_with_sensor_list(sensors):
    return {
        "devCode": "C58ZA",
        "devName": "Test",
        "deviceInfo": {
            "temperature": 2400,
            "temperatureF": 7520,
            "humidity": 5500,
            "vpdnums": 150,
            "ports": [],
            "sensors": sensors,
        },
    }


def test_parse_device_data_phantom_unrecognized_zero_excluded(client):
    """sensorType=99, sensorData=0 → excluded (phantom)."""
    device = _device_with_sensor_list([_sensor_entry(sensor_type=99, sensor_data=0)])
    result = client.parse_device_data(device)
    assert result["external_sensors"] == []


def test_parse_device_data_unrecognized_nonzero_included(client):
    """sensorType=99, sensorData=9900 → included with label 'unrecognized (type 99)'."""
    device = _device_with_sensor_list([_sensor_entry(sensor_type=99, sensor_data=9900)])
    result = client.parse_device_data(device)
    assert len(result["external_sensors"]) == 1
    assert result["external_sensors"][0]["sensor_type_label"] == "unrecognized (type 99)"
    assert result["external_sensors"][0]["value"] == pytest.approx(99.0)


def test_parse_device_data_recognized_zero_included(client):
    """sensorType=11 (CO2), sensorData=0 → always included even at zero."""
    device = _device_with_sensor_list([_sensor_entry(sensor_type=11, sensor_data=0)])
    result = client.parse_device_data(device)
    assert len(result["external_sensors"]) == 1
    assert result["external_sensors"][0]["sensor_type"] == 11
    assert result["external_sensors"][0]["sensor_type_label"] == "co2"


def test_parse_device_data_sensor_type_none_excluded(client):
    """sensorType=None → excluded regardless of sensorData."""
    device = _device_with_sensor_list([_sensor_entry(sensor_type=None, sensor_data=500)])
    result = client.parse_device_data(device)
    assert result["external_sensors"] == []


def test_parse_device_data_sensor_data_none_excluded(client):
    """sensorType=99, sensorData=None → excluded (None treated as 0, unrecognized type)."""
    device = _device_with_sensor_list([_sensor_entry(sensor_type=99, sensor_data=None)])
    result = client.parse_device_data(device)
    assert result["external_sensors"] == []


def test_parse_device_data_sensor_type_none_data_none_excluded(client):
    """sensorType=None, sensorData=None → excluded."""
    device = _device_with_sensor_list([_sensor_entry(sensor_type=None, sensor_data=None)])
    result = client.parse_device_data(device)
    assert result["external_sensors"] == []


def test_parse_device_data_mixed_sensor_list(client):
    """Mixed sensor list: phantom excluded, recognized/nonzero-unrecognized included."""
    sensors = [
        _sensor_entry(sensor_type=99, sensor_data=0, access_port=1),  # phantom — excluded
        _sensor_entry(sensor_type=11, sensor_data=45000, precision=100, access_port=2),  # included
        _sensor_entry(sensor_type=None, sensor_data=500, access_port=3),  # no type — excluded
        _sensor_entry(sensor_type=21, sensor_data=8550, precision=100, access_port=4),  # included
    ]
    device = _device_with_sensor_list(sensors)
    result = client.parse_device_data(device)
    assert len(result["external_sensors"]) == 2
    labels = {s["sensor_type"]: s["sensor_type_label"] for s in result["external_sensors"]}
    assert labels[11] == "co2"
    assert labels[21] == "unrecognized (type 21)"
    values = {s["sensor_type"]: s["value"] for s in result["external_sensors"]}
    assert values[11] == pytest.approx(450.0)
    assert values[21] == pytest.approx(85.5)


# devType=22 phantom sensor fixture (real field values from Proxyman capture)
MOCK_PHANTOM_SENSORS_DEVTYPE22 = [
    {"sensorType": 4, "sensorUnit": 0, "sensorPrecision": 3, "sensorTrend": 0,
     "accessPort": 7, "sensorData": 6320, "sensorKey": "4-7"},
    {"sensorType": 6, "sensorUnit": 0, "sensorPrecision": 3, "sensorTrend": 2,
     "accessPort": 7, "sensorData": 5710, "sensorKey": "6-7"},
    {"sensorType": 7, "sensorUnit": 0, "sensorPrecision": 3, "sensorTrend": 0,
     "accessPort": 7, "sensorData": 83, "sensorKey": "7-7"},
]


def test_should_include_sensor_devtype22_phantoms_excluded(client):
    """sensorType 4, 6, 7 with non-zero sensorData → excluded (devType=22 internal bus readings)."""
    for entry in MOCK_PHANTOM_SENSORS_DEVTYPE22:
        device = _device_with_sensor_list([entry])
        result = client.parse_device_data(device)
        st = entry["sensorType"]
        assert result["external_sensors"] == [], f"sensorType={st} should be excluded"


def test_should_include_sensor_any_lt10_not_in_label_dict_excluded(client):
    """Any sensorType < 10 not in _SENSOR_TYPE_LABELS → excluded regardless of sensorData."""
    for st in range(1, 10):
        entry = {"sensorType": st, "sensorData": 9999, "sensorPrecision": 100, "accessPort": 1}
        device = _device_with_sensor_list([entry])
        result = client.parse_device_data(device)
        assert result["external_sensors"] == [], f"sensorType={st} should be excluded"


def test_should_include_sensor_recognized_type_zero_still_included(client):
    """sensorType=10 (soil_moisture), sensorData=0 → always included even at zero."""
    entry = {"sensorType": 10, "sensorData": 0, "sensorPrecision": 100, "accessPort": 1}
    device = _device_with_sensor_list([entry])
    result = client.parse_device_data(device)
    assert len(result["external_sensors"]) == 1
    assert result["external_sensors"][0]["sensor_type_label"] == "soil_moisture"


def test_parse_device_data_devtype22_fixture_zero_external_sensors(client):
    """devType=22 fixture with three phantom sensors → zero external sensors in response."""
    device = _device_with_sensor_list(MOCK_PHANTOM_SENSORS_DEVTYPE22)
    result = client.parse_device_data(device)
    assert result["external_sensors"] == []


# ============ parse_history_record ============

def test_parse_history_record_divide_by_100(client):
    record = {
        "createTime": 1714000000,
        "temperature": 2400,
        "fTemperature": 7520,
        "humidity": 5500,
        "vpdNums": 150,
        "portSpead": 0,
        "portStatus": 0,
        "devPortCount": 4,
    }
    result = client.parse_history_record(record)
    assert result["temperature_c"] == 24.0
    assert result["temperature_f"] == 75.2
    assert result["humidity"] == 55.0
    assert result["vpd"] == 1.5


def test_parse_history_record_nibble_decoding(client):
    port_spead = (7 << 4) | 5  # port1=5, port2=7
    record = {
        "createTime": 1714000000,
        "temperature": 0,
        "fTemperature": 0,
        "humidity": 0,
        "vpdNums": 0,
        "portSpead": port_spead,
        "portStatus": 0b11,
        "devPortCount": 4,
    }
    result = client.parse_history_record(record)
    ports = {p["port"]: p for p in result["ports"]}
    assert ports[1]["speed"] == 5
    assert ports[1]["on"] is True
    assert ports[2]["speed"] == 7
    assert ports[2]["on"] is True
    assert ports[3]["speed"] == 0
    assert ports[3]["on"] is False


def test_parse_history_record_toggle_device_oxf(client):
    record = {
        "createTime": 1714000000,
        "temperature": 0,
        "fTemperature": 0,
        "humidity": 0,
        "vpdNums": 0,
        "portSpead": 0xF,
        "portStatus": 0,
        "devPortCount": 4,
    }
    result = client.parse_history_record(record)
    assert result["ports"][0]["speed"] == 1


def test_parse_history_record_port_names(client):
    record = {
        "createTime": 1714000000,
        "temperature": 0,
        "fTemperature": 0,
        "humidity": 0,
        "vpdNums": 0,
        "portSpead": 0,
        "portStatus": 0,
        "devPortCount": 2,
    }
    result = client.parse_history_record(record, port_names={1: "Intake Fan", 2: "Exhaust Fan"})
    assert result["ports"][0]["name"] == "Intake Fan"
    assert result["ports"][1]["name"] == "Exhaust Fan"


@pytest.mark.parametrize("bad_record", [
    # P3-F011 (Cycle 1): TypeError path — portSpead is a string
    {"createTime": 1714000000, "portSpead": "not-an-int", "portStatus": 0, "devPortCount": 2},
    # P2-C2-F007: ValueError path — createTime is non-numeric string
    {"createTime": "not-a-number", "temperature": 0, "fTemperature": 0,
     "humidity": 0, "vpdNums": 0, "portSpead": 0, "portStatus": 0, "devPortCount": 1},
])
def test_parse_history_record_raises_typed_error_on_malformed_input(client, bad_record):
    """Upstream structural errors → ACInfinityAPIError (P3-F011, P2-C2-F007)."""
    with pytest.raises(ACInfinityAPIError, match="malformed history record"):
        client.parse_history_record(bad_record)


@pytest.mark.parametrize("bad_device", [
    # P3-F011 (Cycle 1): TypeError path — temperature is a string
    {"devCode": "C58ZA", "devName": "Test", "deviceInfo": {
        "temperature": "not-an-int", "ports": [],
    }},
    # P2-C2-F007: AttributeError path — deviceInfo is not a dict
    {"devCode": "C58ZA", "devName": "Test", "deviceInfo": "not-a-dict"},
    # P2-C2-F007: AttributeError path — sensors is a string (not iterable of dicts)
    {"devCode": "C58ZA", "devName": "Test", "deviceInfo": {
        "temperature": 2300, "ports": [], "sensors": "garbage",
    }},
])
def test_parse_device_data_raises_typed_error_on_malformed_input(client, bad_device):
    """Upstream structural errors in device dict → ACInfinityAPIError (P3-F011, P2-C2-F007)."""
    with pytest.raises(ACInfinityAPIError, match="malformed device data"):
        client.parse_device_data(bad_device)


def test_parse_history_record_automation_flag_does_not_force_on(client):
    """Quirk 6: portStatus is automation-triggered, NOT on/off (P1-F008).

    Speed nibble alone must determine `on`. Previously, a port with portStatus
    bit set but nibble=0 was reported as on=True, overstating activity. The
    automation flag is now exposed as a separate `automation_triggered` field.
    """
    record = {
        "createTime": 1714000000,
        "temperature": 0,
        "fTemperature": 0,
        "humidity": 0,
        "vpdNums": 0,
        "portSpead": 0,           # all ports idle
        "portStatus": 0b00000001, # automation armed on port 1, idle on others
        "devPortCount": 2,
    }
    result = client.parse_history_record(record)
    assert result["ports"][0]["on"] is False
    assert result["ports"][0]["automation_triggered"] is True
    assert result["ports"][1]["on"] is False
    assert result["ports"][1]["automation_triggered"] is False


@pytest.mark.parametrize("missing_devPortCount", [
    {},          # field absent entirely
    {"devPortCount": None},  # field present but null — Quirk 5 documents this
])
def test_parse_history_record_devPortCount_null_falls_back_to_8(client, missing_devPortCount):
    """docs/API.md Quirk 5: devPortCount is often null in history records; fall back to 8.

    A regression to record.get("devPortCount", 8) (which returns None for an
    explicit-null field rather than the default) would cause range(None) to
    raise TypeError. P2-F004.
    """
    record = {
        "createTime": 1714000000,
        "temperature": 0,
        "fTemperature": 0,
        "humidity": 0,
        "vpdNums": 0,
        "portSpead": 0,
        "portStatus": 0,
        **missing_devPortCount,
    }
    result = client.parse_history_record(record)
    assert len(result["ports"]) == 8
    assert [p["port"] for p in result["ports"]] == list(range(1, 9))


# ============ rate limit ============

def test_rate_limit_field_exists(client):
    assert hasattr(client, "_last_write_time")
    assert client._last_write_time == 0.0


def test_enforce_write_rate_limit_is_callable(client):
    assert callable(client._enforce_write_rate_limit)


def test_enforce_write_rate_limit_sleeps_when_elapsed_less_than_1_5s(client, monkeypatch):
    """Mock the clock so the gate's sleep duration is asserted without waiting real time.

    Real-clock tests added ~1.5s per case and risked CI flake on loaded runners.
    By patching time.monotonic and time.sleep in the client module, we assert the
    behavioural contract (sleep when elapsed < 1.5s) without burning wall-clock (P2-F012).
    """
    fake_now = [100.0]
    sleep_calls: list[float] = []

    def fake_monotonic() -> float:
        return fake_now[0]

    def fake_sleep(duration: float) -> None:
        sleep_calls.append(duration)
        fake_now[0] += duration

    monkeypatch.setattr("ac_infinity_mcp.client.time.monotonic", fake_monotonic)
    monkeypatch.setattr("ac_infinity_mcp.client.time.sleep", fake_sleep)

    # First call from cold — no sleep
    client._last_write_time = 0.0
    client._enforce_write_rate_limit()
    assert sleep_calls == []  # nothing slept on the first call

    # Second call only 0.4s after the first — must sleep the remaining 1.1s
    fake_now[0] += 0.4
    client._enforce_write_rate_limit()
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == pytest.approx(1.1, abs=0.01)

    # Third call 2s after the second — already past the rate-limit window
    fake_now[0] += 2.0
    client._enforce_write_rate_limit()
    assert len(sleep_calls) == 1  # no additional sleep


def test_mark_write_completed_anchors_next_gap_from_post_return(client, monkeypatch):
    """_last_write_time is reset after the POST returns so the next gap is measured
    from completion, not start (P1-F015).
    """
    fake_now = [100.0]

    def fake_monotonic() -> float:
        return fake_now[0]

    monkeypatch.setattr("ac_infinity_mcp.client.time.monotonic", fake_monotonic)
    monkeypatch.setattr("ac_infinity_mcp.client.time.sleep", lambda _: None)

    client._last_write_time = 0.0
    client._enforce_write_rate_limit()
    start_ts = client._last_write_time

    # Simulate a 500ms POST
    fake_now[0] += 0.5
    client._mark_write_completed()
    completion_ts = client._last_write_time

    assert completion_ts == start_ts + 0.5
    assert completion_ts == fake_now[0]


def test_enforce_write_rate_limit_lock_serializes_concurrent_writes(client, monkeypatch):
    """Concurrent rate-limit calls must serialize via the lock.

    Uses a fake clock so the test does not burn ~3s of real wall-clock waiting
    for the rate-limit gate. The serialization assertion comes from the lock
    forcing sequential entry, not from real-clock observations (P2-F012).
    """
    import threading

    fake_now = [100.0]
    monotonic_lock = threading.Lock()

    def fake_monotonic() -> float:
        with monotonic_lock:
            return fake_now[0]

    def fake_sleep(duration: float) -> None:
        with monotonic_lock:
            fake_now[0] += duration

    monkeypatch.setattr("ac_infinity_mcp.client.time.monotonic", fake_monotonic)
    monkeypatch.setattr("ac_infinity_mcp.client.time.sleep", fake_sleep)

    client._last_write_time = fake_now[0] - 10.0  # cold start
    entry_times: list[float] = []

    def call_and_record() -> None:
        client._enforce_write_rate_limit()
        entry_times.append(client._last_write_time)

    threads = [threading.Thread(target=call_and_record) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    entry_times.sort()
    # Each call updates _last_write_time after enforcing the gate, so successive
    # entries must be at least 1.5s apart in simulated time.
    for i in range(1, len(entry_times)):
        assert entry_times[i] - entry_times[i - 1] >= 1.5


# ============ authenticate ============

@responses_lib.activate
def test_authenticate_success(client):
    responses_lib.add(responses_lib.POST, LOGIN_URL, json=AUTH_SUCCESS, status=200)
    result = client.authenticate()
    assert result is True
    assert client.token == "tok_test_abc123"


@responses_lib.activate
def test_authenticate_wrong_credentials(client):
    """Real API returns code=400 (not 401) for bad credentials — see docs/API.md.

    AUTH_FAILURE fixture mirrors that shape exactly so any future branch on
    code==400 has accurate test data (P2-F008).
    """
    responses_lib.add(responses_lib.POST, LOGIN_URL, json=AUTH_FAILURE, status=200)
    result = client.authenticate()
    assert result is False
    assert client.token is None
    # Pin the fixture's documented shape so a regression to a thin mock fails.
    assert AUTH_FAILURE["code"] == 400
    assert "wrong" in AUTH_FAILURE["msg"].lower()


@responses_lib.activate
def test_authenticate_connection_error(client, monkeypatch):
    """Persistent ConnectionError returns False only after tenacity exhausts retries."""
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda _: None)  # no real backoff
    responses_lib.add(
        responses_lib.POST,
        LOGIN_URL,
        body=requests.exceptions.ConnectionError("connection refused"),
    )
    result = client.authenticate()
    assert result is False
    # tenacity retries 3 times — proves the wrapper is in place (P1-F005)
    assert len(responses_lib.calls) == 3


@responses_lib.activate
def test_authenticate_timeout(client, monkeypatch):
    """Persistent Timeout returns False after retry exhaustion."""
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda _: None)
    responses_lib.add(
        responses_lib.POST,
        LOGIN_URL,
        body=requests.exceptions.Timeout("timed out"),
    )
    result = client.authenticate()
    assert result is False
    assert len(responses_lib.calls) == 3


@responses_lib.activate
def test_authenticate_recovers_from_transient_connection_error(client, monkeypatch):
    """Transient ConnectionError is retried; eventual success returns True (P1-F005)."""
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda _: None)
    responses_lib.add(
        responses_lib.POST, LOGIN_URL,
        body=requests.exceptions.ConnectionError("transient"),
    )
    responses_lib.add(
        responses_lib.POST, LOGIN_URL,
        body=requests.exceptions.ConnectionError("transient"),
    )
    responses_lib.add(responses_lib.POST, LOGIN_URL, json=AUTH_SUCCESS, status=200)
    result = client.authenticate()
    assert result is True
    assert client.token == "tok_test_abc123"
    assert len(responses_lib.calls) == 3


@responses_lib.activate
def test_authenticate_uses_appPasswordl_typo(client):
    responses_lib.add(responses_lib.POST, LOGIN_URL, json=AUTH_SUCCESS, status=200)
    client.authenticate()
    assert len(responses_lib.calls) == 1
    body = responses_lib.calls[0].request.body
    assert "appPasswordl" in body
    assert "appPassword=" not in body


def test_authenticate_password_truncated_to_25_chars():
    c = ACInfinityClient("test@example.com", "a" * 30)
    assert len(c.password) == 25
    assert c.password == "a" * 25


@responses_lib.activate
def test_authenticate_generic_exception_returns_false(client):
    """Bare except path in authenticate() must return False for unexpected errors."""
    with patch.object(client.session, "post", side_effect=RuntimeError("unexpected boom")):
        result = client.authenticate()
    assert result is False


# ============ Token refresh on 401 ============

@responses_lib.activate
def test_get_devices_refreshes_token_on_401(authed_client):
    """A 401 from get_devices must trigger one re-auth and a retry that succeeds."""
    responses_lib.add(
        responses_lib.POST, DEVICES_URL,
        json={"code": 401, "msg": "token expired"}, status=200,
    )
    responses_lib.add(
        responses_lib.POST, LOGIN_URL,
        json={"code": 200, "data": {"appId": "fresh_token_xyz"}}, status=200,
    )
    responses_lib.add(
        responses_lib.POST, DEVICES_URL, json=DEVICES_SUCCESS, status=200,
    )

    devices = authed_client.get_devices()
    assert len(devices) >= 1
    assert authed_client.token == "fresh_token_xyz"


@responses_lib.activate
def test_get_devices_second_401_after_refresh_raises(authed_client):
    """If the retry after refresh also returns 401, raise without further attempts."""
    responses_lib.add(
        responses_lib.POST, DEVICES_URL,
        json={"code": 401, "msg": "token expired"}, status=200,
    )
    responses_lib.add(
        responses_lib.POST, LOGIN_URL,
        json={"code": 200, "data": {"appId": "fresh_token"}}, status=200,
    )
    responses_lib.add(
        responses_lib.POST, DEVICES_URL,
        json={"code": 401, "msg": "still expired"}, status=200,
    )

    with pytest.raises(ACInfinityAuthError):
        authed_client.get_devices()


@responses_lib.activate
def test_get_devices_no_refresh_when_unauthenticated(client):
    """If client was never authenticated, AuthError raises without attempting refresh."""
    # client fixture has no token set
    with pytest.raises(ACInfinityAuthError):
        client.get_devices()
    # No login call should have been made
    login_calls = [c for c in responses_lib.calls if LOGIN_URL in c.request.url]
    assert len(login_calls) == 0


@responses_lib.activate
def test_get_devices_no_refresh_if_authenticate_fails(authed_client):
    """If re-authentication fails, the original AuthError propagates."""
    responses_lib.add(
        responses_lib.POST, DEVICES_URL,
        json={"code": 401, "msg": "token expired"}, status=200,
    )
    responses_lib.add(
        responses_lib.POST, LOGIN_URL,
        json=AUTH_FAILURE, status=200,
    )

    with pytest.raises(ACInfinityAuthError):
        authed_client.get_devices()


@responses_lib.activate
def test_get_historical_data_refreshes_token_on_401(authed_client):
    """get_historical_data must also refresh on 401."""
    responses_lib.add(
        responses_lib.POST, HISTORY_URL,
        json={"code": 401, "msg": "token expired"}, status=200,
    )
    responses_lib.add(
        responses_lib.POST, LOGIN_URL,
        json={"code": 200, "data": {"appId": "fresh_token"}}, status=200,
    )
    responses_lib.add(
        responses_lib.POST, HISTORY_URL, json=HISTORY_EMPTY, status=200,
    )

    result = authed_client.get_historical_data("12345", 1714000000, 1714086400)
    assert result == []


def test_call_with_token_refresh_serializes_concurrent_401s(authed_client):
    """Concurrent 401s must coalesce into a SINGLE re-authentication.

    Without coordination, N parallel tool calls hitting an expired token would
    each call authenticate(), wasting roundtrips and potentially triggering
    upstream rate limits. The _auth_lock + token_at_start snapshot in
    _call_with_token_refresh must ensure only one thread actually re-auths;
    the others observe the refreshed token and proceed.
    """
    import threading

    n_threads = 5
    # Barrier inside the inner call: all N threads must arrive at the 401 raise
    # before any of them can proceed to the refresh path. This proves every
    # thread captured token_at_start = OLD token (none could observe a refresh
    # mid-flight).
    inner_barrier = threading.Barrier(n_threads)
    thread_local = threading.local()
    auth_call_count = 0
    auth_count_lock = threading.Lock()

    def fake_authenticate() -> bool:
        nonlocal auth_call_count
        with auth_count_lock:
            auth_call_count += 1
        authed_client.token = f"fresh_token_{auth_call_count}"
        return True

    def fake_inner() -> list[dict]:
        attempt = getattr(thread_local, "attempt", 0)
        thread_local.attempt = attempt + 1
        if attempt == 0:
            # Synchronize: every thread must be inside the inner call with the
            # OLD token before ANY thread proceeds to refresh.
            inner_barrier.wait()
            raise ACInfinityAuthError("Token rejected by API (code 401): expired")
        return [{"devCode": "C58ZA"}]

    results: list = []
    errors: list = []
    start_gate = threading.Barrier(n_threads)

    def call() -> None:
        try:
            start_gate.wait()  # release all threads simultaneously
            result = authed_client.get_devices()
            results.append(result)
        except Exception as e:  # pragma: no cover — only fires on test failure
            errors.append(e)

    with patch.object(authed_client, "authenticate", side_effect=fake_authenticate):
        with patch.object(authed_client, "_get_devices_inner", side_effect=fake_inner):
            threads = [threading.Thread(target=call) for _ in range(n_threads)]
            for t in threads:
                t.start()
            # Bound the join — a deadlock-introducing regression in the auth_lock
                # path could hang the whole CI run otherwise. Real wall-clock here
            # is ~50ms; 10s gives generous slack on a loaded shared runner (P2-F013).
            for t in threads:
                t.join(timeout=10.0)
                assert not t.is_alive(), (
                    "Token-refresh thread did not complete within 10s — possible "
                    "deadlock in _call_with_token_refresh"
                )

    assert errors == []
    assert len(results) == n_threads
    # Critical: only ONE authenticate() call despite N threads hitting 401
    assert auth_call_count == 1, f"Expected 1 auth call, got {auth_call_count}"
    # All threads converged on the same refreshed token
    assert authed_client.token == "fresh_token_1"


@responses_lib.activate
def test_get_mode_settings_refreshes_token_on_401(authed_client):
    """get_mode_settings must also refresh on 401."""
    responses_lib.add(
        responses_lib.POST, MODE_SETTINGS_URL,
        json={"code": 401, "msg": "token expired"}, status=200,
    )
    responses_lib.add(
        responses_lib.POST, LOGIN_URL,
        json={"code": 200, "data": {"appId": "fresh_token"}}, status=200,
    )
    responses_lib.add(
        responses_lib.POST, MODE_SETTINGS_URL,
        json={"code": 200, "data": MOCK_MODE_SETTINGS_LEGACY_PORT1}, status=200,
    )

    result = authed_client.get_mode_settings(12345, 1)
    assert "modeType" in result


# ============ Historical data — non-401 API error coverage ============

@responses_lib.activate
def test_get_historical_data_500_raises_api_error(authed_client):
    """Non-401 API error must raise ACInfinityAPIError (not trigger refresh)."""
    responses_lib.add(
        responses_lib.POST, HISTORY_URL,
        json={"code": 500, "msg": "server error"}, status=200,
    )
    with pytest.raises(ACInfinityAPIError):
        authed_client.get_historical_data("12345", 1714000000, 1714086400)


# ============ Historical data — pagination edge ============

@responses_lib.activate
def test_get_historical_data_pagination_stops_when_cursor_no_advance(authed_client):
    """If returned records don't advance the time cursor, stop paginating (line 264)."""
    # Page 1: returns page_size records, but the last record's createTime equals current_start
    page1 = {
        "code": 200,
        "data": {
            "rows": [
                {"createTime": 1714000000, "temperature": 2400}
                for _ in range(3)
            ],
        },
    }
    responses_lib.add(responses_lib.POST, HISTORY_URL, json=page1, status=200)

    result = authed_client.get_historical_data(
        "12345", 1714000000, 1714086400, page_size=3,
    )
    # Should stop after first page because cursor can't advance past start
    assert len(result) == 3


# ============ get_devices ============

@responses_lib.activate
def test_get_devices_success(authed_client):
    responses_lib.add(responses_lib.POST, DEVICES_URL, json=DEVICES_SUCCESS, status=200)
    result = authed_client.get_devices()
    assert result is not None
    assert len(result) == 2


@responses_lib.activate
def test_get_devices_empty(authed_client):
    responses_lib.add(responses_lib.POST, DEVICES_URL, json=DEVICES_EMPTY, status=200)
    result = authed_client.get_devices()
    assert result == []


def test_get_devices_not_authenticated(client):
    with pytest.raises(ACInfinityAuthError):
        client.get_devices()


@responses_lib.activate
def test_get_devices_api_error_code(authed_client):
    responses_lib.add(responses_lib.POST, DEVICES_URL, json=DEVICES_API_ERROR, status=200)
    with pytest.raises(ACInfinityAPIError):
        authed_client.get_devices()


@responses_lib.activate
def test_get_devices_http_error(authed_client):
    responses_lib.add(responses_lib.POST, DEVICES_URL, status=503)
    with pytest.raises(requests.exceptions.HTTPError):
        authed_client.get_devices()


@responses_lib.activate
def test_get_devices_code_401_raises_auth_error(authed_client):
    responses_lib.add(
        responses_lib.POST,
        DEVICES_URL,
        json={"code": 401, "msg": "Unauthorized"},
        status=200,
    )
    with pytest.raises(ACInfinityAuthError, match="Token rejected"):
        authed_client.get_devices()


@responses_lib.activate
def test_get_devices_code_500_raises_api_error(authed_client):
    responses_lib.add(
        responses_lib.POST,
        DEVICES_URL,
        json={"code": 500, "msg": "Internal server error"},
        status=200,
    )
    with pytest.raises(ACInfinityAPIError, match="API error 500"):
        authed_client.get_devices()


def test_get_devices_auth_error_makes_no_http_call(client):
    """Token=None check happens before any HTTP call."""
    import responses as _r
    with _r.RequestsMock() as rsps:
        with pytest.raises(ACInfinityAuthError):
            client.get_devices()
        assert len(rsps.calls) == 0


# ============ get_historical_data ============

@responses_lib.activate
def test_get_historical_data_single_page(authed_client):
    responses_lib.add(responses_lib.POST, HISTORY_URL, json=HISTORY_PAGE_1, status=200)
    result = authed_client.get_historical_data(
        dev_id="12345",
        start_timestamp=1714000000,
        end_timestamp=1714086400,
        page_size=2000,
    )
    assert result is not None
    assert len(result) == 10


@responses_lib.activate
def test_get_historical_data_always_sends_pageNum_1(authed_client):
    """docs/API.md Quirk 3: pageNum is server-ignored; the client always sends 1.

    No prior test inspected the request body to confirm this — a regression
    to pageNum=2 would have failed in subtle ways at runtime but passed CI.
    P2-F005.
    """
    responses_lib.add(responses_lib.POST, HISTORY_URL, json=HISTORY_PAGE_1, status=200)
    authed_client.get_historical_data(
        dev_id="12345", start_timestamp=1714000000, end_timestamp=1714086400,
    )
    body = responses_lib.calls[0].request.body
    assert "pageNum=1" in body
    assert "pageNum=2" not in body


@responses_lib.activate
def test_get_historical_data_pagination(authed_client):
    base_ts = 1714000000
    page1 = {
        "code": 200,
        "data": {
            "rows": [
                {
                    "createTime": base_ts + i,
                    "temperature": 2400,
                    "fTemperature": 7520,
                    "humidity": 5500,
                    "vpdNums": 150,
                    "portSpead": 0,
                    "portStatus": 0,
                    "devPortCount": 2,
                }
                for i in range(3)
            ]
        },
    }
    page2 = {
        "code": 200,
        "data": {
            "rows": [
                {
                    "createTime": base_ts + 3 + i,
                    "temperature": 2400,
                    "fTemperature": 7520,
                    "humidity": 5500,
                    "vpdNums": 150,
                    "portSpead": 0,
                    "portStatus": 0,
                    "devPortCount": 2,
                }
                for i in range(2)
            ]
        },
    }
    responses_lib.add(responses_lib.POST, HISTORY_URL, json=page1, status=200)
    responses_lib.add(responses_lib.POST, HISTORY_URL, json=page2, status=200)

    result = authed_client.get_historical_data(
        dev_id="12345",
        start_timestamp=base_ts,
        end_timestamp=base_ts + 86400,
        page_size=3,
    )
    assert result is not None
    assert len(result) == 5
    assert len(responses_lib.calls) == 2


@responses_lib.activate
def test_get_historical_data_empty(authed_client):
    responses_lib.add(responses_lib.POST, HISTORY_URL, json=HISTORY_EMPTY, status=200)
    result = authed_client.get_historical_data(
        dev_id="12345", start_timestamp=1714000000, end_timestamp=1714086400
    )
    assert result == []


def test_get_historical_data_not_authenticated(client):
    with pytest.raises(ACInfinityAuthError):
        client.get_historical_data(
            dev_id="12345", start_timestamp=1714000000, end_timestamp=1714086400
        )


@responses_lib.activate
def test_get_historical_data_api_error_raises(authed_client):
    responses_lib.add(
        responses_lib.POST,
        HISTORY_URL,
        json={"code": 500, "msg": "Server fault"},
        status=200,
    )
    with pytest.raises(ACInfinityAPIError, match="API error 500"):
        authed_client.get_historical_data(
            dev_id="12345", start_timestamp=1714000000, end_timestamp=1714086400
        )


@responses_lib.activate
def test_get_historical_data_filters_out_of_range(authed_client):
    base_ts = 1714000000
    payload = {
        "code": 200,
        "data": {
            "rows": [
                # In range
                {"createTime": base_ts + 1, "temperature": 2400, "fTemperature": 7520,
                 "humidity": 5500, "vpdNums": 150, "portSpead": 0, "portStatus": 0,
                 "devPortCount": 2},
                # Out of range (before start)
                {"createTime": base_ts - 1, "temperature": 2400, "fTemperature": 7520,
                 "humidity": 5500, "vpdNums": 150, "portSpead": 0, "portStatus": 0,
                 "devPortCount": 2},
            ]
        },
    }
    responses_lib.add(responses_lib.POST, HISTORY_URL, json=payload, status=200)
    result = authed_client.get_historical_data(
        dev_id="12345",
        start_timestamp=base_ts,
        end_timestamp=base_ts + 86400,
    )
    assert result is not None
    assert len(result) == 1
    assert result[0]["createTime"] == base_ts + 1


# ============ get_mode_settings ============

MODE_SETTINGS_SUCCESS = {"code": 200, "msg": "success.", "data": MOCK_MODE_SETTINGS_LEGACY_PORT1}
MODE_SETTINGS_401 = {"code": 401, "msg": "Unauthorized"}
MODE_SETTINGS_999999 = {"code": 999999, "msg": "Operation failed, please try again"}


@responses_lib.activate
def test_get_mode_settings_happy_path(authed_client):
    responses_lib.add(responses_lib.POST, MODE_SETTINGS_URL, json=MODE_SETTINGS_SUCCESS, status=200)
    result = authed_client.get_mode_settings("12345", port=1)
    assert result["externalPort"] == 1
    assert result["onSpead"] == 5
    assert "modeSetid" in result


@responses_lib.activate
def test_get_mode_settings_returns_dict_not_list(authed_client):
    responses_lib.add(responses_lib.POST, MODE_SETTINGS_URL, json=MODE_SETTINGS_SUCCESS, status=200)
    result = authed_client.get_mode_settings("12345", port=1)
    assert isinstance(result, dict)


def test_get_mode_settings_no_token_raises_auth_error(client):
    with pytest.raises(ACInfinityAuthError):
        client.get_mode_settings("12345", port=1)


@responses_lib.activate
def test_get_mode_settings_401_raises_auth_error(authed_client):
    responses_lib.add(responses_lib.POST, MODE_SETTINGS_URL, json=MODE_SETTINGS_401, status=200)
    with pytest.raises(ACInfinityAuthError):
        authed_client.get_mode_settings("12345", port=1)


@responses_lib.activate
def test_get_mode_settings_999999_raises_api_error(authed_client):
    """Quirk 16: 999999 is returned when port parameter is missing or invalid."""
    responses_lib.add(responses_lib.POST, MODE_SETTINGS_URL, json=MODE_SETTINGS_999999, status=200)
    with pytest.raises(ACInfinityAPIError):
        authed_client.get_mode_settings("12345", port=99)


@responses_lib.activate
def test_get_mode_settings_timeout_propagates(authed_client):
    responses_lib.add(
        responses_lib.POST,
        MODE_SETTINGS_URL,
        body=requests.exceptions.Timeout(),
    )
    with pytest.raises(requests.exceptions.Timeout):
        authed_client.get_mode_settings("12345", port=1)


# ============ set_port_mode — dry_run=True ============

LEGACY_DEVICE_DATA = {
    "devId": "1424979258063367506",
    "devType": 11,
    "newFrameworkDevice": False,
}

AI_PLUS_DEVICE_DATA = {
    "devId": "1424979258063547818",
    "devType": 22,
    "newFrameworkDevice": True,
}


@responses_lib.activate
def test_set_port_mode_dry_run_legacy_returns_payload(authed_client):
    responses_lib.add(responses_lib.POST, MODE_SETTINGS_URL, json=MODE_SETTINGS_SUCCESS, status=200)
    result = authed_client.set_port_mode(
        LEGACY_DEVICE_DATA, port=1, updates={"onSpead": 5}, dry_run=True
    )
    assert result["dry_run"] is True
    assert result["sent"] is False
    assert result["controller_type"] == "legacy"
    assert "payload" in result


@responses_lib.activate
def test_set_port_mode_dry_run_does_not_call_write_endpoint(authed_client):
    responses_lib.add(responses_lib.POST, MODE_SETTINGS_URL, json=MODE_SETTINGS_SUCCESS, status=200)
    authed_client.set_port_mode(LEGACY_DEVICE_DATA, port=1, updates={"onSpead": 5}, dry_run=True)
    # Only one request (mode settings read), no write endpoint called
    assert len(responses_lib.calls) == 1
    assert "getdevModeSettingList" in responses_lib.calls[0].request.url


@responses_lib.activate
def test_set_port_mode_dry_run_rate_limit_not_called(authed_client):
    responses_lib.add(responses_lib.POST, MODE_SETTINGS_URL, json=MODE_SETTINGS_SUCCESS, status=200)
    with patch.object(authed_client, "_enforce_write_rate_limit") as mock_limit:
        authed_client.set_port_mode(LEGACY_DEVICE_DATA, port=1, updates={}, dry_run=True)
        mock_limit.assert_not_called()


@responses_lib.activate
def test_set_port_mode_dry_run_quirk_11_modeSetid_absent(authed_client):
    """Quirk 11: modeSetid must not appear in the write payload."""
    responses_lib.add(responses_lib.POST, MODE_SETTINGS_URL, json=MODE_SETTINGS_SUCCESS, status=200)
    result = authed_client.set_port_mode(LEGACY_DEVICE_DATA, port=1, updates={}, dry_run=True)
    assert "modeSetid" not in result["payload"]


@responses_lib.activate
def test_set_port_mode_dry_run_quirk_12_modeType_when_speed_nonzero(authed_client):
    """Quirk 12: modeType=2 must be set when onSpead > 0."""
    responses_lib.add(responses_lib.POST, MODE_SETTINGS_URL, json=MODE_SETTINGS_SUCCESS, status=200)
    result = authed_client.set_port_mode(
        LEGACY_DEVICE_DATA, port=1, updates={"onSpead": 5}, dry_run=True
    )
    assert result["payload"]["modeType"] == 2


@responses_lib.activate
def test_set_port_mode_dry_run_ai_plus(authed_client):
    # AI+ fixture captured with modeType=15 (smart automation); override to manual for this test.
    ai_plus_manual = {**MOCK_MODE_SETTINGS_AI_PLUS_PORT1, "modeType": 0}
    ai_plus_response = {"code": 200, "msg": "success.", "data": ai_plus_manual}
    responses_lib.add(responses_lib.POST, MODE_SETTINGS_URL, json=ai_plus_response, status=200)
    result = authed_client.set_port_mode(
        AI_PLUS_DEVICE_DATA, port=1, updates={"onSpead": 3}, dry_run=True
    )
    assert result["controller_type"] == "new_framework"
    assert result["sent"] is False
    assert result["payload"]["onSpead"] == 3


def test_set_port_mode_no_token_raises_auth_error(client):
    with pytest.raises(ACInfinityAuthError):
        client.set_port_mode(LEGACY_DEVICE_DATA, port=1, updates={}, dry_run=True)


def test_set_port_mode_missing_dev_id_raises_device_error(authed_client):
    with pytest.raises(ACInfinityDeviceError):
        authed_client.set_port_mode({}, port=1, updates={}, dry_run=True)


# ============ set_port_mode — dry_run=False (live write) ============

ADD_MODE_SUCCESS = {"code": 200, "msg": "success", "data": None}
ADD_MODE_403_RATE_LIMIT = {"code": 403, "msg": "Data saving failed. Please try again later."}
ADD_MODE_403_FIELD_ERROR = {"code": 403, "msg": "modeSetid is not allowed in payload."}
MODE_SETTINGS_SMART_AUTO = {
    "code": 200,
    "msg": "success.",
    "data": {**MOCK_MODE_SETTINGS_LEGACY_PORT1, "modeType": 15, "isOpenAutomation": 1},
    # isOpenAutomation: 1 explicit override — base fixture has 0 (non-automation port).
    # This fixture represents an ACTIVE automation (conflict must raise).
}
MODE_SETTINGS_SMART_AUTO_DISABLED = {
    "code": 200,
    "msg": "success.",
    "data": {**MOCK_MODE_SETTINGS_LEGACY_PORT1, "modeType": 15, "isOpenAutomation": 0},
    # isOpenAutomation: 0 = automation disabled; write guard must NOT fire.
}
MODE_SETTINGS_ON_OFF_PORT = {
    "code": 200,
    "msg": "success.",
    "data": {**MOCK_MODE_SETTINGS_LEGACY_PORT1, "modeType": 0, "loadType": 4},
}
MODE_SETTINGS_DIMMER_PORT = {
    "code": 200,
    "msg": "success.",
    "data": {**MOCK_MODE_SETTINGS_LEGACY_PORT1, "modeType": 0, "loadType": 128},
}


@responses_lib.activate
def test_set_port_mode_live_write_calls_rate_limit(authed_client):
    responses_lib.add(responses_lib.POST, MODE_SETTINGS_URL, json=MODE_SETTINGS_SUCCESS, status=200)
    responses_lib.add(responses_lib.POST, ADD_DEV_MODE_URL, json=ADD_MODE_SUCCESS, status=200)
    with patch.object(authed_client, "_enforce_write_rate_limit") as mock_limit:
        authed_client.set_port_mode(LEGACY_DEVICE_DATA, port=1, updates={}, dry_run=False)
        mock_limit.assert_called_once()


@responses_lib.activate
def test_set_port_mode_live_write_sent_true(authed_client):
    responses_lib.add(responses_lib.POST, MODE_SETTINGS_URL, json=MODE_SETTINGS_SUCCESS, status=200)
    responses_lib.add(responses_lib.POST, ADD_DEV_MODE_URL, json=ADD_MODE_SUCCESS, status=200)
    with patch.object(authed_client, "_enforce_write_rate_limit"):
        result = authed_client.set_port_mode(LEGACY_DEVICE_DATA, port=1, updates={}, dry_run=False)
    assert result["sent"] is True


@responses_lib.activate
def test_set_port_mode_live_write_non_rate_limit_403_raises_immediately(authed_client):
    """Non-rate-limit 403 (e.g. field validation error) must fail without retrying."""
    responses_lib.add(
        responses_lib.POST, MODE_SETTINGS_URL, json=MODE_SETTINGS_SUCCESS, status=200
    )
    responses_lib.add(
        responses_lib.POST, ADD_DEV_MODE_URL, json=ADD_MODE_403_FIELD_ERROR, status=200
    )
    with patch.object(authed_client, "_enforce_write_rate_limit"):
        with pytest.raises(ACInfinityAPIError):
            authed_client.set_port_mode(LEGACY_DEVICE_DATA, port=1, updates={}, dry_run=False)
    # Only one write attempt — no retry for non-rate-limit errors
    write_calls = [c for c in responses_lib.calls if "addDevMode" in c.request.url]
    assert len(write_calls) == 1


@responses_lib.activate
def test_set_port_mode_retries_on_403_rate_limit_then_succeeds(authed_client):
    """Rate-limit 403 ('Data saving failed') triggers retry; succeeds on second attempt."""
    responses_lib.add(
        responses_lib.POST, MODE_SETTINGS_URL, json=MODE_SETTINGS_SUCCESS, status=200
    )
    responses_lib.add(
        responses_lib.POST, ADD_DEV_MODE_URL, json=ADD_MODE_403_RATE_LIMIT, status=200
    )
    responses_lib.add(responses_lib.POST, ADD_DEV_MODE_URL, json=ADD_MODE_SUCCESS, status=200)
    with patch.object(authed_client, "_enforce_write_rate_limit"):
        with patch("ac_infinity_mcp.client.time.sleep"):
            result = authed_client.set_port_mode(
                LEGACY_DEVICE_DATA, port=1, updates={}, dry_run=False
            )
    assert result["sent"] is True
    write_calls = [c for c in responses_lib.calls if "addDevMode" in c.request.url]
    assert len(write_calls) == 2


@responses_lib.activate
def test_set_port_mode_exhausts_retries_and_raises(authed_client):
    """Exhausting all 3 retry attempts on rate-limit 403 raises ACInfinityAPIError."""
    responses_lib.add(
        responses_lib.POST, MODE_SETTINGS_URL, json=MODE_SETTINGS_SUCCESS, status=200
    )
    for _ in range(3):
        responses_lib.add(
            responses_lib.POST, ADD_DEV_MODE_URL, json=ADD_MODE_403_RATE_LIMIT, status=200
        )
    with patch.object(authed_client, "_enforce_write_rate_limit"):
        with patch("ac_infinity_mcp.client.time.sleep"):
            with pytest.raises(ACInfinityAPIError):
                authed_client.set_port_mode(
                    LEGACY_DEVICE_DATA, port=1, updates={}, dry_run=False
                )
    write_calls = [c for c in responses_lib.calls if "addDevMode" in c.request.url]
    assert len(write_calls) == 3


@responses_lib.activate
def test_set_port_mode_retries_on_connection_error_then_succeeds(authed_client, monkeypatch):
    """Transient ConnectionError on write POST is retried via tenacity (P1-F004).

    ConnectionError fires before the request reaches the server, so retry is
    safe. Timeout is intentionally excluded from retry — see client decorator.
    """
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda _: None)
    # Two MODE_SETTINGS responses because the retry re-runs the full inner.
    responses_lib.add(
        responses_lib.POST, MODE_SETTINGS_URL, json=MODE_SETTINGS_SUCCESS, status=200
    )
    responses_lib.add(
        responses_lib.POST, ADD_DEV_MODE_URL,
        body=requests.exceptions.ConnectionError("connection reset"),
    )
    responses_lib.add(
        responses_lib.POST, MODE_SETTINGS_URL, json=MODE_SETTINGS_SUCCESS, status=200
    )
    responses_lib.add(responses_lib.POST, ADD_DEV_MODE_URL, json=ADD_MODE_SUCCESS, status=200)
    with patch.object(authed_client, "_enforce_write_rate_limit"):
        result = authed_client.set_port_mode(LEGACY_DEVICE_DATA, port=1, updates={}, dry_run=False)
    assert result["sent"] is True
    write_calls = [c for c in responses_lib.calls if "addDevMode" in c.request.url]
    assert len(write_calls) == 2


@responses_lib.activate
def test_set_port_mode_does_not_retry_on_timeout(authed_client, monkeypatch):
    """Timeout is NOT retried for writes — server may have already processed it (P1-F004)."""
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda _: None)
    responses_lib.add(
        responses_lib.POST, MODE_SETTINGS_URL, json=MODE_SETTINGS_SUCCESS, status=200
    )
    responses_lib.add(
        responses_lib.POST, ADD_DEV_MODE_URL,
        body=requests.exceptions.Timeout("read timeout"),
    )
    with patch.object(authed_client, "_enforce_write_rate_limit"):
        with pytest.raises(requests.exceptions.Timeout):
            authed_client.set_port_mode(LEGACY_DEVICE_DATA, port=1, updates={}, dry_run=False)
    write_calls = [c for c in responses_lib.calls if "addDevMode" in c.request.url]
    assert len(write_calls) == 1


@responses_lib.activate
def test_set_port_mode_raises_on_modeType_15(authed_client):
    """modeType=15 with active automation raises ACInfinityAdvanceConflictError before any write."""
    responses_lib.add(
        responses_lib.POST, MODE_SETTINGS_URL, json=MODE_SETTINGS_SMART_AUTO, status=200
    )
    with pytest.raises(ACInfinityAdvanceConflictError) as exc_info:
        authed_client.set_port_mode(LEGACY_DEVICE_DATA, port=1, updates={}, dry_run=True)
    assert "smart automation" in str(exc_info.value).lower()
    assert "1" in str(exc_info.value)  # port number appears in message


@responses_lib.activate
def test_set_port_mode_modeType_15_no_write_attempted(authed_client):
    """Smart automation guard fires before any write endpoint is reached."""
    responses_lib.add(
        responses_lib.POST, MODE_SETTINGS_URL, json=MODE_SETTINGS_SMART_AUTO, status=200
    )
    with pytest.raises(ACInfinityAdvanceConflictError):
        authed_client.set_port_mode(LEGACY_DEVICE_DATA, port=1, updates={}, dry_run=False)
    write_calls = [c for c in responses_lib.calls if "addDevMode" in c.request.url]
    assert len(write_calls) == 0


@responses_lib.activate
def test_set_port_mode_modeType_15_disabled_automation_allows_dry_run(authed_client):
    """modeType=15 with isOpenAutomation=0 (disabled) does NOT raise; dry_run returns result."""
    responses_lib.add(
        responses_lib.POST,
        MODE_SETTINGS_URL,
        json=MODE_SETTINGS_SMART_AUTO_DISABLED,
        status=200,
    )
    result = authed_client.set_port_mode(LEGACY_DEVICE_DATA, port=1, updates={}, dry_run=True)
    assert result["dry_run"] is True
    assert result["sent"] is False
    assert "payload" in result


@responses_lib.activate
def test_set_port_mode_modeType_15_missing_isOpenAutomation_raises_conflict(authed_client):
    """modeType=15 with absent isOpenAutomation field defaults to 1 (safe-fail) → raises."""
    # Build a settings dict with modeType=15 and NO isOpenAutomation key at all.
    # The base fixture has isOpenAutomation=0, so we must remove it explicitly.
    settings_without_field = {k: v for k, v in MOCK_MODE_SETTINGS_LEGACY_PORT1.items()
                               if k != "isOpenAutomation"}
    settings_without_field["modeType"] = 15
    no_field_fixture = {
        "code": 200,
        "msg": "success.",
        "data": settings_without_field,
        # No isOpenAutomation key — safe-fail default of 1 triggers the guard.
    }
    responses_lib.add(
        responses_lib.POST, MODE_SETTINGS_URL, json=no_field_fixture, status=200
    )
    with pytest.raises(ACInfinityAdvanceConflictError):
        authed_client.set_port_mode(LEGACY_DEVICE_DATA, port=1, updates={}, dry_run=True)


@responses_lib.activate
def test_set_port_mode_modeType_15_disabled_live_write_calls_rate_limit(authed_client):
    """modeType=15 with isOpenAutomation=0 allows live write; rate-limit enforced."""
    responses_lib.add(
        responses_lib.POST,
        MODE_SETTINGS_URL,
        json=MODE_SETTINGS_SMART_AUTO_DISABLED,
        status=200,
    )
    responses_lib.add(responses_lib.POST, ADD_DEV_MODE_URL, json=ADD_MODE_SUCCESS, status=200)
    with patch.object(authed_client, "_enforce_write_rate_limit") as mock_limit:
        authed_client.set_port_mode(LEGACY_DEVICE_DATA, port=1, updates={}, dry_run=False)
        mock_limit.assert_called_once()


@responses_lib.activate
def test_set_port_mode_raises_on_load_type_4_when_variable_speed_required(authed_client):
    """require_variable_speed=True raises ACInfinityDeviceError for on/off hardware."""
    responses_lib.add(
        responses_lib.POST, MODE_SETTINGS_URL, json=MODE_SETTINGS_ON_OFF_PORT, status=200
    )
    with pytest.raises(ACInfinityDeviceError) as exc_info:
        authed_client.set_port_mode(
            LEGACY_DEVICE_DATA, port=1, updates={}, dry_run=True, require_variable_speed=True
        )
    assert "loadType=4" in str(exc_info.value)
    assert "set_port_on" in str(exc_info.value) or "set_port_off" in str(exc_info.value)


@responses_lib.activate
def test_set_port_mode_raises_on_load_type_128_when_variable_speed_required(authed_client):
    """require_variable_speed=True raises ACInfinityDeviceError for dimmer-type hardware."""
    responses_lib.add(
        responses_lib.POST, MODE_SETTINGS_URL, json=MODE_SETTINGS_DIMMER_PORT, status=200
    )
    with pytest.raises(ACInfinityDeviceError) as exc_info:
        authed_client.set_port_mode(
            LEGACY_DEVICE_DATA, port=1, updates={}, dry_run=True, require_variable_speed=True
        )
    assert "loadType=128" in str(exc_info.value)


@responses_lib.activate
def test_set_port_mode_does_not_raise_load_type_4_when_variable_speed_not_required(authed_client):
    """Without require_variable_speed, on/off ports are allowed (set_port_on/off use case)."""
    responses_lib.add(
        responses_lib.POST, MODE_SETTINGS_URL, json=MODE_SETTINGS_ON_OFF_PORT, status=200
    )
    # Should not raise — no require_variable_speed flag
    result = authed_client.set_port_mode(
        LEGACY_DEVICE_DATA, port=1, updates={"onSpead": 0}, dry_run=True
    )
    assert result["dry_run"] is True


@responses_lib.activate
def test_set_port_mode_ai_plus_dry_run_false_returns_unsupported(authed_client):
    """AI+ live write returns ai_plus_write_unsupported=True without calling addDevMode."""
    # AI+ fixture captured with modeType=15; override to manual so modeType guard doesn't fire.
    ai_plus_manual = {**MOCK_MODE_SETTINGS_AI_PLUS_PORT1, "modeType": 0}
    ai_plus_response = {"code": 200, "msg": "success.", "data": ai_plus_manual}
    responses_lib.add(responses_lib.POST, MODE_SETTINGS_URL, json=ai_plus_response, status=200)
    result = authed_client.set_port_mode(AI_PLUS_DEVICE_DATA, port=1, updates={}, dry_run=False)
    assert result.get("ai_plus_write_unsupported") is True
    assert result["sent"] is False
    write_calls = [c for c in responses_lib.calls if "addDevMode" in c.request.url]
    assert len(write_calls) == 0


# ============ Pre-write guard from device_data (Quirk 25 / Issue #133) ============

# Device fixture with isOpenAutomation=1 on port 1 — simulates legacy firmware (devType=11)
# where getdevModeSettingList may return unreliable modeType for ADVANCE-mode ports.
LEGACY_DEVICE_DATA_WITH_OPEN_AUTOMATION = {
    "devId": "1424979258063367506",
    "devType": 11,
    "newFrameworkDevice": False,
    "deviceInfo": {
        "ports": [
            {"port": 1, "portName": "Filter", "speak": 5, "isOpenAutomation": 1},
            {"port": 2, "portName": "Exhaust", "speak": 3, "isOpenAutomation": 0},
        ],
    },
}

# Device fixture with isOpenAutomation=0 — automation disabled; guard must NOT fire.
LEGACY_DEVICE_DATA_AUTOMATION_DISABLED = {
    "devId": "1424979258063367506",
    "devType": 11,
    "newFrameworkDevice": False,
    "deviceInfo": {
        "ports": [
            {"port": 1, "portName": "Filter", "speak": 5, "isOpenAutomation": 0},
        ],
    },
}

# Device fixture where port 1 has no isOpenAutomation key — guard must NOT fire (safe-fail=0).
LEGACY_DEVICE_DATA_NO_OPEN_AUTOMATION_KEY = {
    "devId": "1424979258063367506",
    "devType": 11,
    "newFrameworkDevice": False,
    "deviceInfo": {
        "ports": [
            {"port": 1, "portName": "Filter", "speak": 5},
        ],
    },
}

ADD_MODE_999999 = {"code": 999999, "msg": "Operation denied by active automation"}


@responses_lib.activate
def test_set_port_mode_pre_write_guard_fires_when_isOpenAutomation_1(authed_client):
    """Pre-write guard raises ACInfinityAdvanceConflictError when port isOpenAutomation=1.

    This catches the legacy firmware (devType=11) case where getdevModeSettingList returns
    unreliable modeType. The guard fires BEFORE get_mode_settings is called.
    """
    # No MODE_SETTINGS_URL response registered — if the guard fires correctly,
    # get_mode_settings is never called and no HTTP request is made to that endpoint.
    with pytest.raises(ACInfinityAdvanceConflictError) as exc_info:
        authed_client.set_port_mode(
            LEGACY_DEVICE_DATA_WITH_OPEN_AUTOMATION, port=1, updates={}, dry_run=True
        )
    assert "isOpenAutomation=1" in str(exc_info.value)
    # Confirm no HTTP call was made (get_mode_settings not reached)
    mode_calls = [c for c in responses_lib.calls if "getdevModeSettingList" in c.request.url]
    assert len(mode_calls) == 0


@responses_lib.activate
def test_set_port_mode_pre_write_guard_does_not_fire_when_isOpenAutomation_0(authed_client):
    """Pre-write guard does NOT fire when isOpenAutomation=0 — automation is disabled."""
    responses_lib.add(responses_lib.POST, MODE_SETTINGS_URL, json=MODE_SETTINGS_SUCCESS, status=200)
    # Should not raise — automation disabled; falls through to normal dry_run
    result = authed_client.set_port_mode(
        LEGACY_DEVICE_DATA_AUTOMATION_DISABLED, port=1, updates={}, dry_run=True
    )
    assert result["dry_run"] is True


@responses_lib.activate
def test_set_port_mode_pre_write_guard_absent_key_falls_through(authed_client):
    """Port with no isOpenAutomation key: pre-write guard safe-fail=0 → falls through."""
    responses_lib.add(responses_lib.POST, MODE_SETTINGS_URL, json=MODE_SETTINGS_SUCCESS, status=200)
    result = authed_client.set_port_mode(
        LEGACY_DEVICE_DATA_NO_OPEN_AUTOMATION_KEY, port=1, updates={}, dry_run=True
    )
    assert result["dry_run"] is True


@responses_lib.activate
def test_set_port_mode_pre_write_guard_port_2_not_affected_when_port_1_is_advance(authed_client):
    """Pre-write guard is port-specific: port 2 is not blocked when only port 1 is advance."""
    responses_lib.add(responses_lib.POST, MODE_SETTINGS_URL, json=MODE_SETTINGS_SUCCESS, status=200)
    # port 1 has isOpenAutomation=1 but we're writing to port 2 (isOpenAutomation=0)
    result = authed_client.set_port_mode(
        LEGACY_DEVICE_DATA_WITH_OPEN_AUTOMATION, port=2, updates={}, dry_run=True
    )
    assert result["dry_run"] is True


@responses_lib.activate
def test_set_port_mode_write_code_999999_raises_advance_conflict(authed_client):
    """Defense-in-depth: write response code 999999 raises ACInfinityAdvanceConflictError.

    This covers the case where the pre-write guard misses the conflict (e.g. no isOpenAutomation
    key in device data and legacy getdevModeSettingList returns modeType != 15) but the API
    rejects the write with code 999999 — the server should still return a structured conflict.
    """
    # No guard fires on dry_run=False: device_data has no ports list (no pre-write guard),
    # and MODE_SETTINGS returns modeType != 15 (no modeType guard either).
    settings_no_conflict = {"code": 200, "msg": "success.", "data": MOCK_MODE_SETTINGS_LEGACY_PORT1}
    responses_lib.add(responses_lib.POST, MODE_SETTINGS_URL, json=settings_no_conflict, status=200)
    responses_lib.add(responses_lib.POST, ADD_DEV_MODE_URL, json=ADD_MODE_999999, status=200)
    with patch.object(authed_client, "_enforce_write_rate_limit"):
        with pytest.raises(ACInfinityAdvanceConflictError) as exc_info:
            authed_client.set_port_mode(LEGACY_DEVICE_DATA, port=1, updates={}, dry_run=False)
    assert "999999" in str(exc_info.value)
