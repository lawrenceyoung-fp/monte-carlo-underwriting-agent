"""Stage 1 -- Ingest. Extracts text from an IC deck (PDF) and cell values from a deal model
(Excel), read-only, per house rule 3 (the deal model is read-only). This is what the skill's own
Stage 1 did inside Claude Code's Read tool / openpyxl calls; here it's the same two operations,
just invoked as plain Python so the local app doesn't depend on Claude Code being present.
"""

import io

import openpyxl
import pdfplumber
from openpyxl.reader.workbook import PrintArea, PrintTitles, WorkbookParser
from warnings import warn


def _patch_openpyxl_print_titles():
    """Some real workbooks carry a malformed Print_Titles defined name (e.g. '#N/A') that trips
    openpyxl's strict parser and blocks the whole file from loading. Patch it to skip that one
    defined name rather than fail the entire read -- same fix used ad hoc against the SPS Commerce
    model; folding it in here so every ingest benefits, not just that one deal."""

    def patched_assign_names(self):
        for idx, names in self.defined_names.by_sheet().items():
            if idx == "global":
                self.wb.defined_names = names
                continue
            try:
                sheet = self.wb._sheets[idx]
            except IndexError:
                warn(f"Defined names for sheet index {idx} cannot be located")
                continue
            for name, defn in names.items():
                reserved = defn.is_reserved
                if reserved is None:
                    sheet.defined_names[name] = defn
                elif reserved == "Print_Titles":
                    try:
                        titles = PrintTitles.from_string(defn.value)
                        sheet._print_rows = titles.rows
                        sheet._print_cols = titles.cols
                    except ValueError:
                        continue
                elif reserved == "Print_Area":
                    try:
                        sheet._print_area = PrintArea.from_string(defn.value)
                    except TypeError:
                        continue

    WorkbookParser.assign_names = patched_assign_names


_patch_openpyxl_print_titles()


def extract_pdf_text(file_bytes, filename):
    pages = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            tables = page.extract_tables()
            table_text = ""
            for t in tables:
                table_text += "\n" + "\n".join(
                    " | ".join(cell or "" for cell in row) for row in t
                )
            pages.append(f"--- {filename}, page {i} ---\n{text}{table_text}")
    return "\n\n".join(pages)


_PRIORITY_SHEET_KEYWORDS = ["summary", "financial model", "operating model", "p&l", "p & l", "model", "forecast"]


def _sheet_rank(name):
    lname = name.lower()
    for i, kw in enumerate(_PRIORITY_SHEET_KEYWORDS):
        if kw in lname:
            return i
    return len(_PRIORITY_SHEET_KEYWORDS)


def _prioritize_sheets(sheet_names):
    """Financial-statement tabs (Summary / Financial Model / Operating Model / P&L / etc.) go
    first. This matters because /api/extract-financials's downstream character budget (app/main.py's
    MAX_INGEST_CHARS) can truncate the combined deck+model dump -- if the sheet the associate
    actually needs happens to sit alphabetically/positionally late in the workbook, it can get cut
    entirely before Claude ever sees it, producing exactly the "pulled the wrong numbers" failure
    mode this ordering exists to avoid. "operating model" is ranked ahead of the generic "model"
    keyword so it isn't left to luck the way a same-tier tie would be -- a real deal model (Weave
    Model v54.xlsx) had an Operating Model tab alone larger than the entire old character budget.
    Ties keep their original relative order (stable sort)."""
    return sorted(sheet_names, key=_sheet_rank)


# Ranks 0-2 are Summary / Financial Model / Operating Model specifically (see
# _PRIORITY_SHEET_KEYWORDS) -- the three tabs Stage 5's extraction prompt requires a three-way
# cross-check across, so they get a much higher row cap than every other tab. A real deal model
# (Weave Model v54.xlsx) had its Operating Model roll-up (Gross Profit/EBITDA rows) sitting at
# rows 598-682 in a sheet with 743 total rows -- well past the default 400-non-empty-row cap
# that's plenty for a typical secondary tab, which was silently cutting those rows out entirely
# even after the character-budget fix above gave the sheet enough room to be included at all.
_PRIORITY_SHEET_MAX_ROWS = 1500
_PRIORITY_SHEET_MAX_SCANNED = 20000


