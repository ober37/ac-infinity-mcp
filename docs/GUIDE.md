# AC Infinity MCP — Grower's Guide

A complete walkthrough of every tool, with real conversation examples.

---

## Table of Contents

1. [Introduction](#1-introduction)
2. [Prerequisites & First Steps](#2-prerequisites--first-steps)
3. [Discovering Your Controllers](#3-discovering-your-controllers)
4. [Monitoring Your Environment](#4-monitoring-your-environment)
5. [Analytics & Health](#5-analytics--health)
6. [Port Status & Settings](#6-port-status--settings)
7. [Preview Before You Change Anything](#7-preview-before-you-change-anything)
8. [Single-Port Automations](#8-single-port-automations)
9. [Advance Automations](#9-advance-automations)
10. [Built-in Prompts](#10-built-in-prompts)
11. [Troubleshooting](#11-troubleshooting)
12. [Appendix: All 28 Tools at a Glance](#12-appendix-all-28-tools-at-a-glance)

---

## 1. Introduction

**ac-infinity-mcp** connects Claude to your AC Infinity controllers. Once set up, you can read live sensor data, run analytics, and control your ports through natural conversation — no app switching, no menus.

| Capability | What it gives you |
|---|---|
| **Monitor** | Live sensor readings and multi-day historical data across all registered controllers and ports |
| **Understand** | Environment health scoring with letter grades, trend detection across day/night cycles, port activity analysis |
| **Automate** | Set VPD, temperature, and humidity automation targets; control fan speed, on/off state, and scheduled run windows |
| **Configure** | Full port mode control across all 8 modes with mode-specific parameters; read current automation settings and schedules |
| **Grow** | One-command grow stage templates from seedling through late flower; VPD drift diagnosis with corrective guidance |
| **Learn** | Three guided prompts for environment alert interpretation, stage setup, and VPD troubleshooting |

### What this integration makes possible

AC Infinity controllers already measure, automate, and respond. What they don't do is explain, advise, or anticipate. This integration closes that gap — and because Claude can combine this MCP with other connected tools, some workflows aren't possible through the app at all:

| You say… | What Claude does |
|---|---|
| "How did my tent do overnight?" | Pulls sensor history, compares against stage targets, surfaces what drifted and what needs attention — without opening the app |
| "What would setting fan speed to 8 actually do?" | Returns a full preview showing exactly what would change — device untouched until you confirm |
| "Here are this week's canopy shots — based on how the environment has trended, what would you change?" | Can correlate visual plant indicators with the past week of VPD and temperature data to suggest specific adjustments *(requires image input support in your Claude client)* |
| "We're flipping to 12/12 today" | Can apply the early-flower automation template to your controller in the same step as updating a grow log or drafting a community post *(requires additional MCP tools for calendar/notes)* |

### The safety model

Every write operation — changing a speed, toggling a port, creating an automation — shows you exactly what it will do before doing it. This is called **preview mode**, and it is on by default. Claude will describe the change, tell you what it's changing from, and wait for you to say "yes" (or anything equivalent) before anything reaches your equipment.

### Compatible hardware

| Controller | Reading | Writing |
|---|---|---|
| UIS Controller 69 Pro | ✅ | ✅ |
| UIS Controller 69 Pro+ | ✅ | ✅ |
| UIS Controller 89 AI+ and other AI+ controllers | ✅ | ✅ |

### A note on network security

The AC Infinity API does not use HTTPS. This means your credentials and sensor data travel over the network in plain text. Keep your config file private and avoid running this setup on public or untrusted Wi-Fi networks. This is an upstream limitation of the AC Infinity cloud service — not something this server can change.

---

## 2. Prerequisites & First Steps

### Installation

See [README.md](../README.md) for full installation and setup instructions. The short version is that you install the server once and then point Claude Desktop at it with a config file.

### Configuring your credentials

Edit your Claude Desktop config file to add your AC Infinity credentials as environment variables:

- `AC_INFINITY_EMAIL` — your AC Infinity account email address
- `AC_INFINITY_PASSWORD` — your AC Infinity account password

**Option A — uvx (no separate install step):**

```json
{
  "mcpServers": {
    "ac-infinity": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/ober37/ac-infinity-mcp.git", "ac-infinity-mcp"],
      "env": {
        "AC_INFINITY_EMAIL": "you@example.com",
        "AC_INFINITY_PASSWORD": "yourpassword"
      }
    }
  }
}
```

**Option B — pip install (use the path from `which ac-infinity-mcp`):**

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

**Important:** Your credentials go in the config file only — never type your password into the Claude chat window. Keep the config file private, as it contains your account password in plain text.

For full setup instructions including venv isolation and Docker options, see [README.md](../README.md).

---

## 3. Discovering Your Controllers

The first thing to do is ask Claude to find your controllers. Claude uses the names you've given your devices in the AC Infinity app — you never need to remember device IDs or numbers.

> **User:** What AC Infinity controllers do I have?
>
> **Claude:** You have three controllers:
> - Flower Tent — online
> - Veg Tent — online
> - Propagation Room — online
>
> Which one would you like to start with?

Claude will list every controller associated with your account, along with each device's ports and what's plugged into them.

**If the list comes back empty:** Check that your `AC_INFINITY_EMAIL` and `AC_INFINITY_PASSWORD` values in the config file are correct, and confirm your devices are paired in the AC Infinity app.

---

## 4. Monitoring Your Environment

### Live readings

Ask for the current state of any controller at any time. You'll get temperature, humidity, VPD, and the status of every port.

> **User:** What's going on in Veg Tent right now?
>
> **Claude:** Here's Veg Tent (2:02 PM CDT):
>
> Temperature: 60.6°F | Humidity: 58.7% | VPD: 0.74 kPa
>
> Ports running:
> - Filter (Port 4): speed 1
> - Left Fan (Port 5): speed 2
> - Right Fan (Port 6): speed 2
>
> Ports off:
> - Humidifier (Port 1): off
> - Heater (Port 2): off
> - Light (Port 3): off
>
> Unused ports:
> - Port 7: not powered
> - Port 8: not powered

You can also ask for all devices at once:

> **User:** Give me a snapshot of all my tents.
>
> **Claude:** Here's a summary across all three controllers... [Claude lists each device in turn]

### Understanding port states

Each port falls into one of three states:

- **Running** — the device connected to this port is actively powered and running at a speed above zero.
- **Off** — something is plugged into this port, but it's not currently running.
- **Not powered** — the port doesn't appear to have anything drawing power. This label only shows on ports that still have their default name (like "Port 7" or "Port 8"). If you've given a port a custom name, it will just show as off — even if nothing is plugged in.

### Historical data

Ask for environmental history over any date range. You can get raw readings, or ask Claude to bucket them by 15-minute, 1-hour, or 1-day averages.

> **User:** How has my VPD been in Veg Tent this past week?
>
> **Claude:** Here's the VPD trend for Veg Tent over the last 7 days (hourly averages):
>
> VPD averaged 0.82 kPa, trending up from 0.71 kPa at the start of the week to 0.94 kPa by the end. That's on the low end of the veg range — still within acceptable bounds, but worth watching if you're pushing into early flower.

---

## 5. Analytics & Health

Two of these tools check your current live readings against stage targets. The other two analyze history over up to 30 days.

### VPD drift check

Get a quick yes/no on whether your VPD is on track for a specific grow stage.

> **User:** Is my VPD on track for mid-flower?
>
> **Claude:** Veg Tent VPD is currently 0.74 kPa — that's LOW for mid-flower (target range: 1.2–2.0 kPa). You'll want to raise temperature or lower humidity to bring it up.

Status values are: **OK**, **HIGH**, or **LOW**.

### Environment health score

Get a composite 0–100 health score and letter grade for your grow environment.

Score breakdown:
- VPD accounts for 40% of the score
- Temperature accounts for 30%
- Humidity accounts for 30%

| Grade | Score | Meaning |
|---|---|---|
| A | 90–100 | Excellent |
| B | 80–89 | Good |
| C | 70–79 | Fair |
| D | 60–69 | Poor |
| F | 0–59 | Critical |

> **User:** How is Veg Tent doing overall?
>
> **Claude:** Veg Tent health score: 83/100 — Grade B (Good).
>
> - VPD: 0.74 kPa — slightly low for current stage (score: 72/100)
> - Temperature: 60.6°F — within target range (score: 91/100)
> - Humidity: 58.7% — within target range (score: 88/100)
>
> Top recommendation: nudge VPD up toward 0.9 kPa by raising temperature 2–3°F or reducing humidity slightly.

### Environment trends

Analyzes up to 30 days of history and projects where your environment is heading over the next 7 days.

> **User:** What direction is my Flower Tent environment heading?
>
> **Claude:** Over the past 7 days in Flower Tent: temperature is trending up 0.8°F/day (projected to hit 74°F by end of week), humidity is holding steady at 52%, and VPD is tracking up from 1.0 to a projected 1.3 kPa — right on target for mid-flower if that trend holds.

### Port activity report

See how much each port has been running — total hours on, uptime percentage, and which hour of the day it tends to be most active.

> **User:** Show me which ports have been running the most this week in Veg Tent.
>
> **Claude:** Port activity for Veg Tent, last 7 days:
>
> | Port | Hours On | Uptime | Peak Hour |
> |---|---|---|---|
> | Filter (Port 4) | 168 hrs | 100% | All day |
> | Left Fan (Port 5) | 142 hrs | 85% | 2 PM |
> | Right Fan (Port 6) | 139 hrs | 83% | 2 PM |
> | Light (Port 3) | 84 hrs | 50% | 12 PM |
> | Humidifier (Port 1) | 22 hrs | 13% | 6 AM |

---

## 6. Port Status & Settings

### Live port status

Ask about the current operating state of any specific port — what mode it's in, what speed it's running at, and whether any automation is active.

> **User:** What mode is my Filter running in?
>
> **Claude:** Filter (Port 4) on Veg Tent is running in Automation mode, governed by the "Moderate Airflow" program at target speed 5.

**A note on automation mode:** When a port is under an Advance Automation, the live status shows the mode as "Automation" — and if you ask for the port's full settings, you'll see it listed as "ADVANCE." Both mean exactly the same thing: the port is controlled by a named automation program.

The automation name appears when it was successfully retrieved from your controller. If the name is absent, the port may still be under automation control — the controller just wasn't able to return the name at that moment.

Note: this flag only appears on ports that still have their default name (like "Port 7" or "Port 8") when reading device-level data. If you've given a port a custom name, it will show as off regardless — a custom name implies a device was intentionally connected. Single-port reads apply the same rule.

### Full port settings

Ask for the complete configured settings on any port — mode, VPD target, temperature and humidity thresholds, schedule, and cycle settings.

> **User:** What are the full settings on Port 1?
>
> **Claude:** Humidifier (Port 1) on Veg Tent:
> - Mode: AUTO
> - Humidity range: 50–60% RH (fan runs when humidity drops below 50%)
> - Temperature range: 65–78°F
> - Current speed: 4

**Stale settings note:** If a port doesn't have anything connected to it, the settings shown may be leftover from a previous configuration. This is normal — the controller stores the last-used settings even when nothing is plugged in.

---

## 7. Preview Before You Change Anything

All write operations — changing speeds, toggling ports, creating automations — default to showing you exactly what will happen before making any change. This is preview mode.

Claude will describe the change, tell you what it's changing from, and wait for your confirmation. Only after you say "yes" (or "go ahead," "do it," or anything equivalent) will the change be sent to your controller.

### Setting a port speed

> **User:** Turn Filter on Veg Tent down to speed 3.
>
> **Claude:** Here's the plan — previewing before any changes:
>
> Set Filter (Port 4) on Veg Tent from speed 1 → speed 3.
>
> Ready to apply this?
>
> ---
>
> **User:** Yes, go ahead.
>
> **Claude:** Done — Filter (Port 4) on Veg Tent is now running at speed 3.

### Turning a port on

> **User:** Turn on my Humidifier.
>
> **Claude:** Previewing: turn Humidifier (Port 1) on Veg Tent on at full speed.
>
> Ready to apply?
>
> ---
>
> **User:** Yes.
>
> **Claude:** Done — Humidifier (Port 1) is now on.

### Turning a port off

> **User:** Turn off the Heater in Veg Tent.
>
> **Claude:** Previewing: turn Heater (Port 2) on Veg Tent off.
>
> Ready to apply?
>
> ---
>
> **User:** Yes.
>
> **Claude:** Done — Heater (Port 2) is now off.

### AI+ controllers

If you have an AI+ controller (like the 89 AI+), reads, previews and live changes all work — the same as on the 69 Pro. AI+ controllers speak a newer protocol, which this server handles for you.

---

## 8. Single-Port Automations

These tools set automation modes directly on a single port.

### VPD automation

Sets the port to VPD mode with a specific kPa target. The controller uses its built-in temperature and humidity sensors to maintain your target.

> **User:** Set Veg Tent Filter to VPD mode targeting 1.2 kPa.
>
> **Claude:** Previewing: set Filter (Port 4) on Veg Tent to VPD mode, target 1.2 kPa.
>
> Ready to apply?
>
> ---
>
> **User:** Yes.
>
> **Claude:** Done — Filter (Port 4) is now running in VPD mode targeting 1.2 kPa.

### Temperature automation

Sets the port to AUTO mode with minimum and maximum temperature thresholds.

**Important:** Check which unit your controller uses before setting thresholds — look at any device reading and it will show °F or °C. Use the same unit when you ask Claude to set thresholds.

> **User:** Set the Exhaust Fan to turn on when it gets above 80°F and off below 72°F.
>
> **Claude:** Previewing: set Exhaust Fan (Port 3) on Flower Tent to AUTO mode — on above 80°F, off below 72°F.
>
> Ready to apply?

### Humidity automation

Sets the port to AUTO mode with minimum and maximum relative humidity thresholds.

> **User:** Set the Humidifier to run when humidity drops below 50% and stop at 60%.
>
> **Claude:** Previewing: set Humidifier (Port 1) on Veg Tent to AUTO mode — on below 50% RH, off above 60% RH.
>
> Ready to apply?

### Switching port mode

Switch any port to any mode directly: OFF, ON, AUTO, VPD, CYCLE, SCHEDULE, TIMER_TO_ON, or TIMER_TO_OFF.

> **User:** Put Port 6 into cycle mode.
>
> **Claude:** Previewing: set Right Fan (Port 6) on Veg Tent to CYCLE mode.
>
> Ready to apply?

### Grow stage templates

One-click setup for a named grow stage. Available stages: **clones**, **seedling**, **veg**, **early_flower**, **mid_flower**, **late_flower**.

> **User:** Set up Veg Tent Filter for mid-flower.
>
> **Claude:** Here's the mid-flower template for Filter (Port 4) — previewing before making changes:
>
> - Mode: VPD (this will be active immediately)
> - VPD target: 1.6 kPa
> - Temperature range saved: 64–77°F (stored for AUTO mode — not active yet)
> - Humidity range saved: 45–55% RH (stored for AUTO mode — not active yet)
>
> Apply this to Filter (Port 4) on Veg Tent?
>
> ---
>
> **User:** Yes.
>
> **Claude:** Done — Filter (Port 4) on Veg Tent is now running in VPD mode targeting 1.6 kPa for mid-flower.

**Important:** When you apply a grow stage template, the port is set to VPD mode using the stage's target — that's what runs immediately. The temperature and humidity thresholds for that stage are also saved on your controller, but they won't be active until you switch the port to AUTO mode. Only VPD is running right away.

---

## 9. Advance Automations

Advance Automations are named programs that can control multiple ports on a schedule, with speed settings, day/night cycles, and threshold triggers. They're created and managed in the AC Infinity app — and now through Claude.

### Viewing and managing automations

**List all automations:**

> **User:** What automations do I have on Veg Tent?
>
> **Claude:** Veg Tent has 2 Advance Automations:
> - "Moderate Airflow" — enabled, controlling Filter (Port 4) and Left Fan (Port 5)
> - "Night Cycle" — disabled, controlling Left Fan (Port 5) and Right Fan (Port 6)

**Get full detail on one automation:**

> **User:** Tell me everything about the "Moderate Airflow" automation.
>
> **Claude:** "Moderate Airflow" on Veg Tent:
> - Status: enabled
> - Ports: Filter (Port 4) at speed 5, Left Fan (Port 5) at speed 4
> - Schedule: runs all 7 days
> - Trigger: temperature above 75°F

**Enable or disable an automation:**

> **User:** Enable the "Night Cycle" automation.
>
> **Claude:** Previewing: enable "Night Cycle" on Veg Tent.
>
> Ready to apply?

> **User:** Disable "Moderate Airflow."
>
> **Claude:** Previewing: disable "Moderate Airflow" on Veg Tent.
>
> Ready to apply?

**Delete an automation:**

> **User:** Delete the "Night Cycle" automation.
>
> **Claude:** Previewing: delete "Night Cycle" on Veg Tent. This will also disable it first if it's currently active.
>
> Ready to apply?

### Creating a new automation

Automations are created in preview mode — Claude shows you exactly what will be created before sending anything to your controller. A new automation is created as enabled — it will start controlling the ports you specify as soon as it is created.

> **User:** Create a new automation called "Flower Push" that runs the Filter at speed 8 and both fans at speed 6, all week long.
>
> **Claude:** Here's the "Flower Push" automation — previewing before creating:
>
> - Name: Flower Push
> - Filter (Port 4): speed 8
> - Left Fan (Port 5): speed 6
> - Right Fan (Port 6): speed 6
> - Schedule: all 7 days
> - Status: enabled (this will start controlling these ports once created)
>
> Ready to create this?
>
> ---
>
> **User:** Yes.
>
> **Claude:** Done — "Flower Push" is now active on Veg Tent. Filter (Port 4), Left Fan (Port 5), and Right Fan (Port 6) are now under its control.

### Releasing a port from automation control

The `break_out_of_automation` feature releases a port from automation control and locks it to manual speed.

> **IMPORTANT — Scope warning:** If your controller has more than one Advance Automation running, releasing a port affects all ADVANCE-mode ports on that device — not just the ports in the automation you're targeting. Every port currently under any Advance Automation on that controller will also be released and locked to its current speed. This affects your whole controller, not just one automation.

> **User:** Release the Left Fan from the "Night Cycle" automation.
>
> **Claude:** Previewing: release Left Fan (Port 5) from automation control. Note: this will also lock any other ports currently under an Advance Automation on Veg Tent to their current speeds.
>
> Ready to apply?
>
> ---
>
> **User:** Yes.
>
> **Claude:** Done — Left Fan (Port 5) is now in manual mode at its current speed.

---

### Taking back manual control

When you try to change a port that's under an active Advance Automation, Claude won't just override it silently. Instead, it will show you your options. Here's what to expect across the four situations you might encounter.

---

**Scenario A — You asked to set a specific speed (e.g., "set fan to speed 5")**

Three options appear:

> **Claude:** Left Fan (Port 5) is controlled by "Night Cycle" (target speed: 3). Here are your options:
>
> 1. **Update the automation** — change "Night Cycle" to run at speed 5. The automation stays active.
> 2. **Release Left Fan** — frees this port (and all other ADVANCE-mode ports on the device) from automation control so you can set it manually.
> 3. **Disable "Night Cycle" entirely** — turns off the whole automation program.
>
> Which would you like?

**Follow-through for Option 1 — update the speed:**

> **User:** Update the automation to speed 5.
>
> **Claude:** Previewing: update "Night Cycle" to run Left Fan (Port 5) at speed 5 (currently 3). Ready to apply?
>
> ---
>
> **User:** Yes.
>
> **Claude:** Done — "Night Cycle" will now run Left Fan (Port 5) at speed 5 when active.

**Follow-through for Option 2 — release the port:**

> **User:** Release Left Fan from the automation.
>
> **Claude:** Previewing: release Left Fan (Port 5) from "Night Cycle." Note: this will also lock any other ports currently under an Advance Automation on this device to their current speeds.
>
> Ready to apply?
>
> ---
>
> **User:** Yes.
>
> **Claude:** Done — Left Fan (Port 5) is now in manual mode at its current speed.

**Follow-through for Option 3 — disable the automation:**

> **User:** Disable "Night Cycle."
>
> **Claude:** Previewing: disable "Night Cycle" on Veg Tent.
>
> Ready to apply?
>
> ---
>
> **User:** Yes.
>
> **Claude:** Done — "Night Cycle" is disabled. Left Fan (Port 5) and any other ports it controlled are no longer under automation.

---

**Scenario B — You asked to turn a port on or off (no specific speed)**

Two options appear (there's no speed to update, so Option 1 from above doesn't apply):

> **Claude:** Filter (Port 4) is controlled by "Moderate Airflow" (currently running at speed 5). I can't turn it off while the automation is running. Here are your options:
>
> 1. **Release Filter** — breaks it free from automation control. This affects all ADVANCE-mode ports on this device.
> 2. **Disable "Moderate Airflow" entirely** — turns off the whole automation program.
>
> Which would you like?

---

**Scenario C — Port is stuck but all automations are already off**

Sometimes a port gets left in automation mode after its automation is disabled. Claude handles this:

> **Claude:** Exhaust Fan (Port 2) is stuck in automation mode, but all automations on this device appear to be turned off. This can happen when an automation was disabled without fully releasing the port.
>
> Previewing: force-release Exhaust Fan (Port 2) from automation control so you can set it manually. Ready?
>
> ---
>
> **User:** Yes.
>
> **Claude:** Done — Exhaust Fan (Port 2) is now in manual mode.

---

**Scenario D — Automation details couldn't be retrieved**

Occasionally the automation list can't be fetched from the controller:

> **Claude:** I couldn't retrieve the automation list for this device. Ask me to list your automations — that will let me identify which one is active and what your options are.

---

## 10. Built-in Prompts

The server comes with three built-in prompts — guides that Claude follows when you ask for them. In Claude Desktop, you can invoke them by name or by describing what you want. For example: "Use the VPD troubleshooting guide" or "Walk me through the new grower setup."

When Claude follows a prompt, it responds in plain language — you shouldn't see any code or technical terms in what it says to you.

**Tip:** If anything in Claude's response looks out of place while following a prompt, just say "continue in plain language" and it will carry on naturally.

---

### vpd_troubleshooting

A step-by-step VPD diagnosis guide. Claude checks your current readings, identifies whether VPD is high or low for your grow stage, and walks you through which adjustments to make.

> **User:** My VPD is too high — help me fix it.
>
> **Claude:** Let me check your current environment first...
>
> Veg Tent is at 68°F / 45% RH — VPD is 1.38 kPa. For mid-flower your target is 1.2–2.0 kPa, so you're actually within range, just at the low-mid end. If you're seeing tip burn or stress signs, you could try nudging humidity down to 42% to bring VPD up toward 1.6 kPa. Want me to adjust the Humidifier settings?

---

### new_grower_setup

An onboarding walkthrough from first connection to a working automated environment. Takes you through: discover your devices → check live readings → apply a grow stage template → verify your health score.

> **User:** I just set this up — walk me through getting started.
>
> **Claude:** Great, let's get your grow dialled in. First, let me find your controllers...
>
> I found two: "Veg Tent" and "Flower Tent." Which one should we set up first?
>
> [Claude then walks through checking readings, choosing a grow stage, applying the template, and confirming the health score — all in plain language]

---

### environment_alert_interpretation

Explains what health grades and VPD drift status values mean and what to do about each one.

> **User:** My health score just dropped to a C — what does that mean?
>
> **Claude:** A C (70–79) means your environment is fair — growth is continuing, but one or more metrics are outside their ideal range. It won't cause immediate harm, but you'll want to identify which metric is pulling the score down and address it before it drifts further.
>
> Ask me for a full health report and I'll show you exactly which metric is off and what to do about it.

---

## 11. Troubleshooting

**Port shows "not powered"**
This is normal for unused ports that still have their default name (Port 7, Port 8, etc.). If you've renamed a port and it shows as "off," that's also normal — it just means the device connected to it isn't currently running.

**Port settings look stale or wrong**
If a port doesn't have anything plugged in, the settings shown may be from a previous use. This is normal — the controller stores the last-used configuration even when nothing is connected.

**Authentication errors**
Check the `AC_INFINITY_EMAIL` and `AC_INFINITY_PASSWORD` values in your config file. Make sure there are no extra spaces or typos. Never re-enter credentials in the chat window.

**No controllers found**
Verify your credentials are correct and check that your devices are paired in the AC Infinity app. If you just added a new device, try asking again — it can take a moment to appear.

**AI+ write errors**
AI+ controllers (like the 89 AI+) support live changes. If a write fails with code `100001`, AC Infinity has changed the server-side gate this server relies on — reads and previews keep working. Please open an issue if you hit it.

**HTTP security note**
The AC Infinity API doesn't use HTTPS. Your credentials and sensor data travel over the network without encryption. Keep your config file private and avoid running on untrusted networks. This is an upstream limitation of the AC Infinity service. If you need to run on a less-trusted network, see DEPLOYMENT.md for HTTPS reverse-proxy options.

**Port stuck in automation mode**
See [Taking Back Manual Control](#taking-back-manual-control) in Section 9.

---

## 12. Appendix: All 28 Tools at a Glance

| Tool | Category | Preview mode | What it does |
|---|---|---|---|
| `discover_devices` | Discovery | N/A | List all controllers and ports on your account |
| `get_device_reading` | Readings | N/A | Live temp, humidity, VPD, and port states for one device |
| `get_all_device_readings` | Readings | N/A | Same as above, for all devices at once |
| `get_historical_readings` | Readings | N/A | Time-series data for any date range, with optional bucketing |
| `check_vpd_drift` | Analytics | N/A | Quick VPD compliance check against a named grow stage (OK / HIGH / LOW) |
| `get_environment_health` | Analytics | N/A | Composite 0–100 health score with letter grade and per-metric breakdown |
| `detect_environment_trends` | Analytics | N/A | Trend analysis across up to 30 days, with 7-day projection |
| `get_port_activity_report` | Analytics | N/A | Per-port hours on, uptime %, and peak activity hour over 1–30 days |
| `get_port_status` | Port Status | N/A | Live operating state — mode, speed, automation name, timer countdown |
| `get_port_settings` | Port Status | N/A | Full configured settings — mode, VPD target, thresholds, schedule |
| `set_port_speed` | Write | Yes — on by default | Set a port to a specific speed (1–10) |
| `set_port_on` | Write | Yes — on by default | Turn a port fully on |
| `set_port_off` | Write | Yes — on by default | Turn a port fully off |
| `set_vpd_automation` | Automation | Yes — on by default | Enable VPD mode with a kPa target |
| `set_temperature_automation` | Automation | Yes — on by default | Enable AUTO mode with min/max temperature thresholds |
| `set_humidity_automation` | Automation | Yes — on by default | Enable AUTO mode with min/max % RH thresholds |
| `set_port_mode` | Automation | Yes — on by default | Switch to any mode: OFF, ON, AUTO, VPD, CYCLE, SCHEDULE, TIMER_TO_ON, TIMER_TO_OFF |
| `apply_grow_stage_template` | Intelligence | Yes — on by default | One-click VPD + temp + humidity setup for a named grow stage |
| `list_advance_automations` | Advance Automation | N/A | List all named Advance Automation programs on a device |
| `get_advance_automation` | Advance Automation | N/A | Full detail for one automation — schedule, ports, run state |
| `enable_advance_automation` | Advance Automation | Yes — on by default | Enable a disabled automation |
| `disable_advance_automation` | Advance Automation | Yes — on by default | Disable an enabled automation |
| `create_advance_automation` | Advance Automation | Yes — on by default | Create a new named automation; the first rule can be on, cycle, temperature, humidity, or VPD |
| `delete_advance_automation` | Advance Automation | Yes — on by default | Delete an automation (disables it first if active) |
| `add_automation_rule` | Advance Automation | Yes — on by default | Add one rule (window + behavior) to an existing program |
| `update_automation_rule` | Advance Automation | Yes — on by default | Edit one rule in place — change its window, speed, mode, or sensor targets |
| `delete_automation_rule` | Advance Automation | Yes — on by default | Remove a single rule from a program, leaving the rest intact |
| `break_out_of_automation` | Advance Automation | Yes — on by default | Release a port from automation control and lock co-governed ports to manual speed |
