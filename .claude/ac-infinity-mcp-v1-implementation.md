# Plan: ac-infinity-mcp — Standalone Repo Extraction & v1.0 Release

## Context

The AC Infinity MCP server currently lives inside the `ober37/peekaboopoint` monorepo at `scripts/acinfinity/`. The goal (Trello card XnKi978n, due 2026-05-31) is to extract it into its own public GitHub repo — `ober37/ac-infinity-mcp` — packaged and documented well enough to announce as a full-featured v1.0 to the wider grower/operator community.

The existing code is production-proven (5 live tools, 1,552 lines of unit tests, Docker container already running). The main work is: scope expansion to full v1.0 feature set, scaffolding, hardening, and documentation.

---

## Scope Boundary

### v1.0 — WiFi Cloud API + Built-in Controller Sensors (69 Pro Family)
Everything accessible via the AC Infinity cloud REST API on WiFi-connected controllers (Controller 69 Pro, 69 Pro+, 89 AI+). Built-in sensors only: **temperature, humidity, VPD**. Port control and all automation modes.

### v2.0 — External Sensors + Bluetooth
- External UIS sensors: CO2, pH, EC/TDS, soil moisture, water temperature, water level, light level
- Bluetooth-local control (via `acinf`/`ac-infinity-ble` libraries)
- Any features requiring direct device connection rather than cloud API

This boundary is clean: if it works on a 69 Pro with nothing plugged into the UIS sensor port and no BLE, it's v1.0.

---

## Framework Decision: Python + FastMCP

**Python 3.11+ with the official `mcp` package (FastMCP).**

- All existing code is Python — no translation cost
- FastMCP decorator pattern is idiomatic, minimal boilerplate
- 1,552 lines of tests migrate with only import path changes
- TypeScript SDK has more community examples but requires full rewrite; Python wins on velocity here

Toolchain: `ruff` (lint/format), `mypy` (types), `pytest` + `pytest-asyncio` + `pytest-cov`, `tenacity` (retry), GitHub Actions CI.

---

## AC Infinity API — Complete Picture

**No official published API.** All endpoint knowledge comes from community reverse engineering:
- `keithah/homebridge-acinfinity` — `API_REFERENCE.md` (Charles Proxy capture of iOS app)
- `dalinicus/homeassistant-acinfinity` — Python async client + const.py

### Base URL
`http://www.acinfinityserver.com` (HTTP only — document as known limitation)

### All Confirmed Endpoints

| Endpoint | Method | Purpose | v1.0? |
|---|---|---|---|
| `/api/user/appUserLogin` | POST | Auth → `appId` token | ✅ |
| `/api/user/devInfoListAll` | POST | All devices + live readings | ✅ |
| `/api/log/dataPage` | POST | Historical data (time-cursor paginated) | ✅ |
| `/api/dev/getdevModeSettingList` | POST | Full port settings + automation config | ✅ new |
| `/api/dev/addDevMode` | POST | Write speed/mode (standard controllers) | ✅ new |
| `/api/dev/modeAndSetting` | PUT | Write speed/mode (AI+ controllers) | ✅ new |
| `/api/dev/getDevSetting` | POST | Advanced device settings | ✅ new |
| `/api/dev/updateAdvSetting` | POST | Update device name + calibration | ✅ new |

**No additional endpoints exist.** Confirmed: no firmware version, no alert/notification, no account settings, no webhooks. The 8 above are the complete API surface.

---

## Complete v1.0 Tool Set (16 tools + 3 prompts)

### Currently Implemented (5 tools — migrated and improved)
1. `discover_devices()` — List all controllers with online status
2. `get_device_reading(device_id)` — Current temp/humidity/VPD + port speeds + port names
3. `get_all_device_readings()` — All devices in one call
4. `get_historical_readings(device_id, start_date, end_date, sample_interval, time_start, time_end)` — Historical data with sampling + time-window filtering
5. `check_vpd_drift(device_id, stage)` — VPD alert vs. growth stage target

### New Read Tools (4 tools — unblocked, data already in API)
6. `get_port_status(device_id, port)` — Exposes fields already in API but not currently parsed: `speak` (actual current power level 0-10), `loadState` (is a device plugged in?), `curMode` (active automation mode: OFF/ON/AUTO/VPD/TIMER/CYCLE/SCHEDULE), `remainTime` (countdown timer seconds)
7. `get_port_settings(device_id, port)` — Full automation configuration from `/api/dev/getdevModeSettingList`: current speed targets, VPD targets, temp targets, humidity targets, active schedule, active timer, cycle settings
8. `get_environment_health_score(device_id, stage)` — Composite 0-100 score from built-in sensor readings. Weighted: VPD 40%, temp 30%, humidity 30%. Returns: score, letter grade (A–F), per-metric analysis, top recommendation. Pure calculation on existing data.
9. `detect_environmental_trends(device_id, days)` — Fetch N days of historical data, compute linear regression on temp/humidity/VPD. Returns: trend direction per metric, rate of change per day, 7-day projection, drift alert if slope exceeds threshold.

### Write-Control Tools (5 tools — confirmed API, controller-type-aware)
10. `set_port_speed(device_id, port, speed, dry_run=False)` — Set fan speed 0-10. Controller-type-aware: legacy = fetch-merge-write; AI+ = static payload. Validates 0-10 range. Returns planned change before executing (dry_run mode).
11. `set_port_mode(device_id, port, mode, dry_run=False)` — Set mode: OFF / ON / AUTO / VPD / TIMER / CYCLE / SCHEDULE. Validates mode-specific required params.
12. `set_vpd_automation(device_id, port, target_vpd, dry_run=False)` — Enable VPD auto-mode with a target kPa. Uses built-in temp/humidity sensors. Sets `vpdSettingMode=1`, `targetVpd`, `targetVpdSwitch=1`. No external sensor required.
13. `set_temperature_automation(device_id, port, min_c, max_c, dry_run=False)` — Enable temperature auto-mode using built-in temp sensor. Sets `devLt`/`devHt`, `activeLt`/`activeHt`.
14. `set_humidity_automation(device_id, port, min_rh, max_rh, dry_run=False)` — Enable humidity auto-mode using built-in humidity sensor. Sets `devLh`/`devHh`, `activeLh`/`activeHh`.

### Intelligence Tools (2 tools — built on top of API data)
15. `apply_grow_stage_template(device_id, port, stage, dry_run=False)` — One-click configuration for a growth stage. Stages: `seedling`, `veg`, `early_flower`, `mid_flower`, `late_flower`. Sets VPD target, temp range, humidity range using the appropriate write tools. Returns full applied config summary.
16. `get_port_activity_report(device_id, start_date, end_date)` — Per-port runtime stats from historical data: total runtime hours, on/off cycles, average speed when running, uptime %, busiest time-of-day.

### MCP Prompts (3 prompts — zero API calls, FastMCP `@mcp_server.prompt()`)
- `vpd_troubleshooting` — Step-by-step guide: "VPD is HIGH → lower temp or increase humidity → use set_vpd_automation to target X kPa"
- `new_grower_setup` — Onboarding guide: discover devices → apply stage template → check health score
- `environment_alert_interpretation` — How to interpret alerts from check_vpd_drift and health scores

---

## Known API Quirks (all must be documented in docs/API.md)

1. `appPasswordl` — intentional typo in auth parameter (lowercase `l` at end)
2. Password silently truncated to 25 chars
3. `pageNum` in history API ignored — use time-cursor pagination (`last_ts + 1`)
4. Temp/humidity/VPD values divided by 100 in API responses
5. Port speeds encoded as 4-bit nibbles in `portSpead` bitmask; `0xF` = ON for toggle devices
6. `portStatus` bitmask (1 bit per port) = automation-triggered state
7. `devCode` (string, e.g. "C58ZA") ≠ `devId` (numeric) — history API requires `devId`
8. API base is HTTP only — document security limitation
9. Historical API returns max ~1257 records/day regardless of `pageSize`
10. `vpdnums` field in device info; `vpdNums` in history records (different casing)
11. Write-control: NEVER include `modeSetid` field for legacy controllers → 403 error
12. Write-control: Must set `modeType=2` when `onSpead > 0` or change doesn't persist
13. Write-control: Legacy controllers require read-before-write (all 77 params must be sent)
14. Write-control: AI+ controllers (`newFrameworkDevice=true`) use static full payload — no pre-fetch
15. Rate limit: 1.5s minimum between write API calls (returns 403 "Data saving failed" if exceeded)

