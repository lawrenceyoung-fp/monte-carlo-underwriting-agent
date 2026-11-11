"""Stage 7 -- populate the FP-branded IC slide. Ported from skills/monte-carlo-model/SKILL.md's
Stage 7 spec (histogram + stats, contribution analysis, one slide) into a deterministic python-pptx
export instead of an LLM-authored deck -- this step is pure rendering of already-computed numbers,
no judgment left to make, so it doesn't need Claude in the loop at all.

Template: app/assets/ic_slide_template.pptx -- a real, hand-finished IC slide (not a blank
skeleton): title, a teal "Simulation Results" corner tag, a bordered summary callout (3 bullets),
two colored histogram charts, three tables (stats, probability, contribution analysis), a
Scenario Analysis text box, section headers, and a sources footnote, ALL manually formatted in
PowerPoint by the deal team. On every export this module opens that exact file and updates only
the data-bearing content in place -- title text, the callout's three bullets, both charts' data,
every table's cell values (resizing the contribution-analysis table's row count to however many
drivers this run actually has, capped at the top 7), and the Scenario Analysis box's downside/base/
upside bullets -- deliberately never touching fonts, colors, positions, or any other formatting, so
the deal team's own polish survives every export unchanged. Static labels (the corner tag text, the
section-header bars, the sources footnote, and a stray "think-cell data - do not delete" embedded
object left over from manual editing) are never touched at all. There is no variable-register table
on this template (a previous version had one) -- the confirmed variable card lives in the app
itself; the slide's job is the simulation output, not a restatement of every input.
"""

import copy
import io
import pathlib
import tempfile
import uuid

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.dml import MSO_COLOR_TYPE
from pptx.enum.text import MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Pt

TEMPLATE_PATH = str(pathlib.Path(__file__).resolve().parent / "assets" / "ic_slide_template.pptx")

# Outcome-coded histogram bar colors -- data semantics (bad/neutral/good), matching the web UI's
# equivalent charts. The template's own chart formatting (axis fonts, title styling, gap width,
# vary-by-category) is left as the deal team set it; only per-bin fill colors are computed here.
RED = RGBColor(0xA3, 0x2D, 0x2D)
GOLD = RGBColor(0xB8, 0x91, 0x2F)
GREEN = RGBColor(0x0B, 0x3D, 0x2E)


def _find_shape(slide, name):
    for shape in slide.shapes:
        if shape.name == name:
            return shape
    raise KeyError(f"expected shape {name!r} not found in the IC slide template ({TEMPLATE_PATH})")


def _restyle_paragraph_text(paragraph, text):
    """Replace a paragraph's text with a single new run, preserving the paragraph's own formatting
    (bullet character, indent, spacing -- all set at the pPr level, untouched here) and copying
    character formatting (size, bold, italic, font, color) from whatever its first existing run
    already had, rather than guessing the template's styling. Also collapses any multi-run split
    within the paragraph (a normal side effect of manually editing text in PowerPoint) into one
    clean run, so partial stale text can't survive a value update."""
    if not paragraph.runs:
        run = paragraph.add_run()
        run.text = text
        return run
    src = paragraph.runs[0].font
    size, bold, italic, font_name = src.size, src.bold, src.italic, src.name
    color_type = src.color.type
    color_rgb = src.color.rgb if color_type == MSO_COLOR_TYPE.RGB else None
    color_theme = src.color.theme_color if color_type == MSO_COLOR_TYPE.SCHEME else None

    paragraph.clear()
    run = paragraph.add_run()
    run.text = text
    if size is not None:
        run.font.size = size
    if bold is not None:
        run.font.bold = bold
    if italic is not None:
        run.font.italic = italic
    if font_name is not None:
        run.font.name = font_name
    if color_rgb is not None:
        run.font.color.rgb = color_rgb
    elif color_theme is not None:
        run.font.color.theme_color = color_theme
    return run


_CALLOUT_PT = 8  # the template's own run size (9pt) ran a little large once this box carries 3
# full bullets every export instead of the occasional short 2-line callout it was hand-tuned for


def _set_callout_lines(shape, lines):
    tf = shape.text_frame
    paragraphs = list(tf.paragraphs)
    while len(paragraphs) < len(lines):
        paragraphs.append(tf.add_paragraph())  # only reached if a future line count grows past 3
    for i, line in enumerate(lines):
        run = _restyle_paragraph_text(paragraphs[i], line)
        run.font.size = Pt(_CALLOUT_PT)
    for extra in paragraphs[len(lines):]:
        extra.clear()


