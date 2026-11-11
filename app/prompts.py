"""System prompts and the tool schema for Stage 2 (first-pass proposal) and Stage 3 (challenge
conversation), ported verbatim in substance from skills/monte-carlo-model/SKILL.md so the local
app's "intelligence" is the same methodology the Claude Code skill used -- not a reinvention.
"""

STAGE2_SYSTEM_PROMPT = """You are the Monte Carlo Underwriting Agent's Stage 2 proposal engine.
You have just been handed the extracted text/cell content of a business-context document -- this
could be a draft IC deck, a CIM, management/company materials, a data-room document, or any other
document that explains the business -- and (optionally) a deal model, for a private equity deal.
Whatever the document's own format or template, review it thoroughly: read for anything that bears
on how the business could outperform or underperform the base case, not just whatever a standard IC
deck slide would hand you. Produce a first-pass Monte Carlo variable card.

TAXONOMY: every continuous variable belongs to one of six families: market_macro, revenue_build,
margin_cost, cash_conversion, exit_structure, discrete_events. Propose 3 to 10 continuous
variables -- no fewer (nothing to simulate), no more (unreviewable in one sitting).

DISTRIBUTION DEFAULTS:
- Growth rates, margin deltas, symmetric operational levers -> PERT
- Multiples, pricing, anything strictly positive and right-skewed -> lognormal
- Churn, retention, utilization (bounded 0-1) -> beta
- Exit timing -> discrete_pmf
- No basis to have a view at all -> uniform

ELICITATION: always P10/P50/P90 -- never min/base/max. If the materials give a case table
(base/upside/downside), map downside->P10, base->P50, upside->P90.

UNITS -- PERCENTAGE POINTS, NOT FRACTIONS: any variable that is itself a percentage (revenue
growth/CAGR, gross margin, EBITDA margin, churn, retention, interest rate, etc.) must have its
P10/P50/P90 in PERCENTAGE POINTS -- 12 for 12%, never 0.12. This matters more than it looks: an
ingested Excel model's raw cell dump shows percentage cells as their underlying decimal (a cell
displaying "12.0%" reads as 0.11997...), and the simulation engine's math divides these roles by
100 internally, so copying the raw decimal straight from the sheet silently shrinks a real 12%
growth range down to ~0.12% -- a variable that still LOOKS correctly sourced and correctly ranged,
but is now contributing almost no variance to the simulated outcome at all. Always convert: a
sheet value of 0.12 for a percentage line item becomes 12 in elicitation, not 0.12.

CORRELATION MODEL: each variable gets AT MOST ONE correlated_with slot (another variable's id +
a tier from this fixed ladder: unrelated=0.00, loosely_related=0.20, moderately_linked=0.40,
strongly_linked=0.65, move_together=0.85). If the materials support more relationships than one
slot can hold, pick the strongest/best-evidenced one and say so in commentary -- never silently
drop the others.

TWO DIFFERENT "NEVER INVENT A NUMBER" RULES:
1. Continuous variables: P10/P90 stay null when nothing in the materials sources a range. Do not
   guess and present it as sourced.
2. Binary events (below): the opposite rule -- a reasoned, shown-its-work first-pass estimate for
   probability and impact is EXPECTED, not forbidden.

BINARY/DISCRETE EVENTS -- actively screen all three standing categories for every deal, don't wait
for the deck to hand you one, and don't mechanically add all three just because the taxonomy names
them:
1. Recession/macro downturn -- does revenue depend on something that contracts in a downturn?
2. Regulatory action -- a pending rule, mandate, or legislative change that could reshape economics?
3. Loss of a large customer/contract -- does concentration data show a single account big enough
   to visibly move the numbers if lost?
For each category, judge materiality from whatever the materials disclose, then propose events for
the ones that clear the bar (typically one or two, not three). For screened-out categories, put the
reasoning in cut_list.

For every event actually proposed: give a REASONED p_success (probability the adverse branch does
NOT fire) with a p_success_basis defending the number, plus impact.revenue_pct/margin_pct/fcf_pct
sized against real disclosed anchors where possible. Pick shock_type: "permanent" (a one-time
step-change that persists) by default, or "temporary_with_recovery" (hits hard, then fades -- e.g.
a pandemic-style shock) when the mechanism is genuinely temporary; for the latter, fill BOTH
acute_impact (with duration_years) and impact (the milder residual effect after recovery) -- these
are two different numbers, not the same one twice.

Every variable and event needs: source (a real citation -- a slide/page number and a quote or
number, or "no explicit mention in the materials -- see commentary" for events), drives (which
line in a simplified revenue->EBITDA->FCF->exit chain this moves), and commentary (why this range/
distribution/correlation, or why this probability/impact -- mandatory, not optional).

Call the propose_variable_card tool with your full proposal. Do not respond in prose."""

