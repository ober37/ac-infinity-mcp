"""Unit tests for ac_infinity_mcp.client module-level helpers."""

from ac_infinity_mcp.client import build_add_groups_payload

_REPRESENTATIVE_KWARGS = dict(
    dev_id="ABC123",
    port=3,
    clean_name="Night Cycle",
    on_speed=7,
    begin_time=1320,
    end_time=1439,
)


def test_build_add_groups_payload_required_fields():
    """All required top-level fields are present in output."""
    payload = build_add_groups_payload(**_REPRESENTATIVE_KWARGS)
    assert payload["advName"] == "Night Cycle"
    assert payload["onSpeed"] == 7
    assert payload["beginTime"] == 1320
    assert payload["endTime"] == 1439
    # Port 3 → 2^(3-1) = 4
    assert payload["grouptDevType"] == 4


def test_build_add_groups_payload_field_count():
    """Payload contains the expected ~50 fields — catches accidental omissions."""
    payload = build_add_groups_payload(**_REPRESENTATIVE_KWARGS)
    assert len(payload) >= 45


def test_build_add_groups_payload_returns_dict():
    """Return value is a plain dict."""
    payload = build_add_groups_payload(**_REPRESENTATIVE_KWARGS)
    assert isinstance(payload, dict)


def test_build_add_groups_payload_port_bitmask():
    """grouptDevType bitmask is computed correctly for each port number."""
    for port in range(1, 9):
        payload = build_add_groups_payload(
            dev_id="X",
            port=port,
            clean_name="Test",
            on_speed=5,
            begin_time=0,
            end_time=1439,
        )
        assert payload["grouptDevType"] == 2 ** (port - 1)


def test_build_add_groups_payload_schedule_always_active_sentinel():
    """Sentinel value 255 for begin_time/end_time is mapped to full-day range."""
    payload = build_add_groups_payload(
        dev_id="X",
        port=1,
        clean_name="Always On",
        on_speed=5,
        begin_time=255,
        end_time=255,
    )
    assert payload["beginTime"] == 0
    assert payload["endTime"] == 1439


def test_build_add_groups_payload_devid_not_in_payload():
    """devId is intentionally excluded — _create_advance_automation_inner injects it."""
    payload = build_add_groups_payload(**_REPRESENTATIVE_KWARGS)
    assert "devId" not in payload


def test_build_add_groups_payload_switchtime_is_127():
    """switchTime must be 127 (all 7 days); 255 causes Continuous mode in app."""
    payload = build_add_groups_payload(**_REPRESENTATIVE_KWARGS)
    assert payload["switchTime"] == 127
