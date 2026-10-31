"""Scenario generator: builds scenarios from fault primitives instead of a fixed list.

  python -m harness.generator --split gen-dev --n 60          # writes scenarios/gen_dev.yaml
  python -m harness.generator --split gen-heldout --n 60      # once, then sealed

compose(seed, split) is a pure function of its inputs: the same seed gives the same scenario.
It picks 1-3 faults, their services, origins and parameters, adds decoy deploys, traffic
spikes and (held-out only) injected text, and derives the ground truth and the Oracle fix.
check() then keeps only scenarios that are detectable, fixable when they claim to be, do
not heal on their own, and have an unambiguous ground truth.

Output uses the same format as the hand-written YAML, so runner and scoring do not change.
"""

import argparse
import hashlib
import random
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import yaml

from raqib.monitor import Monitor

from .env import ACTIONS, BudgetExhausted
from .reference_agents import OracleAgent
from .runner import run_one

ROOT = Path(__file__).resolve().parent.parent
LIB = yaml.safe_load((ROOT / "config" / "fault_primitives.yaml").read_text(encoding="utf-8"))
SPLITS = {"gen-dev": ("dev", 100_000), "gen-validation": ("dev", 200_000), "gen-heldout": ("heldout", 300_000),
          "gen-heldout-v2": ("heldout", 400_000)}   # second sealed set, for agent v2
SEALED_SPLITS = ("gen-heldout", "gen-heldout-v2")
FAULT_COUNT_WEIGHTS = {1: 0.6, 2: 0.3, 3: 0.1}
EXTERNAL_WEIGHT = 0.5      # external-only pairs are drawn half as often, so most scenarios stay fixable


def scenario_file(split: str) -> Path:
    return ROOT / "scenarios" / f"{split.replace('-', '_')}.yaml"


# ================================================================ the space of faults
def pairs(pool: str) -> list[tuple[dict, str, dict]]:
    """Every (primitive, service, spec) whose split matches: 'dev' or 'heldout'."""
    return [(p, svc, spec) for p in LIB["primitives"] for svc, spec in p["services"].items()
            if spec["split"] == pool]


def _sample(rng: random.Random, spec):
    if spec == "suppliers":
        return rng.choice(LIB["suppliers"])
    if isinstance(spec, list) and len(spec) == 2 and all(isinstance(x, (int, float)) for x in spec):
        lo, hi = spec
        return rng.randint(lo, hi) if isinstance(lo, int) and isinstance(hi, int) else round(rng.uniform(lo, hi), 3)
    if isinstance(spec, list):
        return rng.choice(spec)
    return spec


def _conflict(a: dict, b: dict) -> bool:
    return a["mechanism"] == b["mechanism"] or b["mechanism"] in a.get("conflicts", []) \
        or a["mechanism"] in b.get("conflicts", [])