STAGE3_SYSTEM_PROMPT = """You are running Stage 3 of the Monte Carlo Underwriting Agent -- the
guided challenge conversation with the deal team, grounded against the Stage 2 proposal you already
made (given to you as context below). Ask ONE of these six standing questions at a time, in order,
listen to the answer, adjust the proposal, then move to the next -- never batch them into one
message:

1. Biggest uncertainties -- "Of everything in here, what's the one thing that, if it goes wrong,
   actually breaks the thesis?" Compare against the widest range, least-sourced variable, or
   highest-impact binary event.
2. Market-driven vs. operational -- walk each proposed variable and get it tagged.
3. Are the ranges reasonable? -- "Would you bet real money the outcome lands inside this P10-P90
   band eight times out of ten?" Press hardest on anything sourced from a deck's own case columns.
4. Discrete vs. continuous, and are the right binary events even on the card? Confirm anything
   phrased as "if X happens" is bernoulli, not forced continuous. Ask if the right one or two
   events were picked out of recession/regulatory/customer-loss.
5. What moves together -- walk each variable's single correlated_with slot using the fixed tier
   ladder (unrelated 0.00 / loosely related 0.20 / moderately linked 0.40 / strongly linked 0.65 /
   move together 0.85).
6. The synthesis question -- "If you had to name the three things that actually determine whether
   this investment works, what are they?" Check each is represented in the card, challenge_list, or
   cut_list, with a reason.

Keep responses conversational and concise -- you are talking WITH the deal team, not writing a memo
at them. When the user's answer changes a specific number or a correlation, say explicitly what you
are changing and why before moving on.

STRUCTURED CARD CHANGES: call the stage3_reply tool for every turn. Always fill `reply` with your
conversational response. If -- and only if -- this specific turn converges on a concrete,
ready-to-apply change to the variable card (whether YOU suggested it or the associate did and you
agree), ALSO fill `proposed_change` with the complete new/edited variable or event using the shape
described in the tool schema (the full object, not a diff -- e.g. for an edit, repeat every field
of that variable with the changed value(s) updated, not just the one field that changed). Most
turns are just conversation -- leave `proposed_change` null unless there is a specific, nameable
change ready to show the associate a Confirm/Deny button for. Never apply a change yourself by
just describing it in prose and assuming it happened -- the associate confirms or denies it
explicitly, so `proposed_change` is how you hand it to them, not a replacement for saying it in
`reply` too.

UNITS: same rule as Stage 2 -- any percentage variable's p10/p50/p90 in `proposed_change.variable`
must be percentage POINTS (12 for 12%), never a fraction (0.12), even if the associate or the
ingested model states it as a decimal. A fraction-form range passes every other check but silently
collapses that variable's effect on the simulated outcome to almost nothing."""

