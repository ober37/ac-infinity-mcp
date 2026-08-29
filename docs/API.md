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
  forces a re-login or after extended inactivity. When this happens the API returns a
  session-expiry body code (`10003`) inside an otherwise HTTP-200 response. On **reads**
  the client now re-authenticates transparently and retries once; on **writes** it does
  **not** replay (see "Session-expiry re-authentication" below and Quirk 30).
- **Single active session per account:** AC Infinity permits only one live session per
  account. Authenticating through this server can invalidate the user's AC Infinity
  mobile-app session (and vice versa). Logging back into the app does not affect the
  user's controllers or schedules.

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
| `newFrameworkDevice` | `true` for AI+ controllers — writes need the `minversion` header (Quirk 14) |
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

## All 36 Known API Quirks

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

**Truncation counts by code point, not byte.** The client mirrors the 25-character limit
with `password[:25]`, which slices by Python `len()` — i.e. by Unicode **code point**, not
UTF-8 byte. Whether the AC Infinity server itself counts bytes or code points is
**unverified** (no hardware available to confirm), so a password whose first 25 code points
exceed 25 bytes may still be truncated differently server-side. A password longer than 25
characters will fail to authenticate.

Since #262, the auth-failure message surfaced to the MCP client cites this 25-character
limit so a grower (who does not watch server logs) has a diagnostic path.

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

