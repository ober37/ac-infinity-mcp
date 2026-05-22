# AC Infinity API Reference

## Overview

- **Base URL:** `http://www.acinfinityserver.com/api` (HTTP only — see Security Note)
- **Auth:** form-POST to `/user/appUserLogin`; session token returned in `data.appId` field
- **All requests:** `Content-Type: application/x-www-form-urlencoded; charset=utf-8`
- **All responses:** `{"code": 200, "msg": "...", "data": ...}`
- **Non-200 codes** indicate errors (e.g. 400 for bad credentials, 500 for server fault)

## Security Note

The AC Infinity cloud API uses HTTP only (no TLS). This is a known upstream limitation
and is an accepted risk for local/trusted network deployments. See `docs/DEPLOYMENT.md`
for HTTPS reverse-proxy setup options.

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

## All 16 Known API Quirks

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

### Quirk 8 — HTTP only (no TLS)

The base URL `http://www.acinfinityserver.com/api` uses plain HTTP. The upstream AC
Infinity app does not support HTTPS. Session tokens and sensor data are transmitted
unencrypted. This is an accepted risk for local/trusted network use. See
`docs/DEPLOYMENT.md` for HTTPS reverse-proxy options if exposure is a concern.

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

### Quirk 18 — Advance Automation API endpoints not yet confirmed

The AC Infinity app manages Advance Automations (named programs that govern multiple
ports) via API endpoints that have not been identified through REST probing.

**Probing summary (Phase 17, 2026-05-22):** Over 200 endpoint guesses tried across
8 probe scripts: `/dev/getAutoList`, `/dev/getAdvanceList`, `/dev/advanceList`,
`/dev/sceneList`, `/dev/workModeList`, and 200+ variants with different HTTP methods,
Content-Type headers, path prefixes (`/v2/`, `/advanced/`, etc.), `devCode`- and
`appId`-based payloads, and alternative subdomain attempts. All returned HTTP 404.

**What IS accessible without a dedicated endpoint:**
- Whether a port is under automation: `isOpenAutomation` field in `devInfoListAll`
- Automation "mode" confirmation: `modeType=15` in `getdevModeSettingList`
- Automation grouping hint: `portParamData` shared value (see Quirk 17)
- Current `speak` (effective power level): from `devInfoListAll`

**What requires a network capture from the AC Infinity app:**
- Automation name (e.g. "Moderate Airflow")
- Automation ID (for enable/disable/delete API calls)
- List of automations per device
- Enable/disable/create/delete endpoint URLs and payloads

**Follow-up:** Capture app traffic (iOS/Android proxy via Charles/mitmproxy) while
listing, enabling, and disabling automations in the AC Infinity app. Document new
endpoint(s) here and in `docs/SECURITY-RISKS.md`.

---

## MCP Tool Reference

This section documents the MCP tool interfaces — parameters, return schemas, and encoding
notes. All tools return JSON strings. On failure every tool returns `{"error": "...", "detail": "..."}`.

---

### `get_port_activity_report(device_id, days=7)`

Build a per-port runtime activity report from historical data. Calls `get_historical_readings`
internally then runs pure analytics calculations — no additional API calls.

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
  "readings_used": 1440,
  "ports": [
    {
      "port": 1,
      "name": "Inline Fan",
      "on_hours": 12.5,
      "off_hours": 11.5,
      "transitions": 4,
      "avg_speed_when_running": 5.2,
      "uptime_pct": 52.1,
      "peak_hour_utc": 14
    }
  ]
}
```

**Field notes:**
- `on_hours` / `off_hours` — calculated from raw historical records; total is `days * 24` when full data is available
- `transitions` — number of on↔off state changes in the period
- `avg_speed_when_running` — average `onSpead` value (1–10) across on-readings with non-zero speed
- `uptime_pct` — `on_hours / (on_hours + off_hours) * 100`, rounded to 1 decimal
- `peak_hour_utc` — UTC hour (0–23) with the most on-readings; `0` when no on-readings exist

---

### `get_port_status(device_id, port)`

Get the live operational status of a single port. Reads real-time fields from
`/api/user/devInfoListAll` that are not exposed by `get_device_reading`.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` (e.g. `"C58ZA"`) |
| `port` | `int` | 1-based port number |

**Response:**
```json
{
  "device_id": "C58ZA",
  "port": 1,
  "port_name": "Intake Fan",
  "power_level": 5,
  "load_detected": true,
  "mode": "AUTO",
  "remain_time_seconds": 0
}
```

