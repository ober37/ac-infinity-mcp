# CLAUDE.md — Contribution Protocol

This file is the authoritative source for how Claude agents contribute to this repo.

---

## Contribution Rules

- Never push directly to `main`. All work goes through feature branches + PRs.
- Each phase = one PR. Phases are never bundled.
- Every issue follows the **4-Stage Issue Workflow Protocol** (see below).
- No PR is raised until all four stages pass. Any stage failure restarts from Stage 1.
- **All PRs are created as drafts.** Convert to "ready for review" only after Gate 5 (manual
  smoke test) is approved by the user. CI runs on draft PRs, so the test gate is not bypassed.
- **PRs are never merged without explicit user permission.** After Gate 5 approval and all CI
  gates pass, present the status to the user and wait for them to say "merge" (or equivalent)
  before running `gh pr merge`.
- **Before declaring CI green or recommending merge, verify the PR's actual branch state:**
  Run `gh pr view <N> --json mergeStateStatus,mergeable,headRefOid,headRefName` and confirm:
  - `mergeStateStatus` is `CLEAN` (not `BEHIND`, `DIRTY`, or `CONFLICTING`)
  - `mergeable` is `MERGEABLE` (not `CONFLICTING`)
  - `headRefOid` matches the local branch HEAD (`git rev-parse HEAD`)

  `CONFLICTING` on either field means a merge conflict exists — do not report CI passing and do
  not request merge until the conflict is resolved. Fix: abort any in-progress rebase, save the
  full diff (`git diff origin/main HEAD > /tmp/branch.patch`), reset to main
  (`git reset --hard origin/main`), re-apply (`git apply /tmp/branch.patch`), commit, then
  force-push. A PR that is `BEHIND` main must be similarly rebased and re-pushed before the
  all-clear is given. Never report CI passing from a stale run — confirm the run SHA matches
  the current HEAD SHA.

---

## GitHub Issue Hygiene

Every issue must have **both a label and a milestone** applied before a PR is raised against it.
When creating a new issue, apply both immediately. When triaging unlabeled or un-milestoned
issues, audit and update before beginning work.

### Labels

| Label | When to apply |
|---|---|
| `bug` | Incorrect behavior in an existing tool |
| `enhancement` | New feature or capability |
| `documentation` | Doc-only changes (API.md, README, CLAUDE.md, etc.) |
| `security` | Security vulnerability or hardening |
| `usability` | Response wording, field ordering, conflict UX, grower-readable output |
| `api-discovery` | New API endpoints or fields discovered via network capture or reverse engineering |

Multiple labels are encouraged — a `bug` that also affects grower UX should carry both
`bug` and `usability`.

### Milestones

| Milestone | When to apply |
|---|---|
| `v1.0` | Core feature set — all 25 tools working, Gate 5 smoke-tested |
| `v1.0-beta` | Pre-release stabilization work |
| `v2.0` | Post-v1.0 new capabilities |
| `v2.0-beta` | Pre-v2.0 work |

Default to `v1.0` for any issue that completes or improves existing tool behavior. Use `v2.0`
only for net-new tools or major architecture changes agreed with the user.

### Audit cadence

At the start of any triage or planning session, run:

```bash
gh issue list --state open --json number,title,labels | \
  jq -r '.[] | select(.labels | length == 0) | [.number, .title] | @tsv'
```

Label any unlabeled open issues before beginning new work.

---

## Issue Workflow Protocol

Every GitHub issue — feature, bug, or chore — passes through four stages before a PR is
raised. The persona prompt templates for all reviewers live in
`.claude/internal/WORKFLOW_TEMPLATES.md`.

---

### Stage 1 — Plan Review (4 experts in parallel, before any code)

Produce a written implementation plan (scope, files changed, example responses, edge cases,
security considerations, test plan). Then spawn four cold reviewer agents simultaneously,
each given the plan text and the corresponding prompt from `WORKFLOW_TEMPLATES.md`:

| Reviewer | What they check |
|---|---|
| **Security** | Input validation, credential safety, sanitization, write guards, exception shapes |
| **Python** | Async safety, type annotations, quirk compliance, retry policy, structure |
| **QA** | Test coverage plan, fixture adequacy, edge cases, dry-run tests, integration impact |
| **Usability** | Grower-readable responses, port name format, conflict messages, dry-run UX |

**Convergence:** Each reviewer returns `PLAN APPROVED — [Discipline]` when they have zero
BLOCKING findings. Only proceed to Stage 2 when all four approve.

