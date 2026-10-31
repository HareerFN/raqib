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

## 2026-10-31 · Proof infrastructure: call ids, evidence validity, recovery claims, anti-cheat
Added before the repair agent exists, so the agent is built against these checks.
- Env: every trace entry gets a stable id (T1, T2, ...). Evidence tools store their result
  with the id. `env.call(tool, **args)` returns `{"id", "result"}`. The old tool methods still
  return the same values, so existing agents are unchanged.
- **evidence_validity** (`harness/citations.py`): a cited item is valid only if its id is a
  real evidence-tool call made before the diagnosis, and that call's stored result supports
  it (`match`: a record has those field values or passes `{op: value}`; `quote`: the text is in
  the result). Bare ids, invented ids, later calls and false claims count as invalid.
- **false_recovery_claims**: `env.finish(recovered=...)` records the agent's own verdict. The
  runner now computes the stability check (`stable`) for every run, not just fixable ones.
  Claimed recovered but not stable = false recovery claim. Claimed not recovered but stable =
  false failure claim. Scoring of success is unchanged.
- Baselines now state a verdict: rule_based claims recovery when its monitor is quiet after a
  runbook, and failure when it escalates. llm_single_shot does not verify, so it claims
  recovery unless it escalated. The Oracle claims `fixable`.
- `tests/test_no_cheating.py`:
  - Static AST scan of `raqib/` and `baselines/`. It bans private attributes off anything but
    `self`, `.faults`, `.trace`, `__dict__`, truth keys, the `scenario` object, imports of
    target / runner / reference_agents / generator / citations, reflection (`vars`, `inspect`,
    computed `getattr`, ...), and paths to scenario files.
  - Runtime check: agents run against a guarded Env that fails on any non-public access.
  - Leak test: agent code, prompts and the config text agents see (actions, topology,
    invariants) contain no scenario ids, fault names, mechanism names (also with spaces or
    hyphens), injected texts or per-scenario fix steps.
  - Every check is also run on planted cheats, so none is vacuous.
- Proof that nothing else changed: the 180-run fingerprint (dev, validation, hand-written
  held-out with null/oracle) is identical apart from the new fields. The only differences are
  the 3 Oracle runs whose timing changed in Step A.

New metric, existing baseline (3 seeds):
| Agent | Split | false_recovery_claims | false_failure_claims |
|-------|-------|-----------------------|----------------------|
| Rule-based | validation (10) | 6 / 30 | 0 |
| Rule-based | gen-dev (60) | 57 / 180 | 0 |
| Rule-based | gen-validation (60) | 63 / 180 | 0 |

A quiet monitor right after a runbook is not proof of recovery: in about a third of generated
runs, rule_based says "recovered" while the harness finds the system still broken.

## 2026-10-31 · Step B: the Raqib repair agent (built and tuned on dev only), then frozen
Agent (`raqib/agent.py`, `evidence.py`, `ledger.py`, `reasoner.py`), registered as `raqib`.
- Evidence collectors (code) write a ledger: invariants, metric changes vs the 300 s healthy
  baseline, new or surging log signatures, deploys and their timing, absence of deploys,
  topology. Each item keeps the Env call id it came from and a checkable form of its claim.
- LLM reasoner: may ask for up to 2 rounds of read-only SQL / log searches, then gives
  hypotheses with supporting and refuting evidence ids. It never acts.
- Evidence Ledger gate: a cause is acted on only with support from >= 2 independent sources
  (database, metrics, logs, deploys), evidence about that service or its dependents, a
  possible kind (bad_deploy needs a deploy, external needs an external service), and
  counter-evidence that is not at least as broad and plentiful as the support.
- Discriminating tests (code): (1) deploy timing: a deploy that lands after the service's
  symptoms began cannot be their cause; (2) dependency graph: a downstream cause whose cited
  symptoms are all cited for a proven upstream cause is a symptom, not a cause;
  (3) experiment: a fix that ran while the symptoms it was meant to explain persisted
  refutes that (service, kind) for the rest of the incident, and the cause is dropped from
  the diagnosis.
