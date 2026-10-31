"""Raqib agent: the evidence gate and the act/verify loop, with a fake model (no network)."""

import json
import re

import yaml

from harness.env import TOPOLOGY
from harness.runner import run_one
from raqib.agent import RaqibAgent
from raqib.ledger import Ledger
from raqib.llm import FakeLLM

DEV = {s["id"]: s for s in yaml.safe_load(open("scenarios/dev.yaml", encoding="utf-8"))}


def _ledger():
    led = Ledger(TOPOLOGY)
    led.add("database", "invariant", ["payment-service", "order-service"], "I1 violated")   # E1
    led.add("deploys", "deploy", ["payment-service"], "deploy payment-service v1.1")         # E2
    led.add("deploys", "deploy", ["sync-job"], "deploy sync-job v1.0.2")                     # E3
    led.add("database", "invariant", ["workflow"], "I4 violated")                            # E4
    led.add("deploys", "no_deploy", ["workflow"], "no deploy of workflow")                   # E5
    return led


def test_ledger_needs_two_independent_sources():
    led = _ledger()
    assert led.assess({"service": "payment-service", "kind": "bad_deploy", "supporting": ["E1", "E2"]}).proven
    assert not led.assess({"service": "payment-service", "kind": "bad_deploy", "supporting": ["E1"]}).proven


def test_ledger_rejects_impossible_or_unrelated_causes():
    led = _ledger()
    # a bad deploy with no deploy of that service
    assert not led.assess({"service": "workflow", "kind": "bad_deploy", "supporting": ["E4", "E5"]}).proven
    # another service's deploy is not evidence, and invented ids do not count
    v = led.assess({"service": "payment-service", "kind": "bad_deploy", "supporting": ["E1", "E3", "E99"]})
    assert not v.proven and len(v.problems) >= 2
    # an external cause must be an external service; evidence from its dependents counts
    assert not led.assess({"service": "workflow", "kind": "external_dependency", "supporting": ["E4", "E5"]}).proven
    assert led.assess({"service": "supplier-api", "kind": "external_dependency", "supporting": ["E4", "E5"]}).proven


def test_ledger_refuting_evidence_can_block_a_fix():
    led = _ledger()
    assert not led.assess({"service": "payment-service", "kind": "bad_deploy",
                           "supporting": ["E1", "E2"], "refuting": ["E4", "E5"]}).proven


def _ids(text: str, pattern: str) -> list[str]:
    return re.findall(rf"^(E\d+) .*{pattern}", text, flags=re.M)


def _responder(fix, supporting_patterns):
    def respond(messages):
        text = messages[1]["content"]
        support = [i for p in supporting_patterns for i in _ids(text, p)]
        return json.dumps({"investigate": [], "summary": "test", "hypotheses": [
            {"service": "payment-service", "kind": "bad_deploy", "mechanism": "webhook handler drops confirmations",
             "supporting": support, "refuting": [], "explains": ["I1_payment_has_order"], "repair_plan": fix}]})
    return respond


def _run(responder, scenario="dev_payment_swallow", **flags):
    fake = FakeLLM(responder)
    agent_box = {}

    def factory(**kw):
        agent_box["a"] = RaqibAgent(llm=fake, **flags, **kw)
        return agent_box["a"]
    return run_one(DEV[scenario], "dev", factory, seed=1), agent_box["a"]


def test_proven_cause_is_fixed_and_verified():
    fix = [{"action": "rollback_service", "params": {"service": "payment-service"}}]   # repair left out on purpose
    r, agent = _run(_responder(fix, [r"invariant I1_payment_has_order violated", r"deploy payment-service v1\.1"]))
    assert r.success, r
    names = [a["action"] for a in r.actions]
    assert names[0] == "rollback_service" and "replay_payment_confirmations" in names
    assert any(e["event"] == "stable" for e in agent.timeline)


def test_weak_evidence_is_escalated_not_acted_on():
    fix = [{"action": "rollback_service", "params": {"service": "payment-service"}}]
    r, _ = _run(_responder(fix, [r"invariant I1_payment_has_order violated"]))
    assert r.actions == [] and r.escalated


def test_destructive_or_unrelated_actions_are_refused():
    fix = [{"action": "purge_queue", "params": {}},
           {"action": "rollback_service", "params": {"service": "worker"}},
           {"action": "rollback_service", "params": {"service": "payment-service"}}]
    r, agent = _run(_responder(fix, [r"invariant I1_payment_has_order violated", r"deploy payment-service v1\.1"]))
    assert "purge_queue" not in [a["action"] for a in r.actions]
    assert all(a["params"].get("service") in (None, "payment-service") for a in r.actions)
    assert {e["action"] for e in agent.timeline if e["event"] == "refused"} == {"purge_queue", "rollback_service"}
    assert r.success


def test_report_cites_real_evidence_and_claims_recovery():
    fix = [{"action": "rollback_service", "params": {"service": "payment-service"}}]
    r, agent = _run(_responder(fix, [r"invariant I1_payment_has_order violated", r"deploy payment-service v1\.1"]))
    assert r.evidence_cited >= 2 and r.evidence_valid == r.evidence_cited, r.invalid_evidence
    assert r.recovery_claim is True and r.stable and not r.false_recovery_claim
    report = next(e for e in agent.timeline if e["event"] == "report")
    assert report["recovered"] and report["root_causes"] and report["evidence"]


