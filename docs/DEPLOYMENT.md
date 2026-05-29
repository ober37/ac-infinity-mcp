# Deployment Guide

This document covers deployment patterns beyond running the server directly on
the developer's workstation. The main concern is:

1. **Where the server runs** (workstation, home server, container).

The server itself speaks MCP over stdio to the local client (Claude Desktop /
Cline / Codex / etc.), so the local link is a process-to-process pipe — no
network exposure on the MCP side. The upstream connection to
`www.acinfinityserver.com` uses HTTPS (TLSv1.3 — verified 2026-05-29; see
`docs/API.md` Quirk 8), so credentials and session tokens are encrypted in transit.

---

## Standard deployment — local workstation

This is what the README walks through and is the safest default for most
growers. The server runs on the same laptop as the MCP client, talks to the
upstream API over HTTPS, and never opens a listening port. **No reverse proxy
is needed.**

---

## Container deployment

The provided `Dockerfile` runs as a non-root `appuser` in a multi-stage build
with no secret baked in. `docker-compose.yml` adds `read_only: true`,
`cap_drop: ALL`, and `no-new-privileges` (see `docker-compose.yml` for the
current set).

```bash
# Build and run with .env supplied at runtime
docker compose up --build
```

If you deploy multiple instances (one per grower), give each its own `.env`
file. Do not bake credentials into the image; the CI workflow checks that no
`.env` is present in the built image (`/.github/workflows/ci.yml`).

---

## What to NOT do

- **Do not** check `.env` into source control — `.gitignore` covers it, but
  verify before pushing.
- **Do not** run the container as root. The Dockerfile sets `USER appuser`
  and docker-compose.yml pins `user: appuser` defensively.
- **Do not** ignore `pip-audit` findings without documenting them in
  `docs/SECURITY-RISKS.md` first.
