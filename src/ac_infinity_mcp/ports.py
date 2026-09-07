from ac_infinity_mcp.analytics import _ZERO_LOAD_DEV_TYPES

# portResistance == 65535 (0xFFFF) is the hardware open-circuit sentinel: nothing connected.
# The controller measures electrical resistance across each port; connected devices present
# real values (e.g. 400Ω light, 7500Ω fan, 15800Ω heater). Confirmed via ProxyMan 2026-05-26.
_PORT_EMPTY_RESISTANCE: int = 65535


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
    """
    if port_data is None or device is None:
        return False

    port_resistance = port_data.get("portResistance")
    if port_resistance is not None:
        try:
            return int(port_resistance) == _PORT_EMPTY_RESISTANCE
        except (ValueError, TypeError, OverflowError):
            return False  # treat as connected on malformed API data

    # Fallback for firmware that omits portResistance: preserve dual-signal heuristic.
    port_name = port_data.get("portName", f"Port {port}")
    if port_name and port_name != f"Port {port}":
        return False  # custom-named → assumed connected
    ports_load = port_data.get("portsLoad", 0) or 0
    dev_type = device.get("devType")
    return ports_load == 0 or dev_type in _ZERO_LOAD_DEV_TYPES


def _empty_port_advisory(port_label: str) -> str:
    """Return the grower-friendly advisory text for an empty port."""
    return (
        f"{port_label} doesn't appear to have anything connected. "
        "If you meant a different port, let me know which one."
    )


def _port_empty_confidence(port_data: dict | None, port: int, device: dict | None) -> str:
    """Return "sentinel", "heuristic" or "no" for the empty-port signal.

    ``_is_port_empty`` collapses two very different signals into one bool, which is
    right for an advisory but not for control flow:

    - ``"sentinel"`` — ``portResistance == 65535``, the hardware open-circuit value
      (Quirk 27). Direct evidence.
    - ``"heuristic"`` — ``portResistance`` absent, so the old-firmware fallback fired:
      a default port name AND (zero load OR a devType known to report zero load). That
      matches plenty of ports that do have equipment attached — notably any
      default-named port sitting at zero load.

    Only "sentinel" is strong enough to redirect a grower away from an automation
    conflict; "heuristic" may be added as an advisory alongside one.
    """
    if not _is_port_empty(port_data, port, device):
        return "no"
    if port_data is not None and port_data.get("portResistance") == _PORT_EMPTY_RESISTANCE:
        return "sentinel"
    return "heuristic"