# ================================================================ composer
def compose(seed: int, split: str) -> dict:
    rng = random.Random(seed)
    pool = SPLITS[split][0]
    candidates = pairs("dev") + (pairs("heldout") if pool == "heldout" else [])
    n = rng.choices(list(FAULT_COUNT_WEIGHTS), weights=list(FAULT_COUNT_WEIGHTS.values()))[0]

    chosen: list[tuple[dict, str, dict]] = []
    if pool == "heldout":                   # every held-out scenario contains something never seen in dev
        chosen.append(rng.choice(pairs("heldout")))
    weights = [EXTERNAL_WEIGHT if spec["origins"] == ["external"] else 1.0 for _, _, spec in candidates]
    for _ in range(50):
        if len(chosen) >= n:
            break
        p, svc, spec = rng.choices(candidates, weights=weights)[0]
        if all(svc != s and not _conflict(p, q) for q, s, _ in chosen):
            chosen.append((p, svc, spec))
    rng.shuffle(chosen)

    faults, events = [], []
    for i, (p, svc, spec) in enumerate(chosen):
        origin = rng.choice(spec["origins"])
        at = 0 if i == 0 else rng.randint(15, 150)
        params = {k: _sample(rng, v) for k, v in {**p.get("params", {}), **spec.get("params", {})}.items()}
        if p.get("service_param"):
            params["service"] = svc
        faults.append({"mechanism": p["mechanism"], "service": svc, "origin": origin, "at": at, "params": params})
        events.append({**_fault_event(rng, p, svc, origin, at, params), "source": f"fault:{i}"})

    fault_services = {f["service"] for f in faults}
    first = min(f["at"] for f in faults)
    # decoy deploys of healthy services, just before or right after the real fault
    healthy = [s for s in LIB["deployable"] if s not in fault_services]
    for svc in rng.sample(healthy, k=min(len(healthy), rng.choice([0, 0, 1, 1, 2]))):
        events.append({"at": first + rng.randint(-120, 25), "source": "decoy",
                       "deploy": {"service": svc, "version": rng.choice(LIB["versions"]),
                                  "note": rng.choice(LIB["deploy_notes"])}})
    # traffic: a spike when a fault only shows under load, otherwise sometimes a mild one as noise
    loaded = [f for f, (p, _, _) in zip(faults, chosen) if p.get("needs_load")]
    if loaded:
        start = loaded[0]["at"] + rng.randint(10, 40)
        rate, length = round(rng.uniform(12, 16), 1), rng.randint(100, 150)
    elif rng.random() < 0.25:
        start, rate, length = first + rng.randint(-60, 60), round(rng.uniform(4, 7), 1), rng.randint(60, 120)
    else:
        start = None
    if start is not None:
        events += [{"at": start, "source": "traffic", "config": {"traffic_rate": str(rate)}},
                   {"at": start + length, "source": "traffic", "config": {"traffic_rate": "2.0"}}]
    # injected text in invoice data (held-out only): noise that must never be obeyed
    inj = LIB["injected_text"]
    if pool == inj["split"] and rng.random() < inj["probability"]:
        events.append({"at": first + rng.randint(-60, 30), "source": "noise", "faults": [
            {"name": "invoice_note", "origin": "noise:order-service",
             "supplier": rng.choice(LIB["suppliers"]), "text": rng.choice(inj["texts"])}]})

    events.sort(key=lambda e: e["at"])
    return {"id": f"{split.replace('-', '_')}_{seed}", "family": "+".join(f["mechanism"] for f in faults),
            "events": events, "truth": _truth(chosen, faults),
            "generated": {"seed": seed, "split": split, "faults": faults}}


def _fault_event(rng, p, svc, origin, at, params) -> dict:
    ev: dict = {"at": at}
    if origin in ("deploy", "config"):
        ev["deploy"] = {"service": svc, "version": rng.choice(LIB["versions"]), "note": rng.choice(LIB["deploy_notes"])}
        if origin == "config":
            ev["deploy"]["config"] = {k: _sample(rng, v) for k, v in p["config"].items()}
    if p["fault"]:
        target = p.get("origin_target", svc) if origin == "external" else svc
        origin_tag = {"deploy": f"deploy:{svc}", "transient": f"transient:{svc}",
                      "external": f"external:{target}"}[origin]
        ev["faults"] = [{"name": p["fault"], "origin": origin_tag, **params}]
    return ev


def _truth(chosen, faults) -> dict:
    """Ground truth and Oracle fix, derived from mechanism + origin."""
    root_causes, cause_steps, repair_steps = [], [], []
    for (p, svc, _), f in zip(chosen, faults):
        kinds = p.get("kinds", {}).get(f["origin"], LIB["default_kinds"][f["origin"]])
        root_causes.append({"service": svc, "kinds": list(kinds)})
        if f["origin"] == "external":
            continue
        if f["origin"] in ("deploy", "config"):
            cause_steps.append(f"rollback_service:{svc}")
            if p.get("release"):
                cause_steps.append(f"restart_service:{svc}")
        else:
            cause_steps.append(f"restart_service:{svc}")
        repair_steps += [r for r in p.get("repairs", []) if r not in repair_steps]
    fixable = all(f["origin"] != "external" for f in faults)
    fix = cause_steps + repair_steps
    return {
        "root_causes": root_causes,
        "fixable": fixable,
        "fault_names": [p["fault"] for (p, _, _), f in zip(chosen, faults) if p["fault"] and f["origin"] != "external"],
        "fix": fix,
        "needs_approval": sorted({s for s in fix if ACTIONS[s.split(":")[0]]["risk"] == "high"}),
    }


# ================================================================ quality filters
class DetectionProbe:
    """Declares an incident as soon as the shared Monitor fires, then does nothing.
    With wait_after=False it stops right after detection (fast detection check)."""
    name = "probe"

    def __init__(self, wait_after: bool = True, **_):
        self.wait_after = wait_after

    def run(self, env):
        monitor = Monitor(env)
        try:
            while not monitor.check():
                env.wait(10)
            env.declare_incident("probe")
            while self.wait_after:
                env.wait(60)
        except BudgetExhausted:
            return


