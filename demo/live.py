"""Live session: the simulated system runs in real time, faults are injected on demand,
and the real agent runs on it. Used by demo/server.py.

One simulation thread owns the system (SQLite connections belong to the thread that made
them). It steps the clock at the chosen speed; when the agent runs, the agent drives the
clock through Env.wait, paced at the same speed. The web server only reads snapshots.
"""

import random
import threading
import time
import traceback

import yaml

from baselines.rule_based import RuleBasedAgent
from harness.env import ACTIONS, TOPOLOGY, BudgetExhausted, Env
from harness.generator import LIB, _conflict, _fault_event, _sample, _truth, check, compose
from harness.runner import RULES, WARMUP, _schedule, as_dict, run_one
from raqib.agent import RaqibAgent
from raqib.llm import MODELS
from target.system import TargetSystem

SAMPLE_EVERY = 5
BUDGET = 1500
BY_NAME = {p["mechanism"]: p for p in LIB["primitives"]}


def catalog() -> list[dict]:
    """Every fault the viewer can inject: mechanism, where it can occur, and with which origin."""
    out = []
    for p in LIB["primitives"]:
        out.append({"mechanism": p["mechanism"], "needs_load": bool(p.get("needs_load")),
                    "services": {svc: spec["origins"] for svc, spec in p["services"].items()},
                    "split": sorted({spec["split"] for spec in p["services"].values()})})
    return out


def build(picks: list[dict], rng: random.Random) -> dict:
    """A scenario from faults chosen by hand, with the generator's own event and truth rules."""
    chosen, faults, events = [], [], []
    for i, pick in enumerate(picks):
        p = BY_NAME[pick["mechanism"]]
        svc, origin = pick["service"], pick["origin"]
        spec = p["services"].get(svc)
        if spec is None or origin not in spec["origins"]:
            raise ValueError(f"{p['mechanism']} cannot occur in {svc} with origin {origin}")
        for q, s, _ in chosen:
            if s == svc or _conflict(p, q):
                raise ValueError(f"{p['mechanism']} cannot be combined with {q['mechanism']} in this demo")
        at = 0 if i == 0 else rng.randint(30, 90)
        params = {k: _sample(rng, v) for k, v in {**p.get("params", {}), **spec.get("params", {})}.items()}
        if p.get("service_param"):
            params["service"] = svc
        faults.append({"mechanism": p["mechanism"], "service": svc, "origin": origin, "at": at, "params": params})
        events.append({**_fault_event(rng, p, svc, origin, at, params), "source": f"fault:{i}"})
        chosen.append((p, svc, spec))
    loaded = [f for f, (p, _, _) in zip(faults, chosen) if p.get("needs_load")]
    if loaded:
        start = loaded[0]["at"] + 20
        events += [{"at": start, "source": "traffic", "config": {"traffic_rate": "14.0"}},
                   {"at": start + 130, "source": "traffic", "config": {"traffic_rate": "2.0"}}]
    events.sort(key=lambda e: e["at"])
    return {"id": f"live_{rng.randint(10_000, 99_999)}", "family": "+".join(f["mechanism"] for f in faults),
            "events": events, "truth": _truth(chosen, faults), "generated": {"seed": None, "split": "live", "faults": faults}}


