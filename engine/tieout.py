"""Component 2, part C — the tie-out against the real Rightway model.

Owned in spirit by Validation (see docs/04-..., "Protected split: Validation never sits with Core
engine"), even though the file lives in engine/ for convenience. Re-run this on every change to
spine.py or sample.py.

Answer key: cases/rightway/Rightway Model v30.xlsx, cell AVP!K35 (IRR) and AVP!K36 (MoM), read via
openpyxl (read-only). At every variable held at its P50, spine.py's output must reproduce these
within 10bps (IRR) and 0.02x (MoM). The workbook's own Data Table at Summary!BK125:BS125 (keyed off
AVP!G5:O5, "Illustrative Offer Prices") is a useful secondary check across a range of offer prices,
not just the single base case.

NOT YET IMPLEMENTED.
"""

# TODO: read_answer_key(path) -> {"irr": ..., "moic": ...} from AVP!K35 / K36, via openpyxl
# TODO: run_base_case_through_spine() -> {"irr": ..., "moic": ...}
# TODO: assert abs(irr - answer_key["irr"]) <= 0.0010
# TODO: assert abs(moic - answer_key["moic"]) <= 0.02
