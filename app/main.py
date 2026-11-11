"""The local standalone app: FastAPI backend + a single-page frontend, running the same
seven-stage methodology the /monte-carlo-model Claude Code skill used, but as a real app you run
with one command instead of inside Claude Code. See README.md (repo root) for how to run it.
"""

import json
import pathlib
import re
import sys
import uuid

from fastapi import FastAPI, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import demo_card, ingest, llm, projects, prompts, slide_export
from simulation_service import distributions, engine, lbo_spine

app = FastAPI(title="Monte Carlo Underwriting Agent")

STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

MAX_INGEST_CHARS = 650_000  # combined deck+model text budget sent to Claude
# Raised from 150,000 after a real deal model (Weave Model v54.xlsx, ~1.5M raw chars from
# ingest.extract_excel_summary once its Operating Model tab's own per-sheet row cap was also fixed
# -- see ingest.py's _PRIORITY_SHEET_MAX_ROWS) proved the old budget was cutting the ingested text
# off partway through Operating Model's own Gross Profit / EBITDA roll-up rows, which Stage 5's
# extraction prompt now requires for its three-way cross-check (see prompts.py). For that file,
# Summary + Financial Model + all of Operating Model together run to ~556k chars -- 650k leaves
# real headroom above that (for this file and others) while staying well inside a 200k-token
# context (650k chars is roughly 160-165k tokens) alongside the system prompt/tool schema/response.

# In-memory session store. This is a local, single-user app -- no auth, no persistence beyond
# process lifetime. Restarting the app loses in-flight sessions; that's an accepted trade-off for
# a hackathon-timeline local tool, not an oversight.
SESSIONS = {}


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "engine_version": engine.ENGINE_VERSION,
        "llm_backend": llm.BACKEND,
        "native_sdk_available": llm.NATIVE_SDK_AVAILABLE,
        "native_cli_on_path": llm._resolve_cli_path() is not None,
    }


@app.post("/api/preview-excel")
async def api_preview_excel(model: UploadFile):
    """Pre-ingest preview for the model (.xlsx) file picker -- there's no native browser viewer for
    spreadsheets the way there is for PDFs, so this renders a lightweight row/column grid (top-left
    corner of each sheet, financial-statement tabs prioritized) instead."""
    model_bytes = await model.read()
    try:
        preview = ingest.extract_excel_grid(model_bytes, model.filename)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"could not read model .xlsx: {exc}")
    return {"filename": model.filename, **preview}


@app.post("/api/ingest")
async def api_ingest(deck: UploadFile = None, model: UploadFile = None):
    if deck is None and model is None:
        raise HTTPException(status_code=422, detail="upload at least one of deck (PDF) or model (.xlsx)")

    parts = []
    deck_excerpt = model_excerpt = None
    if deck is not None:
        deck_bytes = await deck.read()
        try:
            deck_text = ingest.extract_pdf_text(deck_bytes, deck.filename)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"could not read deck PDF: {exc}")
        parts.append(deck_text)
        deck_excerpt = deck_text[:2000]

    if model is not None:
        model_bytes = await model.read()
        try:
            model_text = ingest.extract_excel_summary(model_bytes, model.filename)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"could not read model .xlsx: {exc}")
        parts.append(model_text)
        model_excerpt = model_text[:2000]

    combined = "\n\n".join(parts)
    truncated = False
    if len(combined) > MAX_INGEST_CHARS:
        combined = combined[:MAX_INGEST_CHARS]
        truncated = True

    session_id = str(uuid.uuid4())
    SESSIONS[session_id] = {"ingested_text": combined, "card": None, "chat_history": [], "native_session_id": None}

    return {
        "session_id": session_id,
        "deck_excerpt": deck_excerpt,
        "model_excerpt": model_excerpt,
        "ingested_chars": len(combined),
        "truncated": truncated,
    }


class ProposeRequest(BaseModel):
    session_id: str


