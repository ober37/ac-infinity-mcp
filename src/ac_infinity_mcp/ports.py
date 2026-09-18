from typing import Literal

from ac_infinity_mcp.analytics import _ZERO_LOAD_DEV_TYPES

# portResistance == 65535 (0xFFFF) is the hardware open-circuit sentinel: nothing connected.
# The controller measures electrical resistance across each port; connected devices present
# real values (e.g. 400Ω light, 7500Ω fan, 15800Ω heater). Confirmed via ProxyMan 2026-05-26.
_PORT_EMPTY_RESISTANCE: int = 65535

# Both call sites compare against these string literals, so a typo currently
# type-checks cleanly and disables the branch for good. Naming the type makes
# mypy catch that.
PortEmptyConfidence = Literal["sentinel", "heuristic", "no"]


def _is_port_empty(port_data: dict | None, port: int, device: dict | None) -> bool:
    """Return True when nothing is physically connected to this port.

    Primary signal (Quirk 27): ``portResistance == 65535`` (0xFFFF) in
    ``devInfoListAll.deviceInfo.ports``. The controller measures electrical resistance
    across each port; 65535 is the maximum uint16 value indicating open circuit (nothing
    connected). Connected devices — even in OFF mode — present real values (e.g. 400Ω
    light, 7500Ω fan, 15800Ω heater). When ``portResistance`` is present and is not
    65535, the port is NOT empty regardless of port name or ``portsLoad``.

    Fallback (Quirk 26): when ``portResistance`` is absent (old firmware), the existing
    dual-signal heuristic applies — default name ``"Port N"`` AND (``portsLoad == 0`` OR
    ``devType in _ZERO_LOAD_DEV_TYPES``). Custom-named ports in the fallback path are
    assumed connected.

    Known tradeoff (user-approved 2026-05-26): LED grow lights with their own power
    switches may read ``portResistance=65535`` when that switch is off but the device is
    still physically plugged in. Passive loads (heaters, fans with AC motors) are not
    affected — their resistance is measurable regardless of a device-level switch.

    Returns False when ``port_data`` is None (port not found) or ``device`` is None.

    Derived from :func:`_port_empty_confidence` rather than deciding the same fact
    a second time (Issue #352).
    """
    return _port_empty_confidence(port_data, port, device) != "no"


def _empty_port_advisory(port_label: str) -> str:
    """Return the grower-friendly advisory text for an empty port."""
    return (
        f"{port_label} doesn't appear to have anything connected. "
        "If you meant a different port, let me know which one."
    )


def _port_empty_confidence(
    port_data: dict | None, port: int, device: dict | None
) -> PortEmptyConfidence:
    """Return "sentinel", "heuristic" or "no" for the empty-port signal.

    This is where the empty-port question is decided; ``_is_port_empty`` is this
    answer collapsed to a bool. The two used to compare ``portResistance``
    differently — one coerced with ``int()``, one compared raw — so a stringified
    ``"65535"`` was a sentinel to one and a heuristic to the other (Issue #352).
    That mattered because the heuristic wording tells the grower the port is
    "default-named and drawing no load", and via that path neither clause had been
    evaluated.

    The two signals it separates:

    - ``"sentinel"`` — ``portResistance == 65535``, the hardware open-circuit value
      (Quirk 27). Direct evidence.
    - ``"heuristic"`` — ``portResistance`` absent, so the old-firmware fallback fired:
      a default port name AND (zero load OR a devType known to report zero load). That
      matches plenty of ports that do have equipment attached — notably any
      default-named port sitting at zero load.

    Only "sentinel" is strong enough to redirect a grower away from an automation
    conflict; "heuristic" may be added as an advisory alongside one.

    Returns "no" when ``port_data`` is None (port not found) or ``device`` is None.
    """
    if port_data is None or device is None:
        return "no"

    port_resistance = port_data.get("portResistance")
    if port_resistance is not None:
        # Coerced, because this API sends numbers as strings elsewhere (devId per
        # Quirk 7, devType as "20").
        try:
            coerced = int(port_resistance)
        except (ValueError, TypeError, OverflowError):
            return "no"  # treat as connected on malformed API data
        return "sentinel" if coerced == _PORT_EMPTY_RESISTANCE else "no"

    # Fallback for firmware that omits portResistance: preserve dual-signal heuristic.
    port_name = port_data.get("portName", f"Port {port}")
    if port_name and port_name != f"Port {port}":
        return "no"  # custom-named → assumed connected
    ports_load = port_data.get("portsLoad", 0) or 0
    dev_type = device.get("devType")
    if ports_load == 0 or dev_type in _ZERO_LOAD_DEV_TYPES:
        return "heuristic"
    return "no"