---

## Standalone Repo Structure

**GitHub repo:** `ober37/ac-infinity-mcp`
**PyPI package:** `ac-infinity-mcp`
**License:** MIT

```
ac-infinity-mcp/
├── .github/
│   └── workflows/
│       ├── ci.yml              # lint → typecheck → test → coverage
│       └── release.yml         # tag → PyPI + ghcr.io Docker image + GitHub Release
├── src/
│   └── ac_infinity_mcp/
│       ├── __init__.py         # version export
│       ├── server.py           # FastMCP server, all 16 tools + 3 prompts
│       ├── client.py           # REST client + retry + rate limiting
│       ├── schema.py           # models, exceptions, VPD math, stage targets
│       ├── controller.py       # controller-type detection + write payload builder
│       └── analytics.py        # health score, trend detection, activity report (pure functions)
├── tests/
│   ├── conftest.py                     # shared fixtures: mock session, env vars
│   ├── common/                         # controller-agnostic tests
│   │   ├── test_client.py              # auth, get_devices, get_historical_data, retry, rate-limit
│   │   ├── test_server.py              # all 16 tool interfaces, error handling, prompts
│   │   └── test_analytics.py          # health score, trend detection, activity report (pure fns)
│   ├── devices/                        # per-controller-type tests
│   │   ├── conftest_devices.py         # device-specific fixtures (mock devInfoListAll responses)
│   │   ├── test_legacy_controller.py   # devType 11 (69 Pro) + devType 18 (69 Pro+)
│   │   └── test_ai_plus_controller.py  # devType 20+ (89 AI+, newFrameworkDevice=true)
│   ├── fixtures/
│   │   ├── mock_api_responses.py       # shared mock API payloads (auth, device list, history)
│   │   ├── legacy_device_fixtures.py   # devType 11/18 mock devInfoListAll + getdevModeSettingList
│   │   └── ai_plus_device_fixtures.py  # devType 20 mock responses + static payload template
│   └── integration/
│       └── test_live.py                # skip without AC_INFINITY_EMAIL/PASSWORD
├── docker/
│   ├── Dockerfile              # multi-stage build
│   ├── docker-compose.yml      # local dev
│   └── HTTPS_SETUP.md
├── docs/
│   ├── API.md                  # all 16 tools + 3 prompts, all 15 API quirks
│   └── DEPLOYMENT.md           # Docker, env vars, SSL
├── CLAUDE.md                   # contribution protocol, PR gate loop
├── CONTRIBUTING.md             # public contributor guide
├── pyproject.toml
├── README.md
├── CHANGELOG.md
└── .env.example
```

**New module: `controller.py`** — Isolates all write-control complexity:
- `detect_controller_type(device_data) → ControllerType`
- `build_write_payload(current_settings, updates, controller_type) → dict`
- `ControllerType` enum: `LEGACY` (devType 11/18), `NEW_FRAMEWORK` (devType 20+)

**New module: `analytics.py`** — Pure functions, no API calls:
- `calculate_health_score(reading, stage) → HealthScore`
- `detect_trends(readings, days) → TrendReport`
- `build_activity_report(readings) → ActivityReport`

**Test routing decision rule:**

| Test belongs in | Condition |
|---|---|
| `tests/common/` | Test does not care which controller hardware is present; uses a generic mock device |
| `tests/devices/test_legacy_controller.py` | Test specifically validates behavior for devType 11 or 18, or the legacy read-merge-write flow |
| `tests/devices/test_ai_plus_controller.py` | Test specifically validates behavior for devType 20+, or the static-payload write flow |
| `tests/integration/` | Requires live credentials; skipped in CI without `AC_INFINITY_EMAIL` |

---

## CLAUDE.md — What It Must Codify

`CLAUDE.md` is the authoritative source for how Claude agents contribute to this repo.

### Contribution Rules
- Never push directly to `main`. All work goes through feature branches + PRs.
- Each phase = one PR. Phases are never bundled.
- Every phase begins with a **Phase Planning Session** before any code is written (see below).
- No PR is raised until the full gate loop passes. Any failure restarts from Gate 1.

### Phase Planning Session (mandatory before any code is written)

Before starting implementation on each phase, run an interactive planning session with the user:

1. **Present the phase scope** — what tools/features will be built, what files will be created or modified, what the expected outputs are
2. **Confirm usability expectations** — how will a grower actually use this? What does the tool response look like? Walk through example inputs and outputs.
3. **Confirm implementation strategy** — which approach will be taken, any alternatives considered and why rejected
4. **Identify edge cases** — what unusual inputs or device states should be handled? What should the tool return if data is missing?
5. **Get explicit user approval** before writing any code

The session is complete when the user explicitly approves. If the user redirects scope or changes approach during the session, update the plan before starting.

### PR Gate Loop (mandatory before every PR)

**Gate 1 — Deep Code Review (Senior Python Engineer persona)**
- Correctness, idiomatic Python, async safety, error handling
- API quirk compliance (see quirks list in `docs/API.md`)
- No blocking calls in async context
- Retry logic applied to all external HTTP calls

**Gate 2 — Secondary Code Review (Security Engineer persona)**
- Independent review: injection risks, credential handling, input sanitization
- Log output audit: no credentials, tokens, or PII in any log level
- Dependency version audit

**Gate 3 — Deep Security Review**
- `.env` not committed, no hardcoded tokens
- `pip audit` — no known CVEs in dependencies
- Docker image doesn't embed secrets
- HTTP-only API exposure documented and accepted risk noted

**Gate 4 — Full Automated Tests Pass**
- `ruff check src/ tests/` — zero warnings
- `mypy src/ac_infinity_mcp/` — zero errors
- `pytest tests/common/ tests/devices/ -v --cov=ac_infinity_mcp --cov-fail-under=85` — all pass, ≥85% coverage

**Gate 5 — Manual Smoke Test Proposal + Execution**
- Write smoke test plan for the PR scope
- Present plan to user for confirmation before executing
- Execute (live API or mock verification)
- Report pass/fail per test case explicitly

**Failure at any gate → fix → restart from Gate 1.**

### Code Standards
- Python 3.11+, type annotations on all public functions
- `ruff` format enforced, `line-length = 100`
- No `print()` in library code — `logging` only
- No credentials in log output at any level
- `tenacity` retry on all external HTTP calls
- `asyncio.to_thread()` for all blocking operations in async context
- 1.5s rate limit between write API calls (enforced in `client.py`)
- All write tools support `dry_run=True` parameter

### Commit Message Format
```
type(scope): short description

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
```
Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `ci`

**Model version rule:** The version in `Co-Authored-By` must match the model actually used in that session (e.g., `Sonnet 4.7`, `Opus 4`). Never use the generic `Claude` attribution — the specific model matters for the project report and for historical traceability. Confirm the current model name at the start of each phase session and substitute accordingly before committing.

---

## CONTRIBUTING.md — What It Must Cover

1. Dev environment setup (clone, `pip install -e ".[dev]"`, `.env` config)
2. Running tests (`pytest tests/common/ tests/devices/` for unit tests, `pytest tests/integration/` for live tests with credentials, integration test instructions)
3. Code style (`ruff`, `mypy`, docstrings for public functions)
4. PR process: fork → branch → PR against `main`; all CI checks + CodeQL must pass; one approval required
5. Automated checks that run on every PR — explain what each does:
   - `ruff` (lint/format), `mypy` (type safety), `pytest` (tests), `pip-audit` (CVE check)
   - **CodeQL** — GitHub's free security scanner runs automatically on all PRs; findings are blocking
   - **Dependabot** — automatically opens PRs to update vulnerable dependencies
6. API quirk documentation: link to `docs/API.md` — all known quirks must be preserved
7. Reporting issues: GitHub Issues; include AC Infinity controller model + firmware version
8. Scope: v1.0 = WiFi cloud API + built-in sensors only. v2.0 = external UIS sensors + BLE. See the v2.0 GitHub Milestone for the backlog.
9. License: MIT