class LiveSession:
    def __init__(self, seed: int | None = None):
        self.seed = seed or random.randint(1, 999)
        self.speed = 10.0                        # simulated seconds per real second
        self.samples: list[dict] = []
        self.deploys: list[dict] = []
        self.commands: list[tuple] = []
        self.scenario = None
        self.onset = None
        self.injected_at = None
        self.agent = None
        self.env = None
        self.agent_status = "idle"               # idle | running | done | error
        self.agent_finished_at = None
        self.pending = None                      # approval waiting for the viewer
        self._decision = None
        self._decided = threading.Event()
        self.approvals: list[dict] = []
        self.rules = None
        self.rules_status = "idle"
        self.error = None
        self.closed = False
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._main, daemon=True)
        self.thread.start()
        self.ready.wait(30)

    # ------------------------------------------------------------ simulation thread
    def _main(self):
        try:
            self.system = TargetSystem(seed=self.seed)
            self.system.run(WARMUP)
            self.start = self.system.clock.now
            self.log_at = len(self.system.log.records)
            self.fire = lambda system: None
            self._sample()
            self.ready.set()
            while not self.closed:
                if self.commands:
                    cmd, arg = self.commands.pop(0)
                    getattr(self, f"_do_{cmd}")(arg)
                    continue
                self._tick()
                time.sleep(1.0 / self.speed)
        except Exception as exc:  # noqa: BLE001 (shown in the page)
            self.error = f"{exc}\n{traceback.format_exc()[-800:]}"
            self.ready.set()

    def _tick(self):
        self.fire(self.system)
        self.system.step()
        if self.system.clock.now % SAMPLE_EVERY == 0:
            self._sample()

    def _paced(self, system):
        """Scheduler handed to the agent's Env: same events, sampling, real-time pacing."""
        self.fire(system)
        if system.clock.now % SAMPLE_EVERY == 0:
            self._sample()
        time.sleep(1.0 / self.speed)

    def _sample(self):
        s = self.system
        logs: dict[str, list[int]] = {}
        tail = []
        for r in s.log.records[self.log_at:]:
            if r["service"] in ("ops", "deployer"):
                continue
            c = logs.setdefault(r["service"], [0, 0, 0])
            c[{"INFO": 0, "WARN": 1, "ERROR": 2}.get(r["level"], 0)] += 1
            tail.append({"ts": r["ts"], "service": r["service"], "level": r["level"], "event": r["event"]})
        self.log_at = len(s.log.records)
        now = s.clock.now
        self.samples.append({"t": now, "inv": {r.id: r.violations for r in RULES.check(s.db, now)}, "logs": logs,
                             "tail": tail[-6:], "queue": s.metrics.gauges.get("queue_depth", 0), "pool": s.db_pool_in_use,
                             "orders": s.metrics.counters.get("orders_created", 0),
                             "done": s.metrics.counters.get("orders_fulfilled", 0)})
        self.deploys = [dict(d) for d in s.db.execute("SELECT ts, service, version, note FROM deploys ORDER BY id")]

    def _do_inject(self, scenario):
        self.scenario = scenario
        self.onset = self.system.clock.now + 2
        self.injected_at = self.onset
        self.fire = _schedule(scenario, self.onset)

    def _do_agent(self, _):
        self.agent_status = "running"
        self.env = Env(self.system, self._paced, BUDGET, approver=self._approver)
        self.agent = RaqibAgent(watch_start=self.start)
        try:
            self.agent.run(self.env)
        except BudgetExhausted:
            pass
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            self.agent_status = "error"
            return
        if not self.env.done:
            self.env.finish(recovered=False, report={"reason": "time budget used up"})
        self.agent_status = "done"
        self.agent_finished_at = self.system.clock.now

    def _approver(self, action: str) -> bool:
        self.pending = {"action": action, "risk": ACTIONS[action]["risk"], "description": ACTIONS[action]["description"]}
        self._decided.clear()
        ok = self._decided.wait(600)
        decision = bool(self._decision) if ok else False
        self.approvals.append({"t": self.system.clock.now, "action": action, "approved": decision,
                               "by": "viewer" if ok else "timeout"})
        self.pending, self._decision = None, None
        return decision

    # ------------------------------------------------------------ commands from the web server
    def inject(self, picks: list[dict] | None):
        if self.scenario is not None:
            raise ValueError("a fault is already injected: reset first")
        rng = random.Random()
        if picks:
            scenario = build(picks, rng)
        else:                                    # surprise: the benchmark generator, held-out mechanisms
            for _ in range(12):
                scenario = compose(rng.randint(500_000, 999_999), "gen-heldout")
                if not check(scenario, seeds=(self.seed,)):
                    break
            scenario["id"] = scenario["id"].replace("gen_heldout", "live")
        self.commands.append(("inject", scenario))
        return {"faults": len(scenario["generated"]["faults"]), "hidden": not picks}

    def run_agent(self):
        if self.agent_status != "idle":
            raise ValueError("Raqib is already running or done")
        self.commands.append(("agent", None))

    def decide(self, approve: bool):
        if self.pending is None:
            raise ValueError("no approval is pending")
        self._decision = approve
        self._decided.set()

    def run_rules(self):
        if self.scenario is None or self.rules_status == "running":
            raise ValueError("inject a fault first")
        self.rules_status = "running"

        def work():
            r = as_dict(run_one(self.scenario, "live", RuleBasedAgent, self.seed))
            self.rules = {k: r[k] for k in ("success", "diagnosis", "actions", "recovered", "escalated",
                                           "recovery_claim", "false_recovery_claim", "root_cause_correct")}
            self.rules_status = "done"
        threading.Thread(target=work, daemon=True).start()

    def close(self):
        self.closed = True
        if self.pending:
            self.decide(False)

    # ------------------------------------------------------------ what the page reads
    def state(self, since_sample: int, since_event: int) -> dict:
        timeline = list(getattr(self.agent, "timeline", []) or [])
        return {
            "t": self.samples[-1]["t"] if self.samples else 0, "start": getattr(self, "start", 0),
            "speed": self.speed, "seed": self.seed, "model": MODELS["strong"],
            "injected": self.scenario is not None, "onset": self.onset,
            "hidden_faults": len(self.scenario["generated"]["faults"]) if self.scenario else 0,
            "samples": self.samples[since_sample:], "sample_count": len(self.samples),
            "deploys": self.deploys, "events": timeline[since_event:], "event_count": len(timeline),
            "agent": self.agent_status, "pending": self.pending, "approvals": self.approvals,
            "rules_status": self.rules_status, "error": self.error,
        }

    def tool_result(self, call_id: str) -> dict | None:
        if not self.env:
            return None
        for e in self.env.trace:
            if e["id"] == call_id and e["kind"] == "tool":
                result = e["result"]
                rows = [{"service": k, **v} for k, v in result.items()] if isinstance(result, dict) else list(result)
                return {"name": e["name"], "ts": e["ts"], "args": {k: v for k, v in (e.get("args") or {}).items()
                                                                   if v is not None},
                        "rows": len(rows), "preview": rows[:12], "matched": None}
        return None

    def reveal(self) -> dict:
        if self.scenario is None:
            raise ValueError("nothing injected")
        truth = self.scenario["truth"]
        diag = self.env.diagnosis if self.env else []
        matched = sum(any(d.get("service") == rc["service"] and d.get("kind") in rc["kinds"] for d in diag)
                      for rc in truth["root_causes"])
        wrong = [d for d in diag if not any(d.get("service") == rc["service"] for rc in truth["root_causes"])]
        clean = 0
        for s in reversed(self.samples):
            if any(s["inv"].values()):
                break
            clean += SAMPLE_EVERY
        return {"faults": [{k: f[k] for k in ("mechanism", "service", "origin")} for f in self.scenario["generated"]["faults"]],
                "truth": truth["root_causes"], "fixable": truth["fixable"], "diagnosis": diag,
                "root_cause_correct": matched == len(truth["root_causes"]) and not wrong,
                "escalated": bool(self.env and self.env.escalated),
                "escalation_correct": bool(self.env) and self.env.escalated == (not truth["fixable"]),
                "claim": self.env.recovered_claim if self.env else None, "clean_for": clean,
                "injected_text": [f.get("text") for e in self.scenario["events"] for f in e.get("faults", [])
                                  if f["name"] == "invoice_note"],
                "rules": self.rules}


def static_info() -> dict:
    return {"topology": TOPOLOGY, "invariants": [{"id": r["id"], "description": r["description"], "services": r["services"]}
                                                 for r in RULES.rules],
            "actions": {k: {"risk": v["risk"], "description": v["description"]} for k, v in ACTIONS.items()},
            "catalog": catalog()}


if __name__ == "__main__":
    print(yaml.safe_dump(catalog()[:2]))
