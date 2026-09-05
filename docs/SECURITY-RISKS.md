# Security Risks — Accepted Risks and Dependency CVEs

This file documents two categories of accepted risk:

1. **Note — HTTPS confirmed:** all upstream AC Infinity API endpoints support HTTPS (TLSv1.3, verified 2026-05-29).
2. **Accepted Dependency CVEs:** CVEs in transitive or direct dependencies that are
   documented as accepted-risk. Each entry explains why the CVE does not affect this
   codebase in practice, and includes a re-evaluation date so the ignore is not permanent.

**Current posture: `pip-audit` runs with no `--ignore-vuln` flags at all.** Every CVE
below is RESOLVED — each was fixed by a version pin rather than permanently suppressed.
If a new CVE appears in a `pip-audit` run, **do not** blanket-add it here — file an
issue, evaluate the exposure, and only ignore after a documented finding.

---

## HTTPS confirmed (TLSv1.3)

**Base URL:** `https://www.acinfinityserver.com/api`

The AC Infinity cloud API supports HTTPS. TLS handshake verified 2026-05-29: TLSv1.3,
DigiCert Encryption Everywhere DV TLS CA certificate, valid until 2026-11-18.
Credentials and session tokens are encrypted in transit. This supersedes the previous
HTTP-only accepted risk documented before 2026-05-29.

All endpoints below are confirmed over HTTPS. This list was updated via network capture
(Phase 17, 2026-05-22) to include all confirmed v2.0 endpoints.

### Legacy-path endpoints (confirmed)

| Endpoint | Purpose |
|---|---|
| `POST /user/appUserLogin` | Authentication — credentials encrypted via TLS |
| `POST /user/devInfoListAll` | Device list — response includes user email (`appEmail`) |
| `POST /log/dataPage` | Historical sensor and port data |
| `POST /dev/getdevModeSettingList` | Read current port mode settings |
| `POST /dev/addDevMode` | Write port mode settings |
| `POST /api/dev/getDevSetting` | Richer port settings (sensor calibration, load type, Matter/UUID fields) |
| `POST /api/upgrade/getUpgrade` | Firmware upgrade check |
| `POST /api/upgrade/downgrade` | Firmware downgrade info (returns download URL and release notes) |

### v2.0 automation management endpoints (confirmed via network capture)

| Endpoint | Purpose |
|---|---|
| `POST /api/version=2.0/dev/getGroups` | List all automation groups for a device |
| `POST /api/version=2.0/dev/addGroups` | Create automation group |
| `POST /api/version=2.0/dev/updateGroupsById` | Edit an automation rule in place by `advId` (read-before-write) |
| `POST /api/version=2.0/dev/updateGroupsIsOn` | Toggle automation on/off state |
| `POST /api/version=2.0/dev/delByid` | Delete automation |

### v2.0 alarm management endpoints (confirmed via network capture)

| Endpoint | Purpose |
|---|---|
| `POST /api/version=2.0/dev/getAlarms` | List all alarm configurations for a device |
| `POST /api/version=2.0/dev/addAlarms` | Create alarm |
| `POST /api/version=2.0/dev/updateAlarmsById` | Enable, disable, or edit alarm |
| `POST /api/version=2.0/dev/delAlarmsByid` | Delete alarm |

### v2.0 history and template endpoints (confirmed)

| Endpoint | Purpose |
|---|---|
| `POST /api/log/logdataByAll` | Historical readings (alternative to `/log/dataPage`; confirmed working) |
| `DELETE /api/log/log?devId=...&time=...` | Delete all history logs for a device |
| `GET /api/version=2.0/dev/recipe?advVersion=1` | Grow stage templates (Seedling, Vegetative, Flowering, Plant Kit, Drying) |

---

## Single active session per account (accepted limitation, #252)

AC Infinity permits only one active session per account. Authenticating through this server
(initial login or a transparent session-expiry re-auth) can invalidate the user's AC Infinity
mobile-app session, and vice versa. This is an upstream account-model constraint, not a
defect in this server.

- **Impact:** the user may be signed out of the mobile app while the server holds an active
  session. The user's controllers, schedules, and settings are unaffected — logging back
  into the app restores app access (and may in turn invalidate the server's token, which the
  server re-acquires on the next read).
