# Security Risks — Accepted Risks and Dependency CVEs

This file documents two categories of accepted risk:

1. **Accepted Risk — HTTP only:** all upstream AC Infinity API endpoints use plain HTTP.
2. **Accepted Dependency CVEs:** CVEs in transitive or direct dependencies that are
   documented as accepted-risk. Each entry explains why the CVE does not affect this
   codebase in practice, and includes a re-evaluation date so the ignore is not permanent.

`pip-audit` is invoked with `--ignore-vuln <ID>` for each CVE entry below. If a
new CVE appears in a `pip-audit` run, **do not** blanket-add it here — file
an issue, evaluate the exposure, and only ignore after a documented finding.

---

## Accepted Risk — HTTP only (no TLS)

**Base URL:** `http://www.acinfinityserver.com/api`

The AC Infinity cloud API communicates over plain HTTP. Session tokens, credentials,
and sensor data are transmitted unencrypted. This is an upstream limitation of the
AC Infinity platform; no HTTPS alternative is available. The risk is accepted for
local/trusted network deployments. See `docs/DEPLOYMENT.md` for HTTPS reverse-proxy
options if external exposure is a concern.

All endpoints below are subject to this accepted risk. This list was updated via
network capture (Phase 17, 2026-05-22) to include all confirmed v2.0 endpoints.

### Legacy-path endpoints (confirmed)

| Endpoint | Purpose |
|---|---|
| `POST /user/appUserLogin` | Authentication — transmits credentials in plaintext |
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

## PYSEC-2025-183 — `mcp` package

- **Package:** `mcp` (Model Context Protocol Python SDK; a direct dependency)
- **First observed:** 2026-05-22 (Phase 16 triple-persona quality cycle)
- **Why this server is not exploitable:** the vulnerability is in the
  `mcp` SDK's transport handling for code paths this server does not use.
  Our server runs stdio transport only (see `server.py:main()`); the
  affected paths require HTTP/SSE transport.
- **Mitigation:** monitor upstream `mcp` releases. When a patched release
  is available, bump the version pin in `pyproject.toml` and remove the
  ignore from `.github/workflows/ci.yml` and `CLAUDE.md`.
- **Re-evaluation due:** 2026-08-22 (3 months from acceptance) — check
  upstream advisories quarterly until resolved or scope changes.
- **Verifying the ignore is still required:** run `pip-audit` *without*
  the `--ignore-vuln` flag in a clean venv with current pins. If
  PYSEC-2025-183 does not appear, the patched version is already pulled
  in and the ignore can be removed. P3-C2-F006 raised this concern after
  observing that a fresh install showed no findings under current pins;
  remove the ignore as soon as the next CI run confirms the CVE is gone.

---

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

### starlette — PYSEC-2026-161

- **Package:** `starlette` (transitive via `mcp` SDK)
- **CVE:** PYSEC-2026-161 (fix: 1.0.1)
- **Exposure:** Transitive via `mcp` SDK's HTTP/SSE transport path. This server
  uses **stdio transport only** — the affected starlette request-handling code
  paths are never reached.
- **Re-evaluation:** 2026-08-22 — upgrade when `mcp` bumps its starlette pin.

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