@app.post("/api/propose")
async def api_propose(req: ProposeRequest):
    session = SESSIONS.get(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session_id -- call /api/ingest first")

    user_content = f"Here is the ingested content:\n\n{session['ingested_text']}"
    try:
        card, native_session_id = await llm.propose(prompts.STAGE2_SYSTEM_PROMPT, user_content)
    except (RuntimeError, llm.NativeUnavailable) as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    session["card"] = card
    session["native_session_id"] = native_session_id
    session["chat_history"] = [
        {"role": "user", "content": f"[Stage 2 proposal generated -- for context in this conversation]\n{card}"},
    ]
    return card


class ProposeDemoRequest(BaseModel):
    session_id: str | None = None
    deal_name: str | None = None


@app.post("/api/propose-demo")
def api_propose_demo(req: ProposeDemoRequest):
    """No LLM call at all -- a hardcoded placeholder card (see app/demo_card.py) so the rest of
    the workflow is testable before native Claude Code access or an API key is set up. Every field
    on the returned card says explicitly that it's not AI-generated."""
    session_id = req.session_id
    if session_id is None or session_id not in SESSIONS:
        session_id = str(uuid.uuid4())
        SESSIONS[session_id] = {"ingested_text": "", "card": None, "chat_history": [], "native_session_id": None}

    card = demo_card.get_demo_card(req.deal_name)
    session = SESSIONS[session_id]
    session["card"] = card
    session["chat_history"] = [
        {"role": "user", "content": f"[Demo placeholder card generated -- not AI, see commentary]\n{card}"},
    ]
    card["session_id"] = session_id
    return card


class ChatRequest(BaseModel):
    session_id: str
    message: str


@app.post("/api/chat")
async def api_chat(req: ChatRequest):
    session = SESSIONS.get(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session_id -- call /api/ingest and /api/propose first")
    if session["card"] is None:
        raise HTTPException(status_code=400, detail="no proposal yet -- call /api/propose first")

    session["chat_history"].append({"role": "user", "content": req.message})
    try:
        result, native_session_id = await llm.chat_structured(
            prompts.STAGE3_SYSTEM_PROMPT, session["chat_history"], prompts.STAGE3_REPLY_TOOL,
            native_session_id=session.get("native_session_id"),
        )
    except (RuntimeError, llm.NativeUnavailable) as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if native_session_id:
        session["native_session_id"] = native_session_id
    # Only the plain-text reply goes into chat_history -- keeps future turns' context clean text,
    # not a structured object the model would have to re-parse.
    session["chat_history"].append({"role": "assistant", "content": result["reply"]})
    return {"reply": result["reply"], "proposed_change": result.get("proposed_change")}


class ExtractFinancialsRequest(BaseModel):
    session_id: str


@app.post("/api/extract-financials")
async def api_extract_financials(req: ExtractFinancialsRequest):
    session = SESSIONS.get(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session_id -- call /api/ingest first")

    user_content = f"Here is the ingested content:\n\n{session['ingested_text']}"
    try:
        financials, _ = await llm.extract_structured(
            prompts.STAGE5_FINANCIALS_SYSTEM_PROMPT, user_content, prompts.EXTRACT_FINANCIALS_TOOL
        )
    except (RuntimeError, llm.NativeUnavailable) as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    session["base_case_financials"] = financials
    return financials


class ExtractFinancialsDemoRequest(BaseModel):
    session_id: str | None = None
    deal_name: str | None = None


@app.post("/api/extract-financials-demo")
def api_extract_financials_demo(req: ExtractFinancialsDemoRequest):
    """No LLM call -- a hardcoded, internally-consistent placeholder spine (app/demo_card.py)."""
    session_id = req.session_id
    if session_id is None or session_id not in SESSIONS:
        session_id = str(uuid.uuid4())
        SESSIONS[session_id] = {"ingested_text": "", "card": None, "chat_history": [], "native_session_id": None}

    financials = demo_card.get_demo_financials(req.deal_name)
    SESSIONS[session_id]["base_case_financials"] = financials
    financials["session_id"] = session_id
    return financials


class ExtractLboAssumptionsRequest(BaseModel):
    session_id: str
    is_public: bool = False


@app.post("/api/extract-lbo-assumptions")
async def api_extract_lbo_assumptions(req: ExtractLboAssumptionsRequest):
    """Stage 5b helper -- pulls entry TEV (or, for a publicly traded target, the offer-price/share-
    count/net-debt inputs the app itself computes entry TEV from) plus opening debt/interest rate/
    min cash/exit multiple from the same ingested model Stage 5 already read, so the LBO mechanics
    fields don't have to be hand-typed. `is_public` is the associate's own declaration (the "Publicly
    traded target" toggle in Stage 5b, checked before clicking Extract) -- passed into the prompt as
    a given, not something the extraction is asked to infer. No session mutation: these values live
    entirely client-side in the #f-* inputs, same as when hand-typed."""
    session = SESSIONS.get(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session_id -- call /api/ingest first")

    company_type = (
        "PUBLIC (a take-private, priced off an assumed offer price per share)"
        if req.is_public
        else "PRIVATE (priced off a single Entry TEV figure)"
    )
    user_content = (
        f"COMPANY TYPE (per associate declaration): {company_type}\n\n"
        f"Here is the ingested content:\n\n{session['ingested_text']}"
    )
    try:
        assumptions, _ = await llm.extract_structured(
            prompts.STAGE5B_LBO_ASSUMPTIONS_SYSTEM_PROMPT, user_content, prompts.EXTRACT_LBO_ASSUMPTIONS_TOOL
        )
    except (RuntimeError, llm.NativeUnavailable) as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return assumptions


class SimulateRequest(BaseModel):
    deal_config: dict
    n_paths: int = 10000
    seed: int = 42


@app.post("/api/simulate")
def api_simulate(req: SimulateRequest):
    try:
        return engine.run(req.deal_config, n_paths=req.n_paths, seed=req.seed)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))


class NarrateScenariosRequest(BaseModel):
    session_id: str | None = None
    deal_config: dict
    results: dict
    card_is_demo: bool = False


NARRATIVE_INGEST_EXCERPT_CHARS = 40_000  # secondary call, own smaller context budget than MAX_INGEST_CHARS


@app.post("/api/narrate-scenarios")
async def api_narrate_scenarios(req: NarrateScenariosRequest):
    """Stage 6 add-on: qualitative, business-language narratives for the downside/base/upside
    outcome bands, grounded in the confirmed variable card + driver ranking (which variables
    actually move IRR, per engine.run's driver_ranking) + the ingested materials -- never a
    restatement of the percentiles already on screen. See prompts.STAGE6_NARRATIVE_SYSTEM_PROMPT
    for the grounding rules that keep this from inventing business facts beyond the card/materials."""
    session = SESSIONS.get(req.session_id) if req.session_id else None
    ingested_excerpt = (session or {}).get("ingested_text", "")[:NARRATIVE_INGEST_EXCERPT_CHARS]

    card_context = {
        "variables": req.deal_config.get("variables") or [],
        "binary_events": req.deal_config.get("binary_events") or [],
    }
    results_context = {
        "percentiles": req.results.get("percentiles"),
        "driver_ranking": req.results.get("driver_ranking"),
        "probability_table": req.results.get("probability_table"),
        "binary_events_check": req.results.get("binary_events_check"),
        "unmapped_variables": req.results.get("unmapped_variables"),
        "exit_percentiles": req.results.get("exit_percentiles"),
    }

    user_content = (
        f"VARIABLE CARD (is_demo_placeholder={req.card_is_demo}):\n{json.dumps(card_context, indent=2)}\n\n"
        f"SIMULATION RESULTS:\n{json.dumps(results_context, indent=2)}\n\n"
        + (
            f"EXCERPTS FROM INGESTED BUSINESS MATERIALS (context/vocabulary only):\n{ingested_excerpt}"
            if ingested_excerpt
            else "No ingested business materials available for this session (demo/placeholder card) -- "
            "ground the narrative in the variable card alone and say so."
        )
    )
    try:
        narrative, _ = await llm.extract_structured(
            prompts.STAGE6_NARRATIVE_SYSTEM_PROMPT, user_content, prompts.NARRATE_SCENARIOS_TOOL
        )
    except (RuntimeError, llm.NativeUnavailable) as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return narrative


class DistributionPreviewRequest(BaseModel):
    distribution: str
    p10: float | None = None
    p50: float
    p90: float | None = None
    bounds: list = [None, None]


@app.post("/api/distribution-preview")
def api_distribution_preview(req: DistributionPreviewRequest):
    """A deterministic shape preview for one variable card -- no simulation, just what
    fit_and_sample() would draw for these exact inputs (see distributions.preview_distribution)."""
    variable = {"distribution": req.distribution, "p10": req.p10, "p50": req.p50, "p90": req.p90, "bounds": req.bounds}
    try:
        return distributions.preview_distribution(variable)
    except (KeyError, ValueError, ZeroDivisionError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))


class LboPreviewRequest(BaseModel):
    base_case_financials: dict
    fixed_assumptions: dict = {}
    exit_multiple_override: float | None = None


@app.post("/api/lbo-preview")
def api_lbo_preview(req: LboPreviewRequest):
    """A single deterministic LBO run off the confirmed base-case spine -- Stage 5.5, the
    "does this tie to Excel" checkpoint between confirming financials and running the Monte
    Carlo simulation. No sampling, no correlation machinery: just the same lbo_spine chain the
    simulation itself uses, degenerated to one path (see run_paths_from_spine's own docstring)."""
    fixed = dict(req.fixed_assumptions)
    if req.exit_multiple_override is not None:
        fixed["exit_multiple_override"] = req.exit_multiple_override
    try:
        return lbo_spine.run_paths_from_spine(
            req.base_case_financials, {}, fixed, {}, tax_rate_pct=25, detail=True
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))


