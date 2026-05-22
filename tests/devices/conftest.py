"""Per-suite conftest for tests/devices/.

The mock_env_vars autouse fixture is scoped here (and in tests/common/conftest.py)
so it does NOT apply to tests/integration/test_live.py — see tests/conftest.py.
"""
import pytest

from tests.fixtures.ai_plus_device_fixtures import AI_PLUS_DEVICE
from tests.fixtures.legacy_device_fixtures import LEGACY_11_DEVICE, LEGACY_18_DEVICE


@pytest.fixture(autouse=True)
def mock_env_vars(monkeypatch):
    monkeypatch.setenv("AC_INFINITY_EMAIL", "test@example.com")
    monkeypatch.setenv("AC_INFINITY_PASSWORD", "testpassword123")


@pytest.fixture
def legacy_11_device():
    return LEGACY_11_DEVICE.copy()


@pytest.fixture
def legacy_18_device():
    return LEGACY_18_DEVICE.copy()


@pytest.fixture
def ai_plus_device():
    return AI_PLUS_DEVICE.copy()
