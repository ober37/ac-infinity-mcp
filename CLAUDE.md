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
- [ ] `python3 -m pytest tests/common/ tests/devices/ -v`
      → Expected: all tests pass, 0 errors, 0 failures
- [ ] `pip-audit`
      → Expected: no known vulnerabilities (document any new CVEs; existing `mcp` upstream
        noise is accepted risk — see Gate 3 notes in `docs/API.md`)

**Gate 5 — Manual Smoke Test Proposal + Execution**
- Write smoke test plan for the PR scope
- Present plan to user for confirmation before executing
- Execute (live API or mock verification)
- Report pass/fail per test case explicitly

**Failure at any gate → fix → restart from Gate 1.**

---

## Code Standards

- Python 3.11+, type annotations on all public functions
- `ruff` format enforced, `line-length = 100`
- No `print()` in library code — `logging` only
- No credentials in log output at any level
- `tenacity` retry on all external HTTP calls
- `asyncio.to_thread()` for all blocking operations in async context
- 1.5s rate limit between write API calls (enforced in `client.py`)
- All write tools support `dry_run=True` parameter

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

See the **Closing Requirements** section in `.claude/ac-infinity-mcp-v1-implementation.md`
for the full closing checklist (lessons learned format, defect log, status update).
