# Security Policy

## Supported Versions

| Version | Supported |
|---|---|
| latest (main) | Yes |
| older releases | No |

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Use [GitHub Private Vulnerability Reporting](https://github.com/ober37/ac-infinity-mcp/security/advisories/new) to report vulnerabilities confidentially. You will receive a response within 7 days.

## Scope

This project is an MCP server that communicates with the AC Infinity cloud API over HTTP. Known limitations that are accepted risks:

- The AC Infinity cloud API uses HTTP only (no TLS). This is an upstream limitation outside the scope of this project. See `docs/DEPLOYMENT.md` for HTTPS reverse-proxy options.
- Credentials (`AC_INFINITY_EMAIL`, `AC_INFINITY_PASSWORD`) are passed via environment variables. Never commit `.env` files or hardcode credentials.
- This server is intended for local/trusted network deployments only.

## Out of Scope

- Vulnerabilities in the AC Infinity cloud API itself
- Vulnerabilities in third-party MCP clients (Claude Desktop, Cursor, etc.)
- v2.0 Bluetooth/BLE features (not yet implemented)