def test_escalation_claims_no_recovery():
    fix = [{"action": "rollback_service", "params": {"service": "payment-service"}}]
    r, _ = _run(_responder(fix, [r"invariant I1_payment_has_order violated"]))
    assert r.recovery_claim is False and not r.false_failure_claim


def test_timing_test_rejects_a_deploy_after_the_symptoms():
    led = Ledger(TOPOLOGY)
    led.add("logs", "log_signature", ["worker"], "ERROR worker ...", ts=500)          # E1
    led.add("metrics", "metric", ["worker"], "messages_dead rate rose", ts=510)       # E2
    led.add("deploys", "deploy", ["worker"], "deploy worker v1.2", ts=620)            # E3: a decoy after the fault
    h = {"service": "worker", "kind": "bad_deploy", "supporting": ["E1", "E2", "E3"]}
    v = led.assess(h)
    assert not v.proven and any(p.startswith("timing") for p in v.problems)
    assert Ledger(TOPOLOGY, discriminate=False).assess(h) is not None
    led.items["E3"].ts = 480                                                         # deploy before the symptoms
    assert led.assess(h).proven


def test_downstream_symptoms_are_explained_by_the_upstream_cause():
    led = Ledger(TOPOLOGY)
    led.add("logs", "log_signature", ["payment-service"], "ERROR provider request failed", ts=500)   # E1
    led.add("metrics", "metric", ["payment-service"], "errors.payment-service rate rose", ts=500)   # E2
    led.add("deploys", "deploy", ["payment-service"], "deploy payment-service", ts=470)             # E3
    up = led.assess({"service": "payment-provider", "kind": "external_dependency", "supporting": ["E1", "E2"]})
    down = led.assess({"service": "payment-service", "kind": "bad_deploy", "supporting": ["E1", "E2", "E3"]})
    assert up.proven and down.proven
    led.explained_upstream([up, down])
    assert up.proven and not down.proven
    # a downstream cause with a symptom of its own survives
    led.add("database", "invariant", ["payment-service"], "I5 violated", ts=505)                   # E4
    down2 = led.assess({"service": "payment-service", "kind": "bad_deploy", "supporting": ["E1", "E3", "E4"]})
    led.explained_upstream([up, down2])
    assert down2.proven


def test_a_fix_that_changes_nothing_refutes_its_cause():
    # the model insists on a restart; the real cause is a deploy, so the symptoms persist
    def respond(messages):
        text = "\n".join(m["content"] for m in messages if m["role"] == "user")
        support = _ids(text, r"invariant I1_payment_has_order violated") + _ids(text, r"deploy payment-service v1\.1")
        return json.dumps({"investigate": [], "summary": "t", "hypotheses": [
            {"service": "payment-service", "kind": "runtime_fault", "mechanism": "stuck",
             "supporting": support[:2], "refuting": [], "explains": ["I1_payment_has_order"],
             "repair_plan": [{"action": "restart_service", "params": {"service": "payment-service"}}]}]})
    r, agent = _run(respond, discriminate=False)     # without the deploy-first test, so the restart runs
    events = [e["event"] for e in agent.timeline]
    assert "refuted" in events
    restarts = [a for a in r.actions if a["action"] == "restart_service"]
    assert len(restarts) == 1                      # never repeats the refuted fix
    assert r.escalated and r.recovery_claim is False


def test_deploy_first_blocks_a_restart_until_the_deploy_is_tested():
    led = Ledger(TOPOLOGY)
    led.add("database", "invariant", ["payment-service"], "I1 violated", ts=520)            # E1
    led.add("deploys", "deploy", ["payment-service"], "deploy payment-service", ts=500)     # E2
    led.add("logs", "log_signature", ["payment-service"], "WARN ...", ts=525)              # E3
    h = {"service": "payment-service", "kind": "runtime_fault", "supporting": ["E1", "E3"]}
    v = led.assess(h)
    led.deploy_first([v], refuted=set())
    assert not v.proven and any(p.startswith("deploy first") for p in v.problems)
    v2 = led.assess(h)
    led.deploy_first([v2], refuted={("payment-service", "bad_deploy")})   # rollback was tried and failed
    assert v2.proven


def test_damage_that_keeps_coming_back_reopens_the_incident():
    # the model blames the right service with the wrong fix; repairs alone would hide that
    def respond(messages):
        text = "\n".join(m["content"] for m in messages if m["role"] == "user")
        support = _ids(text, r"invariant I1_payment_has_order violated") + _ids(text, r"deploy payment-service v1\.1")
        return json.dumps({"investigate": [], "summary": "t", "hypotheses": [
            {"service": "payment-service", "kind": "runtime_fault", "mechanism": "stuck",
             "supporting": support[:2], "refuting": [], "explains": ["I1_payment_has_order"],
             "repair_plan": [{"action": "restart_service", "params": {"service": "payment-service"}}]}]})
    r, agent = _run(respond, discriminate=False)
    events = [e["event"] for e in agent.timeline]
    assert "recurring_damage" in events and "refuted" in events
    replays = [a for a in r.actions if a["action"] == "replay_payment_confirmations"]
    assert len(replays) <= 3                      # it stops masking the symptom