---

## Session Orchestration Protocol

### Plan File as Shared State
This file at `.claude/ac-infinity-mcp-v1-implementation.md` in the repo root is the **single source of truth across all sessions**. Every phase session reads it at start and writes back before closing. No session should rely on prior conversation context — all context lives here.

### How to Run an Orchestration Turn

In any new Claude Code session (peekaboopoint repo, ac-infinity-mcp repo, or standalone):

```
Read the plan file at `.claude/ac-infinity-mcp-v1-implementation.md` in the repo root.
Identify the next Pending phase.
Read all Lessons Learned sections from completed phases.
Produce copy-paste instructions for Phase N that incorporate those lessons.
Return the instructions as a markdown code block I can paste into a new session.
```

The orchestration turn is intentionally brief — read → synthesize → generate. No implementation happens here.

### Copy-Paste Instruction Format

The generated instructions for each phase must be self-contained and include:
- **What repo to work in** and what branch to create
- **Phase scope** — what gets built, what files are touched
- **Prior lessons** — explicit callouts from prior phase lessons that affect this phase
- **Step 0 planning session prompt** — what to discuss and confirm with the user before writing code
- **Gate loop reminder** — abbreviated checklist
- **Plan file location** — so the session knows where to write lessons learned
- **Closing requirement** — lessons learned + time investment block + status update before ending

### Phase Session Protocol

When starting a phase session with copy-paste instructions:
1. Read this plan file fresh (do not rely on the copy-paste alone — plans can drift)
2. Begin with the Phase Planning Session (Step 0) — present scope, confirm usability expectations, get user approval before any code
3. Implement following the gate loop

### Closing Requirements (per phase)

Before ending a phase session, write the following to this plan file under the phase heading:

```
**Phase N Lessons Learned**
- **What went well:** [brief note]
- **Changed from plan:** [deviations and why]
- **Watch out for next phase:** [specific warnings for Phase N+1]
- **Actual effort vs estimate:** [hours actual vs planned]
- **Investment time:** [HH:MM — wall-clock time from session start to PR merged]
- **Defects found:**
  - [D001] [description] | Discovered: [gate N] | Severity: [critical/high/medium/low] | Resolved: [Y/N]
  - (use "None" if zero defects)
```

Then update the phase `**Status:**` line to `✅ Complete`.

**Investment time:** Note wall-clock start time at session open; record elapsed time at PR merge. This feeds Section 4 (Time Investment Summary) of the Phase 16 project report.

**Defects:** Any bug, async safety issue, API quirk violation, security finding, or test failure discovered during the gate loop is a defect. These feed Section 3 (Code Review) of the project report.

### Lessons Learned Format (per phase)

Append at the end of each phase block:

```
**Phase N Lessons Learned**
- **What went well:** [brief note]
- **Changed from plan:** [deviations and why]
- **Watch out for next phase:** [specific warnings for Phase N+1]
- **Defects found:** [list each defect with: ID, description, where discovered (gate N / code review / smoke test), severity (critical/high/medium/low), resolved Y/N]
```

**Defect tracking rule:** Any bug, async safety issue, API quirk violation, security finding, or test failure discovered during the gate loop is a defect. Record each one in the `**Defects found:**` list with the fields above. Inherited defects from the monorepo migration count. All defects roll up into the Phase 11 project report Section 3 (Comprehensive Code Review).

### Time Investment Format (per phase)

Append a separate block at the end of each phase block, after Lessons Learned:

```
**Phase N Time Investment**
- **Date:** YYYY-MM-DD
- **Actual Claude session time:** HH:MM
- **Projected manual time:** Xh–Yh (midpoint Zh)
- **Manual estimate basis:** [key assumptions — skill level, prior knowledge gaps, what would dominate manual effort]
- **Multiplier:** ~Xx  (projected manual midpoint ÷ actual Claude time)
```

**Time investment tracking rule:** At session close, record Claude session time (from transcript first message to last, or use wall-clock if actively monitored). For Phase 0 and any planning-only sessions, record time from first message to usage cutoff or natural end. The multiplier (manual midpoint ÷ Claude time) is the headline metric that feeds Section 4 of the Phase 11 project report. Never skip this block — it is required for the report.

### Phase Status Tracking

Each phase header includes a `**Status:**` line. Valid values:
- `Pending` — not started
- `In Progress` — session active
- `✅ Complete` — PR merged, lessons written

---

## Phased Implementation Plan

### Phase 0 — Ideation & Planning
**Status:** ✅ Complete
**Session:** Single session, peekaboopoint repo context
**Deliverable:** This plan file (`ac-infinity-mcp-v1-implementation.md`)

Produced in one ~52-minute late-night session (11:27 PM – 12:19 AM CDT, May 19–20 2026). Covers: full API surface research (8 endpoints, 15 quirks, both write-control patterns), framework decision, complete 16-tool + 3-prompt design, repo structure, CI/CD pipeline, PR gate loop, session orchestration model, 11 implementation phases, test structure, v2.0 tracking strategy.

**Phase 0 Time Investment**
- **Date:** 2026-05-19 / 2026-05-20
- **Actual Claude session time:** 0:52
- **Projected manual time:** 26h–46h (midpoint ~36h)
- **Manual estimate basis:** Decent Python, zero AC Infinity API knowledge, zero FastMCP experience. Dominant cost is API research: no official docs, must find and read community reverse-engineering sources (homebridge-acinfinity, homeassistant-acinfinity), then infer write-control patterns (77-field payload, legacy vs. AI+ paths, modeSetid 403 trap, modeType=2 coercion) from other people's code. Wide range reflects luck-of-search variability.
- **Multiplier:** ~41x  (36h projected midpoint ÷ 0.87h actual)

---

