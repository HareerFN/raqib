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

## 2026-10-30 · Agent draft parked before the generated held-out set
- An early draft of the repair agent (evidence collectors, ledger, reasoner, agent loop) had
  been written while the plan still put the agent first. It was never committed and never run
  with an LLM on any scenario; it ran only offline (fake model) on two dev and three validation
  scenarios to inspect the evidence it collects.
- When the plan changed to "seal the generated held-out set before the agent exists", the draft
  was moved out of the repository untouched. The held-out mechanisms below were designed
  without referring to it, and it will never be run on `gen_heldout.yaml` before `--final`.

## 2026-10-30 · Scenario generator (Step A)
- Why: 22 hand-written scenarios only varied parameters. A judge can fairly say the agent was
  tested on cases its author wrote. Scenarios are now composed from fault primitives
  (`config/fault_primitives.yaml`, `harness/generator.py`).
- Composer: 1-3 faults (60/30/10%), random services, origins (deploy / transient / external /
  config), parameters and onsets; 0-2 decoy deploys of healthy services; traffic spikes (always
  for faults that need load, sometimes as noise); injected text in invoice data (held-out only).
  Real and decoy deploys draw notes and versions from the same pool, so a note never gives the
  answer. Ground truth and the Oracle fix are derived from mechanism + origin.
- Splits are by mechanism, not by seed. gen-dev and gen-validation use the six dev mechanisms.
  gen-heldout adds conn_leak (3 services), invoice_format_change, duplicate_charge, a config
  change (worker capacity), injected text, two new mechanisms (clock_skew: a service's clock
  runs behind; disk_fill: one service fills the shared disk and others fail to write) and an
  unseen pair (invoice_swap in sync-job instead of invoice-agent). Every held-out scenario holds
  at least one of these.
- Quality filters, on all three benchmark seeds: the Oracle solves it, the shared Monitor detects
  it with no false alarm, a fixable failure does not heal on its own, and in multi-fault
  scenarios each fault is detectable on its own. A test proves the filters reject an
  undetectable fault and a wrong fix.
- Target system: new hooks are service-parametric (leaks and disk use are held per service and
  released by restarting that service; corruption can happen in extraction or in the ledger
  copy). Two read-only tables were added, `db_sessions` and `storage`, like pg_stat_activity
  and df. Proof that nothing old changed: all 180 runs of null, oracle and rule_based on dev,
  validation and (null/oracle only) hand-written held-out give identical results before and
  after the change.
- Oracle: it now acts 45 s after the last fault starts (minimum 60 s after onset, as before).
  Before, it could roll back a deploy that had not happened yet. On the hand-written sets
  this changes only the Oracle's timing on `ho_concurrent_two_faults` (it acts 30 s later);
  every Oracle run still succeeds.
- Before sealing, external-only pairs were given half weight: in a first draft about half of
  gen-dev was unfixable, since one external fault makes a whole scenario unfixable.
- Sealed `scenarios/gen_heldout.yaml` with LF line endings (SHA-256 `d1d36c05...e7c1c9f`, full hash in SEALED.md).
  gen-dev and gen-validation are regenerated by command and are not sealed.

## 2026-10-30 · Generated sets validated, rule-based re-run (3 seeds)
| Agent | gen-dev (60) | gen-validation (60) | gen-heldout (60) |
|-------|--------------|---------------------|------------------|
| Null | 0% | 0% | 0% |
| Oracle | 100% | 100% | 100% |
| Rule-based | 26.7% | 23.3% | not run (sealed) |

Rule-based by number of faults: one fault 52% / 40%, two or three faults 0% / 0%.
Runbooks that scored 100% on the hand-written dev set fail most generated cases: transient
origins (no deploy to roll back), decoy deploys, and every concurrent fault.