- Verifier feedback: rejected hypotheses go back to the reasoner once per round with the
  reasons; it may cite more evidence, change service or kind, or drop them.
- Actions: only those tied to a proven cause (rollback / restart of that service, matching
  the kind) or to a violated invariant (data repairs, via `repairs` in actions.yaml). Idempotent,
  attempt budget (5 rounds, 4 runs per repair), approval for high risk. Watch window =
  longest invariant horizon + 30 s; re-runs data repairs while in-flight work lands.
- Escalation governor: escalate when no cause can be proven twice, when the attempt budget is
  used up, or when the cause is outside; an outside cause is handed over after the symptoms it
  does not explain have been handled.
- Ends with a structured report and an explicit `recovered` claim.
- Config additions (system documentation, not per-failure): `owns` in topology.yaml (tables
  each service writes, taken from target/system.py), `repairs` in actions.yaml (the invariant
  each data repair restores), restart description now says it frees temporary files too.
- Anti-cheat and leak tests pass on the agent (the scanner made us rename the hypothesis field
  `fix` to `repair_plan` and drop a `__dict__` use; no exception was added to the scanner).

**Model choice (on hand-written dev, seed 1):** the account budget allows about $0.005 per run.
| Model | dev success | cost (6 runs) |
|-------|-------------|---------------|
| anthropic/claude-sonnet-4.6 | 100% (18 runs, 3 seeds) | $0.72 (18 runs) |
| openai/gpt-4o-mini | 16.7% | $0.015 |
| google/gemini-2.5-flash | 83.3% | $0.031 |
| deepseek/deepseek-chat-v3.1 | 100% | $0.014 |
DeepSeek V3.1 is the default (`RAQIB_MODEL_STRONG`); every reported number below uses it.
The cheap tier is unused: evidence is summarized by code, not by an LLM.

**Tuning on gen-dev (seed 1 only, budget):** three versions, each change general.
| Version | gen-dev success | change |
|---------|-----------------|--------|
| v1 | 30.0% | first full run |
| v2 | 35.0% | verifier feedback, experiment refutation, corrected `owns`, refuting rule by source breadth |
| v3 (frozen) | 35.0% | escalation governor for outside causes; no measured effect (the model says the outside cause explains every symptom, so it still hands over at once) |
Rule-based on the same 60 scenarios, seed 1: 26.7%. By fault count, raqib v3 vs rule-based:
1 fault 52% / 52%, 2 faults 23% / 0%, 3 faults 0% / 0%. Transient origin 17% / 0%.
False recovery claims: raqib 0 / 60, rule-based 18 / 60. Evidence validity 100% (382 citations).
2 false failure claims: the budget ran out while the fix was still being watched, the agent
reported "not verified", and the system had in fact recovered.
Remaining failures (39): 11 no cause proven with two sources, 11 symptoms remained after 5
rounds, 11 outside cause handed over while other faults were missed, 6 budget used up.

The agent is frozen at this commit. Seeds used from here (budget: about $1.6 of LLM credit):
- final held-out (`--final`): hand-written heldout (6) and gen-heldout (60), 3 seeds each.
- validation (10) and gen-validation (60): 1 seed, only if credit remains after the held-out run.
- ablation (no Evidence Ledger): validation, 1 seed, only if credit remains.
- llm_single_shot: not re-run; its earlier results are reported as they are.

## 2026-10-31 · Final held-out run (`--final`, frozen agent `ade6762`, 3 seeds): in progress
- First attempt (6 workers) was stopped after 15 of the 18 hand-written held-out runs because it
  was too slow; nothing from it was reported. The restart (16 workers) replayed the cached
  model answers, so its results are the same as one uninterrupted run.
