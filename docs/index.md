---
title: AC Infinity MCP
---

# AC Infinity MCP

Connect Claude to your AC Infinity controllers. Monitor live sensor data, run analytics, and control your grow environment through natural conversation — no app switching, no menus.

**[→ Getting Started Guide](GUIDE)**

---

## What it does

| Capability | Description |
|---|---|
| **Monitor** | Live sensor readings and multi-day historical data across all controllers and ports |
| **Understand** | Environment health scoring, trend detection, and port activity analysis |
| **Automate** | VPD, temperature, and humidity automation — set targets, let Claude manage the rest |
| **Configure** | Full port mode control across all 8 modes with preview before any change |
| **Grow** | One-command grow stage templates from seedling through late flower |

## Quick install

```bash
uvx ac-infinity-mcp
```

Add to your Claude Desktop `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ac-infinity": {
      "command": "uvx",
      "args": ["ac-infinity-mcp"],
      "env": {
        "AC_INFINITY_EMAIL": "your@email.com",
        "AC_INFINITY_PASSWORD": "yourpassword"
      }
    }
  }
}
```

See the [Deployment Guide](DEPLOYMENT) for Docker, HTTPS, and advanced configuration.

---

## Documentation

- [Getting Started Guide](GUIDE) — full walkthrough of every tool with real conversation examples
- [Deployment Guide](DEPLOYMENT) — Docker, environment variables, HTTPS reverse proxy
- [API Reference](API) — all 25 tools, quirks, and accepted-risk notes
- [Security Risks](SECURITY-RISKS) — known risks and mitigations

---

## Requirements

- Python 3.11+
- An [AC Infinity](https://acinfinity.com) account with registered controllers
- [Claude Desktop](https://claude.ai/download) or any MCP-compatible client