def _set_cell_text(table, r, c, value):
    text = "--" if value is None else str(value)
    _restyle_paragraph_text(table.cell(r, c).text_frame.paragraphs[0], text)


def _resize_table_data_rows(table, target_count, header_rows=1):
    """Add/remove data rows (every row after the header) by cloning/removing <a:tr> elements
    directly, so a row added for this run's exact variable/driver count inherits the cell
    formatting (font, fill, borders) of the template's own last data row, instead of the unstyled
    default python-pptx would give a freshly-added row."""
    tbl = table._tbl
    trs = tbl.findall(qn("a:tr"))
    current = len(trs) - header_rows
    if target_count > current:
        template_tr = trs[-1]
        for _ in range(target_count - current):
            tbl.append(copy.deepcopy(template_tr))
    elif target_count < current:
        for tr in trs[header_rows + target_count:]:
            tbl.remove(tr)


# Contribution-analysis table sizing. Fixed, not dynamic -- safe because build_slide always caps
# this table at 7 data rows, so header + 7 data rows at these row heights (2,371,000 EMU total,
# ~2.59in) fits inside the template's own box (~2.82in) even in the worst case where every long
# variable name wraps to 2 lines. The template's own 4 equal-width columns leave the text-heavy
# Variable column too narrow for a real name (vs. the short id it used to hold) -- redistributed so
# Variable gets most of the room and the numeric columns (which only ever hold short values like
# "53.5%"/"6.41") get just enough. _CONTRIB_NAME_MAX_CHARS is a backstop against a pathologically
# long name pushing past 2 lines even at that width; ordinary names never hit it.
_CONTRIB_COL_FRACTIONS = [0.47, 0.19, 0.17, 0.17]
_CONTRIB_HEADER_ROW_H = Emu(320000)
_CONTRIB_DATA_ROW_H = Emu(293000)
_CONTRIB_HEADER_PT = 8
_CONTRIB_DATA_PT = 7
_CONTRIB_NAME_MAX_CHARS = 68


def _fit_contribution_table(shape, table):
    total_w = sum(col.width for col in table.columns)
    for col, frac in zip(table.columns, _CONTRIB_COL_FRACTIONS):
        col.width = Emu(int(total_w * frac))
    for ri, row in enumerate(table.rows):
        row.height = _CONTRIB_HEADER_ROW_H if ri == 0 else _CONTRIB_DATA_ROW_H
        pt = _CONTRIB_HEADER_PT if ri == 0 else _CONTRIB_DATA_PT
        for cell in row.cells:
            cell.text_frame.word_wrap = True
            for paragraph in cell.text_frame.paragraphs:
                paragraph.font.size = Pt(pt)


def _bin_mid_color(mid, thresholds):
    lo, hi = thresholds
    if mid < lo:
        return RED
    if mid > hi:
        return GREEN
    return GOLD


def _update_histogram_chart(shape, histogram, tick_fmt, color_thresholds):
    edges = histogram["bin_edges"]
    counts = histogram["counts"]
    categories = [tick_fmt(edges[i]) if i % 4 == 0 else "" for i in range(len(counts))]
    chart_data = CategoryChartData()
    chart_data.categories = categories
    chart_data.add_series("Paths", counts)
    chart = shape.chart
    # replace_data() updates categories/values (and the chart's embedded worksheet) in place,
    # preserving the chart's own formatting (title, axis fonts, gap width) -- but it does drop any
    # per-point fill overrides, so those are always reapplied below regardless of whether this
    # run's bin count/edges match the template's.
    chart.replace_data(chart_data)
    plot = chart.plots[0]
    plot.vary_by_categories = True
    series = plot.series[0]
    for i, point in enumerate(series.points):
        mid = (edges[i] + edges[i + 1]) / 2
        point.format.fill.solid()
        point.format.fill.fore_color.rgb = _bin_mid_color(mid, color_thresholds)


def _fmt_pct(x, digits=1):
    return f"{x*100:.{digits}f}%" if x is not None else "--"


def _fmt_x(x, digits=2):
    return f"{x:.{digits}f}x" if x is not None else "--"


def _truncate(text, max_chars):
    return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "…"


