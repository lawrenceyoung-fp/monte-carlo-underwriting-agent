---
name: monte-carlo-model
description: >
  /monte-carlo-model. The full underwriting-simulation pipeline in one command, one skill, no
  hand-off to a separate skill for the slide — nobody runs the variable-card step without wanting
  the finished IC slide at the end. Feed it an IC deck, a deal model, and any other materials; it
  reads them, takes a reasoned first pass at the 3-10 continuous variables worth flexing (ranges,
  distribution shapes, at-most-one correlation each, with commentary) PLUS actively screens for the
  most impactful binary/discrete events (recession, regulatory action, loss of a large customer
  contract, or other) and proposes reasoned first-pass probability and revenue/margin/FCF impact
  estimates for whichever are most material to this specific deal. Runs a structured challenge
  conversation with the deal team, lands on an editable variable-card widget (add/remove variables
  and events, save/load versions, export to YAML or Excel), and — once confirmed — writes
  deal_config.yaml, generates and runs a lightweight Python shadow model, and builds the
  FP-branded IC slide (histogram + stats, variable register, contribution analysis). Worked
  example: cases/rightway/variable_card_widget.html, cases/rightway/variable_card.draft.yaml, and
  cases/rightway/shadow_model.py.
---

# /monte-carlo-model — from IC materials to a finished IC slide, one skill, no hand-off

## What this is

One command, seven stages, two frozen files in the middle (`deal_config.yaml`, `results.json`) —
but ONE skill owns all seven stages. There is no separate "build the slide" skill to call into.
That used to be `skills/build-ic-page`; it has been folded into Stage 7 below and no longer exists
as its own skill, because nobody invokes the variable-card step in isolation — the only reason to
run this at all is to end up with a slide.

```
Stage 1  Ingest deck + model + materials
Stage 2  Claude's first-pass proposal: continuous variables (cited) + screened binary events (reasoned estimates)
Stage 3  Guided challenge conversation (six standing questions)
Stage 4  Editable variable-card widget (add/remove variables AND events, versions, export)
Stage 5  Confirm  ->  deal_config.yaml
Stage 6  Generate + run a lightweight Python shadow model  ->  results.json
Stage 7  Build the FP-branded IC slide (same skill, same run)
```