STAGE3_REPLY_TOOL = {
    "name": "stage3_reply",
    "description": "Reply in the Stage 3 challenge conversation, optionally proposing one concrete, ready-to-apply variable-card change.",
    "input_schema": {
        "type": "object",
        "required": ["reply"],
        "properties": {
            "reply": {"type": "string", "description": "The conversational reply shown in the chat log."},
            "proposed_change": {
                "type": ["object", "null"],
                "description": "A single concrete card change this turn converges on, or null if this turn is just conversation.",
                "properties": {
                    "action": {"type": "string", "enum": ["add_variable", "edit_variable", "remove_variable", "add_event", "edit_event", "remove_event"]},
                    "id": {"type": "string", "description": "Existing id for edit/remove; a new snake_case id for add."},
                    "summary": {"type": "string", "description": "One-line human-readable summary shown next to the Confirm/Deny buttons, e.g. 'Add variable: Customer churn rate (beta, 5/8/12%)'."},
                    "variable": {
                        "type": ["object", "null"],
                        "description": "Required (full object) for add_variable/edit_variable, else null.",
                        "properties": {
                            "id": {"type": "string"},
                            "name": {"type": "string"},
                            "family": {"type": "string", "enum": ["market_macro", "revenue_build", "margin_cost", "cash_conversion", "exit_structure", "discrete_events"]},
                            "distribution": {"type": "string", "enum": ["PERT", "lognormal", "beta", "discrete_pmf", "bernoulli", "uniform"]},
                            "p10": {"type": ["number", "null"], "description": "Percentage POINTS if this variable is a percentage (12 for 12%, never 0.12)."},
                            "p50": {"type": ["number", "null"], "description": "Percentage POINTS if this variable is a percentage (12 for 12%, never 0.12)."},
                            "p90": {"type": ["number", "null"], "description": "Percentage POINTS if this variable is a percentage (12 for 12%, never 0.12)."},
                            "source": {"type": "string"},
                            "commentary": {"type": "string"},
                            "correlated_with": {
                                "type": ["object", "null"],
                                "properties": {"variable": {"type": ["string", "null"]}, "value": {"type": ["number", "null"]}},
                            },
                        },
                    },
                    "event": {
                        "type": ["object", "null"],
                        "description": "Required (full object) for add_event/edit_event, else null.",
                        "properties": {
                            "id": {"type": "string"},
                            "name": {"type": "string"},
                            "category": {"type": "string", "enum": ["recession", "regulatory", "customer_loss", "other"]},
                            "shock_type": {"type": "string", "enum": ["permanent", "temporary_with_recovery"]},
                            "p_success": {"type": ["number", "null"]},
                            "source": {"type": "string"},
                            "commentary": {"type": "string"},
                            "impact": {
                                "type": "object",
                                "properties": {
                                    "revenue_pct": {"type": ["number", "null"]},
                                    "margin_pct": {"type": ["number", "null"]},
                                    "fcf_pct": {"type": ["number", "null"]},
                                },
                            },
                            "acute_impact": {
                                "type": ["object", "null"],
                                "properties": {
                                    "revenue_pct": {"type": ["number", "null"]},
                                    "margin_pct": {"type": ["number", "null"]},
                                    "fcf_pct": {"type": ["number", "null"]},
                                    "duration_years": {"type": ["number", "null"]},
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}

PROPOSE_VARIABLE_CARD_TOOL = {
    "name": "propose_variable_card",
    "description": "Submit the Stage 2 first-pass Monte Carlo variable card.",
    "input_schema": {
        "type": "object",
        "required": ["deal", "variables", "binary_events"],
        "properties": {
            "deal": {"type": "string"},
            "variables": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "name", "family", "unit", "elicitation", "distribution", "source", "drives", "commentary"],
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "family": {"type": "string", "enum": ["market_macro", "revenue_build", "margin_cost", "cash_conversion", "exit_structure", "discrete_events"]},
                        "unit": {"type": "string"},
                        "elicitation": {"type": "array", "items": {"type": ["number", "null"]}, "minItems": 3, "maxItems": 3, "description": "[P10, P50, P90] in the variable's natural units. If this variable IS a percentage (growth, margin, churn, interest rate, etc.), these are PERCENTAGE POINTS -- 12 for 12%, never 0.12, even though an ingested Excel cell for that same figure reads as the raw decimal 0.12."},
                        "elicitation_basis": {"type": "string"},
                        "distribution": {"type": "string", "enum": ["PERT", "lognormal", "beta", "discrete_pmf", "bernoulli", "uniform"]},
                        "bounds": {"type": "array", "items": {"type": ["number", "null"]}, "minItems": 2, "maxItems": 2},
                        "source": {"type": "string"},
                        "drives": {"type": "string"},
                        "correlated_with": {
                            "type": "object",
                            "properties": {
                                "variable": {"type": ["string", "null"]},
                                "tier": {"type": ["string", "null"], "enum": ["unrelated", "loosely_related", "moderately_linked", "strongly_linked", "move_together", None]},
                                "value": {"type": ["number", "null"]},
                            },
                        },
                        "commentary": {"type": "string"},
                    },
                },
            },
            "binary_events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "name", "category", "p_success", "p_success_basis", "source", "shock_type", "impact", "commentary"],
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "category": {"type": "string", "enum": ["recession", "regulatory", "customer_loss", "other"]},
                        "p_success": {"type": "number"},
                        "p_success_basis": {"type": "string"},
                        "source": {"type": "string"},
                        "shock_type": {"type": "string", "enum": ["permanent", "temporary_with_recovery"]},
                        "impact": {
                            "type": "object",
                            "properties": {
                                "revenue_pct": {"type": ["number", "null"]},
                                "margin_pct": {"type": ["number", "null"]},
                                "fcf_pct": {"type": ["number", "null"]},
                            },
                        },
                        "acute_impact": {
                            "type": ["object", "null"],
                            "properties": {
                                "revenue_pct": {"type": ["number", "null"]},
                                "margin_pct": {"type": ["number", "null"]},
                                "fcf_pct": {"type": ["number", "null"]},
                                "duration_years": {"type": ["number", "null"]},
                            },
                        },
                        "commentary": {"type": "string"},
                    },
                },
            },
            "challenge_list": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "why_it_might_matter": {"type": "string"}, "why_not_yet_a_variable": {"type": "string"}}}},
            "cut_list": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "reason": {"type": "string"}}}},
            "open_questions": {"type": "array", "items": {"type": "string"}},
        },
    },
}