def _contribution_pct(driver_ranking):
    """Normalizes each ranked variable's |rank_correlation_to_irr| (a real Spearman correlation
    against the full IRR path array, computed once in engine.py -- not re-derived or estimated
    here) into a share of the total across all ranked variables, so the slide can show "how
    significant" each driver is relative to the others as a single percentage instead of a raw
    correlation coefficient. This is a relative-significance read, not a variance decomposition
    (Sobol indices) -- the engine doesn't compute one -- so it answers "which of these listed
    drivers moves returns the most" rather than "what fraction of total return variance this
    variable explains." Returns a dict of variable -> pct (0-100), or an empty dict if every
    ranked variable's correlation is null/zero (nothing to normalize against)."""
    magnitudes = {
        d["variable"]: abs(d["rank_correlation_to_irr"])
        for d in driver_ranking
        if d.get("rank_correlation_to_irr") is not None
    }
    total = sum(magnitudes.values())
    if total == 0:
        return {}
    return {vid: mag / total * 100 for vid, mag in magnitudes.items()}


def _card_display_name_lookup(deal_config):
    """Maps every variable/event id -- AND its own already-correct name -- to that same name, so
    anything downstream keyed by id (driver_ranking, binary_events_check) or already holding a name
    (an LLM-authored scenario narrative's key_drivers) normalizes to the exact display name the
    user typed into the card, never a family/category label or a raw id slug."""
    lookup = {}
    for v in deal_config.get("variables", []):
        name = v.get("name") or v.get("id")
        if v.get("id"):
            lookup[v["id"]] = name
        if name:
            lookup[name] = name
    for ev in deal_config.get("binary_events", []):
        name = ev.get("name") or ev.get("id")
        if ev.get("id"):
            lookup[ev["id"]] = name
        if name:
            lookup[name] = name
    return lookup


def _driver_descriptor_line(deal_config, results):
    """Plain-language readout of results["driver_ranking"] (already sorted by |rank correlation|
    in engine.py) -- names the top 1-2 variables actually moving IRR, not just a numeric table row,
    so the slide states in words what the contribution-analysis table shows in numbers. Every name
    and figure traces straight to driver_ranking / the variable's own id->name lookup -- nothing
    invented."""
    ranking = results.get("driver_ranking") or []
    if not ranking:
        return "No ranked drivers -- all variables held at P50 or unmapped to the LBO engine."
    id_to_name = _card_display_name_lookup(deal_config)
    parts = []
    for d in ranking[:2]:
        name = id_to_name.get(d["variable"], d["variable"])
        rho = d.get("rank_correlation_to_irr")
        parts.append(f"{name} (corr. to IRR: {rho:+.2f})" if rho is not None else name)
    return "Primary drivers of outcome: " + "; ".join(parts) + "."


# Dispersion/skew classification thresholds -- same kind of fixed judgment cutoff already used by
# _bin_mid_color's (0.0, 0.20) / (1.0, 2.0) histogram-coloring thresholds elsewhere in this file:
# a deterministic read of shape from numbers the engine already computed, not a new estimate.
_WIDE_SPREAD_PP = 0.30  # P90-P10 IRR spread at/above this reads as "wide dispersion"
_TIGHT_SPREAD_PP = 0.15  # P90-P10 IRR spread at/below this reads as "tightly clustered"
_SKEW_RATIO = 1.3  # one tail more than this multiple of the other reads as a "long tail"
_RARE_EVENT_P_SUCCESS = 0.6  # P(no fire) at/above this = fires in <=40% of paths ("rare")


def _tail_binary_event(deal_config, results, upside):
    """Names a binary event as the likely driver of a long tail only when the event's OWN data
    supports it: it fires rarely (P(no fire) >= _RARE_EVENT_P_SUCCESS, i.e. in <=40% of paths) AND
    its impact's sign matches the tail direction (a positive-impact event for a long right/upside
    tail, a negative-impact event for a long left/downside tail). Returns None -- and the calling
    bullet just omits the clause -- rather than ever guessing at attribution the data doesn't
    support."""
    check = results.get("binary_events_check") or {}
    events_by_id = {e.get("id"): e for e in deal_config.get("binary_events", [])}
    candidates = []
    for eid, c in check.items():
        ev = events_by_id.get(eid)
        if not ev:
            continue
        p_success = c.get("p_success")
        if p_success is None or p_success < _RARE_EVENT_P_SUCCESS:
            continue
        impact = ev.get("impact") or {}
        signed_impact = sum(v for v in impact.values() if v is not None)
        if (upside and signed_impact > 0) or (not upside and signed_impact < 0):
            candidates.append((p_success, ev.get("name") or eid))
    if not candidates:
        return None
    candidates.sort(key=lambda t: -t[0])  # rarest event (highest P(no fire)) first
    return candidates[0][1]


