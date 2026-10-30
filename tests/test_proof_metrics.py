"""Proof metrics: tool-call ids, evidence validity, false recovery claims."""

import yaml

from harness import citations
from harness.env import Env
from harness.reference_agents import OracleAgent
from harness.runner import run_one
from target.system import TargetSystem

DEV = {s["id"]: s for s in yaml.safe_load(open("scenarios/dev.yaml", encoding="utf-8"))}
SC = DEV["dev_payment_swallow"]


def _env():
    s = TargetSystem(seed=3)
    s.run(120)
    return Env(s, lambda _: None, 600, lambda _: False)


def test_every_call_gets_a_stable_id_returned_with_its_result():
    env = _env()
    a = env.call("logs", level="INFO", limit=3)
    b = env.call("invariants")
    env.deploys()
    assert (a["id"], b["id"], env.last_call_id) == ("T1", "T2", "T3")
    assert [e["id"] for e in env.trace] == ["T1", "T2", "T3"]
    assert env.trace[1]["result"] == b["result"]


def test_support_rules():
    rows = [{"service": "worker", "level": "ERROR", "event": "disk quota exceeded", "n": 7}]
    assert citations.supports(rows, {"match": {"service": "worker", "level": "ERROR"}})
    assert citations.supports(rows, {"match": {"n": {">": 5}}})
    assert citations.supports(rows, {"quote": "Disk quota"})
    assert not citations.supports(rows, {"match": {"service": "sync-job"}})
    assert not citations.supports(rows, {"match": {"n": {">": 9}}})
    assert not citations.supports(rows, {"quote": "connection refused"})
    assert not citations.supports(rows, {})                          # says nothing checkable
    topo = {"workflow": {"depends_on": ["supplier-api"]}}
    assert citations.supports(topo, {"match": {"service": "workflow", "depends_on": {"contains": "supplier-api"}}})


class Citer:
    """Waits for the fault, cites evidence (real, invented, false), then gives up."""

    def __init__(self, evidence_fn, claim, **_):
        self.evidence_fn, self.claim = evidence_fn, claim

    def run(self, env):
        env.wait(400)
        env.declare_incident("x")
        inv = env.call("invariants")
        dep = env.call("deploys")
        env.report_diagnosis([{"service": "payment-service", "kind": "bad_deploy"}], "x",
                             evidence=self.evidence_fn(inv, dep))
        env.call("logs")                                             # T after the diagnosis
        env.finish(recovered=self.claim)


def _run(evidence_fn, claim=None):
    return run_one(SC, "dev", lambda **kw: Citer(evidence_fn, claim, **kw), seed=1)


def test_real_supported_citations_are_valid():
    r = _run(lambda inv, dep: [
        {"id": inv["id"], "match": {"id": "I1_payment_has_order", "violations": {">": 0}}},
        {"id": dep["id"], "match": {"service": "payment-service"}},
    ])
    assert (r.evidence_cited, r.evidence_valid) == (2, 2)


def test_invented_or_unsupported_citations_count_against_the_agent():
    r = _run(lambda inv, dep: [
        {"id": "T999", "match": {"service": "payment-service"}},             # no such call
        {"id": inv["id"], "match": {"id": "I3_ledger_integrity", "violations": {">": 0}}},  # false claim
        {"id": dep["id"], "match": {"service": "payment-service"}},          # real
        inv["id"],                                                           # bare id
        {"id": "T" + str(int(dep["id"][1:]) + 2), "quote": "x"},             # cites a later call
    ])
    assert (r.evidence_cited, r.evidence_valid) == (5, 1)
    assert len(r.invalid_evidence) == 4


def test_claiming_recovery_without_fixing_is_a_false_claim():
    r = _run(lambda inv, dep: [], claim=True)
    assert not r.stable and r.false_recovery_claim and not r.false_failure_claim


def test_honest_failure_claim_is_not_flagged():
    r = _run(lambda inv, dep: [], claim=False)
    assert not r.false_recovery_claim and not r.false_failure_claim


def test_oracle_claims_match_the_harness():
    for sc in DEV.values():
        r = run_one(sc, "dev", OracleAgent, seed=1)
        assert r.recovery_claim == sc["truth"]["fixable"]
        assert not r.false_recovery_claim and not r.false_failure_claim