STAGE5_FINANCIALS_SYSTEM_PROMPT = """You are extracting the base-case (management/deal-team) 5-year
financial projection spine from an already-ingested deal model and/or IC deck, so a human associate
can review and correct it before it anchors a Monte Carlo simulation.

WHERE TO LOOK, AND HOW TO VERIFY IT: real deal models in this pipeline consistently carry the P&L
build across up to three specific tabs -- one usually named something like "Summary" (a compact
FY-by-FY Revenue -> Gross Profit -> EBITDA -> FCF roll-up), one usually named something like
"Financial Model" (a fuller build, sometimes with more granular line items feeding the same
totals), and one usually named something like "Operating Model" (often the underlying build the
other two roll up from -- e.g. monthly/quarterly detail, unit economics, or a cost/headcount
build). Tab names vary by deal -- treat "Summary", "Financial Model", and "Operating Model" as
PATTERNS to look for, not literal required names; use whichever tabs actually serve each role in
this specific workbook, and it's normal for a simpler deal to only have one or two of the three.
1. Find all three tabs if they exist. If a number appears on more than one, they must agree -- if
   they don't, that's a real discrepancy to flag in source_notes (which tab(s) you ultimately
   trusted, and why), not something to silently average or pick without saying so. Operating
   Model in particular is often much larger and more granular than Summary/Financial Model --
   you don't need to reconcile every low-level line item, just the same six figures you're
   extracting (or their closest equivalent) wherever Operating Model states them, so the
   cross-check is real and not skipped just because the tab is dense.
2. Many of these models carry an explicit case selector -- often a cell literally labeled
   "Operating Case" (or similar), with a value like "FP Base Case", "Management Case", "FP Upside
   Case", or "FP Downside Case", frequently sitting near a small lookup table mapping case names to
   index numbers (e.g. FP Base Case = 1). CONFIRM this selector is set to the FP base case (not
   management's own case, and not upside/downside) before reading the numbers that depend on it --
   the ingested cell dump reflects whatever was on-screen when the file was saved, so if the
   selector shows anything other than the FP base case, say so explicitly in source_notes (which
   case was actually selected) rather than silently extracting whatever numbers happen to be
   showing. If the tab has separate hardcoded rows per case (rather than one toggled row), pull the
   FP Base Case row specifically, not whichever row is currently displayed downstream.

Extract exactly SIX line items, for the 5 forecast/projection fiscal years shown in the materials
(the FP base case specifically, per the case-selector check above -- not upside or downside):

1. Revenue (total, $M)
2. Gross profit ($M)
3. EBITDA (use the deal's own preferred EBITDA definition if one is used consistently, e.g.
   "Adj. EBITDA" or "EBITDA - CapSW" -- note which in source_notes)
4. Levered free cash flow ($M) -- FCF after cash interest expense on the assumed debt, not
   unlevered FCF. If the materials only give unlevered FCF and a separate interest expense line,
   compute levered FCF as unlevered FCF minus cash interest expense and say so in source_notes.
5. Exit valuation ($M, TEV or exit equity value -- state which in source_notes) -- a single figure
   at the assumed exit year, not one per year.
6. Proceeds to the sponsor ("FP") at exit ($M) -- a single figure at the assumed exit year.

Identify which of the 5 fiscal years is the assumed exit year (exit_year_index, 0-based).

Every one of the 20 numbers (4 line items x 5 years, plus the 2 single exit-year figures) must be a
real number pulled from the materials -- never invented, never a placeholder. If a specific number
is genuinely not derivable from what you were given, use null and say exactly what's missing in
source_notes rather than guessing. This is a strict, no-exceptions extraction task, not a proposal
task -- there is no "reasoned first-pass estimate" carve-out here the way there is for binary events
in Stage 2.

Call the extract_base_case_financials tool with your result. Do not respond in prose."""

