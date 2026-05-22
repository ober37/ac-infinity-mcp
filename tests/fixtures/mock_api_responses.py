"""Shared mock API response payloads for HTTP-level tests."""

# docs/API.md documents the real auth-success response shape: code=200, msg
# present, and data.appEmail populated alongside data.appId. Fixture mirrors
# the real shape so tests don't accidentally rely on a thinner mock.
AUTH_SUCCESS = {
    "code": 200,
    "msg": "success",
    "data": {"appId": "tok_test_abc123", "appEmail": "test@example.com"},
}

# Real API returns code 400 (not 401) for bad credentials — see docs/API.md
# "Authentication failure" section. 401 is reserved for expired-token responses
# on other endpoints. Keeping the failure code accurate so code-400-specific
# branches (e.g. "wrong password" UX) are testable.
AUTH_FAILURE = {"code": 400, "msg": "Email or password is wrong"}

# Token-expiry 401 — used by tests that exercise the refresh-on-401 path.
AUTH_401_TOKEN_EXPIRED = {"code": 401, "msg": "Token expired"}

_MOCK_DEVICE_1 = {
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
            {"port": 1, "portName": "Intake Fan", "speak": 5, "portsLoad": 1},
            {"port": 2, "portName": "Exhaust Fan", "speak": 7, "portsLoad": 1},
        ],
    },
}

_MOCK_DEVICE_2 = {
    "devCode": "D89XA",
    "devName": "Test 89 AI+",
    "devType": 20,
    "devId": 67890,
    "online": False,
    "newFrameworkDevice": True,
    "deviceInfo": {
        "temperature": 2400,
        "temperatureF": 7520,
        "humidity": 5500,
        "vpdnums": 150,
        "ports": [{"port": 1, "portName": "Port 1", "speak": 0, "portsLoad": 0}],
    },
}

DEVICES_SUCCESS = {"code": 200, "data": [_MOCK_DEVICE_1, _MOCK_DEVICE_2]}
DEVICES_EMPTY = {"code": 200, "data": []}
DEVICES_API_ERROR = {"code": 500, "msg": "Internal server error"}

# 10 history records with createTime=1714000100..1714000109 (all within range)
_BASE_TS = 1714000100
HISTORY_PAGE_1 = {
    "code": 200,
    "data": {
        "rows": [
            {
                "createTime": _BASE_TS + i,
                "temperature": 2400,
                "fTemperature": 7520,
                "humidity": 5500,
                "vpdNums": 150,
                "portSpead": 5,
                "portStatus": 1,
                "devPortCount": 2,
            }
            for i in range(10)
        ]
    },
}
HISTORY_EMPTY = {"code": 200, "data": {"rows": []}}
HISTORY_API_ERROR = {"code": 500, "msg": "Internal server error"}
