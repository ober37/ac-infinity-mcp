# Contributing to ac-infinity-mcp

Thank you for your interest in contributing! This guide covers everything you need to get started.

---

## Dev Environment Setup

```bash
git clone https://github.com/ober37/ac-infinity-mcp.git
cd ac-infinity-mcp
pip install -e ".[dev]"
cp .env.example .env
# Edit .env with your AC Infinity credentials
```

---

## Running Tests

**Unit tests** (no credentials required):

```bash
pytest tests/common/ tests/devices/ -v
```

**With coverage:**

```bash
pytest tests/common/ tests/devices/ -v --cov=ac_infinity_mcp --cov-fail-under=85
```

**Integration tests** (requires live AC Infinity credentials in `.env`):

```bash
pytest tests/integration/ -v
```

Integration tests are skipped automatically in CI when `AC_INFINITY_EMAIL` is not set.

---

## Code Style

- Python 3.11+, type annotations on all public functions
- `ruff` for linting and formatting (`line-length = 100`)
- `mypy` for type checking

```bash
ruff check src/ tests/
mypy src/ac_infinity_mcp/
```

Docstrings are required for all public functions. Keep them concise — one line for the summary,
then args/returns if non-obvious.

---

## PR Process

1. Fork the repo and create a branch: `git checkout -b feat/your-feature`
2. Make your changes
3. Run the full check suite: `ruff check src/ tests/ && mypy src/ac_infinity_mcp/ && pytest tests/`
4. Open a PR against `main`
5. All CI checks + CodeQL must pass; one approval required

---

## Automated Checks on Every PR

| Check | Tool | What it catches |
|---|---|---|
| Lint/format | `ruff` | Style issues, unused imports, formatting |
| Type safety | `mypy` | Type errors, missing annotations |
| Tests | `pytest` | Regressions, broken logic |
| CVE scan | `pip-audit` | Known vulnerabilities in dependencies |
| Security | CodeQL | Injection flaws, hardcoded credentials (free on public repos) |
| Dependency updates | Dependabot | Automated PRs for vulnerable dependencies |

All checks are required to pass before merge.

---

## Branch Protection (main)

The following GitHub branch protection rules are required on `main`:

| Rule | Setting |
|------|---------|
| Require status checks to pass | ✅ Required |
| Required checks | `test (3.11)`, `test (3.12)`, `Analyze Python` |
| Require branches to be up to date | ✅ Required |
| Dismiss stale reviews on new push | ✅ Required |
| Require linear history | ✅ Required |
| Restrict direct pushes to main | ✅ Enabled (no direct pushes) |

These rules are enforced in GitHub Settings → Branches → Branch protection rules.
All CI checks + CodeQL must pass and the branch must be up-to-date with main before
any PR can be merged.

---

## API Quirk Documentation

The AC Infinity cloud API has 18 documented quirks that affect correct implementation.
**Read `docs/API.md` before writing any code that touches the API.**

Key ones to know:
- Auth parameter has intentional typo: `appPasswordl` (lowercase L at end)
- All sensor readings are divided by 100 in API responses
- Port speeds are nibble-encoded bitmasks in historical data
- Write operations have a 1.5s rate limit

---

## Scope: v1.0 vs v2.0

**v1.0 (this repo):** WiFi cloud API + built-in controller sensors only (temp, humidity, VPD).
Works with Controller 69 Pro, 69 Pro+, 89 AI+ and any other WiFi-connected controller.

**v2.0 (backlog):** External UIS sensors (CO2, pH, EC/TDS, soil moisture, water sensors, light)
and Bluetooth-local control. See the v2.0 GitHub Milestone for the full backlog.

If a feature requires hardware plugged into a UIS sensor port or Bluetooth connectivity, it
belongs in v2.0, not here.

---

## Reporting Issues

Open a GitHub Issue and include:
- Your AC Infinity controller model (e.g., "Controller 69 Pro")
- Firmware version (visible in the AC Infinity app)
- Minimal reproduction steps
- Actual vs. expected behavior

---

## License

MIT. See `LICENSE` for details.