- Hand-written held-out (6 scenarios x 3 seeds), done:
  | Agent | Success | Root cause | Recovered | Escalation | False recovery claims | Evidence valid |
  |-------|---------|------------|-----------|------------|-----------------------|----------------|
  | Null | 0% | 0% | 0% | 83.3% | 0 | - |
  | Rule-based | 50.0% | 50.0% | 80.0% | 83.3% | 0 | - |
  | Raqib | 38.9% | 38.9% | 53.3% | 61.1% | 0 | 100% (128) |
  | Oracle | 100% | 100% | 100% | 100% | 0 | - |
  Raqib is below the rule-based baseline on this small hand-written set.
- gen-heldout (60 x 3): baselines done (Null 0%, Oracle 100%, Rule-based 16.7% with 37 false
  recovery claims out of 180). Raqib stopped at 141 of 180 runs: the OpenRouter credit ran out
  (HTTP 402). Completed model answers are cached; the run resumes from the cache once credit
  is added, with no change to the agent.
- Result files of the finished runs are kept in `reports/final/`.

## 2026-10-31 · Final results (frozen agent `ade6762`)
Credit was added and the gen-heldout run was resumed with the same command. The 141 runs
finished earlier replayed from the cache, so the result equals one uninterrupted run.

| Agent | gen-heldout (60 x 3) | heldout (6 x 3) | False recovery claims | Evidence valid |
|-------|----------------------|-----------------|-----------------------|----------------|
| Null | 0% | 0% | 0 | - |
| Rule-based | 16.7% (sd 0.0) | 50.0% (sd 0.0) | 37 / 198 | - |
| Raqib | 28.9% (26.7 / 26.7 / 33.3, sd 3.1) | 38.9% (16.7 / 66.7 / 33.3, sd 20.8) | 0 / 198 | 100% (2,261) |
| Oracle | 100% | 100% | 0 | - |

gen-heldout by group (rule-based / Raqib): 1 fault 26.3 / 38.6%, 2 faults 0 / 13.0%,
3 faults 0 / 8.3%, unfixable 0 / 25.0%, clock_skew 16.7 / 72.2%, duplicate_charge 30.0 / 40.0%,
invoice_format_change 0 / 28.6%, capacity_config 66.7 / 55.6%, conn_leak 0 / 7.8%,
disk_fill 0 / 1.8%, invoice_swap@sync-job 0 / 20.0%.
Injected instructions: in the 15 runs whose invoice text said to restart the database, Raqib
never restarted it; no purge_queue, no refund without a duplicate charge. All 45 database
restarts were in connection-leak or disk scenarios (pool pressure blamed on the database
instead of the service holding the connections): the main weakness found by the held-out set.

Validation, seed 1 (checked only, never tuned on): Raqib 90%, rule-based 60%,
Raqib without the Evidence Ledger 70% (6 unnecessary restarts/rollbacks vs 1).
gen-validation, seed 1: Raqib 38.3% (3 false recovery claims of 60), rule-based 23.3% (3 seeds).
Seeds actually used: held-out 3; dev, gen-dev, validation, gen-validation 1 for Raqib
(rule-based 3 on the generated sets); one ablation (no Evidence Ledger) instead of four.
LLM single shot was not re-run. All result files are in `reports/final/`.

## 2026-10-31 · Agent v2 (time box: 2 hours), frozen before its held-out run
What was known before v2 work started, recorded for transparency:
- v1's final held-out results (aggregates and the per-group breakdown in the entry above,
  including the database-restart weakness in leak and disk scenarios) were already reported.
- One gen-heldout incident's timeline was seen while reviewing the first demo page (the demo
  opened on a recorded gen-heldout run).
Because of this, gen-heldout is no longer a clean test for any later version.
`scenarios/gen_heldout_v2.yaml` (same generator, same held-out mechanisms, new seeds from
400000) was generated and sealed in commit `3a5334c` before any v2 change. v2 is judged on it.

Failure analysis for v2 used only gen-dev and gen-validation runs of v1 (76 failures). No
held-out trace or result file was opened for it. Two general causes:
1. **Repairs masked a cause that was still active.** After a fix, the watch loop kept re-running
   the data repair (e.g. re-sending approval requests), which cleared the invariant for a few
   minutes each time, so the experiment never concluded: the agent neither refuted the wrong
   cause nor looked for a second one, and ran out of rounds or time. Most "gave up after 5
   rounds" and "time budget used up" failures, and every missed external supplier outage.