def _distribution_shape_lines(deal_config, results):
    """Two deterministic reads of what the IRR distribution's own shape says about this
    opportunity -- dispersion/predictability, then skew/tail -- built only from percentiles and
    probability_table figures the simulation engine already computed (house rule 1: never invent a
    number). This is a judgment about what the shape implies, not a restatement of the histogram
    already on the slide."""
    pct = results["percentiles"]["irr"]
    pt = results["probability_table"]
    p10, p50, p90 = pct["p10"], pct["p50"], pct["p90"]
    spread = p90 - p10
    upside_gap, downside_gap = p90 - p50, p50 - p10

    if spread >= _WIDE_SPREAD_PP:
        dispersion = (
            f"Wide dispersion of outcomes ({_fmt_pct(p10)}-{_fmt_pct(p90)} IRR, a {spread*100:.0f}pp "
            f"P10-P90 range) -- returns are highly sensitive to how key variables resolve, with "
            f"limited predictability around the {_fmt_pct(p50)} median."
        )
    elif spread <= _TIGHT_SPREAD_PP:
        dispersion = (
            f"Tightly clustered outcomes ({_fmt_pct(p10)}-{_fmt_pct(p90)} IRR, a {spread*100:.0f}pp "
            f"P10-P90 range) around a {_fmt_pct(p50)} median -- high confidence the realized return "
            f"lands near the base case."
        )
    else:
        dispersion = (
            f"Moderate dispersion of outcomes ({_fmt_pct(p10)}-{_fmt_pct(p90)} IRR) around a "
            f"{_fmt_pct(p50)} median -- a reasonably predictable range either way."
        )

    if upside_gap > downside_gap * _SKEW_RATIO:
        event = _tail_binary_event(deal_config, results, upside=True)
        tail = (
            f"Long right tail -- {_fmt_pct(pt['p_irr_gt_25'])} probability of exceeding 25% IRR "
            f"against the {_fmt_pct(p50)} median"
            + (f", with the upper end driven by {event} resolving in our favor" if event else "")
            + "."
        )
    elif downside_gap > upside_gap * _SKEW_RATIO:
        event = _tail_binary_event(deal_config, results, upside=False)
        tail = (
            f"Long left tail -- {_fmt_pct(pt['p_irr_lt_0'])} probability of capital loss (IRR<0%) "
            f"despite the {_fmt_pct(p50)} median"
            + (f", concentrated in paths where {event} goes against us" if event else "")
            + "."
        )
    else:
        tail = (
            f"Roughly symmetric around the median -- {_fmt_pct(pt['p_irr_lt_15'])} probability of "
            f"falling short of a 15% IRR, against {_fmt_pct(pt['p_irr_gt_25'])} probability of "
            f"exceeding 25%."
        )
    return [dispersion, tail]


def _callout_lines(deal_config, results):
    """Purely computed from `results`/`deal_config` -- no invented commentary, only figures already
    produced by the simulation engine (house rule 1: never invent a number). Deliberately does NOT
    include a "Simulated N paths (seed S) -- engine mode" line -- that run-manifest detail is
    useful for debugging but not for an IC audience reading the slide, so it's left out of what
    goes in front of them. Leads with what the distribution's shape says about the opportunity
    (dispersion/predictability, then skew/tail), then names the primary drivers -- the template
    reserves exactly 3 bulleted lines for this callout."""
    lines = list(_distribution_shape_lines(deal_config, results))
    lines.append(_driver_descriptor_line(deal_config, results))
    return lines


_BAND_LABELS = [
    ("downside", "Downside (~P10 IRR)", RED),
    ("base_case", "Base Case (~P50 IRR)", GOLD),
    ("upside", "Upside (~P90 IRR)", GREEN),
]
_SCENARIO_HEADER_PT = 9
_SCENARIO_BODY_PT = 8
_SCENARIO_NO_DATA_TEXT = (
    'Run "Explain scenarios in business terms" (Stage 6) before exporting to populate this section.'
)


def _condense_narrative(narrative, max_sentences=1):
    """Trims an already-generated scenario narrative (2-4 sentences from Stage 6's narrate-
    scenarios call) down to its first sentence(s) so it fits as one slide bullet -- a text
    operation on existing AI-authored prose, not new invented content."""
    sentences = [s.strip() for s in narrative.replace("\n", " ").split(". ") if s.strip()]
    if not sentences:
        return ""
    text = ". ".join(sentences[:max_sentences])
    return text if text.endswith((".", "!", "?")) else text + "."