class ProjectSaveRequest(BaseModel):
    project_id: str | None = None
    name: str
    session_id: str | None = None
    base_case_financials: dict | None = None
    fixed_assumptions: dict | None = None
    exit_multiple_override: float | None = None
    deal_config: dict | None = None
    results: dict | None = None
    card_is_demo: bool = False
    financials_is_demo: bool = False
    financials_confirmed: bool = False
    lbo_confirmed: bool = False


@app.post("/api/projects/save")
def api_save_project(req: ProjectSaveRequest):
    session = SESSIONS.get(req.session_id) if req.session_id else None
    project_id = req.project_id or str(uuid.uuid4())
    data = {
        "name": req.name,
        "ingested_text": session["ingested_text"] if session else "",
        "card": session["card"] if session else None,
        "chat_history": session["chat_history"] if session else [],
        "native_session_id": session.get("native_session_id") if session else None,
        "base_case_financials": req.base_case_financials,
        "fixed_assumptions": req.fixed_assumptions,
        "exit_multiple_override": req.exit_multiple_override,
        "deal_config": req.deal_config,
        "results": req.results,
        "card_is_demo": req.card_is_demo,
        "financials_is_demo": req.financials_is_demo,
        "financials_confirmed": req.financials_confirmed,
        "lbo_confirmed": req.lbo_confirmed,
    }
    saved = projects.save_project(project_id, data)
    return {"project_id": project_id, "updated_at": saved["updated_at"]}


