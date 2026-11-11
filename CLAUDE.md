# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

FP AI Summer Challenge, Team 13 (Emilio, Katrina, Lawrence). Demo: Aug 11, 2026.

## What this project is

**PIVOTED 2026-08-04: the primary deliverable is a standalone application (`app/` +
`simulation_service/`), not the Claude Code skill.** Lawrence directed this explicitly — the
`/monte-carlo-model` skill (`skills/monte-carlo-model/`) is superseded as the thing being
shipped/demoed, but its methodology (variable taxonomy, distribution defaults, correlation tier
ladder, binary-event screening rules) is the reference logic the app ports from, not something to
rebuild from scratch. Don't resume building the skill as the primary path unless Lawrence reverses
this again.

Tines deployment is a deliberately later phase (Lawrence's direction, 2026-08-05: "run it locally
first, deploy through Tines later") — not blocked on Tines access, just not started. See memory
`project_pivot_tines` for that design (Pages/Apps for the UI, an AI Action for the brainstorming,
an HTTP Request action calling `simulation_service`) once that phase actually starts.

## Running it

```bash
pip install -r simulation_service/requirements.txt -r app/requirements.txt
uvicorn app.main:app --reload --port 8009        # full app: http://127.0.0.1:8009
```

No test suite or lint config exists yet. The one executable check in the repo is
`simulation_service/smoke_test.py`, which runs the generic engine against the one real
`deal_config.yaml` in the repo (`cases/rxvantage/`) and prints percentiles/driver-ranking for
comparison against that deal's bespoke `shadow_model.py` output (not expected to match exactly —
see "Two engines" below):

```bash
python simulation_service/smoke_test.py
```

`curl http://127.0.0.1:8009/api/health` reports which LLM backend is active
(`native_cli_on_path`, `native_sdk_available`) — check this first when Stage 2/3 calls aren't
behaving as expected.

## Architecture

### `app/` — the local application (FastAPI backend + one-page frontend)

`main.py` wires everything together behind `/api/ingest`, `/api/propose` (+ `/api/propose-demo`),
`/api/chat`, `/api/extract-financials` (+ `/api/extract-financials-demo`), `/api/simulate`,
`/api/export-slide`. Session state (ingested text, the working variable card, the base-case
financials spine, chat history, a native-session id) lives in an in-memory dict, process lifetime
only — no persistence, no auth, single-user by design.

- **`llm.py`** is the part worth understanding before touching any AI-backed stage: it tries THREE
  backends in order — **native** (shells out to the user's own already-authenticated `claude` CLI
  via `claude-agent-sdk`), then **API key** (`claude_client.py`, needs `ANTHROPIC_API_KEY` — via
  `.env`, never committed), controlled by `LLM_BACKEND` (`auto` | `native` | `api`).
  `_resolve_cli_path()` does NOT just trust `shutil.which("claude")` — the process running this app
  (e.g. a Cowork/agent-mode session's own shell) can have a completely different PATH than the
  terminal the user actually ran `claude login` from, even on the same machine, which silently
  routes calls to the SDK's own bundled (unauthenticated) copy instead of the user's real seat. It
  falls back to checking the native installer's default path directly (`~/.local/bin/claude.exe` /
  `~/.local/bin/claude`) before giving up. `extract_structured()` is the generic entry point (one
  system prompt + one tool schema in, structured JSON out) — both Stage 2 propose and the Stage 5
  financials extraction share it rather than duplicating the native/API fallback logic.
  `demo_card.py` is a third, explicit, non-AI path (the "Use demo card" / "Use demo financials"
  buttons) — every field it returns says outright it isn't AI-generated; never let demo data reach
  `/api/export-slide` or a real IC conversation looking like a real proposal or a real extraction.
