<p align="center">
  <h1 align="center">ac-infinity-mcp</h1>
  <p align="center">
    Control and monitor your AC Infinity grow environment through Claude and any MCP-compatible AI assistant.
  </p>
  <p align="center">
    <a href="https://github.com/ober37/ac-infinity-mcp/actions/workflows/ci.yml"><img src="https://github.com/ober37/ac-infinity-mcp/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
    <a href="https://github.com/ober37/ac-infinity-mcp/blob/main/LICENSE"><img src="https://img.shields.io/github/license/ober37/ac-infinity-mcp" alt="License: MIT"></a>
    <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+"></a>
  </p>
</p>

---

## What is this?

**ac-infinity-mcp** is an [MCP server](https://modelcontextprotocol.io) that connects Claude (and other AI assistants) directly to your AC Infinity controllers — so you can read live sensor data, run analytics, and adjust fan speeds or port states using natural language, without opening the AC Infinity app.

> Built with [FastMCP](https://github.com/jlowin/fastmcp) and the AC Infinity cloud API.

## Example prompts

> *"What's the VPD trend in my tent over the last 7 days?"*
> *"Is my environment in the right range for late flower?"*
> *"Turn off port 3 on my 69 Pro controller — dry run first."*
> *"Show me which ports have been running the most this week."*
> *"Apply the veg stage template to port 1 — dry run first so I can see the settings."*
> *"My VPD is too high — what should I adjust?"*

## Compatible hardware

| Controller | Reads | Writes | Notes |
|---|---|---|---|
| UIS Controller 69 Pro | ✅ | ✅ | Legacy protocol |
| UIS Controller 69 Pro+ | ✅ | ✅ | Legacy protocol |
| UIS Controller 89 AI+ | ✅ | ✅ | New AI+ protocol — writes require the `minversion` header (Quirk 14) |

## Tools

| Category | Tool | What it does |
|---|---|---|
| 🔍 **Discovery** | `discover_devices` | List all your controllers and ports |
| 🌡️ **Readings** | `get_device_reading` | Live temp, humidity, VPD, and port states for one device |
| 🌡️ **Readings** | `get_all_device_readings` | Same, for all devices at once |
| 🌡️ **Readings** | `get_historical_readings` | Time-series data with optional bucketing (raw, 15m, 1h, 1d, …) |
| 📊 **Analytics** | `check_vpd_drift` | VPD target compliance check for your current grow stage |
| 📊 **Analytics** | `get_environment_health` | Composite health score (0–100, A–F) across temp, humidity, VPD |
| 📊 **Analytics** | `detect_environment_trends` | Linear trend + 7-day projection per metric |
| 📊 **Analytics** | `get_port_activity_report` | Per-port on/off hours, uptime %, and peak activity hour |
| 🔎 **Port Status** | `get_port_status` | Live port power level, load detection, active mode, and timer countdown |
| 🔎 **Port Status** | `get_port_settings` | Full automation config: mode, VPD target, temp/humidity thresholds, schedule, cycle |
| 🔌 **Write** | `set_port_speed` | Set fan or pump speed 1–10 (`dry_run=True` by default) |
| 🔌 **Write** | `set_port_on` | Turn a port fully on (`dry_run=True` by default) |
| 🔌 **Write** | `set_port_off` | Turn a port fully off (`dry_run=True` by default) |
| ⚙️ **Automation** | `set_vpd_automation` | Enable VPD mode with a kPa target using built-in sensors (`dry_run=True` by default) |
| ⚙️ **Automation** | `set_temperature_automation` | Enable AUTO mode with min/max °C thresholds (`dry_run=True` by default) |
| ⚙️ **Automation** | `set_humidity_automation` | Enable AUTO mode with min/max % RH thresholds (`dry_run=True` by default) |
| ⚙️ **Automation** | `set_port_mode` | Switch to any mode: OFF, ON, AUTO, VPD, CYCLE, SCHEDULE, TIMER_TO_ON, TIMER_TO_OFF (`dry_run=True` by default) |
| 🌱 **Intelligence** | `apply_grow_stage_template` | One-click VPD + temp + humidity automation for a named grow stage (`dry_run=True` by default) |
| 🤖 **Advance Automation** | `list_advance_automations` | List all named Advance Automation programs on a device |
| 🤖 **Advance Automation** | `get_advance_automation` | Get full detail (schedule, port groups, run state) for one automation |
| 🤖 **Advance Automation** | `enable_advance_automation` | Enable a disabled automation — reads state first, no-ops if already enabled (`dry_run=True` by default) |
| 🤖 **Advance Automation** | `disable_advance_automation` | Disable an enabled automation — reads state first, no-ops if already disabled (`dry_run=True` by default) |
| 🤖 **Advance Automation** | `create_advance_automation` | Create a new named automation; the first rule can be off, on, cycle, auto (temp/humidity), or VPD (`dry_run=True` by default) |
| 🤖 **Advance Automation** | `delete_advance_automation` | Delete an automation (disables first if active) (`dry_run=True` by default) |
| 🤖 **Advance Automation** | `add_automation_rule` | Add one rule (window + behavior) to an existing program — off, on, cycle, auto (temp/humidity), or VPD (`dry_run=True` by default) |
| 🤖 **Advance Automation** | `update_automation_rule` | Edit one rule in place — change its window, speed, mode, or sensor targets (`dry_run=True` by default) |
| 🤖 **Advance Automation** | `delete_automation_rule` | Remove a single rule from a program, leaving the rest intact (`dry_run=True` by default) |
| 🤖 **Advance Automation** | `break_out_of_automation` | Safely break a port out of automation control and lock co-governed ports to manual speed (`dry_run=True` by default) |

> ✦ All write tools (Write, Automation, and Intelligence categories) default to `dry_run=True` — they return the exact payload they *would* send without making any changes to your equipment. Pass `dry_run=False` only when you're ready to execute.

> 📖 For a complete grower's guide with conversation examples for every tool, see [docs/GUIDE.md](docs/GUIDE.md).

## MCP Prompts

Three built-in prompts are registered alongside the tools. In Claude Desktop and other MCP clients, these appear as slash commands or prompt suggestions.

| Prompt | What it does |
|---|---|
| `vpd_troubleshooting` | Step-by-step guide: diagnose HIGH or LOW VPD and which tools to call to fix it |
| `new_grower_setup` | Onboarding walkthrough: discover devices → apply a stage template → check your health score |
| `environment_alert_interpretation` | How to read `check_vpd_drift` status values and `get_environment_health` grades (A–F, score weighting, what to do) |

## Quick start

### Option 1: pip (simplest)

```bash
pip install git+https://github.com/ober37/ac-infinity-mcp.git
```

Requires Python 3.11+.

### Option 2: isolated venv (recommended)

```bash
python3 -m venv ~/.venvs/ac-infinity-mcp
source ~/.venvs/ac-infinity-mcp/bin/activate
pip install git+https://github.com/ober37/ac-infinity-mcp.git
which ac-infinity-mcp   # copy this path — you'll need it below
```

### Option 3: uvx (no install)

```bash
uvx --from git+https://github.com/ober37/ac-infinity-mcp.git ac-infinity-mcp
```

## MCP client configuration

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or
`%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "ac-infinity": {
      "command": "/path/to/ac-infinity-mcp",
      "env": {
        "AC_INFINITY_EMAIL": "you@example.com",
        "AC_INFINITY_PASSWORD": "yourpassword"
      }
    }
  }
}
```

Replace `/path/to/ac-infinity-mcp` with the path printed by `which ac-infinity-mcp`.

### Cursor / Windsurf

Add to your MCP settings:

```json
{
  "ac-infinity": {
    "command": "/path/to/ac-infinity-mcp",
    "env": {
      "AC_INFINITY_EMAIL": "you@example.com",
      "AC_INFINITY_PASSWORD": "yourpassword"
    }
  }
}
```

## Docker

```bash
cp .env.example .env
# fill in AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD
docker compose up --build
```

The container runs over stdio. Connect via a stdio bridge such as [mcp-proxy](https://github.com/sparfenyuk/mcp-proxy).

> Credentials are injected at runtime via `env_file` — never baked into the image.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `AC_INFINITY_EMAIL` | Yes | Your AC Infinity account email |
| `AC_INFINITY_PASSWORD` | Yes | Your AC Infinity account password |

> ⚠️ **HTTP-only upstream:** The AC Infinity cloud API does not support HTTPS. Credentials and sensor data traverse the network in plain text. This is an upstream limitation — see [docs/API.md](docs/API.md) Quirk 8. Avoid running this server on untrusted networks.

## Development

```bash
git clone https://github.com/ober37/ac-infinity-mcp.git
cd ac-infinity-mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in credentials

# Lint + type-check
ruff check src/ tests/
mypy src/ac_infinity_mcp/

# Tests (unit + integration)
pytest tests/ -v

# Security audit
pip-audit
```

## Security

Vulnerabilities should be reported via
[GitHub Private Vulnerability Reporting](https://github.com/ober37/ac-infinity-mcp/security/advisories/new).
See [SECURITY.md](SECURITY.md) for the full disclosure policy, scope, and known
limitations (including the HTTP-only upstream API). Accepted dependency CVEs
are tracked in [docs/SECURITY-RISKS.md](docs/SECURITY-RISKS.md).

## License

MIT