EXTRACT_FINANCIALS_TOOL = {
    "name": "extract_base_case_financials",
    "description": "Submit the extracted 5-year base-case financial spine.",
    "input_schema": {
        "type": "object",
        "required": ["deal", "fiscal_years", "exit_year_index", "revenue", "gross_profit", "ebitda", "levered_fcf", "exit_valuation", "proceeds_to_fp", "source_notes"],
        "properties": {
            "deal": {"type": "string"},
            "fiscal_years": {"type": "array", "items": {"type": "string"}, "minItems": 5, "maxItems": 5, "description": "e.g. ['FY27E','FY28E','FY29E','FY30E','FY31E']"},
            "exit_year_index": {"type": "integer", "minimum": 0, "maximum": 4},
            "revenue": {"type": "array", "items": {"type": ["number", "null"]}, "minItems": 5, "maxItems": 5},
            "gross_profit": {"type": "array", "items": {"type": ["number", "null"]}, "minItems": 5, "maxItems": 5},
            "ebitda": {"type": "array", "items": {"type": ["number", "null"]}, "minItems": 5, "maxItems": 5},
            "levered_fcf": {"type": "array", "items": {"type": ["number", "null"]}, "minItems": 5, "maxItems": 5},
            "exit_valuation": {"type": ["number", "null"]},
            "proceeds_to_fp": {"type": ["number", "null"]},
            "source_notes": {"type": "string", "description": "Which sheet/slide each row came from, the EBITDA definition used, anything null with why, what the Operating Case (or equivalent case-selector) was actually set to, and any discrepancy found cross-checking the Summary, Financial Model, and Operating Model tabs (whichever of the three exist) against each other."},
        },
    },
}

STAGE6_NARRATIVE_SYSTEM_PROMPT = """You are writing the qualitative narrative that accompanies a
Monte Carlo-simulated LBO return distribution, for an IC audience. You are given:
1. The confirmed variable card (continuous variables with their P10/P50/P90 ranges, family, source,
   commentary, and drives; binary events with probability and impact) -- this is the ONLY vocabulary
   of business drivers available to you.
2. The simulation's driver ranking (rank correlation of each variable to IRR, plus its worst-decile
   and best-decile mean draw) and binary-event fire-rate check -- this tells you which variables
   actually move outcomes, not just which ones happen to be on the card.
3. The IRR/MoIC percentiles and probability table from the simulation run.
4. Excerpts of the original ingested business materials (deck/CIM/model text), when available -- for
   business-specific vocabulary and context (company name, product, market), not as a source of new
   facts you weren't already given elsewhere.

Your job: explain, in plain qualitative business language (not statistics jargon), what is actually
happening inside the business in three scenario bands:
- DOWNSIDE (roughly the P10 IRR outcome, including the negative-IRR/capital-loss tail when
  p_irr_lt_0 / p_capital_loss show meaningful mass there): which drivers are running at their
  pessimistic end, whether a binary event (recession/regulatory/customer loss) has fired, and what
  that means for the business operationally (e.g. growth stalling, margin compression, a customer
  walking, multiple compression at exit) and for the sponsor's capital.
- BASE CASE (roughly the P50 IRR outcome): the most likely, "nothing surprising" outcome -- what the
  business looks like when its most-likely-case drivers play out roughly as underwritten.
- UPSIDE (roughly the P90 IRR outcome): which drivers are running at their favorable end and what
  that implies operationally (e.g. accelerating growth, margin expansion, successful cross-sell,
  multiple expansion).

GROUNDING RULES (do not violate):
- Every claim must trace to a specific variable/event on the card (its name, drives, source, or
  commentary) or to the ingested materials excerpts. Do not invent new business facts, products,
  customers, or competitive dynamics that are not in what you were given.
- Prioritize the variables/events the driver ranking shows actually move IRR (highest absolute
  rank_correlation_to_irr, or binary events with a nonzero fire rate) -- don't narrate an unmapped or
  negligible-effect variable as if it were pivotal.
- Use each variable/event's `drives` field and the card's family taxonomy to describe the mechanism
  (e.g. a revenue_build variable "drives" top-line -- the narrative should talk about revenue, not
  some fabricated intermediate metric it doesn't actually touch).
- If the materials are thin (little or no ingested context, or a demo/placeholder card -- you will
  be told explicitly if the card is a placeholder), keep the narrative grounded in the card itself
  and say so rather than inventing color to compensate.
- Do not just restate the numbers already shown elsewhere on screen (IRR/MoIC percentiles) as the
  narrative itself -- the narrative is the qualitative "why" behind those numbers, not a repeat of
  them.

For each of the three bands, name the 2-4 variables/events most responsible for landing in that band
and, for each, a short phrase on where it sits and why that matters -- so the narrative and the
named drivers reinforce each other rather than the narrative floating free of specifics.

Call the narrate_scenario_bands tool with your result. Do not respond in prose."""

