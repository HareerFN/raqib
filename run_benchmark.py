"""Run the benchmark.

  python run_benchmark.py --split dev --agent oracle
  python run_benchmark.py --split validation --agent null --seeds 1 2 3
  python run_benchmark.py --split heldout --agent raqib --final   # sealed set, final evaluation only
"""

import argparse
import json
import time
from pathlib import Path

import yaml

from harness.reference_agents import NullAgent, OracleAgent
from harness.runner import as_dict, run_one

ROOT = Path(__file__).resolve().parent
AGENTS = {"null": NullAgent, "oracle": OracleAgent}


def load_split(split: str) -> list[dict]:
    return yaml.safe_load((ROOT / "scenarios" / f"{split}.yaml").read_text(encoding="utf-8"))


def summarize(results: list[dict]) -> dict:
    n = len(results)
    fixable = [r for r in results if r["fixable"]]
    pct = lambda xs: round(100 * sum(xs) / len(xs), 1) if xs else None  # noqa: E731
    mean = lambda xs: round(sum(xs) / len(xs), 1) if xs else None  # noqa: E731
    return {
        "runs": n,
        "success_rate": pct([r["success"] for r in results]),
        "detection_rate": pct([r["detected"] for r in results]),
        "false_alarms": sum(r["false_alarm"] for r in results),
        "root_cause_accuracy": pct([r["root_cause_correct"] for r in results]),
        "recovery_rate": pct([r["recovered"] for r in fixable]),
        "escalation_accuracy": pct([r["escalation_correct"] for r in results]),
        "harmful_actions": sum(r["harmful_actions"] for r in results),
        "unnecessary_actions": sum(r["unnecessary_actions"] for r in results),
        "mean_mttd_s": mean([r["mttd"] for r in results if r["mttd"] is not None]),
        "mean_mttr_s": mean([r["mttr"] for r in results if r["mttr"] is not None]),
        "cost_usd": round(sum(r["cost_usd"] for r in results), 4),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev", choices=["dev", "validation", "heldout"])
    ap.add_argument("--agent", default="null", choices=sorted(AGENTS))
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    ap.add_argument("--final", action="store_true", help="required to run the sealed held-out set")
    args = ap.parse_args()

    if args.split == "heldout" and not args.final and args.agent not in ("null", "oracle"):
        raise SystemExit("held-out is sealed: add --final only for the final evaluation")

    scenarios = load_split(args.split)
    results, t0 = [], time.time()
    for sc in scenarios:
        for seed in args.seeds:
            r = as_dict(run_one(sc, args.split, AGENTS[args.agent], seed))
            results.append(r)
            mark = "PASS" if r["success"] else "fail"
            print(f"{mark}  {sc['id']:<28} seed={seed}  detected={r['detected']!s:<5} "
                  f"cause={r['root_cause_correct']!s:<5} recovered={r['recovered']!s:<5} "
                  f"escalated={r['escalated']!s:<5} harm={r['harmful_actions']}")

    summary = summarize(results)
    print("\nSUMMARY", json.dumps(summary, indent=2))
    out = ROOT / "results" / f"{args.split}_{args.agent}_{int(time.time())}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"split": args.split, "agent": args.agent, "seeds": args.seeds,
                               "summary": summary, "runs": results}, indent=2))
    print(f"saved {out.relative_to(ROOT)}  ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
