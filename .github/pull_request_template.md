## Summary

## Changes
-

## Gate Loop Checklist
- [ ] `ruff check src/ tests/` — zero warnings
- [ ] `mypy src/ac_infinity_mcp/` — zero errors
- [ ] `pytest tests/common/ tests/devices/ -v --cov=ac_infinity_mcp --cov-fail-under=90` — all pass
- [ ] `pip-audit` — no new CVEs (16 transitive CVEs from mcp deps are expected noise)
- [ ] Smoke tests executed and results reported

## AI Model Used
- Model:

## Devices Tested
- [ ] Controller 69 Pro (devType 11) — live API
- [ ] Controller 69 Pro+ (devType 18) — live API
- [ ] Controller 89 AI+ (devType 20+) — live API
- [ ] N/A — docs/tests only

## API Quirks
- [ ] No new API quirks discovered
- [ ] New quirk(s) added to `docs/API.md`
