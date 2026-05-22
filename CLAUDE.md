# CLAUDE.md — Contribution Protocol

This file is the authoritative source for how Claude agents contribute to this repo.

---

## Contribution Rules

- Never push directly to `main`. All work goes through feature branches + PRs.
- Each phase = one PR. Phases are never bundled.
- Every phase begins with a **Phase Planning Session** before any code is written (see below).
- No PR is raised until the full gate loop passes. Any failure restarts from Gate 1.

---

## Session Start Protocol

At the start of every phase session, in order:

1. Read this file (`CLAUDE.md`)
2. Confirm the current Claude model name (for `Co-Authored-By` attribution)
3. **Check for `.env`** — credentials live in `.env` at the repo root. Read it before
   any live API call or Gate 5 smoke test. Never commit it; it is already in `.gitignore`.
4. Begin the Phase Planning Session (see below) before writing any code

---

## Phase Planning Session (mandatory before any code is written)

Before starting implementation on each phase, run an interactive planning session with the user:

1. **Present the phase scope** — what tools/features will be built, what files will be created or modified, what the expected outputs are
2. **Confirm usability expectations** — how will a grower actually use this? What does the tool response look like? Walk through example inputs and outputs.
3. **Confirm implementation strategy** — which approach will be taken, any alternatives considered and why rejected
4. **Identify edge cases** — what unusual inputs or device states should be handled? What should the tool return if data is missing?
5. **Get explicit user approval** before writing any code

The session is complete when the user explicitly approves. If the user redirects scope or changes approach during the session, update the plan before starting.

---

## PR Gate Loop (mandatory before every PR)

**Gate 1 — Deep Code Review (Senior Python Engineer persona)**
- Correctness, idiomatic Python, async safety, error handling
- API quirk compliance (see `docs/API.md` for all 15 quirks)
- No blocking calls in async context — all HTTP calls wrapped in `asyncio.to_thread()`
- Retry logic applied to all external HTTP calls via `tenacity`

**Gate 2 — Secondary Code Review (Security Engineer persona)**
- Independent review: injection risks, credential handling, input sanitization
- Log output audit: no credentials, tokens, or PII in any log level
- Dependency version audit

**Gate 3 — Deep Security Review**
- `.env` not committed, confirmed in `.gitignore`
- Docker image doesn't embed secrets
- HTTP-only API exposure documented and accepted risk noted in `docs/API.md`

**Gate 4 — Full Automated Tests Pass**

Run each command and confirm the expected output before checking the box:

- [ ] `ruff check src/ tests/`
      → Expected: `All checks passed.`
- [ ] `mypy src/ac_infinity_mcp/`
      → Expected: `Success: no issues found in N source files`
- [ ] `python3 -m pytest tests/common/ tests/devices/ tests/integration/test_mcp_protocol.py -v`
      → Expected: all tests pass, 0 errors, 0 failures.
      Matches the CI invocation in `.github/workflows/ci.yml` (which runs `pytest tests/`
      minus the live-API skip path). Running the narrower `tests/common/ tests/devices/`
      set locally misses the MCP wire-protocol integration tests; CI catches regressions
      there but local gate-4 wouldn't (P2-F022).
- [ ] `pip-audit --ignore-vuln PYSEC-2025-183`
      → Expected: no known vulnerabilities. PYSEC-2025-183 is an accepted-risk upstream
        `mcp` CVE — see `docs/SECURITY-RISKS.md` (or the "Accepted Dependency CVEs"
        section in `docs/API.md`) for the rationale. Document any **new** CVEs as they
        appear; do not blanket-ignore.

**Gate 5 — Manual Smoke Test Proposal + Execution**
- Write smoke test plan for the PR scope
- Present plan to user for confirmation before executing
- Execute (live API or mock verification)
- Report pass/fail per test case explicitly

**Failure at any gate → fix → restart from Gate 1.**

### Quality Cycle (every 5 phases, or before any major release)

A standard PR Gate Loop covers the code in the PR. A **Quality Cycle** is a
heavier independent pass that audits the entire codebase by launching three
cold subagent personas in parallel — Senior Python Developer, QA Engineer,
Cyber Security Engineer — each with a discipline-specific checklist. The
personas don't see each other's findings until consolidation, which is
what catches the long-tail bugs a single reviewer can't see (Phase 16
found 80+ unique findings doing this; Lesson 11 in
`.claude/internal/PROJECT_REPORT.md` explains why).

Run a Quality Cycle when:
- The PR introduces > 1000 LOC of new code, OR
- 5 phases have passed since the last Quality Cycle, OR
- A user-facing release is being prepared.

The Quality Cycle workflow is documented in the Phase 16 plan file and
tracks findings in `.claude/internal/REVIEW_FINDINGS.md` (local-only — the
entire `.claude/internal/` directory is gitignored). Re-run the three
personas until convergence (Cycle N returns 0 findings). The plan caps at
3 cycles; user escalation if more are needed.

---

## Code Standards

- Python 3.11+, type annotations on all public functions
- `ruff` format enforced, `line-length = 100`
- No `print()` in library code — `logging` only
- No credentials in log output at any level — the credential-redacting
  formatter installed in `server.py` is defense in depth, not a primary
  control. Don't `logger.error("%s", payload)` for any dict that might
  contain credentials.
- No upstream-API exception text returned to the MCP client via `str(e)`.
  Use the three-branch typed-except pattern: `ACInfinityAuthError` →
  friendly auth message + `"detail": "see server logs"`; `ACInfinityAPIError`
  → generic + `"detail": "see server logs"`; `ACInfinityDeviceError` → may
  return `str(e)` because the messages are self-constructed actionable
  guidance (loadType/modeType hints).
- `tenacity` retry on all external HTTP calls. Writes retry on
  `ConnectionError` only — `Timeout` is intentionally excluded because the
  server may have processed the write before the timeout, so retry could
  double-apply state.
- `asyncio.to_thread()` for all blocking operations in async context
- 1.5s rate limit between write API calls (enforced in `client.py`)
- All write tools support `dry_run=True` parameter

### Bulk replacements

When applying the same correction to many sites (e.g. "replace `str(e)` in
error responses everywhere"), grep for the **symptom** (any `str(e)` in a
JSON response that crosses a tool boundary) rather than the **source-text
pattern** you started with. Phase 16 found three cycles of the same leak
because each pass only caught the syntactic shape of the previous pass's
target, missing analogues with different wrappers. See
`.claude/internal/PROJECT_REPORT.md` Lesson 12.

---

## Commit Message Format

```
type(scope): short description

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
```

Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `ci`

**Model version rule:** The version in `Co-Authored-By` must match the model actually used in
that session (e.g., `Sonnet 4.6`, `Sonnet 4.7`, `Opus 4`). Never use the generic `Claude`
attribution — the specific model matters for the project report and historical traceability.
Confirm the current model name at the start of each phase session and substitute accordingly.

---

## Closing Requirements (per phase)

See the **Closing Requirements** section in
`.claude/internal/ac-infinity-mcp-v1-implementation.md` for the full
closing checklist (lessons learned format, defect log, status update).
The entire `.claude/internal/` directory is gitignored — it's the home
for local-only working artifacts (the project plan, review findings,
project report, and any other docs that should not ship to GitHub).