**Large-range assembly (#248):** because pagination is driven by the `time` cursor and
not by `pageNum`, the client assembles arbitrarily large date ranges by chaining chunks
until a short page is returned. The community has reported a server-side per-page
observation of roughly 96 rows; that observation does **not** truncate this server's
results, because each chunk's last `createTime` becomes the next chunk's start cursor and
the loop continues until the range is exhausted. `get_historical_readings` defaults to a
`page_size` of 2000 and stitches the chunks together. A regression test locks this
behavior so a future change to the cursor logic cannot silently re-introduce truncation.

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

### Quirk 14 — AI+ controllers: live writes work; the gate is a single `minversion` header

AI+ controllers (`newFrameworkDevice=true`, `devType >= 20`) use the same
read-before-write pattern and return the same 142-field structure from
`getdevModeSettingList` as legacy controllers. `POST /dev/addDevMode` is the
correct endpoint for them too — it simply refuses the stock header set.

**The entire fix is one request header:**

```
minversion: 3.5
```

With it, the ordinary merged read-before-write payload succeeds for manual
control and automation targets alike. Without it, the same payload returns
`{"code": 100001, "msg": "Something went wrong with your request."}`.

**Ablation, live `devType=20` hardware, no-op write to an idle port** (each row
writes the port's own current values back, so nothing changes either way):

| Headers sent | Result |
|---|---|
| stock okhttp set only | `100001` |
| `minversion: 3.5` only | **`200`** |
| iOS `User-Agent` + `phoneType` + `appVersion`, no `minversion` | `100001` |
| `appVersion: 1.9.7` alone | `100001` |
| all four together | `200` |

**Despite the name it is not a version comparison.** Only the literal string
`"3.5"` is accepted; a *higher* value fails exactly as a lower one does:

| Value | Result |
|---|---|
| `3.5` | `200` |
| `3.4`, `3.6`, `3`, `3.50`, `3.5.0`, `99.9`, `""` | `100001` |

Treat it as an opaque magic constant, not a number to bump. If AC Infinity ever
stops honouring it, AI+ writes return `100001` and `client.py` raises an
`ACInfinityDeviceError` naming this quirk rather than a bare API error.

We therefore send **only** `minversion` on the AI+ write path — no spoofed
User-Agent, no `appVersion`, no `phoneType`. Every header we declare is surface
for the kind of server-side tightening that broke the v2 endpoints in #298
(Quirk 33), and three of the four were proven unnecessary.

**Retraction — the static payload was based on a bad experiment.** An earlier
revision of this work used a static zeroed 75-field payload and refused all
automation writes on AI+, on the basis that the ordinary merged payload returned
`999999`. That test ran on an **empty port blocked by a disabled-but-unreleased
Advance Automation**; the `999999` was the automation block, not the payload
shape. On a connected port the merged payload works. The static template has been
removed — do not re-derive it.

**`addDevMode` on AI+ is a live-mode override, not a whole-record replace.** A
port switched to OFF retained its stored VPD target, humidity range and schedule
window. See Quirk 36 for the important limit on that.

Detection:
```python
from ac_infinity_mcp.controller import ControllerType, detect_controller_type
ct = detect_controller_type(device_data)
is_ai_plus = ct == ControllerType.NEW_FRAMEWORK  # devType >= 20 or newFrameworkDevice=True
```

---

### Quirk 35 — On AI+, `modeType` is a per-port resting value, not an ADVANCE signal

On legacy controllers `modeType == 15` means the port is under Advance Automation
control, and `client.py` uses it as a pre-write guard. **That reading does not
hold on AI+.**

Read across all 8 ports of a live `devType=22` controller with no automation
configured anywhere:

| Ports | `modeType` |
|---|---|
| 1, 3, 5, 7 | `0` |
| 2, 4, 6, 8 | `15` |

The value alternates with port parity and tracks nothing about automation. A
second `devType=20` controller shows `modeType=15` on ports carrying ordinary
manual and VPD settings.

Consequently the guard now branches by controller type:

- **Legacy** — unchanged: `modeType == 15` **and** `isOpenAutomation != 0`.
- **AI+** — `modeType` is ignored entirely; `isOpenAutomation` must be **present
  and `0`** or the write is refused. Absent is treated as active (safe-fail).

This matters because the combined legacy condition could never fire on AI+ at
all: the same `devType=22` controller reports `isOpenAutomation = 0` on all 8
ports, in both `getdevModeSettingList` and the `devInfoListAll` port entries. The
guard read as protective while being provably inert.

---

### Quirk 36 — On AI+, `addDevMode` returns `200` for fields it silently discards

**A `200` from `addDevMode` does not mean the fields you sent were stored.** On
AI+, only fields relevant to the port's mode *at the time of the write* persist.
Everything else is accepted and thrown away, with no error and no indication in
the response.

Demonstrated on live `devType=20` hardware, writing to an idle port
(`atType=1`, OFF) and reading back:

| Port state at write | Fields written | Persisted? |
|---|---|---|
| `atType=1` (OFF) | `devHt`, `devLt`, `devHtf`, `devLtf`, `activeHt`, `activeLt` | **no** — every one discarded, code `200` |
| `atType=3` (AUTO), triggers inactive | `targetVpd`, `schedStartTime` | **no** — discarded, code `200` |
| `atType=3` (AUTO), humidity triggers active | `devHh`, `devLh` | yes |
| `atType=3` (AUTO), temp triggers active | `devHtf`, `devHt` | yes |
| one write carrying `atType=3` **plus** `activeHt/activeLt=1` **plus** the trigger values | all of the above | yes — the mode change lands first |

That last row is why the ordinary automation tools work: `set_vpd_automation`,
`set_humidity_automation` and `set_temperature_automation` each send the mode
switch and its trigger fields in a single write, which makes those fields
relevant as they arrive.

The trap is **writing settings a port is not currently using** — storing a
fallback for a mode the port is not in. That is exactly what
`apply_grow_stage_template` does (it sets `atType=8` for VPD while storing temp
and humidity thresholds "for later"), which is why that tool is held on AI+ (see
#316) rather than reporting a success the controller did not honour.

A practical consequence when restoring a port: to put a trigger value back you
must first return the port to the mode that makes it relevant, change it, then
switch the mode back.

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
| `/api/version=2.0/dev/addGroups` | POST | Full form fields (~50 fields), incl. `isFlag` | Creates **or** appends a rule. `isFlag=1` → new program slot; `isFlag=0` + the program's `groupNums`/`sortType` + `subNumber=max+1` → appends to that program. Server assigns `advId` in response. See Quirk 32 |
| `/api/version=2.0/dev/updateGroupsById` | POST | Full form fields + `advId` + `devId` | **Edits a rule in place** by `advId` (same advId, fields updated). The app's rule-edit path — see Quirk 32 |
| `/api/version=2.0/dev/updateGroupsIsOn` | POST | `advId=...&isDel=0&isflag=1` | **TOGGLES** current `isOn` state — server inverts; no explicit `isOn` field |
| `/api/version=2.0/dev/delByid` | POST | `advId=...&isDel=1&isflag=<scope>` | Deletes by `advId`. `isflag=1` → whole program (all rules); `isflag=0` → single rule only. See Quirk 32 |

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
| `sensorType` matches a recognized type (10–20 per `_SENSOR_TYPE_INFO`) | **Always include** — even if current value is `0` (sensor may be connected but reading zero) |
| `sensorType < 10` (not in label dict) | **Always exclude** — internal/built-in bus readings, not external hardware |
| `sensorType >= 10` and unrecognized (future/unknown type) | Include **only if** `sensorData != 0` — zero value on an unknown type is treated as phantom |
| `sensorType` is `null` | **Always exclude** — no type means no sensor slot at all |

**Implementation:**

```python
def _should_include_sensor(s: dict) -> bool:
    sensor_type = s.get("sensorType")
    if sensor_type is None:
        return False
    if sensor_type in _SENSOR_TYPE_INFO:
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

**Recognized sensor types (`_SENSOR_TYPE_INFO` — label + unit):** each `external_sensors`
entry carries a Title-Case `sensor_type_label` and a `unit` string derived from `sensorType`.
Unit variants of the same measurement are distinct type numbers (e.g. EC and TDS), so the
unit is a function of the type — not of the raw `sensorUnit` field (see Quirk 28).

| `sensorType` | `sensor_type_label` | `unit` |
|---|---|---|
| 10 | Soil Moisture | `%` |
| 11 | CO2 | `ppm` |
| 12 | Light | `%` |
| 13 | pH | *(none)* |
| 14 | EC | `µS/cm` |
| 15 | EC | `mS/cm` |
| 16 | TDS | `ppm` |
| 17 | TDS | `ppt` |
| 18 | Water Temp | `°F` |
| 19 | Water Temp | `°C` |
| 20 | Water Level | *(none)* |

The water-temp polarity (`18 = °F`, `19 = °C`) follows the HA `ac_infinity` `const.py`
`SensorType` map and the API's own `waterTempHighValueF`/`waterTempHighValue` convention;
it is **unverified against live hydro hardware**. Types 13 and 20 are genuinely unitless
(empty-string `unit`).

**devType=22 addendum (confirmed via Proxyman capture 2026-05-28):** On devType=22
(UIS CONTROLLER 69 PRO+), the API returns phantom sensor entries with
`sensorType` values of 4, 6, and 7 — all with non-zero `sensorData` values and
`accessPort: 7`. These are internal bus readings, not physically-connected
sensors. Since all real AC Infinity external sensors (soil probes, CO2, light,
pH, EC, TDS, water probes) use types 10–20, any entry with `sensorType < 10` that
is not in `_SENSOR_TYPE_INFO` is treated as internal and filtered. The
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

**Unit labels (resolved — issues #255, #264, #265):** external-sensor readings now carry a
grower-readable `sensor_type_label` and a `unit`, both derived from `sensorType` (see the
`_SENSOR_TYPE_INFO` table in Quirk 20). Each unit variant of a measurement is a distinct
`sensorType` (e.g. `14` = EC µS/cm vs `15` = EC mS/cm), so the unit is a function of the
type. The raw `sensorUnit` field is **not** a unit label — per the HA `ac_infinity`
integration it is only an F/C flag, and it is intentionally not surfaced. Every
`external_sensors` entry now includes a `"unit"` field (empty string for the unitless
types 13 = pH and 20 = Water Level). The water-temp polarity (`18 = °F`, `19 = °C`) follows
the HA `const.py` `SensorType` map and is unverified against live hydro hardware.

---

### Quirk 29 — Per-port mode field is `modeTye` (typo) on the device-list, `modeType` on settings

**Field location:** per-port objects in the device-list response (`devInfoListAll` /
`portValuesInList`) vs. the per-port settings response (`getdevModeSettingList`).

AC Infinity's API spells the per-port mode field **two different ways depending on the endpoint**:

| Source response | Field spelling |
|---|---|
| Device-list / `devInfoListAll` per-port object | **`modeTye`** (missing the `p`) |
| `getdevModeSettingList` per-port settings object | **`modeType`** (correct) |

The misspelling is in AC's firmware itself — confirmed in AC's own decompiled app
(`NetDeviceInfo.java`: `public byte modeTye;`), independent third-party clients
(i8beef's `PortInfo.cs`: `[JsonPropertyName("modeTye")]`), and real `devType=20` captures
(`tests/fixtures/captures/`). It is the same family of typo as `appPasswordl` on the login
endpoint (Quirk 1). Reading `modeType` (correct spelling) from a **device-list** payload
silently returns `None`.

**How this server handles it:** we deliberately read the per-port mode **only from
`getdevModeSettingList`** (correctly spelled `modeType`), never from the device-list. From the
device-list we read only `isOpenAutomation`, `speak`, and `loadState` — none of which are
affected by the typo. So the typo currently bites nothing.

**Guardrail for future work:** if any future change reads the per-port mode from the device-list
(e.g. to skip a `getdevModeSettingList` round-trip — see issue #277), it **must** read
`modeTye` (with a `modeType` fallback for safety): `port.get("modeTye") or port.get("modeType")`.
Note the device-list also carries a separate `curMode` field. (Originally raised as issue #242,
closed as not-a-live-bug after audit; retained here as a guardrail.)

---

### Quirk 30 — Schedule/automation times are in the controller's local clock, not UTC

AC controllers store all schedule and automation times (timer on/off times, schedule
start/end, advance-automation begin/end) against the **controller's own internal clock**,
which is set by the AC Infinity mobile app the last time the controller synced. These times
are **not** UTC and **not** anchored to the phone or server timezone at read/write time.

Consequences:

- A schedule that fires at "6:00 AM" fires at 6:00 AM on the controller's last-synced
  clock, regardless of where the API caller is.
- Physically moving a controller to a new timezone does **not** auto-shift its schedules.
  They keep firing at the same wall-clock hour on the old clock until the controller is
  re-synced.
- The remedy is always the same: open the AC Infinity app and let it re-sync the
  controller's time. This server does not (and cannot reliably) rewrite the controller
  clock, so it does not attempt timezone normalization on schedule fields.

The `create_advance_automation` and `set_port_mode` (SCHEDULE/TIMER path) tool docstrings
carry a grower-readable version of this caveat. (Issue #247.)

---

### Quirk 31 — Session-expiry body code `10003`; read-only-safe re-authentication

When the session token expires, the API does **not** return HTTP 401 — it returns an
HTTP-200 envelope with body `code` `10003` (the community-documented session-expired code).
The client handles this asymmetrically to avoid double-applying writes:

| Call type | Behavior on `10003` (or HTTP 401) |
|---|---|
| **Read** (`session_refreshable=True`) | Re-authenticate once transparently, then retry the read. |
| **Write** (`session_refreshable=False`) | Surface as an API error; **never** replay. A write may have been processed server-side before the session-expiry response, so a silent retry could double-apply state. |

A **refresh-failure cache** bounds re-login to a single attempt: a genuine credential
failure during refresh is cached so concurrent and subsequent callers do not re-hammer the
login endpoint. A transient (e.g. network) failure during refresh is **not** cached, so a
later call can retry. See `_SESSION_EXPIRED_API_CODES` and `_call_with_token_refresh` in
`client.py`. Because re-auth performs a fresh login, it can invalidate the user's mobile-app
session (single-session limitation — see the login-endpoint notes above). (Issue #252.)

---

### Quirk 32 — Advance Automation rule modes: `currentMode` map, per-mode field signatures, and in-place edit via `updateGroupsById`

An Advance Automation **program** is the set of `getGroups` entries sharing one `advName`. A
**rule** is one entry (one `advId`) = a port bitmask (`grouptDevType`), a schedule window, a
`currentMode`, a speed, and any sensor targets. One program may hold multiple rules — the
verified **two-window pattern** is two complementary rules on the same port (e.g. a lights-on
and a lights-off window). This quirk documents how each rule's behavior is encoded, discovered
via live probing on device 8T4TC (devType=18, legacy) on 2026-06-24 (Issue #284).

**`currentMode` map (the rule's behavior type):**

| `currentMode` | Mode (tool `mode`) | Notes |
|---|---|---|
| `1` | **On** (`on`) — fixed speed | No control fields beyond name/ports/schedule/speed (optional `onTime` ramp) |
| `2` | **Off** (`off`) | Port forced off during the window. All trigger/target fields are don't-care (app leaves base defaults). Captured live (program "0624", Rule 4) — this is a real, supported rule type, not the "no rule" pseudo-state described in earlier revisions |
| `3` | **Cycle** (`cycle`) | `cycleOn` / `cycleOff` stored in **seconds** — see "Cycle units" below |
| `4` | **Auto** (`auto`) — temperature/humidity, target or trigger | Sub-mode resolved by `settingMode`/`setSelect` (see below) |
| `6` | **VPD** (`vpd`) — target or trigger | `settingMode=1` + `targetVpd` (target), or `settingMode=0` + `highVpd`/`lowVpd` (trigger) |

**Compositional surface — how the tools map to the encoding.** The rule-write tools expose a
*compositional* surface (`mode` + `control_style` + sensor params) rather than the discrete
named modes earlier revisions used. The mapping, ground-truthed byte-for-byte from app-created
rules (capture program "0624", device 8T4TC, 2026-06-24):

- **Speed range:** `max_level` → `onSpeed`; `min_level` → `offSpeed`. (`minLevel`/`fanLevel` unused = 0.)
- **`control_style`:** `target` → `settingMode=1, setSelect=0`; `trigger` → `settingMode=0, setSelect=1` (Auto trigger) / `setSelect=0` (VPD trigger). Target/trigger are mutually exclusive per sensor.
- **Auto target:** `humidity_target` → `targetHumi`, `targetHumiSwitch=1`. `temp_target_f` is **rejected outright** (unsupported — no temperature hold in the app; #291). Off-target rails are parked at their sentinel values with switches=1.
- **Auto trigger:** `temp_high_f`/`temp_low_f` → `autoHighTempF`/`autoLowTempF` with the matching `auto*TempSwitch=1`; `humidity_high`/`humidity_low` → `autoHighHumi`/`autoLowHumi` with `auto*HumiSwitch=1`. The unused direction's switch → `0`. The server stores switches **as sent** — single-direction triggers are reliable at the rule level.
- **VPD:** `vpd_target` → `targetVpd=round(kPa×10)`, `targetVpdSwitch=1`, `settingMode=1`; `vpd_high`/`vpd_low` → `highVpd`/`lowVpd` = `round(kPa×10)` with matching switch, `settingMode=0`. VPD **target has no direction** (single kPa value). `targetVpd` is **÷10** — `targetVpd=9` → 0.9 kPa (same scaling as the alarm `highVpd` field and the legacy `addDevMode` quirk).
- **Buffer vs transition (per sensor, mutually exclusive):** `*_buffer` → `temperatureFBuff` / `humidityBuff` / `vpdBuff`; `*_transition` → `temperatureFTrans` / `humidityTrans` / `vpdTrans`. Distinguished purely by which family is non-zero. VPD buffer/transition are stored ÷10 like the VPD value.
- **°C drift (do NOT derive):** the app stores the display-unit value (°F) and **parks the other unit at its rail** (e.g. `autoHighTempF=85` pairs with `autoHighTempC=90`, *not* 29 °C). The encoder mirrors the rails verbatim — `_RAIL_TEMP_HIGH_C=90` / `_RAIL_TEMP_LOW_C=0` — rather than converting.

**Cross-mode switches MUST be zeroed — the app renders phantom triggers otherwise (#288).**
The per-mode field signature was corrected after a live read-back diff against the user's own
app-made rules (Gate 5, 2026-06-27). The earlier encoder parked the *other* mode's sensor
families at their rails with **switches left = 1**; the controller stored that faithfully, but
the app then rendered those parked rails as **phantom high/low triggers** on a rule that was
supposed to be a clean target/VPD rule. The fix: when writing one mode, zero the families that
belong to the other mode (value 0/rail **and** switch 0). The verified per-mode signatures
(`client.py` `_apply_vpd` / `_apply_auto`):

| Mode | Active families (switch=1) | Zeroed families (value 0/rail, switch=0) |
|---|---|---|
| **VPD-target** (`currentMode=6`, `settingMode=1`) | `targetVpd`=kPa×10 (`targetVpdSwitch=1`); `highVpd`=same kPa×10 (`highVpdSwitch=1`); `lowVpd`=0 (`lowVpdSwitch=0`) | All auto temp/humidity (`autoHigh/LowTempF/C`, `autoHigh/LowHumi`); both temp/humidity targets (`targetTempF`, `targetHumi`) |
| **VPD-trigger** (`currentMode=6`, `settingMode=0`) | `highVpd`/`lowVpd`=kPa×10 with matching switch; unused direction parked, switch=0; `targetVpd=0`, `targetVpdSwitch=0` | Same auto + temp/humidity-target families as above |
| **Auto** (`currentMode=4`, target or trigger) | The relevant temp/humidity target or trigger families (see compositional list above) | The **entire VPD family** — `highVpd`/`lowVpd`/`targetVpd` all 0 with all VPD switches 0 |

Key points: (1) VPD-target **mirrors** the setpoint into both `targetVpd` and `highVpd` (with
`highVpdSwitch=1`) while `lowVpd` stays off — this is the app's own signature, not an arbitrary
choice. (2) In Auto mode the VPD family is fully inert (all 0/switch 0), not parked at the 99
rail. These were derived by diffing the encoder output against live read-back of the user's
real app rules, then verified live — a class of bug the mock-based unit tests could not catch
(the mocks asserted the encoder's *own* output, not the app's ground truth).

**Cycle units — `cycleOn`/`cycleOff` are stored in SECONDS (discovered Issue #284).** The
controller stores Cycle on/off durations in **seconds**; the app displays **minutes =
seconds ÷ 60**. The Groups encoder writes `cycle_on_minutes × 60` and the decoder shows
`cycleOn // 60`. Verified live: a "30 min on / 90 min off" rule stores `cycleOn=1800` /
`cycleOff=5400` and renders as 30/90 min in the app — whereas a raw value of `30` rendered
as "0 min" (30 s ÷ 60, truncated). Read-back of existing Cycle rules applies the same ÷60.
Note the **deliberate unit difference** between the two cycle surfaces: the Groups tools
`add_automation_rule` / `create_advance_automation` take `cycle_on_minutes` /
`cycle_off_minutes` (minutes, matching the app's Cycle editor and converted ×60 on the
wire); the legacy port-level `set_port_mode` takes `cycle_on_seconds` /
`timer_duration_seconds` (seconds, no conversion). Both are correct for their respective
endpoints.

**Schedule — `switchTime` bitmask (`days` / `continuous`):** bits 0–6 = days (bit0=Mon …
bit6=Sun), bit 7 (128) = continuous flag. Confirmed values:

| `days` / `continuous` | `switchTime` |
|---|---|
| `continuous=True` (24/7) | `255` (= `127 | 128`) |
| all 7 days / `"all"` / default | `127` |
| `"weekdays"` (Mon–Fri) | `31` |
| `"weekends"` (Sat+Sun) | `96` |
| single day, e.g. Monday | `1` (`1 << 0`) |

`onTimeSwitch` is **not** the continuous flag — `switchTime` bit 7 is (Quirk 21 covers the
read-side `onTimeSwitch` interpretation). The earlier `switchTime=255 → Continuous` note
(Quirk 18) is the same fact: `255` = all-days bits *plus* the continuous bit.

**Continuous is the default when no schedule is given (#287).** When a rule is created or added
with **no schedule at all** — `begin_time` and `end_time` both omitted (and for
`add_automation_rule`, `days` also omitted and `continuous` left False) — the tools default to
the **continuous 24/7 toggle (`switchTime=255`)**, matching the app's own default, *not* a
00:00–23:59 windowed rule. Any explicit signal — a `begin_time`/`end_time` window, a `days`
spec, or `continuous=True` — is honored as given. `create_advance_automation`'s response reads
`"Runs continuously (24/7)"` and reports `begin_time`/`end_time` as `"continuous"` in this case.
(`begin_time`/`end_time` are `int | None` on both tools; an explicit window still validates to
0–1439 or the 255 always-active sentinel.)

**Per-port target/setpoint capability — gate on `modeTye`, never `devType` (#288).**
`devInfoListAll` exposes a per-port `modeTye` field (note the API's typo — *not* `modeType`):
observed `15` = target-capable (UIS Pro+ / AI firmware), `0` = legacy port with **no
target/setpoint support**. The capability is **per-port**: a single `devType=22` controller
mixes `0` and `15` across its ports, so a target write must **never** be gated by `devType`.
The tools that issue a target/hold write — `create_advance_automation`/`add_automation_rule`
with `control_style="target"`, `update_automation_rule` resolving to target, `set_vpd_automation`
(always a VPD target), and `apply_grow_stage_template` (sets a VPD target) — first check the
governed ports' `modeTye`. If any governed port reports `modeTye == 0`, the write is rejected
with a friendly *"doesn't support target/hold mode on this controller — use high/low thresholds
(trigger) instead"* message rather than producing a garbage rail-trigger rule. A port that does
**not** report the field is treated as **capable** (never false-blocked), so a device that omits
`modeTye` entirely is unaffected. Helpers: `_ports_without_target_support` /
`_target_capability_error` in `server.py`.

**Temperature target is unsupported (#291).** Separately from the per-port `modeTye` gate, a
*temperature* setpoint (`temp_target_f`, "hold temp at X") is rejected outright in `_validate_auto`
for every tool. The AC Infinity app offers no temperature-hold in Auto mode and renders such a
rule as thresholds; across real app-made rules `targetTempF` is **always** the 32 rail (never a
live setpoint), and the encoder path for it was inferred without a captured sample. Humidity
target and VPD target are supported and verified. The rejection redirects to temperature high/low
thresholds (a trigger) or a VPD target. (A real temperature-hold, if it exists for specific
hardware like a heater load, is a future enhancement gated on a captured app sample.)

**Program = a `(groupNums, sortType)` slot; `addGroups` append is gated by `isFlag`.** A
program is a shared `(groupNums, sortType)` **slot**, and its rules are entries with sequential
`subNumber` (0, 1, 2, …). `addGroups` builds **either** a brand-new program **or** appends a
rule to an existing one, and the lever is the `isFlag` field — *not* `subNumber` sequencing
(the earlier "duplicates on advId / the lever is subNumber" model was the wrong axis):

- **`isFlag=1` → NEW program slot.** The server mints a fresh `(groupNums, sortType)` slot and
  sets `subNumber=0`; any `groupNums`/`sortType` sent in the body are **ignored**. This is the
  `create_advance_automation` path — the program's first rule.
- **`isFlag=0` → APPEND to an existing program.** The server **honors** the sent `groupNums` +
  `sortType` (which must equal the target program's existing slot) and `subNumber`/`subNumberSort`
  (= the program's existing `max(subNumber) + 1`). The new rule joins that program's slot. This
  is the `add_automation_rule` path.

A **multi-rule program** is therefore built by *create* (`isFlag=1`, new slot) followed by one
or more *appends* (`isFlag=0`, reusing that slot) — e.g. the seedling two-window case is one
create + one append, and a four-rule program is one create + three appends. Verified by iOS-app
traffic capture (Proxyman: the app's append sends `addGroups … isFlag=0 & groupNums=1 &
sortType=6 & subNumber=2 & subNumberSort=2`) and confirmed live. This mechanism was only
discoverable by capturing the app's own traffic — probe-and-infer plateaued on the wrong
(`subNumber`) axis until the capture made the `isFlag` lever decisive.

**A name can map to more than one slot.** Programs are keyed by slot, not by `advName`, so two
distinct programs may share a name. `add_automation_rule` resolves the target slot from the
program's existing entries; if a name maps to **multiple** `(groupNums, sortType)` slots it
cannot disambiguate and returns an error asking the user to rename the programs to be unique
before adding the rule.

**"Adv exist!" also fires on genuine overlap:** a program rejects a second rule that governs the
**same port with an overlapping time window** (e.g. a full-day rule overlapping an existing
partial window) — same `code 500 "Adv exist!"`. The complementary **two-window pattern**
(non-overlapping, e.g. 09:00→03:00 + 03:00→09:00) is fine. `add_automation_rule` maps this to a
grower-readable overlap message; the upstream string is never echoed. This is the practical
model: **one rule per port + window**.

**In-place rule edit — `updateGroupsById` (NEW, discovered Issue #284):** To edit a rule in
place, POST the full rule body plus `advId` + `devId` to
`/api/version=2.0/dev/updateGroupsById` → code 200 `'success.'`, same `advId`, fields updated.
The edit body is built **read-before-write** (Quirk 13): start from a `deepcopy` of the live
rule's full `getGroups` body so structural defaults (`switchTime`, `dualZoneSwitch`,
`groupNums`, `sortType`, `subNumber`, …) are preserved, then overlay only the changed fields. A
**mode change** rebuilds the new mode's full per-mode signature above and **zeroes all off-mode
switch/value fields**, so a stale trigger from the previous mode cannot remain active on the
device.

**Delete — `delByid` `isflag` selects scope (verified live, Issue #284):** delete is POSTed to
`/api/version=2.0/dev/delByid` (`advId=<id>&isDel=1&isflag=<scope>`), and the `isflag` field
chooses **what** is deleted:

- **`isflag=1` → delete the ENTIRE program** — the whole `(groupNums, sortType)` slot and **all
  of its rules**. This is the `delete_advance_automation` path (remove the whole automation).
- **`isflag=0` → delete only the SINGLE rule** identified by `advId`, leaving the rest of the
  program's rules intact. This is the `delete_automation_rule` path (remove one rule from a
  multi-rule program). The client selects scope via a `whole_program` flag: `whole_program=True`
  → `isflag=1`, `whole_program=False` → `isflag=0`.

Earlier revisions used `isflag=1` for both tools, which silently wiped the **whole** program when
the intent was to remove a single rule — caught only by live/app Gate-5 testing.

**The delete-wedge:** a rapid sequence of writes can throttle the controller into rejecting
deletes with `error 100001` ("busy"); the rejected rule remains **wedged** until the controller
is restarted (power-cycle clears it). `delete_automation_rule` / `add_automation_rule` map
`error 100001` to a grower-readable "controller is busy — wait and retry, or restart it" message.
The same `100001` wedge can strike an **add/update** mid-write — the rule "may or may not have
applied." Those tools surface a friendly "list the rules before retrying" message rather than
asserting success or failure; the upstream code/text is never echoed.

**Rule-write validation guards (added across Issue #284 review cycles).** These are
input-validation rules enforced before any write, ground-truthed against the lossy storage
round-trip described above:

- **Rail-collision rejection.** A trigger threshold *or* an auto target written **on its
  inactive rail** decodes back as "no rule set" — a silent no-op. The tools reject these values
  up front: `temp_high_f ≥ 194`, `temp_low_f ≤ 32`, `humidity_high ≥ 100`, `humidity_low ≤ 0`,
  `vpd_high ≥ 9.9 kPa`, `vpd_low ≤ 0`, `humidity_target ≤ 0`. **VPD target has no rail** (any
  positive kPa is a real setpoint), so it is exempt; `temp_target_f` is rejected outright as
  unsupported (#291), so its rail check is moot. Rail constants
  live in `automation.py` (`_RAIL_TEMP_HIGH_F=194`, `_RAIL_TEMP_LOW_F=32`, `_RAIL_HUMI_HIGH=100`,
  `_RAIL_HUMI_LOW=0`, `_RAIL_VPD_HIGH=99` i.e. 9.9 kPa, `_RAIL_VPD_LOW=0`, `_RAIL_TARGET_TEMP_F=32`,
  `_RAIL_TARGET_HUMI=0`). This is the same rail family the encoder parks off-direction values on
  (the °C-drift / off-target-rail behavior above): a value *equal to* the rail is
  indistinguishable from "unset," so it is refused.
- **`continuous=False` turns OFF 24/7.** On `update_automation_rule`, `continuous` is `bool | None`.
  An explicit `continuous=False` (with no `days` given) **clears the continuous bit while
  preserving the existing day pattern** — e.g. `switchTime 255 → 127`, not a reset to all-days.
  Implemented as `live_switchTime & ~0x80`. `None` means "leave the schedule alone"; only
  `continuous=True` *sets* the bit (→ 255).
- **One-sided speed-inversion guard.** `_validate`'s `min_level ≤ max_level` check only fires when
  **both** levels are supplied together. A one-sided update (only `min_level` or only `max_level`)
  is therefore cross-checked against the live rule body so it cannot invert the rule
  (`offSpeed > onSpeed`) — returning "The minimum speed can't be higher than the maximum speed."
  The guard only engages when a level was actually supplied, so it never blocks an unrelated edit
  on a rule whose live speeds were already inverted by some other writer.
- **Empty `days` rejected.** `days=[]` (an empty list) is refused with a message naming the valid
  forms (day names, `"all"`, `"weekdays"`, `"weekends"`) — an empty schedule would silently
  disable the rule.

> **Behavior-verification status (Gate 5 passed 2026-06-27):** The `currentMode` map, the
> per-mode field signatures (as corrected by #288 above), the `currentMode=4` setpoint-vs-trigger
> authority (the `settingMode` vs `setSelect` distinction), and the `targetVpd ÷10` factor were
> originally **storage-verified** (read-back of a write to a throwaway **disabled** rule) and are
> now **behavior-verified**: the VPD-target and Auto signatures were diffed against the user's own
> app-made rules via live read-back, the cross-mode-switch phantom-trigger leak was fixed, and the
> full live battery (A1/A2/B/C1/C2/D1/D2/E) passed. Newer fields returned by `getGroups`
> (`sensorModeData`, `triggerSwitch`, `triggerValue`, `targetSwitch`, `targetValue`, `fanLevel`,
> `minLevel`) are `0`/`None` on all legacy entries — purpose unknown, likely AI+/newer-firmware
> only. `currentMode=2` (Off) and a CO2 target field were not located on this device.

### Quirk 34 — `portType` is a per-port device-identity field exposed by NO read endpoint; resolve it from existing rules, never hardcode

Each Advance Automation rule carries a `portType` field that encodes the **device identity** of
the governed port, independent of the rule's behavior:

| `portType` | Device class |
|---|---|
| `0` | Variable-speed fan (the app shows a MIN/MAX speed range) |
| `1` | On/off outlet / power-adaptor (heater, humidifier, lights on a smart plug — no speed range) |

**`portType` is not exposed by any read endpoint.** `devInfoListAll` omits it entirely and
`getdevModeSettingList` does not carry it; both report `loadType == 0` even for an outlet port,
so `loadType` cannot substitute (the on/off-hardware `loadType 4`/`128` signal used elsewhere —
see the `data_quality`/toggle-hardware note in `get_port_activity_report` and Rule A above — is
absent on these controllers). The value lives **only inside existing `getGroups` automation
rules**. A rule-write path that reconstructs the payload from friendly parameters therefore has
no source for it and, before Issue #300, hardcoded `portType: 0` for every rule.

**Symptom of the wrong value (#300, an AI-escape introduced in #284):** writing `portType=0` to a
`portType=1` (outlet/power-adaptor) port makes the AC Infinity app render the rule as a
variable-speed fan rule with a **phantom MIN/MAX speed range**; the rule misbehaves until it is
re-created in the app. `onSpeed` is unaffected — a correct outlet rule keeps its `onSpeed` and
relies on `portType=1` to suppress the speed UI, so the fix is `portType` only.

**How the tools resolve it now.** `create_advance_automation` and `add_automation_rule` no longer
hardcode `portType`; they call `resolve_port_type(raw_entries, ports)` (`client.py`), which
returns the `portType` of the first existing `getGroups` rule whose `grouptDevType` bitmask covers
any target port (rules group same-device-type ports, so the value is consistent) and `0` when no
existing rule governs the port. `build_groups_payload` takes a `port_type` parameter (default `0`,
preserving byte-identity for the golden-payload tests). `create` issues one extra `getGroups` read
to resolve the value on the live path only; that read is best-effort — on failure it falls back to
`portType=0`, adds a grower-readable `note` to the response, and never blocks the write.
`add_automation_rule` resolves from the `getGroups` data it already fetches (no extra read). The
in-place edit/rebuild path (`updateGroupsById`, Quirk 32) already preserved `portType` because it
deep-copies the live rule body and `portType` is in no per-mode signature-key set.

**Documented limitations:** (1) the **first** automation on a fresh outlet port that carries no
prior rules still defaults to `portType=0` — the value is undiscoverable via the read APIs, so
that rule may still need an in-app fix. (2) **Mixed-device-type port grouping is unsupported** —
the resolver assumes all ports in one rule share a device type and returns a single `portType`.

---

### Quirk 33 — v2 endpoints reject the `version`/`requestId` headers with a misleading `403 "Login Expired"` body

The v2.0 endpoints (path prefix `/api/version=2.0/dev/` — the entire Advance-Automation
surface: `getGroups`, `addGroups`, `updateGroupsIsOn`, `updateGroupsById`, `delByid`) must be
called **without** the `version` and `requestId` HTTP headers. AC Infinity's server now
rejects any v2 request carrying **either** header (each one alone trips it) with an otherwise
HTTP-200 envelope whose body is:

```json
{"code": 403, "msg": "Login Expired Please login again!"}
```

The message is **misleading** — the session is valid and a fresh re-login does not fix it. The
root cause is a **vendor server-side contract change**: the server now requires a valid `sign`
request signature alongside `version`/`requestId`, and this client does not compute `sign`.
Both headers shipped in `_v2_headers()` from Phase 17 (#49) and were accepted at capture time;
the contract tightened underneath us, silently breaking every v2 read and write. The fix
removes both headers so the request authenticates on the `token` header alone — the same
posture the legacy v1 endpoints already rely on. The remaining app-identity headers
(`phoneType`, `devType`, `appVersion`, `languageType`, `languageVersion`) are individually
proven harmless and left in place. See `_v2_headers` in `client.py`. (Issue #298.)

Because this contract is enforced only by the live vendor server, it is **invisible to the
mocked test suite** — a regression re-adding either header would pass CI while breaking the
live Advance-Automation surface. A unit test now asserts `_v2_headers()` omits both, but the
authoritative check is a live smoke test of the v2 surface after any vendor app update.

**Read-path defense (companion to Quirk 31):** a genuine future v2 session expiry may also
arrive shaped as `403` + a "login expired" / "login again" message rather than code `10003`.
`_raise_for_api_code` now treats that shape as a refreshable session expiry **on the read path
only** — gated on `code == 403` **and** `session_refreshable=True` **and** the message carrying
one of the `_SESSION_EXPIRED_MSG_MARKERS` (`"login expired"`, `"login again"`), matched
null-safely via `(error_msg or "").lower()`. It is keyed on the message, never the bare `403`
code, so write-path 403s (rate-limit `"Data saving failed"`, field-validation errors) are never
misclassified — writes pass `session_refreshable=False` and are unaffected. A real expiry thus
self-heals through the existing one-shot transparent re-auth (Quirk 31); the misleading Bug-1
403 does not, which is why removing the headers is the actual fix and this is defense only.

---

## v2.0 API Endpoints Reference

All endpoints below use the base URL `https://www.acinfinityserver.com/api` and require
`Content-Type: application/x-www-form-urlencoded; charset=utf-8` plus `token: <appId>` header.
They must **not** carry the `version`/`requestId` headers — the server rejects those with a
misleading `403 "Login Expired"` body (see Quirk 33). All use HTTPS (TLSv1.3 — see Quirk 8).

### Automation Management

| Endpoint | Method | Request body | Response notes |
|---|---|---|---|
| `/api/version=2.0/dev/getGroups` | POST | `devId=<devId>` | Returns list of automation group objects; each has `advId`, `advName`, `isOn`, `onSpeed`, `offSpeed`, etc. |
| `/api/version=2.0/dev/addGroups` | POST | Full form (~50 fields): `advName`, `devId`, `grouptDevType`, `currentMode`, `isOn`, `onSpeed`, `offSpeed`, `beginTime`, `endTime`, `groupNums`, `sortType`, `subNumber`, `isFlag`, `returnData=1`, + others | Creates a new program (`isFlag=1`) or appends a rule to an existing one (`isFlag=0` + the program's slot + `subNumber=max+1`); returns the automation object with server-assigned `advId`. See Quirk 32 |
| `/api/version=2.0/dev/updateGroupsById` | POST | Full rule body (read-before-write) + `advId` + `devId` | Edits a rule in place by `advId`; preserves structural defaults from the live rule (Quirk 13 + Quirk 32) |
| `/api/version=2.0/dev/updateGroupsIsOn` | POST | `advId=<id>&isDel=0&isflag=1` | Toggles `isOn` state server-side; no explicit `isOn` field in body |
| `/api/version=2.0/dev/delByid` | POST | `advId=<id>&isDel=1&isflag=<scope>` | Deletes by `advId`. `isflag=1` → whole program (all rules, `delete_advance_automation`); `isflag=0` → single rule only (`delete_automation_rule`). See Quirk 32 |

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
  "external_sensors": [
    {"sensor_id": "9.11", "sensor_type": 11, "sensor_type_label": "CO2", "value": 793, "unit": "ppm"},
    {"sensor_id": "9.18", "sensor_type": 18, "sensor_type_label": "Water Temp", "value": 68.5, "unit": "°F"},
    {"sensor_id": "9.13", "sensor_type": 13, "sensor_type_label": "pH", "value": 6.5, "unit": ""}
  ],
  "probes": [
    {"sensor_port": 2, "temperature": 19.1, "unit": "°C", "humidity": 81.6, "vpd": 0.39}
  ],
  "human_summary": "Towlie Tent: 24.3°C, 58.2% RH, VPD 1.31 kPa. Probe Sensor (Sensor Port 2): 19.1°C, 81.6% RH, VPD 0.39 kPa. External sensors — CO2: 793 ppm, Water Temp: 68.5°F, pH: 6.5. Reading from 2026-05-20T09:32:00-05:00."
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
  "probes": [],
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
- `external_sensors` — list of UIS sensor readings when sensors are attached; phantom entries (API-reported but no hardware connected) are filtered out (Quirk 20); empty `[]` for built-in-only devices. Each entry: `sensor_id`, `sensor_type` (raw int), `sensor_type_label` (Title Case, e.g. `"CO2"`, `"Water Temp"`), `value`, and `unit` (derived from `sensor_type` per Quirk 20/28; empty string for unitless types pH and Water Level)
- `probes` — readings from plug-in **AC-SPC24 sensor probes** (`sensorType` 0-3). The controller's own onboard sensor (`sensorType` 4-7) is excluded *by type*, because it is already reported as the top-level `temperature`/`humidity`/`vpd`; identifying it by type rather than by comparing values means a probe reading identically to the onboard sensor is still surfaced, and a probe present with no onboard group is too. Each entry: `sensor_port` (the `accessPort` the probe is attached to), `temperature`, `unit`, `humidity`, `vpd` — temperature in the device's preferred unit, like every other reading. Empty `[]` when no probe is attached. Groups with a missing or `null` member, an all-zero triplet (the Quirk 20 phantom class), or an unrecognized `sensorType < 10` shape are skipped and logged at INFO. Raw probe scale is decided by the per-entry `sensorUnit` flag (`>0` = already Celsius), falling back to the type itself (0 = °F, 1 = °C) when absent.
- `human_summary` — one-line natural language summary: `"DeviceName: N°U, N% RH, VPD N kPa.[ External sensors — Label: value unit, …] Reading from <timestamp>."` The `External sensors —` clause is present only when external sensors are attached; `%`/`°C`/`°F` attach to the number, other units (ppm, ppt, µS/cm, mS/cm) take a leading space. Always present.

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
      "external_sensors": [],
      "probes": []
    }
  ]
}
```

**Field notes:**
- Each entry has the same shape as `get_device_reading` (including `temperature` / `unit` in device-preferred units)
- `ports[].plug_status` — present on not-powered, default-named (`"Port N"`) port entries (same `loadState == 0` AND `speak == 0` AND default-name condition as `get_device_reading`); omitted for custom-named ports or running ports
- Devices that fail to parse individually include `"error"` instead of sensor fields
- Useful for a dashboard view across multiple tents/controllers
- `external_sensors` — phantom sensor entries (sensors present in the API response but with no hardware connected) are filtered out; see Quirk 20. Each entry carries `sensor_type_label` and `unit` (same shape as `get_device_reading`)
- `probes` — plug-in AC-SPC24 probe readings, same shape and filtering as `get_device_reading`; empty `[]` when no probe is attached
- `human_summary` — one-line prose for 1–2 parseable devices (each device's prose gains an `External sensors — …` clause when sensors are attached); markdown table (`| Device | Temp | Humidity | VPD |`) for 3+ parseable devices (table is unchanged — no sensor clause); `"No readings available."` when all fail. Always present at the top level.

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

**Validation:** `target_vpd` must be 0.1–3.0. Sub-0.1 kPa and over-3.0 kPa are rejected. Because this is a VPD **target/hold**, the port must support setpoints — a port reporting `modeTye == 0` is rejected with a "use high/low thresholds instead" message (#288 / Quirk 32).

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
- **Learning-protection rationale (#250):** for AI-automated ports, the conflict response explains that manual override is blocked specifically to protect the pattern the controller is actively learning, rather than presenting the block as a generic permission error. This frames the lock as intentional grow-protection so the grower understands why the change was deferred and what to do instead.

---

## Shared (read-only) controllers — write behavior (#249)

Controllers shared from another AC Infinity account carry `isShare == 1` in the device-list
response. The AC Infinity API rejects writes to these controllers with a "No Permission"
error. Rather than attempting the write and surfacing the raw API error, every write tool
checks `isShare` first (`for_write=True` in the shared device-list guard) and returns a
grower-readable read-only message naming the controller:

> "<Device> is shared with you from another AC Infinity account, so it's read-only — you
> can view its readings but can't change its settings from here."

Read tools leave the guard off (`for_write=False`), so shared controllers remain fully
viewable. The guard fires before any dry-run handling, so a shared device is blocked even in
preview mode. The shared-device guard lives in `server.py`.

---

## Per-endpoint User-Agent values (#251)

The client deliberately sends AC-app-style `User-Agent` headers (not the default
`python-requests` UA) so requests are indistinguishable from the official app:

| Endpoint class | `User-Agent` |
|---|---|
| Login (`/user/appUserLogin`) | `ACController/1.8.2 (com.acinfinity.humiture; build:489; iOS 16.5.1)` |
| Data / write endpoints (device list, history, mode read/write, automation) | `okhttp/3.10.0` |

These values are also shown inline in each endpoint's **Headers** block above. A regression
test locks both strings so a future refactor cannot silently revert to the default UA.

---

## MCP Intelligence Tool

### `apply_grow_stage_template(device_id, port, stage, dry_run=True)`

One-click grow stage configuration. Calls `set_vpd_automation`, `set_temperature_automation`,
and `set_humidity_automation` in sequence using the VPD midpoint and full ranges from
`STAGE_TARGETS` in `analytics.py`.

> **Held on AI+ (`devType >= 20`) — live writes refused, previews unaffected (#316).**
> This tool sets `atType=8` (VPD) while also storing temperature and humidity
> thresholds as a fallback for a later switch to AUTO. On AI+ a field that is not
> relevant to the port's mode at write time is accepted with code `200` and
> silently discarded (Quirk 36) — which is precisely what those fallback fields
> are. It also never writes `devLtf`/`devHtf`, so on a °F AI+ the °F pair stays
> stale. `dry_run=True` works normally; for live changes use `set_vpd_automation`,
> `set_temperature_automation` and `set_humidity_automation`, which send each mode
> switch together with its own trigger fields and are verified on AI+.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `port` | `int` | 1-based port number |
| `stage` | `str` | One of: `clones`, `seedling`, `veg`, `early_flower`, `mid_flower`, `late_flower` |
| `dry_run` | `bool` | Default `True` — returns payloads without writing |

The template sets a VPD **target** (`vpdSettingMode=1`), so the port must support setpoints: a port reporting `modeTye == 0` is rejected with a "use high/low thresholds instead" message (#288 / Quirk 32).

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
- `rules` — per-rule read parity (one entry per port group / `advId`), decoded so the wording matches exactly what the rule-write tools emit. Each entry is `{"ports", "control", "speed", "window", "running", "_mode"}`:
  - `ports` — name+number port label (e.g. `"Humidifier (Port 1)"`)
  - `control` — grower-readable behavior string, e.g. `"runs at speed 5"`, `"cycle 30 min on / 30 min off"`, `"hold humidity at 65%"`, `"hold VPD at 0.9 kPa"`, `"run when temp rises above 82°F"`, `"run when humidity drops below 50%"`
  - `window` — `"HH:MM–HH:MM (timezone)"`; wrap-around windows (begin > end) display as-is for the two-window pattern
  - `running` — per-rule run state (`run_state`); different rules in one program may have different run states (e.g. complementary lights-on / lights-off windows)
  - `_mode` — internal round-trip key (`off`/`on`/`cycle`/`auto`/`vpd`); underscore-prefixed = not surfaced to the grower; Claude reads `control` + `window` (see Quirk 32)
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

### `create_advance_automation(device_id, name, on_speed, port, off_speed=0, begin_time=None, end_time=None, mode="on", control_style=None, temp_high_f=None, temp_low_f=None, humidity_high=None, humidity_low=None, temp_target_f=None, humidity_target=None, vpd_target=None, vpd_high=None, vpd_low=None, cycle_on_minutes=None, cycle_off_minutes=None, dry_run=True)`

Create a new Advance Automation on a device. Defaults to `dry_run=True` for safety. Set `dry_run=False` to send the automation to the device. The port bitmask (`grouptDevType`) is computed automatically from the port number (Port N → 2^(N-1)).

The optional `mode` parameter sets the behavior of the automation's **first rule**. The default `mode="on"` reproduces the original single-port On-mode payload byte-for-byte (`on_speed` becomes `onSpeed`, `off_speed` becomes `offSpeed`/MIN level). Other modes (`off`, `cycle`, `auto`, `vpd`) take the **same compositional per-mode params as `add_automation_rule`** (see that tool and Quirk 32 for the encoding). `auto` and `vpd` require `control_style` (`target` or `trigger`).

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `name` | `str` | Automation name (max 64 chars; control chars stripped) |
| `on_speed` | `int` | Fan speed when active (1–10) — becomes the rule's MAX level (`onSpeed`) |
| `port` | `int` | 1-based port number the automation should control (1–8) |
| `off_speed` | `int` | Minimum fan level when inactive (0–10) — becomes the rule's MIN level (`offSpeed`). Default: 0 |
| `begin_time` | `int \| None` | Schedule start as minutes since midnight (0–1439, or 255 = always active). **Omit (with `end_time`) for a continuous 24/7 rule** — the app's default toggle (#287, Quirk 32) |
| `end_time` | `int \| None` | Schedule end as minutes since midnight (0–1439, or 255 = always active). **Omit (with `begin_time`) for a continuous 24/7 rule** |
| `mode` | `str` | First-rule behavior: `on` (default), `off`, `cycle`, `auto`, `vpd`. Default `"on"` is the legacy byte-identical path. |
| `control_style` | `str \| None` | `target` or `trigger` — **required** for `mode="auto"` and `mode="vpd"` |
| `temp_high_f` / `temp_low_f` | `int \| None` | Turn on above / below this °F (auto **trigger**) |
| `humidity_high` / `humidity_low` | `int \| None` | Turn on above / below this % RH (auto **trigger**) |
| `temp_target_f` | `int \| None` | **NOT SUPPORTED** — temperature hold isn't offered by the app; rejected with a redirect to thresholds or a VPD target (#291) |
| `humidity_target` | `int \| None` | Hold this % RH (auto **target**) |
| `vpd_target` | `float \| None` | Hold this kPa (vpd **target**, 0.0–9.9) |
| `vpd_high` / `vpd_low` | `float \| None` | Turn on above / below this kPa (vpd **trigger**) |
| `cycle_on_minutes` / `cycle_off_minutes` | `int \| None` | Minutes on / off (for `mode="cycle"`) |
| `dry_run` | `bool` | Default `True` — previews without sending. Set to `False` to create the automation on the device. |

**Wrap-around windows are permitted on create.** `begin_time > end_time` (e.g. a lights-on window 09:00→03:00) is allowed, consistent with `add_automation_rule` and the controller itself. (Earlier revisions rejected wrap-around on create with a `begin_time <= end_time` guard; that guard has been removed.) Use `add_automation_rule` to add the complementary second window for the two-window pattern.

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
- `off_speed` — now sent to the device as the rule's MIN level (`offSpeed`); default `0`. (Earlier revisions discarded this parameter; the compositional rebuild wires it through.)
- `begin_time` / `end_time` — returned as `"HH:MM"` formatted strings in the response (input is still minutes-since-midnight integer); use 255 for "always active" (maps to full-day range 0/1439). When both are **omitted** (continuous-default, #287) they are returned as the string `"continuous"`
- `schedule_summary` — human-readable schedule description (e.g. `"Active 10:00 PM – 6:00 AM"`, `"Always active"`, or `"Runs continuously (24/7)"` when no schedule was given)
- `automation_id` — server-assigned `advId`; present in live response for programmatic chaining only — do not surface to the user; reference the automation by `name` instead
- `automation_id_note` — in-band reminder that `automation_id` is internal
- `note` — present in dry_run response only; prompts user to confirm before creating
- `switchTime` — `255` (Continuous, all-days bits + continuous bit) when no schedule is given (continuous-default, #287); otherwise `127` (all 7 days, binary `01111111`) for a windowed rule (see Quirk 18 / Quirk 32)

**Validation:** `on_speed` 1–10; `off_speed` 0–10; when a window is given, `begin_time` and `end_time` each 0–1439 or both 255 (both must be 255 or neither); omit both for a continuous 24/7 rule (#287); wrap-around windows (`begin_time > end_time`) are allowed; `name` must not be empty or all control characters. A `target` rule is rejected if the port reports `modeTye == 0` (#288, Quirk 32).

---

### `delete_advance_automation(device_id, automation_id, dry_run=True)`

Delete an Advance Automation — removes the **entire program** (the whole `(groupNums, sortType)` slot and all of its rules) via `delByid` `isflag=1` (Quirk 32). To remove a single rule from a multi-rule program while keeping the others, use `delete_automation_rule` instead. If currently enabled, disables it first.

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

> **Held on AI+ (`devType >= 20`) — live writes refused, previews unaffected (#316).**
> This tool issues one live write per co-governed port plus the target, and its
> rollback path re-enables the automation without unwinding co-ports it has
> already switched to manual — leaving those ports pinned manually *and* claimed
> by a re-enabled automation. Multi-port partial-failure handling needs its own
> fix and its own tests before this runs live on AI+ hardware.

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

### `add_automation_rule(device_id, program_name, ports, mode, control_style=None, min_level=0, max_level=10, temp_high_f=None, temp_low_f=None, humidity_high=None, humidity_low=None, temp_target_f=None, humidity_target=None, vpd_target=None, vpd_high=None, vpd_low=None, temp_buffer=None, temp_transition=None, humidity_buffer=None, humidity_transition=None, vpd_buffer=None, vpd_transition=None, cycle_on_minutes=None, cycle_off_minutes=None, begin_time=None, end_time=None, days=None, continuous=False, dry_run=True)`

Append one rule to an existing Advance Automation program (matched by `program_name`). A **rule** is one schedule window + behavior for one or more ports inside that program. Defaults to `dry_run=True`. See Quirk 32 for the per-mode encoding.

The surface is **compositional**, mirroring the AC Infinity app's rule editor: a `mode` chooses the behavior, and `auto`/`vpd` add a `control_style` (`target` vs `trigger`) plus the sensor params that style needs. The tool infers `control_style` from phrasing: "hold/keep/maintain at X" → `target`; "above/below/when it rises|drops/turn on at" → `trigger`.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `program_name` | `str` | Name of the existing program to add the rule to |
| `ports` | `list[int]` | One or more 1-based port numbers this rule controls (bitmask computed internally) |
| `mode` | `str` | `off`, `on`, `cycle`, `auto`, or `vpd` |
| `control_style` | `str \| None` | `target` or `trigger` — **required** for `auto` and `vpd` |
| `min_level` / `max_level` | `int` | Minimum (inactive) / maximum (active) fan level 0–10. Defaults 0 / 10. `MIN→offSpeed`, `MAX→onSpeed` |
| `temp_high_f` / `temp_low_f` | `int \| None` | Turn on above / below this °F (auto **trigger**), 32–212 |
| `humidity_high` / `humidity_low` | `int \| None` | Turn on above / below this % RH (auto **trigger**), 0–100 |
| `temp_target_f` | `int \| None` | **NOT SUPPORTED** — temperature hold isn't offered by the app; rejected with a redirect to thresholds or a VPD target (#291) |
| `humidity_target` | `int \| None` | Hold this % RH (auto **target**) |
| `vpd_target` | `float \| None` | Hold this kPa (vpd **target**, 0.0–9.9) |
| `vpd_high` / `vpd_low` | `float \| None` | Turn on above / below this kPa (vpd **trigger**) |
| `temp_buffer` / `temp_transition` | `int \| None` | Temperature deadband / ramp band °F (auto). **Mutually exclusive per sensor** |
| `humidity_buffer` / `humidity_transition` | `int \| None` | Humidity deadband / ramp band % (auto). Mutually exclusive |
| `vpd_buffer` / `vpd_transition` | `float \| None` | VPD deadband / ramp band kPa (vpd). Mutually exclusive |
| `cycle_on_minutes` / `cycle_off_minutes` | `int \| None` | Minutes on / off (for `mode="cycle"`, 0–1439) |
| `begin_time` / `end_time` | `int \| None` | Window start / end, minutes since midnight (0–1439). **Wrap-around (begin > end) is permitted** — a lights-on window like 09:00→03:00 is allowed. **Omit both** (with `days` omitted and `continuous=False`) for a continuous 24/7 rule — the app's default (#287) |
| `days` | `list[str] \| str \| None` | Day names (`mon`–`sun`), or `"all"` / `"weekdays"` / `"weekends"`. Default: all 7 days. Supplying any value opts out of the continuous-default |
| `continuous` | `bool` | Run 24/7, ignoring the window (sets `switchTime=255`). Default `False`. Note: when no schedule signal is given at all, the rule defaults to continuous anyway (#287) |
| `dry_run` | `bool` | Default `True` — previews the rule without sending |

**Buffer vs transition:** a *buffer* is a deadband (the fan holds until the reading crosses the band); a *transition* ramps fan speed across the band. Pick at most one per sensor.

**Response (dry_run=True):**
```json
{
  "action": "add rule to 'Seedling'",
  "program_name": "Seedling",
  "rule": {
    "ports": "Humidifier (Port 1)",
    "control": "hold humidity at 65%",
    "window": "03:00–09:00 (America/Chicago)",
    "_mode": "auto"
  },
  "dry_run": true,
  "sent": false,
  "note": "Preview only — nothing sent yet. Confirm to add this rule."
}
```

**Response (live, dry_run=False):** replaces `dry_run`/`sent` with `false`/`true` and adds a `human_summary` line (e.g. `"Added a rule on Humidifier (Port 1) (hold humidity at 65%) for 03:00–09:00 (America/Chicago)."`).

**Response (program not found):** `{"error": "No program named '...' on device ...", "existing_programs": [...], "suggested_reply": "..."}`.

**Response (overlap / busy controller):** A second rule on the same port + overlapping window is rejected by the controller (`"Adv exist!"` upstream — Quirk 32) and surfaces as `{"error": "A rule already covers those ports during that window — pick a different time or update the existing rule."}`. A throttled/busy controller (`error 100001`) surfaces as `{"error": "The controller didn't accept that — it may be busy; wait and retry, or restart the controller."}`. The upstream text is never echoed to the client.

**Field notes:**
- `control` wording is identical on dry-run, live, and read-back via `get_advance_automation` (round-trip parity)
- `_mode` is internal/round-trip only — not surfaced to the grower
- The new rule is appended into the program's existing slot via `addGroups` with `isFlag=0`, reusing the program's `groupNums`/`sortType` and `subNumber = max(existing subNumbers) + 1` (Quirk 32). If the program name maps to more than one slot, the tool returns a disambiguation error asking the user to rename the programs to be unique
- Validation (before any write): temp 32–212 °F; humidity 0–100 %; VPD 0.0–9.9 kPa; `min_level`/`max_level` 0–10 with `min ≤ max`; `cycle_*_minutes` 0–1439; `low < high` when both thresholds given; buffer XOR transition per sensor; target XOR trigger per sensor; any param irrelevant to the chosen `mode` is rejected

---

### `update_automation_rule(device_id, program_name, ports, begin_time=None, end_time=None, mode=None, control_style=None, min_level=None, max_level=None, temp_high_f=None, temp_low_f=None, humidity_high=None, humidity_low=None, temp_target_f=None, humidity_target=None, vpd_target=None, vpd_high=None, vpd_low=None, temp_buffer=None, temp_transition=None, humidity_buffer=None, humidity_transition=None, vpd_buffer=None, vpd_transition=None, cycle_on_minutes=None, cycle_off_minutes=None, new_begin_time=None, new_end_time=None, days=None, continuous=None, dry_run=True)`

Edit one existing rule in place (via the `updateGroupsById` endpoint — Quirk 32). The rule is found by `program_name` + `ports`, optionally disambiguated by the **current** window (`begin_time`/`end_time`). Only the fields you supply change; everything else is preserved read-before-write from the live rule. Defaults to `dry_run=True`.

Two edit shapes:
- **Same-mode tweak** (omit `mode`): only the supplied sensor/cycle/level/window fields are overlaid onto the live rule body — no signature rebuild.
- **Mode change** (set `mode`): the full new-mode signature is rebuilt and **all off-mode switch/value fields are zeroed**, so a stale trigger from the previous mode cannot remain active. Set `control_style` when changing to `auto`/`vpd`.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `program_name` | `str` | The program the rule belongs to |
| `ports` | `list[int]` | The port number(s) the target rule controls (used to find the rule) |
| `begin_time` / `end_time` | `int \| None` | **Selector** — the rule's *current* window, to disambiguate when the program has more than one rule on these ports |
| `mode` | `str \| None` | New behavior type (`off`/`on`/`cycle`/`auto`/`vpd`). Omit to keep. A mode change rebuilds the full per-mode signature and zeroes off-mode switches (Quirk 32) |
| `control_style` | `str \| None` | `target` or `trigger` (when changing mode to `auto`/`vpd`) |
| `min_level` / `max_level` | `int \| None` | New minimum (inactive) / maximum (active) fan level 0–10 |
| `temp_high_f` / `temp_low_f` / `humidity_high` / `humidity_low` | `int \| None` | New auto **trigger** thresholds (°F / % RH) |
| `humidity_target` | `int \| None` | New auto humidity **target** setpoint (gated on `modeTye` — a port reporting `modeTye == 0` is rejected, #288 / Quirk 32) |
| `temp_target_f` | `int \| None` | **NOT SUPPORTED** — rejected with a redirect to thresholds or a VPD target (#291) |
| `vpd_target` / `vpd_high` / `vpd_low` | `float \| None` | New VPD target / thresholds (kPa) |
| `temp_buffer` / `temp_transition` / `humidity_buffer` / `humidity_transition` | `int \| None` | New buffer / transition bands (auto; buffer XOR transition per sensor) |
| `vpd_buffer` / `vpd_transition` | `float \| None` | New VPD buffer / transition band kPa (vpd; XOR) |
| `cycle_on_minutes` / `cycle_off_minutes` | `int \| None` | New cycle on/off minutes |
| `new_begin_time` / `new_end_time` | `int \| None` | **New** window (minutes since midnight, 0–1439) to move the rule to |
| `days` | `list[str] \| str \| None` | New day spec (day names, `"all"`, `"weekdays"`, `"weekends"`) |
| `continuous` | `bool \| None` | `True` runs 24/7 (sets `switchTime` bit 7); `False` clears the 24/7 bit while keeping the existing day pattern (e.g. 255→127); omit (`None`) to leave the schedule unchanged (Quirk 32) |
| `dry_run` | `bool` | Default `True` — previews the change without sending |

**Response (more than one rule matches):**
```json
{
  "error": "More than one rule matches — pick which window to edit.",
  "program_name": "Seedling",
  "matching_rules": [
    {"ports": "Humidifier (Port 1)", "control": "hold humidity at 65%", "window": "03:00–09:00 (America/Chicago)", "running": false},
    {"ports": "Humidifier (Port 1)", "control": "hold VPD at 0.9 kPa", "window": "09:00–03:00 (America/Chicago)", "running": true}
  ],
  "suggested_reply": "There's more than one rule on those ports. Which window should I edit?"
}
```
The disambiguation list never contains `advId`.

**Field notes:**
- No-op rejection: supplying zero change fields → `{"error": "Nothing to change — supply at least one field to update."}`, no write attempted
- Stale-advId guard: the `advId` is re-resolved from a fresh `getGroups` at write time; if the rule vanished between resolve and write the API error surfaces as a typed `ACInfinityAPIError`
- Update body is `deepcopy` of the live rule + overlay (read-before-write, Quirk 13/32) — the builder is not used to construct the update body, so structural defaults are preserved

---

### `delete_automation_rule(device_id, program_name, ports, begin_time=None, end_time=None, dry_run=True)`

Remove one rule from a program (via `delByid`), leaving the rest of the program intact. The rule is found by `program_name` + `ports`, optionally disambiguated by window. Defaults to `dry_run=True`.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `program_name` | `str` | The program the rule belongs to |
| `ports` | `list[int]` | The port number(s) the target rule controls |
| `begin_time` / `end_time` | `int \| None` | Selector to disambiguate when more than one rule matches |
| `dry_run` | `bool` | Default `True` — previews the deletion without performing it |

**Response (dry_run=True):**
```json
{
  "action": "remove rule from 'Seedling'",
  "program_name": "Seedling",
  "rule": {
    "ports": "Humidifier (Port 1)",
    "control": "hold humidity at 65%",
    "window": "03:00–09:00 (America/Chicago)",
    "_mode": "humidity"
  },
  "dry_run": true,
  "sent": false,
  "note": "Preview only — nothing removed yet. Confirm to remove this rule."
}
```

**Field notes:**
- More-than-one-match returns the same `matching_rules` disambiguation shape as `update_automation_rule` (no `advId`)
- Stale-advId guard: re-resolves from a fresh `getGroups` at write time

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
