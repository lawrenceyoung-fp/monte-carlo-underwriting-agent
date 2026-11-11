"""Component 2, part A — the reduced-form spine.

THE prove-first piece of the whole build (see docs/04-build-plan-claude-code-three-components.md).

Translates the ~8 tagged cells inside Rightway Model v30.xlsx's Base Case block
(Opmodel - PBM / Opmodel - Nav, columns Q:S, with Summary!AA11 held at 1) into a small,
readable Python function chain: revenue -> gross profit -> EBITDA less CapCosts -> exit TEV ->
FP proceeds at 8.1% fully diluted -> IRR / MoM.

Build this one row at a time and tie each row out against the real workbook before moving to the
next row (the plan's explicit day-2 instruction) — revenue first, then gross profit, then EBITDA,
then exit TEV, then FP proceeds. A tie-out that fails on row four is diagnosable; one that only
fails at the IRR is not.

Tie-out target: AVP!K35 (IRR) / AVP!K36 (MoM) in the real workbook, within 10bps / 0.02x, at every
variable's P50.

NOT YET IMPLEMENTED.
"""

# TODO: read the ~8 tagged cells' base values via openpyxl (read-only) and hardcode them here as
# named constants once Data & inputs has tagged and cited them (see docs/04-..., "Data & inputs").

# TODO: revenue(...) -> gross_profit(...) -> ebitda_less_capcosts(...) -> exit_tev(...) ->
# fp_proceeds(...) -> irr_and_moM(...), each tied out against the real workbook before the next.
