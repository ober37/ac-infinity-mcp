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
from tests.fixtures.advance_automation_fixtures import MOCK_ADVANCE_AUTOMATIONS_LIST
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
GET_GROUPS_URL = "https://www.acinfinityserver.com/api/version=2.0/dev/getGroups"


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


# ============ Password length (Quirk 2) ============


def test_password_below_limit_no_warning(caplog):
    """A genuinely sub-limit password (24 chars) triggers no warning — the
    truncation is a no-op so there's nothing the user needs to know about.
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="ac_infinity_mcp.client"):
        client = ACInfinityClient("user@example.com", "a" * 24)
    assert client.password == "a" * 24
    assert "exceeds the 25-character" not in caplog.text


def test_password_at_exactly_25_no_warning(caplog):
    """Lower boundary: exactly 25 chars is the limit, not over it — no warning.
    Guards against a `> 25` → `>= 25` regression.
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="ac_infinity_mcp.client"):
        client = ACInfinityClient("user@example.com", "x" * 25)
    assert client.password == "x" * 25
    assert "exceeds the 25-character" not in caplog.text


def test_password_just_over_limit_warns(caplog):
    """Upper boundary: 26 chars is one over the limit — warns and truncates.
    Guards against a `> 25` → `> 26` regression that would silently suppress
    the warning for passwords just past the limit.
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="ac_infinity_mcp.client"):
        client = ACInfinityClient("user@example.com", "a" * 26)
    assert client.password == "a" * 25
    assert "Password length 26 exceeds the 25-character" in caplog.text


def test_password_over_limit_warns(caplog):
    """Passwords well over 25 chars are still truncated (preserves Quirk 2
    parity with the AC Infinity API's own server-side behavior) but the user
    now gets a warning in the log so silent auth failures are diagnosable.
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="ac_infinity_mcp.client"):
        client = ACInfinityClient("user@example.com", "a" * 30)
    # Truncation behavior is unchanged
    assert client.password == "a" * 25
    # And the user is told about it
    assert "Password length 30 exceeds the 25-character" in caplog.text


