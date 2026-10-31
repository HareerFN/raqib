"""Run the benchmark.

  python run_benchmark.py --split dev --agent oracle
  python run_benchmark.py --split validation --agent null --seeds 1 2 3
  python run_benchmark.py --split heldout --agent raqib --final   # sealed set, final evaluation only
  python run_benchmark.py --split gen-dev --agent rule_based --n 60
Generated splits (gen-*) are read from scenarios/gen_*.yaml, written by harness/generator.py.
"""

import argparse
import json
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import yaml

from baselines.llm_single_shot import LLMSingleShotAgent
from baselines.rule_based import RuleBasedAgent
from harness.reference_agents import NullAgent, OracleAgent
from harness.generator import SPLITS as GEN_SPLITS
from harness.generator import scenario_file
from harness.runner import as_dict, run_one
from raqib.agent import RaqibAgent

ROOT = Path(__file__).resolve().parent
AGENTS = {
    "null": NullAgent,                    # reference: does nothing
    "oracle": OracleAgent,                # reference: knows the answer
    "rule_based": RuleBasedAgent,         # baseline 1: runbook automation
    "llm_single_shot": LLMSingleShotAgent,  # baseline 2: one LLM call (needs OPENROUTER_API_KEY)
    "raqib": RaqibAgent,                  # the evidence-based repair agent
}
# ablations: the same agent with one part switched off
ABLATIONS = {
    "raqib_no_ledger": {"gate": False},              # act on any hypothesis, no evidence gate
    "raqib_no_discriminating": {"discriminate": False},  # no timing test, no extra investigation
    "raqib_no_topology": {"use_topology": False},    # no dependency graph
    "raqib_metrics_only": {"use_invariants": False},  # no business invariants (detection and evidence)
}
for _name, _flags in ABLATIONS.items():
    AGENTS[_name] = (lambda flags, name: lambda **kw: RaqibAgent(name=name, **flags, **kw))(_flags, _name)


SEALED = ("heldout", "gen-heldout")


def load_split(split: str, n: int | None = None) -> list[dict]:
    path = scenario_file(split) if split in GEN_SPLITS else ROOT / "scenarios" / f"{split}.yaml"
    if not path.exists():
        raise SystemExit(f"{path.name} not found: run python -m harness.generator --split {split}")
    scenarios = yaml.safe_load(path.read_text(encoding="utf-8"))
    return scenarios[:n] if n else scenarios


def summarize(results: list[dict]) -> dict:
    n = len(results)
    fixable = [r for r in results if r["fixable"]]
    pct = lambda xs: round(100 * sum(xs) / len(xs), 1) if xs else None  # noqa: E731
    pct_of = lambda a, b: round(100 * a / b, 1) if b else None  # noqa: E731
    mean = lambda xs: round(sum(xs) / len(xs), 1) if xs else None  # noqa: E731
    return {
        "runs": n,
        "success_rate": pct([r["success"] for r in results]),
        "clean_success_rate": pct([r["success"] and r["unnecessary_actions"] == 0 for r in results]),
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
        # proof metrics
        "evidence_cited": sum(r["evidence_cited"] for r in results),
        "evidence_validity": pct_of(sum(r["evidence_valid"] for r in results),
                                    sum(r["evidence_cited"] for r in results)),
        "recovery_claims": sum(r["recovery_claim"] is not None for r in results),
        "false_recovery_claims": sum(r["false_recovery_claim"] for r in results),
        "false_failure_claims": sum(r["false_failure_claim"] for r in results),
    }


def _job(args):
    split, sc, agent, seed = args
    return as_dict(run_one(sc, split, AGENTS[agent], seed))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev", choices=["dev", "validation", "heldout", *GEN_SPLITS])
    ap.add_argument("--agent", default="null", choices=sorted(AGENTS))
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    ap.add_argument("--n", type=int, default=None, help="generated splits: use the first n scenarios")
    ap.add_argument("--final", action="store_true", help="required to run a sealed held-out set")
    ap.add_argument("--workers", type=int, default=1, help="run scenarios in parallel processes")
    args = ap.parse_args()

    if args.split in SEALED and not args.final and args.agent not in ("null", "oracle"):
        raise SystemExit(f"{args.split} is sealed: add --final only for the final evaluation")

    scenarios = load_split(args.split, args.n)
    results, t0 = [], time.time()
    jobs = [(args.split, sc, args.agent, seed) for sc in scenarios for seed in args.seeds]
    with ProcessPoolExecutor(args.workers) as ex:
        for (_, sc, _, seed), r in zip(jobs, ex.map(_job, jobs)):
            results.append(r)
            mark = "PASS" if r["success"] else "fail"
            print(f"{mark}  {sc['id']:<28} {sc['family'][:40]:<40} seed={seed}  detected={r['detected']!s:<5} "
                  f"cause={r['root_cause_correct']!s:<5} recovered={r['recovered']!s:<5} "
                  f"escalated={r['escalated']!s:<5} harm={r['harmful_actions']}")

    summary = summarize(results)
    print("\nSUMMARY", json.dumps(summary, indent=2))
    out = ROOT / "results" / f"{args.split}_{args.agent}_{int(time.time())}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"split": args.split, "agent": args.agent, "seeds": args.seeds,
                               "summary": summary, "runs": results}, indent=2, default=str))
    print(f"saved {out.relative_to(ROOT)}  ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
