"""Capture the legacy Advance-Automation regression baseline for #326/#328.

Run this against `main` BEFORE the enum change, then assert equality on the branch.
Running it on the branch reproduces the branch and asserts nothing, so the committed
artifact is only ever regenerated from `main`.

Kept runnable on both: `_decode_rule` takes a required `controller_type` on the branch and
none on `main`, so the call below is written the branch's way and the `main` run needs the
keyword dropped. `tools/` is outside both the ruff and mypy gates, so nothing else catches
a signature drift here.

    python3 tools/capture_groups_golden.py

Writes tests/fixtures/captures/getgroups-legacy-<date>.json containing, per devCode:
  - devType
  - raw getGroups entries, with devId/userId redacted to match the anonymisation
    convention already used by the other files in that directory
  - the decoded output of every entry as produced by the code at capture time

Real advId/advName/port names are kept verbatim: they are what the assertions are
about, and the sibling Python fixtures already carry them.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from ac_infinity_mcp.automation import _decode_rule
from ac_infinity_mcp.client import ACInfinityClient
from ac_infinity_mcp.controller import detect_controller_type

_REDACT = ("devId", "userId", "appId")
LEGACY_DEVCODES = ("C58ZA", "8T4TC")


def _redact(entry: dict) -> dict:
    return {k: ("<redacted>" if k in _REDACT else v) for k, v in entry.items()}


def main() -> int:
    email, password = os.environ.get("AC_INFINITY_EMAIL"), os.environ.get("AC_INFINITY_PASSWORD")
    if not email or not password:
        print("AC_INFINITY_EMAIL / AC_INFINITY_PASSWORD required", file=sys.stderr)
        return 1

    client = ACInfinityClient(email, password)
    if not client.authenticate():
        print("authentication failed", file=sys.stderr)
        return 1

    devices = {d["devCode"]: d for d in client.get_devices()}
    out: dict[str, object] = {
        "captured_utc": datetime.now(UTC).isoformat(),
        "purpose": "Legacy Advance-Automation decode baseline for #326/#328. "
                   "Generated on main before the enum change.",
        "devices": {},
    }
    total = 0
    for code in LEGACY_DEVCODES:
        device = devices.get(code)
        if device is None:
            print(f"{code}: not on this account, skipping", file=sys.stderr)
            continue
        entries = client.get_advance_automations(device["devId"])
        out["devices"][code] = {  # type: ignore[index]
            "devType": device.get("devType"),
            "entries": [_redact(e) for e in entries],
            "decoded": [
                _decode_rule(e, controller_type=detect_controller_type(device))
                for e in entries
            ],
        }
        total += len(entries)
        print(f"{code}: devType={device.get('devType')} entries={len(entries)}")

    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    captures = Path(__file__).resolve().parents[1] / "tests/fixtures/captures"
    path = captures / f"getgroups-legacy-{stamp}.json"
    path.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {total} entries -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
