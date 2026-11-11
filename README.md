# Monte Carlo Underwriting Agent

FP AI Summer Challenge, Team 13 (Emilio, Katrina, Lawrence).

Turns a deal's business materials (deck/CIM) and financial model (Excel) into a Monte
Carlo-simulated LBO view an IC can actually interrogate -- a distribution of outcomes with every
driver's contribution ranked, instead of a single hand-picked base/upside/downside case. Every
number traces back to a citation in the source materials; nothing is invented.

**The standalone app (`app/` + `simulation_service/`) is the primary deliverable.** There's also a
`skills/monte-carlo-model/` Claude Code skill from an earlier phase of the project -- it's
superseded as the thing being shipped, but its methodology (variable taxonomy, distribution
defaults, correlation tiers, binary-event rules) is what `app/prompts.py` ports from. See
`CLAUDE.md` for the full architecture writeup; this file is just getting it running.

## Running the app locally

```
pip install -r simulation_service/requirements.txt -r app/requirements.txt
uvicorn app.main:app --reload --port 8009
```

Open `http://127.0.0.1:8009` in a browser. The workflow is seven stages: Ingest (upload a deck PDF
and/or a model `.xlsx`) -> Propose (AI drafts a variable card) -> Chat (challenge/edit it) ->
Confirm -> Extract base-case financials -> Confirm -> check deterministic LBO mechanics against the
real model (public-company deals can be driven off an assumed offer price per share instead of a
typed-in TEV) -> Confirm -> run a 10,000-path Monte Carlo simulation -> export an IC-ready slide.

**No real deal materials needed to try it out** -- "Use demo card" and "Use demo financials" fill in
clearly-labeled placeholder data so you can exercise the whole pipeline (editing, correlation,
simulation, slide export) without an AI call or a real deck/model. Everything from Ingest through
Simulate also works with zero LLM access configured; only Propose, Chat, and the two Stage
5/5b extraction buttons need one of the two options below.

### Getting LLM access -- two ways, and the app auto-picks between them

**Native (default, no API key needed)** -- the app shells out to your OWN, already-logged-in
`claude` CLI via `claude-agent-sdk`, so the AI-backed stages run on your enterprise Claude Code seat
with zero extra setup, *as long as `claude` is already on your PATH and logged in* (the same
`claude` you'd use to open this repo in Claude Code).

- Check it's being picked up: `curl http://127.0.0.1:8009/api/health` -- look for
  `"native_cli_on_path": true`. If it's `false`, `claude` isn't on *this terminal's* PATH even if
  it's installed elsewhere -- open a terminal where the plain `claude` command already works, and
  run the app from there instead. (On Windows, a terminal opened by an IDE or another tool can have
  a different PATH than the one you logged in from, even on the same machine.)
- If native calls fail with something like *"Not logged in -- please run /login"*, run `claude` once
  in that terminal and log in, then retry.

**Direct API key (fallback, or force it with `LLM_BACKEND=api`)** -- the app calls the Anthropic API
directly. Needs `ANTHROPIC_API_KEY` set -- copy `.env.example` to `.env` in this directory, fill in
your key, and **never commit `.env`** (it's gitignored). Or set it as a real environment variable.

By default (`LLM_BACKEND=auto`) the app tries native first and only falls back to the API key if
native genuinely isn't available -- so local iteration needs no key at all.

## Testing it works

```
python simulation_service/smoke_test.py
```

Runs the generic Monte Carlo engine against `cases/rxvantage/deal_config.yaml` (if you have the
`cases/` materials locally -- see below) and prints percentiles / driver ranking for a sanity check.
There's no other automated test suite yet.

## What's NOT in this repo

`cases/` and `materials/reference-decks/` -- the real FP deal materials (decks + Excel models) this
pipeline has been tested against -- are gitignored on purpose. They're genuinely confidential
company financials, not sample data. If you have access to them separately, drop your own copies
into those folders locally (the paths `app/ingest.py` and `simulation_service/smoke_test.py` expect
are documented in `CLAUDE.md`); otherwise use the demo-card path above to try the app with no real
materials at all.

`app/saved_projects/*.json` is also gitignored -- it's where "Save project" writes your own local
session data (which can include real extracted deal content if you've ingested real materials), not
something to share between machines.

## Where everything is

```
app/                     <- the FastAPI backend + single-page frontend (the actual app)
  main.py                   wires everything together behind /api/*
  llm.py                    native CLI vs. API key backend selection
  ingest.py                 PDF/Excel extraction
  prompts.py                the ported skill methodology (keep in sync with skills/monte-carlo-model)
  slide_export.py           builds the IC slide against the FP template
  static/index.html         the whole frontend, no build step
simulation_service/      <- the generic parametric Monte Carlo engine (the math)
  engine.py                 orchestrates distributions -> correlation -> events -> LBO -> stats
  lbo.py / lbo_spine.py     the two LBO chains (anchor-based vs. confirmed-spine-based)
  smoke_test.py              the one existing automated check
engine/                  <- a SEPARATE, more rigorous, still-unimplemented artifact: a hand-built,
                             tied-out-against-the-real-Excel engine from an earlier plan. Not the
                             same as simulation_service/'s generic engine. See CLAUDE.md.
schemas/                 <- frozen file formats (deal_config.yaml, results.json)
skills/monte-carlo-model/ <- the superseded Claude Code skill; methodology reference, see above
docs/                     <- the planning history, in order (00 through the latest)
CLAUDE.md                 <- the fuller architecture writeup and house rules -- read this next
```

## Known environment gotchas

See `CLAUDE.md`'s "Known environment gotchas" section -- covers the Windows Store `python` alias
stub, Git Bash's `curl -F` file-upload quirk, and the native-CLI-PATH issue above in more detail.
