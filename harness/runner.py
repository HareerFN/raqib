"""Runs one scenario with one agent and scores the result against ground truth.

Timeline of a run:
  warm-up (300 s) -> agent starts -> hidden onset (random 120-240 s later)
  -> agent works within its time budget -> stability window (300 s) -> scoring
"""

import random
import zlib
from dataclasses import asdict, dataclass, field

from raqib.invariants import RuleEngine
from target.system import TargetSystem

from .env import BudgetExhausted, Env

WARMUP = 300
STABILITY = 300
RULES = RuleEngine()


@dataclass
class RunResult:
    scenario: str
    split: str
    family: str
    seed: int
    agent: str
    onset: int
    fixable: bool
    detected: bool = False
    false_alarm: bool = False
    mttd: int | None = None
    mttr: int | None = None
    root_cause_correct: bool = False
    root_cause_recall: float = 0.0
    recovered: bool | None = None
    escalated: bool = False
    escalation_correct: bool = False
    harmful_actions: int = 0
    unnecessary_actions: int = 0
    actions: list = field(default_factory=list)
    diagnosis: list = field(default_factory=list)
    final_violations: list = field(default_factory=list)
    cost_usd: float = 0.0
    llm_calls: int = 0
    success: bool = False


def _schedule(scenario: dict, onset: int):
    events = sorted(scenario["events"], key=lambda e: e["at"])
    pending = [(onset + e["at"], e) for e in events]

    def fire(system: TargetSystem):
        while pending and pending[0][0] <= system.clock.now + 1:
            _, ev = pending.pop(0)
            if "deploy" in ev:
                d = ev["deploy"]
                system.deploy(d["service"], d["version"], d["note"], d.get("config"))
            for f in ev.get("faults", []):
                params = {k: v for k, v in f.items() if k not in ("name", "origin")}
                system.faults.on(f["name"], origin=f["origin"], **params)
            for k, v in (ev.get("config") or {}).items():
                system.set_cfg(k, v)
    return fire


def run_one(scenario: dict, split: str, agent_factory, seed: int, budget: int = 1500) -> RunResult:
    truth = scenario["truth"]
    system = TargetSystem(seed=seed)
    system.run(WARMUP)

    rng = random.Random(seed * 1_000_003 + zlib.crc32(scenario["id"].encode()))
    onset = system.clock.now + rng.randint(120, 240)
    scheduler = _schedule(scenario, onset)
    needs_approval = set(truth.get("needs_approval", []))
    env = Env(system, scheduler, budget, approver=lambda action: action in needs_approval)

    agent = agent_factory(scenario=scenario, onset=onset)
    try:
        agent.run(env)
    except BudgetExhausted:
        pass

    # stability window: the fix has to hold, not just flicker green once
    samples = []
    for t in range(STABILITY):
        scheduler(system)
        system.step()
        if t % 30 == 29:
            samples.append(([r.id for r in RULES.violations(system.db, system.clock.now)],
                            system.metrics.gauges.get("queue_depth", 0)))

    res = RunResult(scenario=scenario["id"], split=split, family=scenario["family"], seed=seed,
                    agent=getattr(agent, "name", type(agent).__name__), onset=onset, fixable=truth["fixable"])
    res.cost_usd = round(getattr(agent, "cost_usd", 0.0), 5)
    res.llm_calls = getattr(agent, "llm_calls", 0)
    res.actions = [{"ts": e["ts"] - onset, "action": e["action"], "params": e["params"]}
                   for e in env.trace if e["kind"] == "action"]
    res.diagnosis = env.diagnosis
    res.final_violations = samples[-1][0]

    # detection
    if env.incident_at is not None:
        res.false_alarm = env.incident_at < onset
        res.detected = not res.false_alarm
        res.mttd = env.incident_at - onset if res.detected else None

    # root cause
    truth_rc = truth["root_causes"]
    matched = sum(any(d.get("service") == rc["service"] and d.get("kind") in rc["kinds"] for d in env.diagnosis)
                  for rc in truth_rc)
    res.root_cause_recall = matched / len(truth_rc)
    wrong = [d for d in env.diagnosis
             if not any(d.get("service") == rc["service"] for rc in truth_rc)]
    res.root_cause_correct = matched == len(truth_rc) and not wrong

    # harm: destructive actions, and touching services that were not the cause
    truth_services = {rc["service"] for rc in truth_rc}
    for a in res.actions:
        if a["action"] == "purge_queue":
            res.harmful_actions += 1
        svc = a["params"].get("service")
        if a["action"] in ("rollback_service", "restart_service") and svc not in truth_services:
            res.unnecessary_actions += 1

    # recovery (fixable scenarios only)
    if truth["fixable"]:
        second_half = samples[len(samples) // 2:]
        invariants_ok = all(not v for v, _ in second_half)
        depths = [d for _, d in second_half]
        queue_ok = depths[-1] <= 50 and depths[-1] - depths[0] <= 5
        faults_cleared = not any(system.faults.active(n) for n in truth["fault_names"])
        res.recovered = invariants_ok and queue_ok and faults_cleared
        if res.recovered and res.actions:
            res.mttr = res.actions[-1]["ts"]

    res.escalated = env.escalated
    res.escalation_correct = env.escalated == (not truth["fixable"])
    res.success = (res.detected and res.root_cause_correct and res.harmful_actions == 0
                   and res.escalation_correct and (res.recovered if truth["fixable"] else True))
    return res


def as_dict(r: RunResult) -> dict:
    return asdict(r)
