"""Real Controller 89 AI+ (devType=20) API captures, pinned as test fixtures.

Source: two passive shape-sweep snapshots of the same physical Controller 89 AI+,
captured ~25 days apart and published by mifshub.com. The capture payloads are real
AC Infinity cloud-API responses (``getDevSetting`` + ``getdevModeSettingList`` per port),
PII-scrubbed (user id / serial replaced with ``<id-N>`` placeholders) before persistence.
The captures themselves are released into the public domain by the publisher; only the
publisher's accompanying README narrative is MIT.

These verbatim captures are what mock-only tests cannot provide: they catch real
field-shape regressions and firmware drift on a controller model we cannot test against
directly. See ``.claude/internal/CONTROLLER_89_AIPLUS_RESEARCH.md`` for the field analysis.
"""

import json
import pathlib

_CAPTURE_DIR = pathlib.Path(__file__).parent

# Available capture dates (same controller, physically reconfigured between snapshots).
CAPTURE_DATES = ("2026-05-04", "2026-05-29")


def load_89_aiplus_capture(date: str = "2026-05-29") -> dict:
    """Load a pinned Controller 89 AI+ capture by date (see CAPTURE_DATES)."""
    path = _CAPTURE_DIR / f"controller-89-aiplus-{date}.json"
    return json.loads(path.read_text())