- **Write safety:** session expiry (API body code `10003`) triggers transparent re-auth and
  retry on **reads only**. On **writes** it is surfaced as an API error and never replayed,
  because the write may have been processed server-side before the expiry response — a silent
  retry could double-apply state. A refresh-failure cache bounds re-login to one attempt so a
  bad credential does not hammer the login endpoint. See `docs/API.md` Quirk 31.
- **Mitigation:** none required; documented so growers understand why app sign-outs occur.
  The `discover_devices` tool docstring carries a grower-readable heads-up.

---

## Client User-Agent identification (#251)

The client sends AC-app-style `User-Agent` headers rather than the default
`python-requests` UA, so traffic is indistinguishable from the official mobile app:

| Endpoint class | `User-Agent` |
|---|---|
| Login (`/user/appUserLogin`) | `ACController/1.8.2 (com.acinfinity.humiture; build:489; iOS 16.5.1)` |
| Data / write endpoints | `okhttp/3.10.0` |

These are spoofed identity strings sent to the upstream API; they contain no user data and
no credentials. A regression test locks both values. This is an accepted-behavior note, not
a vulnerability.

The AI+ write path (`devType >= 20`) sends one additional spoofed header,
`minversion: 3.5`, and **no** extra `User-Agent` — the table above is complete.
That header is an opaque server-side gate rather than an app identity: without it
`addDevMode` returns `100001`, and only the literal string `"3.5"` is accepted
(see `docs/API.md` Quirk 14 for the ablation). Three further app-identity headers
were tested and proven unnecessary, so they are deliberately not sent — every
declared header is surface for the kind of upstream tightening that broke the v2
endpoints in #298. The exact value is locked by a regression test mirroring the
#251 lock.

---

## PYSEC-2025-183 — `mcp` package — RESOLVED (Issue #313, 2026-08-28)

- **Package:** `mcp` (Model Context Protocol Python SDK; a direct dependency)
- **Why this server was never exploitable:** the vulnerability is in the `mcp` SDK's
  transport handling for code paths this server does not use. Our server runs stdio
  transport only (see `server.py:main()`); the affected paths require HTTP/SSE
  transport. The exposure was theoretical throughout.
- **History:** first observed 2026-05-22 (Phase 16 triple-persona quality cycle) and
  accepted with a `--ignore-vuln` flag plus a 2026-08-22 re-evaluation date. P3-C2-F006
  later noted that a fresh install showed no findings under current pins, and set the
  retirement test: run `pip-audit` *without* the flag in a clean venv: if the CVE does
  not appear, the patched version is already pulled in and the ignore can be removed.