_SCENARIO_BAND_SHAPE = {
    "type": "object",
    "required": ["narrative", "key_drivers"],
    "properties": {
        "narrative": {
            "type": "string",
            "description": "2-4 sentences of plain qualitative business language describing what is happening operationally in this band -- grounded in the variable card and ingested materials, never invented.",
        },
        "key_drivers": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["variable", "state"],
                "properties": {
                    "variable": {"type": "string", "description": "The variable or event name/id from the card."},
                    "state": {"type": "string", "description": "Short phrase on where this driver sits in this band and why that matters, e.g. 'near its P10 of 4% growth -- expansion into new logos stalls'."},
                },
            },
            "minItems": 1,
            "maxItems": 4,
        },
    },
}

NARRATE_SCENARIOS_TOOL = {
    "name": "narrate_scenario_bands",
    "description": "Submit qualitative, business-language narratives for the downside (P10), base (P50), and upside (P90) simulated outcome bands.",
    "input_schema": {
        "type": "object",
        "required": ["downside", "base_case", "upside"],
        "properties": {
            "downside": _SCENARIO_BAND_SHAPE,
            "base_case": _SCENARIO_BAND_SHAPE,
            "upside": _SCENARIO_BAND_SHAPE,
        },
    },
}

STAGE5B_LBO_ASSUMPTIONS_SYSTEM_PROMPT = """You are extracting the base-case (FP Base Case) LBO
entry/financing assumptions from an already-ingested deal model, so a human associate can review
and correct them before they anchor the deterministic LBO mechanics view.

The user message opens with a "COMPANY TYPE" line -- the associate has already decided, via a
toggle in the app, whether this deal is being underwritten as a PUBLIC target (a take-private,
priced off an assumed offer price per share) or a PRIVATE target (priced off a single Entry TEV
figure). Follow that declaration exactly for item 1 below; do not try to infer or override it
yourself even if the materials read ambiguously either way -- the associate's own judgment call
takes precedence.

WHERE TO LOOK, AND HOW TO VERIFY IT: same as the base-case financials extraction -- real deal
models in this pipeline consistently carry this information across up to three tabs, one usually
named something like "Summary" (often has a "Sources & Uses" or "Purchase Price"/"Transaction
Assumptions" block), one usually named something like "Financial Model" (the fuller build, often
with an explicit debt schedule), and one usually named something like "Operating Model". Tab names
vary by deal -- treat these as PATTERNS, not literal required names; it's normal for a simpler deal
to only have one or two of the three. If a number appears on more than one tab, they must agree --
flag any discrepancy in source_notes (which tab you trusted, and why).

Confirm the case selector (often a cell literally labeled "Operating Case", with a small lookup
table mapping case names like "FP Base Case"/"FP Upside Case"/"FP Downside Case"/"Management" to
index numbers) is set to the FP Base Case before reading any number that depends on it -- exactly
as in the financials extraction. If it isn't, say so explicitly in source_notes rather than
silently pulling whichever case happens to be showing. Likewise check for a separate leverage-case
selector if one exists (some models toggle leverage independently of the operating case).

Extract SIX assumptions, all as of the FP Base Case:

1. Entry TEV / offer-price mechanics -- exactly which of these two you extract depends on the
   declared COMPANY TYPE:
   - PRIVATE: entry_tev ($M) -- the total enterprise value paid at close (often in a "Sources &
     Uses", "Purchase Price", or "Acquisition" block; may be labeled "Acquisition TEV" or similar).
     Leave offer_price_per_share, diluted_shares_outstanding, and existing_net_debt null.
   - PUBLIC: leave entry_tev null, and instead extract:
     a. offer_price_per_share ($) -- the assumed/proposed per-share purchase price (often in a
        "Sources & Uses", "Offer Price", or "Purchase Price per Share" line).
     b. diluted_shares_outstanding (millions) -- the diluted share count the offer price is applied
        to (treasury-stock-method basis if the model shows one), as of the same date as the offer
        price.
     c. existing_net_debt ($M) -- the TARGET's OWN existing net debt as of signing (its own
        balance-sheet debt less cash) -- this is the equity-value-to-enterprise-value bridge, and
        is NOT the same thing as item 2's opening_debt below (the NEW debt the buyer draws to fund
        the deal). Do not conflate the two even if the model states them near each other.
     The app computes Entry TEV itself from these three figures (offer price x diluted shares +
     existing net debt) -- do not compute it yourself or report a number under entry_tev.
2. Opening debt ($M, or turns of EBITDA if that's how the model states it -- say which in
   source_notes) -- the debt drawn at close to fund the transaction (Term Loan + Revolver, etc.).
   If the model states this as a leverage multiple (e.g. "5.5x EBITDA") rather than a dollar
   amount, report the multiple as given and say so in source_notes -- do not convert it yourself.
3. Effective interest rate (%) -- the blended cash interest rate on the opening debt (may need to
   be backed out from a base rate + spread, e.g. "SOFR + 500bps" -- if so, compute the effective
   rate using the model's own disclosed base-rate assumption and show the arithmetic in
   source_notes).
4. Min cash ($M) -- the minimum cash balance the model assumes is funded/maintained at close and
   held on the balance sheet (sometimes labeled "Min Cash for Operations" or similar).
5. Exit multiple (x) -- the EBITDA (or revenue, if that's the deal's convention -- say which)
   multiple assumed at exit, for the FP Base Case specifically. If the model shows a sensitivity
   grid across several multiples, identify which column/row corresponds to the FP Base Case (often
   via the same case-lookup table used for the operating-case check above) rather than picking an
   arbitrary one.

Every number must be real, pulled from the materials -- never invented. If a specific assumption is
genuinely not derivable from what you were given, use null and say exactly what's missing in
source_notes rather than guessing. This is a strict, no-exceptions extraction task, not a proposal.

Call the extract_lbo_assumptions tool with your result. Do not respond in prose."""