**Iteration:** Any BLOCKING finding → revise the plan → re-run all four reviewers.
Only MAJOR/MINOR findings remaining → user decides. Cap at 3 revision cycles; escalate to
user if not converging.

---

### Stage 2 — Implementation

Worker agent implements against the approved plan:

1. Branch from `main` (or use `isolation: "worktree"` if running in parallel with other issues).
   After checkout, immediately run `git log origin/main..HEAD --oneline` and confirm **zero
   commits** ahead of main. Worktrees can be reused across sessions and silently carry stale
   commits from a prior phase. If stale commits exist, run `git reset --hard origin/main`
   before writing any code.
2. Implement the approved plan — no scope changes without returning to Stage 1
3. Run the mechanical gate:
   - `python3 -m ruff check src/ tests/` → `All checks passed.`
   - `python3 -m mypy src/ac_infinity_mcp/` → `Success: no issues found`
   - `python3 -m pytest tests/common/ tests/devices/ tests/integration/test_mcp_protocol.py -v` → all pass
   - `python3 -m pip_audit --ignore-vuln PYSEC-2025-183` → no new CVEs
4. Any gate failure → fix → re-run from step 3. Do not raise PR until gate is clean.

---

### Stage 3 — Code Review (4 experts in parallel, on the implementation)

Spawn the same four reviewers with the code-review variant of each prompt from
`WORKFLOW_TEMPLATES.md`. Each reviewer is given the full diff and the approved plan.

**Convergence:** Each reviewer returns `CODE APPROVED — [Discipline]` when zero BLOCKING
and zero MAJOR findings remain.

**Iteration:** Apply all BLOCKING + MAJOR findings → commit → re-run all four reviewers.
MINOR findings may be deferred as GitHub issues. Cap at 3 review cycles; escalate to user.

After convergence, raise the PR.

---

### Stage 4 — Documentation Agent

After the PR is created (or immediately before), spawn the Documentation Agent from
`WORKFLOW_TEMPLATES.md`. It is the only agent that writes documentation — the implementation
agent writes no docs.

**The Documentation Agent's mandate:**

*Public docs (ship with the PR):*
- `docs/API.md` — new tools, new quirks, new endpoints in "Accepted Risk" section, tool count
- `docs/SECURITY-RISKS.md` — new endpoints, new CVEs
- `README.md` — update tool count, feature list, or usage examples if the PR changes them
- Tool docstrings in `server.py` — verify they match the final implementation

*Internal docs (gitignored, updated every phase):*
- `.claude/internal/PROJECT_REPORT.md` — measure and update LOC, test count, coverage, tool
  count; add any new lessons
- `.claude/internal/BETA_MANUAL_TESTING.md` — add session entry if live testing occurred;
  add lessons if new patterns emerged
- `.claude/internal/ac-infinity-mcp-v1-implementation.md` — mark phase complete, update
  lessons and closing requirements

The Documentation Agent runs these commands to get live values (never estimates from memory):

```bash
find src/ -name "*.py" | xargs wc -l | tail -1          # source LOC
grep -c "@mcp_server.tool()" src/ac_infinity_mcp/server.py  # tool count
python3 -m pytest --collect-only -q tests/ 2>&1 | tail -3   # test count
python3 -m pytest tests/common/ tests/devices/ \
  tests/integration/test_mcp_protocol.py \
  --cov=ac_infinity_mcp --cov-report=term-missing 2>&1 | grep TOTAL  # coverage
```

The Documentation Agent reports: `N items updated. N items already current. Gaps: [list].`
The PR is not considered complete until the Documentation Agent reports no gaps in the
internal docs.

---

## Session Start Protocol

At the start of every phase session, in order:

1. Read this file (`CLAUDE.md`)
2. Confirm the current Claude model name (for `Co-Authored-By` attribution)
3. **Check for `.env`** — credentials live in `.env` at the repo root. Read it before
   any live API call or Gate 5 smoke test. Never commit it; it is already in `.gitignore`.
4. Begin the Phase Planning Session (see below) before writing any code

---

## Stage 1 Detail — What the Plan Must Cover

The implementation plan (Stage 1 input) must address all of the following before the four
reviewers are spawned. A plan that skips any section will receive BLOCKING findings.