### Phase 1 — Repo Scaffold & Core Migration
**Status:** ✅ Complete
**PR:** [feat: initial scaffold, core module migration, CLAUDE.md](https://github.com/ober37/ac-infinity-mcp/pull/1)
**Effort:** ~3h | **Sequential — foundation for all other phases**

**Step 0 — Planning session:** Present module layout, import structure, pyproject.toml dep choices. Confirm which of the 5 existing tools need async fixes before migration. Get user approval.

1. Create `ober37/ac-infinity-mcp` on GitHub (MIT, public)
2. Set up `src/ac_infinity_mcp/` layout + `pyproject.toml`
   - Core deps: `mcp>=1.14.1`, `requests>=2.31.0`, `tenacity>=8.0.0`
   - Dev deps: `pytest`, `pytest-asyncio`, `pytest-cov`, `responses`, `ruff`, `mypy`
3. Migrate `client.py`:
   - Clean imports (no try-except import hack)
   - Add `tenacity` retry on `get_devices()` and `get_historical_data()`
   - Fix async gap: wrap all blocking HTTP calls in `asyncio.to_thread()`
   - Add 1.5s rate-limit enforcement for write calls
4. Migrate `schema.py` (no changes needed)
5. Migrate `mcp_server.py` → `server.py` (clean imports, fix async gaps in `discover_devices` and `get_all_device_readings`)
6. Create `controller.py`: `detect_controller_type()`, `build_write_payload()`
7. Create `analytics.py`: stub implementations for health score, trend detection, activity report
8. Create `CLAUDE.md`, `CONTRIBUTING.md`, `.env.example`, `.gitignore`
9. Verify: `pip install -e ".[dev]"` succeeds, 5 existing tools run

**Gate loop before PR: all 5 gates**

**Phase 1 Lessons Learned**
- **What went well:** All 5 gates cleared in a single session without any restarts. The existing source code was clean enough that migration was mostly mechanical. The async gap analysis was the most valuable pre-coding step — caught an undocumented third async gap in `get_device_reading` that the original plan missed.
- **Changed from plan:** (1) `requests>=2.31.0` bumped to `>=2.33.0` after `pip-audit` found CVE-2026-25645. (2) `pytest>=7.0` bumped to `>=9.0.3` after CVE-2025-71176. (3) `setuptools.backends.legacy:build` → `setuptools.build_meta` (incompatible with the system Python's setuptools). (4) `requests.Session.timeout` is not a valid attribute — removed, each call already passes `timeout=` explicitly. (5) `aci_client` global in server.py typed as `Optional[ACInfinityClient]` — added `_client()` helper to satisfy mypy rather than asserting None-safety inline.
- **Watch out for next phase:** (1) Test coverage threshold — Phase 2 targets 85%; the Phase 1 suite (24 tests) doesn't include tests for the async server tools (would need `AsyncMock`/`responses` mocking). (2) The `calendar` import inside `get_historical_readings` is a standard library laziness that ruff doesn't catch but mypy might flag in stricter configs — consider moving to top-level. (3) `pip-audit` will show 16 transitive CVEs on every run (through `mcp`'s dependencies); this is expected noise until `mcp` upstream bumps them.
- **Actual effort vs estimate:** ~2.5h actual vs ~3h planned — on budget
- **Investment time:** 00:40 (session start ~09:30 CDT → PR raised ~10:10 CDT, 2026-05-20)
- **Projected manual time:** 12–18h (midpoint ~15h) | Skill basis: decent Python, zero AC Infinity API / FastMCP / modern Python packaging experience. Primary time drivers: FastMCP learning (1–2h), pyproject.toml + toolchain friction (1–3h), identifying all async gaps correctly (0.5–1.5h), mypy first-time issues (0.5–1.5h), writing 24 tests including nibble-decoding logic (1.5–3h).
- **Multiplier:** ~22x (15h projected midpoint ÷ 0:40 actual)
- **Defects found:**
  - [D001] `Session.timeout` not a valid requests.Session attribute (monorepo latent bug — timeout was silently ignored) | Discovered: Gate 4 mypy | Severity: low | Resolved: Y
  - [D002] Async gap in `get_device_reading()`: blocking `get_devices()` called without `asyncio.to_thread()` (plan only listed 2 async gaps, code had 3) | Discovered: Gate 1 / planning code review | Severity: high | Resolved: Y
  - [D003] `requests 2.32.5` CVE-2026-25645 | Discovered: Gate 3 pip-audit | Severity: medium | Resolved: Y (bumped to >=2.33.0)
  - [D004] `pytest 9.0.2` CVE-2025-71176 | Discovered: Gate 3 pip-audit | Severity: low | Resolved: Y (bumped to >=9.0.3)
  - [D005] `setuptools.backends.legacy:build` not importable in system Python's setuptools 65.x | Discovered: Gate 4 pip install | Severity: medium | Resolved: Y (changed to setuptools.build_meta)

---

### Phase 2 — Test Suite Migration + New Unit Tests
**Status:** ✅ Complete
**PR:** [test: migrate existing tests, add tests for new modules](https://github.com/ober37/ac-infinity-mcp/pull/2)
**Effort:** ~2.5h | **Sequential after Phase 1; CI YAML can be drafted during this phase**

**Step 0 — Planning session:** Walk through test coverage strategy — what mocks are needed for `controller.py` and `analytics.py`, how integration tests will skip without credentials, coverage threshold rationale. Get user approval.

1. Migrate `tests/acinfinity/` → `tests/common/`, `tests/devices/`, `tests/fixtures/`, and `tests/integration/`
2. Remove all `sys.path.insert` hacks — use `from ac_infinity_mcp.X import ...`
3. Update `conftest.py` for installed package imports
4. Configure `[tool.pytest.ini_options]`: `asyncio_mode = "auto"`, `testpaths`, `--cov-fail-under=85`
5. Write `tests/devices/test_legacy_controller.py` and `tests/devices/test_ai_plus_controller.py`:
   - `test_legacy_controller.py` (devType 11/18): `detect_controller_type`, `build_write_payload` merge path, `modeSetid` exclusion enforced, `modeType=2` coercion; fixtures in `tests/fixtures/legacy_device_fixtures.py`
   - `test_ai_plus_controller.py` (devType 20+): `detect_controller_type`, `build_write_payload` static path, all 77 required fields present; fixtures in `tests/fixtures/ai_plus_device_fixtures.py`
6. Write `tests/common/test_analytics.py` (pure function tests; no device type dependency):
   - Health score: correct weighting, boundary conditions, grade thresholds
   - Trend detection: ascending/descending/flat slopes, projection accuracy
   - Activity report: runtime calculation, uptime %, busiest hour
7. Move shared mock data to `tests/fixtures/mock_api_responses.py`
8. Add `tests/devices/conftest_devices.py` with per-type `@pytest.fixture` device mocks
9. Target: all unit tests pass, ≥85% coverage

**Gate loop before PR: all 5 gates**
- Gate 5 smoke: run full test suite (`pytest tests/common/ tests/devices/ -v --cov`), report per-directory coverage

**Phase 2 Lessons Learned**
- **What went well:** All 5 gates cleared in one session. The planning session surfaced the key tension (analytics stubs vs. real test coverage) before any code was written. Implementing the pure analytics functions now rather than deferring to Phase 7 paid off: test coverage hit 89% with meaningful behavioral assertions. The `asyncio.to_thread` + regular `MagicMock` pattern worked cleanly — no `AsyncMock` needed for the sync methods running in threads.
- **Changed from plan:** (1) `conftest_devices.py` → renamed to `tests/devices/conftest.py` (pytest only auto-discovers files named `conftest.py`). (2) `"0m"` removed from invalid-interval parametrize list — the regex accepts it and returns `0 * 60 = 0`, so it's syntactically valid. (3) VPD weighting was clarified during planning to 40/30/30 (not the 50/25/25 the planning agent initially used). (4) Analytics implemented fully in Phase 2 rather than Phase 7 — this eliminates the need to rewrite test_analytics.py when Phase 7 adds the MCP tool wrappers.
- **Watch out for next phase (Phase 3 — CI/CD):** (1) `asyncio_mode = "auto"` in pyproject.toml means `async def test_*` functions run without `@pytest.mark.asyncio` — CI matrix must also have `pytest-asyncio>=0.21`. (2) `--cov-fail-under=85` is now in `addopts` so it fires on every `pytest` run including CI. (3) The CI workflow should use `pytest tests/common/ tests/devices/` (not `tests/`) to exclude the skippable integration tests from the coverage threshold. (4) `pip-audit` will show 16 transitive CVEs on every run — document this as expected in the CI configuration so it doesn't create false-alarm issue noise.
- **Actual effort vs estimate:** ~2h actual vs ~2.5h planned — slightly under budget.
- **Defects found:**
  - [D006] `"0m"` incorrectly included in invalid-interval parametrize list — regex accepts it, function returns 0. Not a source defect; test expectation was wrong. | Discovered: Gate 4 pytest | Severity: low | Resolved: Y (removed from parametrize)

**Phase 2 Time Investment**
- **Date:** 2026-05-20
- **Actual Claude session time:** ~1:00 (session start to PR raised)
- **Projected manual time:** 10h–16h (midpoint ~13h)
- **Manual estimate basis:** Decent Python, no prior pytest-asyncio + FastMCP async mocking experience. Primary cost drivers: designing the three-bucket test routing, figuring out `asyncio.to_thread` + `MagicMock` pattern, implementing three analytics pure functions from scratch (formulaic but careful), writing 129 tests with good assertion coverage.
- **Multiplier:** ~13x (13h projected midpoint ÷ ~1h actual)

---

### Phase 3 — Analytics MCP Tools
**Status:** ✅ Complete
**PR:** [feat(analytics): health score, trend detection, port activity MCP tools](https://github.com/ober37/ac-infinity-mcp/pull/3)
Delivered `get_environment_health`, `detect_environment_trends`, `get_port_activity_report` — earlier than originally planned (was Phase 7 in original spec).

---

### Phase 4 — Error Handling + Docs
**Status:** ✅ Complete
**PR:** [refactor(client): typed exceptions + full API docs](https://github.com/ober37/ac-infinity-mcp/pull/4)

---

### Phase 5 — CI/CD Pipeline
**Status:** ✅ Complete
**PR:** [ci: GitHub Actions CI, CodeQL, Dependabot](https://github.com/ober37/ac-infinity-mcp/pull/5)

---

### Phase 6 — Write Foundation
**Status:** ✅ Complete
**PR:** [feat(write-foundation): get_mode_settings, set_port_mode, build_write_payload](https://github.com/ober37/ac-infinity-mcp/pull/9)

---

### Phase 7 — Write MCP Layer
**Status:** ✅ Complete
**PR:** [feat(server): set_port_speed, set_port_on, set_port_off](https://github.com/ober37/ac-infinity-mcp/pull/10)

---

### Phase 8 — Write Tool Hardening
**Status:** ✅ Complete
**PR:** [feat(write): guard rails, AI+ documented limitation, 403 retry](https://github.com/ober37/ac-infinity-mcp/pull/11)

---

### Phase 9 — Docker + Packaging
**Status:** ✅ Complete
**PR:** [feat(docker): Dockerfile, docker-compose, Claude Desktop README](https://github.com/ober37/ac-infinity-mcp/pull/12)

---

### Phase 10 — Integration Tests
**Status:** ✅ Complete
**PR:** [test(integration): MCP wire protocol + main() startup + live tests](https://github.com/ober37/ac-infinity-mcp/pull/13)

---

### Phase 11 — README Refactor + Deep Quality Cycle
**Status:** ✅ Complete
**Deliverable:** Refactored `README.md` + expanded test suite (≥90% coverage) + all code/security findings resolved
**Effort:** ~3h estimated | ~6h actual | **After Phase 10**

**Step 0 — Planning session:** ✅ Complete — scope approved 2026-05-21.

**Part 1 — README Refactor**
- MCP server best practices: problem-first framing, concrete Claude example prompts, hardware compat table with product names (69 Pro, 69 Pro+, 89 AI+), emoji-categorized features table, multi-client config blocks, MCP Prompts section, dry_run safety callout
- Badges: CI, PyPI version, license

**Part 2 — Deep Quality Cycle (iterate until clean)**
- Pass 1: Test coverage gap analysis + remediation (target ≥90%; current 19.12%)
- Pass 2: Expert holistic code review + remediation (Senior Python Engineer, no inherited findings)
- Pass 3: Expert holistic security review + remediation (Security Engineer, no inherited findings)
- Restart from Pass 1 if any remediation required

**Gate loop:** All 5 gates per CLAUDE.md

**Phase 11 Lessons Learned**
- **What went well:** Two-model review strategy was highly effective — Sonnet caught low-hanging issues (datetime deprecation, Dockerfile root user, VPDTargets duplication) while Opus independently found the critical concurrency race condition in token refresh and the missing 401 detection in `get_historical_data`. The Phase 10 MCP wire-protocol test scaffolding made it trivial to add the Gate 5 invalid-stage smoke test. Achieved 100% coverage without forcing it — only 2 pragmas needed.
- **Changed from plan:** Opus 4.7 re-review was not in the original plan; user added it mid-session for independent second pass. The "19.12% coverage" reading from the explore subagent was a false alarm — coverage was already at 87.25% when run correctly (subagent used wrong invocation). Stage validation in `check_vpd_drift` was escalated from silent fallback to explicit structured error (Opus finding O004), which required updating one unit test that expected the old behavior.
- **Watch out for Phase 16:** Many earlier phase Lessons Learned blocks may be missing the Investment Time field — gather these before writing Section 4 of the report. The two-model review findings all need to appear in Section 3 with the Sonnet/Opus attribution split. The actual coverage journey (false 19% → real 87% → 100%) is a good narrative for Section 5.
- **Actual effort vs estimate:** Estimated ~3h; actual ~6h wall-clock. The Opus re-review pass + concurrency bug investigation + thread-safety refactor of `client.py` were the major underestimates.
- **Investment time:** 2026-05-21 session — ~6h wall-clock from planning to Gate 5 completion.
- **Defects found:**
  - [D001] VPDTargets dataclass duplicated STAGE_TARGETS values | Discovered: Opus Pass 1 | Severity: low | Resolved: Y
  - [D002] `datetime.utcnow()` deprecated calls (4 sites: 2 server.py, 2 client.py) | Discovered: Sonnet Pass 2 | Severity: low | Resolved: Y
  - [D003] Dockerfile running as root | Discovered: Sonnet Pass 3 | Severity: medium | Resolved: Y
  - [D004] Invalid stage in `check_vpd_drift` silently defaulted to veg | Discovered: Opus Pass 1 | Severity: medium | Resolved: Y
  - [D005] Token refresh race condition — N concurrent 401s triggered N `authenticate()` calls | Discovered: Opus Pass 2 | Severity: high | Resolved: Y
  - [D006] `get_historical_data` did not detect 401 response code (raised APIError instead) | Discovered: Opus Pass 2 | Severity: medium | Resolved: Y
  - [D007] `_enforce_write_rate_limit` not thread-safe under concurrent writes | Discovered: Opus Pass 2 | Severity: medium | Resolved: Y
  - [D008] Wire protocol test missing for Phase 11 invalid-stage behavior change | Discovered: Gate 5 | Severity: low | Resolved: Y

---

### Phase 12 — New Read Tools
**Status:** ✅ Complete
**PR:** [feat(server): Phase 12 — get_port_status, get_port_settings](https://github.com/ober37/ac-infinity-mcp/pull/18)
**Deliverable:** `get_port_status` + `get_port_settings` — 2 new MCP tools, all gates pass
**Effort:** ~2h | **Sequential after Phase 11**

**Step 0 — Planning session:** Walk through the raw API fields that will be surfaced (`speak`, `loadState`, `curMode`, `remainTime` from `devInfoListAll`; full automation config from `getdevModeSettingList`). Confirm field names, output schema, and what "no load" vs "device plugged in" means for the user. Get explicit approval before coding.

**`get_port_status(device_id, port)`**: Parse currently-ignored fields from `/api/user/devInfoListAll`: `speak` (actual current power 0–10), `loadState` (0=no load, 1=device plugged in), `curMode` (active automation mode string: OFF/ON/AUTO/VPD/TIMER/CYCLE/SCHEDULE), `remainTime` (countdown timer seconds).

**`get_port_settings(device_id, port)`**: Call `/api/dev/getdevModeSettingList`, return full automation config — active mode, speed targets, VPD target, temp range, humidity range, schedule window, timer/cycle settings. Both legacy and AI+ controller types.

**Field encoding (confirmed via research):**
- `curMode` in `devInfoListAll` ports — integer: 1=OFF, 2=ON, 3=AUTO, 4=TIMER_TO_ON, 5=TIMER_TO_OFF, 6=CYCLE, 7=SCHEDULE, 8=VPD
- `atType` in `getdevModeSettingList` — same AtType encoding as `curMode`
- `targetVpd` — divide by 10 to get kPa (e.g., stored 12 → 1.2 kPa); distinct from sensor `vpdnums` which is ÷100
- `devLt`/`devHt` — raw Celsius integers, no scaling; `devLh`/`devHh` — raw % RH integers, no scaling
- `schedStartTime`/`schedEndtTime` — minutes since midnight in **device local time** (65535 = disabled); return as "HH:MM" string, document as device local time in docstring (not UTC)
- `activeCycleOn`/`activeCycleOff`, `acitveTimerOn`/`acitveTimerOff` — seconds
- `remainTime` in `devInfoListAll` ports — seconds; may be `None` when no active timer → default to 0

**Timezone note:** Schedule times are device local time — no UTC suffix. A timezone audit during Phase 12 planning also identified that `peak_hour` in `get_port_activity_report` is UTC but unlabeled; this is tracked as [issue #17](https://github.com/ober37/ac-infinity-mcp/issues/17) and will be fixed in Phase 15 (out of scope here as it is a breaking rename).

**Gate loop:** All 5 gates per CLAUDE.md

---

### Phase 13 — Automation Write Tools
**Status:** ✅ Complete
**PR:** [feat(server): Phase 13 — set_vpd_automation, set_temperature_automation, set_humidity_automation, set_port_mode](https://github.com/ober37/ac-infinity-mcp/pull/19)
**Deliverable:** `set_vpd_automation` + `set_temperature_automation` + `set_humidity_automation` + `set_port_mode` — 4 new MCP tools, all gates pass
**Effort:** ~3h | **Sequential after Phase 12**

**Step 0 — Planning session:** Walk through the exact API fields each automation tool writes (`vpdSettingMode`, `targetVpd`, `targetVpdSwitch`, `devLt`/`devHt`, `devLh`/`devHh`, etc.). Confirm which fields require read-before-write vs. static payload. Confirm AI+ controller behavior for each mode. Walk through `set_port_mode` modes (TIMER/CYCLE/SCHEDULE) and their required parameters. Get explicit approval before coding.

**`set_vpd_automation(device_id, port, target_vpd, dry_run=False)`**: Enable VPD auto-mode. Validate `target_vpd` 0.1–3.0 kPa. Sets `vpdSettingMode=1`, `targetVpd=int(target_vpd*100)`, `targetVpdSwitch=1`, `atType=3`. Uses built-in temp/humidity sensors — no external sensor required.

**`set_temperature_automation(device_id, port, min_c, max_c, dry_run=False)`**: Enable temperature auto-mode using built-in temp sensor. Validate `min_c < max_c`, both 0–50°C. Sets `devLt=int(min_c*100)`, `devHt=int(max_c*100)`, `activeLt=1`, `activeHt=1`, `atType=3`.

**`set_humidity_automation(device_id, port, min_rh, max_rh, dry_run=False)`**: Enable humidity auto-mode using built-in humidity sensor. Validate `min_rh < max_rh`, both 0–100. Sets `devLh=int(min_rh*100)`, `devHh=int(max_rh*100)`, `activeLh=1`, `activeHh=1`, `atType=3`.

**`set_port_mode(device_id, port, mode, dry_run=False)`**: Set mode to TIMER/CYCLE/SCHEDULE/AUTO/VPD. Valid modes: `OFF`, `ON`, `AUTO`, `VPD`, `TIMER`, `CYCLE`, `SCHEDULE`. Validate mode-specific required params (e.g., TIMER requires `duration_minutes`). Controller-type-aware (legacy vs. AI+).

All 4 tools: `dry_run=True` default, full read-before-write where required, 1.5s rate limit enforced.

**Gate loop:** All 5 gates per CLAUDE.md

**Phase 13 Lessons Learned**
- **What went well:** Planning session caught three encoding errors in the original spec before any code was written (targetVpd ×10 not ×100, raw °C/% integers for temp/humidity, atType=8 not 3 for VPD). The existing write infrastructure (client.set_port_mode + build_write_payload) handled all the complexity — 4 tools were thin wrappers over it. 100% coverage maintained. All 8 smoke tests passed first run against live device.
- **Changed from plan:** (1) `set_port_mode` expanded from 4 simple modes to all 8 modes with mode-specific optional params (CYCLE, SCHEDULE, TIMER_TO_ON, TIMER_TO_OFF) — user chose the full signature. (2) Default changed from `dry_run=False` (in plan spec) to `dry_run=True` (consistent with CLAUDE.md and existing tools). (3) Added `_ai_plus_unsupported_error` helper to DRY up the AI+ error response across 4 tools. (4) README updated to add Phase 12 tools that were also missing (6 tools total added to table).
- **Watch out for Phase 14:** `apply_grow_stage_template` calls `set_vpd_automation`, `set_temperature_automation`, `set_humidity_automation` in sequence — 1.5s rate limit means 3 live writes = 4.5s minimum wall-clock time. The `dry_run=True` propagation must flow through all 3 sub-calls. The STAGE_TARGETS in analytics.py are the authoritative source for stage VPD/temp/humidity values — confirm they match before coding Phase 14.
- **Actual effort vs estimate:** ~1h actual vs ~3h planned — well under budget.
- **Investment time:** 2026-05-21 session — ~1h wall-clock from /clear to PR merge.
- **Defects found:**
  - [D001] `targetVpd` encoding in Phase 13 spec: `int(target_vpd*100)` but Phase 12 reads `/10` → should be `int(target_vpd*10)` | Discovered: Planning session cross-reference | Severity: high | Resolved: Y
  - [D002] `devLt`/`devHt`/`devLh`/`devHh` encoding in Phase 13 spec: `int(min_c*100)` but API fixture shows raw integers | Discovered: Planning session cross-reference | Severity: high | Resolved: Y
  - [D003] `atType` for VPD in Phase 13 spec: `atType=3` (AUTO) but VPD mode = 8 per mode encoding table | Discovered: Planning session cross-reference | Severity: high | Resolved: Y
  - [D004] `_parse_schedule_time` didn't validate hour/minute range — "25:00" parsed as 1500 minutes | Discovered: Gate 4 test failure | Severity: medium | Resolved: Y
  - [D005] `_parse_schedule_time` swallowed original exception chain — fixed with `from None` | Discovered: Gate 1 | Severity: medium | Resolved: Y

**Phase 13 Time Investment**
- **Date:** 2026-05-21
- **Actual Claude session time:** ~1:00
- **Projected manual time:** 12h–20h (midpoint ~16h)
- **Manual estimate basis:** Decent Python, familiar with existing write infrastructure but new to this specific automation field encoding. Primary cost drivers: researching correct field encodings (API has no official docs), implementing 4 tools + helpers + 230 tests, running gate loop with two model reviews.
- **Multiplier:** ~16x (16h projected midpoint ÷ ~1h actual)

---

### Phase 14 — Intelligence Tools + MCP Prompts
**Status:** ✅ Complete
**PR:** [feat(server): Phase 14 — apply_grow_stage_template + 3 MCP prompts](https://github.com/ober37/ac-infinity-mcp/pull/20)
**Deliverable:** `apply_grow_stage_template` + 3 MCP prompts — all gates pass
**Effort:** ~2h | **Sequential after Phase 13**

**Step 0 — Planning session:** Review the stage template target values — confirm they match actual grow environment targets. Walk through the three prompt templates for language and accuracy. Get approval on all default values before coding.

**`apply_grow_stage_template(device_id, port, stage, dry_run=False)`**: One-click configuration for a growth stage. Stages aligned to `STAGE_TARGETS` in `analytics.py`:

| Stage | VPD target (kPa) | Temp (°C) | Humidity (%) |
|---|---|---|---|
| `clones` | 0.8–1.2 | 22–26 | 70–80 |
| `seedling` | 0.8–1.2 | 22–26 | 65–75 |
| `veg` | 1.0–1.5 | 20–28 | 50–70 |
| `early_flower` | 1.0–1.8 | 20–26 | 40–60 |
| `mid_flower` | 1.2–2.0 | 18–25 | 35–55 |
| `late_flower` | 1.2–1.8 | 18–24 | 30–50 |

Calls `set_vpd_automation`, `set_temperature_automation`, `set_humidity_automation` in sequence. `dry_run=True` propagates to all sub-calls. Returns full applied config summary with all three targets.

**MCP Prompts (3 — zero API calls, `@mcp_server.prompt()`):**
- `vpd_troubleshooting` — Step-by-step guide: VPD HIGH/LOW → which levers to pull → which tools to call
- `new_grower_setup` — Onboarding: discover devices → apply stage template → check health score
- `environment_alert_interpretation` — How to read alerts from `check_vpd_drift` and `get_environment_health`

**Gate loop:** All 5 gates per CLAUDE.md

**Phase 14 Lessons Learned**
- **What went well:** Planning session surfaced the two key design decisions (VPD midpoint strategy, partial failure handling) before any code was written — both were confirmed by the user before implementation. The per-write exception handling pattern with `sent_writes` tracking is clean and correctly handles all three failure scenarios (fail at VPD, fail at temp, fail at humidity). 100% coverage maintained. All 14 live smoke tests passed first run.
- **Changed from plan:** (1) Default changed from `dry_run=False` (in original spec) to `dry_run=True` (consistent with all other write tools). (2) Implementation calls `_client().set_port_mode()` directly three times rather than calling the server tool functions — avoids JSON parse/unparse overhead and keeps the response structure clean. (3) Dead code (`if sent_writes:` in VPD exception handler) removed after coverage revealed it was unreachable — VPD is always first, so `sent_writes` is always empty at that point. (4) One test added (`test_apply_grow_stage_template_get_devices_exception`) to cover the `get_devices` exception path, reaching 100% coverage.
- **Watch out for Phase 15:** (1) `apply_grow_stage_template` with 3 sequential live writes is the most likely target for the rate-limit smoke test — ensure the Phase 15 quality review confirms the 1.5s gap is correctly handled. (2) The plan says "16 tools + 3 prompts" but the actual count is 18 tools (set_port_on and set_port_off were added as separate tools, not part of set_port_mode count). Update the plan's Section 2 coverage summary table accordingly. (3) Phase 12 plan status still shows "Pending" — needs to be updated to ✅ Complete (PR #18 was merged).
- **Actual effort vs estimate:** ~1h actual vs ~2h planned — under budget.
- **Investment time:** 2026-05-21 session — ~1h wall-clock from /clear to PR #20 raised.
- **Defects found:** None

**Phase 14 Time Investment**
- **Date:** 2026-05-21
- **Actual Claude session time:** ~1:00
- **Projected manual time:** 8h–14h (midpoint ~11h)
- **Manual estimate basis:** Decent Python, familiar with write infrastructure. Primary cost drivers: designing partial-failure response format, implementing 3 sequential writes with per-write error handling, writing ~22 tests covering all edge cases (6 stages, partial failures, AI+), writing 3 prompt bodies with accurate content, updating wire protocol tests.
- **Multiplier:** ~11x (11h projected midpoint ÷ ~1h actual)

---

### Phase 15 — Quality Cycle (Second Pass)
**Status:** ✅ Complete
**PR:** [feat(quality): Phase 15 — quality cycle, docs, community files, GitHub hardening](https://github.com/ober37/ac-infinity-mcp/pull/29)
**Deliverable:** All 16 tools + 3 prompts quality-gated; README + docs/API.md fully updated; 100% coverage; all open GitHub issues resolved
**Effort:** ~5h | **Sequential after Phase 14 — runs after all planned functionality is complete**

Same structure as Phase 11: two-model review (Sonnet first pass, Opus independent second pass), no inherited findings between passes, iterate until fully clean.

**Part 1 — README + docs/API.md refresh**
- Update README features table to reflect all 16 tools + 3 prompts
- Update hardware compatibility table if any AI+ behavior changed
- Update docs/API.md with new tools' parameter/return schemas and any new API quirks discovered

**Part 2 — Address GitHub Issues**
- Pull the full open issue list from `ober37/ac-infinity-mcp`
- Triage each open issue: in-scope for v1.0, defer to v2.0, or close as won't-fix
- Implement all in-scope issues before proceeding to Part 3
- Known issues at plan time:
  - [#17](https://github.com/ober37/ac-infinity-mcp/issues/17) `peak_hour` field is UTC but not labeled — rename to `peak_hour_utc` in `ActivityReport`, tool output, tests, and docs/API.md

**Part 3 — Deep Quality Cycle**
- Pass 1: Test coverage gap analysis + remediation (target ≥ 90% across all modules)
- Pass 2: Expert holistic code review (Senior Python Engineer, no prior findings)
- Pass 3: Expert holistic security review (Security Engineer, no prior findings)
- Restart from Pass 1 if any remediation required

**Gate loop:** All 5 gates per CLAUDE.md

**Phase 15 Lessons Learned**
- **What went well:** Two independent Sonnet review passes (code + security) plus a cold second-model pass caught substantive bugs that had survived all prior phases — particularly the VPD banker's rounding error (present since Phase 13) and the check_vpd_drift alert text giving backwards advice. The PR template with AI model + device fields was a clean design decision. Branch protection setup via gh CLI was straightforward once the exact check names were confirmed from the live commit check-runs endpoint.
- **Changed from plan:** (1) Opus was unavailable (daily session limit); second-model review used a cold Sonnet instance instead. Attribution noted in PR. (2) Tool count in plan was "16 tools" — corrected to 18 throughout docs. (3) `get_port_activity_report` was missing from docs/API.md (first code review caught this — not in original scope). (4) Phase 15 found 2 HIGH defects (VPD rounding, grade mismatch) and 1 MEDIUM (missing `deviation` field) that required code changes and restarting Gate 4; the quality cycle worked as designed.
- **Watch out for Phase 16:** (1) Some earlier phases are missing the Investment Time block entirely (check Phases 3–10). (2) The two-model review pattern should be attributed as "Sonnet first pass / cold Sonnet second pass" not "Sonnet/Opus" for Phase 15 specifically — Opus was unavailable. (3) The actual tool count is 18 (not 16) everywhere in the report. (4) Phase 15 found HIGH defects in Phase 13 code (VPD encoding) — note in Section 3 that these were latent defects discovered in the Phase 15 quality pass, not Phase 13 gate loop.
- **Actual effort vs estimate:** ~3h actual vs ~5h planned — under budget despite finding and fixing HIGH defects.
- **Investment time:** 2026-05-21 session — ~3h wall-clock from /clear to PR #29 raised.
- **Defects found:**
  - [D001] VPD banker's rounding: `round(1.25*10)=12` (should be 13); affected veg stage template and any user-supplied VPD with .X5 pattern | Discovered: Independent second-model review (HIGH) | Severity: high | Resolved: Y
  - [D002] Grade boundary mismatch: `_grade()` threshold B≥80 but `environment_alert_interpretation` prompt said B=75–89 | Discovered: Independent second-model review (HIGH) | Severity: high | Resolved: Y
  - [D003] `deviation` field documented in prompt but absent from `check_vpd_drift` response | Discovered: Independent second-model review (MEDIUM) | Severity: medium | Resolved: Y
  - [D004] check_vpd_drift alert text gave backwards advice: HIGH said "reduce humidity" (wrong — should raise humidity); LOW said "lower fan speed or increase humidity" (wrong — should lower humidity) | Discovered: Independent second-model review (LOW) | Severity: medium | Resolved: Y
  - [D005] `get_port_activity_report` missing from docs/API.md MCP Tool Reference section | Discovered: First code review pass | Severity: low | Resolved: Y

**Phase 15 Time Investment**
- **Date:** 2026-05-21
- **Actual Claude session time:** ~3:00
- **Projected manual time:** 20h–35h (midpoint ~27h)
- **Manual estimate basis:** Experienced Python developer. Primary cost drivers: running two independent review passes with remediation cycles, researching correct VPD rounding behavior, writing 9 new docs/API.md tool reference sections, creating 5 community files, configuring branch protection (gh API + correct check name lookup), creating milestones and closing issues, writing tests for new fields.
- **Multiplier:** ~9x (27h projected midpoint ÷ ~3h actual)

---

### Phase 16 — Project Report
**Status:** Pending
**Deliverable:** `PROJECT_REPORT.md` committed to `ober37/ac-infinity-mcp` on `main`
**Effort:** ~2h | **After Phase 15 quality cycle complete**

**Step 0 — Planning session:** Review the report outline and confirm all metric inputs are available: LOC counts, test counts, PR list, time estimates per phase, and the lessons learned sections from all completed phases. Get user approval on the outline before writing.

**Report structure** (matched to `discord-mcp-server/PROJECT_REPORT.md` format):

**Section 1 — Overview**
One-paragraph summary: starting point (5 tools in monorepo), ending point (16 tools + 3 prompts, standalone PyPI package + Docker image). Disclaimer: built on personal Claude subscription credits.

**Section 2 — Coverage Summary**
Four tables:

- **Tools table:** Monorepo vs. v1.0 Standalone delta (`+11 tools, +220%`, `+4 modules`, `10 PRs merged`)
- **Lines of Code table:** Source LOC and test LOC before/after (actuals gathered at report time)
- **Test suite table:** Test count, test files, failures
- **API coverage table:** Phase-by-phase coverage progression (Phase 7 → 56%, Phase 8 → 88%, Phase 9 → 100%)
- **Controller compatibility table:** 69 Pro (devType 11, legacy), 69 Pro+ (devType 18, legacy), 89 AI+ (devType 20, new framework)

**Section 3 — Comprehensive Code Review**
Multi-pass format:
- Pass 1: Sonnet — Gate 1 + Gate 2 per PR, plus holistic audit at Phase 11 and Phase 15
- Pass 2: Independent second-model review (Opus or next-gen Sonnet, no prior exposure to Pass 1 findings) — run at Phase 11 and Phase 15
- Summary table: Critical / High / Medium / Low findings, origin (introduced vs. inherited from monorepo), all resolved?
- Per-finding detail tables: #, Discovered by, Origin, File, Issue, Fix Applied

**Section 4 — Time Investment Summary**
Pull directly from the `**Phase N Time Investment**` blocks written at the close of each phase session (Phase 0 through Phase 15). Each block contains: actual Claude session time, projected manual time (range + midpoint), manual estimate basis, and multiplier.

Table format:
| Phase | Actual Claude Time | Projected Manual | Multiplier | Notes |
|---|---|---|---|---|
| Phase 0 — Ideation | 0:52 | 26–46h (mid ~36h) | ~41x | Zero ACI API knowledge; API research dominates |
| Phase 1 — ... | HH:MM | Xh–Yh | ~Xx | [from block] |
| ... | | | | |
| **Total** | **HH:MM** | **Xh–Yh** | **~Xx** | |

The overall multiplier (total projected manual ÷ total Claude time) is the headline metric for the report and suitable for the README/announcement post.

**Section 5 — Lessons Learned**
Synthesized from all Phase Lessons Learned sections written throughout execution.
Format: numbered list, each item titled, 2–4 paragraphs.
Seed topics (expand at report time with actuals from phases):
- The Phase Planning Session pays for itself
- Per-device test split catches controller-type bugs unit tests miss
- `dry_run` design pattern before every write-control call
- Second-model review finds what the first didn't (same pattern as Discord)
- Living plan file as shared state across sessions
- API quirk documentation is as valuable as the code

**Section 6 — What Remains**
- v2.0 roadmap (link to GitHub Project)
- Peekaboopoint monorepo cleanup status
- Any open issues at time of report

**Section 7 — PR Appendix**
All PRs merged to `ober37/ac-infinity-mcp`, chronological:
| PR | Date | Title | Summary |

---

## Parallelization Map

```
Phase 1 (scaffold + core)      ──────── 3h    ← must run first
                               ↓
Phase 2 (tests)                ────── 2.5h ──────────────────────────────┐
Phase 3 CI YAML draft          ── 0.5h (write during Phase 2)           │
                                    ↓                                     │
Phase 3 finalize + PR          ─ 1h                                      │
                                                                          ↓
Phase 4 (Docker)               ──── 1.5h ←── after Phase 1              │
Phase 5 (Docs)                 ────────── 3h ←── after Phase 1          │
Phase 6 (hardening)            ─── 2h ←── after Phase 1                 ↓
                                    ↓
Phase 7–10 (write + CI + tests) ─── merged/re-scoped ←── completed
                                    ↓
Phase 11 (README + quality)    ─── 6h ←── complete ✅
                                    ↓
Phase 12 (new read tools)      ── 2h
                                    ↓
Phase 13 (automation write)    ─── 3h
                                    ↓
Phase 14 (intelligence+prompts) ── 2h
                                    ↓
Phase 15 (quality cycle #2)    ──── 4h
                                    ↓
Phase 16 (project report)      ── 2h
```

**Remaining sequential critical path (Phase 12→16):** ~13h

---

## Changes Required in Peekaboopoint Monorepo

After standalone repo is live and all 16 tools confirmed working:

### Update:
1. `skills/ac-infinity-access/SKILL.md` — Add "Server installation" section pointing to `ober37/ac-infinity-mcp`; tool names + behavior unchanged
2. `docker-compose.dev.yml` — Pull `ghcr.io/ober37/ac-infinity-mcp:latest` instead of local build

### Remove (after confirmed working):
3. `scripts/acinfinity/` — entire directory
4. `tests/acinfinity/` — entire directory
5. `docker/ac-infinity/` — entire directory

### Leave unchanged:
- All agent config files (`04-grow-tracker.md`, etc.)
- `grows/active/*/device-mapping.json`
- `blueprint/ac-infinity-mcp-architecture.md`

---

## v1.0 Announcement Checklist

- [ ] GitHub repo `ober37/ac-infinity-mcp` live — description + topics: `mcp`, `ac-infinity`, `cannabis`, `grow-automation`, `iot`, `fastmcp`
- [ ] README with CI/PyPI/Docker badges, copy-paste Claude Desktop config
- [ ] All 16 tools + 3 prompts documented in `docs/API.md`
- [ ] GitHub Actions CI passing, badge in README
- [ ] Docker image on `ghcr.io/ober37/ac-infinity-mcp`
- [ ] PyPI package `ac-infinity-mcp` published
- [ ] GitHub Release tagged `v1.0.0` with full release notes
- [ ] All 15 API quirks documented
- [ ] `dry_run` usage guide in docs
- [ ] MCP registry listing submitted
- [ ] Monorepo cleaned up; peekaboopoint confirmed working
- [ ] `PROJECT_REPORT.md` committed to `main` (Phase 11 deliverable — run after all above are checked)

---

## v2.0 Feature Tracking Strategy

### How features are tracked

**Three-layer tracking on GitHub (all free on public repos):**

1. **GitHub Issues** — Each feature is a self-contained spec issue. Point me at the URL, I fetch the full spec and implement. No external context needed.

2. **GitHub Milestone: "v2.0"** — All v2.0 issues are linked to this milestone. Shows progress as issues close (e.g., "3/10 complete"). Created when the repo goes live.

3. **GitHub Project (free, public repos)** — A Project board with a Roadmap view (timeline) for visual planning. Issues added to the project inherit milestone grouping. URL shareable — I can read and update project state via GitHub MCP.

**Setup at repo launch:** Create milestone "v2.0" → create all 10 issues → create a GitHub Project named "AC Infinity MCP Roadmap" → add all v2.0 issues to the project → set Roadmap view as default.

**Free tier note:** GitHub Projects and Milestones are fully unlimited on public repositories. GitHub Actions CI uses the public repo quota (effectively unlimited minutes for public repos — no constraints here).

**To implement any feature:** Point me at the issue URL and say "implement this." Everything needed is embedded in the issue.

### Required Issue Template (applies to every v2.0 issue)

Each issue must include:

```markdown
## Summary
One sentence on what this does and why growers want it.

## User Story
As a [cannabis grower / hydro operator], I want to [action] so that [outcome].

## Scope Boundary
- v2.0: requires [external sensor / BLE hardware]
- Blocked on: [hardware requirement or API gap]

## AC Infinity API
- Endpoint: `/api/...`
- Key fields: [field names + encoding notes]
- Source: [link to homebridge or HA implementation line]

## Implementation Notes
Relevant reverse-engineered code and known constraints.

## Acceptance Criteria
- [ ] Tool returns correct data when sensor is connected
- [ ] Tool returns meaningful error when sensor is absent or unsupported
- [ ] Both controller types tested (legacy + new framework)
- [ ] Documented in docs/API.md
- [ ] Unit tests cover connected / absent / invalid states
```

### v2.0 Issues to Create at Repo Launch

Create these as GitHub Issues immediately after v1.0 ships, tagged `v2.0` + appropriate labels:

| Issue | Feature | Hardware Required | `sensorType` |
|---|---|---|---|
| #1 | CO2 monitoring + automation triggers | UIS CO2 sensor | 11 |
| #2 | Soil moisture monitoring | UIS soil moisture sensor | 10 |
| #3 | Light level monitoring | UIS light sensor | 12 |
| #4 | Hydro pH monitoring + automation | UIS pH probe | 13 |
| #5 | Hydro EC monitoring (µS/cm + mS/cm) | UIS EC probe | 14, 15 |
| #6 | Hydro TDS monitoring (ppm + ppt) | UIS TDS probe | 16, 17 |
| #7 | Water temperature monitoring | UIS water temp probe | 18, 19 |
| #8 | Water level monitoring | UIS water level sensor | 20 |
| #9 | Bluetooth local control | BLE stack — `ac-infinity-ble` library |  |
| #10 | Offline/local mode (no cloud) | BLE only |  |

**Implementation note for issues #1-8:** The API already returns sensor data in the `sensors` array from `/api/user/devInfoListAll` — no new endpoints needed. Each issue's implementation is: parse the correct `sensorType` enum value, expose the reading, add automation write support. The `sensorData` field is divided by `sensorPrecision` for the actual value.

**Implementation note for issues #9-10:** These require the `ac-infinity-ble` Python library (`pip install ac-infinity-ble`) and a Bluetooth stack. Cloud API not involved. Separate authentication model (BLE pairing vs. cloud token).