def _set_scenario_analysis(shape, scenario_narratives, deal_config):
    """Populates the Scenario Analysis box with a header + a couple of bullets per band (downside/
    base/upside), condensed from the Stage 6 "Explain scenarios in business terms" output (never
    invented here -- either that call already ran and produced `scenario_narratives`, or this box
    says plainly that it hasn't). Grows/shrinks the text frame's paragraph count to match, the same
    padding approach `_set_callout_lines` uses for a fixed line count, generalized to a variable
    one.

    Three fixes on top of the template's own box: (1) the template's grey fill reads as a stray
    solid box once real content fills it -- removed, letting the slide's own background show
    through. (2) the template shape has word_wrap OFF, which lets a long line run past the shape's
    right edge instead of wrapping -- forced on. (3) auto_size is set to shrink text to fit the
    (fixed-position, fixed-size) shape rather than left at NONE, so a longer-than-usual set of
    narratives shrinks its font instead of spilling past the bottom of the box."""
    shape.fill.background()
    tf = shape.text_frame
    tf.word_wrap = True
    tf.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
    paragraphs = list(tf.paragraphs)

    if not scenario_narratives:
        while len(paragraphs) < 1:
            paragraphs.append(tf.add_paragraph())
        run = _restyle_paragraph_text(paragraphs[0], _SCENARIO_NO_DATA_TEXT)
        run.font.bold = False
        run.font.size = Pt(_SCENARIO_BODY_PT)
        paragraphs[0].alignment = PP_ALIGN.LEFT
        for extra in paragraphs[1:]:
            extra.clear()
        return

    name_lookup = _card_display_name_lookup(deal_config)
    lines = []  # (text, is_header, color)
    for key, label, color in _BAND_LABELS:
        band = scenario_narratives.get(key) or {}
        lines.append((label, True, color))
        narrative = band.get("narrative")
        if narrative:
            lines.append(("-  " + _condense_narrative(narrative), False, None))
        for driver in (band.get("key_drivers") or [])[:2]:
            raw_variable, state = driver.get("variable", ""), driver.get("state", "")
            variable = name_lookup.get(raw_variable, raw_variable)
            if variable and state:
                lines.append((f"-  {variable}: {state}", False, None))
            elif variable or state:
                lines.append((f"-  {variable or state}", False, None))

    while len(paragraphs) < len(lines):
        paragraphs.append(tf.add_paragraph())
    for i, (text, is_header, color) in enumerate(lines):
        run = _restyle_paragraph_text(paragraphs[i], text)
        run.font.bold = is_header
        run.font.size = Pt(_SCENARIO_HEADER_PT if is_header else _SCENARIO_BODY_PT)
        if color is not None:
            run.font.color.rgb = color
        paragraphs[i].alignment = PP_ALIGN.LEFT
    for extra in paragraphs[len(lines):]:
        extra.clear()