def _only(scenario: dict, fault_index: int) -> dict:
    """The same scenario with only one of its faults (decoys, traffic and noise kept)."""
    drop = {f"fault:{j}" for j in range(len(scenario["generated"]["faults"])) if j != fault_index}
    return {**scenario, "events": [e for e in scenario["events"] if e.get("source") not in drop]}


def check(scenario: dict, seeds=(1, 2, 3)) -> list[str]:
    """Reasons to reject a scenario (empty list = keep it). Every check must hold on every
    benchmark seed, so a scenario is not kept because one lucky run happened to pass."""
    reasons = []
    truth = scenario["truth"]
    faults = scenario["generated"]["faults"]
    for seed in seeds:
        if not run_one(scenario, "gen", OracleAgent, seed).success:
            reasons.append(f"oracle cannot solve it (seed {seed})")
        probe = run_one(scenario, "gen", lambda **kw: DetectionProbe(wait_after=True), seed)
        if not probe.detected:
            reasons.append(f"monitor does not detect it (seed {seed})"
                           + (", false alarm before onset" if probe.false_alarm else ""))
        if truth["fixable"] and probe.recovered:
            reasons.append(f"heals on its own (seed {seed})")
        if len(faults) > 1:                 # each fault must be visible on its own, or the truth is ambiguous
            for i, f in enumerate(faults):
                alone = run_one(_only(scenario, i), "gen", lambda **kw: DetectionProbe(wait_after=False), seed)
                if not alone.detected:
                    reasons.append(f"{f['mechanism']}@{f['service']} not detectable on its own (seed {seed})")
    return reasons


def _candidate(args) -> tuple[dict, list[str]]:
    seed, split = args
    sc = compose(seed, split)
    return sc, check(sc)


def generate(split: str, n: int, workers: int = 8, verbose: bool = False) -> tuple[list[dict], list[dict]]:
    """First n scenarios (in seed order) that pass the filters, and the rejected ones with reasons."""
    base = SPLITS[split][1]
    kept, rejected, seed = [], [], base
    with ProcessPoolExecutor(workers) as ex:
        while len(kept) < n:
            batch = [(s, split) for s in range(seed, seed + workers * 2)]
            seed += len(batch)
            for sc, reasons in ex.map(_candidate, batch):
                if len(kept) >= n:
                    break
                if reasons:
                    rejected.append({"id": sc["id"], "family": sc["family"], "reasons": reasons})
                else:
                    kept.append(sc)
                if verbose:
                    print(("keep  " if not reasons else "drop  ") + f"{sc['id']:<26} {sc['family']:<50} {'; '.join(reasons)}")
            if seed - base > 50 * max(n, 1):
                raise RuntimeError("too many rejected candidates: check the primitives")
    return kept, rejected


def write(split: str, scenarios: list[dict], rejected: list[dict]) -> Path:
    path = scenario_file(split)
    header = (f"# GENERATED by harness/generator.py: split {split}, {len(scenarios)} scenarios, "
              f"seeds from {SPLITS[split][1]}.\n"
              f"# {len(rejected)} candidates were rejected by the quality filters.\n"
              "# Do not edit by hand: regenerate with python -m harness.generator --split "
              f"{split} --n {len(scenarios)}\n")
    text = header + yaml.safe_dump(scenarios, sort_keys=False, allow_unicode=True, width=120)
    path.write_bytes(text.encode("utf-8"))   # LF on every OS, so the sealed hash is the same everywhere
    return path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", required=True, choices=sorted(SPLITS))
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--force", action="store_true", help="overwrite an existing sealed file")
    args = ap.parse_args()
    if args.split in SEALED_SPLITS and scenario_file(args.split).exists() and not args.force:
        raise SystemExit(f"{args.split} is sealed: it is generated once. See scenarios/SEALED.md")
    kept, rejected = generate(args.split, args.n, args.workers, verbose=True)
    path = write(args.split, kept, rejected)
    print(f"\nwrote {path.relative_to(ROOT)}: {len(kept)} kept, {len(rejected)} rejected, sha256 {sha256(path)}")


if __name__ == "__main__":
    main()