1. **Phase scope** — what tools/features will be built, what files will be created or modified, what the expected outputs are
2. **Usability walkthrough** — how will a grower actually use this? Walk through example tool inputs and exact JSON output. Include a sample dry-run response and a sample live response.
3. **Implementation strategy** — which approach will be taken, alternatives considered and why rejected
4. **Edge cases** — unusual inputs or device states; what the tool returns when data is missing, device not found, API error, partial failure
5. **Security considerations** — input validation rules, sanitization, log-safety, write guards
6. **Test plan** — which new test functions, which fixtures, which edge cases get parametrized cases
7. **Documentation delta** — which sections of `docs/API.md` and `docs/SECURITY-RISKS.md` will change

Get explicit user approval of the written plan before spawning the four reviewers. If any
reviewer returns BLOCKING findings, revise the written plan (not just the mental model) and
re-run all four. The approved plan is the contract for Stage 2.

---

## PR Gate Loop (mandatory before every PR)

**Gate 1 — Deep Code Review (Senior Python Engineer persona)**
- Correctness, idiomatic Python, async safety, error handling
- API quirk compliance (see `docs/API.md` for all 27 quirks)
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
- **Restart Claude Desktop before executing.** The MCP server process caches imported modules
  at startup. If commits were made to the worktree since the last Claude Desktop launch, the
  server is running stale code — new fields and behavior will be absent from live responses.
  A restart takes ~5 seconds; skipping it can produce an entire session of phantom failures.
- Write smoke test plan for the PR scope
- Present plan to user for confirmation before executing
- Execute (live API or mock verification)
- Report pass/fail per test case explicitly as user-query + agent-says pairs
- On any failure: file a new GitHub issue immediately; do not ask to merge until user resolves

**After all gates pass — update the PR body before requesting merge:**
Tick every checklist item (`[ ]` → `[x]`) in the PR description and add the Gate 5
result (date + PASS) to the smoke test line. A PR with unchecked boxes must not be merged.
Use `gh pr edit <N> --body "..."` to update — do not leave checkboxes unchecked.

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

### Usability — IDs are internal, names are user-facing

Never surface raw internal identifiers (`automation_id`, `devId`, port integers used as
keys, etc.) in user-facing messages. Users do not know these values and cannot act on
them. Always use the human-readable name from the API response:

- When reporting the result of `create_advance_automation`, reference the automation by
  `name`, not `automation_id`. The ID is returned in the JSON for programmatic chaining
  only — do not mention it to the user.
- When listing automations, present them as a named list. If a follow-up operation
  requires an ID, resolve it by matching the user's name input against the list.
- The same rule applies to device IDs and port numbers: prefer "Humidifier (Port 1)"
  over "port 1" or a raw devId.

### User-facing text rules (instruction, description, suggested_reply, summary fields)

These rules apply to every string returned in a tool's JSON response that a grower (via
Claude) will read aloud or act on. Violations were codified from the #108 audit.

- **No Python function call syntax** — never write `call foo()`, `foo(param=value)`,
  or any text that looks like a Python invocation. Growers cannot execute Python.
- **The word `dry_run` must never appear** in any user-facing string. It is an
  internal implementation detail. Write "preview the action" or "I'll show you what
  will happen before doing it" instead.
- **No internal parameter names** — `device_id`, `adv_ids`, `automation_id`, `port`
  as a raw integer, `devId`, etc. must not appear in `instruction`, `description`,
  `suggested_reply`, `summary`, or `human_summary` fields.
- **No raw numeric IDs** — always use the human-readable name from the API response.
  `automation_id` is returned in the JSON for programmatic chaining only.
- **Options are named for growers, not developers** — option descriptions say
  "release from automation" not "break_out_of_automation".
- **`instruction` fields must be natural-language prompts** — write them as text
  Claude can read aloud: "Ask me to release Filter (Port 4) from the 'Night Cycle'
  automation so you can control it manually." Not: "Call break_out_of_automation(...)".
- **Port references always use name + number** — "Filter (Port 4)" never just "port=4"
  or a bare integer.

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

Closing requirements are executed by the **Stage 4 Documentation Agent** — see the Issue
Workflow Protocol above. The agent uses the prompt template in
`.claude/internal/WORKFLOW_TEMPLATES.md` and the running checklist in
`.claude/internal/ac-infinity-mcp-v1-implementation.md`.

The entire `.claude/internal/` directory is gitignored — it's the home for local-only
working artifacts (project plan, review findings, project report, workflow templates, and
any other docs that should not ship to GitHub).

**A phase is not complete until the Documentation Agent reports zero gaps.** The PR may be
raised before Stage 4 completes, but it should not be merged until the Documentation Agent
has finished and the internal docs are current.
