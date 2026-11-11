"""JSON-file-per-project persistence for "Save Project" / "Open Saved Project". Plain files, no
DB -- matches this app's existing local-single-user-hackathon-timeline posture (see app/main.py's
own SESSIONS docstring). Each project is one file: app/saved_projects/<uuid>.json.
"""

import json
import pathlib
from datetime import datetime, timezone

PROJECTS_DIR = pathlib.Path(__file__).resolve().parent / "saved_projects"
PROJECTS_DIR.mkdir(exist_ok=True)


def _path(project_id):
    return PROJECTS_DIR / f"{project_id}.json"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _stage_reached(data):
    stage = "New"
    if data.get("ingested_text"):
        stage = "Ingested"
    if data.get("card"):
        stage = "Variable card"
    if data.get("base_case_financials"):
        stage = "Financials"
    if data.get("financials_confirmed"):
        stage = "LBO mechanics"
    if data.get("results"):
        stage = "Simulated"
    return stage


def list_projects():
    """Summaries only -- id, name, timestamps, and a stage badge -- for the project-list menu."""
    out = []
    for path in PROJECTS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            "id": path.stem,
            "name": data.get("name") or "Untitled project",
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
            "stage_reached": _stage_reached(data),
        })
    out.sort(key=lambda p: p["updated_at"] or "", reverse=True)
    return out


def load_project(project_id):
    path = _path(project_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_project(project_id, data):
    """Upsert -- preserves created_at from any existing file, always bumps updated_at."""
    existing = load_project(project_id)
    now = _now_iso()
    data = dict(data)
    data["created_at"] = (existing or {}).get("created_at", now)
    data["updated_at"] = now
    _path(project_id).write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def rename_project(project_id, name):
    data = load_project(project_id)
    if data is None:
        return None
    data["name"] = name
    data["updated_at"] = _now_iso()
    _path(project_id).write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def delete_project(project_id):
    path = _path(project_id)
    if not path.exists():
        return False
    path.unlink()
    return True
