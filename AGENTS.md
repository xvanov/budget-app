# Project rules

This file is loaded automatically by OpenCode for every command in this repo. `prd.md` is the authoritative checklist of what to build and how to know it's done. This file captures the rules and conventions that don't fit cleanly into a story.

## What we're building

See `prd.md` for the full description, the 17 stories, the ground-truth rules, and the testing & evaluation model.

In one sentence: a personal-finance app that ingests messy multi-bank exports, collapses internal transfers, categorizes via LLM (not regex) using a living context file that grows with the user, and presents the truth on a dashboard whose design is already laid out in `frontend-design/`.

## Hard rules

- **No regex / keyword maps for categorization.** Every category decision goes through the LLM at ingest time, with `budget_context.md` and the PRD ground-truth rules in the system prompt.
- **Reuse the existing UI design.** Components, fonts, colors, and layouts come from `frontend-design/src/*.jsx` and `frontend-design/src/app.css`. Do not redesign.
- **`budget_context.md` is a living file.** It starts blank, gets inferred from `samples/` on first run, and is continuously updated as the user patches transactions or sends chat-corrections. Treat it as the canonical knowledge base — edit it, add to it, remove stale entries.
- **Deterministic invariants are exact.** Row counts, dedup math, identity checks (`net = income - expense`) have zero tolerance.
- **LLM-judgment is evaluated by Opus 4.7 as judge** (see `prd.md` → "Testing & evaluation"). A story passes when the judge returns zero REJECT verdicts for that story's relevant transaction set.
- **Single source of truth.** When updating a story's `passes` flag, update only `prd.md`. Don't fork copies of the PRD into other files.
- **Don't bypass commit hooks** if/when we add any. Don't `git push` unless explicitly asked.
- **Fully autonomous.** This Ralph loop runs unattended under an outer supervisor agent. No story, no test, no setup step may require human-in-the-loop input. If a step needs information you don't have, infer reasonable defaults from `samples/`, log the assumption in `activity.md`, and continue.
- **No dummy tests.** Every test must exercise real logic with real or representative data. Tests that always pass (`assert True`, mocked-mock smoke checks) are slop — delete them. A story with only dummy tests is NOT done.
- **Stub `call_llm` in tests; never make real LLM calls from inside `pytest`.** The backend container does not have `opencode`/`claude` on PATH (the prd.md spec says it _should_ bind-mount them, but the current image doesn't), so any test that triggers a real shell-out hangs forever. Always stub the boundary helper (`app/llm.py::call_llm`) at test time with `monkeypatch` / a fixture returning canned JSON. The categorizer's real logic — prompt construction, parsing, rule evaluation, context updates — still runs against the stubbed response, so this is NOT a dummy test. If you find a test that hangs in `docker compose exec backend pytest`, the root cause is almost certainly an un-stubbed `call_llm`.
- **Run `pytest` from the host, not inside the container.** Tests should be invoked as `cd /home/k/budget-app-v2 && PYTHONPATH=backend python3 -m pytest backend/tests/ -q`. The host has `opencode`/`claude` on PATH, so even un-stubbed `call_llm` paths work there as a fallback. `docker compose exec backend pytest …` will hang on any un-stubbed test and is the single biggest source of iteration timeouts in this loop — stop using it. The dockerized backend on `:8001` is still the integration target for HTTP-level assertions (point requests at `http://127.0.0.1:8001`).
- **Don't POST `/api/categorize` without a row limit / batch cap.** The endpoint will fire one LLM call per uncategorized transaction (~5–10s each) — with ~5,700 rows in the DB that's hours of in-container `opencode run` / `claude -p` calls and will block the iteration well past any timeout. If you need to verify the categorizer end-to-end, POST a `{"transaction_ids": [...]}` or `{"limit": 5}` payload, or hit `/api/categorize/single` if it exists. Same caution applies to any other "process every row" endpoint built into the app.
- **Opus 4.7 (the judge) is invoked via `claude -p --model claude-opus-4-7`, NOT opencode.** `opencode run -m opus` returns "Model not found" on this account. For the strong-judgment path in `app/llm.py::call_llm(strong=True)` and the visual-judgment harness for story 53, shell out to `claude -p --output-format text --dangerously-skip-permissions --model claude-opus-4-7 "<prompt>"`. The opencode-→-claude fallback in the helper does NOT need to change for routine `strong=False` calls; only the `strong=True` branch should hard-pin claude+opus.
- **`samples/` and `budget_context.md` are gitignored** — proprietary financial data. Never commit, never echo to logs that leave the box, never paste into commit messages.

## Per-iteration workflow (Ralph loop)

1. Read `prd.md`. Find the highest-priority story where `passes` is false and all `depends_on` are true.
2. Implement only that one story.
3. Run the testing pyramid (see `prd.md` → Testing pyramid). At minimum:
   - Smoke: `docker compose up -d --build`, then `/api/health` returns 200, then frontend returns 200
   - Unit / integration / e2e / visual as relevant to the story
4. Log what you did in `activity.md` with a dated entry (what changed, what tests passed, any non-obvious decisions).
5. Commit. One commit per story. Message format: `feat(<story-id>): <what you built>`.
6. If and only if every acceptance criterion is demonstrably true (and the full pyramid up through the relevant level is green), flip that story's `passes` from `false` to `true` in `prd.md` and commit the flip.

The outer supervisor agent watches activity.md + git log to decide whether the loop is converging or stuck. Don't lie in activity.md — false claims of done get caught by the supervisor and waste the next loop.

## Tech stack (locked)

- Backend: Python + FastAPI + SQLite (named Docker volume)
- Frontend: React, ported from `frontend-design/src/`
- LLM: **No Anthropic API key. No SDK calls.** All LLM invocations from the app shell out to the host's local CLI subscriptions — `opencode run` preferred, `claude -p` as fallback (same pattern as `ralph.sh`). `ANTHROPIC_API_KEY` must NOT appear in code, `.env`, `docker-compose.yml`, or anywhere else. See "LLM invocation" below.
- Deploy: `docker compose up -d --build`. Backend on `:8001` (host) → `8000` (container). Frontend on `:8081` (host) → `8080` (container). The non-default host ports avoid a conflict with other docker stacks the user runs locally.

## LLM invocation (in-app)

Every LLM call from the backend (categorization, chat-correct, anomaly explanation, budget suggestions, judge calls) MUST go through one of:

1. `opencode run [-m <model>] "<prompt>"` — preferred.
2. `claude -p "<prompt>" --output-format text` — fallback when opencode fails (missing binary, non-zero exit, or output contains credit/quota/auth signals: `insufficient credits`, `quota exceeded`, `rate limit`, `402`, `401`, `invalid api key`, `billing issue`). Mirror the regex from `ralph.sh`.

Implementation notes the inner loop must respect:

- The backend container must have `opencode` and `claude` on PATH and read access to the host user's credential dirs (typically `~/.claude/` and `~/.config/opencode/` or `~/.opencode/`). Bind-mount these from the host with `:ro` in `docker-compose.yml`. Do not copy creds into the image.
- Model selection: pass a fast model for routine categorization (e.g. `-m haiku` for opencode, or let claude use its default) and Opus 4.7 (`-m opus` / `--model claude-opus-4-7`) for judge/strong-reasoning calls.
- Prompts are constructed in code with `budget_context.md` + the ground-truth rules in the system prompt — no regex/keyword maps anywhere.
- Wrap the shell-out in a single helper (e.g. `app/llm.py::call_llm(prompt, *, strong=False) -> str`) that all callers use. The helper handles the opencode→claude fallback and parses the JSON-ish response.
- The helper must time-out individual calls (e.g. 60s default, 180s for strong) and surface errors as structured exceptions, not silent failures.

## Agent fallback (ralph.sh)

`ralph.sh` tries OpenCode first. If OpenCode is missing on PATH, exits non-zero, or its output contains credit/quota/auth-failure signals (`insufficient credits`, `quota exceeded`, `rate limit`, `402`, `401`, `invalid api key`, `billing issue`, etc.), the iteration falls back to `claude -p`. Each iteration starts fresh with OpenCode, so a transient credit issue in iteration N does not lock you out of OpenCode for iteration N+1. The current agent is logged at the start of each iteration as `[agent=opencode]` or `[agent=claude]`.

Run-time signal: if you see `Falling back to claude for this iteration` in stderr, OpenCode credits are out (or its auth broke). Investigate after the loop completes — keep building.

## File layout

- `prd.md` — the PRD with the JSON task list
- `budget_context.md` — the living knowledge base (blank initially)
- `PROMPT.md`, `ralph.sh`, `activity.md` — Ralph harness (verbatim from upstream)
- `frontend-design/` — the source-of-truth UI design
- `samples/` — real bank/payment exports for development and testing

## Completion

When ALL stories have `passes: true` and the final-integration story is verified, output `<promise>COMPLETE</promise>`.