**Honest scope note on Stage 6:** the script this stage writes is a *lightweight shadow model*
(the workflow spec's "Mode A"), generated directly from the confirmed variable card. It is not a
substitute for a real tie-out against the deal's own Excel model (Mode B) — see
`docs/00-original-workflow-spec.md` section 3.2 and `engine/tieout.py` (a separate, more rigorous,
hand-tied-out engine that this skill's lightweight model does not replace). Say this out loud when
Stage 6 finishes; don't let a plausible-looking distribution stand in for a validated one.

## Stage 1 — Ingest

Read every document handed to the skill: the IC deck, the deal model (via openpyxl, read-only,
when file access exists — if it doesn't in this environment, say so explicitly rather than
fabricating a cell reference), and anything else supplied. Look specifically for:

- **A case table** (base/upside/downside or equivalent) — map downside→P10, base→P50, upside→P90.
- **Concentration and cyclicality signals** — customer concentration tables, macro/RIF commentary,
  regulatory timelines. These feed Stage 2's binary-event screen below.
- **Anywhere the materials explain *why* outcomes diverge** — correlation and structural-link
  evidence.
- **Rule-of-thumb / curve-style framing** — flag as a structural candidate, not a correlation.

**Re-running on a deal that already has saved versions:** check `cases/<deal>/` for
`variable_card.v*.yaml` or `.json` files first. If any exist, ask whether to load the latest (or a
named) version and skip straight to Stage 5/6 instead of re-ingesting from scratch.

## Stage 2 — Claude's first pass

### Continuous variables

Apply the six-family taxonomy (`market_macro`, `revenue_build`, `margin_cost`, `cash_conversion`,
`exit_structure`, `discrete_events`) and propose **3 to 10 variables** — no fewer, no more. For
each: id, name, family, unit, type, `elicitation` [P10, P50, P90], a distribution pick, bounds,
`source`, `drives`, **AT MOST ONE** `correlated_with`, and `commentary`.

| Variable character | Default distribution |
|---|---|
| Growth rates, margin deltas, symmetric operational levers | PERT |
| Multiples, pricing, anything strictly positive and right-skewed | Lognormal |
| Churn, retention, utilization (bounded 0-1) | Beta |
| Exit timing | Discrete PMF |
| No basis to have a view at all | Uniform |

`commentary` is mandatory on every proposed variable: why this range, why this distribution, why
this correlation (or why none). A citation alone doesn't carry that reasoning.

**Correlation model — intentionally simplified.** One correlation slot per variable, not a general
matrix. If the materials support more relationships than one slot can hold, pick the
strongest/best-evidenced one and say so in `commentary` — do not silently drop the others.

Any continuous variable with no case-specific split or no probability anywhere in the materials is
left blank (`null`), not guessed.

### Binary/discrete events — actively screen, don't wait for the deck to hand you one

Every deal gets screened against three standing categories, regardless of whether the materials
happen to case-table one out for you:

1. **Recession / macro downturn** — does revenue depend on something that contracts in a downturn
   (headcount-linked pricing, discretionary spend, cyclical end markets)?
2. **Regulatory action** — is there a pending rule, mandate, or legislative change that could
   reshape the economics (a compliance deadline, a pricing-model mandate, an approval gate)?
3. **Loss of a large customer/contract** — does customer concentration data show a single account
   or small group large enough that losing one would visibly move the numbers?

**Screen all three, but don't mechanically add all three.** For each category, use whatever the
materials disclose (concentration tables, RIF scans, regulatory timelines, renewal schedules,
management commentary) to judge whether it's material enough to model for *this* deal, then
propose binary events for the ones that clear that bar — typically one or two, not three. For any
category screened out, say why in that category's would-be event's place — either as a
`challenge_list` entry with the reasoning, or inline in another event's `commentary` if the
materials themselves offer a mitigant (e.g., "recession risk is disclosed but the deck's own RIF
scan found announced layoffs immaterial to date, and client mix is diversified — deprioritized
versus the customer-concentration risk, which is directly sized").

**For every event actually proposed, take a real first-pass estimate — do not default to null:**

- `p_success`: a reasoned probability, using whatever base rate or disclosed signal is available
  (comparable churn/incidence rates, the materials' own qualitative signal, disclosed NPS/retention
  data, a regulatory timeline's stated milestones). `p_success_basis` defends this specific number.
- `impact.revenue_pct` / `impact.margin_pct` / `impact.fcf_pct`: sized against real disclosed
  anchors where possible (a concentration percentage, a segment's revenue base, a comparable
  cost-structure fact) — a rough, reasoned magnitude beats no magnitude.
- `commentary`: ties it together — why this event, why deprioritized the other two categories (or
  why not), why this probability, why these impact numbers.

This is a **different rule** from the continuous-variable one above — see the schema's top-of-file
note on the two "never invent a number" rules. A shown-its-work estimate is not the same thing as
silently inventing a number; the whole point of `commentary` is to make the estimate arguable.

**Screen for shape, not just category — `shock_type`.** Most events are `permanent` (a one-time
step-change that persists once it fires: losing a customer, a regulatory shift taking hold). Some
are `temporary_with_recovery` (hit hard, then fade — a pandemic-style access shock is the canonical
case). Forcing a temporary shock into a single flat `impact` number is wrong in a way that matters:
either it understates the acute damage or overstates the long-run damage. When a screened event is
this shape, set `shock_type: temporary_with_recovery` and fill both `acute_impact` (the severe,
short-lived hit, with a `duration_years`) and `impact` (the milder RESIDUAL effect that persists
after recovery, through exit) — these are two different numbers, not the same one twice. Flag
explicitly if the deal has real leverage: a severe acute EBITDA collapse can threaten covenant/
liquidity coverage in a way a flat percentage on terminal value doesn't capture — say so in
`commentary` even if Stage 6 doesn't yet fully model the mechanical breach check.

## Stage 3 — The guided challenge conversation

Six standing questions, asked in this order, always grounded against the Stage 2 proposal:

1. **Biggest uncertainties.** "Of everything in here, what's the one thing that, if it goes wrong,
   actually breaks the thesis?" Compare against Stage 2's widest range, least-sourced variable, or
   highest-impact binary event.
2. **Market-driven vs. operational.** Walk each proposed variable and get it tagged.
3. **Are the ranges reasonable?** "Would you bet real money the outcome lands inside this P10-P90
   band eight times out of ten?" Press hardest on anything sourced from a deck's own
   downside/base/upside columns.
4. **Discrete vs. continuous, and are the right binary events even on the card?** Confirm anything
   phrased as "if X happens" is `bernoulli`, not forced into a continuous shape. Also explicitly
   ask: "did we pick the right one or two events out of recession/regulatory/customer-loss? Is
   there a specific customer or regulatory deadline you're more worried about than what's here?"
5. **What moves together.** Walk each variable's single `correlated_with` slot using the fixed tier
   ladder (unrelated 0.00 / loosely related 0.20 / moderately linked 0.40 / strongly linked 0.65 /
   move together 0.85).
6. **The synthesis question.** "If you had to name the three things that actually determine whether
   this investment works, what are they?" Check each is represented in the card, the challenge
   list, or the cut list, with a reason.

Ask, listen, adjust the proposal, then move to the next question — do not batch into one message.

## Stage 4 — The editable variable-card widget

A real interactive artifact. Two rendering paths — use whichever the environment has:

**Path A — an environment with an interactive-widget tool.** Render the same design via that tool;
read live state back only if the environment's tooling actually supports it for that widget type —
verify before relying on it, and fall back to Path B's export/re-import mechanism otherwise.

**Path B — plain Claude Code, no widget tool (where this plugin actually ships).** Generate/update
`cases/<deal>/variable_card_widget.html` from `skills/monte-carlo-model/assets/variable_card_template.html`.
The rendered page must give the team, per **variable**: add/delete, family/distribution dropdowns,
P10/P50/P90 fields, a single correlated-with dropdown + tier, a `commentary` textarea, a source
field. And, per **binary event**: add/delete, a category dropdown (recession / regulatory /
customer_loss / other), a probability field (pre-filled with Claude's reasoned estimate, visually
distinguished from a genuinely-blank required field), three impact fields (revenue/margin/FCF %),
a `commentary` textarea, a source field.

Plus, for the whole card: **Save version** (JSON snapshot), **Load version** (re-populate from a
saved JSON file), **Export YAML**, **Export Excel** (`.xlsx` via SheetJS, one sheet each for
variables and binary events), **Confirm variable card** (prompts for a confirmer name, marks
everything confirmed, exports the `deal_config.yaml`-shaped file).

See `cases/rightway/variable_card_widget.html` for the worked example.

## Stage 5 — Confirm → `deal_config.yaml`

The widget's Confirm button is the actual gate. Once confirmed, reconcile into `deal_config.yaml`
(`schemas/deal_config.schema.yaml`): each variable's `correlated_with` becomes a `correlations`
entry; each binary event (with its category, probability, and impact/on_failure data) becomes an
event block; `confirmed_by` is set from the name given at confirm time. Anything still blank blocks
promotion.

## Stage 6 — Generate and run the lightweight Python shadow model

From the confirmed `deal_config.yaml`, write `cases/<deal>/shadow_model.py` implementing the shadow
LBO chain (revenue → gross profit → EBITDA → FCF → exit → IRR/MoM, simplified to whatever subset
the confirmed variables actually cover):

- Build the correlation structure from each continuous variable's single link.
- Draw N=10,000 correlated paths; inverse-CDF into each variable's marginal.
- For each confirmed binary event, draw its own Bernoulli(p_success) per path, independently of the
  others, and on the adverse branch apply EITHER its `on_failure` variable shifts OR its generic
  `impact.revenue_pct` / `impact.margin_pct` / `impact.fcf_pct` adjustments (or both, if both are
  set) to that path. For a `shock_type: temporary_with_recovery` event, this is two distinct
  adjustments over time, not one: apply `acute_impact` for `duration_years`, then ease to the
  milder `impact` (the residual) for the remainder of the hold — a single-point terminal-year
  shortcut cannot represent this correctly, so this event shape requires an actual year-by-year (or
  at least multi-period) build, not the single-formula compounding shortcut that's fine for
  permanent-shift events. If the deal carries real leverage, also check whether the acute window's
  EBITDA collapse would breach interest coverage on any path, and record that as a flag in
  `results.json` even if the return impact of an actual breach isn't fully modeled.
- Compute IRR/MoM per path, percentiles, the driver ranking, and the top-decile-vs-bottom-decile
  input profile.
- Write `results.json` per `schemas/results.schema.json`.

**Then actually run it yourself, via the Bash tool — this is a standing, automatic part of Stage 6,
not a follow-up the user is expected to do by hand.** The whole point of building this as a Claude
Code skill rather than a document is that Claude has a real terminal available in the environment
this plugin ships to; use it rather than handing the user a `pip install` tutorial by default.

1. **Check for a real Python interpreter.** Try `python --version`, then `python3 --version`, then
   `py --version`. On Windows specifically, a bare `python`/`python3` that prints something about
   an "App Execution Alias" or offers to open the Microsoft Store is a STUB, not a real
   interpreter — treat that output as "Python not found," not as success. Don't be fooled by exit
   code 0 alone; read what it actually printed.
2. **If found:** check for `numpy` with `python -c "import numpy"`; if that fails, `pip install
   numpy scipy` (scipy is optional but improves distribution-fitting accuracy — see
   `draw_variable`'s fallback path in the script). Then run the script itself — `cd` into
   `cases/<deal>/` first, since it writes `results.json` to its own working directory — and capture
   both stdout and the exit code.
3. **If NOT found:** do not silently give up, and do not silently install anything system-level.
   Tell the user Python is missing and ask before taking either path: (a) installing it via the
   platform's package manager (`winget`, `brew`, `apt`) — flag explicitly if that install flow
   requires accepting a license or store agreement, since accepting that on the user's behalf is
   out of bounds regardless of how routine the install otherwise is; (b) walking the user through a
   manual install instead, if they'd rather do that themselves. Offer both, let them pick.
4. **After running, read `results.json` back and validate it** — valid JSON, `percentiles` present
   and numeric, `run_manifest.generated_at` timestamped just now — before reporting anything from
   it. A script that exits 0 but writes a malformed, empty, or stale file is not a successful run;
   catch that before it becomes a false "it worked."
5. **Report the run status plainly either way.** "Ran successfully — here's what it produced, IRR
   p50 of X%" is one valid outcome. "Could not run because Y, here's exactly what's blocking it and
   what I need from you to unblock it" is the other. Never present unexecuted numbers as if a real
   run produced them — that is the entire reason this step is automatic rather than a document
   telling the user how to do it themselves.

## Stage 7 — Build the FP-branded IC slide (same skill, same run)

Read `results.json` and `deal_config.yaml` — no other input, no hand-editing after. Build one
slide, using the `fp-ppt-template` skill for FP branding rather than styling from scratch:

1. **Histogram + summary statistics, top of the slide.** IRR distribution histogram with
   P10/P25/P50/P75/P90 marked, the deck's own Base/Upside/Downside case IRRs pinned on as vertical
   reference lines, and a parallel MoM read. A probability table alongside: P(IRR < 0%),
   P(IRR < 15%), P(IRR > 25%), P(MoM ≥ 2.0x), P(MoM ≥ 3.0x), P(capital loss). One sentence of shape
   read (compressed vs. barbell, P90-minus-P10 spread, skew).
2. **Variable register.** One row per variable in `deal_config.yaml`: name, family, P10/P50/P90,
   distribution, its correlated-with link, source. Every confirmed binary event as its own line:
   category, probability, and its revenue/margin/FCF impact.
3. **Contribution analysis.** Ranked list from `results.driver_ranking` (rank-correlation of each
   variable's draws to output IRR), paired with the top-decile-vs-bottom-decile input profile —
   the "what has to go right together" read, not just a single-lever ranking.

This skill's job ends at a presentable slide with no hand-editing. Run it on two different
`results.json` files and confirm neither needed a manual fix.

**Fallback if time runs out:** per the original build plan, the slide is the lowest-weighted piece
of the whole pipeline. A screenshot of a chart pasted into the deck by hand costs almost nothing in
scoring if Stage 6 holds. Don't trade the engine's correctness for finishing this polish.

## The three house rules

1. **Never invent a number that isn't in a cited source or an explicit team decision** — for
   continuous variables. **Binary events get a different, explicit carve-out:** a reasoned,
   shown-its-work first-pass estimate for probability and impact is expected, not forbidden — see
   the schema's top-of-file note on the two different rules.
2. **`source` and `drives` are mandatory on every variable. Every EXACT number (a tie-out delta, a
   percentile, a correlation repair) comes from a script that actually ran, never asserted in a
   reply.**
3. **The deal model is read-only.** Read via openpyxl; never write back into it.

## What "working" looks like

A variable card with 3-10 continuous rows (every source real, every `commentary` explaining actual
reasoning) plus one or two binary events chosen from a real screen of all three categories (not
just whatever the deck happened to hand over), each with a defended probability and impact
estimate; a `deal_config.yaml` a fresh read of the materials would validate line by line; a
`shadow_model.py` that Claude actually ran itself via Bash and produced a real, validated
`results.json` from — with "couldn't run it, here's why and here's what I need from you" as the
honestly-flagged exception, not the default outcome; and a slide that traces every number on it
back to one of those two files.