@app.get("/api/projects")
def api_list_projects():
    return {"projects": projects.list_projects()}


@app.get("/api/projects/{project_id}")
def api_get_project(project_id: str):
    data = projects.load_project(project_id)
    if data is None:
        raise HTTPException(status_code=404, detail="project not found")
    # Rehydrate a live in-memory session so the reopened project can keep calling
    # /api/propose, /api/chat, /api/extract-financials as normal.
    session_id = str(uuid.uuid4())
    SESSIONS[session_id] = {
        "ingested_text": data.get("ingested_text", ""),
        "card": data.get("card"),
        "chat_history": data.get("chat_history", []),
        "native_session_id": data.get("native_session_id"),
    }
    return {**data, "id": project_id, "session_id": session_id}


class ProjectRenameRequest(BaseModel):
    name: str


@app.patch("/api/projects/{project_id}")
def api_rename_project(project_id: str, req: ProjectRenameRequest):
    data = projects.rename_project(project_id, req.name)
    if data is None:
        raise HTTPException(status_code=404, detail="project not found")
    return {"id": project_id, "name": data["name"], "updated_at": data["updated_at"]}


@app.delete("/api/projects/{project_id}")
def api_delete_project(project_id: str):
    if not projects.delete_project(project_id):
        raise HTTPException(status_code=404, detail="project not found")
    return {"deleted": True}


class SlideRequest(BaseModel):
    deal_config: dict
    results: dict
    scenario_narratives: dict | None = None


_SMART_PUNCTUATION = {
    "—": "-", "–": "-",  # em/en dash
    "‘": "'", "’": "'",  # curly single quotes
    "“": '"', "”": '"',  # curly double quotes
}


def _safe_filename_stem(deal_name):
    """An AI-proposed deal name routinely carries an em-dash or curly quotes (e.g. "Project Willow
    ... -- FP VII/FP VIII take-private"), and Starlette's Content-Disposition header must be
    latin-1-encodable -- an un-sanitized name 500s the whole export the moment the deal name isn't
    plain ASCII, which is closer to the common case than the exception for a real AI-drafted deal
    name. Normalizes common "smart" punctuation to its plain-ASCII equivalent (rather than just
    dropping it, so the name doesn't lose its punctuation-derived structure), drops anything still
    non-ASCII, and strips characters Windows won't allow in a filename."""
    name = deal_name or "deal"
    for smart, plain in _SMART_PUNCTUATION.items():
        name = name.replace(smart, plain)
    name = name.encode("ascii", "ignore").decode("ascii")
    name = re.sub(r'[\\/:*?"<>|]', "", name)
    name = re.sub(r"\s+", "_", name.strip())
    return name or "deal"


@app.post("/api/export-slide")
def api_export_slide(req: SlideRequest):
    try:
        buf = slide_export.build_slide(req.deal_config, req.results, req.scenario_narratives)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    deal_name = _safe_filename_stem(req.deal_config.get("deal"))
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        headers={"Content-Disposition": f'attachment; filename="{deal_name}_monte_carlo.pptx"'},
    )


@app.post("/api/export-slide-open")
def api_export_slide_open(req: SlideRequest):
    """Builds the slide and opens it directly in the user's own local PowerPoint (via COM
    automation, see slide_export.open_in_powerpoint) instead of handing back bytes to save
    manually -- this app runs locally, so the backend and the browser share the same machine."""
    try:
        buf = slide_export.build_slide(req.deal_config, req.results, req.scenario_narratives)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    deal_name = _safe_filename_stem(req.deal_config.get("deal"))
    filename = f"{deal_name}_monte_carlo.pptx"
    try:
        slide_export.open_in_powerpoint(buf, filename)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"opened": True, "filename": filename}