def test_password_empty_string_no_warning(caplog):
    """Empty-string password (#263): len("") > 25 is False, so no warning fires and
    ""[:25] is "". The client constructs without raising; auth fails later at the
    first API call (with the 25-char note), not at construction time.
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="ac_infinity_mcp.client"):
        client = ACInfinityClient("user@example.com", "")
    assert client.password == ""
    assert "exceeds the 25-character" not in caplog.text


def test_password_unicode_at_25_codepoints_no_warning(caplog):
    """Unicode at exactly 25 code points (#263): len() counts code points, not UTF-8
    bytes, so 25 multibyte chars (here 50 bytes) is len()==25 — not over the limit.
    No warning, no truncation. Using a multibyte char proves code-point semantics:
    an ASCII×25 case would pass even under a hypothetical byte-count implementation.
    """
    import logging

    pw = "é" * 25  # 25 code points, 50 UTF-8 bytes
    assert len(pw) == 25
    with caplog.at_level(logging.WARNING, logger="ac_infinity_mcp.client"):
        client = ACInfinityClient("user@example.com", pw)
    assert client.password == pw  # truncation is a no-op
    assert "exceeds the 25-character" not in caplog.text


def test_password_unicode_over_25_codepoints_truncates_by_codepoint(caplog):
    """Mirror case (#263): 26 multibyte code points is over the limit — warns and
    truncates to 25 *code points* (password[:25]), not 25 bytes. Locks code-point
    slicing semantics from the over-limit side; guards against a regression to a
    byte-based length check or slice.
    """
    import logging

    pw = "é" * 26  # 26 code points, 52 UTF-8 bytes
    with caplog.at_level(logging.WARNING, logger="ac_infinity_mcp.client"):
        client = ACInfinityClient("user@example.com", pw)
    assert client.password == "é" * 25  # sliced by code point, not byte
    assert "Password length 26 exceeds the 25-character" in caplog.text


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
                {"accessPort": 1, "sensorType": 11, "sensorData": 850},
            ],
        },
    }
    result = client.parse_device_data(device)
    assert len(result["external_sensors"]) == 1
    assert result["external_sensors"][0]["sensor_id"] == "1.11"
    # No sensorPrecision → defaults to 1 → raw passthrough (850 ppm CO2).
    assert result["external_sensors"][0]["value"] == 850


# ============ _sensor_value — precision scaling (Quirk 28) ============


@pytest.mark.parametrize(
    "precision,data,expected",
    [
        (None, 793, 793),   # absent → passthrough
        (0, 793, 793),      # zero   → passthrough
        (1, 793, 793),      # 1      → passthrough
        (2, 65, 6.5),       # 2      → data / 10
        (3, 2450, 24.5),    # 3      → data / 100 (temperature sensor)
    ],
)
def test_sensor_value(precision, data, expected):
    """Direct coverage of every precision branch, including the precision=3
    (data/100) tier documented in Quirk 28 but otherwise untested.
    """
    from ac_infinity_mcp.client import _sensor_value

    s: dict = {"sensorData": data}
    if precision is not None:
        s["sensorPrecision"] = precision
    result = _sensor_value(s)
    assert result == pytest.approx(expected)
    # precision <= 1 must stay int — no spurious float on raw passthrough
    if precision in (None, 0, 1):
        assert isinstance(result, int)


def test_sensor_value_implausible_precision_passthrough(caplog):
    """A malformed, implausibly large sensorPrecision is logged and treated as
    raw passthrough rather than yielding a silent near-zero reading.
    """
    import logging

    from ac_infinity_mcp.client import _sensor_value

    with caplog.at_level(logging.WARNING, logger="ac_infinity_mcp.client"):
        result = _sensor_value({"sensorData": 793, "sensorType": 11, "sensorPrecision": 99})
    assert result == 793
    assert "Implausible sensorPrecision 99" in caplog.text


# ============ parse_device_data — phantom sensor filtering ============


def _sensor_entry(sensor_type, sensor_data=0, precision=1, access_port=1):
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
    """sensorType=99, sensorData=9900 → included with label 'Unrecognized (type 99)'."""
    device = _device_with_sensor_list([_sensor_entry(sensor_type=99, sensor_data=9900)])
    result = client.parse_device_data(device)
    assert len(result["external_sensors"]) == 1
    assert result["external_sensors"][0]["sensor_type_label"] == "Unrecognized (type 99)"
    # Unrecognized types have no known unit.
    assert result["external_sensors"][0]["unit"] == ""
    # precision defaults to 1 → raw passthrough.
    assert result["external_sensors"][0]["value"] == 9900


def test_parse_device_data_recognized_zero_included(client):
    """sensorType=11 (CO2), sensorData=0 → always included even at zero."""
    device = _device_with_sensor_list([_sensor_entry(sensor_type=11, sensor_data=0)])
    result = client.parse_device_data(device)
    assert len(result["external_sensors"]) == 1
    assert result["external_sensors"][0]["sensor_type"] == 11
    assert result["external_sensors"][0]["sensor_type_label"] == "CO2"
    assert result["external_sensors"][0]["unit"] == "ppm"


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
        _sensor_entry(sensor_type=11, sensor_data=450, precision=1, access_port=2),  # included
        _sensor_entry(sensor_type=None, sensor_data=500, access_port=3),  # no type — excluded
        _sensor_entry(sensor_type=21, sensor_data=855, precision=2, access_port=4),  # included
    ]
    device = _device_with_sensor_list(sensors)
    result = client.parse_device_data(device)
    assert len(result["external_sensors"]) == 2
    labels = {s["sensor_type"]: s["sensor_type_label"] for s in result["external_sensors"]}
    assert labels[11] == "CO2"
    assert labels[21] == "Unrecognized (type 21)"
    values = {s["sensor_type"]: s["value"] for s in result["external_sensors"]}
    assert values[11] == 450  # precision 1 → raw passthrough (450 ppm)
    assert values[21] == pytest.approx(85.5)  # precision 2 → 855 / 10


# ============ sensor label + unit table (#255, #264) ============

# (sensor_type, expected label, expected unit). Hardcoded literals on purpose — deriving
# these (e.g. via .title()) would mangle "CO2" → "Co2" and "pH" → "Ph".
_SENSOR_TYPE_TABLE = [
    (10, "Soil Moisture", "%"),
    (11, "CO2", "ppm"),
    (12, "Light", "%"),
    (13, "pH", ""),
    (14, "EC", "µS/cm"),
    (15, "EC", "mS/cm"),
    (16, "TDS", "ppm"),
    (17, "TDS", "ppt"),
    (18, "Water Temp", "°F"),
    (19, "Water Temp", "°C"),
    (20, "Water Level", ""),
]


@pytest.mark.parametrize("sensor_type,label,unit", _SENSOR_TYPE_TABLE)
def test_sensor_label_and_unit_table(client, sensor_type, label, unit):
    """Every known sensor type maps to its grower-readable label and unit."""
    device = _device_with_sensor_list(
        [_sensor_entry(sensor_type=sensor_type, sensor_data=100)]
    )
    sensor = client.parse_device_data(device)["external_sensors"][0]
    assert sensor["sensor_type_label"] == label
    assert sensor["unit"] == unit


def test_sensor_water_temp_polarity(client):
    """Water temp: type 18 = °F, type 19 = °C.

    NOTE: polarity is unverified against live hydro hardware. It follows the HA
    ``ac_infinity`` const.py convention (even sensorType = °F, odd = °C, across all
    its temperature types) and the API's own waterTempHighValueF/waterTempHighValue
    field naming. This corrects the previously-inverted 18=°C/19=°F mapping. A
    contributor with a hydro probe should re-confirm this one assertion.
    """
    for sensor_type, expected_unit in ((18, "°F"), (19, "°C")):
        device = _device_with_sensor_list(
            [_sensor_entry(sensor_type=sensor_type, sensor_data=680, precision=2)]
        )
        sensor = client.parse_device_data(device)["external_sensors"][0]
        assert sensor["sensor_type_label"] == "Water Temp"
        assert sensor["unit"] == expected_unit


def test_sensor_unit_unknown_type_is_empty(client):
    """An included-but-unrecognized sensor type carries no unit."""
    device = _device_with_sensor_list(
        [_sensor_entry(sensor_type=77, sensor_data=1234)]
    )
    sensor = client.parse_device_data(device)["external_sensors"][0]
    assert sensor["sensor_type_label"] == "Unrecognized (type 77)"
    assert sensor["unit"] == ""


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
    """Any sensorType < 10 not in _SENSOR_TYPE_INFO → excluded regardless of sensorData."""
    for st in range(1, 10):
        entry = {"sensorType": st, "sensorData": 9999, "sensorPrecision": 1, "accessPort": 1}
        device = _device_with_sensor_list([entry])
        result = client.parse_device_data(device)
        assert result["external_sensors"] == [], f"sensorType={st} should be excluded"


def test_should_include_sensor_recognized_type_zero_still_included(client):
    """sensorType=10 (soil_moisture), sensorData=0 → always included even at zero."""
    entry = {"sensorType": 10, "sensorData": 0, "sensorPrecision": 1, "accessPort": 1}
    device = _device_with_sensor_list([entry])
    result = client.parse_device_data(device)
    assert len(result["external_sensors"]) == 1
    assert result["external_sensors"][0]["sensor_type_label"] == "Soil Moisture"
    assert result["external_sensors"][0]["unit"] == "%"


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


@responses_lib.activate
def test_delByid_isflag_scope_whole_program_vs_single_rule(authed_client):
    """delByid isflag selects scope (verified live): whole_program=True -> isflag=1
    (delete the entire program slot); whole_program=False -> isflag=0 (delete ONLY this
    rule). Regression guard for the bug where delete_automation_rule nuked whole programs."""
    url = "https://www.acinfinityserver.com/api/version=2.0/dev/delByid"
    responses_lib.add(responses_lib.POST, url, json={"code": 200, "msg": "success."}, status=200)
    authed_client.delete_advance_automation("12345", 99)  # default: whole program
    assert "isflag=1" in responses_lib.calls[-1].request.body
    authed_client.delete_advance_automation("12345", 99, whole_program=False)  # single rule
    assert "isflag=0" in responses_lib.calls[-1].request.body


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


# ============ Lazy auth ============

@responses_lib.activate
def test_lazy_auth_coalesces_concurrent_first_calls(monkeypatch):
    """N concurrent callers with token=None trigger exactly 1 login attempt."""
    import threading
    call_count = 0
    original_inner = ACInfinityClient._authenticate_inner

    def counting_inner(self):
        nonlocal call_count
        call_count += 1
        original_inner(self)

    monkeypatch.setattr(ACInfinityClient, "_authenticate_inner", counting_inner)
    responses_lib.add(responses_lib.POST, LOGIN_URL, json=AUTH_SUCCESS, status=200)
    responses_lib.add(
        responses_lib.POST, DEVICES_URL, json=DEVICES_SUCCESS, status=200, match_querystring=False
    )
    responses_lib.add(
        responses_lib.POST, DEVICES_URL, json=DEVICES_SUCCESS, status=200, match_querystring=False
    )
    responses_lib.add(
        responses_lib.POST, DEVICES_URL, json=DEVICES_SUCCESS, status=200, match_querystring=False
    )
    responses_lib.add(
        responses_lib.POST, DEVICES_URL, json=DEVICES_SUCCESS, status=200, match_querystring=False
    )
    responses_lib.add(
        responses_lib.POST, DEVICES_URL, json=DEVICES_SUCCESS, status=200, match_querystring=False
    )

    c = ACInfinityClient("test@example.com", "password123")
    barrier = threading.Barrier(5)
    errors = []

    def call_get_devices():
        try:
            barrier.wait()
            c.get_devices()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=call_get_devices) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Unexpected errors: {errors}"
    assert call_count == 1, f"Expected 1 login call, got {call_count}"


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
def test_get_devices_lazy_auth_fires_on_first_call(client):
    """First call with no token triggers a login attempt (lazy auth preamble)."""
    responses_lib.add(responses_lib.POST, LOGIN_URL, json=AUTH_FAILURE, status=200)
    with pytest.raises(ACInfinityAuthError):
        client.get_devices()
    login_calls = [c for c in responses_lib.calls if LOGIN_URL in c.request.url]
    assert len(login_calls) == 1


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


# ============ #252 — session-expiry (code 10003) re-auth, read/write asymmetric ============


@pytest.mark.parametrize(
    "code,msg,session_refreshable,expected",
    [
        (401, "msg", True, ACInfinityAuthError),  # 401 is always auth (read path)
        (401, "msg", False, ACInfinityAuthError),  # 401 is always auth (write path too)
        (10003, "session expired", True, ACInfinityAuthError),  # session-expiry READ → refreshable
        (10003, "session expired", False, ACInfinityAPIError),  # session-expiry WRITE → no replay
        (500, "msg", True, ACInfinityAPIError),  # unrelated codes stay API errors
        (500, "msg", False, ACInfinityAPIError),
        # #298 — 403 + "login expired" message is a session-expiry on the READ path only.
        (403, "Login Expired Please login again!", True, ACInfinityAuthError),  # read → refreshable
        (403, "Login Expired Please login again!", False, ACInfinityAPIError),  # write → API error
        (403, "Data saving failed. Please try again later.", True, ACInfinityAPIError),  # no marker
        (403, "modeSetid is not allowed in payload.", True, ACInfinityAPIError),  # no marker
        (403, None, True, ACInfinityAPIError),  # null msg → no crash, no match
        (403, "", True, ACInfinityAPIError),  # empty msg → no match
        (500, "login expired", True, ACInfinityAPIError),  # marker under non-403 → NOT refreshed
    ],
)
def test_raise_for_api_code_session_mapping(client, code, msg, session_refreshable, expected):
    """Per-direction code mapping: 10003 is an auth error only on reads (so the token
    refresh fires); on writes it stays an API error so the write is never replayed. 401
    remains an auth error on every path. A 403 is a session-expiry ONLY on a read AND only
    when the message carries a login-expired marker (#298) — gated on the 403 code, null-safe.
    Covers the mapping for all call sites, which all delegate here."""
    with pytest.raises(expected):
        client._raise_for_api_code(code, msg, "Ctx", session_refreshable=session_refreshable)


@responses_lib.activate
def test_get_devices_refreshes_token_on_10003(authed_client):
    """A READ that returns session-expiry code 10003 transparently re-auths and retries."""
    responses_lib.add(
        responses_lib.POST, DEVICES_URL, json={"code": 10003, "msg": "session expired"}, status=200
    )
    responses_lib.add(
        responses_lib.POST,
        LOGIN_URL,
        json={"code": 200, "data": {"appId": "fresh_token_10003"}},
        status=200,
    )
    responses_lib.add(responses_lib.POST, DEVICES_URL, json=DEVICES_SUCCESS, status=200)

    devices = authed_client.get_devices()
    assert len(devices) >= 1
    assert authed_client.token == "fresh_token_10003"
    login_calls = [c for c in responses_lib.calls if "appUserLogin" in c.request.url]
    assert len(login_calls) == 1  # exactly one refresh


@responses_lib.activate
def test_get_devices_10003_refresh_failure_caches_and_short_circuits(authed_client):
    """When the 10003-triggered refresh login fails, the failure is cached: the call
    raises AuthError and a SUBSEQUENT call short-circuits without re-hitting login
    (bounds re-auth to one attempt — no credential-stuffing / lockout shape)."""
    responses_lib.add(
        responses_lib.POST, DEVICES_URL, json={"code": 10003, "msg": "session expired"}, status=200
    )
    responses_lib.add(responses_lib.POST, LOGIN_URL, json=AUTH_FAILURE, status=200)

    with pytest.raises(ACInfinityAuthError):
        authed_client.get_devices()
    # Second call: must NOT attempt another login (cached failure short-circuits).
    with pytest.raises(ACInfinityAuthError):
        authed_client.get_devices()
    login_calls = [c for c in responses_lib.calls if "appUserLogin" in c.request.url]
    assert len(login_calls) == 1  # only the first refresh attempted a login


# ============ #298 — v2 header omission + 403 "Login Expired" read refresh ============


def test_v2_headers_omit_version_and_request_id(authed_client):
    """#298 — the server rejects any v2 request carrying `version` or `requestId`.
    Both must be absent from _v2_headers(); token/Host/User-Agent stay present."""
    headers = authed_client._v2_headers()
    assert "version" not in headers
    assert "requestId" not in headers
    assert headers["token"] == authed_client.token
    assert headers["Host"] == "www.acinfinityserver.com"
    assert headers["User-Agent"] == "okhttp/3.10.0"


@responses_lib.activate
def test_get_advance_automations_request_omits_version_and_request_id(authed_client):
    """Wire-level guard: the outgoing getGroups request must not carry version/requestId."""
    responses_lib.add(
        responses_lib.POST, GET_GROUPS_URL,
        json={"code": 200, "msg": "success.", "data": MOCK_ADVANCE_AUTOMATIONS_LIST},
        status=200,
    )
    authed_client.get_advance_automations("12345")
    sent = next(c for c in responses_lib.calls if "getGroups" in c.request.url)
    assert "version" not in sent.request.headers
    assert "requestId" not in sent.request.headers


@responses_lib.activate
def test_get_advance_automations_refreshes_token_on_403_login_expired(authed_client):
    """#298 — a v2 READ that returns HTTP-200 body {code:403, "Login Expired..."} transparently
    re-auths and retries. The fixture uses HTTP status=200 (the real vendor shape): getGroups
    calls raise_for_status() before inspecting the body code, so a status=403 fixture would
    raise HTTPError and never reach the marker classification."""
    responses_lib.add(
        responses_lib.POST, GET_GROUPS_URL,
        json={"code": 403, "msg": "Login Expired Please login again!"}, status=200,
    )
    responses_lib.add(
        responses_lib.POST, LOGIN_URL,
        json={"code": 200, "data": {"appId": "fresh_token_403"}}, status=200,
    )
    responses_lib.add(
        responses_lib.POST, GET_GROUPS_URL,
        json={"code": 200, "msg": "success.", "data": MOCK_ADVANCE_AUTOMATIONS_LIST},
        status=200,
    )

    result = authed_client.get_advance_automations("12345")
    assert len(result) == len(MOCK_ADVANCE_AUTOMATIONS_LIST)
    assert authed_client.token == "fresh_token_403"
    login_calls = [c for c in responses_lib.calls if "appUserLogin" in c.request.url]
    assert len(login_calls) == 1  # exactly one refresh


def test_refresh_network_failure_is_not_cached(authed_client, monkeypatch):
    """A transient network failure during the refresh login must NOT be cached as a
    permanent auth error — otherwise a momentary outage would pin a false lockout until
    process restart. The original auth rejection still surfaces (consistent error type),
    but only genuine credential failures are cached."""
    monkeypatch.setattr(
        authed_client,
        "_authenticate_inner",
        lambda: (_ for _ in ()).throw(requests.exceptions.ConnectionError("boom")),
    )

    def fake_devices_inner():
        raise ACInfinityAuthError("Token rejected by API (code 10003): session expired")

    with pytest.raises(ACInfinityAuthError):
        authed_client._call_with_token_refresh(fake_devices_inner)
    assert authed_client._auth_error is None  # network blip not cached
    assert authed_client.token is not None  # stale token NOT cleared on a transient failure


@responses_lib.activate
def test_set_port_mode_write_10003_not_replayed(authed_client):
    """#252 double-apply guard: a session-expiry code on a WRITE surfaces as an API error
    after exactly ONE write POST, with NO token refresh — the write is never replayed (a
    10003 in a 200 body proves the server received the write)."""
    responses_lib.add(responses_lib.POST, MODE_SETTINGS_URL, json=MODE_SETTINGS_SUCCESS, status=200)
    responses_lib.add(
        responses_lib.POST,
        ADD_DEV_MODE_URL,
        json={"code": 10003, "msg": "session expired"},
        status=200,
    )
    with patch.object(authed_client, "_enforce_write_rate_limit"):
        with pytest.raises(ACInfinityAPIError):
            authed_client.set_port_mode(LEGACY_DEVICE_DATA, port=1, updates={}, dry_run=False)
    write_calls = [c for c in responses_lib.calls if "addDevMode" in c.request.url]
    login_calls = [c for c in responses_lib.calls if "appUserLogin" in c.request.url]
    assert len(write_calls) == 1  # no replay
    assert len(login_calls) == 0  # no refresh on a write


@pytest.mark.parametrize(
    "method,url_fragment,call",
    [
        (
            "enable_advance_automation",
            "updateGroupsIsOn",
            lambda c: c.enable_advance_automation("12345", 99),
        ),
        (
            "disable_advance_automation",
            "updateGroupsIsOn",
            lambda c: c.disable_advance_automation("12345", 99),
        ),
        (
            "create_advance_automation",
            "addGroups",
            lambda c: c.create_advance_automation("12345", {}),
        ),
        (
            "delete_advance_automation",
            "delByid",
            lambda c: c.delete_advance_automation("12345", 99),
        ),
        (
            "update_advance_automation",
            "updateGroupsById",
            lambda c: c.update_advance_automation("12345", {"advId": 99}),
        ),
    ],
)
@responses_lib.activate
def test_v2_write_10003_not_replayed(authed_client, method, url_fragment, call):
    """Every v2 automation WRITE site passes session_refreshable=False: a 10003 surfaces
    as an API error with no replay and no token refresh."""
    url = f"https://www.acinfinityserver.com/api/version=2.0/dev/{url_fragment}"
    responses_lib.add(
        responses_lib.POST, url, json={"code": 10003, "msg": "session expired"}, status=200
    )
    with patch.object(authed_client, "_enforce_write_rate_limit"):
        with pytest.raises(ACInfinityAPIError):
            call(authed_client)
    write_calls = [c for c in responses_lib.calls if url_fragment in c.request.url]
    login_calls = [c for c in responses_lib.calls if "appUserLogin" in c.request.url]
    assert len(write_calls) == 1, f"{method} replayed the write"
    assert len(login_calls) == 0, f"{method} refreshed token on a write"


# ============ #284 — update_advance_automation client method ============

UPDATE_GROUPS_BY_ID_URL = "https://www.acinfinityserver.com/api/version=2.0/dev/updateGroupsById"


@responses_lib.activate
def test_update_advance_automation_success(authed_client):
    """A 200 from updateGroupsById returns the data dict and injects devId into the body."""
    responses_lib.add(
        responses_lib.POST, UPDATE_GROUPS_BY_ID_URL,
        json={"code": 200, "data": {"advId": 99}}, status=200,
    )
    with patch.object(authed_client, "_enforce_write_rate_limit"):
        result = authed_client.update_advance_automation("12345", {"advId": 99})
    assert result == {"advId": 99}
    sent = [c for c in responses_lib.calls if "updateGroupsById" in c.request.url]
    assert len(sent) == 1
    assert "devId=12345" in sent[0].request.body


@responses_lib.activate
def test_update_advance_automation_retries_on_connection_error(authed_client, monkeypatch):
    """ConnectionError fires before the server sees the write — safe to retry (P1-F004)."""
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda _: None)
    responses_lib.add(
        responses_lib.POST, UPDATE_GROUPS_BY_ID_URL,
        body=requests.exceptions.ConnectionError("reset"),
    )
    responses_lib.add(
        responses_lib.POST, UPDATE_GROUPS_BY_ID_URL,
        json={"code": 200, "data": {"advId": 99}}, status=200,
    )
    with patch.object(authed_client, "_enforce_write_rate_limit"):
        result = authed_client.update_advance_automation("12345", {"advId": 99})
    assert result == {"advId": 99}
    sent = [c for c in responses_lib.calls if "updateGroupsById" in c.request.url]
    assert len(sent) == 2


@responses_lib.activate
def test_update_advance_automation_does_not_retry_on_timeout(authed_client, monkeypatch):
    """Timeout is NOT retried — the server may have already applied the edit (double-apply)."""
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda _: None)
    responses_lib.add(
        responses_lib.POST, UPDATE_GROUPS_BY_ID_URL,
        body=requests.exceptions.Timeout("read timeout"),
    )
    with patch.object(authed_client, "_enforce_write_rate_limit"):
        with pytest.raises(requests.exceptions.Timeout):
            authed_client.update_advance_automation("12345", {"advId": 99})
    sent = [c for c in responses_lib.calls if "updateGroupsById" in c.request.url]
    assert len(sent) == 1


# ============ #251 — app User-Agent header (not the default python-requests UA) ============


@responses_lib.activate
def test_user_agent_header_per_endpoint(client):
    """The client sends an AC-app User-Agent on every endpoint, never the default
    python-requests UA (which CloudFront may fingerprint-block on server ASNs)."""
    responses_lib.add(
        responses_lib.POST, LOGIN_URL, json={"code": 200, "data": {"appId": "tok"}}, status=200
    )
    responses_lib.add(responses_lib.POST, DEVICES_URL, json=DEVICES_SUCCESS, status=200)
    client.authenticate()
    client.get_devices()
    ua_by_url = {}
    for c in responses_lib.calls:
        ua_by_url.setdefault(c.request.url, c.request.headers.get("User-Agent", ""))
    login_ua = next(ua for url, ua in ua_by_url.items() if "appUserLogin" in url)
    devices_ua = next(ua for url, ua in ua_by_url.items() if "devInfoListAll" in url)
    assert login_ua == "ACController/1.8.2 (com.acinfinity.humiture; build:489; iOS 16.5.1)"
    assert devices_ua == "okhttp/3.10.0"
    assert all("python-requests" not in ua for ua in (login_ua, devices_ua))


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

    def fake_authenticate_inner() -> None:
        # The refresh path calls _authenticate_inner() (mirrors the lazy-auth preamble),
        # so patch that rather than the public authenticate() wrapper.
        nonlocal auth_call_count
        with auth_count_lock:
            auth_call_count += 1
        authed_client.token = f"fresh_token_{auth_call_count}"

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

    with patch.object(authed_client, "_authenticate_inner", side_effect=fake_authenticate_inner):
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


@responses_lib.activate
def test_get_devices_not_authenticated(client):
    responses_lib.add(responses_lib.POST, LOGIN_URL, json=AUTH_FAILURE, status=200)
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


@responses_lib.activate
def test_get_devices_auth_error_cached_after_first_failure(client):
    """After the first auth failure, subsequent calls raise immediately — no second login."""
    responses_lib.add(responses_lib.POST, LOGIN_URL, json=AUTH_FAILURE, status=200)
    # First call triggers preamble — 1 login attempt
    with pytest.raises(ACInfinityAuthError):
        client.get_devices()
    # Second call uses cached _auth_error — no additional login call
    with pytest.raises(ACInfinityAuthError):
        client.get_devices()
    login_calls = [c for c in responses_lib.calls if LOGIN_URL in c.request.url]
    assert len(login_calls) == 1


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
def test_get_historical_data_pagination_three_plus_chunks(authed_client):
    """#248: multi-day history assembles across MANY chunks via the time cursor (the API
    caps a page at ~96 rows). The mock slices a single canonical dataset by the request's
    'time' cursor, so the result is correct ONLY if the cursor advances correctly — a
    no-advance or off-by-one cursor produces duplicates or dropped rows and fails this."""
    import json
    from urllib.parse import parse_qs

    base_ts = 1714000000
    page_size = 96
    total = 338  # > 96, spans 4 pages of 96/96/96/50
    dataset = [
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
        for i in range(total)
    ]

    def cursor_callback(request):
        # The client sends the cursor as the form field 'time'; return the next
        # page_size rows whose createTime >= cursor (what the real endpoint does).
        cursor = int(parse_qs(request.body)["time"][0])
        rows = [r for r in dataset if r["createTime"] >= cursor][:page_size]
        return (200, {}, json.dumps({"code": 200, "data": {"rows": rows}}))

    responses_lib.add_callback(
        responses_lib.POST, HISTORY_URL, callback=cursor_callback, content_type="application/json"
    )

    result = authed_client.get_historical_data(
        dev_id="12345",
        start_timestamp=base_ts,
        end_timestamp=base_ts + 86400,
        page_size=page_size,
    )
    create_times = [r["createTime"] for r in result]
    assert len(result) == total  # every row assembled, nothing dropped
    assert len(set(create_times)) == total  # no duplicates at chunk boundaries
    assert create_times == sorted(create_times)  # cursor advanced monotonically
    assert len(responses_lib.calls) == 4  # 96 + 96 + 96 + 50, then the short page stops it


@responses_lib.activate
def test_get_historical_data_empty(authed_client):
    responses_lib.add(responses_lib.POST, HISTORY_URL, json=HISTORY_EMPTY, status=200)
    result = authed_client.get_historical_data(
        dev_id="12345", start_timestamp=1714000000, end_timestamp=1714086400
    )
    assert result == []


@responses_lib.activate
def test_get_historical_data_not_authenticated(client):
    responses_lib.add(responses_lib.POST, LOGIN_URL, json=AUTH_FAILURE, status=200)
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


@responses_lib.activate
def test_get_mode_settings_no_token_raises_auth_error(client):
    responses_lib.add(responses_lib.POST, LOGIN_URL, json=AUTH_FAILURE, status=200)
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


@responses_lib.activate
def test_set_port_mode_no_token_raises_auth_error(client):
    responses_lib.add(responses_lib.POST, LOGIN_URL, json=AUTH_FAILURE, status=200)
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
def test_set_port_mode_ai_plus_live_write_sends(authed_client):
    """AI+ live writes now SEND — the iOS app headers are the whole fix.

    Supersedes an earlier test asserting ai_plus_write_unsupported. That refusal
    existed because addDevMode returned 100001 under the default okhttp headers.
    With the iOS app headers the ordinary merged payload succeeds, so AI+ writes
    are no longer refused and the request must actually reach addDevMode.
    """
    ai_plus_manual = {**MOCK_MODE_SETTINGS_AI_PLUS_PORT1, "modeType": 0}
    ai_plus_response = {"code": 200, "msg": "success.", "data": ai_plus_manual}
    responses_lib.add(responses_lib.POST, MODE_SETTINGS_URL, json=ai_plus_response, status=200)
    responses_lib.add(responses_lib.POST, ADD_DEV_MODE_URL,
                      json={"code": 200, "msg": "success."}, status=200)
    with patch.object(authed_client, "_enforce_write_rate_limit"):
        result = authed_client.set_port_mode(
            AI_PLUS_DEVICE_DATA, port=1, updates={}, dry_run=False
        )
    assert "ai_plus_write_unsupported" not in result
    assert result["sent"] is True
    write_calls = [c for c in responses_lib.calls if "addDevMode" in c.request.url]
    assert len(write_calls) == 1
    sent_headers = write_calls[0].request.headers
    assert "Alamofire" in sent_headers["User-Agent"]
    assert sent_headers["phoneType"] == "1"


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

# ============ parse_device_data — plug-in probe readings (`probes`) ============
#
# sensorType is a published enum, not an arithmetic pattern (HA ac_infinity,
# const.py): 0=PROBE_TEMP_F 1=PROBE_TEMP_C 2=PROBE_HUMIDITY 3=PROBE_VPD, and
# 4/5/6/7 are the CONTROLLER_* equivalents. Types 0-3 belong to the plug-in
# AC-SPC24 probe; 4-7 are the controller's own sensor and are already the
# top-level reading.

# LIVE devType=20 capture (grower hardware, 2026-08). This is the only real
# probe payload anchoring this feature — every other probe fixture in the repo
# is hand-written, so keep this one faithful to the wire format.
MOCK_AI_PLUS_DUAL_PROBE_SENSORS = [
    {"sensorType": 11, "sensorPrecision": 1, "accessPort": 1, "sensorData": 585},
    {"sensorType": 12, "sensorPrecision": 2, "accessPort": 1, "sensorData": 997},
    {"sensorType": 0, "sensorPrecision": 3, "accessPort": 2, "sensorData": 6630},
    {"sensorType": 2, "sensorPrecision": 3, "accessPort": 2, "sensorData": 8160},
    {"sensorType": 3, "sensorPrecision": 3, "accessPort": 2, "sensorData": 39},
    {"sensorType": 4, "sensorPrecision": 3, "accessPort": 7, "sensorData": 7950},
    {"sensorType": 6, "sensorPrecision": 3, "accessPort": 7, "sensorData": 5890},
    {"sensorType": 7, "sensorPrecision": 3, "accessPort": 7, "sensorData": 84},
]

MOCK_DUAL_PROBE_DEVICE = {
    "devCode": "D89XA",
    "devName": "AI+ Dual Probe",
    "deviceInfo": {
        "temperature": 2639, "temperatureF": 7950, "humidity": 5890,
        "vpdnums": 84, "ports": [], "sensors": MOCK_AI_PLUS_DUAL_PROBE_SENSORS,
    },
}

# devType=22 (Q0KT4) capture supplied by the maintainer: onboard group ONLY, and
# its values equal the top-level exactly. The realistic single-group anchor.
MOCK_DEVTYPE22_ONBOARD_ONLY = {
    "devCode": "Q0KT4",
    "devName": "69 Pro+",
    "deviceInfo": {
        "temperatureF": 7050, "humidity": 5020, "vpdnums": 124,
        "temperature": 2139, "ports": [],
        "sensors": [
            {"sensorType": 4, "sensorPrecision": 3, "accessPort": 7,
             "sensorData": 7050, "sensorUnit": 0},
            {"sensorType": 6, "sensorPrecision": 3, "accessPort": 7,
             "sensorData": 5020, "sensorUnit": 0},
            {"sensorType": 7, "sensorPrecision": 3, "accessPort": 7,
             "sensorData": 124, "sensorUnit": 0},
        ],
    },
}


def _probe_device(sensors, **top):
    base = {"temperatureF": 7950, "humidity": 5890, "vpdnums": 84, "temperature": 2639}
    base.update(top)
    return {"devCode": "D89XA", "devName": "Probe Test",
            "deviceInfo": {**base, "ports": [], "sensors": sensors}}


def _probe_group(port, temp_type=0, temp=6630, hum=8160, vpd=39, **extra):
    return [
        {"sensorType": temp_type, "sensorPrecision": 3, "accessPort": port,
         "sensorData": temp, **extra},
        {"sensorType": 2, "sensorPrecision": 3, "accessPort": port, "sensorData": hum},
        {"sensorType": 3, "sensorPrecision": 3, "accessPort": port, "sensorData": vpd},
    ]


def _onboard_group(port=7, temp_type=4, temp=7950, hum=5890, vpd=84):
    return [
        {"sensorType": temp_type, "sensorPrecision": 3, "accessPort": port, "sensorData": temp},
        {"sensorType": 6, "sensorPrecision": 3, "accessPort": port, "sensorData": hum},
        {"sensorType": 7, "sensorPrecision": 3, "accessPort": port, "sensorData": vpd},
    ]


def test_parse_device_data_probe_surfaced(client):
    """The 0/2/3 group is the plug-in probe and is reported; 4/6/7 is not."""
    result = client.parse_device_data(MOCK_DUAL_PROBE_DEVICE)
    assert len(result["probes"]) == 1
    probe = result["probes"][0]
    assert probe["sensor_port"] == 2
    assert probe["temperature_c"] == pytest.approx(19.1, abs=0.05)  # 66.30 F
    assert probe["humidity_pct"] == pytest.approx(81.6)
    assert probe["vpd_kpa"] == pytest.approx(0.39)


def test_parse_device_data_probes_do_not_pollute_external_sensors(client):
    """sensorType<10 entries stay out of external_sensors (Quirk 20 unchanged)."""
    result = client.parse_device_data(MOCK_DUAL_PROBE_DEVICE)
    assert {s["sensor_type"] for s in result["external_sensors"]} == {11, 12}


def test_parse_device_data_no_probes_when_no_sensors(client):
    assert client.parse_device_data(MOCK_DEVICE)["probes"] == []


def test_parse_device_data_onboard_only_group_is_not_a_probe(client):
    """A single onboard (4/6/7) group is never reported as a probe.

    Anchored on the real devType=22 capture, where the onboard group equals the
    top-level reading exactly — so this holds by type, not by value comparison.
    """
    assert client.parse_device_data(MOCK_DEVTYPE22_ONBOARD_ONLY)["probes"] == []


def test_parse_device_data_identical_probe_and_onboard_both_kept(client):
    """A probe reading identically to the onboard sensor must still surface, and
    must be labelled with ITS port, not the onboard one."""
    device = _probe_device(
        _onboard_group(port=7) + _probe_group(port=2, temp=7950, hum=5890, vpd=84)
    )
    probes = client.parse_device_data(device)["probes"]
    assert len(probes) == 1
    assert probes[0]["sensor_port"] == 2


def test_parse_device_data_celsius_twin_on_onboard_group(client):
    """An onboard group carrying its Celsius twin (4,5,6,7) must not hide the probe.

    This is the case the arithmetic base/base+2/base+3 model silently broke.
    """
    onboard = _onboard_group(port=7) + [
        {"sensorType": 5, "sensorPrecision": 3, "accessPort": 7, "sensorData": 2639}
    ]
    probes = client.parse_device_data(_probe_device(onboard + _probe_group(port=2)))["probes"]
    assert len(probes) == 1
    assert probes[0]["sensor_port"] == 2


def test_parse_device_data_celsius_only_groups(client):
    """Celsius-only layouts (probe 1/2/3 + onboard 5/6/7) still resolve."""
    onboard = _onboard_group(port=7, temp_type=5, temp=2639)
    probe = _probe_group(port=2, temp_type=1, temp=1910)
    probes = client.parse_device_data(_probe_device(onboard + probe))["probes"]
    assert len(probes) == 1
    assert probes[0]["sensor_port"] == 2
    assert probes[0]["temperature_c"] == pytest.approx(19.1, abs=0.05)


def test_parse_device_data_probe_sensor_unit_flag_celsius(client):
    """sensorUnit > 0 means the raw value is ALREADY Celsius — do not convert."""
    probe = _probe_group(port=2, temp=1906, sensorUnit=1)
    probes = client.parse_device_data(_probe_device(_onboard_group() + probe))["probes"]
    assert probes[0]["temperature_c"] == pytest.approx(19.1, abs=0.05)


def test_parse_device_data_probe_sensor_unit_flag_fahrenheit(client):
    """sensorUnit == 0 means Fahrenheit — convert."""
    probe = _probe_group(port=2, temp=6630, sensorUnit=0)
    probes = client.parse_device_data(_probe_device(_onboard_group() + probe))["probes"]
    assert probes[0]["temperature_c"] == pytest.approx(19.1, abs=0.05)


def test_parse_device_data_all_zero_triplet_is_phantom(client):
    """An unpopulated 0/0/0 slot is a Quirk 20 phantom, not a probe at -17.8 C."""
    phantom = _probe_group(port=3, temp=0, hum=0, vpd=0)
    device = _probe_device(_onboard_group() + phantom)
    assert client.parse_device_data(device)["probes"] == []


def test_parse_device_data_partial_none_triplet_is_dropped(client):
    """A None member makes the group incomplete — it must not become 0 F at 81.6% RH."""
    partial = [
        {"sensorType": 0, "sensorPrecision": 3, "accessPort": 3, "sensorData": None},
        {"sensorType": 2, "sensorPrecision": 3, "accessPort": 3, "sensorData": 8160},
        {"sensorType": 3, "sensorPrecision": 3, "accessPort": 3, "sensorData": 39},
    ]
    device = _probe_device(_onboard_group() + partial)
    assert client.parse_device_data(device)["probes"] == []


def test_parse_device_data_unattested_type_shape_is_rejected(client):
    """Types 2/4/5 accidentally satisfied the old base+2/base+3 rule and minted a
    21.39 kPa VPD probe. Under the enum model it is simply not a probe group."""
    junk = [
        {"sensorType": 2, "sensorPrecision": 3, "accessPort": 3, "sensorData": 8160},
        {"sensorType": 4, "sensorPrecision": 3, "accessPort": 3, "sensorData": 7050},
        {"sensorType": 5, "sensorPrecision": 3, "accessPort": 3, "sensorData": 2139},
    ]
    assert client.parse_device_data(_probe_device(_onboard_group() + junk))["probes"] == []


def test_parse_device_data_lone_probe_without_onboard_group(client):
    """A probe present with NO onboard group must still be reported.

    The old drop-one rule swallowed this entirely.
    """
    probes = client.parse_device_data(_probe_device(_probe_group(port=2)))["probes"]
    assert len(probes) == 1
    assert probes[0]["sensor_port"] == 2


def test_parse_device_data_multiple_probes_ordered_by_port(client):
    """Three probe groups produce three probes, ordered by sensor_port."""
    sensors = (_onboard_group()
               + _probe_group(port=2, temp=6630)
               + _probe_group(port=4, temp=7000)
               + _probe_group(port=3, temp=6800))
    probes = client.parse_device_data(_probe_device(sensors))["probes"]
    assert [p["sensor_port"] for p in probes] == [2, 3, 4]


def test_parse_device_data_probe_and_onboard_on_same_port(client):
    """Types 0-3 and 4-7 sharing an accessPort: the onboard half is filtered by
    type, so the probe half still resolves rather than both being lost."""
    sensors = _onboard_group(port=7) + _probe_group(port=7, temp=6630)
    probes = client.parse_device_data(_probe_device(sensors))["probes"]
    assert len(probes) == 1
    assert probes[0]["sensor_port"] == 7


@pytest.mark.parametrize("bad_entry", [
    {"sensorType": "1", "sensorPrecision": 3, "accessPort": 9, "sensorData": 100},
    {"sensorType": None, "sensorPrecision": 3, "accessPort": 9, "sensorData": 100},
    {"sensorType": 0, "sensorPrecision": 3, "accessPort": None, "sensorData": 100},
    {"sensorType": "abc", "sensorPrecision": 3, "accessPort": 9, "sensorData": 100},
])
def test_parse_device_data_malformed_sensor_entry_does_not_break_reading(client, bad_entry):
    """One unparseable entry must not cost the grower temperature, humidity, VPD,
    ports and external_sensors via ACInfinityAPIError."""
    sensors = MOCK_AI_PLUS_DUAL_PROBE_SENSORS + [bad_entry]
    result = client.parse_device_data(_probe_device(sensors))
    assert result["temperature_f"] == pytest.approx(79.5)
    assert len(result["probes"]) == 1


def test_parse_device_data_all_string_typed_probe_group(client):
    """String-typed sensorType/accessPort are coerced, not crashed on."""
    sensors = _onboard_group() + [
        {"sensorType": "0", "sensorPrecision": 3, "accessPort": "2", "sensorData": 6630},
        {"sensorType": "2", "sensorPrecision": 3, "accessPort": "2", "sensorData": 8160},
        {"sensorType": "3", "sensorPrecision": 3, "accessPort": "2", "sensorData": 39},
    ]
    result = client.parse_device_data(_probe_device(sensors))
    assert result["temperature_f"] == pytest.approx(79.5)
    assert len(result["probes"]) == 1
    assert result["probes"][0]["sensor_port"] == 2
