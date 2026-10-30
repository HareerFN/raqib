# Evaluation log

Every change to the benchmark itself (scenarios, scoring, rubric) is recorded here
with its reason, so results stay comparable and nothing is tuned silently.

## 2026-10-30 · Harness validated
- Null agent: 0% on dev, validation, held-out. Failures do not heal on their own.
- Oracle agent: 100% on all splits. Every fixable scenario is fixable with the available actions.
- Found while building: a refund issued while a duplicate webhook was still in flight
  was undone moments later. Lesson for the agent: fix, wait, verify, repair again.

## 2026-10-30 · Held-out sealed
- `scenarios/heldout.yaml` SHA-256 `8b378896...fa57a8`, committed before any repair agent code.

## 2026-10-30 · Harder validation scenarios added
- Rule-based scored 100% on validation: the set only varied parameters of dev families,
  so it could not show the value of reasoning. Added 4 harder variations of dev families
  (a decoy deploy before detection, two dev faults at once, a stuck worker with no deploy,
  an external supplier outage). No held-out family was used.
- The decoy deploy in `val_decoy_after_fault` was moved from +40 s to +10 s so it lands
  before detection, which is what the scenario is meant to test.

## 2026-10-30 · Baselines on validation (3 seeds)
| Agent | Success | Root cause | Recovery | Escalation | Cost |
|-------|---------|------------|----------|------------|------|
| Rule-based runbooks | 60% | | | | $0 |
| LLM single-shot | 60% | 60% | 87.5% | 93.3% | $0.19 |

Both fail on two concurrent faults, a stuck worker without a deploy, and an external
outage. LLM single-shot handles the decoy deploy better than runbooks (2 of 3).

## 2026-10-30 · Rubric fix: added `runtime_fault` kind
- Reviewing failed runs showed the root-cause taxonomy had no category for a process
  stuck in a bad state that a restart fixes, so `val_stuck_worker_no_deploy` had no
  fully correct answer. Added `runtime_fault` to the taxonomy for every agent, and the
  scenario now accepts `runtime_fault` or `resource_exhaustion`.
- Other failures were reviewed and judged fairly scored (extra wrong causes,
  missed second fault, blaming the symptom service instead of the external dependency).
- Held-out file unchanged (hash verified). Baselines re-run after this change.
