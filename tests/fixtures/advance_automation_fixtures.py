"""Mock data for Advance Automation tools (Phase 17 Part 2)."""

# grouptDevType is a port bitmask: Port N → 2^(N-1).
#   grouptDevType: 48  = ports 5 and 6 combined (32 + 16 = 2^4 + 2^5)
#   grouptDevType: 8   = port 4 (2^3)
#   grouptDevType: 4   = port 3 (2^2)
#
# Two entries for "Moderate Airflow" (same advName, different advId/onSpeed/grouptDevType)
# and one entry for "Pollenation Airflow" (disabled, scheduled).
MOCK_ADVANCE_AUTOMATIONS_LIST = [
    {
        "advId": 1342758,
        "advName": "Moderate Airflow",
        "isOn": 1,
        "onSpeed": 2,
        "offSpeed": 0,
        "grouptDevType": 48,
        "advKey": "1-0",
        "runState": 1,
        "beginTime": 255,
        "endTime": 255,
        "onTimeSwitch": 0,
    },
    {
        "advId": 2179295,
        "advName": "Moderate Airflow",
        "isOn": 1,
        "onSpeed": 1,
        "offSpeed": 0,
        "grouptDevType": 8,
        "advKey": "1-1",
        "runState": 1,
        "beginTime": 255,
        "endTime": 255,
        "onTimeSwitch": 0,
    },
    {
        "advId": 999001,
        "advName": "Pollenation Airflow",
        "isOn": 0,
        "onSpeed": 3,
        "offSpeed": 0,
        "grouptDevType": 4,
        "advKey": "2-0",
        "runState": 0,
        "beginTime": 540,
        "endTime": 1020,
        "onTimeSwitch": 0,
    },
]

MOCK_ADVANCE_AUTOMATIONS_EMPTY: list[dict] = []

# Single-entry automation (used to test single-group human_summary path).
MOCK_ADVANCE_AUTOMATIONS_SINGLE = [
    {
        "advId": 999001,
        "advName": "Pollenation Airflow",
        "isOn": 0,
        "onSpeed": 3,
        "offSpeed": 0,
        "grouptDevType": 4,
        "advKey": "2-0",
        "runState": 0,
        "beginTime": 540,
        "endTime": 1020,
        "onTimeSwitch": 0,
    },
]
