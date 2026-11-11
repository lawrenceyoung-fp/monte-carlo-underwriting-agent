"""FastAPI wrapper around engine.run -- the HTTP surface a Tines Story's HTTP Request action calls.

Run locally with:  uvicorn simulation_service.app:app --reload --port 8008
POST /simulate  { "deal_config": {...per schemas/deal_config.schema.yaml...}, "n_paths": 10000, "seed": 42 }
GET  /health
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import engine

app = FastAPI(title="Monte Carlo Underwriting Agent -- simulation service")


class SimulateRequest(BaseModel):
    deal_config: dict
    n_paths: int = 10000
    seed: int = 42


@app.get("/health")
def health():
    return {"status": "ok", "engine_version": engine.ENGINE_VERSION}


@app.post("/simulate")
def simulate(req: SimulateRequest):
    try:
        return engine.run(req.deal_config, n_paths=req.n_paths, seed=req.seed)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
