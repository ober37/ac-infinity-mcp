"""Per-suite conftest for unit tests under tests/common/.

The mock_env_vars autouse fixture is scoped here (and in tests/devices/conftest.py)
so it does NOT apply to tests/integration/test_live.py, which depends on the
real AC_INFINITY_EMAIL / AC_INFINITY_PASSWORD env vars captured from the
developer's environment.
"""
import pytest


@pytest.fixture(autouse=True)
def mock_env_vars(monkeypatch):
    monkeypatch.setenv("AC_INFINITY_EMAIL", "test@example.com")
    monkeypatch.setenv("AC_INFINITY_PASSWORD", "testpassword123")
