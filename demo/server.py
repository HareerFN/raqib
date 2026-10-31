"""Raqib control room, live.

    pip install fastapi uvicorn
    python demo/server.py

Opens http://127.0.0.1:8000 . The simulated system runs in real time; you inject faults
(pick mechanism, service and origin, or let the generator surprise you), watch the symptoms,
then run the real agent (model calls go to OpenRouter, key in .env, answers cached in
.llm_cache). When it asks to run a high-risk action, you approve or deny it on the page.
The same page, without this server, is the recorded-results site on GitHub Pages.
"""

import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "demo"))

import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from live import LiveSession, static_info  # noqa: E402

SITE = ROOT / "site"
HOST, PORT = "127.0.0.1", 8000
app = FastAPI(title="Raqib control room")
session: dict = {"live": None}


def live() -> LiveSession:
    if session["live"] is None:
        session["live"] = LiveSession()
    return session["live"]


class Pick(BaseModel):
    mechanism: str
    service: str
    origin: str


class Inject(BaseModel):
    faults: list[Pick] = []          # empty = surprise me


class Speed(BaseModel):
    speed: float


class Decision(BaseModel):
    approve: bool


def guard(fn):
    try:
        return fn()
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/health")
def health():
    return {"live": True}


@app.get("/api/info")
def info():
    return {**static_info(), "seed": live().seed}


@app.get("/api/state")
def state(since_sample: int = 0, since_event: int = 0):
    return live().state(since_sample, since_event)


@app.post("/api/inject")
def inject(req: Inject):
    return guard(lambda: live().inject([f.model_dump() for f in req.faults]))


@app.post("/api/agent")
def agent():
    guard(live().run_agent)
    return {"ok": True}


@app.post("/api/approval")
def approval(d: Decision):
    guard(lambda: live().decide(d.approve))
    return {"ok": True}


@app.post("/api/speed")
def speed(s: Speed):
    live().speed = max(1.0, min(60.0, s.speed))
    return {"speed": live().speed}


@app.get("/api/trace/{call_id}")
def trace(call_id: str):
    r = live().tool_result(call_id)
    if r is None:
        raise HTTPException(404, "no such tool call")
    return r


@app.get("/api/reveal")
def reveal():
    return guard(live().reveal)


@app.post("/api/rules")
def rules():
    guard(live().run_rules)
    return {"ok": True}


@app.post("/api/reset")
def reset():
    if session["live"]:
        session["live"].close()
    session["live"] = LiveSession()
    return {"ok": True, "seed": session["live"].seed}


@app.get("/")
def index():
    return FileResponse(SITE / "index.html")


app.mount("/", StaticFiles(directory=SITE), name="site")

if __name__ == "__main__":
    if "--no-browser" not in sys.argv:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://{HOST}:{PORT}/")).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
