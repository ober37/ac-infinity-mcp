# Deployment Guide

This document covers deployment patterns beyond running the server directly on
the developer's workstation. The two main concerns are:

1. **Where the server runs** (workstation, home server, container).
2. **How upstream HTTP traffic to AC Infinity is protected**, since the
   AC Infinity cloud API does not support TLS (`docs/API.md` Quirk 8).

The server itself speaks MCP over stdio to the local client (Claude Desktop /
Cline / Codex / etc.), so the local link is a process-to-process pipe — no
network exposure on the MCP side. The exposed surface is the upstream HTTP
connection to `www.acinfinityserver.com`.

---

## Standard deployment — local workstation, no reverse proxy

This is what the README walks through and is the safest default for most
growers. The server runs on the same laptop as the MCP client, talks to the
upstream API over the local network's egress, and never opens a listening
port. **No reverse proxy is needed for this case.**

The HTTP-only upstream is an accepted risk for this pattern:

- Outgoing traffic traverses your home / office network in plain text.
- Any device on the same network can sniff the credentials and the device
  state. The credentials grant control over the grow controllers on the
  authenticated account.
- This is documented in `SECURITY.md` and `docs/API.md` Quirk 8.

If your local network is trusted (typical home LAN), this risk is acceptable.
If you operate on an untrusted network (shared coworking, public Wi-Fi, a
co-location facility), use one of the mitigations below.

---

## Mitigation 1 — VPN / WireGuard tunnel egress

The simplest mitigation is to route the server's egress traffic through a
WireGuard / OpenVPN tunnel to a trusted endpoint, so the plain-text HTTP
traffic only traverses the network between your tunnel endpoint and the
AC Infinity backend (which is over the public Internet anyway, where TLS
would be useful — but is not offered).

This does **not** add TLS to the upstream link; it adds privacy from
co-resident network devices.

Setup is out of scope here — any standard WireGuard tutorial covers it.

---

## Mitigation 2 — Local HTTPS reverse proxy with hostname rewrite

If you want to terminate the HTTP-only upstream on a TLS endpoint you control
and rewrite the requests, you can run a local reverse proxy in front of the
AC Infinity API. **This requires the upstream to be reachable from your
reverse proxy and accepts that the proxy-to-AC-Infinity hop remains plain
HTTP** — the cleartext segment is shortened, not eliminated.

### Minimal Caddy example

```caddyfile
# /etc/caddy/Caddyfile
acinfinity.proxy.local {
    tls internal     # mkcert / Caddy local CA for development trust
    reverse_proxy http://www.acinfinityserver.com {
        header_up Host www.acinfinityserver.com
    }
}
```

Set the server's `BASE_URL` override to point at the proxy:

```bash
# env / .env (set in your MCP client config)
AC_INFINITY_API_BASE=https://acinfinity.proxy.local
```

The `BASE_URL` constant in `src/ac_infinity_mcp/client.py` is currently a
class constant; consuming `AC_INFINITY_API_BASE` would require a small code
change (a v2.0 candidate — file an issue against the v2.0 milestone if you
want this implemented).

### Minimal nginx example

Let's Encrypt does not issue certificates for `.local`, `.test`, or other
ICANN-reserved private TLDs. If you want LE-signed certs, substitute your own
publicly-resolvable domain. For local-only use, generate a self-signed cert
with `mkcert` (recommended) or `openssl` and trust it at the OS level.

```nginx
server {
    listen 8443 ssl;
    server_name acinfinity.proxy.local;

    # Local-only example: use mkcert to generate a cert trusted by your OS.
    #   mkcert -install
    #   mkcert acinfinity.proxy.local
    # For a publicly-resolvable hostname, replace these paths with the
    # Let's Encrypt certificate paths after running certbot.
    ssl_certificate     /etc/nginx/certs/acinfinity.proxy.local.pem;
    ssl_certificate_key /etc/nginx/certs/acinfinity.proxy.local-key.pem;

    location / {
        proxy_pass http://www.acinfinityserver.com;
        proxy_set_header Host www.acinfinityserver.com;
        proxy_set_header User-Agent $http_user_agent;
    }
}
```

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

- **Do not** expose the upstream HTTP traffic to the public Internet without
  a proxy. The cleartext credentials are recoverable by any on-path observer.
- **Do not** check `.env` into source control — `.gitignore` covers it, but
  verify before pushing.
- **Do not** run the container as root. The Dockerfile sets `USER appuser`
  and docker-compose.yml pins `user: appuser` defensively.
- **Do not** ignore `pip-audit` findings without documenting them in
  `docs/SECURITY-RISKS.md` first.
