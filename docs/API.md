# AC Infinity API Reference

New to the server? The [Grower's Guide](GUIDE.md) walks through every tool with conversation examples.

## Overview

- **Base URL:** `https://www.acinfinityserver.com/api` (HTTPS — TLSv1.3, DigiCert certificate)
- **Auth:** form-POST to `/user/appUserLogin`; session token returned in `data.appId` field
- **All requests:** `Content-Type: application/x-www-form-urlencoded; charset=utf-8`
- **All responses:** `{"code": 200, "msg": "...", "data": ...}`
- **Non-200 codes** indicate errors (e.g. 400 for bad credentials, 500 for server fault)

## Security Note

The AC Infinity cloud API supports HTTPS (TLSv1.3) with a valid DigiCert certificate
(verified 2026-05-29). Credentials and session tokens are encrypted in transit.

Additionally, device list responses include the authenticated user's email address in the
`appEmail` field. Never log raw device API responses at any log level.

---

## Endpoints

### POST /user/appUserLogin

**Purpose:** Authenticate and retrieve a session token.

**Headers:**
```
Content-Type: application/x-www-form-urlencoded; charset=utf-8
User-Agent: ACController/1.8.2 (com.acinfinity.humiture; build:489; iOS 16.5.1)
```

**Request parameters:**

| Field | Type | Notes |
|-------|------|-------|
| `appEmail` | string | User email address |
| `appPasswordl` | string | **Intentional typo — lowercase `l` at end (Quirk 1)** |

**Request example:**
```
appEmail=user%40example.com&appPasswordl=yourpassword
```

**Response (success):**
```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "appId": "abcdef12...",
    "appEmail": "user@example.com"
  }
}
```

**Response (failure):**
```json
{
  "code": 400,
  "msg": "Email or password is wrong",
  "data": null
}
```

**Notes:**
- Store `data.appId` as the session token for all subsequent requests
- Password is silently truncated to 25 characters server-side (Quirk 2)
- Token does not expire on a fixed TTL in testing; it may expire if the mobile app
  forces a re-login or after extended inactivity. Re-authenticate by restarting the server.

---

### POST /user/devInfoListAll

**Purpose:** Fetch all devices associated with the account.

**Headers:**
```
token: <appId>
Host: www.acinfinityserver.com
User-Agent: okhttp/3.10.0
```

**Query parameters:**
```
userId=<appId>
```

**Response (success):**
```json
{
  "code": 200,
  "msg": "success",
  "data": [
    {
      "devId": "9876543210123456789",
      "devCode": "C58ZA",
      "devName": "Towlie Tent",
      "devType": 11,
      "devPortCount": 4,
      "online": 1,
      "newFrameworkDevice": false,
      "firmwareVersion": "3.2.56",
      "hardwareVersion": "1.1",
      "appEmail": "user@example.com",
      "deviceInfo": {
        "temperature": 1803,
        "temperatureF": 6445,
        "humidity": 5895,
        "vpdnums": 78,
        "vpdstatus": 2,
        "ports": [
          {
            "port": 1,
            "portName": "Humidifier",
            "speak": 0,
            "loadType": 0,
            "loadState": 0,
            "online": 0
          },
          {
            "port": 4,
            "portName": "Filter",
            "speak": 5,
            "loadType": 0,
            "loadState": 0,
            "online": 1
          }
        ],
        "sensors": null
      }
    }
  ]
}
```

**Key field notes:**

| Field | Notes |
|-------|-------|
| `devId` | Numeric ID (as string at top level, as integer inside `deviceInfo`). Required by history API. (Quirk 7) |
| `devCode` | Alphanumeric device code (e.g. `"C58ZA"`). Used as `device_id` in MCP tools. (Quirk 7) |
| `online` | `1` = online, `0` = offline |
| `newFrameworkDevice` | `true` for AI+ controllers — use static full payload on write (Quirk 14) |
| `deviceInfo.temperature` | Raw value ÷ 100 = °C (Quirk 4) |
| `deviceInfo.temperatureF` | Raw value ÷ 100 = °F (Quirk 4) |
| `deviceInfo.humidity` | Raw value ÷ 100 = % RH (Quirk 4) |
| `deviceInfo.vpdnums` | Raw value ÷ 100 = VPD in kPa. Note lowercase `n` (Quirk 10) |
| `deviceInfo.ports[].speak` | Port speed 0–10 (Quirk 5 decoding applies in history records, not here) |
| `zoneId` | IANA timezone string (e.g. `"America/Chicago"`) — used by MCP tools to localise timestamps and schedule windows. Absent on some older firmware; falls back to UTC (Quirk 23) |
| `deviceInfo.unit` | Temperature unit preference: `0` = °F, `1` = °C. Absent on some devices; falls back to °C (Quirk 23) |
| `appEmail` | User's email exposed in every device record — never log raw API responses (Security Note) |

---

### POST /log/dataPage

**Purpose:** Fetch historical sensor and port data for a device.

**Headers:**
```
token: <appId>
Host: www.acinfinityserver.com
User-Agent: okhttp/3.10.0
Content-Type: application/x-www-form-urlencoded; charset=utf-8
```

**Request parameters:**

| Field | Type | Notes |
|-------|------|-------|
| `appId` | string | Session token |
| `devId` | string/int | Numeric device ID from `devInfoListAll.devId` (not `devCode`) |
| `time` | int | Unix timestamp (seconds) — start of window |
| `endTime` | int | Unix timestamp (seconds) — end of window |
| `pageNum` | int | Always send `1` — API ignores this field (Quirk 3) |
| `pageSize` | int | Max records per response. API caps at ~1,257/day regardless (Quirk 9) |

**Request example:**
```
appId=abcdef12...&devId=9876543210123456789&time=1748000000&endTime=1748003600&pageNum=1&pageSize=2000
```

**Response (success):**
```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "rows": [
      {
        "devId": "9876543210123456789",
        "createTime": 1748000060,
        "temperature": 1796,
        "humidity": 5900,
        "ftemperature": 6433,
        "fTemperature": 6433,
        "vpdNums": 78,
        "vpdnums": 78,
        "portSpead": 0,
        "portStatus": 0,
        "devPortCount": null,
        "allSpead": 0,
        "dataStatus": 0,
        "leafTemp": 0,
        "sensorData": null,
        "sensors": null
      }
    ]
  }
}
```

**Key field notes:**

| Field | Notes |
|-------|-------|
| `createTime` | Unix timestamp of the reading |
| `temperature` | Raw ÷ 100 = °C (Quirk 4) |
| `humidity` | Raw ÷ 100 = % RH (Quirk 4) |
| `fTemperature` | Raw ÷ 100 = °F. Both `ftemperature` and `fTemperature` present — use `fTemperature` (Quirk 4) |
| `vpdNums` | Raw ÷ 100 = VPD. Note uppercase `N` — differs from live device field `vpdnums` (Quirk 10) |
| `portSpead` | Bitmask: 4 bits (one nibble) per port, LSB = Port 1. Values 0–10 = speed; `0xF` (15) = ON for toggle devices (Quirk 5) |
| `portStatus` | Bitmask: 1 bit per port, LSB = Port 1. `1` = port is automation-triggered (Quirk 6) |
| `devPortCount` | Often `null` in history records — fall back to 8 when null (Quirk 5) |

**Pagination strategy:**

The `pageNum` field is ignored by the server (Quirk 3). To retrieve records beyond one
page, use time-cursor pagination:

```python
# After each response, advance the time cursor past the last record
last_ts = rows[-1]["createTime"]
next_request_time = last_ts + 1  # exclusive start for next page
# Stop when: len(rows) < page_size, or last_ts >= end_timestamp
```

---

### POST /dev/getdevModeSettingList

**Purpose:** Read current mode settings for one port on a device (required before every legacy write).

**Headers:**
```
token: <appId>
Host: www.acinfinityserver.com
User-Agent: okhttp/3.10.0
Content-Type: application/x-www-form-urlencoded; charset=utf-8
```

**Request parameters:**

| Field | Type | Notes |
|-------|------|-------|
| `devId` | string | Numeric device ID from `devInfoListAll` (Quirk 7) |
| `port` | int | 1-based port number. **Required** — omitting returns code 999999 (Quirk 16) |
| `appId` | string | Session token (`appId` from login) |

**Request example:**
```
devId=REDACTED_DEV_ID&port=1&appId=REDACTED_TOKEN
```

**Response (success):**
```json
{
  "code": 200,
  "msg": "success.",
  "data": {
    "modeSetid": "REDACTED_MODE_SET_ID",
    "devId": "REDACTED_DEV_ID",
    "externalPort": 1,
    "offSpead": 0,
    "onSpead": 5,
    "onSelfSpead": 0,
    "activeHt": 0,
    "devHt": 90,
    "devHtf": 194,
    "devLtf": 32,
    "activeLt": 0,
    "devLt": 0,
    "activeHh": 0,
    "devHh": 100,
    "activeLh": 0,
    "devLh": 0,
    "acitveTimerOn": 0,
    "acitveTimerOff": 0,
    "activeCycleOn": 300,
    "activeCycleOff": 60,
    "schedStartTime": 65535,
    "schedEndtTime": 65535,
    "surplus": 0,
    "modeType": 0,
    "activeHtVpd": 0,
    "activeLtVpd": 0,
    "activeHtVpdNums": 99,
    "activeLtVpdNums": 1,
    "targetTSwitch": 0,
    "targetHumiSwitch": 0,
    "settingMode": 0,
    "vpdSettingMode": 0,
    "targetVpdSwitch": 0,
    "targetVpd": 0,
    "targetTemp": 0,
    "targetTempF": 32,
    "targetHumi": 65,
    "isUpdateVpdNums": false,
    "co2TargetSwitch": 0,
    "co2SettingMode": 0,
    "co2HighSwitch": 0,
    "co2LowSwitch": 0,
    "co2HighValue": 0,
    "co2LowValue": 0,
    "co2TargetValue": 0,
    "co2Accuracy": 0,
    "co2FanTargetSwitch": 0,
    "co2FanSettingMode": 0,
    "co2FanHighSwitch": 0,
    "co2FanLowSwitch": 0,
    "co2FanHighValue": 0,
    "co2FanLowValue": 0,
    "co2FanTargetValue": 0,
    "co2FanAccuracy": 0,
    "moistureTargetSwitch": 0,
    "moistureSettingMode": 0,
    "moistureHighSwitch": 0,
    "moistureLowSwitch": 0,
    "moistureHighValue": 0,
    "moistureLowValue": 0,
    "moistureTargetValue": 0,
    "moistureAccuracy": 0,
    "waterTempTargetSwitch": 0,
    "waterTempSettingMode": 0,
    "waterTempHighSwitch": 0,
    "waterTempLowSwitch": 0,
    "waterTempHighValueF": 32,
    "waterTempHighValue": 0,
    "waterTempLowValueF": 32,
    "waterTempLowValue": 0,
    "waterTempTargetValueF": 32,
    "waterTempTargetValue": 0,
    "waterTempAccuracy": 0,
    "phTargetSwitch": 0,
    "phSettingMode": 0,
    "phHighSwitch": 0,
    "phLowSwitch": 0,
    "phHighValue": 0,
    "phLowValue": 0,
    "phTargetValue": 0,
    "phAccuracy": 0,
    "ecTdsTargetSwitch": 0,
    "ecTdsSettingMode": 0,
    "ecTdsHighSwitch": 0,
    "ecTdsLowSwitchEc": 0,
    "ecTdsLowSwitchTds": 0,
    "ecTdsHighValueEcUs": 0,
    "ecTdsHighValueEcMs": 0,
    "ecTdsHighValueTdsPpm": 0,
    "ecTdsHighValueTdsPpt": 0,
    "ecTdsLowValueEcUs": 0,
    "ecTdsLowValueEcMs": 0,
    "ecTdsLowValueTdsPpm": 0,
    "ecTdsLowValueTdsPpt": 0,
    "ecTdsTargetValueEcUs": 0,
    "ecTdsTargetValueEcMs": 0,
    "ecTdsTargetValueTdsPpm": 0,
    "ecTdsTargetValueTdsPpt": 0,
    "ecTdsAccuracy": 0,
    "waterLevelTargetSwitch": 0,
    "waterLevelSettingMode": 0,
    "waterLevelHighSwitch": 0,
    "waterLevelLowSwitch": 0,
    "waterLevelHighValue": 0,
    "waterLevelLowValue": 0,
    "waterLevelTargetValue": 0,
    "waterLevelAccuracy": 0,
    "ecOrTds": null,
    "flowRate": null,
    "quickRunTime": null,
    "quickRunState": null,
    "sensorModeFlowRate": null,
    "maxWateringAmount": null,
    "protection": null,
    "schedModeFlowRate": null,
    "waterDuration": 0,
    "interval": 0,
    "timestamp": null,
    "reportSeq": null,
    "fieldSet": [],
    "humidity": 5714,
    "temperature": 1792,
    "tTrend": 0,
    "hTrend": 0,
    "unit": 0,
    "speak": 0,
    "trend": 0,
    "atType": 1,
    "temperatureF": 6426,
    "isOpenAutomation": 0,
    "devTimeZone": null,
    "loadType": 0,
    "loadState": 0,
    "abnormalState": 0,
    "devMacAddr": null,
    "restore": false,
    "masterPort": null,
    "onlyUpdateSpeed": 0,
    "tdsUnit": 0,
    "ecUnit": 0,
    "devSetting": { "...": "nested device config — not included in write payload" },
    "ipcSetting": null
  }
}
```

**Structure notes:**

| Aspect | Detail |
|--------|--------|
| Total fields | 142 per port response |
| Flat scalar fields | 140 (these form the write payload basis) |
| `fieldSet` | Always `[]` — exclude from write payload (Quirk 13) |
| `devSetting` | Nested device config dict — exclude from write payload (Quirk 13) |
| `ipcSetting` | Always `null` — exclude from write payload |
| Response vs legacy vs AI+ | Identical 142-field structure for devType 11, 18, and 22 |

**Field reference (140 flat fields):**

| Field | Type | Description |
|-------|------|-------------|
| `modeSetid` | string | Record ID — **exclude from write payload** (Quirk 11) |
| `devId` | string | Device ID — include in write payload |
| `externalPort` | int | Port number (1-based) |
| `offSpead` | int | Off speed (0–10) |
| `onSpead` | int | On speed (0–10) |
| `onSelfSpead` | int | Self-start speed |
| `modeType` | int | Mode type — must be 2 when `onSpead > 0` (Quirk 12) |
| `activeHt` / `activeHh` / `activeLt` / `activeLh` | int | High/low temp/humidity trigger enables (0=off, 1=on) |
| `devHt` / `devHtf` / `devLt` / `devLtf` | int | High/low temp thresholds in raw °C and °F (no ×100 scaling — `devHt=28` means 28°C) |
| `devHh` / `devLh` | int | High/low humidity thresholds in raw % RH (no ×100 scaling — `devHh=70` means 70%) |
| `acitveTimerOn` / `acitveTimerOff` | int | Timer countdown durations in **seconds** for TIMER_TO_ON / TIMER_TO_OFF modes respectively (note typo in field name: `acitve`) |
| `activeCycleOn` / `activeCycleOff` | int | Cycle mode on/off durations (seconds) |
| `schedStartTime` / `schedEndtTime` | int | Schedule start/end as **minutes since midnight** in device local time (65535 = disabled; note typo in `schedEndtTime`). Convert: `06:30` → 390 |
| `targetVpd` | int | VPD automation target — divide by 10 for kPa (`targetVpd=14` → 1.4 kPa). Distinct from live sensor `vpdnums` which is ÷100. |
| `vpdSettingMode` / `targetVpdSwitch` | int | VPD automation mode and enable flags (both set to 1 to enable VPD mode) |
| `surplus` | int or null | Legacy: 0; AI+: null |
| `activeHtVpd` / `activeLtVpd` | int | VPD high/low trigger enables |
| `activeHtVpdNums` / `activeLtVpdNums` | int | VPD thresholds |
| `targetTSwitch` / `targetHumiSwitch` | int | Target mode enables |
| `settingMode` | int | Setting mode flag |
| `targetTemp` / `targetTempF` / `targetHumi` | int | Temperature and humidity target values |
| `isUpdateVpdNums` | bool | VPD update flag |
| `co2*` / `co2Fan*` | int | CO2 and CO2 fan automation settings (8 fields each) |
| `moisture*` | int | Moisture sensor automation settings (8 fields) |
| `waterTemp*` | int | Water temperature automation settings (11 fields) |
| `ph*` | int | pH automation settings (8 fields) |
| `ecTds*` | int | EC/TDS automation settings (17 fields) |
| `waterLevel*` | int | Water level automation settings (8 fields) |
| `waterDuration` / `interval` | int | Watering duration and interval |
| `humidity` / `temperature` / `temperatureF` | int | Current sensor readings (raw ×100) — included in write payload |
| `speak` / `trend` / `tTrend` / `hTrend` | int | Current port/trend state |
| `atType` / `unit` | int | Automation type / unit flags |
| `isOpenAutomation` | int | Automation enabled flag |
| `loadType` / `loadState` / `abnormalState` | int | Port load info |
| `restore` | bool | Restore flag |
| `onlyUpdateSpeed` / `tdsUnit` / `ecUnit` | int | Misc flags |
| Null fields | — | `ecOrTds`, `flowRate`, `quickRunTime`, `quickRunState`, `sensorModeFlowRate`, `maxWateringAmount`, `protection`, `schedModeFlowRate`, `timestamp`, `reportSeq`, `devTimeZone`, `devMacAddr`, `masterPort` |

---

### POST /dev/addDevMode

**Purpose:** Write mode settings for one port. Used by both legacy and AI+ controllers.

**Critical:** Strip `modeSetid` (Quirk 11). Set `modeType=2` when `onSpead > 0` (Quirk 12).
Enforce 1.5s minimum between calls (Quirk 15).

**Headers:** Same as `getdevModeSettingList`.

**Request parameters:** All 140 flat scalar fields from `getdevModeSettingList` response,
with `modeSetid` removed and desired changes overlaid. Do **not** include `fieldSet` (list)
or `devSetting` (nested dict) — these cannot be form-encoded.

**Request example (partial):**
```
devId=REDACTED_DEV_ID&externalPort=1&onSpead=5&modeType=2&offSpead=0&...
```

**Response (success):**
```json
{"code": 200, "msg": "success", "data": null}
```

**Response (rate limit exceeded — Quirk 15):**
```json
{"code": 403, "msg": "Data saving failed. Please try again later.", "data": null}
```

---

## All 28 Known API Quirks

### Quirk 1 — Auth typo: `appPasswordl`

The login endpoint parameter for the password is `appPasswordl` — with a lowercase letter
`l` at the end, not the digit `1`. This is an intentional (or permanent) typo in the
AC Infinity app. Using the correct spelling `appPassword` silently fails — the server
accepts the request but returns `code=400`.

**Request field:** `appPasswordl=yourpassword` (not `appPassword`)

---

### Quirk 2 — Password silently truncated to 25 characters

The AC Infinity API silently truncates passwords longer than 25 characters server-side.
Passwords are truncated in the client before sending to ensure consistent authentication
across sessions:

```python
self.password = password[:25]  # applied in ACInfinityClient.__init__
```

---

### Quirk 3 — `pageNum` ignored; use time-cursor pagination

The `pageNum` parameter in `/log/dataPage` is accepted but ignored — the server always
returns the first `pageSize` records starting from `time`. To retrieve subsequent pages,
advance the `time` field past the last returned `createTime`:

```
# Request 1: time=T0, endTime=T1, pageSize=2000
# Response: records [R1...R2000] (oldest to newest within the page)
# Request 2: time=R2000.createTime + 1, endTime=T1, pageSize=2000
# Repeat until response has fewer than pageSize records
```

Records within a page are returned oldest-first; advancing `time` past the
newest `createTime` in the current page moves the cursor forward through
history. The client's pagination test in `tests/common/test_client.py`
exercises this ordering explicitly.

---

### Quirk 4 — Sensor values divided by 100

All numeric sensor values in API responses are integers representing the actual value × 100.
Divide by 100 to get the real-world value:

| API field | Raw value | Parsed value |
|-----------|-----------|-------------|
| `temperature` | `1803` | `18.03 °C` |
| `temperatureF` | `6445` | `64.45 °F` |
| `humidity` | `5895` | `58.95 % RH` |
| `vpdnums` | `78` | `0.78 kPa` |

---

### Quirk 5 — Port speeds as 4-bit nibbles in `portSpead` bitmask

In historical records, port speeds are packed into the `portSpead` integer field as
4-bit nibbles (one nibble per port). LSB nibble = Port 1:

```python
port_spead = record["portSpead"]  # e.g. 0x0050 = Port1=0, Port2=5
for i in range(port_count):
    nibble = (port_spead >> (i * 4)) & 0xF
    speed = 1 if nibble == 0xF else nibble  # 0xF = ON for toggle devices (lights, heaters)
```

Values 0–10 represent fan/dimmer speed. Value `0xF` (15) represents ON state for
on/off devices (lights, heaters, humidifiers). `devPortCount` is often `null` in
history records — fall back to 8.

---

### Quirk 6 — `portStatus` bitmask (1 bit per port)

The `portStatus` field is a bitmask where each bit indicates whether a port is currently
being triggered by an automation rule (as opposed to manual control):

```python
port_status = record["portStatus"]
for i in range(port_count):
    automation_triggered = bool((port_status >> i) & 1)
```

---

### Quirk 7 — `devCode` (string) ≠ `devId` (numeric)

Every device has two distinct identifiers:

| Field | Example | Used for |
|-------|---------|----------|
| `devCode` | `"C58ZA"` | MCP tool `device_id` parameter; device list display |
| `devId` | `"9876543210123456789"` | History API `devId` parameter |

Passing `devCode` to the history API returns an empty result with no error. Always look
up `devId` from the device list before calling `/log/dataPage`.

Note: `devId` appears as a string at the top level of device records and as a large
integer inside `deviceInfo`. Both represent the same value.

---

### Quirk 8 — HTTPS confirmed (TLSv1.3)

The base URL `https://www.acinfinityserver.com/api` supports HTTPS. TLS handshake
verified 2026-05-29: TLSv1.3, DigiCert Encryption Everywhere DV TLS CA certificate,
`SSL certificate verify ok`, valid until 2026-11-18. Credentials and session tokens
are encrypted in transit.

---

### Quirk 9 — History API caps at ~1,257 records/day

Regardless of `pageSize`, the `/log/dataPage` endpoint returns at most approximately
1,257 records per calendar day. For multi-day queries the data may appear sparse — this
is a server-side limitation, not a client bug. Expect roughly one record per minute
(1,440/day theoretical maximum, ~1,257 in practice).

---

### Quirk 10 — `vpdnums` (live) vs `vpdNums` (history) casing

The VPD field has different casing in the two contexts:

| Context | Field name | Example |
|---------|-----------|---------|
| Device list (`devInfoListAll`) | `vpdnums` (lowercase `n`) | `"vpdnums": 78` |
| History records (`dataPage`) | `vpdNums` (uppercase `N`) | `"vpdNums": 78` |

Both fields are present in history records (the API returns both `vpdNums` and `vpdnums`),
but only `vpdnums` appears in live device records. Parsers must use the correct field
for each context.

---

### Quirk 11 — Never include `modeSetid` for legacy controllers (→ 403)

When writing mode settings to legacy controllers (where `newFrameworkDevice=false`),
do **not** include the `modeSetid` field in the request payload. Including it causes a
403 error even with a valid token and correct parameters. Omit the field entirely:

```
# BAD  (legacy controller, will 403)
devId=...&modeSetid=0&onSpead=5&...

# GOOD (legacy controller)
devId=...&onSpead=5&...
```

---

### Quirk 12 — Must set `modeType=2` when `onSpead > 0`

When sending a write command with a non-zero fan speed (`onSpead > 0`), the `modeType`
field must be set to `2`. Sending `modeType=0` or omitting it causes the command to
be accepted (200 response) but not persisted — the device reverts to its previous mode.

```
# Required when turning on a port at speed > 0
modeType=2&onSpead=5&...
```

---

### Quirk 13 — Legacy controllers require read-before-write (all ~138 flat fields)

Legacy controllers (`newFrameworkDevice=false`) require the full set of ~138 flat scalar
fields in every write request to `/dev/addDevMode`. Sending a partial payload results in
the omitted fields being reset to zero/default, which can turn off ports or wipe schedules.

The correct pattern is:
1. Call `getdevModeSettingList` with `devId` + `port` + auth to get the 142-field response
2. Take all 140 flat scalar fields from `data`; exclude `modeSetid` (Quirk 11), `fieldSet`
   (list), and `devSetting` (nested dict) — these cannot be form-encoded
3. Overlay the desired change
4. Send the complete merged payload (~138 fields) to `/dev/addDevMode`

Note: AI+ controllers (`newFrameworkDevice=true`) return the same 142-field structure
from `getdevModeSettingList` and benefit from the same read-before-write pattern.

---

### Quirk 14 — AI+ controllers: live write path is unknown

AI+ controllers (`newFrameworkDevice=true`, `devType=22`) use the same read-before-write
pattern and return the same 142-field structure from `getdevModeSettingList` as legacy
controllers. However, the write endpoint differs:

- `POST /dev/addDevMode` returns `{"code": 100001, "msg": "Something went wrong with your request."}` for AI+ devices — this endpoint is for legacy only.
- Phase 8 exhaustively probed 11 endpoint variants; all returned HTTP 404 except `addDevMode`.

**Current status:** AI+ `dry_run=True` is fully supported and returns the payload that
would be sent. AI+ `dry_run=False` is not yet implemented and returns a documented error.

**To discover the AI+ write endpoint:** Use mitmproxy to intercept mobile app traffic
while making a setting change on an AI+ controller. Update this quirk and implement the
branch in `client.py::set_port_mode` once discovered.

Detection:
```python
from ac_infinity_mcp.controller import ControllerType, detect_controller_type
ct = detect_controller_type(device_data)
is_ai_plus = ct == ControllerType.NEW_FRAMEWORK  # devType >= 20 or newFrameworkDevice=True
```

---

### Quirk 15 — Rate limit: 1.5s between write calls (→ 403 "Data saving failed")

The AC Infinity API enforces a minimum 1.5-second gap between write API calls. Sending
write requests faster than this returns:

```json
{"code": 403, "msg": "Data saving failed", "data": null}
```

This is enforced in `client.py` via `_enforce_write_rate_limit()`:

```python
def _enforce_write_rate_limit(self) -> None:
    elapsed = time.monotonic() - self._last_write_time
    if elapsed < 1.5:
        time.sleep(1.5 - elapsed)
    self._last_write_time = time.monotonic()
```

Read-only calls (`devInfoListAll`, `dataPage`, `getdevModeSettingList`) are not rate-limited.

---

### Quirk 16 — `getdevModeSettingList` requires `port` parameter; returns one dict per call

The `/dev/getdevModeSettingList` endpoint requires a `port` parameter (1-based integer).
Omitting `port` returns `{"code": 999999, "msg": "Operation failed, please try again"}`.
The response `data` field is a **single dict** for that port — not a list of all ports.

To read settings for all ports on a device, call the endpoint once per port:

```python
for port in range(1, port_count + 1):
    settings = get_mode_settings(dev_id, port)
    # settings is a dict with 142 fields for that port
```

The `externalPort` field in the response matches the `port` parameter sent.
Both legacy and AI+ controllers return the same 142-field structure.

Calling with `port=0` returns the controller-level settings (not any single port).

---

### Quirk 17 — ADVANCE mode (`modeType=15`) — detection and write guard

AC Infinity "Advance Automation" assigns a named program to govern one or more ports
simultaneously. From the API perspective:

**Detection fields (in `devInfoListAll` port sub-objects):**

| Field | ADVANCE port | Non-ADVANCE port | Notes |
|---|---|---|---|
| `curMode` | `1` | `1` | **Ambiguous** — same value as OFF |
| `modeTye` (note typo) | `15` | `15` | Unreliable — `15` on ALL ports |
| `isOpenAutomation` | `1` | `0` | **Reliable trigger** |
| `speak` | > 0 when running | `0` (always) | Secondary heuristic only |

**`getdevModeSettingList` for ADVANCE ports:**

| Field | Value |
|---|---|
| `modeType` | `15` |
| `atType` | `1` (OFF — NOT the effective mode) |
| `isOpenAutomation` | `1` |

**Detection strategy (in priority order):**
1. `isOpenAutomation == 1` in device list port data → ADVANCE (no secondary call needed)
2. `curMode not in _MODE_LABELS` → secondary `getdevModeSettingList` call (AI+ devices,
   future firmware codes where `curMode` may be absent or use an unmapped integer)
3. `curMode == 1 AND speak > 0` → secondary call fallback (firmware without `isOpenAutomation`)

**`_ADVANCE_MODE_TYPE = 15`** — do NOT add to `_MODE_LABELS`. If it were in `_MODE_LABELS`,
`set_port_mode(mode="ADVANCE")` would become a valid call and write `atType=15` to the
write endpoint, causing a `999999` error from the AC Infinity API.

**Write guard:** When `_set_port_mode_inner` detects `modeType == 15` in the pre-read
settings, it raises `ACInfinityAdvanceConflictError` (a typed subclass of
`ACInfinityDeviceError`). Server-side write tools catch this typed exception and return a
structured conflict response instead of an opaque error string.

**Automation grouping indicator in `devSetting.portParamData`:**
All ports governed by the same automation share identical `portParamData` values.
Ports outside automation have `0, 0` at indices 4–5 of the array; automation-grouped
ports have non-zero values (`19, 136` observed for "Moderate Airflow"). The encoding
of these values is not yet confirmed — a network capture is required to determine how
to decode the automation name or ID from this field. Document in a follow-up issue.

---

### Quirk 18 — Advance Automation API: v2.0 endpoints confirmed via network capture

The AC Infinity app manages Advance Automations (named programs that govern multiple
ports) via versioned API endpoints under the path prefix `/api/version=2.0/dev/`.
These were confirmed via mobile app network capture (Phase 17, 2026-05-22) after
REST probing of 200+ legacy-path variants returned only HTTP 404.

**Confirmed automation management endpoints (v2.0 path prefix):**

| Endpoint | Method | Body | Notes |
|---|---|---|---|
| `/api/version=2.0/dev/getGroups` | POST | `devId=...` | Returns all automation groups for device |
| `/api/version=2.0/dev/addGroups` | POST | Full form fields (~50 fields) | Creates automation; server assigns `advId` in response |
| `/api/version=2.0/dev/updateGroupsIsOn` | POST | `advId=...&isDel=0&isflag=1` | **TOGGLES** current `isOn` state — server inverts; no explicit `isOn` field |
| `/api/version=2.0/dev/delByid` | POST | `advId=...&isDel=1&isflag=1` | Deletes automation |

**Confirmed alarm management endpoints (v2.0 path prefix):**

| Endpoint | Method | Body | Notes |
|---|---|---|---|
| `/api/version=2.0/dev/getAlarms` | POST | `devId=...` | Returns alarm configurations for device |
| `/api/version=2.0/dev/addAlarms` | POST | Full form fields (~35 fields) | Creates alarm; `returnData=1` causes server to return created object |
| `/api/version=2.0/dev/updateAlarmsById` | POST | Full alarm object with `advId`, `advCode=1`, explicit `isOn` | Enable, disable, or edit alarm — `isOn` is explicit (NOT a toggle) |
| `/api/version=2.0/dev/delAlarmsByid` | POST | Full alarm object | Deletes alarm |

**Key behavioral asymmetry:**
- `updateGroupsIsOn` (automation): **TOGGLES** — must call `getGroups` first to know
  current state before deciding whether to call it.
- `updateAlarmsById` (alarm): **explicit** — send `isOn=0` to disable, `isOn=1` to enable.

**`addGroups` body fields (observed):**
`advName`, `devId`, `grouptDevType`, `currentMode`, `isOn`, `onSpeed`, `offSpeed`,
`beginTime`, `endTime`, `groupNums`, `sortType`, `subNumber`, `returnData=1`, ~50 total.
Server response includes the created object with server-assigned `advId`.

**`addAlarms` body fields (observed):**
`advName`, `devId`, `currentMode`, `isOn=1`, `advCode=0`, `highVpd`, `highVpdSwitch`,
`lowVpd`, `lowVpdSwitch`, `alertSound`, `setPort`, `switchHt`, `switchLh`, `switchHt`,
`switchLh`, `returnData=1`, ~35 total.

**`grouptDevType` is a port bitmask — Port N → 2^(N-1):**
| Port | grouptDevType |
|---|---|
| 1 | 1 |
| 2 | 2 |
| 3 | 4 |
| 4 | 8 |
| 5 | 16 |
| 6 | 32 |
| 7 | 64 |
| 8 | 128 |

Confirmed via Proxyman iOS network capture (Phase 21, 2026-05-23): port 4 → `grouptDevType=8` (=2^3), port 1 → `grouptDevType=1` (=2^0). Earlier documentation incorrectly listed these as device type codes (4=Inline fan, 8=Clip fan, 48=Mixed speed group) — those values coincidentally matched ports 3, 4, and 5+6.

**`switchTime` is a 7-bit day bitmask:**
`switchTime=127` (binary `01111111`) = all 7 days active. **Do not use `switchTime=255`** — bit 7 set causes the AC Infinity app to ignore the schedule window entirely and treat the automation as Continuous mode (always running).

**`advCode` lifecycle for `addGroups` (automation):**
`advCode` is **absent** from `addGroups` payloads — do not include it. The server assigns an `advId` and returns it in the response.

**`advCode` lifecycle for `addAlarms` (alarm):**
Send `advCode=0` on create (`addAlarms`); server returns `advCode=1`.
All subsequent alarm calls (`updateAlarmsById`, `delAlarmsByid`) send `advCode=1`.

**VPD units in alarm fields:**
`highVpd=50` means 5.0 kPa — divide by 10 for display (same scaling factor as `targetVpd`
in mode settings, not the ÷100 used for live sensor `vpdnums`).

**Alert sound:**
`alertSound=255` = controller beep enabled; `alertSound=0` = silent.

---

### Quirk 19 — `isOpenAutomation` is the authoritative ADVANCE guard; `modeType=15` alone is not sufficient

This quirk extends Quirk 17 with a critical fix for the false-positive ADVANCE conflict
detected in issue #63.

**Background:** When an Advance Automation is disabled (via `disable_advance_automation` or
in the app), the controller does **not** reset `modeType` in `getdevModeSettingList`. The
`modeType=15` marker persists even after the automation is fully disabled — it is a static
configuration marker, not a live-state signal.

**The authoritative live-state field is `isOpenAutomation`:**

| Context | Field | Meaning |
|---|---|---|
| `devInfoListAll` port sub-objects | `isOpenAutomation` | `1` = automation currently active; `0` = disabled |
| `getdevModeSettingList` response | `isOpenAutomation` | Same meaning — present in both responses |
| `getdevModeSettingList` response | `modeType` | `15` = port is configured for ADVANCE, but may or may not be actively running |

**Correct guard conditions (both must be satisfied to block a write):**

```python
# In client.py _set_port_mode_inner — read from getdevModeSettingList
if mode_type == 15 and current_settings.get("isOpenAutomation", 1) != 0:
    raise ACInfinityAdvanceConflictError(...)

# In server.py _check_advance_mode — read from devInfoListAll port data
return "ADVANCE" if (
    settings.get("modeType") == _ADVANCE_MODE_TYPE
    and settings.get("isOpenAutomation", 1) != 0
) else fallback
```

**Safe-fail default:** When the `isOpenAutomation` field is absent from the API response
(future firmware may omit it), both guards default to `1` (treat as active). This is the
safe conservative direction — it prevents a write to a possibly-governed port rather than
silently overriding an automation.

**Before this fix (Phase 19, PR #67):** Any port with `modeType=15` would trigger the
ADVANCE conflict guard even after the automation was disabled, making it impossible to
manually control ports on a controller that ever had an automation. After the fix, the
guard only fires when the automation is confirmed active (`isOpenAutomation != 0`).

**Ghost state in `break_out_of_automation` (issue #191, PR #233):** When `modeType=15`
is set on a port but no active automation's `grouptDevType` bitmask covers that port
(stale configuration marker from a deleted or fully-disabled automation), the tool now
returns an idempotent info response rather than an error. The port is not under active
automation control — the `modeType=15` flag is a historical artifact and no write-guard
should block manual control of the port.

---

### Quirk 20 — Phantom external sensor entries in `devInfoListAll`

The AC Infinity API includes sensor slot entries in the `deviceInfo.sensors` array even
when no physical sensor is connected to a UIS port. These phantom entries have a non-null
`sensorType` integer and a `sensorData` value of `0` or `null`. Including them in the
`external_sensors` response would cause growers to see sensors they don't own.

**Filtering rule applied in `parse_device_data` (`client.py`):**

| Condition | Action |
|---|---|
| `sensorType` matches a recognized type (10–20 per `_SENSOR_TYPE_LABELS`) | **Always include** — even if current value is `0` (sensor may be connected but reading zero) |
| `sensorType < 10` (not in label dict) | **Always exclude** — internal/built-in bus readings, not external hardware |
| `sensorType >= 10` and unrecognized (future/unknown type) | Include **only if** `sensorData != 0` — zero value on an unknown type is treated as phantom |
| `sensorType` is `null` | **Always exclude** — no type means no sensor slot at all |

**Implementation:**

```python
def _should_include_sensor(s: dict) -> bool:
    sensor_type = s.get("sensorType")
    if sensor_type is None:
        return False
    if sensor_type in _SENSOR_TYPE_LABELS:
        return True  # recognized external sensor type (10–20): always include
    try:
        if int(sensor_type) < 10:
            return False  # types 1–9 are internal/built-in readings, not external hardware
    except (ValueError, TypeError):
        return False
    return (s.get("sensorData") or 0) != 0  # unrecognized high type: include if non-zero
```

This means `external_sensors` will be `[]` on a controller with no sensors plugged in,
regardless of how many phantom slot entries the API returns.

**devType=22 addendum (confirmed via Proxyman capture 2026-05-28):** On devType=22
(UIS CONTROLLER 69 PRO+), the API returns phantom sensor entries with
`sensorType` values of 4, 6, and 7 — all with non-zero `sensorData` values and
`accessPort: 7`. These are internal bus readings, not physically-connected
sensors. Since all real AC Infinity external sensors (soil probes, CO2, light,
pH, EC, TDS, water probes) use types 10–20, any entry with `sensorType < 10` that
is not in `_SENSOR_TYPE_LABELS` is treated as internal and filtered. The
zero-value filter alone is insufficient for devType=22.

---

### Quirk 21 — `onTimeSwitch` field controls schedule mode

The Advance Automation API returns an `onTimeSwitch` field per group entry. It maps to the
**"Continuous 24 Hours / 7 Days"** toggle in the AC Infinity app:

- `onTimeSwitch = 0` — toggle **OFF**: the time window applies. The automation is scheduled
  and only active between `beginTime` and `endTime`. Both values must be real (non-sentinel)
  for the schedule to be shown; if either is `255` (sentinel), treat as continuous.
- `onTimeSwitch = 1` — toggle **ON**: runs 24/7 regardless of `beginTime`/`endTime` values.
  Always treated as continuous.
- Missing field defaults to `0` (scheduled if real times present, else continuous).
- Values other than `0` and `1` are treated as continuous (safe fallback).

**Important:** The mapping is the opposite of what the field name implies. A value of `0`
(switch "off") means the time-window restriction is in effect (scheduled). See
`_group_automations()` — `on_time_switch` key.

### Quirk 22 — Ghost port filtering and toggle-device data quality in `get_port_activity_report`

The history API returns data for all ports on a controller, including ports with no device
attached. These phantom ports produce misleading activity data. Six filter/caveat rules are
applied by `build_activity_report`:

**Ghost-port exclusion rules (port removed from response):**

- **Rule A**: A port is excluded when ALL of: `transitions == 0`, `uptime_pct == 100.0`,
  `port_loads` is provided (not `None`), and `portsLoad == 0`. Requires the supplementary
  `get_devices` call to succeed; if it fails, Rule A is disabled and the port is kept.
- **Rule B** (enhanced, fixes #89): A port is excluded when its name matches `^Port \d+$` AND either
  (a) its average on-time is < 1 h/day, OR (b) `portsLoad == 0` when port_loads data is available.
  The portsLoad guard prevents Rule B from missing phantom mirror ports at short windows (e.g.
  days=1) where phantom activity exceeds the 1 h/day threshold.
- **Rule C** (fixes #88): A named port (not matching `^Port \d+$`) is excluded when
  `port_loads` is provided AND `portsLoad == 0` AND average on-time is < 1 h/day AND
  `transitions == 0`. Catches physically-disconnected named devices with no recorded transitions.
- **Rule D** (ghost filter, fixes #101 partial): A named port is excluded when `port_loads` is
  provided AND `portsLoad == 0` AND `avg_speed_when_running <= 1.0`. Catches toggle-hardware
  ghost artifacts where the history API emits speed=1 even when the device is physically off.
  Toggle devices that are genuinely connected are exempted via the `data_quality` early-exit
  (see caveat rule below) before this filter evaluates.
- **Rule E** (fixes #101): A named port with `transitions > 0`, `avg_speed_when_running > 1.0`,
  `portsLoad == 0`, and average on-time < 1 h/day is excluded. Closes the gap where the
  history API records a port's previously-configured speed (e.g. speed=5) even after it is
  set to OFF — producing phantom records with non-zero avg_speed and spurious transitions that
  pass Rules A–D. The `avg_speed > 1.0` condition ensures Rule E does not overlap with Rule D
  (toggle-speed devices).
- **Rule F** (phantom clone detection, fixes #139): A custom-named port is excluded when it
  shares an identical activity signature `(uptime_pct, transitions, peak_hour_utc)` with one
  or more other custom-named ports AND all matching ports have average on-time < 1 h/day
  (`_GHOST_LOAD_ZERO_THRESHOLD`). Fires only when `port_loads` is provided (requires
  supplementary `get_devices` call). A proper-subset guard prevents excluding every port
  — at least one non-matching port must remain. Targets phantom history artifacts on legacy
  controllers (devType=11) where disconnected ports are cloned at identical low-activity
  levels. Only fires when `port_loads is not None` (disabled on devType=18/22 zero-load devices).
- **Rule G** (fixes #142/#143): A custom-named port on a devType=18/22 device
  (`_ZERO_LOAD_DEV_TYPES = frozenset({18, 22})`) is excluded when `avg_speed_when_running == 1.0`
  AND `on_hours / days < _GHOST_LOAD_ZERO_THRESHOLD` (1.0 h/day). This rule fires after the
  `api_constant_speed` early-exit (which retains toggle hardware) and after Rule F, before Rule A.
  It targets zero-load controller ghosts that the api_constant_speed path would skip (speed=1 but
  no toggle-hardware classification available) and that Rule F cannot catch (Rule F is disabled
  when `port_loads is None`). Because `port_loads` is always forced `None` on devType=18/22,
  Rule G provides the only ghost-exclusion path for these devices.

Rule A and the `data_quality` caveat rule (below) both exempt toggle hardware (loadType 4 or
128) — toggle devices are never ghost-filtered regardless of uptime or portsLoad, because they
appear as always-on in history. A toggle device excluded by Rule A would silently vanish from
the report even though it is physically connected.

**Transition debouncing (fixes #112, `_MIN_DWELL_READINGS = 2`):**

The `transitions` count uses `_count_debounced_transitions()` to filter out single-reading
state changes. A state change is only counted when the new state persists for at least
`_MIN_DWELL_READINGS` (2) consecutive readings. Single-reading blips at automation window
boundaries are API artifacts — the history API occasionally emits one record with a
different nibble value at the edge of a scheduled automation window, creating a phantom
on→off→on sequence that would inflate `transitions` and corrupt `peak_hour_local` if not
filtered. After debouncing, only genuine sustained state changes (fan turning on and staying
on for ≥ 2 readings) are counted.

**Data-quality caveat rule (port kept, flagged):**

- **Data-quality caveat** (fixes #85, formerly labelled Rule D in code comments): A named port
  is kept in the response but flagged with `data_quality = "api_constant_speed"` when ALL of:
  `transitions == 0`, `uptime_pct == 100.0`, `avg_speed_when_running == 1.0`, AND the port's
  `loadType` is 4 or 128 (toggle hardware — heaters, lights, humidifiers). The AC Infinity
  history API records toggle devices as always-on at speed 1 regardless of actual runtime;
  `on_hours` and `uptime_pct` for these ports are fabricated and must not be presented as real
  runtime data. The `human_summary` includes a plain-English caveat for each flagged port. The
  `port_load_types` parameter (from `deviceInfo.ports[].loadType`) flows from
  `get_port_activity_report` through `build_activity_report` to enable this detection. When
  `port_load_types` is absent, the caveat rule is disabled and the port is reported without a
  caveat.

**When supplementary call fails:**

When `port_loads` is `None` (supplementary `get_devices` call failed), Rules A, B (portsLoad
guard), C, D, E, and the toggle-exemption in the data-quality caveat rule are all disabled —
the report still returns but without ghost filtering.

**Known limitation (Rules C and E):** A named device averaging < 1 h/day that draws zero
current at query time will be filtered. Example: a misting pump running 20 min/day queried
while off. Growers with low-duty named devices that disappear from the report should verify
with `get_port_status`. The exclusion message "no load or activity detected at time of report"
is accurate — it reflects the zero-current-draw state at query time, not a permanent device state.

The response includes `ports_excluded_count` (integer count of filtered ports) and
`human_summary` (plain-English summary for growers). When `ports_excluded_count > 0`, the
`human_summary` already contains a note about excluded ports — do not repeat the count in prose.

### Quirk 23 — Timezone-aware and unit-aware responses

All grower-facing temperature values and timestamps are localised using two fields from the
device record:

- **`zoneId`** (top-level string) — IANA timezone identifier, e.g. `"America/Chicago"`.
  Used to convert UTC timestamps to local time in `get_device_reading`,
  `get_historical_readings`, `get_port_activity_report`, and `detect_environment_trends`.
  Also used to provide the `"timezone"` key in `get_port_settings.schedule_window`.
  Falls back to UTC when the field is absent or contains an unrecognized zone string.

- **`deviceInfo.unit`** (integer) — temperature unit preference: `0` = °F, `1` = °C.
  Used by `get_device_reading`, `get_historical_readings`, `get_port_settings`,
  `set_temperature_automation`, and `apply_grow_stage_template` to display or accept
  temperatures in the grower's preferred unit. Falls back to °C when the field is absent.

**Impact on tool output fields:**

| Old field name | New field name | Notes |
|---|---|---|
| `temperature_c` | `temperature` | Value in preferred unit; `unit` field added |
| `temperature_c` statistics key | `temperature` | In `get_historical_readings` statistics |
| `temp_range_c` | `temp_range` | `{"min": N, "max": N, "unit": "°C"/"°F"}` |
| `peak_hour_utc` | `peak_hour_local` | Local time with peak date, e.g. "4:00 PM CDT (peak on May 20)"; uses `astimezone()` for DST-aware conversion including sub-hour offsets (UTC+5:30) |
| `min_c` / `max_c` parameters | `min_temp` / `max_temp` | `set_temperature_automation` |
| `schedule_window` | `schedule_window` | Added `"timezone"` key |

**No impact on write encoding:** The API always stores temperature as raw °C integers. The
MCP server converts °F inputs to °C before writing. `detect_environment_trends` trend
metrics use `"temperature"` as the metric key (matching the read-side field name).

---

### Quirk 24 — devType=18 (`69 Pro+`) and devType=22 (`Q0KT4`) always report `portsLoad=0`/`None`

Devices with `devType=18` (UIS Controller 69 Pro+) return `portsLoad=0` for all ports in
`devInfoListAll` regardless of actual device load state. Devices with `devType=22` (Q0KT4
Genetics Lab) return `portsLoad=None` for all ports (converted to 0 via `or 0` in the
server). Both are firmware reporting gaps — these controllers do not populate the load field.

**Impact on `get_port_activity_report`:**

All five load-based ghost-port rules (A, B-portsLoad guard, C, D, E) use `portsLoad` to
confirm a port has no physical device connected. On devType=18 and devType=22, these rules
are disabled by forcing `port_loads=None` for the device — otherwise, every port would be
filtered out as a "ghost" even when devices are physically connected and actively running.

Toggle-hardware detection (data-quality caveat path) on these device types uses pattern
alone: `transitions == 0` AND `uptime_pct == 100.0` AND all running speeds == 1. The
`loadType`-based confirmation is also skipped for devType=18 and devType=22 because
`loadType` is similarly unreliable on these devices (Issue #126).

**Note behavior (fixes #136, updated #151):** A device-level Note about missing load data is
emitted in `human_summary` whenever the result is non-empty and the device is **devType=22
only** — regardless of whether any port has the `api_constant_speed` caveat. The Note text
reads: "This controller does not report power draw for individual ports. ON/OFF state is the
only reliable activity indicator — history-based runtime data is not available for this
controller type."

devType=18 (UIS 69 Pro+) no longer emits this Note. Active ports on devType=18 produce real
runtime data in the historical records — `on_hours` and `uptime_pct` reflect genuine
activity, making the Note misleading when shown alongside those figures. For devType=18,
runtime data is reliable even though `portsLoad` is always 0 (the load field is simply not
populated by that firmware). The implementation guard changed from
`if dev_type in _ZERO_LOAD_DEV_TYPES:` to `if dev_type == 22:` for the Note emission path.

**Detection:**

```python
_ZERO_LOAD_DEV_TYPES = frozenset({18, 22})
if device.get("devType") in _ZERO_LOAD_DEV_TYPES:
    port_loads = None  # bypass all load-based ghost rules
```

**Known limitation:** Without a load signal, a briefly-run port (transitions > 0) on a
devType=18 or devType=22 device cannot be reliably distinguished from phantom API artifact
activity. The only available filter is the pattern detector, which requires `transitions == 0`.

---

### Quirk 25 — Legacy firmware (devType=11) may return unreliable `modeType` from `getdevModeSettingList` for ADVANCE-mode ports

**Background:** When a port is under Advance Automation control on legacy controllers
(e.g. C58ZA, devType=11, firmware 3.2.56), the `getdevModeSettingList` endpoint may
return `modeType != 15` even though the port is actively governed by an automation.
This caused the primary ADVANCE conflict guard in `_set_port_mode_inner` to miss the
conflict and fall through to the write, which then failed with the generic
`ACInfinityAPIError` path rather than the structured `ACInfinityAdvanceConflictError`.

**Discovery:** `get_port_status` correctly identified the port as ADVANCE because it
reads `isOpenAutomation` from `devInfoListAll` (not `getdevModeSettingList`). This
confirmed that `devInfoListAll` is the reliable source for automation state on legacy firmware.

**Fix — two-layer guard:**

1. **Pre-write guard (primary):** Before calling `get_mode_settings`, check the port's
   `isOpenAutomation` field from `device_data["deviceInfo"]["ports"][N]`. If `isOpenAutomation == 1`,
   raise `ACInfinityAdvanceConflictError` immediately — before the unreliable
   `getdevModeSettingList` call can return misleading data.
   - Safe-fail: absent `isOpenAutomation` key treated as `0` (not active) — falls through
     to the secondary `getdevModeSettingList` check which has its own safe-fail of `1`.

2. **999999 fallback (defense-in-depth):** In the write response loop, if the API returns
   `code == 999999` (the legacy API's "blocked by active automation" sentinel), raise
   `ACInfinityAdvanceConflictError`. This catches the case where both guards missed the
   conflict (e.g. race condition between guard and write, or firmware variation not yet observed).

**Code location:** `client._set_port_mode_inner`, before the `get_mode_settings` call
and in the post-write response loop.

---

### Quirk 26 — Empty-port detection: `portResistance == 65535` (primary) + name/load heuristic (fallback)

**Background (issues #165, #183):** When a user asks to control or inspect a port that has nothing
plugged in, the server previously responded with a confident action (write) or settings read
with no indication that the target was empty. This caused confusion when users misidentified a
port number.

**Primary signal (Quirk 27 — firmware that supplies `portResistance`):**
`portResistance == 65535` (0xFFFF) in `devInfoListAll.deviceInfo.ports`. The controller measures
electrical resistance across each port; 65535 is the uint16 open-circuit sentinel meaning nothing
is connected. Connected devices — even in OFF mode — present real values (e.g. 400 Ω light,
7500 Ω fan, 15800 Ω heater). When `portResistance` is present and is **not** 65535, the port is
treated as connected regardless of port name or `portsLoad`.

**Known tradeoff (user-approved 2026-05-26):** LED grow lights with their own inline power
switches may read `portResistance=65535` when that switch is off but the device is still
physically plugged in. Passive loads (heaters, fans with AC motors) are not affected — their
resistance is measurable regardless of a device-level switch.

**Fallback signal (old firmware that omits `portResistance`):**
When `portResistance` is absent from the API response, the legacy dual-signal heuristic applies:
1. The `portName` matches the API-default pattern `"Port N"` (i.e. the grower has not custom-named it), AND
2. `portsLoad == 0` (no power draw detected), OR the device `devType` is in `{18, 22}` — see Quirk 24.

Custom-named ports are assumed connected in the fallback path. If a grower named a port,
something is plugged in.

**devType=18 and devType=22 exception (fallback path only):** Because `portsLoad` is always `0`
on these devices (Quirk 24), the fallback detection relies solely on the default-name signal.
A default-named port on a devType=18 (8T4TC) or devType=22 (Q0KT4) device is always flagged
as possibly empty when `portResistance` is absent.

**Affected tools:**
- **Write tools** (7): `set_port_on`, `set_port_off`, `set_port_speed`, `set_port_mode`,
  `set_vpd_automation`, `set_temperature_automation`, `set_humidity_automation` — add `warning` field
- **Read tools** (2): `get_port_status`, `get_port_settings` — add `note` field
- **Excluded:** `get_port_activity_report` already has its own ghost-port filter (Rules A–G)

**Behaviour:** The warning/note is **advisory only** — it does not block writes (including
live writes). The grower is shown the advisory and can confirm or redirect.

**Code location:** `server._is_port_empty()` helper; `_PORT_EMPTY_RESISTANCE = 65535` constant.
Called after the read-before-write fetch in each affected tool.

---

### Quirk 27 — `portResistance` field: hardware open-circuit sentinel

**Field location:** `devInfoListAll.deviceInfo.ports[N].portResistance`

**What it is:** The AC Infinity controller continuously measures electrical resistance across
each port outlet. The value is a uint16 integer representing the measured resistance in ohms.

**Sentinel value:** `65535` (0xFFFF) — the maximum uint16 value, used as the open-circuit
sentinel. When the server reads 65535, the hardware found no measurable resistance path,
meaning nothing is electrically connected to that port.

**Connected device examples (from Proxyman capture 2026-05-26):**
- LED grow light (with inline switch ON): ~400 Ω
- Inline fan: ~7500 Ω
- Heater: ~15800 Ω

**Firmware availability:** Not all firmware versions include this field. When `portResistance`
is absent from the port object, the server falls back to the name/load heuristic (Quirk 26
fallback). Always treat absence as "unknown" — never as 0 Ω (short circuit).

**Code constant:** `_PORT_EMPTY_RESISTANCE: int = 65535` in `server.py`.

---

### Quirk 28 — `sensorPrecision` is a decimal-place exponent, not a literal divisor

**Field location:** `devInfoListAll.deviceInfo.sensors[N].sensorPrecision`

**What it is:** `sensorPrecision` is the number of decimal places encoded into the integer
`sensorData`, exactly like Python's `round()` precision argument. The real-world value is:

```
value = sensorData / 10 ** (sensorPrecision - 1)
```

A precision of `1` (or a missing/zero field) means `sensorData` is already the real value and
is returned **as-is** — no division, no spurious float (CO2 `793` stays `793`, not `793.0`).

| `sensorPrecision` | Real value | Example |
|---|---|---|
| absent / 0 / 1 | `sensorData` (raw passthrough) | CO2: `793` → `793` ppm |
| 2 | `sensorData / 10` | pH: `65` → `6.5` |
| 3 | `sensorData / 100` | temp: `2450` → `24.5` °C |
| 4 | `sensorData / 1000` | (3-decimal sensors) |

**Why it matters:** The original implementation divided by `sensorPrecision or 100` (a literal
divisor). That happened to be correct only for CO2 (`sensorType` 11, precision 1, where both
formulas yield the raw value), which masked the bug. Every other external sensor — light, pH,
EC, TDS, water temperature — was mis-scaled. The most visible case is the **light sensor
(`sensorType` 12), a 0–100% reading** (`device_class power_factor`, unit `%` per the AC Infinity
app and the HA `ac_infinity` integration): at precision 2 the old formula reported `1000 / 2 =
500%`, an impossible value, instead of `1000 / 10 = 100%`.

**Confirmed against:** the AC Infinity official app and the open-source HA `ac_infinity`
integration (`custom_components/ac_infinity/sensor.py`,
`__get_value_fn_sensor_value_default`), which reads the same `devInfoListAll` `sensors` array.

**Implementation:**

```python
_MAX_SENSOR_PRECISION = 6  # real values are 1-3; anything well above is malformed

def _sensor_value(s: dict) -> float | int:
    data = s.get("sensorData") or 0
    precision = s.get("sensorPrecision")
    if precision is None:
        precision = 1
    if precision > _MAX_SENSOR_PRECISION:
        # Implausible precision (malformed response) would yield a silent
        # near-zero reading; log it and treat the value as raw instead.
        precision = 1
    return data / (10 ** (precision - 1)) if precision > 1 else data
```

**Out of scope (tracked separately):** `sensorUnit` is present on every sensor entry but not
yet read — readings are emitted without unit labels. See issue #255.

---

## v2.0 API Endpoints Reference

All endpoints below use the base URL `https://www.acinfinityserver.com/api` and require
`Content-Type: application/x-www-form-urlencoded; charset=utf-8` plus `token: <appId>` header.
All use HTTPS (TLSv1.3 — see Quirk 8).

### Automation Management

| Endpoint | Method | Request body | Response notes |
|---|---|---|---|
| `/api/version=2.0/dev/getGroups` | POST | `devId=<devId>` | Returns list of automation group objects; each has `advId`, `advName`, `isOn`, `onSpeed`, `offSpeed`, etc. |
| `/api/version=2.0/dev/addGroups` | POST | Full form (~50 fields): `advName`, `devId`, `grouptDevType`, `currentMode`, `isOn`, `onSpeed`, `offSpeed`, `beginTime`, `endTime`, `groupNums`, `sortType`, `subNumber`, `returnData=1`, + others | Returns created automation object with server-assigned `advId` |
| `/api/version=2.0/dev/updateGroupsIsOn` | POST | `advId=<id>&isDel=0&isflag=1` | Toggles `isOn` state server-side; no explicit `isOn` field in body |
| `/api/version=2.0/dev/delByid` | POST | `advId=<id>&isDel=1&isflag=1` | Deletes automation by `advId` |

### Alarm Management

| Endpoint | Method | Request body | Response notes |
|---|---|---|---|
| `/api/version=2.0/dev/getAlarms` | POST | `devId=<devId>` | Returns list of alarm objects; each has `advId`, `advName`, `isOn`, `advCode`, VPD/temp/humidity thresholds |
| `/api/version=2.0/dev/addAlarms` | POST | Full form (~35 fields): `advName`, `devId`, `currentMode`, `isOn=1`, `advCode=0`, `highVpd`, `highVpdSwitch`, `lowVpd`, `lowVpdSwitch`, `alertSound`, `setPort`, switch fields, `returnData=1`, + others | Returns created alarm object |
| `/api/version=2.0/dev/updateAlarmsById` | POST | Full alarm object with `advId`, `advCode=1`, explicit `isOn=0` or `isOn=1` | Enable, disable, or edit alarm; `isOn` is explicit (not a toggle) |
| `/api/version=2.0/dev/delAlarmsByid` | POST | Full alarm object | Deletes alarm |

### History

| Endpoint | Method | Request body / query | Response notes |
|---|---|---|---|
| `/api/log/logdataByAll` | POST | `appId=<token>&devId=<devId>&endTime=<unix>&id=0&orderDirection=1&pageNum=0&pageSize=1000&time=<unix>` | Returns historical readings; `validFrom` in response marks oldest available record. Previously thought broken — confirmed working. |
| `/api/log/log?devId=<devId>&time=<unix>` | DELETE | No body | Deletes all history logs for device; `time` is current Unix timestamp. After deletion `logdataByAll` returns `validFrom` = deletion timestamp. |

### Grow Stage Templates

| Endpoint | Method | Query params | Response notes |
|---|---|---|---|
| `/api/version=2.0/dev/recipe?advVersion=1` | GET | None | Returns grow stage templates: Seedling, Vegetative, Flowering, Plant Kit, Drying |

### Additional Legacy-Path Endpoints

| Endpoint | Method | Request body | Response notes |
|---|---|---|---|
| `/api/dev/getDevSetting` | POST | `devId=<devId>&port=<N>` | Richer port settings than `getdevModeSettingList`; includes sensor calibration, load type, plant data, Matter/UUID fields, `portParamData` |
| `/api/upgrade/getUpgrade` | POST | `fFamily=<family>&firmwareVersion=<ver>&hardwareVersion=<ver>` | Firmware upgrade check |
| `/api/upgrade/downgrade` | POST | `devMacAddr=<mac>&fFamily=<family>&firmwareVersion=<ver>&hardwareVersion=<ver>` | Firmware downgrade info; returns download URL and release notes |

---

## MCP Tool Reference

This section documents the MCP tool interfaces — parameters, return schemas, and encoding
notes. All tools return JSON strings. On failure every tool returns `{"error": "...", "detail": "..."}`.

---

### `discover_devices()`

List all AC Infinity devices on the account with their metadata.

**Parameters:** None.

**Response (1 device):**
```json
{
  "devices": [
    {
      "device_id": "C58ZA",
      "device_name": "Towlie Tent",
      "status": "online",
      "device_type": 11,
      "port_count": 8,
      "firmware_version": "3.2.56",
      "hardware_version": "1.1"
    }
  ],
  "human_summary": "1 device found: Towlie Tent (C58ZA, online)."
}
```

**Response (3+ devices — markdown table):**
```json
{
  "devices": [...],
  "human_summary": "| Device | ID | Status |\n|---|---|---|\n| Towlie Tent | C58ZA | online |\n| Veg Tent | D91XB | online |\n| Clone Chamber | F03KR | online |"
}
```

**Field notes:**
- `device_id` — the `devCode` value; used as `device_id` in all other tools (Quirk 7)
- `status` — `"online"` or `"offline"` from the `online` bitmask field
- `device_type` — `11` = legacy 69 Pro / 69 Pro+, `22` = AI+ 89 AI+
- `human_summary` — one-line prose for 1–2 devices; markdown table for 3+ devices; `"No devices found."` when the account is empty
- Empty account: `{"devices": [], "message": "No devices found"}`

---

### `get_device_reading(device_id)`

Get current sensor readings (temp, humidity, VPD) and port states for one device.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |

**Response (port running):**
```json
{
  "timestamp": "2026-05-20T09:32:00-05:00",
  "device_id": "C58ZA",
  "device_name": "Towlie Tent",
  "temperature": 24.3,
  "unit": "°C",
  "humidity": 58.2,
  "vpd": 1.31,
  "ports": [
    {"port": 1, "name": "Inline Fan", "speed": 5}
  ],
  "external_sensors": [],
  "human_summary": "Towlie Tent: 24.3°C, 58.2% RH, VPD 1.31 kPa. Reading from 2026-05-20T09:32:00-05:00."
}
```

**Response (port not powered):**
```json
{
  "timestamp": "2026-05-20T09:32:00-05:00",
  "device_id": "C58ZA",
  "device_name": "Towlie Tent",
  "temperature": 24.3,
  "unit": "°C",
  "humidity": 58.2,
  "vpd": 1.31,
  "ports": [
    {"port": 1, "name": "Inline Fan", "speed": 5},
    {"port": 2, "name": "Port 2", "speed": 0, "plug_status": "not powered"}
  ],
  "external_sensors": [],
  "human_summary": "Towlie Tent: 24.3°C, 58.2% RH, VPD 1.31 kPa. Reading from 2026-05-20T09:32:00-05:00."
}
```

**Field notes:**
- `temperature` — current temperature in the device's preferred unit (`deviceInfo.unit`); decoded from raw API value ÷ 100 (Quirk 4, Quirk 23)
- `unit` — `"°C"` or `"°F"` matching `deviceInfo.unit`; falls back to `"°C"` when the field is absent (Quirk 23)
- `timestamp` — ISO 8601 in device local time with UTC offset (from `zoneId`); falls back to UTC `"Z"` suffix when `zoneId` is absent (Quirk 23)
- `vpd` — decoded from `vpdnums ÷ 100` (Quirk 4, Quirk 10)
- `ports[].speed` — current port speed 0–10 from `speak` field
- `ports[].plug_status` *(conditional)* — `"not powered"` when `loadState == 0` AND `speak == 0` AND the port still has its **default name** (`"Port N"`). Custom-named ports are excluded — a user-assigned name implies a device was intentionally connected, and `loadState=0` alone cannot distinguish "nothing plugged in" from "device is off" for on/off devices (see Quirk 26). **Omitted entirely** otherwise. Matches the identical signal in `get_port_status`.
- `external_sensors` — list of UIS sensor readings when sensors are attached; phantom entries (API-reported but no hardware connected) are filtered out (Quirk 20); empty `[]` for built-in-only devices
- `human_summary` — one-line natural language summary: `"DeviceName: N°U, N% RH, VPD N kPa. Reading from <timestamp>."` Always present.

---

### `get_all_device_readings()`

Get current sensor readings for all devices at once.

**Parameters:** None.

**Response:**
```json
{
  "readings": [
    {
      "device_id": "C58ZA",
      "device_name": "Towlie Tent",
      "temperature": 24.3,
      "unit": "°C",
      "humidity": 58.2,
      "vpd": 1.31,
      "ports": [...],
      "external_sensors": []
    }
  ]
}
```

**Field notes:**
- Each entry has the same shape as `get_device_reading` (including `temperature` / `unit` in device-preferred units)
- `ports[].plug_status` — present on not-powered, default-named (`"Port N"`) port entries (same `loadState == 0` AND `speak == 0` AND default-name condition as `get_device_reading`); omitted for custom-named ports or running ports
- Devices that fail to parse individually include `"error"` instead of sensor fields
- Useful for a dashboard view across multiple tents/controllers
- `external_sensors` — phantom sensor entries (sensors present in the API response but with no hardware connected) are filtered out; see Quirk 20
- `human_summary` — one-line prose for 1–2 parseable devices; markdown table (`| Device | Temp | Humidity | VPD |`) for 3+ parseable devices; `"No readings available."` when all fail. Always present at the top level.

---

### `get_historical_readings(device_id, start_date, end_date, sample_interval="1h", time_start=None, time_end=None)`

Query historical environment data with configurable bucketing and optional time-of-day filtering.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `start_date` | `str` | Start date `YYYY-MM-DD` |
| `end_date` | `str` | End date `YYYY-MM-DD` |
| `sample_interval` | `str` | Bucket size. `"raw"` = all records; or `"1m"`, `"5m"`, `"15m"`, `"30m"`, `"1h"`, `"2h"`, `"6h"`, `"12h"`, `"1d"` / `"daily"`. Default: `"1h"` |
| `time_start` | `str \| None` | UTC time filter `"HH:MM"` — only return readings at or after this time |
| `time_end` | `str \| None` | UTC time filter `"HH:MM"` — only return readings at or before this time. When `time_start > time_end`, the window crosses midnight |

**Response:**
```json
{
  "device_id": "C58ZA",
  "readings": [
    {
      "timestamp": "2026-05-20T09:00:00-05:00",
      "temperature": 24.1,
      "unit": "°C",
      "humidity": 58.0,
      "vpd": 1.30,
      "ports": [{"port": 1, "name": "Inline Fan", "speed": 5}]
    }
  ],
  "statistics": {
    "readings_count": 168,
    "sample_interval": "1h",
    "date_range": {"start": "2026-05-13", "end": "2026-05-20"},
    "temperature": {"min": 20.1, "avg": 23.8, "max": 27.4},
    "humidity": {"min": 52.0, "avg": 58.2, "max": 65.1},
    "vpd": {"min": 1.01, "avg": 1.28, "max": 1.72},
    "port_statistics": {
      "Inline Fan": {"min": 0, "avg": 4.8, "max": 10}
    }
  }
}
```

**Field notes:**
- History API caps at ~1,257 records/day regardless of page size (Quirk 9)
- `"dropped_readings"` and `"drop_reason"` keys appear when records had unparseable timestamps
- `port_statistics` only includes ports that were on (speed > 0) at least once in the window

---

### `check_vpd_drift(device_id, stage="veg")`

Check whether current VPD is within the target range for a named grow stage.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `stage` | `str` | One of: `clones`, `seedling`, `veg`, `early_flower`, `mid_flower`, `late_flower`. Default: `veg` |

**Response:**
```json
{
  "device_id": "C58ZA",
  "current_vpd": 1.58,
  "target_range": [1.0, 1.5],
  "stage": "veg",
  "status": "HIGH",
  "deviation": 0.08,
  "alert": "VPD 1.58 exceeds target 1.00–1.50. Raise humidity or lower temperature.",
  "human_summary": "VPD 1.58 exceeds target 1.00–1.50. Raise humidity or lower temperature."
}
```

**Field notes:**
- `status` — `"OK"`, `"LOW"`, or `"HIGH"`
- `deviation` — `0` when OK; positive kPa when HIGH (above upper bound); negative when LOW
- `alert` — `null` when status is `"OK"`
- `human_summary` — mirrors `alert` when status is not OK; `"VPD is on target at N kPa (target L–H kPa for stage)."` when OK. Always present.

---

### `get_environment_health(device_id, stage="veg")`

Calculate a composite health score (0–100, A–F grade) across temp, humidity, and VPD,
including the actual sensor readings that produced the score.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `stage` | `str` | One of: `clones`, `seedling`, `veg`, `early_flower`, `mid_flower`, `late_flower`. Default: `veg` |

**Response:**
```json
{
  "device_id": "C58ZA",
  "stage": "veg",
  "score": 82,
  "grade": "B",
  "top_recommendation": "VPD slightly high — increase humidity or lower temperature.",
  "vpd_score": 70,
  "temp_score": 100,
  "humidity_score": 85,
  "temperature_c": 24.7,
  "temperature_f": 76.5,
  "humidity_pct": 65.0,
  "vpd_kpa": 1.24,
  "human_summary": "Temperature 76.5°F (24.7°C), humidity 65%, VPD 1.24 kPa. Overall health: B (82.0/100)."
}
```

**Field notes:**
- Composite score weights: VPD 40%, temperature 30%, humidity 30%
- Grade bands: A=90–100, B=80–89, C=70–79, D=60–69, F=0–59
- `top_recommendation` — top actionable suggestion based on the lowest sub-score
- `temperature_c` / `temperature_f` — current sensor reading in Celsius and Fahrenheit
- `humidity_pct` — current relative humidity percentage
- `vpd_kpa` — current vapour-pressure deficit in kPa
- `human_summary` — one-line natural language summary of readings and overall grade

---

### `detect_environment_trends(device_id, days=7)`

Detect linear trends in temperature, humidity, and VPD with a 7-day projection.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `days` | `int` | Look-back window in days (1–30). Default: 7 |

**Response:**
```json
{
  "device_id": "C58ZA",
  "days_analyzed": 7,
  "readings_used": 168,
  "trends": [
    {
      "metric": "temperature",
      "slope_per_hour": 0.03,
      "direction": "rising",
      "projection_7d": 25.1,
      "alert": false
    }
  ],
  "human_summary": "| Metric | Direction | Slope | 7-Day Projection |\n|---|---|---|---|\n| Temperature | ↑ Rising | +0.0300 °C/hr | 25.1 °C |\n| Humidity | → Stable | +0.0001 /hr | 58.3  |\n| Vpd | → Stable | +0.0000 /hr | 1.31  |"
}
```

**Field notes:**
- `slope_per_hour` — linear regression slope; positive = rising, negative = falling
- `direction` — `"rising"`, `"falling"`, or `"stable"`
- `projection_7d` — projected value 7 days from the last reading at the current slope
- `alert` — `true` when the projection would leave the target range for the given stage
- `human_summary` — always a markdown table (`| Metric | Direction | Slope | 7-Day Projection |`) with one row per metric; alert lines appended below the table when any `alert` is `true` (e.g. `"⚠ Temperature is trending rising — 7-day projection: 25.1 °C."`)

---

### `get_port_activity_report(device_id, days=7)`

Build a per-port runtime activity report from historical data. Calls `get_historical_readings`
internally and makes a supplementary `get_devices` call to obtain `portsLoad` values for the
ghost-port Rule A filter (see Quirk 22). If the supplementary call fails, Rule A is disabled
and the report is still returned.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `days` | `int` | Number of days to analyze (1–30, default 7) |

**Response:**
```json
{
  "device_id": "C58ZA",
  "days_analyzed": 7,
  "window_start_local": "May 17, 10:35 AM CDT",
  "window_end_local": "May 24, 10:35 AM CDT",
  "readings_used": 1440,
  "ports": [
    {
      "port": 1,
      "name": "Inline Fan",
      "on_hours": 87.5,
      "off_hours": 80.5,
      "transitions": 14,
      "avg_speed_when_running": 5.2,
      "uptime_pct": 52.1,
      "peak_hour_local": "4:00 PM CDT (peak on May 20)"
    },
    {
      "port": 2,
      "name": "Heater",
      "on_hours": 168.0,
      "off_hours": 0.0,
      "transitions": 0,
      "avg_speed_when_running": 1.0,
      "uptime_pct": 100.0,
      "peak_hour_local": null
    }
  ],
  "ports_excluded_count": 2,
  "human_summary": "Analyzed 7 days (May 17 – May 24) of activity across 1 active port. Inline Fan (Port 1) ran 52.1% uptime (87.5h total), most active around 4:00 PM CDT (peak on May 20). ▎ Currently OFF: Heater (Port 2). 2 ports excluded (no power detected)."
}
```

**Field notes:**
- `window_start_local` / `window_end_local` — the exact local time range analyzed, formatted in the device's timezone (e.g. "May 17, 10:35 AM CDT"). Use these fields when explaining why a report spans multiple calendar days — the window is a rolling `days`×24 h span starting from the current time, not a calendar-day boundary.
- `on_hours` / `off_hours` — cumulative total hours over the full `days` window (not hours per day); total is `days * 24` when full data is available. Present to growers as total elapsed hours, e.g. "ran 87.5 hours over the past 7 days (52%)."
- `transitions` — number of debounced on↔off state changes in the period. Single-reading blips at automation window edges are API boundary artifacts (Quirk 22) and are not counted — only transitions where the new state persists for at least `MIN_DWELL_READINGS` (2) consecutive readings are recorded.
- `avg_speed_when_running` — average `onSpead` value (1–10) across on-readings with non-zero speed
- `uptime_pct` — `on_hours / (on_hours + off_hours) * 100`, rounded to 1 decimal
- `peak_hour_local` — device-local time string with peak date, always including the calendar date for disambiguation across multi-day windows (e.g. "4:00 PM CDT (peak on May 20)"); `null` when port never ran (always_off case). Uses `astimezone()` for full DST-aware conversion; sub-hour UTC offsets (UTC+5:30) are handled correctly. Falls back to UTC when `zoneId` is absent (Quirk 23). Computed via weighted median of hourly activity slots — prevents a single-reading nibble from inflating peak hour to an off-peak slot (fixes #112). Specifically: all on-readings are bucketed by `(date, hour)` UTC slot; the median slot (by reading count) is selected as peak, so high-frequency hours dominate over isolated blips.
- `data_quality` — Internal classification field used to generate `human_summary` caveat lines; **not present in the JSON output** (stripped before serialization). Internally: `null` for ports with reliable history; `"api_constant_speed"` for toggle hardware (heaters, lights, humidifiers — loadType 4 or 128) where the AC Infinity API records constant speed=1 regardless of actual runtime; `"no_load_signal"` for ports on devType=18/22 devices where load data is absent. The effects of these classifications are visible only via `human_summary`: toggle-hardware ports produce `▎`-prefixed caveat lines grouped by current ON/OFF state, e.g. "▎ Currently ON: Heater (Port 2)." or "▎ Currently OFF: Humidifier (Port 3)." — all ON ports in one line, all OFF ports in another. A device-level Note about missing power-draw data is emitted **only for devType=22** (Q0KT4 Genetics Lab) — devType=18 (UIS 69 Pro+) does not emit this Note because its active ports produce reliable runtime data in historical records even though `portsLoad` is always 0. Do not quote `on_hours` or `uptime_pct` for ports with a `▎` caveat — relay the caveat text verbatim.
- `ports_excluded_count` — number of ports removed by the ghost-port filter (see Quirk 22). Capped at `devPortCount` when the device's physical port count is known (fixes over-counting on sub-8-port devices; Issue #129). On devices where `devPortCount` is absent or zero, no cap is applied and the count may reflect all 8 history slots. Do not repeat this count in prose when presenting `human_summary` to a grower.
- `human_summary` — plain-English activity summary. The preamble varies by device type: on standard devices the preamble is "Analyzed N days (date range) of activity across M active port(s)."; on devType=18/22 zero-load devices the preamble is "Analyzed N days (date range) across M port(s)." (no "active" qualifier, since load data is absent). Includes an exclusion note when `ports_excluded_count > 0`: on zero-load devices the note includes port names, e.g. "N port(s) excluded (no activity detected): Name (Port N)."; on standard devices it says "N port(s) excluded (no activity detected)." or "N port(s) excluded (no power detected)." depending on whether port names are available. Includes `▎`-prefixed caveat lines for toggle-hardware ports grouped by ON/OFF state. When `ports` is empty and `ports_excluded_count > 0`, summarizes the no-activity result with the exclusion count (e.g., "No active port activity was detected over the past 7 day(s). 2 ports excluded (no power detected)."). When `ports` is empty and `ports_excluded_count == 0`, includes a troubleshooting explanation (devices off, unplugged, or no scheduled activity). Relay the caveat text for `data_quality = "api_constant_speed"` ports verbatim — do not estimate runtime from `on_hours`.

---

### `get_port_status(device_id, port)`

Get the live operational status of a single port. Reads real-time fields from
`/api/user/devInfoListAll` that are not exposed by `get_device_reading`. When the port is
in Advance Automation mode, makes a secondary call to `/api/version=2.0/dev/getGroups` to
resolve the governing automation name (graceful degradation if the secondary call fails).

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` (e.g. `"C58ZA"`) |
| `port` | `int` | 1-based port number |

**Response (port off, unpowered — default-named port only):**
```json
{
  "device_id": "C58ZA",
  "port": 1,
  "port_name": "Port 1",
  "power_level": 0,
  "mode": "OFF",
  "plug_status": "not powered",
  "human_summary": "Port 1 is OFF (speed 0)."
}
```

**Response (port running — no plug_status, no remain_time_seconds):**
```json
{
  "device_id": "C58ZA",
  "port": 4,
  "port_name": "Filter",
  "power_level": 5,
  "mode": "AUTO",
  "human_summary": "Filter (Port 4) is AUTO at speed 5."
}
```

**Response (port in timer countdown):**
```json
{
  "device_id": "C58ZA",
  "port": 2,
  "port_name": "Intake Fan",
  "power_level": 0,
  "mode": "TIMER_TO_ON",
  "remain_time_seconds": 3600,
  "human_summary": "Intake Fan (Port 2) is TIMER_TO_ON (speed 0)."
}
```

**Response (Advance Automation port — governing automation found):**
```json
{
  "device_id": "C58ZA",
  "port": 4,
  "port_name": "Filter",
  "power_level": 5,
  "mode": "Automation",
  "automation_name": "Moderate Airflow",
  "human_summary": "Filter (Port 4) is running under 'Moderate Airflow' automation at speed 5."
}
```

**Response (Advance Automation port — name lookup failed or automation not found):**
```json
{
  "device_id": "C58ZA",
  "port": 4,
  "port_name": "Filter",
  "power_level": 5,
  "mode": "Automation",
  "human_summary": "Filter (Port 4) is Automation at speed 5."
}
```

**Field notes:**
- `power_level` — actual current power level 0–10 from `speak` API field
- `plug_status` *(conditional)* — `"not powered"` when `loadState == 0` AND `speak == 0` AND the port still has its **default name** (`"Port N"`). Custom-named ports are excluded — a user-assigned name implies a device was intentionally connected, and `loadState=0` alone cannot distinguish "nothing plugged in" from "device is off" for on/off devices (see Quirk 26). Field is **omitted entirely** otherwise. Matches the identical signal in `get_device_reading`.
- `mode` — one of: `OFF`, `ON`, `AUTO`, `VPD`, `TIMER_TO_ON`, `TIMER_TO_OFF`, `CYCLE`, `SCHEDULE`, `Automation`. `Automation` replaces the raw internal label `ADVANCE` and means the port is governed by a named Advance Automation program. The `Automation` value is returned any time `isOpenAutomation==1` in the device list (Quirk 17/19), regardless of whether the secondary automation-name lookup succeeds.
- `automation_name` *(optional)* — present only when `mode == "Automation"` AND the governing automation was successfully identified via the secondary `getGroups` call. Absent when the secondary call fails, the port is not covered by any automation's port-group bitmask, or `devId` is absent from the device record.
- `remain_time_seconds` *(conditional)* — countdown timer seconds remaining from `remainTime` API field; **omitted entirely** when no timer is active (value would be 0). Only present when a TIMER_TO_ON or TIMER_TO_OFF countdown is running.
- `note` *(optional)* — present when the port appears to have nothing connected (`portResistance == 65535`, or fallback name/load heuristic — see Quirks 26 and 27). Example: `"Port 7 doesn't appear to have anything connected. If you meant a different port, let me know which one."`
- `human_summary` — natural language port status: `"Name (Port N) is running under 'AutomationName' automation at speed N."` (Automation mode with name resolved); `"Name (Port N) is Mode at speed N."` (running); `"Name (Port N) is Mode (speed 0)."` (stopped). Always present.

---

### `get_port_settings(device_id, port)`

Get the full automation configuration for a port from `/api/dev/getdevModeSettingList`.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `port` | `int` | 1-based port number |

**Response (non-ADVANCE port):**
```json
{
  "device_id": "C58ZA",
  "port": 1,
  "mode": "VPD",
  "speed_target": 5,
  "vpd_target_kpa": 1.4,
  "temp_range": null,
  "humidity_range_pct": null,
  "schedule_window": null
}
```

**Note:** `cycle_on_seconds`, `cycle_off_seconds`, `timer_on_seconds`, and `timer_off_seconds` are **omitted entirely when their value is 0**. They only appear in the response when the port has a non-zero cycle or timer duration configured.

**Response (ADVANCE mode port — governing automation found):**
```json
{
  "device_id": "C58ZA",
  "port": 5,
  "mode": "ADVANCE",
  "advance_automation": true,
  "automation_name": "Moderate Airflow",
  "automation_id": "1234567890",
  "automation_on_speed": 5,
  "current_speed": 5,
  "speed_target": null,
  "vpd_target_kpa": null,
  "temp_range": null,
  "humidity_range_pct": null,
  "schedule_window": null,
  "cycle_on_seconds": null,
  "cycle_off_seconds": null,
  "timer_on_seconds": null,
  "timer_off_seconds": null,
  "automation_running": true,
  "automation_configured": true,
  "human_summary": "Port is running under 'Moderate Airflow' automation (target speed: 5, current live speed: 5). The automation is active."
}
```

**Response (ADVANCE mode port — all automations disabled):**
```json
{
  "device_id": "C58ZA",
  "port": 5,
  "mode": "ADVANCE",
  "advance_automation": true,
  "automation_name": null,
  "automation_id": null,
  "automation_on_speed": null,
  "current_speed": 0,
  "speed_target": null,
  "vpd_target_kpa": null,
  "temp_range": null,
  "humidity_range_pct": null,
  "schedule_window": null,
  "cycle_on_seconds": null,
  "cycle_off_seconds": null,
  "timer_on_seconds": null,
  "timer_off_seconds": null,
  "automation_running": false,
  "automation_configured": true,
  "human_summary": "Port is in automation mode, but all automations are disabled. The port hasn't fully released. Ask me to list your automations for details."
}
```

**Response (ADVANCE mode port — secondary call failed / degraded):**
```json
{
  "device_id": "C58ZA",
  "port": 5,
  "mode": "ADVANCE",
  "advance_automation": true,
  "automation_name": null,
  "automation_id": null,
  "automation_on_speed": null,
  "current_speed": 0,
  "speed_target": null,
  "vpd_target_kpa": null,
  "temp_range": null,
  "humidity_range_pct": null,
  "schedule_window": null,
  "cycle_on_seconds": null,
  "cycle_off_seconds": null,
  "timer_on_seconds": null,
  "timer_off_seconds": null,
  "automation_running": null,
  "automation_configured": null,
  "human_summary": "Port is in ADVANCE automation mode. Automation details could not be retrieved.",
  "note": "Could not fetch automation details. Use list_advance_automations to view active automations."
}
```

**ADVANCE mode field notes:**
- `mode` — `"ADVANCE"` when `modeType=15` and `isOpenAutomation != 0` in `getdevModeSettingList` (Quirk 19)
- `automation_name` / `automation_id` — populated from the governing automation; `null` when all automations are disabled or the secondary lookup degrades
- `automation_on_speed` — the `on_speed` configured in the port group of the governing automation whose `grouptDevType` bitmask covers the requested port (bitmask-matched); `null` when no governing automation, no matching port group, or on degraded path
- `current_speed` — live fan speed from `devInfoListAll` `speak` field (reflects what the port is currently doing)
- `speed_target` — always `null` in ADVANCE mode (the automation governs speed, not a static target)
- `automation_running` — `true` if the governing automation has `run_state=True`; `false` if an automation was found but not running (all disabled); `null` when the secondary API call failed (degraded)
- `automation_configured` — `true` if the automations list is non-empty; `false` if empty; `null` when degraded (secondary call failed)
- `human_summary` — grower-readable description of the ADVANCE state; always present
- `vpd_target_kpa`, `temp_range`, `humidity_range_pct`, `schedule_window`, cycle/timer fields — all `null` in ADVANCE mode
- When the secondary automation lookup fails (API error), a `note` field is added: `"Could not fetch automation details. Use list_advance_automations to view active automations."`
- When the port appears to have nothing connected (empty-port detection via `portResistance == 65535` or the fallback name/load heuristic — see Quirks 26 and 27), a `note` field is appended with the empty-port staleness advisory. If both conditions apply, both messages are concatenated in the same `note` field. On the ADVANCE path, `human_summary` is **preserved** (it already describes the automation state); only `note` is appended.

**Non-ADVANCE empty-port behavior:**

When `_is_port_empty()` fires on the non-ADVANCE path (primary: `portResistance == 65535`; fallback for old firmware: default-named `"Port N"` with zero load, or devType=18/22), the response diverges from the standard non-ADVANCE form:

- `human_summary` is **overridden** with a staleness statement (e.g. `"Port 3 (Port 3) may not have anything connected — the settings below are from its last configuration and may not reflect an active device."`)
- `note` is set to a redirect hint (e.g. `"If you meant a different port, let me know which one."`)
- All raw data fields (`humidity_range_pct`, `cycle_on_seconds`, etc.) are still returned — they represent the controller's stored configuration regardless of whether hardware is present.

This prevents the response from confidently asserting automation targets (e.g. "Humidity automation: 60–100%") for a port that likely has nothing plugged in.

**Non-ADVANCE field notes:**
- `vpd_target_kpa` — non-null only when VPD automation active; decoded as `targetVpd ÷ 10` (Quirk 4 analogue)
- `temp_range` — `{"min": N, "max": N, "unit": "°C"}` (or `"°F"`) when temp thresholds enabled; values in the device's preferred unit. Internally stored as raw °C integers; converted to °F when `deviceInfo.unit=0` (Quirk 23, no ×100 scaling)
- `humidity_range_pct` — `{"min_pct": N, "max_pct": N}` when humidity thresholds enabled; raw % RH integers
- `schedule_window` — `{"start": "HH:MM", "end": "HH:MM", "timezone": "America/Chicago"}` in device local time; includes `"timezone"` key from `zoneId` (falls back to `"UTC"` when absent) (Quirk 23); `null` when disabled
- `timer_on_seconds` / `timer_off_seconds` — from `acitveTimerOn` / `acitveTimerOff` (API typo: `acitve`)

---

### `set_port_speed(device_id, port, speed, dry_run=True)`

Set fan or dimmer speed on a specific port. Uses read-before-write (legacy controllers).
All 77 mode-setting fields are preserved; only `onSpead` is updated.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `port` | `int` | 1-based port number |
| `speed` | `int` | Target speed 1–10 (10 = full speed) |
| `dry_run` | `bool` | Default `True` — returns payload without writing |

**Validation:** `speed` must be 1–10. Use `set_port_off` to set speed 0.

**Response:**
```json
{
  "action": "set Exhaust Fan (Port 2) speed to 5",
  "device_id": "C58ZA",
  "port": 2,
  "speed": 5,
  "dry_run": true,
  "controller_type": "legacy",
  "sent": false,
  "payload": { "...": "77-field legacy payload" }
}
```

**OFF-mode warning:** When the port is in OFF mode (`atType=0` uninitialized or `atType=1` OFF) at the
time of the call, the response includes an additional `warning` field:

```json
{
  "action": "set Left Fan (Port 3) speed to 5",
  "device_id": "8T4TC",
  "port": 3,
  "speed": 5,
  "dry_run": false,
  "controller_type": "legacy",
  "sent": true,
  "warning": "Left Fan (Port 3) is currently in OFF mode — speed was stored but the port will not run until the mode is changed to ON. To activate it, ask me to switch this port to ON mode."
}
```

The speed is stored in the controller's settings but the port does not activate. Ask Claude
to switch the port to ON mode to bring it up at the stored speed.

**Empty-port warning:** All 7 write tools (`set_port_on`, `set_port_off`, `set_port_speed`,
`set_port_mode`, `set_vpd_automation`, `set_temperature_automation`, `set_humidity_automation`)
include a `warning` field when the target port appears to have nothing connected. When both the
OFF-mode condition and the empty-port condition apply simultaneously (on `set_port_speed`),
both warning messages are concatenated in the same `warning` field.

**AI+ note:** `dry_run=True` is supported. `dry_run=False` returns an unsupported error — see Quirk 14.

---

### `set_port_on(device_id, port, dry_run=True)`

Turn a port on at full speed (`onSpead=10`). Sets `atType=2` (ON mode) explicitly. Works for
fan-type and on/off toggle devices. Uses read-before-write.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `port` | `int` | 1-based port number |
| `dry_run` | `bool` | Default `True` — returns payload without writing |

**Payload fields set:** `atType=2` (ON mode), `onSpead=10`.

**Response:** Same structure as `set_port_speed` without the `speed` field; `action` uses the port's name and number, e.g. `"turn Intake Fan (Port 1) on"` (or `"turn Port 1 on"` when no custom name is configured). Includes a `warning` field when the port appears to have nothing connected (Quirk 26).

---

### `set_port_off(device_id, port, dry_run=True)`

Turn a port off. Sets `atType=1` (OFF mode) explicitly and zeros the speed (`onSpead=0`).
Works for all device types including toggle hardware (heaters, lights, on/off outlets).
Uses read-before-write.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `port` | `int` | 1-based port number |
| `dry_run` | `bool` | Default `True` — returns payload without writing |

**Payload fields set:** `atType=1` (OFF mode), `onSpead=0`. Sending `atType=1` is required for
toggle hardware — zeroing speed alone leaves the mode as ON, causing the device to remain
energized (issue #232, fixed in PR #233).

**Response:** Same structure as `set_port_speed` without the `speed` field; `action` uses the port's name and number, e.g. `"turn Intake Fan (Port 1) off"` (or `"turn Port 1 off"` when no custom name is configured). Includes a `warning` field when the port appears to have nothing connected (Quirk 26).

---

### `set_vpd_automation(device_id, port, target_vpd, dry_run=True)`

Enable VPD automation using the built-in temperature and humidity sensors.
Switches the port to VPD mode (`atType=8`) and sets the VPD target.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `port` | `int` | 1-based port number |
| `target_vpd` | `float` | Target VPD in kPa, range 0.1–3.0 |
| `dry_run` | `bool` | Default `True` — returns payload without writing |

**Validation:** `target_vpd` must be 0.1–3.0. Sub-0.1 kPa and over-3.0 kPa are rejected.

**Encoding:** `targetVpd = round(target_vpd × 10)` — e.g. 1.4 kPa → stored as 14 (Quirk 4 analogue for writes).
Also sets `vpdSettingMode=1`, `targetVpdSwitch=1`, `atType=8`.

**Response:**
```json
{
  "action": "set Exhaust Fan (Port 1) VPD automation to 1.4 kPa",
  "device_id": "C58ZA",
  "port": 1,
  "target_vpd_kpa": 1.4,
  "dry_run": true,
  "controller_type": "legacy",
  "sent": false,
  "payload": { "...": "77-field legacy payload" }
}
```

---

### `set_temperature_automation(device_id, port, min_temp, max_temp, dry_run=True)`

Enable temperature automation using the built-in temperature sensor.
Switches the port to AUTO mode (`atType=3`) and sets temperature thresholds.
The controller speeds up when temperature exceeds `max_temp` and slows below `min_temp`.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `port` | `int` | 1-based port number |
| `min_temp` | `float` | Minimum threshold in device-preferred unit (°C or °F). Range: 0–50°C (32–122°F). Sub-degree values rounded to nearest int |
| `max_temp` | `float` | Maximum threshold in device-preferred unit. Must exceed `min_temp` |
| `dry_run` | `bool` | Default `True` — returns payload without writing |

**Encoding:** Values accepted in device-preferred unit; converted to °C internally if needed.
`devLt = int(min_c + 0.5)`, `devHt = int(max_c + 0.5)` — raw °C integers, no ×100 scaling.
Also sets `activeLt=1`, `activeHt=1`, `atType=3`. (Quirk 23)

**Response:**
```json
{
  "action": "set Exhaust Fan (Port 1) temperature automation 20–26°C",
  "device_id": "C58ZA",
  "port": 1,
  "min_temp": 20.0,
  "max_temp": 26.0,
  "dry_run": true,
  "controller_type": "legacy",
  "sent": false,
  "payload": { "...": "77-field legacy payload" }
}
```

---

### `set_humidity_automation(device_id, port, min_rh, max_rh, dry_run=True)`

Enable humidity automation using the built-in humidity sensor.
Switches the port to AUTO mode (`atType=3`) and sets humidity thresholds.
The controller speeds up when humidity exceeds `max_rh` and slows below `min_rh`.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `port` | `int` | 1-based port number |
| `min_rh` | `float` | Minimum threshold % RH, range 0–100. Sub-percent values rounded to nearest int |
| `max_rh` | `float` | Maximum threshold % RH, range 0–100. Must exceed `min_rh` |
| `dry_run` | `bool` | Default `True` — returns payload without writing |

**Encoding:** `devLh = round(min_rh)`, `devHh = round(max_rh)` — raw % RH integers, no ×100 scaling.
Also sets `activeLh=1`, `activeHh=1`, `atType=3`.

**Response:**
```json
{
  "action": "set Exhaust Fan (Port 1) humidity automation 40–60%",
  "device_id": "C58ZA",
  "port": 1,
  "min_rh": 40.0,
  "max_rh": 60.0,
  "dry_run": true,
  "controller_type": "legacy",
  "sent": false,
  "payload": { "...": "77-field legacy payload" }
}
```

---

### `set_port_mode(device_id, port, mode, dry_run=True, ...)`

Switch a port to a specific automation mode. All 8 AC Infinity automation modes are
supported. For setting automation targets alongside the mode, prefer the dedicated tools:
`set_vpd_automation`, `set_temperature_automation`, `set_humidity_automation`.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `port` | `int` | 1-based port number |
| `mode` | `str` | One of: `OFF`, `ON`, `AUTO`, `VPD`, `CYCLE`, `SCHEDULE`, `TIMER_TO_ON`, `TIMER_TO_OFF` |
| `dry_run` | `bool` | Default `True` — returns payload without writing |
| `cycle_on_seconds` | `int \| None` | Required for `CYCLE` — seconds port runs per cycle |
| `cycle_off_seconds` | `int \| None` | Required for `CYCLE` — seconds port is off per cycle |
| `schedule_start` | `str \| None` | Required for `SCHEDULE` — start time `"HH:MM"` in device local time |
| `schedule_end` | `str \| None` | Required for `SCHEDULE` — end time `"HH:MM"` in device local time |
| `timer_duration_seconds` | `int \| None` | Required for `TIMER_TO_ON` and `TIMER_TO_OFF` — countdown duration |

**Mode → `atType` encoding:**
| Mode | `atType` |
|---|---|
| `OFF` | 1 |
| `ON` | 2 |
| `AUTO` | 3 |
| `TIMER_TO_ON` | 4 |
| `TIMER_TO_OFF` | 5 |
| `CYCLE` | 6 |
| `SCHEDULE` | 7 |
| `VPD` | 8 |

**Response:**
```json
{
  "action": "set Exhaust Fan (Port 1) mode to CYCLE",
  "device_id": "C58ZA",
  "port": 1,
  "mode": "CYCLE",
  "dry_run": true,
  "controller_type": "legacy",
  "sent": false,
  "payload": { "...": "77-field legacy payload" }
}
```

---

### ADVANCE_AUTOMATION conflict response (all write tools)

When any write tool detects an active Advance Automation on the target port, it returns a
structured conflict response instead of an error string. This response is returned by
`set_port_speed`, `set_port_on`, `set_port_off`, `set_port_mode`, `set_vpd_automation`,
`set_temperature_automation`, `set_humidity_automation`, and `apply_grow_stage_template`.

The conflict response has four distinct paths depending on what the secondary automation
lookup finds:

**Auth-error path (secondary lookup raises `ACInfinityAuthError`):**
```json
{
  "error": "Authentication failed — check AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD",
  "detail": "see server logs"
}
```

**Normal path from `set_port_speed` (governing automation found, speed provided):**
```json
{
  "conflict": "ADVANCE_AUTOMATION",
  "summary": "While 'Moderate Airflow' automation is running, all ports on this controller are locked from manual control. Your change requires resolving this conflict first.",
  "human_summary": "'Moderate Airflow' is actively controlling this port at target speed 5. To make manual adjustments, you need to resolve this automation conflict first.",
  "suggested_reply": "'Moderate Airflow' automation is controlling this port right now (target speed: 5). The easiest fix is to update the automation to run at speed 3 instead — the automation stays active, just at the new speed. Alternatively, I can release Inline Fan (Port 1) from the automation so you can control it manually — but that will also release all other ports currently on 'Moderate Airflow'. What would you prefer?",
  "target_port": "Inline Fan (Port 1)",
  "automation_name": "Moderate Airflow",
  "automation_id": "1234567890",
  "active_automations": [
    {"name": "Moderate Airflow", "automation_id": "1234567890"}
  ],
  "co_governed_ports": [],
  "switching_guidance": "To regain manual control: ask me to disable any active automations, then apply your change. To add this port to an automation instead, ask me to create a new one.",
  "options": {
    "0_update_speed": {
      "description": "Change the 'Moderate Airflow' automation's target speed from 5 to 3, keeping the automation active.",
      "instruction": "Ask me to update the 'Moderate Airflow' automation to run at speed 3 instead.",
      "available": true
    },
    "1_break_out": {
      "description": "Release Inline Fan (Port 1) from 'Moderate Airflow' to regain manual control.",
      "_tool": "break_out_of_automation",
      "instruction": "Ask me to release Inline Fan (Port 1) from the 'Moderate Airflow' automation so you can control it manually.",
      "available": true
    },
    "2_disable_automation": {
      "description": "Disable 'Moderate Airflow' entirely — releases all ports on this automation.",
      "_tool": "disable_advance_automation",
      "instruction": "Ask me to disable the 'Moderate Airflow' automation — this will release all ports it currently controls.",
      "available": true
    },
    "3_fork_automation": {
      "available": false,
      "status": "not_yet_implemented"
    }
  }
}
```

**Normal path from `set_port_on` / `set_port_off` (no speed provided, no option 0):**

Same structure as above but **without** the `"0_update_speed"` key and with `suggested_reply` not mentioning update-speed as the primary option.

**All-disabled path (API succeeded, automations non-empty, none currently active):**
```json
{
  "conflict": "ADVANCE_AUTOMATION",
  "summary": "An Advance Automation is blocking this port. All configured automations are currently disabled, but the port hasn't fully released from automation mode.",
  "human_summary": "This port is in automation mode, but all automations are disabled. The port hasn't fully released. Ask me to list your automations for details.",
  "suggested_reply": "Your automations for this port are all turned off, but the port is still stuck in automation mode — it hasn't fully released. I can force-release it by re-applying the disable command. Want me to do that?",
  "target_port": "Inline Fan (Port 1)",
  "automation_name": null,
  "automation_id": null,
  "active_automations": [],
  "co_governed_ports": [],
  "switching_guidance": "To regain manual control: ask me to disable any active automations, then apply your change. To add this port to an automation instead, ask me to create a new one.",
  "options": {
    "1_re_disable_to_clear": {
      "description": "Force-release this port by re-applying the disable command.",
      "_tool": "disable_advance_automation",
      "instruction": "Ask me to list your automations so we can identify which one is blocking this port, then ask me to force-release it.",
      "available": true
    },
    "2_disable_automation": {
      "available": false,
      "status": "All automations already disabled — use option 1 to force-release the port."
    },
    "3_fork_automation": {
      "available": false,
      "status": "not_yet_implemented"
    }
  }
}
```

**Degraded path (API error during lookup, or automation list is empty):**
```json
{
  "conflict": "ADVANCE_AUTOMATION",
  "summary": "An Advance Automation is running on this controller, locking all ports from manual control. Your change requires resolving this conflict first.",
  "human_summary": "An active automation is blocking manual port control on this controller. Ask me to list your automations to see what's set up.",
  "suggested_reply": "An active automation is blocking this port. Let me look up the active automations to resolve this — shall I get started?",
  "target_port": "Inline Fan (Port 1)",
  "automation_name": null,
  "automation_id": null,
  "active_automations": [],
  "co_governed_ports": [],
  "switching_guidance": "To regain manual control: ask me to disable any active automations, then apply your change. To add this port to an automation instead, ask me to create a new one.",
  "options": {
    "1_find_and_disable": {
      "description": "Find and disable the active automation, then apply your manual change.",
      "_tool": "list_advance_automations",
      "instruction": "Ask me to list your automations so we can identify which one is blocking this port, then ask me to disable it and force-release the port.",
      "available": true
    },
    "2_disable_automation": {
      "available": false,
      "status": "Use option 1 first to identify the automation."
    },
    "3_fork_automation": {
      "available": false,
      "status": "not_yet_implemented"
    }
  }
}
```

**Key field notes:**
- **Auth-error path** — when `ACInfinityAuthError` is raised during the secondary automation lookup, none of the standard conflict fields (`conflict`, `options`, etc.) are present; only `error` and `detail` are returned. The caller must re-authenticate before retrying.
- `active_automations` — list of `{"name": ..., "automation_id": ...}` objects for all enabled automations on this controller (empty list in all-disabled and degraded paths)
- `human_summary` — plain-language summary suitable for display to the grower; always present in the normal/all-disabled/degraded paths (absent in auth-error path)
- `suggested_reply` — pre-written reply text the LLM can use verbatim; no tool call syntax exposed to the grower
  - Normal path (with speed): leads with update-automation-speed as the easiest option, then offers break-out as alternative
  - Normal path (no speed): asks whether to break out or update the automation settings
  - All-disabled path: explains the stuck-in-automation-mode situation and offers to force-release via re-apply
  - Degraded path: offers to list automations to identify the blocking one (no tool names exposed to the grower)
- `options.0_update_speed` — present in normal path only when called from `set_port_speed` (i.e. `requested_speed` is not None). Not present for `set_port_on` / `set_port_off` (no speed target applies). Not present in all-disabled or degraded paths.
- Option key naming by path:
  - Normal path: `"0_update_speed"` (when speed provided), `"1_break_out"` (`_tool`: `break_out_of_automation`), `"2_disable_automation"` (`_tool`: `disable_advance_automation`)
  - All-disabled path: `"1_re_disable_to_clear"` (`_tool`: `disable_advance_automation`), `"2_disable_automation"` (available: false)
  - Degraded path: `"1_find_and_disable"` (`_tool`: `list_advance_automations`), `"2_disable_automation"` (available: false)
- `options.1_break_out.available` — set to `governing.get("enabled", False) or governing.get("run_state", False)`; `true` when the automation is enabled OR actively running (handles mid-toggle transient state where `isOn=0` but `runState=1`)
- The `isOpenAutomation` guard condition is documented in Quirk 19; the pre-write guard from devInfoListAll is documented in Quirk 25
- **User-facing text rules:** All `instruction`, `description`, `suggested_reply`, and `switching_guidance` fields must use natural-language prose (no Python function call syntax, no `dry_run`, no `device_id=`, no raw numeric IDs). See `CLAUDE.md` § "User-facing text rules".

---

## MCP Intelligence Tool

### `apply_grow_stage_template(device_id, port, stage, dry_run=True)`

One-click grow stage configuration. Calls `set_vpd_automation`, `set_temperature_automation`,
and `set_humidity_automation` in sequence using the VPD midpoint and full ranges from
`STAGE_TARGETS` in `analytics.py`.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `port` | `int` | 1-based port number |
| `stage` | `str` | One of: `clones`, `seedling`, `veg`, `early_flower`, `mid_flower`, `late_flower` |
| `dry_run` | `bool` | Default `True` — returns payloads without writing |

**Stage targets (VPD is the midpoint of the stage range):**
| Stage | VPD (kPa) | Temp (°C) | Humidity (%) |
|---|---|---|---|
| `clones` | 1.00 | 22–26 | 70–80 |
| `seedling` | 1.00 | 22–26 | 65–75 |
| `veg` | 1.25 | 20–28 | 50–70 |
| `early_flower` | 1.40 | 20–26 | 40–60 |
| `mid_flower` | 1.60 | 18–25 | 35–55 |
| `late_flower` | 1.50 | 18–24 | 30–50 |

**Response:** JSON with flat `sent`, `controller_type`, and `payload` (when `dry_run=True`)
fields. The `vpd`, `temperature`, and `humidity` sub-objects carry the per-target
display values (`target_kpa`, `min`/`max`/`unit`, `min_rh`/`max_rh`) but not their own
`sent`/`payload` keys. Temperature values in `temperature` are in the device-preferred unit (Quirk 23). The call is atomic: it succeeds or fails as a single write, so
there is no partial-failure state to surface — either all the stage's targets land on
the controller, or the prior state is preserved.

**Encoding:**
- VPD: `int(target_vpd * 10 + 0.5)` — e.g. 1.25 kPa → stored as 13 (round-half-up)
- Temp/humidity: `int(value + 0.5)` raw integer — e.g. 20°C → `devLt=20` (no × 100 scaling)
- Rate limit: a single write, so the 1.5s rate gate fires once (Quirk 15)

**AI+ note:** `dry_run=True` is fully supported. `dry_run=False` returns the AI+
unsupported error before any writes (same as individual automation tools).

---

## MCP Advance Automation Tools

These tools manage Advance Automations — named programs that govern one or more ports
simultaneously. See Quirk 17 and Quirk 18 for the underlying API behavior.

All write tools (`enable`, `disable`, `create`, `delete`, `break_out_of_automation`) default
to `dry_run=True` and return the planned action without executing.

---

### `list_advance_automations(device_id)`

List all Advance Automations configured on a device.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |

**Response:**
```json
{
  "device_id": "C58ZA",
  "automations": [
    {
      "automation_id": 12345,
      "name": "Moderate Airflow",
      "enabled": true,
      "currently_running": true
    }
  ]
}
```

**Field notes:**
- `automation_id` — use this value in all other automation tools
- `enabled` — whether the automation is active (controlled by `updateGroupsIsOn`)
- `currently_running` — whether any port governed by this automation is actively running
- Empty: `{"device_id": "...", "automations": []}`

---

### `get_advance_automation(device_id, automation_id)`

Get full detail for a single Advance Automation.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `automation_id` | `str` | automation_id from `list_advance_automations` |

**Response:**
**Continuous mode (always active when enabled):**
```json
{
  "device_id": "C58ZA",
  "automation_id": 12345,
  "name": "Moderate Airflow",
  "enabled": true,
  "currently_running": true,
  "schedule": {
    "mode": "continuous",
    "begin_time": null,
    "end_time": null
  },
  "port_groups": [
    {
      "adv_id": 12345,
      "on_speed": 5,
      "device_type": "Left Fan (Port 5), Right Fan (Port 6)"
    }
  ],
  "governed_ports": [
    {"port": 5, "port_name": "Left Fan (Port 5)"},
    {"port": 6, "port_name": "Right Fan (Port 6)"}
  ],
  "port_resolution": "resolved",
  "human_summary": "'Moderate Airflow' runs continuously at speed 5, currently enabled."
}
```

**Scheduled mode with a time window configured:**
```json
{
  "schedule": {
    "mode": "scheduled",
    "begin_time": "09:00",
    "end_time": "17:00"
  },
  "human_summary": "'Moderate Airflow' runs at speed 5 from 09:00 to 17:00, currently enabled."
}
```

**Scheduled mode selected but no time window set:**
```json
{
  "schedule": {
    "mode": "scheduled",
    "begin_time": null,
    "end_time": null,
    "schedule_note": "scheduled mode selected but no time window is configured"
  },
  "human_summary": "'Moderate Airflow' runs at speed 5 on a schedule (no time window set), currently enabled."
}
```

**Field notes:**
- `schedule.mode` — `"scheduled"` when `onTimeSwitch=0` AND real `begin_time`/`end_time` values are present (the "Continuous 24H/7D" app toggle is OFF, so the time window applies); `"continuous"` when `onTimeSwitch=1` (toggle ON, runs 24/7) or when times are sentinel values (255). See Quirk 21.
- `schedule.begin_time` / `schedule.end_time` — `null` for continuous mode; `"HH:MM"` for scheduled mode with a time window; `null` for scheduled mode with no window configured
- `schedule.schedule_note` — present only in scheduled mode with no time window; value: `"scheduled mode selected but no time window is configured"`
- `port_groups` — each group has its own speed settings; `device_type` lists the actual port names governed by that group, resolved from the `grouptDevType` bitmask (e.g. `"Left Fan (Port 5), Right Fan (Port 6)"`). Each port is formatted as `"Name (Port N)"`. When the bitmask covers no ports, `"Unknown"` is returned. Port names are sourced from `deviceInfo.ports` via the same `port_name_map` used for `governed_ports`.
- `governed_ports` — list of `{"port": N, "port_name": "Name (Port N)"}` objects identifying which ports this automation controls; decoded from the `grouptDevType` bitmask of each of the automation's port groups (Port N = bit N-1); port names sourced from `deviceInfo.ports` (Quirk 18)
- `port_resolution` — one of:
  - `"resolved"` — `governed_ports` decoded successfully from bitmasks; accurate for this automation regardless of whether other automations are simultaneously active
  - `"error"` — an exception occurred while decoding bitmasks; `governed_ports` is empty
- `human_summary` — natural-language description; adapts to mode and group configuration:
  - Continuous, single group: `"'Name' runs continuously at speed N, currently enabled."`
  - Scheduled with times, single group: `"'Name' runs at speed N from HH:MM to HH:MM, currently enabled."`
  - Scheduled, no time window, single group: `"'Name' runs at speed N on a schedule (no time window set), currently enabled."`
  - Multi-group: `"'Name' controls Port N Name, Port M Name at varying speeds. Currently enabled."`

---

### `enable_advance_automation(device_id, automation_id, dry_run=True)`

Enable a previously disabled Advance Automation. No-ops if already enabled.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `automation_id` | `str` | automation_id from `list_advance_automations` |
| `dry_run` | `bool` | Default `True` — returns plan without executing |

**Response (dry_run=True):**
```json
{
  "action": "enable",
  "automation_name": "Moderate Airflow",
  "automation_id": 12345,
  "dry_run": true,
  "sent": false
}
```

**Response (already enabled):**
```json
{"info": "Automation 'Moderate Airflow' is already enabled. No action taken.", "dry_run": true}
```

**API note:** Uses the `updateGroupsIsOn` toggle endpoint (Quirk 18). This tool reads
current state first and only calls the API when the automation is disabled, so a single
toggle always results in the enabled state.

---

### `disable_advance_automation(device_id, automation_id, dry_run=True)`

Disable a currently enabled Advance Automation. No-ops if already disabled.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `automation_id` | `str` | automation_id from `list_advance_automations` |
| `dry_run` | `bool` | Default `True` — returns plan without executing |

**Response (dry_run=True):**
```json
{
  "action": "disable",
  "automation_name": "Moderate Airflow",
  "automation_id": 12345,
  "governed_ports": [
    {"port": 3, "port_name": "Intake Fan (Port 3)"},
    {"port": 5, "port_name": "Exhaust Fan (Port 5)"}
  ],
  "human_summary": "Disabling 'Moderate Airflow' will take Intake Fan (Port 3), Exhaust Fan (Port 5) off automation control. Re-enabling it restores automation control immediately — no wait for the next trigger.",
  "dry_run": true,
  "sent": false,
  "to_restore": "Ask me to re-enable 'Moderate Airflow'."
}
```

**Response (live, dry_run=False):**
```json
{
  "action": "disable",
  "automation_name": "Moderate Airflow",
  "automation_id": 12345,
  "governed_ports": [
    {"port": 3, "port_name": "Intake Fan (Port 3)"},
    {"port": 5, "port_name": "Exhaust Fan (Port 5)"}
  ],
  "human_summary": "'Moderate Airflow' has been disabled. Re-enabling it will restore automation control immediately.",
  "dry_run": false,
  "sent": true,
  "to_restore": "Ask me to re-enable 'Moderate Airflow'."
}
```

**Field notes:**
- `governed_ports` — list of `{"port": N, "port_name": "Name (Port N)"}` dicts for every port the automation controls, decoded from the `grouptDevType` bitmask across all port groups (Port N = bit N−1 set). Port names are sourced from `deviceInfo.ports` via `_sanitize_api_string`; fallback is `"Port N"` (bare, no redundant suffix) when `portName` is absent or matches the API default (e.g. `"Port 1"`).
- `human_summary` — dry_run: describes what will happen when confirmed. Live: confirms the automation was disabled. Replaces the former `revert_behavior_confirmed` boolean. Always present.
- `to_restore` — natural-language hint for re-enabling the automation by name; intentionally avoids Python function-call syntax so the MCP caller can relay it to the user verbatim

---

### `create_advance_automation(device_id, name, on_speed, port, off_speed=0, begin_time=0, end_time=1439, dry_run=True)`

Create a new Advance Automation on a device. Defaults to `dry_run=True` for safety. Set `dry_run=False` to send the automation to the device. The port bitmask (`grouptDevType`) is computed automatically from the port number (Port N → 2^(N-1)).

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `name` | `str` | Automation name (max 64 chars; control chars stripped) |
| `on_speed` | `int` | Fan speed when active (1–10) |
| `port` | `int` | 1-based port number the automation should control (1–8) |
| `off_speed` | `int` | Accepted for compatibility but not sent to the device — On mode relies on the port's own minimum speed setting. Default: 0 |
| `begin_time` | `int` | Schedule start as minutes since midnight (0–1439, or 255 = always active). Default: 0 (midnight) |
| `end_time` | `int` | Schedule end as minutes since midnight (0–1439, or 255 = always active). Default: 1439 (23:59) |
| `dry_run` | `bool` | Default `True` — previews without sending. Set to `False` to create the automation on the device. |

**Response (dry_run=True):**
```json
{
  "action": "create",
  "name": "Night Mode",
  "port": 3,
  "port_name": "Intake Fan",
  "on_speed": 3,
  "min_speed": 1,
  "begin_time": "22:00",
  "end_time": "06:00",
  "schedule_summary": "Active 10:00 PM – 6:00 AM",
  "dry_run": true,
  "sent": false,
  "note": "Preview only — nothing sent to your device yet. Confirm to create this automation."
}
```

**Response (live, dry_run=False):**
```json
{
  "action": "create",
  "automation_id": "12345",
  "automation_id_note": "internal — reference this automation by name to users",
  "name": "Night Mode",
  "port": 3,
  "port_name": "Intake Fan",
  "on_speed": 3,
  "min_speed": 1,
  "begin_time": "22:00",
  "end_time": "06:00",
  "schedule_summary": "Active 10:00 PM – 6:00 AM",
  "dry_run": false,
  "sent": true
}
```

**Response (port not found on device):**
```json
{
  "error": "Port 5 not found on device C58ZA",
  "available_ports": [
    {"port": 1, "name": "Intake Fan"},
    {"port": 2, "name": "Exhaust Fan"}
  ],
  "suggested_reply": "Port 5 isn't in use on this device. Let me show you what's connected."
}
```

**Port name fallback:** When a port's `portName` field is absent or empty in the API response, the `name` field in `available_ports` falls back to `"Port N"` (e.g., `"Port 3"`). Control characters in `portName` values are sanitized via `_sanitize_api_string` before inclusion.

**Field notes:**
- `port` — required; identifies the port the automation will govern
- `port_name` — resolved from `devInfoListAll` for the given port number
- `min_speed` — the port's configured minimum speed (read from `offSpead` in `getdevModeSettingList`); used by the device when the automation is inactive
- `off_speed` — not sent to the device; On mode uses the port's own minimum speed setting (`min_speed`)
- `begin_time` / `end_time` — returned as `"HH:MM"` formatted strings in the response (input is still minutes-since-midnight integer); use 255 for "always active" (maps to full-day range 0/1439)
- `schedule_summary` — human-readable schedule description (e.g. `"Active 10:00 PM – 6:00 AM"` or `"Always active"`)
- `automation_id` — server-assigned `advId`; present in live response for programmatic chaining only — do not surface to the user; reference the automation by `name` instead
- `automation_id_note` — in-band reminder that `automation_id` is internal
- `note` — present in dry_run response only; prompts user to confirm before creating
- `switchTime` — always sent as `127` (binary `01111111`, all 7 days bitmask); value `255` causes the app to ignore the schedule and treat it as Continuous mode (see Quirk 18)

**Validation:** `on_speed` 1–10; `off_speed` 0–10; `begin_time` and `end_time` each 0–1439 or both 255; `begin_time` ≤ `end_time` unless both are 255; `name` must not be empty or all control characters.

---

### `delete_advance_automation(device_id, automation_id, dry_run=True)`

Delete an Advance Automation. If currently enabled, disables it first.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `automation_id` | `str` | automation_id from `list_advance_automations` |
| `dry_run` | `bool` | Default `True` — returns plan without deleting |

**Response:**
```json
{
  "action": "delete",
  "automation_name": "Moderate Airflow",
  "automation_id": 12345,
  "was_enabled": true,
  "dry_run": true,
  "sent": false
}
```

---

### `break_out_of_automation(device_id, port, dry_run=True, confirm_automation_name=None)`

Safely break a port out of Advance Automation control. Identifies the governing automation
(the one whose bitmask covers the target port), disables it, and locks only the co-ports
within that same automation to their current manual speed, leaving the target port free for
manual control. Ports in other automations are unaffected.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `port` | `int` | Port number to break free (1-based) |
| `dry_run` | `bool` | Default `True` — returns execution plan without making changes |
| `confirm_automation_name` | `str \| None` | Required when `dry_run=False` — automation name (case-insensitive) for safety confirmation |

**Response (dry_run=True):**
```json
{
  "plan": [
    "Disable automation 'Moderate Airflow'",
    "Lock port 3 to speed 5 (manual)"
  ],
  "governing_automation": "Moderate Airflow",
  "co_ports_to_lock": [3],
  "target_port": 1,
  "estimated_duration_seconds": 3,
  "dry_run": true
}
```

**Response (not under automation — idempotent):**
```json
{"info": "Port is not currently under automation control."}
```

**Response (port is in ADVANCE mode but no active automation claims it — ghost state):**
```json
{"info": "Port is not currently under active automation control. No action taken."}
```

**Field notes:**
- Locks only the co-ports within the governing automation (ports that share the same automation
  as the target port). Ports governed by other automations, or empty ports
  (`portResistance == 65535`), are unaffected. Empty-port detection uses `_is_port_empty()`,
  which handles devices where `portResistance` is absent and `devType=18` does not expose
  `portResistance` (issue #190, fixed in PR #233).
- On devices with multiple active automations, only the automation whose bitmask covers the
  target port is disabled and its co-ports locked — other automations continue running.
- `confirm_automation_name` match is case-insensitive; required for live execution as a safety gate
- **Ghost state no-op (issue #191, fixed in PR #233):** When `modeType=15` is set but no active
  automation's bitmask covers the port (stale flag from a deleted or fully-disabled automation),
  the tool returns an idempotent info response — `{"info": "... No action taken."}` — rather
  than an error. This is the correct behavior: the port is not under active automation control.
- **2s propagation wait after disable (issue #234, fixed in PR #233):** After the automation is
  disabled, the server waits 2 seconds and invalidates the device cache before re-fetching. Both
  ADVANCE guards (pre-write `isOpenAutomation` and `getdevModeSettingList`) need this settling
  time; otherwise the co-port lock writes see stale state and are blocked by the conflict guard.
- **Target port locked to automation speed (issue #235, fixed in PR #233):** After co-port locks
  are applied, the target port itself is written to its automation-controlled `on_speed` (from
  the governing port group). This ensures the grower sees no unexpected speed change after release
  — the port starts manual control from its previous automation-controlled baseline speed.

---

## MCP Prompts

Static text responses — zero API calls. Registered with `@mcp_server.prompt()`.

### `vpd_troubleshooting`

Step-by-step VPD diagnosis guide. Covers HIGH VPD (air too dry) and LOW VPD (air too
humid) with specific tool calls for each fix path. Includes stage VPD target table.

### `new_grower_setup`

Onboarding guide: `discover_devices` → `get_device_reading` → `apply_grow_stage_template`
(dry_run first) → `get_environment_health`. Explains each step and available stage names.

### `environment_alert_interpretation`

Explains `check_vpd_drift` status values (OK / HIGH / LOW) and `get_environment_health`
score grades (A–F, 90–100 → 0–39). Covers score weighting (VPD 40%, temp 30%, humidity
30%), `top_recommendation` field, and quick action reference table.
