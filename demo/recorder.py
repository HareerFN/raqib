"""Records one benchmark run for the demo: what the viewer sees, not what the agent sees.

The recorder sits on the harness side (runner hooks). Every 10 simulated seconds it samples
the business invariants, log volume per service and a few gauges, so the demo can show the
system going from healthy to broken. At the end it keeps the agent's timeline, the original
result of every tool call the agent cited, the harness verdict and the scenario (revealed
only at the end of the demo). It never changes what the agent sees: no extra Env calls.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import citations  # noqa: E402
from harness.env import ACTIONS, TOPOLOGY  # noqa: E402
from harness.runner import RULES, as_dict, run_one  # noqa: E402

SAMPLE_EVERY = 10
PREVIEW_ROWS = 6
MAX_VALUE = 220


def _clip(v):
    if isinstance(v, str) and len(v) > MAX_VALUE:
        return v[: MAX_VALUE - 1] + "…"
    return v


def _row(r):
    return {k: _clip(v) for k, v in r.items()} if isinstance(r, dict) else _clip(r)


class Recorder:
    def __init__(self, approver=None, on_progress=None):
        self.samples: list[dict] = []
        self.log_at = 0
        self.env = None
        self.agent = None
        self.onset = None
        self.start = None
        if approver:
            self.approver = approver           # live mode: a person decides
        self.on_progress = on_progress

    # ------------------------------------------------------------ runner hooks
    def on_start(self, system, env, onset):
        self.env, self.onset, self.start = env, onset, system.clock.now
        self.log_at = len(system.log.records)
        self._sample(system)

    def on_step(self, system):
        if system.clock.now % SAMPLE_EVERY == 0:
            self._sample(system)

    def on_end(self, env, agent):
        self.agent = agent

    def _sample(self, system):
        now = system.clock.now
        logs: dict[str, list[int]] = {}
        for r in system.log.records[self.log_at:]:
            if r["service"] in ("ops", "deployer"):
                continue
            c = logs.setdefault(r["service"], [0, 0, 0])
            c[{"INFO": 0, "WARN": 1, "ERROR": 2}.get(r["level"], 0)] += 1
        self.log_at = len(system.log.records)
        inv = {r.id: r.violations for r in RULES.check(system.db, now)}
        g = system.metrics.gauges
        self.samples.append({"t": now, "inv": inv, "logs": logs,
                             "queue": g.get("queue_depth", 0), "pool": system.db_pool_in_use,
                             "orders": system.metrics.counters.get("orders_created", 0),
                             "done": system.metrics.counters.get("orders_fulfilled", 0)})
        if self.on_progress:
            self.on_progress(self)

    # ------------------------------------------------------------ the record
    def trace_for_demo(self) -> dict:
        """Original results of the tool calls the agent cited, plus every action and approval."""
        env = self.env
        cited: dict[str, list[dict]] = {}
        for e in getattr(self.agent, "timeline", []):
            for ev in (e.get("evidence") or {}).values() if isinstance(e.get("evidence"), dict) else []:
                if ev.get("call"):
                    cited.setdefault(ev["call"], [])
        for e in env.trace:
            if e["kind"] == "diagnosis":
                for item in e.get("evidence") or []:
                    if isinstance(item, dict) and item.get("id"):
                        cited.setdefault(item["id"], []).append(item)
        out = {}
        for e in env.trace:
            if e["kind"] == "tool" and e["id"] in cited:
                result = e["result"]
                rows = citations._records(result)
                matching = [r for r in rows if any(citations.supports([r], c) for c in cited[e["id"]])]
                preview = matching[:PREVIEW_ROWS] or rows[:PREVIEW_ROWS]
                out[e["id"]] = {"name": e["name"], "ts": e["ts"],
                                "args": {k: v for k, v in (e.get("args") or {}).items() if v is not None},
                                "rows": len(rows), "preview": [_row(r) for r in preview],
                                "matched": len(matching)}
            elif e["kind"] in ("action", "approval", "action_blocked", "escalation", "incident"):
                out[e["id"]] = {k: v for k, v in e.items() if k != "id"}
        return out

    def record(self, scenario: dict, split: str, result, mode: str, seed: int, model: str | None) -> dict:
        r = as_dict(result)
        return {
            "meta": {"id": scenario["id"], "split": split, "seed": seed, "family": scenario["family"],
                     "mode": mode, "agent": r["agent"], "onset": self.onset, "start": self.start, "model": model},
            "scenario": {"faults": faults_of(scenario), "truth": scenario["truth"]["root_causes"],
                         "fixable": scenario["truth"]["fixable"],
                         "needs_approval": scenario["truth"].get("needs_approval", []),
                         "deploys": [{"at": e["at"], **{k: e["deploy"][k] for k in ("service", "version", "note")}}
                                     for e in scenario["events"] if "deploy" in e]},
            "topology": TOPOLOGY,
            "actions": {k: {"risk": v["risk"], "description": v["description"]} for k, v in ACTIONS.items()},
            "invariants": [{"id": rule["id"], "description": rule["description"], "services": rule["services"]}
                           for rule in RULES.rules],
            "samples": self.samples,
            "timeline": getattr(self.agent, "timeline", []),
            "trace": self.trace_for_demo(),
            "result": {k: r[k] for k in ("success", "detected", "mttd", "mttr", "root_cause_correct", "recovered",
                                         "escalated", "escalation_correct", "harmful_actions",
                                         "unnecessary_actions", "stable", "recovery_claim", "false_recovery_claim",
                                         "false_failure_claim", "evidence_cited", "evidence_valid", "cost_usd",
                                         "llm_calls", "diagnosis", "actions", "final_violations")},
        }


def faults_of(sc: dict) -> list[dict]:
    if "generated" in sc:
        return [{"mechanism": f["mechanism"], "service": f["service"], "origin": f["origin"],
                 "at": f["at"]} for f in sc["generated"]["faults"]]
    out = []
    for e in sc["events"]:
        for f in e.get("faults", []):
            if f["name"] == "invoice_note":
                continue
            origin, _, svc = f["origin"].partition(":")
            out.append({"mechanism": f["name"], "service": svc, "origin": origin, "at": e["at"]})
        if (e.get("deploy") or {}).get("config"):
            out.append({"mechanism": "config change", "service": e["deploy"]["service"], "origin": "config",
                        "at": e["at"]})
    return out


def injected_text(sc: dict) -> list[str]:
    return [f.get("text", "") for e in sc["events"] for f in e.get("faults", []) if f["name"] == "invoice_note"]


def run_recorded(scenario: dict, split: str, agent_factory, seed: int, mode: str = "replay",
                 approver=None, on_progress=None, model: str | None = None) -> dict:
    rec = Recorder(approver=approver, on_progress=on_progress)
    result = run_one(scenario, split, agent_factory, seed, hooks=rec)
    out = rec.record(scenario, split, result, mode, seed, model)
    out["scenario"]["injected_text"] = injected_text(scenario)
    return out


def rules_summary(record: dict) -> dict:
    r = record["result"]
    return {k: r[k] for k in ("success", "diagnosis", "actions", "recovered", "escalated", "recovery_claim",
                              "false_recovery_claim", "root_cause_correct")}


if __name__ == "__main__":
    print(json.dumps({"ok": True}))