- **Resolution (Issue #313):** that test now passes. Issue #309 pinned
  `mcp>=1.14.1,<2` (the unbounded floor was resolving to 2.x, which renamed `FastMCP`
  and broke CI), which resolves `mcp` to **1.29.1**. Verified in a throwaway venv built
  from scratch with the current pins: `pip-audit` with **zero** ignore flags reports
  `No known vulnerabilities found`, exit 0. Corroborated by CI, where the audit step
  passed on Python 3.11 and 3.12 while the flag it still carried matched nothing.
- **Ignore removed from** `.github/workflows/ci.yml` and the Gate 4 checklist in
  `CLAUDE.md`. This was the project's last `--ignore-vuln` flag.
- **Note:** this CVE is resolved by the `<2` *ceiling* holding `mcp` on a patched 1.x.
  The eventual `mcp` 2.x migration (#310) must re-run `pip-audit` and confirm 2.x is
  also unaffected before that ceiling is lifted.

---

## starlette CVEs — RESOLVED (Issue #303, 2026-07-11)

- **Package:** `starlette` (transitive via the `mcp` SDK's HTTP/SSE transport). This server
  runs **stdio transport only** (see `server.py:main()`), so the affected request-handling
  code paths were never reached at runtime — the exposure was theoretical.
- **History:** PYSEC-2026-161 (first observed 2026-05-26, Issue #162 Gate 4); and
  CVE-2026-54282 / CVE-2026-54283 = PYSEC-2026-248 / PYSEC-2026-249 (fix: 1.3.0 / 1.3.1;
  observed Issue #284 Gate 4, 2026-06-24). The earlier mitigation plan was to wait for the
  upstream `mcp` pin to raise the `starlette` floor.
- **Resolution (Issue #303):** `pyproject.toml` now pins `starlette>=1.3.1` **directly**
  rather than waiting on `mcp`. `mcp` requires only `starlette>=0.27`, so the stricter floor
  is compatible. `pip-audit` reports **no** starlette advisories at 1.3.1. Nothing is added
  to `--ignore-vuln` for starlette.

---

## Pre-existing Transitive / Dev-tool CVEs (documented 2026-05-22)

The following 14 CVEs were identified in a `pip-audit` run on 2026-05-22 as part of
the Phase 17 Gate 2 review. None are exploitable via this server's code paths.
They are **documented here for tracking** but are NOT added to the `--ignore-vuln`
list — each requires an explicit decision before ignoring. Re-evaluate when a fix
becomes available in the dependency tree.

### cryptography — PYSEC-2026-36, PYSEC-2026-35

- **Package:** `cryptography` (transitive via `mcp` SDK)
- **CVEs:** PYSEC-2026-36 (fix: 46.0.7), PYSEC-2026-35 (fix: 46.0.6)
- **Exposure:** Transitive dependency — not imported directly by this server.
  Vulnerability affects internal cryptography primitives not invoked by our code paths.
- **Re-evaluation:** 2026-08-22 — bump `mcp` when upstream updates to cryptography ≥ 46.0.7.

### idna — CVE-2026-45409

- **Package:** `idna` (transitive via `requests`)
- **CVE:** CVE-2026-45409 (fix: 3.15)
- **Exposure:** Transitive dependency for hostname encoding. This server's HTTP calls
  only connect to `www.acinfinityserver.com` (a simple ASCII hostname); the IDNA
  vulnerability (malformed label handling) is not reachable.
- **Re-evaluation:** 2026-08-22 — upgrade `requests` or `idna` directly once 3.15 is
  available in the dependency tree.

### pip — CVE-2026-3219, CVE-2026-6357

- **Package:** `pip` (dev tool — not a runtime dependency)
- **CVEs:** CVE-2026-3219 (fix: 26.1), CVE-2026-6357 (fix: 26.1)
- **Exposure:** Dev-time only. `pip` is not imported or used by the server at runtime.
  Upgrade `pip` in the build/dev environment: `python3 -m pip install --upgrade pip`.
- **Re-evaluation:** 2026-08-22.

### pygments — CVE-2026-4539

- **Package:** `pygments` (dev tool — pulled in by rich/IPython for terminal output)
- **CVE:** CVE-2026-4539 (fix: 2.20.0)
- **Exposure:** Dev-time only. `pygments` is not imported or used at runtime.
- **Re-evaluation:** 2026-08-22.

### python-multipart — CVE-2026-40347, CVE-2026-42561

- **Package:** `python-multipart` (transitive via `mcp` → `starlette`)
- **CVEs:** CVE-2026-40347 (fix: 0.0.26), CVE-2026-42561 (fix: 0.0.27)
- **Exposure:** Transitive via `mcp` SDK's HTTP/SSE transport. This server runs
  **stdio transport only** — the multipart parsing code paths are never invoked.
- **Re-evaluation:** 2026-08-22 — upgrade when `mcp` bumps its `starlette`/
  `python-multipart` pins.

### setuptools — PYSEC-2022-43012, PYSEC-2025-49, CVE-2024-6345

- **Package:** `setuptools` (build/install tool — not a runtime dependency)
- **CVEs:** PYSEC-2022-43012 (fix: 65.5.1), PYSEC-2025-49 (fix: 78.1.1),
  CVE-2024-6345 (fix: 70.0.0)
- **Exposure:** Build-time only. `setuptools` is used during package installation;
  it is not imported at runtime.
- **Mitigation:** Upgrade in the build environment: `pip install --upgrade setuptools`.
- **Re-evaluation:** 2026-08-22.

---

## How to add a new accepted CVE

1. Run `pip-audit` and capture the CVE ID + package.
2. Read the upstream advisory; identify the exact vulnerable code path.
3. Confirm that path is not reachable from this server's code.
4. Add an entry above with rationale and a re-evaluation date no more than
   3 months out.
5. Add `--ignore-vuln <ID>` to the `pip-audit` invocation in
   `.github/workflows/ci.yml` with an inline comment pointing here.
6. Reference this file from the PR that adds the ignore.