def build_slide(deal_config, results, scenario_narratives=None):
    prs = Presentation(TEMPLATE_PATH)
    slide = prs.slides[0]

    _restyle_paragraph_text(
        _find_shape(slide, "Title 1").text_frame.paragraphs[0],
        "Monte Carlo Simulation Distribution of Returns",
    )

    _set_callout_lines(_find_shape(slide, "Rectangle 4"), _callout_lines(deal_config, results))

    _update_histogram_chart(
        _find_shape(slide, "Chart 5"), results["histogram"]["irr"],
        lambda e: f"{e*100:.0f}%", (0.0, 0.20),
    )
    _update_histogram_chart(
        _find_shape(slide, "Chart 6"), results["histogram"]["moic"],
        lambda e: f"{e:.1f}x", (1.0, 2.0),
    )

    # --- stats + probability tables (always exactly 2 and 3 data rows -- no resizing needed) ---
    pct = results["percentiles"]
    pt = results["probability_table"]
    stats_table = _find_shape(slide, "Table 7").table
    stats_values = [
        ["IRR", _fmt_pct(pct["irr"]["p10"]), _fmt_pct(pct["irr"]["p50"]), _fmt_pct(pct["irr"]["p90"])],
        ["MoIC", _fmt_x(pct["moic"]["p10"]), _fmt_x(pct["moic"]["p50"]), _fmt_x(pct["moic"]["p90"])],
    ]
    for r, row in enumerate(stats_values, start=1):
        for c, val in enumerate(row):
            _set_cell_text(stats_table, r, c, val)

    prob_table = _find_shape(slide, "Table 8").table
    prob_values = [
        ["P(IRR<0%)", _fmt_pct(pt["p_irr_lt_0"]), "P(MoIC>=2x)", _fmt_pct(pt["p_moic_ge_2"])],
        ["P(IRR<15%)", _fmt_pct(pt["p_irr_lt_15"]), "P(MoIC>=3x)", _fmt_pct(pt["p_moic_ge_3"])],
        ["P(IRR>25%)", _fmt_pct(pt["p_irr_gt_25"]), "P(capital loss)", _fmt_pct(pt["p_capital_loss"])],
    ]
    for r, row in enumerate(prob_values):
        for c, val in enumerate(row):
            _set_cell_text(prob_table, r, c, val)

    # --- contribution analysis (top 7 ranked drivers by |rank correlation to IRR|, so the table
    # fits the box regardless of how many variables the card has; % contribution is still each
    # driver's share of ALL ranked variables' total magnitude, not renormalized to just these 7,
    # so the numbers shown stay truthful shares rather than artificially summing to 100%) ---
    id_to_name = _card_display_name_lookup(deal_config)
    driver_ranking = results.get("driver_ranking", [])
    contribution_pct = _contribution_pct(driver_ranking)
    driver_rows = []
    for d in driver_ranking[:7]:
        pct = contribution_pct.get(d["variable"])
        name = id_to_name.get(d["variable"], d["variable"])
        driver_rows.append([
            _truncate(name, _CONTRIB_NAME_MAX_CHARS),
            f"{pct:.1f}%" if pct is not None else "--",
            round(d["worst_decile_mean"], 2),
            round(d["best_decile_mean"], 2),
        ])
    if not driver_rows:
        driver_rows = [["(no ranked variables -- all held at P50 or unmapped)", "--", "--", "--"]]
    driver_shape = _find_shape(slide, "Table 12")
    driver_table = driver_shape.table
    _restyle_paragraph_text(driver_table.cell(0, 1).text_frame.paragraphs[0], "% Contribution to Returns")
    _resize_table_data_rows(driver_table, len(driver_rows))
    for r, row in enumerate(driver_rows, start=1):
        for c, val in enumerate(row):
            _set_cell_text(driver_table, r, c, val)
    _fit_contribution_table(driver_shape, driver_table)

    _set_scenario_analysis(_find_shape(slide, "Rectangle 14"), scenario_narratives, deal_config)

    buf = io.BytesIO()
    prs.save(buf)
    buf.seek(0)
    return buf


def open_in_powerpoint(buf, filename):
    """Save the generated slide to a local temp file and open it directly in the user's own
    PowerPoint via COM automation, instead of making them download/save it manually. Legitimate
    here specifically because this is a local, single-user desktop app (see main.py's own
    SESSIONS docstring) -- the FastAPI process and the browser are running on the SAME Windows
    machine, so launching a GUI app from the backend is local automation, not a server reaching
    into a client's machine. Windows + PowerPoint (+ pywin32) only -- raises RuntimeError with a
    clear message otherwise so the caller can fall back to a plain download."""
    try:
        import win32com.client
    except ImportError as exc:
        raise RuntimeError("pywin32 is not installed -- can't open PowerPoint automatically") from exc

    # A unique filename per export -- reusing the same name (e.g. always
    # "Demo_Deal_(placeholder)_monte_carlo.pptx" for the demo path) meant a second export while
    # the first was still open in PowerPoint would try to overwrite a file Windows has locked,
    # failing the write and leaving PowerPoint showing the stale first export -- which reads
    # exactly like "the export isn't working."
    stem = pathlib.Path(filename).stem
    suffix = pathlib.Path(filename).suffix or ".pptx"
    unique_filename = f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"

    tmp_dir = pathlib.Path(tempfile.gettempdir()) / "monte_carlo_underwriting_agent"
    try:
        tmp_dir.mkdir(exist_ok=True)
        path = tmp_dir / unique_filename
        path.write_bytes(buf.getvalue())

        app = win32com.client.Dispatch("PowerPoint.Application")
        app.Visible = True
        app.Presentations.Open(str(path))
    except Exception as exc:
        raise RuntimeError(f"PowerPoint automation failed: {exc}") from exc