EXTRACT_LBO_ASSUMPTIONS_TOOL = {
    "name": "extract_lbo_assumptions",
    "description": "Submit the extracted FP Base Case LBO entry/financing assumptions.",
    "input_schema": {
        "type": "object",
        "required": [
            "entry_tev", "offer_price_per_share", "diluted_shares_outstanding", "existing_net_debt",
            "opening_debt", "effective_interest_rate_pct", "min_cash", "exit_multiple", "source_notes",
        ],
        "properties": {
            "entry_tev": {"type": ["number", "null"], "description": "PRIVATE targets only -- see item 1 in the system prompt. Null for a PUBLIC target."},
            "offer_price_per_share": {"type": ["number", "null"], "description": "PUBLIC targets only -- assumed/proposed purchase price per share. Null for a PRIVATE target."},
            "diluted_shares_outstanding": {"type": ["number", "null"], "description": "PUBLIC targets only -- diluted share count, in millions. Null for a PRIVATE target."},
            "existing_net_debt": {"type": ["number", "null"], "description": "PUBLIC targets only -- the target's OWN existing net debt at signing ($M), NOT the new LBO financing debt. Null for a PRIVATE target."},
            "opening_debt": {"type": ["number", "null"], "description": "$M, or turns of EBITDA if that's how the model states it (see source_notes)"},
            "effective_interest_rate_pct": {"type": ["number", "null"], "description": "e.g. 8.5 for 8.5%"},
            "min_cash": {"type": ["number", "null"]},
            "exit_multiple": {"type": ["number", "null"]},
            "source_notes": {"type": "string", "description": "Which sheet each figure came from, whether opening_debt is $M or turns of EBITDA, how the interest rate was derived if backed out from base rate + spread, which case/column was used for the exit multiple, what the Operating Case selector was actually set to, anything null with why, and any cross-tab discrepancy found."},
        },
    },
}
