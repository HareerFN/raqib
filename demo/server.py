"""Live mode for the demo: runs the real agent on a fresh incident, locally.

    pip install fastapi uvicorn
    python demo/server.py            # then open http://127.0.0.1:8000

The page is the same one GitHub Pages serves (site/). When it finds this server it offers
Live mode: each fault card is a new scenario composed by the generator from the held-out
fault mechanisms with a random seed (500000 and up, so it is in no benchmark file), kept only
if it passes the generator's quality filters. The agent then runs for real (model calls go
to OpenRouter, cached in .llm_cache). When the agent asks for approval of a high-risk action,
the page asks the viewer, and the decision goes to the agent.
"""

import random
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "demo"))

import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from baselines.rule_based import RuleBasedAgent  # noqa: E402
from harness.generator import check, compose  # noqa: E402
from raqib.agent import RaqibAgent  # noqa: E402
from raqib.llm import MODELS  # noqa: E402
from recorder import rules_summary, run_recorded  # noqa: E402

SITE = ROOT / "site"
LIVE_SPLIT = "gen-heldout"            # fault mechanisms to draw from (never the sealed files themselves)
APPROVAL_TIMEOUT = 180
app = FastAPI(title="Raqib live demo")
RUNS: dict[str, dict] = {}


def fresh_scenario(rng: random.Random, run_seed: int) -> dict:
    """A new generated incident that passes the quality filters."""
    for _ in range(12):
        sc = compose(rng.randint(500_000, 999_999), LIVE_SPLIT)
        if not check(sc, seeds=(run_seed,)):
            sc["id"] = sc["id"].replace("gen_heldout", "live")
            return sc
    raise RuntimeError("could not compose a scenario that passes the filters")


class Start(BaseModel):
    card: int | None = None           # which face-down card (only changes the random draw)


class Decision(BaseModel):
    approve: bool


@app.get("/api/health")
def health():
    return {"live": True, "model": MODELS["strong"]}


@app.post("/api/runs")
def start(req: Start):
    run_id = uuid.uuid4().hex[:10]
    state = {"status": "preparing", "progress": {}, "pending": None, "decision": None,
             "event": threading.Event(), "record": None, "error": None, "started": time.time()}
    RUNS[run_id] = state

    def approver(action: str) -> bool:
        state["pending"] = {"action": action}
        state["event"].clear()
        ok = state["event"].wait(APPROVAL_TIMEOUT)
        decision = bool(state["decision"]) if ok else False
        state["pending"], state["decision"] = None, None
        state.setdefault("approvals", []).append({"action": action, "approved": decision,
                                                  "by": "viewer" if ok else "timeout"})
        return decision

    def progress(rec):
        state["progress"] = {"t": rec.samples[-1]["t"] if rec.samples else 0,
                             "events": len(rec.env.trace) if rec.env else 0}

    def work():
        try:
            seed = random.randint(1, 99)
            sc = fresh_scenario(random.Random(), seed)
            state["status"] = "running"
            record = run_recorded(sc, "live", RaqibAgent, seed, mode="live", approver=approver,
                                  on_progress=progress, model=MODELS["strong"])
            record["rules"] = rules_summary(run_recorded(sc, "live", RuleBasedAgent, seed, mode="live"))
            record["approvals"] = state.get("approvals", [])
            state["record"], state["status"] = record, "done"
        except Exception as exc:  # noqa: BLE001 (shown to the viewer)
            state["error"], state["status"] = str(exc), "error"

    threading.Thread(target=work, daemon=True).start()
    return {"id": run_id}


@app.get("/api/runs/{run_id}")
def status(run_id: str):
    s = RUNS.get(run_id)
    if s is None:
        raise HTTPException(404, "unknown run")
    return {k: s[k] for k in ("status", "progress", "pending", "record", "error")}


@app.post("/api/runs/{run_id}/approval")
def approval(run_id: str, d: Decision):
    s = RUNS.get(run_id)
    if s is None or s["pending"] is None:
        raise HTTPException(409, "no approval is pending")
    s["decision"] = d.approve
    s["event"].set()
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(SITE / "index.html")


app.mount("/", StaticFiles(directory=SITE), name="site")

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