**Field notes:**
- `power_level` — actual current power level 0–10 from `speak` API field
- `load_detected` — `true` when a device is physically plugged into the port (`loadState=1`)
- `mode` — one of: `OFF`, `ON`, `AUTO`, `VPD`, `TIMER_TO_ON`, `TIMER_TO_OFF`, `CYCLE`, `SCHEDULE`
- `remain_time_seconds` — countdown timer seconds from `remainTime` field; `0` when no active timer

---

### `get_port_settings(device_id, port)`

Get the full automation configuration for a port from `/api/dev/getdevModeSettingList`.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `port` | `int` | 1-based port number |

**Response:**
```json
{
  "device_id": "C58ZA",
  "port": 1,
  "mode": "VPD",
  "speed_target": 5,
  "vpd_target_kpa": 1.4,
  "temp_range_c": null,
  "humidity_range_pct": null,
  "schedule_window": null,
  "cycle_on_seconds": 0,
  "cycle_off_seconds": 0,
  "timer_on_seconds": 0,
  "timer_off_seconds": 0
}
```

**Field notes:**
- `vpd_target_kpa` — non-null only when VPD automation active; decoded as `targetVpd ÷ 10` (Quirk 4 analogue)
- `temp_range_c` — `{"min_c": N, "max_c": N}` when temp thresholds enabled; raw °C integers (no scaling)
- `humidity_range_pct` — `{"min_pct": N, "max_pct": N}` when humidity thresholds enabled; raw % RH integers
- `schedule_window` — `{"start": "HH:MM", "end": "HH:MM"}` in **device local time** (not UTC); `null` when disabled
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
  "action": "set port 2 speed to 5",
  "device_id": "C58ZA",
  "port": 2,
  "speed": 5,
  "dry_run": true,
  "controller_type": "legacy",
  "sent": false,
  "payload": { "...": "77-field legacy payload" }
}
```

**AI+ note:** `dry_run=True` is supported. `dry_run=False` returns an unsupported error — see Quirk 14.

---

### `set_port_on(device_id, port, dry_run=True)`

Turn a port on at full speed (`onSpead=10`). Works for fan-type and on/off toggle devices.
Uses read-before-write.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `port` | `int` | 1-based port number |
| `dry_run` | `bool` | Default `True` — returns payload without writing |

**Response:** Same structure as `set_port_speed` without the `speed` field; `action` is `"turn port N on"`.

---

### `set_port_off(device_id, port, dry_run=True)`

Turn a port off (`onSpead=0`). Uses read-before-write.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `port` | `int` | 1-based port number |
| `dry_run` | `bool` | Default `True` — returns payload without writing |

**Response:** Same structure as `set_port_speed` without the `speed` field; `action` is `"turn port N off"`.

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
  "action": "set port 1 VPD automation to 1.4 kPa",
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

### `set_temperature_automation(device_id, port, min_c, max_c, dry_run=True)`

Enable temperature automation using the built-in temperature sensor.
Switches the port to AUTO mode (`atType=3`) and sets temperature thresholds.
The controller speeds up when temperature exceeds `max_c` and slows below `min_c`.

**Parameters:**
| Parameter | Type | Description |
|---|---|---|
| `device_id` | `str` | Device code from `discover_devices` |
| `port` | `int` | 1-based port number |
| `min_c` | `float` | Minimum threshold °C, range 0–50. Sub-degree values rounded to nearest int |
| `max_c` | `float` | Maximum threshold °C, range 0–50. Must exceed `min_c` |
| `dry_run` | `bool` | Default `True` — returns payload without writing |

**Encoding:** `devLt = round(min_c)`, `devHt = round(max_c)` — raw °C integers, no ×100 scaling.
Also sets `activeLt=1`, `activeHt=1`, `atType=3`.

**Response:**
```json
{
  "action": "set port 1 temperature automation 20–26°C",
  "device_id": "C58ZA",
  "port": 1,
  "min_c": 20.0,
  "max_c": 26.0,
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
  "action": "set port 1 humidity automation 40–60%",
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
  "action": "set port 1 mode to CYCLE",
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
display values (`target_kpa`, `min_c`/`max_c`, `min_rh`/`max_rh`) but not their own
`sent`/`payload` keys. The call is atomic: it succeeds or fails as a single write, so
there is no partial-failure state to surface — either all the stage's targets land on
the controller, or the prior state is preserved.

**Encoding:**
- VPD: `int(target_vpd * 10 + 0.5)` — e.g. 1.25 kPa → stored as 13 (round-half-up)
- Temp/humidity: `int(value + 0.5)` raw integer — e.g. 20°C → `devLt=20` (no × 100 scaling)
- Rate limit: a single write, so the 1.5s rate gate fires once (Quirk 15)

**AI+ note:** `dry_run=True` is fully supported. `dry_run=False` returns the AI+
unsupported error before any writes (same as individual automation tools).

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
