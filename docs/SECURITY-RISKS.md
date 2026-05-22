# Security Risks — Accepted Dependency CVEs

This file lists CVEs in transitive or direct dependencies that are documented
as accepted-risk for this project. Each entry explains why the CVE does not
affect this codebase in practice, and includes a re-evaluation date so the
ignore is not permanent.

`pip-audit` is invoked with `--ignore-vuln <ID>` for each entry below. If a
new CVE appears in a `pip-audit` run, **do not** blanket-add it here — file
an issue, evaluate the exposure, and only ignore after a documented finding.

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

## How to add a new accepted CVE

1. Run `pip-audit` and capture the CVE ID + package.
2. Read the upstream advisory; identify the exact vulnerable code path.
3. Confirm that path is not reachable from this server's code.
4. Add an entry above with rationale and a re-evaluation date no more than
   3 months out.
5. Add `--ignore-vuln <ID>` to the `pip-audit` invocation in
   `.github/workflows/ci.yml` with an inline comment pointing here.
6. Reference this file from the PR that adds the ignore.
