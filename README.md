# Raqib: Evidence-Based Autonomous Recovery

An AI agent that detects silent failures through business invariants, proves the
root cause with evidence, applies the least risky fix, and proves the system
recovered without causing new harm.

## Quick start

    python -m venv venv
    venv\Scripts\activate            # Windows  (Mac/Linux: source venv/bin/activate)
    pip install pyyaml pytest
    python -m pytest -q              # 12 tests
    python run_benchmark.py --split dev --agent oracle

## Layout

    target/        the simulated system that breaks (virtual clock, SQLite in memory)
    config/        business invariants, dependency graph, repair actions with risk tiers
    scenarios/     dev / validation / held-out (sealed) failure scenarios with ground truth
    harness/       agent environment, runner and scoring, reference agents
    raqib/         the repair agent (next step)
    results/       benchmark outputs (JSON)

## Harness validation

| Agent  | Dev | Validation | Held-out |
|--------|-----|------------|----------|
| Null   | 0%  | 0%         | 0%       |
| Oracle | 100% | 100%      | 100%     |

Null proves failures do not heal on their own. Oracle (reads the ground truth)
proves every fixable scenario can really be fixed with the available actions.