def _fmt_grid_cell(v):
    """No number-format info is available in read_only mode, so this is a display heuristic, not a
    faithful reproduction of the workbook's own formatting -- good enough for a quick-look preview,
    not for reading exact figures off of. Whole numbers under 1900/over 2100 get thousands
    separators; the 1900-2100 band is left plain so a fiscal year like 2024 doesn't render as the
    confusing '2,024'."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        if v == int(v) and abs(v) < 1e15:
            iv = int(v)
            return str(iv) if 1900 <= iv <= 2100 else f"{iv:,}"
        return f"{v:,.2f}"
    if isinstance(v, int):
        return str(v) if 1900 <= v <= 2100 else f"{v:,}"
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    return str(v)


def extract_excel_grid(file_bytes, filename, max_sheets=6, max_rows=35, max_cols=22):
    """A lightweight VISUAL preview for the pre-ingest modal -- an actual row/column grid (the top-
    left corner of each sheet, like opening the file and looking at the first screen without
    scrolling) rather than the 'coordinate=value' cell-dump extract_excel_summary produces for the
    AI. Read-only, same openpyxl patch, same sheet-priority ordering, much smaller caps than a real
    ingest since this only has to look right in a modal, not capture everything Stage 5 needs."""
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    ordered_names = _prioritize_sheets(wb.sheetnames)
    sheets_out = []
    for sheet_name in ordered_names[:max_sheets]:
        ws = wb[sheet_name]
        rows = []
        for row in ws.iter_rows(min_row=1, max_row=max_rows, max_col=max_cols):
            rows.append([_fmt_grid_cell(c.value) for c in row])
        n_cols = len(rows[0]) if rows else 0
        sheets_out.append({
            "name": sheet_name,
            "col_letters": [openpyxl.utils.get_column_letter(i + 1) for i in range(n_cols)],
            "row_numbers": list(range(1, len(rows) + 1)),
            "rows": rows,
            "truncated_rows": (ws.max_row or 0) > max_rows,
            "truncated_cols": (ws.max_column or 0) > max_cols,
        })
    return {
        "sheet_names_all": wb.sheetnames,
        "sheets": sheets_out,
    }


def extract_excel_summary(file_bytes, filename, max_sheets=40, max_rows_per_sheet=400, max_rows_scanned_per_sheet=5000):
    """Read-only cell dump, sheet by sheet. Cached formula values only (data_only=True) -- this
    does not, and cannot, recalculate the workbook; it reads whatever was last saved.

    Bounds BOTH non-empty rows collected (max_rows_per_sheet) AND total rows scanned
    (max_rows_scanned_per_sheet) -- a real, large workbook can have a huge used-range with sparse
    data, and iterating every row looking for 400 non-empty ones can be very slow without a hard
    scan cap. Both caps are reported in the output, not silently applied. Sheets are read in
    priority order (see _prioritize_sheets), not workbook order, for the same reason -- and the
    top three priority ranks get a much higher cap still (_PRIORITY_SHEET_MAX_ROWS), since those
    are specifically the tabs Stage 5 depends on being complete, not just present."""
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    ordered_names = _prioritize_sheets(wb.sheetnames)
    out = [f"Workbook: {filename}", f"Sheets (original order): {', '.join(wb.sheetnames)}"]
    if len(ordered_names) > max_sheets:
        out.append(f"(showing first {max_sheets} of {len(ordered_names)} sheets, financial-statement tabs prioritized)")
    for sheet_name in ordered_names[:max_sheets]:
        ws = wb[sheet_name]
        out.append(f"\n--- Sheet: {sheet_name} ---")
        is_priority = _sheet_rank(sheet_name) <= 2
        row_cap = _PRIORITY_SHEET_MAX_ROWS if is_priority else max_rows_per_sheet
        scan_cap = _PRIORITY_SHEET_MAX_SCANNED if is_priority else max_rows_scanned_per_sheet
        row_count = 0
        scanned = 0
        for row in ws.iter_rows():
            scanned += 1
            cells = [(c.coordinate, c.value) for c in row if c.value is not None]
            if cells:
                out.append("  " + ", ".join(f"{coord}={val!r}" for coord, val in cells))
                row_count += 1
            if row_count >= row_cap:
                out.append(f"  ... truncated at {row_cap} non-empty rows for this sheet ...")
                break
            if scanned >= scan_cap:
                out.append(f"  ... stopped after scanning {scan_cap} rows for this sheet (only {row_count} were non-empty) ...")
                break
    return "\n".join(out)
