"""Re-record finished benchmark runs for the replay demo (site/data/).

    python demo/record_library.py

Each run is replayed with the same scenario, seed and agent version. The model answers come
from .llm_cache, so the replay is the same run, at zero cost; the script checks that the
outcome and the diagnosis match the benchmark result file, and refuses to write a run that
does not match (or that needed a new model call).
Also records the rule-based agent on the same scenario and seed, for "What would rules do?".
"""

import json
import random
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "demo"))

from baselines.rule_based import RuleBasedAgent  # noqa: E402
from harness.generator import scenario_file  # noqa: E402
from raqib.agent import RaqibAgent  # noqa: E402
from raqib.llm import MODELS  # noqa: E402
from recorder import rules_summary, run_recorded  # noqa: E402

SITE = ROOT / "site" / "data"
SOURCES = {  # split -> benchmark result file of the frozen agent (v2)
    "gen-heldout-v2": "reports/final/gen-heldout-v2_raqib.json",
    "gen-validation": "reports/final/gen-validation-v2_raqib.json",
}
PER_SPLIT = {"gen-heldout-v2": 16, "gen-validation": 8}


def pick(runs: list[dict], n: int, rng: random.Random) -> list[dict]:
    """Half solved, half failed, spread over scenario families."""
    out = []
    for want in (True, False):
        pool = [r for r in runs if r["success"] == want]
        rng.shuffle(pool)
        seen = set()
        for r in sorted(pool, key=lambda r: r["family"] in seen):
            if len([x for x in out if x["success"] == want]) >= n // 2:
                break
            if r["family"] in seen:
                continue
            seen.add(r["family"])
            out.append(r)
    return out


def job(args):
    split, sid, seed, expected = args
    scenarios = {s["id"]: s for s in yaml.safe_load(scenario_file(split).read_text(encoding="utf-8"))}
    sc = scenarios[sid]
    rec = run_recorded(sc, split, RaqibAgent, seed, mode="replay", model=MODELS["strong"])
    got = rec["result"]
    same = (got["success"] == expected["success"] and got["diagnosis"] == expected["diagnosis"]
            and got["actions"] == expected["actions"])
    if not same or got["cost_usd"] > 0:
        return sid, seed, None, f"mismatch (success {got['success']} vs {expected['success']}, cost {got['cost_usd']})"
    rules = run_recorded(sc, split, RuleBasedAgent, seed, mode="replay")
    rec["rules"] = rules_summary(rules)
    return sid, seed, rec, "ok"


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=sorted(SOURCES), help="record one split only")
    ap.add_argument("--n", type=int, help="runs per split")
    args = ap.parse_args()
    rng = random.Random(7)
    (SITE / "runs").mkdir(parents=True, exist_ok=True)
    jobs = []
    for split, path in SOURCES.items():
        if args.only and split != args.only:
            continue
        if args.n:
            PER_SPLIT[split] = args.n
        data = json.loads((ROOT / path).read_text(encoding="utf-8"))
        for r in pick(data["runs"], PER_SPLIT[split], rng):
            jobs.append((split, r["scenario"], r["seed"],
                         {"success": r["success"], "diagnosis": r["diagnosis"],
                          "actions": [{k: a[k] for k in ("ts", "action", "params")} for a in r["actions"]]}))
    index = []
    with ProcessPoolExecutor(8) as ex:
        for sid, seed, rec, note in ex.map(job, jobs):
            print(f"{note:<8} {sid} seed={seed}")
            if rec is None:
                continue
            name = f"{sid}_s{seed}.json"
            (SITE / "runs" / name).write_text(json.dumps(rec, ensure_ascii=False, default=str), encoding="utf-8")
            index.append({"file": f"runs/{name}", "id": sid, "split": rec["meta"]["split"], "seed": seed,
                          "success": rec["result"]["success"], "faults": len(rec["scenario"]["faults"])})
    (SITE / "library.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    print(f"{len(index)} runs recorded -> site/data/")


if __name__ == "__main__":
    main()
