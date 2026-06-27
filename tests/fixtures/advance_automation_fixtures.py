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


# ============ Issue #284 — full per-mode getGroups rule shapes (live-captured) ============
#
# These mirror the real Seedling/Clone-Transplant entries (capture program "0624"):
# currentMode=4 rules carry coexisting targetTSwitch/targetHumiSwitch/targetVpdSwitch=1
# plus rail-value triggers, so _decode_rule must use the rail-sentinel rules, not raw
# switch flags. Field names use the compositional Rev-4 model (mode auto|vpd|... ).

# currentMode=4 AUTO-TARGET (settingMode=1, targetHumi>0); the trigger families are parked
# at rails with switches=1, exactly as the live "Seedling" Auto-target (humidifier) rule.
MOCK_RULE_HUMIDITY_SETPOINT = {
    "advId": 2376632, "advName": "Seedling", "isOn": 1, "currentMode": 4,
    "grouptDevType": 1, "beginTime": 180, "endTime": 540, "runState": 0,
    "onSpeed": 2, "offSpeed": 0, "switchTime": 127,
    "setSelect": 0, "settingMode": 1, "targetHumi": 65, "targetTempF": 32,
    "autoLowTempF": 32, "autoHighTempF": 194, "autoLowTempC": 0, "autoHighTempC": 90,
    "autoLowTempSwitch": 1, "autoHighTempSwitch": 1,
    "autoLowHumi": 0, "autoHighHumi": 100, "autoLowHumiSwitch": 1, "autoHighHumiSwitch": 1,
    # VPD family is zeroed in Auto mode (real app signature; #288).
    "lowVpd": 0, "highVpd": 0, "lowVpdSwitch": 0, "highVpdSwitch": 0, "targetVpd": 0,
    "targetTSwitch": 1, "targetHumiSwitch": 1, "targetVpdSwitch": 0,
    "temperatureFBuff": 0, "temperatureFTrans": 0, "humidityBuff": 0, "humidityTrans": 0,
    "vpdBuff": 0, "vpdTrans": 0,
    "cycleOn": 0, "cycleOff": 0, "onTimeSwitch": 0,
}

# currentMode=4 AUTO-TRIGGER, temperature on_below only (autoLowTempSwitch=1 + real low,
# autoHighTempF=194 rail / switch=0) — the live "Seedling" Auto-trigger (heater) rule.
MOCK_RULE_TEMPERATURE_TRIGGER = {
    "advId": 2375084, "advName": "Seedling", "isOn": 1, "currentMode": 4,
    "grouptDevType": 2, "beginTime": 540, "endTime": 180, "runState": 1,
    "onSpeed": 10, "offSpeed": 0, "switchTime": 127,
    "setSelect": 1, "settingMode": 0, "targetHumi": 0, "targetTempF": 32,
    "autoLowTempF": 76, "autoHighTempF": 194, "autoLowTempC": 0, "autoHighTempC": 90,
    "autoLowTempSwitch": 1, "autoHighTempSwitch": 0,
    "autoLowHumi": 0, "autoHighHumi": 100, "autoLowHumiSwitch": 0, "autoHighHumiSwitch": 0,
    # VPD family is zeroed in Auto mode (real app signature; #288).
    "lowVpd": 0, "highVpd": 0, "lowVpdSwitch": 0, "highVpdSwitch": 0, "targetVpd": 0,
    "targetTSwitch": 1, "targetHumiSwitch": 1, "targetVpdSwitch": 0,
    "temperatureFBuff": 0, "temperatureFTrans": 0, "humidityBuff": 0, "humidityTrans": 0,
    "vpdBuff": 0, "vpdTrans": 0,
    "cycleOn": 0, "cycleOff": 0, "onTimeSwitch": 0,
}

# currentMode=6 VPD-TARGET (settingMode=1, targetVpd=9 → 0.9 kPa) — live "Clone Transplant".
MOCK_RULE_VPD = {
    "advId": 1832148, "advName": "Clone Transplant", "isOn": 0, "currentMode": 6,
    "grouptDevType": 1, "beginTime": 120, "endTime": 480, "runState": 0,
    "onSpeed": 1, "offSpeed": 0, "switchTime": 127,
    "setSelect": 0, "settingMode": 1, "targetVpd": 9,
    "autoLowTempF": 32, "autoHighTempF": 32, "autoLowTempSwitch": 0, "autoHighTempSwitch": 0,
    "autoLowHumi": 0, "autoHighHumi": 0, "autoLowHumiSwitch": 0, "autoHighHumiSwitch": 0,
    # Real Clone Transplant signature: setpoint mirrored into highVpd, low off (#288).
    "lowVpd": 0, "highVpd": 9, "lowVpdSwitch": 0, "highVpdSwitch": 1,
    "targetTSwitch": 0, "targetHumiSwitch": 0, "targetVpdSwitch": 1, "targetTempF": 32,
    "targetHumi": 0, "temperatureFBuff": 0, "temperatureFTrans": 0,
    "humidityBuff": 0, "humidityTrans": 0, "vpdBuff": 0, "vpdTrans": 0,
    "cycleOn": 0, "cycleOff": 0, "onTimeSwitch": 0,
}

# Two rules, same advName, same port (bitmask 1), different windows — the verified
# two-window pattern. One VPD lights-on, one humidity-setpoint lights-off.
MOCK_TWO_WINDOW_PROGRAM = [
    {**MOCK_RULE_VPD, "advName": "Seedling", "grouptDevType": 1,
     "beginTime": 540, "endTime": 180, "runState": 1},
    {**MOCK_RULE_HUMIDITY_SETPOINT, "advName": "Seedling", "grouptDevType": 1,
     "beginTime": 180, "endTime": 540, "runState": 0},
]

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