- **`ingest.py`** reads sheets in priority order, not workbook order (`_prioritize_sheets`) — tabs
  matching "summary"/"financial model"/"p&l"/"model"/"forecast" go first. This exists because
  `main.py`'s `MAX_INGEST_CHARS` truncates the combined deck+model dump, and the financial-statement
  tab is exactly the one that must not be the part that gets cut (this happened for real — SPS
  Commerce's ingest hit `truncated: true` before this fix existed).
- **`prompts.py`** is the ported skill methodology (Stage 2 taxonomy/distribution-defaults/
  correlation-ladder/binary-event rules, Stage 3's six standing questions, Stage 5's strict
  no-invented-numbers financial-spine extraction) — keep this in sync with
  `skills/monte-carlo-model/SKILL.md` if either changes; they should stay the same judgment, two
  different delivery mechanisms. Stage 1/2's "deck" is deliberately framed as any business-context
  document (draft IC deck, CIM, management materials, etc.), not a fixed IC-deck template — the
  ingest UI label and Stage 2's system prompt were both broadened together; keep them in sync if
  either changes.
- **Stage 5 (base-case financials)** sits between the variable card and the simulation: `/api/
  extract-financials` pulls a 6-row, 5-fiscal-year spine (revenue, gross profit, EBITDA, levered
  FCF, plus a single exit-year exit valuation and proceeds-to-FP figure) from the ingested model,
  rendered as an editable grid (`static/index.html`'s `renderFinancialsGrid`/`readFinancialsGrid`)
  with live growth/margin annotations recomputed on every edit. The associate must click "Confirm
  base case financials" before Stage 6 unlocks. This is a strict extraction task, not a proposal —
  `prompts.py`'s Stage 5 prompt has no "reasoned first-pass estimate" carve-out; a number that can't
  be found is `null`, never guessed. The prompt specifically instructs finding the "Summary" and
  "Financial Model" tabs (the pattern real deal models in `cases/` consistently use, whatever the
  literal tab names are), cross-checking that the two tabs agree, and confirming any case-selector
  cell (often literally "Operating Case") is set to the FP base case before reading — flagged in
  `source_notes`, which the grid now displays, if it isn't. **A blank grid cell is a hard stop, not
  a silent zero**: `null` in the JSON becomes `nan` once cast to a numpy array, and previously
  propagated all the way to an opaque `"supplied range of [nan, nan] is not finite"` from numpy's
  histogram call. Both `static/index.html` (`validateFinancialsGrid`) and `lbo_spine.py`
  (`_require_complete_spine`) now check for this before any path gets sampled, naming the exact
  missing row/year instead.
- **`slide_export.py`** builds Stage 7's IC slide against the real `fp-ppt-template` skill's
  template (copied into `app/assets/fp_template.pptx` so the app is self-contained). The template
  ships with example slides baked in — **`_strip_example_slides()` must run before adding real
  content**, or the export silently contains leftover placeholder slides ahead of the real one
  (this bit us once already). This slide export is deliberately NOT casino-themed — it's the actual
  IC-facing artifact; the theme lives only in `static/index.html`.
- **`static/index.html`** is a single self-contained HTML/JS page (no build step, no framework)
  talking to the backend purely via `fetch`. Casino-themed (felt green `#0b3d2e` + brass gold
  `#b8912f`, a CSS `conic-gradient` roulette wheel) — intentionally confined to this file, not the
  exported slide. `startLoadingStatus(elementId, messages)` is the reusable spinner-plus-rotating-
  status-text helper (Propose, Extract Financials, and Simulate all use it with their own message
  lists) — add new AI-backed calls through this rather than a bare "Loading..." string. Simulate
  itself takes no user-facing parameters — paths (10,000) and seed (42) are hardcoded in the
  `simulate-btn` handler, not exposed as inputs. Watch the `opts(list, sel)` pattern here: option
  arrays must be `[value, label]` pairs in that order — a swapped order (tier *name* as the option
  value) serializes as `NaN`/`null` downstream and crashes `simulation_service` with an opaque
  `TypeError` (already happened once with the correlation-tier dropdown; the pattern is worth
  re-checking if a similar dropdown misbehaves).

### `simulation_service/` — the generic parametric Monte Carlo engine

Exposed both as its own standalone FastAPI service (`app.py`, importable independently) and as a
library `app/main.py` calls directly. Pipeline: `distributions.py` (PERT/lognormal/beta/discrete_pmf/
bernoulli/uniform samplers fit from P10/P50/P90) → `correlation.py` (tier ladder → matrix → Higham
nearest-PSD repair → Gaussian copula sampling) → `events.py` (binary-event firing + permanent vs.
temporary-with-recovery shock shaping) → `lbo.py` (the actual chain) → `engine.py` (orchestrates all
of the above, adds driver ranking, histogram bins, and the probability table, all computed from the
full path arrays, never estimated from the small `paths` sample the response also carries for
spot-checking).

**Role-matching is the one non-obvious mechanism here**: `deal_config.yaml` carries no explicit
"this variable drives revenue growth" tag, so variables are matched to roles (`revenue_growth`,
`ebitda_margin`/`gross_margin`, `exit_multiple`, `leverage`, `entry_tev`, `cost_out`) by **substring
match on their `id`**. Anything that matches no role is still sampled (so its percentiles are real)
but has zero effect on the LBO math — surfaced back as `unmapped_variables`, never silently
dropped. Leverage variables are further disambiguated by magnitude (`< 20` ⇒ treated as
turns-of-EBITDA, `>= 20` ⇒ treated as a direct dollar amount) since `deal_config.yaml` doesn't
distinguish these either.

**Two engines, chosen automatically by `engine.py` on whether `deal_config["base_case_financials"]`
is present:**
- **`lbo.py`** (`run_paths`) — the older generic parametric chain, anchored on single
  `fixed_assumptions` numbers (`anchor_revenue`, etc.). Still the fallback when no base-case spine
  was confirmed.
- **`lbo_spine.py`** (`run_paths_from_spine`) — anchored on the CONFIRMED 6-row/5-year spine from
  Stage 5 instead of one hand-typed number, and used whenever it's present. The key design property
  (worth re-reading `lbo_spine.py`'s own docstring before changing the math): every row is built as
  *"the base case's own real multi-year trajectory, scaled or ramp-shifted so it lands exactly on
  the sampled variable's value"* rather than *"replace the base case with a fresh formula"* — so a
  path where every variable draws the base case's own implied value (its own CAGR, its own terminal
  margin, its own implied exit multiple) reproduces the real pulled numbers almost exactly. This is
  what makes it a genuine improvement over `lbo.py`'s single-anchor approach, and it's also the
  built-in regression test: `lbo_spine.py` has no other tie-out, so if this reproduction property
  ever breaks, something in the math is wrong (this happened once already — see
  `_base_interest_schedule`'s docstring for why the interest-expense wedge has to be a declining
  year-by-year schedule, not one flat number, to keep it consistent with the simulated debt sweep).
  Levered FCF is the one row the spine doesn't give components for (interest vs. taxes/capex/NWC),
  so `_base_interest_schedule` backs out an implied base interest schedule from
  `fixed_assumptions.opening_debt`/`entry_leverage` and holds the rest of the EBITDA-to-FCF wedge at
  the base case's own ratio — a disclosed approximation, not a hidden one.

**Neither engine is a tie-out engine.** Both are the standalone-app equivalent of the old skill's
Stage 6 "lightweight shadow model it generates itself" — a reusable generic/spine-anchored engine
instead of bespoke Python per deal, because a deployed app can't safely write and run fresh code per
request the way Claude Code could, and (importantly) **neither runs an LLM per Monte Carlo path** —
both are deterministic Python run 10,000 times, same as any real Monte Carlo engine. See `lbo.py`'s
own docstring for exactly what this trades away against a deal-specific `shadow_model.py` (e.g.
RxVantage's year-by-year rep-cohort build, SPS Commerce's churn/downsell/upsell ARR waterfall).

### `engine/` — a separate, still-unimplemented, more rigorous artifact

`spine.py`/`sample.py`/`tieout.py` are the hand-built, tied-out-against-the-real-Excel engine from
the original three-component plan (`docs/04-build-plan-claude-code-three-components.md`). Answer
key: `cases/rightway/Rightway Model v30.xlsx`, cell `AVP!K35` (IRR) / `K36` (MoM) — 20.6% / 2.3x,
within 10bps / 0.02x at every variable's P50. Neither the old skill's Stage 6 nor
`simulation_service`'s generic engine replaces this; it remains a distinct, harder job.

### `cases/` — reference worked deals, not sample data to discard

`rightway/`, `rxvantage/`, `sps-commerce/` are real deals this whole pipeline was tested against
end-to-end (deck + model → variable card → simulation). Their `variable_card.draft.yaml` /
`shadow_model.py` files are the source of the actual variable-taxonomy/distribution/correlation
judgment calls made for real materials — port from their reasoning rather than re-deriving it from
scratch when extending `app/prompts.py` or `simulation_service`.

### `skills/monte-carlo-model/` — superseded primary path, retained as methodology source

`SKILL.md` documents the full seven-stage pipeline (ingest → first-pass proposal → challenge
conversation → editable widget → confirm → shadow model → IC slide) this app ports from. Still the
most complete written statement of the underwriting judgment; `app/prompts.py` should track it.

## House rules (project-specific — not generic advice)

1. **Never invent a number that isn't in a cited source, for continuous variables.** Binary events
   are the deliberate exception — a reasoned, shown-its-work first-pass probability/impact estimate
   is expected there, not forbidden (see `schemas/variable_card.schema.yaml`'s top-of-file note).
2. **Every exact number (a tie-out delta, a correlation repair, a percentile) comes from a script
   that actually ran** — never asserted in a reply, never eyeballed from a small sample when the
   full array is available.
3. **Source deal workbooks (e.g. `Rightway Model v30.xlsx`) are read-only.** Read cell values via
   `openpyxl`; never write back into one. (Note: some real workbooks carry a malformed
   `Print_Titles` defined name that crashes openpyxl's default parser — `app/ingest.py` patches
   `WorkbookParser.assign_names` to skip it rather than fail the whole read; reuse that patch
   rather than re-solving it if it recurs elsewhere.)

## Known environment gotchas on this Windows setup

- Bare `python`/`python3` on PATH may resolve to a Windows Store "App Execution Alias" stub, not a
  real interpreter — it prints a Store-install message rather than erroring, so check output, not
  just exit code. The real interpreter in this environment lives at
  `C:\Users\Lawrence.Young\AppData\Local\Programs\Python\Python313\python.exe`.
- Git Bash's `curl -F "field=@path;type=..."` silently fails the file read on this setup (`curl:
  (26) Failed to open/read local data from file/application`) — drop the `;type=...` suffix rather
  than debugging the path.
- `claude` being logged in in one terminal (e.g. a PowerShell window) does NOT mean `shutil.which
  ("claude")` finds it from a different shell's process on the same machine (e.g. this Cowork
  session's own Bash tool) — PATH is per-shell here, but the CLI's login state itself is not tied to
  which shell started it. If a native call fails with "Not logged in" despite the user swearing
  they already logged in, check `_resolve_cli_path()`'s result first before assuming the login
  itself is the problem — it's very often just PATH, and the SDK's bundled-first default will
  silently run an unauthenticated copy instead of erroring.
- Calling the real `claude-agent-sdk` `query()` repeatedly against a hung/slow call and letting the
  HTTP client (e.g. `curl --max-time`) time out and disconnect does NOT kill the underlying
  subprocess — orphaned `claude.exe` processes accumulate (23 were found running simultaneously
  from repeated test attempts in one debugging session). Check `tasklist //FI "IMAGENAME eq
  claude.exe"` if native calls seem to be getting slower over a session, and clean up orphans
  rather than letting them compound.