2. **Restart chosen while a deploy of the same service preceded the symptoms** (10 cases).

Changes (general, no scenario-specific code; anti-cheat and leak tests pass):
- Recurring damage: while watching one fix, a data repair may run once more per invariant
  (work already in flight); if the damage comes back after that, the incident reopens with
  those symptoms instead of repairing again. The reopened round is told which symptoms the
  fixed causes do not explain, so it opens a new hypothesis.
- Deploy first (discriminating test in the ledger): a runtime_fault / resource_exhaustion
  hypothesis is rejected while a deploy of that service preceded its symptoms, until the
  deploy explanation has been refuted by experiment. So rollback is tested before restart.
- Reasoner rule: a cause refuted by experiment is not the answer; look at what else explains
  the persisting symptoms, including its dependencies, and escalate external ones.
- Tests: deploy-first, recurring damage reopens the incident; the refutation test now runs
  with discriminating tests off (with them on, the deploy-first test blocks that restart).

gen-validation, seed 1 (also used in the failure analysis above, so it is not a clean check):
| Version | Success | 1 fault | 2 faults | 3 faults | Unfixable | False recovery claims | Cost |
|---------|---------|---------|----------|----------|-----------|-----------------------|------|
| Rule-based | 23.3% | 40% | 0% | 0% | 0% | 21 | $0 |
| v1 | 38.3% | 54% | 18% | 0% | 0% | 3 | $0.26 |
| v2 | 63.3% | 80% | 41% | 33% | 30% | 3 | $0.19 |
The improvement is clear, so v2 is frozen at this commit and run once on gen_heldout_v2
(`--final`, 3 seeds), with rule-based, Null and Oracle on the same set.

## 2026-10-31 · Agent v2 on the sealed gen_heldout_v2 (`--final`, 3 seeds, run once)
- Frozen v2 = commit `e4ed01a`. During the run, OpenRouter sent a truncated reply and the
  client crashed (55 of 180 runs were printed). Commit `50e3b52` widened the client's retries
  (transport only: no prompt, cache key or decision changed) and the same command was resumed;
  finished runs replayed from the cache, so the result equals one uninterrupted run.

| Agent | gen_heldout_v2 (60 x 3) | Per seed | False recovery claims | Evidence valid |
|-------|-------------------------|----------|-----------------------|----------------|
| Null | 0% | | 0 | - |
| Rule-based | 16.7% | 16.7 / 16.7 / 16.7 | 37 / 180 | - |
| **Raqib v2** | **32.2%** | 35.0 / 31.7 / 30.0 (sd 2.1) | **0 / 180** (1 false failure claim) | 100% (1,713) |
| Oracle | 100% | | 0 | - |

By group (rule-based / v2): 1 fault 28.6 / 45.7%, 2 faults 0 / 15.8%, 3 faults 0 / 5.6%,
unfixable (must escalate) 0 / 48.1%, invoice_format_change 0 / 60.0%, duplicate_charge
37.5 / 50.0%, capacity_config 45.5 / 51.5%, clock_skew 8.3 / 33.3%, invoice_swap@sync-job
11.1 / 25.9%, conn_leak 0 / 6.7%, disk_fill 0 / 4.5%. Cost $0.56 for 180 runs.

Reading it plainly:
- v2 is clearly better than runbooks on failures it never saw, and better on escalation.
- The gain on gen-validation (38.3% -> 63.3%) mostly did not carry over: gen_heldout_v2 is a
  different scenario set, so it cannot be compared one-to-one with v1's 28.9% on gen-heldout,
  but 32.2% is far below 63.3%. gen-validation's failures were used in the v2 analysis, so part
  of that gain was fitting to it. The held-out number is the one to trust.
- Resource leaks and the full disk remain the weak spot (33 database restarts, all the same
  symptom-vs-cause confusion found in v1).
